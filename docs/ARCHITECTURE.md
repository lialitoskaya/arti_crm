# Архитектура проекта

## Текущая структура

```text
app/
  main.py                 FastAPI app, API routes, sync orchestration
  repository.py           SQLite repository layer
  task_chat_status_automation.py  Transaction-local chat-status arbiter
  db.py                   schema/migrations/init
  schemas.py              Pydantic DTO
  services/
    analytics.py          SQL analytics/dashboard calculations
    ozon_fbo/             FBO supply pipeline: rules, drafts, dedupe, orders, cargo, runner
  integrations/
    ozon_fbo/client.py    Ozon Seller API client for FBO supplies (httpx, rate limits, dumps)
  ozon_fbo_router.py      admin-only /api/ozon-fbo/* routes
  ozon_fbo_repository.py  ozon_fbo_* tables (settings, runs, state, warehouse map)
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

### Startup configuration boundary

`app.runtime_config` owns dotenv priority and absolute SQLite path selection for
ASGI, `server.py`, `passenger_wsgi.py` and direct DB imports. Hosting environment
wins; relative paths resolve from the installation root. Configuration performs no
DB I/O, migration or automatic relocation. Ambiguous old defaults fail closed.
Each serving entry point initializes schema before invoking the single
`app.main.ensure_initial_admin()` operation; its repository transaction serializes
the first-user check/insert. WSGI does not invoke the full ASGI lifecycle.

`app.startup_diagnostics` owns the shared safe WSGI failure response and private,
value-free frame diagnostics. It neither writes public site-root logs nor returns
exception details to clients. Deployment/configuration and rollback requirements
are documented in `docs/HOSTING_UPGRADE.md`.

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

### Code-verified historical hosting upgrade

`legacy_hosting_upgrade.py` recognizes only the supplied historical hash-only outbox
and inline task-effect schema. It is not a general partial-schema repair or a legacy
sender. Before the first normal `init_db()` schema write, the versioned
`20261019_legacy_hosting_review_upgrade` bridge takes `BEGIN IMMEDIATE`, validates
the known tables/indexes/markers and consumed parent references, archives old rows,
runs the preserved identity migration, and creates the modern outbox/automation
schema. Conversion and its marker commit together; errors roll the conversion back.
The rest of existing `init_db()` still has `executescript()` commit boundaries. A
failure later in initialization needs forward recovery, not a claim that the whole
startup was atomic.

The four `legacy_*_audit` tables have no cascading foreign keys. Their exact rows,
including historical diagnostic fields, are frozen by insert/update/delete guards
and checked against an immutable count/hash manifest on subsequent initialization.
This private audit is never returned by an API or logged. A separate
`legacy_message_review_links` table preserves an effective canonical-message link;
identity deduplication remaps it **before** deleting a duplicate, without changing
the archived original ID. Modern AUTOINCREMENT is reserved above both the old
maximum ID and the historical SQLite sequence, preventing numeric polling conflicts.

Old operations do not enter the modern command table: they have no complete text
payload and cannot be claimed, retried, drained, or dispatched. The existing
operation service exposes a whitelisted review representation (`uncertain`, empty
internal payload and a fixed display-only explanation), never fabricated sendable
text. Only an archived `confirmed` state with an exact surviving same-chat outbound
message, matching provider ID and original legacy text hash is represented as
confirmed and omitted from unresolved rows. An accepted ACK alone is insufficient.
No new history matcher, renderer, worker or manual-resolution endpoint is added.

Under the existing registration write lock, a repeated legacy client ID returns
the same review operation, a different hash conflicts, and a new ID with an
unresolved legacy text hash also conflicts. Hashing follows the original hosted
normalization at this one boundary before connector-specific normalization; modern
payload hashing is unchanged. Public author labels are resolved from the old
server-stored user ID, never from the old client-influenced display label.

Old task state, mapping and inline effect fields are archived. Chat workflow status
and its manual metadata are not rewritten; no old ownership/snapshot is synthesized.
The preserved message-identity migration may still repair preview/activity cache and
its timestamp when genuine duplicate messages are merged. Valid active-status
mappings survive, including dormant inactive task types; invalid/orphaned mappings
remain audit-only. Explicit future task create/rebind/reactivation uses the existing
arbiter. See `docs/HOSTING_UPGRADE.md` for rollout and rollback boundaries.

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

## Task-type chat-status automation

`task_chat_status_automation.py` is the single transaction-local arbiter for
changes to an existing `chats.status` caused by manual CRM edits, task lifecycle,
or confirmed provider activity. Repository entry points acquire `BEGIN IMMEDIATE`
before reading the chat, task, task type, mapping, effect, or arbitration state;
all writes and winner calculation then use the same SQLite connection. No network
call or nested transaction occurs inside this boundary.

The additive `20260824_task_type_chat_status_automation` migration runs after the
preserved message-identity and durable-outbox migrations and after active chat
statuses are seeded. It creates:

- `task_type_chat_status_links`, one optional active-status mapping per task type;
- `chat_task_status_state`, the chat baseline, current cycle, override source, and
  current winning task/status;
- `task_chat_status_effects`, immutable acquisition snapshots with task, mapped
  status, cycle and application time plus explicit release metadata.

The winner query is indexed by chat and cycle and orders active snapshots by the
latest application time, then task id and effect id. Existing tasks are not
backfilled: a snapshot is created only by task creation, type change, or terminal
task reactivation. Terminal transition and deletion release their snapshot in the
same transaction and recalculate the remaining winner or a still-active baseline.

Deactivating a task type keeps its mapping as dormant configuration while the
arbiter releases every active effect owned by tasks of that type and recalculates
the affected chats under the same write lock. Reactivating the type never backfills
effects; a later task creation, type change, or terminal-task reactivation is still
required. Deactivating or deleting a chat status uses the same arbiter: active
effects are released, affected chats move to an active baseline/fallback, and all
task-type mappings to that status are cleared before the status becomes inactive
or is deleted. Reactivating the status does not recreate those mappings. If no
active fallback exists, the complete configuration mutation rolls back.

Manual status changes and a provider reopen invalidate every task-owned effect in
the current cycle. Provider reopen is fail-closed: it requires the stored canonical
status key to equal `closed` and the latest direction to equal `inbound`. A later
explicit task lifecycle event starts a new cycle; old snapshots can never restore a
pre-override status. Repeated provider activity for an already-open chat is a no-op.
Marketplace upsert preserves the current workflow status and never performs a
competing status write.

Migration validation runs on every `init_db()`: compatible empty partial tables
may be rebuilt with the canonical audit columns and indexes, while a nonempty
partial table without trustworthy acquisition timestamps fails closed. Column
types, nullability, defaults, primary/unique keys, foreign-key actions and ordered
cycle indexes are revalidated before the migration marker is written. Forward
reconciliation is available as `repository.reconcile_task_chat_status_automation()`.
Foreign-key data validation is scoped to the migration's three child tables:
`task_type_chat_status_links`, `chat_task_status_state`, and
`task_chat_status_effects`. Every owned relationship is checked on each startup,
including when the marker already exists; an owned orphan remains fatal before
the marker is written or forward reconciliation starts. Errors identify only the
static table name, never row IDs or payloads. Unrelated legacy FK violations or
malformed FK definitions are not diagnosed or repaired by this migration and must
be investigated separately; successful startup is not a whole-database integrity
certificate. No records are deleted, nulled, or synthesized, and runtime FK
enforcement remains enabled.

The same validation runs through the shared `init_db()` used by ASGI and hosted
WSGI startup. The isolated FK-validation change is confined to
`app/task_chat_status_automation.py`; this describes code scope, not permission to
replace that module alone on an older hosting release. Deploy the coherent release
using `docs/HOSTING_UPGRADE.md`, with a verified database/code backup. The FK scan
itself needs no server-specific paths, configuration, or schema changes.
The migration participates in its caller's transaction, so a failure rolls back
that transaction; `init_db()` as a whole is not globally atomic because existing
`executescript()` calls establish earlier commit boundaries. Rolling this fix back
restores the old global FK scan and can reproduce the startup failure when
unrelated legacy integrity debt remains; there is no data conversion to undo.

Before rolling application code back, operators must run the idempotent
`repository.release_task_chat_status_automation_for_rollback()` routine; it
releases task ownership and restores only an active baseline (or the active default)
without undoing manual or provider overrides. A plain code rollback while effects
remain active is unsupported.

## Ozon FBO supplies

The «Поставки FBO» section ports the desktop `ozon_fbo` application into the CRM
without changing its business rules. The boundaries follow `AGENTS.md`:

```text
POST /api/ozon-fbo/runs (admin, multipart plan)
  -> app/ozon_fbo_router.py validates input and registers the run
    -> app/ozon_fbo_repository.create_run(): BEGIN IMMEDIATE + partial unique index
       idx_ozon_fbo_runs_single_active (at most one queued/running/awaiting run)
      -> app/services/ozon_fbo/runner.start_run(): worker thread in the web process
        -> services/ozon_fbo/pipeline.run_pipeline / run_live_batch / cargo.run_cargo_orders_pipeline
          -> integrations/ozon_fbo/client.OzonFboApi (httpx, trust_env=False, 1 req/s,
             2 drafts/min, header-free JSON dumps under OZON_FBO_LOG_DIR/run_<id>/)
          -> ozon_fbo_repository (RepositoryStateStore): article memory, order cargo state,
             learned warehouse→cluster map
