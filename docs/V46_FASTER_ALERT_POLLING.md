# V46 faster alert polling

## Root cause

Notification delay came from multiple intervals:

- `notificationsTimer`: 60s
- frontend Ozon sync: 10s + backend throttle
- Ozon questions min gap: 45s
- backend Ozon min interval: 20s

## Changes

### Frontend

- `FRONTEND_OZON_SYNC_INTERVAL_MS`: 10000 -> 5000
- `FRONTEND_OZON_SYNC_MIN_GAP_MS`: 8000 -> 5000
- `ACTIVE_CHAT_MESSAGES_REFRESH_INTERVAL_MS`: 5000 -> 3000
- `FRONTEND_OZON_QUESTIONS_SYNC_MIN_GAP_MS`: 45000 -> 12000
- `notificationsTimer`: 60000 -> 10000
- Added `alertsTimer` for quick alert polling outside chat/question sections.
- `loadQuestions()` can now run without rendering UI but still trigger sound/PWA alerts.

### Backend

- Ozon background min interval default: 20s -> 8s.
- Ozon operator fast sync max chats default: 80 -> 50.

Version: v46-faster-alert-polling-20260630
