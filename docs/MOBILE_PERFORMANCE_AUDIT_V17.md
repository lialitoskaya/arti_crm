# Arti CRM v17 — mobile first-open performance and chat list refresh fix

## Root cause found

The first mobile chat open felt slow because several expensive things happened around the same tap:

1. The chat switched to mobile full-screen state, but the real header/messages waited for `/api/chats/{id}`.
2. `openChat()` rendered messages, tasks and the chat list close together.
3. `refreshActiveChatUi()` could run from the timer and trigger `loadChats()` + `openChat()` again near the first open.
4. `loadChats()` always called `loadStats()`, which made even simple list refreshes heavier.
5. New chats could be missed visually because the optimized sync branch refreshed the list only when `/api/sync/operator` reported changes. If the backend/background worker had already imported data, the sync endpoint could return zero while the local DB changed.

## What v17 changes

### Faster first mobile open

- The chat header is painted immediately from the selected chat summary.
- The full-screen mobile state is applied before the API response.
- Mobile first-load messages limit is reduced to 35.
- Task rendering is deferred with `requestIdleCallback`/fallback timeout.
- Chat list re-rendering after open is deferred.
- Background active-chat refresh is blocked briefly while a chat is opening.

### New chats refresh

- `loadChats()` now supports options:
  - `withStats`
  - `render`
- Periodic chat-list refresh can update the list without always loading stats.
- The sync loop now occasionally refreshes the list even when sync returns no changes.
- `refreshActiveChatUi()` no longer reopens the active chat on mobile timers.

## What still requires backend work

If new chats are not imported into the backend database at all, frontend refresh cannot invent them.
The backend should have:
- marketplace queue per marketplace/businessId;
- Yandex semaphore with max 4 parallel requests;
- deduplication of sync jobs;
- a lightweight `/api/chats` list endpoint separated from heavy message loading.
