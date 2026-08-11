from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
SERVICE_WORKER = (ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")


class ReplyTemplatesUiTests(unittest.TestCase):
    def test_template_card_renders_title_without_update_metadata(self) -> None:
        render_start = APP_JS.index("function renderReplyTemplates()")
        render_end = APP_JS.index("async function loadReplyTemplates", render_start)
        render = APP_JS[render_start:render_end]

        self.assertIn("reply-template-item-title", render)
        self.assertIn("template.title || 'Без названия'", render)
        self.assertNotIn("reply-template-item-meta", render)
        self.assertNotIn("обновлён", render)
        self.assertNotIn("template.updated_at", render)

    def test_actions_are_compact_borderless_vertical_icons(self) -> None:
        self.assertIn("flex-direction: column", STYLES)
        self.assertIn("button.reply-template-action", STYLES)
        self.assertIn("border: 0 !important", STYLES)
        self.assertIn("background: transparent !important", STYLES)
        self.assertIn("reply-template-edit-action", APP_JS)
        self.assertIn("reply-template-delete-action", APP_JS)
        self.assertIn('<svg viewBox="0 0 24 24"', APP_JS)

    def test_template_title_is_visible_and_preview_is_limited(self) -> None:
        title_start = STYLES.index(".reply-template-item-title {")
        title_end = STYLES.index("}", title_start)
        title_rule = STYLES[title_start:title_end]
        self.assertIn("display: block", title_rule)
        self.assertIn("font-weight: 700", title_rule)
        self.assertIn("-webkit-line-clamp: 3", STYLES)

    def test_frontend_cache_version_is_consistent(self) -> None:
        version = "v94-2-reply-template-cards-20260805"
        self.assertIn(f"styles.css?v={version}", INDEX)
        self.assertIn(f"app.js?v={version}", INDEX)
        self.assertIn(f"ARTI_CRM_SW_VERSION = '{version}'", SERVICE_WORKER)


if __name__ == "__main__":
    unittest.main()
