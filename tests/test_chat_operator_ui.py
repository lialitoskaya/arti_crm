from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    opening_brace = source.index("{", start)
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


if __name__ == "__main__":
    unittest.main()
