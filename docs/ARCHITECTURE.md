# Архитектура проекта

## Текущая структура

```text
app/
  main.py                 FastAPI app, API routes, sync orchestration
  repository.py           SQLite repository layer
  db.py                   schema/migrations/init
  schemas.py              Pydantic DTO
  services/
    analytics.py          SQL analytics/dashboard calculations
  connectors/
    base.py               unified connector contracts
    ozon.py               Ozon Seller API connector
    wildberries.py        WB Buyers Chat connector
    yandex_market.py      Yandex connector
    mock.py               local mock connector
  static/
    index.html            single-page UI markup
    app.js                frontend logic
    styles.css            frontend styles
```

## Главные зоны ответственности

### Connectors

Connectors должны только:
- ходить во внешний API;
- нормализовывать ответ в `UnifiedChat` / `UnifiedMessage`;
- хранить диагностический `last_sync_debug`.

Connectors не должны:
- напрямую писать в SQLite;
- принимать решения о статусах менеджера;
- удалять локальные данные.

### Repository

`repository.py` отвечает за:
- чтение/запись SQLite;
- idempotent upsert;
- индексы;
- защиту от дублей сообщений;
- сохранение ручных статусов менеджера.

Repository не должен:
- ходить во внешние API;
- содержать бизнес-логику конкретного маркетплейса, кроме безопасной нормализации/миграций.

### Main / sync orchestration

`main.py` сейчас содержит слишком много ответственности:
- routes;
- background tasks;
- marketplace sync;
- debug endpoints;
- AI reply;
- review/question sync.

Это рабочее состояние MVP, но для дальнейшей поддержки рекомендуется выносить код по модулям.

## Рекомендуемая будущая структура

```text
app/
  api/
    chats.py
    reviews.py
    questions.py
    knowledge.py
    users.py
    debug.py
  services/
    sync_service.py
    ozon_sync.py
    wb_sync.py
    ai_reply_service.py
    chat_service.py
  repositories/
    chats.py
    messages.py
    reviews.py
    questions.py
  services/
    analytics.py          SQL analytics/dashboard calculations
  connectors/
  static/
```

Переход лучше делать постепенно, без изменения поведения: сначала перенос функций, потом тесты, потом чистка старых маршрутов.
