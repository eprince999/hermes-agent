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

## 第 4 件事：Mac 上训练（用 FileZilla 拷数据）

**不要在灯上跑 `train.py`，也不要在灯上 `pip install` 这份 requirements。**

### 4.1 FileZilla 从灯拷示教

1. 协议选 **SFTP**（不是 FTP），端口 **22**
2. 主机：灯的地址（常见是 `raspberrypi.local` 或你平时 SSH 用的 IP）
3. 用户名：`spocklamp`
4. 连上后进入远程目录：

```
/home/spocklamp/lelamp_runtime/data/look_at_person
```

5. 把整个 `look_at_person` 文件夹拖到 Mac，例如放到：

```
~/lelamp_data/look_at_person
```

同时把训练脚本也拷下来（若 Mac 上还没有这份仓库）：

```
/home/spocklamp/hermes-agent/lelamp_il
```

拖到 Mac 后应能看到 `train.py`、`requirements-train.txt`。

拷完后在 Mac 终端检查，`rgb` 必须一起过来（只拷了 `joints.csv` 会训成「不看图」的策略）：

```bash
ls ~/lelamp_data/look_at_person | wc -l
# 大约几十个 ep_xxx

ls ~/lelamp_data/look_at_person/ep_000/rgb | wc -l
# 每一段大约 180 张 jpg
```

### 4.2 在 Mac 上建训练环境并开训

```bash
cd ~/lelamp_il          # 改成你放 train.py 的目录
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements-train.txt

python train.py --data ~/lelamp_data/look_at_person --epochs 40 --export ./artifacts
```

`device=auto` 时 Apple Silicon 会走 MPS。看到 `vision=True` 才对。`val_l1` 降到大约几度就可以用。

产物在 `./artifacts/`：

| 文件 | 干什么 |
|------|--------|
| `tiny_lamp_int8.onnx` | **拷回灯** |
| `meta.json` | **拷回灯**（关节归一化） |
| `tiny_lamp.onnx` | 备用 FP32 |
| `best.pt` | 留在 Mac，以后继续训 |

### 4.3 FileZilla 把模型传回灯

在灯上建目录（SSH 或 FileZilla 新建均可）：

```
/home/spocklamp/hermes-agent/lelamp_il/artifacts/
```

上传这两个文件（必须一对）：

```
tiny_lamp_int8.onnx
meta.json
```

灯上的 `local_main.py` 会在 `~/hermes-agent/lelamp_il/artifacts/` 找到它们。说「看我」之前语音可以再开；先单独试策略时仍建议停着语音，避免抢串口。

---

## 第 5 件事：接回中文指令（不替换音乐和动画）

「看我 / 看着我 / 看过来」会**一直跟着**画面里的人/手，直到你说「停 / 好了 / 别看了」，或再说点头、放音乐、关灯。不是播 6 秒动画。「点头 / 摇头 / 开心」继续 canned 动画；「放音乐 / 大声一点」继续音乐。不要另接一套 LiveKit agent。

单独试策略仍可用固定步数（确认灯会转头）：

```bash
# 语音仍要停着
python infer_pi.py --model artifacts/tiny_lamp_int8.onnx --meta artifacts/meta.json \
    --port /dev/ttyACM0 --steps 60
```

然后把训好的模型拷回灯。灯上跑的是 `~/lelamp_runtime/local_main.py`。覆盖这一份（推荐脚本）：

```bash
bash ~/hermes-agent/plugins/lelamp/install_on_lamp.sh
```

FileZilla：本地 `hermes-agent/lelamp_runtime/local_main.py` → 远端 `/home/spocklamp/lelamp_runtime/local_main.py`。
不要用 `raw.githubusercontent.com`（会 429）。灯上覆盖用 GitHub API 或 git：

```bash
curl -fsSL -H "Accept: application/vnd.github.raw" \
  "https://api.github.com/repos/eprince999/hermes-agent/contents/lelamp_runtime/local_main.py?ref=cursor/lelamp-zero2w-train-36b0" \
  -o ~/lelamp_runtime/local_main.py
grep -E "install-service|BOOT_REVISION" ~/lelamp_runtime/local_main.py | head
```

覆盖后重启已有开机服务，或第一次按 OpenDuck 同款装上：

```bash
sudo systemctl restart lelamp-local
journalctl -u lelamp-local -f
```

上电流程对齐鸭子的 `duck-walk.service` + `~/start_duck.sh`：等 `/dev/ttyACM0` → 舵机就绪等 1 秒 → 昼夜心情色 + 立刻播 `wake_up` → 听「你好 / 点头 / 看我 / 关灯 / 音乐」。不要同时再手动开一份 `--listen`。启动日志应有 `look-at 2026-08-28-follow` 和 `boot 2026-08-31-openduck-cal`。

日志若有 `has no calibration registered`：串口和五个舵机是好的，`play_recording` 只是找不到已有校准 json。官方 `sudo uv run -m lelamp.calibrate` 写到 `/root/.cache/huggingface/lerobot/`，开机服务却把 `HOME` 设成 `/home/spocklamp`。新脚本会去 `/root` 和用户 home 找已有文件，或从舵机 EEPROM 读回，**不要重新 calibrate**。覆盖 `local_main.py` 后：

```bash
sudo systemctl restart lelamp-local
journalctl -u lelamp-local -f
```

应看到 `calibration file /root/.cache/.../lelamp.json` 或 `calibration from motor EEPROM`，然后 `wake_up` 不再报这个错。

舵机不动时，若没有校准那行错误，几乎都是串口被抢（两份 `local_main` 或官方 `lelamp.service`）：

```bash
sudo fuser -v /dev/ttyACM0
journalctl -u lelamp-local -b --no-pager | tail -n 80
```

只保留一个进程：`sudo systemctl stop lelamp.service`，不要再手动 `--listen`。日志若有 `PID=` 或 `[no motors]`，就是这个原因。

第一次装开机服务（灯上只做一次，或 unit 坏了再跑）：

```bash
cd ~/lelamp_runtime
sudo LELAMP_IL_DIR=/home/spocklamp/hermes-agent/lelamp_il \
  .venv/bin/python local_main.py --install-service
```

或：`bash ~/hermes-agent/plugins/lelamp/install_on_lamp.sh --boot`

检测五个舵机（只 ping / 读位置，**不** `setup_motors` / calibrate）。开机服务占着串口时先停掉：

```bash
sudo systemctl stop lelamp-local
cd ~/lelamp_runtime
sudo LELAMP_IL_DIR=/home/spocklamp/hermes-agent/lelamp_il \
  .venv/bin/python ~/hermes-agent/lelamp_il/feetech_bus.py --probe
sudo systemctl start lelamp-local
```

---

## 不要做的事

- 不要重新 `setup_motors` / `calibrate`（灯已经在动）
- 不要改英语/中文 ASR，除非你只是允许模型用中文回复
- 不要让策略去学放音乐
- 不要语音 agent 和 `record_demo.py` 同时开
