/* =========================================================
   Jodala Microfinance -- Service Worker
   Scope is deliberately narrow: this app is a live financial ledger, so
   it must never show stale account/loan/savings data. The only things
   this worker caches are static assets (CSS/JS/icons) that make the app
   installable and let the shell repaint instantly on repeat visits.
   Every page (HTML) and every /api/ call always goes to the network --
   the only exception is a plain offline fallback page shown when a page
   navigation fails with no connection at all.
   ========================================================= */

const CACHE_VERSION = 'jodala-static-v2';
const OFFLINE_URL = '/static/offline.html';

const PRECACHE_URLS = [
  '/static/css/main.css',
  '/static/js/app.js',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/vendor/bootstrap/css/bootstrap.min.css',
  '/static/vendor/bootstrap/js/bootstrap.bundle.min.js',
  '/static/vendor/bootstrap-icons/font/bootstrap-icons.min.css',
  '/static/vendor/bootstrap-icons/font/fonts/bootstrap-icons.woff2',
  '/static/vendor/chartjs/chart.umd.min.js',
  OFFLINE_URL,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith('/static/') &&
    !url.pathname.startsWith('/static/uploads/'); // just in case -- never cache anything user-uploaded
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // leave CDN/font requests alone

  // Full-page navigations: always prefer the live, authenticated page.
  // Only fall back to the offline shell if the network is truly
  // unreachable -- never serve a cached page in its place.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Static assets: cache-first for instant repeat loads, refreshing the
  // cache in the background from the network.
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req).then((res) => {
          if (res && res.ok) {
            caches.open(CACHE_VERSION).then((cache) => cache.put(req, res.clone()));
          }
          return res;
        }).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // Everything else (in particular /*/api/* calls) -- straight to the
  // network, no caching, no interception.
});
