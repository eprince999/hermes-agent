"""LeLamp local agent — Stages 1–3, no OpenAI, no LiveKit.

Always copy onto the Pi as ``~/lelamp_runtime/local_main.py``.
Do not rename the runnable file per stage. Snapshot instead::

    mkdir -p ~/lelamp_runtime/lamp_snapshots
    cp local_main.py lamp_snapshots/stage3.py

Keep official ``main.py`` untouched. From the runtime repo root:

    sudo uv run python local_main.py
    sudo uv run python local_main.py --listen
    sudo uv run python local_main.py --download-vosk
    sudo uv run python local_main.py --snap
    sudo uv run python local_main.py --snapshot

Stage 3: Vosk hears English. Desk commands (lights, brightness, study/reading,
closer) run locally. Other talk is sent to Cursor with a coin-flip; if it
fires, it may play one official recording. No spoken replies.

Roadmap:
  1. keyboard + motors + RGB
  2. on-device speech keywords (Vosk)
  3. Vosk + silent pose (this file)
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import queue
import random
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
from urllib.request import Request, urlopen, urlretrieve

# Bump when a stage lands. Printed at startup so a snapshot is identifiable.
AGENT_STAGE = 3
AGENT_LABEL = "keyboard + vosk(en) + desk + coin-flip pose"

# Chance that leftover talk is sent to Cursor for an official recording.
POSE_CHANCE = 0.5


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
    "yeah": "nod",
    "yep": "nod",
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
    "study": (255, 255, 255),
    "read": (255, 196, 70),
    "white": (255, 255, 255),
    "yellow": (255, 210, 40),
    "off": (0, 0, 0),
}

# Desk poses in the same units as official recordings (about -100..100).
# Yaw stays facing the desk; pitch folds the neck toward a book.
POSES: Dict[str, Dict[str, float]] = {
    "study": {
        "base_yaw.pos": 0.0,
        "base_pitch.pos": -40.0,
        "elbow_pitch.pos": 70.0,
        "wrist_roll.pos": 0.0,
        "wrist_pitch.pos": 18.0,
    },
    "read": {
        "base_yaw.pos": 0.0,
        "base_pitch.pos": -52.0,
        "elbow_pitch.pos": 48.0,
        "wrist_roll.pos": 0.0,
        "wrist_pitch.pos": 42.0,
    },
}

_POSE_LIMITS: Dict[str, Tuple[float, float]] = {
    "base_yaw.pos": (-90.0, 90.0),
    "base_pitch.pos": (-70.0, -15.0),
    "elbow_pitch.pos": (25.0, 90.0),
    "wrist_roll.pos": (-30.0, 30.0),
    "wrist_pitch.pos": (0.0, 70.0),
}


def _clamp_pose(pose: Dict[str, float]) -> Dict[str, float]:
    out = dict(pose)
    for key, (lo, hi) in _POSE_LIMITS.items():
        if key in out:
            out[key] = max(lo, min(hi, float(out[key])))
    return out


def _closer_pose(present: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Nudge the head down toward a book. Safe to say more than once."""
    base = dict(present) if present else dict(POSES["read"])
    base["base_pitch.pos"] = float(base.get("base_pitch.pos", -52.0)) - 8.0
    base["elbow_pitch.pos"] = float(base.get("elbow_pitch.pos", 48.0)) - 8.0
    base["wrist_pitch.pos"] = float(base.get("wrist_pitch.pos", 42.0)) + 8.0
    return _clamp_pose(base)

LIGHT_ONLY: Dict[str, str] = {
    "lights on": "auto",
    "light on": "auto",
    "turn on": "auto",
    "turn on the light": "auto",
    "turn on the lights": "auto",
    "switch on": "auto",
    "开灯": "auto",
    "lights off": "off",
    "light off": "off",
    "turn off": "off",
    "turn off the light": "off",
    "turn off the lights": "off",
    "switch off": "off",
    "关灯": "off",
    "warm light": "warm",
    "cool light": "cool",
    "night light": "night",
    "focus light": "focus",
    "white light": "white",
    "yellow light": "yellow",
    "on": "auto",
    "off": "off",
    "warm": "warm",
    "cool": "cool",
    "auto": "auto",
    "night": "night",
    "focus": "focus",
    "white": "white",
    "yellow": "yellow",
    "白光": "white",
    "黄光": "yellow",
    "暖光": "warm",
    "冷光": "cool",
}

