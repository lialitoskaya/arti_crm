v103 patch 30 — prevent chat scroll jumps while images load

Updated in the same v103 iteration:
- Added guarded auto-scroll: if the operator scrolls up, delayed image load callbacks no longer throw the dialog back to the bottom.
- Added a short sticky-bottom window only while a chat initially opens.
- Added stable aspect-ratio image cards to reserve space before images finish loading.
- Added image width/height and async decoding hints for browser layout stability.
- Build version remains v103_analytics_ui_polish_2026-06-18.
