v80 — Ozon support filter fix

Проблема:
- В backfill Ozon API отдавал тысячи чатов, но CRM сохраняла только десятки.
- Причина: слишком широкий фильтр служебных/поддержки Ozon.
- Слова вроде service/system/notification/news могли попадать в технический тип нормального клиентского чата.
- Из-за этого skipped_support_count мог быть огромным, например 2185.

Что исправлено:
- Фильтр Ozon стал консервативным: теперь пропускаются только явно служебные/поддержка/API/newsletter-чаты.
- Обычные buyer/customer/order/posting/return/claim чаты сохраняются.
- Убрано удаление чатов по широким словам service/system/notification/news.
- В debug теперь показываются samples с причиной пропуска.

Что сделать после установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v80_ozon_support_filter_fix_2026-06-17.
5. Запустить backfill:
   /api/debug/ozon/backfill-chats?max_chats=2000&pages_per_variant=20&history_pages=5&include_closed=1
6. Проверить:
   /api/debug/ozon/chats

Ожидаемый результат:
- skipped_support_count должен сильно уменьшиться.
- unique_chats и local_ozon_total_chats должны вырасти, если Ozon API отдаёт эти чаты.