```

Routes contain no SQL, retry or marketplace mapping. Business rules
(`rules.py`, `drafts.py`, `dedupe.py`, `cargo.py`) are deterministic and take an
API object and a state store; tests exercise them with fakes and an in-memory
store. Mutable settings are a frozen `FboSettings` dataclass held in a
`ContextVar` for the duration of a run, not module-global state.

Run lifecycle is persisted in `ozon_fbo_runs`: `queued -> running -> awaiting_answer
-> running -> finished | failed | stopped | interrupted`. The worker appends journal
lines to `ozon_fbo_run_log` (monotonic `seq` under `BEGIN IMMEDIATE`), rewrites
`ozon_fbo_run_rows` after each article, stores a pending question
(`question_kind`, `question_text`) and polls the same row for `answer` and
`stop_requested`; a heartbeat thread refreshes `heartbeat_at` every 10 s. Because
every interaction goes through SQLite, the browser can poll, answer and stop from any
hosting process. A run whose heartbeat is older than 180 s is marked `interrupted` by
`GET /api/ozon-fbo/status` or the explicit `release` route (forward recovery after a
process restart); orders already created were saved per article, so nothing is lost
or replayed. Draft TTL (30 min) is handled by the pipeline itself through
`refresh_draft`; an unanswered question times out as «no» after two hours.

Schema: `ozon_fbo_settings`, `ozon_fbo_runs`, `ozon_fbo_run_log`, `ozon_fbo_run_rows`,
`ozon_fbo_article_state`, `ozon_fbo_order_state`, `ozon_fbo_warehouse_map`,
`ozon_fbo_learned_clusters`, all created with `CREATE TABLE IF NOT EXISTS` in
`init_db()`. Uploaded plans and result workbooks are stored as BLOBs on the run row.
Rolling back to older application code leaves the tables unused; no destructive
migration exists. The frontend (`#ozonFboView`, route `#fbo`) keeps only transport and
view state: the server decides modes, safeguards, duplicates and results.
