#!/usr/bin/env python3
"""Look-at policy used by plugins/lelamp/local_main.py.

Do not replace play_recording, RGB, volume, or music. local_main.py already
maps 「看我」 to run_watch_person(). Copy artifacts next to lelamp_il/:

    tiny_lamp_int8.onnx
    meta.json

The LiveKit function-tool snippet below is only a fallback if someone is
still on official main.py. The lamp that already speaks Chinese should stay
on local_main.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Printed when 「看我」 starts. If the lamp still says「6.0 秒」, this file was not copied.
WATCH_REVISION = "2026-08-28-follow"

WATCH_PERSON_PROMPT = """
看人（视觉策略，不是播放动画）：
用户说「看我」「看着我」「看这边」「看过来」「look at me」「watch me」时，
必须调用 watch_person 并一直跟着画面里的人/手，禁止用 play_recording 播 nod/scanning/curious。
用户说「停」「停止」「好了」「别看了」或点头/放音乐/关灯时停下跟随。
点头、摇头、开心扭、唤醒，继续用 play_recording。
放音乐、调音量，继续用现有工具。
用户用中文说话时，用中文简短回复。
"""


def run_watch_person(
    model: Path,
    meta: Path,
    port: str,
    seconds: float = 0.0,
    camera_index: int = 0,
    stop_event=None,
) -> str:
    """Run the ONNX policy at control_hz until stop, or for `seconds` if > 0.

    seconds <= 0 means keep following (local_main starts this in a thread and
    sets stop_event when the user says 停 / 别看了, or issues another command).
    The LiveKit agent must not also be driving MotorsService during this call:
    pause / stop motor playback first, then resume canned animations after.
    """
    from infer_pi import (
        MotorBus,
        close_camera,
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
    bounded = seconds is not None and float(seconds) > 0
    n_steps = max(1, int(round(float(seconds) * control_hz))) if bounded else None

    print(
        f"watch_person {WATCH_REVISION}  until_stop={not bounded}  "
        f"file={Path(__file__).resolve()}",
        flush=True,
    )
    sess = make_session(model)
    camera = open_camera(camera_index)
    bus = MotorBus(port, names)
    stopped = False
    try:
        step = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            if bounded and step >= n_steps:
                break
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
                if stop_event is not None:
                    if stop_event.wait(remain):
                        stopped = True
                        break
                else:
                    time.sleep(remain)
            step += 1
    finally:
        bus.close()
        close_camera(camera)
    if stopped:
        return "好，不看了。"
    if bounded:
        return f"已看向画面中的人（{float(seconds):g} 秒）。"
    return "好，不看了。"


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Run watch_person. seconds<=0 follows until Ctrl-C."
    )
    p.add_argument("--model", type=Path, default=Path("artifacts/tiny_lamp_int8.onnx"))
    p.add_argument("--meta", type=Path, default=Path("artifacts/meta.json"))
    p.add_argument("--port", default="/dev/ttyACM0")
    p.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="how long to follow; 0 means until Ctrl-C",
    )
    args = p.parse_args()
    print(run_watch_person(args.model, args.meta, args.port, args.seconds))
    sys.exit(0)
