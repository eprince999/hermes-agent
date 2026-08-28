#!/usr/bin/env python3
"""按步骤录制看人/跟手示教：你用手摆灯，脚本同时写下关节和相机帧.

输出格式与 train.py 一致::

    data/look_at_person/
      ep_000/joints.csv
      ep_000/rgb/000000.jpg
      ep_001/...

真实灯::

    python record_demo.py --task look_at_person --port /dev/ttyUSB0 --id lelamp

没有灯、先走一遍流程（笔记本摄像头 + 假关节）::

    python record_demo.py --task look_at_person --dummy --episodes 2 --seconds 3

录满后直接::

    python train.py --data ./data/look_at_person --epochs 40 --export ./artifacts
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PIL import Image

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from feetech_bus import (
    Sts3215Bus,
    Sts3215RawBus,
    add_runtime_site_packages,
    uv_run_hint,
)

# Bump when camera/servo recording logic changes. Printed as the first
# line of main(). If the lamp does not print this, git pull did not land
# — do not copy this file into ~/lelamp_runtime/.
RECORD_DEMO_REVISION = "2026-08-28-stream"

add_runtime_site_packages()

JOINT_NAMES = (
    "base_yaw",
    "base_pitch",
    "elbow_pitch",
    "wrist_roll",
    "wrist_pitch",
)
CSV_FIELDS = ["timestamp"] + [f"{name}.pos" for name in JOINT_NAMES]
COMMON_PORTS = (
    "/dev/ttyACM0",
    "/dev/ttyUSB0",
    "/dev/ttyAMA0",
    "/dev/tty.usbmodem",
)


# ---------------------------------------------------------------------------
# Hardware adapters (real lamp if present, otherwise dummy)
# ---------------------------------------------------------------------------


def guess_port() -> str | None:
    for path in COMMON_PORTS:
        if path.endswith("usbmodem"):
            matches = sorted(Path("/dev").glob("tty.usbmodem*"))
            if matches:
                return str(matches[0])
            continue
        if Path(path).exists():
            return path
    return None


def port_users(port: str) -> str:
    try:
        result = subprocess.run(
            ["fuser", port],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip()


def warn_stop_voice_agent(port: str | None) -> None:
    print("录制前先停掉正在跑的语音/音乐程序（它占着舵机串口和摄像头）。")
    print("  sudo systemctl stop lelamp.service")
    print("  或在 main.py / console 那个终端按 Ctrl-C")
    print("放音乐、中文指令、点头动画都保留，录完再开回去。")
    if port:
        users = port_users(port)
        if users:
            print(f"警告: {port} 仍被占用: {users}  —— 不停掉会连不上舵机。")
    print()


class JointSource:
    def connect(self) -> str: ...
    def read(self) -> dict[str, float]: ...
    def close(self) -> None: ...


class CameraSource:
    def connect(self) -> str: ...
    def grab(self) -> Image.Image | bytes: ...
    def close(self) -> None: ...


class LeLampJoints(JointSource):
    """Loose (torque-off) Feetech bus — you can move the lamp by hand."""

    def __init__(self, port: str, lamp_id: str) -> None:
        self.port = port
        self.lamp_id = lamp_id
        self._leader = None
        self._bus = None
        self._sts = None
        self._names = list(JOINT_NAMES)

    def connect(self) -> str:
        errors: list[str] = []

        try:
            from lelamp.leader import LeLampLeader, LeLampLeaderConfig

            cfg = LeLampLeaderConfig(port=self.port, id=self.lamp_id)
            leader = LeLampLeader(cfg)
            leader.connect(calibrate=False)
            # Leader.configure() already disables torque.
            self._leader = leader
            probe = leader.get_action()
            short = {name: float(probe[f"{name}.pos"]) for name in self._names}
            return (
                f"LeLampLeader {self.port} (id={self.lamp_id})  "
                f"力矩已关闭，可用手摆  当前={_fmt_joints(short)}"
            )
        except Exception as leader_exc:
            errors.append(f"LeLampLeader: {leader_exc}")

        try:
            from lerobot.motors import Motor, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus

            motors = {
                name: Motor(i + 1, "sts3215", MotorNormMode.DEGREES)
                for i, name in enumerate(self._names)
            }
            bus = FeetechMotorsBus(port=self.port, motors=motors)
            bus.connect()
            bus.disable_torque()
            self._bus = bus
            present = bus.sync_read("Present_Position")
            short = {name: float(present[name]) for name in self._names}
            return (
                f"Feetech {self.port}  力矩已关闭，可用手摆  "
                f"当前={_fmt_joints(short)}"
            )
        except Exception as bus_exc:
            errors.append(f"lerobot Feetech: {bus_exc}")

        try:
            sts = Sts3215Bus(self.port, tuple(self._names))
            label = sts.connect()
            sts.disable_torque()
            probe = sts.read_degrees()
            self._sts = sts
            return (
                f"{label}  力矩已关闭，可用手摆（不经过 lerobot）  "
                f"当前={_fmt_joints(probe)}"
            )
        except Exception as sts_exc:
            errors.append(f"scservo_sdk: {sts_exc}")

        try:
            raw = Sts3215RawBus(self.port, tuple(self._names))
            label = raw.connect()
            raw.disable_torque()
            probe = raw.read_degrees()
            self._sts = raw
            return (
                f"{label}  力矩已关闭，可用手摆（纯串口，无 lerobot）  "
                f"当前={_fmt_joints(probe)}"
            )
        except Exception as raw_exc:
            errors.append(f"raw STS3215: {raw_exc}")

        raise RuntimeError(
            "无法连接舵机。\n  - "
            + "\n  - ".join(errors)
            + "\n"
            + uv_run_hint("record_demo.py")
            + "\n没有灯时才加 --dummy。"
        )

    def read(self) -> dict[str, float]:
        if self._leader is not None:
            raw = self._leader.get_action()
            return {name: float(raw[f"{name}.pos"]) for name in self._names}
        if self._sts is not None:
            return self._sts.read_degrees()
        present = self._bus.sync_read("Present_Position")
        return {name: float(present[name]) for name in self._names}

    def close(self) -> None:
        if self._leader is not None:
            self._leader.disconnect()
            self._leader = None
        if self._bus is not None:
            try:
                self._bus.disconnect(True)
            except TypeError:
                self._bus.disconnect()
            self._bus = None
        if self._sts is not None:
            self._sts.close()
            self._sts = None


class DummyJoints(JointSource):
    """No motors: a slow sinusoid so the saved CSV is not a pile of zeros."""

    def connect(self) -> str:
        self._t0 = time.perf_counter()
        return "假关节（没有接灯）。流程可以走，但学不会真实看人。"

    def read(self) -> dict[str, float]:
        t = time.perf_counter() - self._t0
        return {
            "base_yaw": 25.0 * math.sin(t),
            "base_pitch": 8.0 * math.cos(t * 0.7),
            "elbow_pitch": 15.0 * math.sin(t * 1.3),
            "wrist_roll": 6.0 * math.cos(t * 0.9),
            "wrist_pitch": 4.0 * math.sin(t * 0.5),
        }

    def close(self) -> None:
        return


def _is_raspberry_pi() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore").lower()
    except OSError:
        model = ""
    return "raspberry pi" in model or Path("/usr/bin/rpicam-hello").exists()


def _camera_holders() -> str:
    cmd = [
        "fuser",
        "-v",
        "/dev/video0",
        "/dev/video1",
        "/dev/media0",
        "/dev/media1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip()


def _release_camera_apps() -> None:
    """Stop leftover rpicam-* tools so the CSI pipeline is free.

    Does not kill python — that would stop this recorder.
    """
    for name in (
        "rpicam-hello",
        "rpicam-still",
        "rpicam-vid",
        "libcamera-hello",
        "libcamera-still",
        "libcamera-vid",
    ):
        try:
            subprocess.run(["pkill", "-x", name], capture_output=True, timeout=3)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue


def take_jpeg_from_buffer(buf: bytearray) -> bytes | None:
    """Pop one JPEG from an MJPEG byte buffer. Incomplete frames stay in buf."""
    soi, eoi = b"\xff\xd8", b"\xff\xd9"
    start = buf.find(soi)
    if start < 0:
        if len(buf) > 1_000_000:
            del buf[:-1]
        return None
    if start:
        del buf[:start]
    end = buf.find(eoi, 2)
    if end < 0:
        if len(buf) > 2_000_000:
            raise RuntimeError("MJPEG 帧过大，相机输出异常")
        return None
    jpeg = bytes(buf[: end + 2])
    del buf[: end + 2]
    return jpeg


def _enlarge_pipe(fd: int) -> None:
    """Give rpicam-vid more than the default 64KiB pipe so a slow grab cannot stall it."""
    try:
        import fcntl

        flag = getattr(fcntl, "F_SETPIPE_SZ", 1031)
        fcntl.fcntl(fd, flag, 1 << 20)
    except Exception:
        return


def write_frame(path: Path, frame: Image.Image | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(frame, (bytes, bytearray)):
        path.write_bytes(bytes(frame))
        return
    frame.convert("RGB").save(path, quality=90)


class MjpegLiveStream:
    """Drain rpicam-vid stdout on a side thread; keep only the latest JPEG.

    The record loop also talks to servos and sleeps to hit --fps. If it is
    the only stdout reader, the 64KiB pipe fills, libcamera stalls, and
    grab() raises TimeoutError mid-episode.
    """

    def __init__(self, fd: int, proc: subprocess.Popen | None = None) -> None:
        self._fd = fd
        self._proc = proc
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._latest_ts = 0.0
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self._got_one = threading.Event()
        _enlarge_pipe(fd)
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mjpeg-drain"
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if self._proc is not None and self._proc.poll() is not None:
                    ready, _, _ = select.select([self._fd], [], [], 0)
                    if not ready:
                        with self._lock:
                            if self._latest is None and self._error is None:
                                self._error = RuntimeError(
                                    f"rpicam-vid 退出码 {self._proc.returncode}"
                                )
                        self._got_one.set()
                        return
                try:
                    ready, _, _ = select.select([self._fd], [], [], 0.25)
                except (ValueError, OSError):
                    return
                if not ready:
                    continue
                try:
                    chunk = os.read(self._fd, 65536)
                except OSError:
                    return
                if not chunk:
                    if self._proc is not None and self._proc.poll() is not None:
                        return
                    time.sleep(0.02)
                    continue
                self._buf.extend(chunk)
                while True:
                    jpeg = take_jpeg_from_buffer(self._buf)
                    if jpeg is None:
                        break
                    with self._lock:
                        self._latest = jpeg
                        self._latest_ts = time.monotonic()
                    self._got_one.set()
        except BaseException as exc:
            with self._lock:
                self._error = exc
            self._got_one.set()

    def _raise_if_dead(self) -> None:
        with self._lock:
            err = self._error
        if err is not None:
            raise err

    def wait_first(self, timeout_s: float, progress: bool = False) -> bytes:
        deadline = time.monotonic() + timeout_s
        last_nudge = 0.0
        while time.monotonic() < deadline:
            self._raise_if_dead()
            remaining = deadline - time.monotonic()
            if self._got_one.wait(min(0.25, max(remaining, 0.0))):
                jpeg = self.latest_bytes()
                if jpeg:
                    return jpeg
            if progress:
                waited = timeout_s - (deadline - time.monotonic())
                if waited - last_nudge >= 2.0:
                    last_nudge = waited
                    print(f"    rpicam-vid: 等待第一帧 {waited:.0f}s ...", flush=True)
        self._raise_if_dead()
        raise TimeoutError(f"{timeout_s:.0f}s 内没有收到 JPEG 帧")

    def latest_bytes(self) -> bytes | None:
        with self._lock:
            return self._latest

    def latest_age(self) -> float:
        with self._lock:
            if self._latest is None or self._latest_ts <= 0:
                return float("inf")
            return time.monotonic() - self._latest_ts

    def grab_jpeg(self) -> tuple[bytes, float]:
        """Return (jpeg, age_seconds). Never blocks on the pipe itself."""
        self._raise_if_dead()
        jpeg = self.latest_bytes()
        if jpeg is None:
            jpeg = self.wait_first(5.0)
        return jpeg, self.latest_age()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.5)


def _picamera_capture(cam, timeout_s: float = 8.0):
    try:
        return cam.capture_array("main", wait=timeout_s)
    except TypeError:
        return cam.capture_array()


def _stop_picamera(cam) -> None:
    for name in ("stop", "close"):
        fn = getattr(cam, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def _fmt_joints(values: dict[str, float]) -> str:
    parts = []
    for name in JOINT_NAMES:
        if name in values:
            v = values[name]
        elif f"{name}.pos" in values:
            v = values[f"{name}.pos"]
        else:
            continue
        parts.append(f"{name}={v:+6.1f}")
    return " ".join(parts)


def _rpicam_commands(width: int, height: int, fps: int = 15) -> list[list[str]]:
    bins = []
    for name in ("rpicam-vid", "libcamera-vid"):
        if shutil.which(name):
            bins.append(name)
    cmds: list[list[str]] = []
    for binary in bins:
        sized = [
            binary,
            "-t",
            "0",
            "--codec",
            "mjpeg",
            "--width",
            str(width),
            "--height",
            str(height),
            "--framerate",
            str(fps),
            "--nopreview",
            "-o",
            "-",
        ]
        cmds.append(sized + ["--flush", "--denoise", "cdn_off"])
        cmds.append(sized + ["--flush"])
        cmds.append(list(sized))
        cmds.append(
            [
                binary,
                "-t",
                "0",
                "--codec",
                "mjpeg",
                "--nopreview",
                "--flush",
                "-o",
                "-",
            ]
        )
    return cmds


class PiOrWebcam(CameraSource):
    def __init__(self, index: int, width: int, height: int) -> None:
        self.index = index
        self.width = width
        self.height = height
        self._kind = None
        self._handle = None
        self._mjpeg: MjpegLiveStream | None = None
        self._stderr: deque[str] = deque(maxlen=40)
        self._stale_warns = 0

    def connect(self) -> str:
        errors: list[str] = []
        print("    正在打开灯头摄像头（马上会有进度；完全没字就是旧脚本）...", flush=True)
        holders = _camera_holders()
        if holders:
            print(f"    警告: 摄像头可能被占用:\n{holders}", flush=True)
            print("    先停 rpicam-hello / 语音 agent / 另一个 python，再录。", flush=True)
        _release_camera_apps()

        if _is_raspberry_pi():
            try:
                return self._connect_rpicam()
            except Exception as exc:
                errors.append(f"rpicam-vid: {exc}")
                print(f"    rpicam-vid 失败: {exc}", flush=True)
            try:
                return self._connect_picamera2()
            except Exception as exc:
                errors.append(str(exc))
            hint = (
                "树莓派灯头 CSI 不要走 OpenCV（/dev/video0 会一直卡住）。\n"
                "  sudo pkill -x rpicam-hello; sudo pkill -x rpicam-vid || true\n"
                "  确认 `rpicam-hello --list-cameras` 仍能列出 IMX708。\n"
                "  然后: bash ~/hermes-agent/lelamp_il/record_on_lamp.sh"
            )
            raise RuntimeError(
                "没有摄像头可用。\n  - " + "\n  - ".join(errors) + "\n" + hint
            )

        try:
            return self._connect_picamera2()
        except Exception as exc:
            errors.append(str(exc))

        try:
            import cv2

            nodes = [p for p in (Path("/dev/video0"), Path("/dev/video1")) if p.exists()]
            if not nodes:
                errors.append("opencv: 没有 /dev/video0")
            else:
                for path in nodes:
                    cap = cv2.VideoCapture(str(path))
                    if not cap.isOpened():
                        cap.release()
                        continue
                    ok, _ = cap.read()
                    if not ok:
                        cap.release()
                        continue
                    self._kind, self._handle = "cv2", cap
                    return f"OpenCV {path}"
                errors.append("opencv: video0/1 打开了但读不到帧")
        except Exception as exc:
            errors.append(f"opencv: {exc}")

        raise RuntimeError("没有摄像头可用。\n  - " + "\n  - ".join(errors))

    def _connect_rpicam(self) -> str:
        sizes = []
        for pair in ((self.width, self.height), (320, 240), (640, 480)):
            if pair not in sizes:
                sizes.append(pair)
        last_err = "no command tried"
        for width, height in sizes:
            cmds = _rpicam_commands(width, height, fps=15)
            if not cmds:
                raise RuntimeError("找不到 rpicam-vid / libcamera-vid")
            for cmd in cmds:
                print(f"    rpicam-vid: 尝试 {' '.join(cmd)}", flush=True)
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                    )
                except FileNotFoundError as exc:
                    last_err = str(exc)
                    continue
                if proc.stdout is None:
                    last_err = "rpicam-vid 没有 stdout"
                    self._stop_rpicam_proc(proc)
                    continue
                self._handle = proc
                self._stderr.clear()
                threading.Thread(
                    target=self._drain_rpicam_stderr,
                    args=(proc,),
                    daemon=True,
                ).start()
                stream = MjpegLiveStream(proc.stdout.fileno(), proc)
                self._mjpeg = stream
                try:
                    stream.wait_first(timeout_s=10.0, progress=True)
                except Exception as exc:
                    last_err = str(exc)
                    err_tail = " | ".join(list(self._stderr)[-6:])
                    if err_tail:
                        last_err = f"{last_err}; stderr: {err_tail}"
                    print(f"    rpicam-vid: 失败: {last_err}", flush=True)
                    self._stop_rpicam()
                    continue
                self._kind = "rpicam-vid"
                return (
                    f"Pi Camera via {cmd[0]} (MJPEG {width}x{height} @ 15fps, "
                    "后台抽帧)"
                )
        raise RuntimeError(last_err)

    def _drain_rpicam_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr.append(line)
        except Exception:
            return

    def _stop_rpicam_proc(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _stop_rpicam(self) -> None:
        if self._mjpeg is not None:
            self._mjpeg.stop()
            self._mjpeg = None
        proc = self._handle
        self._handle = None
        if proc is not None:
            self._stop_rpicam_proc(proc)

    def _connect_picamera2(self) -> str:
        print("    picamera2: 准备 import（这一步卡住请 Ctrl-C，改用 rpicam-vid）...", flush=True)
        from picamera2 import Picamera2

        print("    picamera2: 正在初始化 CSI（Module 3 第一次可能要几秒）...", flush=True)
        stuck = threading.Event()

        def _nudge() -> None:
            if not stuck.wait(8):
                print(
                    "    仍在等 Picamera2()。12 秒后会放弃，改试下一种方式。",
                    flush=True,
                )

        threading.Thread(target=_nudge, daemon=True).start()
        cam = Picamera2()
        stuck.set()
        config_tries = [
            {"size": (self.width, self.height), "format": "XBGR8888"},
            {"size": (320, 240), "format": "XBGR8888"},
            {"size": (640, 480), "format": "XBGR8888"},
            {"size": (self.width, self.height)},
        ]
        last_exc: Exception | None = None
        for spec in config_tries:
            print(f"    picamera2: 尝试 {spec} ...", flush=True)
            try:
                kwargs = {"main": spec, "buffer_count": 1}
                try:
                    config = cam.create_preview_configuration(**kwargs)
                except TypeError:
                    config = cam.create_preview_configuration(main=spec)
                cam.configure(config)
                cam.start()
                time.sleep(0.4)
                _ = _picamera_capture(cam, timeout_s=8.0)
                self._kind, self._handle = "picamera2", cam
                w, h = spec.get("size", (self.width, self.height))
                return f"Pi Camera {w}x{h} (picamera2)"
            except Exception as exc:
                last_exc = exc
                print(f"    picamera2: {spec} 失败: {exc}", flush=True)
                _stop_picamera(cam)
        raise RuntimeError(f"picamera2: {last_exc}")

    def grab(self) -> Image.Image | bytes:
        if self._kind == "rpicam-vid":
            if self._mjpeg is None:
                raise RuntimeError("rpicam-vid 流未打开")
            jpeg, age = self._mjpeg.grab_jpeg()
            if age > 0.8:
                self._stale_warns += 1
                if self._stale_warns == 1 or self._stale_warns % 30 == 0:
                    print(
                        f"    警告: 相机帧偏旧 {age:.1f}s，复用最新一张 "
                        f"(连续 {self._stale_warns})",
                        flush=True,
                    )
            else:
                self._stale_warns = 0
            return jpeg
        if self._kind == "picamera2":
            arr = _picamera_capture(self._handle, timeout_s=2.0)
            img = Image.fromarray(arr).convert("RGB")
            if img.size[0] > 320 or img.size[1] > 240:
                img.thumbnail((320, 240))
            return img
        ok, frame = self._handle.read()
        if not ok:
            raise RuntimeError("摄像头丢帧")
        rgb = frame[:, :, ::-1]
        return Image.fromarray(rgb)

    def close(self) -> None:
        if self._kind == "cv2" and self._handle is not None:
            self._handle.release()
        if self._kind == "picamera2" and self._handle is not None:
            _stop_picamera(self._handle)
        if self._kind == "rpicam-vid":
            self._stop_rpicam()
        self._handle = None


class DummyCamera(CameraSource):
    def connect(self) -> str:
        self._i = 0
        return "假画面（纯色帧）。没有摄像头时只用来试流程。"

    def grab(self) -> Image.Image:
        self._i += 1
        color = ((self._i * 7) % 180 + 40, 70, 110)
        return Image.new("RGB", (320, 240), color)

    def close(self) -> None:
        return


def next_episode_index(task_dir: Path) -> int:
    existing = []
    for child in task_dir.glob("ep_*"):
        if not child.is_dir():
            continue
        try:
            existing.append(int(child.name.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return (max(existing) + 1) if existing else 0


def countdown(seconds: int) -> None:
    for n in range(seconds, 0, -1):
        print(f"    {n}...", flush=True)
        time.sleep(1)


def prompt(message: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        got = input(f"{message}{suffix} ").strip()
    except EOFError:
        return default
    return got or default


def save_joints_csv(
    ep_dir: Path,
    timestamps: list[float],
    joints_rows: list[dict[str, float]],
) -> None:
    csv_path = ep_dir / "joints.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for ts, joints in zip(timestamps, joints_rows):
            row = {"timestamp": f"{ts:.6f}"}
            for name in JOINT_NAMES:
                row[f"{name}.pos"] = f"{joints[name]:.4f}"
            writer.writerow(row)


def record_one(
    joints: JointSource,
    camera: CameraSource,
    seconds: float,
    fps: int,
    rgb_dir: Path,
) -> tuple[list[float], list[dict[str, float]]]:
    n_target = max(1, int(round(seconds * fps)))
    period = 1.0 / fps
    t0 = time.perf_counter()
    timestamps: list[float] = []
    rows: list[dict[str, float]] = []
    rgb_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_target):
        tick = time.perf_counter()
        pose = joints.read()
        frame = camera.grab()
        write_frame(rgb_dir / f"{i:06d}.jpg", frame)
        timestamps.append(tick - t0)
        rows.append(pose)
        if i == 0 or (i + 1) % fps == 0 or i + 1 == n_target:
            print(
                f"    {i + 1:>4}/{n_target}  {_fmt_joints(pose)}",
                flush=True,
            )
        remain = period - (time.perf_counter() - tick)
        if remain > 0:
            time.sleep(remain)
    return timestamps, rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="用手摆灯，同时录关节 + 相机帧（给 train.py 用）。",
    )
    p.add_argument("--task", default="look_at_person", help="任务名，数据写到 data/<task>/")
    p.add_argument("--out", type=Path, default=Path("data"), help="数据根目录")
    p.add_argument("--port", default=None, help="Feetech 串口。省略则尝试 /dev/ttyACM0 或 /dev/ttyUSB0")
    p.add_argument("--id", default="lelamp", help="灯的校准 id，需与 calibrate 时一致")
    p.add_argument("--camera", type=int, default=0, help="OpenCV 摄像头编号")
    p.add_argument("--width", type=int, default=320)
    p.add_argument("--height", type=int, default=240)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--seconds", type=float, default=6.0, help="每一段录几秒")
    p.add_argument("--episodes", type=int, default=50, help="一共录几段")
    p.add_argument("--countdown", type=int, default=3)
    p.add_argument(
        "--dummy",
        action="store_true",
        help="不接灯、不接摄像头，用假数据走完步骤（练习 / 测试）。",
    )
    p.add_argument(
        "--no-prompt",
        action="store_true",
        help="不暂停、不倒计时、不询问是否重录。给脚本测试用。",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    print(f"record_demo {RECORD_DEMO_REVISION}", flush=True)
    print(f"file {Path(__file__).resolve()}", flush=True)
    args = parse_args(argv)
    task_dir = (args.out / args.task).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("LeLamp 示教录制（接在已有中文语音灯上，只补「看人」）")
    print(f"revision {RECORD_DEMO_REVISION}")
    print(f"file {Path(__file__).resolve()}")
    print("=" * 60)
    print(
        "你的灯已有：中文指令、放音乐、点头/摇头等动画。这些都不要卸。\n"
        "这一段只录「看我」：你用手把灯头转到人脸，程序同时存图和关节。\n"
        "每一段：人站好 → Enter → 倒计时结束 → 用手把灯头转向人脸，保持几秒。\n"
    )
    print(f"任务目录: {task_dir}")
    print(f"计划: {args.episodes} 段 × {args.seconds} 秒 @ {args.fps} fps")
    print()

    port = args.port
    if not args.dummy:
        if port is None:
            port = guess_port()
        if port is None:
            print(
                "error: 找不到串口。用 --port /dev/ttyACM0 或 --port /dev/ttyUSB0",
                file=sys.stderr,
            )
            return 2
        warn_stop_voice_agent(port)

    joint_src: JointSource | None = None
    cam_src: CameraSource | None = None
    start_idx = next_episode_index(task_dir)
    kept = 0
    try:
        print("步骤 1/4  连接舵机并关闭力矩（这样你才能用手掰）")
        if args.dummy:
            joint_src = DummyJoints()
        else:
            joint_src = LeLampJoints(port, args.id)
        print("   ", joint_src.connect())

        print("步骤 2/4  打开灯头摄像头（模型要看灯自己看见的画面）")
        if args.dummy:
            cam_src = DummyCamera()
        else:
            cam_src = PiOrWebcam(args.camera, args.width, args.height)
        print("   ", cam_src.connect())
        print()
        if joint_src is None or cam_src is None:
            raise RuntimeError("internal: sources not created")
        while kept < args.episodes:
            ep_idx = start_idx + kept
            ep_name = f"ep_{ep_idx:03d}"
            print("-" * 60)
            print(
                f"步骤 3/4  第 {kept + 1}/{args.episodes} 段  →  {ep_name}\n"
                "   请把人/手放到这一段要用的位置，握住灯，准备好转头。"
            )
            if not args.no_prompt:
                prompt("   准备好后按 Enter 开始", default="")
                if args.countdown > 0:
                    countdown(args.countdown)
            print(f"   >> 开始录 {args.seconds} 秒，现在用手把灯转向目标")
            ep_dir = task_dir / ep_name
            if ep_dir.exists():
                ep_dir = task_dir / f"ep_{next_episode_index(task_dir):03d}"
                ep_name = ep_dir.name
            ep_dir.mkdir(parents=True, exist_ok=True)
            try:
                ts, rows = record_one(
                    joint_src,
                    cam_src,
                    seconds=args.seconds,
                    fps=args.fps,
                    rgb_dir=ep_dir / "rgb",
                )
            except KeyboardInterrupt:
                shutil.rmtree(ep_dir, ignore_errors=True)
                print("\n   本段中断，未保存。")
                if args.no_prompt:
                    break
                if prompt("   继续下一段? [Y/n]", default="Y").lower().startswith("n"):
                    break
                continue
            except Exception as exc:
                n_partial = len(list((ep_dir / "rgb").glob("*.jpg"))) if (ep_dir / "rgb").exists() else 0
                print(f"\n   本段失败: {exc}  (已落盘 {n_partial} 张图)")
                if n_partial == 0:
                    shutil.rmtree(ep_dir, ignore_errors=True)
                if args.no_prompt:
                    raise
                if prompt("   继续下一段? [Y/n]", default="Y").lower().startswith("n"):
                    break
                continue

            save_joints_csv(ep_dir, ts, rows)
            n = len(rows)
            print(f"   已写入 {ep_dir}  ({n} 帧关节 + {n} 张图)")

            if args.no_prompt:
                keep = True
            else:
                ans = prompt("步骤 4/4  保留这段? [Y = 留下 / n = 删掉重录]", default="Y")
                keep = not ans.lower().startswith("n")
            if keep:
                kept += 1
                print(
                    "   下一段请换一个位置（左/中/右、近/远），不要站在同一个地方录 50 遍。"
                )
            else:
                shutil.rmtree(ep_dir)
                print("   已删除，重新来这一段。")
    finally:
        if joint_src is not None:
            joint_src.close()
        if cam_src is not None:
            cam_src.close()

    print()
    print("=" * 60)
    print(f"录完 {kept} 段，存在 {task_dir}")
    if kept:
        print("不要在这台灯上训练。用 FileZilla/SFTP 把下面目录拷到 Mac：")
        print(f"  {task_dir}")
        print("Mac 上：")
        print(
            "  python train.py --data ./look_at_person --epochs 40 --export ./artifacts"
        )
    else:
        print("没有留下任何段。")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
