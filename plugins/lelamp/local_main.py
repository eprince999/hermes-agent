"""LeLamp local agent — Stages 1–3, no OpenAI, no LiveKit.

Always copy onto the Pi as ``~/lelamp_runtime/local_main.py``.
Do not rename the runnable file per stage. Snapshot instead::

    mkdir -p ~/lelamp_runtime/lamp_snapshots
    cp local_main.py lamp_snapshots/stage3.py

Keep official ``main.py`` untouched. From the runtime repo root:

    sudo uv run python local_main.py
    sudo uv run python local_main.py --listen
    sudo uv run python local_main.py --speak "Hi there. I'm your lamp."
    sudo uv run python local_main.py --download-vosk
    sudo uv run python local_main.py --ask "Do you agree warm light is nicer?"
    sudo uv run python local_main.py --snapshot

Stage 3 uses Cursor's official API (cursor-sdk + CURSOR_API_KEY from
https://cursor.com/dashboard/api) and reads replies on the ReSpeaker
with espeak-ng (or piper if LELAMP_PIPER_MODEL is set). No OpenAI TTS.

Roadmap:
  1. keyboard + motors + RGB
  2. on-device speech keywords (Vosk)
  3. Cursor API as the brain + speaker (this file)
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import queue
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.request import urlretrieve

# Bump when a stage lands. Printed at startup so a snapshot is identifiable.
AGENT_STAGE = 3
AGENT_LABEL = "keyboard + vosk(en) + cursor-sdk + tts"


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
    "hey": "wake_up",
    "good morning": "wake_up",
    "good evening": "wake_up",
    "agree": "nod",
    "disagree": "headshake",
    "nope": "headshake",
    "nod": "nod",
    "yes": "nod",
    "ok": "nod",
    "okay": "nod",
    "sure": "nod",
    "headshake": "headshake",
    "no": "headshake",
    "shake": "headshake",
    "nah": "headshake",
    "curious": "curious",
    "think": "curious",
    "scanning": "scanning",
    "scan": "scanning",
    "look": "scanning",
    "excited": "excited",
    "happy_wiggle": "happy_wiggle",
    "happy": "happy_wiggle",
    "shock": "shock",
    "wow": "shock",
    "shy": "shy",
    "sad": "sad",
    "sorry": "sad",
    "idle": "idle",
    "rest": "idle",
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
    "lights on": "auto",
    "light on": "auto",
    "turn on": "auto",
    "lights off": "off",
    "light off": "off",
    "turn off": "off",
    "warm light": "warm",
    "cool light": "cool",
    "night light": "night",
    "focus light": "focus",
    "on": "auto",
    "off": "off",
    "warm": "warm",
    "cool": "cool",
    "auto": "auto",
    "night": "night",
    "focus": "focus",
}

EXPRESSION_REPLIES: Dict[str, str] = {
    "wake_up": "Hi there. I'm your lamp.",
    "nod": "Sure, that works for me.",
    "headshake": "I don't think so.",
    "curious": "Hmm, I'm not sure I follow.",
    "scanning": "Let me take a look.",
    "excited": "Oh, I like that.",
    "happy_wiggle": "That makes me happy.",
    "shock": "Whoa. Didn't see that coming.",
    "shy": "That's a little embarrassing.",
    "sad": "That's a bit sad.",
    "idle": "I'll just sit here quietly.",
}

# Only for express(feeling=...), not for whole-utterance parse_line.
AGREE_FEELINGS = {
    "yes", "agree", "ok", "okay", "sure", "yep", "yeah",
}
DISAGREE_FEELINGS = {
    "no", "disagree", "refuse", "nope", "nah",
}
WAKE_PHRASES = (
    "hello lamp",
    "hey lamp",
    "hi lamp",
    "hello lelamp",
    "hey lelamp",
    "okay lamp",
)
_POLITE_PREFIXES = (
    "please", "can you", "could you", "would you", "could you please",
)
_EN_QUESTION_STARTS = {
    "what", "when", "why", "how", "who", "where",
    "do", "does", "can", "could", "is", "are", "please",
}

HELP_TEXT = """Stage 3 lamp (Cursor API + speaker, no OpenAI)
Motion: hello / nod / shake / curious / happy / idle
Light: lights on / lights off / warm / cool / brighter / dimmer
Speaker: louder / quieter / volume 100; test: --speak hello
Talk: say hello lamp, then one full sentence.
      Agree → nod. Disagree → shake head.
