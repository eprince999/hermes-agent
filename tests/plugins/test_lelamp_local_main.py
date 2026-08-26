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
    assert extract_spoken_command("please lights off") == "lights off"
    assert extract_spoken_command("lights on") == "lights on"


def test_direct_spoken_command_keeps_full_sentences_for_the_model():
    assert direct_spoken_command("关灯") == "关灯"
    assert direct_spoken_command("请点头") == "点头"
    assert direct_spoken_command("帮我暖光") == "暖光"
    assert direct_spoken_command("lights off") == "lights off"
    assert direct_spoken_command("please nod") == "nod"
    assert direct_spoken_command("warm light") == "warm light"
    assert direct_spoken_command("Do you agree warm light is nicer") is None
    assert direct_spoken_command("这样用暖光看书你同意吗") is None
    assert direct_spoken_command("今天天气怎么样") is None


def test_resolve_feeling_agree_and_disagree():
    assert resolve_feeling("点头") == "nod"
    assert resolve_feeling("同意") == "nod"
    assert resolve_feeling("赞同") == "nod"
    assert resolve_feeling("摇头") == "headshake"
    assert resolve_feeling("不同意") == "headshake"
    assert resolve_feeling("做不到") == "headshake"


def test_express_tool_nods_on_agree_feeling():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    execute_lamp_tool(lamp, "express", {"feeling": "同意"})
    assert lamp.last_expression == "nod"
    execute_lamp_tool(lamp, "express", {"feeling": "不同意"})
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
            return "嗯，我同意。"

    result = apply_speech(lamp, "这样调成暖光你同意吗", Brain())
    assert result == "chat"
    assert seen == ["这样调成暖光你同意吗"]
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
            raise AssertionError("short 点头 should not call Cursor")

    apply_speech(lamp, "点头", Brain())
    assert lamp.last_expression == "nod"


def test_join_speech_glues_vosk_fragments():
    assert join_speech("今天", "几号 了") == "今天几号了"
    assert join_speech("把你", "把你差了吧") == "把你差了吧"
    assert join_speech("今天几号了") == "今天几号了"
    assert join_speech("what day", "is it") == "what day is it"
    assert join_speech("hello lamp") == "hello lamp"


def test_split_wake_and_complete_utterance():
    assert split_wake("你好台灯") == (True, "")
    assert split_wake("你好台灯今天几号了") == (True, "今天几号了")
    assert split_wake("hello lamp") == (True, "")
    assert split_wake("hello lamp what day is it") == (True, "what day is it")
    assert split_wake("hey lamp lights off") == (True, "lights off")
    assert split_wake("关灯") == (False, "关灯")
    assert split_wake("lights off") == (False, "lights off")
    assert looks_complete_utterance("关灯") is True
    assert looks_complete_utterance("今天") is False
    assert looks_complete_utterance("今天几号了") is True
    assert looks_complete_utterance("暖光看书更舒服你同意吗") is True
    assert looks_complete_utterance("what day is it") is True
    assert looks_complete_utterance("today") is False


def test_speech_catcher_waits_then_merges():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clock = Clock()
    catcher = SpeechCatcher(hold_s=0.9, now=clock)
    catcher.note_final("今天")
    assert catcher.take_ready() == ""
    clock.t = 0.5
    assert catcher.take_ready() == ""
    catcher.note_final("几号了")
    assert catcher.take_ready() == "今天几号了"


def test_speech_catcher_flushes_local_command_immediately():
    class Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    catcher = SpeechCatcher(hold_s=2.0, now=Clock())
    catcher.note_final("关灯")
    assert catcher.take_ready() == "关灯"


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
            raise AssertionError("listen 你好 should not call Cursor")

    apply_speech(lamp, "你好", Brain(), listen_mode=True)
    assert lamp.last_expression != "wake_up"
    apply_speech(lamp, "hello", Brain(), listen_mode=True)
    assert lamp.last_expression != "wake_up"
    assert lamp.last_spoken == "I'm here."


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


def test_volume_keywords_and_status():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert lamp.volume == 100
    assert parse_line("大声").kind == "volume_delta"
    lamp.apply(parse_line("大声"))
    assert lamp.volume == 100
    lamp.apply(parse_line("volume 40"))
    assert lamp.volume == 40
    assert "volume=40" in lamp.apply(parse_line("status"))


def test_espeak_amplitude_is_loud():
    from plugins.lelamp.local_main import _espeak_cmd

    cmd = _espeak_cmd("/usr/bin/espeak-ng", "你好", 100)
    assert "-a" in cmd
    assert int(cmd[cmd.index("-a") + 1]) == 200


def test_speak_text_sim_prints(capsys):
    assert speak_text("你好，我是台灯", sim=True) == "sim"
    assert "[sim] speak 你好，我是台灯" in capsys.readouterr().out
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
    assert speak_text("点头", sim=False, volume=80) == "espeak-ng"
    assert calls[0][0] == "/usr/bin/espeak-ng"
    assert "-v" in calls[0]
    assert "点头" in calls[0]


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
    monkeypatch.delenv("LELAMP_ESPEAK_VOICE_EN", raising=False)
    monkeypatch.setattr(local_main.shutil, "which", fake_which)
    monkeypatch.setattr(local_main.subprocess, "run", fake_run)
    assert speak_text("Hello, I am the lamp.", sim=False, volume=100) == "espeak-ng"
    assert calls[0][calls[0].index("-v") + 1] == "en"


def test_main_sim_speak_without_cursor(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--speak", "你好，我是台灯"]) == 0
    out = capsys.readouterr().out
    assert "stage 3" in out
    assert "[sim] speak 你好，我是台灯" in out


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
    assert "Okay." in lamp.apply(parse_line("nod"))
    lamp.apply(parse_line("lights off"))
    assert lamp.last_rgb == (0, 0, 0)


def test_bilingual_helpers():
    assert speech_lang("今天几号了") == "zh"
    assert speech_lang("what day is it") == "en"
    assert wake_ack("你好台灯") == "我在。"
    assert wake_ack("hello lamp") == "I'm here."
    assert utterance_too_short("今天") is True
    assert utterance_too_short("hi") is True
    assert utterance_too_short("what day is it") is False
    assert pick_asr(("final", "今"), ("final", "what day is it"))[1] == "what day is it"
    assert pick_asr(("final", "今天几号了"), ("final", "ah"))[1] == "今天几号了"


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


def test_download_vosk_flag_is_bilingual():
    from plugins.lelamp.local_main import build_parser

    help_text = build_parser().format_help()
    assert "English" in help_text or "zh+en" in help_text
    args = build_parser().parse_args(["--download-vosk"])
    assert args.download_vosk is True


def test_main_sim_speak_english(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--speak", "hello I'm the lamp"]) == 0
    out = capsys.readouterr().out
    assert "[sim] speak hello I'm the lamp" in out
    assert "vosk(zh+en)" in out
