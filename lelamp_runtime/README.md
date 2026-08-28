# 灯上跑的 `local_main.py`

把这个文件覆盖到树莓派：

```
/home/spocklamp/lelamp_runtime/local_main.py
```

FileZilla：本地选仓库里的 `lelamp_runtime/local_main.py`，远端选 `~/lelamp_runtime/local_main.py`。

不要把 `record_demo.py` 放进这个目录。音乐、Vosk 模型仍用灯上 runtime 里已有的 `music/` 和 `models/`。

覆盖后重启：

```bash
cd ~/lelamp_runtime
sudo LELAMP_IL_DIR=/home/spocklamp/hermes-agent/lelamp_il \
  uv run python local_main.py --listen
```

启动应看到 `look-at 2026-08-28-follow`。若仍打印「6.0 秒」，覆盖没成功。
