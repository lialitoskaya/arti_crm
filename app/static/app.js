let currentChatId = null;
let currentChat = null;
let chats = [];
let refreshTimer = null;
let statusTimer = null;
let frontendSyncTimer = null;
let frontendSyncInFlight = false;
let frontendSyncLastStartedAt = 0;
let frontendSyncLastSuccessAt = 0;
let chatOpenInFlight = false;
let chatImageLazyObserver = null;
let chatImageLazyObserverRoot = null;
let activeView = 'chats';
const ROUTE_STORAGE_KEY = 'artiCrm.activeView';
const VALID_VIEWS = ['chats', 'analytics', 'tasks', 'reviews', 'questions', 'knowledge', 'users', 'techSettings', 'profile'];
const VIEW_ROUTES = {
  chats: 'chats',
  analytics: 'analytics',
  tasks: 'tasks',
  reviews: 'reviews',
  questions: 'questions',
  knowledge: 'knowledge',
  users: 'users',
  techSettings: 'settings',
  profile: 'profile',
};
const ROUTE_VIEWS = {
  chats: 'chats',
  chat: 'chats',
  analytics: 'analytics',
  analytic: 'analytics',
  tasks: 'tasks',
  task: 'tasks',
  reviews: 'reviews',
  review: 'reviews',
  questions: 'questions',
  question: 'questions',
  knowledge: 'knowledge',
  kb: 'knowledge',
  users: 'users',
  employees: 'users',
  staff: 'users',
  settings: 'techSettings',
  techsettings: 'techSettings',
  techSettings: 'techSettings',
  profile: 'profile',
};
let activeExtraPanel = '';
let chatScope = 'active';
let selectedAiMessageId = null;
let aiGenerating = false;
let reviews = [];
let currentReviewId = null;
let currentReview = null;
let questions = [];
let currentQuestionId = null;
let currentQuestion = null;
let questionAnswerDrafts = {};
let currentUser = null;
let assignees = [];
let usersCache = [];
let chatOwnerScope = 'all';
let knowledgeCategories = [];
let knowledgeArticles = [];
let currentKnowledgeCategoryId = null;
let currentKnowledgeArticleId = null;
let currentKnowledgeArticle = null;
let knowledgeMode = "empty";
let appInitialized = false;
let analyticsLoadedOnce = false;
let notifications = [];
let notificationsUnreadCount = 0;
let notificationsTimer = null;
let notificationsPanelOpen = false;
let notificationToastIds = [];
let notificationSeenUnreadIds = new Set();
let notificationsBootstrapDone = false;
let lastBrowserNotificationAt = 0;
let notificationsLoadPromise = null;
let chatsLoadPromise = null;
let statsLoadPromise = null;
let syncStatusLoadPromise = null;
let questionsSyncPromise = null;
let questionsSyncLastStartedAt = 0;
// Mobile navigation: when an operator taps 'back to chat list', keep the selected
// chat in memory but do not auto-open it again during background refresh.
let mobileChatClosedByUser = false;

let currentChatMetaSaveTimer = null;
let selectedChatImageFiles = [];
let openChatRequestSeq = 0;

function mergeChatSummary(updated) {
  if (!updated || !updated.id) return;
  chats = (chats || []).map((chat) => Number(chat.id) === Number(updated.id) ? { ...chat, ...updated } : chat);
  if (currentChat && Number(currentChat.id) === Number(updated.id)) {
    currentChat = { ...currentChat, ...updated };
  }
}

function refreshChatListInBackground() {
  loadChats().catch(err => console.warn('background chat list refresh failed', err));
}

async function persistCurrentChatMeta() {
  if (!currentChatId) return;
  const chatId = Number(currentChatId);
  const newStatus = $('chatStatus')?.value;
  const assignedUserId = $('assignedUserSelect')?.value ? Number($('assignedUserSelect').value) : null;

  // Optimistic UI update: the list should reflect the selected status immediately,
  // without waiting for a full chat reload.
  const statusMeta = (chatSettings.statuses || []).find(s => String(s.key) === String(newStatus));
  const assigneeOption = $('assignedUserSelect')?.selectedOptions?.[0];
  const optimistic = {
    id: chatId,
    status: newStatus,
    status_label: statusMeta?.title || statusNames[newStatus] || newStatus,
    status_color: statusMeta?.color || '',
    assigned_user_id: assignedUserId,
    assigned_to: assignedUserId ? (assigneeOption?.textContent || '').trim() : null,
  };
  mergeChatSummary(optimistic);
  renderChatList();

  try {
    const updated = await api(`/api/chats/${chatId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        status: newStatus,
        assigned_user_id: assignedUserId,
      }),
    });
    mergeChatSummary(updated);
    if (isClosedWorkflowStatus(updated.status || newStatus, updated.status_label || optimistic.status_label) && chatScope !== 'archive') {
      chats = (chats || []).filter((chat) => Number(chat.id) !== Number(chatId));
    }
    renderChatList();

    if (isClosedWorkflowStatus(updated?.status || newStatus, updated?.status_label || optimistic.status_label) && chatScope !== 'archive') {
      currentChatId = null;
      currentChat = null;
      selectedAiMessageId = null;
      mobileChatClosedByUser = false;
      setMobileChatOpen(false);
      $('chatPanel')?.classList.add('hidden');
      $('emptyState')?.classList.remove('hidden');
      if ($('emptyState')) $('emptyState').textContent = 'Чат закрыт и перенесён в Архив.';
      await loadChats();
      return;
    }

    // Refresh list data in the background, but do not reopen the dialog.
    // Reopening here caused slow mobile transitions and stale status display.
    refreshChatListInBackground();
  } catch (err) {
    notify('Не удалось сохранить статус чата', String(err.message || err));
    refreshChatListInBackground();
  }
}
function scheduleCurrentChatMetaSave() {
  if (currentChatMetaSaveTimer) clearTimeout(currentChatMetaSaveTimer);
  currentChatMetaSaveTimer = setTimeout(() => { persistCurrentChatMeta().catch(err => console.error(err)); }, 250);
}
let chatSettings = { funnels: [], statuses: [] };

const $ = (id) => document.getElementById(id);

const marketplaceNames = {
  ozon: 'Ozon',
  yandex: 'Яндекс',
  wildberries: 'WB',
};

let statusNames = {
  new: 'новый',
  in_progress: 'в работе',
  waiting_customer: 'ждём клиента',
  closed: 'закрыт',
};

const taskStatusNames = {
  open: 'открыта',
  in_progress: 'в работе',
  done: 'выполнено',
  archived: 'архив',
  cancelled: 'отменена',
};

const reviewStatusNames = {
  UNPROCESSED: 'без ответа',
  PROCESSED: 'обработан',
};

const questionStatusNames = {
  UNPROCESSED: 'без ответа',
  PROCESSED: 'обработан',
  PUBLISHED: 'опубликован',
  NEW: 'новый',
  ANSWERED: 'есть ответ',
};

function customerLabel(chat) {
  const name = (chat.customer_name || '').trim();
  if (name) return name;
  const publicId = (chat.customer_public_id || '').trim();
  if (publicId) return `Клиент ${publicId.slice(0, 8)}`;
  const externalId = (chat.external_chat_id || '').trim();
  if (externalId) return `Клиент ${externalId.slice(0, 8)}`;
  return 'Клиент';
}

function setStatus(text) {
  const status = $('syncStatus');
  if (status) status.textContent = text || '';
}

function notify(title, data) {
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  console.log(title, data);
  alert(`${title}\n\n${text}`);
}


function notificationTypeLabel(type) {
  const labels = {
    new_message: 'Сообщение',
    assigned_chat: 'Ответственный',
    new_task: 'Задача',
    task_event: 'Задача',
    event: 'Событие',
  };
  return labels[type] || 'Событие';
}

function updateNotificationsBadge() {
  const text = notificationsUnreadCount > 99 ? '99+' : String(notificationsUnreadCount || 0);
  ['notificationsBadge', 'mobileMoreBadge', 'mobileMoreNotificationsBadge'].forEach((id) => {
    const badge = $(id);
    if (!badge) return;
    if (notificationsUnreadCount > 0) {
      badge.textContent = text;
      badge.classList.remove('hidden');
    } else {
      badge.textContent = '0';
      badge.classList.add('hidden');
    }
  });
}

function currentUnreadNotifications() {
  return (notifications || []).filter((item) => !item.is_read);
}

function rememberUnreadNotificationIds(items) {
  notificationSeenUnreadIds = new Set((items || []).map((item) => Number(item.id)).filter(Boolean));
}

function enqueueNotificationToasts(items, { replace = false } = {}) {
  const incomingIds = (items || []).map((item) => Number(item.id)).filter(Boolean);
  if (replace) notificationToastIds = [];
  for (const id of incomingIds) {
    if (!notificationToastIds.includes(id)) notificationToastIds.unshift(id);
  }
  if (notificationToastIds.length > 3) {
    notificationToastIds = notificationToastIds.slice(0, 3);
  }
}

function renderNotifications() {
  const stack = $('notificationToasts');
  if (!stack) return;

  const unreadMap = new Map(currentUnreadNotifications().map((item) => [Number(item.id), item]));
  notificationToastIds = notificationToastIds.filter((id) => unreadMap.has(Number(id)));
  const visibleIds = notificationsPanelOpen ? notificationToastIds.slice(0, 3) : [];

  if (!visibleIds.length) {
    stack.innerHTML = '';
    stack.classList.remove('has-items');
    return;
  }

  stack.classList.add('has-items');
  stack.innerHTML = visibleIds.map((id) => {
    const item = unreadMap.get(Number(id));
    if (!item) return '';
    const title = escapeHtml(item.title || 'Уведомление');
    const body = escapeHtml(item.body || '');
    const time = escapeHtml(formatDateTime(item.created_at) || '');
    const typeLabel = escapeHtml(notificationTypeLabel(item.type));
    const icon = item.task_id ? '✓' : '✉';
    return `
      <article class="notification-toast" data-notification-open="${item.id}" role="button" tabindex="0">
        <button class="notification-close-btn" type="button" data-notification-close="${item.id}" aria-label="Закрыть уведомление">×</button>
        <div class="notification-toast-topline">
          <span class="notification-toast-icon" aria-hidden="true">${icon}</span>
          <div class="notification-toast-meta">
            <strong class="notification-toast-title">${title}</strong>
            <span class="notification-toast-subtitle">${typeLabel} · ${time}</span>
          </div>
        </div>
        ${body ? `<div class="notification-body">${body}</div>` : ''}
      </article>
    `;
  }).join('');

  stack.querySelectorAll('[data-notification-open]').forEach((el) => {
    const openHandler = (event) => {
      if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
      event.preventDefault();
      openNotification(Number(el.dataset.notificationOpen));
    };
    el.addEventListener('click', openHandler);
    el.addEventListener('keydown', openHandler);
  });

  stack.querySelectorAll('[data-notification-close]').forEach((el) => {
    el.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const id = Number(el.dataset.notificationClose);
      try {
        await markNotificationRead(id, { silent: true });
      } catch (err) {
        notify('Ошибка уведомлений', String(err.message || err));
      }
    });
  });
}

async function loadNotifications({ silent = true } = {}) {
  if (notificationsLoadPromise) return notificationsLoadPromise;
  notificationsLoadPromise = (async () => {
    try {
      const data = await api('/api/notifications?limit=30');
    const previousUnread = notificationsUnreadCount || 0;
    notifications = data.items || [];
    notificationsUnreadCount = Number(data.unread_count || 0);
    const unreadItems = currentUnreadNotifications();

    if (!notificationsBootstrapDone) {
      notificationsBootstrapDone = true;
      rememberUnreadNotificationIds(unreadItems);
    } else {
      const newUnreadItems = unreadItems.filter((item) => !notificationSeenUnreadIds.has(Number(item.id)));
      if (newUnreadItems.length) {
        enqueueNotificationToasts(newUnreadItems);
        notificationsPanelOpen = true;
      }
      rememberUnreadNotificationIds(unreadItems);
    }

    if (notificationsUnreadCount === 0) {
      notificationToastIds = [];
      notificationsPanelOpen = false;
    }

    updateNotificationsBadge();
    renderNotifications();

    if (notificationsUnreadCount > previousUnread && document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      const newest = unreadItems[0];
      const now = Date.now();
      if (newest && now - lastBrowserNotificationAt > 5000) {
        lastBrowserNotificationAt = now;
        new Notification(newest.title || 'Новое уведомление', { body: newest.body || 'Arti CRM' });
      }
    }
    } catch (err) {
      if (!silent) notify('Ошибка загрузки уведомлений', String(err.message || err));
      console.warn('notifications load failed', err);
    }
  })();
  try {
    return await notificationsLoadPromise;
  } finally {
    notificationsLoadPromise = null;
  }
}

async function toggleNotificationsPanel(force) {
  const unreadItems = currentUnreadNotifications();
  if (typeof force === 'boolean') {
    notificationsPanelOpen = force;
  } else if (!notificationsPanelOpen && unreadItems.length) {
    enqueueNotificationToasts(unreadItems.slice(0, 3), { replace: true });
    notificationsPanelOpen = true;
  } else {
    notificationsPanelOpen = !notificationsPanelOpen;
  }
  renderNotifications();
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().catch(() => {});
  }
}

async function markNotificationRead(notificationId, { silent = false } = {}) {
  notificationToastIds = notificationToastIds.filter((id) => Number(id) !== Number(notificationId));
  renderNotifications();
  await api(`/api/notifications/${notificationId}/read`, { method: 'POST' });
  await loadNotifications({ silent });
}

async function openNotification(notificationId) {
  const item = notifications.find((n) => Number(n.id) === Number(notificationId));
  if (!item) return;
  await markNotificationRead(notificationId, { silent: true });
  if (item.chat_id) {
    showView('chats');
    await loadChats();
    await openChat(Number(item.chat_id));
  } else if (item.task_id) {
    showView('tasks');
    await loadAllTasks();
  }
}

async function markAllNotificationsRead() {
  notificationToastIds = [];
  notificationsPanelOpen = false;
  renderNotifications();
  await api('/api/notifications/read-all', { method: 'POST' });
  await loadNotifications({ silent: false });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      detail = parsed.detail || parsed.error || body;
      if (Array.isArray(parsed.detail)) {
        detail = parsed.detail.map((item) => {
          const field = Array.isArray(item.loc) ? item.loc.filter(part => part !== 'body').join('.') : '';
          return `${field ? field + ': ' : ''}${item.msg || 'Ошибка заполнения'}`;
        }).join('; ');
      }
    } catch (_) {}
    if (response.status === 401) showLogin();
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json();
}

async function apiForm(path, formData, options = {}) {
  const response = await fetch(path, {
    method: options.method || 'POST',
    cache: 'no-store',
    body: formData,
    ...(options.fetchOptions || {}),
  });
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      detail = parsed.detail || parsed.error || body;
    } catch (_) {}
    if (response.status === 401) showLogin();
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json();
}

function parseDate(value) {
  if (!value) return null;
  const raw = String(value);
  let d = new Date(raw);
  if (Number.isNaN(d.getTime()) && raw.includes(' ') && !raw.includes('T')) {
    d = new Date(raw.replace(' ', 'T'));
  }
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatDateTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  return d.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatChatTime(value) {
  const d = parseDate(value);
  if (!d) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
}


function isImagePlaceholderText(value) {
  const text = String(value || '').trim();
  if (!text) return false;
  if (/^!\[[^\]]*\]\((https?:\/\/[^)]+|[^)]*изображ[^)]*)\)$/i.test(text)) return true;
  if (/^(https?:\/\/|\/api\/chat-uploads\/)[^\s<>"']+\.(jpg|jpeg|png|webp|gif|bmp|svg)(\?|#|$)/i.test(text)) return true;
  if (/^https?:\/\/api-seller\.ozon\.ru\/v\d+\/chat\/file\//i.test(text)) return true;
  return false;
}

function previewText(value) {
  const text = String(value || '').trim();
  if (!text) return 'Нет сообщений';
  if (isImagePlaceholderText(text)) return 'Изображение';
  const noMarkdownImages = text.replace(/!\[[^\]]*\]\([^)]+\)/g, '').trim();
  if (!noMarkdownImages && /!\[[^\]]*\]\([^)]+\)/.test(text)) return 'Изображение';
  if (/https?:\/\/api-seller\.ozon\.ru\/v\d+\/chat\/file\//i.test(noMarkdownImages)) return 'Изображение';
  return noMarkdownImages || 'Изображение';
}

function chatSubtitleParts(chat) {
  const parts = [];
  if (chat.order_id) parts.push(`Заказ: ${chat.order_id}`);
  if (chat.customer_public_id) parts.push(`клиент ID: ${chat.customer_public_id}`);
  if (chat.external_chat_id) parts.push(`chat_id: ${chat.external_chat_id}`);
  if (chat.last_message_at) parts.push(`последнее: ${formatDateTime(chat.last_message_at)}`);
  if (shouldShowWaitingMarker(chat)) parts.push(waitingDurationText(chat.sla_waiting_since_at || chat.last_message_at));
  return parts.join(' · ');
}


function pluralRu(value, one, few, many) {
  const n = Math.abs(Number(value || 0));
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function waitingDurationText(value) {
  const startedAt = parseDate(value);
  if (!startedAt) return 'ждёт ответа';
  const minutes = Math.max(0, Math.floor((Date.now() - startedAt.getTime()) / 60000));
  if (minutes < 1) return 'ждёт ответа меньше минуты';
  if (minutes < 60) {
    return `ждёт ответа ${minutes} ${pluralRu(minutes, 'минуту', 'минуты', 'минут')}`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) {
    const hourText = `${hours} ${pluralRu(hours, 'час', 'часа', 'часов')}`;
    return rest > 0
      ? `ждёт ответа ${hourText} ${rest} ${pluralRu(rest, 'минуту', 'минуты', 'минут')}`
      : `ждёт ответа ${hourText}`;
  }
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  const dayText = `${days} ${pluralRu(days, 'день', 'дня', 'дней')}`;
  return restHours > 0
    ? `ждёт ответа ${dayText} ${restHours} ${pluralRu(restHours, 'час', 'часа', 'часов')}`
    : `ждёт ответа ${dayText}`;
}

function isWaitingResponseBlockedStatus(chat) {
  const status = String(chat?.status || '').toLowerCase();
  const label = String(chat?.status_label || statusNames[status] || '').toLowerCase().replace('ё', 'е');
  return status === 'closed'
    || status === 'waiting_customer'
    || label.includes('закры')
    || label.includes('ждем клиент');
}

function isClosedWorkflowStatus(status, label = '') {
  const rawStatus = String(status || '').toLowerCase();
  const rawLabel = String(label || statusNames[rawStatus] || '').toLowerCase().replace('ё', 'е');
  return rawStatus === 'closed'
    || rawStatus === 'archived'
    || rawStatus === 'zakryt'
    || rawLabel.includes('закры')
    || rawLabel.includes('архив');
}

function shouldShowWaitingMarker(chat) {
  return Boolean(chat?.sla_waiting_response) && !isWaitingResponseBlockedStatus(chat);
}

function waitingResponseBadge(chat) {
  if (!shouldShowWaitingMarker(chat)) return '';
  const since = chat.sla_waiting_since_at || chat.last_message_at || chat.updated_at || chat.created_at;
  return `<span class="sla-badge sla-badge-waiting">${escapeHtml(waitingDurationText(since))}</span>`;
}


function datetimeLocalValue(value) {
  const d = parseDate(value);
  if (!d) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function taskStatusOptions(current) {
  return ['open', 'in_progress', 'done', 'archived', 'cancelled'].map(status =>
    `<option value="${status}" ${current === status ? 'selected' : ''}>${escapeHtml(taskStatusNames[status] || status)}</option>`
  ).join('');
}



function assigneeDisplay(user) {
  if (!user) return '';
  return user.display_name || user.username || '';
}

function assigneeOptions(selectedId, includeEmpty = true) {
  const selected = selectedId ? String(selectedId) : '';
  const empty = includeEmpty ? `<option value="">Не назначен</option>` : '';
  return empty + (assignees || []).map(u => {
    const value = String(u.id);
    const label = `${assigneeDisplay(u)}${u.role === 'admin' ? ' · админ' : u.role === 'viewer' ? ' · наблюдатель' : ''}`;
    return `<option value="${escapeHtml(value)}" ${value === selected ? 'selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
}

async function loadAssignees() {
  try {
    const data = await api('/api/users/assignees');
    assignees = Array.isArray(data) ? data : [];
    if (!assignees.length && currentUser?.id) {
      assignees = [currentUser];
    }
    hydrateAssigneeSelects();
    return assignees;
  } catch (err) {
    console.warn('assignees load failed', err);
    if (!assignees.length && currentUser?.id) assignees = [currentUser];
    hydrateAssigneeSelects();
    return assignees;
  }
}

function hydrateAssigneeSelects() {
  const chatSelect = $('assignedUserSelect');
  if (chatSelect) {
    const current = chatSelect.value || currentChat?.assigned_user_id || '';
    chatSelect.innerHTML = assigneeOptions(current, true);
  }
  const taskSelect = $('taskAssignee');
  if (taskSelect) {
    const current = taskSelect.value || '';
    taskSelect.innerHTML = assigneeOptions(current, true);
  }
}

function assigneeNameFromTask(task) {
  return task.assignee || task.assignee_user_display_name || task.assignee_user?.display_name || task.assignee_user?.username || '';
}

function renderTaskComments(task) {
  const comments = task.comments || [];
  if (!comments.length) return '<div class="task-comments-empty">Комментариев пока нет.</div>';
  return comments.map(c => `
    <div class="task-comment">
      <div class="task-comment-meta">${escapeHtml(c.author || 'manager')} · ${escapeHtml(formatDateTime(c.created_at) || '')}</div>
      <div>${escapeHtml(c.comment || '')}</div>
    </div>
  `).join('');
}

async function patchTask(taskId, body, options = {}) {
  const task = await api(`/api/tasks/${taskId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
  if (currentChatId && options.refreshChat !== false) await openChat(currentChatId);
  await loadAllTasks();
  await loadStats();
  return task;
}

function bindTaskCardActions(item, options = {}) {
  item.querySelectorAll('[data-task-action]').forEach(btn => {
    btn.onclick = async () => {
      const taskId = Number(btn.dataset.taskId);
      const action = btn.dataset.taskAction;
      const card = btn.closest('[data-task-card]');
      if (!taskId || !card) return;
      const body = {};
      if (action === 'save') {
        body.status = card.querySelector('[data-task-status]')?.value || undefined;
        body.assigned_user_id = card.querySelector('[data-task-assignee]')?.value ? Number(card.querySelector('[data-task-assignee]').value) : null;
        body.due_at = card.querySelector('[data-task-due]')?.value || null;
        body.comment = card.querySelector('[data-task-comment]')?.value || null;
      } else if (action === 'start') {
        body.status = 'in_progress';
      } else if (action === 'done') {
        body.status = 'done';
        body.comment = card.querySelector('[data-task-comment]')?.value || null;
      } else if (action === 'archive') {
        body.status = 'archived';
        body.comment = card.querySelector('[data-task-comment]')?.value || null;
      } else if (action === 'cancel') {
        body.status = 'cancelled';
        body.comment = card.querySelector('[data-task-comment]')?.value || null;
      }
      btn.disabled = true;
      try {
        await patchTask(taskId, body, options);
      } catch (err) {
        notify('Не удалось обновить задачу', String(err.message || err));
      } finally {
        btn.disabled = false;
      }
    };
  });
}

function chatStatusColor(status, fallbackColor = '') {
  if (fallbackColor) return fallbackColor;
  const found = (chatSettings.statuses || []).find((item) => String(item.key) === String(status));
  return found?.color || '';
}

function statusBadge(status, labelOverride = '', color = '') {
  const label = labelOverride || statusNames[status] || status || '—';
  const resolvedColor = chatStatusColor(status, color);
  const colorClass = resolvedColor ? ` status-color-${String(resolvedColor).replace(/[^a-z0-9_-]/gi, '')}` : '';
  return `<span class="status-badge status-${escapeHtml(status || 'unknown')}${colorClass}" data-status-color="${escapeHtml(resolvedColor || '')}">${escapeHtml(label)}</span>`;
}

function activeChatStatuses(includeClosed = false) {
  const statuses = (chatSettings.statuses || []).filter(s => s.is_active !== 0 && s.is_active !== false);
  if (includeClosed) return statuses;
  return statuses.filter(s => s.key !== 'closed');
}

function funnelOptions(selected = '') {
  return '<option value="">Все статусы</option>';
}

function statusOptions(selected = '', includeClosed = false, funnelId = '') {
  const statuses = activeChatStatuses(includeClosed);
  return [includeClosed ? '' : '<option value="">Все активные</option>', ...statuses.map(s => `<option value="${escapeHtml(s.key)}" ${String(selected) === String(s.key) ? 'selected' : ''}>${escapeHtml(s.title)}</option>`)].join('');
}

async function loadChatSettings(options = {}) {
  try {
    chatSettings = await api('/api/chat-settings');
    const nextNames = {};
    (chatSettings.statuses || []).forEach(s => { nextNames[s.key] = s.title; });
    statusNames = { ...statusNames, ...nextNames };
    renderChatSettingsControls(options);
  } catch (err) {
    console.warn('chat settings failed', err);
  }
}

const chatStatusColors = {
  orange: { label: 'Оранжевый', dot: '🟠' },
  blue: { label: 'Синий', dot: '🔵' },
  purple: { label: 'Фиолетовый', dot: '🟣' },
  green: { label: 'Зелёный', dot: '🟢' },
  red: { label: 'Красный', dot: '🔴' },
  gray: { label: 'Серый', dot: '⚪' },
};

function statusColorChoices(selected = 'orange') {
  return Object.entries(chatStatusColors).map(([key, meta]) => `
    <option value="${escapeHtml(key)}" ${String(selected || 'orange') === key ? 'selected' : ''}>${escapeHtml(meta.dot)} ${escapeHtml(meta.label)}</option>
  `).join('');
}

function renderChatSettingsControls(options = {}) {
  const funnelFilter = $('funnelFilter');
  if (funnelFilter) {
    funnelFilter.closest?.('select')?.classList.add('hidden');
    funnelFilter.value = '';
  }

  const statusFilter = $('statusFilter');
  if (statusFilter) {
    const selected = statusFilter.value;
    statusFilter.innerHTML = statusOptions(selected, false, '');
  }

  const chatStatus = $('chatStatus');
  if (chatStatus) {
    const selected = currentChat?.status || chatStatus.value || 'new';
    chatStatus.innerHTML = statusOptions(selected, true, '');
    chatStatus.value = selected;
  }

  renderChatSettingsLists();
}

function renderChatSettingsLists() {
  const statusesList = $('chatStatusesList');
  if (!statusesList) return;

  const statuses = chatSettings.statuses || [];
  statusesList.innerHTML = statuses.length ? statuses.map(s => `
    <article class="status-settings-row" data-status-id="${s.id}">
      <div class="status-settings-main">
        <input data-status-title value="${escapeHtml(s.title)}" aria-label="Название статуса" />
        <span class="status-key-hint">${escapeHtml(s.key || '')}</span>
      </div>

      <select class="status-color-select" data-status-color aria-label="Цвет статуса">
        ${statusColorChoices(s.color || 'orange')}
      </select>

      <input class="status-sort-input" data-status-sort type="number" value="${Number(s.sort_order || 0)}" aria-label="Порядок" />

      <label class="status-active-toggle">
        <input data-status-active type="checkbox" ${s.is_active ? 'checked' : ''} />
        <span>активен</span>
      </label>

      <button type="button" class="crm-light-btn status-delete-btn" data-status-action="delete">Удалить</button>
    </article>
  `).join('') : '<p class="muted">Статусов пока нет.</p>';
}

async function saveAllChatStatuses() {
  const rows = Array.from(document.querySelectorAll('#chatStatusesList [data-status-id]'));
  if (!rows.length) return;
  const topBtn = $('saveChatSettingsBtn');
  const bottomBtn = $('saveChatSettingsBottomBtn');
  [topBtn, bottomBtn].forEach(btn => { if (btn) btn.disabled = true; });
  try {
    for (const row of rows) {
      const id = Number(row.dataset.statusId);
      await api(`/api/chat-settings/statuses/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          title: row.querySelector('[data-status-title]')?.value?.trim() || '',
          funnel_id: null,
          color: row.querySelector('[data-status-color]')?.value || 'orange',
          sort_order: Number(row.querySelector('[data-status-sort]')?.value || 0),
          is_active: Boolean(row.querySelector('[data-status-active]')?.checked),
        }),
      });
    }
    await loadChatSettings({ keepValues: true });
    await loadChats();
    notify('Технические настройки', 'Статусы сохранены.');
  } catch (err) {
    notify('Статусы не сохранены', String(err.message || err));
  } finally {
    [topBtn, bottomBtn].forEach(btn => { if (btn) btn.disabled = false; });
  }
}

