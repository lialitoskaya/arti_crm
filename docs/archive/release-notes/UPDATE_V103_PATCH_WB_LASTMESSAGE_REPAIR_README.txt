WB lastMessage direction repair patch

- Repairs already imported WB chat-list lastMessage rows using raw._chat_item.clientName.
- If raw._chat_item.clientName is filled, the WB lastMessage is treated as seller/outbound.
- If raw._chat_item.clientName is explicitly empty, the WB lastMessage is treated as customer/inbound.
- Runs repair at startup and after WB sync, so old rows no longer keep chats in "ждёт ответа".
- Keeps current v103 build version unchanged.
