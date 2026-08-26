"""Stage 1 local lamp: keyboard commands, no OpenAI/LiveKit."""

from __future__ import annotations

from pathlib import Path
import threading
import time

from plugins.lelamp.local_main import (
    LocalLamp,
    SpeechCatcher,
    apply_speech,
    cursor_api_key,
    direct_spoken_command,
    ensure_builtin_music,
    execute_lamp_tool,
    extract_spoken_command,
    hardware_spoken_command,
    join_speech,
    looks_complete_utterance,
    looks_like_look,
    parse_line,
    pick_asr,
    resolve_feeling,
    should_pose_from_chat,
    speak_text,
    speech_lang,
    split_wake,
    utterance_too_short,
    wake_ack,
    write_beat_wav,
    _BUILTIN_TRACKS,
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
    assert "Hey. I'm here with you." in lamp.apply(parse_line("hello"))
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
    assert "play nod" in out
    assert "expression=nod" in out
    assert "Okay. I'll be right here." in out


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
    assert hardware_spoken_command("lights off") == "lights off"
    assert hardware_spoken_command("please lights off") == "lights off"
    assert hardware_spoken_command("nod") is None
    assert hardware_spoken_command("how are you") is None


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


def test_long_speech_is_ignored_not_keyword_snip():
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
            return "ok"

    result = apply_speech(lamp, "Do you agree warm light is nicer", Brain(), pose_chance=1.0)
    assert result == "unknown"
    assert seen == []
    assert lamp.last_expression != "nod"
    assert lamp.last_spoken == ""


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
            raise AssertionError("lights off should not call a model")

    apply_speech(lamp, "lights off", Brain())
    assert lamp.last_rgb == (0, 0, 0)
    assert lamp.last_spoken == ""


def test_spoken_keywords_stay_local_without_a_prompt():
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
            return "I would talk but the lamp stays silent."

    assert apply_speech(lamp, "nod", Brain(), pose_chance=1.0) == "express"
    assert apply_speech(lamp, "how are you", Brain(), pose_chance=1.0) == "unknown"
    assert apply_speech(lamp, "yeah I like the warm light", Brain(), pose_chance=1.0) == "unknown"
    assert seen == []
    assert lamp.last_expression == "nod"
    assert lamp.last_spoken == ""


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
    assert looks_complete_utterance("what day is") is False
    assert looks_complete_utterance("what day is it") is True
    assert looks_complete_utterance("do you agree") is True
    assert looks_complete_utterance("you good") is True


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
    assert lamp.last_spoken == ""


def test_show_stage_prints_current_stage(capsys):
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE >= 1
    assert local_main.main(["--show-stage"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(local_main.AGENT_STAGE))


def test_snapshot_saves_current_stage_copy(tmp_path, capsys):
    from plugins.lelamp import local_main

    dest = local_main.snapshot_current(dest_dir=tmp_path)
    assert dest.name == f"stage{local_main.AGENT_STAGE}.py"
    assert dest.is_file()
    assert f"AGENT_STAGE = {local_main.AGENT_STAGE}" in dest.read_text(encoding="utf-8")
    assert "saved snapshot" in capsys.readouterr().out
    args = local_main.build_parser().parse_args(["--snapshot"])
    assert args.snapshot == ""
    args = local_main.build_parser().parse_args(["--snapshot", "stage2"])
    assert args.snapshot == "stage2"


def test_tracked_stage_snapshots_keep_dance_on_stage4():
    root = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "lelamp"
        / "lamp_snapshots"
    )
    stage2 = (root / "stage2.py").read_text(encoding="utf-8")
    stage3 = (root / "stage3.py").read_text(encoding="utf-8")
    stage4 = (root / "stage4.py").read_text(encoding="utf-8")
    assert "AGENT_STAGE = 2" in stage2
    assert "keyboard + vosk listen" in stage2
    assert "def play_music" not in stage2
    assert "AGENT_STAGE = 3" in stage3
    assert "def play_music" not in stage3
    assert "AGENT_STAGE = 4" in stage4
    assert "def play_music" in stage4
    assert "MUSIC_START" in stage4
    assert "brain.ask(transcript)" not in stage4


def test_main_sim_say_phrases_without_repl(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--say", "please nod", "--say", "lights off"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "rgb (0, 0, 0)" in out
    assert "[sim] speak" not in out


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
    assert "ok nod" in execute_lamp_tool(lamp, "express", {"feeling": "nod"})
    assert lamp.last_expression == "nod"
    execute_lamp_tool(lamp, "set_mood", {"mood": "lights off"})
    assert lamp.last_rgb == (0, 0, 0)


def test_express_tool_only_plays_official_recordings():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert "ok curious" in execute_lamp_tool(lamp, "express", {"feeling": "curious"})
    assert lamp.last_expression == "curious"
    lamp.last_expression = "nod"
    out = execute_lamp_tool(lamp, "express", {"feeling": "closer"})
    assert "unknown pose" in out
    assert lamp.last_expression == "nod"
    out = execute_lamp_tool(lamp, "express", {"feeling": "study mode"})
    assert "unknown pose" in out


def test_leftover_talk_does_not_call_a_model():
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
            return ""

    assert apply_speech(lamp, "how are you", Brain(), pose_chance=0.0) == "unknown"
    assert apply_speech(lamp, "how are you", Brain(), pose_chance=1.0) == "unknown"
    assert seen == []


def test_should_pose_from_chat_uses_chance():
    assert should_pose_from_chat(chance=1.0, rng=lambda: 0.99) is True
    assert should_pose_from_chat(chance=0.2, rng=lambda: 0.9) is False
    assert should_pose_from_chat(chance=0.0, rng=lambda: 0.0) is False


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
    assert speak_text("Hey. I'm here with you.", sim=True) == "sim"
    assert "[sim] speak Hey. I'm here with you." in capsys.readouterr().out
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

    assert local_main.main(["--sim", "--no-wake", "--speak", "Hey. I'm here with you."]) == 0
    out = capsys.readouterr().out
    assert f"stage {local_main.AGENT_STAGE}" in out
    assert "[sim] speak Hey. I'm here with you." in out


def test_main_no_cursor_keeps_keywords_silent(monkeypatch, capsys):
    from plugins.lelamp import local_main

    monkeypatch.setenv("CURSOR_API_KEY", "crsr_fake_key")
    assert local_main.main(["--sim", "--no-wake", "--no-cursor", "--say", "nod"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "[sim] speak" not in out
    assert "Cursor agent ready" not in out


def test_main_sim_ask_without_key_is_local(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--ask", "warm light"]) == 0
    out = capsys.readouterr().out
    assert "rgb" in out
    assert "[sim] speak" not in out


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
    assert "Yeah." in lamp.apply(parse_line("nod"))
    lamp.apply(parse_line("lights off"))
    assert lamp.last_rgb == (0, 0, 0)


def test_voice_desk_modes_are_local_hardware():
    assert parse_line("study mode").kind == "scene"
    assert parse_line("study mode").payload["mood"] == "study"
    assert parse_line("reading mode").payload["pose"] == "read"
    assert parse_line("white light").kind == "mood"
    assert parse_line("white light").payload == "white"
    assert parse_line("yellow light").payload == "yellow"
    assert parse_line("look down").kind == "scene"
    assert parse_line("look down").payload["pose"] == "closer"
    assert parse_line("closer").payload["pose"] == "closer"
    assert parse_line("学习模式").payload["mood"] == "study"
    assert parse_line("黄光").payload == "yellow"
    assert parse_line("亮一点").kind == "brightness_delta"
    assert parse_line("look down").kind != "snap"
    assert hardware_spoken_command("study mode") == "study mode"
    assert hardware_spoken_command("reading mode") == "reading mode"
    assert hardware_spoken_command("closer") == "closer"

    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("study mode"))
    assert lamp.last_expression == "study"
    assert lamp.brightness == 100
    assert lamp.last_rgb == (255, 255, 255)

    lamp.apply(parse_line("reading mode"))
    assert lamp.last_expression == "read"
    assert lamp.brightness == 80

    lamp.apply(parse_line("yellow light"))
    assert lamp.base_rgb[0] == 255
    assert lamp.base_rgb[1] > lamp.base_rgb[2]

    lamp.apply(parse_line("closer"))
    assert lamp.last_expression == "closer"

    seen = []

    class Brain:
        def ask(self, text):
            seen.append(text)
            return "nope"

    assert apply_speech(lamp, "study mode", Brain()) == "scene"
    assert apply_speech(lamp, "lights off", Brain()) == "mood"
    assert seen == []
    assert lamp.last_rgb == (0, 0, 0)


def test_english_helpers():
    assert speech_lang("what day is it") == "en"
    assert speech_lang("q") == "en"
    assert wake_ack("hello lamp") == "Yeah?"
    assert utterance_too_short("uh") is True
    assert utterance_too_short("you good") is False
    assert utterance_too_short("yeah") is False
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
    piper = build_parser().parse_args(["--download-piper"])
    assert piper.download_piper is True


def test_main_sim_speak_english(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--speak", "hello I'm the lamp"]) == 0
    out = capsys.readouterr().out
    assert "[sim] speak hello I'm the lamp" in out
    assert "vosk(en)" in out


def test_piper_model_path_uses_env_file(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    missing = tmp_path / "missing.onnx"
    monkeypatch.setenv("LELAMP_PIPER_MODEL", str(missing))
    assert local_main.piper_model_path() is None
    model = tmp_path / "en_US-ryan-medium.onnx"
    model.write_bytes(b"fake-onnx")
    monkeypatch.setenv("LELAMP_PIPER_MODEL", str(model))
    assert local_main.piper_model_path() == model


def test_find_tts_engine_prefers_piper_when_model_exists(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    model = tmp_path / "en_US-ryan-medium.onnx"
    model.write_bytes(b"fake-onnx")
    monkeypatch.setenv("LELAMP_PIPER_MODEL", str(model))
    monkeypatch.delenv("LELAMP_TTS", raising=False)
    monkeypatch.setattr(local_main, "_piper_can_synth", lambda: True)
    assert local_main.find_tts_engine() == "piper"


def test_find_tts_engine_falls_back_to_espeak_without_piper(monkeypatch):
    from plugins.lelamp import local_main

    monkeypatch.delenv("LELAMP_TTS", raising=False)
    monkeypatch.delenv("LELAMP_PIPER_MODEL", raising=False)
    monkeypatch.setattr(local_main, "piper_model_path", lambda: None)

    def fake_which(name):
        if name == "espeak-ng":
            return "/usr/bin/espeak-ng"
        return None

    monkeypatch.setattr(local_main.shutil, "which", fake_which)
    monkeypatch.setattr(local_main, "_piper_can_synth", lambda: False)
    assert local_main.find_tts_engine() == "espeak-ng"


def test_download_piper_voice_fetches_onnx_and_json(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    dest = tmp_path / "en_US-ryan-medium.onnx"

    def fake_fetch(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if str(path).endswith(".json"):
            path.write_text('{"audio":{"sample_rate":22050}}', encoding="utf-8")
        else:
            path.write_bytes(b"x" * 1_500_000)

    monkeypatch.setattr(local_main, "_fetch_url", fake_fetch)
    got = local_main.download_piper_voice(dest)
    assert got == dest
    assert dest.is_file()
    assert dest.stat().st_size > 1_000_000
    assert Path(str(dest) + ".json").is_file()


def test_speak_text_uses_piper_when_selected(monkeypatch, tmp_path):
    from plugins.lelamp import local_main

    model = tmp_path / "voice.onnx"
    model.write_bytes(b"fake")
    monkeypatch.setenv("LELAMP_TTS", "piper")
    monkeypatch.setattr(local_main, "piper_model_path", lambda: model)
    monkeypatch.setattr(local_main, "_speak_piper", lambda text: "piper")
    assert speak_text("Hey. I'm here with you.", sim=False) == "piper"


def test_yeah_is_a_nod():
    cmd = parse_line("yeah")
    assert cmd.kind == "express"
    assert cmd.payload == "nod"


def test_listen_hold_default_is_snappy():
    from plugins.lelamp.local_main import build_parser

    args = build_parser().parse_args([])
    assert args.listen_hold == 0.45


def test_speech_catcher_holds_trailing_is():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    catcher = SpeechCatcher(hold_s=0.45, now=clock)
    catcher.note_final("what day is")
    assert catcher.take_ready() == ""
    catcher.note_final("it")
    assert catcher.take_ready() == "what day is it"


def test_incomplete_short_phrase_commits_under_a_second():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    catcher = SpeechCatcher(hold_s=0.45, now=clock)
    catcher.note_final("what")
    clock.t = 0.5
    assert catcher.take_ready() == ""
    clock.t = 0.75
    assert catcher.take_ready() == "what"


def test_vosk_drops_low_confidence_crumbs():
    from plugins.lelamp.local_main import _vosk_too_noisy

    noisy = {
        "text": "the",
        "result": [{"conf": 0.2, "word": "the"}],
    }
    assert _vosk_too_noisy(noisy, "the") is True
    clear = {
        "text": "what day is it",
        "result": [{"conf": 0.9, "word": "what"}],
    }
    assert _vosk_too_noisy(clear, "what day is it") is False


def test_piper_chunk_bytes_prefers_int16_property():
    from plugins.lelamp.local_main import _piper_chunk_bytes

    chunk = type("C", (), {"audio_int16_bytes": b"\x01\x00\x02\x00"})()
    assert _piper_chunk_bytes(chunk) == b"\x01\x00\x02\x00"


def test_speak_text_mutes_mic_while_playing(monkeypatch):
    from plugins.lelamp import local_main

    seen = []

    def fake_espeak(text, volume):
        seen.append(local_main._MIC_MUTE.is_set())
        return "espeak-ng"

    monkeypatch.setenv("LELAMP_TTS", "espeak-ng")
    monkeypatch.setattr(local_main, "find_tts_engine", lambda: "espeak-ng")
    monkeypatch.setattr(local_main, "_speak_espeak", fake_espeak)
    assert speak_text("Hey.", sim=False) == "espeak-ng"
    assert seen == [True]
    assert local_main._MIC_MUTE.is_set() is False


def test_conversation_play_returns_while_motion_runs():
    lamp = LocalLamp(
        sim=False,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    started = threading.Event()
    released = threading.Event()

    class Motors:
        robot = object()
        dispatched = []
        _current_event = None

        def _handle_play(self, recording_name):
            started.set()
            released.wait(timeout=2)

        def wait_until_idle(self, timeout=None):
            return True

    lamp.motors = Motors()
    t0 = time.monotonic()
    lamp._play("nod", wait=False)
    assert time.monotonic() - t0 < 0.2
    assert started.wait(timeout=1.0)
    assert lamp.last_expression == "nod"
    released.set()
    lamp._wait_hw(timeout=2.0)


def test_looks_like_look_phrases():
    assert looks_like_look("look") is True
    assert looks_like_look("what do you see") is True
    assert looks_like_look("can you see me") is True
    assert looks_like_look("warm light") is False
    assert looks_like_look("how are you") is False


def test_sim_snap_writes_a_still():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    path = lamp.snap()
    assert path is not None
    assert path.is_file()
    assert lamp.last_expression == "scanning"
    assert lamp.last_photo == str(path)
    assert "I'm looking." in lamp.apply(parse_line("snap"))


def test_look_tool_uses_sim_camera():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    out = execute_lamp_tool(lamp, "look", {})
    assert "Photo saved" in out
    assert lamp.last_expression == "scanning"


def test_what_do_you_see_snaps_locally():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    seen = []

    class Brain:
        def ask(self, text, photo=None):
            seen.append(text)
            return ""

    assert parse_line("what do you see").kind == "snap"
    assert parse_line("look").kind == "snap"
    assert apply_speech(lamp, "what do you see", Brain(), pose_chance=1.0) == "snap"
    assert seen == []
    assert lamp.last_expression == "scanning"
    assert lamp.last_spoken == ""


def test_what_do_you_see_snaps_without_a_model():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert apply_speech(lamp, "what do you see") == "snap"
    assert lamp.last_photo
    assert lamp.last_expression == "scanning"
    assert lamp.last_spoken == ""


def test_main_sim_snap(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--snap"]) == 0
    out = capsys.readouterr().out
    assert "[sim] camera snap" in out
    assert "camera=sim" in out


def test_music_command_is_local_hardware(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    assert parse_line("music").kind == "music"
    assert parse_line("dance").kind == "music"
    assert parse_line("play music").kind == "music"
    assert parse_line("stop music").kind == "music_stop"
    assert parse_line("turn off the music").kind == "music_stop"
    assert parse_line("放音乐").kind == "music"
    assert hardware_spoken_command("music") == "music"
    assert hardware_spoken_command("please play music") == "play music"
    assert hardware_spoken_command("stop music") == "stop music"
    assert hardware_spoken_command("nod") is None

    for name, bpm, notes in _BUILTIN_TRACKS:
        write_beat_wav(tmp_path / name, bpm=bpm, notes=notes, seconds=0.4)
    paths = ensure_builtin_music(tmp_path)
    assert paths
    assert all(path.is_file() and path.stat().st_size > 2000 for path in paths)

    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    out = lamp.apply(parse_line("music"))
    assert out.startswith("music ")
    assert lamp.last_music.endswith(".wav")
    assert lamp.last_expression == "happy_wiggle"
    assert lamp.music_playing is True
    printed = capsys.readouterr().out
    assert "music " in printed
    assert "bpm=" in printed

    seen = []

    class Brain:
        def ask(self, text):
            seen.append(text)
            return "nope"

    assert apply_speech(lamp, "music", Brain()) == "music"
    assert apply_speech(lamp, "how are you", Brain()) == "busy"
    assert seen == []
    assert lamp.music_playing is True

    assert apply_speech(lamp, "stop music", Brain()) == "music_stop"
    assert lamp.music_playing is False
    assert seen == []
