Patch: Ozon processed questions no longer show need-answer badge

Changes:
- Ozon questions with status PROCESSED/ANSWERED are no longer counted as unanswered when answer_text is empty.
- The questions list no longer shows the red "нужен ответ" badge for processed questions.
- The unanswered filter excludes processed questions even if Ozon did not return answer_text.

Install:
1. Extract into the site root with replacement.
2. Keep .env, data/crm.sqlite3 and chat_attachments unchanged.
3. Restart Python and hard refresh CRM.
