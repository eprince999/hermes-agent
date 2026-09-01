#!/usr/bin/env python3
"""Train a Pi Zero 2W-sized LeLamp imitation policy on a laptop.

Why this file exists
--------------------
LeRobot ACT / Diffusion are the right policies for a Pi 4/5 or a NUC, but a
Raspberry Pi Zero 2W has 512 MB RAM, four Cortex-A53 cores at 1 GHz, and no
NPU. A ResNet18+transformer will not run there in any useful closed loop.

This script trains a ~0.4M parameter CNN+MLP that:

* consumes one 96x96 RGB frame + 5 joint positions
* predicts an action chunk of future joint targets (absolute positions)
* exports a fixed-shape ONNX graph (and optional INT8 weights)
* is small enough to scp onto a Zero 2W and run at ~8-12 Hz

Data layout (one task directory)
--------------------------------
    data/look_at_person/
      ep_000/
        joints.csv          # LeLamp record.py format
        rgb/000000.jpg      # optional; frame i <-> joints row i
      ep_001/
        ...

``joints.csv`` header (written by ``uv run -m lelamp.record``)::

    timestamp,base_yaw.pos,base_pitch.pos,elbow_pitch.pos,wrist_roll.pos,wrist_pitch.pos

Vision-free CSV folders are accepted: the policy then uses joints only. A
look-at / follow-hand skill *requires* images.

Typical laptop run
------------------
    python train.py --data ./data/look_at_person --epochs 40 --export ./artifacts

Dry-run (no robot, no dataset)
------------------------------
    python train.py --synthetic --epochs 2 --export ./artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch.utils.data import DataLoader, Dataset, random_split
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyTorch is required on the laptop.\n"
        "  pip install -r lelamp_il/requirements-train.txt"
    ) from exc

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Pillow is required to load demonstration frames.\n"
        "  pip install -r lelamp_il/requirements-train.txt"
    ) from exc


JOINT_NAMES = (
    "base_yaw",
    "base_pitch",
    "elbow_pitch",
    "wrist_roll",
    "wrist_pitch",
)
N_JOINTS = len(JOINT_NAMES)


# ---------------------------------------------------------------------------
# Model — sized for Pi Zero 2W (INT8 ONNX, 96x96, batch=1)
# ---------------------------------------------------------------------------


class TinyLampPolicy(nn.Module):
    """Visual-motor behavioral clone with a short action chunk.

    Parameters (~400k)
    ------------------
    image   : (B, 3, H, W) float32 in [-1, 1]
    joints  : (B, 5)       float32, z-scored with dataset mean/std
    output  : (B, chunk, 5) z-scored future absolute joint targets
    """

    def __init__(
        self,
        n_joints: int = N_JOINTS,
        chunk_size: int = 8,
        image_size: int = 96,
        vision: bool = True,
    ) -> None:
        super().__init__()
        self.n_joints = n_joints
        self.chunk_size = chunk_size
        self.image_size = image_size
        self.vision = vision

        if vision:
            self.backbone = nn.Sequential(
                nn.Conv2d(3, 16, 3, stride=2, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(16, 32, 3, stride=2, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(32, 64, 3, stride=2, padding=1),
                nn.ReLU(inplace=True),
                nn.Conv2d(64, 64, 3, stride=2, padding=1),
                nn.ReLU(inplace=True),
            )
            spatial = image_size // 16
            self.visual_fc = nn.Linear(64 * spatial * spatial, 128)
            fused = 128 + 32
        else:
            self.backbone = None
            self.visual_fc = None
            fused = 32

        self.joint_fc = nn.Linear(n_joints, 32)
        self.head = nn.Sequential(
            nn.Linear(fused, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, chunk_size * n_joints),
        )

    def forward(self, image: torch.Tensor, joints: torch.Tensor) -> torch.Tensor:
        proprio = F.relu(self.joint_fc(joints))
        if self.vision:
            feat = self.backbone(image)
            feat = feat.flatten(1)
            feat = F.relu(self.visual_fc(feat))
            fused = torch.cat([feat, proprio], dim=-1)
        else:
            fused = proprio
        out = self.head(fused)
        return out.view(-1, self.chunk_size, self.n_joints)

    def count_parameters(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)


class _OnnxWrapper(nn.Module):
    """Fixed two-input graph so onnxruntime does not need dynamic axes."""

    def __init__(self, policy: TinyLampPolicy) -> None:
        super().__init__()
        self.policy = policy

    def forward(self, image: torch.Tensor, joints: torch.Tensor) -> torch.Tensor:
        return self.policy(image, joints)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def _pos_columns(fieldnames: Iterable[str]) -> list[str]:
    cols = []
    by_name = {name: None for name in JOINT_NAMES}
    for raw in fieldnames:
        key = raw.strip()
        stem = key.removesuffix(".pos")
        if stem in by_name and by_name[stem] is None:
            by_name[stem] = key
    for name in JOINT_NAMES:
        if by_name[name] is None:
            raise ValueError(
                f"joints.csv is missing '{name}.pos'. "
                f"Found columns: {list(fieldnames)}"
            )
        cols.append(by_name[name])
    return cols


def _load_joints_csv(path: Path) -> np.ndarray:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header")
        cols = _pos_columns(reader.fieldnames)
        rows = []
        for row in reader:
            rows.append([float(row[c]) for c in cols])
    if not rows:
        raise ValueError(f"{path} is empty")
    return np.asarray(rows, dtype=np.float32)


def _list_frames(rgb_dir: Path) -> list[Path]:
    frames = sorted(
        p
        for p in rgb_dir.iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    return frames


def _discover_episodes(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"data directory not found: {root}")
    episodes = []
    # Nested: root/ep_xxx/joints.csv
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "joints.csv").is_file():
            episodes.append(child)
    # Flat: root/joints.csv (single recording from lelamp.record)
    if (root / "joints.csv").is_file():
        episodes.append(root)
    # Also accept a directory of CSV files (lelamp_runtime/lelamp/recordings)
    for csv_path in sorted(root.glob("*.csv")):
        if csv_path.name == "joints.csv":
            continue
        episodes.append(csv_path)
    if not episodes:
        raise FileNotFoundError(
            f"No episodes under {root}. Expected ep_*/joints.csv "
            "or a folder of LeLamp CSV recordings."
        )
    return episodes


@dataclass
class SampleIndex:
    joints: np.ndarray
    frame: Path | None
    action: np.ndarray  # (chunk, n_joints)


class LampChunkDataset(Dataset):
    def __init__(
        self,
        samples: list[SampleIndex],
        image_size: int,
        vision: bool,
        augment: bool,
        joint_mean: np.ndarray,
        joint_std: np.ndarray,
    ) -> None:
        self.samples = samples
        self.image_size = image_size
        self.vision = vision
        self.augment = augment
        self.joint_mean = joint_mean.astype(np.float32)
        self.joint_std = joint_std.astype(np.float32)

    def __len__(self) -> int:
        return len(self.samples)

    def _load_image(self, path: Path) -> torch.Tensor:
        image = Image.open(path).convert("RGB")
        if self.augment:
            # Photometric jitter only. Do not flip: base_yaw is left/right
            # and a mirrored frame would teach the lamp to look the wrong way.
            arr = np.asarray(image, dtype=np.float32)
            arr = arr * random.uniform(0.8, 1.2)
            arr = arr + random.uniform(-12.0, 12.0)
            arr = np.clip(arr, 0, 255).astype(np.uint8)
            image = Image.fromarray(arr)
        image = image.resize((self.image_size, self.image_size), Image.BILINEAR)
        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = arr * 2.0 - 1.0
        return torch.from_numpy(arr.transpose(2, 0, 1).copy())

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        sample = self.samples[idx]
        joints = (sample.joints - self.joint_mean) / self.joint_std
        action = (sample.action - self.joint_mean) / self.joint_std
        if self.vision:
            if sample.frame is None:
                raise RuntimeError("vision=True but a sample has no frame")
            image = self._load_image(sample.frame)
        else:
            image = torch.zeros(3, self.image_size, self.image_size)
        return {
            "image": image,
            "joints": torch.from_numpy(joints),
            "action": torch.from_numpy(action),
        }


def _subsample(values: np.ndarray, stride: int) -> np.ndarray:
    if stride <= 1:
        return values
    return values[::stride]


def build_samples(
    data_dir: Path,
    chunk_size: int,
    record_fps: int,
    control_hz: int,
) -> tuple[list[SampleIndex], bool]:
    stride = max(1, round(record_fps / control_hz))
    samples: list[SampleIndex] = []
    saw_vision = False
    skipped = 0

    for episode in _discover_episodes(data_dir):
        if episode.suffix.lower() == ".csv":
            joints = _load_joints_csv(episode)
            frames: list[Path] = []
        else:
            joints = _load_joints_csv(episode / "joints.csv")
            rgb_dir = episode / "rgb"
            frames = _list_frames(rgb_dir) if rgb_dir.is_dir() else []

        joints = _subsample(joints, stride)
        if frames:
            frames = frames[::stride]
            n = min(len(joints), len(frames))
            joints = joints[:n]
            frames = frames[:n]
            saw_vision = True
        else:
            frames = [None] * len(joints)  # type: ignore[list-item]

        if len(joints) <= chunk_size:
            skipped += 1
            continue

        for t in range(0, len(joints) - chunk_size):
            samples.append(
                SampleIndex(
                    joints=joints[t],
                    frame=frames[t],
                    action=joints[t + 1 : t + 1 + chunk_size],
                )
            )

    if saw_vision:
        samples = [s for s in samples if s.frame is not None]

    if not samples:
        raise RuntimeError(
            f"No training samples in {data_dir} "
            f"(skipped {skipped} too-short episodes). "
            "Record longer demonstrations or lower --chunk-size."
        )
    return samples, saw_vision


def synthetic_samples(
    n_episodes: int,
    length: int,
    chunk_size: int,
    image_size: int,
    tmp_dir: Path,
) -> tuple[list[SampleIndex], bool]:
    """Deterministic fake lamp: joints track a slow sinusoid, frames are solid color."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    samples: list[SampleIndex] = []
    for ep in range(n_episodes):
        t = np.linspace(0, 4 * math.pi, length, dtype=np.float32)
        phase = ep * 0.35
        joints = np.stack(
            [
                30 * np.sin(t + phase),
                10 * np.cos(t + phase),
                20 * np.sin(2 * t + phase),
                15 * np.cos(1.5 * t),
                8 * np.sin(0.7 * t + phase),
            ],
            axis=1,
        ).astype(np.float32)
        rgb_dir = tmp_dir / f"ep_{ep:03d}" / "rgb"
        rgb_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(length):
            color = (
                int(127 + 80 * math.sin(t[i])),
                int(127 + 80 * math.cos(t[i])),
                90,
            )
            path = rgb_dir / f"{i:06d}.jpg"
            Image.new("RGB", (image_size, image_size), color).save(path, quality=85)
            frames.append(path)
        for i in range(0, length - chunk_size):
            samples.append(
                SampleIndex(
                    joints=joints[i],
                    frame=frames[i],
                    action=joints[i + 1 : i + 1 + chunk_size],
                )
            )
    return samples, True


