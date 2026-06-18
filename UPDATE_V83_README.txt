v83 — Ozon fast inbox sync

Проблема:
- После глубокой догрузки Ozon history CRM могла долго заниматься backfill/архивом.
- Новые чаты могли появляться с задержкой, потому что обычная синхронизация и глубокая догрузка использовали похожий путь.

Что исправлено:
- Добавлена отдельная быстрая синхронизация Ozon для новых/последних чатов.
- Фоновый Ozon sync теперь использует отдельный свежий OzonConnector и лёгкий профиль:
  unread/opened/all, мало страниц, 1 страница истории.
- Deep backfill остаётся отдельным endpoint и больше не должен мешать быстрым новым чатам.
- Интервал Ozon background sync по умолчанию снижен до 20 секунд.
- Добавлен endpoint для ручной быстрой проверки:
  /api/debug/ozon/fast-sync

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v83_ozon_fast_inbox_sync_2026-06-18.
5. Проверить быстрый sync:
   /api/debug/ozon/fast-sync
6. Проверить общий фон:
   /api/debug/wb или /api/debug/version и список чатов через UI.

Рекомендованные .env:
OZON_FAST_INBOX_SYNC_ENABLED=true
OZON_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS=20
OZON_FAST_SYNC_MAX_CHATS=300
OZON_FAST_SYNC_PAGES_PER_VARIANT=3
OZON_FAST_HISTORY_PAGES=1
