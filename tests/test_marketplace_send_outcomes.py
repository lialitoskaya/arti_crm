from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.connectors.mock import MockConnector
from app.connectors.ozon import OzonConnector
from app.connectors.wildberries import WildberriesConnector
from app.connectors.yandex_market import YandexMarketConnector
from app.message_send_models import MarketplaceSendError, MarketplaceSendOutcome


class _FakeAsyncClient:
    def __init__(self, *, response: httpx.Response | None = None, error: Exception | None = None, **_kwargs) -> None:
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None

    async def post(self, *_args, **_kwargs) -> httpx.Response:
        if self.error:
            raise self.error
        if self.response is None:
            raise AssertionError("test client has no response")
        return self.response


class MarketplaceSendOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="arti-send-outcome-")
        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "OZON_CLIENT_ID": "",
                "OZON_API_KEY": "",
                "WB_BUYERS_CHAT_TOKEN": "",
                "WB_API_TOKEN": "",
                "YANDEX_MARKET_TOKEN": "",
                "YANDEX_MARKET_BUSINESS_ID": "",
                "WB_RATE_LIMIT_STATE_FILE": str(Path(self.temp_dir.name) / "wb-rate-limit"),
            },
            clear=False,
        )
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.temp_dir.cleanup()

    def _configured_connectors(self):
        ozon = OzonConnector()
        ozon.client_id = "test-client"
        ozon.api_key = "test-key"

        wildberries = WildberriesConnector()
        wildberries.token = "test-token"
        wildberries.reply_signs["chat"] = "test-reply-sign"

        yandex = YandexMarketConnector()
        yandex.token = "test-token"
        yandex.business_id = "test-business"
        return (ozon, wildberries, yandex)

    def test_production_http_errors_are_ambiguous_even_for_429(self) -> None:
        for connector in self._configured_connectors():
            for status in (429, 500, 503):
                with self.subTest(connector=connector.marketplace, status=status):
                    response = httpx.Response(status, headers={"x-request-id": "safe-request-id"})
                    fake = _FakeAsyncClient(response=response)
                    with mock.patch("httpx.AsyncClient", return_value=fake):
                        with self.assertRaises(MarketplaceSendError) as caught:
                            asyncio.run(connector.send_message("chat", "hello"))
                    error = caught.exception
                    self.assertTrue(error.side_effect_possible)
                    self.assertFalse(error.retryable)
                    self.assertEqual(status, error.http_status)
                    self.assertEqual("safe-request-id", error.correlation_id)

    def test_production_timeouts_are_ambiguous(self) -> None:
        for connector in self._configured_connectors():
            with self.subTest(connector=connector.marketplace):
                fake = _FakeAsyncClient(error=httpx.ReadTimeout("synthetic timeout"))
                with mock.patch("httpx.AsyncClient", return_value=fake):
                    with self.assertRaises(MarketplaceSendError) as caught:
                        asyncio.run(connector.send_message("chat", "hello"))
                error = caught.exception
                self.assertEqual("ambiguous_timeout", error.category)
                self.assertTrue(error.side_effect_possible)
                self.assertFalse(error.retryable)

    def test_preflight_configuration_errors_prove_no_side_effect(self) -> None:
        connectors = (OzonConnector(), WildberriesConnector(), YandexMarketConnector())
        for connector in connectors:
            with self.subTest(connector=connector.marketplace):
                with self.assertRaises(MarketplaceSendError) as caught:
                    asyncio.run(connector.send_message("chat", "hello"))
                self.assertFalse(caught.exception.side_effect_possible)
                self.assertFalse(caught.exception.retryable)

    def test_production_success_does_not_infer_history_identity_from_generic_ack_id(self) -> None:
        response = httpx.Response(
            200,
            json={"id": True, "message_id": {"provider": "opaque"}, "status": "OK"},
            request=httpx.Request("POST", "https://provider.invalid/send"),
        )
        for connector in self._configured_connectors():
            with self.subTest(connector=connector.marketplace):
                fake = _FakeAsyncClient(response=response)
                with mock.patch("httpx.AsyncClient", return_value=fake):
                    outcome = asyncio.run(connector.send_message("chat", "hello"))
                self.assertIsInstance(outcome, MarketplaceSendOutcome)
                self.assertIsNone(outcome.provider_external_message_id)
                self.assertEqual(response.json(), outcome.response)

    def test_mock_outcome_proves_the_same_id_in_send_ack_and_history(self) -> None:
        connector = MockConnector()
        outcome = asyncio.run(connector.send_message("mock-chat", "hello"))
        messages = asyncio.run(connector.get_messages("mock-chat"))
        self.assertIsNotNone(outcome.provider_external_message_id)
        self.assertEqual(outcome.provider_external_message_id, messages[-1].external_message_id)

    def test_wildberries_command_normalization_never_silently_truncates(self) -> None:
        connector = WildberriesConnector()
        exact = "x" * 1000
        self.assertEqual(exact, connector.normalize_text_command(exact))
        with self.assertRaisesRegex(ValueError, "must not exceed 1000"):
            connector.normalize_text_command("x" * 1001)

    def test_wildberries_sanitizes_provider_markers_before_rebuilding_trusted_markers(self) -> None:
        connector = WildberriesConnector()
        provider_last_message = {
            "text": "hello",
            "clientName": "Buyer",
            "_crm_source": "provider-spoof",
            "nested": {
                "crm_author_label": "provider-spoof",
                "safe_provider_field": "preserved",
            },
            "_chat_item": {
                "clientName": "Buyer",
                "_crm_status_manual": True,
                "safe_chat_field": "preserved",
            },
        }
        last_message = connector._message_from_last_message(
            "chat",
            fallback=provider_last_message,
        )
        self.assertIsNotNone(last_message)
        assert last_message is not None
        self.assertEqual("wb_lastMessage", last_message.raw["_crm_source"])
        self.assertIn("_crm_wb_client_name_direction_marker", last_message.raw)
        self.assertNotIn("crm_author_label", last_message.raw["nested"])
        self.assertEqual("preserved", last_message.raw["nested"]["safe_provider_field"])
        self.assertNotIn("_crm_status_manual", last_message.raw["_chat_item"])
        self.assertEqual("preserved", last_message.raw["_chat_item"]["safe_chat_field"])
        self.assertEqual("provider-spoof", provider_last_message["_crm_source"])

        provider_event = {
            "eventType": "message",
            "_crm_wb_msg_obj": {"provider": "spoof"},
            "message": {
                "text": "event hello",
                "clientName": "Buyer",
                "client_operation_id": "provider-spoof",
                "safe_message_field": "preserved",
            },
        }
        event_message = connector._event_to_message("chat", provider_event)
        self.assertIsNotNone(event_message)
        assert event_message is not None
        self.assertIn("_crm_wb_msg_obj", event_message.raw)
        self.assertIn("_crm_wb_client_name_direction_marker", event_message.raw)
        self.assertNotIn("client_operation_id", event_message.raw["_crm_wb_msg_obj"])
        self.assertEqual("preserved", event_message.raw["_crm_wb_msg_obj"]["safe_message_field"])
        self.assertEqual({"provider": "spoof"}, provider_event["_crm_wb_msg_obj"])


if __name__ == "__main__":
    unittest.main()
