/**
 * JARVIS — Main entry point.
 *
 * Wires together the orb visualization, WebSocket communication,
 * speech recognition, and audio playback into a single experience.
 */

import { createOrb, type OrbState } from "./orb";
import { createVoiceInput, createAudioPlayer } from "./voice";
import { createSocket } from "./ws";
import { openSettings, checkFirstTimeSetup } from "./settings";
import "./style.css";

// ---------------------------------------------------------------------------
// State machine
// ---------------------------------------------------------------------------

type State = "idle" | "listening" | "thinking" | "speaking";
let currentState: State = "idle";
let isMuted = false;

const statusEl = document.getElementById("status-text")!;
const captionEl = document.getElementById("caption-text")!;
let captionTimer: ReturnType<typeof setTimeout> | null = null;

function showCaption(text: string) {
  if (!text) return;

  // Split into sentences for chunked display
  const sentences = text.match(/[^.!?]+[.!?]+/g) || [text];
  let index = 0;

  if (captionTimer) clearTimeout(captionTimer);
  captionEl.classList.add("visible");

  function showNext() {
    if (index >= sentences.length) {
      captionTimer = setTimeout(() => captionEl.classList.remove("visible"), 2000);
      return;
    }
    const chunk = sentences.slice(index, index + 2).join(" ").trim();
    captionEl.textContent = chunk;
    index += 2;
    // ~65ms per character to roughly match speech speed
    const delay = Math.max(2000, chunk.length * 65);
    captionTimer = setTimeout(showNext, delay);
  }

  showNext();
}
const errorEl = document.getElementById("error-text")!;

function showError(msg: string) {
  errorEl.textContent = msg;
  errorEl.style.opacity = "1";
  setTimeout(() => {
    errorEl.style.opacity = "0";
  }, 5000);
}

function updateStatus(state: State) {
  const labels: Record<State, string> = {
    idle: "",
    listening: "listening...",
    thinking: "thinking...",
    speaking: "",
  };
  statusEl.textContent = labels[state];
}

// ---------------------------------------------------------------------------
// Init components
// ---------------------------------------------------------------------------

const canvas = document.getElementById("orb-canvas") as HTMLCanvasElement;
const orb = createOrb(canvas);

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = `${wsProto}//${window.location.host}/ws/voice`;
const socket = createSocket(WS_URL);

const audioPlayer = createAudioPlayer();
orb.setAnalyser(audioPlayer.getAnalyser());

function transition(newState: State) {
  if (newState === currentState) return;
  currentState = newState;
  orb.setState(newState as OrbState);
  updateStatus(newState);

  switch (newState) {
    case "idle":
      if (!isMuted) voiceInput.resume();
      break;
    case "listening":
      if (!isMuted) voiceInput.resume();
      break;
    case "thinking":
      voiceInput.pause();
      break;
    case "speaking":
      voiceInput.pause();
      break;
  }
}

// ---------------------------------------------------------------------------
// Voice input
// ---------------------------------------------------------------------------

const voiceInput = createVoiceInput(
  (text: string) => {
    // Cancel any current JARVIS response before sending new input
    audioPlayer.stop();
    // User spoke — send transcript
    socket.send({ type: "transcript", text, isFinal: true });
    transition("thinking");
  },
  (msg: string) => {
    showError(msg);
  }
);

// ---------------------------------------------------------------------------
// Audio playback finished
// ---------------------------------------------------------------------------

audioPlayer.onFinished(() => {
  transition("idle");
});

// ---------------------------------------------------------------------------
// WebSocket messages
// ---------------------------------------------------------------------------

socket.onMessage((msg) => {
  const type = msg.type as string;

  if (type === "audio") {
    const audioData = msg.data as string;
    console.log("[audio] received", audioData ? `${audioData.length} chars` : "EMPTY", "state:", currentState);
    if (audioData) {
      if (currentState !== "speaking") {
        transition("speaking");
      }
      audioPlayer.enqueue(audioData);
    } else {
      // TTS failed — no audio but still need to return to idle
      console.warn("[audio] no data received, returning to idle");
      transition("idle");
    }
    // Log text for debugging
    if (msg.text) { console.log("[JARVIS]", msg.text); showCaption(msg.text); }
  } else if (type === "status") {
    const state = msg.state as string;
    if (state === "thinking" && currentState !== "thinking") {
      transition("thinking");
    } else if (state === "working") {
      // Task spawned — show thinking with a different label
      transition("thinking");
      statusEl.textContent = "working...";
    } else if (state === "idle") {
      transition("idle");
    }
  } else if (type === "text") {
    // Text fallback when TTS fails
    console.log("[JARVIS]", msg.text);
  } else if (type === "open_text_mode") {
    (window as any).openTextModal();
  } else if (type === "task_spawned") {
    console.log("[task]", "spawned:", msg.task_id, msg.prompt);
  } else if (type === "task_complete") {
    console.log("[task]", "complete:", msg.task_id, msg.status, msg.summary);
  }
});

// ---------------------------------------------------------------------------
// Kick off
// ---------------------------------------------------------------------------

// Start listening after a brief delay for the orb to render
setTimeout(() => {
  voiceInput.start();
  transition("listening");
}, 1000);

// Resume AudioContext on ANY user interaction (browser autoplay policy)
function ensureAudioContext() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state === "suspended" || ctx.state === "interrupted") {
    ctx.resume().then(() => {
      console.log("[audio] context resumed, state:", ctx.state);
    }).catch(e => console.warn("[audio] resume failed:", e));
  }
}

