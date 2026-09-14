from __future__ import annotations

import unittest
from pathlib import Path

from test_chat_read_state_ui import _extract_function, _run_node


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"
STYLES_PATH = ROOT / "app" / "static" / "styles.css"


class ChatPinStateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.styles = STYLES_PATH.read_text(encoding="utf-8")
        cls.controller = _extract_function(cls.source, "createChatPinStateController")

    def test_search_open_state_releases_reserved_space_on_close_and_clear(self) -> None:
        current_search = _extract_function(self.source, "currentChatMessageSearch")
        update_search = _extract_function(self.source, "updateChatSearchUi")
        set_search_open = _extract_function(self.source, "setChatSearchOpen")
        clear_search = _extract_function(self.source, "clearChatMessageSearch")

        _run_node(
            f"""
            {current_search}
            {update_search}
            {set_search_open}
            {clear_search}

            function classes(initial = []) {{
              const values = new Set(initial);
              return {{
                contains(name) {{ return values.has(name); }},
                toggle(name, force) {{
                  const enabled = force === undefined ? !values.has(name) : Boolean(force);
                  if (enabled) values.add(name); else values.delete(name);
                  return enabled;
                }},
              }};
            }}

            const filters = {{ classList: classes() }};
            const box = {{
              classList: classes(['hidden']),
              closest(selector) {{ return selector === '.filters' ? filters : null; }},
            }};
            const input = {{ value: '', focus() {{}}, select() {{}} }};
            const toggle = {{
              classList: classes(),
              setAttribute(name, value) {{ this[name] = value; }},
            }};
            const clearButton = {{ classList: classes(['hidden']) }};
            const elements = {{
              chatSearchBox: box,
              chatSearchInput: input,
              chatSearchToggleBtn: toggle,
              chatSearchClearBtn: clearButton,
            }};
            function $(id) {{ return elements[id] || null; }}

            let chatMessageSearch = '';
            let chatSearchTimer = null;
            let dateCloseCount = 0;
            let loadCount = 0;
            function setDateRangePopover(open) {{
              if (open) throw new Error('search opened the date popover');
              dateCloseCount += 1;
            }}
            function resetChatListFeed() {{}}
            function loadChats() {{ loadCount += 1; return Promise.resolve([]); }}
            function notify() {{}}
            function clearTimeout() {{}}
            const window = {{ setTimeout(callback) {{ callback(); }} }};

            setChatSearchOpen(true);
            if (!filters.classList.contains('chat-search-open')) throw new Error('open state missing');
            if (toggle['aria-expanded'] !== 'true') throw new Error('expanded state missing');
            if (dateCloseCount !== 1) throw new Error('date popover was not closed once');

            input.value = 'needle';
            updateChatSearchUi();
            setChatSearchOpen(false);
            if (filters.classList.contains('chat-search-open')) throw new Error('close left reserved space');
            if (toggle['aria-expanded'] !== 'false') throw new Error('collapsed state missing');
            if (!toggle.classList.contains('active')) throw new Error('active query state was lost');

            setChatSearchOpen(true);
            clearChatMessageSearch();
            if (filters.classList.contains('chat-search-open')) throw new Error('clear left reserved space');
            if (!box.classList.contains('hidden')) throw new Error('clear left search visible');
            if (input.value !== '') throw new Error('clear left query text');
            if (loadCount !== 1) throw new Error('clear did not refresh the list once');
            """
        )

    def test_pin_controller_is_single_flight_and_protects_against_stale_get(self) -> None:
        _run_node(
            f"""
            {self.controller}
            let rows = [{{ id: 1, is_pinned: false, pinned_at: null }}];
            let resolveRequest;
            let requestCount = 0;
            const controller = createChatPinStateController({{
              request() {{
                requestCount += 1;
                return new Promise((resolve) => {{ resolveRequest = resolve; }});
              }},
              read() {{ return rows[0]; }},
              apply(chatId, state) {{ rows[0] = {{ ...rows[0], ...state }}; }},
            }});

            const beforeMutation = controller.captureRequestContext();
            const first = controller.set(1, true);
            const duplicate = controller.set(1, true);
            if (first !== duplicate) throw new Error('pin operation did not use single-flight');
            if (requestCount !== 1) throw new Error('duplicate pin PATCH was started');
            if (!rows[0].is_pinned) throw new Error('optimistic pin missing');

            const stale = controller.reconcile({{ id: 1, is_pinned: false }}, beforeMutation);
            if (!stale.is_pinned) throw new Error('stale GET removed optimistic pin');

            resolveRequest({{ chat_id: 1, is_pinned: true, pinned_at: '2026-08-05T12:00:00Z' }});
            first.then(() => {{
              if (!rows[0].is_pinned) throw new Error('canonical pin was not applied');
              return controller.set(1, true);
            }}).then(() => {{
              if (requestCount !== 1) throw new Error('already pinned chat sent another PATCH');
            }}).catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_pin_error_rolls_back_previous_state(self) -> None:
        _run_node(
            f"""
            {self.controller}
            let row = {{ id: 7, is_pinned: false, pinned_at: null }};
            const controller = createChatPinStateController({{
              request() {{ return Promise.reject(new Error('failed')); }},
              read() {{ return row; }},
              apply(chatId, state) {{ row = {{ ...row, ...state }}; }},
            }});

            controller.set(7, true).then(
              () => {{ throw new Error('failed request was accepted'); }},
              () => {{
                if (row.is_pinned) throw new Error('pin error did not roll back');
              }},
            ).catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_pin_is_before_marketplace_badge_and_uses_delegated_events(self) -> None:
        render = _extract_function(self.source, "renderChatList")
        bind = _extract_function(self.source, "bindChatListInfiniteScroll")

        self.assertLess(render.index("data-chat-pin-state"), render.index('<span class="badge'))
        self.assertIn("list.addEventListener('click', handleChatListClick)", bind)
        self.assertNotIn("addEventListener", render)
        self.assertIn("#chatsView .chat-pin-control", self.styles)
        self.assertIn("#chatsView .chat-item.is-pinned .chat-pin-control", self.styles)


if __name__ == "__main__":
    unittest.main()
