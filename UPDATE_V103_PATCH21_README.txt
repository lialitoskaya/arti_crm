v103 patch 21 — keep closed chats out of active inbox

Updated in the same v103 iteration:
- Fixed closed dialogs returning to the active chat list after background sync.
- Preserved CRM manual workflow metadata during marketplace upsert.
- Background sync now cannot reopen built-in or custom closed-like statuses.
- Archive filtering now includes custom statuses named "Закрыт", not only key "closed".
- Frontend now treats any status labeled "Закрыт" as closed-like and removes it from active list immediately after saving.
- Build version remains v103_analytics_ui_polish_2026-06-18.
