# LeLamp Tiny Policy — 笔记本训练，树莓派 Zero 2W 部署

可以。流程是：**笔记本（或任何带 PyTorch 的机器）训练 → 导出 ONNX/INT8 → `scp` 到 Zero 2W 上跑闭环**。

不要把 LeRobot 的 ACT（ResNet18 + Transformer）塞进 Zero 2W。那块板子是 512 MB RAM、四核 Cortex-A53 1 GHz、没有 NPU。ACT 连树莓派 4 都吃力。这份脚本训练的是约 **40 万参数** 的小 CNN+MLP：96×96 图像 + 5 轴关节 → 一段未来关节目标。INT8 后大约 **0.5 MB**，Zero 2W 上闭环大约 **8–12 Hz**。

官方 LeLamp 用的是树莓派 4。Zero 2W 能做「看人 / 点头 / 躲开」这类短技能，做不了 ACT 级操作，更跑不动跳跃 RL。

## 1. 笔记本上训练

```bash
cd lelamp_il
pip install -r requirements-train.txt

# 确认流水线（不需要灯、不需要数据）
python train.py --synthetic --epochs 2 --export ./artifacts

# 用真实示教
python train.py --data ./data/look_at_person --epochs 40 --export ./artifacts
```

产物：

| 文件 | 给谁 |
|---|---|
| `artifacts/best.pt` | 笔记本，继续训练 / 排查 |
| `artifacts/tiny_lamp.onnx` | 备用 FP32 |
| `artifacts/tiny_lamp_int8.onnx` | **拷到 Pi** |
| `artifacts/meta.json` | **拷到 Pi**（归一化、关节限位、输入尺寸） |

拷贝：

```bash
scp artifacts/tiny_lamp_int8.onnx artifacts/meta.json infer_pi.py requirements-pi.txt \
    pi@zero.local:~/lelamp/
```

## 2. 示教数据格式

`lelamp.record` 只写 CSV，没有图像。要学「看人 / 跟手」，必须同时存相机帧。

```
data/look_at_person/
  ep_000/
    joints.csv
    rgb/000000.jpg
    rgb/000001.jpg
    ...
  ep_001/
    ...
```

`joints.csv` 与 LeLamp runtime 一致：

```
timestamp,base_yaw.pos,base_pitch.pos,elbow_pitch.pos,wrist_roll.pos,wrist_pitch.pos
```

第 `i` 张图对应第 `i` 行关节。默认按 30 fps 录、10 Hz 训练（`--record-fps 30 --control-hz 10`），与 Zero 2W 能稳住的闭环频率对齐。

没有 `rgb/` 时脚本会改训 **仅关节** 策略，只能复现点头这类不看世界的动作。

起步量：一个任务 50 段、每段 5–8 秒，换距离和位置。`val_l1` 掉到大约几度再部署。

## 3. 树莓派 Zero 2W

必须刷 **64-bit Raspberry Pi OS Lite**。32 位没有能用的 `onnxruntime` wheel。不要装桌面，512 MB 会被 GUI 吃光。

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv
python3 -m venv ~/lelamp/.venv
source ~/lelamp/.venv/bin/activate
pip install -r requirements-pi.txt

# 先不接舵机，确认模型能跑
python infer_pi.py --model tiny_lamp_int8.onnx --meta meta.json --dry-run --steps 20

# 接上 Feetech 总线（需要 LeLamp runtime / lerobot）
python infer_pi.py --model tiny_lamp_int8.onnx --meta meta.json --port /dev/ttyUSB0
```

CSI 摄像头会优先走 `picamera2`，否则用 OpenCV。`--execute-chunk 1` 表示每步只用预测轨迹的第一拍再重新推理，闭环更稳。

## 4. 为什么不是 `lerobot-train --policy.type=act`

| | TinyLampPolicy（本目录） | ACT |
|---|---|---|
| 参数量 | ~0.4M | 数千万 |
| Zero 2W | 可以 INT8 闭环 | 不行 |
| 树莓派 4 / 笔记本推理 | 可以 | 可以 |
| 跟手、夹取、长视野 | 弱 | 强 |
| 数据格式 | 本目录的 `ep_*/joints.csv` + `rgb/` | `LeRobotDataset` |

Pi 4 上要上 ACT，走 LeRobot 官方流水线（`lerobot-record` → `lerobot-train` → `lerobot-rollout`），推理放在笔记本/NUC，灯上只跑舵机环。Zero 2W 请用这个小模型。

## 5. `train.py` 常用参数

```
--data PATH           任务目录
--synthetic           生成正弦数据，冒烟
--export DIR          默认 ./artifacts
--epochs 40
--chunk-size 8        一次预测 8 拍（约 0.8 s @ 10 Hz）
--image-size 96
--control-hz 10
--no-vision           强制只用关节
--no-int8             只导出 FP32
--device auto         cuda / mps / cpu
```
