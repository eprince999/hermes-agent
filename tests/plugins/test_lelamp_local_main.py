"""Stage 2 local lamp: Chinese Vosk keywords plus a music folder."""

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


def test_stage2_keeps_chinese_vosk():
    source = _source()
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE == 2
    assert local_main.VOSK_MODEL_NAME == "vosk-model-small-cn-0.22"
    assert "vosk-model-small-en" not in source


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


def test_show_stage_prints_current_stage(capsys):
    from plugins.lelamp import local_main

    assert local_main.AGENT_STAGE >= 1
    assert local_main.main(["--show-stage"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith(str(local_main.AGENT_STAGE))


def test_snapshot_saves_stage2_copy(tmp_path, capsys):
    from plugins.lelamp import local_main

    dest = local_main.snapshot_current("stage2", dest_dir=tmp_path)
    assert dest.name == "stage2.py"
    assert dest.is_file()
    assert "AGENT_STAGE = 2" in dest.read_text(encoding="utf-8")
    assert "saved snapshot" in capsys.readouterr().out
    args = local_main.build_parser().parse_args(["--snapshot"])
    assert args.snapshot == ""
    args = local_main.build_parser().parse_args(["--snapshot", "stage2"])
    assert args.snapshot == "stage2"


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
    assert not (root / "stage3.py").is_file()
    assert not (root / "stage4.py").is_file()


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

    assert apply_speech(lamp, "今天天气") == "busy"
    assert apply_speech(lamp, "停止音乐") == "music_stop"
    assert lamp.music_playing is False

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
