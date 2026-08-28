"""Stage 2 local lamp: Chinese Vosk keywords plus a music folder."""

from __future__ import annotations

import time
from pathlib import Path

from plugins.lelamp.local_main import (
    LocalLamp,
    apply_speech,
    ensure_builtin_music,
    ensure_music_dir,
    extract_spoken_command,
    list_music_files,
    parse_line,
    pick_random_track,
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


def test_stage4_keeps_chinese_vosk():
    source = _source()
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE == 4
    assert local_main.VOSK_MODEL_NAME == "vosk-model-small-cn-0.22"
    assert "vosk-model-small-en" not in source


def test_hello_is_wake_up_not_a_light_command():
    cmd = parse_line("你好")
    assert cmd.kind == "express"
    assert cmd.payload == "wake_up"


def test_watch_me_is_not_scanning_or_nod():
    cmd = parse_line("看我")
    assert cmd.kind == "watch_person"
    assert cmd.payload == 0.0
    assert parse_line("看着我").kind == "watch_person"
    assert parse_line("看过来").kind == "watch_person"
    assert parse_line("点头").kind == "express"
    assert parse_line("点头").payload == "nod"
    scanning = parse_line("张望")
    assert scanning.kind == "express"
    assert scanning.payload == "scanning"
    spoken = extract_spoken_command("请看我")
    assert spoken
    assert parse_line(spoken).kind == "watch_person"


def test_import_run_watch_person_skips_six_second_hook(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "agent_hook.py").write_text(
        "def run_watch_person(model, meta, port, seconds=6.0):\n    return 'old'\n",
        encoding="utf-8",
    )
    (new / "agent_hook.py").write_text(
        "WATCH_REVISION = 'new'\n"
        "def run_watch_person(model, meta, port, seconds=0.0, stop_event=None):\n"
        "    return 'new'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(local_main, "resolve_look_at_artifacts", lambda: (old, None, None))
    monkeypatch.setattr(local_main, "look_at_search_roots", lambda: [new])
    fn = local_main._import_run_watch_person()
    assert fn(None, None, None) == "new"


def test_import_run_watch_person_errors_if_only_six_second_hook(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    old = tmp_path / "old"
    old.mkdir()
    (old / "agent_hook.py").write_text(
        "def run_watch_person(model, meta, port, seconds=6.0):\n    return 'old'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(local_main, "resolve_look_at_artifacts", lambda: (old, None, None))
    monkeypatch.setattr(local_main, "look_at_search_roots", lambda: [])
    try:
        local_main._import_run_watch_person()
    except ImportError as exc:
        assert "6 秒" in str(exc)
    else:
        raise AssertionError("expected ImportError for old 6-second hook")


def test_look_at_search_roots_follow_sudo_user(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    class Pw:
        pw_dir = str(tmp_path / "spocklamp")

    monkeypatch.setenv("SUDO_USER", "spocklamp")
    monkeypatch.setattr(local_main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(local_main.pwd, "getpwnam", lambda name: Pw())
    roots = [str(p) for p in local_main.look_at_search_roots()]
    assert str(tmp_path / "spocklamp" / "hermes-agent" / "lelamp_il") in roots


def test_resolve_look_at_artifacts_via_env(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    il = tmp_path / "lelamp_il"
    art = il / "artifacts"
    art.mkdir(parents=True)
    (art / "tiny_lamp_int8.onnx").write_bytes(b"onnx")
    (art / "meta.json").write_text("{}")
    monkeypatch.setenv("LELAMP_IL_DIR", str(il))
    root, model, meta = local_main.resolve_look_at_artifacts()
    assert root == il
    assert model == art / "tiny_lamp_int8.onnx"
    assert meta == art / "meta.json"


def test_sim_watch_person_does_not_play_a_recording():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    out = lamp.apply(parse_line("看我"))
    assert "看着你" in out
    assert lamp.last_expression == "watch_person"
    assert lamp.last_rgb != (0, 0, 0)
    assert lamp.watching


def test_sim_watch_person_follows_until_spoken_stop():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("看我"))
    assert lamp.watching
    assert parse_line("停").kind == "unknown"
    assert parse_line("好了").kind == "unknown"
    assert parse_line("别看了").kind == "watch_stop"
    assert parse_line("停止音乐").kind == "music_stop"
    assert apply_speech(lamp, "停") == "watch_stop"
    assert not lamp.watching


def test_sim_watch_person_stops_when_nodding():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("看我"))
    assert lamp.watching
    out = lamp.apply(parse_line("点头"))
    assert "好的" in out
    assert not lamp.watching
    assert lamp.last_expression == "nod"


def test_extract_stop_watching_phrase():
    spoken = extract_spoken_command("请别看了")
    assert spoken
    assert parse_line(spoken).kind == "watch_stop"


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
    unknown = parse_line("今天天气怎么样")
    assert unknown.kind == "unknown"


def test_brightness_and_rgb_parse():
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


def test_sim_wake_fades_from_black_to_80_at_motion_tail():
    from plugins.lelamp.local_main import WAKE_BRIGHTNESS, _scale_rgb

    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.wake()
    assert lamp.last_expression == "wake_up"
    assert lamp.brightness == WAKE_BRIGHTNESS == 80
    assert lamp.last_rgb == _scale_rgb(lamp.base_rgb, 80)


def test_wake_blackout_covers_all_but_the_tail():
    from plugins.lelamp.local_main import wake_blackout_seconds

    assert wake_blackout_seconds(3.5, 1.5) == 2.0
    assert wake_blackout_seconds(1.0, 1.5) == 0.0


def test_recording_duration_reads_csv(tmp_path, monkeypatch):
    from plugins.lelamp import local_main

    rec = tmp_path / "lelamp_runtime" / "lelamp" / "recordings"
    rec.mkdir(parents=True)
    (rec / "wake_up.csv").write_text(
        "t,a\n" + "\n".join(f"{i},0" for i in range(90)),
        encoding="utf-8",
    )
    monkeypatch.setattr(local_main, "_effective_home", lambda: tmp_path)
    assert local_main.recording_duration_seconds("wake_up", fps=30.0) == 3.0


def test_main_sim_scripted_session(monkeypatch, capsys):
    from plugins.lelamp import local_main

    lines = iter(["点头", "status", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(lines))
    assert local_main.main(["--sim", "--no-wake"]) == 0
    out = capsys.readouterr().out
    assert "好的。" in out
    assert "expression=nod" in out
    assert "好，我先歇着。" in out
    assert "music 文件夹" in out


def test_boot_service_unit_wakes_and_listens(tmp_path):
    from plugins.lelamp import local_main

    unit = tmp_path / "lelamp-local.service"
    dest = local_main.install_boot_service(
        runtime_dir=tmp_path / "runtime",
        unit_path=unit,
        enable=False,
    )
    text = dest.read_text(encoding="utf-8")
    assert dest == unit
    assert "$" not in text
    assert "$(" not in text
    assert "ExecStartPre" not in text
    assert "sound.target" not in text
    assert "StartLimitIntervalSec=0" in text
    assert "WantedBy=multi-user.target" in text
    assert local_main.BOOT_REVISION in text
    assert "lelamp-local-run.sh" in text
    wrapper = tmp_path / "runtime" / "lelamp-local-run.sh"
    assert wrapper.is_file()
    body = wrapper.read_text(encoding="utf-8")
    assert "--listen" in body
    assert "ttyACM0" in body
    assert "LELAMP_LISTEN=1" in body
    copied = tmp_path / "runtime" / "local_main.py"
    assert copied.is_file()
    copied_text = copied.read_text(encoding="utf-8")
    assert 'WATCH_REVISION = "2026-08-28-follow"' in copied_text
    assert 'BOOT_REVISION = "2026-08-28-boot2"' in copied_text


def test_wait_for_serial_port_finds_existing_node(tmp_path):
    from plugins.lelamp.local_main import wait_for_serial_port

    port = tmp_path / "ttyACM0"
    port.write_text("", encoding="utf-8")
    assert wait_for_serial_port(str(port), timeout=1.0) is True


def test_wait_for_serial_port_times_out(tmp_path):
    from plugins.lelamp.local_main import wait_for_serial_port

    missing = tmp_path / "missing-acm"
    t0 = time.monotonic()
    assert wait_for_serial_port(str(missing), timeout=0.25) is False
    assert time.monotonic() - t0 < 1.5


def test_wake_failure_still_listens(monkeypatch):
    from plugins.lelamp import local_main

    seen = {}

    def boom(_self):
        raise RuntimeError("rgb down")

    def fake_listen(lamp, *, device, model_path):
        seen["listen"] = True
        return 0

    monkeypatch.setattr(local_main.LocalLamp, "wake", boom)
    monkeypatch.setattr(local_main, "run_listen_loop", fake_listen)
    monkeypatch.setenv("LELAMP_LISTEN", "1")
    assert local_main.main(["--sim"]) == 0
    assert seen.get("listen") is True


def test_lelamp_listen_env_skips_repl(monkeypatch):
    from plugins.lelamp import local_main

    seen = {}

    def fake_listen(lamp, *, device, model_path):
        seen["listen"] = True
        return 0

    monkeypatch.setenv("LELAMP_LISTEN", "1")
    monkeypatch.setattr(local_main, "run_listen_loop", fake_listen)
    assert local_main.main(["--sim", "--no-wake"]) == 0
    assert seen.get("listen") is True


def test_repl_flag_overrides_listen_env(monkeypatch):
    from plugins.lelamp import local_main

    monkeypatch.setenv("LELAMP_LISTEN", "1")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "q")
    monkeypatch.setattr(
        local_main,
        "run_listen_loop",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not listen")),
    )
    assert local_main.main(["--sim", "--no-wake", "--repl"]) == 0


def test_extract_spoken_command_from_padded_asr():
    assert extract_spoken_command("请关灯") == "关灯"
    assert extract_spoken_command("你 好 呀") == "你好"
    assert extract_spoken_command("今天天气怎么样") is None
    assert extract_spoken_command("不要关灯") == "不要"
    assert extract_spoken_command("请点头") == "点头"
    spoken = extract_spoken_command("放音乐吧")
    assert spoken
    assert parse_line(spoken).kind == "music"


def test_bare_music_word_is_a_command():
    assert parse_line("音乐").kind == "music"
    assert parse_line("音 乐").kind == "music"
    assert parse_line("听音乐").kind == "music"
    assert parse_line("停止音乐").kind == "music_stop"
    assert extract_spoken_command("音乐") == "音乐"
    assert extract_spoken_command("音 乐") == "音乐"
    assert extract_spoken_command("下一首") == "下一首"
    assert parse_line("大点声").kind == "volume_delta"
    assert parse_line("小点声").kind == "volume_delta"
    assert parse_line("循环播放").kind == "music_loop"
    assert parse_line("循环播放").payload == "all"
    assert parse_line("单曲循环").kind == "music_loop"
    assert parse_line("单曲循环").payload == "one"


def test_show_stage_prints_current_stage(capsys):
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE >= 1
    assert local_main.main(["--show-stage"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(local_main.AGENT_STAGE))


def test_snapshot_saves_stage4_copy(tmp_path, capsys):
    from plugins.lelamp import local_main

    dest = local_main.snapshot_current("stage4", dest_dir=tmp_path)
    assert dest.name == "stage4.py"
    assert dest.is_file()
    assert f"AGENT_STAGE = {local_main.AGENT_STAGE}" in dest.read_text(encoding="utf-8")
    assert "saved snapshot" in capsys.readouterr().out
    args = local_main.build_parser().parse_args(["--snapshot"])
    assert args.snapshot == ""
    args = local_main.build_parser().parse_args(["--snapshot", "stage4"])
    assert args.snapshot == "stage4"


def test_tracked_stage2_archive_has_no_music_player():
    root = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "lelamp"
        / "lamp_snapshots"
    )
    stage2 = (root / "stage2.py").read_text(encoding="utf-8")
    assert "AGENT_STAGE = 2" in stage2
    assert "vosk-model-small-cn-0.22" in stage2
    assert "def play_music" not in stage2
    stage3 = (root / "stage3.py").read_text(encoding="utf-8")
    stage4 = (root / "stage4.py").read_text(encoding="utf-8")
    runnable = (root.parent / "local_main.py").read_text(encoding="utf-8")
    assert "AGENT_STAGE = 3" in stage3
    assert "def play_music" in stage3
    assert "vosk-model-small-cn-0.22" in stage3
    assert "def _start_player" in stage3
    assert "watch_person" not in stage3
    assert "AGENT_STAGE = 4" in runnable
    assert "看我" in runnable
    assert "def watch_person" in runnable
    assert stage4 == runnable
    assert stage3 != runnable
    assert not (root / "stage5.py").is_file()
    dropin = (
        Path(__file__).resolve().parents[2] / "lelamp_runtime" / "local_main.py"
    )
    assert dropin.read_text(encoding="utf-8") == runnable
    installer = root.parent / "install_on_lamp.sh"
    script = installer.read_text(encoding="utf-8")
    assert "2026-08-28-follow" in script
    assert "2026-08-28-boot2" in script
    assert 'DEST="$DEST_DIR/local_main.py"' in script


def test_main_sim_say_phrases_without_repl(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--say", "请点头", "--say", "关灯"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "rgb (0, 0, 0)" in out
    assert "关灯。" in out


def test_music_command_plays_from_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    assert parse_line("音乐").kind == "music"
    assert parse_line("放音乐").kind == "music"
    assert parse_line("停止音乐").kind == "music_stop"
    spoken = extract_spoken_command("请放音乐")
    assert spoken
    assert parse_line(spoken).kind == "music"

    song = tmp_path / "desk_tune_96.wav"
    write_beat_wav(song, bpm=96, notes=(0, 4, 7), seconds=0.3)
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    out = lamp.apply(parse_line("音乐"))
    assert out.startswith("music ")
    assert lamp.last_music == "desk_tune_96.wav"
    assert lamp.last_expression != "happy_wiggle"
    assert lamp.music_playing is True
    printed = capsys.readouterr().out
    assert "bpm=" in printed
    assert lamp.last_rgb != (0, 0, 0)
    assert lamp.mic_hold.is_set() is False

    assert apply_speech(lamp, "今天天气") == "unknown"
    assert apply_speech(lamp, "点头") == "busy"
    assert apply_speech(lamp, "下一首") in {"music_next", "music"}
    assert apply_speech(lamp, "大点声") == "volume_delta"
    assert apply_speech(lamp, "停止音乐") == "music_stop"
    assert lamp.music_playing is False


def test_music_folder_plays_user_files_not_builtins(tmp_path, monkeypatch):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    folder = ensure_music_dir()
    assert folder == tmp_path
    song = tmp_path / "desk_tune_96.wav"
    write_beat_wav(song, bpm=96, notes=(0, 4, 7), seconds=0.3)
    ensure_builtin_music(tmp_path / ".builtin")
    assert list_music_files(tmp_path) == [song]
    path, bpm = pick_random_track()
    assert path == song
    assert bpm == 96

    empty = tmp_path / "empty"
    empty.mkdir()
    fallback, _bpm = pick_random_track(empty)
    assert fallback.parent.name == ".builtin"
    assert list_music_files(empty) == []
    assert _BUILTIN_TRACKS


def test_mp3_player_commands_include_mpg123_and_ffmpeg(monkeypatch):
    from plugins.lelamp import local_main

    def fake_bin(name: str):
        known = {
            "aplay": "/usr/bin/aplay",
            "mpg123": "/usr/bin/mpg123",
            "ffmpeg": "/usr/bin/ffmpeg",
        }
        return known.get(name)

    monkeypatch.setattr(local_main, "_bin", fake_bin)
    commands = local_main.music_player_commands(Path("rain.mp3"), device="plughw:0,0")
    joined = [" ".join(cmd) for cmd in commands]
    assert any(line.startswith("/usr/bin/mpg123") for line in joined)
    assert any(line.startswith("/usr/bin/ffmpeg") for line in joined)
    assert not any(line.startswith("/usr/bin/aplay") for line in joined)
    mpg = next(line for line in joined if line.startswith("/usr/bin/mpg123"))
    assert "-o alsa" in mpg
    assert "-a plughw:0,0" in mpg


def test_mp3_player_commands_work_with_only_mpg123(monkeypatch):
    from plugins.lelamp import local_main

    monkeypatch.setattr(local_main, "_bin", lambda name: "/usr/bin/mpg123" if name == "mpg123" else None)
    commands = local_main.music_player_commands(Path("rain.mp3"), device="plughw:0,0")
    assert commands
    assert commands[0][0].endswith("mpg123")
    assert "ffmpeg" not in " ".join(commands[0])


def test_install_hint_is_mpg123_not_ffmpeg():
    source = _source()
    assert "sudo apt install -y mpg123 ffmpeg" not in source
    assert "sudo apt update" in source
    assert "sudo apt install -y mpg123" in source


def test_default_audio_is_seeed_not_bluetooth():
    from plugins.lelamp import local_main

    assert local_main.AUDIO_PREFER == "seeed"
    args = local_main.build_parser().parse_args([])
    assert args.audio == "seeed"


def test_choose_playback_seeed_skips_bluetooth(monkeypatch):
    from plugins.lelamp import local_main

    monkeypatch.setattr(
        local_main,
        "find_bluetooth_playback",
        lambda: (_ for _ in ()).throw(AssertionError("seeed must not probe bluetooth")),
    )
    monkeypatch.setattr(local_main, "find_alsa_playback", lambda: ("plughw:0,0", "0"))
    monkeypatch.setattr(local_main, "unmute_alsa_card", lambda card: None)
    device, card, backend = local_main.choose_playback(prefer="seeed")
    assert (device, card, backend) == ("plughw:0,0", "0", "alsa")


def test_next_song_command_skips_current_track(tmp_path, monkeypatch):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    first = tmp_path / "a_100.wav"
    second = tmp_path / "b_120.wav"
    write_beat_wav(first, bpm=100, notes=(0, 4, 7), seconds=0.2)
    write_beat_wav(second, bpm=120, notes=(0, 4, 7), seconds=0.2)
    monkeypatch.setattr("plugins.lelamp.local_main.random.shuffle", lambda items: items.sort(key=lambda p: p.name))

    assert parse_line("下一首").kind == "music_next"
    assert parse_line("换一首").kind == "music_next"
    spoken = extract_spoken_command("请下一首")
    assert spoken
    assert parse_line(spoken).kind == "music_next"
    assert parse_line("下一首").kind != "music"

    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("音乐"))
    assert lamp.last_music == "a_100.wav"
    assert apply_speech(lamp, "下一首") == "music_next"
    assert lamp.last_music == "b_120.wav"
    assert lamp.music_playing is True


def test_volume_and_loop_commands_work_while_playing(tmp_path, monkeypatch):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    song = tmp_path / "desk_tune_96.wav"
    write_beat_wav(song, bpm=96, notes=(0, 4, 7), seconds=0.2)
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("音乐"))
    assert lamp.music_volume == 85
    assert lamp._loop_mode == "all"
    assert apply_speech(lamp, "大点声") == "volume_delta"
    assert lamp.music_volume == 100
    assert apply_speech(lamp, "小点声") == "volume_delta"
    assert lamp.music_volume == 85
    assert apply_speech(lamp, "单曲循环") == "music_loop"
    assert lamp._loop_mode == "one"
    assert apply_speech(lamp, "循环播放") == "music_loop"
    assert lamp._loop_mode == "all"
    assert lamp.music_playing is True
    spoken = extract_spoken_command("请大点声")
    assert spoken
    assert parse_line(spoken).kind == "volume_delta"


def test_continue_after_track_respects_loop_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    first = tmp_path / "a_100.wav"
    second = tmp_path / "b_120.wav"
    write_beat_wav(first, bpm=100, notes=(0, 4, 7), seconds=0.2)
    write_beat_wav(second, bpm=120, notes=(0, 4, 7), seconds=0.2)
    monkeypatch.setattr("plugins.lelamp.local_main.random.shuffle", lambda items: items.sort(key=lambda p: p.name))
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.apply(parse_line("音乐"))
    lamp.set_loop_mode("one")
    same, bpm = lamp._continue_after_track()
    assert same.name == "a_100.wav"
    assert bpm == 100
    lamp.set_loop_mode("all")
    nxt, nxt_bpm = lamp._continue_after_track()
    assert nxt.name == "b_120.wav"
    assert nxt_bpm == 120


def test_music_viz_warm_highs_cool_lows():
    from plugins.lelamp.local_main import music_viz_color

    bass = music_viz_color(t=0.0, bpm=120, hue_shift=0.2)
    treble = music_viz_color(t=0.25, bpm=120, hue_shift=0.2)
    assert (bass[0] - bass[2]) < (treble[0] - treble[2])


def test_music_visualizer_does_not_play_recordings():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    lamp.last_expression = "wake_up"
    from plugins.lelamp.local_main import music_viz_color

    lamp._paint_rgb(music_viz_color(t=0.0, bpm=100, hue_shift=0.4), quiet=True)
    assert lamp.last_expression == "wake_up"
    assert lamp.last_rgb != (0, 0, 0)


def test_downmix_stereo_pcm_to_mono():
    from plugins.lelamp.local_main import _downmix_pcm16
    import array

    stereo = array.array("h", [1000, 3000, -2000, 2000])
    mono = array.array("h")
    mono.frombytes(_downmix_pcm16(stereo.tobytes(), 2))
    assert list(mono) == [2000, 0]
    assert _downmix_pcm16(b"\x01\x00", 1) == b"\x01\x00"


def test_parse_seeed_aplay_listing():
    from plugins.lelamp.local_main import parse_alsa_playback

    listing = (
        "**** List of PLAYBACK Hardware Devices ****\n"
        "card 0: seeed2micvoicec [seeed-2mic-voicecard], device 0: "
        "bcm2835-i2s-tlv320aic3x-hifi tlv320aic3x-hifi-0 []\n"
        "  Subdevices: 1/1\n"
    )
    device, card = parse_alsa_playback(listing)
    assert card == "0"
    assert device == "plughw:0,0"


def test_alsa_playback_skips_hdmi_and_vc4():
    from plugins.lelamp.local_main import parse_alsa_playback

    listing = (
        "**** List of PLAYBACK Hardware Devices ****\n"
        "card 0: seeed2micvoicec [seeed-2mic-voicecard], device 0: "
        "bcm2835-i2s-tlv320aic3x-hifi tlv320aic3x-hifi-0 []\n"
        "  Subdevices: 1/1\n"
        "card 1: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [i2s-hifi-0]\n"
        "  Subdevices: 1/1\n"
    )
    device, card = parse_alsa_playback(listing)
    assert card == "0"
    assert device == "plughw:0,0"
    hdmi_only = (
        "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 []\n"
        "card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 []\n"
    )
    assert parse_alsa_playback(hdmi_only) == (None, None)


def test_parse_bluetooth_sinks_and_devices():
    from plugins.lelamp.local_main import (
        parse_bluealsa_pcms,
        parse_bluetoothctl_devices,
        parse_pactl_bluez_sinks,
    )

    devices = parse_bluetoothctl_devices(
        "Device AA:BB:CC:DD:EE:FF JBL Flip 5\n"
        "Device 11:22:33:44:55:66 Headphones\n"
        "Controller 00:11:22:33:44:55 raspberrypi\n"
    )
    assert devices[0] == ("AA:BB:CC:DD:EE:FF", "JBL Flip 5")
    assert devices[1][0] == "11:22:33:44:55:66"

    sinks = parse_pactl_bluez_sinks(
        "1\talsa_output.platform-vc4-hdmi.stereo\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED\n"
        "2\tbluez_output.AA_BB_CC_DD_EE_FF.a2dp_sink\tmodule-bluez5-device.c\ts16le 2ch 44100Hz\tIDLE\n"
    )
    assert len(sinks) == 1
    assert "bluez_output" in sinks[0][0]
    assert "hdmi" not in sinks[0][0].lower()

    pcms = parse_bluealsa_pcms(
        "null\nbluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp\ndefault\n"
    )
    assert pcms == ["bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"]