# ---------------------------------------------------------------------------
# Train / eval
# ---------------------------------------------------------------------------


@dataclass
class TrainMeta:
    joint_names: list[str]
    n_joints: int
    chunk_size: int
    image_size: int
    vision: bool
    control_hz: int
    record_fps: int
    joint_mean: list[float]
    joint_std: list[float]
    joint_min: list[float]
    joint_max: list[float]
    model: str
    parameters: int
    best_val_l1: float
    trained_at: str
    onnx_inputs: dict[str, list[int]]
    onnx_output: list[int]


def _l1_deg(pred: torch.Tensor, target: torch.Tensor, std: torch.Tensor) -> torch.Tensor:
    """L1 in original joint units (degrees if the CSV is in degrees)."""
    return (pred - target).abs().mul(std).mean()


def run_epoch(
    model: TinyLampPolicy,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer | None,
    device: torch.device,
    joint_std: torch.Tensor,
) -> dict[str, float]:
    train = optimizer is not None
    model.train(train)
    total_loss = 0.0
    total_l1 = 0.0
    n = 0
    for batch in loader:
        image = batch["image"].to(device, non_blocking=True)
        joints = batch["joints"].to(device, non_blocking=True)
        action = batch["action"].to(device, non_blocking=True)
        pred = model(image, joints)
        loss = F.l1_loss(pred, action)
        # first-step extra weight: the Pi executes this immediately
        loss = loss + 0.5 * F.l1_loss(pred[:, 0], action[:, 0])
        if train:
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        bs = image.size(0)
        total_loss += loss.item() * bs
        total_l1 += _l1_deg(pred.detach(), action, joint_std).item() * bs
        n += bs
    return {"loss": total_loss / max(n, 1), "l1": total_l1 / max(n, 1)}


