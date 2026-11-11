v84 — codebase maintenance docs

Что исправлено:
- Проверена структура проекта.
- Добавлены docs/ARCHITECTURE.md, SYNC_STRATEGY.md, ENV_REFERENCE.md, MAINTENANCE.md, CODE_AUDIT.md.
- README обновлён под текущую версию и реальные endpoints.
- Добавлен tools/check_project.py для базовой проверки проекта.
- Ozon system-history deletion окончательно переведён в safe mode:
  по умолчанию CRM не удаляет Ozon-чаты автоматически.
- Deep backfill теперь также отключает system-history exclusion на время include_service_chats=1.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v84_codebase_maintenance_docs_2026-06-18.
5. Для dev-проверки можно выполнить:
   python tools/check_project.py
