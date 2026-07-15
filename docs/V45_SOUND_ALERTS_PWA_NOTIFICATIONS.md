# V45 sound alerts + PWA notifications

## Added

- `/static/manifest.webmanifest`
- `/static/sw.js`
- PWA icons:
  - `/static/icons/app-icon-180.png`
  - `/static/icons/app-icon-192.png`
  - `/static/icons/app-icon-512.png`

## Frontend

- Service Worker registration.
- Notification permission helper.
- Browser/system notifications through `registration.showNotification`.
- Click handler in service worker.

## Preserved

- v44 sound alerts.
- v43 real nav icon assets.
- v41 Ozon return links.
- Single static folder only.

## Important limitation

Full closed-app push requires backend Web Push subscriptions and server-side push send.

Version: v45-sound-alerts-pwa-notifications-20260630
