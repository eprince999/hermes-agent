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
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

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
    def grab(self) -> Image.Image: ...
    def close(self) -> None: ...


class LeLampJoints(JointSource):
    """Loose (torque-off) Feetech bus — you can move the lamp by hand."""

    def __init__(self, port: str, lamp_id: str) -> None:
        self.port = port
        self.lamp_id = lamp_id
        self._leader = None
        self._bus = None
        self._names = list(JOINT_NAMES)

    def connect(self) -> str:
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
                return (
                    f"Feetech {self.port}  力矩已关闭，可用手摆  "
                    f"(LeLampLeader 不可用: {leader_exc.__class__.__name__})"
                )
            except Exception as bus_exc:
                raise RuntimeError(
                    f"无法连接舵机。LeLampLeader: {leader_exc}; Feetech: {bus_exc}\n"
                    "没有灯时请加 --dummy。"
                ) from bus_exc

    def read(self) -> dict[str, float]:
        if self._leader is not None:
            raw = self._leader.get_action()
            return {name: float(raw[f"{name}.pos"]) for name in self._names}
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


class PiOrWebcam(CameraSource):
    def __init__(self, index: int, width: int, height: int) -> None:
        self.index = index
        self.width = width
        self.height = height
        self._kind = None
        self._handle = None

    def connect(self) -> str:
        try:
            from picamera2 import Picamera2

            cam = Picamera2()
            cam.configure(
                cam.create_preview_configuration(
                    main={"size": (self.width, self.height), "format": "RGB888"}
                )
            )
            cam.start()
            time.sleep(0.4)
            self._kind, self._handle = "picamera2", cam
            return f"Pi Camera {self.width}x{self.height}"
        except Exception:
            pass
        try:
            import cv2

            cap = cv2.VideoCapture(self.index)
            if not cap.isOpened():
                raise RuntimeError(f"打不开摄像头 index={self.index}")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            ok, _ = cap.read()
            if not ok:
                cap.release()
                raise RuntimeError("摄像头读不到画面")
            self._kind, self._handle = "cv2", cap
            return f"OpenCV 摄像头 {self.index}"
        except Exception as exc:
            raise RuntimeError(
                f"没有摄像头可用 ({exc}). 装 opencv-python 或 picamera2，或加 --dummy。"
            ) from exc

    def grab(self) -> Image.Image:
        if self._kind == "picamera2":
            arr = self._handle.capture_array()
            return Image.fromarray(arr).convert("RGB")
        ok, frame = self._handle.read()
        if not ok:
            raise RuntimeError("摄像头丢帧")
        rgb = frame[:, :, ::-1]
        return Image.fromarray(rgb)

    def close(self) -> None:
        if self._kind == "cv2" and self._handle is not None:
            self._handle.release()
        if self._kind == "picamera2" and self._handle is not None:
            self._handle.stop()
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


def save_episode(
    ep_dir: Path,
    timestamps: list[float],
    joints_rows: list[dict[str, float]],
    frames: list[Image.Image],
) -> None:
    rgb_dir = ep_dir / "rgb"
    rgb_dir.mkdir(parents=True, exist_ok=True)
    csv_path = ep_dir / "joints.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for i, (ts, joints) in enumerate(zip(timestamps, joints_rows)):
            row = {"timestamp": f"{ts:.6f}"}
            for name in JOINT_NAMES:
                row[f"{name}.pos"] = f"{joints[name]:.4f}"
            writer.writerow(row)
            frames[i].convert("RGB").save(rgb_dir / f"{i:06d}.jpg", quality=90)


def record_one(
    joints: JointSource,
    camera: CameraSource,
    seconds: float,
    fps: int,
) -> tuple[list[float], list[dict[str, float]], list[Image.Image]]:
    n_target = max(1, int(round(seconds * fps)))
    period = 1.0 / fps
    t0 = time.perf_counter()
    timestamps: list[float] = []
    rows: list[dict[str, float]] = []
    frames: list[Image.Image] = []
    for i in range(n_target):
        tick = time.perf_counter()
        pose = joints.read()
        frame = camera.grab()
        timestamps.append(tick - t0)
        rows.append(pose)
        frames.append(frame)
        if i == 0 or (i + 1) % fps == 0 or i + 1 == n_target:
            print(
                f"    {i + 1:>4}/{n_target}  {_fmt_joints(pose)}",
                flush=True,
            )
        remain = period - (time.perf_counter() - tick)
        if remain > 0:
            time.sleep(remain)
    return timestamps, rows, frames


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
    args = parse_args(argv)
    task_dir = (args.out / args.task).resolve()
    task_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("LeLamp 示教录制（接在已有中文语音灯上，只补「看人」）")
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

    print("步骤 1/4  连接舵机并关闭力矩（这样你才能用手掰）")
    if args.dummy:
        joint_src: JointSource = DummyJoints()
    else:
        joint_src = LeLampJoints(port, args.id)
    print("   ", joint_src.connect())

    print("步骤 2/4  打开灯头摄像头（模型要看灯自己看见的画面）")
    if args.dummy:
        cam_src: CameraSource = DummyCamera()
    else:
        cam_src = PiOrWebcam(args.camera, args.width, args.height)
    print("   ", cam_src.connect())
    print()

    start_idx = next_episode_index(task_dir)
    kept = 0
    try:
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
            try:
                ts, rows, frames = record_one(
                    joint_src, cam_src, seconds=args.seconds, fps=args.fps
                )
            except KeyboardInterrupt:
                print("\n   本段中断，未保存。")
                if args.no_prompt:
                    break
                if prompt("   继续下一段? [Y/n]", default="Y").lower().startswith("n"):
                    break
                continue

            ep_dir = task_dir / ep_name
            if ep_dir.exists():
                # should not happen with next_episode_index, but be safe
                ep_dir = task_dir / f"ep_{next_episode_index(task_dir):03d}"
                ep_name = ep_dir.name
            save_episode(ep_dir, ts, rows, frames)
            n = len(frames)
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
        joint_src.close()
        cam_src.close()

    print()
    print("=" * 60)
    print(f"录完 {kept} 段，存在 {task_dir}")
    if kept:
        print("下一步训练:")
        print(
            f"  python train.py --data {task_dir} --epochs 40 --export ./artifacts"
        )
    else:
        print("没有留下任何段。")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
