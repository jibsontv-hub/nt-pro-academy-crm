// NT Pro Academy — Service Worker
// Stellt Offline-Fähigkeit + Push-Notifications bereit.

const CACHE_VERSION = 'ntpro-v3';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const RUNTIME_CACHE = `${CACHE_VERSION}-runtime`;

// Statische Assets die immer gecacht werden
const STATIC_ASSETS = [
    '/static/icons/coach-figure-200.jpg',
    '/static/icons/coach-figure.jpg',
    '/static/icons/apple-touch-icon.png',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    '/favicon.svg',
    '/manifest.json',
];

// === INSTALL ===
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE)
            .then(cache => cache.addAll(STATIC_ASSETS).catch(() => {}))
            .then(() => self.skipWaiting())
    );
});

// === ACTIVATE — alte Caches löschen ===
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys => {
            return Promise.all(
                keys.filter(k => !k.startsWith(CACHE_VERSION)).map(k => caches.delete(k))
            );
        }).then(() => self.clients.claim())
    );
});

// === FETCH ===
// Strategy:
// - HTML-Pages: Network-first, fallback Cache, fallback offline.html
// - Static assets: Cache-first, fallback Network
// - API/POST: immer Network (kein Cache)
self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Nur GET cachen
    if (req.method !== 'GET') return;

    // POST/API niemals cachen
    if (req.url.includes('/api/') || req.url.includes('/admin/')) return;

    const url = new URL(req.url);

    // Static assets: cache-first
    if (req.url.includes('/static/') || req.url.endsWith('.svg') || req.url.endsWith('.css') || req.url.endsWith('.js') || req.url.endsWith('.jpg') || req.url.endsWith('.png')) {
        event.respondWith(
            caches.match(req).then(cached => {
                if (cached) return cached;
                return fetch(req).then(resp => {
                    if (resp.ok) {
                        const copy = resp.clone();
                        caches.open(RUNTIME_CACHE).then(cache => cache.put(req, copy));
                    }
                    return resp;
                }).catch(() => cached);
            })
        );
        return;
    }

    // HTML: network-first
    if (req.headers.get('accept') && req.headers.get('accept').includes('text/html')) {
        event.respondWith(
            fetch(req)
                .then(resp => {
                    if (resp.ok) {
                        const copy = resp.clone();
                        caches.open(RUNTIME_CACHE).then(cache => cache.put(req, copy));
                    }
                    return resp;
                })
                .catch(() => {
                    return caches.match(req).then(cached => {
                        if (cached) return cached;
                        return new Response(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>Offline</title></head><body style="font-family:system-ui;background:#0a0e1a;color:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px"><div><h1 style="font-size:32px;margin-bottom:12px">📡 Offline</h1><p style="color:#94a3b8">Keine Verbindung. Sobald du wieder online bist, läuft alles wieder normal.</p><button onclick="location.reload()" style="margin-top:20px;padding:12px 24px;background:#d4a843;color:#0f1c3f;border:none;border-radius:10px;font-weight:700;cursor:pointer">Erneut versuchen</button></div></body></html>`, { headers: { 'Content-Type': 'text/html' } });
                    });
                })
        );
        return;
    }
});

// === PUSH NOTIFICATIONS ===
self.addEventListener('push', (event) => {
    let data = { title: 'NT Pro Academy', body: 'Neue Nachricht', icon: '/static/icons/icon-192.png' };
    if (event.data) {
        try { data = event.data.json(); } catch (e) { data.body = event.data.text(); }
    }
    const options = {
        body: data.body || '',
        icon: data.icon || '/static/icons/icon-192.png',
        badge: data.badge || '/static/icons/favicon-32.png',
        tag: data.tag || 'ntpro-notification',
        data: { url: data.url || '/dashboard' },
        vibrate: [100, 50, 100],
        requireInteraction: data.urgent || false,
        actions: data.actions || [],
    };
    event.waitUntil(self.registration.showNotification(data.title, options));
});

// === NOTIFICATION CLICK ===
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || '/dashboard';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            // Wenn schon ein Tab offen ist, fokussieren
            for (const client of clientList) {
                if (client.url.includes(self.location.origin) && 'focus' in client) {
                    client.navigate(url);
                    return client.focus();
                }
            }
            return clients.openWindow(url);
        })
    );
});
