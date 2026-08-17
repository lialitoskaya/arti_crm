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
        cls.author_label = _extract_function(cls.source, "crmMessageAuthorLabel")

    def test_text_and_attachment_requests_use_separate_stable_operation_ids(self) -> None:
        self.assertIn(
            "operation_id: operationId,\n        intent_origin: intentOrigin",
            self.source,
        )
        self.assertIn(
            "formData.append('operation_id', operationId);",
            self.source,
        )
        self.assertNotIn("formData.append('caption'", self.source)
        caption_position = self.source.index(
            "intentOrigin: imageFiles.length ? 'attachment_caption' : 'message'"
        )
        upload_position = self.source.index("await uploadCurrentChatImages", caption_position)
        self.assertLess(caption_position, upload_position)

    def test_submit_creates_stable_command_and_attachment_ids_without_client_retry(self) -> None:
        self.assertIn(
            "const messageOperationId = text ? createClientOperationId() : null;",
            self.source,
        )
        self.assertIn(
            "const attachmentOperationId = imageFiles.length ? createClientOperationId() : null;",
            self.source,
        )
        self.assertNotIn("sendCurrentChatMessageWithRetry", self.source)
        self.assertIn("waitForMessageSendOperation", self.source)

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
