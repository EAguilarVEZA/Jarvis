# Martin Voiceline

A local **hold-to-talk phone line** to Martin. Hold a key, speak, release — your
words are transcribed on your machine and answered in Martin's voice, with a live
reactive meter in the terminal so you can watch him listen and speak.

This is the second door into Martin. The web app already has a voice line (the
browser mic + the orb, driven server-side by `martin_voice.MartinSession`); the
Voiceline is the terminal / menu-bar-style door, so you can reach Martin without
opening a browser tab.

## Why it's built this way

- **One brain, two doors.** The Voiceline is *only* a microphone, a speaker and a
  hotkey. Martin's memory, actions, compliance gates and text-to-speech all live
  in `server.py` and are reached over the same `/ws/voice` channel the web app
  uses. Swap the front door; it's the same Martin behind it.
- **Safety can't be bypassed.** Because the risky-action gates ("never send,
  publish, spend, activate or delete without a spoken yes") are enforced *in the
  server brain*, the phone line physically can't route around them. Martin asks
  first and waits for your confirmation — by design.
- **Local by default.** Speech-to-text runs on your machine (faster-whisper).
  Audio never leaves the box; only the transcript goes to Martin.

## Setup (one time)

```bash
cd ~/Jarvis
source venv/bin/activate
pip install -r requirements-voice.txt
```

The first run downloads the Whisper model (a few hundred MB for `base.en`).

## Run

Make sure the server is up first:

```bash
cd ~/Jarvis && source venv/bin/activate
python server.py --port 8340
```

Then, in another terminal:

```bash
./martin-voiceline            # tap-to-talk: press Enter to start, Enter to stop
./martin-voiceline --mode ptt  # push-to-talk: hold Right-Option and speak
```

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `tap` | `tap` = press Enter to start/stop; `ptt` = hold a key (needs `pynput`) |
| `--ptt-key` | `alt_r` | pynput key for push-to-talk (`alt_r`=Right Option, `cmd_r`, `ctrl_r`) |
| `--whisper` | `base.en` | STT model: `tiny.en` (fastest) … `medium.en` (most accurate) |
| `--url` | `wss://localhost:8340/ws/voice` | override if the server runs elsewhere |

Environment overrides: `MARTIN_VOICE_URL`, `MARTIN_WHISPER`.

## Notes

- macOS plays audio via `afplay` (built in). On Linux it uses `ffplay` (install
  ffmpeg). The localhost cert is self-signed, so the client skips TLS
  verification for `localhost` only.
- Push-to-talk uses a global hotkey, which on macOS needs Accessibility
  permission for your terminal (System Settings → Privacy & Security →
  Accessibility). Tap mode needs no special permission.
- This client is intentionally separate from `martin_voice.py` (the in-app voice
  control module) so the server's imports are untouched.
