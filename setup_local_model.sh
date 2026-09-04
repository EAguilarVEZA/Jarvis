#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Smart With Martin — one-time local model setup (free, private, on-device).
# Installs Ollama and pulls a strong local model so Martin + the KTX data-question
# planner can run entirely on this Mac. Nothing leaves your network.
#
#   Usage:   bash ~/Jarvis/setup_local_model.sh
#
# Your machine: MacBook Pro (Apple M4 Max, 36 GB) — plenty for a 32B-class model.
# ─────────────────────────────────────────────────────────────────────────────
set -e

MODEL="${1:-qwen2.5:32b-instruct}"   # override: bash setup_local_model.sh llama3.1:8b

echo "▶ Smart With Martin — local model setup"
echo "  Model: $MODEL"
echo

# 1) Install Ollama (via Homebrew if present, else the official installer).
if ! command -v ollama >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "▶ Installing Ollama with Homebrew…"
    brew install ollama
  else
    echo "▶ Homebrew not found — installing Ollama with the official script…"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
else
  echo "✓ Ollama already installed."
fi

# 2) Make sure the server is running (needed for pulls + for Martin to call it).
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "▶ Starting Ollama server in the background…"
  (ollama serve >/tmp/ollama.log 2>&1 &) || true
  sleep 3
fi

# 3) Pull the model (this is the big one-time download).
echo "▶ Pulling $MODEL (one-time download, may take a while)…"
ollama pull "$MODEL"

echo
echo "✓ Done. $MODEL is ready on this Mac."
echo "  Next: open Smart With Martin → Admin → System model →"
echo "        pick 'Martin Local (Ollama)' → Use this."
echo "  Keep Ollama running (it stays up after 'ollama serve')."
echo
echo "  Quick test:  ollama run $MODEL \"Say hello in one line\""
