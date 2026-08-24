import asyncio
import contextlib
import importlib.util
import io
import json
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "omadesk-daemon.py"
SPEC = importlib.util.spec_from_file_location("omadesk_daemon", MODULE_PATH)
assert SPEC and SPEC.loader
DAEMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DAEMON)


class FakeWriter:
    def __init__(self) -> None:
        self.output = bytearray()

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class DaemonSecurityTests(unittest.TestCase):
    def tearDown(self) -> None:
        lock_file = DAEMON._daemon_lock_file
        if lock_file is not None:
            lock_file.close()
            DAEMON._daemon_lock_file = None

    def test_config_is_replaced_atomically_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            previous = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = root
            try:
                config = {"mac": "AA:BB", "presets": {"sit": 73}}
                DAEMON.save_config(config)
                path = Path(root) / "omarchy" / "omadesk" / "config.json"

                self.assertEqual(json.loads(path.read_text()), config)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(list(path.parent.glob(".config.*")), [])
            finally:
                if previous is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = previous

    def test_oversized_socket_request_is_rejected(self) -> None:
        async def exercise() -> dict:
            daemon = DAEMON.DeskDaemon(dict(DAEMON.DEFAULT_CONFIG))
            reader = asyncio.StreamReader()
            writer = FakeWriter()
            reader.feed_data(b"x" * (DAEMON.MAX_REQUEST_BYTES + 1))
            reader.feed_eof()

            await daemon.handle_client(reader, writer)
            return json.loads(writer.output.decode())

        self.assertEqual(
            asyncio.run(exercise()),
            {"ok": False, "error": "request too large"},
        )

    def test_preset_move_runs_when_displayed_height_differs_by_two_millimetres(self) -> None:
        self.assertFalse(DAEMON.at_displayed_height(0.742, 0.74))
        self.assertTrue(DAEMON.at_displayed_height(0.740, 0.74))
        self.assertTrue(DAEMON.at_displayed_height(0.7404, 0.74))

    def test_height_protocol_decodes_height_and_signed_speed(self) -> None:
        raw = struct.pack("<Hh", 1234, -25)

        self.assertEqual(DAEMON.parse_height(raw), (0.7434, -0.0025))

    def test_height_protocol_rejects_truncated_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected 4 bytes"):
            DAEMON.parse_height(b"\x00\x01\x02")

    def test_target_height_encoding_uses_linak_base_height(self) -> None:
        self.assertEqual(DAEMON.meters_to_bytes(DAEMON.MIN_HEIGHT_M), b"\x00\x00")
        self.assertEqual(DAEMON.meters_to_bytes(0.74), struct.pack("<H", 1200))

    def test_busy_bluez_errors_are_recognized(self) -> None:
        for message in (
            "Operation already in progress",
            "br-connection-canceled",
            "Device is already connected",
        ):
            with self.subTest(message=message):
                self.assertTrue(DAEMON.is_busy_bluez_error(RuntimeError(message)))
        self.assertFalse(DAEMON.is_busy_bluez_error(RuntimeError("not authorized")))

    def test_desk_detection_accepts_service_uuid_or_desk_name(self) -> None:
        named = SimpleNamespace(name="Desk 1234", address="AA")
        unnamed = SimpleNamespace(name=None, address="BB")
        service = SimpleNamespace(service_uuids=[DAEMON.UUID_ADV_SVC.upper()], local_name=None)
        unrelated = SimpleNamespace(service_uuids=[], local_name="Lamp")

        self.assertTrue(DAEMON.is_linak_desk(named, unrelated))
        self.assertTrue(DAEMON.is_linak_desk(unnamed, service))
        self.assertFalse(DAEMON.is_linak_desk(unnamed, unrelated))

    def test_device_selection_prefers_mac_then_name(self) -> None:
        first = SimpleNamespace(name="Desk Alpha", address="AA:BB")
        second = SimpleNamespace(name="Desk Beta", address="CC:DD")
        advertisement = SimpleNamespace(local_name=None)
        hits = [(first, advertisement), (second, advertisement)]

        self.assertIs(
            DAEMON.pick_scanned_device(hits, {"mac": "cc:dd", "name": "Desk Alpha"}),
            second,
        )
        self.assertIs(
            DAEMON.pick_scanned_device(hits, {"mac": "", "name": "Desk Alpha"}),
            first,
        )
        self.assertIsNone(DAEMON.pick_scanned_device(hits, {}))
        self.assertIs(DAEMON.pick_scanned_device(hits[:1], {}), first)

    def test_invalid_config_falls_back_to_independent_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ):
            path = Path(root) / "omarchy" / "omadesk" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text("not json")

            loaded = DAEMON.load_config()
            loaded["presets"]["sit"] = 99

            self.assertEqual(DAEMON.load_config()["presets"]["sit"], 73)

    def test_atomic_config_failure_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": root}
        ), mock.patch.object(DAEMON.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                DAEMON.save_config({"mac": "AA"})

            directory = Path(root) / "omarchy" / "omadesk"
            self.assertEqual(list(directory.glob(".config.*")), [])
            self.assertFalse((directory / "config.json").exists())

    def test_daemon_lock_is_exclusive_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": root}
        ):
            self.assertTrue(DAEMON.acquire_daemon_lock())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(DAEMON.acquire_daemon_lock())

            path = Path(root) / "omarchy" / "omadesk.pid"
            self.assertEqual(path.read_text(), str(os.getpid()))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_invalid_json_receives_error_without_dispatch(self) -> None:
        async def exercise() -> dict:
            daemon = DAEMON.DeskDaemon(dict(DAEMON.DEFAULT_CONFIG))
            reader = asyncio.StreamReader()
            writer = FakeWriter()
            reader.feed_data(b"{broken}\n")
            reader.feed_eof()

            await daemon.handle_client(reader, writer)
            return json.loads(writer.output.decode())

        response = asyncio.run(exercise())
        self.assertFalse(response["ok"])
        self.assertIn("invalid json", response["error"])

    def test_move_command_rejects_height_outside_safe_range(self) -> None:
        async def exercise() -> dict:
            daemon = DAEMON.DeskDaemon(dict(DAEMON.DEFAULT_CONFIG))
            daemon.desk.connected = True
            return await daemon.handle_command(
                {"id": 7, "cmd": "move", "height_cm": 200}
            )

        response = asyncio.run(exercise())
        self.assertEqual(response["id"], 7)
        self.assertFalse(response["ok"])
        self.assertIn("out of range", response["error"])

    def test_save_preset_rounds_and_persists_height(self) -> None:
        async def exercise() -> tuple[dict, dict]:
            config = dict(DAEMON.DEFAULT_CONFIG)
            config["presets"] = dict(config["presets"])
            daemon = DAEMON.DeskDaemon(config)
            daemon.desk.connected = True
            with mock.patch.object(DAEMON, "save_config") as save:
                response = await daemon.handle_command(
                    {"id": 8, "cmd": "save_preset", "name": "focus", "height_cm": 88.26}
                )
                save.assert_called_once_with(config)
            return response, config

        response, config = asyncio.run(exercise())
        self.assertTrue(response["ok"])
        self.assertEqual(response["presets"]["focus"], 88.3)
        self.assertEqual(config["presets"]["focus"], 88.3)


if __name__ == "__main__":
    unittest.main()
