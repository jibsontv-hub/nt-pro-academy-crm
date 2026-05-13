// NT Pro Academy — Service Worker v6
// v6: SWR (Stale-While-Revalidate) für HTML — App fühlt sich INSTANT an.
// Vorher: Network-First → 1-3 Sek weißer Screen bei jedem Page-Load.
// Jetzt: cached HTML wird SOFORT angezeigt, frisches HTML im Hintergrund
// geladen + Cache aktualisiert. Beim nächsten Visit ist's frisch.

const CACHE_VERSION = 'ntpro-v6';
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

// === ACTIVATE — NUKE-RESET ===
self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        // 1. ALLE Caches löschen die nicht zur aktuellen Version gehören
        const keys = await caches.keys();
        await Promise.all(
            keys.filter(k => !k.startsWith(CACHE_VERSION)).map(k => caches.delete(k))
        );
        // 2. Sofort alle Clients (Tabs) übernehmen — auch die schon offen sind
        await self.clients.claim();
        // 3. Allen Tabs sagen: lade neu! (nur einmal pro SW-Update)
        const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        for (const client of allClients) {
            client.postMessage({ type: 'FORCE_RELOAD', version: CACHE_VERSION });
        }
    })());
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

    // HTML: Stale-While-Revalidate — INSTANT-Render aus Cache, Update im Hintergrund.
    // Wenn nichts im Cache: Network. Wenn Network failt + nichts im Cache: Offline-Page.
    if (req.headers.get('accept') && req.headers.get('accept').includes('text/html')) {
        event.respondWith(
            caches.open(RUNTIME_CACHE).then(cache =>
                cache.match(req).then(cached => {
                    // Background-Fetch — Cache wird im Hintergrund aktualisiert
                    const networkPromise = fetch(req).then(resp => {
                        if (resp.ok) {
                            cache.put(req, resp.clone());
                        }
                        return resp;
                    }).catch(() => null);

                    // Wenn cached vorhanden: SOFORT zurückgeben (kein weißer Screen).
                    // Sonst auf Network warten.
                    if (cached) return cached;
                    return networkPromise.then(resp => resp || new Response(
                        `<!DOCTYPE html><html><head><meta charset="utf-8"><title>Offline</title></head><body style="font-family:system-ui;background:#0a0e1a;color:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px"><div><h1 style="font-size:32px;margin-bottom:12px">📡 Offline</h1><p style="color:#94a3b8">Keine Verbindung. Sobald du wieder online bist, läuft alles wieder normal.</p><button onclick="location.reload()" style="margin-top:20px;padding:12px 24px;background:#d4a843;color:#0f1c3f;border:none;border-radius:10px;font-weight:700;cursor:pointer">Erneut versuchen</button></div></body></html>`,
                        { headers: { 'Content-Type': 'text/html' } }
                    ));
                })
            )
        );
        return;
    }
});

// === MESSAGE — Manueller Reset über Client-API ===
// Frontend kann `navigator.serviceWorker.controller.postMessage({type:'NUKE_CACHE'})` schicken,
// dann werden alle Caches sofort gelöscht und Reload getriggert.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'NUKE_CACHE') {
        event.waitUntil((async () => {
            const keys = await caches.keys();
            await Promise.all(keys.map(k => caches.delete(k)));
            const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
            for (const client of allClients) {
                client.postMessage({ type: 'FORCE_RELOAD', version: CACHE_VERSION });
            }
        })());
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
