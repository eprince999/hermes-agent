"""Stage 1 local lamp: keyboard commands, no OpenAI/LiveKit."""

from __future__ import annotations

from pathlib import Path

from plugins.lelamp.local_main import (
    LocalLamp,
    cursor_api_key,
    execute_lamp_tool,
    extract_spoken_command,
    parse_line,
)


def _source() -> str:
    return (
        Path(__file__).resolve().parents[2] / "plugins" / "lelamp" / "local_main.py"
    ).read_text(encoding="utf-8")


def test_local_main_does_not_import_openai_or_livekit():
    source = _source()
    lowered = source.lower()
    assert "from livekit" not in lowered
    assert "import openai" not in lowered
    assert "plugins.openai" not in lowered
    assert "realtimemodel" not in lowered


def test_hello_is_wake_up_not_a_light_command():
    cmd = parse_line("你好")
    assert cmd.kind == "express"
    assert cmd.payload == "wake_up"


def test_off_is_light_only_not_headshake():
    cmd = parse_line("关灯")
    assert cmd.kind == "mood"
    assert cmd.payload == "off"


def test_on_uses_circadian_auto():
    cmd = parse_line("开灯")
    assert cmd.kind == "mood"
    assert cmd.payload == "auto"


def test_quit_and_unknown():
    assert parse_line("q").kind == "quit"
    unknown = parse_line("跳个舞")
    assert unknown.kind == "unknown"


def test_brightness_and_rgb_parse():
    assert parse_line("亮一点") == parse_line("亮一点")
    delta = parse_line("亮一点")
    assert delta.kind == "brightness_delta"
    assert delta.payload == 15
    rgb = parse_line("rgb 255 176 80")
    assert rgb.kind == "rgb"
    assert rgb.payload == (255, 176, 80)


def test_sim_express_then_off_updates_state():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert "你好呀" in lamp.apply(parse_line("你好"))
    assert lamp.last_expression == "wake_up"
    assert lamp.last_rgb != (0, 0, 0)
    lamp.apply(parse_line("关灯"))
    assert lamp.last_rgb == (0, 0, 0)
    lamp.apply(parse_line("亮一点"))
    assert lamp.brightness == 85


class _OfficialStopMotors:
    """Mirrors MotorsService.stop(): null robot before the worker can finish."""

    def __init__(self):
        self.robot = object()
        self.play_saw_robot = None
        self.dispatched = []
        self.waits = 0
        self.stopped = False
        self._current_event = None

    def _handle_play(self, recording_name):
        self.play_saw_robot = self.robot
        assert self.robot is not None

    def dispatch(self, event_type, payload, priority=None):
        self.dispatched.append((event_type, payload))

    def wait_until_idle(self, timeout=None):
        self.waits += 1
        return True

    def stop(self, timeout=5.0):
        self.robot = None
        self.stopped = True


def test_hardware_play_sync_before_stop_nulls_robot():
    lamp = LocalLamp(
        sim=False,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.motors = _OfficialStopMotors()
    lamp.rgb = _OfficialStopMotors()
    lamp._play("nod")
    assert lamp.motors.play_saw_robot is not None
    assert lamp.motors.dispatched == []
    lamp.stop()
    assert lamp.motors.robot is None
    assert lamp.motors.stopped is True


def test_hardware_play_dispatch_fallback_waits():
    class DispatchOnly:
        def __init__(self):
            self.robot = None
            self.plays = []
            self.waits = 0
            self.stopped = False
            self._current_event = None

        def dispatch(self, event_type, payload, priority=None):
            self.plays.append((event_type, payload))
            self._current_event = None

        def wait_until_idle(self, timeout=None):
            self.waits += 1
            return True

        def stop(self, timeout=5.0):
            self.stopped = True

    lamp = LocalLamp(
        sim=False,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.motors = DispatchOnly()
    lamp.rgb = DispatchOnly()
    lamp._play("nod")
    assert lamp.motors.plays == [("play", "nod")]
    assert lamp.motors.waits >= 1
    lamp.stop()
    assert lamp.motors.stopped is True


def test_main_sim_scripted_session(monkeypatch, capsys):
    from plugins.lelamp import local_main

    lines = iter(["点头", "status", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
    assert local_main.main(["--sim", "--no-wake"]) == 0
    out = capsys.readouterr().out
    assert "好的。" in out
    assert "expression=nod" in out
    assert "好，我先歇着。" in out


def test_extract_spoken_command_from_padded_asr():
    assert extract_spoken_command("请关灯") == "关灯"
    assert extract_spoken_command("你 好 呀") == "你好"
    assert extract_spoken_command("今天天气怎么样") is None
    assert extract_spoken_command("不要关灯") == "不要"


def test_show_stage_prints_current_stage(capsys):
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE >= 1
    assert local_main.main(["--show-stage"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(local_main.AGENT_STAGE))


def test_snapshot_saves_stage2_copy(tmp_path, capsys):
    from plugins.lelamp import local_main

    dest = local_main.snapshot_current("stage3", dest_dir=tmp_path)
    assert dest.name == "stage3.py"
    assert dest.is_file()
    assert "AGENT_STAGE = 3" in dest.read_text(encoding="utf-8")
    assert "saved snapshot" in capsys.readouterr().out
    args = local_main.build_parser().parse_args(["--snapshot"])
    assert args.snapshot == ""
    args = local_main.build_parser().parse_args(["--snapshot", "stage2"])
    assert args.snapshot == "stage2"


def test_main_sim_say_phrases_without_repl(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--say", "请点头", "--say", "关灯"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "rgb (0, 0, 0)" in out
    assert "关灯。" in out


def test_create_cursor_agent_omits_unsupported_tools_kwarg():
    from plugins.lelamp.local_main import create_cursor_agent

    seen = {}

    class FakeAgent:
        @staticmethod
        def create(model, api_key, local):
            seen["kwargs"] = {"model": model, "api_key": api_key, "local": local}
            return "agent"

    agent = create_cursor_agent(
        model="composer-2.5",
        api_key="crsr_test",
        local={"cwd": "/tmp"},
        agent_cls=FakeAgent,
    )
    assert agent == "agent"
    assert "tools" not in seen["kwargs"]


def test_execute_lamp_tool_moves_and_lights():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert "好的" in execute_lamp_tool(lamp, "express", {"feeling": "点头"})
    assert lamp.last_expression == "nod"
    execute_lamp_tool(lamp, "set_mood", {"mood": "关灯"})
    assert lamp.last_rgb == (0, 0, 0)


def test_cursor_api_key_missing_is_explicit(monkeypatch):
    from plugins.lelamp import local_main

    monkeypatch.setattr(local_main, "load_runtime_env", lambda: None)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    try:
        cursor_api_key()
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "CURSOR_API_KEY" in str(exc)