function isMobileChatLayout() {
  return window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
}

function setMobileChatOpen(isOpen) {
  document.body.classList.toggle('mobile-chat-open', Boolean(isOpen));
}

function backToChatListMobile() {
  // On mobile this is a true screen transition: list -> chat -> list.
  // Do not clear currentChatId, otherwise the selected item and draft context are lost,
  // but prevent auto-refresh from opening the chat again by itself.
  mobileChatClosedByUser = true;
  openChatRequestSeq += 1;
  setMobileChatOpen(false);
  renderChatList();
}

let messagesAutoScrollToken = 0;
let messagesAutoScrollActive = false;
let messagesAutoScrollProgrammaticAt = 0;

function messagesDistanceFromBottom(box) {
  if (!box) return 0;
  return box.scrollHeight - box.scrollTop - box.clientHeight;
}

function bindMessagesScrollGuard(box) {
  if (!box || box.dataset.scrollGuardBound === '1') return;
  box.dataset.scrollGuardBound = '1';
  box.addEventListener('scroll', () => {
    if (!messagesAutoScrollActive) return;
    // Ignore scroll events caused by our own box.scrollTop changes.
    if (Date.now() - messagesAutoScrollProgrammaticAt < 90) return;
    // If the operator scrolls up while images are still loading, stop all pending
    // auto-scroll callbacks so the dialog is not thrown back to the bottom.
    if (messagesDistanceFromBottom(box) > 120) {
      messagesAutoScrollActive = false;
      messagesAutoScrollToken += 1;
    }
  }, { passive: true });
}

function scrollMessagesToBottom(reason = '') {
  const box = $('messages');
  if (!box) return;
  bindMessagesScrollGuard(box);

  const token = ++messagesAutoScrollToken;
  messagesAutoScrollActive = true;

  const doScroll = () => {
    if (token !== messagesAutoScrollToken || !messagesAutoScrollActive) return;
    messagesAutoScrollProgrammaticAt = Date.now();
    box.scrollTop = box.scrollHeight;
  };

  doScroll();
  requestAnimationFrame(() => {
    doScroll();
    requestAnimationFrame(doScroll);
  });
  setTimeout(doScroll, 80);
  setTimeout(doScroll, 250);
  setTimeout(doScroll, 650);

  box.querySelectorAll('img').forEach(img => {
    if (!img.complete) {
      const safeImageScroll = () => {
        if (token !== messagesAutoScrollToken || !messagesAutoScrollActive) return;
        requestAnimationFrame(doScroll);
      };
      img.addEventListener('load', safeImageScroll, { once: true });
      img.addEventListener('error', safeImageScroll, { once: true });
    }
  });

  // After the first layout settles, release the sticky-bottom mode. New image
  // loads after this point must not override manual reading position.
  setTimeout(() => {
    if (token === messagesAutoScrollToken) {
      messagesAutoScrollActive = false;
    }
  }, 900);
}

async function loadSyncStatus() {
  if (syncStatusLoadPromise) return syncStatusLoadPromise;
  syncStatusLoadPromise = (async () => {
    try {
      const data = await api('/api/sync/status');
    const bg = data.background || {};
    if (bg.enabled === false) {
      setStatus('Автообновление выключено');
      return;
    }
    const marketplaces = bg.marketplaces || {};
    const parts = [];
    for (const [key, value] of Object.entries(marketplaces)) {
      if (!value || value.enabled === false) continue;
      const label = key === 'ozon_reviews' ? 'Отзывы Ozon' : key === 'ozon_questions' ? 'Вопросы Ozon' : (marketplaceNames[key] || key);
      if (value.configured === false || value.status === 'skipped') {
        parts.push(`${label}: не настроен`);
        continue;
      }
      if (value.status === 'ok') {
        const r = value.result || {};
        parts.push(`${label}: ${r.count ?? 0}/${r.messages_count ?? 0}`);
      } else if (value.status === 'throttled') {
        parts.push(`${label}: пауза ${value.retry_after_seconds || 0}с`);
      } else if (value.status === 'cooldown') {
        parts.push(`${label}: лимит, пауза ${value.retry_after_seconds || 0}с`);
      } else if (value.status === 'error') {
        parts.push(`${label}: ошибка`);
        console.warn(`${label} background sync error`, value.error);
      }
    }
    if (parts.length) {
      setStatus(`Авто: ${parts.join(' · ')} · ${bg.interval_seconds || 20}с`);
    } else if (bg.status === 'waiting') {
      setStatus('Маркетплейсы обновляются в фоне');
    } else {
      setStatus('Фоновое обновление активно');
    }
    } catch (err) {
      console.warn('sync status failed', err);
    }
  })();
  try {
    return await syncStatusLoadPromise;
  } finally {
    syncStatusLoadPromise = null;
  }
}


// On Fastfox the opened CRM tab is also the reliable "worker".
// Ozon sync runs every 30s; WB/Yandex are included by /api/sync/operator
// but server-side throttles protect them from rate limits.
const FRONTEND_OZON_SYNC_INTERVAL_MS = 30000;
const FRONTEND_OZON_SYNC_MIN_GAP_MS = 25000;
const FRONTEND_OZON_QUESTIONS_SYNC_INTERVAL_MS = 60000;
const FRONTEND_OZON_QUESTIONS_SYNC_MIN_GAP_MS = 45000;

function isFrontendSyncAllowed() {
  if (!appInitialized || !currentUser) return false;
  if (document.hidden) return false;
  if (chatOpenInFlight) return false;
  // On shared hosting the backend process may not keep background loops alive.
  // While an operator has CRM open, the browser safely triggers a lightweight
  // Ozon inbox sync so new buyer messages appear without opening /docs manually.
  return true;
}

async function runFrontendOzonFastSync(options = {}) {
  const { silent = true, force = false } = options;
  if (!isFrontendSyncAllowed()) return null;
  if (frontendSyncInFlight) return null;
  const now = Date.now();
  if (!force && now - frontendSyncLastStartedAt < FRONTEND_OZON_SYNC_MIN_GAP_MS) return null;

  frontendSyncInFlight = true;
  frontendSyncLastStartedAt = now;
  try {
    if (!silent) setStatus('Обновляем чаты Ozon...');
    const result = await api('/api/sync/operator', { method: 'POST' });
    frontendSyncLastSuccessAt = Date.now();

    const chatsCount = Number(result?.count || 0);
    const messagesCount = Number(result?.messages_count || 0);
    const changed = chatsCount > 0 || messagesCount > 0 || Number(result?.reopened_closed_chats || 0) > 0;

    if (activeView === 'chats') {
      // On shared hosting each chat-list request can take seconds. Do not reload
      // chats on every polling tick; only refresh when the sync reports a real
      // update. This keeps opening a dialog from waiting behind background polls.
      if (changed) {
        await loadChats();
        if (messagesCount > 0 && currentChatId && !chatOpenInFlight && !(isMobileChatLayout() && mobileChatClosedByUser)) {
          await openChat(currentChatId, { keepScroll: true, silent: true });
        }
      }
    } else if (changed) {
      await loadStats();
    }

    if (!silent || changed) {
      setStatus(`Маркетплейсы обновлены: чатов ${chatsCount}, сообщений ${messagesCount}`);
    }
    return result;
  } catch (err) {
    console.warn('frontend marketplace sync failed', err);
    if (!silent) notify('Автосинхронизация маркетплейсов', String(err.message || err));
    setStatus('Автосинхронизация маркетплейсов: ошибка');
    return null;
  } finally {
    frontendSyncInFlight = false;
  }
}

async function runFrontendOzonQuestionsSync(options = {}) {
  const { silent = true, force = false } = options;
  if (!appInitialized || !currentUser) return null;
  if (document.hidden) return null;
  if (activeView !== 'questions' && !force) return null;
  if (questionsSyncPromise) return questionsSyncPromise;
  const now = Date.now();
  if (!force && now - questionsSyncLastStartedAt < FRONTEND_OZON_QUESTIONS_SYNC_MIN_GAP_MS) return null;

  questionsSyncLastStartedAt = now;
  questionsSyncPromise = (async () => {
    try {
      if (!silent) setStatus('Загружаю вопросы Ozon…');
      const result = await api('/api/questions/sync/ozon', { method: 'POST' });
      const count = Number(result?.count || 0);
      if (activeView === 'questions') {
        await loadQuestions();
        if (currentQuestionId) await openQuestion(currentQuestionId, { silent: true });
      }
      await loadStats();
      if (!silent || count > 0) setStatus(`Вопросы Ozon: загружено ${count}`);
      return result;
    } catch (err) {
      console.warn('frontend Ozon questions sync failed', err);
      if (!silent) notify('Вопросы не загрузились', String(err.message || err));
      return null;
    }
  })();
  try {
    return await questionsSyncPromise;
  } finally {
    questionsSyncPromise = null;
  }
}

