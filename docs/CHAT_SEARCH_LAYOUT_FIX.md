# Search result height — 2026-11-05

When message search left one or two chats, the grid's default content alignment
stretched its auto-sized rows to fill the sidebar. A single result measured 683px
in the desktop fixture, compared with the ordinary 125px row.

Set `align-content: start` on the existing `#chatsView .chat-list` rule. This keeps
the existing row minimum, content-driven growth, preview truncation, themes and
scrolling. It also keeps short unfiltered lists compact. No extra selectors,
components, handlers or fixed-height overrides are introduced.

Validation: `node tests/test_chat_search_layout.cjs` with Playwright available
(via Node module resolution or `NODE_PATH`) and Microsoft Edge installed.
`TEST_BROWSER_CHANNEL` can select another installed Playwright browser channel.
`UI_SCREENSHOT_DIR` optionally saves screenshots outside the repository.

The browser regression failed before the fix (683px versus 125px) and passed after
it at 1366px, 768px and 375px in light/dark themes. It exercises the production
sidebar markup, CSS and list renderer with synthetic data and blocked networking:
one/two/many results, long names and unbroken text, clearing search, empty results
and scrolling. Desktop before/after and mobile after screenshots were inspected.
The renderer's data dependencies are stubbed; this is not end-to-end coverage of
search requests, keyboard actions, loading/error states or permissions.

JavaScript syntax and Git whitespace checks pass. Independent review found no
blockers. No hosting deployment or database change is included. Reverting the
single CSS declaration restores the previous layout.
