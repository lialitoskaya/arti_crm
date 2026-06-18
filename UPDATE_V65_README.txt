v65 — WB: исправление импорта истории events

Что было не так:
- В v64 CRM отправляла первый запрос /api/v1/seller/events с параметром next=timestamp.
- По документации WB первый запрос событий должен быть БЕЗ параметра next.
- Затем нужно брать next из ответа WB и повторять запрос, пока totalEvents не станет 0.
- Из-за этого история могла не подтягиваться, а в CRM оставался только lastMessage из списка чатов.

Исправлено:
- Первый запрос /api/v1/seller/events теперь идёт без next.
- Следующие страницы идут строго по next из ответа WB.
- В debug добавлен connector_debug.events_pages_debug, где видно:
  request_next, response_next, totalEvents, events_count, first_event_keys, first_event_chat_id.
- Версия: v65_wb_events_pagination_fix_2026-06-15.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version.
5. Когда cooldown=0, открыть:
   /api/debug/wb/import-events?days=30&pages=20&max_events=5000

Если снова будет только последнее сообщение:
- пришлите полный результат /api/debug/wb/import-events?days=30&pages=20&max_events=5000
- особенно connector_debug.events_pages_debug
