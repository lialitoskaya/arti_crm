v63 — ремонт пустых WB-чатов без обращения к WB API

Что было видно по /api/debug/wb:
- local_total_in_db=100
- messages_count=0
- last_message_at=null
- WB cooldown active: ещё 1430 сек

Это значит: WB-чаты уже сохранены в локальную БД, но сообщения не были импортированы. Пока активен cooldown/429, CRM не может сходить в WB за events/history.

Исправлено:
- Добавлен локальный ремонт WB-чатов из уже сохранённого metadata_json.
- Если в metadata_json есть WB lastMessage, CRM создаст из него локальное сообщение.
- Это заполнит last_message_at / last_message_preview и чаты начнут сортироваться по времени.
- Ремонт не делает запросов в WB API, поэтому его можно запускать во время 429 cooldown.
- При успешной будущей синхронизации WB CRM тоже сразу импортирует lastMessage из /seller/chats, даже если events недоступны.
- Добавлен endpoint:
  GET/POST /api/debug/wb/repair-local

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v63_wb_local_lastmessage_repair_2026-06-15.
5. Открыть /api/debug/wb/repair-local
6. Затем открыть /api/debug/wb и проверить, что messages_count и last_message_at начали заполняться.
