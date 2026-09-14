"""Behavioral guard for the chat/task visual restoration; layout uses browser QA."""

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_chat_read_state_ui import _extract_function, _run_node


ROOT = Path(__file__).resolve().parents[1]


def _chat_runtime() -> str:
    source = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    names = (
        "renderChatList", "bindChatListInfiniteScroll", "paintChatRowReadState",
        "escapeHtml", "previewText",
    )
    # The shared brace scanner does not parse regex literals containing quotes.
    image_helper = source[source.index("function isImagePlaceholderText("):source.index("function previewText(")]
    return image_helper + "\n".join(_extract_function(source, name) for name in names) + r"""
const assert = require('node:assert/strict');
function element() {
  return {
    dataset: {}, children: [], listeners: [], attributes: {}, markup: '', className: '',
    set innerHTML(value) { this.markup = value; this.children = []; },
    get innerHTML() { return this.markup; },
    appendChild(child) { this.children.push(...(child.fragment ? child.children : [child])); },
    addEventListener(...args) { this.listeners.push(args); },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}
const list = element();
const document = {
  createElement: element,
  createDocumentFragment() { return { fragment: true, children: [], appendChild(child) { this.children.push(child); } }; },
};
function $(id) { return id === 'chatList' ? list : null; }
let chatListInfiniteScrollBound = false;
function handleChatListClick() {}
function handleChatListKeydown() {}
function scheduleChatListInfiniteLoad() {}
let countUpdates = 0;
function updateChatCountLabel() { countUpdates += 1; }
let mobileOpen = false;
function isMobileChatOpen() { return mobileOpen; }
let search = '';
function currentChatMessageSearch() { return search; }
function isClosedWorkflowStatus(status) { return status === 'closed'; }
function shouldShowWaitingMarker(chat) { return Boolean(chat.waiting); }
function waitingResponseBadge() { return ''; }
function formatChatTime() { return '12:30'; }
function customerLabel(chat) { return chat.customer_name; }
function statusBadge() { return '<span class="status-badge">Открыт</span>'; }
const marketplaceNames = { ozon: 'Ozon' };
const requestAnimationFrame = callback => callback();
let currentChatId = 1;
let chatScope = 'active';
let chats = [
  { id: 1, marketplace: 'ozon', customer_name: 'Synthetic <customer> "long"', last_message_preview: '<script>alert(1)</script> ' + 'long '.repeat(100), is_unread: true, is_pinned: true, waiting: true },
  { id: 2, marketplace: 'ozon', customer_name: 'Synthetic read', last_message_preview: 'Read preview', is_unread: false, is_pinned: false },
];
"""


def test_real_chat_render_preserves_states_escaping_and_delegated_events() -> None:
    _run_node(_chat_runtime() + r"""
renderChatList({ preserveScrollTop: 117 });
assert.equal(list.children.length, 2);
assert.equal(list.scrollTop, 117);
const unread = list.children[0];
assert.match(unread.className, /active.*needs-response.*is-unread.*is-pinned/);
assert.deepEqual(unread.dataset, { chatId: '1', unread: '1', pinned: '1' });
assert.match(unread.innerHTML, /class="chat-read-state-checks" aria-hidden="true"/);
assert.doesNotMatch(unread.innerHTML, /chat-read-state-dot|<script>|<customer>/);
assert.match(unread.innerHTML, /&lt;script&gt;/);
assert.match(unread.innerHTML, /&quot;long&quot;/);
assert.match(unread.innerHTML, /data-chat-read-state[^>]*tabindex="-1"[^>]*aria-pressed="true"/);
assert.match(unread.innerHTML, /data-chat-pin-state[^>]*aria-label="Открепить чат"[^>]*aria-pressed="true"/);
assert.match(list.children[1].innerHTML, /data-chat-read-state[^>]*tabindex="0"[^>]*aria-pressed="false"/);
assert.match(list.children[1].innerHTML, /aria-label="Закрепить чат"/);
assert.equal(unread.listeners.length, 0);
renderChatList();
assert.equal(list.children.length, 2);
assert.deepEqual(list.listeners.map(args => args[0]), ['click', 'keydown', 'scroll']);
assert.equal(list.listeners[0][1], handleChatListClick);
assert.equal(list.listeners[1][1], handleChatListKeydown);
assert.deepEqual(list.listeners[2][2], { passive: true });
const rendered = list.children;
mobileOpen = true;
renderChatList();
assert.equal(list.children, rendered);
renderChatList({ force: true });
assert.notEqual(list.children, rendered);
assert.equal(list.listeners.length, 3);
""")


def test_real_chat_render_search_empty_and_read_repaint_contract() -> None:
    _run_node(_chat_runtime() + r"""
search = '<needle>';
chats[0].search_match_text = '<needle> & synthetic match';
renderChatList();
assert.match(list.children[0].innerHTML, /chat-search-match-preview/);
assert.match(list.children[0].innerHTML, /Найдено: &lt;needle&gt; &amp; synthetic match/);
const row = list.children[0];
const control = element();
row.querySelector = selector => selector === '[data-chat-read-state]' ? control : null;
row.classList = { toggle(name, enabled) {
  const classes = new Set(row.className.split(/\s+/).filter(Boolean));
  if (enabled) classes.add(name); else classes.delete(name);
  row.className = [...classes].join(' ');
} };
const markup = row.innerHTML;
paintChatRowReadState(row, { is_unread: false });
assert.doesNotMatch(row.className, /is-unread/);
assert.match(row.className, /is-pinned/);
assert.equal(row.dataset.unread, '0');
assert.equal(control.tabIndex, 0);
assert.equal(control.attributes['aria-pressed'], 'false');
assert.equal(control.attributes['aria-label'], 'Отметить чат непрочитанным');
paintChatRowReadState(row, { is_unread: true });
assert.match(row.className, /is-unread/);
assert.equal(control.tabIndex, -1);
assert.equal(control.attributes['aria-pressed'], 'true');
assert.equal(row.innerHTML, markup);
chats = [];
renderChatList();
assert.match(list.innerHTML, /&lt;needle&gt;.*ничего не найдено/);
assert.doesNotMatch(list.innerHTML, /<needle>/);
search = '';
chatScope = 'archive';
renderChatList();
assert.match(list.innerHTML, /В архиве пока нет закрытых чатов/);
""")


def test_html_asset_versions_match_the_executed_service_worker() -> None:
    versions: dict[str, str] = {}

    class AssetParser(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if tag == "meta" and values.get("name") == "arti-build-version":
                assert "build" not in versions
                versions["build"] = values.get("content") or ""
            url = urlsplit(values.get("href") or values.get("src") or "")
            if url.path in {"/static/app.js", "/static/styles.css"}:
                assert url.path not in versions
                versions[url.path] = parse_qs(url.query)["v"][0]

    AssetParser().feed((ROOT / "app/static/index.html").read_text(encoding="utf-8"))
    assert set(versions) == {"build", "/static/app.js", "/static/styles.css"}
    assert len(set(versions.values())) == 1
    assert versions["build"]
    worker = (ROOT / "app/static/sw.js").read_text(encoding="utf-8")
    _run_node(
        "const self = { addEventListener() {} };\n"
        + worker
        + "\nrequire('node:assert/strict').equal(ARTI_CRM_SW_VERSION, "
        + json.dumps(versions["build"])
        + ");"
    )
