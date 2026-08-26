"""LeLamp plugin — Hermes as the brain, the lamp as the body.

Standalone and opt-in: ``hermes plugins enable lelamp``. Tools stay off
until that happens, so there is no model-tool footprint for users who
are not building a lamp. Default backend is a file-backed simulator so
the personality can be rehearsed without servos.
"""

from __future__ import annotations

from pathlib import Path

from .cli import lelamp_command, register_cli
from .tools import (
    LELAMP_EXPRESS_SCHEMA,
    LELAMP_LIGHT_SCHEMA,
    LELAMP_STATUS_SCHEMA,
    check_lelamp_available,
    handle_lelamp_express,
    handle_lelamp_light,
    handle_lelamp_status,
)


_TOOLS = (
    ("lelamp_express", LELAMP_EXPRESS_SCHEMA, handle_lelamp_express, "🎭"),
    ("lelamp_light", LELAMP_LIGHT_SCHEMA, handle_lelamp_light, "💡"),
    ("lelamp_status", LELAMP_STATUS_SCHEMA, handle_lelamp_status, "🏮"),
)


def _slash_lamp(raw_args: str) -> str:
    """In-session `/lamp` shortcut: express, light, or status."""
    text = (raw_args or "").strip()
    if not text or text in {"status", "状态"}:
        return handle_lelamp_status({})
    parts = text.split(maxsplit=1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    if head in {"light", "灯", "rgb"}:
        args = {"auto": rest in {"auto", "自动", ""}}
        if rest and rest not in {"auto", "自动"}:
            if "," in rest:
                r, g, b = [int(p.strip()) for p in rest.split(",")]
                args = {"red": r, "green": g, "blue": b}
            else:
                args = {"mood": rest}
        return handle_lelamp_light(args)
    if head in {"catalog", "目录"}:
        from .expressions import catalog
        import json
        return json.dumps(catalog(), ensure_ascii=False, indent=2)
    return handle_lelamp_express({"expression": text})


def register(ctx) -> None:
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="lelamp",
            schema=schema,
            handler=handler,
            check_fn=check_lelamp_available,
            emoji=emoji,
        )

    ctx.register_cli_command(
        name="lelamp",
        help="Expressive robot lamp (sim or hardware)",
        setup_fn=register_cli,
        handler_fn=lelamp_command,
        description=(
            "Drive LeLamp motion and light. Default backend is a simulator. "
            "See: hermes lelamp setup"
        ),
    )

    ctx.register_command(
        "lamp",
        _slash_lamp,
        description="LeLamp: /lamp 你好  /lamp light 暖光  /lamp status",
        args_hint="[express|light|status] [arg]",
    )

    skill_md = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "smart-home"
        / "lelamp"
        / "SKILL.md"
    )
    if skill_md.is_file():
        ctx.register_skill(
            "lamp",
            skill_md,
            description="Control LeLamp motion, light, and lamp personality.",
        )