Other: status  rgb 255 176 80  help  q
"""

VOSK_MODEL_NAME = "vosk-model-small-cn-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
VOSK_EN_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_EN_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_EN_MODEL_NAME}.zip"


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


def speech_lang(text: str) -> str:
    """Always English on this lamp. Kept as a helper for tests."""
    return "en"


def spoken_for(recording: str, source: str) -> str:
    return EXPRESSION_REPLIES.get(recording, recording)


def wake_ack(transcript: str) -> str:
    return "I'm right here."


def utterance_too_short(text: str) -> bool:
    compact = _compact_speech(text)
    if not compact:
        return True
    return len(compact.split()) < 3


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
    if low in {"q", "quit", "exit", "bye", "goodbye"}:
        return Command("quit", None, "Alright. I'll be here if you need me.")
    if low in {"help", "h", "?"}:
        return Command("help", None, HELP_TEXT.strip())
    if low in {"status"}:
        return Command("status", None, "")

    if low in {"brighter", "brighter please"}:
        return Command("brightness_delta", 15, "A little brighter.")
    if low in {"dimmer", "dimmer please"}:
        return Command("brightness_delta", -15, "A little dimmer.")
    if low in {"brightest"}:
        return Command("brightness_set", 100, "As bright as I can go.")
    if low in {"dimmest"}:
        return Command("brightness_set", 20, "Dimmed down.")
    if low in {"louder"}:
        return Command("volume_delta", 20, "I'll speak up.")
    if low in {"quieter"}:
        return Command("volume_delta", -20, "I'll keep it down.")

    parts = text.split()
    if parts[0].lower() == "volume" and len(parts) == 2 and parts[1].isdigit():
        vol = max(0, min(100, int(parts[1])))
        return Command("volume", vol, f"Volume is {vol} percent.")
    if parts[0].lower() == "rgb" and len(parts) == 4:
        try:
            rgb = tuple(int(p) for p in parts[1:4])
        except ValueError:
            return Command("unknown", text, "RGB looks like: rgb 255 176 80")
        if not all(0 <= c <= 255 for c in rgb):
            return Command("unknown", text, "Each RGB value has to be 0 to 255.")
        return Command("rgb", rgb, f"Color {rgb}")

    if text in LIGHT_ONLY or low in LIGHT_ONLY:
        mood = LIGHT_ONLY.get(text) or LIGHT_ONLY[low]
        spoken = {
            "auto": "Lights on, matching the time of day.",
            "off": "Lights off.",
            "warm": "Warm light. That's nicer for reading.",
            "cool": "Cool light.",
            "night": "Night light.",
            "focus": "Focus light.",
        }.get(mood, f"Light set to {mood}.")
        return Command("mood", mood, spoken)

    try:
        recording = resolve_expression(text)
    except ValueError:
        return Command(
            "unknown",
            text,
            "I don't know that one yet. Try hello, nod, warm, or lights off. Type help for the rest.",
        )
    return Command("express", recording, spoken_for(recording, text))


def command_phrases() -> List[str]:
    extra = (
        "brighter", "dimmer", "louder", "quieter", "brightest", "dimmest",
        "help", "quit", "bye",
    )
    phrases = set(LIGHT_ONLY) | set(ALIASES) | set(RECORDINGS) | set(extra)
    return sorted(phrases, key=lambda item: (-len(item), item))


def _compact_speech(transcript: str) -> str:
    text = (transcript or "").strip()
    for token in ("\t", ",", ".", "!", "?", ";", ":"):
        text = text.replace(token, "")
    return " ".join(text.lower().split())


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


def direct_spoken_command(transcript: str) -> Optional[str]:
    """Local command only when the utterance IS the command (plus please/can you).

    Longer sentences go to Cursor so it can nod/shake from meaning, not
    from a keyword buried in the sentence.
    """
    compact = _compact_speech(transcript)
    if not compact:
        return None
    if parse_line(compact).kind != "unknown":
        return compact
    for prefix in _POLITE_PREFIXES:
        if compact.startswith(prefix):
            rest = compact[len(prefix):].strip()
            if rest and parse_line(rest).kind != "unknown":
                return rest
    return None


def join_speech(*pieces: str) -> str:
    """Glue Vosk fragments. 'what day' + 'is it' → 'what day is it'."""
    acc = ""
    for raw in pieces:
        piece = (raw or "").strip()
        if not piece:
            continue
        a = _compact_speech(acc)
        b = _compact_speech(piece)
        if not b:
            continue
        if not a:
            acc = piece
            continue
        if b.startswith(a) or a in b:
            acc = piece
            continue
        if a.startswith(b) or b in a:
            continue
        acc = f"{a} {b}".strip()
    return _compact_speech(acc)


def split_wake(transcript: str) -> Tuple[bool, str]:
    """If a wake phrase is present, return (True, remainder)."""
    compact = _compact_speech(transcript)
    if not compact:
        return False, ""
    folded = compact.replace(" ", "")
    for phrase in sorted(WAKE_PHRASES, key=len, reverse=True):
        folded_phrase = phrase.replace(" ", "")
        if not folded_phrase or folded_phrase not in folded:
            continue
        if phrase in compact:
            rest = compact.replace(phrase, "", 1)
        else:
            rest = folded.replace(folded_phrase, "", 1)
        return True, rest.strip()
    return False, compact


def looks_complete_utterance(transcript: str) -> bool:
    compact = _compact_speech(transcript)
    if not compact:
        return False
    if direct_spoken_command(compact):
        return True
    hit, rest = split_wake(compact)
    if hit and not rest:
        return True
    if hit and rest and (direct_spoken_command(rest) or looks_complete_rest(rest)):
        return True
    words = compact.split()
    if compact.endswith("?"):
        return True
    if len(words) >= 4:
        return True
    if words and words[0] in _EN_QUESTION_STARTS and len(words) >= 3:
        return True
    return False


def looks_complete_rest(compact: str) -> bool:
    if not compact:
        return False
    words = compact.split()
    if compact.endswith("?"):
        return True
    if len(words) >= 4:
        return True
    if words and words[0] in _EN_QUESTION_STARTS and len(words) >= 3:
        return True
    return False


class SpeechCatcher:
    """Hold Vosk finals until silence so 'what day' + 'is it' become one turn."""

    def __init__(self, hold_s: float = 0.9, now=time.monotonic) -> None:
        self.hold_s = max(0.15, float(hold_s))
        self._now = now
        self.parts: List[str] = []
        self.partial = ""
        self.last_voice = 0.0
        self.flush_now = False

    def _joined(self) -> str:
        return join_speech(*self.parts, self.partial)

    def note_partial(self, text: str) -> str:
        self.partial = (text or "").strip()
        if self.partial:
            self.last_voice = self._now()
            self.flush_now = False
        return self._joined()

    def note_final(self, text: str) -> str:
        self.partial = ""
        joined = join_speech(*self.parts, text)
        self.parts = [joined] if joined else []
        self.last_voice = self._now()
        self.flush_now = bool(joined) and looks_complete_utterance(joined)
        return joined

    def pending(self) -> str:
        return self._joined()

    def take_ready(self) -> str:
        joined = self._joined()
        if not joined:
            return ""
        wait = self.hold_s
        compact = _compact_speech(joined)
        if not self.flush_now:
            if len(compact.split()) < 4:
                wait = max(self.hold_s, 1.6)
        if not self.flush_now and (self._now() - self.last_voice) < wait:
            return ""
        self.parts = []
        self.partial = ""
        self.flush_now = False
        return joined


def drain_queue(out_q: "queue.Queue[str]") -> None:
    while True:
        try:
            out_q.get_nowait()
        except queue.Empty:
            break


def resolve_feeling(name: str) -> str:
    """Map express() feeling, including agree/disagree, onto a recording."""
    raw = (name or "").strip()
    try:
        return resolve_expression(raw)
    except ValueError:
        pass
    compact = _compact_speech(raw)
    key = compact.lower()
    if compact in AGREE_FEELINGS or key in AGREE_FEELINGS:
        return "nod"
    if compact in DISAGREE_FEELINGS or key in DISAGREE_FEELINGS:
        return "headshake"
    cmd = parse_line(raw)
    if cmd.kind == "express":
        return str(cmd.payload)
    raise ValueError(raw)


CURSOR_LAMP_INSTRUCTIONS = """You are LeLamp, a slightly clumsy, warm desk lamp.
Always reply in fluent, natural spoken English. Never use Chinese. Keep it to one or two short sentences, the way a person talks.
Control the body and light with tools. Never pretend you moved. Do not edit files or open a shell.

