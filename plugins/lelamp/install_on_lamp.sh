#!/usr/bin/env bash
# Copy the Stage-4 Chinese/music/look-at agent onto the lamp runtime.
# The process that actually runs is ~/lelamp_runtime/local_main.py — not
# the copy inside hermes-agent. Saying 「看我」 and getting「6.0 秒」means
# this copy was skipped.
# Cold-boot uses OpenDuck-style systemd (duck-walk.service analog):
# BOOT_REVISION=2026-08-31-openduck-cal. Re-run with --boot after first copy.
set -euo pipefail

REV="2026-08-28-follow"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/local_main.py"
DEST_DIR="${LELAMP_RUNTIME:-$HOME/lelamp_runtime}"
DEST="$DEST_DIR/local_main.py"

if [[ ! -f "$SRC" ]]; then
  echo "找不到 $SRC"
  exit 1
fi
if ! grep -q "$REV" "$SRC"; then
  echo "这是旧 local_main.py，看我仍会 6 秒结束。先更新仓库后再跑本脚本。"
  exit 1
fi
if [[ ! -d "$DEST_DIR" ]]; then
  echo "找不到 runtime 目录 $DEST_DIR"
  echo "灯上应已有官方 lelamp_runtime。不要把 record_demo.py 拷进这里。"
  exit 1
fi

mkdir -p "$DEST_DIR/lamp_snapshots"
cp -f "$SRC" "$DEST"
cp -f "$SRC" "$DEST_DIR/lamp_snapshots/stage4.py"

REPO_ROOT="$(cd "$HERE/../.." && pwd)"
if [[ -f "$REPO_ROOT/plugins/lelamp/local_main.py" ]]; then
    mkdir -p "$REPO_ROOT/lelamp_runtime/lamp_snapshots"
    cp -f "$SRC" "$REPO_ROOT/lelamp_runtime/local_main.py"
    cp -f "$SRC" "$REPO_ROOT/lelamp_runtime/lamp_snapshots/stage4.py"
fi

echo "已写入 $DEST"
grep -n "WATCH_REVISION" "$DEST" | head -n 1
echo
if [[ "${1:-}" == "--boot" || "${1:-}" == "--service" ]]; then
  PYTHON="$DEST_DIR/.venv/bin/python"
  if [[ ! -x "$PYTHON" ]]; then
    PYTHON="$DEST_DIR/venv/bin/python"
  fi
  echo "安装开机自启（OpenDuck duck-walk 同款：等串口 → 醒来 → 听令）..."
  sudo LELAMP_IL_DIR="${LELAMP_IL_DIR:-$HOME/hermes-agent/lelamp_il}" \
    "$PYTHON" "$DEST" --install-service
  exit 0
fi
echo "下一步（先 Ctrl-C 掉旧的 local_main，或重启已有服务）："
echo "  sudo systemctl restart lelamp-local"
echo "或手动："
echo "  cd $DEST_DIR"
echo "  sudo LELAMP_IL_DIR=\$HOME/hermes-agent/lelamp_il uv run python local_main.py --listen"
echo "不要用 raw.githubusercontent.com（会 429）。覆盖文件："
echo "  curl -fsSL -H \"Accept: application/vnd.github.raw\" \\"
echo "    \"https://api.github.com/repos/eprince999/hermes-agent/contents/lelamp_runtime/local_main.py?ref=cursor/lelamp-zero2w-train-36b0\" \\"
echo "    -o \$DEST"
echo "开机自启（上电等串口、wake_up、听令），和鸭子 duck-walk.service 一样："
echo "  bash $HERE/install_on_lamp.sh --boot"
echo "启动应看到: look-at ${REV} 以及 boot 2026-08-31-openduck-cal"
