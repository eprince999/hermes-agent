"""LeLamp local agent — Stages 1–3, no OpenAI, no LiveKit.

Always copy onto the Pi as ``~/lelamp_runtime/local_main.py``.
Do not rename the runnable file per stage. Snapshot instead::

    mkdir -p ~/lelamp_runtime/lamp_snapshots
    cp local_main.py lamp_snapshots/stage2.py

Keep official ``main.py`` untouched. From the runtime repo root:

    sudo uv run python local_main.py
    sudo uv run python local_main.py --listen
    sudo uv run python local_main.py --ask "把灯调成暖光并点点头"
    sudo uv run python local_main.py --snapshot

Stage 3 uses Cursor's official API (cursor-sdk + CURSOR_API_KEY from
https://cursor.com/dashboard/api). That API is an agent SDK, not a
DeepSeek/OpenAI chat-completions URL. Local custom tools move the lamp.

Roadmap:
  1. keyboard + motors + RGB
  2. on-device speech keywords (Vosk)
  3. Cursor API as the brain (this file)
  4. spoken replies on the speaker
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import queue
import select
import shutil
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.request import urlretrieve

# Bump when a stage lands. Printed at startup so a snapshot is identifiable.
AGENT_STAGE = 3
AGENT_LABEL = "keyboard + vosk + cursor-sdk"


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

HELP_TEXT = """本地台灯 Stage 3（Cursor API，无 OpenAI）
动作：你好 / 点头 / 摇头 / 好奇 / 张望 / 开心 / 兴奋 / 惊讶 / 害羞 / 难过 / 待机
灯光：开灯 / 关灯 / 暖光 / 冷光 / 自动 / 亮一点 / 暗一点
说话：--listen；不会的句子交给 Cursor（CURSOR_API_KEY）
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
            "我还没学会这句。可以说：你好、点头、暖光、关灯。输入 help 看全部。",
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
    phrases = set(LIGHT_ONLY) | set(ALIASES) | set(RECORDINGS) | set(extra)
    return sorted(phrases, key=lambda item: (-len(item), item))


def _compact_speech(transcript: str) -> str:
    text = (transcript or "").strip()
    for token in (" ", "\t", "，", "。", "！", "？", ",", ".", "!", "?", "、"):
        text = text.replace(token, "")
    return text


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


