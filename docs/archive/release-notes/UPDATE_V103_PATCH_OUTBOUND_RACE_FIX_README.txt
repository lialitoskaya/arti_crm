Patch: prevent outbound duplicate race before it reaches UI

Install into the project/site root with replacement.
Keep existing .env, data/crm.sqlite3 and chat_attachments.
Restart Python and hard-refresh the browser.

What changed:
- backend now handles the reverse race where marketplace sync imports seller echo before the CRM send endpoint saves the local outbound row;
- frontend pauses autosync briefly while an operator is sending a message;
- this prevents the temporary duplicate bubble from appearing first and only being removed later.
