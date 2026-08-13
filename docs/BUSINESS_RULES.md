# Business rules

## Personal chat read state

The backend and SQLite database are the source of truth for chat read state. The
state is scoped by the composite identity `(user_id, chat_id)`; marketplace
unread metadata and the global CRM workflow status are not personal read state.

1. A newly inserted, recent inbound message makes the chat unread for every active
   CRM user that existed when the message arrived.
2. Replayed historical messages and reordered messages that are not the newest
   dialog message do not create unread state.
3. An outbound or internal message neither marks the chat read nor changes another
   user's read boundary.
4. Opening a chat marks it read only for the authenticated current user.
5. A user may explicitly mark a read chat unread; the manual marker is personal.
6. Marking a chat read advances `last_read_message_id` to the current local message
   boundary and clears `is_marked_unread`.
7. Repeating the same PATCH is idempotent and returns the same canonical state.
8. Users without a personal state row see pre-migration history as read. Rows are
   created lazily by new inbound activity or an explicit manual unread action.
9. Viewer, manager, and admin roles may mutate only their own read state. The
   endpoint never accepts a user ID.
10. Unsafe requests use the shared session-bound CSRF protection.
11. Chat read-state operations do not load message history, invoke marketplace
    connectors, start synchronization, or refresh the full chat list.
12. Frontend optimistic state is protected by an operation version and a request
    snapshot; a GET started before or during a PATCH cannot roll the state back.

## Personal chat pinning

1. Pin state is personal to the authenticated CRM user and keyed by
   `(user_id, chat_id)`.
2. Pinning or unpinning never changes another user's state and never invokes a
   marketplace connector or synchronization.
3. Pinned chats sort before ordinary chats; each group remains ordered by latest
   message activity.
4. Repeating the same pin-state PATCH is idempotent.
5. Pinning a legacy chat must not make its pre-existing history unread.
6. Viewer, manager, and admin roles may mutate only their own pin state; unsafe
   requests use the shared CSRF protection.
7. The pin control is placed in the top-right action group before the marketplace
   badge. It uses delegated events and does not open the chat.

## Chat-list lazy loading

1. The interactive CRM list initially loads 30 chats and requests the next 30
   only when the operator scrolls near the end of the loaded list.
2. Marketplace, workflow, owner, archive, and message-search filters are applied
   by SQLite before each bounded batch.
3. The displayed total and unread counters describe the complete filtered result,
   not only the loaded batches.
4. A batch append must be single-flight, deduplicate chat ids, preserve scroll
   position, and never load message history or call marketplace APIs.
5. Changing a filter, search, owner scope, or archive scope resets the feed to the
   first batch; stale responses from the previous query must be ignored.
6. Passive refreshes update the loaded prefix and must not discard already loaded
   older dialogs or force the operator back to the top.
7. The legacy non-paginated repository/API behavior remains available for existing
   non-UI consumers until they are migrated explicitly.

## CRM-sent message identity and employee attribution

1. Every message sent through CRM has one stable `client_operation_id` for the
   logical send, including all frontend retries.
2. A repeated request with the same `(chat_id, client_operation_id)` must not call
   the marketplace connector again after the operation has been persisted.
3. Marketplace send acknowledgement ids are not treated as canonical history ids;
   the later provider history record enriches the same local row.
4. Employee attribution is displayed only when `is_crm_sent=1`. Generic
   marketplace authors such as `seller`, `manager`, `operator`, or `customer` are
   not employee identities.
5. `crm_author_user_id` is the stable employee reference and
   `crm_author_label` is the historical display label saved with the message.
6. Marketplace synchronization may enrich raw provider data and the canonical
   external id, but it must not remove CRM provenance, change the message to
   inbound, or overwrite the employee label.
7. Exact provider ids and client operation ids are protected by database unique
   indexes. Concurrent retries and synchronization reconcile into one row.
