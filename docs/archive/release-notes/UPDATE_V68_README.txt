v68 — WB events: создавать чаты, которых нет в локальном списке

Причина, почему после import-events ничего не менялось:
- WB /seller/events возвращал события, но они могли относиться к chatID, которых нет в текущих 100 чатах из /seller/chats.
- Старый импорт просто пропускал такие events: if not local: continue.
- Поэтому events_count мог быть > 0, но messages_imported_or_updated оставался 0, и в видимых чатах ничего не менялось.

Исправлено:
- /api/debug/wb/import-events теперь создаёт локальный WB-чат из event, если такого chatID ещё нет в CRM.
- Затем импортирует сообщения в этот созданный чат.
- В ответ добавлены поля:
  chats_created_from_events
  created_sample
  parser_skipped_events
  parser_skipped_sample
- /api/debug/version теперь показывает app_version=0.68.0.

Как пользоваться:
1. Установить архив поверх C:\crm_marketplaces.
2. Перезапустить CRM.
3. Дождаться /api/debug/wb cooldown_remaining_seconds=0.
4. Открыть /api/debug/wb/import-events один раз.
5. Проверить поля messages_imported_or_updated и chats_created_from_events.
