"""LeLamp local agent — Stage 4 look-at-person, no OpenAI, no LiveKit.

Always copy onto the Pi as ``~/lelamp_runtime/local_main.py``.
Do not rename the runnable file. Snapshot 2 is the keyword-only archive.
Snapshot 3 is the music-folder archive. This file is snapshot 4::

    mkdir -p ~/lelamp_runtime/lamp_snapshots
    cp local_main.py lamp_snapshots/stage4.py

Keep official ``main.py`` untouched. From the runtime repo root:

    sudo uv run python local_main.py
    sudo uv run python local_main.py --sim
    sudo uv run python local_main.py --say 你好 --say 关灯
    sudo uv run python local_main.py --say 音乐
    sudo uv run python local_main.py --say 看我
    sudo uv run python local_main.py --download-vosk
    sudo uv run python local_main.py --listen
    sudo uv run python local_main.py --install-service
    sudo uv run python local_main.py --boot-status
    sudo uv run python local_main.py --snapshot

``--install-service`` writes systemd unit ``lelamp-local`` the same way
OpenDuck's ``duck-walk.service`` runs ``~/start_duck.sh``: wait for
``/dev/ttyACM0``, then exec this file with ``--listen``. Official
``main.py`` / ``lelamp.service`` stay untouched; that unit is disabled
so it does not grab the serial port.

Type Chinese commands, or with ``--listen`` speak them to the ReSpeaker.
Say 音乐 to play a random file from the music/ folder. Say 下一首 to skip.
Say 大点声 / 小点声 for volume, 循环播放 or 单曲循环 while a song plays.
Say 看我 to follow the person/hand in the camera until you say 停
or 别看了 (not a 6-second clip, not scanning/nod).
While a song plays, the RGB ring does a soft music-box wash (warm highs,
cool lows) and the motors stay still. ``q`` or Ctrl+C quits.
Music plays on the lamp ReSpeaker speaker by default (never HDMI).
Optional ``--audio bt`` uses a paired Bluetooth speaker instead.
Install the mp3 CLI with ``sudo apt update && sudo apt install -y mpg123``
(do not install ffmpeg — it pulls a huge GUI stack).

Roadmap:
  1. keyboard + motors + RGB
  2. on-device speech keywords (Vosk, no cloud)
  3. music folder random play (snapshot stage3.py)
  4. look-at-person visual policy (this file)
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import pwd
import queue
import random
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.request import urlretrieve

# Bump when a stage lands. Printed at startup so a snapshot is identifiable.
AGENT_STAGE = 4
AGENT_LABEL = "vosk listen + music folder + look-at"
# If the lamp prints「已看向画面中的人（6.0 秒）」it is still the old runtime copy.
WATCH_REVISION = "2026-08-28-follow"
# Printed at boot. Match OpenDuck duck-walk.service + ~/start_duck.sh.
# -cal: find existing LeRobot json (sudo calibrate writes /root/.cache).
BOOT_REVISION = "2026-08-31-openduck-cal"
# USB CDC ACM can appear after local-fs.target on a Pi Zero 2W.
SERIAL_WAIT_SECONDS = 30.0
# OpenDuck HWI.turn_on() waits 1s after connecting before posing.
SERVO_SETTLE_SECONDS = 1.0


def snapshot_current(name: Optional[str] = None, *, dest_dir: Optional[Path] = None) -> Path:
    """Copy this file into lamp_snapshots/. Default name is stage{N}.py."""
    folder = dest_dir or (Path(__file__).resolve().parent / "lamp_snapshots")
    folder.mkdir(parents=True, exist_ok=True)
    raw = (name or "").strip() or f"stage{AGENT_STAGE}"
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw).strip("-_")
    if not slug:
        slug = f"stage{AGENT_STAGE}"
    if not slug.endswith(".py"):
        slug += ".py"
    dest = folder / slug
    shutil.copy2(Path(__file__).resolve(), dest)
    print(f"saved snapshot {dest}")
    return dest


BOOT_SERVICE_NAME = "lelamp-local"
BOOT_WRAPPER_NAME = "lelamp-local-run.sh"


def _find_uv(home: Optional[Path] = None) -> Optional[Path]:
    home = home or _effective_home()
    candidates: List[Path] = []
    which = shutil.which("uv")
    if which:
        candidates.append(Path(which))
    candidates.extend(
        [
            home / ".local" / "bin" / "uv",
            Path("/home/spocklamp/.local/bin/uv"),
            Path("/usr/local/bin/uv"),
            Path("/usr/bin/uv"),
        ]
    )
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


def _runtime_python(runtime_dir: Path) -> Tuple[str, List[str]]:
    """Interpreter for the systemd wrapper.

    OpenDuck's start_duck.sh sources ~/py313 then execs python. On the lamp
    that is ``~/lelamp_runtime/.venv/bin/python -u``. Do not ``uv run``:
    under systemd it can sit on a lock and never exec local_main.
    """
    for cand in (
        runtime_dir / ".venv" / "bin" / "python",
        runtime_dir / "venv" / "bin" / "python",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand), [str(cand), "-u"]
    uv = _find_uv()
    if uv is not None:
        return str(uv), [
            str(uv),
            "run",
            "--offline",
            "--no-sync",
            "--directory",
            str(runtime_dir),
            "python",
            "-u",
        ]
    return sys.executable, [sys.executable, "-u"]


def wait_for_serial_port(port: str, *, timeout: float = SERIAL_WAIT_SECONDS) -> bool:
    """Block until the Feetech USB serial node exists (OpenDuck start_duck.sh)."""
    path = Path(port)
    deadline = time.monotonic() + max(0.0, float(timeout))
    announced = False
    while True:
        if path.exists():
            if announced:
                print(f"舵机口到了 {port}", flush=True)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"等了 {timeout:.0f}s 还没有 {port}，先继续", flush=True)
            return False
        if not announced:
            print(f"等待舵机口 {port}（最多 {timeout:.0f}s）…", flush=True)
            announced = True
        time.sleep(min(0.5, remaining))


def serial_port_users(port: str) -> List[str]:
    """PIDs holding the servo USB node, if any."""
    try:
        proc = subprocess.run(
            ["fuser", port],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    text = f"{proc.stdout or ''} {proc.stderr or ''}"
    return [tok for tok in text.replace(":", " ").split() if tok.isdigit()]


def boot_wrapper_script(
    *,
    runtime_dir: Path,
    script: Path,
    home: Path,
    user: str,
    il_dir: Path,
    port: str = "/dev/ttyACM0",
) -> str:
    """Lamp analog of OpenDuck ``~/start_duck.sh``. Keep ``$`` out of the unit."""
    _exe, prefix = _runtime_python(runtime_dir)
    cmd = " ".join(shlex.quote(part) for part in prefix + [str(script), "--listen"])
    serial = shlex.quote((port or "/dev/ttyACM0").strip() or "/dev/ttyACM0")
    log = shlex.quote(str(runtime_dir / "lelamp_start.log"))
    path_env = (
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
        f"{home}/.local/bin:{runtime_dir}/.venv/bin"
    )
    return f"""#!/bin/bash
export HOME={shlex.quote(str(home))}
export USER={shlex.quote(user)}
export SUDO_USER={shlex.quote(user)}
export LELAMP_IL_DIR={shlex.quote(str(il_dir))}
export LELAMP_LISTEN=1
export PYTHONUNBUFFERED=1
export UV_NO_SYNC=1
export UV_OFFLINE=1
export PATH={shlex.quote(path_env)}
# Official sudo calibrate stores json under /root/.cache.
# This service sets HOME to the lamp user, so point LeRobot at whichever
# cache already has lelamp_follower/*.json. Do not re-calibrate.
for cand in \\
  /root/.cache/huggingface/lerobot/calibration \\
  {shlex.quote(str(home))}/.cache/huggingface/lerobot/calibration
