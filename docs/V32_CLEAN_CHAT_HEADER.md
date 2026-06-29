# V32 clean chat header

Problem:
The open dialog header showed too much technical customer/chat data: order id, public customer id, external chat id, last message time.

Fix:
- `renderChatSubtitle()` no longer renders `chatSubtitleParts(chat)`.
- Header title still renders `customerLabel(chat)`.
- Marketplace link `открыть чат` is retained when available.
- If no marketplace link is available, subtitle row is hidden.

Version: v32-clean-chat-header-20260629
