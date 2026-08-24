import asyncio
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
