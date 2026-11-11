const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const {setup, employee, none} = require('./crm_browser_fixture.cjs');

async function checkFboNavigation(browser, width) {
  const {page, requests} = await setup(browser, width, 'light', {
    ...employee, permissions: {...none, tasks: 'read', ozonFbo: 'read'},
  });
  try {
    if (!(await page.locator('#navOzonFbo').isVisible())) {
      await page.locator('#mobileMoreBtn').click();
      await page.locator('[data-mobile-view="ozonFbo"]').click();
    } else {
      await page.locator('#navOzonFbo').click();
    }
    await page.locator('#ozonFboView').waitFor();
    await page.waitForFunction(() => document.querySelector('#ozonFboSettingsForm').inert);
    assert.match(new URL(page.url()).hash, /fbo/);
    assert.ok(requests.some(r => r.path === '/api/ozon-fbo/status'));
    await page.reload();
    await page.locator('#ozonFboView').waitFor();
  } finally { await page.close(); }
}

async function checkReviewTitles(page) {
  const reviews = [
    {id: 1, product_name: 'Модель X', sku: '123'},
    {id: 2, sku: '456'},
    {id: 3},
  ];
  await page.route('**/api/reviews**', route => {
    const id = new URL(route.request().url()).pathname.split('/')[3];
    return route.fulfill({json: id ? reviews.find(row => row.id === Number(id)) : reviews});
  });
  await page.evaluate(() => showView('reviews'));
  for (const name of ['Модель X', 'SKU 456', 'Товар не указан']) {
    await page.locator('#reviewsList').getByText(name, {exact: true}).click();
    await page.waitForFunction(name => document.querySelector('#reviewTitle').textContent === name, name);
  }
}

async function checkProductCard(page) {
  const card = page.locator('#messages').getByRole('link', {name: 'Открыть товар SKU 123 на Ozon'});
  await card.getByText('Товар', {exact: true}).waitFor();
  assert.equal(await card.count(), 1);
  assert.equal(await card.getAttribute('href'), 'https://www.ozon.ru/product/123');
  assert.equal(await card.getAttribute('target'), '_blank');
  assert.deepEqual(new Set((await card.getAttribute('rel')).split(/\s+/)), new Set(['noopener', 'noreferrer']));
  assert.equal(await card.getAttribute('title'), 'Открыть товар на Ozon');
  assert.ok(await card.getByText('Открыть товар на Ozon', {exact: true}).isVisible());
  assert.equal(await card.locator('img').count(), 0);
  assert.equal(await card.evaluate(el => getComputedStyle(el).borderBottomWidth), '1px');
}

async function applyReplyTemplate(page, template) {
  await page.locator('#replyTemplatesToggleBtn').click();
  const row = page.locator('#replyTemplatesList .reply-template-row');
  await row.getByText('Без названия').waitFor();
  const preview = row.locator('.reply-template-item-preview');
  assert.equal((await row.innerText()).replace(/\s+/g, ' ').trim(),
    'Без названия ' + (await preview.innerText()).trim());
  const edit = row.getByRole('button', {name: 'Редактировать шаблон'});
  const remove = row.getByRole('button', {name: 'Удалить шаблон'});
  const editBox = await edit.boundingBox();
  const removeBox = await remove.boundingBox();
  assert.ok(editBox && removeBox && editBox.y + editBox.height <= removeBox.y + 1);
  assert.equal(await edit.evaluate(el => getComputedStyle(el).borderTopWidth), '0px');
  assert.equal(await edit.locator('svg').count(), 1);
  assert.equal(await remove.locator('svg').count(), 1);
  assert.equal(await preview.evaluate(el => getComputedStyle(el).webkitLineClamp), '3');
  await row.locator('[data-reply-template-apply]').click();
  assert.equal((await page.locator('#messageText').inputValue()).trim(), template.content.trim());
}

