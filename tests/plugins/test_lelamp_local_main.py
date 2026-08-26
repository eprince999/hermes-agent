"""Stage 2 local lamp: Chinese keywords plus a music folder."""

from __future__ import annotations

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
    unknown = parse_line("do a dance")
    assert unknown.kind == "unknown"


def test_brightness_and_rgb_parse():
    delta = parse_line("亮一点")
    assert delta.kind == "brightness_delta"
    assert delta.payload == 15
    rgb = parse_line("rgb 10 20 30")
    assert rgb.kind == "rgb"
    assert rgb.payload == (10, 20, 30)


def test_sim_express_then_off_updates_state():
    lamp = LocalLamp(
        sim=True,
        port="/dev/null",
        lamp_id="lelamp",
        led_count=64,
        brightness=70,
    )
    assert "好的。" in lamp.apply(parse_line("点头"))
    lamp.apply(parse_line("关灯"))
    assert lamp.last_rgb == (0, 0, 0)


def test_extract_spoken_command_from_padded_asr():
    assert extract_spoken_command("请关灯") == "关灯"
    assert extract_spoken_command("嗯点头") == "点头"
    assert extract_spoken_command("放音乐吧") == "放音乐"


def test_show_stage_prints_current_stage(capsys):
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE == 2
    assert local_main.main(["--show-stage"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("2")


def test_snapshot_saves_current_stage_copy(tmp_path, capsys):
    from plugins.lelamp import local_main

    dest = local_main.snapshot_current(dest_dir=tmp_path)
    assert dest.name == "stage2.py"
    assert dest.is_file()
    assert "AGENT_STAGE = 2" in dest.read_text(encoding="utf-8")
    assert "saved snapshot" in capsys.readouterr().out


def test_tracked_stage2_archive_has_no_music_player():
    root = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "lelamp"
        / "lamp_snapshots"
    )
    stage2 = (root / "stage2.py").read_text(encoding="utf-8")
    assert "AGENT_STAGE = 2" in stage2
    assert "def play_music" not in stage2
    assert not (root / "stage3.py").is_file()
    assert not (root / "stage4.py").is_file()


def test_main_sim_say_phrases_without_repl(capsys):
    from plugins.lelamp import local_main

    assert local_main.main(["--sim", "--no-wake", "--say", "点头", "--say", "关灯"]) == 0
    out = capsys.readouterr().out
    assert "play nod" in out
    assert "rgb (0, 0, 0)" in out


def test_english_keywords_still_work():
    assert parse_line("nod").kind == "express"
    assert parse_line("nod").payload == "nod"
    assert parse_line("off").kind == "mood"


def test_music_command_plays_from_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LELAMP_MUSIC_DIR", str(tmp_path))
    assert parse_line("音乐").kind == "music"
    assert parse_line("放音乐").kind == "music"
    assert parse_line("music").kind == "music"
    assert parse_line("停止音乐").kind == "music_stop"
    assert extract_spoken_command("请放音乐") == "放音乐"

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
    assert lamp.last_expression == "happy_wiggle"
    assert lamp.music_playing is True
    printed = capsys.readouterr().out
    assert "bpm=" in printed

    assert apply_speech(lamp, "今天天气") == "busy"
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
