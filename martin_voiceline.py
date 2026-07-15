#!/usr/bin/env python3
"""Martin Voiceline — a local hold-to-talk phone line to Martin.

Hold a key (or tap Enter), speak, release. Your words are transcribed locally
with Whisper, sent to the *running Martin brain* over the same `/ws/voice`
channel the web app uses, and Martin's spoken reply plays back — with a live,
reactive amplitude meter in the terminal so you can watch him listen and speak.

How this fits the rest of the system
------------------------------------
- The **web app** already has a voice line (browser Web Speech + the orb, driven
  server-side by `martin_voice.MartinSession`). This file is the *other* door:
  a terminal / menu-bar-style push-to-talk line so you can reach Martin without
  opening the browser.
- Both doors share ONE brain. Martin's memory, actions, compliance gates and TTS
  live in `server.py`; this client is just a local microphone + speaker + hotkey.
  That matters for safety: Martin's "ask before anything risky" discipline
  (never send / publish / spend / activate / delete without a spoken yes) is
  enforced server-side, so the phone line physically cannot route around it.
- **Everything but the LLM stays on your machine.** Speech-to-text runs locally
  (faster-whisper); audio never leaves the box except as text to Martin.
- **Degrades gracefully.** No global-hotkey library? It falls back to tap-to-talk
  (Enter to start/stop). No Whisper? It prints exactly what to `pip install`.

Run:  python martin_voiceline.py             # tap-to-talk (press Enter)
      python martin_voiceline.py --mode ptt   # hold Right-Option (needs pynput)

Heavy imports are deferred so this file imports/compiles before the audio stack
is installed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import ssl
import subprocess
import sys
import tempfile
import time

SAMPLE_RATE = 16000
CHANNELS = 1
DEFAULT_URL = os.getenv("MARTIN_VOICE_URL", "wss://localhost:8340/ws/voice")

# ── signal bus (for the voice visualizer) ────────────────────────────────────
# We write two tiny files a separate visualizer can watch: .voice_state (idle |
# listening | thinking | speaking) and .voice_waveform ({ts, samples:[64]}).
# All writes are best-effort; the bus must never crash the voice line.
from pathlib import Path
BUS_DIR = Path(os.getenv("MARTIN_VOICE_BUS", str(Path.home() / ".martin_voice")))
try:
    BUS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
_last_wave = [0.0]

def _bus_state(s):
    try:
        (BUS_DIR / ".voice_state").write_text(s)
    except Exception:
        pass

def _bus_wave(samples):
    try:
        (BUS_DIR / ".voice_waveform").write_text(json.dumps({"ts": time.time(), "samples": samples}))
    except Exception:
        pass

def _bus_wave_arr(arr, throttle=0.066):
    now = time.time()
    if now - _last_wave[0] < throttle:
        return
    _last_wave[0] = now
    try:
        import numpy as np
        a = np.abs(np.asarray(arr, dtype=float)).reshape(-1)
        n = 64
        if a.size == 0:
            _bus_wave([0.0] * n); return
        step = max(1, a.size // n)
        out = [round(float(a[i:i + step].mean()), 4) for i in range(0, a.size, step)][:n]
        out += [0.0] * (n - len(out))
        _bus_wave(out)
    except Exception:
        pass

# ── tiny terminal styling ───────────────────────────────────────────────────
DIM = "\033[2m"; BOLD = "\033[1m"; CYAN = "\033[36m"; AMBER = "\033[33m"
GREEN = "\033[32m"; RED = "\033[31m"; RESET = "\033[0m"
BAR = "▁▂▃▄▅▆▇█"


def _meter(level: float, width: int = 28) -> str:
    """A reactive amplitude bar. `level` is 0..1."""
    level = max(0.0, min(1.0, level))
    filled = int(level * width)
    cells = []
    for i in range(width):
        if i < filled:
            cells.append(BAR[min(len(BAR) - 1, (i * len(BAR)) // width)])
        else:
            cells.append(" ")
    color = GREEN if level < 0.5 else (AMBER if level < 0.8 else RED)
    return f"{color}{''.join(cells)}{RESET}"


def _need(pkg: str, pip_name: str | None = None):
    print(f"{RED}Missing dependency: {pkg}{RESET}\n"
          f"  Install the voice stack:  pip install -r requirements-voice.txt\n"
          f"  (or just:  pip install {pip_name or pkg})")
    sys.exit(1)


# ── local speech-to-text (Whisper) ───────────────────────────────────────────
class Transcriber:
    def __init__(self, model_name: str = "base.en"):
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except Exception:
            _need("faster_whisper", "faster-whisper")
        from faster_whisper import WhisperModel
        print(f"{DIM}Loading Whisper '{model_name}' (first run downloads it)…{RESET}")
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def transcribe(self, audio) -> str:
        segments, _ = self.model.transcribe(audio, language="en", vad_filter=True)
        return " ".join(s.text for s in segments).strip()


# ── microphone capture with a live meter ─────────────────────────────────────
def _record_tap():
    """Tap-to-talk: Enter to start, Enter again to stop. Returns float32 audio."""
    try:
        import sounddevice as sd  # noqa: F401
        import numpy as np  # noqa: F401
    except Exception:
        _need("sounddevice/numpy", "sounddevice numpy")
    import sounddevice as sd
    import numpy as np

    input(f"{CYAN}▶ Press Enter, speak, then press Enter again…{RESET}")
    frames: list = []

    def cb(indata, n, t, status):  # noqa: ARG001
        frames.append(indata.copy())
        level = float(np.sqrt(np.mean(indata ** 2))) * 6.0
        _bus_wave_arr(indata)
        sys.stdout.write("\r🎙  " + _meter(level) + "  " + DIM + "(Enter to stop)" + RESET)
        sys.stdout.flush()

    _bus_state("listening")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32", callback=cb):
        input()  # block until Enter → stop
    sys.stdout.write("\r" + " " * 60 + "\r")
    if not frames:
        return np.zeros(0, dtype="float32")
    return np.concatenate(frames, axis=0).flatten()


def _record_ptt(key_name: str):
    """Push-to-talk: hold `key_name` (e.g. 'alt_r' = Right Option) to record."""
    try:
        import sounddevice as sd  # noqa: F401
        import numpy as np  # noqa: F401
        from pynput import keyboard  # noqa: F401
    except Exception:
        _need("pynput/sounddevice/numpy", "pynput sounddevice numpy")
    import sounddevice as sd
    import numpy as np
    from pynput import keyboard

    held = {"down": False, "done": False}
    target = getattr(keyboard.Key, key_name, None)

    def on_press(k):
        if target is not None and k == target:
            held["down"] = True

    def on_release(k):
        if target is not None and k == target:
            held["done"] = True
            return False  # stop listener

    print(f"{CYAN}▶ Hold {BOLD}{key_name}{RESET}{CYAN} and speak…{RESET}")
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()
    while not held["down"] and not held["done"]:
        time.sleep(0.01)

    frames: list = []

    def cb(indata, n, t, status):  # noqa: ARG001
        frames.append(indata.copy())
        level = float(np.sqrt(np.mean(indata ** 2))) * 6.0
        _bus_wave_arr(indata)
        sys.stdout.write("\r🎙  " + _meter(level) + "  " + DIM + "(release to send)" + RESET)
        sys.stdout.flush()

    _bus_state("listening")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32", callback=cb):
        while not held["done"]:
            time.sleep(0.01)
    listener.join()
    sys.stdout.write("\r" + " " * 60 + "\r")
    if not frames:
        return np.zeros(0, dtype="float32")
    return np.concatenate(frames, axis=0).flatten()


# ── playback with a pulsing "speaking" meter ─────────────────────────────────
def _play_mp3(mp3_bytes: bytes, label: str):
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        path = f.name
    player = ["afplay", path] if sys.platform == "darwin" else ["ffplay", "-nodisp", "-autoexit", path]
    try:
        proc = subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print(f"{DIM}(no audio player found; text only){RESET}")
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    t = 0.0
    _bus_state("speaking")
    while proc.poll() is None:
        level = 0.45 + 0.45 * abs(math.sin(t * 6.0))
        _bus_wave_arr([level] * 128)
        sys.stdout.write("\r🔊 " + _meter(level) + "  " + DIM + label[:40] + RESET)
        sys.stdout.flush()
        time.sleep(0.05)
        t += 0.05
    sys.stdout.write("\r" + " " * 78 + "\r")
    try:
        os.unlink(path)
    except OSError:
        pass


# ── the voice line ───────────────────────────────────────────────────────────
async def run(url: str, mode: str, ptt_key: str, whisper_model: str):
    try:
        import websockets  # noqa: F401
    except Exception:
        _need("websockets", "websockets")
    import websockets

    stt = Transcriber(whisper_model)

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE  # self-signed localhost cert

    print(f"{BOLD}{CYAN}Martin Voiceline{RESET} — connecting to {DIM}{url}{RESET}")
    try:
        conn = websockets.connect(url, ssl=ssl_ctx, max_size=None)
        ws = await conn.__aenter__()
    except Exception as e:
        print(f"{RED}Could not reach Martin at {url}{RESET}\n"
              f"  Is the server running?  cd ~/Jarvis && python server.py --port 8340\n  ({e})")
        return

    print(f"{GREEN}● connected{RESET} — {DIM}Ctrl-C to hang up{RESET}\n")
    _bus_state("idle")

    async def drain():
        """Read server messages until Martin goes idle after speaking."""
        spoke = False
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except asyncio.TimeoutError:
                return
            except Exception:
                return
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mt = msg.get("type")
            if mt == "status":
                st = msg.get("state")
                if st == "thinking":
                    sys.stdout.write(f"\r{DIM}…thinking{RESET}     ")
                    sys.stdout.flush()
                elif st == "working":
                    print(f"\r{AMBER}⚙ working on it…{RESET}")
                elif st == "idle" and spoke:
                    return
            elif mt == "audio":
                text = (msg.get("text") or "").strip()
                if text:
                    print(f"\r{BOLD}Martin:{RESET} {text}")
                data = msg.get("data")
                if data:
                    _play_mp3(base64.b64decode(data), text or "Martin")
                spoke = True
            elif mt == "text":
                print(f"\r{BOLD}Martin:{RESET} {msg.get('text','')}")
                spoke = True
            elif mt == "task_spawned":
                print(f"{DIM}  → started a task: {str(msg.get('prompt',''))[:60]}{RESET}")
            elif mt == "task_complete":
                print(f"{GREEN}  ✓ {msg.get('summary','done')}{RESET}")

    # consume the opening greeting (server greets on connect)
    await drain()

    loop = asyncio.get_event_loop()
    try:
        while True:
            recorder = (lambda: _record_ptt(ptt_key)) if mode == "ptt" else _record_tap
            audio = await loop.run_in_executor(None, recorder)
            if audio is None or len(audio) < SAMPLE_RATE // 3:  # < ~0.3s
                print(f"{DIM}(didn't catch that){RESET}")
                continue
            print(f"{DIM}…transcribing{RESET}", end="\r")
            _bus_state("thinking")
            text = await loop.run_in_executor(None, stt.transcribe, audio)
            if not text:
                _bus_state("idle")
                print(f"{DIM}(silence){RESET}")
                continue
            print(f"{BOLD}You:{RESET} {text}")
            await ws.send(json.dumps({"type": "transcript", "text": text, "isFinal": True}))
            await drain()
            _bus_state("idle")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n{DIM}Hanging up. Good day, sir.{RESET}")
    finally:
        try:
            await conn.__aexit__(None, None, None)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Martin Voiceline — local hold-to-talk phone line to Martin")
    ap.add_argument("--url", default=DEFAULT_URL, help="Martin voice WebSocket URL")
    ap.add_argument("--mode", choices=["tap", "ptt"], default="tap",
                    help="tap = press Enter to start/stop; ptt = hold a key (needs pynput)")
    ap.add_argument("--ptt-key", default="alt_r",
                    help="pynput Key name for push-to-talk (e.g. alt_r=Right Option, cmd_r, ctrl_r)")
    ap.add_argument("--whisper", default=os.getenv("MARTIN_WHISPER", "base.en"),
                    help="faster-whisper model (tiny.en, base.en, small.en, medium.en)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.url, args.mode, args.ptt_key, args.whisper))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
