"""Read-only STS3215 probe helpers — no robot."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _feetech():
    path = Path(__file__).resolve().parents[2] / "lelamp_il" / "feetech_bus.py"
    spec = importlib.util.spec_from_file_location("feetech_bus_probe_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_servos_missing_node():
    mod = _feetech()
    with pytest.raises(FileNotFoundError):
        mod.probe_servos("/dev/lelamp-missing-acm0")


def test_format_probe_report_all_ok():
    mod = _feetech()
    rows = [
        {"id": i, "name": name, "ok": True, "degrees": 0.0, "detail": "ticks=2048"}
        for i, name in enumerate(
            ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"),
            start=1,
        )
    ]
    text = mod.format_probe_report("STS3215 /dev/ttyACM0 baud=1000000", rows)
    assert "5/5" in text
    assert "都正常" in text
    assert "校准" in text