function runFrontendQuestionsSyncSoon(reason = '') {
  window.setTimeout(() => {
    runFrontendOzonQuestionsSync({ silent: true, force: reason === 'show-questions' || reason === 'startup-questions' })
      .catch(err => console.warn('frontend questions sync failed', err));
  }, 400);
}

function startFrontendAutoSync() {
  if (frontendSyncTimer) clearInterval(frontendSyncTimer);
  frontendSyncTimer = setInterval(() => {
    runFrontendOzonFastSync({ silent: true }).catch(err => console.warn('frontend sync timer failed', err));
    if (activeView === 'questions') {
      runFrontendOzonQuestionsSync({ silent: true }).catch(err => console.warn('frontend questions sync timer failed', err));
    }
  }, FRONTEND_OZON_SYNC_INTERVAL_MS);
}

function runFrontendSyncSoon(reason = '') {
  window.setTimeout(() => {
    runFrontendOzonFastSync({ silent: true, force: reason === 'startup' }).catch(err => console.warn('frontend sync failed', err));
  }, 250);
}

async function loadStats() {
  if (statsLoadPromise) return statsLoadPromise;
  statsLoadPromise = (async () => {
    const stats = await api('/api/stats');
    const el = $('stats');
    if (el) {
      el.textContent = `Ждут ответа: ${stats.waiting_response || 0} · Задачи: ${stats.tasks_open || 0} · Отзывы: ${stats.reviews_unanswered || 0} · Вопросы: ${stats.questions_unanswered || 0} · Архив: ${stats.archived_chats || 0}`;
    }
    return stats;
  })();
  try {
    return await statsLoadPromise;
  } finally {
    statsLoadPromise = null;
  }
}

async function loadChats() {
  if (chatsLoadPromise) return chatsLoadPromise;
  chatsLoadPromise = (async () => {
    const params = new URLSearchParams();
  const marketplaceEl = $('marketplaceFilter');
  const statusEl = $('statusFilter');
  const funnelEl = $('funnelFilter');
  const marketplace = marketplaceEl ? marketplaceEl.value : '';
  const status = statusEl ? statusEl.value : '';
  const funnelId = funnelEl ? funnelEl.value : '';
  if (marketplace) params.set('marketplace', marketplace);
  if (chatScope === 'archive') {
    params.set('archived', 'true');
  } else {
    if (status) params.set('status', status);
    if (funnelId) params.set('funnel_id', funnelId);
    if (chatOwnerScope === 'mine') params.set('mine', 'true');
  }

    chats = await api(`/api/chats?${params.toString()}`);
    const chatCountLabel = $('chatCountLabel');
    if (chatCountLabel) chatCountLabel.textContent = String(chats.length);
    renderChatList();
    renderScopeTabs();
    await loadStats();
    return chats;
  })();
  try {
    return await chatsLoadPromise;
  } finally {
    chatsLoadPromise = null;
  }
}

async function refreshVisibleData() {
  if (document.hidden) return;
  if (frontendSyncInFlight || chatOpenInFlight) return;
  try {
    loadNotifications().catch(err => console.warn('notifications refresh failed', err));
    if (activeView === 'analytics') {
      await loadAnalytics();
      await loadStats();
      await loadSyncStatus();
      return;
    }
    if (activeView === 'tasks') {
      await loadAllTasks();
      await loadStats();
      await loadSyncStatus();
      return;
    }
    if (activeView === 'knowledge') {
      await loadKnowledge();
      return;
    }
    if (activeView === 'reviews') {
      await loadReviews();
      if (currentReviewId) await openReview(currentReviewId, { silent: true });
      await loadStats();
      await loadSyncStatus();
      return;
    }
    if (activeView === 'questions') {
      await runFrontendOzonQuestionsSync({ silent: true });
      await loadSyncStatus();
      return;
    }

    // Chat view is the heaviest view on shared hosting. Do not reload the open
    // dialog on passive timers; frontend fast-sync refreshes it only when new
    // messages are imported, and the Refresh button still calls this explicitly.
    await loadChats();
    await loadSyncStatus();
  } catch (err) {
    console.warn('auto refresh failed', err);
  }
}

function currentChatScopeSelectValue() {
  if (chatScope === 'archive') return 'archive';
  if (chatOwnerScope === 'mine') return 'mine';
  return 'active';
}

function renderScopeTabs() {
  $('activeChatsTab')?.classList.toggle('active', chatScope === 'active');
  $('archiveChatsTab')?.classList.toggle('active', chatScope === 'archive');
  $('myChatsTab')?.classList.toggle('active', chatOwnerScope === 'mine' && chatScope !== 'archive');
  const scopeSelect = $('chatScopeSelect');
  if (scopeSelect) scopeSelect.value = currentChatScopeSelectValue();
  const statusFilter = $('statusFilter');
  if (statusFilter) {
    statusFilter.disabled = chatScope === 'archive';
    statusFilter.title = chatScope === 'archive' ? 'В архиве показываются только закрытые чаты' : '';
  }
}

function handleChatScopeSelectChange(event) {
  const value = event.target?.value || 'active';
  if (value === 'archive') {
    switchChatScope('archive');
    return;
  }
  if (value === 'mine') {
    chatOwnerScope = 'all';
    switchChatOwnerScope('mine');
    return;
  }
  chatOwnerScope = 'all';
  switchChatScope('active');
}

function switchChatScope(scope) {
  chatScope = scope === 'archive' ? 'archive' : 'active';
  if (chatScope === 'archive') chatOwnerScope = 'all';
  currentChatId = null;
  currentChat = null;
  selectedAiMessageId = null;
  setMobileChatOpen(false);
  $('chatPanel')?.classList.add('hidden');
  $('emptyState')?.classList.remove('hidden');
  if ($('emptyState')) {
    $('emptyState').textContent = chatScope === 'archive' ? 'Архив закрытых чатов.' : 'Выберите чат слева. Маркетплейсы обновляются автоматически в фоне.';
  }
  loadAssignees().then(() => loadChats()).catch(err => notify('Ошибка загрузки чатов', String(err.message || err)));
}



function switchChatOwnerScope(scope) {
  chatScope = 'active';
  // Toggle behavior: if “Мои чаты” is already active, a second click returns
  // the operator to the normal active inbox.
  if (scope === 'mine' && chatOwnerScope === 'mine') {
    chatOwnerScope = 'all';
  } else {
    chatOwnerScope = scope === 'mine' ? 'mine' : 'all';
  }
  currentChatId = null;
  currentChat = null;
  selectedAiMessageId = null;
  setMobileChatOpen(false);
  $('chatPanel')?.classList.add('hidden');
  $('emptyState')?.classList.remove('hidden');
  if ($('emptyState')) $('emptyState').textContent = chatOwnerScope === 'mine' ? 'Ваши назначенные чаты.' : 'Выберите чат слева.';
  loadChats().catch(err => notify('Ошибка загрузки чатов', String(err.message || err)));
}

function renderChatList() {
  const list = $('chatList');
  if (!list) return;
  list.innerHTML = '';
  if (!chats.length) {
    list.innerHTML = `<div class="chat-item empty-chat-item"><p>${chatScope === 'archive' ? 'В архиве пока нет закрытых чатов.' : 'Активных чатов пока нет. Маркетплейсы обновляются автоматически в фоне.'}</p></div>`;
    return;
  }
  const visibleChats = (chats || []).filter(chat => chatScope === 'archive' || !isClosedWorkflowStatus(chat.status, chat.status_label));
  for (const chat of visibleChats) {
    const item = document.createElement('div');
    const showWaitingMarker = shouldShowWaitingMarker(chat);
    item.className = `chat-item ${chat.id === currentChatId ? 'active' : ''} ${showWaitingMarker ? 'needs-response' : ''}`;
    item.onclick = () => openChat(chat.id);
    const slaBadge = waitingResponseBadge(chat);
    const assigneeBadge = chat.assigned_user_id ? `<span class="assignee-chip">${escapeHtml(chat.assigned_user_display_name || chat.assigned_user_username || chat.assigned_to || 'назначен')}</span>` : ''; 
    const time = formatChatTime(chat.last_message_at || chat.updated_at || chat.created_at);
    item.innerHTML = `
      <div class="chat-item-topline">
        <div class="chat-item-title">
          <strong title="${escapeHtml(customerLabel(chat))}">${escapeHtml(customerLabel(chat))}</strong>
          <span class="chat-time">${escapeHtml(time)}</span>
        </div>
        <span class="badge ${chat.marketplace}">${marketplaceNames[chat.marketplace] || chat.marketplace}</span>
      </div>
      <p class="preview">${escapeHtml(previewText(chat.last_message_preview))}</p>
      <div class="chat-item-footer">
        <div class="chat-badges">${statusBadge(chat.status, chat.status_label, chat.status_color)}${slaBadge}${assigneeBadge}</div>
      </div>
    `;
    list.appendChild(item);
  }
}

async function openChat(chatId, options = {}) {
  const requestSeq = ++openChatRequestSeq;
  mobileChatClosedByUser = false;
  const previousChatId = currentChatId;
  currentChatId = Number(chatId);
  if (previousChatId !== currentChatId) selectedAiMessageId = null;
  const messagesBox = $('messages');
  const wasNearBottom = messagesBox ? (messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 80) : true;

  // Make the tap feel instant: show the chat screen and active row before the API responds.
  $('emptyState')?.classList.add('hidden');
  $('chatPanel')?.classList.remove('hidden');
  setMobileChatOpen(true);
  renderChatList();

  if (messagesBox && previousChatId !== currentChatId) {
    messagesBox.innerHTML = '<div class="empty-card chat-loading-card">Загружаю диалог…</div>';
  }

  let chat;
  chatOpenInFlight = true;
  try {
    chat = await api(`/api/chats/${chatId}?messages_limit=120`);
  } catch (err) {
    if (requestSeq === openChatRequestSeq) {
      notify('Не удалось открыть чат', String(err.message || err));
      setMobileChatOpen(false);
    }
    chatOpenInFlight = false;
    return;
  }
  chatOpenInFlight = false;

  // Ignore stale responses when the operator taps another chat quickly or returns to the list.
  if (requestSeq !== openChatRequestSeq || Number(currentChatId) !== Number(chatId)) {
    return;
  }

  currentChat = chat;
  mergeChatSummary(chat);
  if (selectedAiMessageId && !(chat.messages || []).some(m => Number(m.id) === Number(selectedAiMessageId))) {
    selectedAiMessageId = null;
  }

  if ($('chatTitle')) $('chatTitle').textContent = `${customerLabel(chat)}`;
  const chatMarketplaceBadge = $('chatMarketplaceBadge');
  if (chatMarketplaceBadge) {
    const mp = String(chat.marketplace || '').toLowerCase();
    chatMarketplaceBadge.textContent = marketplaceNames[chat.marketplace] || chat.marketplace || '';
    chatMarketplaceBadge.className = `marketplace-pill ${mp}`;
    if (chatMarketplaceBadge.textContent) {
      chatMarketplaceBadge.classList.remove('hidden');
    } else {
      chatMarketplaceBadge.classList.add('hidden');
    }
  }
  if ($('chatAvatar')) $('chatAvatar').textContent = customerLabel(chat).trim().slice(0, 1).toUpperCase() || 'A';
  if ($('chatSubtitle')) {
    const subtitle = chatSubtitleParts(chat);
    $('chatSubtitle').textContent = subtitle || 'Данные по заказу не переданы маркетплейсом';
  }
  renderChatSettingsControls({ keepValues: true });
  if ($('chatStatus')) $('chatStatus').value = chat.status;
  if ($('assignedUserSelect')) { $('assignedUserSelect').innerHTML = assigneeOptions(chat.assigned_user_id || '', true); $('assignedUserSelect').value = chat.assigned_user_id || ''; }
  if ($('customerNameInput')) $('customerNameInput').value = chat.customer_name || '';

  renderMessages(chat.messages || []);
  renderTasks(chat.tasks || []);
  renderAiSelectionBar();
  renderChatList();

  const shouldPreserveScroll = options.keepScroll && messagesBox && !wasNearBottom && previousChatId === Number(chatId);
  if (shouldPreserveScroll) {
    return;
  }
  scrollMessagesToBottom('open-chat');
}


function ensureChatImageLazyObserver() {
  const root = $('messages') || null;
  if (!('IntersectionObserver' in window)) return null;
  if (chatImageLazyObserver && chatImageLazyObserverRoot === root) return chatImageLazyObserver;
  if (chatImageLazyObserver) chatImageLazyObserver.disconnect();
  chatImageLazyObserverRoot = root;
  chatImageLazyObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const img = entry.target;
      chatImageLazyObserver.unobserve(img);
      const src = img.dataset.src;
      if (src && img.src !== src) {
        img.src = src;
        img.classList.remove('chat-image-lazy');
      }
    }
  }, { root, rootMargin: '900px 0px', threshold: 0.01 });
  return chatImageLazyObserver;
}

function prepareLazyChatImage(img, src) {
  img.dataset.src = src;
  img.classList.add('chat-image-lazy');
  const observer = ensureChatImageLazyObserver();
  if (observer) {
    observer.observe(img);
  } else {
    img.src = src;
  }
}

function renderMessages(messages) {
  const box = $('messages');
  if (!box) return;
  box.innerHTML = '';
  for (const message of messages) {
    const item = document.createElement('div');
    item.className = `message ${message.direction} ${Number(message.id) === Number(selectedAiMessageId) ? 'ai-selected-message' : ''}`;
    item.dataset.messageId = message.id;

    const meta = document.createElement('div');
    meta.className = 'message-meta';
    const directionText = message.direction === 'inbound' ? 'клиент' : message.direction === 'outbound' ? 'мы' : 'заметка';
    meta.textContent = `${directionText}${message.author ? ` · ${message.author}` : ''}${message.created_at ? ` · ${formatDateTime(message.created_at)}` : ''}`;

    if (message.direction === 'inbound') {
      const aiWrap = document.createElement('span');
      aiWrap.className = 'message-actions-menu-wrap';

      const menuBtn = document.createElement('button');
      menuBtn.type = 'button';
      menuBtn.className = 'message-actions-btn';
      menuBtn.textContent = '⋯';
      menuBtn.title = 'Действия с сообщением';
      menuBtn.setAttribute('aria-label', 'Действия с сообщением');

      const menu = document.createElement('span');
      menu.className = 'message-actions-menu hidden';
      const aiBtn = document.createElement('button');
      aiBtn.type = 'button';
      aiBtn.textContent = 'ИИ ответ';
      aiBtn.title = 'Сгенерировать черновик ответа на это сообщение';
      aiBtn.onclick = async (event) => {
        event.stopPropagation();
        selectedAiMessageId = Number(message.id);
        menu.classList.add('hidden');
        renderMessages(currentChat?.messages || []);
        renderAiSelectionBar();
        await generateAiReplyForSelected();
      };
      menu.appendChild(aiBtn);

      menuBtn.onclick = (event) => {
        event.stopPropagation();
        document.querySelectorAll('.message-actions-menu').forEach(el => {
          if (el !== menu) el.classList.add('hidden');
        });
        menu.classList.toggle('hidden');
      };

      aiWrap.appendChild(menuBtn);
      aiWrap.appendChild(menu);
      meta.appendChild(aiWrap);
    }

    const images = extractImageUrls(message);
    const displayText = cleanMessageTextForDisplay(message.text || '', images);
    item.appendChild(meta);

    if (displayText) {
      const text = document.createElement('div');
      text.className = 'message-text';
      renderTextWithLinks(text, displayText);
      item.appendChild(text);
    }

    if (images.length) {
      const gallery = document.createElement('div');
      gallery.className = 'message-images';
      for (const url of images) {
        const card = document.createElement('a');
        const safeImageUrl = imagePreviewSrc(url);
        card.href = safeImageUrl;
        card.target = '_blank';
        card.rel = 'noreferrer';
        card.className = 'image-card';
        card.title = 'Открыть изображение в полном размере';

        const img = document.createElement('img');
        img.alt = 'Изображение из сообщения';
        img.loading = 'lazy';
        img.decoding = 'async';
        img.width = 640;
        img.height = 480;
        img.referrerPolicy = 'no-referrer';
        img.onerror = () => card.classList.add('image-error');
        prepareLazyChatImage(img, safeImageUrl);

        const fallback = document.createElement('span');
        fallback.textContent = 'Открыть в полном размере';
        fallback.className = 'image-fallback';

        card.appendChild(img);
        card.appendChild(fallback);
        gallery.appendChild(card);
      }
      item.appendChild(gallery);
    }

    const receipt = messageReceiptInfo(message);
    if (receipt) {
      const receiptEl = document.createElement('div');
      receiptEl.className = `message-receipt ${receipt.read ? 'is-read' : 'is-sent'}`;
      receiptEl.title = receipt.title || receipt.label;
      receiptEl.innerHTML = `<span class="receipt-checks">${escapeHtml(receipt.icon)}</span><span>${escapeHtml(receipt.label)}</span>`;
      item.appendChild(receiptEl);
    }

    box.appendChild(item);
  }
}

function cleanMessageTextForDisplay(value, imageUrls = []) {
  let text = String(value || '');

  // Ozon sometimes sends attachment placeholders such as ![](изображение)
  // together with a real image object/link. The placeholder is not useful for an operator.
  text = text.replace(/!\[[^\]]*\]\([^)]+\)/g, '').trim();

  if (imageUrls.length) {
    // If an image URL is rendered as a preview below the message, remove the raw URL from text.
    text = text.replace(/(?:https?:\/\/|\/api\/chat-uploads\/)[^\s<>"]+/g, (rawUrl) => {
      const clean = rawUrl.replace(/[),.;]+$/, '');
      const trailing = rawUrl.slice(clean.length);
      return imageUrls.includes(clean) || isLikelyImageUrl(clean, 'text') ? trailing : rawUrl;
    });
  }

  return text.replace(/\n{3,}/g, '\n\n').trim();
}

