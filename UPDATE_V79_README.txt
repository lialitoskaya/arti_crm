v79 — Ozon history backfill

Проблема:
- обычная синхронизация Ozon была лёгкой: ограниченное количество страниц списка чатов и только первая страница истории чата.
- поэтому локальная база могла содержать не всю историю и не все старые/закрытые чаты.

Что добавлено:
- Ozon chat history теперь поддерживает постраничную догрузку через OZON_HISTORY_PAGES.
- новый endpoint диагностики: /api/debug/ozon/chats
- новый endpoint глубокого импорта: /api/debug/ozon/backfill-chats
  пример:
  /api/debug/ozon/backfill-chats?max_chats=2000&pages_per_variant=20&history_pages=5&include_closed=1

Важно:
- CRM может сохранить только то, что Ozon API реально отдаёт.
- Если Ozon API не отдаёт старую историю, CRM не сможет восстановить её задним числом.
- После backfill вся полученная история хранится локально в crm.sqlite3.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v79_ozon_history_backfill_2026-06-17.
5. Открыть /api/debug/ozon/chats.
6. Запустить глубокий импорт:
   /api/debug/ozon/backfill-chats?max_chats=2000&pages_per_variant=20&history_pages=5&include_closed=1
