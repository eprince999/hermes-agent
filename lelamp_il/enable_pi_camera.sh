#!/bin/bash
# Run this ON the Raspberry Pi lamp, inside or outside lelamp-runtime.
# picamera2 is a Debian package bound to libcamera. Do NOT pip install it.
set -euo pipefail

echo "=== 1) 强制 apt 走 IPv4（避免 Debian IPv6 404）==="
echo 'Acquire::ForceIPv4 "true";' | sudo tee /etc/apt/apt.conf.d/99force-ipv4 >/dev/null

echo "=== 2) 安装系统 picamera2 + libcamera ==="
sudo apt-get update
sudo apt-get install -y python3-picamera2 python3-libcamera

echo "=== 3) 系统 Python 能否 import ==="
/usr/bin/python3 -c "from picamera2 import Picamera2; print('system picamera2 ok')"

echo "=== 4) 让 lelamp-runtime 虚拟环境能看见系统包 ==="
CFG=""
for candidate in \
  "${VIRTUAL_ENV:-}/pyvenv.cfg" \
  "$HOME/lelamp_runtime/.venv/pyvenv.cfg" \
  "$HOME/lelamp_runtime/venv/pyvenv.cfg" \
  "$HOME/lelamp/.venv/pyvenv.cfg"
do
  if [ -n "${candidate}" ] && [ -f "${candidate}" ]; then
    CFG="${candidate}"
    break
  fi
done

if [ -z "${CFG}" ]; then
  echo "找不到 pyvenv.cfg。请手动编辑虚拟环境里的这个文件："
  echo "  include-system-site-packages = true"
  exit 1
fi

echo "patching ${CFG}"
if grep -q '^include-system-site-packages' "${CFG}"; then
  sed -i 's/^include-system-site-packages = .*/include-system-site-packages = true/' "${CFG}"
else
  echo "include-system-site-packages = true" >> "${CFG}"
fi
grep include-system-site-packages "${CFG}"

echo
echo "=== 必须重新进入虚拟环境后才会生效 ==="
echo "  deactivate"
echo "  source ~/lelamp_runtime/.venv/bin/activate   # 路径按你机器改"
echo "  python3 -c \"from picamera2 import Picamera2; print('venv picamera2 ok')\""
echo
echo "然后再录："
echo "  cd ~/hermes-agent/lelamp_il"
echo "  python record_demo.py --task look_at_person --port /dev/ttyACM0 --id lelamp --episodes 2 --seconds 6"