function messageReceiptInfo(message) {
  if (!message || message.direction === 'internal') return null;
  if (message.raw && message.raw._crm_marketplace_attachment_sent) {
    return { icon: '✓✓', label: 'отправлено', read: false, title: 'Изображение отправлено в маркетплейс и сохранено в CRM' };
  }
  if (message.raw && message.raw._crm_local_attachment) {
    return { icon: '✓', label: 'локально в CRM', read: false, title: 'Изображение сохранено только в CRM-чате' };
  }

  if (message.direction === 'outbound') {
    const state = outboundReceiptState(message);
    if (state === 'read') {
      return { icon: '✓✓', label: 'прочитано', read: true, title: 'Маркетплейс вернул признак прочтения клиентом' };
    }
    if (state === 'delivered') {
      return { icon: '✓✓', label: 'доставлено', read: false, title: 'Маркетплейс подтвердил доставку сообщения' };
    }
    return { icon: '✓', label: 'отправлено', read: false, title: 'Сообщение отправлено; подтверждение прочтения пока не получено' };
  }

  return { icon: '✓', label: 'получено', read: false, title: 'Сообщение получено в CRM' };
}

function normalizeRawKey(key) {
  return String(key || '').toLowerCase().replace(/[_\-\s]/g, '');
}

function valueLooksTruthy(value) {
  if (value === true) return true;
  if (typeof value === 'number') return value > 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    return !!normalized && !['false', '0', 'no', 'null', 'none', 'undefined', 'нет', 'не прочитано'].includes(normalized);
  }
  return false;
}

function statusLooks(value, positiveWords) {
  const normalized = String(value || '').trim().toLowerCase().replace(/[_\-\s]/g, '');
  if (!normalized) return false;
  return positiveWords.some(word => normalized.includes(word));
}

function scanReceiptSignal(value, type, depth = 0) {
  if (!value || depth > 8) return false;
  if (Array.isArray(value)) return value.some(v => scanReceiptSignal(v, type, depth + 1));
  if (typeof value !== 'object') return false;

  const readKeys = new Set([
    'read', 'isread', 'wasread', 'readbycustomer', 'customerread', 'clientread',
    'seen', 'isseen', 'viewed', 'isviewed', 'opened', 'isopened',
    'readat', 'seenat', 'viewedat', 'openedat', 'readtime', 'readtimestamp',
    'readbycustomerat', 'customerreadat', 'clientreadat',
  ]);
  const deliveredKeys = new Set([
    'delivered', 'isdelivered', 'delivery', 'deliveryconfirmed',
    'deliveredat', 'deliveryat', 'sentat', 'sendat',
  ]);
  const statusKeys = new Set([
    'status', 'state', 'messagestatus', 'deliverystatus', 'deliverystate',
    'readstatus', 'receiptstatus', 'messageState', 'messageStatus'.toLowerCase(),
  ].map(normalizeRawKey));

  const readStatuses = ['read', 'seen', 'viewed', 'opened', 'прочитан'];
  const deliveredStatuses = ['delivered', 'sent', 'sended', 'success', 'ok', 'доставлен', 'отправлен'];

  for (const [key, nested] of Object.entries(value)) {
    const k = normalizeRawKey(key);

    if (type === 'read' && readKeys.has(k) && valueLooksTruthy(nested)) return true;
    if (type === 'delivered' && deliveredKeys.has(k) && valueLooksTruthy(nested)) return true;

    if (statusKeys.has(k) && (typeof nested === 'string' || typeof nested === 'number' || typeof nested === 'boolean')) {
      if (type === 'read' && statusLooks(nested, readStatuses)) return true;
      if (type === 'delivered' && statusLooks(nested, deliveredStatuses)) return true;
    }

    if (scanReceiptSignal(nested, type, depth + 1)) return true;
  }
  return false;
}

function outboundReceiptState(message) {
  const raw = message.raw || {};
  const status = String(message.delivery_status || message.status || '').toLowerCase();
  if (message.read_at || message.customer_read_at || statusLooks(status, ['read', 'seen', 'viewed', 'opened', 'прочитан'])) return 'read';
  if (message.delivered_at || statusLooks(status, ['delivered', 'sent', 'sended', 'success', 'ok', 'доставлен', 'отправлен'])) return 'delivered';

  // Only outbound messages are scanned. This prevents inbound/customer statuses
  // from being interpreted as read receipts for seller replies.
  if (scanReceiptSignal(raw, 'read')) return 'read';
  if (scanReceiptSignal(raw, 'delivered')) return 'delivered';

  // A marketplace message ID means the marketplace accepted the message even if
  // it did not expose a separate delivery/read flag.
  if (message.external_message_id || raw.message_id || raw.messageId || raw.id) return 'sent';
  return 'sent';
}

function hasRawFlag(value, keys, depth = 0) {
  if (!value || depth > 7) return false;
  if (Array.isArray(value)) return value.some(v => hasRawFlag(v, keys, depth + 1));
  if (typeof value !== 'object') return false;
  const normalizedKeys = new Set(keys.map(k => normalizeRawKey(k)));
  for (const [key, nested] of Object.entries(value)) {
    const k = normalizeRawKey(key);
    if (normalizedKeys.has(k) && valueLooksTruthy(nested)) return true;
    if (hasRawFlag(nested, keys, depth + 1)) return true;
  }
  return false;
}

function findRawStatusValue(value, depth = 0) {
  if (!value || depth > 6) return '';
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findRawStatusValue(item, depth + 1);
      if (found) return found;
    }
    return '';
  }
  if (typeof value !== 'object') return '';
  for (const [key, nested] of Object.entries(value)) {
    const k = normalizeRawKey(key);
    if (['status', 'state', 'messagestatus', 'deliverystatus', 'deliverystate', 'readstatus', 'receiptstatus'].includes(k)) {
      if (typeof nested === 'string' || typeof nested === 'number' || typeof nested === 'boolean') return String(nested);
    }
    const found = findRawStatusValue(nested, depth + 1);
    if (found) return found;
  }
  return '';
}