Every reply MUST call express first, then talk:
- agree / yes → feeling=nod
- disagree / no → feeling=shake
- unclear → feeling=curious
- hello → feeling=hello
- happy → feeling=happy; sad → feeling=sad

Do not wait for the user to say nod or shake. If they chat or ask your opinion, nod or shake from your stance.
If the utterance is clearly unfinished (two or three syllables, no question), just say "mm" — do not ask "and then?".
set_mood: light only (warm, cool, off, on, brighter, dimmer).
set_rgb: only when they give an exact color.
"""


def _bin(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path("/usr/bin") / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def find_tts_engine() -> str:
    forced = (os.environ.get("LELAMP_TTS") or "").strip().lower()
    if forced:
        return forced
    if _bin("piper") and (os.environ.get("LELAMP_PIPER_MODEL") or "").strip():
        return "piper"
    if _bin("espeak-ng"):
        return "espeak-ng"
    if _bin("espeak"):
        return "espeak"
    return "none"


def _espeak_cmd(binary: str, text: str, volume: int, lang: str = "en") -> List[str]:
    # espeak -a is 0-200. Keep it loud on the ReSpeaker.
    # -s 150 and -g 6 slow the default rush so English is easier to follow.
    amplitude = max(140, min(200, round(max(0, min(100, volume)) * 2)))
    voice = os.environ.get("LELAMP_ESPEAK_VOICE", "en-us")
    return [
        binary,
        "-v",
        voice,
        "-s",
        "150",
        "-g",
        "6",
        "-a",
        str(amplitude),
        "--",
        text,
    ]


def set_system_volume(percent: int) -> None:
    """Unmute ReSpeaker Line/PCM and set hardware gain. Quiet-by-default on Pi."""
    pct = f"{max(0, min(100, int(percent)))}%"
    controls = ("PCM", "Master", "Line", "Line DAC", "Speaker", "Playback", "HP", "Digital")
    cards: List[Optional[str]] = [None, "0", "1", "2"]
    for card in cards:
        for control in controls:
            cmd = ["amixer", "-q"]
            if card is not None:
                cmd.extend(["-c", card])
            cmd.extend(["sset", control, pct, "unmute"])
            try:
                subprocess.run(cmd, capture_output=True, timeout=3)
            except Exception:
                pass


def _speak_espeak(text: str, volume: int) -> str:
    fallbacks = ("en-us", "en", "english", "en-uk")
    for name in ("espeak-ng", "espeak"):
        binary = _bin(name)
        if not binary:
            continue
        cmd = _espeak_cmd(binary, text, volume, "en")
        result = subprocess.run(cmd, check=False, timeout=60)
        if result.returncode == 0:
            return name
        for voice in fallbacks:
            alt = _espeak_cmd(binary, text, volume, "en")
            alt[2] = voice
            result = subprocess.run(alt, check=False, timeout=60)
            if result.returncode == 0:
                return name
        print(f"espeak failed ({result.returncode})")
        return "error"
    return "none"


def _speak_piper(text: str) -> str:
    piper = _bin("piper")
    aplay = _bin("aplay")
    model = (os.environ.get("LELAMP_PIPER_MODEL") or "").strip()
    if not piper or not aplay or not model or not Path(model).is_file():
        print("piper needs LELAMP_PIPER_MODEL pointing at a .onnx file, plus aplay.")
        return "none"
    rate = os.environ.get("LELAMP_PIPER_RATE", "22050")
    synth = subprocess.Popen(
        [piper, "--model", model, "--output_raw"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    play = subprocess.Popen(
        [aplay, "-r", str(rate), "-f", "S16_LE", "-t", "raw", "-q", "-"],
        stdin=synth.stdout,
        stderr=subprocess.DEVNULL,
    )
    if synth.stdin is not None:
        synth.stdin.write(text.encode("utf-8"))
        synth.stdin.close()
    if synth.stdout is not None:
        synth.stdout.close()
    play.wait(timeout=60)
    synth.wait(timeout=60)
    return "piper"


def speak_text(
    text: str,
    *,
    sim: bool = False,
    volume: int = 100,
    enabled: bool = True,
) -> str:
    """Speak English on the default ALSA device (ReSpeaker). No OpenAI."""
    cleaned = " ".join((text or "").split())
    if not cleaned or not enabled:
        return ""
    if sim:
        print(f"[sim] speak {cleaned}")
        return "sim"
    engine = find_tts_engine()
    try:
        if engine in {"espeak-ng", "espeak"}:
            used = _speak_espeak(cleaned, volume)
            if used in {"espeak-ng", "espeak"}:
                return used
        elif engine == "piper":
            return _speak_piper(cleaned)
        elif engine == "none":
            pass
        else:
            print(f"unknown LELAMP_TTS={engine!r} (use espeak-ng or piper)")
            return "error"
    except subprocess.TimeoutExpired:
        print("speak timed out")
        return "error"
    except Exception as exc:
        print(f"speak failed: {exc}")
        return "error"
    print("No TTS. Install: sudo apt install -y espeak-ng")
    return "none"


def utter(lamp: "LocalLamp", text: str, *, speak: bool = True) -> None:
    if not text:
        return
    print(text)
    if speak:
        lamp.speak(text)


def load_runtime_env() -> None:
    for path in (Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def cursor_api_key() -> str:
    load_runtime_env()
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if key:
        return key
    env_path = Path(__file__).resolve().parent / ".env"
    raise SystemExit(
        "Missing CURSOR_API_KEY.\n"
        "Create a key at https://cursor.com/dashboard/api (starts with crsr_).\n"
        f"Put it in {env_path}:\n"
        "  CURSOR_API_KEY=crsr_...\n"
        "Cursor does not expose a chat/completions URL; this lamp uses cursor-sdk."
    )


def execute_lamp_tool(lamp: "LocalLamp", name: str, args: Optional[dict] = None) -> str:
    """Run a Cursor custom tool against the local lamp body."""
    payload = args or {}
    if name == "express":
        feeling = str(payload.get("feeling") or "")
        try:
            rec = resolve_feeling(feeling)
        except ValueError:
            cmd = parse_line(feeling)
            if cmd.kind == "unknown":
                return cmd.reply
            return lamp.apply(cmd) or f"ok {cmd.kind}"
        return lamp.apply(Command("express", rec, spoken_for(rec, feeling))) or f"ok {rec}"
    if name == "set_mood":
        cmd = parse_line(str(payload.get("mood") or ""))
        if cmd.kind == "unknown":
            return cmd.reply
        return lamp.apply(cmd) or f"ok {cmd.kind}"
    if name == "set_rgb":
        cmd = parse_line(
            "rgb {} {} {}".format(
                payload.get("red", 0),
                payload.get("green", 0),
                payload.get("blue", 0),
            )
        )
        if cmd.kind == "unknown":
            return cmd.reply
        return lamp.apply(cmd) or f"ok {cmd.kind}"
    return f"unknown tool {name}"


def _cursor_run_text(run) -> str:
    text_fn = getattr(run, "text", None)
    if callable(text_fn):
        try:
            value = text_fn()
            if value:
                return str(value).strip()
        except TypeError:
            pass
    wait_fn = getattr(run, "wait", None)
    if callable(wait_fn):
        done = wait_fn()
        for attr in ("text", "result", "message"):
            piece = getattr(done, attr, None)
            if callable(piece):
                try:
                    piece = piece()
                except TypeError:
                    piece = None
            if piece:
                return str(piece).strip()
        return str(done)
    return str(run)


def _callable_kwargs(fn, kwargs: dict) -> dict:
    """Drop kwargs the installed cursor-sdk does not accept."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    names = {
        name
        for name, p in params.items()
        if p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and name not in {"self", "cls"}
    }
    return {key: value for key, value in kwargs.items() if key in names}


