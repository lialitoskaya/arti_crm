// Run with Node and Playwright available; uses synthetic data and blocks all network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'app/static/app.js'), 'utf8');
const renderer = source.slice(source.indexOf('function renderChatList('), source.indexOf('async function openChat('));
const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const asideStart = html.indexOf('<aside', html.indexOf('id="chatsView"'));
const sidebar = html.slice(asideStart, html.indexOf('</aside>', asideStart) + 8);
const css = fs.readFileSync(path.join(root, 'app/static/styles.css'), 'utf8');

(async () => {
  const browser = await chromium.launch({ channel: process.env.TEST_BROWSER_CHANNEL || 'msedge', headless: true });
  try {
    for (const width of [1366, 375, 768]) {
      for (const theme of ['light', 'dark']) {
        const page = await browser.newPage({ viewport: { width, height: 812 } });
        await page.route('**/*', route => route.abort());
        await page.setContent(`<html data-theme="${theme}"><body><div id="chatsView" class="layout" style="height:780px">${sidebar}<section class="conversation panel-card"></section></div></body></html>`);
        await page.addStyleTag({ content: css });
        await page.addScriptTag({ content: `
          const $ = id => document.getElementById(id);
          const bindChatListInfiniteScroll = () => {};
          const updateChatCountLabel = () => {};
          const isMobileChatOpen = () => false;
          const currentChatMessageSearch = () => $('chatSearchInput').value;
          const isClosedWorkflowStatus = () => false;
          const shouldShowWaitingMarker = () => false;
          const waitingResponseBadge = () => '';
          const formatChatTime = () => '12:30';
          const previewText = text => text;
          const escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
          const customerLabel = chat => chat.customer_name;
          const statusBadge = () => '<span class="status-badge">Новый</span>';
          const marketplaceNames = { ozon: 'Ozon' };
          const chatScope = 'active';
          const currentChatId = 1;
          let chats = [];
          ${renderer}
          window.showChats = (count, search, long = false) => {
            $('chatSearchInput').value = search ? 'доставка' : '';
            $('chatSearchBox').classList.toggle('hidden', !search);
            $('chatSearchBox').closest('.filters').classList.toggle('chat-search-open', search);
            chats = Array.from({length: count}, (_, i) => ({id: i + 1, marketplace: 'ozon', status: 'new',
              customer_name: long ? 'Очень длинное имя покупателя '.repeat(8) : 'Тестовый покупатель',
              last_message_preview: 'Когда доставка?', search_match_text: search ? (long ? 'Доставка'.repeat(150) : 'Когда доставка?') : null}));
            renderChatList();
          };
        ` });
        const metrics = () => page.evaluate(() => {
          const list = document.getElementById('chatList');
          const row = list.querySelector('.chat-item');
          const rect = row.getBoundingClientRect();
          return { height: rect.height, top: rect.top, listTop: list.getBoundingClientRect().top,
            overflow: list.scrollWidth > list.clientWidth, scrolls: list.scrollHeight > list.clientHeight };
        });
        await page.evaluate(() => showChats(12, false));
        const ordinary = await metrics();
        await page.evaluate(() => showChats(1, true));
        const single = await metrics();
        if (process.env.UI_SCREENSHOT_DIR) {
          fs.mkdirSync(process.env.UI_SCREENSHOT_DIR, { recursive: true });
          await page.screenshot({ path: path.join(process.env.UI_SCREENSHOT_DIR, `search-${width}-${theme}.png`) });
        }
        assert.ok(Math.abs(single.height - ordinary.height) < 1,
          `${width}/${theme}: single search result ${single.height}px, ordinary row ${ordinary.height}px`);
        assert.ok(Math.abs(single.top - single.listTop) < 1, 'Results must start at the top');
        for (const count of [1, 2, 12]) {
          await page.evaluate(count => showChats(count, true, true), count);
          const long = await metrics();
          assert.ok(Math.abs(long.height - ordinary.height) < 1, 'Long preview must retain standard row height');
          assert.equal(long.overflow, false, 'Long preview must not widen list');
          if (count === 12) assert.equal(long.scrolls, true, 'Long list must still scroll');
        }
        await page.evaluate(() => showChats(1, false));
        assert.ok(Math.abs((await metrics()).height - ordinary.height) < 1, 'Clearing search keeps normal sizing');
        await page.evaluate(() => showChats(0, true));
        assert.match(await page.locator('.empty-chat-item').innerText(), /ничего не найдено/);
        assert.equal((await metrics()).overflow, false);
        console.log(`PASS ${width}px ${theme}: search, long content, empty, clear, scrolling`);
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
