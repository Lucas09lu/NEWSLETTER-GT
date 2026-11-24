/* Service worker: app shell en caché (offline) + datos con red primero. */
var VERSION = "20261124194539";
var SHELL = "gt-shell-" + VERSION;
var DATA = "gt-data";
var SHELL_FILES = [
  "./",
  "index.html",
  "styles.css",
  "app.js",
  "manifest.json",
  "assets/logo-global-transport.png",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "icons/favicon.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(SHELL).then(function (c) { return c.addAll(SHELL_FILES); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k.indexOf("gt-shell-") === 0 && k !== SHELL; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;

  // Ediciones: red primero (siempre lo más reciente), caché como respaldo offline.
  if (req.url.indexOf("/data/") !== -1) {
    event.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(DATA).then(function (c) { c.put(req, copy); });
        return res;
      }).catch(function () { return caches.match(req); })
    );
    return;
  }
  // App shell: caché primero.
  event.respondWith(caches.match(req).then(function (hit) { return hit || fetch(req); }));
});
