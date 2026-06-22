Patch: Ozon questions autosync while Questions view is open

- Adds frontend-triggered Ozon questions sync when the Questions section is opened.
- Repeats questions sync while the Questions section is active.
- Shares the sync lock with the manual button to prevent overlapping /api/questions/sync/ozon requests.
- Refreshes questions list, selected question and stats after successful sync.

Install into the site root with file replacement. Keep .env, data/crm.sqlite3, chat_attachments and server.py as configured on Fastfox.
