// Service Worker: App-Shell cachen, Daten immer live vom Server.
const CACHE = "lagerwerk-shell-v1";
const SHELL = [
  "/static/css/app.css", "/static/js/app.js", "/static/js/scan.js", "/static/js/wall.js",
  "/static/vendor/htmx-2.0.11.min.js", "/static/vendor/alpine-3.17.4.min.js", "/static/vendor/sortable-1.15.7.min.js",
  "/static/vendor/zxing/zxing-reader-2.2.4.js", "/static/vendor/zxing/zxing_reader.wasm",
  "/static/icons/icon.svg", "/static/icons/icon-192.png",
];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request, { ignoreSearch: true }).then((r) => r || fetch(e.request)));
    return;
  }
  // Seiten & API: Netzwerk zuerst, offline freundliche Meldung
  e.respondWith(fetch(e.request).catch(() => new Response(
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><body style='font-family:sans-serif;padding:2rem'>" +
    "<h2>Keine Verbindung zu DaLiegt’s</h2><p>Bitte WLAN prüfen – die Daten liegen auf deinem Server.</p></body>",
    { headers: { "Content-Type": "text/html; charset=utf-8" } })));
});
