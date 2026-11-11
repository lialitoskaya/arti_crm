v103 patch 31 — marketplace image sending

Updated in the same v103 iteration:
- Added connector-level send_file support.
- Ozon images are sent through /v1/chat/send/file using base64_content, chat_id and name.
- Yandex Market images are sent through /v2/businesses/{businessId}/chats/file/send using multipart/form-data.
- The chat attachments endpoint now sends images to the marketplace before saving the outgoing CRM message.
- If the marketplace rejects an image, CRM returns an error instead of marking it as delivered locally.
- Local-only image attachment status remains only for mock/demo chats.
- WB image sending is blocked with an explicit error until a confirmed WB file-send method is added.
- Build version remains v103_analytics_ui_polish_2026-06-18.
