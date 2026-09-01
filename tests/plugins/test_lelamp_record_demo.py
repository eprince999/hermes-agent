"""Contracts for look-at-person recording — no robot, no CSI camera."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "lelamp_il" / "record_demo.py"
WRAPPER = ROOT / "lelamp_il" / "record_on_lamp.sh"
REVISION = "2026-08-28-stream"


def _il_path() -> str:
    return str(ROOT / "lelamp_il")


def test_revision_fingerprint_is_stream_not_old_cam():
    source = RECORD.read_text(encoding="utf-8")
    assert f'RECORD_DEMO_REVISION = "{REVISION}"' in source
    assert "MjpegLiveStream" in source
    assert "不要走 OpenCV" in source
    assert "LeLampLeader 不可用:" not in source
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert REVISION in wrapper
    assert "record_demo.py" in wrapper


def test_infer_pi_uses_record_demo_camera():
    source = (ROOT / "lelamp_il" / "infer_pi.py").read_text(encoding="utf-8")
    assert "PiOrWebcam" in source
    assert "def close_camera" in source
    assert "kind == \"lelamp\"" in source


def test_agent_hook_follows_until_stop_not_fixed_six_seconds():
    source = (ROOT / "lelamp_il" / "agent_hook.py").read_text(encoding="utf-8")
    assert "stop_event" in source
    assert "close_camera" in source
    assert "seconds: float = 0.0" in source
    assert 'WATCH_REVISION = "2026-08-28-follow"' in source
    assert "n_steps = max(1, int(round(seconds * control_hz)))" not in source
    local = (
        Path(__file__).resolve().parents[2] / "plugins" / "lelamp" / "local_main.py"
    ).read_text(encoding="utf-8")
    assert "Command(\"watch_person\", 6.0" not in local
    assert "一直看着你" in local
    assert "WATCH_STOP_SHORT" in local
    assert 'WATCH_REVISION = "2026-08-28-follow"' in local


def test_infer_motor_bus_does_not_require_lerobot_calibration():
    source = (ROOT / "lelamp_il" / "infer_pi.py").read_text(encoding="utf-8")
    assert "无需 lerobot 校准" in source
    assert source.find("Sts3215Bus") < source.find("MotorNormMode.DEGREES")


def test_mjpeg_splitter_extracts_one_frame_and_keeps_rest():
    sys.path.insert(0, _il_path())
    from record_demo import take_jpeg_from_buffer

    jpeg = b"\xff\xd8" + b"fake-jpeg-body" + b"\xff\xd9"
    leftover = b"\xff\xd8partial"
    buf = bytearray(b"noise" + jpeg + leftover)
    got = take_jpeg_from_buffer(buf)
    assert got == jpeg
    assert bytes(buf) == leftover
    assert take_jpeg_from_buffer(buf) is None
    buf.extend(b"more\xff\xd9")
    got2 = take_jpeg_from_buffer(buf)
    assert got2 == leftover + b"more\xff\xd9"
    assert bytes(buf) == b""


def test_mjpeg_stream_drains_while_caller_sleeps():
    """The bug on the lamp: grab() stopped reading during joint I/O / sleep,
    the pipe filled, then TimeoutError at ~frame 120."""
    sys.path.insert(0, _il_path())
    from record_demo import MjpegLiveStream

    rfd, wfd = os.pipe()
    stream = MjpegLiveStream(rfd, proc=None)
    jpeg = b"\xff\xd8" + b"FRAME" + b"\xff\xd9"
    stop_writer = threading.Event()

    def writer() -> None:
        try:
            for _ in range(25):
                if stop_writer.is_set():
                    break
                os.write(wfd, jpeg)
                time.sleep(0.02)
        except BrokenPipeError:
            return
        finally:
            try:
                os.close(wfd)
            except OSError:
                return

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.35)  # simulate servo read + fps sleep with no grab()
    got, age = stream.grab_jpeg()
    assert got == jpeg
    assert age < 1.0
    stop_writer.set()
    stream.stop()
    try:
        os.close(rfd)
    except OSError:
        pass
    thread.join(timeout=1.0)


def test_write_frame_accepts_jpeg_bytes(tmp_path: Path):
    sys.path.insert(0, _il_path())
    from record_demo import write_frame

    jpeg = b"\xff\xd8" + b"tiny" + b"\xff\xd9"
    dest = tmp_path / "000000.jpg"
    write_frame(dest, jpeg)
    assert dest.read_bytes() == jpeg


def test_dummy_record_prints_revision_first(tmp_path: Path):
    data_root = tmp_path / "data"
    result = subprocess.run(
        [
            sys.executable,
            str(RECORD),
            "--dummy",
            "--no-prompt",
            "--episodes",
            "1",
            "--seconds",
            "0.2",
            "--fps",
            "10",
            "--out",
            str(data_root),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    first = result.stdout.splitlines()[0]
    assert first.startswith(f"record_demo {REVISION}"), first
    assert "file " in result.stdout.splitlines()[1]
    frames = list((data_root / "look_at_person" / "ep_000" / "rgb").glob("*.jpg"))
    assert len(frames) >= 2
    assert (data_root / "look_at_person" / "ep_000" / "joints.csv").is_file()


def test_rpicam_commands_empty_when_binaries_missing(monkeypatch):
    sys.path.insert(0, _il_path())
    import record_demo

    monkeypatch.setattr(record_demo.shutil, "which", lambda _name: None)
    assert record_demo._rpicam_commands(640, 480) == []
