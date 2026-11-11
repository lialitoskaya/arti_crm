v103 patch 22 — canonical closed status persistence

Updated in the same v103 iteration:
- Any selected status whose key/title means "closed" is saved as the canonical built-in status "closed".
- Active chat list excludes closed-like statuses by direct status value and by chat_statuses title/key.
- Background marketplace sync preserves closed-like existing statuses even when CRM manual metadata was missing before.
- Frontend uses the returned canonical status to keep the chat hidden from active list.
- Build version remains v103_analytics_ui_polish_2026-06-18.
