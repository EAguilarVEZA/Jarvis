/**
 * Voice input and audio output for JARVIS.
 * Desktop: SpeechRecognition API
 * iOS: MediaRecorder + push-to-talk + /api/transcribe
 */

export interface VoiceInput {
  start(): void;
  stop(): void;
  pause(): void;
  resume(): void;
  isIOS(): boolean;
}

declare const webkitSpeechRecognition: any;

function detectIOS(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function hasSpeechRecognition(): boolean {
  return !!((window as any).SpeechRecognition ||
    (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null));
}

function createDesktopVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  const SR = (window as any).SpeechRecognition ||
    (typeof webkitSpeechRecognition !== "undefined" ? webkitSpeechRecognition : null);

  const recognition = new SR();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = "en-US";

  let shouldListen = false;
  let paused = false;

  recognition.onresult = (event: any) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        const text = event.results[i][0].transcript.trim();
        if (text) onTranscript(text);
      }
    }
  };

  recognition.onend = () => {
    if (shouldListen && !paused) {
      try { recognition.start(); } catch { }
    }
  };

  recognition.onerror = (event: any) => {
    if (event.error === "not-allowed") {
      onError("Microphone access denied.");
      shouldListen = false;
    }
  };

  return {
    isIOS: () => false,
    start() { shouldListen = true; paused = false; try { recognition.start(); } catch { } },
    stop() { shouldListen = false; paused = false; recognition.stop(); },
    pause() { paused = true; recognition.stop(); },
    resume() { paused = false; if (shouldListen) { try { recognition.start(); } catch { } } },
  };
}

function createIOSVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  let mediaRecorder: MediaRecorder | null = null;
  let audioChunks: Blob[] = [];
  let stream: MediaStream | null = null;
  let isRecording = false;
  let enabled = false;

  async function startRecording() {
    if (isRecording || !enabled) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm"
        : MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "audio/ogg";
      mediaRecorder = new MediaRecorder(stream, { mimeType });
      audioChunks = [];
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) audioChunks.push(e.data); };
      mediaRecorder.onstop = async () => {
        if (audioChunks.length === 0) return;
        const blob = new Blob(audioChunks, { type: mimeType });
        audioChunks = [];
        await transcribeAudio(blob, mimeType);
        stream?.getTracks().forEach((t) => t.stop());
        stream = null;
      };
      mediaRecorder.start();
      isRecording = true;
    } catch (err) {
      onError("Microphone access denied. Please allow microphone in Settings.");
    }
  }

  function stopRecording() {
    if (!isRecording || !mediaRecorder) return;
    isRecording = false;
    mediaRecorder.stop();
    mediaRecorder = null;
  }

  async function transcribeAudio(blob: Blob, mimeType: string) {
    try {
      const formData = new FormData();
      const ext = mimeType.includes("mp4") ? "mp4" : mimeType.includes("ogg") ? "ogg" : "webm";
      formData.append("audio", blob, `recording.${ext}`);
      const resp = await fetch("/api/transcribe", { method: "POST", body: formData });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const text = data.text?.trim();
      if (text) onTranscript(text);
      else onError("Could not understand audio. Please try again.");
    } catch (err) {
      onError("Transcription failed. Please try again.");
    }
  }

  function injectPTTButton() {
    document.getElementById("ios-ptt-btn")?.remove();
    document.getElementById("ios-hint")?.remove();

    const btn = document.createElement("button");
    btn.id = "ios-ptt-btn";
    btn.innerHTML = "🎤";
    btn.style.cssText = `
      position: fixed; bottom: 40px; left: 50%; transform: translateX(-50%);
      width: 80px; height: 80px; border-radius: 50%;
      background: rgba(255,255,255,0.12); border: 2px solid rgba(255,255,255,0.35);
      color: white; font-size: 32px; cursor: pointer; z-index: 9999;
      backdrop-filter: blur(10px); transition: all 0.15s ease;
      display: flex; align-items: center; justify-content: center;
      -webkit-tap-highlight-color: transparent; touch-action: none;
      user-select: none; -webkit-user-select: none;
    `;

    const setActive = () => {
      btn.style.background = "rgba(220,50,50,0.6)";
      btn.style.transform = "translateX(-50%) scale(1.12)";
      btn.innerHTML = "🔴";
      startRecording();
    };
    const setIdle = () => {
      btn.style.background = "rgba(255,255,255,0.12)";
      btn.style.transform = "translateX(-50%) scale(1)";
      btn.innerHTML = "🎤";
      stopRecording();
    };

    btn.addEventListener("touchstart", (e) => { e.preventDefault(); setActive(); }, { passive: false });
    btn.addEventListener("touchend", (e) => { e.preventDefault(); setIdle(); }, { passive: false });
    btn.addEventListener("touchcancel", (e) => { e.preventDefault(); setIdle(); }, { passive: false });
    btn.addEventListener("mousedown", setActive);
    btn.addEventListener("mouseup", setIdle);
    document.body.appendChild(btn);

    const hint = document.createElement("div");
    hint.id = "ios-hint";
    hint.textContent = "Hold to speak";
    hint.style.cssText = `
      position: fixed; bottom: 128px; left: 50%; transform: translateX(-50%);
      color: rgba(255,255,255,0.4); font-size: 12px; z-index: 9999;
      white-space: nowrap; font-family: -apple-system, sans-serif; letter-spacing: 0.05em;
    `;
    document.body.appendChild(hint);
  }

  return {
    isIOS: () => true,
    start() { enabled = true; injectPTTButton(); },
    stop() {
      enabled = false; stopRecording();
      document.getElementById("ios-ptt-btn")?.remove();
      document.getElementById("ios-hint")?.remove();
    },
    pause() { },
    resume() { },
  };
}

