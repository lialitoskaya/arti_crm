v78 — WB auto events import always-on

Что изменено:
- Автоматический импорт истории WB включается сам при запуске CRM.
- Больше не нужно открывать /api/debug/wb/import-events вручную.
- CRM сама ждёт cooldown=0, делает один безопасный запрос /seller/events, сохраняет cursor и планирует следующий запуск.
- Проверять можно через /api/debug/wb/import-events-auto?action=status.
- Отключить можно через /api/debug/wb/import-events-auto?action=stop.
- В .env можно выключить автопланировщик: WB_EVENTS_AUTO_IMPORT_ENABLED=false.

Что значит “1 страница”:
- Это не один чат.
- Это одна пачка событий WB /seller/events за один API-запрос.
- В одной пачке могут быть сообщения из разных чатов.
- Если WB возвращает 50 events, это может быть 50 сообщений/событий сразу по разным клиентам.
- Следующая пачка доступна только после следующего разрешённого запроса WB.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v78_wb_auto_events_always_on_2026-06-16.
5. Открыть /api/debug/wb/import-events-auto?action=status и убедиться, что enabled=true.
