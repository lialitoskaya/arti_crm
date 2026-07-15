# V48 push safe foreground + background tick

## Fixed

### Foreground loading
`loadChats()` now renders visible UI after awaiting an already-running background promise.

### External background tick
Added:

- `GET /api/background/tick?token=...`
- `POST /api/background/tick?token=...`

The endpoint runs:

- Ozon fast inbox sync
- Ozon questions sync
- push outbox drain

### Diagnostics
`/api/push/status` now includes:

- VAPID key presence flags
- active subscriptions count
- last push outbox state
- last background sync
- last external background tick
- whether tick token is configured

Version: v48-push-safe-foreground-and-background-tick-20260630
