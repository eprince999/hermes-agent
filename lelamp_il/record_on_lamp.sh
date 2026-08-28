#!/usr/bin/env bash
# Canonical lamp recorder. Refuses to run an old record_demo.py.
# Old copies hang silently at "步骤 2/4" on Camera Module 3.
set -euo pipefail

REV="2026-08-28-stream"
SCRIPT="$(cd "$(dirname "$0")" && pwd)/record_demo.py"

if [[ ! -f "$SCRIPT" ]]; then
  echo "找不到 $SCRIPT"
  exit 1
fi

if ! grep -q "$REV" "$SCRIPT"; then
  echo "这是旧 record_demo.py，步骤 2 会无输出地卡住。先更新仓库："
  echo "  cd ~/hermes-agent"
  echo "  git fetch origin cursor/lelamp-zero2w-train-36b0"
  echo "  git checkout cursor/lelamp-zero2w-train-36b0"
  echo "  git pull origin cursor/lelamp-zero2w-train-36b0"
  echo "  git reset --hard origin/cursor/lelamp-zero2w-train-36b0"
  echo "不要把 record_demo.py 复制进 ~/lelamp_runtime/，也不要从那个目录直接 python record_demo.py"
  exit 1
fi

echo "即将运行: $SCRIPT  ($REV)"
echo "第一行必须是: record_demo ${REV}"
echo "步骤 2 必须立刻出现: rpicam-vid: 尝试 ..."
echo

cd "${LELAMP_RUNTIME:-$HOME/lelamp_runtime}"
sudo uv run python "$SCRIPT" \
  --task look_at_person \
  --port /dev/ttyACM0 \
  --id lelamp \
  --episodes 2 \
  --seconds 6 \
  "$@"
