# V25 root cause from debug log

The v24 log showed the real blocker:

- openChat finished around 2059ms
- renderChatList started around 2181ms
- renderChatList finished around 6327ms
- duration: 4147ms

The first tap on the status select happened immediately after this freeze ended.
That means the UI was blocked by hidden chat-list rendering, not by native selects.

Another sync cycle later:
- /api/sync/operator took 11560ms
- then loadChats triggered renderChatList
- renderChatList took 4270ms

Fix:
Do not render the hidden chat list while the mobile dialog is open. Keep the
chat data in memory and render the list only when the operator returns to it.
