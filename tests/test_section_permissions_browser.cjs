const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const {setup, baseline, employee, none} = require('./crm_browser_fixture.cjs');

async function screenshot(page, name) {
  if (!process.env.UI_SCREENSHOT_DIR) return;
  fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, {recursive: true});
  await page.screenshot({path: path.join(process.env.UI_SCREENSHOT_DIR, name), fullPage: true});
}

async function exerciseYandexLinks(page, width, theme) {
  const endpoint = '/api/admin/yandex-oauth-links';
  const writes = [];
  const links = [];
  await page.route('**' + endpoint + '**', async route => {
    const request = route.request();
    const method = request.method();
    const payload = request.postDataJSON();
    if (method !== 'GET') writes.push([method, new URL(request.url()).pathname, payload]);
    if (method === 'POST') links.push({...payload, id: 7, is_active: true, confirmed: false});
    if (method === 'PATCH') links[0].is_active = payload.is_active;
    if (method === 'DELETE') links[0].is_active = false;
    await route.fulfill({json: method === 'GET' ? {links, users: [employee]} : {ok: true}});
  });
  await page.evaluate(() => showView('techSettings'));
  const list = page.locator('#yandexOAuthLinksList');
  await list.getByText('Связей пока нет.').waitFor();
  const email = 'synthetic-user@example.test';
  await page.locator('#yandexOAuthIdentifierType').selectOption('email');
  await page.locator('#yandexOAuthIdentifier').fill(email);
  await page.locator('#yandexOAuthLinkForm button[type=submit]').press('Enter');
  const row = list.locator('[data-yandex-oauth-link-id="7"]');
  await row.getByText('Email: ' + email).waitFor();
  await row.getByRole('button', {name: 'Отключить'}).click();
  await row.getByRole('button', {name: 'Включить'}).click();
  await row.getByRole('button', {name: 'Отключить'}).waitFor();
  page.once('dialog', dialog => dialog.accept());
  await row.getByRole('button', {name: 'Запретить вход'}).click();
  await row.getByText('Отключена', {exact: true}).waitFor();
  assert.deepEqual(writes, [
    ['POST', endpoint, {identifier_type: 'email', identifier: email, crm_user_id: employee.id}],
    ['PATCH', endpoint + '/7', {is_active: false}],
    ['PATCH', endpoint + '/7', {is_active: true}],
    ['DELETE', endpoint + '/7', null],
  ]);
  if (process.env.UI_SCREENSHOT_DIR) {
    await page.locator('#yandexOAuthLinksCard').screenshot({
      path: path.join(process.env.UI_SCREENSHOT_DIR, `yandex-links-${width}-${theme}.png`),
      animations: 'disabled',
    });
  }
}

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    for (const width of [1366, 375]) for (const theme of ['light', 'dark']) {
      const {page, requests} = await setup(browser, width, theme);
      await page.evaluate(() => showView('users'));
      await page.locator('[data-user-edit="2"]').click();
      if (!baseline) {
        assert.equal(await page.locator('#editUserPermissions select').count(), 9);
        await page.locator('#editUserPermissions [data-section="chats"]').selectOption('read');
        await page.locator('#editUserPermissions [data-section="questions"]').selectOption('write');
        await page.locator('#editUserRole').selectOption('admin');
        assert.equal(await page.locator('#editUserPermissions select:disabled').count(), 9);
        await page.locator('#editUserRole').selectOption('manager');
        await page.locator('#editUserActive').focus();
        await page.keyboard.press('Tab');
        assert.ok(await page.locator('#editUserPermissions [data-section="chats"]').evaluate(el => el.matches(':focus-visible')));
      }
      await screenshot(page, `permissions-${baseline ? 'before' : 'after'}-${width}-${theme}.png`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
      if (!baseline) {
        await page.locator('#userEditForm button[type=submit]').click();
        await page.waitForFunction(() => document.getElementById('userEditModal').classList.contains('hidden'));
        const saved = requests.find(r => r.path === '/api/users/2' && r.method === 'PATCH');
        assert.equal(saved.body.permissions.chats, 'read');
        assert.equal(saved.body.permissions.questions, 'write');
        assert.equal(saved.body.permissions.knowledge, 'none');
        await page.locator('#openUserCreateModalBtn').click();
        assert.equal(await page.locator('#newUserPermissions select').count(), 9);
        assert.equal(await page.locator('#newUserPermissions [data-section="chats"]').inputValue(), 'none');
        await page.locator('#cancelUserCreateBtn').click();
        await exerciseYandexLinks(page, width, theme);
      }
      await page.close();
    }
    if (!baseline) {
      const {page, requests} = await setup(browser, 375, 'light', {...employee, permissions: {...none, tasks: 'read'}});
      await page.waitForTimeout(100);
      assert.equal(await page.evaluate(() => activeView), 'tasks');
      assert.equal(await page.locator('#navChats').isVisible(), false);
      assert.equal(await page.locator('[data-mobile-view="knowledge"]').getAttribute('class').then(c => c.includes('hidden')), true);
      assert.equal(await page.locator('#taskStandaloneNewBtn').evaluate(el => el.inert), true);
      assert.equal(requests.filter(r => r.path === '/api/chats' || r.path === '/api/sync/status' || r.path === '/api/sync/operator').length, 0);
      await page.evaluate(() => showView('chats'));
      assert.equal(await page.evaluate(() => activeView), 'tasks');
      assert.equal(await page.locator('#navOzonFbo').isVisible(), false);
      await page.evaluate(() => showView('ozonFbo'));
      assert.equal(await page.evaluate(() => activeView), 'tasks');
      assert.ok(!requests.some(r => r.path.startsWith('/api/ozon-fbo/')));
      await screenshot(page, 'permissions-readonly-mobile.png');
      await page.evaluate(() => { currentUser.permissions = {}; updateAuthUiForUser(); showView('tasks'); });
      assert.equal(await page.evaluate(() => activeView), 'profile');
      await page.setViewportSize({width: 1366, height: 900});
      const cached = JSON.stringify({defaultTargetDays: 45, rows: [{id: '1', marketplace: 'ozon', product: 'Test', deliveryDays: '5', inTransit: '0'}]});
      await page.evaluate(cached => {
        currentUser.permissions = {supplyPlanning: 'read'};
        localStorage.setItem(SUPPLY_PLANNING_STORAGE_KEY, cached);
        updateAuthUiForUser();
        showView('supplyPlanning');
      }, cached);
      assert.ok(await page.locator('#navSupplyPlanning').isVisible());
      for (const selector of ['#supplyPlanningDefaultTargetDays', '[data-supply-field="deliveryDays"]', '[data-supply-field="inTransit"]']) {
        const input = page.locator(selector);
        await page.waitForFunction(selector => document.querySelector(selector)?.inert, selector);
        await input.dispatchEvent('input');
      }
      assert.equal(await page.evaluate(() => localStorage.getItem(SUPPLY_PLANNING_STORAGE_KEY)), cached);
      assert.ok(!requests.some(r => r.path === '/api/supply-planning/sync'));
      await page.close();
    }
    console.log('Employee permission forms, save payload, admin role, keyboard, mobile navigation and read-only controls: PASS');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
