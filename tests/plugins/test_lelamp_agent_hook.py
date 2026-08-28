"""Look-at policy keeps following until stop_event, not a 6-second clip."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
IL = ROOT / "lelamp_il"


def _load_agent_hook():
    if str(IL) not in sys.path:
        sys.path.insert(0, str(IL))
    import agent_hook
    import infer_pi

    return agent_hook, infer_pi


def test_run_watch_person_stops_on_event_and_closes_camera(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    agent_hook, infer_pi = _load_agent_hook()

    model = tmp_path / "tiny_lamp_int8.onnx"
    meta = tmp_path / "meta.json"
    model.write_bytes(b"onnx")
    meta.write_text("{}")

    calls = {"writes": 0, "closed": 0}

    class FakeBus:
        def __init__(self, *args, **kwargs):
            pass

        def read_joints(self):
            return np.zeros(5, dtype=np.float32)

        def write_joints(self, _target):
            calls["writes"] += 1

        def close(self):
            pass

    class FakeSess:
        def run(self, _names, _feeds):
            return [np.zeros((1, 4, 5), dtype=np.float32)]

    stop = threading.Event()

    def fake_load_meta(_path):
        return {
            "image_size": 16,
            "control_hz": 100.0,
            "joint_mean": [0.0] * 5,
            "joint_std": [1.0] * 5,
            "joint_min": [-10.0] * 5,
            "joint_max": [10.0] * 5,
            "joint_names": ["a", "b", "c", "d", "e"],
        }

    def fake_grab(_camera, _size):
        if calls["writes"] >= 2:
            stop.set()
        return MagicMock()

    def fake_preprocess(_frame, size):
        return np.zeros((1, 3, size, size), dtype=np.float32)

    def fake_close(_camera):
        calls["closed"] += 1

    monkeypatch.setattr(infer_pi, "MotorBus", FakeBus)
    monkeypatch.setattr(infer_pi, "load_meta", fake_load_meta)
    monkeypatch.setattr(infer_pi, "make_session", lambda _model: FakeSess())
    monkeypatch.setattr(infer_pi, "open_camera", lambda _index: ("lelamp", object()))
    monkeypatch.setattr(infer_pi, "grab_frame", fake_grab)
    monkeypatch.setattr(infer_pi, "preprocess_image", fake_preprocess)
    monkeypatch.setattr(infer_pi, "close_camera", fake_close)

    msg = agent_hook.run_watch_person(
        model=model,
        meta=meta,
        port="/dev/null",
        seconds=0.0,
        stop_event=stop,
    )
    assert "不看了" in msg
    assert 2 <= calls["writes"] <= 8
    assert calls["closed"] == 1


def test_run_watch_person_bounded_seconds_still_works(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    agent_hook, infer_pi = _load_agent_hook()

    model = tmp_path / "tiny_lamp_int8.onnx"
    meta = tmp_path / "meta.json"
    model.write_bytes(b"onnx")
    meta.write_text("{}")

    writes = {"n": 0}

    class FakeBus:
        def __init__(self, *args, **kwargs):
            pass

        def read_joints(self):
            return np.zeros(5, dtype=np.float32)

        def write_joints(self, _target):
            writes["n"] += 1

        def close(self):
            pass

    class FakeSess:
        def run(self, _names, _feeds):
            return [np.zeros((1, 4, 5), dtype=np.float32)]

    def fake_load_meta(_path):
        return {
            "image_size": 16,
            "control_hz": 20.0,
            "joint_mean": [0.0] * 5,
            "joint_std": [1.0] * 5,
            "joint_min": [-10.0] * 5,
            "joint_max": [10.0] * 5,
            "joint_names": ["a", "b", "c", "d", "e"],
        }

    monkeypatch.setattr(infer_pi, "MotorBus", FakeBus)
    monkeypatch.setattr(infer_pi, "load_meta", fake_load_meta)
    monkeypatch.setattr(infer_pi, "make_session", lambda _model: FakeSess())
    monkeypatch.setattr(infer_pi, "open_camera", lambda _index: ("cv2", object()))
    monkeypatch.setattr(infer_pi, "grab_frame", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(
        infer_pi,
        "preprocess_image",
        lambda _frame, size: np.zeros((1, 3, size, size), dtype=np.float32),
    )
    monkeypatch.setattr(infer_pi, "close_camera", lambda _camera: None)

    msg = agent_hook.run_watch_person(
        model=model,
        meta=meta,
        port="/dev/null",
        seconds=0.1,
    )
    assert "0.1 秒" in msg
    assert writes["n"] == 2
