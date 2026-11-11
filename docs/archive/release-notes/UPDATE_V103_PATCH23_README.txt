v103 patch 23 — filter Ozon system chats

Updated in the same v103 iteration:
- Ozon notificationuser/systemuser chats are skipped at chat-list stage and no history request is made.
- Ozon history system detection is now enabled by default.
- If the first message sender/user designation is chatbot, the dialog is treated as support/system and removed from CRM.
- Existing local Ozon system chats are cleaned on startup using exact notificationuser/systemuser markers and first-message chatbot rule.
- Build version remains v103_analytics_ui_polish_2026-06-18.
