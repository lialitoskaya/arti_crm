v98 — Yandex unread notifications

Что добавлено:
- Уведомления по Яндекс Маркету теперь тоже учитывают read/unread/actionable-состояние.
- Для Яндекса используется статус чата из API:
  - NEW / WAITING_FOR_PARTNER => чат требует ответа, можно показывать уведомление;
  - WAITING_FOR_CUSTOMER / FINISHED / CLOSED => уведомление по сообщению скрывается как прочитанное.
- В metadata_json для Yandex добавляется _sync_hint:
  - yandex_chat_status
  - yandex_needs_partner_reply

Важно:
У Яндекса не всегда есть отдельный unread_count как у Ozon/WB. Поэтому для уведомлений используется его бизнес-статус чата: ждёт ответа партнёра или нет.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v98_yandex_unread_notifications_2026-06-18.
5. Обновить браузер Ctrl+F5.
