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
