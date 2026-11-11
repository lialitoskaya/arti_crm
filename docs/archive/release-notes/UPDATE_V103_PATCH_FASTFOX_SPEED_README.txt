v103 Fastfox speed patch

- Adds threaded WSGI server.py for Fastfox PORT-based Python hosting.
- Keeps DATABASE_PATH and CRM_CHAT_ATTACHMENTS_DIR absolute in startup.
- Adds browser cache headers for /static/* and /api/chat-uploads/* to reduce repeated JS/CSS/image downloads.

Install in site root with replacement. Keep .env and data/crm.sqlite3. Restart Python and Ctrl+F5 once.
