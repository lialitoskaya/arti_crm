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