do
  if ls "$cand"/robots/lelamp_follower/*.json >/dev/null 2>&1; then
    export HF_LEROBOT_HOME="$(dirname "$cand")"
    export HF_LEROBOT_CALIBRATION="$cand"
    echo "calibration $cand" | tee -a "$log"
    break
  fi
done
cd {shlex.quote(str(runtime_dir))} || exit 1
log={log}
echo "lelamp-local-run {BOOT_REVISION}" | tee -a "$log"
if [ -e {serial} ]; then
  echo "serial ready {serial}" | tee -a "$log"
else
  echo "serial missing {serial}, wait up to 30s" | tee -a "$log"
  i=0
  while [ "$i" -lt 30 ]; do
    if [ -e {serial} ]; then
      echo "serial ready {serial}" | tee -a "$log"
      break
    fi
    i=$((i + 1))
    sleep 1
  done
fi
echo "exec {cmd}" | tee -a "$log"
exec {cmd}
echo "exec failed" | tee -a "$log"
exit 127
"""


def boot_service_unit(
    *,
    runtime_dir: Path,
    script: Path,
    home: Path,
    user: str,
    il_dir: Path,
    port: str = "/dev/ttyACM0",
    wrapper: Optional[Path] = None,
) -> str:
    """Lamp analog of OpenDuck ``/etc/systemd/system/duck-walk.service``."""
    del script, user, il_dir, port
    run = wrapper or (runtime_dir / BOOT_WRAPPER_NAME)
    # No `$` and no ExecStartPre with $(): systemd would treat them as variables
    # and never reach ExecStart.
    return f"""[Unit]
Description=LeLamp local agent (wake + listen, {BOOT_REVISION})
After=local-fs.target bluetooth.service multi-user.target
Wants=bluetooth.service
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={runtime_dir}
Environment=HOME={home}
Environment=SDL_VIDEODRIVER=dummy
TimeoutStartSec=90
ExecStart=/bin/bash {run}
Restart=on-failure
RestartSec=8
KillSignal=SIGINT
TimeoutStopSec=15
StandardInput=null
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _systemctl_run(systemctl: str, args: List[str]) -> subprocess.CompletedProcess:
    cmd = [systemctl, *args]
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out.rstrip(), flush=True)
    if proc.returncode != 0:
        print(f"命令失败 exit={proc.returncode}", flush=True)
    return proc


def print_boot_status(
    *,
    runtime_dir: Optional[Path] = None,
    unit_path: Optional[Path] = None,
) -> int:
    """Show whether systemd actually launched local_main (run on the lamp)."""
    home = _effective_home()
    runtime = Path(runtime_dir or (home / "lelamp_runtime")).resolve()
    unit = Path(unit_path or "/etc/systemd/system/lelamp-local.service")
    wrapper = runtime / BOOT_WRAPPER_NAME
    script = runtime / "local_main.py"
    print(f"boot-status {BOOT_REVISION}", flush=True)
    for path in (
        unit,
        wrapper,
        script,
        runtime / "lelamp_start.log",
        runtime / ".venv" / "bin" / "python",
        runtime / "venv" / "bin" / "python",
    ):
        exists = path.is_file()
        exe = bool(exists and os.access(path, os.X_OK))
        print(f"  {path} exists={exists} executable={exe}", flush=True)
    if script.is_file():
        for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
            if "BOOT_REVISION =" in line or "WATCH_REVISION =" in line:
                print(f"  {line.strip()}", flush=True)
    if unit.is_file():
        print("--- unit ---", flush=True)
        print(unit.read_text(encoding="utf-8", errors="replace"), end="", flush=True)
    if wrapper.is_file():
        print("--- wrapper ---", flush=True)
        print(wrapper.read_text(encoding="utf-8", errors="replace"), end="", flush=True)
    systemctl = shutil.which("systemctl")
    if not systemctl:
        print("没有 systemctl", flush=True)
        return 0
    for args in (
        ["is-enabled", BOOT_SERVICE_NAME],
        ["is-active", BOOT_SERVICE_NAME],
        ["status", BOOT_SERVICE_NAME, "--no-pager", "-l"],
    ):
        _systemctl_run(systemctl, args)
    journalctl = shutil.which("journalctl")
    if journalctl:
        print("+ journalctl -u lelamp-local -b -n 80", flush=True)
        proc = subprocess.run(
            [journalctl, "-u", BOOT_SERVICE_NAME, "-b", "--no-pager", "-n", "80"],
            check=False,
            text=True,
            capture_output=True,
        )
        print((proc.stdout or proc.stderr or "").rstrip(), flush=True)
    log = runtime / "lelamp_start.log"
    if log.is_file():
        print("--- lelamp_start.log ---", flush=True)
        text = log.read_text(encoding="utf-8", errors="replace")
        print(text[-4000:], end="" if text.endswith("\n") else "\n", flush=True)
    return 0


def install_boot_service(
    *,
    runtime_dir: Optional[Path] = None,
    unit_path: Optional[Path] = None,
    enable: bool = True,
) -> Path:
    """Write systemd unit so the lamp wakes and listens after power-on.

    Mirrors OpenDuck: duck-walk.service ExecStart=/bin/bash ~/start_duck.sh
    """
    home = _effective_home()
    user = (os.environ.get("SUDO_USER") or "").strip() or (
        os.environ.get("USER") or "spocklamp"
    )
    runtime = Path(runtime_dir or (home / "lelamp_runtime")).resolve()
    script = runtime / "local_main.py"
    src = Path(__file__).resolve()
    if src.is_file() and src.name == "local_main.py":
        runtime.mkdir(parents=True, exist_ok=True)
        if script.resolve() != src:
            shutil.copy2(src, script)
            snap = runtime / "lamp_snapshots"
            snap.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, snap / "stage4.py")
    il = Path(os.environ.get("LELAMP_IL_DIR") or (home / "hermes-agent" / "lelamp_il"))
    port = (os.environ.get("LELAMP_PORT") or "/dev/ttyACM0").strip() or "/dev/ttyACM0"
    wrapper = runtime / BOOT_WRAPPER_NAME
    runtime.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        boot_wrapper_script(
            runtime_dir=runtime,
            script=script if script.is_file() else src,
            home=home,
            user=user,
            il_dir=il,
            port=port,
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    print(f"wrote {wrapper}", flush=True)
    text = boot_service_unit(
        runtime_dir=runtime,
        script=script if script.is_file() else src,
        home=home,
        user=user,
        il_dir=il,
        port=port,
        wrapper=wrapper,
    )
    dest = Path(unit_path or "/etc/systemd/system/lelamp-local.service")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"wrote {dest}", flush=True)
    if "$" in text:
        raise RuntimeError("unit 文件里不能有 $ ，否则 systemd 会当成变量、根本不启动")
    if not enable:
        return dest
    systemctl = shutil.which("systemctl")
    if not systemctl:
        print("没有 systemctl，只写了 unit 文件。")
        return dest

    listed = subprocess.run(
        [systemctl, "list-unit-files", "lelamp.service"],
        check=False,
        capture_output=True,
        text=True,
    )
    if "lelamp.service" in (listed.stdout or ""):
        _systemctl_run(systemctl, ["stop", "lelamp.service"])
        _systemctl_run(systemctl, ["disable", "lelamp.service"])
    _systemctl_run(systemctl, ["daemon-reload"])
    _systemctl_run(systemctl, ["reset-failed", BOOT_SERVICE_NAME])
    enable_proc = _systemctl_run(systemctl, ["enable", BOOT_SERVICE_NAME])
    restart_proc = _systemctl_run(systemctl, ["restart", BOOT_SERVICE_NAME])
    time.sleep(1.0)
    status_proc = _systemctl_run(systemctl, ["status", BOOT_SERVICE_NAME, "--no-pager", "-l"])
    active_proc = _systemctl_run(systemctl, ["is-active", BOOT_SERVICE_NAME])
    active = (active_proc.stdout or "").strip()
    print(
        f"已开机自启 {BOOT_SERVICE_NAME}（{BOOT_REVISION}，OpenDuck duck-walk 同款）。",
        flush=True,
    )
    print("上电：等串口 → 昼夜色 + wake_up → 听令。不要再手动开一份 --listen。", flush=True)
    print(f"日志: journalctl -u {BOOT_SERVICE_NAME} -b --no-pager", flush=True)
    print(f"文件日志: {runtime / 'lelamp_start.log'}", flush=True)
    if enable_proc.returncode != 0 or restart_proc.returncode != 0 or active != "active":
        print("服务现在没有 active。把上面的 status 整段发过来。", flush=True)
        print_boot_status(runtime_dir=runtime, unit_path=dest)
        if status_proc.returncode != 0:
            return dest
    return dest


def _want_listen(args: argparse.Namespace) -> bool:
    if getattr(args, "repl", False):
        return False
    if getattr(args, "listen", False):
        return True
    flag = (os.environ.get("LELAMP_LISTEN") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


RECORDINGS = (
    "curious",
    "excited",
    "happy_wiggle",
    "headshake",
    "idle",
    "nod",
    "sad",
    "scanning",
    "shock",
    "shy",
    "wake_up",
)

ALIASES: Dict[str, str] = {
    "wake_up": "wake_up",
    "wakeup": "wake_up",
    "hello": "wake_up",
    "hi": "wake_up",
    "你好": "wake_up",
    "您好": "wake_up",
    "早上好": "wake_up",
    "晚上好": "wake_up",
    "打招呼": "wake_up",
    "nod": "nod",
    "yes": "nod",
    "ok": "nod",
    "okay": "nod",
    "点头": "nod",
    "同意": "nod",
    "好的": "nod",
    "明白": "nod",
    "嗯": "nod",
    "headshake": "headshake",
    "no": "headshake",
    "摇头": "headshake",
    "不行": "headshake",
    "不要": "headshake",
    "拒绝": "headshake",
    "curious": "curious",
    "think": "curious",
    "好奇": "curious",
    "思考": "curious",
    "疑惑": "curious",
    "scanning": "scanning",
    "scan": "scanning",
    "寻找": "scanning",
    "张望": "scanning",
    "excited": "excited",
    "兴奋": "excited",
    "happy_wiggle": "happy_wiggle",
    "happy": "happy_wiggle",
    "高兴": "happy_wiggle",
    "开心": "happy_wiggle",
    "shock": "shock",
    "wow": "shock",
    "惊讶": "shock",
    "shy": "shy",
    "害羞": "shy",
    "sad": "sad",
    "sorry": "sad",
    "难过": "sad",
    "伤心": "sad",
    "idle": "idle",
    "待机": "idle",
    "休息": "idle",
}

EXPRESSION_RGB: Dict[str, Tuple[int, int, int]] = {
    "wake_up": (255, 220, 170),
    "nod": (255, 214, 150),
    "headshake": (255, 90, 70),
    "curious": (140, 180, 255),
    "scanning": (170, 210, 255),
    "excited": (255, 200, 40),
    "happy_wiggle": (255, 200, 40),
    "shock": (255, 60, 50),
    "shy": (255, 176, 80),
    "sad": (90, 90, 180),
    "idle": (255, 176, 80),
}

MOOD_RGB: Dict[str, Tuple[int, int, int]] = {
    "warm": (255, 176, 80),
    "cool": (170, 210, 255),
    "talk": (255, 214, 150),
    "listen": (80, 150, 255),
    "happy": (255, 200, 40),
    "sad": (90, 90, 180),
    "alert": (255, 60, 50),
    "night": (255, 120, 40),
    "focus": (180, 220, 255),
    "off": (0, 0, 0),
}

LIGHT_ONLY: Dict[str, str] = {
    "开灯": "auto",
    "开": "auto",
    "打开灯": "auto",
    "on": "auto",
    "关灯": "off",
    "关": "off",
    "关掉": "off",
    "熄灭": "off",
    "off": "off",
    "暖光": "warm",
    "暖": "warm",
    "warm": "warm",
    "冷光": "cool",
    "冷": "cool",
    "cool": "cool",
    "自动": "auto",
    "auto": "auto",
    "夜间": "night",
    "晚上": "night",
    "专注": "focus",
}

MUSIC_START = {
    "音乐", "放音乐", "播放音乐", "来点音乐", "听音乐", "放首歌", "来一首",
    "放歌",
    "music", "play music", "playmusic",
}
MUSIC_STOP = {
    "停止音乐", "别放了", "关掉音乐", "暂停音乐",
    "stop music", "stopmusic",
}
MUSIC_NEXT = {
    "下一首", "换一首", "下一曲", "切歌", "换歌", "下一首歌", "换首歌",
    "next", "next song", "skip",
}
MUSIC_VOL_UP = {
    "大点声", "大声点", "大一点声", "声音大点", "音量高", "音量大", "大声一点",
}
MUSIC_VOL_DOWN = {
    "小点声", "小声点", "小一点声", "声音小点", "音量低", "音量小", "小声一点",
}
MUSIC_LOOP_ALL = {
    "循环播放", "列表循环", "全部循环", "循环列表",
}
WATCH_PERSON = {
    "看我", "看着我", "看这边", "看过来", "看一眼",
    "look at me", "watch me", "lookatme",
}
WATCH_STOP = {
    "别看了", "不用看了", "看够了", "停止看我", "不要看了", "别跟着了",
    "stop watching", "stop looking",
}
# Only honored while already following, so 停止音乐 still wins.
WATCH_STOP_SHORT = {
    "停", "停止", "停了", "好了", "别看", "不看了",
}

MUSIC_LOOP_ONE = {
    "单曲循环", "单曲重复", "这一首循环", "重复这一首",
}
_BUILTIN_TRACKS = (
    ("pulse_100.wav", 100, (0, 3, 7, 10)),
    ("bounce_120.wav", 120, (0, 4, 7, 12)),
    ("spark_140.wav", 140, (0, 5, 7, 9)),
)

HELP_TEXT = """本地台灯 Stage 4（无 OpenAI）
动作：你好 / 点头 / 摇头 / 好奇 / 张望 / 开心 / 兴奋 / 惊讶 / 害羞 / 难过 / 待机
看人：看我 / 看着我 / 看过来（一直跟着画面里的人/手；说停、好了、别看了停下）
灯光：开灯 / 关灯 / 暖光 / 冷光 / 自动 / 亮一点 / 暗一点
音乐：音乐 / 下一首 / 停止音乐 / 大点声 / 小点声 / 循环播放 / 单曲循环
说话：启动时加 --listen（先 --download-vosk）
其它：status  rgb 255 176 80  help  q
"""

VOSK_MODEL_NAME = "vosk-model-small-cn-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"


@dataclass(frozen=True)
class Command:
    kind: str
    payload: object
    reply: str


def circadian_mood(hour: Optional[int] = None) -> Tuple[str, int]:
    h = datetime.now().hour if hour is None else int(hour) % 24
    if 6 <= h < 9:
        return "cool", 80
    if 9 <= h < 17:
        return "focus", 90
    if 17 <= h < 21:
        return "warm", 70
    return "night", 35


def resolve_expression(name: str) -> str:
    raw = (name or "").strip()
    key = raw.lower()
    for rec in RECORDINGS:
        if rec.lower() == key:
            return rec
    if raw in ALIASES:
        return ALIASES[raw]
    if key in ALIASES:
        return ALIASES[key]
    raise ValueError(raw)


def _scale_rgb(rgb: Tuple[int, int, int], brightness: int) -> Tuple[int, int, int]:
    b = max(0, min(100, int(brightness)))
    r, g, bl = rgb
    return (
        max(0, min(255, round(r * b / 100))),
        max(0, min(255, round(g * b / 100))),
        max(0, min(255, round(bl * b / 100))),
    )


def lerp_rgb(
    start: Tuple[int, int, int],
    end: Tuple[int, int, int],
    t: float,
) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, float(t)))
    return (
        int(start[0] + (end[0] - start[0]) * t),
        int(start[1] + (end[1] - start[1]) * t),
        int(start[2] + (end[2] - start[2]) * t),
    )


_VIZ_WARM = (255, 186, 92)
_VIZ_WARM_HI = (255, 220, 160)
_VIZ_COOL = (118, 168, 255)
_VIZ_COOL_LO = (70, 92, 196)


def music_viz_color(*, t: float, bpm: int, hue_shift: float) -> Tuple[int, int, int]:
    """Soft music-box wash: downbeat/bass leans cool, in-between highs lean warm."""
    import math

    beat = max(40, min(220, int(bpm)))
    phase = (max(0.0, float(t)) * beat / 60.0) % 1.0
    bass = 0.5 + 0.5 * math.cos(2.0 * math.pi * phase)
    treble = 1.0 - bass
    wander = 0.5 + 0.5 * math.sin(2.0 * math.pi * (max(0.0, float(t)) / 8.0 + float(hue_shift)))
    cool = lerp_rgb(_VIZ_COOL, _VIZ_COOL_LO, wander)
    warm = lerp_rgb(_VIZ_WARM, _VIZ_WARM_HI, 1.0 - wander)
    mix = 0.16 + 0.84 * (0.74 * treble + 0.26 * wander)
    rgb = lerp_rgb(cool, warm, mix)
    pulse = 0.80 + 0.20 * (0.5 + 0.5 * math.cos(2.0 * math.pi * phase))
    return (
        max(0, min(255, int(rgb[0] * pulse))),
        max(0, min(255, int(rgb[1] * pulse))),
        max(0, min(255, int(rgb[2] * pulse))),
    )


def parse_line(line: str) -> Command:
    """Turn one typed line into a hardware command. No I/O."""
    text = (line or "").strip()
    if not text:
        return Command("noop", None, "")

    low = text.lower()
    if low in {"q", "quit", "exit", "退出"}:
        return Command("quit", None, "好，我先歇着。")
    if low in {"help", "h", "?", "帮助"}:
        return Command("help", None, HELP_TEXT.strip())
    if low in {"status", "状态"}:
        return Command("status", None, "")

    if low in {"亮一点", "亮一些", "亮点"}:
        return Command("brightness_delta", 15, "亮一点。")
    if low in {"暗一点", "暗一些", "暗点"}:
        return Command("brightness_delta", -15, "暗一点。")
    if low in {"最亮"}:
        return Command("brightness_set", 100, "最亮。")
    if low in {"最暗"}:
        return Command("brightness_set", 20, "暗下来。")

    music_kind = _music_kind(text)
    if music_kind == "music":
        return Command("music", None, "放音乐。")
    if music_kind == "music_next":
        return Command("music_next", None, "下一首。")
    if music_kind == "music_stop":
        return Command("music_stop", None, "停了。")
    if music_kind == "volume_up":
        return Command("volume_delta", 15, "大点声。")
    if music_kind == "volume_down":
        return Command("volume_delta", -15, "小点声。")
    if music_kind == "loop_all":
        return Command("music_loop", "all", "循环播放。")
    if music_kind == "loop_one":
        return Command("music_loop", "one", "单曲循环。")

    if _watch_stop_kind(text):
        return Command("watch_stop", None, "好，不看了。")
    if _watch_kind(text):
        return Command("watch_person", 0.0, "一直看着你。说停或别看了就停。")

    parts = text.split()
    if parts[0].lower() == "volume" and len(parts) == 2 and parts[1].isdigit():
        vol = max(0, min(100, int(parts[1])))
        return Command("volume", vol, f"音量 {vol}%")
    if parts[0].lower() == "rgb" and len(parts) == 4:
        try:
            rgb = tuple(int(p) for p in parts[1:4])
        except ValueError:
            return Command("unknown", text, "RGB 要写成：rgb 255 176 80")
        if not all(0 <= c <= 255 for c in rgb):
            return Command("unknown", text, "RGB 每个数 0 到 255。")
        return Command("rgb", rgb, f"颜色 {rgb}")

    if text in LIGHT_ONLY or low in LIGHT_ONLY:
        mood = LIGHT_ONLY.get(text) or LIGHT_ONLY[low]
        spoken = {
            "auto": "按现在的时间开灯。",
            "off": "关灯。",
            "warm": "暖光。",
            "cool": "冷光。",
            "night": "夜间光。",
            "focus": "专注光。",
        }.get(mood, f"灯：{mood}")
        return Command("mood", mood, spoken)

    try:
        recording = resolve_expression(text)
    except ValueError:
        return Command(
            "unknown",
            text,
            "我还没学会这句。可以说：你好、点头、看我、别看了、暖光、关灯、音乐、下一首、大点声。输入 help 看全部。",
        )
    spoken = {
        "wake_up": "你好呀，我是台灯。",
        "nod": "好的。",
        "headshake": "这个不行。",
        "curious": "嗯？",
        "scanning": "我看看。",
        "excited": "好激动！",
        "happy_wiggle": "开心！",
        "shock": "哇！",
        "shy": "有点不好意思。",
        "sad": "唉。",
        "idle": "我歇一会儿。",
    }.get(recording, recording)
    return Command("express", recording, spoken)


def command_phrases() -> List[str]:
    extra = (
        "亮一点", "亮一些", "亮点", "暗一点", "暗一些", "暗点", "最亮", "最暗",
        "帮助", "退出",
        "大点声", "小点声", "循环播放", "单曲循环",
    )
    phrases = (
        set(LIGHT_ONLY)
        | set(ALIASES)
        | set(RECORDINGS)
        | set(MUSIC_START)
        | set(MUSIC_STOP)
        | set(MUSIC_NEXT)
        | set(MUSIC_VOL_UP)
        | set(MUSIC_VOL_DOWN)
        | set(MUSIC_LOOP_ALL)
        | set(MUSIC_LOOP_ONE)
        | set(WATCH_PERSON)
        | set(WATCH_STOP)
        | set(extra)
    )
    return sorted(phrases, key=lambda item: (-len(item), item))


def _compact_speech(transcript: str) -> str:
    text = (transcript or "").strip()
    for token in (
        " ",
        "\t",
        "\u00a0",
        "\u3000",
        "\u200b",
        "，",
        "。",
        "！",
        "？",
        ",",
        ".",
        "!",
        "?",
        "、",
    ):
        text = text.replace(token, "")
    return text


def _phrase_in_blobs(phrases: set, blobs: Tuple[str, ...]) -> bool:
    for phrase in sorted(phrases, key=len, reverse=True):
        needle = _compact_speech(phrase).lower()
        if needle and any(needle in blob for blob in blobs):
            return True
    return False


def _music_kind(transcript: str) -> Optional[str]:
    """Return a music command kind if the transcript names one."""
    compact = _compact_speech(transcript)
    low = compact.lower()
    blobs = (compact, low, (transcript or "").strip().lower())
    if _phrase_in_blobs(MUSIC_STOP, blobs):
        return "music_stop"
    if _phrase_in_blobs(MUSIC_NEXT, blobs):
        return "music_next"
    if _phrase_in_blobs(MUSIC_LOOP_ONE, blobs):
        return "loop_one"
    if _phrase_in_blobs(MUSIC_LOOP_ALL, blobs):
        return "loop_all"
    if _phrase_in_blobs(MUSIC_VOL_UP, blobs):
        return "volume_up"
    if _phrase_in_blobs(MUSIC_VOL_DOWN, blobs):
        return "volume_down"
    if _phrase_in_blobs(MUSIC_START, blobs):
        return "music"
    if "音乐" in compact:
        return "music"
    return None


def _watch_kind(transcript: str) -> bool:
    compact = _compact_speech(transcript)
    low = compact.lower()
    blobs = (compact, low, (transcript or "").strip().lower())
    return _phrase_in_blobs(WATCH_PERSON, blobs)


def _watch_stop_kind(transcript: str) -> bool:
    compact = _compact_speech(transcript)
    low = compact.lower()
    blobs = (compact, low, (transcript or "").strip().lower())
    return _phrase_in_blobs(WATCH_STOP, blobs)


def _is_short_watch_stop(transcript: str) -> bool:
    compact = _compact_speech(transcript)
    if not compact:
        return False
    return compact in WATCH_STOP_SHORT or compact.lower() in WATCH_STOP_SHORT


def extract_spoken_command(transcript: str) -> Optional[str]:
    """Pick a Stage-1 command out of an ASR transcript, or None."""
    compact = _compact_speech(transcript)
    if not compact:
        return None
    direct = parse_line(compact)
    if direct.kind != "unknown":
        return compact
    best_phrase = None
    best_pos = 10**9
    best_len = 0
    for phrase in command_phrases():
        pos = compact.find(phrase)
        if pos < 0:
            continue
        if pos < best_pos or (pos == best_pos and len(phrase) > best_len):
            best_phrase = phrase
            best_pos = pos
            best_len = len(phrase)
    return best_phrase


_EXTRA_BIN_DIRS = ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin")


def _ensure_os_path() -> None:
    """Keep apt binaries visible under ``sudo uv run`` (venv PATH is narrow)."""
    current = os.environ.get("PATH") or ""
    prefix = ":".join(_EXTRA_BIN_DIRS)
    if not current.startswith(prefix):
        os.environ["PATH"] = f"{prefix}:{current}" if current else prefix


def _bin(name: str) -> Optional[str]:
    _ensure_os_path()
    found = shutil.which(name)
    if found:
        return found
    for folder in _EXTRA_BIN_DIRS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def music_dir() -> Path:
    override = (os.environ.get("LELAMP_MUSIC_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "music"


def ensure_music_dir(folder: Optional[Path] = None) -> Path:
    root = folder or music_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_beat_wav(
    path: Path,
    *,
    bpm: int,
    notes: Sequence[int],
    seconds: float = 12.0,
    rate: int = 16000,
) -> Path:
    import math
    import struct
    import wave

    n = int(seconds * rate)
    spb = max(1.0, (60.0 / max(40, int(bpm))) * rate)
    frames = bytearray()
    note_count = max(1, len(notes))
    for i in range(n):
        beat = i / spb
        beat_i = int(beat)
        pos = beat - beat_i
        t = i / rate
        kick = 0.0
        if pos < 0.14:
            kick = math.sin(2 * math.pi * 75 * t) * (1.0 - pos / 0.14)
            if beat_i % 2:
                kick *= 0.45
        hat = ((i * 17) % 11 / 11 - 0.5) * (0.18 if pos < 0.05 else 0.0)
        degree = notes[beat_i % note_count]
        freq = 196.0 * (2 ** (degree / 12.0))
        mel = 0.22 * math.sin(2 * math.pi * freq * t) * max(0.0, 1.0 - pos)
        sample = max(-1.0, min(1.0, kick * 0.75 + hat + mel))
        frames.extend(struct.pack("<h", int(sample * 12000)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(bytes(frames))
    return path


def ensure_builtin_music(dest: Optional[Path] = None) -> List[Path]:
    folder = dest or (ensure_music_dir() / ".builtin")
    folder.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for name, bpm, notes in _BUILTIN_TRACKS:
        path = folder / name
        if not path.is_file() or path.stat().st_size < 2000:
            write_beat_wav(path, bpm=bpm, notes=notes)
        paths.append(path)
    return paths


def list_music_files(folder: Optional[Path] = None) -> List[Path]:
    root = folder or music_dir()
    if not root.is_dir():
        return []
    allowed = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
    files: List[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() in allowed:
            files.append(path)
    return sorted(files)


def bpm_from_name(path: Path) -> Optional[int]:
    stem = path.stem
    if "_" in stem:
        tail = stem.rsplit("_", 1)[-1]
        if tail.isdigit():
            value = int(tail)
            if 40 <= value <= 220:
                return value
    return None


def pick_random_track(folder: Optional[Path] = None) -> Tuple[Path, int]:
    root = ensure_music_dir(folder)
    files = list_music_files(root)
    if not files:
        files = ensure_builtin_music(root / ".builtin")
        print(f"music 文件夹是空的，把 wav/mp3 放到 {root}")
    if not files:
        raise RuntimeError(f"no music files in {root}")
    path = random.choice(files)
    return path, bpm_from_name(path) or 120


def parse_alsa_playback(listing: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (pcm name, card index) for the ReSpeaker/seeed card. Never HDMI."""
    for line in listing.splitlines():
        low = line.lower()
        if not low.startswith("card "):
            continue
        if "hdmi" in low or "vc4" in low:
            continue
        if not any(tag in low for tag in ("seeed", "respeaker", "voicecard", "array")):
            continue
        try:
            card = line.split(":", 1)[0].split()[1]
        except (IndexError, ValueError):
            continue
        return f"plughw:{card},0", card
    return None, None


def parse_bluetoothctl_devices(text: str) -> List[Tuple[str, str]]:
    """Return [(mac, name), ...] from `bluetoothctl devices` output."""
    found: List[Tuple[str, str]] = []
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2 or parts[0].lower() != "device":
            continue
        mac = parts[1].strip()
        if mac.count(":") != 5:
            continue
        name = parts[2].strip() if len(parts) > 2 else mac
        found.append((mac.upper(), name))
    return found


def parse_pactl_bluez_sinks(text: str) -> List[Tuple[str, str]]:
    """Return [(sink_name, label), ...] for Bluetooth Pulse/PipeWire sinks."""
    found: List[Tuple[str, str]] = []
    for line in (text or "").splitlines():
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 2:
            continue
        name = cols[1] if cols[0].isdigit() else cols[0]
        low = name.lower()
        if "bluez" not in low and "bluealsa" not in low:
            continue
        found.append((name, name))
    return found


def parse_bluealsa_pcms(text: str) -> List[str]:
    found: List[str] = []
    for line in (text or "").splitlines():
        raw = line.strip()
        low = raw.lower()
        if "bluealsa" in low and ("a2dp" in low or raw.startswith("bluealsa:")):
            found.append(raw.split()[0])
    return found


def _run_text(argv: Sequence[str], *, timeout: float = 8.0) -> str:
    try:
        return subprocess.check_output(
            list(argv),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except Exception:
        return ""


def bluetooth_macs_and_names() -> List[Tuple[str, str]]:
    bt = _bin("bluetoothctl")
    if not bt:
        return []
    _run_text([bt, "power", "on"], timeout=5)
    devices = parse_bluetoothctl_devices(_run_text([bt, "devices"], timeout=8))
    connected = parse_bluetoothctl_devices(_run_text([bt, "devices", "Connected"], timeout=8))
    # Prefer already-connected speakers first.
    connected_macs = {mac for mac, _name in connected}
    ordered = list(connected) + [item for item in devices if item[0] not in connected_macs]
    seen = set()
    unique: List[Tuple[str, str]] = []
    for mac, name in ordered:
        if mac in seen:
            continue
        seen.add(mac)
        unique.append((mac, name))
    return unique


_BT_TRIED = False


def print_bluetooth_pair_help() -> None:
    print("没有已配对的蓝牙设备。音箱开机可发现后，在 Pi 上执行：")
    print("  bluetoothctl power on")
    print("  bluetoothctl scan on")
    print("  bluetoothctl devices")
    print("  bluetoothctl pair AA:BB:CC:DD:EE:FF")
    print("  bluetoothctl trust AA:BB:CC:DD:EE:FF")
    print("  bluetoothctl connect AA:BB:CC:DD:EE:FF")
    print("或: sudo uv run python local_main.py --bt-connect")


def connect_bluetooth_speaker(
    mac: Optional[str] = None,
    *,
    quiet: bool = False,
) -> Optional[Tuple[str, str]]:
    global _BT_TRIED
    _BT_TRIED = True
    bt = _bin("bluetoothctl")
    if not bt:
        if not quiet:
            print("没有 bluetoothctl，装: sudo apt install -y bluez")
        return None
    _run_text([bt, "power", "on"], timeout=5)
    _run_text([bt, "agent", "on"], timeout=5)
    _run_text([bt, "default-agent"], timeout=5)
    targets = bluetooth_macs_and_names()
    if mac:
        mac_u = mac.strip().upper()
        name = next((n for m, n in targets if m == mac_u), mac_u)
        targets = [(mac_u, name)] + [item for item in targets if item[0] != mac_u]
    if not targets:
        if not quiet:
            print_bluetooth_pair_help()
        return None
    for target_mac, name in targets:
        print(f"连接蓝牙 {name} ({target_mac})")
        out = _run_text([bt, "connect", target_mac], timeout=20)
        low = (out or "").lower()
        connected = parse_bluetoothctl_devices(_run_text([bt, "devices", "Connected"], timeout=8))
        if (
            "connected" in low
            or "already" in low
            or "success" in low
            or any(m == target_mac for m, _n in connected)
        ):
            # Pulse/PipeWire needs a moment to publish the A2DP sink.
            time.sleep(1.2)
            print(f"蓝牙已连接 {name}")
            return target_mac, name
        print(f"没连上 {name}: {(out or '').strip()[:160]}")
    return None


def find_bluetooth_playback() -> Tuple[Optional[str], Optional[str], str]:
    """Return (device, card_or_sink_hint, backend) for a Bluetooth speaker.

    backend is pulse or alsa. HDMI is never returned.
    """
    pactl = _bin("pactl")
    if pactl:
        sinks = parse_pactl_bluez_sinks(_run_text([pactl, "list", "short", "sinks"], timeout=5))
        if sinks:
            return sinks[0][0], None, "pulse"
    aplay = _bin("aplay")
    if aplay:
        pcms = parse_bluealsa_pcms(_run_text([aplay, "-L"], timeout=5))
        if pcms:
            return pcms[0], None, "alsa"
    macs = bluetooth_macs_and_names()
    connected = parse_bluetoothctl_devices(
        _run_text([_bin("bluetoothctl") or "bluetoothctl", "devices", "Connected"], timeout=8)
    ) if _bin("bluetoothctl") else []
    if connected:
        mac = connected[0][0]
        return f"bluealsa:DEV={mac},PROFILE=a2dp", None, "alsa"
    if macs:
        mac = macs[0][0]
        return f"bluealsa:DEV={mac},PROFILE=a2dp", None, "alsa"
    return None, None, "alsa"


def find_alsa_playback_device() -> Optional[str]:
    device, _card = find_alsa_playback()
    return device


def find_alsa_playback() -> Tuple[Optional[str], Optional[str]]:
    """Prefer the ReSpeaker/seeed speaker over HDMI."""
    aplay = _bin("aplay")
    if not aplay:
        return None, None
    try:
        listing = subprocess.check_output(
            [aplay, "-l"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=3,
        )
    except Exception:
        return None, None
    return parse_alsa_playback(listing)


def set_alsa_playback_volume(card: Optional[str], percent: int) -> None:
    amixer = _bin("amixer")
    if not amixer or card is None or card == "":
        return
    pct = f"{max(0, min(100, int(percent)))}%"
    for control in (
        "Speaker",
        "Playback",
        "PCM",
        "Digital",
        "Headphone",
        "DAC",
        "Master",
        "Lineout",
    ):
        subprocess.run(
            [amixer, "-c", str(card), "sset", control, pct, "unmute"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )


def unmute_alsa_card(card: Optional[str]) -> None:
    set_alsa_playback_volume(card, 90)


_LAST_PLAYBACK: Dict[str, Optional[str]] = {"device": None, "card": None, "backend": "alsa"}


def remember_playback(device: Optional[str], card: Optional[str], backend: str) -> None:
    _LAST_PLAYBACK["device"] = device
    _LAST_PLAYBACK["card"] = card
    _LAST_PLAYBACK["backend"] = backend


def apply_playback_volume(percent: int) -> None:
    """Live volume via ALSA mixer or Pulse sink. Never touches HDMI."""
    percent = max(0, min(100, int(percent)))
    backend = (_LAST_PLAYBACK.get("backend") or "alsa")
    device = _LAST_PLAYBACK.get("device")
    card = _LAST_PLAYBACK.get("card")
    if backend == "pulse" and device:
        pactl = _bin("pactl")
        if pactl:
            _run_text([pactl, "set-sink-volume", device, f"{percent}%"], timeout=3)
            _run_text([pactl, "set-sink-mute", device, "0"], timeout=3)
            return
    if not card:
        _device, card = find_alsa_playback()
        if _device:
            remember_playback(_device, card, "alsa")
    set_alsa_playback_volume(card, percent)


def _pulse_server() -> Optional[str]:
    """Find Pulse/PipeWire even when this process is root via sudo."""
    candidates: List[str] = []
    sudo_uid = (os.environ.get("SUDO_UID") or "").strip()
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if sudo_uid:
        candidates.append(f"/run/user/{sudo_uid}/pulse/native")
    if runtime:
        candidates.append(f"{runtime}/pulse/native")
    candidates.append(f"/run/user/{os.getuid()}/pulse/native")
    if sudo_uid != "1000" and str(os.getuid()) != "1000":
        candidates.append("/run/user/1000/pulse/native")
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if Path(path).exists():
            return f"unix:{path}"
    return None


def _player_env(
    device: Optional[str],
    card: Optional[str] = None,
    *,
    backend: str = "alsa",
) -> Dict[str, str]:
    _ensure_os_path()
    env = os.environ.copy()
    if backend == "pulse" and device:
        env["SDL_AUDIODRIVER"] = "pulse"
        env["PULSE_SINK"] = device
        server = _pulse_server()
        if server:
            env["PULSE_SERVER"] = server
        return env
    env["SDL_AUDIODRIVER"] = "alsa"
    if device:
        env["AUDIODEV"] = device
        env["SDL_AUDIO_DEVICE_NAME"] = device
    if card:
        env["ALSA_CARD"] = str(card)
        env["ALSA_PCM_CARD"] = str(card)
        env["ALSA_PCM_DEVICE"] = "0"
    return env


def music_player_commands(
    path: Path,
    *,
    device: Optional[str] = None,
    backend: str = "alsa",
) -> List[List[str]]:
    """Build argv lists for common Pi players. Never default to HDMI."""
    path_s = str(path)
    suffix = path.suffix.lower()
    commands: List[List[str]] = []

    def add(binary: Optional[str], args: Sequence[str]) -> None:
        if binary:
            commands.append([binary, *args])

    if backend == "pulse" and device:
        mpg = _bin("mpg123") or _bin("mpg321")
        if suffix in {".mp3", ".mp2"} and mpg:
            add(mpg, ["-q", "-o", "pulse", path_s])
        if suffix == ".wav":
            add(_bin("paplay"), ["--sink", device, path_s])
        add(_bin("mpv"), [f"--audio-device=pulse/{device}", "--no-video", "--really-quiet", path_s])
        add(_bin("ffplay"), ["-nodisp", "-autoexit", "-loglevel", "quiet", path_s])
        ffmpeg = _bin("ffmpeg")
        if ffmpeg:
            add(
                ffmpeg,
                ["-nostdin", "-hide_banner", "-loglevel", "error", "-i", path_s, "-f", "pulse", device],
            )
        gst_launch = _bin("gst-launch-1.0")
        if gst_launch:
            add(
                gst_launch,
                [
                    "-q",
                    "filesrc",
                    f"location={path_s}",
                    "!",
                    "decodebin",
                    "!",
                    "audioconvert",
                    "!",
                    "audioresample",
                    "!",
                    "pulsesink",
                    f"device={device}",
                ],
            )
        return commands

    aplay = _bin("aplay")
    ffmpeg = _bin("ffmpeg")
    if suffix == ".wav":
        if aplay:
            wav = ["-q"]
            if device:
                wav.extend(["-D", device])
            wav.append(path_s)
            add(aplay, wav)
        add(_bin("paplay"), [path_s])

    mpg = _bin("mpg123") or _bin("mpg321")
    if suffix in {".mp3", ".mp2"} and mpg:
        mpg_args = ["-q", "-o", "alsa"]
        if device:
            mpg_args.extend(["-a", device])
        mpg_args.append(path_s)
        add(mpg, mpg_args)

    ffplay = _bin("ffplay")
    if ffplay:
        add(ffplay, ["-nodisp", "-autoexit", "-loglevel", "quiet", path_s])

    mpv = _bin("mpv")
    if mpv:
        add(mpv, ["--no-video", "--really-quiet", path_s])

    gst = _bin("gst-play-1.0")
    if gst:
        add(gst, ["--no-interactive", path_s])

    cvlc = _bin("cvlc") or _bin("vlc")
    if cvlc:
        add(cvlc, ["--play-and-exit", "--intf", "dummy", "--quiet", path_s])

    gst_launch = _bin("gst-launch-1.0")
    if gst_launch:
        sink = ["alsasink"]
        if device:
            sink.extend([f"device={device}"])
        add(
            gst_launch,
            ["-q", "filesrc", f"location={path_s}", "!", "decodebin", "!", "audioconvert", "!", "audioresample", "!", *sink],
        )

    if ffmpeg:
        alsa_out = device or "default"
        add(
            ffmpeg,
            [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                path_s,
                "-f",
                "alsa",
                alsa_out,
            ],
        )
    return commands


def _spawn_player(argv: Sequence[str], *, env: Dict[str, str]) -> Optional["subprocess.Popen[bytes]"]:
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"player skip {' '.join(list(argv)[:2])}: {exc}")
        return None
    time.sleep(0.25)
    if proc.poll() is not None:
        print(f"player fail {' '.join(list(argv)[:3])} exit={proc.returncode}")
        return None
    return proc


def _spawn_ffmpeg_aplay(path: Path, *, device: Optional[str], env: Dict[str, str]) -> Optional["subprocess.Popen[bytes]"]:
    ffmpeg = _bin("ffmpeg")
    aplay = _bin("aplay")
    if not ffmpeg or not aplay:
        return None
    aplay_cmd = [aplay, "-q"]
    if device:
        aplay_cmd.extend(["-D", device])
    try:
        decode = subprocess.Popen(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "wav",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        play = subprocess.Popen(
            aplay_cmd,
            stdin=decode.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return None
    if decode.stdout is not None:
        decode.stdout.close()
    play._lelamp_buddy = decode  # type: ignore[attr-defined]
    time.sleep(0.25)
    if play.poll() is not None:
        _stop_process(decode)
        return None
    return play


def _spawn_pygame_player(
    path: Path,
    *,
    device: Optional[str],
    card: Optional[str],
    env: Dict[str, str],
    backend: str = "alsa",
) -> Optional["subprocess.Popen[bytes]"]:
    script = r"""
import os, sys, time
path, device, card, backend = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if backend == "pulse":
    os.environ["SDL_AUDIODRIVER"] = "pulse"
    if device:
        os.environ["PULSE_SINK"] = device
else:
    os.environ["SDL_AUDIODRIVER"] = "alsa"
    if card:
        os.environ["ALSA_CARD"] = card
        os.environ["ALSA_PCM_CARD"] = card
        os.environ["ALSA_PCM_DEVICE"] = "0"
    elif device.startswith("plughw:") or device.startswith("hw:"):
        os.environ["ALSA_CARD"] = device.split(":", 1)[1].split(",", 1)[0]
    if device:
        os.environ["AUDIODEV"] = device
import pygame
# Do not pass mixer devicename=plughw:... — SDL_mixer rejects it and
# the whole player subprocess exits, leaving no sound.
pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
pygame.mixer.music.set_volume(1.0)
pygame.mixer.music.load(path)
pygame.mixer.music.play()
sys.stderr.write("pygame backend=%s device=%s mixer=%s\n" % (backend, device or "default", pygame.mixer.get_init()))
while pygame.mixer.music.get_busy():
    time.sleep(0.15)
"""
    argv = [sys.executable, "-c", script, str(path), device or "", card or "", backend]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return None
    time.sleep(0.35)
    if proc.poll() is not None:
        return None
    return proc


def _stop_process(proc: Optional["subprocess.Popen[bytes]"]) -> None:
    if proc is None:
        return
    buddy = getattr(proc, "_lelamp_buddy", None)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    if buddy is not None:
        _stop_process(buddy)


AUDIO_PREFER = "seeed"


def choose_playback(*, prefer: Optional[str] = None) -> Tuple[Optional[str], Optional[str], str]:
    """Return (device, card, backend). ReSpeaker by default. Never HDMI."""
    mode = (prefer or AUDIO_PREFER or os.environ.get("LELAMP_AUDIO") or "seeed").strip().lower()
    if mode in {"bt", "bluetooth"}:
        device, card, backend = find_bluetooth_playback()
        if not device and not _BT_TRIED:
            connect_bluetooth_speaker(quiet=False)
            device, card, backend = find_bluetooth_playback()
        if device:
            print(f"喇叭 蓝牙 {device}")
            remember_playback(device, card, backend)
            return device, card, backend
        print("没有蓝牙音箱，不使用 HDMI。")
        return None, None, "alsa"
    device, card = find_alsa_playback()
    if device:
        print(f"喇叭 ReSpeaker {device}")
        remember_playback(device, card, "alsa")
        return device, card, "alsa"
    print("没有 ReSpeaker 喇叭。不会使用 HDMI。")
    return None, None, "alsa"


def print_mpg123_install_hint() -> None:
    print("没有播放器。mp3 只需轻量 mpg123，不要装 ffmpeg（会拖 100+ 个 GTK 包）。")
    print("Debian 源过期时先更新再装：")
    print("  sudo apt update")
    print("  sudo apt install -y mpg123")


def start_music_player(path: Path, *, volume: int = 85) -> Optional["subprocess.Popen[bytes]"]:
    device, card, backend = choose_playback()
    env = _player_env(device, card, backend=backend)
    if not device:
        print("没有 ReSpeaker 喇叭。不会使用 HDMI。")
        return None
    apply_playback_volume(volume)
    for argv in music_player_commands(path, device=device, backend=backend):
        proc = _spawn_player(argv, env=env)
        if proc is not None:
            print(f"player {' '.join(argv[:1])}")
            return proc
    pygame_proc = _spawn_pygame_player(
        path, device=device, card=card, env=env, backend=backend
    )
    if pygame_proc is not None:
        print(f"player pygame {device}")
        return pygame_proc
    if backend == "alsa":
        piped = _spawn_ffmpeg_aplay(path, device=device, env=env)
        if piped is not None:
            print("player ffmpeg|aplay")
            return piped
    return None


def vosk_model_dir() -> Path:
    override = os.environ.get("LELAMP_VOSK_MODEL")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    return here / "models" / VOSK_MODEL_NAME


def download_vosk_model(dest: Optional[Path] = None) -> Path:
    target = dest or vosk_model_dir()
    marker = target / "am" / "final.mdl"
    if marker.is_file():
        print(f"vosk model already at {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    zip_path = target.parent / f"{VOSK_MODEL_NAME}.zip"
    print(f"downloading {VOSK_MODEL_URL}")
    urlretrieve(VOSK_MODEL_URL, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("..") or name.startswith("/"):
                raise RuntimeError(f"unsafe zip entry: {name}")
        zf.extractall(target.parent)
    zip_path.unlink(missing_ok=True)
    if not marker.is_file():
        raise RuntimeError(f"vosk model missing after extract: {target}")
    print(f"vosk model ready at {target}")
    return target


def find_input_device(preferred: Optional[int] = None):
    import sounddevice as sd

    if preferred is not None:
        return preferred
    for index, device in enumerate(sd.query_devices()):
        name = str(device.get("name") or "")
        if "seeed" in name.lower() and int(device.get("max_input_channels") or 0) > 0:
            print(f"using ReSpeaker input: {index} {name}")
            return index
    default = sd.default.device[0]
    print(f"no seeed input, using default device {default}")
    return default


def _input_channel_count(index: int) -> int:
    import sounddevice as sd

    try:
        info = sd.query_devices(index)
    except Exception:
        return 1
    n = int(info.get("max_input_channels") or 1)
    if n >= 2:
        return 2
    return 1 if n >= 1 else 1


def _downmix_pcm16(data: bytes, channels: int) -> bytes:
    """Vosk wants mono s16le. ReSpeaker capture is often 2ch."""
    raw = bytes(data)
    if channels <= 1 or not raw:
        return raw
    import array

    frame = 2 * channels
    usable = raw[: len(raw) - (len(raw) % frame)]
    samples = array.array("h")
    samples.frombytes(usable)
    mono = array.array("h")
    for i in range(0, len(samples), channels):
        mono.append(int(sum(samples[i : i + channels]) / channels))
    return mono.tobytes()


def vosk_listen_worker(
    out_q: "queue.Queue[str]",
    stop: threading.Event,
    *,
    device: Optional[int],
    model_path: Path,
    mic_hold: Optional[threading.Event] = None,
) -> None:
    os.environ.setdefault("VOSK_LOG_LEVEL", "0")
    try:
        import sounddevice as sd
        import vosk
    except ImportError as exc:
        out_q.put(f"__error__ 缺依赖 {exc}. 在 ~/lelamp_runtime 执行: uv add vosk")
        return
    if not (model_path / "am" / "final.mdl").is_file():
        out_q.put("__error__ 还没有中文离线模型。先运行: sudo uv run python local_main.py --download-vosk")
        return
    try:
        model = vosk.Model(str(model_path))
        rec = vosk.KaldiRecognizer(model, 16000)
        rec.SetWords(True)
        index = find_input_device(device)
        wanted = _input_channel_count(index)
        stream = None
        channels = wanted
        last_exc: Optional[BaseException] = None
        seen_channels = set()
        for channels in (wanted, 1, 2):
            if channels in seen_channels:
                continue
            seen_channels.add(channels)
            print(f"打开麦克风 index={index} channels={channels} rate=16000 …")
            try:
                stream = sd.RawInputStream(
                    samplerate=16000,
                    blocksize=4000,
                    device=index,
                    dtype="int16",
                    channels=channels,
                )
                stream.start()
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                print(f"channels={channels} 打不开: {exc}")
                stream = None
        if stream is None:
            raise last_exc or RuntimeError("麦克风打不开")
        try:
            print("麦克风流已开")
            out_q.put("__ready__")
            last_partial = ""
            while not stop.is_set():
                if mic_hold is not None and mic_hold.is_set():
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    print("麦克风让出喇叭")
                    while mic_hold.is_set() and not stop.is_set():
                        time.sleep(0.05)
                    try:
                        stream.start()
                        print("麦克风收回")
                    except Exception as exc:
                        print(f"麦克风收回失败: {exc}")
                    last_partial = ""
                    continue
                data, _overflow = stream.read(4000)
                chunk = _downmix_pcm16(bytes(data), channels)
                if rec.AcceptWaveform(chunk):
                    payload = json.loads(rec.Result())
                    text = (payload.get("text") or "").strip()
                    if text:
                        out_q.put(text)
                    last_partial = ""
                else:
                    payload = json.loads(rec.PartialResult())
                    partial = (payload.get("partial") or "").strip()
                    if partial and partial != last_partial:
                        last_partial = partial
                        out_q.put(f"__partial__ {partial}")
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
    except Exception as exc:
        out_q.put(f"__error__ 麦克风失败: {exc}")


def _command_with_watch_stop(lamp: "LocalLamp", raw: str, cmd: Command) -> Command:
    """While following, 停 / 好了 / 别看 mean stop watching."""
    if lamp.watching and cmd.kind in {"unknown", "noop"} and _is_short_watch_stop(raw):
        return Command("watch_stop", None, "好，不看了。")
    return cmd


def dispatch_text(lamp: LocalLamp, raw: str) -> str:
    cmd = _command_with_watch_stop(lamp, raw, parse_line(raw))
    if cmd.kind == "quit":
        lamp.stop_watch_person()
        print(cmd.reply)
        return "quit"
    text = lamp.apply(cmd)
    if text:
        print(text)
    return cmd.kind


def apply_speech(lamp: LocalLamp, transcript: str) -> str:
    compact = _compact_speech(transcript)
    phrase = extract_spoken_command(transcript)
    raw = phrase or compact or (transcript or "").strip()
    cmd = _command_with_watch_stop(lamp, raw, parse_line(raw))
    # While a song plays the mic stays open, so lyrics become noise.
    # Drop unknown fragments quietly; real commands still go through.
    if lamp.music_playing and cmd.kind in {"unknown", "noop"}:
        return "unknown"
    if lamp.watching and cmd.kind in {"unknown", "noop"}:
        return "unknown"
    print(f"灯< {transcript}")
    if lamp.music_playing and cmd.kind not in {
        "music_stop",
        "quit",
        "music",
        "music_next",
        "volume_delta",
        "volume",
        "music_loop",
        "watch_stop",
    }:
        print("正在放歌")
        return "busy"
    if cmd.kind in {"unknown", "noop"}:
        print(f"听到「{transcript}」，但不是灯的指令。")
        return "unknown"
    if phrase and phrase != compact:
        print(f"听成：{phrase}")
    if cmd.kind == "watch_stop":
        return dispatch_text(lamp, "别看了")
    return dispatch_text(lamp, raw)


def run_listen_loop(lamp: LocalLamp, *, device: Optional[int], model_path: Path) -> int:
    stop = threading.Event()
    out_q: "queue.Queue[str]" = queue.Queue()
    worker = threading.Thread(
        target=vosk_listen_worker,
        kwargs={
            "out_q": out_q,
            "stop": stop,
            "device": device,
            "model_path": model_path,
            "mic_hold": lamp.mic_hold,
        },
        daemon=True,
    )
    worker.start()
    print("麦克风线程已开。请说：你好、点头、看我、关灯、音乐、下一首、大点声。打字回车也可以。")
    print("放歌时麦克风继续听：停止音乐 / 下一首 / 大点声 / 小点声 / 循环播放 / 单曲循环。")
    print("看人时麦克风继续听：停 / 好了 / 别看了。点头、音乐、关灯也会停下跟随。")
    try:
        while True:
            try:
                item = out_q.get(timeout=0.1)
            except queue.Empty:
                item = None
            if item == "__ready__":
                print("麦克风好了，请说话。")
            elif item and item.startswith("__partial__ "):
                print(f"\r听… {item[len('__partial__ '):]}", end="", flush=True)
            elif item and item.startswith("__error__ "):
                print()
                print(item[len("__error__ "):])
                return 1
            elif item:
                print()
                if apply_speech(lamp, item) == "quit":
                    return 0
            if _stdin_is_tty() and select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line == "":
                    print("好，我先歇着。")
                    return 0
                print()
                if dispatch_text(lamp, line) == "quit":
                    return 0
    except KeyboardInterrupt:
        print()
        print("好，我先歇着。")
        return 0
    finally:
        stop.set()


def _stdin_is_tty() -> bool:
    """Keyboard REPL is only for a real terminal. systemd stdin is /dev/null."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _effective_home() -> Path:
    """Home of the real user, even when launched with sudo uv run."""
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            return Path("/home") / sudo_user
    return Path.home()


def lerobot_calibration_homes() -> List[Path]:
    """Homes that may already hold a LeLamp Feetech calibration json.

    Official calibrate is ``sudo uv run -m lelamp.calibrate``, which writes
    ``/root/.cache/huggingface/lerobot/...``. systemd then sets
    ``HOME=/home/spocklamp``, so LeRobot looks in the empty user cache and
    ``play`` raises ``has no calibration registered``. Search both.
    """
    homes: List[Path] = []
    extra = (os.environ.get("LELAMP_CALIBRATION_HOME") or "").strip()
    if extra:
        homes.append(Path(extra).expanduser())
    sudo_user = (os.environ.get("SUDO_USER") or "").strip()
    if sudo_user and sudo_user != "root":
        try:
            homes.append(Path(pwd.getpwnam(sudo_user).pw_dir))
        except KeyError:
            homes.append(Path("/home") / sudo_user)
    for raw in (Path.home(), _effective_home(), Path("/root"), Path("/home/spocklamp")):
        homes.append(raw)
    seen: List[Path] = []
    for home in homes:
        if home and home not in seen:
            seen.append(home)
    return seen


def _looks_like_lelamp_calibration(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "base_yaw" in data and "wrist_pitch" in data


def find_lerobot_calibration_file(lamp_id: str = "lelamp") -> Optional[Path]:
    """Return an existing LeLamp calibration json. Never create a new one."""
    pinned = (os.environ.get("LELAMP_CALIBRATION") or "").strip()
    if pinned:
        path = Path(pinned).expanduser()
        if path.is_file() and _looks_like_lelamp_calibration(path):
            return path
    names: List[str] = []
    for name in (lamp_id, "lelamp"):
        if name and name not in names:
            names.append(name)
    exact: List[Path] = []
    fuzzy: List[Path] = []
    for home in lerobot_calibration_homes():
        cache = home / ".cache" / "huggingface" / "lerobot"
        cal_root = cache / "calibration"
        for robot_name in ("lelamp_follower", "le_lamp_follower"):
            folder = cal_root / "robots" / robot_name
            for name in names:
                candidate = folder / f"{name}.json"
                if candidate.is_file() and _looks_like_lelamp_calibration(candidate):
                    exact.append(candidate)
        if cal_root.is_dir():
            try:
                matches = list(cal_root.rglob("*.json"))
            except OSError:
                matches = []
            for candidate in matches:
                if _looks_like_lelamp_calibration(candidate):
                    fuzzy.append(candidate)
    seen: List[Path] = []
    for path in exact + fuzzy:
        resolved = path.resolve()
        if resolved not in seen:
            seen.append(resolved)
    return seen[0] if seen else None


def apply_lerobot_calibration_env(lamp_id: str = "lelamp") -> Optional[Path]:
    """Set HF_LEROBOT_* so LeLampFollower.__init__ can load the existing json.

    Must run before ``import MotorsService`` / lerobot if those constants
    still read the environment at import time.
    """
    fpath = find_lerobot_calibration_file(lamp_id)
    if fpath is None:
        return None
    for parent in fpath.parents:
        if parent.name == "calibration":
            os.environ["HF_LEROBOT_CALIBRATION"] = str(parent)
            if parent.parent.name == "lerobot":
                os.environ["HF_LEROBOT_HOME"] = str(parent.parent)
            break
    print(f"calibration file {fpath}", flush=True)
    return fpath


def ensure_motors_calibration(svc: object, lamp_id: str = "lelamp") -> bool:
    """Put existing calibration onto the Feetech bus. Do not run calibrate()."""
    robot = getattr(svc, "robot", None)
    if robot is None:
        return False
    bus = getattr(robot, "bus", None)

    def _has_cal(obj: object) -> bool:
        return bool(getattr(obj, "calibration", None))

    if bus is not None and _has_cal(bus):
        print("calibration already on bus", flush=True)
        return True
    if _has_cal(robot) and bus is not None:
        bus.calibration = robot.calibration
        print("calibration copied robot → bus", flush=True)
        return True

    fpath = find_lerobot_calibration_file(lamp_id)
    if fpath is not None:
        load = getattr(robot, "_load_calibration", None)
        try:
            if callable(load):
                load(fpath)
            if _has_cal(robot) and bus is not None:
                bus.calibration = robot.calibration
            if bus is not None and _has_cal(bus):
                print(f"calibration loaded {fpath}", flush=True)
                return True
        except Exception as exc:
            print(f"calibration json 读失败 {fpath}: {exc}", flush=True)

    read = getattr(bus, "read_calibration", None) if bus is not None else None
    if callable(read):
        try:
            cal = read()
            if cal:
                bus.calibration = cal
                robot.calibration = cal
                print(
                    "calibration from motor EEPROM（当前 HOME 下没有 json）",
                    flush=True,
                )
                return True
        except Exception as exc:
            print(f"calibration EEPROM 读失败: {exc}", flush=True)

    looked = "\n".join(f"  {home}" for home in lerobot_calibration_homes())
    print(
        "has no calibration registered。舵机已连上，只是校准 json 不在当前 HOME。\n"
        "官方 sudo calibrate 写到 /root/.cache/huggingface/lerobot/，\n"
        "开机服务 HOME 是用户目录。不要重新 calibrate。已找过:\n"
        f"{looked}",
        flush=True,
    )
    return False


def look_at_search_roots() -> List[Path]:
    here = Path(__file__).resolve().parent
    home = _effective_home()
    roots = [
        here,
        here / "lelamp_il",
        home / "hermes-agent" / "lelamp_il",
        home / "lelamp_runtime" / "lelamp_il",
        home / "lelamp_runtime",
        Path.home() / "hermes-agent" / "lelamp_il",
        Path.home() / "lelamp_runtime",
    ]
    try:
        roots.insert(2, here.parents[2] / "lelamp_il")
    except IndexError:
        pass
    extra = (os.environ.get("LELAMP_IL_DIR") or "").strip()
    if extra:
        roots.insert(0, Path(extra).expanduser())
    seen: List[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


def resolve_look_at_artifacts() -> Tuple[Optional[Path], Optional[Path], Optional[Path]]:
    """Return (il_dir, onnx, meta). Missing files yield Nones."""
    for root in look_at_search_roots():
        nested = (root / "artifacts" / "tiny_lamp_int8.onnx", root / "artifacts" / "meta.json")
        flat = (root / "tiny_lamp_int8.onnx", root / "meta.json")
        for model, meta in (nested, flat):
            if model.is_file() and meta.is_file():
                return root, model, meta
    return None, None, None


def _import_run_watch_person():
    """Load lelamp_il.agent_hook without importing onnxruntime at module import.

    Skip copies that still use the old 6-second signature (no stop_event).
    Load by file path so a stale ``import agent_hook`` cannot win.
    """
    candidates = []
    il_dir, _, _ = resolve_look_at_artifacts()
    if il_dir is not None:
        candidates.append(il_dir)
    candidates.extend(look_at_search_roots())
    seen = set()
    last_old = None
    for folder in candidates:
        hook = (folder / "agent_hook.py").resolve()
        if not hook.is_file() or hook in seen:
            continue
        seen.add(hook)
        mod_name = f"lelamp_agent_hook_{abs(hash(str(hook)))}"
        spec = importlib.util.spec_from_file_location(mod_name, hook)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, "run_watch_person", None)
        if not callable(fn):
            continue
        if "stop_event" not in inspect.signature(fn).parameters:
            last_old = hook
            print(f"跳过旧 6 秒 agent_hook: {hook}", flush=True)
            continue
        print(f"watch hook {hook}  {getattr(mod, 'WATCH_REVISION', '?')}", flush=True)
        return fn
    if last_old is not None:
        raise ImportError(
            f"{last_old} 还是 6 秒旧版。请覆盖 ~/hermes-agent/lelamp_il/agent_hook.py"
        )
    raise ImportError("找不到 lelamp_il/agent_hook.py")



class LocalLamp:
    def __init__(
        self,
        *,
        sim: bool,
        port: str,
        lamp_id: str,
        led_count: int,
        brightness: int,
    ) -> None:
        self.sim = sim
        self.port = port
        self.lamp_id = lamp_id
        self.led_count = led_count
        self.brightness = max(0, min(100, brightness))
        self.base_rgb: Tuple[int, int, int] = MOOD_RGB["warm"]
        self.last_rgb: Tuple[int, int, int] = (0, 0, 0)
        self.last_expression = ""
        self.last_music = ""
        self.motors = None
        self.rgb = None
        self._music_proc = None
        self._music_stop = threading.Event()
        self._viz_thread = None
        self._music_playing = False
        self._playlist: List[Path] = []
        self._playlist_index = 0
        self._viz_rgb: Optional[Tuple[int, int, int]] = None
        self._pre_music_rgb: Tuple[int, int, int] = MOOD_RGB["warm"]
        self.music_volume = 85
        self._loop_mode = "all"
        self.mic_hold = threading.Event()
        self._watch_stop = threading.Event()
        self._watch_thread = None
        self._watch_playing = False

    def start(self) -> None:
        folder = ensure_music_dir()
        print(f"music 文件夹 {folder}")
        if self.sim:
            print("[sim] skip motors/rgb connect")
            return
        self._try_start_motors()
        self._try_start_rgb()
        print(
            f"motors {'on' if self.motors is not None else 'MISSING'} {self.port}  "
            f"rgb {'on' if self.rgb is not None else 'MISSING'} leds={self.led_count}",
            flush=True,
        )

    def _try_start_rgb(self) -> None:
        from lelamp.service.rgb.rgb_service import RGBService

        try:
            self.rgb = RGBService(
                led_count=self.led_count,
                led_pin=12,
                led_freq_hz=800000,
                led_dma=10,
                led_brightness=255,
                led_invert=False,
                led_channel=0,
            )
            self.rgb.start()
        except Exception as exc:
            print(f"RGB 没起来: {exc}", flush=True)
            self.rgb = None

    def _try_start_motors(self) -> None:
        """OpenDuck: wait for the bus, then claim it. Retry — do not give up."""
        apply_lerobot_calibration_env(self.lamp_id)
        from lelamp.service.motors.motors_service import MotorsService

        wait_for_serial_port(self.port)
        deadline = time.monotonic() + SERIAL_WAIT_SECONDS
        attempt = 0
        last_exc: Optional[BaseException] = None
        while True:
            attempt += 1
            holders = serial_port_users(self.port)
            if holders:
                print(
                    f"{self.port} 被占用 PID={','.join(holders)}。"
                    "先停掉另一份 local_main / lelamp.service："
                    "sudo systemctl stop lelamp.service lelamp-local",
                    flush=True,
                )
            svc = None
            try:
                svc = MotorsService(port=self.port, lamp_id=self.lamp_id, fps=30)
                svc.start()
            except Exception as exc:
                last_exc = exc
                print(f"舵机第 {attempt} 次没起来: {exc}", flush=True)
                if svc is not None:
                    self.motors = svc
                    self._release_motors()
                else:
                    self.motors = None
                if time.monotonic() >= deadline:
                    break
                time.sleep(1.5)
                continue
            self.motors = svc
            ensure_motors_calibration(svc, self.lamp_id)
            if SERVO_SETTLE_SECONDS > 0:
                print(
                    f"舵机就绪，等 {SERVO_SETTLE_SECONDS:.0f}s 再醒来（OpenDuck turn_on）",
                    flush=True,
                )
                time.sleep(SERVO_SETTLE_SECONDS)
            print(f"motors on {self.port}", flush=True)
            return
        print(f"舵机暂时没连上（点头时会再试）: {last_exc}", flush=True)

    def stop(self) -> None:
        self.stop_watch_person()
        self.stop_music()
        for svc in (self.motors, self.rgb):
            if svc is None:
                continue
            for name in ("stop", "close", "disconnect"):
                fn = getattr(svc, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def _play(self, recording: str, *, wait: bool = True) -> None:
        self.last_expression = recording
        if self.sim:
            print(f"[sim] play {recording}")
            return
        if not self._ensure_motors():
            print(f"[no motors] play {recording}", flush=True)
            return
        self.motors.dispatch("play", recording)

    def _apply_rgb(self, rgb: Tuple[int, int, int]) -> None:
        self.base_rgb = rgb
        scaled = _scale_rgb(rgb, self.brightness)
        self.last_rgb = scaled
        if self.sim or self.rgb is None:
            print(f"[sim] rgb {scaled} brightness={self.brightness}")
            return
        self.rgb.dispatch("solid", scaled)

    def _paint_rgb(self, rgb: Tuple[int, int, int], *, quiet: bool = False) -> None:
        """Push a color without treating it as the saved mood."""
        scaled = _scale_rgb(rgb, self.brightness)
        self.last_rgb = scaled
        if self.sim or self.rgb is None:
            if not quiet:
                print(f"[sim] rgb {scaled} brightness={self.brightness}")
            return
        self.rgb.dispatch("solid", scaled)

    @property
    def music_playing(self) -> bool:
        return bool(self._music_playing)

    @property
    def watching(self) -> bool:
        return bool(self._watch_playing)

    def _rebuild_playlist(self, *, shuffle: bool = True) -> List[Path]:
        root = ensure_music_dir()
        files = list_music_files(root)
        if not files:
            files = ensure_builtin_music(root / ".builtin")
            print(f"music 文件夹是空的，把 wav/mp3 放到 {root}")
        self._playlist = list(files)
        if shuffle:
            random.shuffle(self._playlist)
        self._playlist_index = 0
        return self._playlist

    def _viz_loop(self, bpm: int, hue_shift: float) -> None:
        t0 = time.monotonic()
        self._viz_rgb = None
        while not self._music_stop.is_set():
            t = time.monotonic() - t0
            target = music_viz_color(t=t, bpm=bpm, hue_shift=hue_shift)
            if self._viz_rgb is None:
                self._viz_rgb = target
            else:
                self._viz_rgb = lerp_rgb(self._viz_rgb, target, 0.18)
            self._paint_rgb(self._viz_rgb, quiet=True)
            proc = self._music_proc
            if proc is not None and proc.poll() is not None:
                nxt = self._continue_after_track()
                if nxt is None or self._music_stop.is_set():
                    break
                path, bpm = nxt
                hue_shift = random.random()
                t0 = time.monotonic()
                self.last_music = path.name
                print(f"music {path.name} bpm={bpm}")
                started = self._start_player(path)
                self._music_proc = started
                if started is None:
                    break
                continue
            time.sleep(0.04)
        self._music_playing = False
        self._release_mic()

    def _continue_after_track(self) -> Optional[Tuple[Path, int]]:
        if not self._playlist:
            return None
        if self._loop_mode == "one":
            path = self._playlist[self._playlist_index % len(self._playlist)]
            return path, bpm_from_name(path) or 120
        if self._loop_mode == "all":
            self._playlist_index = (self._playlist_index + 1) % len(self._playlist)
            path = self._playlist[self._playlist_index]
            return path, bpm_from_name(path) or 120
        return None

    def _hold_mic(self) -> None:
        self.mic_hold.set()
        time.sleep(0.4)
        print("麦克风暂停")

    def _release_mic(self) -> None:
        if self.mic_hold.is_set():
            self.mic_hold.clear()
            print("麦克风继续听")

    def _start_player(self, path: Path) -> Optional["subprocess.Popen[bytes]"]:
        """Start playback without silencing the mic for the whole song.

        ReSpeaker capture and playback are separate ALSA streams. Holding the
        mic for the duration made 停止音乐 / 下一首 impossible to speak.
        If the player cannot open while capture is live, pause the mic briefly,
        start the player, then give the mic back.
        """
        proc = start_music_player(path, volume=self.music_volume)
        if proc is not None:
            return proc
        self._hold_mic()
        try:
            proc = start_music_player(path, volume=self.music_volume)
        finally:
            self._release_mic()
        return proc

    def _play_current_track(self) -> str:
        if not self._playlist:
            try:
                self._rebuild_playlist(shuffle=True)
            except Exception:
                self._playlist = []
        if not self._playlist:
            print("没有音乐")
            return "没有音乐"
        path = self._playlist[self._playlist_index % len(self._playlist)]
        bpm = bpm_from_name(path) or 120
        hue_shift = random.random()
        self.last_music = path.name
        self._pre_music_rgb = self.base_rgb
        wash = music_viz_color(t=0.0, bpm=bpm, hue_shift=hue_shift)
        print(f"music {path.name} bpm={bpm} vol={self.music_volume} loop={self._loop_mode}")
        self._music_stop.clear()
        self._music_playing = True
        self._paint_rgb(wash, quiet=self.sim)
        if self.sim:
            return f"music {path.name}"
        proc = self._start_player(path)
        self._music_proc = proc
        if proc is None:
            print_mpg123_install_hint()
        self._viz_thread = threading.Thread(
            target=self._viz_loop,
            args=(bpm, hue_shift),
            daemon=True,
            name="lelamp-viz",
        )
        self._viz_thread.start()
        return f"music {path.name}"

    def play_music(self) -> str:
        self.stop_music(restore=False)
        files = self._rebuild_playlist(shuffle=True)
        if not files:
            return "没有音乐"
        return self._play_current_track()

    def next_music(self) -> str:
        if not self._playlist:
            return self.play_music()
        self.stop_music(restore=False)
        self._playlist_index = (self._playlist_index + 1) % len(self._playlist)
        print("music next")
        return self._play_current_track()

    def stop_music(self, *, restore: bool = True) -> str:
        self._music_stop.set()
        proc = self._music_proc
        self._music_proc = None
        _stop_process(proc)
        thread = self._viz_thread
        self._viz_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.5)
        was = self._music_playing
        self._music_playing = False
        self._release_mic()
        if was and restore:
            print("music stop")
            self._apply_rgb(self._pre_music_rgb)
        return "停了。"

    def apply(self, cmd: Command) -> str:
        if cmd.kind == "noop":
            return ""
        if cmd.kind in {"quit", "help", "unknown"}:
            return cmd.reply
        if cmd.kind == "status":
            return (
                f"sim={self.sim} expression={self.last_expression or '-'} "
                f"rgb={self.last_rgb} brightness={self.brightness} "
                f"music={self.last_music or '-'} volume={self.music_volume} "
                f"loop={self._loop_mode} watch={'on' if self.watching else 'off'}"
            )
        if cmd.kind in {"express", "music", "music_next", "music_stop", "mood", "rgb"}:
            if self.watching:
                self.stop_watch_person()
        if cmd.kind == "music":
            return self.play_music()
        if cmd.kind == "music_next":
            return self.next_music()
        if cmd.kind == "music_stop":
            return self.stop_music()
        if cmd.kind == "volume_delta":
            return self.adjust_volume(int(cmd.payload))
        if cmd.kind == "volume":
            return self.set_volume(int(cmd.payload))
        if cmd.kind == "music_loop":
            return self.set_loop_mode(str(cmd.payload))
        if cmd.kind == "watch_person":
            spoken = self.watch_person()
            return spoken or cmd.reply
        if cmd.kind == "watch_stop":
            spoken = self.stop_watch_person()
            return spoken or "没在看。"
        if cmd.kind == "express":
            rec = str(cmd.payload)
            self._play(rec)
            self._apply_rgb(EXPRESSION_RGB[rec])
            return cmd.reply
        if cmd.kind == "mood":
            mood = str(cmd.payload)
            if mood == "auto":
                mood, bri = circadian_mood()
                self.brightness = bri
            self._apply_rgb(MOOD_RGB[mood])
            return cmd.reply
        if cmd.kind == "brightness_delta":
            self.brightness = max(0, min(100, self.brightness + int(cmd.payload)))
            self._apply_rgb(self.base_rgb)
            return f"{cmd.reply} 亮度 {self.brightness}%"
        if cmd.kind == "brightness_set":
            self.brightness = int(cmd.payload)
            self._apply_rgb(self.base_rgb)
            return f"{cmd.reply} 亮度 {self.brightness}%"
        if cmd.kind == "rgb":
            rgb = cmd.payload
            assert isinstance(rgb, tuple)
            self._apply_rgb((int(rgb[0]), int(rgb[1]), int(rgb[2])))
            return cmd.reply
        return cmd.reply

    def set_volume(self, percent: int) -> str:
        self.music_volume = max(0, min(100, int(percent)))
        if not self.sim:
            apply_playback_volume(self.music_volume)
        print(f"volume {self.music_volume}%")
        return f"音量 {self.music_volume}%"

    def adjust_volume(self, delta: int) -> str:
        return self.set_volume(self.music_volume + int(delta))

    def set_loop_mode(self, mode: str) -> str:
        key = (mode or "").strip().lower()
        if key in {"one", "single", "track"}:
            self._loop_mode = "one"
            print("loop one")
            return "单曲循环。"
        self._loop_mode = "all"
        print("loop all")
        return "循环播放。"

    def _release_motors(self) -> None:
        svc = self.motors
        self.motors = None
        if svc is None:
            return
        for name in ("stop", "close", "disconnect"):
            fn = getattr(svc, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break

    def _reconnect_motors(self) -> None:
        if self.sim or self.motors is not None:
            return
        apply_lerobot_calibration_env(self.lamp_id)
        from lelamp.service.motors.motors_service import MotorsService

        holders = serial_port_users(self.port)
        if holders:
            print(
                f"{self.port} 被占用 PID={','.join(holders)}，舵机连不上",
                flush=True,
            )
        svc = MotorsService(port=self.port, lamp_id=self.lamp_id, fps=30)
        svc.start()
        ensure_motors_calibration(svc, self.lamp_id)
        self.motors = svc

    def _ensure_motors(self) -> bool:
        if self.sim:
            return False
        if self.motors is not None:
            return True
        try:
            self._reconnect_motors()
        except Exception as exc:
            print(f"舵机重连失败: {exc}", flush=True)
            self.motors = None
            return False
        return self.motors is not None

    def watch_person(self, seconds: float = 0.0) -> str:
        """Follow the person/hand until stop_watch_person(). Never scanning/nod."""
        if self.watching:
            return "已经在看了。说停或别看了就停。"
        self.stop_music(restore=False)
        self.last_expression = "watch_person"
        self._apply_rgb(MOOD_RGB["listen"])
        self._watch_stop.clear()
        self._watch_playing = True
        if self.sim:
            print("[sim] watch_person until stop")
            return "一直看着你。说停或别看了就停。"
        _il_dir, model, meta = resolve_look_at_artifacts()
        if model is None or meta is None:
            self._watch_playing = False
            looked = "\n".join(f"  {p}" for p in look_at_search_roots())
            print("没有 tiny_lamp_int8.onnx + meta.json。已搜索:\n" + looked)
            print("模型应放在 /home/spocklamp/hermes-agent/lelamp_il/artifacts/")
            print("若用 sudo 启动，可先: sudo LELAMP_IL_DIR=/home/spocklamp/hermes-agent/lelamp_il \\")
            print("  uv run python local_main.py --listen")
            return "看人策略还没拷到灯上。"
        print(
            f"look-at {WATCH_REVISION}  until stop  file={Path(__file__).resolve()}",
            flush=True,
        )
        self._release_motors()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(model, meta, float(seconds or 0.0)),
            daemon=True,
            name="lelamp-watch",
        )
        self._watch_thread.start()
        return "一直看着你。说停或别看了就停。"

    def _watch_loop(self, model, meta, seconds: float) -> None:
        try:
            run_watch_person = _import_run_watch_person()
            msg = run_watch_person(
                model=model,
                meta=meta,
                port=self.port,
                seconds=seconds,
                stop_event=self._watch_stop,
            )
            if msg:
                print(msg)
        except Exception as exc:
            print(f"看人失败: {exc}")
        finally:
            self._watch_playing = False
            try:
                if self.motors is None:
                    self._reconnect_motors()
            except Exception as exc:
                print(f"舵机重连失败: {exc}")

    def stop_watch_person(self) -> str:
        was = self._watch_playing
        self._watch_stop.set()
        thread = self._watch_thread
        self._watch_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._watch_playing = False
        if not self.sim:
            try:
                if self.motors is None:
                    self._reconnect_motors()
            except Exception as exc:
                print(f"舵机重连失败: {exc}")
        if was:
            print("watch stop")
            return "好，不看了。"
        return ""

    def wake(self) -> None:
        mood, bri = circadian_mood()
        self.brightness = bri
        self._apply_rgb(MOOD_RGB[mood])
        self._play("wake_up")
        print(f"台灯醒了。现在 {mood} 光，亮度 {self.brightness}%。输入 help 看命令。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeLamp local Stage 4 + music folder + look-at (no OpenAI)")
    parser.add_argument("--sim", action="store_true", help="no motors/LED, print actions")
    parser.add_argument("--port", default=os.environ.get("LELAMP_PORT", "/dev/ttyACM0"))
    parser.add_argument("--id", dest="lamp_id", default=os.environ.get("LELAMP_ID", "lelamp"))
    parser.add_argument("--led-count", type=int, default=int(os.environ.get("LELAMP_LED_COUNT", "64")))
    parser.add_argument("--no-wake", action="store_true", help="skip wake_up on start")
    parser.add_argument("--listen", action="store_true", help="Stage 4: Vosk keywords, music folder, look-at")
    parser.add_argument(
        "--repl",
        action="store_true",
        help="keyboard prompt (do not auto-listen under systemd / LELAMP_LISTEN)",
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="install systemd unit like OpenDuck duck-walk.service: wake + listen after power-on",
    )
    parser.add_argument(
        "--boot-status",
        action="store_true",
        help="print systemd unit, wrapper, and journal for lelamp-local",
    )
    parser.add_argument("--download-vosk", action="store_true", help="download offline Chinese Vosk model")
    parser.add_argument("--say", action="append", default=[], help="inject a spoken phrase (repeatable)")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input index")
    parser.add_argument("--model", type=Path, default=None, help="path to vosk-model-small-cn-0.22")
    parser.add_argument(
        "--audio",
        choices=("auto", "bt", "seeed"),
        default=(os.environ.get("LELAMP_AUDIO") or "seeed"),
        help="music output: ReSpeaker (seeed, default), bluetooth (bt), or auto. Never HDMI.",
    )
    parser.add_argument("--bt-connect", action="store_true", help="connect a paired Bluetooth speaker and exit")
    parser.add_argument("--bt-mac", default=None, help="Bluetooth MAC to connect, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument("--show-stage", action="store_true", help="print stage number and exit")
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const="",
        metavar="NAME",
        help="copy this file to lamp_snapshots/NAME.py (default stageN.py) and exit",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    global AUDIO_PREFER
    args = build_parser().parse_args(argv)
    AUDIO_PREFER = args.audio
    if args.show_stage:
        print(f"{AGENT_STAGE} {AGENT_LABEL}")
        return 0
    if args.snapshot is not None:
        snapshot_current(args.snapshot)
        return 0
    if args.install_service:
        install_boot_service()
        return 0
    if args.boot_status:
        return print_boot_status()
    print(f"local_main  stage {AGENT_STAGE}  ({AGENT_LABEL})")
    print(f"boot {BOOT_REVISION}", flush=True)
    print(
        f"look-at {WATCH_REVISION}  {Path(__file__).resolve()}  "
        "（看我会一直跟；若结束打印「6.0 秒」=还在跑旧脚本）"
    )
    if args.bt_connect:
        connected = connect_bluetooth_speaker(args.bt_mac)
        return 0 if connected else 1
    if args.download_vosk:
        download_vosk_model(args.model)
        return 0
    if not args.sim and args.audio in {"bt", "bluetooth"}:
        connected = connect_bluetooth_speaker(args.bt_mac)
        if not connected:
            print("没有蓝牙音箱，不使用 HDMI。可改回灯上喇叭：--audio seeed")
    lamp = LocalLamp(
        sim=args.sim,
        port=args.port,
        lamp_id=args.lamp_id,
        led_count=args.led_count,
        brightness=70,
    )
    try:
        try:
            lamp.start()
        except Exception as exc:
            print(f"硬件启动失败（仍会听令）: {exc}", flush=True)
        if not args.no_wake:
            try:
                lamp.wake()
            except Exception as exc:
                print(f"醒来动作失败（仍会听令）: {exc}", flush=True)
        for phrase in args.say:
            if apply_speech(lamp, phrase) == "quit":
                return 0
        if args.say and not _want_listen(args):
            return 0
        if _want_listen(args):
            model_path = args.model if args.model is not None else vosk_model_dir()
            return run_listen_loop(lamp, device=args.device, model_path=Path(model_path))
        while True:
            try:
                raw = input("灯> ")
            except (EOFError, KeyboardInterrupt):
                print()
                print("好，我先歇着。")
                return 0
            if dispatch_text(lamp, raw) == "quit":
                return 0
    finally:
        lamp.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