# Light + pose scenes. Longer phrases first in parse_line.
SCENE_PHRASES: Dict[str, Dict[str, object]] = {
    "study mode": {"mood": "study", "brightness": 100, "pose": "study"},
    "reading mode": {"mood": "read", "brightness": 80, "pose": "read"},
    "read mode": {"mood": "read", "brightness": 80, "pose": "read"},
    "learning mode": {"mood": "study", "brightness": 100, "pose": "study"},
    "study": {"mood": "study", "brightness": 100, "pose": "study"},
    "reading": {"mood": "read", "brightness": 80, "pose": "read"},
    "学习模式": {"mood": "study", "brightness": 100, "pose": "study"},
    "阅读模式": {"mood": "read", "brightness": 80, "pose": "read"},
    "学习": {"mood": "study", "brightness": 100, "pose": "study"},
    "阅读": {"mood": "read", "brightness": 80, "pose": "read"},
    "closer to the book": {"pose": "closer"},
    "closer to the page": {"pose": "closer"},
    "closer please": {"pose": "closer"},
    "lean down": {"pose": "closer"},
    "look down": {"pose": "closer"},
    "head down": {"pose": "closer"},
    "lower please": {"pose": "closer"},
    "come closer": {"pose": "closer"},
    "closer": {"pose": "closer"},
    "lower": {"pose": "closer"},
    "低头": {"pose": "closer"},
    "靠近": {"pose": "closer"},
    "近一点": {"pose": "closer"},
    "离书更近": {"pose": "closer"},
    "离书近一点": {"pose": "closer"},
}

BRIGHTNESS_UP = {
    "brighter", "brighter please", "more light", "brightness up",
    "turn up", "increase brightness", "a bit brighter", "too dark",
    "亮一点", "亮一些", "亮一点吧", "太暗了", "更亮",
}
BRIGHTNESS_DOWN = {
    "dimmer", "dimmer please", "less light", "brightness down",
    "turn down", "decrease brightness", "a bit dimmer", "too bright", "dim",
    "暗一点", "暗一些", "暗一点吧", "太亮了", "更暗",
}

EXPRESSION_REPLIES: Dict[str, str] = {
    "wake_up": "Hey. I'm here with you.",
    "nod": "Yeah.",
    "headshake": "I'd rather not.",
    "curious": "Tell me a bit more?",
    "scanning": "I'm looking.",
    "excited": "Oh, that's nice.",
    "happy_wiggle": "That makes me happy.",
    "shock": "Oh. I didn't expect that.",
    "shy": "A little shy, if I'm honest.",
    "sad": "I'm sorry. That sounds hard.",
    "idle": "I'll stay right here.",
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

HELP_TEXT = """Stage 3 lamp (voice control, silent)
Light: lights on / off   brighter / dimmer   white / yellow
Modes: study mode (white)   reading mode (yellow, lean to the book)
Pose: closer / look down / lean down
Chat: sometimes a recording (nod, shake, curious, …)
Camera: look / snap
Other: status  help  q
No spoken replies.
"""

VOSK_MODEL_NAME = "vosk-model-small-cn-0.22"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
VOSK_EN_MODEL_NAME = "vosk-model-small-en-us-0.15"
VOSK_EN_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_EN_MODEL_NAME}.zip"
PIPER_VOICE = "en_US-ryan-medium"
PIPER_ONNX_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
    "en/en_US/ryan/medium/en_US-ryan-medium.onnx"
)
PIPER_JSON_URL = PIPER_ONNX_URL + ".json"


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
    return "Yeah?"


def utterance_too_short(text: str) -> bool:
    compact = _compact_speech(text)
    if not compact:
        return True
    if direct_spoken_command(compact):
        return False
    return len(compact.split()) < 2


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

    low = _compact_speech(text)
    if low.endswith(" please"):
        low = low[: -len(" please")].strip()
    if not low:
        return Command("noop", None, "")

    if low in {"q", "quit", "exit", "bye", "goodbye"}:
        return Command("quit", None, "Okay. I'll be right here.")
    if low in {"help", "h", "?"}:
        return Command("help", None, HELP_TEXT.strip())
    if low in {"status"}:
        return Command("status", None, "")

    if low in BRIGHTNESS_UP or low in {"brighter please"}:
        return Command("brightness_delta", 15, "A little brighter.")
    if low in BRIGHTNESS_DOWN or low in {"dimmer please"}:
        return Command("brightness_delta", -15, "A little dimmer.")
    if low in {"brightest", "最亮"}:
        return Command("brightness_set", 100, "As bright as I can go.")
    if low in {"dimmest", "最暗"}:
        return Command("brightness_set", 20, "Dimmed down.")
    if low in {"louder"}:
        return Command("volume_delta", 20, "I'll speak up.")
    if low in {"quieter"}:
        return Command("volume_delta", -20, "I'll keep it down.")

    parts = low.split()
    if parts[0] == "volume" and len(parts) == 2 and parts[1].isdigit():
        vol = max(0, min(100, int(parts[1])))
        return Command("volume", vol, f"Volume is {vol} percent.")
    if parts[0] == "brightness" and len(parts) == 2 and parts[1].isdigit():
        bri = max(0, min(100, int(parts[1])))
        return Command("brightness_set", bri, f"Brightness {bri} percent.")
    if parts[0] == "rgb" and len(parts) == 4:
        try:
            rgb = tuple(int(p) for p in parts[1:4])
        except ValueError:
            return Command("unknown", text, "RGB looks like: rgb 255 176 80")
        if not all(0 <= c <= 255 for c in rgb):
            return Command("unknown", text, "Each RGB value has to be 0 to 255.")
        return Command("rgb", rgb, f"Color {rgb}")

    for phrase, payload in sorted(SCENE_PHRASES.items(), key=lambda item: -len(item[0])):
        if low == phrase:
            return Command("scene", dict(payload), "")

    if low in {"snap", "photo", "take a photo", "take a picture"} or looks_like_look(low):
        return Command("snap", None, "I'm looking.")

    if low in LIGHT_ONLY:
        mood = LIGHT_ONLY[low]
        spoken = {
            "auto": "Lights on, matching the time of day.",
            "off": "Lights off.",
            "warm": "Warm light. That's nicer for reading.",
            "cool": "Cool light.",
            "night": "Night light.",
            "focus": "Focus light.",
            "study": "Study light.",
            "read": "Reading light.",
            "white": "White light.",
            "yellow": "Yellow light.",
        }.get(mood, f"Light set to {mood}.")
        return Command("mood", mood, spoken)

    try:
        recording = resolve_expression(low)
    except ValueError:
        return Command(
            "unknown",
            text,
            "Try lights on, study mode, reading mode, yellow, brighter, or closer.",
        )
    return Command("express", recording, spoken_for(recording, text))


