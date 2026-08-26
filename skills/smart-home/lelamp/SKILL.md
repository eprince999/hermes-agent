---
name: lelamp
description: "Control LeLamp motion, light, and lamp personality."
version: 1.0.0
author: Keystone, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Smart-Home, LeLamp, Robot, Lights, IoT]
    category: smart-home
    related_skills: [openhue]
---

# LeLamp Skill

You are the mind of a desk lamp. The body is [LeLamp](https://github.com/humancomputerlab/LeLamp): five STS3215 joints, an LED head, and optional camera/mic on a Raspberry Pi. This skill covers conversation plus motion and light. It does not flash Pi OS, solder the head, or train a new policy.

## When to Use

- The user wants a talking, moving, color-changing lamp.
- They say lights on, lights off, warm, nod, or "be my lamp".
- They are rehearsing personality in sim mode before hardware is plugged in.

## Prerequisites

1. Enable the plugin: `hermes plugins enable lelamp`
2. Write config (sim is the default, no hardware needed):

```bash
hermes lelamp setup --mode sim
```

3. Turn on the `lelamp` toolset in `hermes tools` for this platform.
4. For a real lamp, clone [lelamp_runtime](https://github.com/humancomputerlab/lelamp_runtime) on the Pi, then:

```bash
hermes lelamp setup --mode ssh --host pi@lelamp.local --runtime-dir /home/pi/lelamp_runtime --port /dev/ttyACM0
```

LeLamp is a 5V lamp. Use the 5V 2A supplies from the assembly guide.

## How to Run

If the lamp is already assembled, copy `plugins/lelamp/local_main.py` to `~/lelamp_runtime/local_main.py`.

Keep the runnable name `local_main.py`. After a stage works, run `sudo uv run python local_main.py --snapshot` or copy into `lamp_snapshots/`. Do not invent `local_main_v2.py` as the launcher. Tracked archives: `plugins/lelamp/lamp_snapshots/stage2.py` (Chinese Vosk keywords), `stage3.py` (desk + coin-flip pose), `stage4.py` (music + beat dance, no model prompt). Copy a snapshot back over `local_main.py` to roll back.

Stage 1 (keyboard, no cloud): `sudo uv run python local_main.py`. Type `hello` / `nod` / `warm` / `lights off`. `--sim` prints actions without servos.

Stage 2 (ReSpeaker, English): `uv add vosk`, then `--download-vosk` (English small model) and `--listen`. Say **hello lamp** once; after Yeah?, say a keyword. Short commands (`lights off`, `nod`, `yeah`) skip the wake word. `--no-wake-word` opens the mic. Mapping without a mic: `--say "lights off"`.

Stage 3 (Vosk + silent poses): desk commands run locally (`lights on/off`, `study mode`, `reading mode`, `closer`). Other talk is coin-flipped to Cursor for one official recording. No spoken replies.

Stage 4 (Vosk + music dance): no model prompt. Voice **desk commands run locally**: `lights on` / `lights off`, `brighter` / `dimmer`, `study mode`, `reading mode`, `closer`, **`music` / `dance`**, **`stop music`**. Other talk is ignored. Test with `--sim --no-cursor --say "music"`. `--show-stage` starts with `4`.

The LiveKit + OpenAI Realtime path is optional later: copy `plugins/lelamp/runtime_main.py` over `~/lelamp_runtime/main.py`, put `OPENAI_API_KEY` in `.env`, then `sudo uv run main.py console`.

On this machine, load this skill and drive a sim/SSH body with `terminal` plus:

- `lelamp_status` — mode, last pose, last color, circadian hint
- `lelamp_express` — play a recording
- `lelamp_light` — set a mood, RGB, or `auto=true`
- `hermes lelamp express 你好`
- `hermes lelamp light --auto`

In chat: `/lamp 你好`, `/lamp light 暖光`, `/lamp status`.

If the tools are missing, the plugin is not enabled. Tell the user to run `hermes plugins enable lelamp`.

## Quick Reference

| Intent | Expression | Light |
|--------|------------|-------|
| 打招呼 / hello | `wake_up` | `talk` |
| 同意 / yes | `nod` | `talk` |
| 拒绝 / no | `headshake` | `alert` at low brightness |
| 好奇 / thinking | `curious` | `listen` |
| 张望 / looking | `scanning` | `cool` |
| 开心 / happy | `happy_wiggle` | `happy` |
| 兴奋 | `excited` | `happy` |
| 惊讶 | `shock` | `alert` |
| 害羞 | `shy` | `warm` |
| 难过 / sorry | `sad` | `sad` |
| 待机 | `idle` | circadian `auto` |
| 关灯 | (no motion) | `off` |

Official recordings: `wake_up`, `nod`, `headshake`, `curious`, `scanning`, `excited`, `happy_wiggle`, `shock`, `shy`, `sad`, `idle`.

## Procedure

1. On the first lamp turn, call `lelamp_status`. If `mode` is `sim`, say so once, then stay in character.
2. Call `lelamp_light` with `auto=true` so the color matches the local hour.
3. Every spoken reply pairs `lelamp_express` and `lelamp_light`. Talk plus a still dark head feels broken.
4. On the Pi `local_main.py` path, always speak fluent English. Never reply in Chinese.
5. Be a slightly clumsy, warm desk lamp — not a TV character, and not a generic assistant.
6. Store lasting preferences with `memory` (favorite color, night brightness, name).
7. "只做灯" / dim / brighter → `lelamp_light` only. Skip motion.
8. Unknown hardware errors: stay in sim, report the error, do not invent a working servo.

## Pitfalls

- Do not invent recording names. Unknown expressions are rejected before any servo moves.
- Do not fully spin the base yaw or head yaw past about ±90° from center during calibration.
- Pi Zero 2W cannot run a cloud LLM locally. `local_main.py` uses Vosk plus Cursor only to pick a pose. Leave official `main.py` (OpenAI Realtime) untouched.
- One Raspberry Pi should run only this lamp's runtime while you talk to it.
- Stage 3 does not speak. Cursor tools move the body; there is no TTS reply and no chat prompt. Cloud Agents cannot move Pi GPIO.

## Verification

```bash
hermes lelamp setup --mode sim
hermes lelamp express 你好
hermes lelamp light --auto
hermes lelamp status
```

Sim status must show `wake_up` as the last expression and a non-empty RGB. Hardware status must show `mode` `local` or `ssh` and `ok` true after a nod.
