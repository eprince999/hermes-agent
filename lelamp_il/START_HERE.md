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

## 第 2 件事：灯上只留一个环境 `(lelamp-runtime)`

灯上**禁止** `pip install -r requirements-train.txt`，也**禁止**再 `python3 -m venv`。  
只保留官方 `~/lelamp_runtime/.venv`（提示符里的 `(lelamp-runtime)`）。训练在笔记本做。

先删掉误建的环境：

```bash
cd ~/hermes-agent
git pull
bash lelamp_il/keep_only_runtime_venv.sh
```

录制永远走仓库里这份脚本，不要把 `record_demo.py` 复制进 `~/lelamp_runtime/`：

```bash
bash ~/hermes-agent/lelamp_il/record_on_lamp.sh
```

灯头 Camera Module 3 现在**优先 `rpicam-vid`**（和 `rpicam-hello --list-cameras` 同一套系统相机）。`picamera2` 只是后备。不要 `pip install picamera2`。若以后仍要给 venv 看见系统 picamera2：

```bash
cd ~/hermes-agent/lelamp_il
bash enable_pi_camera.sh
```

启动时**第一行**必须是 `record_demo 2026-08-28-stream`。没有这一行就还是旧文件：

```bash
# 先 Ctrl-C 掉卡住的旧进程
sudo pkill -x rpicam-hello; sudo pkill -x rpicam-vid || true

cd ~/hermes-agent
git fetch origin cursor/lelamp-zero2w-train-36b0
git checkout cursor/lelamp-zero2w-train-36b0
git pull origin cursor/lelamp-zero2w-train-36b0
git reset --hard origin/cursor/lelamp-zero2w-train-36b0

grep RECORD_DEMO_REVISION lelamp_il/record_demo.py
# 必须看到 2026-08-28-stream

# 不要复制脚本，不要 cd 到 ~/lelamp_runtime 后跑 python record_demo.py
bash ~/hermes-agent/lelamp_il/record_on_lamp.sh
```

步骤 2 必须立刻打印 `rpicam-vid: 尝试 ...`。录的时候关节数字会变，6 秒内应打到 `180/180`，不能再在中途 `TimeoutError`。

---

## 第 3 件事：用手教它「看我」（先 2 段试通，再 50 段）

```bash
# 树莓派上，语音已停。用包装脚本，它会拒绝旧文件。
bash ~/hermes-agent/lelamp_il/record_on_lamp.sh
```

看到 `Pi Camera via rpicam-vid` 之后：你站到一个位置 → Enter → 倒计时结束 → **用手把灯头转到看着你的脸**，保持 6 秒。

2 段没报错后，再录到大约 50 段，换左/中/右、近/远：

```bash
bash ~/hermes-agent/lelamp_il/record_on_lamp.sh --episodes 50
```

数据默认写在 `~/lelamp_runtime/data/look_at_person/`（包装脚本会 cd 到 runtime）。

---

## 第 4 件事：笔记本训练

把 `data/look_at_person/` 拷回笔记本（灯上默认在 runtime 下）：

```bash
scp -r spocklamp@raspberrypi:~/lelamp_runtime/data/look_at_person ./data/
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
