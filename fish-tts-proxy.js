const http = require('http');
const { encode } = require('@msgpack/msgpack');

const API_KEY = process.env.FISH_API_KEY || '';
const ADRIAN = process.env.ADRIAN_MODEL_ID || 'bf322df2096a46f18c579d0baa36f41d';
const MODELS = { Adrian: ADRIAN, Margaux: process.env.MARGAUX_MODEL_ID || ADRIAN, Giulia: process.env.GIULIA_MODEL_ID || ADRIAN };
const PORT = process.env.PORT || 8787;
const FISH_URL = 'https://api.fish.audio/v1/tts';
const FISH_MODEL = process.env.FISH_MODEL || 's1';

const cors = (res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
};

http.createServer((req, res) => {
  cors(res);
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }
  if (req.method !== 'POST') { res.writeHead(405); return res.end('POST only'); }
  let body = '';
  req.on('data', c => body += c);
  req.on('end', async () => {
    try {
      if (!API_KEY) { res.writeHead(500); return res.end('Missing FISH_API_KEY'); }
      const { text, voice = 'Adrian' } = JSON.parse(body || '{}');
      if (!text) { res.writeHead(400); return res.end('No text'); }
      const reference_id = MODELS[voice] || MODELS.Adrian;
      const payload = encode({ text, reference_id: reference_id || undefined, format: 'mp3', mp3_bitrate: 128, normalize: true, latency: 'normal' });
      const fishRes = await fetch(FISH_URL, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${API_KEY}`, 'Content-Type': 'application/msgpack', 'model': FISH_MODEL },
        body: payload,
      });
      if (!fishRes.ok) { const t = await fishRes.text(); res.writeHead(fishRes.status); return res.end('Fish Audio error: ' + t); }
      const buf = Buffer.from(await fishRes.arrayBuffer());
      res.writeHead(200, { 'Content-Type': 'audio/mpeg', 'Content-Length': buf.length });
      res.end(buf);
    } catch (e) { res.writeHead(500); res.end('Proxy error: ' + e.message); }
  });
}).listen(PORT, () => console.log(`Fish TTS proxy → http://localhost:${PORT}  (voice: Adrian)`));
