from __future__ import annotations

import unittest
from pathlib import Path

from test_chat_read_state_ui import _extract_function, _run_node


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"


def _extract_range(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    end = source.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0 or start >= end:
        raise AssertionError(
            f"invalid JavaScript range: {start_marker!r} -> {end_marker!r}"
        )
    return source[start:end]


class MessageSendOutboxUiBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.status_contract = _extract_range(
            cls.source,
            "const MESSAGE_OPERATION_STATUS_LABELS",
            "async function getMessageSendOperation",
        )
        cls.dispatch = _extract_range(
            cls.source,
            "async function dispatchComposerCommands",
            "function parseDate",
        )
        cls.upload = _extract_range(
            cls.source,
            "async function uploadCurrentChatImages",
            "function assertMessageOperationAllowsAttachmentUpload",
        )
        cls.attachment_gate = _extract_function(
            cls.source, "assertMessageOperationAllowsAttachmentUpload"
        )
        cls.merge = _extract_function(cls.source, "mergeMessagesWithSendOperations")
        cls.render_messages = _extract_function(cls.source, "renderMessages")
        cls.author_label = _extract_function(cls.source, "crmMessageAuthorLabel")

    def test_status_labels_execute_for_durable_states(self) -> None:
        _run_node(
            f"""
            {self.status_contract}
            const expected = {{
              pending: 'В очереди',
              sending: 'Отправляется',
              retry_wait: 'Ожидает повторной попытки',
              accepted: 'Принято, ожидается подтверждение',
              uncertain: 'Результат отправки неизвестен',
              permanent_failed: 'Отправка отклонена',
            }};
            for (const [status, label] of Object.entries(expected)) {{
              if (messageOperationStatusLabel(status) !== label) {{
                throw new Error(`wrong label for ${{status}}`);
              }}
            }}
            """
        )

    def test_render_places_one_current_status_inside_bubble_and_author_outside(self) -> None:
        _run_node(
            f"""
            class FakeClassList {{
              constructor(owner) {{ this.owner = owner; }}
              _values() {{ return this.owner.className.split(/[ ]+/).filter(Boolean); }}
              add(...names) {{ this.owner.className = [...new Set([...this._values(), ...names])].join(' '); }}
              remove(...names) {{
                const rejected = new Set(names);
                this.owner.className = this._values().filter((name) => !rejected.has(name)).join(' ');
              }}
              contains(name) {{ return this._values().includes(name); }}
              toggle(name) {{
                if (this.contains(name)) {{ this.remove(name); return false; }}
                this.add(name); return true;
              }}
            }}
            class FakeElement {{
              constructor(tagName = 'div') {{
                this.tagName = String(tagName).toUpperCase();
                this.className = '';
                this.children = [];
                this.dataset = {{}};
                this.attributes = {{}};
                this.classList = new FakeClassList(this);
                this._innerHTML = '';
                this.textContent = '';
              }}
              appendChild(child) {{ child.parentNode = this; this.children.push(child); return child; }}
              set innerHTML(value) {{ this._innerHTML = String(value); this.children = []; }}
              get innerHTML() {{ return this._innerHTML; }}
              setAttribute(name, value) {{ this.attributes[name] = String(value); }}
              querySelectorAll(selector) {{
                const className = selector.startsWith('.') ? selector.slice(1) : '';
                const matches = [];
                const visit = (node) => {{
                  for (const child of node.children) {{
                    if (className && child.classList.contains(className)) matches.push(child);
                    visit(child);
                  }}
                }};
                visit(this);
                return matches;
              }}
              querySelector(selector) {{ return this.querySelectorAll(selector)[0] || null; }}
            }}

            const box = new FakeElement('div');
            const document = {{
              createElement: (tagName) => new FakeElement(tagName),
              querySelectorAll: () => [],
            }};
            const $ = (id) => id === 'messages' ? box : null;
            let selectedAiMessageId = 0;
            let activeExtraPanel = null;
            let currentChat = {{messages: []}};
            const closeActiveExtraPanel = () => {{}};
            const renderAiSelectionBar = () => {{}};
            const generateAiReplyForSelected = async () => {{}};
            const startInternalNoteEdit = () => {{}};
            const deleteInternalNote = async () => {{}};
            const buildMessageReceiptContext = () => ({{}});
            const ozonProductContext = (message) => message.productContext || null;
            const createOzonProductContextCard = () => {{
              const card = document.createElement('a');
              card.className = 'message-product-context';
              return card;
            }};
            const extractImageUrls = (message) => message.images || [];
            const cleanMessageTextForDisplay = (text) => String(text || '');
            const renderMessageTextWithLinks = (element, message, text) => {{ element.textContent = text; }};
            const imagePreviewSrc = (url) => url;
            const prepareLazyChatImage = (image, url) => {{ image.src = url; }};
            const formatDateTime = (value) => `date:${{value}}`;
            const formatMessageTime = (value) => value ? `time:${{value}}` : '';
            const escapeHtml = (value) => String(value);
            const outboundReceiptState = (message) => message.receiptState || 'sent';
            const messageReceiptInfo = (message) => {{
              if (message.direction !== 'outbound') return null;
              const read = message.receiptState === 'read';
              return {{icon: read ? '✓✓' : '✓', label: read ? 'прочитано' : 'отправлено', read, title: read ? 'Прочитано' : 'Отправлено'}};
            }};
            {self.status_contract}
            {self.author_label}
            {self.render_messages}

            const statuses = ['pending', 'sending', 'retry_wait', 'accepted', 'uncertain', 'permanent_failed'];
            const messages = statuses.map((status, index) => ({{
              id: index + 1,
              direction: 'outbound',
              created_at: `2026-08-24T10:0${{index}}:00Z`,
              text: `operation-${{status}}`,
              _send_operation_status: status,
              _send_operation_error: {{summary: `summary-${{status}}`}},
              receiptState: 'read',
            }}));
            messages.push(
              {{id: 20, direction: 'outbound', created_at: '2026-08-24T11:00:00Z', text: 'read', receiptState: 'read'}},
              {{id: 21, direction: 'outbound', created_at: '2026-08-24T11:01:00Z', text: 'author', is_crm_sent: 1, crm_author_label: 'Лия'}},
              {{id: 22, direction: 'outbound', created_at: '2026-08-24T11:02:00Z', text: 'spoof', is_crm_sent: 0, crm_author_label: 'Подмена'}},
              {{id: 23, direction: 'internal', created_at: '2026-08-24T11:03:00Z', text: 'note', author: 'Лия'}},
              {{id: 24, direction: 'outbound', created_at: '2026-08-24T11:04:00Z', text: 'assets', images: ['https://example.test/image.jpg'], productContext: {{sku: '1'}}}},
            );

            renderMessages(messages);
            const rows = box.children;
            if (rows.length !== messages.length) throw new Error('render changed the logical row count');

            for (let index = 0; index < statuses.length; index += 1) {{
              const row = rows[index];
              const bubble = row.querySelector('.message-bubble');
              const meta = row.querySelector('.message-bubble-meta');
              if (!meta || meta.parentNode !== bubble) throw new Error(`metadata outside bubble for ${{statuses[index]}}`);
              if (meta.querySelectorAll('.message-time').length !== 1) throw new Error(`timestamp missing for ${{statuses[index]}}`);
              const receipts = meta.querySelectorAll('.message-receipt');
              if (receipts.length !== 1) throw new Error(`expected one status for ${{statuses[index]}}`);
              const receipt = receipts[0];
              if (!receipt.classList.contains(`status-${{statuses[index]}}`)) throw new Error(`wrong status class for ${{statuses[index]}}`);
              if (receipt.textContent !== messageOperationStatusLabel(statuses[index])) throw new Error(`wrong status label for ${{statuses[index]}}`);
              if (receipt.innerHTML.includes('отправлено')) throw new Error(`receipt fallback masked ${{statuses[index]}}`);
              if (row.querySelector('.message-footer')) throw new Error(`empty footer rendered for ${{statuses[index]}}`);
            }}

            const canonicalMeta = rows[6].querySelector('.message-bubble-meta');
            const canonicalReceipt = canonicalMeta.querySelector('.message-receipt');
            if (!canonicalReceipt || !canonicalReceipt.classList.contains('is-read')) throw new Error('canonical read receipt missing');
            if (!canonicalReceipt.innerHTML.includes('прочитано')) throw new Error('canonical receipt label missing');

            const trustedFooter = rows[7].querySelector('.message-footer');
            if (!trustedFooter || trustedFooter.children.length !== 1) throw new Error('trusted author footer missing or contains metadata');
            if (trustedFooter.parentNode !== rows[7]) throw new Error('trusted author footer is not a separate row below the bubble');
            if (!trustedFooter.querySelector('.message-crm-author')) throw new Error('trusted author label missing');
            if (trustedFooter.querySelector('.message-time') || trustedFooter.querySelector('.message-receipt')) throw new Error('message metadata leaked into author footer');
            const sentReceipt = rows[7].querySelector('.message-bubble-meta')?.querySelector('.message-receipt');
            if (!sentReceipt || !sentReceipt.classList.contains('is-sent') || !sentReceipt.innerHTML.includes('отправлено')) {{
              throw new Error('canonical sent receipt missing from bubble');
            }}
            if (rows[8].querySelector('.message-footer')) throw new Error('untrusted author created a footer');

            const internal = rows[9];
            if (!internal.querySelector('.message-meta')) throw new Error('internal-note metadata changed');
            if (internal.querySelector('.message-bubble-meta')) throw new Error('internal note received outbound metadata');

            const assets = rows[10];
            if (!assets.querySelector('.message-product-context')) throw new Error('product context disappeared');
            if (!assets.querySelector('.message-images')) throw new Error('attachment gallery disappeared');

            renderMessages(messages);
            if (box.children.length !== messages.length) throw new Error('repeat render duplicated rows');
            for (const row of box.children.filter((item) => !item.classList.contains('internal'))) {{
              if (row.querySelectorAll('.message-bubble-meta').length !== 1) throw new Error('repeat render duplicated metadata');
            }}
            """
        )

    def test_merge_renders_one_transient_or_canonical_logical_row(self) -> None:
        _run_node(
            f"""
            const messageTimestampMs = (item) => Number(item?.sort || 0);
            {self.merge}
            const canonical = [{{
              id: 10,
              sort: 10,
              client_operation_id: 'already-canonical',
              text: 'Canonical',
            }}];
            const operations = [
              {{id: 1, sort: 1, client_operation_id: 'already-canonical', status: 'accepted', text: 'Duplicate'}},
              {{id: 2, sort: 2, client_operation_id: 'transient', status: 'pending', text: 'Transient'}},
              {{id: 3, sort: 3, client_operation_id: 'confirmed', status: 'confirmed', text: 'Confirmed'}},
              {{id: 4, sort: 4, client_operation_id: 'linked', status: 'accepted', canonical_message_id: 99, text: 'Linked'}},
            ];
            const rows = mergeMessagesWithSendOperations(canonical, operations);
            if (rows.length !== 2) throw new Error(`expected two logical rows, got ${{rows.length}}`);
            if (rows.filter((item) => item.client_operation_id === 'already-canonical').length !== 1) {{
              throw new Error('canonical operation was rendered twice');
            }}
            const transient = rows.find((item) => item.client_operation_id === 'transient');
            if (!transient || transient._send_operation_status !== 'pending') {{
              throw new Error('pending transient row missing');
            }}
            """
        )

    def test_caption_waits_for_acceptance_and_terminal_failures_block_upload(self) -> None:
        _run_node(
            f"""
            (async () => {{
              {self.status_contract}
              {self.attachment_gate}
              {self.dispatch}
              let releaseSend;
              let uploadCalls = 0;
              let order = [];
              async function sendChatTextOperation() {{
                order.push('send');
                return new Promise((resolve) => {{ releaseSend = () => resolve({{status: 'accepted'}}); }});
              }}
              async function uploadCurrentChatImages() {{
                uploadCalls += 1;
                order.push('upload');
              }}
              const options = {{
                text: 'Caption',
                imageFiles: [{{name: 'one.png'}}],
                messageOperationId: 'caption-op',
                attachmentOperationId: 'attachment-op',
                areFilesAvailable: () => true,
              }};
              const pending = dispatchComposerCommands(1, options);
              await new Promise((resolve) => setImmediate(resolve));
              if (uploadCalls !== 0) throw new Error('upload started before caption outcome');
              releaseSend();
              await pending;
              if (order.join(',') !== 'send,upload') throw new Error(`wrong order: ${{order}}`);

              sendChatTextOperation = async () => ({{status: 'confirmed'}});
              await dispatchComposerCommands(1, options);
              if (uploadCalls !== 2) throw new Error('confirmed caption did not allow upload');

              for (const status of ['uncertain', 'permanent_failed']) {{
                sendChatTextOperation = async () => ({{status}});
                const before = uploadCalls;
                let rejected = false;
                try {{
                  await dispatchComposerCommands(1, options);
                }} catch (error) {{
                  rejected = true;
                }}
                if (!rejected) throw new Error(`${{status}} did not reject file upload`);
                if (uploadCalls !== before) throw new Error(`${{status}} started file upload`);
              }}
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_attachment_multipart_contains_files_and_operation_id_but_no_caption(self) -> None:
        _run_node(
            f"""
            (async () => {{
              {self.upload}
              let captured = null;
              class FakeFormData {{
                constructor() {{ this.values = []; }}
                append(name, value) {{ this.values.push([name, value]); }}
              }}
              globalThis.FormData = FakeFormData;
              async function apiForm(url, formData) {{
                captured = {{url, values: formData.values}};
                return {{ok: true}};
              }}
              await uploadCurrentChatImages(42, [{{name: 'one.png'}}, {{name: 'two.png'}}], 'attachment-op');
              const names = captured.values.map(([name]) => name);
              if (captured.url !== '/api/chats/42/attachments') throw new Error('wrong attachment URL');
              if (names.filter((name) => name === 'images').length !== 2) throw new Error('images missing');
              if (!names.includes('operation_id')) throw new Error('operation ID missing');
              if (names.includes('caption')) throw new Error('caption leaked into multipart');
            }})().catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )


if __name__ == "__main__":
    unittest.main()
