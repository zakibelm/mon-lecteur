// Service worker : met en cache la coquille de l'app (jamais l'API) pour un démarrage instantané.
const CACHE = "lecteur-v13";   // changer à chaque mise à jour de la coquille (index/app.js/style)
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icon-192.png", "icon-512.png",
  "fonts/inter-normal-latin-ext.woff2", "fonts/inter-normal-latin.woff2", "fonts/merriweather-italic-latin-ext.woff2", "fonts/merriweather-italic-latin.woff2", "fonts/merriweather-normal-latin-ext.woff2", "fonts/merriweather-normal-latin.woff2"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;   // l'API passe toujours par le réseau
  // réseau d'abord (mises à jour immédiates), cache en secours hors ligne
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
