"""STS3215 bus that does not import lerobot.

Official LeLamp runtime talks to the same Feetech bus through lerobot.
The lamp's ``uv run`` environment already has that stack. Recording from
another venv (the one that only has pillow + picamera2) must use the
lighter ``scservo_sdk`` package from ``feetech-servo-sdk``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

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
TICKS_PER_TURN = 4096
TICK_CENTER = 2048
BAUDRATES = (1_000_000, 115_200)


def ticks_to_degrees(ticks: int) -> float:
    """Map raw STS3215 ticks (0..4095, center 2048) to degrees around zero."""
    return (int(ticks) - TICK_CENTER) * 360.0 / TICKS_PER_TURN


def degrees_to_ticks(degrees: float) -> int:
    ticks = int(round(float(degrees) * TICKS_PER_TURN / 360.0 + TICK_CENTER))
    return max(0, min(TICKS_PER_TURN - 1, ticks))


def add_runtime_site_packages() -> list[str]:
    """Put the official runtime venv on sys.path so ``import lerobot`` can work.

    ``record_demo.py`` already adds ``~/lelamp_runtime`` (the source tree).
    ``lerobot`` lives in that project's ``.venv`` site-packages, not in the
    source tree — that is why Leader fails with ``No module named 'lerobot'``.
    """
    roots: list[Path] = [
        Path.home() / "lelamp_runtime" / ".venv",
        Path.home() / "lelamp_runtime" / "venv",
    ]
    env = (os.environ.get("VIRTUAL_ENV") or "").strip()
    if env:
        roots.append(Path(env))
    added: list[str] = []
    for root in roots:
        for site in sorted(root.glob("lib/python*/site-packages")):
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


def _as_u16(value: int) -> int:
    raw = int(value)
    if raw < 0:
        raw &= 0xFFFF
    return raw
