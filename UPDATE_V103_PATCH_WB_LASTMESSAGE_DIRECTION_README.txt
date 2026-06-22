WB lastMessage sender detection fix

Install by extracting this archive into the project root with replacement.
Do not replace .env, data/crm.sqlite3, chat_attachments, or your production server.py unless you intentionally want to update it.

Changes:
- Detect WB chat-list lastMessage direction from the surrounding chat item clientName marker.
- Empty clientName around lastMessage is treated as buyer/customer (inbound).
- Non-empty clientName around lastMessage is treated as seller/operator (outbound).
- The rule is scoped to WB lastMessage rows only; full WB event/history messages still prefer explicit sender/flag fields.
- Existing old WB lastMessage rows are updated on next sync instead of duplicated.
