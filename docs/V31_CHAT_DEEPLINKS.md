# V31 chat deeplinks

## CRM internal routing

Added per-chat hash routes:
- `#/chats`
- `#/chats/<crm_chat_id>`

Opening a chat pushes `#/chats/<id>`.
Opening that URL directly loads chats and opens the target chat.

## Marketplace deeplink

The chat subtitle now renders a marketplace link named `открыть чат`.

### Ozon

Uses:
`https://seller.ozon.ru/app/messenger/?group=customers_v2&id={external_chat_id}`

### Wildberries

Uses:
`https://seller.wildberries.ru/chat-with-clients?chatId={id_after_colon}`

WB external ids can contain a prefix separated by `:`, so the code takes the last part.

### Yandex

No exact chat deeplink is generated from API `chat_id`, because Partner UI uses another internal id. If a ready URL exists in metadata, the CRM can use it.

Version: v31-chat-deeplinks-20260629
