Arti CRM v51 push catch-up notifications

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
- Push test уже отправляет sent=3, значит Web Push работает.
- Но background tick показывал push_outbox total=0, то есть после Ozon sync CRM не создавала push-задачи для новых/ожидающих сообщений.
- В v51 добавлен idempotent catch-up:
  - после фоновой синхронизации проверяет чаты, где последний ответ от клиента и чат ждёт ответа;
  - если уведомление по этому message_id ещё не создано, создаёт notification + push_outbox;
  - аналогично проверяет новые неотвеченные вопросы Ozon;
  - затем сразу отправляет push_outbox.

После установки:
1. Заменить файлы.
2. Перезапустить приложение.
3. Дождаться cron или вручную открыть:
   https://ВАШ-ДОМЕН/api/background/tick?token=ВАШ_ТОКЕН
4. Проверить /api/push/status.
   В last_external_background_tick должно появиться:
   notification_catchup.messages.created
   notification_catchup.questions.created
   push_outbox.sent

Версия:
v51-push-catchup-notifications-20260630