async function checkComposer(page) {
  const previewUrl = 'https://fixture.invalid/preview.png';
  const imageRequests = [];
  page.on('request', req => { if (req.url().includes('fixture.invalid')) imageRequests.push(req.url()); });
  const message = {id: 81, chat_id: 71, direction: 'inbound', text: 'Вопрос о товаре', raw: {
    context: {image_url: previewUrl},
    _crm_product_context: {kind: 'ozon_product', sku: '123', url: 'https://www.ozon.ru/product/123', image_url: previewUrl},
  }};
  const chat = {id: 71, customer_name: 'Тестовый покупатель', marketplace: 'ozon', status: 'new', messages: [message], tasks: []};
  await page.route('**/api/chats?**', route => route.fulfill({json: {items: [chat], total: 1, has_more: false}}));
  await page.route('**/api/chats/71?**', route => route.fulfill({json: chat}));
  await page.route('**/api/chats/71/message-send-operations', route => route.fulfill({json: {operations: []}}));
  const template = {id: 3, title: '', content: 'Длинный ответ '.repeat(50), updated_at: '2001-02-03'};
  await page.route('**/api/reply-templates', route => route.fulfill({json: [template]}));
  await page.evaluate(async () => { showView('chats'); await openChat(71); });
  await checkProductCard(page);
  await applyReplyTemplate(page, template);

  // A lost POST response must be resolved using the original operation ID.
  const messages = [], lookups = [], uploads = [];
  await page.route('**/api/chats/71/messages', route => {
    messages.push(route.request().postDataJSON());
    return route.abort();
  });
  await page.route('**/api/chats/71/message-send-operations/*', route => {
    lookups.push(new URL(route.request().url()).pathname.split('/').pop());
    return route.fulfill({json: {operation: {status: 'accepted'}}});
  });
  await page.route('**/api/chats/71/attachments', async route => {
    const req = route.request();
    const form = await new Response(req.postDataBuffer(), {
      headers: {'content-type': req.headers()['content-type']},
    }).formData();
    uploads.push(form);
    await route.fulfill({json: {ok: true}});
  });
  const image = {name: 'example.png', mimeType: 'image/png', buffer: Buffer.from('fixture')};
  await page.locator('#chatImageInput').setInputFiles(image);
  const send = page.locator('#messageForm button[type=submit]');
  await Promise.all([page.waitForResponse(r => r.url().endsWith('/attachments')), send.click()]);
  await page.waitForFunction(() => !document.querySelector('#messageForm button[type=submit]').disabled);
  await checkProductCard(page);
  assert.deepEqual(imageRequests, []);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].text, template.content.trim());
  assert.equal(messages[0].intent_origin, 'attachment_caption');
  assert.equal(messages[0].author, undefined);
  assert.ok(messages[0].operation_id);
  assert.deepEqual(lookups, [messages[0].operation_id]);
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].getAll('images').length, 1);
  assert.equal(uploads[0].has('caption'), false);
  assert.ok(uploads[0].get('operation_id'));
  assert.notEqual(uploads[0].get('operation_id'), messages[0].operation_id);

  await checkUnavailableAttachment(page, image, send);
  assert.equal(uploads.length, 1);
}

async function checkUnavailableAttachment(page, image, send) {
  // Exercise the composer's fallback for an unavailable browser File API
  // after the server has already received the caption.
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route('**/api/chats/71/messages', async route => {
    await gate;
    await route.fulfill({json: {operation: {status: 'confirmed'}}});
  });
  await page.locator('#messageText').fill('Вторая подпись');
  await page.locator('#chatImageInput').setInputFiles(image);
  // Simulate a later submission, outside the composer's double-click interval.
  await page.clock.setFixedTime(new Date(Date.now() + 60000));
  await Promise.all([page.waitForRequest(r => r.url().endsWith('/messages')), send.click()]);
  await page.evaluate(() => { window.File = undefined; });
  release();
  await page.getByText('Подпись отправлена; вложения не загружены. Выберите файлы повторно.', {exact: true}).waitFor();
}

async function checkLoginError(page) {
  const dialogs = [];
  page.on('dialog', async dialog => { dialogs.push(dialog.type()); await dialog.dismiss(); });
  await page.route('**/api/auth/login', route => route.fulfill({status: 401, json: {detail: {code: 'unknown-test-error'}}}));
  await page.evaluate(() => showLogin());
  await page.locator('#loginUsername').fill('fixture');
  await page.locator('#loginPassword').fill('fixture');
  await page.locator('#loginSubmitBtn').click();
  await page.locator('#loginError').getByText('Не удалось выполнить вход. Повторите попытку позже').waitFor();
  assert.deepEqual(dialogs, []);
}

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    for (const width of [1366, 375]) await checkFboNavigation(browser, width);
    const {page} = await setup(browser, 1366, 'light');
    await checkReviewTitles(page);
    await checkComposer(page);
    await checkLoginError(page);
    console.log('PASS FBO navigation, review titles, reply templates, attachment delivery and login errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
