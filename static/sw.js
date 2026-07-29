/**
 * sw.js — Offline support for Uza Pap POS
 * ─────────────────────────────────────────────────────────────────────────
 * This app is a single HTML file (all CSS/JS inline), which makes offline
 * caching simple: there's basically one "app shell" document to cache.
 *
 * Strategy:
 *  - Navigation requests (loading the page itself) and the two external
 *    CDN assets (Google Fonts, Chart.js): network-first, falling back to
 *    cache when offline. This means once you've loaded the app successfully
 *    at least once, it keeps loading even with no internet.
 *  - Read-only GET API calls (medicines, inventory, customers, etc.):
 *    network-first, falling back to the last successful cached response
 *    when offline. This is a safety net on top of the app's own
 *    localStorage-based medicine cache (see pharmacy_pos_frontend.html).
 *  - Everything that writes data (POST/PUT/PATCH/DELETE): always goes to
 *    the network, never cached, never faked. Offline sale queuing is
 *    handled deliberately in the app's own JS (submitSale/syncOfflineQueue),
 *    NOT here — silently "succeeding" a write while offline would be
 *    dangerous (e.g. pretending a sale saved when it didn't).
 */

const CACHE_NAME = 'uzapap-pos-v1';
const APP_SHELL_URLS = ['/'];
const RUNTIME_CACHEABLE_GET_PATHS = ['/medicines', '/inventory', '/customers', '/suppliers'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL_URLS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

function isWriteRequest(request) {
  return !['GET', 'HEAD'].includes(request.method);
}

function isCacheableApiGet(url) {
  return RUNTIME_CACHEABLE_GET_PATHS.some((p) => url.pathname.startsWith(p));
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept writes — always hit the network, no caching, no fallback.
  if (isWriteRequest(request)) return;

  // App shell: the page itself (navigation requests)
  if (request.mode === 'navigate' || (url.origin === self.location.origin && url.pathname === '/')) {
    event.respondWith(networkFirstWithCache(request));
    return;
  }

  // The two external CDN assets (fonts CSS, Chart.js) — opportunistic caching
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com' || url.hostname === 'cdn.jsdelivr.net') {
    event.respondWith(networkFirstWithCache(request, { opaque: true }));
    return;
  }

  // Selected read-only API GETs — network-first with cache fallback
  if (url.origin === self.location.origin && isCacheableApiGet(url)) {
    event.respondWith(networkFirstWithCache(request));
    return;
  }

  // Everything else (other API GETs, auth checks, etc.): network only,
  // no offline fallback — stale auth/permission data offline is worse
  // than a clear error.
});

async function networkFirstWithCache(request, opts = {}) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && (response.ok || (opts.opaque && response.type === 'opaque'))) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    // Nothing cached and no network — let it fail naturally so the app's
    // own error handling (isOffline flag) can show a clear message.
    throw err;
  }
}