CURSOR_LAMP_INSTRUCTIONS = """你是书桌上的智能灯 LeLamp。用简体中文短句说话，像一个稍微笨拙、很热心的台灯。
用工具控制身体和灯光，不要假装已经动过。不要改文件、不要开 shell。
express：点头/摇头/打招呼等动作（feeling 用 你好、点头、摇头、开心、难过、好奇、待机 等）。
set_mood：只改灯（暖光、冷光、关灯、开灯、亮一点、暗一点）。
set_rgb：用户说了具体颜色时用。
"""


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
        cmd = parse_line(str(payload.get("feeling") or ""))
        if cmd.kind == "unknown":
            return cmd.reply
        return lamp.apply(cmd) or f"ok {cmd.kind}"
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
                        description="Play a lamp body recording and matching light. feeling: 你好, 点头, 摇头, 开心, 难过, 好奇, 待机.",
                        input_schema={
                            "type": "object",
                            "properties": {
                                "feeling": {"type": "string"},
                            },
                            "required": ["feeling"],
                        },
                        execute=_express,
                    ),
                    "set_mood": CustomTool(
                        description="Change light only. mood: 开灯, 关灯, 暖光, 冷光, 亮一点, 暗一点.",
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
        prompt = text
        if self._first:
            prompt = CURSOR_LAMP_INSTRUCTIONS + "\n\n用户：" + text
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


def dispatch_text(lamp: LocalLamp, raw: str, brain: Optional[CursorLampSession] = None) -> str:
    cmd = parse_line(raw)
    if cmd.kind == "quit":
        print(cmd.reply)
        return "quit"
    if cmd.kind == "unknown" and brain is not None:
        print("Cursor …")
        reply = brain.ask(raw)
        if reply:
            print(reply)
        return "chat"
    text = lamp.apply(cmd)
    if text:
        print(text)
    return cmd.kind


def apply_speech(
    lamp: LocalLamp,
    transcript: str,
    brain: Optional[CursorLampSession] = None,
) -> str:
    print(f"灯< {transcript}")
    phrase = extract_spoken_command(transcript)
    if not phrase:
        if brain is not None:
            print("Cursor …")
            reply = brain.ask(transcript)
            if reply:
                print(reply)
            return "chat"
        print(f"听到「{transcript}」，但不是灯的指令。")
        return "unknown"
    if phrase != _compact_speech(transcript):
        print(f"听成：{phrase}")
    return dispatch_text(lamp, phrase)


def run_listen_loop(
    lamp: LocalLamp,
    *,
    device: Optional[int],
    model_path: Path,
    brain: Optional[CursorLampSession] = None,
) -> int:
    stop = threading.Event()
    out_q: "queue.Queue[str]" = queue.Queue()
    worker = threading.Thread(
        target=vosk_listen_worker,
        kwargs={"out_q": out_q, "stop": stop, "device": device, "model_path": model_path},
        daemon=True,
    )
    worker.start()
    print("麦克风线程已开。请说：你好、点头、关灯。打字回车也可以。")
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
                if apply_speech(lamp, item, brain) == "quit":
                    return 0
            if select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline()
                if line == "":
                    print("好，我先歇着。")
                    return 0
                print()
                if dispatch_text(lamp, line, brain) == "quit":
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
        print(f"motors on {self.port}  rgb leds={self.led_count}")

    def stop(self) -> None:
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
        self.motors.dispatch("play", recording)

    def _apply_rgb(self, rgb: Tuple[int, int, int]) -> None:
        self.base_rgb = rgb
        scaled = _scale_rgb(rgb, self.brightness)
        self.last_rgb = scaled
        if self.sim or self.rgb is None:
            print(f"[sim] rgb {scaled} brightness={self.brightness}")
            return
        self.rgb.dispatch("solid", scaled)

    def apply(self, cmd: Command) -> str:
        if cmd.kind == "noop":
            return ""
        if cmd.kind in {"quit", "help", "unknown"}:
            return cmd.reply
        if cmd.kind == "status":
            return (
                f"sim={self.sim} expression={self.last_expression or '-'} "
                f"rgb={self.last_rgb} brightness={self.brightness}"
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
    parser = argparse.ArgumentParser(description="LeLamp local Stages 1–3 (Cursor API, no OpenAI)")
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
        download_vosk_model(args.model)
        return 0
    if args.ask and args.no_cursor:
        raise SystemExit("--ask needs Cursor. Remove --no-cursor and set CURSOR_API_KEY.")
    lamp = LocalLamp(
        sim=args.sim,
        port=args.port,
        lamp_id=args.lamp_id,
        led_count=args.led_count,
        brightness=70,
    )
    brain = None
    if not args.no_cursor and (args.ask or os.environ.get("CURSOR_API_KEY")):
        brain = CursorLampSession(lamp)
    lamp.start()
    try:
        if not args.no_wake:
            lamp.wake()
        for phrase in args.say:
            if apply_speech(lamp, phrase) == "quit":
                return 0
        for text in args.ask:
            print(f"你：{text}")
            reply = brain.ask(text) if brain is not None else ""
            if reply:
                print(reply)
        if (args.say or args.ask) and not args.listen:
            return 0
        if args.listen:
            model_path = args.model if args.model is not None else vosk_model_dir()
            return run_listen_loop(
                lamp,
                device=args.device,
                model_path=Path(model_path),
                brain=brain,
            )
        while True:
            try:
                raw = input("灯> ")
            except (EOFError, KeyboardInterrupt):
                print()
                print("好，我先歇着。")
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
