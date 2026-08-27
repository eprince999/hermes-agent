#!/usr/bin/env python3
"""Hook a trained look-at policy into an existing LeLamp LiveKit agent.

Do not replace play_recording, RGB, volume, or music. Add one extra tool:

    from agent_hook import run_watch_person, WATCH_PERSON_PROMPT

    @function_tool
    async def watch_person(self, seconds: int = 6) -> str:
        \"\"\"Look at the person visible in the head camera.\"\"\"
        return run_watch_person(
            model=Path("artifacts/tiny_lamp_int8.onnx"),
            meta=Path("artifacts/meta.json"),
            port="/dev/ttyACM0",
            seconds=seconds,
        )

Paste WATCH_PERSON_PROMPT into the agent instructions (Chinese + English).
Stop the canned scan/nod from being used for 「看我」.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

WATCH_PERSON_PROMPT = """
看人（视觉策略，不是播放动画）：
用户说「看我」「看着我」「看这边」「看过来」「look at me」「watch me」时，
必须调用 watch_person，禁止用 play_recording 播 nod/scanning/curious。
点头、摇头、开心扭、唤醒，继续用 play_recording。
放音乐、调音量，继续用现有工具。
用户用中文说话时，用中文简短回复。
"""


def run_watch_person(
    model: Path,
    meta: Path,
    port: str,
    seconds: float = 6.0,
    camera_index: int = 0,
) -> str:
    """Block for `seconds` while the ONNX policy tracks the person, then return.

    The LiveKit agent must not also be driving MotorsService during this call:
    pause / stop motor playback first, then resume canned animations after.
    """
    from infer_pi import (
        MotorBus,
        grab_frame,
        load_meta,
        make_session,
        open_camera,
        preprocess_image,
    )
    import time

    import numpy as np

    model = Path(model)
    meta_path = Path(meta)
    if not model.is_file():
        return f"找不到模型 {model}。先跑 train.py 并 scp 到灯上。"
    if not meta_path.is_file():
        return f"找不到 {meta_path}。"

    cfg = load_meta(meta_path)
    image_size = int(cfg["image_size"])
    control_hz = float(cfg["control_hz"])
    mean = np.asarray(cfg["joint_mean"], dtype=np.float32)
    std = np.asarray(cfg["joint_std"], dtype=np.float32)
    jmin = np.asarray(cfg["joint_min"], dtype=np.float32)
    jmax = np.asarray(cfg["joint_max"], dtype=np.float32)
    names = list(cfg["joint_names"])
    period = 1.0 / max(control_hz, 1.0)
    n_steps = max(1, int(round(seconds * control_hz)))

    sess = make_session(model)
    camera = open_camera(camera_index)
    bus = MotorBus(port, names)
    try:
        for _ in range(n_steps):
            t0 = time.perf_counter()
            frame = grab_frame(camera, image_size)
            image = preprocess_image(frame, image_size)
            joints = bus.read_joints()
            joints_n = ((joints - mean) / std).reshape(1, -1)
            chunk = sess.run(
                ["action_chunk"],
                {"image": image, "joints": joints_n},
            )[0][0]
            target = np.clip(chunk[0] * std + mean, jmin, jmax)
            bus.write_joints(target)
            remain = period - (time.perf_counter() - t0)
            if remain > 0:
                time.sleep(remain)
    finally:
        bus.close()
        if camera is not None and camera[0] == "cv2":
            camera[1].release()
        if camera is not None and camera[0] == "picamera2":
            camera[1].stop()
    return f"已看向画面中的人（{seconds} 秒）。"


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Run watch_person once, then exit.")
    p.add_argument("--model", type=Path, default=Path("artifacts/tiny_lamp_int8.onnx"))
    p.add_argument("--meta", type=Path, default=Path("artifacts/meta.json"))
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument("--seconds", type=float, default=6.0)
    args = p.parse_args()
    print(run_watch_person(args.model, args.meta, args.port, args.seconds))
    sys.exit(0)