def command_phrases() -> List[str]:
    extra = (
        "brighter", "dimmer", "louder", "quieter", "brightest", "dimmest",
        "help", "quit", "bye",
    )
    phrases = (
        set(LIGHT_ONLY)
        | set(ALIASES)
        | set(RECORDINGS)
        | set(SCENE_PHRASES)
        | set(BRIGHTNESS_UP)
        | set(BRIGHTNESS_DOWN)
        | set(extra)
    )
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


_HARDWARE_KINDS = {
    "mood", "brightness_delta", "brightness_set",
    "volume", "volume_delta", "rgb", "quit", "help", "status",
    "scene",
}
_CHAT_FILLERS = {"uh", "um", "ah", "er", "mm", "hmm", "mhm"}


def hardware_spoken_command(transcript: str) -> Optional[str]:
    """Lights, brightness, study/reading, closer — not chat poses."""
    compact = _compact_speech(transcript)
    if not compact:
        return None
    cmd = parse_line(compact)
    if cmd.kind in _HARDWARE_KINDS:
        return compact
    for prefix in _POLITE_PREFIXES:
        if compact.startswith(prefix):
            rest = compact[len(prefix):].strip()
            if rest and parse_line(rest).kind in _HARDWARE_KINDS:
                return rest
    return None


def looks_like_look(text: str) -> bool:
    compact = _compact_speech(text)
    if not compact:
        return False
    if compact in {"look", "see", "snap", "photo", "picture", "camera"}:
        return True
    needles = (
        "look at", "take a look", "what do you see", "what can you see",
        "can you see", "do you see", "who is there", "whats in front",
        "in front of you", "take a photo", "take a picture",
    )
    return any(n in compact for n in needles)


