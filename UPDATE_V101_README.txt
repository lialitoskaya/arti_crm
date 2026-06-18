v101 — analytics drilldown bindings fix

Исправлено:
- Ошибка /api/analytics/chats/drilldown:
  sqlite3.ProgrammingError: Incorrect number of bindings supplied.
- Причина: в SQL детализации аналитики было 8 placeholder `?`, а передавалось 7 параметров.
- Исправлены параметры основного списка строк и списка исключённых служебных сообщений.

Проверки:
- Python compile
- node --check app/static/app.js
- tools/check_project.py
- smoke-тест build_chat_analytics_drilldown на временной SQLite базе

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — v101_analytics_drilldown_bindings_fix_2026-06-18.
5. Обновить браузер Ctrl+F5.
