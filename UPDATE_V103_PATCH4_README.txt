v103 patch 4 — waiting response elapsed marker

Updated in the same v103 iteration:
- Added elapsed waiting marker in chat preview list: "ждёт ответа N минут/часов".
- Marker appears only for unanswered chats.
- Marker is hidden for statuses:
  - closed / Закрыт
  - waiting_customer / Ждём клиента
- Backend SLA flag now respects these statuses.
- Frontend refreshes the marker text every minute while chats view is open.
