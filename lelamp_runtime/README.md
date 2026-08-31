# 灯上跑的 `local_main.py`

把这个文件覆盖到树莓派：

```
/home/spocklamp/lelamp_runtime/local_main.py
```

FileZilla：本地选仓库里的 `lelamp_runtime/local_main.py`，远端选 `~/lelamp_runtime/local_main.py`。

不要把 `record_demo.py` 放进这个目录。音乐、Vosk 模型仍用灯上 runtime 里已有的 `music/` 和 `models/`。

开机自启（和鸭子 `duck-walk.service` 一样：上电等串口，醒来，听中文指令）：

```bash
cd ~/lelamp_runtime
sudo LELAMP_IL_DIR=/home/spocklamp/hermes-agent/lelamp_il \
  .venv/bin/python local_main.py --install-service
```

之后不要再手动开一份 `--listen`。看日志：`journalctl -u lelamp-local -f`

醒来是 stage 4：昼夜心情色 + 立刻播 `wake_up`。
