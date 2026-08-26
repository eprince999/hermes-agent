"""LeLamp backends: sim (default) and hardware (local subprocess or SSH).

Sim never touches servos, GPIO, or the network. Hardware paths only
accept allowlisted recording names resolved by ``expressions.py``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_constants import get_hermes_home

from .expressions import (
    MOODS,
    RECORDINGS,
    circadian_mood,
    resolve_expression,
    resolve_mood,
    scale_rgb,
)

_MODES = ("sim", "local", "ssh")
_DEFAULT_PORT = "/dev/ttyACM0"
_DEFAULT_ID = "lelamp"
_REPLAY_TIMEOUT_S = 60
_RGB_TIMEOUT_S = 20


def _config_path() -> Path:
    return Path(get_hermes_home()) / "lelamp.json"


def _state_path() -> Path:
    return Path(get_hermes_home()) / "lelamp" / "state.json"


def default_config() -> Dict[str, Any]:
    return {
        "mode": "sim",
        "lamp_id": _DEFAULT_ID,
        "port": _DEFAULT_PORT,
        "host": "",
        "runtime_dir": "",
        "language": "zh",
    }


def load_config() -> Dict[str, Any]:
    cfg = default_config()
    path = _config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            cfg.update({k: v for k, v in raw.items() if k in cfg})
    mode = str(cfg.get("mode") or "sim").strip().lower()
    if mode not in _MODES:
        mode = "sim"
    cfg["mode"] = mode
    cfg["lamp_id"] = str(cfg.get("lamp_id") or _DEFAULT_ID).strip() or _DEFAULT_ID
    cfg["port"] = str(cfg.get("port") or _DEFAULT_PORT).strip() or _DEFAULT_PORT
    cfg["host"] = str(cfg.get("host") or "").strip()
    cfg["runtime_dir"] = str(cfg.get("runtime_dir") or "").strip()
    cfg["language"] = str(cfg.get("language") or "zh").strip() or "zh"
    return cfg


def save_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    for key, value in updates.items():
        if key in cfg and value is not None:
            cfg[key] = value
    if cfg["mode"] not in _MODES:
        raise ValueError(f"mode must be one of {_MODES}")
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cfg


def load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {
            "expression": None,
            "rgb": [0, 0, 0],
            "mood": "off",
            "history": [],
            "updated_at": None,
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "expression": None,
            "rgb": [0, 0, 0],
            "mood": "off",
            "history": [],
            "updated_at": None,
        }
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("expression", None)
    raw.setdefault("rgb", [0, 0, 0])
    raw.setdefault("mood", "off")
    raw.setdefault("history", [])
    raw.setdefault("updated_at", None)
    if not isinstance(raw["history"], list):
        raw["history"] = []
    return raw


def _write_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(kind: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    state = load_state()
    event = {"kind": kind, "at": _now_iso(), **detail}
    history = list(state.get("history") or [])
    history.append(event)
    state["history"] = history[-40:]
    state["updated_at"] = event["at"]
    if "expression" in detail:
        state["expression"] = detail["expression"]
    if "rgb" in detail:
        state["rgb"] = list(detail["rgb"])
    if "mood" in detail:
        state["mood"] = detail["mood"]
    _write_state(state)
    return state


def _replay_argv(cfg: Dict[str, Any], expression: str) -> list[str]:
    return [
        "uv",
        "run",
        "-m",
        "lelamp.replay",
        "--id",
        cfg["lamp_id"],
        "--port",
        cfg["port"],
        "--name",
        expression,
    ]


def _run_hardware(cfg: Dict[str, Any], argv: list[str], timeout: int) -> Tuple[bool, str]:
    mode = cfg["mode"]
    runtime_dir = cfg["runtime_dir"]
    if mode == "local":
        cwd = runtime_dir or None
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return False, f"command not found: {exc}"
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, out.strip() or f"exit {proc.returncode}"
        return True, out.strip()
    if mode == "ssh":
        host = cfg["host"]
        if not host:
            return False, "ssh mode requires host (user@pi) in lelamp.json"
        remote_bits = []
        if runtime_dir:
            remote_bits.append("cd " + shlex.quote(runtime_dir) + " &&")
        # argv is already allowlisted; quote so python -c snippets survive SSH.
        remote_bits.extend(shlex.quote(part) for part in argv)
        remote = " ".join(remote_bits)
        ssh_argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            host,
            remote,
        ]
        try:
            proc = subprocess.run(
                ssh_argv,
                capture_output=True,
                text=True,
                timeout=timeout + 10,
                check=False,
            )
        except FileNotFoundError:
            return False, "ssh is not installed"
        except subprocess.TimeoutExpired:
            return False, f"ssh timed out after {timeout + 10}s"
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return False, out.strip() or f"ssh exit {proc.returncode}"
        return True, out.strip()
    return False, f"unsupported hardware mode {mode!r}"


def express(name: str) -> Dict[str, Any]:
    expression = resolve_expression(name)
    cfg = load_config()
    result: Dict[str, Any] = {
        "ok": True,
        "mode": cfg["mode"],
        "expression": expression,
        "sim": cfg["mode"] == "sim",
    }
    if cfg["mode"] == "sim":
        _record("express", {"expression": expression})
        result["message"] = f"sim: played {expression}"
        return result

    ok, output = _run_hardware(cfg, _replay_argv(cfg, expression), _REPLAY_TIMEOUT_S)
    _record("express", {"expression": expression, "hardware_ok": ok})
    result["ok"] = ok
    result["message"] = output or f"played {expression}"
    if not ok:
        result["error"] = output or "hardware replay failed"
    return result


def light(
    mood: Optional[str] = None,
    rgb: Optional[Tuple[int, int, int]] = None,
    brightness: Optional[int] = None,
    auto: bool = False,
) -> Dict[str, Any]:
    cfg = load_config()
    chosen_mood = None
    if auto and mood is None and rgb is None:
        hour = datetime.now().hour
        chosen_mood, auto_brightness = circadian_mood(hour)
        if brightness is None:
            brightness = auto_brightness
        mood = chosen_mood
    if rgb is None:
        if not mood:
            raise ValueError("provide mood, rgb, or auto=true")
        chosen_mood = resolve_mood(mood)
        rgb = MOODS[chosen_mood]
    else:
        rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        if not all(0 <= c <= 255 for c in rgb):
            raise ValueError("rgb values must be 0-255")
        if mood:
            chosen_mood = resolve_mood(mood)
    if brightness is None:
        brightness = 100
    scaled = scale_rgb(rgb, brightness)

    result: Dict[str, Any] = {
        "ok": True,
        "mode": cfg["mode"],
        "mood": chosen_mood,
        "rgb": list(scaled),
        "brightness": int(brightness),
        "sim": cfg["mode"] == "sim",
    }
    if cfg["mode"] == "sim":
        _record("light", {"mood": chosen_mood or "custom", "rgb": list(scaled)})
        result["message"] = f"sim: rgb {scaled}"
        return result

    # Hardware RGB is best-effort: the official runtime has no CLI for it.
    snippet = (
        "from lelamp.service.rgb.rgb_service import RGBService; "
        f"s=RGBService(); s.handle_event('solid', {list(scaled)!r})"
    )
    argv = ["uv", "run", "python", "-c", snippet]
    ok, output = _run_hardware(cfg, argv, _RGB_TIMEOUT_S)
    _record("light", {"mood": chosen_mood or "custom", "rgb": list(scaled), "hardware_ok": ok})
    result["ok"] = ok
    result["message"] = output or f"rgb {scaled}"
    if not ok:
        result["error"] = output or "hardware light failed"
        result["hint"] = (
            "Motion replay still works without RGB. "
            "RGB needs lelamp_runtime on the Pi with `uv sync --extra hardware`."
        )
    return result


def status() -> Dict[str, Any]:
    cfg = load_config()
    state = load_state()
    hour = datetime.now().hour
    mood, bri = circadian_mood(hour)
    return {
        "ok": True,
        "config": cfg,
        "state": {
            "expression": state.get("expression"),
            "rgb": state.get("rgb"),
            "mood": state.get("mood"),
            "updated_at": state.get("updated_at"),
        },
        "history": list(state.get("history") or [])[-8:],
        "recordings": list(RECORDINGS),
        "circadian": {"hour": hour, "mood": mood, "brightness": bri},
        "config_path": str(_config_path()),
        "sim": cfg["mode"] == "sim",
        "plugin_enabled_hint": (
            "hermes plugins enable lelamp && hermes lelamp setup --mode sim"
        ),
    }


def plugin_enabled() -> bool:
    """True when this plugin is loaded. Used as a tool check_fn.

    Tools only register when the plugin is enabled, so this can stay True.
    Override via LELAMP_DISABLED=1 in tests.
    """
    return os.environ.get("LELAMP_DISABLED", "").strip() not in {"1", "true", "yes"}
