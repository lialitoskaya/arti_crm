# Стратегия синхронизации

## Общий принцип

CRM хранит историю локально в SQLite. API маркетплейсов используются для получения новых событий и восстановления истории.

## Ozon

Есть два режима:

### Fast inbox sync

Endpoint:

```text
/api/debug/ozon/fast-sync
```

Назначение:
- быстро подтягивать новые/последние чаты;
- работать часто в фоне;
- не сканировать весь архив.

Настройки:

```env
OZON_FAST_INBOX_SYNC_ENABLED=true
OZON_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS=20
OZON_FAST_SYNC_MAX_CHATS=300
OZON_FAST_SYNC_PAGES_PER_VARIANT=3
OZON_FAST_HISTORY_PAGES=1
```

### Deep backfill

Endpoint:

```text
/api/debug/ozon/backfill-chats?max_chats=5000&pages_per_variant=50&history_pages=5&include_closed=1&include_service_chats=1
```

Назначение:
- восстановить старую историю;
- пройти глубже по страницам Ozon;
- сохранить всё, что отдаёт API, чтобы не потерять клиентские диалоги.

Безопасная политика v84:
- не удалять Ozon-чаты автоматически;
- не выбрасывать чаты по словам `service`, `system`, `notification`;
- сначала сохранить, потом классифицировать/скрывать.

## Wildberries

WB history идёт через events и может иметь строгий hourly cooldown.

Нормальная модель:
- `/seller/chats` даёт список и `lastMessage`;
- `/seller/events` догружает историю;
- CRM хранит cursor в `.wb_events_cursor.json`;
- auto planner ждёт cooldown и сам импортирует по одной разрешённой пачке.

Проверка:

```text
/api/debug/wb
/api/debug/wb/import-events-auto?action=status
```

## Почему нельзя каждый раз грузить всю историю из API

- API может ограничивать глубину истории.
- API имеет rate limits.
- Интерфейс будет тормозить.
- При сбое API менеджер потеряет доступ к истории.

Поэтому старая история должна читаться из SQLite, а API должен использоваться только для синхронизации.

## Связь с аналитикой

Аналитика строится только по локальной SQLite-базе. Поэтому полнота отчётов напрямую зависит от того, насколько хорошо сработала синхронизация истории маркетплейсов.
