Patch: CRM outbound echo de-duplication

Root cause fixed:
- Some marketplaces acknowledge a sent message without returning the final marketplace message id.
- The previous send handler could store raw `result` values such as True or an object as external_message_id.
- Later sync imported the same seller message with the real marketplace id, so the chat showed two outbound bubbles.

Changes:
- Only trusted scalar message id fields are used as external_message_id after send.
- CRM-sent local messages are marked in raw_json with _crm_sent_from_crm.
- Repository-level ingest upgrades a recent local/provisional outbound row when a marketplace echo arrives with the real id.
- Startup repair merges old already-created duplicate outbound echoes conservatively.

Install:
- Extract into the site root with replacement.
- Keep .env, data/crm.sqlite3 and chat_attachments.
- Restart Python and hard-refresh the browser.
