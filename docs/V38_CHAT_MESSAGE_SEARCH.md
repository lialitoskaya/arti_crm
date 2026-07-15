# V38 chat message search

## Backend

`GET /api/chats` now accepts:

- `q`: search phrase for message text.

`repo.list_chats()` filters chats through an `EXISTS` query over `messages`.
If a matching message is found, the response includes:

- `search_match_text`
- `search_match_at`
- `search_query`

## Frontend

The chat filters row now has a last search icon.

Behavior:

1. Click search icon.
2. Search field opens.
3. User types at least 2 characters.
4. CRM reloads chat list with `/api/chats?q=...`.
5. Matching chats show the matched message text in the preview.

Version: v38-chat-message-search-20260629
