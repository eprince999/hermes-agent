"""Contracts for look-at-person recording — no robot, no CSI camera."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "lelamp_il" / "record_demo.py"
WRAPPER = ROOT / "lelamp_il" / "record_on_lamp.sh"


def _il_path() -> str:
    return str(ROOT / "lelamp_il")


def test_revision_fingerprint_is_rpicam_not_old_cam():
    source = RECORD.read_text(encoding="utf-8")
    assert 'RECORD_DEMO_REVISION = "2026-08-28-rpicam"' in source
    assert "rpicam-vid" in source
    assert "不要走 OpenCV" in source
    # Old silent-hang path: OpenCV /dev/video0 on Pi, or Feetech line with exception class.
    assert "LeLampLeader 不可用:" not in source
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "2026-08-28-rpicam" in wrapper
    assert "record_demo.py" in wrapper


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
    assert first.startswith("record_demo 2026-08-28-rpicam"), first
    assert "file " in result.stdout.splitlines()[1]
    frames = list((data_root / "look_at_person" / "ep_000" / "rgb").glob("*.jpg"))
    assert len(frames) >= 2


def test_rpicam_commands_empty_when_binaries_missing(monkeypatch):
    sys.path.insert(0, _il_path())
    import record_demo

    monkeypatch.setattr(record_demo.shutil, "which", lambda _name: None)
    assert record_demo._rpicam_commands(640, 480) == []
