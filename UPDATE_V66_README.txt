v66 — WB safe events import for X-Ratelimit-Limit=1

Что показал WB:
- X-Ratelimit-Limit=1
- X-Ratelimit-Retry=3598
- X-Ratelimit-Reset=3598

Это значит, что /api/v1/seller/events для этого токена фактически даёт 1 запрос примерно в час. Полную историю нельзя быстро выгрузить несколькими страницами подряд — второй запрос сразу вернёт 429.

Исправлено:
- CRM больше не вызывает /seller/events автоматически при обычной синхронизации списка WB-чатов.
- /api/debug/wb/import-events по умолчанию делает только 1 страницу/1 запрос.
- Если WB снова отдаёт 429, endpoint возвращает понятный JSON, а не падает 502.
- lastMessage из /seller/chats продолжает работать для списка чатов и сортировки.
- Историю можно подтягивать безопасно: один запуск import-events после cooldown=0.

Установка:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v66_wb_safe_events_import_2026-06-15.

Как пользоваться:
1. Откройте /api/debug/wb и дождитесь cooldown_remaining_seconds=0.
2. Откройте /api/debug/wb/import-events
3. Если WB вернёт X-Ratelimit-Limit=1, повторять импорт можно только после следующего cooldown.
