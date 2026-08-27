# 你的灯已经能听中文、放音乐、做动作 —— 从这里开始

不要重装、不要重新校准、不要把语音 agent 卸掉。  
模仿学习只补一件官方动画做不到的事：**看见人在哪，再把灯头转过去。**

放音乐、中文指令、点头/摇头/开心扭，全部继续走你现在的 `play_recording` / 音量 / LiveKit。

```
中文语音（你已有） ──「放首歌」──► 音乐，不动策略
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

## 第 2 件事：在 Cursor 桌面打开训练脚本

笔记本 Cursor 打开 `lelamp_il`（或整个 `~/lelamp`）：

```bash
cd ~/lelamp/lelamp_il
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-train.txt
```

串口若在树莓派上、训练在笔记本上：USB 把灯的舵机板接到**笔记本**，或 SSH 到树莓派上跑 `record_demo.py`（摄像头必须是灯头那颗）。

推荐：在**灯自己的树莓派**上录（摄像头和舵机都在灯上），拷数据回笔记本训练。

---

## 第 3 件事：用手教它「看我」（先 2 段试通，再 50 段）

```bash
# 树莓派上，语音已停
cd ~/lelamp/lelamp_il
source .venv/bin/activate   # 树莓派若只录、不训，至少 pip install pillow
python record_demo.py --task look_at_person --port /dev/ttyACM0 --id lelamp \
    --episodes 2 --seconds 6
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

然后把 `agent_hook.py` 里的 `watch_person` 加进你现有的 LiveKit Agent（和 `play_recording` 并列）。系统提示加一句：

- 用户说「看我 / 看着我 / 看这边」→ 调用 `watch_person`
- 「点头 / 摇头 / 开心」→ 继续 `play_recording`
- 「放音乐 / 大声一点」→ 继续你现在的音乐和音量工具

最后再开语音：

```bash
sudo systemctl start lelamp.service
```

---

## 不要做的事

- 不要重新 `setup_motors` / `calibrate`（灯已经在动）
- 不要改英语/中文 ASR，除非你只是允许模型用中文回复
- 不要让策略去学放音乐
- 不要语音 agent 和 `record_demo.py` 同时开
