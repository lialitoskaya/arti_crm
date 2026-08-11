from __future__ import annotations

import unittest
from pathlib import Path

from test_chat_read_state_ui import _extract_function, _run_node


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"


class CrmMessageIdentityUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.create_operation_id = _extract_function(cls.source, "createClientOperationId")
        cls.send_request = _extract_function(cls.source, "sendCurrentChatMessageRequest")
        cls.author_label = _extract_function(cls.source, "crmMessageAuthorLabel")

    def test_text_and_attachment_requests_send_the_same_operation_identity(self) -> None:
        _run_node(
            f"""
            {self.send_request}
            const calls = [];
            globalThis.api = async (url, options) => {{ calls.push({{ kind: 'json', url, options }}); return {{ ok: true }}; }};
            globalThis.apiForm = async (url, form) => {{
              calls.push({{ kind: 'form', url, operationId: form.get('operation_id') }});
              return {{ ok: true }};
            }};

            Promise.resolve()
              .then(() => sendCurrentChatMessageRequest(7, {{ text: 'hello', imageFiles: [], operationId: 'operation-stable-1' }}))
              .then(() => sendCurrentChatMessageRequest(7, {{ text: 'caption', imageFiles: [new Blob(['x'])], operationId: 'operation-stable-2' }}))
              .then(() => {{
                const jsonBody = JSON.parse(calls[0].options.body);
                if (jsonBody.operation_id !== 'operation-stable-1') throw new Error('text operation id missing');
                if (calls[1].operationId !== 'operation-stable-2') throw new Error('attachment operation id missing');
              }})
              .catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_submit_creates_one_operation_id_and_reuses_it_for_retry_payload(self) -> None:
        self.assertIn("const operationId = createClientOperationId();", self.source)
        self.assertIn(
            "sendCurrentChatMessageWithRetry(chatIdForSend, { text, imageFiles, operationId })",
            self.source,
        )
        retry = _extract_function(self.source, "sendCurrentChatMessageWithRetry")
        self.assertIn("sendCurrentChatMessageRequest(chatId, payload)", retry)
        self.assertNotIn("createClientOperationId", retry)

    def test_employee_label_requires_canonical_crm_provenance(self) -> None:
        _run_node(
            f"""
            {self.author_label}
            const label = 'Liya';
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: 1, crm_author_label: label }}) !== label) {{
              throw new Error('numeric CRM provenance did not render canonical label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: true, crm_author_label: label }}) !== label) {{
              throw new Error('boolean CRM provenance did not render canonical label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: 0, crm_author_label: label }}) !== '') {{
              throw new Error('numeric false CRM provenance rendered employee label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: false, crm_author_label: label }}) !== '') {{
              throw new Error('boolean false CRM provenance rendered employee label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', crm_author_label: label }}) !== '') {{
              throw new Error('missing CRM provenance rendered employee label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: '1', crm_author_label: label }}) !== '') {{
              throw new Error('string CRM provenance was treated as canonical');
            }}
            if (crmMessageAuthorLabel({{ direction: 'inbound', is_crm_sent: 1, crm_author_label: label }}) !== '') {{
              throw new Error('inbound message showed employee label');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: 1, author: label }}) !== '') {{
              throw new Error('generic author field was treated as CRM provenance');
            }}
            if (crmMessageAuthorLabel({{ direction: 'outbound', is_crm_sent: 1, crm_author_label: '   ' }}) !== '') {{
              throw new Error('blank canonical label was rendered');
            }}
            """
        )


if __name__ == "__main__":
    unittest.main()
