#!/usr/bin/env bash
# Copy the Stage-4 Chinese/music/look-at agent onto the lamp runtime.
# The process that actually runs is ~/lelamp_runtime/local_main.py — not
# the copy inside hermes-agent. Saying 「看我」 and getting「6.0 秒」means
# this copy was skipped.
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
  mkdir -p "$REPO_ROOT/lelamp_runtime"
  cp -f "$SRC" "$REPO_ROOT/lelamp_runtime/local_main.py"
fi

echo "已写入 $DEST"
grep -n "WATCH_REVISION" "$DEST" | head -n 1
echo
echo "下一步（先 Ctrl-C 掉旧的 local_main）："
echo "  cd $DEST_DIR"
echo "  sudo LELAMP_IL_DIR=\$HOME/hermes-agent/lelamp_il uv run python local_main.py --listen"
echo "启动应看到: look-at ${REV}"
