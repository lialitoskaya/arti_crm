v82 — repository os import fix

Что исправлено:
- Исправлена ошибка запуска v81:
  NameError: name 'os' is not defined
- В app/repository.py добавлен import os.
- Логика v81 сохранена: Ozon backfill сохраняет все чаты, которые отдаёт API, без опасной автофильтрации.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v82_repository_os_import_fix_2026-06-17.
5. Запустить:
   /api/debug/ozon/backfill-chats?max_chats=5000&pages_per_variant=50&history_pages=5&include_closed=1&include_service_chats=1
