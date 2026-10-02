from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    function_start = source.index(marker)
    start = function_start
    if source[max(0, function_start - len("async ")) : function_start] == "async ":
        start -= len("async ")
    signature_depth = 0
    signature_quote = ""
    signature_escaped = False
    signature_index = function_start + len(marker) - 1
    while signature_index < len(source):
        char = source[signature_index]
        if signature_quote:
            if signature_escaped:
                signature_escaped = False
            elif char == "\\":
                signature_escaped = True
            elif char == signature_quote:
                signature_quote = ""
        elif char in {"'", '"', "`"}:
            signature_quote = char
        elif char == "(":
            signature_depth += 1
        elif char == ")":
            signature_depth -= 1
            if signature_depth == 0:
                break
        signature_index += 1
    else:
        raise AssertionError(f"unterminated JavaScript signature: {name}")
    opening_brace = source.index("{", signature_index + 1)
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


class ChatOperatorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")

    def test_chat_refresh_preserves_active_task_editor_and_unsaved_values(self) -> None:
        preserve = _extract_function(self.source, "shouldPreserveTaskChatEditor")
        render = _extract_function(self.source, "renderTasks")

        self.assertLess(render.index("shouldPreserveTaskChatEditor"), render.index("box.innerHTML = ''"))
        self.assertIn("if (shouldPreserveTaskChatEditor(box)) return;", render)

        _run_node(
            f"""
            let currentChatId = 42;
            {preserve}
            const draft = {{ title: 'unsaved title', comment: 'unsaved comment' }};
            const box = {{
              dataset: {{ chatId: '42' }},
              querySelector(selector) {{
                return selector === '[data-task-edit-panel]:not(.hidden)' ? {{ draft }} : null;
              }},
            }};
            if (!shouldPreserveTaskChatEditor(box)) throw new Error('open editor was not protected');
            if (draft.title !== 'unsaved title' || draft.comment !== 'unsaved comment') throw new Error('draft changed');
            if (shouldPreserveTaskChatEditor(box, 99)) throw new Error('editor leaked into another chat');
            """
        )

    def test_task_save_error_does_not_close_editor(self) -> None:
        save = _extract_function(self.source, "saveTaskChatEditor")
        _run_node(
            f"""
            let currentChatId = 42;
            let closeCalls = 0;
            let openCalls = 0;
            const injected = new Error('save failed');
            async function patchTask() {{ throw injected; }}
            function closeTaskChatEditor() {{ closeCalls += 1; }}
            async function openChat() {{ openCalls += 1; }}
            {save}

            (async () => {{
              let caught = null;
              try {{
                await saveTaskChatEditor(17, {{ title: 'draft' }}, {{}}, {{}});
              }} catch (error) {{
                caught = error;
              }}
              if (caught !== injected) throw new Error('original save error was not preserved');
              if (closeCalls !== 0) throw new Error('editor closed after failed save');
              if (openCalls !== 0) throw new Error('chat refreshed after failed save');
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_message_timestamp_uses_time_today_and_full_date_for_older_messages(self) -> None:
        parse_date = _extract_function(self.source, "parseDate")
        formatter = _extract_function(self.source, "formatMessageTime")
        _run_node(
            f"""
            {parse_date}
            {formatter}
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12, 34, 0);
            const old = new Date(2020, 0, 2, 3, 4, 0);
            const todayValue = formatMessageTime(today.toISOString());
            const oldValue = formatMessageTime(old.toISOString());
            if (todayValue !== '12:34') throw new Error('today format: ' + todayValue);
            if (oldValue !== '02.01.2020, 03:04') throw new Error('old format: ' + oldValue);
            """
        )

    def test_notification_toast_keeps_context_and_uses_time_only_subtitle(self) -> None:
        render = _extract_function(self.source, "renderNotifications")
        unread = _extract_function(self.source, "currentUnreadNotifications")
        escape = _extract_function(self.source, "escapeHtml")
        parse_date = _extract_function(self.source, "parseDate")
        format_date_time = _extract_function(self.source, "formatDateTime")

        _run_node(
            f"""
            class NotificationElement {{
              constructor(kind, id) {{
                this.kind = kind;
                this.dataset = kind === 'open'
                  ? {{ notificationOpen: String(id) }}
                  : {{ notificationClose: String(id) }};
                this.listeners = new Map();
              }}
              addEventListener(type, handler) {{
                const handlers = this.listeners.get(type) || [];
                handlers.push(handler);
                this.listeners.set(type, handlers);
              }}
              async dispatch(type, key = '') {{
                const event = {{
                  type,
                  key,
                  prevented: false,
                  propagationStopped: false,
                  preventDefault() {{ this.prevented = true; }},
                  stopPropagation() {{ this.propagationStopped = true; }},
                }};
                for (const handler of this.listeners.get(type) || []) {{
                  await handler(event);
                }}
                return event;
              }}
            }}

            const stack = {{
              html: '',
              openElements: [],
              closeElements: [],
              classes: new Set(),
              classList: {{
                add(name) {{ stack.classes.add(name); }},
                remove(name) {{ stack.classes.delete(name); }},
              }},
              set innerHTML(value) {{
                this.html = String(value);
                this.openElements = [...this.html.matchAll(/data-notification-open="(\\d+)"/g)]
                  .map((match) => new NotificationElement('open', match[1]));
                this.closeElements = [...this.html.matchAll(/data-notification-close="(\\d+)"/g)]
                  .map((match) => new NotificationElement('close', match[1]));
              }},
              get innerHTML() {{ return this.html; }},
              querySelectorAll(selector) {{
                if (selector === '[data-notification-open]') return this.openElements;
                if (selector === '[data-notification-close]') return this.closeElements;
                return [];
              }},
            }};

            const createdAt = '2026-08-26T09:15:00Z';
            let notifications = [{{
              id: 17,
              type: 'new_message',
              title: 'Клиент <Тест> & "VIP"',
              body: 'Ozon <script>alert("x")</script> & товар',
              created_at: createdAt,
              chat_id: 42,
              is_read: false,
            }}];
            let notificationToastIds = [17];
            let notificationsPanelOpen = true;
            let openCalls = [];
            let closeCalls = [];
            function $(id) {{ return id === 'notificationToasts' ? stack : null; }}
            function openNotification(id) {{ openCalls.push(id); }}
            async function markNotificationRead(id) {{ closeCalls.push(id); }}
            function notify() {{ throw new Error('unexpected notification error'); }}
            function notificationTypeLabel() {{ return 'Сообщение'; }}

            {escape}
            {parse_date}
            {format_date_time}
            {unread}
            {render}

            (async () => {{
              renderNotifications();
              const firstOpenElement = stack.openElements[0];
              renderNotifications();
              const secondOpenElement = stack.openElements[0];
              if (firstOpenElement === secondOpenElement) throw new Error('innerHTML replacement did not recreate toast nodes');
              if (stack.openElements.length !== 1 || stack.closeElements.length !== 1) throw new Error('toast copy duplicated');

              const expectedTitle = escapeHtml(notifications[0].title);
              const expectedBody = escapeHtml(notifications[0].body);
              if (!stack.innerHTML.includes(expectedTitle) || !stack.innerHTML.includes(expectedBody)) {{
                throw new Error('contextual title or body was lost');
              }}
              if (stack.innerHTML.includes('<script>') || stack.innerHTML.includes('<Тест>')) {{
                throw new Error('notification content bypassed escaping');
              }}

              const subtitleMatch = stack.innerHTML.match(/<span class="notification-toast-subtitle">([^<]*)<\\/span>/);
              if (!subtitleMatch) throw new Error('subtitle was not rendered');
              const expectedTime = escapeHtml(formatDateTime(createdAt) || '');
              if (subtitleMatch[1] !== expectedTime) throw new Error(`unexpected subtitle: ${{subtitleMatch[1]}}`);
              if (['Сообщение', 'Вопрос', 'Задача'].some((label) => subtitleMatch[1].includes(label))) {{
                throw new Error('type label remained in subtitle');
              }}
              if (subtitleMatch[1].includes(' · ')) throw new Error('type separator remained in subtitle');

              await secondOpenElement.dispatch('click');
              if (openCalls.length !== 1 || openCalls[0] !== 17) throw new Error('click did not open linked object once');
              await secondOpenElement.dispatch('keydown', 'Escape');
              if (openCalls.length !== 1) throw new Error('unsupported key opened notification');
              await secondOpenElement.dispatch('keydown', 'Enter');
              await secondOpenElement.dispatch('keydown', ' ');
              if (openCalls.length !== 3 || openCalls.some((id) => id !== 17)) {{
                throw new Error('keyboard activation did not reuse the open handler exactly once');
              }}

              const closeEvent = await stack.closeElements[0].dispatch('click');
              if (closeCalls.length !== 1 || closeCalls[0] !== 17) throw new Error('close handler duplicated');
              if (!closeEvent.prevented || !closeEvent.propagationStopped) throw new Error('close event leaked into open handler');
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_notification_poll_replaces_previous_day_and_does_not_replay_on_reload(self) -> None:
        functions = "\n".join(_extract_function(self.source, name) for name in (
            "loadNotifications", "currentUnreadNotifications", "rememberUnreadNotificationIds",
            "enqueueNotificationToasts", "renderNotifications", "updateNotificationsBadge",
            "escapeHtml", "parseDate", "formatDateTime",
        ))
        _run_node(
            f"""
            const stack = {{ innerHTML: '', classList: {{ add() {{}}, remove() {{}} }}, querySelectorAll() {{ return []; }} }};
            const badges = Object.fromEntries(['notificationsBadge', 'mobileMoreBadge', 'mobileMoreNotificationsBadge'].map(id => [id, {{
              textContent: '', hidden: true,
              classList: {{ add() {{ badges[id].hidden = true; }}, remove() {{ badges[id].hidden = false; }} }},
            }}]));
            function $(id) {{ return id === 'notificationToasts' ? stack : badges[id]; }}
            let notifications = [], notificationToastIds = [], notificationSeenUnreadIds = new Set();
            let notificationsPanelOpen = false, notificationsBootstrapDone = false;
            let notificationsUnreadCount = 0, notificationsLoadPromise = null, lastBrowserNotificationAt = 0;
            const document = {{ hidden: false }}, window = {{}};
            let soundCalls = 0;
            const notificationLooksLikeMessage = item => item.type === 'new_message';
            const notificationLooksLikeQuestion = item => item.type === 'new_question';
            function playNotificationSound() {{ soundCalls++; }}
            function notify() {{ throw new Error('unexpected notification error'); }}
            console.warn = (...args) => {{ throw new Error(args.join(' ')); }};
            const yesterday = {{ id: 17, type: 'new_message', title: 'Yesterday', body: 'Old fixture', created_at: '2026-09-27T20:59:00Z', is_read: false }};
            const today = {{ id: 18, type: 'new_message', title: 'Today <context>', body: 'Ozon fixture', created_at: '2026-09-27T21:00:00Z', is_read: false }};
            let response = {{ items: [yesterday], unread_count: 1 }};
            async function api(path) {{
              if (path !== '/api/notifications?limit=30') throw new Error('client added a date filter');
              return response;
            }}
            {functions}
            (async () => {{
              await loadNotifications();
              if (stack.innerHTML || notificationToastIds.length || soundCalls) throw new Error('bootstrap replayed backlog');
              enqueueNotificationToasts([yesterday]);
              notificationsPanelOpen = true;
              renderNotifications();
              if (!stack.innerHTML.includes('data-notification-open="17"')) throw new Error('old-day setup failed');
              // The next successful server poll crosses midnight; the browser has no date predicate.
              response = {{ items: [today], unread_count: 1 }};
              await loadNotifications();
              if (notifications.length !== 1 || notifications[0].id !== 18) throw new Error('old feed remained');
              if (notificationToastIds.join(',') !== '18' || stack.innerHTML.includes('Yesterday')) throw new Error('old toast remained');
              if (!stack.innerHTML.includes('Today &lt;context&gt;') || !stack.innerHTML.includes('Ozon fixture')) throw new Error('context lost');
              if (Object.values(badges).some(b => b.textContent !== '1' || b.hidden)) throw new Error('badges disagree with server count');
              await loadNotifications();
              if (notificationToastIds.join(',') !== '18' || soundCalls !== 1) throw new Error('poll replayed duplicate');
              // Reload establishes a baseline and does not replay the server's unread backlog.
              notificationsBootstrapDone = false; notificationToastIds = []; notificationsPanelOpen = false;
              notificationSeenUnreadIds = new Set();
              await loadNotifications();
              if (stack.innerHTML || notificationToastIds.length || soundCalls !== 1) throw new Error('reload replayed backlog');
              response = {{ items: [], unread_count: 0 }};
              await loadNotifications();
              if (stack.innerHTML || notificationsPanelOpen || notificationsUnreadCount) throw new Error('empty day did not clear feed');
              if (Object.values(badges).some(b => b.textContent !== '0' || !b.hidden)) throw new Error('empty day did not clear badges');
            }})().catch(error => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_question_notification_keeps_first_poll_baseline_and_context(self) -> None:
        sound_key = _extract_function(self.source, "questionSoundKey")
        track = _extract_function(self.source, "trackQuestionSounds")

        _run_node(
            f"""
            let questionSoundBaselineDone = false;
            let knownQuestionSoundKeys = new Set();
            const soundCalls = [];
            const browserCalls = [];
            function questionNeedsAnswer() {{ return true; }}
            function questionProductName(question) {{ return question.product_name; }}
            function previewText(value) {{ return String(value || '').trim(); }}
            function crmNotificationUrl(kind, id) {{ return `/crm/${{kind}}/${{id}}`; }}
            function playNotificationSound(kind) {{ soundCalls.push(kind); }}
            function showCrmBrowserNotification(kind, title, body, options) {{
              browserCalls.push({{ kind, title, body, options }});
            }}

            {sound_key}
            {track}

            const existingQuestion = {{
              id: 101,
              product_name: 'Старый товар',
              text: 'Старый вопрос',
            }};
            const newQuestion = {{
              id: 202,
              product_name: 'Ozon Super Product',
              text: 'Когда будет доставка?',
            }};

            trackQuestionSounds([existingQuestion]);
            if (!questionSoundBaselineDone || knownQuestionSoundKeys.size !== 1) {{
              throw new Error('first poll did not establish the question baseline');
            }}
            if (soundCalls.length || browserCalls.length) {{
              throw new Error('first poll emitted a notification');
            }}

            trackQuestionSounds([existingQuestion, newQuestion]);
            if (soundCalls.length !== 1 || soundCalls[0] !== 'question') {{
              throw new Error('new question sound was not emitted exactly once');
            }}
            if (browserCalls.length !== 1) throw new Error('browser notification was not emitted exactly once');
            const call = browserCalls[0];
            if (call.kind !== 'question' || call.title !== 'Новый вопрос Ozon') {{
              throw new Error(`question notification title changed: ${{call.title}}`);
            }}
            if (!call.body.includes(newQuestion.product_name) || !call.body.includes(newQuestion.text)) {{
              throw new Error(`question notification lost product or preview context: ${{call.body}}`);
            }}
            if (call.options.entityId !== newQuestion.id
                || call.options.tag !== `arti-crm-question-${{newQuestion.id}}`
                || call.options.url !== `/crm/question/${{newQuestion.id}}`) {{
              throw new Error('question notification did not target the new question');
            }}
            """
        )

    def test_task_type_status_mapping_uses_existing_renderer_and_save_lifecycle(self) -> None:
        active_statuses = _extract_function(self.source, "activeChatStatuses")
        options = _extract_function(self.source, "taskTypeChatStatusOptions")
        label = _extract_function(self.source, "taskTypeStatusLabel")
        render = _extract_function(self.source, "renderTaskTypeSettingsList")
        save = _extract_function(self.source, "saveTaskTypeRow")
        escape = _extract_function(self.source, "escapeHtml")

        _run_node(
            f"""
            let taskTypes = [{{
              id: 7,
              title: 'Длинный тип задачи',
              comment_label: 'Подробное поле',
              sort_order: 4,
              is_active: true,
              chat_status_id: 12,
            }}];
            let chatSettings = {{ statuses: [
              {{ id: 11, key: 'new', title: 'Новый', is_active: 1 }},
              {{ id: 12, key: 'waiting_customer', title: 'Очень длинный статус ожидания покупателя', is_active: 1 }},
              {{ id: 13, key: 'disabled', title: 'Скрытый', is_active: 0 }},
            ] }};
            const list = {{ innerHTML: '' }};
            const createSelect = {{ value: '', innerHTML: '' }};
            function $(id) {{
              if (id === 'taskTypesSettingsList') return list;
              if (id === 'taskTypeChatStatus') return createSelect;
              return null;
            }}
            {escape}
            {active_statuses}
            {options}
            {label}
            {render}
            renderTaskTypeSettingsList();
            if (!list.innerHTML.includes('data-task-type-chat-status')) throw new Error('mapping select missing');
            if (!list.innerHTML.includes('value="12" selected')) throw new Error('saved mapping did not reload');
            if (list.innerHTML.includes('value="13"')) throw new Error('inactive status was offered');
            if (!createSelect.innerHTML.includes('Не менять статус чата')) throw new Error('null mapping option missing');

            const calls = [];
            let rejectSave = false;
            async function api(url, options) {{
              calls.push({{ url, body: JSON.parse(options.body) }});
              if (rejectSave) throw new Error('server validation');
              return {{ ok: true }};
            }}
            function rowWithMapping(value, isActive = true) {{
              const elements = {{
                '[data-task-type-title]': {{ value: 'Длинный тип задачи', focus() {{}} }},
                '[data-task-type-label]': {{ value: 'Подробное поле' }},
                '[data-task-type-chat-status]': {{ value }},
                '[data-task-type-sort]': {{ value: '4' }},
                '[data-task-type-active]': {{ checked: isActive }},
              }};
              return {{ dataset: {{ taskTypeId: '7' }}, querySelector(selector) {{ return elements[selector] || null; }} }};
            }}
            {save}
            (async () => {{
              await saveTaskTypeRow(rowWithMapping('12'));
              await saveTaskTypeRow(rowWithMapping(''));
              await saveTaskTypeRow(rowWithMapping('12', false));
              if (calls[0].body.chat_status_id !== 12) throw new Error('status mapping not saved');
              if (calls[1].body.chat_status_id !== null) throw new Error('null mapping not saved');
              if (calls[2].body.chat_status_id !== 12 || calls[2].body.is_active !== false) throw new Error('dormant mapping not saved atomically');
              rejectSave = true;
              let rejected = false;
              try {{ await saveTaskTypeRow(rowWithMapping('12')); }} catch (error) {{ rejected = error.message === 'server validation'; }}
              if (!rejected) throw new Error('server validation error was hidden');
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_extra_panel_geometry_is_coalesced_guarded_and_recomputed(self) -> None:
        cancel = _extract_function(self.source, "cancelExtraPanelGeometrySync")
        sync = _extract_function(self.source, "syncExtraPanelGeometry")
        schedule = _extract_function(self.source, "scheduleExtraPanelGeometrySync")
        bind_geometry = _extract_function(self.source, "bindExtraPanelGeometry")
        close_panel = _extract_function(self.source, "closeActiveExtraPanel")
        show_panel = _extract_function(self.source, "showExtraPanel")

        _run_node(
            f"""
            const EXTRA_PANEL_GAP_PX = 8;
            let activeExtraPanel = '';
            let extraPanelGeometryFrame = 0;
            let extraPanelResizeObserver = null;
            let nextFrameId = 1;
            const frames = new Map();
            const windowListeners = {{}};
            const viewportListeners = {{}};
            let resizeObserverInstances = 0;
            let resizeObserverInstance = null;
            let observedNodes = [];
            let headerBottom = 80;
            let headerReads = 0;
            let containingReads = 0;
            let composerReads = 0;

            function classList(initial = []) {{
              const values = new Set(initial);
              return {{
                add(...names) {{ names.forEach((name) => values.add(name)); }},
                remove(...names) {{ names.forEach((name) => values.delete(name)); }},
                contains(name) {{ return values.has(name); }},
                toggle(name, force) {{
                  if (force === undefined) force = !values.has(name);
                  if (force) values.add(name); else values.delete(name);
                  return force;
                }},
              }};
            }}

            const header = {{
              getBoundingClientRect() {{ headerReads += 1; return {{ bottom: headerBottom }}; }},
            }};
            const conversation = {{}};
            const chatPanel = {{
              dataset: {{}},
              querySelector(selector) {{ return selector === '.chat-header' ? header : null; }},
              closest(selector) {{ return selector === '.conversation' ? conversation : null; }},
              getBoundingClientRect() {{ containingReads += 1; return {{ top: 0, bottom: 600 }}; }},
            }};
            const composer = {{
              getBoundingClientRect() {{ composerReads += 1; return {{ top: 550 }}; }},
            }};
            const styleValues = new Map();
            const panel = {{
              classList: classList(['hidden']),
              offsetParent: chatPanel,
              style: {{
                getPropertyValue(name) {{ return styleValues.get(name) || ''; }},
                setProperty(name, value) {{ styleValues.set(name, value); }},
              }},
            }};
            const sections = {{
              tasksSection: {{ classList: classList(['hidden']) }},
              noteSection: {{ classList: classList(['hidden']) }},
              customerSection: {{ classList: classList(['hidden']) }},
            }};
            const elements = {{ chatPanel, extraPanel: panel, messageForm: composer, ...sections }};
            function $(id) {{ return elements[id] || null; }}
            function pauseMobileChatBackgroundWork() {{}}
            function toggleExtraMenu() {{}}
            const window = {{
              innerHeight: 700,
              visualViewport: {{
                offsetTop: 0,
                height: 700,
                addEventListener(name, handler) {{
                  (viewportListeners[name] ||= []).push(handler);
                }},
              }},
              requestAnimationFrame(callback) {{
                const id = nextFrameId++;
                frames.set(id, callback);
                return id;
              }},
              cancelAnimationFrame(id) {{ frames.delete(id); }},
              addEventListener(name, handler) {{
                (windowListeners[name] ||= []).push(handler);
              }},
            }};
            class ResizeObserver {{
              constructor(callback) {{
                this.callback = callback;
                resizeObserverInstance = this;
                resizeObserverInstances += 1;
              }}
              observe(node) {{ observedNodes.push(node); }}
            }}
            function flushFrame() {{
              const pending = [...frames.values()];
              frames.clear();
              pending.forEach((callback) => callback());
            }}

            {cancel}
            {sync}
            {schedule}
            {bind_geometry}
            {close_panel}
            {show_panel}

            bindExtraPanelGeometry();
            bindExtraPanelGeometry();
            if (resizeObserverInstances !== 1) throw new Error('duplicate ResizeObserver');
            if (observedNodes.length !== 4 || !observedNodes.includes(header)
                || !observedNodes.includes(chatPanel) || !observedNodes.includes(conversation)
                || !observedNodes.includes(composer)) throw new Error('wrong observed geometry nodes');
            if ((windowListeners.resize || []).length !== 1) throw new Error('duplicate window resize');
            if ((viewportListeners.resize || []).length !== 1
                || (viewportListeners.scroll || []).length !== 1) throw new Error('duplicate visual viewport listeners');

            showExtraPanel('tasks');
            showExtraPanel('note');
            scheduleExtraPanelGeometrySync();
            if (frames.size !== 1) throw new Error('geometry work was not coalesced');
            flushFrame();
            if (headerReads !== 1 || containingReads !== 1 || composerReads !== 1) {{
              throw new Error('geometry was read more than once per frame');
            }}
            if (styleValues.get('--extra-panel-top') !== '88px') throw new Error('wrong top');
            if (styleValues.get('--extra-panel-max-height') !== '454px') throw new Error('wrong max height');
            if (!sections.tasksSection.classList.contains('hidden')
                || sections.noteSection.classList.contains('hidden')) throw new Error('note panel path diverged');

            showExtraPanel('customer');
            flushFrame();
            if (sections.customerSection.classList.contains('hidden')) throw new Error('customer panel path diverged');
            showExtraPanel('customer');
            if (!panel.classList.contains('hidden') || frames.size !== 0) throw new Error('toggle close left geometry work');

            headerBottom = 120;
            showExtraPanel('tasks');
            flushFrame();
            if (styleValues.get('--extra-panel-top') !== '128px') throw new Error('reopen did not remeasure');

            headerBottom = 140;
            resizeObserverInstance.callback();
            resizeObserverInstance.callback();
            if (frames.size !== 1) throw new Error('ResizeObserver callbacks were not coalesced');
            flushFrame();
            if (styleValues.get('--extra-panel-top') !== '148px') throw new Error('ResizeObserver did not remeasure');

            scheduleExtraPanelGeometrySync();
            const staleFrame = [...frames.values()][0];
            const readsBeforeClose = headerReads + containingReads + composerReads;
            closeActiveExtraPanel();
            staleFrame();
            if (headerReads + containingReads + composerReads !== readsBeforeClose) {{
              throw new Error('closed panel performed stale layout reads');
            }}
            """
        )


if __name__ == "__main__":
    unittest.main()
