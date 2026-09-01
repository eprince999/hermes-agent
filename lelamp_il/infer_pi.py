#!/usr/bin/env python3
"""Closed-loop inference for a TinyLampPolicy ONNX graph on Raspberry Pi Zero 2W.

Copy from the laptop after training::

    scp artifacts/tiny_lamp_int8.onnx artifacts/meta.json pi@zero.local:~/lelamp/
    ssh pi@zero.local
    cd ~/lelamp
    pip install -r requirements-pi.txt
    python infer_pi.py --model tiny_lamp_int8.onnx --meta meta.json --port /dev/ttyUSB0

``--dry-run`` runs the ONNX graph on a laptop webcam (or a black frame) without
motors, so you can confirm the export before flashing the Pi.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import numpy as np

try:
    import onnxruntime as ort
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "onnxruntime is required on the Pi.\n"
        "  pip install -r lelamp_il/requirements-pi.txt\n"
        "Use 64-bit Raspberry Pi OS Lite; 32-bit has no usable wheel."
    ) from exc

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Pillow is required: pip install pillow") from exc


def load_meta(path: Path) -> dict:
    meta = json.loads(path.read_text(encoding="utf-8"))
    for key in ("joint_mean", "joint_std", "joint_min", "joint_max", "joint_names"):
        if key not in meta:
            raise ValueError(f"{path} missing '{key}'")
    return meta


def make_session(model_path: Path) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(
        str(model_path),
        sess_options=so,
        providers=["CPUExecutionProvider"],
    )


def preprocess_image(image: Image.Image, image_size: int) -> np.ndarray:
    image = image.convert("RGB").resize((image_size, image_size), Image.BILINEAR)
    arr = np.asarray(image, dtype=np.float32) / 255.0
    arr = arr * 2.0 - 1.0
    return np.transpose(arr, (2, 0, 1))[None, ...].copy()


def grab_frame(camera, image_size: int) -> Image.Image:
    if camera is None:
        return Image.new("RGB", (image_size, image_size), (32, 32, 32))
    kind = camera[0]
    handle = camera[1]
    if kind == "lelamp":
        frame = handle.grab()
        if isinstance(frame, (bytes, bytearray)):
            from io import BytesIO

            return Image.open(BytesIO(frame)).convert("RGB")
        return frame.convert("RGB")
    if kind == "picamera2":
        arr = handle.capture_array()
        return Image.fromarray(arr)
    if kind == "cv2":
        ok, frame = handle.read()
        if not ok:
            raise RuntimeError("webcam returned an empty frame")
        rgb = frame[:, :, ::-1]
        return Image.fromarray(rgb)
    raise RuntimeError(f"unknown camera backend {kind}")


def open_camera(index: int):
    """Prefer the same rpicam-vid path as record_demo.py (CSI must not use OpenCV)."""
    try:
        from record_demo import PiOrWebcam

        cam = PiOrWebcam(index, 320, 240)
        print("   ", cam.connect(), flush=True)
        return ("lelamp", cam)
    except Exception as exc:
        print(f"record_demo camera failed ({exc}); trying picamera2/OpenCV", flush=True)
    try:
        from picamera2 import Picamera2

        cam = Picamera2()
        cam.configure(cam.create_preview_configuration(main={"size": (320, 240)}))
        cam.start()
        time.sleep(0.3)
        return ("picamera2", cam)
    except Exception:
        pass
    try:
        import cv2

        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        return ("cv2", cap)
    except Exception:
        return None


def close_camera(camera) -> None:
    if camera is None:
        return
    kind, handle = camera[0], camera[1]
    if kind == "lelamp":
        handle.close()
    elif kind == "cv2":
        handle.release()
    elif kind == "picamera2":
        stop = getattr(handle, "stop", None)
        if callable(stop):
            stop()



class MotorBus:
    """Talk to the five STS3215s in the same units as record_demo.py.

    Official lerobot FeetechMotorsBus in DEGREES mode requires a
    calibration file the lamp does not have. Recording already fell
    through to Sts3215Bus / Sts3215RawBus; inference must use that path
    too, or read_joints() raises ``has no calibration registered``.
    """

    def __init__(self, port: str | None, joint_names: list[str]) -> None:
        self.port = port
        self.joint_names = joint_names
        self._bus = None
        self._sts = None
        self._dummy = np.zeros(len(joint_names), dtype=np.float32)
        if not port:
            print("no --port: running without motors")
            return
        errors: list[str] = []
        if self._connect_sts():
            return
        if self._connect_lerobot(errors):
            return
        print("舵机回退失败: " + " | ".join(errors) + "；dummy joints only")

    def _connect_sts(self) -> bool:
        from feetech_bus import Sts3215Bus, Sts3215RawBus, add_runtime_site_packages

        add_runtime_site_packages()
        for cls in (Sts3215Bus, Sts3215RawBus):
            try:
                sts = cls(self.port, tuple(self.joint_names))
                label = sts.connect()
                sts.enable_torque()
                probe = sts.read_degrees()
                self._sts = sts
                pretty = " ".join(
                    f"{name}={probe[name]:+6.1f}" for name in self.joint_names
                )
                print(f"{label}  力矩已开（无需 lerobot 校准）  当前={pretty}")
                return True
            except Exception as exc:
                print(f"    {cls.__name__} 不可用: {exc}", flush=True)
        return False

    def _connect_lerobot(self, errors: list[str]) -> bool:
        try:
            from lerobot.motors import Motor, MotorNormMode
            from lerobot.motors.feetech import FeetechMotorsBus
        except Exception as exc:
            errors.append(f"lerobot import: {exc}")
            return False
        bus = None
        try:
            motors = {
                name: Motor(i + 1, "sts3215", MotorNormMode.DEGREES)
                for i, name in enumerate(self.joint_names)
            }
            bus = FeetechMotorsBus(port=self.port, motors=motors)
            bus.connect()
            present = bus.sync_read("Present_Position")
            self._bus = bus
            print(f"connected Feetech bus on {self.port}")
            _ = present
            return True
        except Exception as exc:
            errors.append(f"lerobot Feetech: {exc}")
            if bus is not None:
                try:
                    bus.disconnect(True)
                except TypeError:
                    try:
                        bus.disconnect()
                    except Exception:
                        pass
                except Exception:
                    pass
            self._bus = None
            return False

    def read_joints(self) -> np.ndarray:
        if self._sts is not None:
            pose = self._sts.read_degrees()
            return np.asarray(
                [float(pose[name]) for name in self.joint_names], dtype=np.float32
            )
        if self._bus is None:
            return self._dummy.copy()
        present = self._bus.sync_read("Present_Position")
        return np.asarray(
            [float(present[name]) for name in self.joint_names], dtype=np.float32
        )

    def write_joints(self, targets: np.ndarray) -> None:
        if self._sts is not None:
            goal = {name: float(targets[i]) for i, name in enumerate(self.joint_names)}
            self._sts.write_degrees(goal)
            return
        if self._bus is None:
            self._dummy = targets.astype(np.float32)
            return
        goal = {name: float(targets[i]) for i, name in enumerate(self.joint_names)}
        self._bus.sync_write("Goal_Position", goal)

    def close(self) -> None:
        if self._sts is not None:
            self._sts.close()
            self._sts = None
        if self._bus is not None:
            self._bus.disconnect(True)
            self._bus = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run TinyLampPolicy ONNX on a Pi Zero 2W.")
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--meta", type=Path, required=True)
    p.add_argument("--port", default=None, help="Feetech serial port, e.g. /dev/ttyUSB0")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--dry-run", action="store_true", help="No motors; print actions.")
    p.add_argument(
        "--steps",
        type=int,
        default=0,
        help="Stop after N control steps. 0 = run until Ctrl-C.",
    )
    p.add_argument(
        "--execute-chunk",
        type=int,
        default=1,
        help="How many predicted steps to execute before the next inference. 1 is safest.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    meta = load_meta(args.meta)
    image_size = int(meta["image_size"])
    chunk_size = int(meta["chunk_size"])
    control_hz = float(meta["control_hz"])
    mean = np.asarray(meta["joint_mean"], dtype=np.float32)
    std = np.asarray(meta["joint_std"], dtype=np.float32)
    jmin = np.asarray(meta["joint_min"], dtype=np.float32)
    jmax = np.asarray(meta["joint_max"], dtype=np.float32)
    names = list(meta["joint_names"])
    n_exec = max(1, min(args.execute_chunk, chunk_size))
    period = 1.0 / max(control_hz, 1.0)

    sess = make_session(args.model)
    camera = open_camera(args.camera)
    if camera is None:
        print("no camera found; feeding a black frame (proprio-only behaviour)")
    bus = MotorBus(None if args.dry_run else args.port, names)

    print(
        f"model={args.model}  vision={meta.get('vision')}  "
        f"{control_hz} Hz  chunk={chunk_size}  execute={n_exec}"
    )
    step = 0
    try:
        while args.steps == 0 or step < args.steps:
            t0 = time.perf_counter()
            frame = grab_frame(camera, image_size)
            image = preprocess_image(frame, image_size)
            joints = bus.read_joints()
            joints_n = ((joints - mean) / std).reshape(1, -1)
            chunk = sess.run(
                ["action_chunk"],
                {"image": image, "joints": joints_n},
            )[0][0]
            chunk = chunk * std + mean
            chunk = np.clip(chunk, jmin, jmax)
            for k in range(n_exec):
                target = chunk[k]
                bus.write_joints(target)
                if args.dry_run and k == 0:
                    pretty = " ".join(
                        f"{n}={target[i]:+6.1f}" for i, n in enumerate(names)
                    )
                    print(f"step {step:04d}  {pretty}")
                if k + 1 < n_exec:
                    remain = period - (time.perf_counter() - t0)
                    if remain > 0:
                        time.sleep(remain)
                    t0 = time.perf_counter()
            step += 1
            remain = period - (time.perf_counter() - t0)
            if remain > 0:
                time.sleep(remain)
    except KeyboardInterrupt:
        print("\nstop")
    finally:
        bus.close()
        close_camera(camera)
    return 0


if __name__ == "__main__":
    sys.exit(main())
