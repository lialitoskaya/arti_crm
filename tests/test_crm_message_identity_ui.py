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
        cls.author_label = _extract_function(cls.source, "crmMessageAuthorLabel")

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
