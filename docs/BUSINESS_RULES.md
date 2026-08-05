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