def export_onnx(
    model: TinyLampPolicy,
    path: Path,
    image_size: int,
    n_joints: int,
    opset: int = 17,
) -> None:
    model.eval()
    wrapper = _OnnxWrapper(model).to("cpu").eval()
    dummy_image = torch.zeros(1, 3, image_size, image_size)
    dummy_joints = torch.zeros(1, n_joints)
    path.parent.mkdir(parents=True, exist_ok=True)
    export_kwargs = dict(
        input_names=["image", "joints"],
        output_names=["action_chunk"],
        opset_version=opset,
        do_constant_folding=True,
    )
    try:
        torch.onnx.export(
            wrapper,
            (dummy_image, dummy_joints),
            str(path),
            dynamo=False,
            **export_kwargs,
        )
    except TypeError:
        torch.onnx.export(
            wrapper,
            (dummy_image, dummy_joints),
            str(path),
            **export_kwargs,
        )


def quantize_dynamic(src: Path, dest: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic as _qd

    dest.parent.mkdir(parents=True, exist_ok=True)
    _qd(
        model_input=str(src),
        model_output=str(dest),
        weight_type=QuantType.QInt8,
    )


def _verify_onnx(path: Path, image_size: int, n_joints: int, chunk_size: int) -> None:
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    image = np.zeros((1, 3, image_size, image_size), dtype=np.float32)
    joints = np.zeros((1, n_joints), dtype=np.float32)
    out = sess.run(["action_chunk"], {"image": image, "joints": joints})[0]
    if out.shape != (1, chunk_size, n_joints):
        raise RuntimeError(f"ONNX output shape {out.shape} != {(1, chunk_size, n_joints)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Train a Pi Zero 2W LeLamp imitation policy and export ONNX.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Task directory (ep_*/joints.csv[+rgb] or a folder of LeLamp CSVs).",
    )
    p.add_argument(
        "--synthetic",
        action="store_true",
        help="Train on generated sinusoid data to verify the pipeline.",
    )
    p.add_argument("--export", type=Path, default=Path("artifacts"))
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--val-split", type=float, default=0.15)
    p.add_argument("--chunk-size", type=int, default=8)
    p.add_argument("--image-size", type=int, default=96)
    p.add_argument("--record-fps", type=int, default=30, help="FPS used while recording.")
    p.add_argument(
        "--control-hz",
        type=int,
        default=10,
        help="Closed-loop rate on the Pi. Demonstrations are subsampled to this.",
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--num-workers", type=int, default=0)
    p.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda", "mps"),
    )
    p.add_argument(
        "--no-vision",
        action="store_true",
        help="Force proprio-only policy even if RGB frames exist.",
    )
    p.add_argument(
        "--no-int8",
        action="store_true",
        help="Skip dynamic INT8 quantization (FP32 ONNX only).",
    )
    p.add_argument(
        "--patience",
        type=int,
        default=8,
        help="Early-stop patience in epochs. 0 disables.",
    )
    return p.parse_args(argv)


def resolve_device(name: str) -> torch.device:
    if name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    return torch.device(name)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.synthetic and args.data is None:
        print("error: provide --data PATH or --synthetic", file=sys.stderr)
        return 2

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    device = resolve_device(args.device)
    export_dir = args.export
    export_dir.mkdir(parents=True, exist_ok=True)

    if args.synthetic:
        syn_root = export_dir / "_synthetic"
        samples, has_vision = synthetic_samples(
            n_episodes=6,
            length=80,
            chunk_size=args.chunk_size,
            image_size=args.image_size,
            tmp_dir=syn_root,
        )
        print(f"synthetic dataset: {len(samples)} chunks under {syn_root}")
    else:
        samples, has_vision = build_samples(
            data_dir=args.data.expanduser().resolve(),
            chunk_size=args.chunk_size,
            record_fps=args.record_fps,
            control_hz=args.control_hz,
        )

    vision = has_vision and not args.no_vision
    if has_vision is False and not args.no_vision:
        print(
            "warning: no rgb/ frames found; training a proprio-only policy. "
            "Look-at / follow-hand skills need cameras."
        )

    all_joints = np.stack([s.joints for s in samples], axis=0)
    joint_mean = all_joints.mean(axis=0)
    joint_std = all_joints.std(axis=0)
    joint_std = np.maximum(joint_std, 1e-3)
    joint_min = all_joints.min(axis=0)
    joint_max = all_joints.max(axis=0)

    dataset = LampChunkDataset(
        samples=samples,
        image_size=args.image_size,
        vision=vision,
        augment=vision,
        joint_mean=joint_mean,
        joint_std=joint_std,
    )

    n_val = max(1, int(len(dataset) * args.val_split))
    n_train = len(dataset) - n_val
    if n_train < 1:
        raise RuntimeError(
            f"Not enough samples ({len(dataset)}) for a train/val split. "
            "Record more episodes."
        )
    train_set, val_set = random_split(
        dataset,
        [n_train, n_val],
        generator=torch.Generator().manual_seed(args.seed),
    )
    val_dataset = LampChunkDataset(
        samples=[dataset.samples[i] for i in val_set.indices],
        image_size=args.image_size,
        vision=vision,
        augment=False,
        joint_mean=joint_mean,
        joint_std=joint_std,
    )
    train_dataset = LampChunkDataset(
        samples=[dataset.samples[i] for i in train_set.indices],
        image_size=args.image_size,
        vision=vision,
        augment=vision,
        joint_mean=joint_mean,
        joint_std=joint_std,
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        drop_last=False,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
    )

    model = TinyLampPolicy(
        n_joints=N_JOINTS,
        chunk_size=args.chunk_size,
        image_size=args.image_size,
        vision=vision,
    ).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(args.epochs, 1))
    std_t = torch.from_numpy(joint_std).to(device).view(1, 1, -1)

    print(
        f"device={device}  samples={len(samples)}  "
        f"train={len(train_dataset)} val={len(val_dataset)}  "
        f"vision={vision}  params={model.count_parameters():,}"
    )

    best_l1 = math.inf
    best_path = export_dir / "best.pt"
    stale = 0
    t0 = time.time()
    for epoch in range(1, args.epochs + 1):
        # restore train-time augmentation
        train_stats = run_epoch(model, train_loader, optimizer, device, std_t)
        val_stats = run_epoch(model, val_loader, None, device, std_t)
        scheduler.step()
        mark = ""
        if val_stats["l1"] < best_l1:
            best_l1 = val_stats["l1"]
            stale = 0
            torch.save(
                {
                    "state_dict": model.state_dict(),
                    "vision": vision,
                    "chunk_size": args.chunk_size,
                    "image_size": args.image_size,
                    "n_joints": N_JOINTS,
                    "joint_mean": joint_mean,
                    "joint_std": joint_std,
                },
                best_path,
            )
            mark = "  * best"
        else:
            stale += 1
        print(
            f"epoch {epoch:03d}/{args.epochs}  "
            f"train_l1={train_stats['l1']:.3f}  val_l1={val_stats['l1']:.3f}  "
            f"lr={scheduler.get_last_lr()[0]:.2e}{mark}"
        )
        if args.patience and stale >= args.patience:
            print(f"early stop after {epoch} epochs (patience={args.patience})")
            break

    try:
        ckpt = torch.load(best_path, map_location="cpu", weights_only=False)
    except TypeError:
        ckpt = torch.load(best_path, map_location="cpu")
    model.load_state_dict(ckpt["state_dict"])
    model.to("cpu").eval()

    onnx_fp32 = export_dir / "tiny_lamp.onnx"
    export_onnx(model, onnx_fp32, args.image_size, N_JOINTS)
    _verify_onnx(onnx_fp32, args.image_size, N_JOINTS, args.chunk_size)
    print(f"wrote {onnx_fp32}  ({onnx_fp32.stat().st_size / 1024:.1f} KB)")

    onnx_int8 = export_dir / "tiny_lamp_int8.onnx"
    if not args.no_int8:
        try:
            quantize_dynamic(onnx_fp32, onnx_int8)
            _verify_onnx(onnx_int8, args.image_size, N_JOINTS, args.chunk_size)
            print(f"wrote {onnx_int8}  ({onnx_int8.stat().st_size / 1024:.1f} KB)")
        except Exception as exc:
            print(f"INT8 quantization skipped: {exc}")
            onnx_int8 = onnx_fp32

    meta = TrainMeta(
        joint_names=list(JOINT_NAMES),
        n_joints=N_JOINTS,
        chunk_size=args.chunk_size,
        image_size=args.image_size,
        vision=vision,
        control_hz=args.control_hz,
        record_fps=args.record_fps,
        joint_mean=joint_mean.tolist(),
        joint_std=joint_std.tolist(),
        joint_min=joint_min.tolist(),
        joint_max=joint_max.tolist(),
        model="TinyLampPolicy",
        parameters=model.count_parameters(),
        best_val_l1=float(best_l1),
        trained_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        onnx_inputs={
            "image": [1, 3, args.image_size, args.image_size],
            "joints": [1, N_JOINTS],
        },
        onnx_output=[1, args.chunk_size, N_JOINTS],
    )
    meta_path = export_dir / "meta.json"
    meta_path.write_text(json.dumps(asdict(meta), indent=2), encoding="utf-8")
    print(f"wrote {meta_path}")
    print(
        f"done in {time.time() - t0:.1f}s  best_val_l1={best_l1:.3f} (joint units)\n"
        f"copy to the Pi:\n"
        f"  scp {onnx_int8} {meta_path} pi@zero.local:~/lelamp/"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
