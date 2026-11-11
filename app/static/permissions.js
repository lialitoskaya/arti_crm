// The server supplies effective rights and remains the authority for every API call.
let employeePermissionCatalog = [];
let permissionRefreshPending = null;

function canSection(section, level = 'read') {
  if (!currentUser) return false;
  if (currentUser.role === 'admin') return true;
  const actual = currentUser.permissions?.[section] || 'none';
  return level === 'write' ? actual === 'write' : actual === 'read' || actual === 'write';
}

function canOpenView(view) {
  if (view === 'profile') return Boolean(currentUser);
  if (view === 'users') return currentUser?.role === 'admin';
  return canSection(view);
}

function firstAllowedView() {
  return Object.keys(currentUser?.permissions || {}).find(key => canOpenView(key)) || 'profile';
}

function permissionControlAllowed(element) {
  if (element.dataset.permissionAdmin !== undefined) return currentUser?.role === 'admin';
  const requirements = (element.dataset.permissionWrite || '').split(' ').filter(Boolean);
  return requirements.every(key => canSection(key, 'write'));
}

function applyPermissionControls(root = document) {
  const selector = '[data-permission-write], [data-permission-admin]';
  const elements = [...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)];
  for (const element of elements) {
    const denied = !permissionControlAllowed(element);
    element.inert = denied;
    element.classList.toggle('permission-readonly', denied);
    element.setAttribute('aria-disabled', String(denied));
    if (denied) element.setAttribute('title', 'Для этого действия нужно право «Работа с данными»');
    else if (element.getAttribute('title') === 'Для этого действия нужно право «Работа с данными»') element.removeAttribute('title');
  }
}

function updatePermissionNavigation() {
  const names = ['chats', 'tasks', 'reviews', 'questions', 'knowledge', 'analytics', 'supplyPlanning', 'ozonFbo', 'techSettings', 'users'];
  names.forEach(key => {
    const element = document.getElementById(`nav${key[0].toUpperCase()}${key.slice(1)}`);
    element?.classList.toggle('hidden', !canOpenView(key));
    if (key === 'supplyPlanning') element?.toggleAttribute('data-section-granted', currentUser?.role !== 'admin' && canOpenView(key));
  });
  document.querySelectorAll('[data-mobile-view]').forEach(element => {
    element.classList.toggle('hidden', !canOpenView(element.dataset.mobileView));
  });
  document.getElementById('analyticsDrilldownDetails')?.classList.toggle('hidden', !canSection('chats'));
  document.querySelector('[data-extra="tasks"]')?.classList.toggle('hidden', !canSection('tasks'));
  if (!canSection('tasks')) document.getElementById('tasksSection')?.classList.add('hidden');
  applyPermissionControls();
}

function updateSectionAccessStatus(view) {
  document.querySelectorAll('.section-access-status').forEach(element => element.remove());
  if (view === 'users' || view === 'profile' || canSection(view, 'write')) return;
  const root = document.getElementById(`${view}View`);
  if (!root) return;
  const message = document.createElement('p');
  message.className = 'muted section-access-status';
  message.textContent = 'Доступ: только просмотр';
  message.setAttribute('role', 'status');
  (root.querySelector('h2')?.parentElement || root).append(message);
}

function markTaskPermissionControls(item) {
  const section = item.dataset.permissionTaskStandalone === 'true' ? 'tasks' : 'tasks chats';
  item.querySelectorAll('[data-task-action], [data-task-edit-toggle], [data-task-status-pill], [data-task-ref-edit], [data-task-ref-delete], [data-task-edit-panel], [data-task-ref-edit-panel]')
    .forEach(element => { element.dataset.permissionWrite = section; });
  applyPermissionControls(item);
}

function renderEmployeePermissions(prefix, permissions = {}) {
  const root = document.getElementById(`${prefix}UserPermissions`);
  if (!root) return;
  root.replaceChildren();
  for (const section of employeePermissionCatalog) {
    const label = document.createElement('label');
    label.className = 'employee-permission-row';
    const title = document.createElement('span');
    title.textContent = section.label;
    const select = document.createElement('select');
    select.dataset.section = section.key;
    select.setAttribute('aria-label', `Доступ: ${section.label}`);
    for (const [value, text] of [['none', 'Нет доступа'], ['read', 'Просмотр'], ['write', 'Работа с данными']]) {
      select.add(new Option(text, value));
    }
    select.value = permissions[section.key] || 'none';
    label.append(title, select);
    root.append(label);
  }
  updateEmployeePermissionRole(prefix);
}

function updateEmployeePermissionRole(prefix) {
  const admin = document.getElementById(`${prefix}UserRole`)?.value === 'admin';
  const root = document.getElementById(`${prefix}UserPermissions`);
  root?.querySelectorAll('select').forEach(select => { select.disabled = admin; });
  const note = document.getElementById(`${prefix}UserPermissionsNote`);
  if (note) note.textContent = admin
    ? 'Администратор имеет полный доступ ко всем разделам и управлению сотрудниками.'
    : 'Права ниже действуют независимо от названия роли. Связанные с чатами задачи требуют также доступа к чатам.';
}

function readEmployeePermissions(prefix) {
  if (!employeePermissionCatalog.length) throw new Error('Список прав ещё не загружен. Откройте раздел «Сотрудники» заново.');
  return Object.fromEntries([...document.querySelectorAll(`#${prefix}UserPermissions select`)]
    .map(select => [select.dataset.section, select.value]));
}

async function refreshSessionPermissions() {
  if (!currentUser || permissionRefreshPending) return permissionRefreshPending;
  permissionRefreshPending = (async () => {
    const response = await fetch('/api/auth/me', {cache: 'no-store', credentials: 'same-origin'});
    if (response.status === 401) { window.location.reload(); return; }
    if (!response.ok) return;
    const {user} = await response.json();
    if (user && (user.id !== currentUser.id || user.role !== currentUser.role ||
        JSON.stringify(user.permissions) !== JSON.stringify(currentUser.permissions))) window.location.reload();
  })().catch(() => {}).finally(() => { permissionRefreshPending = null; });
  return permissionRefreshPending;
}

function bindSectionPermissions() {
  const shell = document.getElementById('appShell');
  if (!shell || shell.dataset.permissionsBound) return;
  shell.dataset.permissionsBound = '1';
  ['new', 'edit'].forEach(prefix => {
    document.getElementById(`${prefix}UserRole`)?.addEventListener('change', () => updateEmployeePermissionRole(prefix));
  });
  // Only newly inserted subtrees are inspected; message-history updates do not rescan the page.
  new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (node.nodeType === 1) applyPermissionControls(node);
    }
  }).observe(shell, {childList: true, subtree: true});
  document.addEventListener('click', event => {
    const control = event.target.closest?.('[data-permission-write], [data-permission-admin]');
    if (control && !permissionControlAllowed(control)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
}
