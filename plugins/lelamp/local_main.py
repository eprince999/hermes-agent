"""LeLamp local agent — Stage 2 + music folder, no OpenAI, no LiveKit.

Always copy onto the Pi as ``~/lelamp_runtime/local_main.py``.
Do not rename the runnable file. Snapshot 2 is the keyword-only archive::

    mkdir -p ~/lelamp_runtime/lamp_snapshots
    cp local_main.py lamp_snapshots/stage2.py

Keep official ``main.py`` untouched. From the runtime repo root:

    sudo uv run python local_main.py
    sudo uv run python local_main.py --sim
    sudo uv run python local_main.py --say 你好 --say 关灯
    sudo uv run python local_main.py --say 音乐
    sudo uv run python local_main.py --download-vosk
    sudo uv run python local_main.py --listen
    sudo uv run python local_main.py --snapshot

Type Chinese commands, or with ``--listen`` speak them to the ReSpeaker.
Say 音乐 to play a random file from the music/ folder. ``q`` or Ctrl+C quits.

Roadmap:
  1. keyboard + motors + RGB
  2. on-device speech keywords (Vosk, no cloud)
  3. music folder random play (this file)
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import select
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
AGENT_STAGE = 2
AGENT_LABEL = "keyboard + vosk listen + music"


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
    "跳舞", "放歌",
    "music", "play music", "playmusic", "dance",
}
MUSIC_STOP = {
    "停止音乐", "别放了", "关掉音乐",
    "stop music", "stopmusic", "stop dancing",
}
_BUILTIN_TRACKS = (
    ("pulse_100.wav", 100, (0, 3, 7, 10)),
    ("bounce_120.wav", 120, (0, 4, 7, 12)),
    ("spark_140.wav", 140, (0, 5, 7, 9)),
)

HELP_TEXT = """本地台灯 Stage 2（无 OpenAI）
动作：你好 / 点头 / 摇头 / 好奇 / 张望 / 开心 / 兴奋 / 惊讶 / 害羞 / 难过 / 待机
灯光：开灯 / 关灯 / 暖光 / 冷光 / 自动 / 亮一点 / 暗一点
音乐：音乐 / 放音乐（随机播放 music/ 文件夹）  停止音乐
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
    if music_kind == "music_stop":
        return Command("music_stop", None, "停了。")

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
            "我还没学会这句。可以说：你好、点头、暖光、关灯、音乐。输入 help 看全部。",
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
    extra = ("亮一点", "亮一些", "亮点", "暗一点", "暗一些", "暗点", "最亮", "最暗", "帮助", "退出")
    phrases = (
        set(LIGHT_ONLY)
        | set(ALIASES)
        | set(RECORDINGS)
        | set(MUSIC_START)
        | set(MUSIC_STOP)
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


def _music_kind(transcript: str) -> Optional[str]:
    """Return music / music_stop if the transcript names a song command."""
    compact = _compact_speech(transcript)
    low = compact.lower()
    blobs = (compact, low, (transcript or "").strip().lower())
    for phrase in sorted(MUSIC_STOP, key=len, reverse=True):
        needle = _compact_speech(phrase).lower()
        if needle and any(needle in blob for blob in blobs):
            return "music_stop"
    for phrase in sorted(MUSIC_START, key=len, reverse=True):
        needle = _compact_speech(phrase).lower()
        if needle and any(needle in blob for blob in blobs):
            return "music"
    if "音乐" in compact:
        return "music"
    return None


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


def _bin(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path("/usr/bin") / name
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


def find_alsa_playback_device() -> Optional[str]:
    """Prefer the ReSpeaker/seeed speaker over HDMI."""
    aplay = _bin("aplay")
    if not aplay:
        return None
    try:
        listing = subprocess.check_output(
            [aplay, "-l"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=3,
        )
    except Exception:
        return None
    for line in listing.splitlines():
        low = line.lower()
        if not low.startswith("card "):
            continue
        if not any(tag in low for tag in ("seeed", "respeaker", "voicecard", "array")):
            continue
        try:
            card = int(line.split(":", 1)[0].split()[1])
        except (IndexError, ValueError):
            continue
        return f"plughw:{card},0"
    return None


def _player_env(device: Optional[str]) -> Dict[str, str]:
    env = os.environ.copy()
    if device:
        env["AUDIODEV"] = device
        env.setdefault("SDL_AUDIODRIVER", "alsa")
    return env


def music_player_commands(path: Path, *, device: Optional[str] = None) -> List[List[str]]:
    """Build argv lists for common Pi players. aplay alone cannot decode mp3."""
    path_s = str(path)
    suffix = path.suffix.lower()
    commands: List[List[str]] = []

    def add(binary: Optional[str], args: Sequence[str]) -> None:
        if binary:
            commands.append([binary, *args])

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
        mpg_args = ["-q"]
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
    except OSError:
        return None
    time.sleep(0.2)
    if proc.poll() is not None:
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


def _spawn_pygame_player(path: Path, *, env: Dict[str, str]) -> Optional["subprocess.Popen[bytes]"]:
    script = (
        "import sys, time\n"
        "path = sys.argv[1]\n"
        "import pygame\n"
        "pygame.mixer.init()\n"
        "pygame.mixer.music.load(path)\n"
        "pygame.mixer.music.play()\n"
        "while pygame.mixer.music.get_busy():\n"
        "    time.sleep(0.15)\n"
    )
    return _spawn_player([sys.executable, "-c", script, str(path)], env=env)


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


def start_music_player(path: Path) -> Optional["subprocess.Popen[bytes]"]:
    device = find_alsa_playback_device()
    env = _player_env(device)
    for argv in music_player_commands(path, device=device):
        proc = _spawn_player(argv, env=env)
        if proc is not None:
            print(f"player {' '.join(argv[:1])}")
            return proc
    piped = _spawn_ffmpeg_aplay(path, device=device, env=env)
    if piped is not None:
        print("player ffmpeg|aplay")
        return piped
    pygame_proc = _spawn_pygame_player(path, env=env)
    if pygame_proc is not None:
        print("player pygame")
        return pygame_proc
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


def vosk_listen_worker(
    out_q: "queue.Queue[str]",
    stop: threading.Event,
    *,
    device: Optional[int],
    model_path: Path,
) -> None:
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
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=8000,
            device=index,
            dtype="int16",
            channels=1,
        ) as stream:
            out_q.put("__ready__")
            last_partial = ""
            while not stop.is_set():
                data, _overflow = stream.read(8000)
                chunk = bytes(data)
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
    except Exception as exc:
        out_q.put(f"__error__ 麦克风失败: {exc}")


def dispatch_text(lamp: LocalLamp, raw: str) -> str:
    cmd = parse_line(raw)
    if cmd.kind == "quit":
        print(cmd.reply)
        return "quit"
    text = lamp.apply(cmd)
    if text:
        print(text)
    return cmd.kind


def apply_speech(lamp: LocalLamp, transcript: str) -> str:
    print(f"灯< {transcript}")
    compact = _compact_speech(transcript)
    phrase = extract_spoken_command(transcript)
    raw = phrase or compact or (transcript or "").strip()
    cmd = parse_line(raw)
    if lamp.music_playing and cmd.kind not in {"music_stop", "quit", "music"}:
        print("正在跳舞")
        return "busy"
    if cmd.kind in {"unknown", "noop"}:
        print(f"听到「{transcript}」，但不是灯的指令。")
        return "unknown"
    if phrase and phrase != compact:
        print(f"听成：{phrase}")
    return dispatch_text(lamp, raw)


def run_listen_loop(lamp: LocalLamp, *, device: Optional[int], model_path: Path) -> int:
    stop = threading.Event()
    out_q: "queue.Queue[str]" = queue.Queue()
    worker = threading.Thread(
        target=vosk_listen_worker,
        kwargs={"out_q": out_q, "stop": stop, "device": device, "model_path": model_path},
        daemon=True,
    )
    worker.start()
    print("麦克风线程已开。请说：你好、点头、关灯、音乐。打字回车也可以。")
    print("音乐指令已开：听到「音乐」就随机播放 music/ 文件夹。")
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
            if select.select([sys.stdin], [], [], 0)[0]:
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
        self._dance_thread = None
        self._music_playing = False

    def start(self) -> None:
        folder = ensure_music_dir()
        print(f"music 文件夹 {folder}")
        if self.sim:
            print("[sim] skip motors/rgb connect")
            return
        from lelamp.service.motors.motors_service import MotorsService
        from lelamp.service.rgb.rgb_service import RGBService

        self.motors = MotorsService(port=self.port, lamp_id=self.lamp_id, fps=30)
        self.rgb = RGBService(
            led_count=self.led_count,
            led_pin=12,
            led_freq_hz=800000,
            led_dma=10,
            led_brightness=255,
            led_invert=False,
            led_channel=0,
        )
        self.motors.start()
        self.rgb.start()
        print(f"motors on {self.port}  rgb leds={self.led_count}")

    def stop(self) -> None:
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
        if self.sim or self.motors is None:
            print(f"[sim] play {recording}")
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

    @property
    def music_playing(self) -> bool:
        return bool(self._music_playing)

    def _flash_rgb(self, rgb: Tuple[int, int, int]) -> None:
        scaled = _scale_rgb(rgb, self.brightness)
        self.last_rgb = scaled
        if self.sim or self.rgb is None:
            print(f"[sim] rgb {scaled} brightness={self.brightness}")
            return
        self.rgb.dispatch("solid", scaled)

    def _dance_step(self, beat: int) -> None:
        # RGB only. Playing official recordings here races MotorsService
        # on /dev/ttyACM0 ("Port is in use").
        self._flash_rgb(MOOD_RGB["happy"] if beat % 2 == 0 else MOOD_RGB["talk"])

    def _dance_loop(self, bpm: int) -> None:
        period = 60.0 / max(40, min(220, int(bpm)))
        beat = 0
        next_beat = time.monotonic()
        while not self._music_stop.is_set():
            now = time.monotonic()
            if now < next_beat:
                time.sleep(min(0.03, next_beat - now))
                continue
            self._dance_step(beat)
            beat += 1
            next_beat += period
            proc = self._music_proc
            if proc is not None and proc.poll() is not None:
                break
        self._music_playing = False

    def play_music(self) -> str:
        self.stop_music()
        try:
            path, bpm = pick_random_track()
        except RuntimeError as exc:
            print(str(exc))
            return "没有音乐"
        self.last_music = path.name
        self.last_expression = "happy_wiggle"
        self._apply_rgb(MOOD_RGB["happy"])
        print(f"music {path.name} bpm={bpm}")
        self._music_stop.clear()
        self._music_playing = True
        if self.sim:
            self._dance_step(0)
            return f"music {path.name}"
        proc = start_music_player(path)
        self._music_proc = proc
        if proc is None:
            print("没有播放器。mp3 需要 mpg123 或 ffmpeg。在 Pi 上执行：")
            print("  sudo apt install -y mpg123 ffmpeg")
        self._dance_thread = threading.Thread(
            target=self._dance_loop,
            args=(bpm,),
            daemon=True,
            name="lelamp-dance",
        )
        self._dance_thread.start()
        return f"music {path.name}"

    def stop_music(self) -> str:
        self._music_stop.set()
        proc = self._music_proc
        self._music_proc = None
        _stop_process(proc)
        thread = self._dance_thread
        self._dance_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.5)
        was = self._music_playing or bool(self.last_music)
        self._music_playing = False
        if was:
            print("music stop")
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
                f"music={self.last_music or '-'}"
            )
        if cmd.kind == "music":
            return self.play_music()
        if cmd.kind == "music_stop":
            return self.stop_music()
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
        if cmd.kind == "volume":
            return cmd.reply + "（Stage 1 还没接管喇叭，先记下。）"
        return cmd.reply

    def wake(self) -> None:
        mood, bri = circadian_mood()
        self.brightness = bri
        self._apply_rgb(MOOD_RGB[mood])
        self._play("wake_up")
        print(f"台灯醒了。现在 {mood} 光，亮度 {self.brightness}%。输入 help 看命令。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeLamp local Stage 2 + music folder (no OpenAI)")
    parser.add_argument("--sim", action="store_true", help="no motors/LED, print actions")
    parser.add_argument("--port", default=os.environ.get("LELAMP_PORT", "/dev/ttyACM0"))
    parser.add_argument("--id", dest="lamp_id", default=os.environ.get("LELAMP_ID", "lelamp"))
    parser.add_argument("--led-count", type=int, default=int(os.environ.get("LELAMP_LED_COUNT", "64")))
    parser.add_argument("--no-wake", action="store_true", help="skip wake_up on start")
    parser.add_argument("--listen", action="store_true", help="Stage 2: Vosk keywords on the mic")
    parser.add_argument("--download-vosk", action="store_true", help="download offline Chinese Vosk model")
    parser.add_argument("--say", action="append", default=[], help="inject a spoken phrase (repeatable)")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input index")
    parser.add_argument("--model", type=Path, default=None, help="path to vosk-model-small-cn-0.22")
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
    args = build_parser().parse_args(argv)
    if args.show_stage:
        print(f"{AGENT_STAGE} {AGENT_LABEL}")
        return 0
    if args.snapshot is not None:
        snapshot_current(args.snapshot)
        return 0
    print(f"local_main  stage {AGENT_STAGE}  ({AGENT_LABEL})")
    if args.download_vosk:
        download_vosk_model(args.model)
        return 0
    lamp = LocalLamp(
        sim=args.sim,
        port=args.port,
        lamp_id=args.lamp_id,
        led_count=args.led_count,
        brightness=70,
    )
    lamp.start()
    try:
        if not args.no_wake:
            lamp.wake()
        for phrase in args.say:
            if apply_speech(lamp, phrase) == "quit":
                return 0
        if args.say and not args.listen:
            return 0
        if args.listen:
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