// iOS audio unlock - resume on any interaction
function unlockAudio() {
  const ctx = audioPlayer.getAnalyser().context as AudioContext;
  if (ctx.state !== "running") {
    ctx.resume().then(() => console.log("[audio] unlocked, state:", ctx.state));
  }
}
document.body.addEventListener("touchstart", unlockAudio, { passive: true });
document.body.addEventListener("touchend", unlockAudio, { passive: true });
document.body.addEventListener("click", unlockAudio);
document.addEventListener("click", ensureAudioContext);
document.addEventListener("touchstart", ensureAudioContext, { passive: true });
document.addEventListener("touchend", ensureAudioContext, { passive: true });
document.addEventListener("keydown", ensureAudioContext, { once: true });

// iOS specific: resume on any interaction
document.addEventListener("pointerdown", ensureAudioContext, { passive: true });

// Try to resume audio context on load
ensureAudioContext();

// ---------------------------------------------------------------------------
// UI Controls
// ---------------------------------------------------------------------------

const btnMute = document.getElementById("btn-mute")!;
const btnMenu = document.getElementById("btn-menu")!;
const menuDropdown = document.getElementById("menu-dropdown")!;
const btnRestart = document.getElementById("btn-restart")!;
const btnFixSelf = document.getElementById("btn-fix-self")!;

btnMute.addEventListener("click", (e) => {
  e.stopPropagation();
  isMuted = !isMuted;
  btnMute.classList.toggle("muted", isMuted);
  if (isMuted) {
    voiceInput.pause();
    transition("idle");
  } else {
    voiceInput.resume();
    transition("listening");
  }
});

btnMenu.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = menuDropdown.style.display === "none" ? "block" : "none";
});

document.addEventListener("click", () => {
  menuDropdown.style.display = "none";
});

btnRestart.addEventListener("click", async (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  statusEl.textContent = "restarting...";
  try {
    await fetch("/api/restart", { method: "POST" });
    // Wait a few seconds then reload
    setTimeout(() => window.location.reload(), 4000);
  } catch {
    statusEl.textContent = "restart failed";
  }
});

btnFixSelf.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  // Activate work mode on the WebSocket session (JARVIS becomes Claude Code's voice)
  socket.send({ type: "fix_self" });
  statusEl.textContent = "entering work mode...";
});

// Settings button
const btnSettings = document.getElementById("btn-settings")!;
btnSettings.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdown.style.display = "none";
  openSettings();
});

// First-time setup detection — check after a short delay for server readiness
setTimeout(() => {
  checkFirstTimeSetup();
}, 2000);

// ---------------------------------------------------------------------------
// Text Input Modal
// ---------------------------------------------------------------------------

const textModal = document.getElementById("text-modal")!;
const textInput = document.getElementById("text-input") as HTMLTextAreaElement;
const textSendBtn = document.getElementById("text-send-btn")!;
const textModalClose = document.getElementById("text-modal-close")!;
const fileInput = document.getElementById("file-input") as HTMLInputElement;
const attachLabel = document.getElementById("attach-label")!;

let attachedFile: File | null = null;

export function openTextModal() {
  textModal.style.display = "flex";
  setTimeout(() => textInput.focus(), 50);
}
// Register on window so WebSocket handler can call it
(window as any).openTextModal = openTextModal;

function closeTextModal() {
  textModal.style.display = "none";
  textInput.value = "";
  attachedFile = null;
  attachLabel.textContent = "";
  fileInput.value = "";
}

fileInput.addEventListener("change", () => {
  attachedFile = fileInput.files?.[0] || null;
  attachLabel.textContent = attachedFile ? attachedFile.name : "";
});

textModalClose.addEventListener("click", closeTextModal);
textModal.addEventListener("click", (e) => {
  if (e.target === textModal) closeTextModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && textModal.style.display === "flex") closeTextModal();
});

async function sendTextInput() {
  const text = textInput.value.trim();
  if (!text) return;

  closeTextModal();
  audioPlayer.stop();

  if (attachedFile) {
    // Send file + text to upload endpoint
    const formData = new FormData();
    formData.append("file", attachedFile);
    formData.append("prompt", text);
    try {
      transition("thinking");
      const resp = await fetch("/api/upload", { method: "POST", body: formData });
      const data = await resp.json();
      socket.send({ type: "transcript", text: data.combined_prompt, isFinal: true });
    } catch (err) {
      console.error("[text-modal] upload error:", err);
      socket.send({ type: "transcript", text, isFinal: true });
    }
  } else {
    socket.send({ type: "transcript", text, isFinal: true });
  }
  transition("thinking");
}

textSendBtn.addEventListener("click", sendTextInput);
textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendTextInput();
  }
});

// Add to menu
const menuDropdownEl = document.getElementById("menu-dropdown")!;
const textModeItem = document.createElement("button");
textModeItem.textContent = "⌨️  Text Input";
textModeItem.className = "menu-item";
textModeItem.style.cssText = "display:block; width:100%; text-align:left; background:none; border:none; color:white; padding:10px 16px; cursor:pointer; font-size:14px;";
textModeItem.addEventListener("click", (e) => {
  e.stopPropagation();
  menuDropdownEl.style.display = "none";
  openTextModal();
});
menuDropdownEl.appendChild(textModeItem);

