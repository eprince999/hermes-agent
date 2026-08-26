"""Stage 1 local lamp: keyboard commands, no OpenAI/LiveKit."""

from __future__ import annotations

from pathlib import Path

from plugins.lelamp.local_main import (
    LocalLamp,
    SpeechCatcher,
    apply_speech,
    cursor_api_key,
    direct_spoken_command,
    execute_lamp_tool,
    extract_spoken_command,
    join_speech,
    looks_complete_utterance,
    parse_line,
    pick_asr,
    resolve_feeling,
    speak_text,
    speech_lang,
    split_wake,
    utterance_too_short,
    wake_ack,
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
    cmd = parse_line("hello")
    assert cmd.kind == "express"
    assert cmd.payload == "wake_up"


def test_off_is_light_only_not_headshake():
    cmd = parse_line("lights off")
    assert cmd.kind == "mood"
    assert cmd.payload == "off"


def test_on_uses_circadian_auto():
    cmd = parse_line("lights on")
    assert cmd.kind == "mood"
    assert cmd.payload == "auto"


def test_quit_and_unknown():
    assert parse_line("q").kind == "quit"
    unknown = parse_line("do a dance")
    assert unknown.kind == "unknown"


def test_brightness_and_rgb_parse():
    delta = parse_line("brighter")
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
    assert "Hi there" in lamp.apply(parse_line("hello"))
    assert lamp.last_expression == "wake_up"
    assert lamp.last_rgb != (0, 0, 0)
    lamp.apply(parse_line("lights off"))
    assert lamp.last_rgb == (0, 0, 0)
    lamp.apply(parse_line("brighter"))
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

    lines = iter(["nod", "status", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
    assert local_main.main(["--sim", "--no-wake"]) == 0
    out = capsys.readouterr().out
    assert "Sure, that works for me." in out
    assert "expression=nod" in out
    assert "I'll be here if you need me." in out


def test_extract_spoken_command_from_padded_asr():
    assert extract_spoken_command("please lights off") == "lights off"
    assert extract_spoken_command("hello there") == "hello"
    assert extract_spoken_command("what is the weather today") is None
    assert extract_spoken_command("lights on") == "lights on"


def test_direct_spoken_command_keeps_full_sentences_for_the_model():
    assert direct_spoken_command("lights off") == "lights off"
    assert direct_spoken_command("please nod") == "nod"
    assert direct_spoken_command("warm light") == "warm light"
    assert direct_spoken_command("Do you agree warm light is nicer") is None
    assert direct_spoken_command("what is the weather today") is None


def test_resolve_feeling_agree_and_disagree():
    assert resolve_feeling("nod") == "nod"
    assert resolve_feeling("agree") == "nod"
    assert resolve_feeling("yes") == "nod"
    assert resolve_feeling("shake") == "headshake"
    assert resolve_feeling("disagree") == "headshake"
    assert resolve_feeling("refuse") == "headshake"


def test_express_tool_nods_on_agree_feeling():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    execute_lamp_tool(lamp, "express", {"feeling": "agree"})
    assert lamp.last_expression == "nod"
    execute_lamp_tool(lamp, "express", {"feeling": "disagree"})
    assert lamp.last_expression == "headshake"


def test_long_speech_goes_to_cursor_not_keyword_snip():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    seen = []

    class Brain:
        def ask(self, text):
            seen.append(text)
            return "Sure, I agree."

    result = apply_speech(lamp, "Do you agree warm light is nicer", Brain())
    assert result == "chat"
    assert seen == ["Do you agree warm light is nicer"]
    assert lamp.last_expression != "nod"


def test_short_keyword_speech_stays_local():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )

    class Brain:
        def ask(self, text):
            raise AssertionError("short nod should not call Cursor")

    apply_speech(lamp, "nod", Brain())
    assert lamp.last_expression == "nod"


def test_join_speech_glues_vosk_fragments():
    assert join_speech("what day", "is it") == "what day is it"
    assert join_speech("what day is it") == "what day is it"
    assert join_speech("hello lamp") == "hello lamp"


def test_split_wake_and_complete_utterance():
    assert split_wake("hello lamp") == (True, "")
    assert split_wake("hello lamp what day is it") == (True, "what day is it")
    assert split_wake("hey lamp lights off") == (True, "lights off")
    assert split_wake("lights off") == (False, "lights off")
    assert looks_complete_utterance("lights off") is True
    assert looks_complete_utterance("today") is False
    assert looks_complete_utterance("what day is it") is True
    assert looks_complete_utterance("do you agree") is True


def test_speech_catcher_waits_then_merges():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    catcher = SpeechCatcher(hold_s=0.9, now=clock)
    catcher.note_final("what")
    assert catcher.take_ready() == ""
    clock.t = 0.5
    assert catcher.take_ready() == ""
    catcher.note_final("day is it")
    assert catcher.take_ready() == "what day is it"


def test_speech_catcher_flushes_local_command_immediately():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    catcher = SpeechCatcher(hold_s=2.0, now=Clock())
    catcher.note_final("lights off")
    assert catcher.take_ready() == "lights off"


def test_listen_hello_does_not_replay_wake_up():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )

    class Brain:
        def ask(self, text):
            raise AssertionError("listen hello should not call Cursor")

    apply_speech(lamp, "hello", Brain(), listen_mode=True)
    assert lamp.last_expression != "wake_up"
    assert lamp.last_spoken == "I'm right here."


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

    assert local_main.main(["--sim", "--no-wake", "--say", "please nod", "--say", "lights off"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "rgb (0, 0, 0)" in out
    assert "Lights off." in out


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
    assert "Sure, that works for me." in execute_lamp_tool(lamp, "express", {"feeling": "nod"})
    assert lamp.last_expression == "nod"
    execute_lamp_tool(lamp, "set_mood", {"mood": "lights off"})
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


def test_volume_keywords_and_status():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert lamp.volume == 100
    assert parse_line("louder").kind == "volume_delta"
    lamp.apply(parse_line("louder"))
    assert lamp.volume == 100
    lamp.apply(parse_line("volume 40"))
    assert lamp.volume == 40
    assert "volume=40" in lamp.apply(parse_line("status"))


def test_espeak_amplitude_is_loud():
    from plugins.lelamp.local_main import _espeak_cmd

    cmd = _espeak_cmd("/usr/bin/espeak-ng", "hello", 100)
    assert "-a" in cmd
    assert int(cmd[cmd.index("-a") + 1]) == 200
    assert cmd[cmd.index("-v") + 1] == "en-us"
    assert cmd[cmd.index("-s") + 1] == "150"


def test_speak_text_sim_prints(capsys):
    assert speak_text("Hi there. I'm your lamp.", sim=True) == "sim"
    assert "[sim] speak Hi there. I'm your lamp." in capsys.readouterr().out
    assert speak_text("ignore me", sim=True, enabled=False) == ""


def test_speak_text_espeak_ng(monkeypatch):
    from plugins.lelamp import local_main

    calls = []

    def fake_which(name):
        if name == "espeak-ng":
            return "/usr/bin/espeak-ng"
        return None

    def fake_run(cmd, check=False, timeout=None):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    monkeypatch.delenv("LELAMP_TTS", raising=False)
    monkeypatch.delenv("LELAMP_PIPER_MODEL", raising=False)
    monkeypatch.setattr(local_main.shutil, "which", fake_which)
    monkeypatch.setattr(local_main.subprocess, "run", fake_run)
    assert speak_text("Alright.", sim=False, volume=80) == "espeak-ng"
    assert calls[0][0] == "/usr/bin/espeak-ng"
    assert "-v" in calls[0]
    assert "Alright." in calls[0]


def test_espeak_english_voice(monkeypatch):
    from plugins.lelamp import local_main

    calls = []

    def fake_which(name):
        if name == "espeak-ng":
            return "/usr/bin/espeak-ng"
        return None

    def fake_run(cmd, check=False, timeout=None):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    monkeypatch.delenv("LELAMP_TTS", raising=False)
    monkeypatch.delenv("LELAMP_PIPER_MODEL", raising=False)
    monkeypatch.delenv("LELAMP_ESPEAK_VOICE", raising=False)
    monkeypatch.setattr(local_main.shutil, "which", fake_which)
    monkeypatch.setattr(local_main.subprocess, "run", fake_run)
    assert speak_text("Hello, I am the lamp.", sim=False, volume=100) == "espeak-ng"
    assert calls[0][calls[0].index("-v") + 1] == "en-us"


def test_main_sim_speak_without_cursor(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--speak", "Hi there. I'm your lamp."]) == 0
    out = capsys.readouterr().out
    assert "stage 3" in out
    assert "[sim] speak Hi there. I'm your lamp." in out


def test_english_keywords_parse_and_reply():
    assert parse_line("lights off").kind == "mood"
    assert parse_line("lights off").payload == "off"
    assert parse_line("lights off").reply == "Lights off."
    assert parse_line("warm light").payload == "warm"
    assert parse_line("nod").kind == "express"
    assert parse_line("nod").payload == "nod"
    assert parse_line("shake").payload == "headshake"
    assert parse_line("brighter").kind == "brightness_delta"
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert "Sure, that works for me." in lamp.apply(parse_line("nod"))
    lamp.apply(parse_line("lights off"))
    assert lamp.last_rgb == (0, 0, 0)


def test_english_helpers():
    assert speech_lang("what day is it") == "en"
    assert speech_lang("q") == "en"
    assert wake_ack("hello lamp") == "I'm right here."
    assert utterance_too_short("hi") is True
    assert utterance_too_short("what day is it") is False
    assert pick_asr(("final", "ah"), ("final", "what day is it"))[1] == "what day is it"


def test_speech_catcher_keeps_english_spaces():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    catcher = SpeechCatcher(hold_s=0.9, now=clock)
    catcher.note_final("what day")
    assert catcher.take_ready() == ""
    clock.t = 0.5
    catcher.note_final("is it")
    assert catcher.take_ready() == "what day is it"


def test_download_vosk_flag_is_english():
    from plugins.lelamp.local_main import build_parser

    help_text = build_parser().format_help()
    assert "English" in help_text
    args = build_parser().parse_args(["--download-vosk"])
    assert args.download_vosk is True


def test_main_sim_speak_english(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--speak", "hello I'm the lamp"]) == 0
    out = capsys.readouterr().out
    assert "[sim] speak hello I'm the lamp" in out
    assert "vosk(en)" in out
