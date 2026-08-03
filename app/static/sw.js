const ARTI_CRM_SW_VERSION = 'v85-yandex-oauth-cache-fix-20260803';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin !== self.location.origin || !requestUrl.pathname.startsWith('/api/auth/')) return;
  event.respondWith(fetch(event.request, { cache: 'no-store' }));
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = { title: 'Arti CRM', body: event.data ? event.data.text() : 'Новое уведомление' };
  }

  const title = payload.title || 'Arti CRM';
  const options = {
    body: payload.body || 'Новое событие',
    icon: payload.icon || '/static/icons/app-icon-192.png',
    badge: payload.badge || '/static/icons/app-icon-192.png',
    tag: payload.tag || 'arti-crm-event',
    renotify: true,
    data: {
      url: payload.url || '/',
      kind: payload.kind || '',
      entityId: payload.entityId || '',
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawTargetUrl = event.notification?.data?.url || '/';
  const targetUrl = new URL(rawTargetUrl, self.location.origin).href;

  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of allClients) {
      if ('focus' in client) {
        try {
          await client.focus();
          if ('navigate' in client && targetUrl) await client.navigate(targetUrl);
          return;
        } catch (error) {}
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
  })());
});
