v64 — WB: история сообщений и время сообщений

Что исправлено:
- lastMessage WB теперь берёт время из addTimestamp в первую очередь.
- Игнорируются placeholder-даты WB вида 0001-01-01.
- Локальный ремонт WB больше не должен создавать дубликат lastMessage после исправления времени.
- /seller/events теперь запрашивается с lookback-окном WB_EVENTS_LOOKBACK_DAYS=30, чтобы подтягивать не только последнее сообщение, а историю событий за период.
- Добавлен ручной импорт истории:
  GET/POST /api/debug/wb/import-events?days=30&pages=20&max_events=5000

Важно:
- Пока WB cooldown/429 активен, можно восстановить только lastMessage из локальной metadata.
- Полную историю можно подтянуть только после cooldown=0 через /api/debug/wb/import-events или обычную синхронизацию WB.

После установки:
1. Остановить CRM.
2. Распаковать архив в C:\crm_marketplaces с заменой файлов.
3. Запустить CRM.
4. Проверить /api/debug/version — должна быть v64_wb_history_time_fix_2026-06-15.
5. Открыть /api/debug/wb/repair-local — обновит время lastMessage из addTimestamp.
6. Когда cooldown=0, открыть /api/debug/wb/import-events?days=30&pages=20&max_events=5000 — подтянет историю сообщений из WB events.
