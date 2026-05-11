// Service Worker для Roastberry PWA.
// Стратегия:
//   - HTML/JSON/JS/CSS — network-first с fallback на cache (чтобы свежие правки
//     были видны сразу, но при оффлайне приложение всё равно открывалось).
//   - Картинки (assets/, photos/) — stale-while-revalidate.
// На каждой деплое подписи меняй CACHE_VERSION чтобы старый SW выбросил кеш.

const CACHE_VERSION = 'rb-v3';
const CACHE_STATIC  = `rb-static-${CACHE_VERSION}`;
const CACHE_IMG     = `rb-img-${CACHE_VERSION}`;

const APP_SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './products.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_STATIC).then(c => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_STATIC && k !== CACHE_IMG)
          .map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

function isImage(req) {
  if (req.destination === 'image') return true;
  const u = req.url;
  return /\.(jpe?g|png|webp|gif|svg)(\?|$)/i.test(u);
}

function isApi(req) {
  return /\/tma\/api\//.test(req.url);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // API — никогда не кешируем (заказы, авторизация, чат)
  if (isApi(req)) return;

  if (isImage(req)) {
    e.respondWith(
      caches.open(CACHE_IMG).then(cache =>
        cache.match(req).then(cached => {
          const network = fetch(req).then(res => {
            if (res.ok) cache.put(req, res.clone());
            return res;
          }).catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // HTML / JS / CSS / JSON — network-first
  e.respondWith(
    fetch(req).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE_STATIC).then(c => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req).then(r => r || caches.match('./index.html')))
  );
});
