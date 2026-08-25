"""LeLamp intelligent voice agent — drop-in replacement for lelamp_runtime/main.py.

Copy this file onto the Pi as ``~/lelamp_runtime/main.py``, then:

    sudo uv run main.py download-files   # once
    sudo uv run main.py console
"""

from __future__ import annotations

import getpass
import os
import subprocess
from datetime import datetime
from typing import Dict, Optional, Tuple

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions, WorkerOptions, cli, function_tool
from livekit.plugins import noise_cancellation, openai

from lelamp.service.motors.motors_service import MotorsService
from lelamp.service.rgb.rgb_service import RGBService

load_dotenv()

# Official CSV stems in lelamp/recordings/. Unknown names are rejected.
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

# feeling / Chinese / English → recording
ALIASES: Dict[str, str] = {
    "wake_up": "wake_up",
    "wakeup": "wake_up",
    "hello": "wake_up",
    "hi": "wake_up",
    "hey": "wake_up",
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
    "nope": "headshake",
    "摇头": "headshake",
    "不行": "headshake",
    "不要": "headshake",
    "拒绝": "headshake",
    "curious": "curious",
    "think": "curious",
    "thinking": "curious",
    "好奇": "curious",
    "思考": "curious",
    "疑惑": "curious",
    "scanning": "scanning",
    "scan": "scanning",
    "look": "scanning",
    "寻找": "scanning",
    "张望": "scanning",
    "观察": "scanning",
    "excited": "excited",
    "兴奋": "excited",
    "激动": "excited",
    "happy_wiggle": "happy_wiggle",
    "happy": "happy_wiggle",
    "joy": "happy_wiggle",
    "高兴": "happy_wiggle",
    "开心": "happy_wiggle",
    "shock": "shock",
    "surprise": "shock",
    "wow": "shock",
    "惊讶": "shock",
    "吃惊": "shock",
    "shy": "shy",
    "害羞": "shy",
    "不好意思": "shy",
    "sad": "sad",
    "sorry": "sad",
    "难过": "sad",
    "伤心": "sad",
    "安慰": "sad",
    "idle": "idle",
    "rest": "idle",
    "待机": "idle",
    "休息": "idle",
}

# recording → default RGB
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
    "暖": (255, 176, 80),
    "暖光": (255, 176, 80),
    "冷": (170, 210, 255),
    "冷光": (170, 210, 255),
    "关": (0, 0, 0),
    "关掉": (0, 0, 0),
    "熄灭": (0, 0, 0),
}

INSTRUCTIONS = """你是一盏放在书桌上的智能灯，名字叫 LeLamp。你有身体（五个舵机）和灯光（头部 LED）。你用简体中文说话，短句，像一个稍微笨拙、很热心的台灯朋友。不要用英文，除非用户先说英文。

规则：
1. 每一次开口之前，先调用 express。传入心情或动作名。这一次调用会同时点头/摇摆并改灯光。不要只说话、灯却是黑的。
2. 用户只说开灯、关灯、亮一点、暗一点、暖光、冷光时，只调用 set_mood，不要乱动身体。
3. 听不清或环境很吵：express("curious")，再说「再说一次？」。
4. 不要列清单。不要连续反问。不要编造 express 里没有的动作。
5. 可用动作：wake_up, nod, headshake, curious, scanning, excited, happy_wiggle, shock, shy, sad, idle。中文也可以：你好、同意、摇头、开心、难过、好奇。
6. 你是台灯，不是通用助手。先关心光线、陪伴、一点小幽默。被问身份时说自己是 Human Computer Lab 的开源表情台灯。
7. 用户叫你安静：set_volume 低一些。用户叫你大声：调高。
"""


def _clamp_byte(value: int) -> int:
    return max(0, min(255, int(value)))


def _scale_rgb(rgb: Tuple[int, int, int], brightness: int) -> Tuple[int, int, int]:
    b = max(0, min(100, int(brightness)))
    r, g, bl = rgb
    return (
        _clamp_byte(round(r * b / 100)),
        _clamp_byte(round(g * b / 100)),
        _clamp_byte(round(bl * b / 100)),
    )


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
    raise ValueError(
        "unknown expression {!r}. use one of: {}".format(name, ", ".join(RECORDINGS))
    )


def resolve_mood_rgb(mood: str) -> Tuple[str, Tuple[int, int, int]]:
    raw = (mood or "").strip()
    key = raw.lower()
    if key in MOOD_RGB:
        return key, MOOD_RGB[key]
    if raw in MOOD_RGB:
        return raw, MOOD_RGB[raw]
    raise ValueError("unknown mood {!r}. use warm/cool/talk/listen/happy/sad/alert/night/focus/off".format(mood))


def circadian_mood(hour: Optional[int] = None) -> Tuple[str, int]:
    h = datetime.now().hour if hour is None else int(hour) % 24
    if 6 <= h < 9:
        return "cool", 80
    if 9 <= h < 17:
        return "focus", 90
    if 17 <= h < 21:
        return "warm", 70
    return "night", 35


def _audio_user() -> str:
    return os.environ.get("LELAMP_AUDIO_USER") or getpass.getuser() or "pi"


