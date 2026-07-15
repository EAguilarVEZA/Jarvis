#!/usr/bin/env python3
"""Martin Voice Visualizer — tiny read-only bridge to the voice line's signal bus.

Serves the fullscreen scene (index.html) and a /state endpoint the page polls.
It ONLY reads the bus files the voice line writes; it never writes them.

Bus files (in ~/.martin_voice by default, override with MARTIN_VOICE_BUS):
  .voice_state     plain text: idle | listening | thinking | speaking
  .voice_waveform  JSON {"ts": <unix>, "samples": [64 floats]}
  .voice_alert     exists => alert is true

Run:
  python3 server.py           # real bus, port 8777
  python3 server.py --mock    # scripted demo loop, port 8778 (no voice line needed)
"""
import json
import os
import sys
import time
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BUS_DIR = Path(os.getenv("MARTIN_VOICE_BUS", str(Path.home() / ".martin_voice")))
HERE = Path(__file__).resolve().parent
MOCK = "--mock" in sys.argv
PORT = 8778 if MOCK else 8777
_T0 = time.time()


def _read_bus():
    """Return (state, level, alert) from the bus, or idle if stale/missing."""
    state, level, alert = "idle", 0.0, False
    try:
        alert = (BUS_DIR / ".voice_alert").exists()
    except Exception:
        alert = False
    try:
        wf = json.loads((BUS_DIR / ".voice_waveform").read_text())
        ts = float(wf.get("ts", 0))
        samples = wf.get("samples") or []
        fresh = (time.time() - ts) < 2.0
        if fresh and samples:
            level = min(1.0, (sum(abs(float(s)) for s in samples) / len(samples)) * 4.0)
    except Exception:
        pass
    try:
        st = (BUS_DIR / ".voice_state").read_text().strip()
        if st in ("idle", "listening", "thinking", "speaking"):
            state = st
    except Exception:
        pass
    # a fresh waveform means audio is really playing, whatever the state file says
    if level > 0.02 and state not in ("speaking", "listening"):
        state = "speaking"
    return state, round(level, 3), alert


def _mock_state():
    """A scripted loop so you can enjoy the scene with no voice line running."""
    cycle = (time.time() - _T0) % 20.0
    if cycle < 4:
        return "idle", 0.0, False
    if cycle < 8:
        return "listening", 0.35 + 0.15 * math.sin(cycle * 3), False
    if cycle < 11:
        return "thinking", 0.2, False
    if cycle < 17:
        lvl = 0.35 + 0.45 * abs(math.sin(cycle * 4.5))
        return "speaking", round(lvl, 3), False
    if cycle < 18:
        return "idle", 0.0, True
    return "idle", 0.0, False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, (HERE / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "index.html not found: " + str(e), "text/plain")
            return
        if path == "/state":
            state, level, alert = _mock_state() if MOCK else _read_bus()
            self._send(200, json.dumps({"state": state, "level": level, "alert": bool(alert)}), "application/json")
            return
        self._send(404, "not found", "text/plain")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    mode = "MOCK demo" if MOCK else "live bus " + str(BUS_DIR)
    print("Martin Voice Visualizer — http://127.0.0.1:%d  (%s)" % (PORT, mode))
    print("Open that URL in your browser (Ctrl-C to stop).")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
