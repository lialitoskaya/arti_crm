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
    bundled = (
        Path(sys.executable).resolve().parent.parent / "node" / "bin" / executable_name
    )
    return str(bundled) if bundled.is_file() else None


NODE_EXECUTABLE = _find_node_executable()


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    opening_paren = source.index("(", start)
    paren_depth = 0
    quote = ""
    escaped = False
    index = opening_paren
    opening_brace = -1

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
        elif char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                opening_brace = source.index("{", index)
                break
        index += 1

    if opening_brace < 0:
        raise AssertionError(f"function body not found: {name}")

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
    if not NODE_EXECUTABLE:
        raise unittest.SkipTest("Node.js is not available")
    subprocess.run(
        [NODE_EXECUTABLE, "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


class ChatReadStateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.controller = _extract_function(cls.source, "createChatReadStateController")

    def test_stale_get_and_other_chat_cannot_overwrite_optimistic_state(self) -> None:
        _run_node(
            f"""
            {self.controller}
            let rows = [
              {{ id: 1, is_unread: true, is_marked_unread: false }},
              {{ id: 2, is_unread: false, is_marked_unread: false }},
            ];
            let resolveRequest;
            let requestCount = 0;
            const controller = createChatReadStateController({{
              request(chatId, isUnread) {{
                requestCount += 1;
                return new Promise((resolve) => {{ resolveRequest = resolve; }});
              }},
              read(chatId) {{ return rows.find((row) => Number(row.id) === Number(chatId)) || null; }},
              apply(chatId, state) {{
                rows = rows.map((row) => Number(row.id) === Number(chatId) ? {{ ...row, ...state }} : row);
              }},
            }});

            const beforeMutation = controller.captureRequestContext();
            const first = controller.set(1, false);
            const duplicate = controller.set(1, false);
            if (first !== duplicate) throw new Error('same operation did not use single-flight');
            if (requestCount !== 1) throw new Error('duplicate PATCH was started');
            if (rows[0].is_unread !== false) throw new Error('optimistic state missing');
            if (rows[1].is_unread !== false) throw new Error('other chat changed');

            const duringMutation = controller.captureRequestContext();
            const staleBefore = controller.reconcile({{ id: 1, is_unread: true }}, beforeMutation);
            if (staleBefore.is_unread !== false) throw new Error('old GET rolled state back');

            resolveRequest({{ chat_id: 1, is_unread: false, is_marked_unread: false }});
            first.then(() => {{
              const staleDuring = controller.reconcile({{ id: 1, is_unread: true }}, duringMutation);
              if (staleDuring.is_unread !== false) throw new Error('in-flight GET rolled state back');
              const other = controller.reconcile({{ id: 2, is_unread: true }}, beforeMutation);
              if (other.is_unread !== true) throw new Error('unrelated server chat was rewritten');
              return controller.set(1, false);
            }}).then(() => {{
              if (requestCount !== 1) throw new Error('already-read chat sent another PATCH');
            }}).catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )

    def test_error_and_mismatched_chat_id_roll_back_previous_state(self) -> None:
        _run_node(
            f"""
            {self.controller}
            let row = {{ id: 7, is_unread: true, is_marked_unread: false }};
            let mode = 'error';
            const controller = createChatReadStateController({{
              request() {{
                if (mode === 'error') return Promise.reject(new Error('failed'));
                return Promise.resolve({{ chat_id: 8, is_unread: false, is_marked_unread: false }});
              }},
              read() {{ return row; }},
              apply(chatId, state) {{
                if (Number(chatId) !== 7) throw new Error('wrong chat mutated');
                row = {{ ...row, ...state }};
              }},
            }});

            controller.set(7, false).catch(() => {{
              if (row.is_unread !== true) throw new Error('request error did not roll back');
              mode = 'mismatch';
              return controller.set(7, false);
            }}).then(
              () => {{ throw new Error('mismatched chat id was accepted'); }},
              () => {{
                if (row.is_unread !== true) throw new Error('mismatched response did not roll back');
              }},
            ).catch((error) => {{ console.error(error); process.exit(1); }});
            """
        )


if __name__ == "__main__":
    unittest.main()
