v103 patch 32 — route persistence by section

Updated in the same v103 iteration:
- Added hash routes for CRM sections: #/chats, #/analytics, #/tasks, #/reviews, #/questions, #/knowledge, #/users, #/settings, #/profile.
- The current section is saved in localStorage and restored after browser reload.
- Refreshing the browser no longer drops the operator back to the initial Chats page.
- Browser back/forward now switches CRM sections through hashchange.
- Existing section buttons and mobile More navigation now update the URL route automatically.
- Build version remains v103_analytics_ui_polish_2026-06-18.
