v91 — notifications startup fix

Исправлено:
- Ошибка запуска SQLite: "You can only execute one statement at a time".
- Блок миграции `task_comments` + `notifications` теперь выполняется через `conn.executescript`.
- Добавлены индексы notifications.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v91_notifications_startup_fix_2026-06-18.
