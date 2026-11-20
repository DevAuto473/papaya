/* ببايه service worker: offline shell + push notifications. */
const CACHE = "babaya-v2";
const SHELL = [
  "/", "/index.html", "/app.css", "/app.js", "/manifest.webmanifest",
  "/fonts/pixelae-bold.ttf", "/fonts/plex-ar-400.woff2", "/fonts/plex-ar-700.woff2", "/fonts/plex-la-400.woff2", "/fonts/plex-la-700.woff2",
  ...["normal", "angry", "thinking", "idea", "happy", "sad", "sleeping"].map((m) => `/img/babaya-${m}.webp`),
  "/icons/icon-192.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // app files: network first (fresh after a deploy), the cache when offline
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/index.html"))));
});

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "ببايه", {
    body: d.body || "", icon: "/icons/icon-192.png", badge: "/icons/icon-192.png", lang: "ar", dir: "rtl",
    tag: d.tag || undefined, data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL(e.notification.data?.url || "/", location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if (c.url.startsWith(location.origin)) { c.focus(); return c.navigate(target); }
    }
    return self.clients.openWindow(target);
  }));
});