def direct_spoken_command(transcript: str) -> Optional[str]:
    """Local command only when the utterance IS the command (plus please/can you).

    With a model, poses are not taken from a keyword buried in a sentence.
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


_TRAILING_INCOMPLETE = {
    "a", "an", "the", "to", "for", "and", "or", "but", "if", "is", "are",
    "was", "were", "i", "my", "your", "of", "in", "on", "at", "with",
    "can", "could", "would", "will",
}
_SHORT_COMPLETE = {
    "thank you", "thanks", "good night", "good morning", "good evening",
    "how are you", "you good", "what's up", "whats up", "you okay",
}


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
    return looks_complete_rest(compact)


def looks_complete_rest(compact: str) -> bool:
    if not compact:
        return False
    words = compact.split()
    if compact in _SHORT_COMPLETE:
        return True
    if words[-1] in _TRAILING_INCOMPLETE:
        return False
    if compact.endswith("?"):
        return True
    if len(words) >= 4:
        return True
    if words[0] in _EN_QUESTION_STARTS and len(words) >= 3:
        return True
    return False


class SpeechCatcher:
    """Hold Vosk finals until silence so 'what day' + 'is it' become one turn."""

    def __init__(self, hold_s: float = 0.45, now=time.monotonic) -> None:
        self.hold_s = max(0.12, float(hold_s))
        self._now = now
        self.parts: List[str] = []
        self.partial = ""
        self.last_voice = 0.0
        self.flush_now = False

    def _joined(self) -> str:
        return join_speech(*self.parts, self.partial)

    def clear(self) -> None:
        self.parts = []
        self.partial = ""
        self.flush_now = False

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
        compact = _compact_speech(joined)
        if self.flush_now:
            wait = 0.0
        elif len(compact.split()) < 4:
            wait = max(self.hold_s, 0.75)
        else:
            wait = self.hold_s
        if (self._now() - self.last_voice) < wait:
            return ""
        self.clear()
        return joined


def drain_queue(out_q: "queue.Queue[str]") -> None:
    while True:
        try:
            out_q.get_nowait()
        except queue.Empty:
            break


def _listen_settle(catcher: SpeechCatcher, out_q: "queue.Queue[str]") -> None:
    """Drop speaker echo so the next turn starts clean."""
    time.sleep(0.08)
    drain_queue(out_q)
    catcher.clear()


def resolve_feeling(name: str) -> str:
    """Map express() feeling, including agree/disagree, onto a recording."""
    raw = (name or "").strip()
    try:
        rec = resolve_expression(raw)
    except ValueError:
        rec = ""
        compact = _compact_speech(raw)
        key = compact.lower()
        if compact in AGREE_FEELINGS or key in AGREE_FEELINGS:
            rec = "nod"
        elif compact in DISAGREE_FEELINGS or key in DISAGREE_FEELINGS:
            rec = "headshake"
        else:
            cmd = parse_line(raw)
            if cmd.kind == "express":
                rec = str(cmd.payload)
    if rec in RECORDINGS:
        return rec
    raise ValueError(raw)


def should_pose_from_chat(*, chance: float = POSE_CHANCE, rng=None) -> bool:
    """Coin-flip: leftover talk is sent to Cursor this often."""
    roll = random.random() if rng is None else float(rng())
    return roll < max(0.0, min(1.0, float(chance)))


CURSOR_LAMP_INSTRUCTIONS = """Silent lamp. Do not speak. Do not write a reply.
Do not change lights. Desk modes are handled locally.
If you react, call express with ONE official recording:
nod, headshake, curious, scanning, excited, happy_wiggle, shock, shy, sad, idle, wake_up.
If the feeling is unclear, do nothing.
"""


def _bin(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    candidate = Path("/usr/bin") / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def piper_models_dir() -> Path:
    override = (os.environ.get("LELAMP_PIPER_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "models"


def piper_model_path() -> Optional[Path]:
    env = (os.environ.get("LELAMP_PIPER_MODEL") or "").strip()
    if env:
        path = Path(env)
        return path if path.is_file() else None
    onnx = piper_models_dir() / f"{PIPER_VOICE}.onnx"
    return onnx if onnx.is_file() else None


def _piper_can_synth() -> bool:
    if _bin("piper"):
        return True
    try:
        import piper  # noqa: F401
        return True
    except Exception:
        return False


def _piper_cli() -> Optional[List[str]]:
    found = _bin("piper")
    if found:
        return [found]
    try:
        import piper  # noqa: F401
    except Exception:
        return None
    return [sys.executable, "-m", "piper"]


def _fetch_url(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "lelamp-local-main/3"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urlopen(req, timeout=180) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def download_piper_voice(dest: Optional[Path] = None) -> Path:
    """Fetch the companion English voice (Ryan). Not a celebrity clone."""
    target = dest or (piper_models_dir() / f"{PIPER_VOICE}.onnx")
    json_path = Path(str(target) + ".json")
    if target.is_file() and target.stat().st_size > 1_000_000 and json_path.is_file():
        print(f"piper voice already at {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {PIPER_ONNX_URL}")
    _fetch_url(PIPER_ONNX_URL, target)
    print(f"downloading {PIPER_JSON_URL}")
    _fetch_url(PIPER_JSON_URL, json_path)
    if not target.is_file() or target.stat().st_size < 1_000_000:
        raise RuntimeError(f"piper model missing or too small: {target}")
    if not json_path.is_file():
        raise RuntimeError(f"piper voice json missing: {json_path}")
    print(f"piper voice ready at {target}")
    return target


def find_tts_engine() -> str:
    forced = (os.environ.get("LELAMP_TTS") or "").strip().lower()
    if forced:
        return forced
    if piper_model_path() is not None and _piper_can_synth():
        return "piper"
    if _bin("espeak-ng"):
        return "espeak-ng"
    if _bin("espeak"):
        return "espeak"
    return "none"


def describe_tts() -> str:
    engine = find_tts_engine()
    if engine == "piper":
        return f"tts=piper  {PIPER_VOICE} (companion)"
    if engine in {"espeak-ng", "espeak"}:
        if piper_model_path() is None:
            return (
                "tts=espeak  robotic fallback; human voice: "
                "uv add piper-tts && sudo uv run python local_main.py --download-piper"
            )
        return "tts=espeak  piper model is present but piper-tts is not installed (uv add piper-tts)"
    return "tts=none  install piper-tts or espeak-ng"


def camera_bin() -> Optional[str]:
    for name in ("rpicam-still", "libcamera-still", "fswebcam"):
        if _bin(name):
            return name
    return None


def describe_camera(*, sim: bool = False) -> str:
    if sim:
        return "camera=sim  stills only, on demand"
    name = camera_bin()
    if name:
        return f"camera={name}  stills only, on demand (not a live stream)"
    return "camera=none  install rpicam-still or fswebcam; look/snap is skipped"


def capture_still(dest: Optional[Path] = None, *, sim: bool = False) -> Optional[Path]:
    """One JPEG, then close the camera. Live video would starve listen/speak."""
    target = dest or (Path(tempfile.gettempdir()) / "lelamp-see.jpg")
    target.parent.mkdir(parents=True, exist_ok=True)
    if sim:
        target.write_bytes(b"sim-camera")
        print(f"[sim] camera snap {target}")
        return target
    cmds: List[List[str]] = []
    if _bin("rpicam-still"):
        cmds.append(
            ["rpicam-still", "-n", "-t", "400", "--width", "1280", "--height", "720", "-o", str(target)]
        )
    if _bin("libcamera-still"):
        cmds.append(
            ["libcamera-still", "-n", "-t", "400", "--width", "1280", "--height", "720", "-o", str(target)]
        )
    if _bin("fswebcam"):
        cmds.append(["fswebcam", "-r", "1280x720", "--no-banner", "-q", str(target)])
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=12, check=False)
        except Exception:
            continue
        if result.returncode == 0 and target.is_file() and target.stat().st_size > 1000:
            print(f"camera snap {target} ({target.stat().st_size} bytes)")
            return target
    index = os.environ.get("LELAMP_CAMERA", "0")
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(int(index) if str(index).isdigit() else index)
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            ok, frame = cap.read()
        finally:
            cap.release()
        if ok and frame is not None:
            cv2.imwrite(str(target), frame)
            if target.is_file() and target.stat().st_size > 1000:
                print(f"camera snap {target} ({target.stat().st_size} bytes)")
                return target
    except Exception:
        pass
    print("No camera still. On a Pi Camera: sudo apt install -y rpicam-apps")
    return None


def _espeak_cmd(binary: str, text: str, volume: int, lang: str = "en") -> List[str]:
    # Default male American English. -s 150 / -g 6 keep it from rushing.
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


_PIPER_VOICE_OBJ = None
_MIC_MUTE = threading.Event()
PIPER_LENGTH_SCALE = 1.0


def _piper_synth_config():
    try:
        from piper import SynthesisConfig

        return SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)
    except Exception:
        return None


def _piper_chunk_bytes(chunk) -> bytes:
    for attr in ("audio_int16_bytes", "audio_bytes"):
        val = getattr(chunk, attr, None)
        if callable(val):
            try:
                val = val()
            except TypeError:
                val = None
        if val:
            return bytes(val)
    audio = getattr(chunk, "audio_float_array", None)
    if audio is None:
        if isinstance(chunk, (bytes, bytearray)):
            return bytes(chunk)
        return b""
    clipped = [max(-1.0, min(1.0, float(s))) for s in audio]
    return b"".join(int(s * 32767).to_bytes(2, "little", signed=True) for s in clipped)


def _piper_stream_play(text: str, model: Path) -> bool:
    global _PIPER_VOICE_OBJ
    try:
        from piper import PiperVoice
    except Exception:
        return False
    try:
        if _PIPER_VOICE_OBJ is None:
            _PIPER_VOICE_OBJ = PiperVoice.load(str(model))
        voice = _PIPER_VOICE_OBJ
    except Exception as exc:
        print(f"piper load failed: {exc}")
        return False
    synthesize = getattr(voice, "synthesize", None)
    if not callable(synthesize):
        return False
    aplay = _bin("aplay")
    if not aplay:
        return False
    sample_rate = getattr(getattr(voice, "config", None), "sample_rate", None) or 22050
    play = subprocess.Popen(
        [aplay, "-q", "-r", str(int(sample_rate)), "-f", "S16_LE", "-t", "raw", "-c", "1"],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        syn_config = _piper_synth_config()
        try:
            chunks = synthesize(text, syn_config=syn_config) if syn_config is not None else synthesize(text)
        except TypeError:
            chunks = synthesize(text)
        if play.stdin is None:
            play.kill()
            return False
        for chunk in chunks:
            raw = _piper_chunk_bytes(chunk)
            if raw:
                play.stdin.write(raw)
        play.stdin.close()
        return play.wait(timeout=60) == 0
    except Exception as exc:
        print(f"piper stream failed: {exc}")
        try:
            play.kill()
        except Exception:
            pass
        return False


def _piper_synth_python(text: str, model: Path, wav_path: str) -> bool:
    global _PIPER_VOICE_OBJ
    try:
        from piper import PiperVoice
    except Exception:
        return False
    try:
        if _PIPER_VOICE_OBJ is None:
            _PIPER_VOICE_OBJ = PiperVoice.load(str(model))
        voice = _PIPER_VOICE_OBJ
        import wave

        syn_config = _piper_synth_config()
        with wave.open(wav_path, "wb") as wav_file:
            if syn_config is not None:
                voice.synthesize_wav(text, wav_file, syn_config=syn_config)
            else:
                voice.synthesize_wav(text, wav_file)
        return Path(wav_path).is_file() and Path(wav_path).stat().st_size > 44
    except Exception as exc:
        print(f"piper python synth failed: {exc}")
        return False


def _piper_synth_cli(text: str, model: Path, wav_path: str) -> bool:
    argv = _piper_cli()
    if not argv:
        return False
    result = subprocess.run(
        argv + ["--model", str(model), "--output_file", wav_path],
        input=(text + "\n").encode("utf-8"),
        timeout=90,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0 and Path(wav_path).is_file() and Path(wav_path).stat().st_size > 44


def _play_wav(path: str) -> bool:
    for name, extra in (("aplay", ["-q"]), ("paplay", []), ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"])):
        binary = _bin(name)
        if not binary:
            continue
        result = subprocess.run([binary, *extra, path], check=False, timeout=60, capture_output=True)
        if result.returncode == 0:
            return True
    return False


def _speak_piper(text: str) -> str:
    model = piper_model_path()
    if model is None:
        print("piper model missing. Run: sudo uv run python local_main.py --download-piper")
        return "none"
    if _piper_stream_play(text, model):
        return "piper"
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if not _piper_synth_python(text, model, wav_path) and not _piper_synth_cli(text, model, wav_path):
            print("piper synth failed. Try: uv add piper-tts")
            return "none"
        if not _play_wav(wav_path):
            print("could not play wav (need aplay)")
            return "none"
        return "piper"
    finally:
        Path(wav_path).unlink(missing_ok=True)


def warm_tts() -> None:
    """Load Piper once so the first reply does not stall."""
    model = piper_model_path()
    if model is None or not _piper_can_synth():
        return
    global _PIPER_VOICE_OBJ
    if _PIPER_VOICE_OBJ is not None:
        return
    try:
        from piper import PiperVoice

        print("loading companion voice…")
        _PIPER_VOICE_OBJ = PiperVoice.load(str(model))
        print("voice ready")
    except Exception as exc:
        print(f"voice warm failed: {exc}")


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
    _MIC_MUTE.set()
    try:
        engine = find_tts_engine()
        try:
            if engine == "piper":
                used = _speak_piper(cleaned)
                if used == "piper":
                    return used
                engine = "espeak-ng" if _bin("espeak-ng") else ("espeak" if _bin("espeak") else "none")
            if engine in {"espeak-ng", "espeak"}:
                used = _speak_espeak(cleaned, volume)
                if used in {"espeak-ng", "espeak"}:
                    return used
            elif engine == "none":
                pass
            else:
                print(f"unknown LELAMP_TTS={engine!r} (use piper or espeak-ng)")
                return "error"
        except subprocess.TimeoutExpired:
            print("speak timed out")
            return "error"
        except Exception as exc:
            print(f"speak failed: {exc}")
            return "error"
        print(
            "No TTS. For a human voice: uv add piper-tts && "
            "sudo uv run python local_main.py --download-piper"
        )
        return "none"
    finally:
        _MIC_MUTE.clear()


def utter(lamp: "LocalLamp", text: str, *, speak: bool = False) -> None:
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
            return "unknown pose. use: " + ", ".join(RECORDINGS)
        return lamp.apply(Command("express", rec, ""), wait_motion=False) or f"ok {rec}"
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
    if name == "look":
        path = lamp.snap()
        if path is None:
            return "No camera still."
        return f"Photo saved at {path}."
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

        def _look(args, _context=None):
            path = lamp.snap()
            if path is None:
                return "No camera still."
            dest = path
            if self._workspace:
                dest = self._workspace / "what_i_see.jpg"
                try:
                    shutil.copy2(path, dest)
                except OSError:
                    dest = path
            return f"Photo saved at {dest}."

        self._agent = create_cursor_agent(
            model=model,
            api_key=key,
            local=LocalAgentOptions(
                cwd=str(self._workspace),
                custom_tools={
                    "express": CustomTool(
                        description=(
                            "Play one official recording. feeling must be one of: "
                            + ", ".join(RECORDINGS)
                            + ". Skip if unsure. Never speak."
                        ),
                        input_schema={
                            "type": "object",
                            "properties": {
                                "feeling": {
                                    "type": "string",
                                    "description": ", ".join(RECORDINGS) + ", agree, disagree",
                                },
                            },
                            "required": ["feeling"],
                        },
                        execute=_express,
                    ),
                    "set_mood": CustomTool(
                        description="Desk light or scene. mood: on, off, warm, cool, white, yellow, study mode, reading mode, brighter, dimmer, closer.",
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
                    "look": CustomTool(
                        description="Take one still and play scanning. Do not describe the photo. Do not stream video.",
                        input_schema={"type": "object", "properties": {}},
                        execute=_look,
                    ),
                },
            ),
        )
        print(f"Cursor agent ready  model={model}")

    def ask(self, text: str, *, photo: Optional[Path] = None) -> str:
        self.start()
        run = self._agent.send((text or "").strip())
        return _cursor_run_text(run)

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
        text = (payload.get("text") or "").strip()
        if _vosk_too_noisy(payload, text):
            return "final", ""
        return "final", text
    payload = json.loads(rec.PartialResult())
    return "partial", (payload.get("partial") or "").strip()


def _vosk_too_noisy(payload: dict, text: str) -> bool:
    """Drop low-confidence crumbs that are usually fan/echo, not speech."""
    words = payload.get("result") or []
    if not text or not isinstance(words, list) or not words:
        return False
    confs = [float(w.get("conf", 1.0)) for w in words if isinstance(w, dict)]
    if not confs:
        return False
    avg = sum(confs) / len(confs)
    return avg < 0.45 and len(text.split()) <= 2


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
        frames = 1600  # 100ms @ 16kHz — snappier partials than 125ms
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
                if _MIC_MUTE.is_set():
                    continue
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
    if cmd.kind == "help":
        utter(lamp, cmd.reply)
        return "help"
    if cmd.kind == "status":
        utter(lamp, lamp.apply(cmd))
        return "status"
    if cmd.kind == "snap":
        path = lamp.snap()
        if not path:
            print("no camera still")
        return "snap"
    if cmd.kind == "unknown":
        if brain is not None:
            if not should_pose_from_chat():
                print("still")
                return "skip"
            brain.ask(raw)
            return "chat"
        utter(lamp, cmd.reply)
        return "unknown"
    lamp.apply(cmd)
    return cmd.kind


def apply_speech(
    lamp: LocalLamp,
    transcript: str,
    brain: Optional[CursorLampSession] = None,
    *,
    listen_mode: bool = False,
    pose_chance: Optional[float] = None,
    rng=None,
) -> str:
    print(f"lamp< {transcript}")
    compact = _compact_speech(transcript)
    if listen_mode and compact in {"hello", "hi", "hey"}:
        return "ack"
    if compact in _CHAT_FILLERS:
        return "ack"
    if brain is not None:
        hardware = hardware_spoken_command(transcript)
        if hardware:
            if hardware != compact:
                print(f"heard as: {hardware}")
            return dispatch_text(lamp, hardware, None)
        chance = POSE_CHANCE if pose_chance is None else pose_chance
        if not should_pose_from_chat(chance=chance, rng=rng):
            print("still")
            return "skip"
        brain.ask(transcript)
        return "chat"
    phrase = direct_spoken_command(transcript)
    if phrase:
        if phrase != compact:
            print(f"heard as: {phrase}")
        return dispatch_text(lamp, phrase, None)
    print(f"I heard “{transcript}”, but that isn't a lamp command.")
    return "unknown"


def run_listen_loop(
    lamp: LocalLamp,
    *,
    device: Optional[int],
    model_path: Path,
    brain: Optional[CursorLampSession] = None,
    wake_word: bool = True,
    hold_s: float = 0.45,
    session_s: float = 90.0,
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
        print("Mic on. Say hello lamp once, then talk. I may pose from recordings.")
        print("Desk commands (lights / study / reading / closer) skip the wake word.")
    else:
        print("Mic on (no wake word). I pose from what you mean. No voice reply.")
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
                        print("listening")
                        _listen_settle(catcher, out_q)
                    ready = rest
                if ready:
                    chatting = (not wake_word) or (time.monotonic() < awake_until)
                    local = (
                        hardware_spoken_command(ready)
                        if brain is not None
                        else direct_spoken_command(ready)
                    )
                    if local or chatting:
                        if apply_speech(lamp, ready, brain, listen_mode=True) == "quit":
                            return 0
                        _listen_settle(catcher, out_q)
                        if chatting:
                            awake_until = time.monotonic() + session_s
                    else:
                        print("Say hello lamp first.")
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line == "":
                    print("Okay. I'll be right here.")
                    return 0
                print()
                if dispatch_text(lamp, line, brain) == "quit":
                    return 0
    except KeyboardInterrupt:
        print()
        print("Okay. I'll be right here.")
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
        self.last_photo = ""
        self.motors = None
        self.rgb = None
        self._motion_thread = None

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
        thread = self._motion_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.05, deadline - time.monotonic()))
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

    def _play(self, recording: str, *, wait: bool = True) -> None:
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
            if wait:
                play_fn(recording)
                return
            # Conversation path: nod in the background while TTS starts.
            thread = threading.Thread(
                target=play_fn,
                args=(recording,),
                daemon=True,
                name="lelamp-motion",
            )
            self._motion_thread = thread
            thread.start()
            return
        self.motors.dispatch("play", recording)
        if wait:
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

    def _present_action(self) -> Optional[Dict[str, float]]:
        robot = getattr(self.motors, "robot", None) if self.motors is not None else None
        bus = getattr(robot, "bus", None) if robot is not None else None
        if bus is None:
            return None
        try:
            present = bus.sync_read("Present_Position")
        except Exception as exc:
            print(f"pose read failed: {exc}")
            return None
        return {f"{name}.pos": float(val) for name, val in present.items()}

    def _goto_named_pose(self, name: str, *, seconds: float = 1.1) -> None:
        """Move to a desk pose. 'closer' nudges down toward the book."""
        self.last_expression = name
        start = None if self.sim else self._present_action()
        if name == "closer":
            target = _closer_pose(start)
        else:
            target = dict(POSES[name])
            if start is not None:
                target["base_yaw.pos"] = start.get("base_yaw.pos", target["base_yaw.pos"])
        if self.sim or self.motors is None:
            print(f"[sim] pose {name} {target}")
            return
        robot = getattr(self.motors, "robot", None)
        send = getattr(robot, "send_action", None) if robot is not None else None
        if not callable(send):
            print("pose skipped: no send_action")
            return
        self._wait_hw()
        origin = start or target
        frames = max(8, int(seconds * 30))
        try:
            for step in range(1, frames + 1):
                t = step / frames
                action = {
                    key: float(origin.get(key, value)) * (1.0 - t) + float(value) * t
                    for key, value in target.items()
                }
                send(action)
                time.sleep(1.0 / 30.0)
        except Exception as exc:
            print(f"pose failed: {exc}")

    def snap(self) -> Optional[Path]:
        self._play("scanning", wait=False)
        self._apply_rgb(EXPRESSION_RGB["scanning"])
        photo = capture_still(sim=self.sim)
        if photo is not None:
            self.last_photo = str(photo)
        return photo

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

    def apply(self, cmd: Command, *, wait_motion: bool = True) -> str:
        if cmd.kind == "noop":
            return ""
        if cmd.kind in {"quit", "help", "unknown"}:
            return cmd.reply
        if cmd.kind == "status":
            return (
                f"sim={self.sim} expression={self.last_expression or '-'} "
                f"rgb={self.last_rgb} brightness={self.brightness} volume={self.volume} "
                f"photo={self.last_photo or '-'}"
            )
        if cmd.kind == "express":
            rec = str(cmd.payload)
            self._play(rec, wait=wait_motion)
            self._apply_rgb(EXPRESSION_RGB[rec])
            return cmd.reply
        if cmd.kind == "snap":
            path = self.snap()
            if path is None:
                return "I can't see right now."
            return f"{cmd.reply} Photo {path}."
        if cmd.kind == "mood":
            mood = str(cmd.payload)
            if mood == "auto":
                mood, bri = circadian_mood()
                self.brightness = bri
            self._apply_rgb(MOOD_RGB[mood])
            return cmd.reply
        if cmd.kind == "scene":
            payload = cmd.payload
            assert isinstance(payload, dict)
            if payload.get("brightness") is not None:
                self.brightness = max(0, min(100, int(payload["brightness"])))
            mood = payload.get("mood")
            if mood:
                self._apply_rgb(MOOD_RGB[str(mood)])
            pose = payload.get("pose")
            if pose:
                self._goto_named_pose(str(pose))
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
        print("Lamp's awake. I'll nod or shake from what you mean. No voice.")
        print("To talk, say hello lamp first.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeLamp local Stages 1–3 (Vosk + silent pose)")
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
        default=float(os.environ.get("LELAMP_LISTEN_HOLD", "0.45")),
        help="seconds of silence before an unfinished sentence is committed (default 0.45)",
    )
    parser.add_argument("--download-vosk", action="store_true", help="download the English Vosk small model")
    parser.add_argument(
        "--download-piper",
        action="store_true",
        help="download the companion English Piper voice (Ryan, not a celebrity clone)",
    )
    parser.add_argument("--say", action="append", default=[], help="inject a spoken phrase (repeatable)")
    parser.add_argument("--speak", action="append", default=[], help="speak this sentence on the speaker")
    parser.add_argument("--no-speak", action="store_true", help="print replies only, do not use the speaker")
    parser.add_argument("--device", type=int, default=None, help="sounddevice input index")
    parser.add_argument("--model", type=Path, default=None, help="path to vosk-model-small-en-us-0.15")
    parser.add_argument("--en-model", type=Path, default=None, help="alias for --model")
    parser.add_argument("--ask", action="append", default=[], help="one sentence for the pose model (repeatable)")
    parser.add_argument("--snap", action="store_true", help="take one camera still and exit (unless --listen/--say)")
    parser.add_argument("--no-cursor", action="store_true", help="keywords only; do not call Cursor")
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
    fetched = False
    if args.download_vosk:
        download_vosk_model(args.en_model or args.model, lang="en")
        fetched = True
    if args.download_piper:
        download_piper_voice()
        fetched = True
    if fetched and not (args.listen or args.speak or args.say or args.ask or args.snap):
        return 0
    print(describe_camera(sim=args.sim))
    speak_enabled = bool(args.speak) and not args.no_speak
    lamp = LocalLamp(
        sim=args.sim,
        port=args.port,
        lamp_id=args.lamp_id,
        led_count=args.led_count,
        brightness=70,
        speak_enabled=speak_enabled,
    )
    brain = None
    if not args.no_cursor and os.environ.get("CURSOR_API_KEY"):
        brain = CursorLampSession(lamp)
    if brain is not None:
        print("talk: desk commands are local. Other talk: coin-flip official pose. No voice.")
    else:
        print("talk: local desk commands. Put CURSOR_API_KEY=crsr_... in .env for chat poses.")
    if speak_enabled:
        print(describe_tts())
    voice_warm = None
    if speak_enabled and not args.sim:
        voice_warm = threading.Thread(target=warm_tts, daemon=True, name="lelamp-voice")
        voice_warm.start()
    lamp.start()
    if voice_warm is not None:
        voice_warm.join(timeout=20)
    if brain is not None and (args.listen or args.ask):
        brain.start()
    try:
        if not args.no_wake:
            lamp.wake()
        if args.snap:
            path = lamp.snap()
            if path is None and not args.sim:
                return 1
            if not (args.listen or args.speak or args.say or args.ask):
                return 0
        for phrase in args.speak:
            lamp.speak(phrase)
        for phrase in list(args.say) + list(args.ask):
            if apply_speech(lamp, phrase, brain) == "quit":
                return 0
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
                print("Okay. I'll be right here.")
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
