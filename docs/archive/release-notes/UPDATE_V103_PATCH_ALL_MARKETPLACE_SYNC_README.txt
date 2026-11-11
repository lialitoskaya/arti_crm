Patch: full Fastfox marketplace autosync bundle

Includes previous Fastfox fixes and adds operator-tab autosync for all configured chat marketplaces:
- Ozon chats: fast inbox sync while CRM is open.
- Wildberries chats: background-profile sync through /api/sync/operator, with WB rate-limit/cooldown protection.
- Yandex Market chats: background-profile sync through /api/sync/operator.
- Ozon questions: autosync remains active while the Questions section is open.

Note: the current CRM codebase has a dedicated Questions section only for Ozon questions.
WB/Yandex are checked/synced as chat marketplaces; separate WB/Yandex question modules do not exist in this build.

Install into the site root with file replacement. Keep .env, data/crm.sqlite3 and chat_attachments.
