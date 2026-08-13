from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.connectors.ozon import OzonConnector
from test_chat_read_state_ui import _extract_function, _run_node


ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "app" / "static" / "app.js"
STYLES_PATH = ROOT / "app" / "static" / "styles.css"


class OzonProductContextConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.connector = OzonConnector()
        self.connector.client_id = "test-client"
        self.connector.api_key = "test-key"
        self.connector.history_pages = 1

    def test_error_text_transport_token_is_not_exposed_as_message_text(self) -> None:
        text = self.connector._extract_message_text(
            {
                "data": [
                    "errorText",
                    "Здравствуйте! Сообщение связано с товаром.",
                ]
            }
        )
        self.assertEqual(
            "Здравствуйте! Сообщение связано с товаром.",
            text,
        )

    def test_product_context_with_order_number_uses_valid_top_level_sku(self) -> None:
        expected = {
            "kind": "ozon_product",
            "sku": "300000043269549888",
            "url": "https://www.ozon.ru/product/300000043269549888",
        }
        self.assertEqual(
            expected,
            self.connector._normalize_product_context(
                {"context": {"sku": 300000043269549888, "order_number": "1850393460"}}
            ),
        )

    def test_product_context_without_order_number_uses_valid_top_level_sku(self) -> None:
        self.assertEqual(
            {
                "kind": "ozon_product",
                "sku": "12345",
                "url": "https://www.ozon.ru/product/12345",
            },
            self.connector._normalize_product_context({"context": {"sku": "12345"}}),
        )

    def test_empty_malformed_or_missing_sku_does_not_create_product_context(self) -> None:
        for value in (None, "", "   ", "../../unsafe", "sku with spaces", True, False, 12345.0, [], {}):
            with self.subTest(sku=value):
                self.assertIsNone(
                    self.connector._normalize_product_context({"context": {"sku": value}})
                )
        self.assertIsNone(self.connector._normalize_product_context({}))

    def test_optional_preview_is_normalized_without_additional_marketplace_call(self) -> None:
        payload = {
            "context": {
                "sku": "12345",
                "product_name": "Беспроводные наушники",
                "image": {"url": "https://cdn1.ozone.ru/s3/product-preview.jpg"},
            }
        }
        self.assertEqual(
            {
                "kind": "ozon_product",
                "sku": "12345",
                "url": "https://www.ozon.ru/product/12345",
                "title": "Беспроводные наушники",
                "image_url": "https://cdn1.ozone.ru/s3/product-preview.jpg",
            },
            self.connector._normalize_product_context(payload),
        )

    async def test_history_mapping_keeps_one_canonical_context_contract(self) -> None:
        raw_message = {
            "message_id": "3000000432695498888",
            "created_at": "2026-07-17T05:41:58.890866Z",
            "user": {"id": "1728628", "type": "Seller"},
            "data": ["errorText", "Здравствуйте! Сообщение связано с товаром."],
            "context": {"sku": "300000043269549888", "order_number": "1850393460"},
        }
        with patch.object(
            self.connector,
            "_post",
            new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
        ) as post_mock:
            messages = await self.connector.get_messages("2314926")

        post_mock.assert_awaited_once()
        self.assertEqual(1, len(messages))
        self.assertEqual("Здравствуйте! Сообщение связано с товаром.", messages[0].text)
        self.assertEqual(
            {
                "kind": "ozon_product",
                "sku": "300000043269549888",
                "url": "https://www.ozon.ru/product/300000043269549888",
            },
            messages[0].raw.get("_crm_product_context"),
        )

        raw_message["context"] = {"sku": ""}
        with patch.object(
            self.connector,
            "_post",
            new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
        ):
            messages_without_sku = await self.connector.get_messages("2314926")
        self.assertNotIn("_crm_product_context", messages_without_sku[0].raw)

    async def test_provider_reserved_context_is_removed_without_valid_context(self) -> None:
        spoofed_context = {
            "kind": "ozon_product",
            "sku": "99999",
            "url": "https://www.ozon.ru/product/99999",
        }
        raw_message = {
            "message_id": "spoof-only",
            "user": {"type": "Customer"},
            "data": ["Обычное сообщение"],
            "_crm_product_context": spoofed_context,
        }
        with patch.object(
            self.connector,
            "_post",
            new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
        ):
            messages = await self.connector.get_messages("ordinary-chat")

        self.assertNotIn("_crm_product_context", messages[0].raw)
        self.assertIs(raw_message["_crm_product_context"], spoofed_context)

    async def test_malformed_provider_reserved_context_is_removed(self) -> None:
        for spoofed_context in ("spoofed", ["spoofed"], {"kind": "wrong"}, None):
            with self.subTest(spoofed_context=spoofed_context):
                raw_message = {
                    "message_id": f"malformed-{type(spoofed_context).__name__}",
                    "user": {"type": "Customer"},
                    "data": ["Обычное сообщение"],
                    "context": {"sku": "../../unsafe"},
                    "_crm_product_context": spoofed_context,
                }
                with patch.object(
                    self.connector,
                    "_post",
                    new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
                ):
                    messages = await self.connector.get_messages("ordinary-chat")

                self.assertNotIn("_crm_product_context", messages[0].raw)

    async def test_provider_reserved_context_is_replaced_by_normalized_context(self) -> None:
        spoofed_context = {
            "kind": "ozon_product",
            "sku": "99999",
            "url": "https://www.ozon.ru/product/99999",
            "title": "Подменённое название",
            "image_url": "https://attacker.example/spoof.jpg",
            "extra": "provider-controlled",
        }
        raw_message = {
            "message_id": "canonical-wins",
            "user": {"type": "Customer"},
            "data": ["Сообщение с контекстом"],
            "context": {
                "sku": "12345",
                "product_name": "Проверенное название",
                "image_url": "https://cdn1.ozone.ru/product.jpg",
            },
            "_crm_product_context": spoofed_context,
        }
        with patch.object(
            self.connector,
            "_post",
            new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
        ):
            messages = await self.connector.get_messages("context-chat")

        self.assertEqual(
            {
                "kind": "ozon_product",
                "sku": "12345",
                "url": "https://www.ozon.ru/product/12345",
                "title": "Проверенное название",
                "image_url": "https://cdn1.ozone.ru/product.jpg",
            },
            messages[0].raw.get("_crm_product_context"),
        )
        self.assertNotIn("extra", messages[0].raw["_crm_product_context"])
        self.assertIs(raw_message["_crm_product_context"], spoofed_context)

    async def test_mapping_does_not_mutate_provider_payload(self) -> None:
        raw_message = {
            "message_id": "input-unchanged",
            "user": {"type": "Customer"},
            "data": ["Сообщение"],
            "context": {"sku": "12345"},
            "_crm_product_context": {"kind": "provider-owned"},
        }
        expected = {
            **raw_message,
            "user": dict(raw_message["user"]),
            "data": list(raw_message["data"]),
            "context": dict(raw_message["context"]),
            "_crm_product_context": dict(raw_message["_crm_product_context"]),
        }
        with patch.object(
            self.connector,
            "_post",
            new=AsyncMock(return_value={"result": {"messages": [raw_message], "has_next": False}}),
        ):
            await self.connector.get_messages("context-chat")

        self.assertEqual(expected, raw_message)


class OzonProductContextUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = APP_JS_PATH.read_text(encoding="utf-8")
        cls.styles = STYLES_PATH.read_text(encoding="utf-8")
        cls.context_function = _extract_function(cls.source, "ozonProductContext")

    def test_frontend_builds_link_only_for_non_empty_normalized_sku(self) -> None:
        _run_node(
            f"""
            {self.context_function}
            const valid = ozonProductContext({{ raw: {{ _crm_product_context: {{ kind: 'ozon_product', sku: '12345', url: 'https://www.ozon.ru/product/12345' }} }} }});
            if (!valid || valid.url !== 'https://www.ozon.ru/product/12345') throw new Error('valid Ozon product link missing');
            if (ozonProductContext({{ raw: {{ _crm_product_context: {{ kind: 'ozon_product', sku: '', url: 'https://www.ozon.ru/product/' }} }} }}) !== null) throw new Error('empty SKU created a link');
            if (ozonProductContext({{ raw: {{ _crm_product_context: {{ kind: 'ozon_product', sku: '../../bad', url: 'https://www.ozon.ru/product/../../bad' }} }} }}) !== null) throw new Error('unsafe SKU created a link');
            if (ozonProductContext({{ raw: {{ _crm_product_context: {{ kind: 'ozon_product', sku: '12345', url: 'https://example.com/product/12345' }} }} }}) !== null) throw new Error('external host was accepted');
            if (ozonProductContext({{ raw: {{ context: {{ sku: '12345' }} }} }}) !== null) throw new Error('raw provider context bypassed canonical mapping');
            """
        )

    def test_preview_is_lazy_and_not_duplicated_in_generic_image_gallery(self) -> None:
        create_card = _extract_function(self.source, "createOzonProductContextCard")
        extract_images = _extract_function(self.source, "extractImageUrls")
        self.assertIn("img.loading = 'lazy'", create_card)
        self.assertIn("prepareLazyChatImage", create_card)
        self.assertIn("productContext?.imageUrl", self.source)
        self.assertIn("found.delete", extract_images)
        self.assertNotIn("/v", create_card)
        self.assertNotIn("fetch(", create_card)

    def test_product_context_uses_compact_existing_message_layout(self) -> None:
        self.assertIn(".message-product-context {", self.styles)
        self.assertIn("border-bottom: 1px solid var(--crm-line);", self.styles)
        self.assertIn(".message-product-context.without-image", self.styles)
        self.assertNotIn("message-product-context-placeholder", self.source)

    def test_frontend_uses_neutral_product_label_and_accessible_name(self) -> None:
        create_card = _extract_function(self.source, "createOzonProductContextCard")
        self.assertIn("label.textContent = 'Товар'", create_card)
        self.assertIn("card.title = 'Открыть товар на Ozon'", create_card)
        self.assertIn("`Открыть товар SKU ${context.sku} на Ozon`", create_card)
        self.assertIn("`Изображение товара ${context.title}`", create_card)
        self.assertNotIn("Товар из отзыва", create_card)
        self.assertNotIn("отзыв", create_card.lower())

    def test_repeated_render_rebuilds_only_one_product_block_per_message(self) -> None:
        render_messages = _extract_function(self.source, "renderMessages")
        self.assertLess(
            render_messages.index("box.innerHTML = ''"),
            render_messages.index("for (const message of messages)"),
        )
        self.assertEqual(1, render_messages.count("createOzonProductContextCard(productContext)"))


if __name__ == "__main__":
    unittest.main()
