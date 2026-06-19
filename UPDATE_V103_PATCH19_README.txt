v103 patch 19 — employee create validation fix

Updated in the same v103 iteration:
- Added frontend validation for employee creation before sending POST /api/users.
- Username must be at least 2 characters.
- Password must be at least 6 characters.
- Display name is now sent as null when empty, matching the API schema.
- Improved FastAPI 422 validation messages so they appear as readable text instead of raw JSON/array output.
- Build version remains v103_analytics_ui_polish_2026-06-18.
