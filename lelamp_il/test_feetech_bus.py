#!/usr/bin/env python3
"""Unit tests for STS3215 helpers — no robot, no lerobot."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from feetech_bus import (  # noqa: E402
    degrees_to_ticks,
    ticks_to_degrees,
    uv_run_hint,
)


class TickConversionTests(unittest.TestCase):
    def test_center_is_zero_degrees(self) -> None:
        self.assertEqual(ticks_to_degrees(2048), 0.0)

    def test_round_trip_near_center(self) -> None:
        for deg in (-90.0, -12.5, 0.0, 8.0, 45.0, 90.0):
            back = ticks_to_degrees(degrees_to_ticks(deg))
            self.assertAlmostEqual(back, deg, delta=360.0 / 4096.0 + 1e-9)

    def test_ticks_are_clamped(self) -> None:
        self.assertEqual(degrees_to_ticks(-1000), 0)
        self.assertEqual(degrees_to_ticks(1000), 4095)


class HintTests(unittest.TestCase):
    def test_uv_run_hint_does_not_tell_user_to_pip_install_lerobot(self) -> None:
        hint = uv_run_hint("record_demo.py")
        self.assertIn("uv run python", hint)
        self.assertIn("record_demo.py", hint)
        self.assertIn("不要 pip install lerobot", hint)


class ConnectFallbackTests(unittest.TestCase):
    def test_missing_backends_mention_uv_run(self) -> None:
        from record_demo import LeLampJoints

        joints = LeLampJoints("/dev/null", "lelamp")
        with self.assertRaises(RuntimeError) as caught:
            joints.connect()
        msg = str(caught.exception)
        self.assertIn("无法连接舵机", msg)
        self.assertIn("uv run python", msg)
        self.assertIn("scservo_sdk", msg)
        self.assertIn("没有灯时才加 --dummy", msg)


if __name__ == "__main__":
    unittest.main()
