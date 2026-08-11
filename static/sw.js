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

const CACHE_VERSION = 'jodala-static-v5';
const PAGE_CACHE = 'jodala-pages-v1';
const DATA_CACHE = 'jodala-data-v1';
const OFFLINE_URL = '/static/offline.html';

const PRECACHE_URLS = [
  '/static/css/main.css',
  '/static/js/app.js',
  '/static/js/pwa.js',
  '/static/js/webauthn.js',
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
        keys.filter((key) => ![CACHE_VERSION, PAGE_CACHE, DATA_CACHE].includes(key))
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith('/static/') &&
    !url.pathname.startsWith('/static/uploads/'); // just in case -- never cache anything user-uploaded
}

function canCachePage(url) {
  // Never retain login/reset/logout pages. Other HTML pages are cached only
  // after the signed-in user has successfully opened them online.
  return !url.pathname.startsWith('/auth/');
}

function canCacheApi(url) {
  // Read-only API results let lists and dashboards render when a device is
  // offline. Mutating requests are never intercepted or queued.
  return url.pathname.includes('/api/') &&
    !url.pathname.startsWith('/auth/') &&
    !url.pathname.includes('/export/') &&
    !url.pathname.includes('/download');
}

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(cacheName);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (_) {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw _;
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // leave CDN/font requests alone

  // Signing out also removes cached customer data from this device. Static
  // assets remain cached, so the sign-in screen is still fast next time.
  if (url.pathname === '/auth/logout') {
    event.respondWith(fetch(req));
    event.waitUntil(Promise.all([caches.delete(PAGE_CACHE), caches.delete(DATA_CACHE)]));
    return;
  }

  // Full-page navigations: always prefer the live, authenticated page.
  // Only fall back to the offline shell if the network is truly
  // unreachable -- never serve a cached page in its place.
  if (req.mode === 'navigate') {
    event.respondWith(
      canCachePage(url)
        ? networkFirst(req, PAGE_CACHE).catch(() => caches.match(OFFLINE_URL))
        : fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Static assets: cache-first for instant repeat loads, refreshing the
  // cache in the background from the network.
  if (isStaticAsset(url)) {
    event.respondWith(
      // The main stylesheet is loaded with an asset-version query string;
      // match its precached base URL too, otherwise it is needlessly missed
      // whenever the device is offline.
      caches.match(req, { ignoreSearch: true }).then((cached) => {
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

  // Keep successful GET responses for operational screens (dashboard,
  // members, loans, repayments, savings, etc.). When offline the cached
  // response is returned, allowing a recently viewed screen to render with
  // its last synchronised data.
  if (canCacheApi(url)) {
    event.respondWith(networkFirst(req, DATA_CACHE));
    return;
  }

  // Everything else (in particular /*/api/* calls) -- straight to the
  // network, no caching, no interception.
});
