// Cover1Picks Edge Engine — service worker
//
// EVERYTHING is network-first. The cache exists only so the app opens when
// there is no signal, never as the primary source.
//
// This is deliberate. index.html is regenerated and uploaded by hand, so a
// cache-first shell keyed on a version constant would keep serving the previous
// build until someone remembered to bump that constant. In a standalone app
// there is no address bar and no reload button, so a user stuck on an old shell
// has no way out. A slightly slower open is a far cheaper price than a card
// that silently shows last week's picks.
const CACHE = 'edge-engine';
const SHELL = ['./', './index.html', './manifest.webmanifest',
  './icon-192.png', './icon-512.png', './apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE)
    .then(c => c.addAll(SHELL))
    .catch(() => null)
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('message', e => {
  if (e.data === 'skip-waiting') self.skipWaiting();
});

// One strategy for every same-origin GET: go to the network, keep a copy for
// offline, and fall back to that copy only when the network actually fails.
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin && !/\.json($|\?)/.test(url.pathname + url.search)) return;

  e.respondWith(
    fetch(req, { cache: 'no-store' })
      .then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy)).catch(() => null);
        }
        return res;
      })
      .catch(() => caches.match(req).then(hit => hit || Response.error()))
  );
});
