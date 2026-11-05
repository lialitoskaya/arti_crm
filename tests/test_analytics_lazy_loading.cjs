const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const loader = source.slice(source.indexOf('const analyticsRequests ='), source.indexOf('const ANALYTICS_WEEKDAY_NAMES'));

function fixture() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { open: false, disabled: false, textContent: '', dataset: {},
      replaceChildren() { this.textContent = ''; }, setAttribute() {}, addEventListener() {} });
    return elements.get(id);
  };
  const requests = [];
  const rendered = [];
  const context = vm.createContext({ $, Promise, analyticsLoadedOnce: false, query: 'date_from=2026-11-01',
    setupAnalyticsDefaults() {}, analyticsQueryString: () => context.query,
    setAnalyticsText: (id, text) => { element(id).textContent = text; },
    api: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })),
    renderAnalytics: data => rendered.push(['summary', data]),
    renderAnalyticsDrilldown: data => rendered.push(['details', data]), notify() {},
  });
  function $(id) { return element(id); }
  vm.runInContext(loader, context);
  return { context, requests, rendered, element };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const f = fixture();
  const summary = f.context.loadAnalytics();
  assert.equal(f.requests.length, 1, 'Closed details must not send a request');
  assert.ok(!f.requests[0].url.includes('/drilldown'));
  f.element('analyticsDrilldownDetails').open = true;
  const details = f.context.loadAnalyticsDrilldown();
  const duplicate = f.context.loadAnalyticsDrilldown();
  assert.equal(f.requests.length, 2, 'Repeated open must deduplicate');
  f.requests[0].resolve({ value: 'summary-first' });
  await summary;
  assert.deepEqual(f.rendered, [['summary', { value: 'summary-first' }]], 'Summary must render while details remain pending');
  f.requests[1].resolve({ value: 'details-later' });
  await Promise.all([details, duplicate]);
  await f.context.loadAnalyticsDrilldown();
  assert.equal(f.requests.length, 2, 'Reopening same filters reuses loaded rows');
  const refresh = f.context.loadAnalyticsDrilldown(true);
  const failedRefresh = assert.rejects(refresh, /Refresh failed/);
  f.requests[2].reject(new Error('Refresh failed'));
  await failedRefresh;
  const reopened = f.context.loadAnalyticsDrilldown();
  assert.equal(f.requests.length, 4, 'Failed forced refresh must not leave a false success cache');
  f.requests[3].resolve({ value: 'reopened-after-failure' });
  await reopened;

  const latest = fixture();
  latest.element('analyticsDrilldownDetails').open = true;
  const old = latest.context.loadAnalytics();
  latest.context.query = 'date_from=2026-11-04';
  const next = latest.context.loadAnalytics();
  assert.equal(latest.requests.length, 2, 'One request per endpoint stays in flight');
  latest.requests[0].resolve({ value: 'stale-summary' });
  latest.requests[1].resolve({ value: 'stale-details' });
  await flush();
  assert.equal(latest.rendered.length, 0, 'Old filter responses must never render');
  assert.equal(latest.requests.length, 4);
  assert.ok(latest.requests[2].url.includes('2026-11-04'));
  assert.ok(latest.requests[3].url.includes('2026-11-04'));
  latest.requests[2].resolve({ value: 'current-summary' });
  latest.requests[3].resolve({ value: 'current-details' });
  await Promise.all([old, next]);
  await flush();
  assert.deepEqual(latest.rendered.map(row => row[1].value), ['current-summary', 'current-details']);

  const failed = fixture();
  failed.element('analyticsDrilldownDetails').open = true;
  const done = failed.context.loadAnalytics();
  failed.requests[1].reject(new Error('Temporary failure'));
  failed.requests[0].resolve({ value: 'still-available' });
  await done;
  await flush();
  assert.equal(failed.rendered[0][0], 'summary');
  assert.match(failed.element('analyticsDrilldownSummary').textContent, /Не удалось/);
  assert.equal(failed.element('analyticsDrilldownRefreshBtn').disabled, false);
  const retry = failed.context.loadAnalyticsDrilldown(true);
  failed.requests[2].resolve({ value: 'retried' });
  await retry;
  assert.equal(failed.rendered[1][1].value, 'retried');

  const closed = fixture();
  closed.element('analyticsDrilldownDetails').open = true;
  const pending = closed.context.loadAnalyticsDrilldown();
  closed.element('analyticsDrilldownDetails').open = false;
  closed.context.query = 'date_from=2026-11-05';
  const newSummary = closed.context.loadAnalytics();
  closed.requests[0].resolve({ value: 'old' });
  closed.requests[1].resolve({ value: 'new' });
  await Promise.all([pending, newSummary]);
  assert.equal(closed.requests.length, 2, 'Closed details must not queue another request');
  assert.deepEqual(closed.rendered, [['summary', { value: 'new' }]]);
  console.log('PASS lazy requests, independent summary, deduplication, latest filters, retry, closed details');
})().catch(error => { console.error(error); process.exitCode = 1; });
