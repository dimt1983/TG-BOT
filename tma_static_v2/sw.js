/* Roastberry service worker
 *  Strategies:
 *    – HTML / *.json  : network-first  (fresh content wins, fallback to cache)
 *    – images / fonts : stale-while-revalidate
 *    – shell assets   : cache-first (precached on install)
 *  Bump CACHE_VERSION whenever the shell ships.
 */

const CACHE_VERSION = '2026-05-22.admin.v6';
const SHELL_CACHE   = `rb-shell-${CACHE_VERSION}`;
const RUNTIME_CACHE = `rb-runtime-${CACHE_VERSION}`;
const IMG_CACHE     = `rb-img-${CACHE_VERSION}`;

// minimal app shell — relative paths, resolved against /tma/v2/ scope.
// Не приводим тут отсутствующие файлы — install упадёт целиком если хоть один 404.
const SHELL_URLS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './tokens.css',
];

// ── install ───────────────────────────────────────────────────
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

// ── activate: drop old caches ─────────────────────────────────
self.addEventListener('activate', (e) => {
  const allow = new Set([SHELL_CACHE, RUNTIME_CACHE, IMG_CACHE]);
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.map((k) => allow.has(k) ? null : caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── fetch routing ─────────────────────────────────────────────
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  const accept = req.headers.get('accept') || '';

  // 1. HTML navigations & .html / .json — network-first
  if (req.mode === 'navigate' || accept.includes('text/html')
      || url.pathname.endsWith('.html') || url.pathname.endsWith('.json')) {
    e.respondWith(networkFirst(req, RUNTIME_CACHE, null));
    return;
  }

  // 2. images — stale-while-revalidate
  if (req.destination === 'image' || /\.(webp|jpg|jpeg|png|avif|svg)$/i.test(url.pathname)) {
    e.respondWith(staleWhileRevalidate(req, IMG_CACHE));
    return;
  }

  // 3. fonts — cache-first (fonts never change without a hash)
  if (req.destination === 'font' || /\.(woff2?|otf|ttf)$/i.test(url.pathname)) {
    e.respondWith(cacheFirst(req, SHELL_CACHE));
    return;
  }

  // 4. everything else — stale-while-revalidate
  e.respondWith(staleWhileRevalidate(req, RUNTIME_CACHE));
});

// ── strategies ────────────────────────────────────────────────
async function networkFirst(req, cacheName, offlineFallback) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(req);
    if (fresh.ok) cache.put(req, fresh.clone());
    return fresh;
  } catch (_) {
    const cached = await cache.match(req);
    if (cached) return cached;
    if (offlineFallback) {
      const off = await caches.match(offlineFallback);
      if (off) return off;
    }
    return Response.error();
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const fresh = await fetch(req);
  if (fresh.ok) cache.put(req, fresh.clone());
  return fresh;
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(req);
  const fetchPromise = fetch(req).then((r) => {
    if (r.ok) cache.put(req, r.clone());
    return r;
  }).catch(() => null);
  return cached || fetchPromise || Response.error();
}

// ── web push ─────────────────────────────────────────────────
self.addEventListener('push', (e) => {
  const data = (() => { try { return e.data.json(); } catch (_) { return {}; }})();
  const title = data.title || 'Roastberry';
  const body  = data.body  || 'Свежая партия в ростере.';
  const url   = data.url   || '/fresh';
  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/icons/icon-192.png',
      badge: '/icons/badge-72.png',
      data: { url },
      tag: data.tag || 'rb-push',
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(self.clients.matchAll({ type: 'window' }).then((wins) => {
    const open = wins.find((w) => w.url.includes(url));
    if (open) return open.focus();
    return self.clients.openWindow(url);
  }));
});
