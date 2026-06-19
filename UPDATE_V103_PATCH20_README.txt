v103 patch 20 — chat open and status update stability

Updated in the same v103 iteration:
- Chat taps now switch to the dialog immediately and show a lightweight loading state.
- Added stale request protection so delayed chat responses cannot overwrite the UI after returning to the list.
- Status/assignee changes now update the chat list optimistically without a full dialog reload.
- Returning to the chat list no longer lets a pending open request reopen/lock the chat view.
- PATCH /api/chats/{id} no longer loads the full message history twice; it uses lightweight chat summary data.
- Build version remains v103_analytics_ui_polish_2026-06-18.
