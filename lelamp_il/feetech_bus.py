"""STS3215 bus that does not import lerobot.

Recording often runs in a slim venv (pillow + picamera2). Official
LeLampLeader needs ``lelamp`` + ``lerobot`` from ``uv run``. This module
tries, in order: that venv's site-packages, ``scservo_sdk``, then a
stdlib termios talker that only needs ``/dev/ttyACM0``.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
from pathlib import Path
from typing import Any, Optional

JOINT_NAMES = (
    "base_yaw",
    "base_pitch",
    "elbow_pitch",
    "wrist_roll",
    "wrist_pitch",
)
JOINT_IDS = {name: i + 1 for i, name in enumerate(JOINT_NAMES)}

# STS3215 control table (same numbers lerobot's FeetechMotorsBus uses).
ADDR_TORQUE_ENABLE = 40
ADDR_GOAL_POSITION = 42
ADDR_PRESENT_POSITION = 56
ADDR_LOCK = 55
INST_PING = 1
INST_READ = 2
INST_WRITE = 3
TICKS_PER_TURN = 4096
TICK_CENTER = 2048
BAUDRATES = (1_000_000, 115_200)


def ticks_to_degrees(ticks: int) -> float:
    """Map raw STS3215 ticks (0..4095, center 2048) to degrees around zero."""
    return (int(ticks) - TICK_CENTER) * 360.0 / TICKS_PER_TURN


def degrees_to_ticks(degrees: float) -> int:
    ticks = int(round(float(degrees) * TICKS_PER_TURN / 360.0 + TICK_CENTER))
    return max(0, min(TICKS_PER_TURN - 1, ticks))


def build_packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    """Feetech protocol 0 (SMS/STS) instruction packet."""
    length = len(params) + 2
    body = bytes([servo_id & 0xFF, length & 0xFF, instruction & 0xFF]) + params
    return b"\xff\xff" + body + bytes([(~sum(body)) & 0xFF])


def split_status_packets(buf: bytes) -> tuple[list[tuple[int, int, bytes]], bytes]:
    """Parse complete status packets; return (packets, leftover)."""
    packets: list[tuple[int, int, bytes]] = []
    i = 0
    while i + 6 <= len(buf):
        if buf[i] != 0xFF or buf[i + 1] != 0xFF:
            i += 1
            continue
        if i + 4 > len(buf):
            break
        servo_id = buf[i + 2]
        length = buf[i + 3]
        end = i + 4 + length
        if end > len(buf):
            break
        frame = buf[i + 2 : end]
        if ((~sum(frame[:-1])) & 0xFF) != frame[-1]:
            i += 1
            continue
        error = frame[2]
        params = frame[3:-1]
        packets.append((servo_id, error, params))
        i = end
    return packets, buf[i:]


def find_runtime_roots() -> list[Path]:
    home = Path.home()
    here = Path(__file__).resolve().parent
    extra = (os.environ.get("LELAMP_RUNTIME") or "").strip()
    candidates = [
        Path(extra) if extra else None,
        home / "lelamp_runtime",
        home / "lelamp",
        here.parent.parent / "lelamp_runtime",
        here.parent / "lelamp_runtime",
        Path("/home/spocklamp/lelamp_runtime"),
    ]
    roots: list[Path] = []
    for item in candidates:
        if item is None:
            continue
        resolved = item.expanduser()
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def add_runtime_site_packages() -> list[str]:
    """Put official runtime source + venv site-packages on sys.path."""
    added: list[str] = []
    env = (os.environ.get("VIRTUAL_ENV") or "").strip()
    venv_roots = [Path(env)] if env else []
    for runtime in find_runtime_roots():
        src = str(runtime)
        if src not in sys.path:
            sys.path.insert(0, src)
            added.append(src)
        venv_roots.extend([runtime / ".venv", runtime / "venv"])
    for root in venv_roots:
        if not root:
            continue
        for site in sorted(Path(root).glob("lib/python*/site-packages")):
            path = str(site)
            if path not in sys.path:
                sys.path.append(path)
                added.append(path)
    return added


def uv_run_hint(script: str = "record_demo.py") -> str:
    return (
        "不要 pip install lerobot / torch。用灯已经能转头的那个解释器：\n"
        "  cd ~/lelamp_runtime && sudo uv run python "
        f"~/hermes-agent/lelamp_il/{script} "
        "--task look_at_person --port /dev/ttyACM0 --id lelamp "
        "--episodes 2 --seconds 6"
    )


class Sts3215Bus:
    """Read/write five STS3215 servos through scservo_sdk."""

    def __init__(self, port: str, joint_names: tuple[str, ...] = JOINT_NAMES) -> None:
        self.port = port
        self.joint_names = list(joint_names)
        self._scs: Any = None
        self._port: Any = None
        self._packet: Any = None

    def connect(self) -> str:
        try:
            import scservo_sdk as scs
        except ImportError as exc:
            raise ImportError(
                "没有 scservo_sdk（feetech-servo-sdk）。"
            ) from exc
        last: Exception | None = None
        for baud in BAUDRATES:
            for proto in (0, 1):
                try:
                    handler = scs.PortHandler(self.port)
                    if not handler.openPort():
                        raise RuntimeError(f"打不开串口 {self.port}")
                    if not handler.setBaudRate(baud):
                        handler.closePort()
                        raise RuntimeError(f"波特率 {baud} 失败")
                    packet = scs.PacketHandler(proto)
                    self._probe(scs, handler, packet)
                    self._scs = scs
                    self._port = handler
                    self._packet = packet
                    return f"STS3215 {self.port} baud={baud} proto={proto}"
                except Exception as exc:
                    last = exc
                    try:
                        handler.closePort()
                    except Exception:
                        pass
        raise RuntimeError(f"STS3215 打不开: {last}") from last

    def _probe(self, scs: Any, handler: Any, packet: Any) -> None:
        servo_id = JOINT_IDS[self.joint_names[0]]
        _value, comm, _err = packet.read2ByteTxRx(
            handler, servo_id, ADDR_PRESENT_POSITION
        )
        ok = getattr(scs, "COMM_SUCCESS", 0)
        if comm != ok:
            raise RuntimeError(f"读舵机 {servo_id} 失败 comm={comm}")

    def disable_torque(self) -> None:
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            self._write1(sid, ADDR_LOCK, 0)
            self._write1(sid, ADDR_TORQUE_ENABLE, 0)

    def enable_torque(self) -> None:
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            self._write1(sid, ADDR_TORQUE_ENABLE, 1)
            self._write1(sid, ADDR_LOCK, 1)

    def read_degrees(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            raw, comm, _err = self._packet.read2ByteTxRx(
                self._port, sid, ADDR_PRESENT_POSITION
            )
            ok = getattr(self._scs, "COMM_SUCCESS", 0)
            if comm != ok:
                raise RuntimeError(f"读 {name}(id={sid}) 失败 comm={comm}")
            out[name] = ticks_to_degrees(_as_u16(raw))
        return out

    def write_degrees(self, values: dict[str, float]) -> None:
        for name in self.joint_names:
            if name not in values:
                continue
            ticks = degrees_to_ticks(values[name])
            sid = JOINT_IDS[name]
            self._packet.write2ByteTxRx(
                self._port, sid, ADDR_GOAL_POSITION, ticks
            )

    def close(self) -> None:
        port = self._port
        self._port = None
        self._packet = None
        if port is None:
            return
        try:
            port.closePort()
        except Exception:
            pass

    def _write1(self, servo_id: int, addr: int, value: int) -> None:
        self._packet.write1ByteTxRx(self._port, servo_id, addr, int(value))


def _termios_baud_flag(baud: int) -> int:
    table = {
        115200: termios.B115200,
        57600: termios.B57600,
        9600: termios.B9600,
    }
    if baud == 1_000_000:
        flag = getattr(termios, "B1000000", None)
        if flag is None:
            raise RuntimeError("这个 Python 的 termios 没有 B1000000")
        return int(flag)
    if baud not in table:
        raise RuntimeError(f"不支持波特率 {baud}")
    return int(table[baud])


def _open_tty(port: str, baud: int) -> int:
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        flag = _termios_baud_flag(baud)
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        cflag = termios.CS8 | termios.CLOCAL | termios.CREAD
        if hasattr(termios, "CRTSCTS"):
            cflag &= ~termios.CRTSCTS
        attrs[2] = cflag
        attrs[3] = 0
        attrs[4] = flag
        attrs[5] = flag
        cc = list(attrs[6])
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 1
        attrs[6] = list(cc)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


class Sts3215RawBus:
    """Talk STS3215 with stdlib termios. No lerobot, no scservo_sdk."""

    def __init__(self, port: str, joint_names: tuple[str, ...] = JOINT_NAMES) -> None:
        self.port = port
        self.joint_names = list(joint_names)
        self._fd: int | None = None
        self._baud = 0

    def connect(self) -> str:
        last: Exception | None = None
        for baud in BAUDRATES:
            fd = None
            try:
                fd = _open_tty(self.port, baud)
                self._fd = fd
                self._baud = baud
                self._probe()
                return f"STS3215 raw {self.port} baud={baud}"
            except Exception as exc:
                last = exc
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._fd = None
        raise RuntimeError(f"STS3215 raw 打不开: {last}") from last

    def disable_torque(self) -> None:
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            self._write_u8(sid, ADDR_LOCK, 0)
            self._write_u8(sid, ADDR_TORQUE_ENABLE, 0)

    def enable_torque(self) -> None:
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            self._write_u8(sid, ADDR_TORQUE_ENABLE, 1)
            self._write_u8(sid, ADDR_LOCK, 1)

    def read_degrees(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name in self.joint_names:
            sid = JOINT_IDS[name]
            raw = self._read_u16(sid, ADDR_PRESENT_POSITION)
            out[name] = ticks_to_degrees(raw)
        return out

    def write_degrees(self, values: dict[str, float]) -> None:
        for name in self.joint_names:
            if name not in values:
                continue
            ticks = degrees_to_ticks(values[name])
            sid = JOINT_IDS[name]
            self._write_u16(sid, ADDR_GOAL_POSITION, ticks)

    def close(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _probe(self) -> None:
        sid = JOINT_IDS[self.joint_names[0]]
        self._read_u16(sid, ADDR_PRESENT_POSITION)

    def _write_u8(self, servo_id: int, addr: int, value: int) -> None:
        self._xfer(build_packet(servo_id, INST_WRITE, bytes([addr, int(value) & 0xFF])))

    def _write_u16(self, servo_id: int, addr: int, value: int) -> None:
        value = int(value) & 0xFFFF
        payload = bytes([addr, value & 0xFF, (value >> 8) & 0xFF])
        self._xfer(build_packet(servo_id, INST_WRITE, payload))

    def _read_u16(self, servo_id: int, addr: int) -> int:
        _sid, error, params = self._xfer(
            build_packet(servo_id, INST_READ, bytes([addr, 2])),
            expect_id=servo_id,
        )
        if error:
            raise RuntimeError(f"舵机 {servo_id} 读 0x{addr:02x} error={error}")
        if len(params) < 2:
            raise RuntimeError(f"舵机 {servo_id} 读到的字节不够: {params!r}")
        return params[0] | (params[1] << 8)

    def _xfer(
        self, packet: bytes, *, expect_id: int | None = None, timeout: float = 0.25
    ) -> tuple[int, int, bytes]:
        fd = self._fd
        if fd is None:
            raise RuntimeError("串口未打开")
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass
        os.write(fd, packet)
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            wait = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                continue
            chunk = os.read(fd, 64)
            if not chunk:
                continue
            buf += chunk
            packets, buf = split_status_packets(buf)
            for servo_id, error, params in packets:
                if expect_id is None or servo_id == expect_id:
                    return servo_id, error, params
        raise TimeoutError(f"舵机无响应 ({self.port})")


def _as_u16(value: int) -> int:
    raw = int(value)
    if raw < 0:
        raw &= 0xFFFF
    return raw


def probe_servos(port: str) -> tuple[str, list[dict[str, Any]]]:
    """Read-only health check: ping ids 1–5 and read present position.

    Never enables torque, never writes a goal, never runs setup/calibrate.
    """
    if not Path(port).exists():
        raise FileNotFoundError(f"没有串口 {port}")
    last: Exception | None = None
    for baud in BAUDRATES:
        fd = None
        try:
            fd = _open_tty(port, baud)
            bus = Sts3215RawBus(port)
            bus._fd = fd
            bus._baud = baud
            rows: list[dict[str, Any]] = []
            any_ok = False
            for name in JOINT_NAMES:
                sid = JOINT_IDS[name]
                row: dict[str, Any] = {
                    "id": sid,
                    "name": name,
                    "ok": False,
                    "degrees": None,
                    "detail": "",
                }
                try:
                    bus._xfer(
                        build_packet(sid, INST_PING),
                        expect_id=sid,
                        timeout=0.2,
                    )
                    ticks = bus._read_u16(sid, ADDR_PRESENT_POSITION)
                    row["ok"] = True
                    row["degrees"] = round(ticks_to_degrees(ticks), 1)
                    row["detail"] = f"ticks={ticks}"
                    any_ok = True
                except Exception as exc:
                    row["detail"] = str(exc)
                rows.append(row)
            bus._fd = None
            if any_ok:
                return f"STS3215 {port} baud={baud}", rows
            last = RuntimeError(f"波特率 {baud} 下五个舵机都没应答")
        except Exception as exc:
            last = exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
    raise RuntimeError(f"舵机检测失败: {last}") from last


def format_probe_report(link: str, rows: list[dict[str, Any]]) -> str:
    lines = [f"链路 {link}", "id  name          应答  角度"]
    for row in rows:
        deg = "—" if row.get("degrees") is None else f"{row['degrees']:>6.1f}"
        flag = "OK" if row.get("ok") else "FAIL"
        lines.append(
            f"{int(row['id']):2d}  {str(row['name']):<12}  {flag:<4}  {deg}  {row.get('detail') or ''}"
        )
    ok_n = sum(1 for row in rows if row.get("ok"))
    lines.append(f"{ok_n}/{len(rows)} 个舵机应答")
    if ok_n == len(rows) and rows:
        lines.append("五个舵机都正常（只读检测，没有重新校准）。")
    elif ok_n:
        missing = [str(row["name"]) for row in rows if not row.get("ok")]
        lines.append("无应答: " + ", ".join(missing))
    else:
        lines.append("一个舵机都没应答。串口、供电、或被别的进程占用。")
    return "\n".join(lines)


def _probe_main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only STS3215 ping (no calibrate)")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--port", default=os.environ.get("LELAMP_PORT", "/dev/ttyACM0"))
    args = parser.parse_args(argv)
    if not args.probe:
        parser.print_help()
        return 2
    try:
        link, rows = probe_servos(args.port)
    except Exception as exc:
        print(f"检测失败: {exc}")
        return 1
    print(format_probe_report(link, rows))
    return 0 if rows and all(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(_probe_main())
