Arti CRM v49 push status storage hotfix

Это НЕ полный проект. Не распаковывать поверх всей папки проекта.

Заменить / добавить только:
- app/main.py
- app/db.py
- app/repository.py
- app/static/app.js
- app/static/index.html
- app/static/styles.css
- app/static/sw.js
- app/static/manifest.webmanifest
- app/static/icons/app-icon-180.png
- app/static/icons/app-icon-192.png
- app/static/icons/app-icon-512.png
- app/static/icons/nav-analytics.svg
- app/static/icons/nav-chats.svg
- app/static/icons/nav-knowledge.svg
- app/static/icons/nav-questions.svg
- app/static/icons/nav-reviews.svg
- app/static/icons/nav-tasks.svg
- requirements_push.txt

Что исправлено:
- /api/push/status больше не должен падать с Internal Server Error;
- push-таблицы push_subscriptions и push_outbox создаются/ремонтируются прямо из push-функций, даже если init_db не успел применить миграцию;
- /api/push/status теперь возвращает storage_error текстом вместо 500.

После установки:
1. Заменить файлы.
2. Перезапустить приложение.
3. Открыть:
   https://ВАШ-ДОМЕН/api/push/status

Нормально, если configured=false, если VAPID ключи еще не заданы.
Ненормально, если storage_error не пустой — пришлите его текст.

Версия:
v49-push-status-storage-hotfix-20260630
