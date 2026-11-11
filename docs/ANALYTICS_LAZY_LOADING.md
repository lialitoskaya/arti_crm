# Analytics: summary first, messages on demand

The dashboard previously requested summary and up to 1,000 drilldown rows together
and waited for both before rendering anything. It now loads the summary alone.
The existing calculation-audit section is a native, initially closed `details`
element. Opening it requests the message rows, including excluded service rows;
clicking an hourly bar opens it with the selected hour. Closing/reopening reuses
completed rows for the current filters; refresh invalidates them.

Each endpoint has one in-flight request. Filter changes queue the latest query;
superseded results are discarded. Detail errors leave the summary available and
offer a retry. Failed refreshes clear the successful-query marker. Status text,
native keyboard disclosure, focus visibility and the existing panel/table styles
are reused. The old combined `Promise.all` path was removed.

Three remaining inbound queries now use the existing coarse date bounds before
their exact timezone/date predicates: excluded-message count, regular drilldown
and excluded-message drilldown. This enables the existing direction/date index
without changing the requested period or classifications. No schema changes or
cross-request data cache are introduced.

## Validation

- `python -m unittest discover -s tests -p test_analytics_query_bounds.py -v`: five
  tests pass, including filtered results/limits/timezones and full-response
  equivalence to broad bounds. On 10,000 historical fixture messages, drilldown
  and excluded count each use less than one fifth of the original SQLite VM work.
- `node tests/test_analytics_lazy_loading.cjs`: initial request count, independent
  summary rendering, deduplication, latest-filter ordering, failed refresh/retry
  and closed-detail cases pass.
- `node tests/test_analytics_lazy_browser.cjs`: Edge at 1366px/375px in light/dark
  themes passes keyboard, loading, disabled, error, retry, empty, long-content,
  overflow and cached-reopen checks. Uses production markup/styles/renderers and
  synthetic deferred API responses with external network blocked.
- Baseline reproduction: set `ANALYTICS_BASELINE=1` (default source `78aeaa1`,
  override with `ANALYTICS_BASELINE_REF`). Confirmed two initial requests and a
  summary still waiting after its own response. `UI_SCREENSHOT_DIR` saves evidence;
  screenshots are kept outside Git. Playwright must be available via module
  resolution or `NODE_PATH`, and Edge installed (`TEST_BROWSER_CHANNEL` overrides).
- Python/JavaScript syntax and `git diff --check` pass. Full application tests and
  live authenticated timing were not run: application dependencies are absent;
  no production DB or credentials were accessed.

The summary's remaining repeated aggregates and large active-chat response
histories may still be slow. This change avoids waiting for detail rows; it does
not promise a particular hosting latency or solve all SQL performance limits.

## Deployment for the currently verified hosting layout

The user confirmed port 20006 and PM2 `vh20541.ffox.site` share the same PID and
working directory `/var/www/vh20540/data/www/vh20541.ffox.site`. Public frontend
updates became visible on `https://perfectservice-crm.ffox.site/` after replacing
files there. The similarly named `perfectservice-crm.ffox.site` directory was
not the source of those running files. Recheck this mapping if hosting changes.

Back up and replace these four files together under the active directory:

- `app/services/analytics.py`
- `app/static/app.js`
- `app/static/index.html`
- `app/static/styles.css`

Then run `pm2 restart vh20541.ffox.site` and reload the page. Updated asset query
versions in index.html request the matching JS/CSS. Keep the existing database and
private runtime settings. Rollback restores these four backups and restarts the
same process. This change has not been deployed by the development agent.
