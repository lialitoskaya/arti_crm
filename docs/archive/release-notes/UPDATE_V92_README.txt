v92 — analytics NameError fix

Исправлено:
- Ошибка загрузки аналитики после v91.
- В app/services/analytics.py поля answered_chats/unanswered_chats больше не ссылаются на несуществующие переменные.
- Исправлено количество SQL-параметров в CTE расчёта среднего времени ответа.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v92_analytics_nameerror_fix_2026-06-18.
5. Открыть раздел Аналитика.
