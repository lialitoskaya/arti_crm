# V36 fast chat load + mobile zoom fix

## Diagnosis

The chat detail endpoint reads:
- chat summary
- latest messages
- tasks

The message query used `ORDER BY datetime(created_at)`, which prevents SQLite from using a normal index efficiently.
Also, if background marketplace sync writes to SQLite, reads can wait. WAL improves read/write concurrency.

## Changes

### app/db.py

- `sqlite3.connect(..., timeout=15)`
- `PRAGMA busy_timeout=15000`
- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA temp_store=MEMORY`
- indexes:
  - `idx_messages_chat_created_id`
  - `idx_tasks_chat_created_id`

### app/repository.py

- `repo.get_chat()` now orders by `created_at` directly, not `datetime(created_at)`.
- This allows the new indexes to work.

### app/static/styles.css

- iOS input zoom fix: focused mobile chat fields use 16px font-size.

Version: v36-fast-chat-load-mobile-zoom-fix-20260629
