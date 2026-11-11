v102 — analytics CTE bindings fix

Исправлено:
- Ошибка /api/analytics/chats:
  sqlite3.ProgrammingError: Incorrect number of bindings supplied.
- Причина: при исправлении drilldown в v101 лишний SQL-параметр попал в общий CTE аналитики.
- Исправлен основной расчёт аналитики и сохранён работающий drilldown.

Проверки:
- Python compile
- node --check app/static/app.js
- tools/check_project.py
- smoke-тест build_chat_analytics
- smoke-тест build_chat_analytics_drilldown

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — v102_analytics_cte_bindings_fix_2026-06-18.
5. Обновить браузер Ctrl+F5.
