Patch: robust Ozon processed-question badge handling

- Treats Russian status values like "обработан" as processed, not as needing an answer.
- Adds backend fields is_processed and needs_answer to Ozon question API responses.
- Uses the backend needs_answer flag in the frontend badge logic.
- Applies the same robust processed-status check to dashboard counters and the unanswered filter.

Install into the project root with file replacement, keep .env/data/chat_attachments, restart Python, then Ctrl+F5.
