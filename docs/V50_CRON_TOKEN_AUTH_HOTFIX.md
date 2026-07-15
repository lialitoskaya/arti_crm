Arti CRM v50 cron token auth hotfix

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
- /api/background/tick теперь доступен для cron без cookie-авторизации.
- Безопасность сохраняется: сам endpoint всё равно проверяет token из query-параметра или заголовка x-crm-tick-token.
- Исправляет ошибку cron: HTTP/1.1 401 Unauthorized / Username/Password Authentication Failed.

После установки:
1. Заменить файлы.
2. Перезапустить приложение.
3. Проверить вручную:
   https://ВАШ-ДОМЕН/api/background/tick?token=ВАШ_РЕАЛЬНЫЙ_ТОКЕН

Ожидаемо:
- с правильным token: JSON ok/status
- с неправильным token: 403 Invalid background tick token

Версия:
v50-cron-token-auth-hotfix-20260630
