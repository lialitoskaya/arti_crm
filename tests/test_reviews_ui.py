"""Отзывы: характеризация рендера списка — `reviewModelName` определена и даёт название модели.

Регрессия: `renderReviewsList`/`openReview` вызывали `reviewModelName(review)`, которой не было в `app.js`,
и любой непустой список отзывов заканчивался `ReferenceError` и пустым разделом.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_chat_read_state_ui import _extract_function, _run_node  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


class ReviewModelNameTests(unittest.TestCase):
    def test_review_model_name_is_defined_once_and_used_by_the_renderers(self) -> None:
        self.assertEqual(APP_JS.count("function reviewModelName("), 1)
        render_list = _extract_function(APP_JS, "renderReviewsList")
        open_review = _extract_function(APP_JS, "openReview")
        self.assertIn("reviewModelName(review)", render_list)
        self.assertIn("reviewModelName(review)", open_review)

    def test_review_model_name_prefers_product_name_then_sku(self) -> None:
        script = _extract_function(APP_JS, "reviewModelName") + """
const assert = require('node:assert/strict');
assert.equal(reviewModelName({ product_name: 'Чехол X5', sku: '123' }), 'Чехол X5');
assert.equal(reviewModelName({ product_name: '', sku: '123' }), 'SKU 123');
assert.equal(reviewModelName({}), 'Товар не указан');
"""
        _run_node(script)


if __name__ == "__main__":
    unittest.main()
