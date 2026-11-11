v81 — Ozon keep-all backfill

Проблема:
- Ozon API отдавал тысячи элементов, но CRM сохраняла слишком мало чатов.
- Даже после ослабления фильтра история не уходила глубже 15.06.
- Безопаснее не угадывать, какие чаты служебные, а сохранять всё, что отдаёт Ozon, и потом уже скрывать/помечать лишнее.

Что изменено:
- Автоматическое удаление Ozon support/service chats выключено по умолчанию.
- Ozon connector по умолчанию не выбрасывает service-looking чаты.
- Backfill теперь по умолчанию include_service_chats=true.
- Backfill лимиты увеличены: max_chats default 5000, pages_per_variant default 50.
- В ответ backfill добавлен local_after_backfill: сколько чатов/сообщений стало после импорта.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v81_ozon_keep_all_backfill_2026-06-17.
5. Запустить:
   /api/debug/ozon/backfill-chats?max_chats=5000&pages_per_variant=50&history_pages=5&include_closed=1&include_service_chats=1
6. Потом проверить:
   /api/debug/ozon/chats

Если после v81 local_ozon_total_chats резко вырастет — проблема была в фильтрации.
Если не вырастет и min_last_message_at не уйдёт глубже — нужно смотреть connector_debug variants/requests/items и увеличивать pages_per_variant или искать другой Ozon endpoint/фильтр.
