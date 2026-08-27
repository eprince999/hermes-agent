"""CLI commands for the LeLamp plugin: ``hermes lelamp <subcommand>``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import backend
from .expressions import RECORDINGS, catalog


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="lelamp_command")

    setup = subs.add_parser("setup", help="Write ~/.hermes/lelamp.json")
    setup.add_argument("--mode", choices=("sim", "local", "ssh"), default="sim")
    setup.add_argument("--id", dest="lamp_id", default=None, help="Lamp id (default lelamp)")
    setup.add_argument("--port", default=None, help="Servo serial port")
    setup.add_argument("--host", default=None, help="SSH host for mode=ssh, e.g. pi@lelamp.local")
    setup.add_argument("--runtime-dir", default=None, help="Path to lelamp_runtime checkout")
    setup.add_argument("--language", default=None, help="Preferred spoken language (zh/en)")

    subs.add_parser("status", help="Show mode, last expression, last color")
    subs.add_parser("catalog", help="List recordings, moods, and aliases")

    express = subs.add_parser("express", help="Play a recording or alias")
    express.add_argument("expression", help="wake_up, nod, 你好, 开心, ...")

    light = subs.add_parser("light", help="Set LED mood or RGB")
    light.add_argument("mood", nargs="?", default=None, help="warm, night, off, 暖光, ...")
    light.add_argument("--rgb", default=None, help="R,G,B e.g. 255,176,80")
    light.add_argument("--brightness", type=int, default=None)
    light.add_argument("--auto", action="store_true", help="Circadian mood from local time")


def lelamp_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "lelamp_command", None)
    if not sub:
        print("usage: hermes lelamp {setup,status,catalog,express,light}")
        return 2
    if sub == "setup":
        return _cmd_setup(args)
    if sub == "status":
        print(json.dumps(backend.status(), indent=2, ensure_ascii=False))
        return 0
    if sub == "catalog":
        print(json.dumps(catalog(), indent=2, ensure_ascii=False))
        return 0
    if sub == "express":
        try:
            result = backend.express(args.expression)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            print("known:", ", ".join(RECORDINGS), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if sub == "light":
        rgb = _parse_rgb(getattr(args, "rgb", None))
        try:
            result = backend.light(
                mood=args.mood,
                rgb=rgb,
                brightness=getattr(args, "brightness", None),
                auto=bool(getattr(args, "auto", False)),
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    print(f"unknown lelamp command: {sub}", file=sys.stderr)
    return 2


def _parse_rgb(value: Optional[str]):
    if not value:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 3:
        raise ValueError("--rgb must be R,G,B")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _cmd_setup(args: argparse.Namespace) -> int:
    updates = {"mode": args.mode}
    if args.lamp_id:
        updates["lamp_id"] = args.lamp_id
    if args.port:
        updates["port"] = args.port
    if args.host:
        updates["host"] = args.host
    if getattr(args, "runtime_dir", None):
        updates["runtime_dir"] = args.runtime_dir
    if args.language:
        updates["language"] = args.language
    cfg = backend.save_config(updates)
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    print()
    if cfg["mode"] == "sim":
        print("Sim mode is on. Try: hermes lelamp express 你好")
        print("Then enable the lelamp toolset in `hermes tools` so the agent can drive the lamp.")
    elif cfg["mode"] == "local":
        print("Local mode: replay runs `uv run -m lelamp.replay` in runtime_dir.")
    else:
        print(f"SSH mode: commands run on {cfg['host']}.")
    return 0