function renderTextWithLinks(container, value) {
  const text = String(value || '');
  const urlRegex = /(https?:\/\/[^\s<>"]+)/g;
  let lastIndex = 0;
  let match;
  while ((match = urlRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      container.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    const url = match[0].replace(/[),.;]+$/, '');
    const trailing = match[0].slice(url.length);
    const a = document.createElement('a');
    const imageLike = isLikelyImageUrl(url);
    a.href = imageLike ? imagePreviewSrc(url) : url;
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = imageLike ? 'открыть изображение' : url;
    container.appendChild(a);
    if (trailing) container.appendChild(document.createTextNode(trailing));
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    container.appendChild(document.createTextNode(text.slice(lastIndex)));
  }
}

function extractImageUrls(message) {
  const found = new Set();
  const textUrls = String(message.text || '').match(/(?:https?:\/\/|\/api\/chat-uploads\/)[^\s<>"]+/g) || [];
  for (const url of textUrls) {
    const clean = url.replace(/[),.;]+$/, '');
    if (isLikelyImageUrl(clean, 'text')) found.add(clean);
  }
  scanForImages(message.raw || {}, '', found);
  return Array.from(found).slice(0, 16);
}

function scanForImages(value, keyHint, found) {
  if (!value) return;
  if (typeof value === 'string') {
    const urls = value.match(/https?:\/\/[^\s<>"]+/g) || [];
    for (const rawUrl of urls) {
      const url = rawUrl.replace(/[),.;]+$/, '');
      if (isLikelyImageUrl(url, keyHint)) found.add(url);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(v => scanForImages(v, keyHint, found));
    return;
  }
  if (typeof value === 'object') {
    for (const [key, nested] of Object.entries(value)) {
      const hint = `${keyHint} ${key}`.toLowerCase();
      scanForImages(nested, hint, found);
    }
  }
}

function isLikelyImageUrl(url, keyHint = '') {
  if (/^\/api\/chat-uploads\//i.test(url)) return true;
  if (!/^https:\/\//i.test(url)) return false;
  const lower = url.toLowerCase();
  const hint = String(keyHint || '').toLowerCase();
  if (/\.(jpg|jpeg|png|webp|gif|bmp|svg)(\?|#|$)/i.test(lower)) return true;
  if (hint.includes('image') || hint.includes('photo') || hint.includes('picture') || hint.includes('preview') || hint.includes('thumbnail') || hint.includes('file') || hint.includes('attachment')) return true;
  if (lower.includes('image') || lower.includes('photo') || lower.includes('cdn') || lower.includes('ozon')) return true;
  return false;
}

function imagePreviewSrc(url) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    // Marketplace links often need backend headers or fail because of browser/CORS/referrer rules.
    if (host.includes('ozon') || host.includes('ozone') || host.includes('o3') || host.includes('cdn')) {
      return `/api/assets/image?url=${encodeURIComponent(url)}`;
    }
  } catch (err) {
    return url;
  }
  return url;
}




function renderComposerAttachments() {
  const box = $('composerAttachmentPreview');
  if (!box) return;
  if (!selectedChatImageFiles.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = selectedChatImageFiles.map((file, index) => `
    <span class="composer-attachment-chip" title="${escapeHtml(file.name || 'Изображение')}">
      <span class="composer-attachment-icon">🖼</span>
      <span class="composer-attachment-name">${escapeHtml(file.name || 'Изображение')}</span>
      <button type="button" data-remove-chat-image="${index}" aria-label="Удалить изображение">×</button>
    </span>
  `).join('');
  box.querySelectorAll('[data-remove-chat-image]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = Number(btn.dataset.removeChatImage);
      selectedChatImageFiles = selectedChatImageFiles.filter((_, i) => i !== idx);
      const input = $('chatImageInput');
      if (input && !selectedChatImageFiles.length) input.value = '';
      renderComposerAttachments();
    });
  });
}

function clearComposerAttachments() {
  selectedChatImageFiles = [];
  const input = $('chatImageInput');
  if (input) input.value = '';
  renderComposerAttachments();
}

function handleChatImageSelection(event) {
  const files = Array.from(event.target?.files || []).filter((file) => file.type && file.type.startsWith('image/'));
  if (!files.length) {
    selectedChatImageFiles = [];
    renderComposerAttachments();
    return;
  }
  selectedChatImageFiles = files.slice(0, 5);
  if (files.length > 5) notify('Изображения', 'Можно прикрепить до 5 изображений за раз.');
  renderComposerAttachments();
}

function closeMessageActionsMenus() {
  document.querySelectorAll('.message-actions-menu').forEach(el => el.classList.add('hidden'));
}

function getSelectedAiMessage() {
  if (!selectedAiMessageId || !currentChat) return null;
  return (currentChat.messages || []).find(m => Number(m.id) === Number(selectedAiMessageId)) || null;
}

function renderAiSelectionBar() {
  const bar = $('aiSelectionBar');
  const snippet = $('aiSelectedSnippet');
  const notice = $('aiNotice');
  if (!bar || !snippet) return;
  const message = getSelectedAiMessage();
  if (!message) {
    bar.classList.add('hidden');
    snippet.textContent = '';
    if (notice) notice.textContent = '';
    return;
  }
  const text = (message.text || '[вложение/сообщение без текста]').replace(/\s+/g, ' ').trim();
  snippet.textContent = `ИИ ответит на: ${text.slice(0, 180)}${text.length > 180 ? '…' : ''}`;
  bar.classList.remove('hidden');
}

function clearAiSelection() {
  selectedAiMessageId = null;
  renderMessages(currentChat?.messages || []);
  renderAiSelectionBar();
}

async function generateAiReplyForSelected() {
  if (!currentChatId) return;
  const selected = getSelectedAiMessage();
  if (!selected) {
    notify('Выберите сообщение', 'Откройте ⋯ на входящем сообщении и выберите «ИИ ответ».');
    return;
  }
  if (aiGenerating) return;
  aiGenerating = true;
  const buttons = [$('aiGenerateBtn'), $('aiGenerateMenuBtn')].filter(Boolean);
  buttons.forEach(btn => { btn.disabled = true; });
  const notice = $('aiNotice');
  if (notice) notice.textContent = 'Генерирую черновик ответа…';
  setStatus('ИИ генерирует ответ…');
  try {
    const result = await api(`/api/chats/${currentChatId}/ai-reply`, {
      method: 'POST',
      body: JSON.stringify({ message_id: Number(selectedAiMessageId) }),
    });
    const textarea = $('messageText');
    if (textarea) {
      textarea.value = result.draft || '';
      autosizeComposerTextarea(textarea);
      textarea.focus();
      textarea.classList.add('ai-filled');
      setTimeout(() => textarea.classList.remove('ai-filled'), 1600);
    }
    if (notice) notice.textContent = 'Черновик вставлен в поле ответа. Проверьте и отправьте вручную.';
    setStatus('ИИ ответ готов — проверьте перед отправкой');
  } catch (err) {
    const message = String(err.message || err);
    if (notice) notice.textContent = 'Ошибка генерации. Проверьте OpenAI-ключ/модель или откройте /api/debug/openai.';
    notify('Не удалось сгенерировать ИИ ответ', `${message}\n\nДля проверки откройте в браузере: http://127.0.0.1:8000/api/debug/openai`);
  } finally {
    aiGenerating = false;
    buttons.forEach(btn => { btn.disabled = false; });
  }
}


function reviewStatusBadge(status) {
  const label = reviewStatusNames[status] || status || '—';
  const safe = String(status || 'unknown').toLowerCase();
  return `<span class="review-status review-status-${escapeHtml(safe)}">${escapeHtml(label)}</span>`;
}

function reviewRatingStars(rating) {
  const n = Number(rating || 0);
  if (!n) return '—';
  return '★'.repeat(Math.max(1, Math.min(5, n)));
}

async function loadReviews() {
  const params = new URLSearchParams();
  const status = $('reviewStatusFilter')?.value || '';
  const unanswered = $('reviewUnansweredFilter')?.checked;
  if (status) params.set('status', status);
  if (unanswered) params.set('unanswered', 'true');
  reviews = await api(`/api/reviews?${params.toString()}`);
  renderReviewsList();
}

function renderReviewsList() {
  const box = $('reviewsList');
  if (!box) return;
  box.innerHTML = '';
  if (!reviews.length) {
    box.innerHTML = '<div class="empty-card">Отзывы пока не загружены. Нажмите «Обновить» или дождитесь фоновой синхронизации.</div>';
    return;
  }
  for (const review of reviews) {
    const item = document.createElement('article');
    item.className = `review-item ${Number(review.id) === Number(currentReviewId) ? 'active' : ''}`;
    item.onclick = () => openReview(review.id);
    const title = reviewModelName(review);
    const time = formatChatTime(review.published_at || review.updated_at || review.created_at);
    const replyBadge = review.reply_text ? '<span class="review-status review-status-processed">есть ответ</span>' : '<span class="sla-badge">нужен ответ</span>';
    item.innerHTML = `
      <div class="review-item-top">
        <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
        <span class="chat-time">${escapeHtml(time)}</span>
      </div>
      <div class="review-stars">${escapeHtml(reviewRatingStars(review.rating))}</div>
      <p>${escapeHtml(review.text || 'Отзыв без текста')}</p>
      <div class="chat-badges">${reviewStatusBadge(review.status)}${replyBadge}</div>
    `;
    box.appendChild(item);
  }
}

async function openReview(reviewId, options = {}) {
  currentReviewId = Number(reviewId);
  const review = await api(`/api/reviews/${reviewId}`);
  currentReview = review;
  $('reviewEmptyState')?.classList.add('hidden');
  $('reviewPanel')?.classList.remove('hidden');
  if ($('reviewTitle')) $('reviewTitle').textContent = reviewModelName(review);
  if ($('reviewSubtitle')) $('reviewSubtitle').textContent = `Ozon · ${formatDateTime(review.published_at) || 'дата не указана'}`;
  if ($('reviewRating')) $('reviewRating').textContent = `${review.rating || '—'} / 5`;
  if ($('reviewText')) $('reviewText').textContent = review.text || 'Отзыв без текста';
  const facts = $('reviewFacts');
  if (facts) {
    facts.innerHTML = `
      <div class="review-fact"><span>Модель</span><strong title="${escapeHtml(reviewModelName(review))}">${escapeHtml(reviewModelName(review))}</strong></div>
      <div class="review-fact"><span>Количество звёзд</span><strong>${escapeHtml(String(review.rating || '—'))} из 5</strong></div>
      <div class="review-fact"><span>Статус</span><strong>${escapeHtml(reviewStatusNames[review.status] || review.status || '—')}</strong></div>
    `;
  }
  const media = $('reviewMedia');
  if (media) {
    media.innerHTML = '';
    for (const url of (review.media || [])) {
      const a = document.createElement('a');
      a.href = url;
      a.target = '_blank';
      a.rel = 'noreferrer';
      a.className = 'image-card';
      const img = document.createElement('img');
      img.src = imagePreviewSrc(url);
      img.alt = 'Фото из отзыва';
      img.loading = 'lazy';
      a.appendChild(img);
      const span = document.createElement('span');
      span.className = 'image-fallback';
      span.textContent = 'Открыть фото';
      a.appendChild(span);
      media.appendChild(a);
    }
  }
  const existing = $('reviewExistingReply');
  if (existing) {
    if (review.reply_text) {
      existing.textContent = review.reply_text;
      existing.classList.remove('hidden');
    } else {
      existing.textContent = '';
      existing.classList.add('hidden');
    }
  }
  if ($('reviewReplyText')) $('reviewReplyText').value = '';
  renderReviewsList();
}

async function syncReviews() {
  const btn = $('syncReviewsBtn');
  if (btn) btn.disabled = true;
  setStatus('Загружаю отзывы Ozon…');
  try {
    const result = await api('/api/reviews/sync/ozon', { method: 'POST' });
    setStatus(`Отзывы Ozon: загружено ${result.count || 0}`);
    await loadReviews();
    await loadStats();
  } catch (err) {
    notify('Отзывы не загрузились', String(err.message || err));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function submitReviewReply(event) {
  event.preventDefault();
  if (!currentReviewId) return;
  const text = $('reviewReplyText')?.value.trim();
  if (!text) return;
  const form = $('reviewReplyForm');
  const btn = form?.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/reviews/${currentReviewId}/reply`, {
      method: 'POST',
      body: JSON.stringify({ text, mark_processed: Boolean($('reviewMarkProcessed')?.checked) }),
    });
    await loadReviews();
    await openReview(currentReviewId);
    await loadStats();
    setStatus('Ответ на отзыв отправлен');
  } catch (err) {
    notify('Ответ на отзыв не отправлен', String(err.message || err));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function startChatFromReview() {
  if (!currentReviewId) return;
  const btn = $('startChatFromReviewBtn');
  if (btn) btn.disabled = true;
  try {
    const result = await api(`/api/reviews/${currentReviewId}/start-chat`, { method: 'POST' });
    if (result.local_chat_id) {
      showView('chats');
      await loadChats();
      await openChat(result.local_chat_id);
    } else {
      notify('Чат создан', result);
    }
  } catch (err) {
    notify('Не удалось создать чат из отзыва', String(err.message || err));
  } finally {
    if (btn) btn.disabled = false;
  }
}

function saveCurrentQuestionDraft() {
  const field = $('questionAnswerText');
  if (!field || !currentQuestionId) return;
  questionAnswerDrafts[String(currentQuestionId)] = field.value || '';
}

function restoreQuestionDraft(questionId, options = {}) {
  const field = $('questionAnswerText');
  if (!field) return;
  const key = String(questionId);
  if (options.clearDraft) {
    delete questionAnswerDrafts[key];
    field.value = '';
    return;
  }
  if (options.silent && document.activeElement === field) {
    questionAnswerDrafts[key] = field.value || '';
    return;
  }
  field.value = questionAnswerDrafts[key] || '';
}

function questionStatusBadge(status) {
  const label = questionStatusNames[status] || status || '—';
  const safe = String(status || 'unknown').toLowerCase();
  return `<span class="review-status review-status-${escapeHtml(safe)}">${escapeHtml(label)}</span>`;
}

function normalizeQuestionStatus(value) {
  return String(value || '').trim().toLowerCase().replaceAll('ё', 'е').replace(/\s+/g, ' ');
}

function isQuestionProcessed(question) {
  if (question && typeof question.is_processed === 'boolean') return question.is_processed;
  const values = [
    question?.status,
    question?.status_label,
    questionStatusNames[question?.status],
    question?.raw?.status,
    question?.raw?.state,
    question?.raw?.question_status,
    question?.raw?.questionStatus,
    question?.raw?.question?.status,
    question?.raw?.question?.state,
  ];
  return values.some((value) => {
    const token = normalizeQuestionStatus(value);
    return ['processed', 'answered', 'done', 'closed', 'resolved', 'обработан', 'обработано', 'ответ дан', 'есть ответ', 'отвечен', 'отвечено'].includes(token);
  });
}

function questionNeedsAnswer(question) {
  if (!question) return false;
  if (typeof question.needs_answer === 'boolean') return question.needs_answer;
  if (String(question.answer_text || '').trim()) return false;
  return !isQuestionProcessed(question);
}

function questionAnswerBadge(question) {
  if (String(question?.answer_text || '').trim()) {
    return '<span class="review-status review-status-processed">есть ответ</span>';
  }
  if (questionNeedsAnswer(question)) {
    return '<span class="sla-badge">нужен ответ</span>';
  }
  return '';
}

function questionProductName(question) {
  return question.product_name || (question.sku ? `SKU ${question.sku}` : 'Товар не указан');
}

async function loadQuestions() {
  const params = new URLSearchParams();
  const status = $('questionStatusFilter')?.value || '';
  const unanswered = $('questionUnansweredFilter')?.checked;
  if (status) params.set('status', status);
  if (unanswered) params.set('unanswered', 'true');
  questions = await api(`/api/questions?${params.toString()}`);
  renderQuestionsList();
}

function renderQuestionsList() {
  const box = $('questionsList');
  if (!box) return;
  box.innerHTML = '';
  if (!questions.length) {
    box.innerHTML = '<div class="empty-card">Вопросы пока не загружены. Нажмите «Обновить» или дождитесь фоновой синхронизации.</div>';
    return;
  }
  for (const question of questions) {
    const item = document.createElement('article');
    item.className = `review-item ${Number(question.id) === Number(currentQuestionId) ? 'active' : ''}`;
    item.onclick = () => openQuestion(question.id);
    const title = questionProductName(question);
    const time = formatChatTime(question.published_at || question.updated_at || question.created_at);
    const answerBadge = questionAnswerBadge(question);
    item.innerHTML = `
      <div class="review-item-top">
        <strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>
        <span class="chat-time">${escapeHtml(time)}</span>
      </div>
      <p>${escapeHtml(question.text || 'Вопрос без текста')}</p>
      <div class="chat-badges">${questionStatusBadge(question.status)}${answerBadge}</div>
    `;
    box.appendChild(item);
  }
}

async function openQuestion(questionId, options = {}) {
  saveCurrentQuestionDraft();
  currentQuestionId = Number(questionId);
  const question = await api(`/api/questions/${questionId}`);
  currentQuestion = question;
  $('questionEmptyState')?.classList.add('hidden');
  $('questionPanel')?.classList.remove('hidden');
  if ($('questionTitle')) $('questionTitle').textContent = questionProductName(question);
  if ($('questionSubtitle')) $('questionSubtitle').textContent = `Ozon · ${formatDateTime(question.published_at) || 'дата не указана'}`;
  if ($('questionStatusBadge')) $('questionStatusBadge').innerHTML = questionStatusBadge(question.status);
  if ($('questionText')) $('questionText').textContent = question.text || 'Вопрос без текста';
  const facts = $('questionFacts');
  if (facts) {
    facts.innerHTML = `
      <div class="review-fact"><span>Товар</span><strong title="${escapeHtml(questionProductName(question))}">${escapeHtml(questionProductName(question))}</strong></div>
      <div class="review-fact"><span>SKU</span><strong>${escapeHtml(question.sku || '—')}</strong></div>
      <div class="review-fact"><span>Статус</span><strong>${escapeHtml(questionStatusNames[question.status] || question.status || '—')}</strong></div>
      <div class="review-fact"><span>Автор</span><strong>${escapeHtml(question.author_name || 'покупатель')}</strong></div>
    `;
  }
  const productLink = $('questionProductLink');
  if (productLink) {
    if (question.product_url) {
      productLink.href = question.product_url;
      productLink.classList.remove('hidden');
    } else {
      productLink.removeAttribute('href');
      productLink.classList.add('hidden');
    }
  }
  const existing = $('questionExistingAnswer');
  if (existing) {
    if (question.answer_text) {
      existing.textContent = question.answer_text;
      existing.classList.remove('hidden');
    } else {
      existing.textContent = '';
      existing.classList.add('hidden');
    }
  }
  restoreQuestionDraft(questionId, options);
  renderQuestionsList();
}

async function syncQuestions() {
  const btn = $('syncQuestionsBtn');
  if (btn) btn.disabled = true;
  try {
    await runFrontendOzonQuestionsSync({ silent: false, force: true });
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function submitQuestionAnswer(event) {
  event.preventDefault();
  if (!currentQuestionId) return;
  const text = $('questionAnswerText')?.value.trim();
  if (!text) return;
  const form = $('questionAnswerForm');
  const btn = form?.querySelector('button[type="submit"]');
  if (btn) btn.disabled = true;
  try {
    await api(`/api/questions/${currentQuestionId}/answer`, {
      method: 'POST',
      body: JSON.stringify({ text, mark_processed: Boolean($('questionMarkProcessed')?.checked) }),
    });
    delete questionAnswerDrafts[String(currentQuestionId)];
    await loadQuestions();
    await openQuestion(currentQuestionId, { clearDraft: true });
    await loadStats();
    setStatus('Ответ на вопрос отправлен');
  } catch (err) {
    notify('Ответ на вопрос не отправлен', String(err.message || err));
  } finally {
    if (btn) btn.disabled = false;
  }
}


function renderTasks(tasks) {
  const box = $('taskList');
  if (!box) return;
  box.innerHTML = '';
  if (!currentChatId) {
    box.innerHTML = '<p class="muted">Выберите чат.</p>';
    return;
  }
  const visibleTasks = (tasks || []).filter(t => !['done', 'archived', 'cancelled'].includes(t.status));
  if (!visibleTasks.length) {
    box.innerHTML = '<p class="muted">Активных задач по этому чату пока нет.</p>';
    return;
  }
  for (const task of visibleTasks) {
    const item = document.createElement('div');
    item.className = `task-card task-${task.status}`;
    item.dataset.taskCard = '1';
    item.innerHTML = `
      <div class="task-card-head">
        <strong>${escapeHtml(task.title)}</strong>
        <span class="task-status task-status-${escapeHtml(task.status)}">${escapeHtml(taskStatusNames[task.status] || task.status)}</span>
      </div>
      ${task.description ? `<p class="task-description">${escapeHtml(task.description)}</p>` : ''}
      <div class="task-edit-grid">
        <label>Статус<select data-task-status>${taskStatusOptions(task.status)}</select></label>
        <label>Исполнитель<select data-task-assignee>${assigneeOptions(task.assigned_user_id || '', true)}</select></label>
        <label>Срок<input data-task-due type="datetime-local" value="${escapeHtml(datetimeLocalValue(task.due_at))}" /></label>
      </div>
      <div class="task-meta-line">Создана: ${escapeHtml(formatDateTime(task.created_at) || '—')}${task.completed_at ? ` · выполнена: ${escapeHtml(formatDateTime(task.completed_at))}` : ''}</div>
      <div class="task-comments">${renderTaskComments(task)}</div>
      <textarea data-task-comment class="task-comment-input" placeholder="Добавить комментарий к задаче"></textarea>
      <div class="task-actions">
        <button class="small" data-task-card-button data-task-id="${task.id}" data-task-action="save">Сохранить</button>
        <button class="small secondary" data-task-id="${task.id}" data-task-action="start">В работу</button>
        <button class="small secondary" data-task-id="${task.id}" data-task-action="done">Выполнено</button>
        <button class="small ghost" data-task-id="${task.id}" data-task-action="archive">В архив</button>
      </div>
    `;
    bindTaskCardActions(item);
    box.appendChild(item);
  }
}

async function loadAllTasks() {
  const params = new URLSearchParams();
  const status = $('taskStatusFilter')?.value || '';
  const bucket = $('taskBucketFilter')?.value || 'active';
  if (status) params.set('status', status);
  else if (bucket === 'mine') { params.set('bucket', 'active'); params.set('mine', 'true'); }
  else if (bucket && bucket !== 'all') params.set('bucket', bucket);
  const tasks = await api(`/api/tasks?${params.toString()}`);
  renderAllTasks(tasks);
}

function renderAllTasks(tasks) {
  const box = $('allTasksList');
  if (!box) return;
  box.innerHTML = '';
  if (!tasks.length) {
    box.innerHTML = '<div class="empty-card">Задач пока нет.</div>';
    return;
  }
  for (const task of tasks) {
    const item = document.createElement('article');
    item.className = `all-task-card task-${task.status}`;
    item.dataset.taskCard = '1';
    item.innerHTML = `
      <div class="task-main-block">
        <div class="task-card-head">
          <strong>${escapeHtml(task.title)}</strong>
          <span class="task-status task-status-${escapeHtml(task.status)}">${escapeHtml(taskStatusNames[task.status] || task.status)}</span>
        </div>
        ${task.description ? `<p class="task-description">${escapeHtml(task.description)}</p>` : ''}
        <p class="task-context">${escapeHtml(customerLabel(task))} · ${escapeHtml(marketplaceNames[task.marketplace] || task.marketplace)}${assigneeNameFromTask(task) ? ' · исполнитель: ' + escapeHtml(assigneeNameFromTask(task)) : ''}</p>
        <div class="task-edit-grid compact">
          <label>Статус<select data-task-status>${taskStatusOptions(task.status)}</select></label>
          <label>Исполнитель<select data-task-assignee>${assigneeOptions(task.assigned_user_id || '', true)}</select></label>
          <label>Срок<input data-task-due type="datetime-local" value="${escapeHtml(datetimeLocalValue(task.due_at))}" /></label>
        </div>
        <div class="task-meta-line">Создана: ${escapeHtml(formatDateTime(task.created_at) || '—')}${task.completed_at ? ` · выполнена: ${escapeHtml(formatDateTime(task.completed_at))}` : ''}${task.archived_at ? ` · архив: ${escapeHtml(formatDateTime(task.archived_at))}` : ''}</div>
        <div class="task-comments">${renderTaskComments(task)}</div>
        <textarea data-task-comment class="task-comment-input" placeholder="Добавить комментарий"></textarea>
      </div>
      <div class="task-actions task-actions-column">
        <button class="small" data-task-id="${task.id}" data-task-action="save">Сохранить</button>
        <button class="small secondary" data-task-id="${task.id}" data-task-action="start">В работу</button>
        <button class="small secondary" data-task-id="${task.id}" data-task-action="done">Выполнено</button>
        <button class="small ghost" data-task-id="${task.id}" data-task-action="archive">В архив</button>
        <button class="small ghost" data-open-chat="${task.chat_id}">Открыть чат</button>
      </div>
    `;
    bindTaskCardActions(item);
    const openBtn = item.querySelector('[data-open-chat]');
    if (openBtn) {
      openBtn.onclick = async () => {
        showView('chats');
        await loadChats();
        await openChat(Number(openBtn.dataset.openChat));
      };
    }
    box.appendChild(item);
  }
}


function toggleExtraMenu(force) {
  const menu = $('extraMenu');
  const btn = $('extraMenuBtn');
  if (!menu) return;
  const shouldOpen = typeof force === 'boolean' ? force : menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !shouldOpen);
  if (btn) btn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
}

function showExtraPanel(panelName) {
  activeExtraPanel = activeExtraPanel === panelName ? '' : panelName;
  const panel = $('extraPanel');
  panel?.classList.toggle('hidden', !activeExtraPanel);
  $('tasksSection')?.classList.toggle('hidden', activeExtraPanel !== 'tasks');
  $('noteSection')?.classList.toggle('hidden', activeExtraPanel !== 'note');
  $('customerSection')?.classList.toggle('hidden', activeExtraPanel !== 'customer');
  toggleExtraMenu(false);
}



function isKnowledgeMobile() {
  return !!window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
}

function syncKnowledgeLayoutState() {
  const view = $('knowledgeView');
  if (!view) return;
  const mode = knowledgeMode || 'empty';
  const panel = isKnowledgeMobile() ? (mode === 'edit' ? 'editor' : 'library') : 'split';
  view.dataset.kbMode = mode;
  view.dataset.kbPanel = panel;
  view.dataset.kbModalOpen = isKnowledgeMobile() && mode === 'read' ? '1' : '0';
}

function closeKnowledgeModal() {
  const modal = $('knowledgeModal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
}

function openKnowledgeModal(article) {
  const modal = $('knowledgeModal');
  if (!modal) return;
  if ($('knowledgeModalCategory')) $('knowledgeModalCategory').textContent = `${article.category_title || 'Без раздела'} · обновлено ${formatDateTime(article.updated_at) || ''}`;
  if ($('knowledgeModalTitle')) $('knowledgeModalTitle').textContent = article.title || 'Без названия';
  if ($('knowledgeModalTags')) $('knowledgeModalTags').innerHTML = knowledgeTagsHtml(article.tags) || '<span>без тегов</span>';
  const img = $('knowledgeModalImage');
  if (img) {
    if (article.image_url) {
      img.src = article.image_url;
      img.classList.remove('hidden');
    } else {
      img.removeAttribute('src');
      img.classList.add('hidden');
    }
  }
  if ($('knowledgeModalContent')) {
    const content = article.content || 'Текст статьи пока пустой.';
    $('knowledgeModalContent').innerHTML = escapeHtml(content).replace(/\n/g, '<br>');
  }
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
}

function knowledgeArticleCardHtml(a, opts = {}) {
  const active = Number(a.id) === Number(currentKnowledgeArticleId);
  const preview = (a.content || '').replace(/\s+/g, ' ').trim().slice(0, 120) || 'Без описания';
  const category = escapeHtml(a.category_title || 'Без раздела');
  const updated = escapeHtml(formatDateTime(a.updated_at) || '');
  const tags = String(a.tags || '').split(',').map(t => t.trim()).filter(Boolean).slice(0, 2);
  const tagHtml = tags.length ? `<div class="knowledge-card-tags">${tags.map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : '';
  const compact = !!opts.compact;
  return `<button class="knowledge-article-item ${active ? 'active' : ''} ${compact ? 'compact' : 'rich'}" type="button" data-kb-article="${a.id}">
    <div class="knowledge-card-top">
      <span class="knowledge-card-category">${category}</span>
      <span class="knowledge-card-date">${updated}</span>
    </div>
    <strong>${escapeHtml(a.title)}</strong>
    <p>${escapeHtml(preview)}</p>
    ${tagHtml}
  </button>`;
}

function renderKnowledgeCategories() {
  const box = $('knowledgeCategories');
  if (!box) return;
  const allArticles = knowledgeArticles || [];
  const uncategorized = allArticles.filter(a => !a.category_id);
  const allActive = !currentKnowledgeCategoryId;

  if (isKnowledgeMobile()) {
    const chips = [
      `<button class="knowledge-chip ${allActive ? 'active' : ''}" type="button" data-kb-category="">Все <span>${allArticles.length}</span></button>`,
      ...(knowledgeCategories || []).map(c => {
        const active = Number(c.id) === Number(currentKnowledgeCategoryId);
        const count = allArticles.filter(a => Number(a.category_id) === Number(c.id)).length;
        return `<button class="knowledge-chip ${active ? 'active' : ''}" type="button" data-kb-category="${c.id}">${escapeHtml(c.title)} <span>${count}</span></button>`;
      }),
      ...(uncategorized.length ? [`<button class="knowledge-chip ${currentKnowledgeCategoryId === -1 ? 'active' : ''}" type="button" data-kb-category="-1">Без раздела <span>${uncategorized.length}</span></button>`] : []),
    ];
    const activeLabel = allActive ? 'Все статьи' : currentKnowledgeCategoryId === -1 ? 'Без раздела' : escapeHtml((knowledgeCategories || []).find(c => Number(c.id) === Number(currentKnowledgeCategoryId))?.title || 'Раздел');
    box.innerHTML = `<div class="knowledge-mobile-summary">
      <div>
        <div class="knowledge-mini-head">Раздел</div>
        <strong>${activeLabel}</strong>
      </div>
      <span>${allArticles.length} статей</span>
    </div>
    <div class="knowledge-chip-row">${chips.join('')}</div>`;
  } else {
    const allBlock = `<div class="knowledge-section ${allActive ? 'active' : ''}">
      <button class="knowledge-category-item ${allActive ? 'active' : ''}" type="button" data-kb-category="">
        <span><strong>Все статьи</strong><small>${allArticles.length} статей</small></span>
        <span class="knowledge-chevron">${allActive ? '⌄' : '›'}</span>
      </button>
      <div class="knowledge-nested ${allActive ? '' : 'hidden'}">${allActive ? (allArticles.map(a => knowledgeArticleCardHtml(a, {compact:true})).join('') || '<div class="empty-card">Статей пока нет.</div>') : ''}</div>
    </div>`;

    const categoryBlocks = (knowledgeCategories || []).map(c => {
      const active = Number(c.id) === Number(currentKnowledgeCategoryId);
      const items = allArticles.filter(a => Number(a.category_id) === Number(c.id));
      return `<div class="knowledge-section ${active ? 'active' : ''}">
        <button class="knowledge-category-item ${active ? 'active' : ''}" type="button" data-kb-category="${c.id}">
          <span><strong>${escapeHtml(c.title)}</strong><small>${items.length} статей</small></span>
          <span class="knowledge-chevron">${active ? '⌄' : '›'}</span>
        </button>
        <div class="knowledge-nested ${active ? '' : 'hidden'}">${active ? (items.map(a => knowledgeArticleCardHtml(a, {compact:true})).join('') || '<div class="empty-card">В этом разделе пока нет статей.</div>') : ''}</div>
      </div>`;
    }).join('');

    const uncategorizedBlock = uncategorized.length && currentKnowledgeCategoryId === -1 ? `<div class="knowledge-section active">
      <button class="knowledge-category-item active" type="button" data-kb-category="-1"><span><strong>Без раздела</strong><small>${uncategorized.length} статей</small></span><span class="knowledge-chevron">⌄</span></button>
      <div class="knowledge-nested">${uncategorized.map(a => knowledgeArticleCardHtml(a, {compact:true})).join('')}</div>
    </div>` : '';

    box.innerHTML = allBlock + categoryBlocks + uncategorizedBlock;
  }

  box.querySelectorAll('[data-kb-category]').forEach(btn => {
    btn.onclick = () => {
      currentKnowledgeCategoryId = btn.dataset.kbCategory ? Number(btn.dataset.kbCategory) : null;
      currentKnowledgeArticleId = null;
      currentKnowledgeArticle = null;
      showKnowledgeEmpty();
      loadKnowledgeArticles();
    };
  });
  box.querySelectorAll('[data-kb-article]').forEach(btn => {
    btn.onclick = async (event) => {
      event.stopPropagation();
      await openKnowledgeArticle(Number(btn.dataset.kbArticle));
    };
  });
}

function renderKnowledgeArticleCategoryOptions() {
  const select = $('knowledgeArticleCategory');
  if (!select) return;
  const current = select.value || currentKnowledgeCategoryId || currentKnowledgeArticle?.category_id || '';
  select.innerHTML = '<option value="">Без раздела</option>' + (knowledgeCategories || []).map(c => `<option value="${c.id}" ${String(c.id) === String(current) ? 'selected' : ''}>${escapeHtml(c.title)}</option>`).join('');
}

async function loadKnowledge() {
  knowledgeCategories = await api('/api/knowledge/categories');
  renderKnowledgeArticleCategoryOptions();
  await loadKnowledgeArticles();
  renderKnowledgeCategories();
  if (!currentKnowledgeArticleId && knowledgeMode !== 'edit') showKnowledgeEmpty();
  syncKnowledgeLayoutState();
}

async function loadKnowledgeArticles() {
  const params = new URLSearchParams();
  if (currentKnowledgeCategoryId) params.set('category_id', String(currentKnowledgeCategoryId));
  const q = $('knowledgeSearch')?.value?.trim();
  if (q) params.set('q', q);
  knowledgeArticles = await api(`/api/knowledge/articles?${params.toString()}`);
  renderKnowledgeArticles();
}

function renderKnowledgeArticles() {
  renderKnowledgeCategories();
  const list = $('knowledgeArticlesList');
  if (!list) return;
  if (isKnowledgeMobile()) {
    list.classList.remove('hidden');
    list.innerHTML = knowledgeArticles.length
      ? knowledgeArticles.map(a => knowledgeArticleCardHtml(a)).join('')
      : '<div class="empty-card">По выбранным фильтрам статьи не найдены.</div>';
    list.querySelectorAll('[data-kb-article]').forEach(btn => {
      btn.onclick = async () => {
        await openKnowledgeArticle(Number(btn.dataset.kbArticle));
      };
    });
  } else {
    list.classList.add('hidden');
    list.innerHTML = '';
  }
}

function knowledgeTagsHtml(tags) {
  const parts = String(tags || '').split(',').map(t => t.trim()).filter(Boolean);
  if (!parts.length) return '';
  return parts.map(t => `<span>${escapeHtml(t)}</span>`).join('');
}

function showKnowledgeEmpty() {
  knowledgeMode = 'empty';
  $('knowledgeEmpty')?.classList.remove('hidden');
  $('knowledgeReader')?.classList.add('hidden');
  $('knowledgeArticleForm')?.classList.add('hidden');
  closeKnowledgeModal();
  syncKnowledgeLayoutState();
}

function showKnowledgeReader(article) {
  currentKnowledgeArticle = article;
  knowledgeMode = 'read';
  $('knowledgeEmpty')?.classList.add('hidden');
  $('knowledgeArticleForm')?.classList.add('hidden');
  $('knowledgeReader')?.classList.remove('hidden');
  syncKnowledgeLayoutState();
  if ($('knowledgeReaderTitle')) $('knowledgeReaderTitle').textContent = article.title || 'Без названия';
  if ($('knowledgeReaderCategory')) $('knowledgeReaderCategory').textContent = `${article.category_title || 'Без раздела'} · обновлено ${formatDateTime(article.updated_at) || ''}`;
  if ($('knowledgeReaderTags')) $('knowledgeReaderTags').innerHTML = knowledgeTagsHtml(article.tags) || '<span>без тегов</span>';
  const img = $('knowledgeReaderImage');
  if (img) {
    if (article.image_url) {
      img.src = article.image_url;
      img.classList.remove('hidden');
    } else {
      img.removeAttribute('src');
      img.classList.add('hidden');
    }
  }
  if ($('knowledgeReaderContent')) {
    const content = article.content || 'Текст статьи пока пустой.';
    $('knowledgeReaderContent').innerHTML = escapeHtml(content).replace(/\n/g, '<br>');
  }
  if (isKnowledgeMobile()) openKnowledgeModal(article); else closeKnowledgeModal();
}

function showKnowledgeEditor(article = null) {
  knowledgeMode = 'edit';
  currentKnowledgeArticle = article;
  currentKnowledgeArticleId = article?.id ? Number(article.id) : null;
  $('knowledgeEmpty')?.classList.add('hidden');
  $('knowledgeReader')?.classList.add('hidden');
  $('knowledgeArticleForm')?.classList.remove('hidden');
  closeKnowledgeModal();
  syncKnowledgeLayoutState();
  if ($('knowledgeFormTitle')) $('knowledgeFormTitle').textContent = article?.id ? 'Редактирование статьи' : 'Новая статья';
  if ($('knowledgeArticleTitle')) $('knowledgeArticleTitle').value = article?.title || '';
  if ($('knowledgeArticleContent')) $('knowledgeArticleContent').value = article?.content || '';
  if ($('knowledgeArticleTags')) $('knowledgeArticleTags').value = article?.tags || '';
  if ($('knowledgeArticleImageUrl')) $('knowledgeArticleImageUrl').value = article?.image_url || '';
  if ($('knowledgeArticleImageFile')) $('knowledgeArticleImageFile').value = '';
  if ($('knowledgeClearImage')) $('knowledgeClearImage').checked = false;
  $('knowledgeClearImageLabel')?.classList.toggle('hidden', !article?.image_url);
  updateKnowledgeImagePreview(article?.image_url || '');
  renderKnowledgeArticleCategoryOptions();
  if ($('knowledgeArticleCategory')) $('knowledgeArticleCategory').value = article?.category_id || currentKnowledgeCategoryId || '';
  renderKnowledgeArticles();
  setTimeout(() => $('knowledgeArticleTitle')?.focus(), 50);
}

function updateKnowledgeImagePreview(url) {
  const box = $('knowledgeImagePreview');
  if (!box) return;
  if (!url) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = `<img src="${escapeHtml(url)}" alt="Превью изображения статьи" /><span>Изображение будет показано в статье</span>`;
}

async function uploadKnowledgeImageIfNeeded() {
  const fileInput = $('knowledgeArticleImageFile');
  const file = fileInput?.files?.[0];
  if (!file) return null;
  const form = new FormData();
  form.append('file', file);
  const response = await fetch('/api/knowledge/upload-image', { method: 'POST', body: form, credentials: 'same-origin' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Не удалось загрузить изображение');
  return data.url;
}

async function openKnowledgeArticle(articleId) {
  const a = await api(`/api/knowledge/articles/${articleId}`);
  currentKnowledgeArticleId = Number(a.id);
  showKnowledgeReader(a);
  renderKnowledgeArticles();
}

function resetKnowledgeEditor() {
  showKnowledgeEditor(null);
}



// v85: Analytics dashboard.
// Keep calculations on the backend; frontend only formats filters and renders the dashboard.
function analyticsDefaultDateRange() {
  const now = new Date();
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const start = new Date(end);
  start.setDate(start.getDate() - 13);
  const toInput = (date) => {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  };
  return { from: toInput(start), to: toInput(end) };
}

function setupAnalyticsDefaults() {
  const from = $('analyticsDateFrom');
  const to = $('analyticsDateTo');
  const range = analyticsDefaultDateRange();
  if (from && !from.value) from.value = range.from;
  if (to && !to.value) to.value = range.to;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '—';
  const total = Math.max(0, Math.round(Number(seconds)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} д ${hours} ч`;
  if (hours > 0) return `${hours} ч ${minutes} мин`;
  if (minutes > 0) return `${minutes} мин`;
  return `${total} сек`;
}

function hourLabel(hour) {
  const h = Number(hour);
  if (!Number.isFinite(h)) return '—';
  return `${String(h).padStart(2, '0')}:00–${String((h + 1) % 24).padStart(2, '0')}:00`;
}

function marketplaceLabel(value) {
  const map = { ozon: 'Ozon', wildberries: 'WB', yandex: 'Яндекс' };
  return map[value] || value || '—';
}

function analyticsQueryString() {
  setupAnalyticsDefaults();
  const params = new URLSearchParams();
  const dateFrom = $('analyticsDateFrom')?.value || '';
  const dateTo = $('analyticsDateTo')?.value || '';
  const marketplace = $('analyticsMarketplace')?.value || '';
  const hourFrom = $('analyticsHourFrom')?.value;
  const hourTo = $('analyticsHourTo')?.value;
  if (dateFrom) params.set('date_from', dateFrom);
  if (dateTo) params.set('date_to', dateTo);
  if (marketplace) params.set('marketplace', marketplace);
  if (hourFrom !== undefined && hourFrom !== null && String(hourFrom).trim() !== '') params.set('hour_from', String(hourFrom).trim());
  if (hourTo !== undefined && hourTo !== null && String(hourTo).trim() !== '') params.set('hour_to', String(hourTo).trim());
  return params.toString();
}

function setAnalyticsText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

async function loadAnalytics() {
  setupAnalyticsDefaults();
  const query = analyticsQueryString();
  const [data, drilldown] = await Promise.all([
    api(`/api/analytics/chats?${query}`),
    api(`/api/analytics/chats/drilldown?${query}&limit=1000&include_excluded=1`),
  ]);
  renderAnalytics(data);
  renderAnalyticsDrilldown(drilldown);
  analyticsLoadedOnce = true;
}

function renderAnalytics(data) {
  const summary = data.summary || {};
  const filters = data.filters || {};
  const total = Number(summary.requests || summary.daily_first_requests || 0);
  const rawInbound = Number(summary.raw_inbound_messages || summary.inbound_messages || 0);
  const uniqueChats = Number(summary.unique_chats || 0);
  const uniqueClients = Number(summary.unique_clients || 0);
  const outside = Number(summary.outside_hours_requests || summary.outside_hours_messages || 0);
  const before10 = Number(summary.before_10_requests || summary.before_10_messages || 0);
  const after19 = Number(summary.after_19_requests || summary.after_19_messages || 0);
  const closed = Number(summary.closed_chats || 0);
  const answered = Number(summary.answered_response_blocks || summary.answered_requests || summary.answered_inbound_messages || 0);
  const unanswered = Number(summary.unanswered_response_blocks || summary.unanswered_requests || summary.unanswered_inbound_messages || 0);
  const closedNoResponse = Number(summary.closed_without_response_blocks || 0);
  const peak = summary.peak_hour;

  setAnalyticsText('analyticsTotalRequests', String(total));
  setAnalyticsText('analyticsTotalRequestsHint', `первые обращения за день · сообщений: ${rawInbound}`);
  setAnalyticsText('analyticsUniqueChats', `${uniqueClients} / ${uniqueChats}`);
  setAnalyticsText('analyticsUniqueClientsHint', 'уникальные клиенты / чаты за период');
  setAnalyticsText('analyticsAvgResponse', formatDuration(summary.avg_response_seconds));
  setAnalyticsText('analyticsAvgResponseHint', `диалоговых ответов ${answered}, ждут ответа ${unanswered}${closedNoResponse ? `, закрыто без ответа ${closedNoResponse}` : ''}`);
  setAnalyticsText('analyticsOutsideHours', String(outside));
  setAnalyticsText('analyticsOutsideHoursHint', `первых до 10:00 — ${before10}, после 19:00 — ${after19}`);
  setAnalyticsText('analyticsClosedChats', String(closed));
  setAnalyticsText('analyticsPeakHour', peak ? hourLabel(peak.hour) : '—');
  setAnalyticsText('analyticsPeakHourHint', peak ? `${Number(peak.requests || peak.inbound_messages || 0)} первых обращений` : 'нет данных');

  renderAnalyticsHourly(data.hourly || []);
  renderAnalyticsDaily(data.daily || []);
  renderAnalyticsMarketplaceBreakdown(data.marketplace_breakdown || []);
}

function renderAnalyticsHourly(rows) {
  const el = $('analyticsHourlyChart');
  if (!el) return;
  const max = Math.max(1, ...rows.map(row => Number(row.requests || row.inbound_messages || 0)));
  el.innerHTML = rows.map(row => {
    const count = Number(row.requests || row.inbound_messages || 0);
    const width = Math.max(2, Math.round((count / max) * 100));
    return `
      <button class="analytics-hour-row analytics-hour-button" type="button" data-analytics-hour="${Number(row.hour)}">
        <div class="analytics-hour-label">${escapeHtml(String(row.hour).padStart(2, '0'))}:00</div>
        <div class="analytics-hour-track" title="${escapeHtml(hourLabel(row.hour))}">
          <div class="analytics-hour-fill" style="width:${width}%"></div>
        </div>
        <div class="analytics-hour-value">${count}</div>
      </button>
    `;
  }).join('') || '<div class="analytics-empty">Нет данных за выбранный период.</div>';
  el.querySelectorAll('[data-analytics-hour]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const hour = Number(btn.dataset.analyticsHour);
      const from = $('analyticsHourFrom');
      const to = $('analyticsHourTo');
      if (from) from.value = String(hour);
      if (to) to.value = String(hour);
      loadAnalytics().catch(err => notify('Ошибка аналитики', String(err.message || err)));
      $('analyticsDrilldownTable')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

function renderAnalyticsDrilldown(data) {
  const summaryEl = $('analyticsDrilldownSummary');
  const tableEl = $('analyticsDrilldownTable');
  const excludedEl = $('analyticsExcludedTable');
  if (!tableEl) return;

  const items = data?.items || [];
  const excluded = data?.excluded_service_items || [];
  const filters = data?.filters || {};
  if (summaryEl) {
    const hoursText = filters.hour_from !== null && filters.hour_from !== undefined
      ? ` · час ${String(filters.hour_from).padStart(2, '0')}:00${filters.hour_to !== filters.hour_from ? `–${String(filters.hour_to).padStart(2, '0')}:59` : ''}`
      : '';
    summaryEl.textContent = `В отчёт входит строк: ${items.length}${hoursText}. Исключено служебных сообщений: ${data?.excluded_service_count || 0}.`;
  }

  if (!items.length) {
    tableEl.innerHTML = '<div class="analytics-empty">Нет строк, которые входят в расчёт по текущим фильтрам.</div>';
  } else {
    tableEl.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Дата/час</th>
            <th>Маркетплейс</th>
            <th>Клиент / чат</th>
            <th>Сообщение</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${items.map(item => `
            <tr>
              <td>${escapeHtml(item.local_datetime || item.created_at || '')}<br><small>${String(item.local_hour ?? '').padStart(2, '0')}:00</small></td>
              <td>${escapeHtml(marketplaceLabel(item.marketplace))}</td>
              <td>
                <strong>${escapeHtml(item.customer_label || item.customer_name || 'Клиент')}</strong><br>
                <small>ID чата: ${escapeHtml(String(item.chat_id || ''))}${item.order_id ? ` · заказ ${escapeHtml(String(item.order_id))}` : ''}</small>
              </td>
              <td>${escapeHtml(item.text_preview || '')}</td>
              <td><button class="secondary small" type="button" data-open-analytics-chat="${Number(item.chat_id)}">Открыть</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
    tableEl.querySelectorAll('[data-open-analytics-chat]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const chatId = Number(btn.dataset.openAnalyticsChat);
        showView('chats');
        await loadChats();
        await openChat(chatId);
      });
    });
  }

  if (excludedEl) {
    if (!excluded.length) {
      excludedEl.innerHTML = '<div class="analytics-empty">Нет исключённых служебных сообщений за выбранный период.</div>';
    } else {
      excludedEl.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Дата/час</th>
              <th>Маркетплейс</th>
              <th>Чат</th>
              <th>Автор</th>
              <th>Текст</th>
            </tr>
          </thead>
          <tbody>
            ${excluded.map(item => `
              <tr>
                <td>${escapeHtml(item.created_at || '')}<br><small>${String(item.local_hour ?? '').padStart(2, '0')}:00</small></td>
                <td>${escapeHtml(marketplaceLabel(item.marketplace))}</td>
                <td>${escapeHtml(item.customer_label || '—')}<br><small>ID ${escapeHtml(String(item.chat_id || ''))}</small></td>
                <td>${escapeHtml(item.author || '')}</td>
                <td>${escapeHtml(item.text_preview || '')}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }
  }
}

function renderAnalyticsDaily(rows) {
  const el = $('analyticsDailyTable');
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = '<div class="analytics-empty">Нет обращений за выбранный период.</div>';
    return;
  }
  el.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Дата</th>
          <th>Обращения</th>
          <th>Сообщения</th>
          <th>Чаты</th>
          <th>Закрыто</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map(row => `
          <tr>
            <td>${escapeHtml(row.date || '')}</td>
            <td>${Number(row.requests || row.daily_first_requests || row.inbound_messages || 0)}</td>
            <td>${Number(row.raw_inbound_messages || row.inbound_messages || 0)}</td>
            <td>${Number(row.unique_chats || 0)}</td>
            <td>${Number(row.closed_chats || 0)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderAnalyticsMarketplaceBreakdown(rows) {
  const el = $('analyticsMarketplaceBreakdown');
  if (!el) return;
  if (!rows.length) {
    el.innerHTML = '<div class="analytics-empty">Нет данных по маркетплейсам.</div>';
    return;
  }
  const max = Math.max(1, ...rows.map(row => Number(row.requests || row.inbound_messages || 0)));
  el.innerHTML = rows.map(row => {
    const count = Number(row.requests || row.inbound_messages || 0);
    const width = Math.max(2, Math.round((count / max) * 100));
    return `
      <div class="analytics-marketplace-row">
        <div class="analytics-marketplace-name">${escapeHtml(marketplaceLabel(row.marketplace))}</div>
        <div class="analytics-marketplace-track">
          <div class="analytics-marketplace-fill" style="width:${width}%"></div>
        </div>
        <div>${count} / ${Number(row.unique_chats || 0)} чатов</div>
      </div>
    `;
  }).join('');
}



function isMobileSecondaryView(view) {
  return ['questions', 'knowledge', 'users', 'techSettings', 'profile'].includes(String(view || ''));
}

function setMobileMoreActiveState(view = activeView) {
  const btn = $('mobileMoreBtn');
  if (!btn) return;
  const isActive = isMobileSecondaryView(view);
  btn.classList.toggle('active', isActive);
  btn.setAttribute('aria-current', isActive ? 'page' : 'false');
}

function toggleMobileMoreSheet(force) {
  const backdrop = $('mobileNavBackdrop');
  const sheet = $('mobileMoreSheet');
  if (!backdrop || !sheet) return;
  const shouldOpen = typeof force === 'boolean' ? force : sheet.classList.contains('hidden');
  sheet.classList.toggle('hidden', !shouldOpen);
  backdrop.classList.toggle('hidden', !shouldOpen);
  sheet.setAttribute('aria-hidden', shouldOpen ? 'false' : 'true');
  document.body.classList.toggle('mobile-more-open', shouldOpen);
}

function handleMobileMoreAction(action, view) {
  if (action === 'logout') {
    toggleMobileMoreSheet(false);
    $('profileLogoutBtn')?.click();
    return;
  }
  if (action === 'notifications') {
    toggleMobileMoreSheet(false);
    toggleNotificationsPanel(true);
    return;
  }
  if (view) {
    toggleMobileMoreSheet(false);
    showView(view);
  }
}

function normalizeViewName(view) {
  return VALID_VIEWS.includes(view) ? view : 'chats';
}

function viewFromRouteValue(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const clean = raw
    .replace(/^#/, '')
    .replace(/^\/?/, '')
    .split(/[/?&]/)[0]
    .trim();
  return ROUTE_VIEWS[clean] || normalizeViewName(clean);
}

function getViewFromLocationHash() {
  return viewFromRouteValue(window.location.hash || '');
}

function getInitialRouteView() {
  const hashView = getViewFromLocationHash();
  if (hashView) return hashView;
  try {
    const saved = localStorage.getItem(ROUTE_STORAGE_KEY);
    if (saved) return viewFromRouteValue(saved);
  } catch (_) {}
  return 'chats';
}

function routeForView(view) {
  return VIEW_ROUTES[normalizeViewName(view)] || 'chats';
}

function rememberRouteView(view) {
  try {
    localStorage.setItem(ROUTE_STORAGE_KEY, normalizeViewName(view));
  } catch (_) {}
}

function syncRouteForView(view, { replace = false } = {}) {
  const normalizedView = normalizeViewName(view);
  rememberRouteView(normalizedView);
  const targetHash = `#/${routeForView(normalizedView)}`;
  if (window.location.hash === targetHash) return;
  const targetUrl = `${window.location.pathname}${window.location.search}${targetHash}`;
  if (replace && window.history?.replaceState) {
    window.history.replaceState(null, '', targetUrl);
    return;
  }
  if (window.history?.pushState) {
    window.history.pushState(null, '', targetUrl);
    return;
  }
  window.location.hash = targetHash;
}

function showView(view, options = {}) {
  const normalizedView = normalizeViewName(view);
  activeView = normalizedView;
  document.body.dataset.activeView = normalizedView;
  if (options.syncRoute !== false) {
    syncRouteForView(normalizedView, { replace: Boolean(options.replaceRoute) });
  } else {
    rememberRouteView(normalizedView);
  }
  if (normalizedView !== 'chats') {
    mobileChatClosedByUser = false;
    setMobileChatOpen(false);
  }
  if (normalizedView !== 'knowledge') {
    closeKnowledgeModal();
  }

  const viewMap = {
    chats: $('chatsView'),
    analytics: $('analyticsView'),
    tasks: $('tasksView'),
    reviews: $('reviewsView'),
    questions: $('questionsView'),
    knowledge: $('knowledgeView'),
    users: $('usersView'),
    techSettings: $('techSettingsView'),
    profile: $('profileView'),
  };
  Object.entries(viewMap).forEach(([key, element]) => {
    if (!element) return;
    const isActive = key === normalizedView;
    element.classList.toggle('hidden', !isActive);
    element.classList.toggle('active-view', isActive);
    element.setAttribute('aria-hidden', isActive ? 'false' : 'true');
  });

  const navMap = {
    chats: $('navChats'),
    analytics: $('navAnalytics'),
    tasks: $('navTasks'),
    reviews: $('navReviews'),
    questions: $('navQuestions'),
    knowledge: $('navKnowledge'),
    users: $('navUsers'),
    techSettings: $('navTechSettings'),
    profile: $('navProfile'),
  };
  Object.entries(navMap).forEach(([key, element]) => {
    if (!element) return;
    const isActive = key === normalizedView;
    element.classList.toggle('active', isActive);
    element.setAttribute('aria-current', isActive ? 'page' : 'false');
  });

  setMobileMoreActiveState(normalizedView);
  if (normalizedView !== 'profile' && normalizedView !== 'techSettings' && normalizedView !== 'users') {
    toggleMobileMoreSheet(false);
  }

  if (normalizedView === 'analytics') loadAnalytics().catch(err => notify('Ошибка загрузки аналитики', String(err.message || err)));
  if (normalizedView === 'tasks') loadAllTasks().catch(err => notify('Ошибка загрузки задач', String(err.message || err)));
  if (normalizedView === 'reviews') loadReviews().catch(err => notify('Ошибка загрузки отзывов', String(err.message || err)));
  if (normalizedView === 'questions') { loadQuestions().catch(err => notify('Ошибка загрузки вопросов', String(err.message || err))); runFrontendQuestionsSyncSoon('show-questions'); }
  if (normalizedView === 'knowledge') loadKnowledge().catch(err => notify('Ошибка загрузки базы знаний', String(err.message || err)));
  if (normalizedView === 'users') loadUsers().catch(err => notify('Ошибка загрузки сотрудников', String(err.message || err)));
  if (normalizedView === 'techSettings') loadChatSettings({ keepValues: true }).catch(err => notify('Ошибка загрузки тех. настроек', String(err.message || err)));
  if (normalizedView === 'profile') fillProfileForm();
  if (normalizedView === 'chats') runFrontendSyncSoon('show-chats');
}

function escapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function bind(id, eventName, handler) {
  const el = $(id);
  if (!el) {
    console.warn(`Element not found: #${id}`);
    return;
  }
  el.addEventListener(eventName, handler);
}


function showLogin(errorText = '') {
  $('loginScreen')?.classList.remove('hidden');
  $('appShell')?.classList.add('app-locked');
  if ($('loginError')) $('loginError').textContent = errorText;
}

function showApp(user) {
  currentUser = user || currentUser;
  $('loginScreen')?.classList.add('hidden');
  $('appShell')?.classList.remove('app-locked');
  updateAuthUiForUser();
  loadAssignees().catch(err => console.warn('assignees after auth failed', err));
}

async function checkAuth() {
  try {
    const data = await api('/api/auth/me');
    showApp(data.user);
    return true;
  } catch (_) {
    showLogin();
    return false;
  }
}

function updateAuthUiForUser() {
  const user = currentUser || {};
  const roleLabel = user.role === 'admin' ? 'администратор' : user.role === 'viewer' ? 'наблюдатель' : 'менеджер';
  if ($('currentUserLabel')) $('currentUserLabel').textContent = user.username ? `${user.display_name || user.username} · ${roleLabel}` : '—';
  document.querySelectorAll('.admin-only').forEach((el) => el.classList.toggle('hidden', user.role !== 'admin'));
}

function fillProfileForm() {
  if (!$('profileForm')) return;
  $('profileUsername').value = currentUser?.username || '';
  $('profileDisplayName').value = currentUser?.display_name || '';
  $('profileCurrentPassword').value = '';
  $('profileNewPassword').value = '';
}

async function submitProfile(event) {
  event.preventDefault();
  const payload = {
    username: $('profileUsername')?.value?.trim() || '',
    display_name: $('profileDisplayName')?.value?.trim() || '',
  };
  const currentPassword = $('profileCurrentPassword')?.value || '';
  const newPassword = $('profileNewPassword')?.value || '';
  if (newPassword) {
    payload.current_password = currentPassword;
    payload.new_password = newPassword;
  }
  try {
    const data = await api('/api/auth/profile', { method: 'PATCH', body: JSON.stringify(payload) });
    currentUser = data.user || currentUser;
    updateAuthUiForUser();
    fillProfileForm();
    notify('Готово', 'Профиль сохранён.');
  } catch (err) {
    notify('Не удалось сохранить профиль', String(err.message || err));
  }
}


function openUserCreateModal() {
  const modal = $('userCreateModal');
  if (!modal) return;
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
  setTimeout(() => $('newUserUsername')?.focus(), 30);
}

function closeUserCreateModal() {
  const modal = $('userCreateModal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
}

function handleUserCreateModalBackdropClick(event) {
  if (event.target?.matches?.('[data-user-create-close]')) {
    closeUserCreateModal();
  }
}

function roleLabel(role) {
  return role === 'admin' ? 'Админ' : role === 'viewer' ? 'Наблюдатель' : 'Менеджер';
}

function openUserEditModal(userId) {
  const user = usersCache.find((item) => Number(item.id) === Number(userId));
  if (!user) return;
  const modal = $('userEditModal');
  if (!modal) return;
  modal.dataset.userId = String(user.id);
  if ($('editUserUsername')) $('editUserUsername').value = user.username || '';
  if ($('editUserDisplayName')) $('editUserDisplayName').value = user.display_name || '';
  if ($('editUserRole')) $('editUserRole').value = user.role || 'manager';
  if ($('editUserPassword')) $('editUserPassword').value = '';
  if ($('editUserActive')) $('editUserActive').checked = Boolean(user.is_active);
  if ($('userEditModalSubtitle')) {
    $('userEditModalSubtitle').textContent = `@${user.username || 'user'} · ${roleLabel(user.role)} · ${user.is_active ? 'активен' : 'отключён'}`;
  }
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
  setTimeout(() => $('editUserDisplayName')?.focus(), 30);
}

function closeUserEditModal() {
  const modal = $('userEditModal');
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
  delete modal.dataset.userId;
}

function handleUserEditModalBackdropClick(event) {
  if (event.target?.matches?.('[data-user-edit-close]')) {
    closeUserEditModal();
  }
}

async function submitUserEdit(event) {
  event.preventDefault();
  const modal = $('userEditModal');
  const userId = Number(modal?.dataset.userId || 0);
  if (!userId) return;
  const payload = {
    display_name: $('editUserDisplayName')?.value?.trim() || '',
    role: $('editUserRole')?.value || 'manager',
    is_active: Boolean($('editUserActive')?.checked),
  };
  const password = $('editUserPassword')?.value || '';
  try {
    await api(`/api/users/${userId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    if (password) {
      await api(`/api/users/${userId}/password`, { method: 'POST', body: JSON.stringify({ password }) });
    }
    closeUserEditModal();
    await loadUsers();
    await loadAssignees();
    notify('Готово', 'Данные сотрудника сохранены.');
  } catch (err) {
    notify('Не удалось сохранить сотрудника', String(err.message || err));
  }
}


async function loadUsers() {
  if (!currentUser || currentUser.role !== 'admin') {
    showView('profile');
    return;
  }
  const users = await api('/api/users');
  usersCache = Array.isArray(users) ? users : [];
  const list = $('usersList');
  if (!list) return;
  if (!usersCache.length) {
    list.innerHTML = '<p class="muted">Сотрудников пока нет.</p>';
    return;
  }
  list.innerHTML = usersCache.map((u) => `
    <article class="user-row user-row-compact" data-user-id="${u.id}">
      <div class="user-summary">
        <strong>${escapeHtml(u.display_name || u.username)}</strong>
        <p>@${escapeHtml(u.username)} · ${escapeHtml(roleLabel(u.role))}</p>
        <div class="user-chip-row">
          <span class="user-status-chip ${u.is_active ? 'is-active' : 'is-disabled'}">${u.is_active ? 'Активен' : 'Отключён'}</span>
        </div>
      </div>
      <button class="user-light-btn user-edit-btn" type="button" data-user-edit="${u.id}" aria-label="Редактировать сотрудника">
        <span class="btn-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16v4z"/><path d="M13.5 6.5l4 4"/></svg></span>
        <span>Редактировать</span>
      </button>
    </article>
  `).join('');
}

function userCreateValidationError(payload) {
  if (!payload.username || payload.username.length < 2) {
    return { message: 'Логин должен быть не короче 2 символов.', fieldId: 'newUserUsername' };
  }
  if (!payload.password || payload.password.length < 6) {
    return { message: 'Пароль должен быть не короче 6 символов.', fieldId: 'newUserPassword' };
  }
  return null;
}


async function submitUserCreate(event) {
  event.preventDefault();
  const payload = {
    username: $('newUserUsername')?.value?.trim() || '',
    display_name: $('newUserDisplayName')?.value?.trim() || null,
    password: $('newUserPassword')?.value || '',
    role: $('newUserRole')?.value || 'manager',
  };
  const validation = userCreateValidationError(payload);
  if (validation) {
    notify('Проверьте данные сотрудника', validation.message);
    $(validation.fieldId)?.focus();
    return;
  }
  try {
    await api('/api/users', { method: 'POST', body: JSON.stringify(payload) });
    ['newUserUsername', 'newUserDisplayName', 'newUserPassword'].forEach(id => { if ($(id)) $(id).value = ''; });
    closeUserCreateModal();
    await loadUsers();
    await loadAssignees();
    notify('Готово', 'Сотрудник создан.');
  } catch (err) {
    const message = String(err.message || err).replace(/^422:\s*/,'');
    notify('Не удалось создать сотрудника', message || 'Проверьте логин, пароль и роль.');
  }
}

async function handleUsersListClick(event) {
  const editBtn = event.target.closest?.('[data-user-edit]');
  if (!editBtn) return;
  event.preventDefault();
  openUserEditModal(Number(editBtn.dataset.userEdit));
}

async function handleChatFunnelListClick(event) {
  // Воронки временно скрыты из интерфейса. Оставлено для совместимости старых DOM.
}

async function handleChatStatusListClick(event) {
  const colorBtn = event.target.closest('[data-status-color-choice]');
  if (colorBtn) {
    const row = colorBtn.closest('[data-status-id]');
    const color = colorBtn.dataset.statusColorChoice || 'orange';
    row?.querySelector('[data-status-color]')?.setAttribute('value', color);
    row?.querySelectorAll('[data-status-color-choice]').forEach(btn => btn.classList.toggle('selected', btn === colorBtn));
    return;
  }

  const btn = event.target.closest('[data-status-action]');
  if (!btn) return;
  const row = btn.closest('[data-status-id]');
  if (!row) return;
  const id = Number(row.dataset.statusId);
  if (btn.dataset.statusAction === 'delete') {
    if (!confirm('Удалить статус? Если он системный или уже используется в чатах, CRM просто скроет его.')) return;
    await api(`/api/chat-settings/statuses/${id}`, { method: 'DELETE' });
    await loadChatSettings({ keepValues: true });
    await loadChats();
  }
}

function setupAuthUi() {
  const form = $('loginForm');
  if (form && !form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const btn = $('loginSubmitBtn');
      if (btn) btn.disabled = true;
      if ($('loginError')) $('loginError').textContent = '';
      try {
        const data = await api('/api/auth/login', {
          method: 'POST',
          body: JSON.stringify({
            username: $('loginUsername')?.value || '',
            password: $('loginPassword')?.value || '',
          }),
        });
        showApp(data.user);
        if (!appInitialized) init();
        await refreshVisibleData();
      } catch (err) {
        showLogin(String(err.message || err).replace(/^401:\s*/, ''));
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  }
  const doLogout = async () => {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (_) {}
    currentUser = null;
    appInitialized = false;
    if (refreshTimer) clearInterval(refreshTimer);
    if (statusTimer) clearInterval(statusTimer);
    if (notificationsTimer) clearInterval(notificationsTimer);
    if (frontendSyncTimer) clearInterval(frontendSyncTimer);
    frontendSyncTimer = null;
    frontendSyncInFlight = false;
    document.body.classList.remove('mobile-chat-open');
    showLogin('');
  };

  ['logoutBtn', 'mobileLogoutBtn', 'profileLogoutBtn'].forEach((id) => {
    const btn = $(id);
    if (btn && !btn.dataset.bound) {
      btn.dataset.bound = '1';
      btn.addEventListener('click', doLogout);
    }
  });
}

async function bootstrap() {
  setupAuthUi();
  const ok = await checkAuth();
  if (ok && !appInitialized) init();
}

function init() {
  if (appInitialized) return;
  appInitialized = true;
  activeView = getInitialRouteView();
  document.body.dataset.activeView = activeView || 'chats';
  loadChatSettings({ keepValues: true }).catch(err => console.warn('chat settings init failed', err));
  bind('refreshBtn', 'click', async () => {
    await runFrontendOzonFastSync({ silent: false, force: true });
    await refreshVisibleData();
  });
  bind('mobileBackBtn', 'click', backToChatListMobile);
  bind('marketplaceFilter', 'change', loadChats);
  bind('statusFilter', 'change', loadChats);
  if ($('funnelFilter')) bind('funnelFilter', 'change', () => { renderChatSettingsControls({ keepValues: true }); loadChats(); });
  bind('chatScopeSelect', 'change', handleChatScopeSelectChange);
  bind('taskStatusFilter', 'change', loadAllTasks);
  bind('taskBucketFilter', 'change', loadAllTasks);
  bind('navChats', 'click', () => showView('chats'));
  bind('navAnalytics', 'click', () => showView('analytics'));
  bind('navTasks', 'click', () => showView('tasks'));
  bind('navReviews', 'click', () => showView('reviews'));
  bind('navQuestions', 'click', () => showView('questions'));
  bind('navKnowledge', 'click', () => showView('knowledge'));
  bind('navUsers', 'click', () => showView('users'));
  bind('navTechSettings', 'click', () => showView('techSettings'));
  bind('navProfile', 'click', () => showView('profile'));
  bind('notificationsBtn', 'click', () => toggleNotificationsPanel());
  bind('mobileMoreBtn', 'click', (event) => { event.preventDefault(); event.stopPropagation(); toggleMobileMoreSheet(true); });
  bind('mobileMoreClose', 'click', () => toggleMobileMoreSheet(false));
  bind('mobileNavBackdrop', 'click', () => toggleMobileMoreSheet(false));
  $('mobileMoreSheet')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-mobile-action], [data-mobile-view]');
    if (!button) return;
    event.preventDefault();
    handleMobileMoreAction(button.dataset.mobileAction || '', button.dataset.mobileView || '');
  });
  document.querySelector('.main-nav')?.addEventListener('click', (event) => {
    const button = event.target.closest('#navChats, #navAnalytics, #navTasks, #navReviews, #navQuestions, #navKnowledge, #navUsers, #navTechSettings, #navProfile, #mobileMoreBtn');
    if (!button) return;
    if (button.id === 'mobileMoreBtn') return;
    event.preventDefault();
    const targetView = button.id === 'navAnalytics' ? 'analytics' : button.id === 'navTasks' ? 'tasks' : button.id === 'navReviews' ? 'reviews' : button.id === 'navQuestions' ? 'questions' : button.id === 'navKnowledge' ? 'knowledge' : button.id === 'navUsers' ? 'users' : button.id === 'navTechSettings' ? 'techSettings' : button.id === 'navProfile' ? 'profile' : 'chats';
    showView(targetView);
  });
  bind('analyticsRefreshBtn', 'click', loadAnalytics);
  bind('analyticsDrilldownRefreshBtn', 'click', loadAnalytics);
  ['analyticsDateFrom', 'analyticsDateTo', 'analyticsMarketplace', 'analyticsHourFrom', 'analyticsHourTo'].forEach((id) => {
    const el = $(id);
    if (el && !el.dataset.boundAnalytics) {
      el.dataset.boundAnalytics = '1';
      el.addEventListener('change', () => {
        if (activeView === 'analytics') loadAnalytics().catch(err => notify('Ошибка загрузки аналитики', String(err.message || err)));
      });
    }
  });
  bind('analyticsWorkHoursBtn', 'click', () => {
    if ($('analyticsHourFrom')) $('analyticsHourFrom').value = '10';
    if ($('analyticsHourTo')) $('analyticsHourTo').value = '18';
    loadAnalytics().catch(err => notify('Ошибка загрузки аналитики', String(err.message || err)));
  });
  bind('analyticsClearHoursBtn', 'click', () => {
    if ($('analyticsHourFrom')) $('analyticsHourFrom').value = '';
    if ($('analyticsHourTo')) $('analyticsHourTo').value = '';
    loadAnalytics().catch(err => notify('Ошибка загрузки аналитики', String(err.message || err)));
  });
  setupAnalyticsDefaults();
  bind('reviewStatusFilter', 'change', loadReviews);
  bind('questionStatusFilter', 'change', loadQuestions);
  bind('reviewUnansweredFilter', 'change', loadReviews);
  bind('questionUnansweredFilter', 'change', loadQuestions);
  bind('syncReviewsBtn', 'click', syncReviews);
  bind('syncQuestionsBtn', 'click', syncQuestions);
  bind('reviewReplyForm', 'submit', submitReviewReply);
  bind('questionAnswerForm', 'submit', submitQuestionAnswer);
  const questionAnswerText = $('questionAnswerText');
  if (questionAnswerText && !questionAnswerText.dataset.boundDraftSave) {
    questionAnswerText.dataset.boundDraftSave = '1';
    questionAnswerText.addEventListener('input', saveCurrentQuestionDraft);
  }
  bind('startChatFromReviewBtn', 'click', startChatFromReview);
  const userCreateForm = $('userCreateForm');
  if (userCreateForm && !userCreateForm.dataset.bound) { userCreateForm.dataset.bound = '1'; userCreateForm.addEventListener('submit', submitUserCreate); }
  bind('openUserCreateModalBtn', 'click', openUserCreateModal);
  bind('userCreateModalCloseBtn', 'click', closeUserCreateModal);
  bind('cancelUserCreateBtn', 'click', closeUserCreateModal);
  bind('userCreateModal', 'click', handleUserCreateModalBackdropClick);
  const profileForm = $('profileForm');
  if (profileForm && !profileForm.dataset.bound) { profileForm.dataset.bound = '1'; profileForm.addEventListener('submit', submitProfile); }
  const usersList = $('usersList');
  if (usersList && !usersList.dataset.bound) { usersList.dataset.bound = '1'; usersList.addEventListener('click', handleUsersListClick); }
  const userEditForm = $('userEditForm');
  if (userEditForm && !userEditForm.dataset.bound) { userEditForm.dataset.bound = '1'; userEditForm.addEventListener('submit', submitUserEdit); }
  bind('userEditModalCloseBtn', 'click', closeUserEditModal);
  bind('cancelUserEditBtn', 'click', closeUserEditModal);
  bind('userEditModal', 'click', handleUserEditModalBackdropClick);

  const funnelForm = $('chatFunnelForm');
  if (funnelForm && !funnelForm.dataset.bound) {
    funnelForm.dataset.bound = '1';
    funnelForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      await api('/api/chat-settings/funnels', { method: 'POST', body: JSON.stringify({ title: $('chatFunnelTitle').value.trim(), sort_order: Number($('chatFunnelSort').value || 0) }) });
      $('chatFunnelTitle').value = '';
      $('chatFunnelSort').value = '0';
      await loadChatSettings({ keepValues: true });
    });
  }
  const statusForm = $('chatStatusForm');
  if (statusForm && !statusForm.dataset.bound) {
    statusForm.dataset.bound = '1';
    statusForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const title = $('chatStatusTitle')?.value?.trim();
      if (!title) return;
      await api('/api/chat-settings/statuses', { method: 'POST', body: JSON.stringify({
        title,
        funnel_id: null,
        color: $('chatStatusColor')?.value || 'orange',
        sort_order: Number($('chatStatusSort')?.value || 0),
      }) });
      $('chatStatusTitle').value = '';
      $('chatStatusSort').value = '0';
      await loadChatSettings({ keepValues: true });
      notify('Технические настройки', 'Статус добавлен.');
    });
  }
  $('chatFunnelsList')?.addEventListener('click', handleChatFunnelListClick);
  $('chatStatusesList')?.addEventListener('click', handleChatStatusListClick);
  bind('saveChatSettingsBtn', 'click', saveAllChatStatuses);
  bind('saveChatSettingsBottomBtn', 'click', saveAllChatStatuses);
  bind('extraMenuBtn', 'click', (event) => {
    event.stopPropagation();
    toggleExtraMenu();
  });
  bind('aiGenerateBtn', 'click', generateAiReplyForSelected);
  bind('aiGenerateMenuBtn', 'click', () => { toggleExtraMenu(false); generateAiReplyForSelected(); });
  bind('aiClearSelectionBtn', 'click', clearAiSelection);
  document.addEventListener('click', (event) => {
    const menu = $('extraMenu');
    const btn = $('extraMenuBtn');
    if (!menu || !btn) return;
    if (!menu.contains(event.target) && !btn.contains(event.target)) toggleExtraMenu(false);
  });
  document.querySelectorAll('[data-extra]').forEach(button => {
    button.addEventListener('click', () => showExtraPanel(button.dataset.extra));
  });
  document.querySelectorAll('[data-view="tasks"]').forEach(button => {
    button.addEventListener('click', () => showView('tasks'));
  });

  bind('saveChatBtn', 'click', persistCurrentChatMeta);
  bind('chatStatus', 'change', scheduleCurrentChatMetaSave);
  bind('assignedUserSelect', 'change', scheduleCurrentChatMetaSave);
  bind('chatHeaderMenuBtn', 'click', () => {
    notify('Настройки чата', 'Статус и ответственный сохраняются автоматически.');
  });

  const customerForm = $('customerForm');
  if (customerForm) {
    customerForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!currentChatId) return;
      const name = $('customerNameInput').value.trim();
      await api(`/api/chats/${currentChatId}`, {
        method: 'PATCH',
        body: JSON.stringify({ customer_name: name || null }),
      });
      await loadChats();
      await openChat(currentChatId);
    });
  }

  const messageForm = $('messageForm');
  bind('attachImageBtn', 'click', () => $('chatImageInput')?.click());
  bind('chatImageInput', 'change', handleChatImageSelection);

  const messageTextArea = $('messageText');
  if (messageTextArea) {
    autosizeComposerTextarea(messageTextArea);
    messageTextArea.addEventListener('input', () => autosizeComposerTextarea(messageTextArea));
    messageTextArea.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return;
      event.preventDefault();
      if (messageForm?.requestSubmit) messageForm.requestSubmit();
      else messageForm?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
  }

  if (messageForm) {
    messageForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!currentChatId) return;
      const text = $('messageText').value.trim();
      const imageFiles = selectedChatImageFiles.slice();
      if (!text && !imageFiles.length) return;

      const sendBtn = messageForm.querySelector('button[type="submit"]');
      const attachBtn = $('attachImageBtn');
      if (sendBtn) sendBtn.disabled = true;
      if (attachBtn) attachBtn.disabled = true;
      try {
        if (imageFiles.length) {
          const formData = new FormData();
          imageFiles.forEach((file) => formData.append('images', file));
          formData.append('caption', text || '');
          await apiForm(`/api/chats/${currentChatId}/attachments`, formData);
        } else {
          await api(`/api/chats/${currentChatId}/messages`, {
            method: 'POST',
            body: JSON.stringify({ text, author: 'manager' }),
          });
        }
        $('messageText').value = '';
        clearComposerAttachments();
        autosizeComposerTextarea($('messageText'));
        await loadChats();
        await openChat(currentChatId);
      } catch (err) {
        notify('Сообщение не отправлено', String(err.message || err));
      } finally {
        if (sendBtn) sendBtn.disabled = false;
        if (attachBtn) attachBtn.disabled = false;
      }
    });
  }

  const taskForm = $('taskForm');
  if (taskForm) {
    taskForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!currentChatId) return;
      const title = $('taskTitle').value.trim();
      if (!title) return;
      await api('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          chat_id: currentChatId,
          title,
          assigned_user_id: $('taskAssignee').value ? Number($('taskAssignee').value) : null,
          due_at: $('taskDueAt').value || null,
          description: $('taskDescription')?.value || null,
        }),
      });
      $('taskTitle').value = '';
      if ($('taskAssignee')) $('taskAssignee').value = '';
      $('taskDueAt').value = '';
      if ($('taskDescription')) $('taskDescription').value = '';
      await openChat(currentChatId);
      await loadAllTasks();
      await loadStats();
    });
  }


  bind('knowledgeNewArticleBtn', 'click', resetKnowledgeEditor);
  bind('knowledgeEditArticleBtn', 'click', () => { if (currentKnowledgeArticle) showKnowledgeEditor(currentKnowledgeArticle); });
  bind('knowledgeBackToListBtn', 'click', () => { showKnowledgeEmpty(); renderKnowledgeArticles(); });
  bind('knowledgeBackFromEditorBtn', 'click', () => { showKnowledgeEmpty(); renderKnowledgeArticles(); });
  bind('knowledgeModalBackBtn', 'click', () => { showKnowledgeEmpty(); renderKnowledgeArticles(); });
  bind('knowledgeModalCloseBtn', 'click', () => { showKnowledgeEmpty(); renderKnowledgeArticles(); });
  bind('knowledgeModalEditBtn', 'click', () => { if (currentKnowledgeArticle) showKnowledgeEditor(currentKnowledgeArticle); });
  $('knowledgeModal')?.addEventListener('click', (event) => {
    if (event.target?.id === 'knowledgeModal') {
      showKnowledgeEmpty();
      renderKnowledgeArticles();
    }
  });
  bind('knowledgeCancelEditBtn', 'click', () => { if (currentKnowledgeArticleId) openKnowledgeArticle(currentKnowledgeArticleId); else showKnowledgeEmpty(); });
  bind('knowledgeSearch', 'input', () => { clearTimeout(window.__kbSearchTimer); window.__kbSearchTimer = setTimeout(loadKnowledgeArticles, 250); });
  window.addEventListener('resize', () => {
    clearTimeout(window.__kbResizeTimer);
    window.__kbResizeTimer = setTimeout(() => {
      if (document.body?.dataset?.activeView === 'knowledge') {
        renderKnowledgeArticles();
        syncKnowledgeLayoutState();
        if (!isKnowledgeMobile()) closeKnowledgeModal();
      }
    }, 120);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('knowledgeModal')?.classList.contains('hidden')) {
      showKnowledgeEmpty();
      renderKnowledgeArticles();
    }
  });
  const knowledgeCategoryForm = $('knowledgeCategoryForm');
  if (knowledgeCategoryForm && !knowledgeCategoryForm.dataset.bound) {
    knowledgeCategoryForm.dataset.bound = '1';
    knowledgeCategoryForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const title = $('knowledgeCategoryTitle')?.value?.trim();
      if (!title) return;
      await api('/api/knowledge/categories', { method: 'POST', body: JSON.stringify({ title }) });
      $('knowledgeCategoryTitle').value = '';
      await loadKnowledge();
    });
  }
  const knowledgeArticleForm = $('knowledgeArticleForm');
  if (knowledgeArticleForm && !knowledgeArticleForm.dataset.bound) {
    knowledgeArticleForm.dataset.bound = '1';
    knowledgeArticleForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const submitBtn = knowledgeArticleForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        let imageUrl = $('knowledgeArticleImageUrl')?.value?.trim() || null;
        const uploadedUrl = await uploadKnowledgeImageIfNeeded();
        if (uploadedUrl) imageUrl = uploadedUrl;
        const payload = {
          title: $('knowledgeArticleTitle')?.value?.trim(),
          category_id: $('knowledgeArticleCategory')?.value ? Number($('knowledgeArticleCategory').value) : null,
          tags: $('knowledgeArticleTags')?.value?.trim() || null,
          content: $('knowledgeArticleContent')?.value || '',
          image_url: imageUrl,
          clear_image: !!$('knowledgeClearImage')?.checked,
          is_published: true,
        };
        if (!payload.title) return notify('Укажите название статьи', 'Название обязательно.');
        let savedArticle;
        if (currentKnowledgeArticleId) {
          savedArticle = await api(`/api/knowledge/articles/${currentKnowledgeArticleId}`, { method: 'PATCH', body: JSON.stringify(payload) });
        } else {
          savedArticle = await api('/api/knowledge/articles', { method: 'POST', body: JSON.stringify(payload) });
          currentKnowledgeArticleId = savedArticle.id;
        }
        await loadKnowledge();
        await openKnowledgeArticle(savedArticle.id || currentKnowledgeArticleId);
        notify('База знаний', 'Статья сохранена.');
      } catch (err) {
        notify('Статья не сохранена', String(err.message || err));
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
    $('knowledgeArticleImageUrl')?.addEventListener('input', () => updateKnowledgeImagePreview($('knowledgeArticleImageUrl')?.value?.trim() || ''));
    $('knowledgeArticleImageFile')?.addEventListener('change', () => {
      const file = $('knowledgeArticleImageFile')?.files?.[0];
      if (file) updateKnowledgeImagePreview(URL.createObjectURL(file));
    });
  }

  const noteForm = $('noteForm');
  if (noteForm) {
    noteForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!currentChatId) return;
      const text = $('noteText').value.trim();
      if (!text) return;
      await api(`/api/chats/${currentChatId}/notes`, {
        method: 'POST',
        body: JSON.stringify({ text, author: 'manager' }),
      });
      $('noteText').value = '';
      await openChat(currentChatId);
    });
  }

  showView(activeView, { replaceRoute: true });

  loadAssignees()
    .then(() => {
      if (activeView === 'chats') return loadChats();
      return null;
    })
    .catch(err => console.warn('initial assignees load failed', err));
  loadSyncStatus();
  loadNotifications().catch(err => console.warn('notifications initial load failed', err));

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      runFrontendSyncSoon('visible');
      if (activeView === 'questions') runFrontendQuestionsSyncSoon('visible-questions');
    }
  });
  window.addEventListener('focus', () => {
    runFrontendSyncSoon('focus');
    if (activeView === 'questions') runFrontendQuestionsSyncSoon('focus-questions');
  });

  startFrontendAutoSync();
  runFrontendSyncSoon('startup');

  // Fastfox shared hosting is sensitive to many parallel fetches. Avoid the
  // previous 5-second full-view refresh loop; it made /chats, /notifications and
  // /sync/status overlap and delayed opening dialogs.
  refreshTimer = setInterval(() => {
    if (!document.hidden && activeView !== 'chats') refreshVisibleData();
  }, 60000);
  statusTimer = setInterval(() => {
    if (!document.hidden) loadSyncStatus();
  }, 60000);
  notificationsTimer = setInterval(() => {
    if (!document.hidden) loadNotifications();
  }, 60000);
}

document.addEventListener('DOMContentLoaded', bootstrap);

window.addEventListener('hashchange', () => {
  const routeView = getViewFromLocationHash();
  if (!routeView || routeView === activeView) return;
  showView(routeView, { syncRoute: false });
});



function autosizeComposerTextarea(textarea) {
  if (!textarea) return;
  textarea.style.height = 'auto';
  const maxHeight = window.matchMedia('(max-width: 520px)').matches ? 112 : 140;
  textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
}


document.addEventListener('click', (event) => {
  if (!event.target.closest?.('.message-actions-menu-wrap')) closeMessageActionsMenus();
});


document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    toggleMobileMoreSheet(false);
  }
});



document.addEventListener('click', (event) => {
  const moreBtn = event.target.closest?.('#mobileMoreBtn');
  if (moreBtn) {
    event.preventDefault();
    event.stopPropagation();
    toggleMobileMoreSheet(true);
  }
});


document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !$('userCreateModal')?.classList.contains('hidden')) {
    closeUserCreateModal();
  }
});


document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (!$('userEditModal')?.classList.contains('hidden')) closeUserEditModal();
  }
});
