v103 Fastfox polling throttle patch

- Stops the 5-second full refresh loop on chat view.
- Reduces sync status and notification polling to 60 seconds.
- Prevents overlapping /chats, /notifications, /stats and /sync/status requests.
- Stops Ozon frontend autosync from reloading chat list on every tick when nothing changed.
- Keeps manual refresh and 30-second Ozon autosync while CRM is open.

Install in site root with replacement. Keep .env, data/crm.sqlite3 and chat_attachments. Restart Python and Ctrl+F5.
