# V40 chat list infinite scroll

## Problem

Mobile chat list rendered only first 90 dialogs for performance.
That fixed the old iPhone freeze, but made the rest of chat history unreachable.

## Fix

- Keep initial mobile render limit: 90.
- Add infinite scroll on `#chatList`.
- When user scrolls near bottom, increase render limit by 60.
- Preserve scroll position while appending more chats.
- Reset render limit when filters/scope/search changes.

Version: v40-chat-list-infinite-scroll-20260629