8. Text/time matching is only a fallback when a provider does not expose a common
   send/history id. It is allowed only for one unambiguous opposite-origin
   candidate; repeated identical replies remain separate rather than being merged
   heuristically.
9. Reconciliation of an outbound marketplace echo must not create personal unread
   state or a second notification.
10. Existing duplicate rows are repaired once by an idempotent schema migration;
    the application does not run recurring legacy repair passes at startup.

## Chat-list filtering by date

1. The calendar filter is shown only in the chat list. The open chat header has
   no calendar and message history is not filtered by this control.
2. A chat matches when its latest canonical message timestamp falls inside the
   selected inclusive local calendar-date range.
3. The backend validates both boundaries, converts the browser-local range to a
   half-open UTC interval and remains the source of truth for selection.
4. Lazy loading, total/unread counters and passive list refreshes use the same
   repository predicate.
5. Applying or clearing the range resets the chat feed to the first 30 rows and
   stale requests for the previous range are ignored.
6. Chats without messages do not match an active date range.

## Task date and responsible employee

1. `due_at` is the task date shown in cards and used by the existing calendar
   filter. `created_at` and `updated_at` are audit timestamps and never substitute
   for it.
2. The task calendar opens one inclusive `from`/`to` range. Both boundaries are
   required, validated by the route and applied in the repository.
3. Tasks are ordered strictly from the newest due date to the oldest; tasks
   without a date appear after all dated tasks.
4. Status does not override date ordering. Equal dates use descending task id as
   a stable deterministic tie breaker.
5. The responsible-employee filter uses `assigned_user_id` and is evaluated in
   the repository. The frontend does not re-filter an already returned array.
6. `mine=true` always means the authenticated user and takes precedence over an
   explicit responsible-employee query parameter.
7. The same due-date ordering applies to the all-tasks view and tasks shown
   inside an individual chat.

## Ozon message product context

1. Ozon transport markers such as the leading `errorText` token are not user-visible
   message text and are removed at the connector boundary.
2. This rule applies to an Ozon `/v3/chat/history` message only when its top-level
   `context.sku` is present, non-empty, and safe for an Ozon product path.
3. A valid `context.sku` confirms product context, but it does not prove the source
   or classify the message as review-origin.
4. The UI uses the neutral label `Товар`. Without a confirmed provider-owned
   discriminator, CRM does not classify the message as review-origin.
5. The canonical product link is `https://www.ozon.ru/product/{sku}` and opens in a
   separate tab with `noopener`/`noreferrer` protections.
6. `_crm_product_context` is an internal canonical marker created only by the Ozon
   connector after SKU validation. A marker from provider payload is never accepted.
7. The UI consumes only this connector-normalized marker; it does not parse arbitrary
   provider context through a second presentation path.
8. Product title is optional. The canonical marker contains only `kind`, `sku`, `title`,
   and `url`; it never contains `image_url`. The UI renders a compact text-only link and
   does not render or request product-context images.
9. For a canonical-normalized message, provider `context` metadata and the canonical
   marker are excluded from the generic attachment image scanner, so product previews
   cannot reappear as ordinary message attachments.
10. Rendering this context must not call an additional marketplace product endpoint,
   start synchronization, or add per-message background requests.

## CRM outbound message author

1. Every outbound message sent through the CRM carries CRM-origin markers and the
   authenticated employee identity at send time.
2. Chat history exposes `crm_author_label` only for messages proven to have been
   sent through the CRM. Marketplace-origin outbound messages do not receive a
   synthetic employee name.
3. The label resolution order is the saved CRM label, the saved CRM user ID, and
   finally a non-technical stored author value.
4. Generic transport roles such as `seller`, `manager`, `operator`, `customer`,
   and `мы` are never displayed as employee names.
5. User-ID fallback is resolved in one bulk query per opened chat, not one query
   per message.
6. The frontend shows the employee label in the outbound message footer next to
   the timestamp. Inbound and internal messages are unchanged.
