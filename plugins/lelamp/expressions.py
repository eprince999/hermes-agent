"""Expression catalog and intent aliases for LeLamp.

Official recordings live in humancomputerlab/lelamp_runtime
(``lelamp/recordings/*.csv``). Names here must stay in lockstep with
those files — unknown names are rejected before any hardware call.
"""

from __future__ import annotations

from typing import Dict, Iterable, Tuple

# Official CSV stems shipped with lelamp_runtime.
RECORDINGS: Tuple[str, ...] = (
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

# mood name → RGB. Values are 0-255.
MOODS: Dict[str, Tuple[int, int, int]] = {
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

# Free-text / Chinese / English aliases → official recording name.
_EXPRESSION_ALIASES: Dict[str, str] = {
    "wake_up": "wake_up",
    "wakeup": "wake_up",
    "wake": "wake_up",
    "hello": "wake_up",
    "hi": "wake_up",
    "hey": "wake_up",
    "greet": "wake_up",
    "你好": "wake_up",
    "您好": "wake_up",
    "早上好": "wake_up",
    "打招呼": "wake_up",
    "醒来": "wake_up",
    "nod": "nod",
    "yes": "nod",
    "agree": "nod",
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
    "deny": "headshake",
    "refuse": "headshake",
    "摇头": "headshake",
    "不行": "headshake",
    "不要": "headshake",
    "拒绝": "headshake",
    "curious": "curious",
    "think": "curious",
    "thinking": "curious",
    "wonder": "curious",
    "好奇": "curious",
    "思考": "curious",
    "疑惑": "curious",
    "scanning": "scanning",
    "scan": "scanning",
    "look": "scanning",
    "search": "scanning",
    "寻找": "scanning",
    "张望": "scanning",
    "观察": "scanning",
    "excited": "excited",
    "兴奋": "excited",
    "激动": "excited",
    "happy_wiggle": "happy_wiggle",
    "happy": "happy_wiggle",
    "wiggle": "happy_wiggle",
    "joy": "happy_wiggle",
    "高兴": "happy_wiggle",
    "开心": "happy_wiggle",
    "开心一下": "happy_wiggle",
    "shock": "shock",
    "surprise": "shock",
    "surprised": "shock",
    "wow": "shock",
    "惊讶": "shock",
    "吃惊": "shock",
    "shy": "shy",
    "embarrassed": "shy",
    "害羞": "shy",
    "不好意思": "shy",
    "sad": "sad",
    "sorry": "sad",
    "comfort": "sad",
    "难过": "sad",
    "伤心": "sad",
    "安慰": "sad",
    "idle": "idle",
    "rest": "idle",
    "wait": "idle",
    "待机": "idle",
    "休息": "idle",
}

_MOOD_ALIASES: Dict[str, str] = {
    "warm": "warm",
    "cozy": "warm",
    "reading": "warm",
    "暖": "warm",
    "暖光": "warm",
    "阅读": "warm",
    "cool": "cool",
    "day": "cool",
    "冷": "cool",
    "冷光": "cool",
    "白天": "cool",
    "talk": "talk",
    "speak": "talk",
    "说话": "talk",
    "listen": "listen",
    "hearing": "listen",
    "听": "listen",
    "happy": "happy",
    "开心灯": "happy",
    "sad": "sad",
    "难过灯": "sad",
    "alert": "alert",
    "red": "alert",
    "警告": "alert",
    "night": "night",
    "sleep": "night",
    "晚上": "night",
    "夜间": "night",
    "focus": "focus",
    "work": "focus",
    "专注": "focus",
    "off": "off",
    "dark": "off",
    "关": "off",
    "关掉": "off",
    "熄灭": "off",
}


def resolve_expression(name: str) -> str:
    """Return an official recording name or raise ValueError."""
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("expression name is empty")
    # Exact recording names are case-insensitive.
    for rec in RECORDINGS:
        if rec.lower() == key:
            return rec
    alias = _EXPRESSION_ALIASES.get(key) or _EXPRESSION_ALIASES.get((name or "").strip())
    if alias:
        return alias
    raise ValueError(
        f"unknown expression {name!r}. "
        f"Known: {', '.join(RECORDINGS)}"
    )


def resolve_mood(name: str) -> str:
    """Return an official mood name or raise ValueError."""
    raw = (name or "").strip()
    key = raw.lower()
    if not key:
        raise ValueError("mood name is empty")
    if key in MOODS:
        return key
    alias = _MOOD_ALIASES.get(key) or _MOOD_ALIASES.get(raw)
    if alias:
        return alias
    raise ValueError(
        f"unknown mood {name!r}. "
        f"Known: {', '.join(sorted(MOODS))}"
    )


def scale_rgb(rgb: Iterable[int], brightness: int) -> Tuple[int, int, int]:
    """Scale an RGB triple by brightness 0-100."""
    b = max(0, min(100, int(brightness)))
    r, g, bl = tuple(int(x) for x in rgb)
    return (
        max(0, min(255, round(r * b / 100))),
        max(0, min(255, round(g * b / 100))),
        max(0, min(255, round(bl * b / 100))),
    )


def circadian_mood(hour: int) -> Tuple[str, int]:
    """Return (mood, brightness) for a local hour 0-23."""
    h = int(hour) % 24
    if 6 <= h < 9:
        return "cool", 80
    if 9 <= h < 17:
        return "focus", 90
    if 17 <= h < 21:
        return "warm", 70
    return "night", 35


def catalog() -> Dict[str, object]:
    return {
        "expressions": list(RECORDINGS),
        "moods": {name: list(rgb) for name, rgb in MOODS.items()},
        "aliases": {
            "expressions": dict(_EXPRESSION_ALIASES),
            "moods": dict(_MOOD_ALIASES),
        },
    }
