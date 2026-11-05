// Isolated production analytics markup/rendering with deferred synthetic API responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const baseline = process.env.ANALYTICS_BASELINE === '1';
const baselineRef = process.env.ANALYTICS_BASELINE_REF || '78aeaa1';
const readSource = file => baseline ? execFileSync('git', ['show', `${baselineRef}:${file}`], { cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 }) : fs.readFileSync(path.join(root, file), 'utf8');
const source = readSource('app/static/app.js');
const analytics = source.slice(source.indexOf('// v85: Analytics dashboard.'), source.indexOf('function supplyPlanningCreateRow('));
const html = readSource('app/static/index.html');
const start = html.indexOf('<main id="analyticsView"');
const markup = html.slice(start, html.indexOf('</main>', start) + 7).replace('view hidden', 'view');
const css = readSource('app/static/styles.css');
const fixture = { summary: { requests: 42 }, filters: {}, daily: [], hourly: [], marketplace_breakdown: [] };

(async () => {
  const browser = await chromium.launch({ channel: process.env.TEST_BROWSER_CHANNEL || 'msedge', headless: true });
  try {
    for (const width of [1366, 375]) for (const theme of ['light', 'dark']) {
      const page = await browser.newPage({ viewport: { width, height: 900 } });
      await page.route('**/*', route => route.abort());
      await page.setContent(`<html data-theme="${theme}"><body>${markup}</body></html>`);
      await page.addStyleTag({ content: css });
      await page.addScriptTag({ content: `
        const $ = id => document.getElementById(id);
        let analyticsLoadedOnce = false;
        const notify = () => {};
        const escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
        window.requests = [];
        const api = url => new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
        ${analytics}
        if (typeof bindAnalyticsDrilldown === 'function') bindAnalyticsDrilldown();
        window.summaryDone = loadAnalytics();
      ` });
      if (baseline) {
        assert.equal(await page.evaluate(() => requests.length), 2);
        await page.evaluate(data => requests[0].resolve(data), fixture);
        assert.equal(await page.locator('#analyticsTotalRequests').innerText(), '—', 'Baseline blocks summary on details');
        if (process.env.UI_SCREENSHOT_DIR) {
          fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, { recursive: true });
          await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, `analytics-before-${width}-${theme}.png`), fullPage: true });
        }
        console.log(`Confirmed baseline ${width}/${theme}: two requests, summary waits for details`);
        await page.close();
        continue;
      }
      assert.equal(await page.evaluate(() => requests.length), 1);
      await page.evaluate(data => requests[0].resolve(data), fixture);
      await page.evaluate(() => summaryDone);
      assert.equal(await page.locator('#analyticsTotalRequests').innerText(), '42');
      assert.equal(await page.locator('#analyticsDrilldownRefreshBtn').isVisible(), false);
      const summary = page.locator('#analyticsDrilldownDetails > summary');
      await summary.focus();
      assert.ok(await summary.evaluate(el => el.matches(':focus-visible')), 'Keyboard focus is visible');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => requests.length === 2);
      assert.equal(await page.locator('#analyticsDrilldownRefreshBtn').isDisabled(), true);
      assert.match(await page.locator('#analyticsDrilldownSummary').innerText(), /Загрузка/);
      if (process.env.UI_SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, { recursive: true });
        await page.locator('#analyticsDrilldownDetails').evaluate(el => el.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, `analytics-loading-${width}-${theme}.png`), fullPage: true });
      }
      await page.evaluate(() => requests[1].reject(new Error('Synthetic network failure')));
      await page.waitForFunction(() => !document.getElementById('analyticsDrilldownRefreshBtn').disabled);
      assert.match(await page.locator('#analyticsDrilldownSummary').innerText(), /Не удалось/);
      assert.equal(await page.locator('#analyticsTotalRequests').innerText(), '42');
      await page.locator('#analyticsDrilldownRefreshBtn').click();
      await page.waitForFunction(() => requests.length === 3);
      await page.evaluate(() => requests[2].resolve({ items: [], excluded_service_items: [], filters: {} }));
      await page.waitForFunction(() => !document.getElementById('analyticsDrilldownRefreshBtn').disabled);
      assert.match(await page.locator('#analyticsDrilldownTable').innerText(), /Нет/);
      await page.locator('#analyticsDrilldownRefreshBtn').click();
      await page.waitForFunction(() => requests.length === 4);
      await page.evaluate(() => requests[3].resolve({ filters: {}, excluded_service_items: [], items: [{
        chat_id: 1, marketplace: 'ozon', customer_label: 'Длинное имя покупателя '.repeat(8),
        text_preview: 'Текст сообщения '.repeat(40), local_day: '2026-11-05', local_hour: 12,
        local_datetime: '2026-11-05 12:00', created_at: '2026-11-05T09:00:00Z', author: 'buyer',
      }] }));
      await page.waitForFunction(() => !document.getElementById('analyticsDrilldownRefreshBtn').disabled);
      assert.equal(await page.locator('[data-open-analytics-chat]').count(), 1);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (process.env.UI_SCREENSHOT_DIR) {
        await page.locator('#analyticsDrilldownDetails').evaluate(el => el.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, `analytics-ready-${width}-${theme}.png`), fullPage: true });
      }
      await summary.click();
      await summary.click();
      await page.waitForFunction(() => document.getElementById('analyticsDrilldownDetails').open);
      assert.equal(await page.evaluate(() => requests.length), 4, 'Reopen must reuse completed response');
      console.log(`PASS ${width}/${theme}: lazy loading, keyboard, loading/error/retry/empty/long content, no overflow`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
