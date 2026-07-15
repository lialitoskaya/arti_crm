# Arti CRM refactor v16

## Scope

This package refactors the frontend files that were provided:

- `app/static/app.js`
- `app/static/styles.css`

It keeps the existing server API contract and does not require an additional JavaScript file.

## What changed

### 1. API error handling

The old `api()` function threw plain `Error` objects with raw backend text.  
The refactored version keeps the same public function name, but now creates structured API errors:

- `error.status`
- `error.detail`
- `error.body`
- `error.parsed`
- `error.path`

This makes it possible to distinguish a normal validation error from a marketplace rate-limit error.

### 2. Marketplace send retry

The message send flow now uses:

- a serial outbound queue;
- protection from repeated clicks;
- retry with backoff for Yandex Market `420`, `429`, `METHOD_FAILURE`, and rate-limit messages;
- friendly user-facing error text.

This does not fully replace backend rate limiting. The correct backend fix is still a semaphore/queue per marketplace and `businessId`.

### 3. Mobile chat state

Mobile chat opening is now controlled through one function:

- `setMobileChatOpen(isOpen)`

It applies classes to:

- `body`
- `html`
- `#chatsView`
- `#conversation`
- `#chatPanel`

This removes the need for a separate mobile JS patch and makes the state more predictable.

### 4. Faster mobile open

On mobile:

- the chat screen is shown before the API response;
- only 60 latest messages are requested instead of 120;
- the chat list is not re-rendered before the first mobile frame;
- tasks and chat-list refresh are deferred until after the messages render;
- background sync is temporarily suppressed while opening/sending.

### 5. CSS preservation

The CSS is based on the uploaded `styles(30).css`.  
The header layout is not converted to grid. The user's flex-based `.chat-heading-row` styling is preserved.

The only CSS runtime changes are:

- replacing heavy `body:has(#chatsView.mobile-chat-mode)` selectors with `body.mobile-chat-open`;
- adding a lock state for mobile chat;
- adding a disabled state for the send button.

## What still needs backend refactor

To remove Yandex rate-limit errors fully, backend code must implement request queuing:

- key by marketplace + `businessId`;
- max 4 parallel requests for Yandex;
- retry/backoff on `420/429`;
- deduplicate background sync jobs;
- avoid sending chat refresh requests while an outbound message is being sent.

