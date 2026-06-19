v103 patch 24 — precise Ozon system chat filter

Updated in the same v103 iteration:
- Fixed overly broad Ozon system-chat filtering from the previous patch.
- notificationuser/systemuser are detected only by exact sender/user name fields.
- chatbot is detected only when the first message sender/user name is exactly chatbot.
- Removed broad raw JSON substring checks that could classify normal customer dialogs as bots.
- Kept list-stage skip for explicit notificationuser/systemuser chats so CRM does not load their history.
- Build version remains v103_analytics_ui_polish_2026-06-18.
