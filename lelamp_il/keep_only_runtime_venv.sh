#!/bin/bash
# Run ON the Raspberry Pi. Keep only the official (lelamp-runtime) venv.
# That env lives at ~/lelamp_runtime/.venv (uv's project venv).
# Do NOT create ~/lelamp/.venv or lelamp_il/.venv. Do NOT pip install torch.
set -euo pipefail

KEEP_ROOT="${LELAMP_RUNTIME:-$HOME/lelamp_runtime}"
KEEP_VENV=""
for candidate in "$KEEP_ROOT/.venv" "$KEEP_ROOT/venv"; do
  if [ -f "$candidate/pyvenv.cfg" ]; then
    KEEP_VENV="$candidate"
    break
  fi
done

if [ -z "$KEEP_VENV" ]; then
  echo "找不到官方环境 $KEEP_ROOT/.venv"
  echo "先确认灯还能转头的那个 (lelamp-runtime) 还在，再跑本脚本。"
  exit 1
fi

if [ -n "${VIRTUAL_ENV:-}" ]; then
  case "$VIRTUAL_ENV" in
    "$KEEP_VENV"|"$KEEP_VENV/") ;;
    *)
      echo "当前激活的是 $VIRTUAL_ENV"
      echo "请先: deactivate"
      echo "然后再跑本脚本。只保留 $KEEP_VENV"
      exit 1
      ;;
  esac
fi

echo "保留: $KEEP_VENV  ($(du -sh "$KEEP_VENV" | awk '{print $1}'))"

KNOWN=(
  "$HOME/hermes-agent/lelamp_il/.venv"
  "$HOME/hermes-agent/.venv"
  "$HOME/hermes-agent/venv"
  "$HOME/lelamp/.venv"
  "$HOME/lelamp/venv"
  "$HOME/lelamp_il/.venv"
)

TO_DELETE=()
for path in "${KNOWN[@]}"; do
  if [ -e "$path" ]; then
    TO_DELETE+=("$path")
  fi
done

# Any other home venv that is not under ~/lelamp_runtime
while IFS= read -r cfg; do
  [ -n "$cfg" ] || continue
  venv_dir="$(dirname "$cfg")"
  case "$venv_dir" in
    "$KEEP_ROOT"|"$KEEP_ROOT"/*) continue ;;
  esac
  already=0
  for item in "${TO_DELETE[@]+"${TO_DELETE[@]}"}"; do
    if [ "$item" = "$venv_dir" ]; then
      already=1
      break
    fi
  done
  if [ "$already" -eq 0 ]; then
    TO_DELETE+=("$venv_dir")
  fi
done < <(find "$HOME" -name pyvenv.cfg -not -path "$KEEP_ROOT/*" 2>/dev/null)

if [ "${#TO_DELETE[@]}" -eq 0 ]; then
  echo "没有多余虚拟环境。"
  echo "录制只用："
  echo "  bash ~/hermes-agent/lelamp_il/record_on_lamp.sh"
  exit 0
fi

echo "将删除："
for path in "${TO_DELETE[@]}"; do
  size="$(du -sh "$path" 2>/dev/null | awk '{print $1}')"
  echo "  $path  ${size:-?}"
done

for path in "${TO_DELETE[@]}"; do
  rm -rf "$path"
  echo "deleted $path"
done

echo
echo "还在的环境："
echo "  $KEEP_VENV"
echo "录制："
echo "  bash ~/hermes-agent/lelamp_il/record_on_lamp.sh"
