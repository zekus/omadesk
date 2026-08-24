#!/usr/bin/env python3
"""Omadesk BLE daemon for LINAK / IKEA IDÅSEN desks."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import struct
import sys
import time
from pathlib import Path
from typing import Any

from bleak import BleakClient

UUID_ADV_SVC = "99fa0001-338a-1024-8a49-009c0215f78a"
UUID_COMMAND = "99fa0002-338a-1024-8a49-009c0215f78a"
UUID_HEIGHT = "99fa0021-338a-1024-8a49-009c0215f78a"
UUID_REFERENCE_INPUT = "99fa0031-338a-1024-8a49-009c0215f78a"

UUID_DPG = "99fa0011-338a-1024-8a49-009c0215f78a"

COMMAND_WAKEUP = bytearray([0xFE, 0x00])
COMMAND_UP = bytearray([0x47, 0x00])
COMMAND_DOWN = bytearray([0x46, 0x00])
COMMAND_STOP = bytearray([0xFF, 0x00])
COMMAND_REFERENCE_INPUT_STOP = bytearray([0x01, 0x80])

MIN_HEIGHT_M = 0.62
MAX_HEIGHT_M = 1.27
MOVE_TOLERANCE_M = 0.005
MOVE_LOOP_INTERVAL = 0.1
MOVE_CONSECUTIVE_ZERO_SPEED = 2
MOVE_MAX_STALL_RETRIES = 3
DIRECTION_REPEAT_INTERVAL = 0.35
DIRECTION_SAFETY_SEC = 6.0

DEFAULT_CONFIG: dict[str, Any] = {
    "mac": "",
    "name": "",
    "presets": {"sit": 73, "stand": 110},
    "pollIntervalSec": 2,
}


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy" / "omadesk"


def state_dir() -> Path:
    path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "omarchy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config() -> dict[str, Any]:
    path = config_dir() / "config.json"
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data if isinstance(data, dict) else {})
    if not isinstance(merged.get("presets"), dict):
        merged["presets"] = dict(DEFAULT_CONFIG["presets"])
    return merged


def pid_path() -> Path:
    return state_dir() / "omadesk.pid"


def read_pid() -> int | None:
    path = pid_path()
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_daemon_lock() -> bool:
    existing = read_pid()
    if existing and pid_alive(existing):
        print(f"omadesk already running (pid {existing})", file=sys.stderr, flush=True)
        return False
    pid_path().write_text(str(os.getpid()))
    return True


def save_config(config: dict[str, Any]) -> None:
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps(config, indent=2) + "\n")


def meters_to_cm(value: float) -> float:
    return round(value * 100, 1)


def cm_to_meters(value: float) -> float:
    return value / 100.0


def parse_height(raw: bytes | bytearray) -> tuple[float, float]:
    if len(raw) < 4:
        raise ValueError(f"expected 4 bytes, got {len(raw)}")
    int_raw, speed_raw = struct.unpack("<Hh", raw[:4])
    height = int_raw / 10000 + MIN_HEIGHT_M
    speed = speed_raw / 10000
    return height, speed


def meters_to_bytes(meters: float) -> bytearray:
    int_raw = int((meters - MIN_HEIGHT_M) * 10000)
    return bytearray(struct.pack("<H", int_raw))


async def release_bluez_connection(mac: str) -> None:
    if not mac:
        return
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", "disconnect", mac,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    await asyncio.sleep(0.5)


def _adv_name(device, adv) -> str:
    return (device.name or (adv.local_name if adv else "") or "").strip()


def is_linak_desk(device, adv) -> bool:
    uuids = [str(uuid).lower() for uuid in (adv.service_uuids if adv else [])]
    name = _adv_name(device, adv)
    return UUID_ADV_SVC.lower() in uuids or name.startswith("Desk")


def desk_hit(device, adv) -> dict[str, Any]:
    name = _adv_name(device, adv)
    rssi = getattr(adv, "rssi", None) if adv is not None else None
    return {
        "mac": device.address,
        "name": name or "Desk",
        "rssi": rssi,
    }


async def scan_desk_advertisements(timeout: float = 8.0) -> list[tuple[Any, Any]]:
    from bleak import BleakScanner

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hits: list[tuple[Any, Any]] = []
    for device, adv in discovered.values():
        if is_linak_desk(device, adv):
            hits.append((device, adv))
    hits.sort(key=lambda item: (desk_hit(*item)["name"], item[0].address))
    return hits


def scan_hits_payload(hits: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [desk_hit(device, adv) for device, adv in hits]


def pick_scanned_device(hits: list[tuple[Any, Any]], config: dict[str, Any]):
    if not hits:
        return None
    preferred_mac = str(config.get("mac", "")).strip().upper()
    preferred_name = str(config.get("name", "")).strip()
    if preferred_mac:
        for device, adv in hits:
            if device.address.upper() == preferred_mac:
                return device
    if preferred_name:
        for device, adv in hits:
            if _adv_name(device, adv) == preferred_name:
                return device
    if len(hits) == 1:
        return hits[0][0]
    return None


async def find_desk_device(config: dict[str, Any]):
    preferred_mac = str(config.get("mac", "")).strip()
    hits = await scan_desk_advertisements(15.0)
    device = pick_scanned_device(hits, config)
    if device is None and preferred_mac:
        await release_bluez_connection(preferred_mac)
        hits = await scan_desk_advertisements(10.0)
        device = pick_scanned_device(hits, config)
    if device is None:
        if not hits:
            raise RuntimeError("No Desk device found. Pair it in Bluetooth settings, then scan.")
        raise RuntimeError(
            f"Found {len(hits)} desks. Select one in the panel."
        )

    config["mac"] = device.address
    if device.name:
        config["name"] = device.name
    save_config(config)
    return device


class DeskController:
    def __init__(self, mac: str, config: dict[str, Any] | None = None) -> None:
        self.mac = mac
        self.config = config or {"mac": mac, "name": ""}
        self.client: BleakClient | None = None
        self.connected = False
        self.moving = False
        self.height_m = 0.0
        self.speed_m = 0.0
        self._move_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None
        self._direction: str | None = None
        self.motion_gen: int = 0

    async def connect(self) -> None:
        if self.client and self.client.is_connected:
            self.connected = True
            return
        if self._connect_task and not self._connect_task.done():
            await self._connect_task
            return

        async def do_connect() -> None:
            last_error: Exception | None = None
            for attempt in range(5):
                try:
                    if self.client and self.client.is_connected:
                        break
                    device = await find_desk_device(self.config)
                    self.mac = device.address
                    if attempt == 0 and self.mac:
                        await release_bluez_connection(self.mac)
                    self.client = BleakClient(device, timeout=20)
                    await self.client.connect()
                    await self.wakeup()
                    self.height_m, self.speed_m = await self.read_height()
                    self.connected = True
                    return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if self.client and self.client.is_connected:
                        try:
                            await self.client.disconnect()
                        except Exception:
                            pass
                    self.client = None
                    await asyncio.sleep(0.5 * (attempt + 1))
            self.connected = False
            if last_error is not None:
                raise last_error

        self._connect_task = asyncio.create_task(do_connect())
        try:
            await self._connect_task
        finally:
            self._connect_task = None

    async def disconnect(self) -> None:
        self.moving = False
        if self._move_task and not self._move_task.done():
            self._move_task.cancel()
            try:
                await self._move_task
            except asyncio.CancelledError:
                pass
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.connected = False

    async def wakeup(self) -> None:
        assert self.client is not None
        await self.client.write_gatt_char(UUID_DPG, b"\x7f\x86\x00")
        await self.client.write_gatt_char(
            UUID_DPG,
            b"\x7f\x86\x80\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11",
        )
        await self.client.write_gatt_char(UUID_COMMAND, COMMAND_WAKEUP)

    async def read_height(self) -> tuple[float, float]:
        assert self.client is not None
        raw = await self.client.read_gatt_char(UUID_HEIGHT)
        return parse_height(raw)

    async def _write_command(self, payload: bytearray) -> None:
        if not self.client or not self.client.is_connected:
            raise RuntimeError("desk is not connected")
        await self.client.write_gatt_char(UUID_COMMAND, payload, response=False)

    async def _send_stop_bytes(self) -> None:
        if not self.client or not self.client.is_connected:
            return
        try:
            await self.client.write_gatt_char(UUID_COMMAND, COMMAND_STOP, response=False)
        except Exception:
            pass
        try:
            await self.client.write_gatt_char(
                UUID_REFERENCE_INPUT, COMMAND_REFERENCE_INPUT_STOP, response=False
            )
        except Exception:
            pass
        try:
            await self.client.write_gatt_char(UUID_COMMAND, COMMAND_STOP, response=False)
        except Exception:
            pass

    def _cancel_move_task(self) -> None:
        task = self._move_task
        self._move_task = None
        if task and not task.done():
            task.cancel()

    async def stop(self) -> None:
        self.motion_gen += 1
        self.moving = False
        self._direction = None
        self._cancel_move_task()
        await self._send_stop_bytes()

    async def start_direction(self, direction: str) -> None:
        if direction not in ("up", "down"):
            raise ValueError(f"unknown direction: {direction}")
        await self.stop()
        gen = self.motion_gen
        command = COMMAND_UP if direction == "up" else COMMAND_DOWN
        self.moving = True
        self._direction = direction
        await self._write_command(command)
        if self.motion_gen != gen:
            self.moving = False
            self._direction = None
            await self._send_stop_bytes()
            return

        async def keep_going() -> None:
            deadline = time.monotonic() + DIRECTION_SAFETY_SEC
            try:
                while (
                    self.moving
                    and self._direction == direction
                    and self.motion_gen == gen
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(DIRECTION_REPEAT_INTERVAL)
                    if (
                        not self.moving
                        or self._direction != direction
                        or self.motion_gen != gen
                    ):
                        return
                    await self._write_command(command)
            except asyncio.CancelledError:
                return
            finally:
                if self.motion_gen == gen and self._direction == direction:
                    self.moving = False
                    self._direction = None
                    await self._send_stop_bytes()

        self._move_task = asyncio.create_task(keep_going())

    async def move_up(self) -> None:
        await self.start_direction("up")

    async def move_down(self) -> None:
        await self.start_direction("down")

    async def move_to(self, target_m: float) -> None:
        if target_m > MAX_HEIGHT_M or target_m < MIN_HEIGHT_M:
            raise ValueError(f"target {meters_to_cm(target_m)} cm out of range")
        await self.stop()
        self.moving = True

        async def do_move() -> None:
            assert self.client is not None
            try:
                current, _ = await self.read_height()
                if abs(current - target_m) < MOVE_TOLERANCE_M:
                    return
                await self.client.write_gatt_char(UUID_COMMAND, COMMAND_WAKEUP)
                await self.client.write_gatt_char(UUID_COMMAND, COMMAND_STOP)
                payload = meters_to_bytes(target_m)
                consecutive_zero = 0
                stall_retries = 0
                while self.moving:
                    await self.client.write_gatt_char(UUID_REFERENCE_INPUT, payload)
                    await asyncio.sleep(MOVE_LOOP_INTERVAL)
                    height, speed = await self.read_height()
                    self.height_m, self.speed_m = height, speed
                    if speed == 0:
                        consecutive_zero += 1
                    else:
                        consecutive_zero = 0
                        stall_retries = 0
                    if consecutive_zero >= MOVE_CONSECUTIVE_ZERO_SPEED:
                        if abs(height - target_m) < MOVE_TOLERANCE_M:
                            break
                        stall_retries += 1
                        if stall_retries >= MOVE_MAX_STALL_RETRIES:
                            break
                        consecutive_zero = 0
            except asyncio.CancelledError:
                return
            finally:
                self.moving = False
                await self._send_stop_bytes()
                try:
                    self.height_m, self.speed_m = await self.read_height()
                except Exception:
                    pass

        self._move_task = asyncio.create_task(do_move())

    async def start_notify(self, callback) -> None:
        assert self.client is not None

        def listener(_char, data: bytearray) -> None:
            try:
                height, speed = parse_height(data)
            except ValueError:
                return
            self.height_m, self.speed_m = height, speed
            callback(height, speed)

        await self.client.start_notify(UUID_HEIGHT, listener)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "moving": self.moving,
            "height_cm": meters_to_cm(self.height_m),
            "speed_mps": round(self.speed_m, 4),
            "min_cm": meters_to_cm(MIN_HEIGHT_M),
            "max_cm": meters_to_cm(MAX_HEIGHT_M),
            "mac": self.mac,
            "name": str(self.config.get("name", "") or ""),
        }


class DeskDaemon:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.desk = DeskController(str(config.get("mac", "")), config)
        self.clients: set[asyncio.StreamWriter] = set()
        self._stop = asyncio.Event()
        self._notify_started = False

    async def broadcast(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload) + "\n"
        dead: list[asyncio.StreamWriter] = []
        for writer in self.clients:
            try:
                writer.write(line.encode())
                await writer.drain()
            except OSError:
                dead.append(writer)
        for writer in dead:
            self.clients.discard(writer)

    def on_height(self, height: float, speed: float) -> None:
        asyncio.create_task(
            self.broadcast(
                {
                    "event": "height",
                    "connected": self.desk.connected,
                    "height_cm": meters_to_cm(height),
                    "speed_mps": round(speed, 4),
                    "moving": self.desk.moving or abs(speed) > 0.0001,
                }
            )
        )

    async def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        cmd = request.get("cmd", "")
        base = {"id": req_id, "ok": True}

        try:
            if cmd == "stop":
                await self.desk.stop()
                return {**base, **self.desk.status()}

            if cmd == "scan":
                hits = await scan_desk_advertisements(8.0)
                return {
                    **base,
                    "event": "scan",
                    "devices": scan_hits_payload(hits),
                    **self.desk.status(),
                }

            if cmd == "select":
                mac = str(request.get("mac", "")).strip()
                if not mac:
                    raise ValueError("mac required")
                name = str(request.get("name", "")).strip()
                async with self.desk._lock:
                    await self.desk.disconnect()
                    self._notify_started = False
                    self.config["mac"] = mac
                    if name:
                        self.config["name"] = name
                    save_config(self.config)
                    self.desk.mac = mac
                    await asyncio.wait_for(self.desk.connect(), timeout=25)
                    if self.desk.connected:
                        await self.desk.start_notify(self.on_height)
                        self._notify_started = True
                    return {**base, "event": "select", **self.desk.status()}

            motion_cmds = ("up", "down", "move", "goto_preset")
            seq = self.desk.motion_gen
            async with self.desk._lock:
                if cmd in motion_cmds and self.desk.motion_gen != seq:
                    return {**base, **self.desk.status()}
                if cmd == "status":
                    if self.desk.connected:
                        try:
                            self.desk.height_m, self.desk.speed_m = await asyncio.wait_for(
                                self.desk.read_height(), timeout=3
                            )
                        except Exception:
                            pass
                    return {**base, **self.desk.status()}

                if cmd == "connect":
                    await asyncio.wait_for(self.desk.connect(), timeout=25)
                    return {**base, **self.desk.status()}

                if cmd == "disconnect":
                    await self.desk.disconnect()
                    return {**base, **self.desk.status()}

                if not self.desk.connected:
                    await self.desk.connect()

                if cmd == "up":
                    await self.desk.move_up()
                elif cmd == "down":
                    await self.desk.move_down()
                elif cmd == "move":
                    target = cm_to_meters(float(request["height_cm"]))
                    await self.desk.move_to(target)
                elif cmd == "presets":
                    return {**base, "presets": self.config.get("presets", {})}
                elif cmd == "save_preset":
                    name = str(request.get("name", "")).strip()
                    if not name:
                        raise ValueError("preset name required")
                    height_cm = float(request["height_cm"])
                    presets = dict(self.config.get("presets", {}))
                    presets[name] = round(height_cm, 1)
                    self.config["presets"] = presets
                    save_config(self.config)
                    return {**base, "presets": presets}
                elif cmd == "goto_preset":
                    name = str(request.get("name", "")).strip()
                    presets = self.config.get("presets", {})
                    if name not in presets:
                        raise ValueError(f"unknown preset: {name}")
                    await self.desk.move_to(cm_to_meters(float(presets[name])))
                else:
                    raise ValueError(f"unknown command: {cmd}")

                return {**base, **self.desk.status()}
        except Exception as exc:  # noqa: BLE001
            return {"id": req_id, "ok": False, "error": str(exc), **self.desk.status()}

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        buffer = ""
        write_lock = asyncio.Lock()

        async def reply(payload: dict[str, Any]) -> None:
            async with write_lock:
                try:
                    writer.write((json.dumps(payload) + "\n").encode())
                    await writer.drain()
                except OSError:
                    pass

        async def dispatch(request: dict[str, Any]) -> None:
            response = await self.handle_command(request)
            await reply(response)

        try:
            while not self._stop.is_set():
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer += chunk.decode(errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError as exc:
                        await reply({"ok": False, "error": f"invalid json: {exc}"})
                        continue
                    if request.get("cmd") == "stop":
                        await dispatch(request)
                    else:
                        asyncio.create_task(dispatch(request))
        finally:
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def run(self, socket_path: Path) -> None:
        if socket_path.exists():
            socket_path.unlink()

        server = await asyncio.start_unix_server(self.handle_client, path=str(socket_path))
        print(f"omadesk listening on {socket_path}", file=sys.stderr, flush=True)

        async def maintain_connection() -> None:
            while not self._stop.is_set():
                try:
                    has_target = bool(
                        str(self.config.get("mac", "")).strip()
                        or str(self.config.get("name", "")).strip()
                    )
                    if not has_target and not self.desk.connected:
                        hits = await scan_desk_advertisements(8.0)
                        await self.broadcast(
                            {
                                "event": "scan",
                                "devices": scan_hits_payload(hits),
                                **self.desk.status(),
                            }
                        )
                        if len(hits) != 1:
                            await asyncio.sleep(20)
                            continue
                        device = hits[0][0]
                        self.config["mac"] = device.address
                        if device.name:
                            self.config["name"] = device.name
                        save_config(self.config)
                        self.desk.mac = device.address
                    await self.desk.connect()
                    if self.desk.connected and not self._notify_started:
                        await self.desk.start_notify(self.on_height)
                        self._notify_started = True
                    if self.desk.connected:
                        await self.broadcast({**self.desk.status(), "event": "height"})
                        await asyncio.sleep(5)
                        continue
                except Exception as exc:  # noqa: BLE001
                    print(f"omadesk BLE connect failed: {exc}", file=sys.stderr, flush=True)
                    self._notify_started = False
                    await self.broadcast({"event": "error", "error": str(exc), **self.desk.status()})
                await asyncio.sleep(3)

        connect_task = asyncio.create_task(maintain_connection())

        async def shutdown() -> None:
            self._stop.set()
            await self.desk.disconnect()
            server.close()
            await server.wait_closed()
            if socket_path.exists():
                socket_path.unlink()
            if pid_path().exists():
                pid_path().unlink()
            connect_task.cancel()
            try:
                await connect_task
            except asyncio.CancelledError:
                pass

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

        try:
            await self._stop.wait()
        finally:
            await shutdown()


def discover_mac() -> str | None:
    async def _scan() -> str | None:
        config = load_config()
        hits = await scan_desk_advertisements(12.0)
        device = pick_scanned_device(hits, config)
        if device is None and len(hits) == 1:
            device = hits[0][0]
        if device is None:
            return None
        config["mac"] = device.address
        if device.name:
            config["name"] = device.name
        save_config(config)
        return device.address

    return asyncio.run(_scan())


async def resolve_mac(config: dict[str, Any]) -> str:
    device = await find_desk_device(config)
    return device.address


def cmd_status(config: dict[str, Any]) -> int:
    print(json.dumps({"config": config}, indent=2))
    return 0


def cmd_once(config: dict[str, Any], command: str, **kwargs: Any) -> int:
    async def _run() -> dict[str, Any]:
        desk = DeskController(config.get("mac", ""), config)
        daemon = DeskDaemon(config)
        daemon.desk = desk
        return await daemon.handle_command({"id": 1, "cmd": command, **kwargs})

    result = asyncio.run(_run())
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", False) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Omadesk LINAK BLE daemon")
    parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
    parser.add_argument("--discover", action="store_true", help="Scan for Desk devices")
    parser.add_argument("--status", action="store_true", help="Print config")
    parser.add_argument("command", nargs="?", choices=["up", "down", "stop", "move", "goto"])
    parser.add_argument("--height-cm", type=float, help="Target height in cm")
    parser.add_argument("--preset", help="Preset name")
    args = parser.parse_args()

    if args.discover:
        hits = asyncio.run(scan_desk_advertisements(12.0))
        devices = scan_hits_payload(hits)
        print(json.dumps({"devices": devices}, indent=2))
        if not devices:
            print("No Desk device found", file=sys.stderr)
            return 1
        config = load_config()
        device = pick_scanned_device(hits, config)
        if device is None and len(hits) == 1:
            device = hits[0][0]
        if device is not None:
            config["mac"] = device.address
            if device.name:
                config["name"] = device.name
            save_config(config)
        return 0

    config = load_config()

    if args.status:
        return cmd_status(config)

    if args.daemon:
        if not acquire_daemon_lock():
            return 0
        socket_path = state_dir() / "omadesk.sock"
        asyncio.run(DeskDaemon(config).run(socket_path))
        return 0

    if not config.get("mac"):
        mac = discover_mac()
        if mac:
            config["mac"] = mac
            save_config(config)
    if not config.get("mac"):
        print("No desk MAC configured. Pair the desk and run with --discover.", file=sys.stderr)
        return 1

    if args.command == "move":
        if args.height_cm is None:
            print("--height-cm required", file=sys.stderr)
            return 1
        return cmd_once(config, "move", height_cm=args.height_cm)
    if args.command == "goto":
        if not args.preset:
            print("--preset required", file=sys.stderr)
            return 1
        return cmd_once(config, "goto_preset", name=args.preset)
    if args.command:
        return cmd_once(config, args.command)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
