# 你的灯已经能听中文、放音乐、做动作 —— 从这里开始

不要重装、不要重新校准、不要把语音 agent 卸掉。  
模仿学习只补一件官方动画做不到的事：**看见人在哪，再把灯头转过去。**

灯上真正在跑的是 `plugins/lelamp/local_main.py`（拷到 `~/lelamp_runtime/local_main.py`）：中文 Vosk、音乐、点头/摇头。  
**不要另写一套 LiveKit agent。** 「看我」已经接到这份 `local_main.py` 上。录数据用 `lelamp_il/record_demo.py`，笔记本训练用 `lelamp_il/train.py`，训好的 ONNX 再给同一份 `local_main.py` 用。

```
中文语音（local_main.py） ──「放首歌」──► 音乐，不动策略
                         ──「点头」  ──► play_recording("nod")
                         ──「看我」  ──► 本次要训的视觉策略
```

舵机串口和摄像头同时只能被一个程序占用。所以：**录制和训练时先停语音；用的时候再开回去。**

---

## 今天第 1 件事：停掉语音，把串口让出来

在灯那台机器（树莓派）上：

```bash
sudo systemctl stop lelamp.service
# 如果是手动跑的：到那个终端按 Ctrl-C
```

确认没人占串口：

```bash
sudo fuser /dev/ttyACM0 /dev/ttyUSB0 2>/dev/null || true
```

有输出就说明还有进程占着，把那个 PID 停掉。官方默认口常常是 `/dev/ttyACM0`。

---

## 第 2 件事：在灯上录制（不要装 PyTorch）

灯上**禁止** `pip install -r requirements-train.txt`。那会下载 400MB+ 的 torch，SD 卡会满。

用灯上**已经能做动作的** `lelamp_runtime` 虚拟环境，只补 pillow：

```bash
# 先清掉误装的环境（若刚把盘装爆）
rm -rf ~/hermes-agent/lelamp_il/.venv
rm -rf ~/.cache/pip

# 换成官方 runtime 的环境（路径按你机器上实际位置改）
source ~/lelamp_runtime/.venv/bin/activate   # 或 venv/
python3 -m pip install pillow
```

**Camera Module 3 必须用系统 `picamera2`，不能 `pip install picamera2`。**  
`(lelamp-runtime)` 是隔离虚拟环境：系统没装包，或 venv 看不见系统包，都会报 `No module named 'picamera2'`。`dpkg -l ... | grep ^ii` 没有输出 = 系统包也没装。

在灯上跑一次（会改 apt 为 IPv4，避免之前的 Debian IPv6 404）：

```bash
# 先 git pull 拿到脚本，然后：
cd ~/hermes-agent/lelamp_il
bash enable_pi_camera.sh
```

或手动做同样的事：

```bash
echo 'Acquire::ForceIPv4 "true";' | sudo tee /etc/apt/apt.conf.d/99force-ipv4
sudo apt-get update
sudo apt-get install -y python3-picamera2 python3-libcamera
/usr/bin/python3 -c "from picamera2 import Picamera2; print('system picamera2 ok')"

# 让官方 runtime 的 venv 能 import 系统包
sed -i 's/^include-system-site-packages = .*/include-system-site-packages = true/' \
  ~/lelamp_runtime/.venv/pyvenv.cfg
# 若文件在 venv/ 而不是 .venv/，改路径后再跑一次 sed

deactivate
source ~/lelamp_runtime/.venv/bin/activate
python3 -c "from picamera2 import Picamera2; print('venv picamera2 ok')"
```

确认启动时打印 ``revision 2026-08-28-raw-sts``。如果没有这一行，灯上还是旧文件，需要：

```bash
cd ~/hermes-agent
git fetch origin cursor/lelamp-zero2w-train-36b0
git checkout cursor/lelamp-zero2w-train-36b0
git pull origin cursor/lelamp-zero2w-train-36b0
```

当前这个 Python 如果报 `No module named 'lerobot'`，说明你没用灯已经能转头的那个解释器。优先：

```bash
# 语音已停
cd ~/lelamp_runtime
sudo uv run python ~/hermes-agent/lelamp_il/record_demo.py --task look_at_person \
    --port /dev/ttyACM0 --id lelamp --episodes 2 --seconds 6
```

`uv run` 就是日常点头/放歌用的环境，里面已经有舵机库。新版本的 `record_demo.py` 也会在没有 lerobot 时走 `scservo_sdk`（runtime 自带的 feetech-servo-sdk）。

或在已激活的 runtime venv 里：

```bash
cd ~/hermes-agent/lelamp_il
python record_demo.py --task look_at_person --port /dev/ttyACM0 --id lelamp \
    --episodes 2 --seconds 6
```

---

## 第 3 件事：用手教它「看我」（先 2 段试通，再 50 段）

```bash
# 树莓派上，语音已停，且上一步 venv 已能 import picamera2
cd ~/lelamp_runtime
sudo uv run python ~/hermes-agent/lelamp_il/record_demo.py --task look_at_person \
    --port /dev/ttyACM0 --id lelamp --episodes 2 --seconds 6
```

脚本会关掉力矩。每一段：你站到一个位置 → Enter → 倒计时结束 → **用手把灯头转到看着你的脸**，保持 6 秒。

2 段没报错后，再录到大约 50 段，换左/中/右、近/远。

```bash
python record_demo.py --task look_at_person --port /dev/ttyACM0 --id lelamp \
    --episodes 50 --seconds 6
```

---

## 第 4 件事：笔记本训练

把 `data/look_at_person/` 拷回笔记本（若录在 Pi 上）：

```bash
scp -r pi@lamp:~/lelamp/lelamp_il/data/look_at_person ./data/
python train.py --data ./data/look_at_person --epochs 40 --export ./artifacts
```

再把模型拷回灯：

```bash
scp artifacts/tiny_lamp_int8.onnx artifacts/meta.json pi@lamp:~/lelamp/lelamp_il/artifacts/
```

---

## 第 5 件事：接回中文指令（不替换音乐和动画）

先单独试策略 6 秒，确认灯会转头：

```bash
# 语音仍要停着
python infer_pi.py --model artifacts/tiny_lamp_int8.onnx --meta artifacts/meta.json \
    --port /dev/ttyACM0 --steps 60
```

然后把训好的模型拷回灯，覆盖 runtime 里的 `local_main.py`（仓库里这份已经接上「看我」）：

```bash
scp artifacts/tiny_lamp_int8.onnx artifacts/meta.json pi@lamp:~/hermes-agent/lelamp_il/artifacts/
# 在灯上：
cp ~/hermes-agent/plugins/lelamp/local_main.py ~/lelamp_runtime/local_main.py
```

「看我 / 看着我 / 看过来」走视觉策略；「点头 / 摇头 / 开心」继续 canned 动画；「放音乐 / 大声一点」继续音乐。不要另接一套 LiveKit agent。

最后再开语音：

```bash
cd ~/lelamp_runtime
sudo uv run python local_main.py --listen
```

---

## 不要做的事

- 不要重新 `setup_motors` / `calibrate`（灯已经在动）
- 不要改英语/中文 ASR，除非你只是允许模型用中文回复
- 不要让策略去学放音乐
- 不要语音 agent 和 `record_demo.py` 同时开
