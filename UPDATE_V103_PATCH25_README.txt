v103 patch 25 — persistently hide Ozon system chats

Updated in the same v103 iteration:
- Fixed system/chatbot dialogs reappearing after some time.
- System dialogs are now hidden with _crm_excluded_as_system instead of being deleted by default.
- Active/archive chat lists exclude hidden system dialogs.
- Sync skips already hidden system dialogs and does not request their message history again.
- New chatbot-first-message dialogs are hidden and remembered after the first history detection.
- notificationuser/systemuser list-stage skip is kept.
- Build version remains v103_analytics_ui_polish_2026-06-18.