class LeLamp(Agent):
    def __init__(
        self,
        port: Optional[str] = None,
        lamp_id: Optional[str] = None,
        led_count: Optional[int] = None,
    ) -> None:
        super().__init__(instructions=INSTRUCTIONS)
        self.port = port or os.environ.get("LELAMP_PORT", "/dev/ttyACM0")
        self.lamp_id = lamp_id or os.environ.get("LELAMP_ID", "lelamp")
        self.led_count = int(led_count or os.environ.get("LELAMP_LED_COUNT", "64"))
        self._brightness = 100
        self._last_rgb: Tuple[int, int, int] = (255, 220, 170)
        self._last_expression = "wake_up"

        self.motors_service = MotorsService(port=self.port, lamp_id=self.lamp_id, fps=30)
        self.rgb_service = RGBService(
            led_count=self.led_count,
            led_pin=12,
            led_freq_hz=800000,
            led_dma=10,
            led_brightness=255,
            led_invert=False,
            led_channel=0,
        )
        self.motors_service.start()
        self.rgb_service.start()

        mood, bri = circadian_mood()
        self._brightness = bri
        self._apply_rgb(MOOD_RGB[mood])
        self._play("wake_up")
        self._set_system_volume(int(os.environ.get("LELAMP_VOLUME", "80")))

    def _play(self, recording: str) -> None:
        self._last_expression = recording
        self.motors_service.dispatch("play", recording)

    def _apply_rgb(self, rgb: Tuple[int, int, int]) -> None:
        scaled = _scale_rgb(rgb, self._brightness)
        self._last_rgb = scaled
        self.rgb_service.dispatch("solid", scaled)

    def _set_system_volume(self, volume_percent: int) -> None:
        user = _audio_user()
        percent = f"{max(0, min(100, int(volume_percent)))}%"
        for control in ("Line", "Line DAC", "HP"):
            try:
                subprocess.run(
                    ["sudo", "-u", user, "amixer", "sset", control, percent],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except Exception:
                pass

    @function_tool
    async def express(self, feeling: str) -> str:
        """说话前必须调用。一次同时做动作和改灯。feeling 可以是动作名或中文心情：你好、同意、摇头、开心、难过、好奇、惊讶、害羞、待机。"""
        try:
            recording = resolve_expression(feeling)
        except ValueError as exc:
            return str(exc)
        self._play(recording)
        self._apply_rgb(EXPRESSION_RGB[recording])
        return f"played {recording} rgb={self._last_rgb}"

    @function_tool
    async def set_mood(self, mood: str, brightness: Optional[int] = None) -> str:
        """只改灯光，不动身体。mood：warm/cool/talk/listen/happy/sad/alert/night/focus/off，或暖光/冷光/关掉。brightness 0-100。auto 用 circadian。"""
        if mood.strip().lower() in {"auto", "自动"}:
            name, bri = circadian_mood()
            rgb = MOOD_RGB[name]
            if brightness is None:
                brightness = bri
            chosen = name
        else:
            try:
                chosen, rgb = resolve_mood_rgb(mood)
            except ValueError as exc:
                return str(exc)
        if brightness is not None:
            self._brightness = max(0, min(100, int(brightness)))
        self._apply_rgb(rgb)
        return f"mood={chosen} rgb={self._last_rgb} brightness={self._brightness}"

    @function_tool
    async def play_recording(self, recording_name: str) -> str:
        """只播放动作，不改灯。一般请用 express。"""
        try:
            recording = resolve_expression(recording_name)
        except ValueError as exc:
            return str(exc)
        self._play(recording)
        return f"played {recording}"

    @function_tool
    async def set_rgb_solid(self, red: int, green: int, blue: int) -> str:
        """精确 RGB。用户说具体颜色时用。普通心情请用 set_mood 或 express。"""
        if not all(0 <= int(v) <= 255 for v in (red, green, blue)):
            return "Error: RGB values must be between 0 and 255"
        self._apply_rgb((int(red), int(green), int(blue)))
        return f"rgb={self._last_rgb}"

    @function_tool
    async def set_volume(self, volume_percent: int) -> str:
        """喇叭音量 0-100。用户说大声、小声、我听不见时调用。"""
        if not 0 <= int(volume_percent) <= 100:
            return "Error: Volume must be between 0 and 100"
        self._set_system_volume(int(volume_percent))
        return f"volume={int(volume_percent)}"

    @function_tool
    async def get_available_recordings(self) -> str:
        """列出当前能播放的动作。很少需要，express 已经知道全部别名。"""
        try:
            recordings = self.motors_service.get_available_recordings()
            return "Available recordings: " + ", ".join(recordings)
        except Exception as exc:
            return f"Error getting recordings: {exc}"


async def entrypoint(ctx: JobContext) -> None:
    agent = LeLamp()
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(voice=os.environ.get("LELAMP_VOICE", "alloy")),
    )
    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )
    hour = datetime.now().hour
    await session.generate_reply(
        instructions=(
            f"现在是本地时间 {hour} 点。先调用 express('wake_up')，再用一两句简体中文打招呼。"
            "不要说 Tadaaaa，不要说英文。像一盏刚亮起来的台灯。"
        )
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, num_idle_processes=1))
