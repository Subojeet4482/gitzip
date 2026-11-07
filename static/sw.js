/* GitZip Bot service worker: app shell only. API calls are never cached or intercepted. */
var C = "gitzip-v7", A = ["./", "index.html", "style.css", "app.js", "icon.svg", "manifest.webmanifest"];
self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(C).then(function (c) { return Promise.all(A.map(function (u) { return c.add(u).catch(function () { }); })); }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (k) { return Promise.all(k.filter(function (x) { return x !== C; }).map(function (x) { return caches.delete(x); })); }).then(function () { return self.clients.claim(); }));
});
self.addEventListener("fetch", function (e) {
  var r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin || u.pathname.indexOf("/api/") === 0 || u.pathname === "/healthz") return;
  e.respondWith(fetch(r).then(function (n) {
    if (n.ok) { var cp = n.clone(); caches.open(C).then(function (c) { c.put(r, cp); }); }
    return n;
  }).catch(function () { return caches.match(r).then(function (m) { return m || caches.match("index.html"); }); }));
});