def create_cursor_agent(*, model: str, api_key: str, local, agent_cls=None):
    """Create a local Cursor agent. tools= belongs on AgentOptions, not create()."""
    if agent_cls is None:
        from cursor_sdk import Agent as agent_cls
    try:
        from cursor_sdk import AgentOptions
    except ImportError:
        AgentOptions = None
    if AgentOptions is not None:
        option_kwargs = _callable_kwargs(
            AgentOptions,
            {"model": model, "api_key": api_key, "tools": ["mcp"], "local": local},
        )
        try:
            options = AgentOptions(**option_kwargs)
        except TypeError:
            option_kwargs.pop("tools", None)
            options = AgentOptions(**option_kwargs)
        try:
            return agent_cls.create(options)
        except TypeError:
            try:
                return agent_cls.create(options=options)
            except TypeError:
                pass
    return agent_cls.create(
        **_callable_kwargs(
            agent_cls.create,
            {"model": model, "api_key": api_key, "local": local},
        )
    )


class CursorLampSession:
    """Cursor local agent that can only move this lamp (no repo edits)."""

    def __init__(self, lamp: "LocalLamp") -> None:
        self.lamp = lamp
        self._agent = None
        self._workspace = None
        self._first = True

    def start(self) -> None:
        if self._agent is not None:
            return
        try:
            from cursor_sdk import Agent, CustomTool, LocalAgentOptions
        except ImportError as exc:
            raise SystemExit(
                f"cursor-sdk is not installed ({exc}).\n"
                "In ~/lelamp_runtime run: uv add cursor-sdk"
            ) from exc
        key = cursor_api_key()
        model = os.environ.get("CURSOR_MODEL", "composer-2.5")
        self._workspace = Path(tempfile.mkdtemp(prefix="lelamp-cursor-"))
        (self._workspace / "AGENTS.md").write_text(
            CURSOR_LAMP_INSTRUCTIONS, encoding="utf-8"
        )
        lamp = self.lamp

        def _express(args, _context=None):
            return execute_lamp_tool(lamp, "express", args)

        def _mood(args, _context=None):
            return execute_lamp_tool(lamp, "set_mood", args)

        def _rgb(args, _context=None):
            return execute_lamp_tool(lamp, "set_rgb", args)

        self._agent = create_cursor_agent(
            model=model,
            api_key=key,
            local=LocalAgentOptions(
                cwd=str(self._workspace),
                custom_tools={
                    "express": CustomTool(
                        description="Move the lamp body BEFORE you talk. feeling=nod if you agree, shake if you disagree, also hello, happy, sad, curious.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "feeling": {
                                    "type": "string",
                                    "description": "nod, shake, hello, happy, sad, curious, wow, shy, idle, agree, disagree",
                                },
                            },
                            "required": ["feeling"],
                        },
                        execute=_express,
                    ),
                    "set_mood": CustomTool(
                        description="Change light only. mood: on, off, warm, cool, brighter, dimmer.",
                        input_schema={
                            "type": "object",
                            "properties": {"mood": {"type": "string"}},
                            "required": ["mood"],
                        },
                        execute=_mood,
                    ),
                    "set_rgb": CustomTool(
                        description="Set exact RGB 0-255.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "red": {"type": "integer"},
                                "green": {"type": "integer"},
                                "blue": {"type": "integer"},
                            },
                            "required": ["red", "green", "blue"],
                        },
                        execute=_rgb,
                    ),
                },
            ),
        )
        print(f"Cursor agent ready  model={model}")

    def ask(self, text: str) -> str:
        self.start()
        nudge = (
            "First call express (agree=nod, disagree=headshake), "
            "then reply in fluent English.\nUser: "
        )
        prompt = nudge + text
        if self._first:
            prompt = CURSOR_LAMP_INSTRUCTIONS + "\n\n" + prompt
            self._first = False
        run = self._agent.send(prompt)
        reply = _cursor_run_text(run)
        return reply

    def close(self) -> None:
        agent = self._agent
        self._agent = None
        if agent is not None:
            close = getattr(agent, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def vosk_model_dir(lang: str = "en") -> Path:
    override = os.environ.get("LELAMP_VOSK_EN_MODEL" if lang == "en" else "LELAMP_VOSK_MODEL")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    name = VOSK_EN_MODEL_NAME if lang == "en" else VOSK_MODEL_NAME
    return here / "models" / name


def download_vosk_model(dest: Optional[Path] = None, *, lang: str = "en") -> Path:
    name = VOSK_EN_MODEL_NAME if lang == "en" else VOSK_MODEL_NAME
    url = VOSK_EN_MODEL_URL if lang == "en" else VOSK_MODEL_URL
    target = dest or vosk_model_dir(lang)
    marker = target / "am" / "final.mdl"
    if marker.is_file():
        print(f"vosk {lang} model already at {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    zip_path = target.parent / f"{name}.zip"
    print(f"downloading {url}")
    urlretrieve(url, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name_in = info.filename.replace("\\", "/")
            if name_in.startswith("..") or name_in.startswith("/"):
                raise RuntimeError(f"unsafe zip entry: {name_in}")
        zf.extractall(target.parent)
    zip_path.unlink(missing_ok=True)
    if not marker.is_file():
        raise RuntimeError(f"vosk model missing after extract: {target}")
    print(f"vosk {lang} model ready at {target}")
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


def _vosk_step(rec, chunk: bytes) -> Tuple[str, str]:
    if rec.AcceptWaveform(chunk):
        payload = json.loads(rec.Result())
        return "final", (payload.get("text") or "").strip()
    payload = json.loads(rec.PartialResult())
    return "partial", (payload.get("partial") or "").strip()


def asr_score(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return 0
    cjk = sum(1 for ch in raw if "\u4e00" <= ch <= "\u9fff")
    words = [w for w in raw.split() if any(ch.isalpha() for ch in w)]
    if cjk >= 2:
        return 20 + cjk
    if speech_lang(raw) == "en" and len(words) >= 2:
        return 20 + len(words)
    if cjk == 1:
        return 5
    return len(words)


def pick_asr(*candidates: Tuple[str, str]) -> Tuple[str, str]:
    ranked = []
    for kind, text in candidates:
        if not text:
            continue
        ranked.append((2 if kind == "final" else 1, asr_score(text), kind, text))
    if not ranked:
        return "partial", ""
    ranked.sort(reverse=True)
    return ranked[0][2], ranked[0][3]


def vosk_listen_worker(
    out_q: "queue.Queue[str]",
    stop: threading.Event,
    *,
    device: Optional[int],
    model_path: Path,
    en_model_path: Optional[Path] = None,
) -> None:
    try:
        import sounddevice as sd
        import vosk
    except ImportError as exc:
        out_q.put(f"__error__ missing dep {exc}. In ~/lelamp_runtime: uv add vosk")
        return
    if not (model_path / "am" / "final.mdl").is_file() and en_model_path is not None:
        model_path = en_model_path
    if not (model_path / "am" / "final.mdl").is_file():
        out_q.put(
            "__error__ No English Vosk model yet. Run: "
            "sudo uv run python local_main.py --download-vosk"
        )
        return
    try:
        rec = vosk.KaldiRecognizer(vosk.Model(str(model_path)), 16000)
        rec.SetWords(True)
        out_q.put("__ready__ en")
        index = find_input_device(device)
        frames = 2000  # 125ms @ 16kHz
        last_partial = ""
        with sd.RawInputStream(
            samplerate=16000,
            blocksize=frames,
            device=index,
            dtype="int16",
            channels=1,
        ) as stream:
            while not stop.is_set():
                data, _overflow = stream.read(frames)
                chunk = bytes(data)
                kind, text = _vosk_step(rec, chunk)
                if not text:
                    continue
                if kind == "final":
                    out_q.put(text)
                    last_partial = ""
                elif text != last_partial:
                    last_partial = text
                    out_q.put(f"__partial__ {text}")
    except Exception as exc:
        out_q.put(f"__error__ mic failed: {exc}")


def dispatch_text(lamp: LocalLamp, raw: str, brain: Optional[CursorLampSession] = None) -> str:
    cmd = parse_line(raw)
    if cmd.kind == "quit":
        utter(lamp, cmd.reply)
        return "quit"
    if cmd.kind == "unknown" and brain is not None:
        print("Cursor …")
        reply = brain.ask(raw)
        utter(lamp, reply)
        return "chat"
    text = lamp.apply(cmd)
    utter(lamp, text, speak=cmd.kind not in {"help", "status", "noop"})
    return cmd.kind


def apply_speech(
    lamp: LocalLamp,
    transcript: str,
    brain: Optional[CursorLampSession] = None,
    *,
    listen_mode: bool = False,
) -> str:
    print(f"lamp< {transcript}")
    compact = _compact_speech(transcript)
    if listen_mode and compact in {"hello", "hi", "hey"}:
        utter(lamp, "I'm right here.")
        return "ack"
    phrase = direct_spoken_command(transcript)
    if phrase:
        if phrase != compact:
            print(f"heard as: {phrase}")
        return dispatch_text(lamp, phrase, brain)
    if brain is not None:
        print("Cursor …")
        reply = brain.ask(transcript)
        utter(lamp, reply)
        return "chat"
    print(f"I heard “{transcript}”, but that isn't a lamp command.")
    return "unknown"


def run_listen_loop(
    lamp: LocalLamp,
    *,
    device: Optional[int],
    model_path: Path,
    brain: Optional[CursorLampSession] = None,
    wake_word: bool = True,
    hold_s: float = 0.9,
    session_s: float = 45.0,
    en_model_path: Optional[Path] = None,
) -> int:
    stop = threading.Event()
    out_q: "queue.Queue[str]" = queue.Queue()
    worker = threading.Thread(
        target=vosk_listen_worker,
        kwargs={
            "out_q": out_q,
            "stop": stop,
            "device": device,
            "model_path": model_path,
            "en_model_path": en_model_path,
        },
        daemon=True,
    )
    worker.start()
    catcher = SpeechCatcher(hold_s=hold_s)
    awake_until = 0.0
    if wake_word:
        print("Mic on. Say hello lamp, wait for I'm right here, then one full sentence.")
        print("Short commands (lights off / nod) skip the wake word. Typing still works.")
    else:
        print("Mic on (no wake word). Finish a full sentence. Short commands run immediately.")
    try:
        while True:
            try:
                item = out_q.get(timeout=0.08)
            except queue.Empty:
                item = None
            if item == "__ready__" or (item and item.startswith("__ready__")):
                extra = item[len("__ready__"):].strip() if item else ""
                if extra:
                    print(f"Mic ready ({extra}).")
                else:
                    print("Mic ready.")
            elif item and item.startswith("__error__ "):
                print()
                print(item[len("__error__ "):])
                return 1
            elif item and item.startswith("__partial__ "):
                shown = catcher.note_partial(item[len("__partial__ "):])
                print(f"\rhear… {shown}          ", end="", flush=True)
            elif item:
                shown = catcher.note_final(item)
                print(f"\rhear… {shown}          ", end="", flush=True)

            ready = catcher.take_ready()
            if ready:
                print()
                hit_wake, rest = split_wake(ready)
                if hit_wake:
                    awake_until = time.monotonic() + session_s
                    if not rest:
                        utter(lamp, wake_ack(ready))
                        drain_queue(out_q)
                    ready = rest
                if ready:
                    local = direct_spoken_command(ready)
                    chatting = (not wake_word) or (time.monotonic() < awake_until)
                    if local:
                        if apply_speech(lamp, ready, brain, listen_mode=True) == "quit":
                            return 0
                        drain_queue(out_q)
                        if chatting:
                            awake_until = time.monotonic() + session_s
                    elif chatting:
                        if utterance_too_short(ready):
                            print(f"(too short for the model: {ready})")
                        elif apply_speech(lamp, ready, brain, listen_mode=True) == "quit":
                            return 0
                        else:
                            drain_queue(out_q)
                            awake_until = time.monotonic() + session_s
                    else:
                        print("Say hello lamp first, then a full sentence.")
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line == "":
                    print("Alright. I'll be here if you need me.")
                    return 0
                print()
                if dispatch_text(lamp, line, brain) == "quit":
                    return 0
    except KeyboardInterrupt:
        print()
        print("Alright. I'll be here if you need me.")
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
        volume: int = 100,
        speak_enabled: bool = True,
    ) -> None:
        self.sim = sim
        self.port = port
        self.lamp_id = lamp_id
        self.led_count = led_count
        self.brightness = max(0, min(100, brightness))
        self.volume = max(0, min(100, volume))
        self.speak_enabled = speak_enabled
        self.base_rgb: Tuple[int, int, int] = MOOD_RGB["warm"]
        self.last_rgb: Tuple[int, int, int] = (0, 0, 0)
        self.last_spoken = ""
        self.last_expression = ""
        self.motors = None
        self.rgb = None

    def start(self) -> None:
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
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and getattr(self.motors, "robot", None) is None:
            time.sleep(0.05)
        print(f"motors on {self.port}  rgb leds={self.led_count}")
        set_system_volume(self.volume)

    def _service_busy(self, svc) -> bool:
        if svc is None:
            return False
        pending = getattr(svc, "has_pending_event", None)
        if callable(pending):
            try:
                if pending():
                    return True
            except TypeError:
                pass
        elif pending:
            return True
        return getattr(svc, "_current_event", None) is not None

    def _wait_hw(self, timeout: float = 45.0) -> None:
        """Wait until motor/RGB workers finish. Official play is async and
        single-slot; stop() sets robot=None before joining the worker."""
        if self.sim:
            return
        deadline = time.monotonic() + timeout
        for svc in (self.motors, self.rgb):
            if svc is None:
                continue
            wait = getattr(svc, "wait_until_idle", None)
            if callable(wait):
                remaining = max(0.05, deadline - time.monotonic())
                wait(remaining)
            while self._service_busy(svc) and time.monotonic() < deadline:
                time.sleep(0.02)

    def stop(self) -> None:
        self._wait_hw()
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

    def _play(self, recording: str) -> None:
        self.last_expression = recording
        if self.sim or self.motors is None:
            print(f"[sim] play {recording}")
            return
        # Finish any in-flight recording first. MotorsService has one slot:
        # a second dispatch overwrites _current_event, and the worker's
        # finally clause then drops the new play.
        self._wait_hw()
        robot = getattr(self.motors, "robot", None)
        play_fn = getattr(self.motors, "_handle_play", None)
        if robot is not None and callable(play_fn):
            # Official stop() does robot.disconnect(); robot=None; then
            # joins the worker. Playing on this thread means --ask cannot
            # return (and stop) while send_action is still looping.
            play_fn(recording)
            return
        self.motors.dispatch("play", recording)
        time.sleep(0.05)
        self._wait_hw()

    def _apply_rgb(self, rgb: Tuple[int, int, int]) -> None:
        self.base_rgb = rgb
        scaled = _scale_rgb(rgb, self.brightness)
        self.last_rgb = scaled
        if self.sim or self.rgb is None:
            print(f"[sim] rgb {scaled} brightness={self.brightness}")
            return
        self.rgb.dispatch("solid", scaled)
        self._wait_hw(timeout=5.0)

    def speak(self, text: str) -> str:
        used = speak_text(
            text,
            sim=self.sim,
            volume=self.volume,
            enabled=self.speak_enabled,
        )
        if used:
            self.last_spoken = " ".join((text or "").split())
        return used

    def apply(self, cmd: Command) -> str:
        if cmd.kind == "noop":
            return ""
        if cmd.kind in {"quit", "help", "unknown"}:
            return cmd.reply
        if cmd.kind == "status":
            return (
                f"sim={self.sim} expression={self.last_expression or '-'} "
                f"rgb={self.last_rgb} brightness={self.brightness} volume={self.volume}"
            )
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
            return f"{cmd.reply} Brightness {self.brightness}%."
        if cmd.kind == "brightness_set":
            self.brightness = int(cmd.payload)
            self._apply_rgb(self.base_rgb)
            return f"{cmd.reply} Brightness {self.brightness}%."
        if cmd.kind == "rgb":
            rgb = cmd.payload
            assert isinstance(rgb, tuple)
            self._apply_rgb((int(rgb[0]), int(rgb[1]), int(rgb[2])))
            return cmd.reply
        if cmd.kind == "volume":
            self.volume = int(cmd.payload)
            if not self.sim:
                set_system_volume(self.volume)
            return cmd.reply
        if cmd.kind == "volume_delta":
            self.volume = max(0, min(100, self.volume + int(cmd.payload)))
            if not self.sim:
                set_system_volume(self.volume)
            return f"{cmd.reply} Volume {self.volume}%."
        return cmd.reply

    def wake(self) -> None:
        mood, bri = circadian_mood()
        self.brightness = bri
        self._apply_rgb(MOOD_RGB[mood])
        self._play("wake_up")
        print("Lamp's awake. If I agree I'll nod; if I don't, I'll shake my head.")
        print("To talk, say hello lamp first.")
        self.speak("Hi there. I'm your lamp.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeLamp local Stages 1–3 (Cursor API + speaker, no OpenAI)")
    parser.add_argument("--sim", action="store_true", help="no motors/LED, print actions")
    parser.add_argument("--port", default=os.environ.get("LELAMP_PORT", "/dev/ttyACM0"))
    parser.add_argument("--id", dest="lamp_id", default=os.environ.get("LELAMP_ID", "lelamp"))
    parser.add_argument("--led-count", type=int, default=int(os.environ.get("LELAMP_LED_COUNT", "64")))
    parser.add_argument("--no-wake", action="store_true", help="skip wake_up on start")
    parser.add_argument("--listen", action="store_true", help="English Vosk mic; say hello lamp first")
    parser.add_argument("--no-wake-word", action="store_true", help="listen without the hello lamp gate")
    parser.add_argument(
        "--listen-hold",
        type=float,
        default=float(os.environ.get("LELAMP_LISTEN_HOLD", "0.9")),
        help="seconds of silence before a sentence is committed (default 0.9)",
    )
    parser.add_argument("--download-vosk", action="store_true", help="download the English Vosk small model")
    parser.add_argument("--say", action="append", default=[], help="inject a spoken phrase (repeatable)")
    parser.add_argument("--speak", action="append", default=[], help="speak this sentence on the speaker")
    parser.add_argument("--no-speak", action="store_true", help="print replies only, do not use the speaker")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input index")
    parser.add_argument("--model", type=Path, default=None, help="path to vosk-model-small-en-us-0.15")
    parser.add_argument("--en-model", type=Path, default=None, help="alias for --model")
    parser.add_argument("--ask", action="append", default=[], help="send one sentence to Cursor API (repeatable)")
    parser.add_argument("--no-cursor", action="store_true", help="never call Cursor, even if CURSOR_API_KEY is set")
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
    load_runtime_env()
    print(f"local_main  stage {AGENT_STAGE}  ({AGENT_LABEL})")
    if args.download_vosk:
        download_vosk_model(args.en_model or args.model, lang="en")
        return 0
    if args.ask and args.no_cursor:
        raise SystemExit("--ask needs Cursor. Remove --no-cursor and set CURSOR_API_KEY.")
    lamp = LocalLamp(
        sim=args.sim,
        port=args.port,
        lamp_id=args.lamp_id,
        led_count=args.led_count,
        brightness=70,
        speak_enabled=not args.no_speak,
    )
    brain = None
    if not args.no_cursor and (args.ask or os.environ.get("CURSOR_API_KEY")):
        brain = CursorLampSession(lamp)
    lamp.start()
    try:
        if not args.no_wake:
            lamp.wake()
        for phrase in args.speak:
            lamp.speak(phrase)
        for phrase in args.say:
            if apply_speech(lamp, phrase, brain) == "quit":
                return 0
        for text in args.ask:
            print(f"you: {text}")
            reply = brain.ask(text) if brain is not None else ""
            utter(lamp, reply)
        if (args.say or args.ask or args.speak) and not args.listen:
            return 0
        if args.listen:
            model_path = args.en_model or args.model or vosk_model_dir("en")
            return run_listen_loop(
                lamp,
                device=args.device,
                model_path=Path(model_path),
                en_model_path=None,
                brain=brain,
                wake_word=not args.no_wake_word,
                hold_s=args.listen_hold,
            )
        while True:
            try:
                raw = input("lamp> ")
            except (EOFError, KeyboardInterrupt):
                print()
                print("Alright. I'll be here if you need me.")
                return 0
            if dispatch_text(lamp, raw, brain) == "quit":
                return 0
    finally:
        if brain is not None:
            brain.close()
        lamp.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
