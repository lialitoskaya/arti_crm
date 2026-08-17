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

    def test_status_labels_execute_for_durable_states(self) -> None:
        _run_node(
            f"""
            {self.status_contract}
            const expected = {{
              pending: 'В очереди',
              sending: 'Отправляется',
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
