from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"


def _find_node_executable() -> str | None:
    configured = os.environ.get("NODE_EXECUTABLE") or shutil.which("node")
    if configured:
        return configured
    executable_name = "node.exe" if os.name == "nt" else "node"
    bundled = Path(sys.executable).resolve().parent.parent / "node" / "bin" / executable_name
    return str(bundled) if bundled.is_file() else None


NODE_EXECUTABLE = _find_node_executable()


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    opening_brace = source.index("{", source.index(")", start))
    depth = 0
    quote = ""
    escaped = False
    index = opening_brace
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
        index += 1
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(script: str) -> None:
    if not NODE_EXECUTABLE:
        raise unittest.SkipTest("Node.js is not available")
    subprocess.run(
        [NODE_EXECUTABLE, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


class CrmMessageAuthorLabelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.helper = _extract_function(cls.source, "crmMessageAuthorLabel")

    def test_label_is_shown_only_for_real_crm_outbound(self) -> None:
        _run_node(
            f"""
            {self.helper}
            const crm = crmMessageAuthorLabel({{
              direction: 'outbound',
              is_crm_sent: 1,
              crm_author_label: 'Лия',
              raw: {{ _crm_sent_from_crm: true }},
            }});
            if (crm !== 'Лия') throw new Error('CRM employee label missing');

            const marketplace = crmMessageAuthorLabel({{
              direction: 'outbound',
              raw: {{ marketplace_payload: true }},
            }});
            if (marketplace !== '') throw new Error('marketplace outbound faked CRM author');

            const inbound = crmMessageAuthorLabel({{
              direction: 'inbound',
              is_crm_sent: 1,
              crm_author_label: 'Лия',
              raw: {{ _crm_sent_from_crm: true }},
            }});
            if (inbound !== '') throw new Error('inbound message showed CRM author');

            const technical = crmMessageAuthorLabel({{
              direction: 'outbound',
              raw: {{ _crm_sent_from_crm: true, _crm_sent_by_label: 'seller' }},
            }});
            if (technical !== '') throw new Error('frontend bypassed canonical backend label');
            """
        )

    def test_render_places_employee_name_in_outbound_footer(self) -> None:
        self.assertIn("authorEl.className = 'message-crm-author'", self.source)
        self.assertIn("footer.appendChild(authorEl)", self.source)
        self.assertIn("crmMessageAuthorLabel(message)", self.source)


if __name__ == "__main__":
    unittest.main()
