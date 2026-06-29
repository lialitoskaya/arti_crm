# V37 async operator sync

## Problem

Frontend marketplace sync was awaiting `/api/sync/operator` with a 25-second timeout.
If marketplace APIs took 20-25 seconds, the browser request stayed pending and UI updates waited.

## Fix

### Backend

`POST /api/sync/operator` is now async-by-default:

- starts a background task
- returns immediately with `status=started`
- returns `status=running` if a sync is already in progress
- supports old blocking behavior through `?wait=true`

Ozon operator sync profile is lighter by default:

- `OZON_OPERATOR_FAST_SYNC_MAX_CHATS=80`
- `OZON_OPERATOR_FAST_SYNC_PAGES_PER_VARIANT=1`

### Frontend

- Calls `/api/sync/operator` with a 5s timeout.
- Does not wait for marketplace sync to finish.
- Keeps refreshing local chat/messages.
- Active chat local poll interval changed from 9s to 5s.

Version: v37-async-operator-sync-20260629
