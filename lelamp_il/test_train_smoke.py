#!/usr/bin/env python3
"""Smoke tests for lelamp_il.train — no robot, no GPU."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from train import (  # noqa: E402
    JOINT_NAMES,
    TinyLampPolicy,
    _load_joints_csv,
    build_samples,
    export_onnx,
    main,
)


def _write_episode(root: Path, n: int, with_rgb: bool) -> None:
    ep = root / "ep_000"
    rgb = ep / "rgb"
    rgb.mkdir(parents=True, exist_ok=True)
    csv_path = ep / "joints.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["timestamp"] + [f"{n}.pos" for n in JOINT_NAMES],
        )
        writer.writeheader()
        for i in range(n):
            row = {"timestamp": i / 30.0}
            for j, name in enumerate(JOINT_NAMES):
                row[f"{name}.pos"] = float(j + 0.1 * i)
            writer.writerow(row)
            if with_rgb:
                from PIL import Image

                Image.new("RGB", (64, 64), (i, 40, 80)).save(rgb / f"{i:06d}.jpg")


def test_csv_roundtrip(tmp_path: Path) -> None:
    _write_episode(tmp_path, n=40, with_rgb=False)
    joints = _load_joints_csv(tmp_path / "ep_000" / "joints.csv")
    assert joints.shape == (40, 5)
    samples, vision = build_samples(tmp_path, chunk_size=8, record_fps=30, control_hz=10)
    assert vision is False
    assert len(samples) > 0
    assert samples[0].action.shape == (8, 5)


def test_vision_samples(tmp_path: Path) -> None:
    _write_episode(tmp_path, n=40, with_rgb=True)
    samples, vision = build_samples(tmp_path, chunk_size=8, record_fps=30, control_hz=10)
    assert vision is True
    assert samples[0].frame is not None


def test_policy_shapes() -> None:
    model = TinyLampPolicy(chunk_size=8, image_size=96, vision=True)
    image = torch.zeros(2, 3, 96, 96)
    joints = torch.zeros(2, 5)
    out = model(image, joints)
    assert out.shape == (2, 8, 5)
    assert model.count_parameters() < 1_000_000


def test_onnx_export(tmp_path: Path) -> None:
    model = TinyLampPolicy(chunk_size=8, image_size=96, vision=True).eval()
    path = tmp_path / "tiny.onnx"
    export_onnx(model, path, image_size=96, n_joints=5)
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    out = sess.run(
        ["action_chunk"],
        {
            "image": np.zeros((1, 3, 96, 96), np.float32),
            "joints": np.zeros((1, 5), np.float32),
        },
    )[0]
    assert out.shape == (1, 8, 5)


def test_synthetic_train(tmp_path: Path) -> None:
    export = tmp_path / "artifacts"
    rc = main(
        [
            "--synthetic",
            "--epochs",
            "1",
            "--batch-size",
            "8",
            "--export",
            str(export),
            "--device",
            "cpu",
            "--num-workers",
            "0",
            "--patience",
            "0",
        ]
    )
    assert rc == 0
    assert (export / "tiny_lamp.onnx").is_file()
    assert (export / "meta.json").is_file()
    meta = json.loads((export / "meta.json").read_text())
    assert meta["n_joints"] == 5
    assert meta["chunk_size"] == 8


def test_record_dummy_then_load(tmp_path: Path) -> None:
    from record_demo import main as record_main

    data_root = tmp_path / "data"
    rc = record_main(
        [
            "--task",
            "look_at_person",
            "--out",
            str(data_root),
            "--dummy",
            "--no-prompt",
            "--episodes",
            "2",
            "--seconds",
            "1.5",
            "--fps",
            "20",
        ]
    )
    assert rc == 0
    ep0 = data_root / "look_at_person" / "ep_000"
    assert (ep0 / "joints.csv").is_file()
    frames = list((ep0 / "rgb").glob("*.jpg"))
    assert len(frames) >= 20
    joints = _load_joints_csv(ep0 / "joints.csv")
    assert joints.shape[0] == len(frames)
    samples, vision = build_samples(
        data_root / "look_at_person",
        chunk_size=8,
        record_fps=20,
        control_hz=10,
    )
    assert vision is True
    assert len(samples) > 0
    assert samples[0].frame is not None


def test_guess_port_does_not_raise() -> None:
    from record_demo import guess_port, warn_stop_voice_agent

    warn_stop_voice_agent(None)
    guess_port()


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        test_csv_roundtrip(tmp / "csv")
        test_vision_samples(tmp / "rgb")
        test_policy_shapes()
        test_onnx_export(tmp / "onnx")
        test_synthetic_train(tmp / "train")
        test_record_dummy_then_load(tmp / "record")
        test_guess_port_does_not_raise()
    print("ok")
