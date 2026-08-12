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

## Personal chat read state

`chat_user_states` is the personal source of truth for read/unread state. Its
primary key is `(user_id, chat_id)`; it stores `last_read_message_id`,
`last_read_at`, and `is_marked_unread`. The table is additive, created with
`CREATE TABLE IF NOT EXISTS`, and uses cascading user/chat foreign keys.

The execution flow is:

```text
PATCH /api/chats/{chat_id}/read-state
  -> authenticated current user + shared CSRF middleware
    -> repository.set_chat_read_state()
      -> chat_user_states
```

`repository.add_message()` creates missing personal boundaries in the same SQLite
transaction only for a recent inbound message that is the newest dialog message.
Outbound messages do not touch the table. Chat list/summary queries left-join the
current user's row and compute canonical `is_unread` without loading history or
calling a marketplace connector.

The frontend applies PATCH optimistically to one in-memory chat and one rendered
row. A per-chat single-flight operation version plus GET request snapshot prevents
responses started before or during the mutation from restoring stale state. The
chat list uses one delegated click/keyboard handler; no per-row listeners are
created.

Rollback to older application code leaves the additive table and index unused.
Restoring this version resumes the same personal state; no destructive down
migration is required.

## Personal chat pin state

The same `chat_user_states` row also stores personal pin metadata in `is_pinned`
and `pinned_at`; no second user/chat state table is introduced. Pinning an old chat
lazily initializes its read boundary to the current local message so pre-existing
history does not become unread as a side effect.

The execution flow is:

```text
PATCH /api/chats/{chat_id}/pin-state
  -> authenticated current user + shared CSRF middleware
    -> repository.set_chat_pin_state()
      -> chat_user_states
```

Chat list SQL sorts the current user's pinned chats first and preserves descending
message activity order inside pinned and ordinary groups. The frontend applies a
per-chat optimistic update with rollback and stale-GET protection, then refreshes
the bounded loaded prefix to reconcile global ordering. A single delegated list
handler owns the control; clicking the pin never opens the chat.

## Bounded chat-list lazy loading

The browser uses the opt-in paginated form of `GET /api/chats` as a transport
contract, but presents it as infinite scroll rather than numbered pages. The
first request loads 30 chats and each near-end scroll requests the next 30.
SQLite applies `LIMIT/OFFSET` before serialization and returns canonical `total`
and personal `unread_total` counters separately from each batch. The legacy list
response remains available when `paginated` is omitted, so existing internal
consumers are not broken.

Filters, search, owner scope, and archive scope reset the feed to the first
30-item batch. Passive refreshes update the already loaded prefix instead of
resetting the operator to page one. Duplicate ids are rejected when a later
batch is merged, only one list request may be in flight, and stale filter/search
responses are ignored. The DOM therefore starts with 30 rows and grows only as
the operator actually scrolls through older dialogs.

## Canonical CRM message identity

CRM-origin provenance is stored in structured `messages` columns rather than
being inferred from marketplace payload JSON:

- `is_crm_sent` marks messages created by an authenticated CRM send operation;
- `crm_author_user_id` and `crm_author_label` preserve the responsible employee;
- `client_operation_id` is the idempotency key generated once by the browser and
  reused for every retry of the same send.

The canonical persistence flow is:

```text
authenticated send route
  -> stable client_operation_id
    -> marketplace connector send
      -> repository.add_message()
        -> one identity reconciliation path
          -> messages + unique identity indexes
```

Marketplace send acknowledgements are audit data and are not assumed to be the
same identifier later returned by message history. The acknowledgement id stays
in `raw_json` as `_crm_send_ack_message_id`; the provider history id becomes the
canonical `external_message_id` when synchronization reconciles the echo.

`repository._add_message_conn()` is the only message insert/update boundary. It
resolves identity in this order: client operation id, exact provider id, explicit
WB event identities, then one unambiguous opposite-origin outbound text/time
counterpart. Ambiguous repeated identical replies are never merged by guesswork.
Database partial unique indexes enforce `(chat_id, external_message_id)` and
`(chat_id, client_operation_id)` under concurrent sync/retry races.

The one-time `20260806_message_identity` migration backfills structured CRM
provenance, resolves employee labels from stored user ids, merges only safe old
duplicates, and records completion in `schema_migrations`. Repeated startup repair
jobs and scattered post-hoc duplicate deletion paths are not used.

## Chat message calendar-date filter

The calendar control in the chat header is a transport-level filter over the
canonical `messages.created_at` timeline. The browser sends the selected local
calendar date together with `Date.getTimezoneOffset()`. The route converts that
pair to one half-open UTC range `[start, end)` and passes the boundaries to the
repository; the repository owns the SQL predicate and ordering.

Both initial chat opening and passive message refresh use the single
`chatMessagesRequestUrl()` frontend builder. This prevents the foreground and
background paths from drifting into different filter behavior. Switching to a
different chat clears the date filter; refreshing the same chat preserves it.
The repository still applies the existing bounded message limit inside the
selected day, so the filter never turns into an unbounded history load.

## Canonical task filtering and date ordering

The Tasks view sends search, task type, status/bucket, due date, and responsible
employee filters to `GET /api/tasks`. The browser no longer re-filters the
returned task array, so one repository query is the source of truth for both
filter membership and ordering.

`tasks.due_at` is the canonical task date entered during creation or editing.
Task list queries and tasks embedded in a chat use the same ordering rule:
ascending `due_at`, with undated tasks last and `id` as the deterministic tie
breaker. Workflow status does not create a second sort group. The explicit
`assigned_user_id` query parameter filters by responsible employee; `mine=true`
intentionally takes precedence and resolves to the authenticated user's id.
