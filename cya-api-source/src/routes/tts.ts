import { Router } from 'express';

// Text-to-speech proxy to Fish Audio so the API key never reaches the browser.
// Returns natural Spanish audio (mp3). The app falls back to the browser voice
// if this isn't configured. Set FISH_API_KEY + FISH_VOICE_ID (a Spanish voice
// reference_id from fish.audio) in the backend environment to enable it.
export const ttsRouter = Router();

ttsRouter.post('/', async (req: any, res) => {
  const text = String(req.body?.text || '').trim().slice(0, 800);
  if (!text) return res.status(400).json({ error: 'text_required' });

  const key = process.env.FISH_API_KEY;
  if (!key) return res.status(503).json({ error: 'tts_not_configured' });

  try {
    const r = await fetch('https://api.fish.audio/v1/tts', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
        // request the latest, most natural model
        'model': process.env.FISH_MODEL || 's1',
      },
      body: JSON.stringify({
        text,
        // Default to "Narrador v2" — a warm Latin-American Spanish narrator on fish.audio.
        // Override with FISH_VOICE_ID to use a different voice.
        reference_id: process.env.FISH_VOICE_ID || '35199d5438854f5d9157c500479ab684',
        format: 'mp3',
        // a touch slower + clearer for the kid-friendly butler vibe
        prosody: { speed: 0.95 },
        latency: 'balanced',
      }),
    });

    if (!r.ok) {
      const detail = (await r.text().catch(() => '')).slice(0, 200);
      return res.status(502).json({ error: 'tts_upstream', detail });
    }

    const buf = Buffer.from(await r.arrayBuffer());
    res.setHeader('Content-Type', 'audio/mpeg');
    res.setHeader('Cache-Control', 'no-store');
    res.send(buf);
  } catch (e: any) {
    res.status(500).json({ error: 'tts_failed', detail: String(e?.message || e) });
  }
});
