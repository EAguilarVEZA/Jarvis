// Jarvis PWA service worker — cache shell + CDN libs for offline use.
const CACHE = 'jarvis-v1';
const SHELL = [
  './martin_app.html',
  './manifest.json',
  // Charting + table libs
  'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js',
  'https://cdn.jsdelivr.net/npm/gridjs/dist/gridjs.umd.js',
  'https://cdn.jsdelivr.net/npm/gridjs/dist/theme/mermaid.min.css',
  // Map
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js',
  'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL).catch(() => {/* ignore individual fetch failures */}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Network-first for our own API endpoints — always want fresh data
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // Cache-first for shell + CDN assets — fast offline boot
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        // Only cache successful GETs (no opaque + no errors)
        if (e.request.method === 'GET' && resp.ok && resp.type !== 'opaque') {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached);
    })
  );
});

// Allow runtime "skipWaiting" message from page to activate a new SW immediately
self.addEventListener('message', e => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});
