"""Agent-facing tools for the LeLamp plugin."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import backend
from .expressions import catalog

LELAMP_EXPRESS_SCHEMA: Dict[str, Any] = {
    "name": "lelamp_express",
    "description": (
        "Play one of LeLamp's body-language recordings. Use this on almost "
        "every spoken reply so the lamp moves while it talks. Accepts official "
        "names (wake_up, nod, headshake, curious, scanning, excited, "
        "happy_wiggle, shock, shy, sad, idle) or aliases like hello/你好, "
        "yes/同意, no/摇头, happy/开心."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Recording name or alias to play.",
            },
        },
        "required": ["expression"],
    },
}

LELAMP_LIGHT_SCHEMA: Dict[str, Any] = {
    "name": "lelamp_light",
    "description": (
        "Set LeLamp's LED color. Pair with lelamp_express on every reply. "
        "Moods: warm, cool, talk, listen, happy, sad, alert, night, focus, off. "
        "Pass auto=true to pick a circadian color from the local clock."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mood": {
                "type": "string",
                "description": "Named mood or alias (warm/暖光, night/晚上, off/关掉).",
            },
            "red": {
                "type": "integer",
                "description": "Red 0-255. Use with green and blue to skip moods.",
            },
            "green": {
                "type": "integer",
                "description": "Green 0-255.",
            },
            "blue": {
                "type": "integer",
                "description": "Blue 0-255.",
            },
            "brightness": {
                "type": "integer",
                "description": "0-100. Scales the chosen color.",
            },
            "auto": {
                "type": "boolean",
                "description": "If true, pick mood and brightness from time of day.",
            },
        },
    },
}

LELAMP_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "lelamp_status",
    "description": (
        "Report LeLamp mode (sim/local/ssh), last expression, last RGB, "
        "circadian suggestion, and the official recording catalog."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def check_lelamp_available() -> bool:
    return backend.plugin_enabled()


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def handle_lelamp_express(args: Dict[str, Any], **kwargs: Any) -> str:
    expression = str(args.get("expression") or "").strip()
    try:
        return _json(backend.express(expression))
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc), "catalog": catalog()["expressions"]})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def handle_lelamp_light(args: Dict[str, Any], **kwargs: Any) -> str:
    mood: Optional[str] = args.get("mood")
    if mood is not None:
        mood = str(mood).strip() or None
    rgb = None
    if all(k in args and args[k] is not None for k in ("red", "green", "blue")):
        rgb = (int(args["red"]), int(args["green"]), int(args["blue"]))
    brightness = args.get("brightness")
    if brightness is not None:
        brightness = int(brightness)
    auto = bool(args.get("auto", False))
    try:
        return _json(backend.light(mood=mood, rgb=rgb, brightness=brightness, auto=auto))
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc), "moods": list(catalog()["moods"].keys())})
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})


def handle_lelamp_status(args: Dict[str, Any], **kwargs: Any) -> str:
    try:
        return _json(backend.status())
    except Exception as exc:
        return _json({"ok": False, "error": str(exc)})
