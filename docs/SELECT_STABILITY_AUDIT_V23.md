# Arti CRM v23 — status/assignee select stability

## Root cause

The mobile delay for chat status and assignee was caused by repeated rebuilding of native select controls in the chat header.

The previous code path did this:

1. active chat live-refresh runs;
2. `refreshCurrentChatMessagesOnly()` fetches the current chat;
3. it calls `paintChatHeader(currentChat)`;
4. `paintChatHeader()` calls `renderChatSettingsControls()`;
5. `renderChatSettingsControls()` rebuilds `#chatStatus.innerHTML`;
6. `paintChatHeader()` also rebuilds `#assignedUserSelect.innerHTML`.

This means that while the operator is tapping the native iOS select, the DOM node and its options can be rehydrated. On iOS Safari this often feels like a long loading delay.

## Fix

v23 separates header text refresh from select hydration.

### New behavior

- Chat title/avatar/subtitle can refresh normally.
- `#chatStatus` and `#assignedUserSelect` are hydrated only:
  - on initial chat open;
  - when chat id changes;
  - when status/assignee options actually change;
  - when explicitly forced by settings reload.

- Live message refresh does not rebuild these select controls.
- While the operator is touching/focusing/changing them, background refresh is paused.
- Mobile status/assignee changes are saved optimistically without rendering the hidden chat list during the interaction.

## What this does not change

- No header grid/layout rewrite.
- No `contain` on chat header.
- No risky menu positioning changes.
