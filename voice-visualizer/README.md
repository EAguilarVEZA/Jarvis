# Martin Voice Visualizer

A fullscreen browser scene — a **living circuit board** — that reacts to the voice line as it listens, thinks, and speaks. Signals race along the copper traces toward a central chip (Martin's core); the whole board breathes with his voice. Read-only: it watches the signal bus the voice line writes and never touches it.

State behaviour: **listening** pulls the current inward toward the chip, **thinking** scrambles it and spins a processing ring on the die, **speaking** fires it outward and breathes with the voice level, **alert** bleeds the board red, **idle** drifts calm.

## See it right now (no voice line needed)

```bash
cd ~/Jarvis/voice-visualizer
python3 server.py --mock
```

Open http://127.0.0.1:8778/ — it walks a scripted loop through all five states so you can enjoy the scene standalone.

## Pair it with the live voice line

Terminal 1 — the voice line (writes the bus as you talk):
```bash
cd ~/Jarvis && source venv/bin/activate
./martin-voiceline
```

Terminal 2 — the visualizer (reads the bus):
```bash
cd ~/Jarvis/voice-visualizer
python3 server.py
```

Open http://127.0.0.1:8777/ and put it fullscreen. Now talk to Martin: the core reacts inbound while you speak, spins up while he thinks, and breathes with his voice while he talks.

## The bus

Both sides share `~/.martin_voice/` (override with `MARTIN_VOICE_BUS`):
- `.voice_state` — idle | listening | thinking | speaking
- `.voice_waveform` — `{ts, samples:[64]}`, drives the level
- `.voice_alert` — its presence turns the scene red

## Controls

- Any key skips the boot intro. **`Space` = cinematic flythrough** (a scripted camera glide over the board; press Space again to cancel). `F` toggles an FPS meter. A small state tag sits bottom-left.
- `launch.command` (double-click) starts the server and opens Chrome fullscreen. `./launch.command --mock` for the demo.

## Swap the scene

The scene is one self-contained `index.html` (canvas 2D, no dependencies). To build a different scene (circuit board, starfield, neon skyline, fireplace), paste [[Voice Visualizer]] from your vault into Claude Code — it keeps this same server/bus and just changes the drawing. The movie-flythrough add-on is [[Cinematic Camera]].
