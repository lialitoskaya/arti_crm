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
- `client_operation_id` is the required idempotency key generated once by the browser
  and reused for every retry of the same send; the route never invents a replacement
  key for a request that omitted it.

Text send intent is persisted before marketplace I/O. The canonical flow is:

```text
authenticated send route
  -> stable client_operation_id
    -> message_send_operations (durable normalized payload + server author)
      -> conditional SQLite claim and committed transaction
        -> MessageSendService dispatcher
          -> marketplace connector send (outside the transaction)
            -> accepted/uncertain/permanent outcome
              -> provider history echo
                -> repository._add_message_conn()
                  -> one canonical messages row + confirmed operation
```

`message_send_operations` stores the complete connector-normalized `payload_json`,
its hash,
the server-derived author, target snapshot, attempt/lease fields and only sanitized
error metadata. Its state machine is `pending -> sending -> accepted -> confirmed`,
with `retry_wait` only after a structured result proves that no external side effect
occurred. Ambiguous timeout, transport and provider HTTP outcomes move to
`uncertain`; stale `sending` leases also move to `uncertain` and are never
automatically resent. `permanent_failed` is terminal.
The normalized payload is exactly what is dispatched: for example, Wildberries text
over its confirmed 1000-character limit is rejected before enqueue instead of being
silently truncated after hashing.

Claim uses `BEGIN IMMEDIATE` plus a conditional update, a unique token and a
90-second UTC lease. The connector call has a 60-second hard timeout and starts
only after the claim transaction commits. Completion requires the matching token;
a late success may move that token's stale `uncertain` claim to `accepted`, while a
late error cannot overwrite `uncertain` or `confirmed`. Multiple application
processes may run the same bounded drain because SQLite, not an in-memory lock,
owns the concurrency guarantee.

The exact guarantee is:

> Durable registration, не более одной одновременной marketplace-попытки для
> одной operation и отсутствие автоматического повтора после неоднозначного
> исхода. Новая автоматическая попытка разрешена только после структурированного
> результата, доказывающего отсутствие внешнего side effect.

This is not exactly-once and not at-most-one attempt over the complete operation
lifetime. One operation may have sequential safe attempts, but only one claim can
be active at a time. Without provider-native idempotency, a crash after provider
acceptance and before local ACK is indistinguishable from a crash before acceptance;
the operation remains `uncertain` until provider-history reconciliation or manual
resolution outside this slice.

Text messages and attachment captions share `command_kind='chat_text'` and the
same dispatcher. `intent_origin` records `message` or `attachment_caption` locally
but is not provider identity. The attachment endpoint rejects a nonempty caption;
after a caption becomes `accepted` or `confirmed`, the browser uploads only files
with a separate attachment operation id. File-bundle idempotency is not claimed.

`repository._add_message_conn()` is the only message insert/update boundary. It
resolves identity in this order: client operation id, an external id explicitly
proven by the connector to be the history identity, explicit WB event identities,
then a bounded FIFO payload-hash match. The fallback requires an echo timestamp
between 120 seconds before and 900 seconds after the operation's `last_attempt_at`;
old identical history cannot claim a new operation. Ambiguous repeated replies are
kept as distinct FIFO operations and messages rather than merged by text alone.
Database partial unique indexes enforce `(chat_id, external_message_id)` and
`(chat_id, client_operation_id)` under concurrent sync/retry races.

The one-time `20260806_message_identity` migration backfills structured CRM
provenance, resolves employee labels from stored user ids, merges only safe old
duplicates, and records completion in `schema_migrations`. Repeated startup repair
jobs and scattered post-hoc duplicate deletion paths are not used.
The additive `20260814_message_send_command_outbox` migration runs only after that
identity contract. It validates the complete column, CHECK, foreign-key, and index
contract. A nonempty partial outbox schema fails closed because its canonical payload
and server attribution cannot be reconstructed safely.

## Chat-list calendar-date filter

The calendar control belongs to the chat-list filters, not to an opened dialog.
It filters chats by the timestamp of each chat's latest canonical message. The
browser sends an inclusive local calendar range together with
`Date.getTimezoneOffset()`. The route validates both boundaries, converts them
to one half-open UTC interval `[start, end)` and passes those boundaries into
the same repository query used by lazy loading, counters and background list
refreshes.

The open-chat endpoint always returns the normal bounded message history and has
no date-filter parameters. This keeps message loading, polling and read-state
behavior independent from list discovery filters. Changing or clearing the chat
range resets the lazy feed to its first 30-item batch; the active range is part
of the stale-response key so an older request cannot overwrite the new result.

## Canonical task filtering and date ordering

The standalone task calendar filter uses one inclusive `due_at` date range.
Both boundaries are validated by the route and applied in the repository; the
frontend does not hide tasks locally. The existing single calendar control opens
one shared date-range popover with `from` and `to` fields.


The Tasks view sends search, task type, status/bucket, due date, and responsible
employee filters to `GET /api/tasks`. The browser no longer re-filters the
returned task array, so one repository query is the source of truth for both
filter membership and ordering.

`tasks.due_at` is the canonical task date entered during creation or editing.
Task list queries and tasks embedded in a chat use the same ordering rule:
descending `due_at`, with undated tasks last and descending `id` as the
deterministic tie breaker. Workflow status does not create a second sort group. The explicit
`assigned_user_id` query parameter filters by responsible employee; `mine=true`
intentionally takes precedence and resolves to the authenticated user's id.