export function createVoiceInput(
  onTranscript: (text: string) => void,
  onError: (msg: string) => void
): VoiceInput {
  if (!hasSpeechRecognition() || detectIOS()) {
    console.log("[voice] iOS mode — push-to-talk + Whisper");
    return createIOSVoiceInput(onTranscript, onError);
  }
  console.log("[voice] Desktop mode — SpeechRecognition");
  return createDesktopVoiceInput(onTranscript, onError);
}

// ---------------------------------------------------------------------------
// Audio Player
// ---------------------------------------------------------------------------

export interface AudioPlayer {
  enqueue(base64: string): Promise<void>;
  stop(): void;
  getAnalyser(): AnalyserNode;
  onFinished(cb: () => void): void;
  warmup(): void;
}

export function createAudioPlayer(): AudioPlayer {
  const audioCtx = new AudioContext();
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.8;
  analyser.connect(audioCtx.destination);

  const queue: AudioBuffer[] = [];
  let isPlaying = false;
  let currentSource: AudioBufferSourceNode | null = null;
  let finishedCallback: (() => void) | null = null;

  function playNext() {
    if (queue.length === 0) {
      isPlaying = false; currentSource = null; finishedCallback?.(); return;
    }
    isPlaying = true;
    const buffer = queue.shift()!;
    const source = audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(analyser);
    currentSource = source;
    source.onended = () => { if (currentSource === source) playNext(); };
    source.start();
  }

  return {
    async enqueue(base64: string) {
      if (audioCtx.state === "suspended") await audioCtx.resume();
      try {
        const binary = atob(base64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        const audioBuffer = await audioCtx.decodeAudioData(bytes.buffer.slice(0));
        queue.push(audioBuffer);
        if (!isPlaying) playNext();
      } catch (err) {
        console.error("[audio] decode error:", err);
        if (!isPlaying && queue.length > 0) playNext();
      }
    },
    stop() {
      queue.length = 0;
      if (currentSource) { try { currentSource.stop(); } catch { } currentSource = null; }
      isPlaying = false;
    },
    getAnalyser() { return analyser; },
    onFinished(cb: () => void) { finishedCallback = cb; },
    warmup() {
      audioCtx.resume().then(() => {
        const buf = audioCtx.createBuffer(1, 1, 22050);
        const src = audioCtx.createBufferSource();
        src.buffer = buf;
        src.connect(audioCtx.destination);
        src.start(0);
        console.log("[audio] warmup done, state:", audioCtx.state);
      });
    },
  };
}
