# Arti CRM v19 — desktop live message refresh

## Problem

After v18, mobile refresh was improved, but desktop still depended on the old heavy path:

- sync reports messages;
- load chat list;
- run full openChat for the current desktop chat.

If sync was slow, returned zero, or was blocked by an in-flight state, desktop did not reliably show new marketplace messages.

## Fix

v19 makes active chat message refresh independent and shared for desktop and mobile.

### Key changes

1. `refreshCurrentChatMessagesOnly()` is now the primary way to update an opened chat.
2. The chat timer calls it directly every 5 seconds.
3. Desktop no longer uses full `openChat()` just to poll messages.
4. If sync returns zero, the app still checks the current chat from local DB.
5. `focus` / `visibilitychange` triggers quick refresh of both list and current chat.
6. `api()` now supports request timeout to prevent stuck fetches from blocking refresh state.

## Backend boundary

Frontend can only display messages already present in the CRM database.
If marketplace sync does not import them, v19 cannot fix it alone.
Backend must be checked for:
- background worker health;
- `/api/sync/operator` result;
- marketplace API errors/rate limits;
- per-business queue/sync locks.
