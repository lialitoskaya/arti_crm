const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const baseline = process.env.PERMISSIONS_BASELINE === '1';
const read = file => baseline ? execFileSync('git', ['show', `8ba8161:${file}`], {cwd: root, encoding: 'utf8', maxBuffer: 4e6}) : fs.readFileSync(path.join(root, file), 'utf8');
const catalog = ['chats', 'tasks', 'reviews', 'questions', 'knowledge', 'analytics', 'supplyPlanning', 'ozonFbo', 'techSettings'];
const labels = ['Чаты', 'Задачи', 'Отзывы', 'Вопросы', 'База знаний', 'Аналитика', 'Планирование поставок', 'Поставки FBO', 'Технические настройки'];
const none = Object.fromEntries(catalog.map(key => [key, 'none']));
const admin = {id: 1, username: 'fixture-admin', role: 'admin', is_active: true, permissions: Object.fromEntries(catalog.map(key => [key, 'write']))};
const employee = {id: 2, username: 'fixture-employee', display_name: 'Сотрудник с длинным именем для проверки отображения', role: 'manager', is_active: true, permissions: {...none, tasks: 'write'}};

async function setup(browser, width, theme, user = admin) {
  const page = await browser.newPage({viewport: {width, height: 900}});
  const requests = [];
  await page.route('**/*', async route => {
    const req = route.request(); const url = new URL(req.url());
    if (url.hostname !== 'crm.test') return route.abort();
    const pathname = url.pathname;
    if (pathname === '/') return route.fulfill({contentType: 'text/html', body: read('app/static/index.html')});
    if (pathname === '/static/app.js' || pathname === '/static/styles.css' || (!baseline && ['/static/permissions.js', '/static/yandex-oauth-links.js'].includes(pathname))) {
      return route.fulfill({contentType: pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: read(`app${pathname}`)});
    }
    if (!pathname.startsWith('/api/')) return route.fulfill({status: 404, body: ''});
    requests.push({path: pathname, method: req.method(), body: req.postDataJSON()});
    let body = {};
    if (pathname === '/api/auth/me') body = {authenticated: true, user};
    else if (pathname === '/api/security/csrf') body = {csrf_token: 'synthetic-csrf'};
    else if (pathname === '/api/permissions') body = {sections: catalog.map((key, i) => ({key, label: labels[i]}))};
    else if (pathname === '/api/users') body = req.method() === 'GET' ? [admin, employee] : employee;
    else if (pathname === '/api/users/assignees') body = [admin, employee];
    else if (pathname === '/api/notifications') body = {items: [], unread_count: 0};
    else if (pathname === '/api/chat-settings') body = {statuses: [], funnels: []};
    else if (pathname === '/api/task-types' || pathname === '/api/tasks' || pathname === '/api/questions' || pathname === '/api/reviews') body = [];
    else if (pathname === '/api/chats') body = {items: [], total: 0, unread_count: 0, has_more: false};
    else if (pathname === '/api/auth/yandex/status') body = {enabled: false};
    return route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
  });
  await page.goto('https://crm.test/');
  await page.waitForFunction(() => typeof appInitialized !== 'undefined' && appInitialized);
  await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
  return {page, requests};
}

module.exports = {setup, baseline, employee, none};
