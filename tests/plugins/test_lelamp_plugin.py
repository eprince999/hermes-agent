"""Tests for the LeLamp plugin.

Behavior contracts: alias resolution, sim state round-trip, circadian
mapping, tool JSON shape, CLI setup. No live SSH, servos, or GPIO.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("LELAMP_DISABLED", raising=False)
    yield hermes_home


def _backend():
    from plugins.lelamp import backend
    return backend


def _expressions():
    from plugins.lelamp import expressions
    return expressions


def test_official_recordings_are_resolvable():
    expr = _expressions()
    for name in expr.RECORDINGS:
        assert expr.resolve_expression(name) == name
        assert expr.resolve_expression(name.upper()) == name


def test_chinese_and_english_aliases_map_to_recordings():
    expr = _expressions()
    assert expr.resolve_expression("你好") == "wake_up"
    assert expr.resolve_expression("hello") == "wake_up"
    assert expr.resolve_expression("同意") == "nod"
    assert expr.resolve_expression("摇头") == "headshake"
    assert expr.resolve_expression("开心") == "happy_wiggle"


def test_unknown_expression_is_rejected():
    expr = _expressions()
    with pytest.raises(ValueError, match="unknown expression"):
        expr.resolve_expression("dance")


def test_mood_rgb_values_stay_in_byte_range():
    expr = _expressions()
    assert expr.MOODS
    for name, rgb in expr.MOODS.items():
        assert len(rgb) == 3
        assert all(0 <= c <= 255 for c in rgb), name
    assert expr.resolve_mood("暖光") == "warm"
    assert expr.resolve_mood("关掉") == "off"


def test_scale_rgb_respects_brightness_bounds():
    expr = _expressions()
    assert expr.scale_rgb((100, 0, 50), 50) == (50, 0, 25)
    assert expr.scale_rgb((10, 10, 10), 0) == (0, 0, 0)
    assert expr.scale_rgb((10, 10, 10), 200) == (10, 10, 10)


def test_circadian_mood_changes_across_day():
    expr = _expressions()
    morning = expr.circadian_mood(7)
    noon = expr.circadian_mood(12)
    evening = expr.circadian_mood(19)
    night = expr.circadian_mood(23)
    assert morning[0] != night[0] or morning[1] != night[1]
    assert noon[1] >= evening[1]
    assert night[1] < morning[1]


def test_sim_express_and_light_round_trip(_isolate_home):
    backend = _backend()
    backend.save_config({"mode": "sim"})
    played = backend.express("你好")
    assert played["ok"] is True
    assert played["sim"] is True
    assert played["expression"] == "wake_up"

    lit = backend.light(mood="暖光", brightness=50)
    assert lit["ok"] is True
    assert lit["mood"] == "warm"
    assert lit["rgb"] == [128, 88, 40]

    st = backend.status()
    assert st["state"]["expression"] == "wake_up"
    assert st["state"]["rgb"] == [128, 88, 40]
    assert st["config"]["mode"] == "sim"
    kinds = [event["kind"] for event in st["history"]]
    assert "express" in kinds
    assert "light" in kinds


def test_sim_auto_light_uses_circadian_clock(_isolate_home):
    backend = _backend()
    backend.save_config({"mode": "sim"})
    lit = backend.light(auto=True)
    assert lit["ok"] is True
    assert lit["mood"] in {"cool", "focus", "warm", "night"}
    assert len(lit["rgb"]) == 3


def test_tool_handlers_return_json(_isolate_home):
    from plugins.lelamp import tools

    backend = _backend()
    backend.save_config({"mode": "sim"})
    express = json.loads(tools.handle_lelamp_express({"expression": "nod"}))
    assert express["ok"] is True
    assert express["expression"] == "nod"

    bad = json.loads(tools.handle_lelamp_express({"expression": "moonwalk"}))
    assert bad["ok"] is False
    assert "wake_up" in bad["catalog"]

    light = json.loads(tools.handle_lelamp_light({"mood": "off"}))
    assert light["ok"] is True
    assert light["rgb"] == [0, 0, 0]

    status = json.loads(tools.handle_lelamp_status({}))
    assert status["ok"] is True
    assert "wake_up" in status["recordings"]


def test_cli_setup_writes_config(_isolate_home):
    from plugins.lelamp.cli import lelamp_command, register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["setup", "--mode", "sim", "--id", "desk"])
    assert lelamp_command(args) == 0
    cfg = json.loads((_isolate_home / "lelamp.json").read_text(encoding="utf-8"))
    assert cfg["mode"] == "sim"
    assert cfg["lamp_id"] == "desk"


def test_cli_express_unknown_returns_2(_isolate_home, capsys):
    from plugins.lelamp.cli import lelamp_command, register_cli

    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["express", "moonwalk"])
    assert lelamp_command(args) == 2
    err = capsys.readouterr().err
    assert "unknown expression" in err


def test_hardware_replay_uses_allowlisted_argv(_isolate_home):
    backend = _backend()
    cfg = backend.default_config()
    cfg["lamp_id"] = "desk"
    cfg["port"] = "/dev/ttyUSB0"
    argv = backend._replay_argv(cfg, "nod")
    assert argv[:4] == ["uv", "run", "-m", "lelamp.replay"]
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "nod"


def test_plugin_register_wires_tools_cli_and_skill(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    mgr = PluginManager()
    manifest = PluginManifest(name="lelamp", kind="standalone", key="lelamp")
    ctx = PluginContext(manifest, mgr)

    from plugins.lelamp import register

    register(ctx)
    assert "lelamp_express" in mgr._plugin_tool_names
    assert "lelamp_light" in mgr._plugin_tool_names
    assert "lelamp_status" in mgr._plugin_tool_names
    assert "lelamp" in mgr._cli_commands
    assert "lamp" in mgr._plugin_commands
    assert "lelamp:lamp" in mgr._plugin_skills


def test_slash_chinese_hello_plays_wake_up(_isolate_home):
    from plugins.lelamp import _slash_lamp, backend

    backend.save_config({"mode": "sim"})
    payload = json.loads(_slash_lamp("你好"))
    assert payload["ok"] is True
    assert payload["expression"] == "wake_up"


def test_runtime_main_is_self_contained_and_allowlisted():
    import ast

    path = Path(__file__).resolve().parents[2] / "plugins" / "lelamp" / "runtime_main.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    recordings = None
    aliases = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "RECORDINGS" in names:
            recordings = ast.literal_eval(node.value)
        if "ALIASES" in names:
            aliases = ast.literal_eval(node.value)
    assert recordings, "runtime_main.py must define RECORDINGS"
    assert aliases, "runtime_main.py must define ALIASES"
    assert "wake_up" in recordings
    assert "idle" in recordings
    unknown = {name: target for name, target in aliases.items() if target not in recordings}
    assert not unknown, unknown
    assert "class LeLamp" in source
    assert "async def express" in source
    assert "cli.run_app" in source


def test_skill_frontmatter_stays_short():
    import re

    path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "smart-home"
        / "lelamp"
        / "SKILL.md"
    )
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^description: (.*)$", text, re.MULTILINE)
    assert match, "skill is missing a description"
    description = match.group(1).strip().strip('"')
    assert description.endswith(".")
    assert len(description) <= 60
    assert "LeLamp" in text
    assert "`lelamp_express`" in text
    assert "`lelamp_light`" in text
    assert "`lelamp_status`" in text
