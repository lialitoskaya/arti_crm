from __future__ import annotations
import asyncio
import inspect
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.connectors.base import MarketplaceConnector, UnifiedChat, UnifiedMessage  # noqa: E402
from app.message_outbox_migration import (  # noqa: E402
    MESSAGE_SEND_OPERATION_MIGRATION,
    apply_message_send_operation_migration,
    assert_message_send_operation_schema,
)
from app.message_send_models import (  # noqa: E402
    MESSAGE_SEND_GUARANTEE,
    MarketplaceSendError,
    MarketplaceSendOutcome,
    sanitize_provider_payload,
)
from app.message_send_operations import (  # noqa: E402
    MessageSendOperationConflict,
    claim_next_operation,
    complete_operation_accepted,
    complete_operation_error,
    count_operations,
    get_operation,
    get_operation_by_client_id,
    match_operation_for_echo_conn,
    mark_stale_sending_uncertain,
    register_operation,
)
from app.message_send_service import MessageSendService  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


async def _client_for_user(user: dict[str, object]) -> tuple[httpx.AsyncClient, dict[str, str]]:
    token = repo.create_session(int(user["id"]), user_agent="message-outbox-test")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )
    client.cookies.set(main.AUTH_COOKIE_NAME, token)
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain CSRF token: {response.status_code}")
    return client, {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


class ControlledConnector(MarketplaceConnector):
    marketplace = "mock"

    def __init__(self, outcome: MarketplaceSendError | None = None) -> None:
        self.outcome = outcome
        self.calls = 0
        self.messages: list[UnifiedMessage] = []

    async def list_chats(self) -> list[UnifiedChat]:
        return []

    async def get_messages(self, external_chat_id: str) -> list[UnifiedMessage]:
        return [
            message
            for message in self.messages
            if str(message.external_chat_id) == str(external_chat_id)
        ]

    async def send_message(self, external_chat_id: str, text: str) -> MarketplaceSendOutcome:
        self.calls += 1
        if self.outcome:
            raise self.outcome
        external_id = f"controlled-{self.calls}"
        self.messages.append(
            UnifiedMessage(
                external_message_id=external_id,
                external_chat_id=str(external_chat_id),
                direction="outbound",
                text=text,
                author="seller",
                created_at=datetime.now(timezone.utc).isoformat(),
                raw={"message_id": external_id},
            )
        )
        return MarketplaceSendOutcome(
            response={"message_id": external_id},
            provider_external_message_id=external_id,
        )


class MessageSendOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "outbox-manager",
            "outbox-password-2026",
            "Лия",
            "admin",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="outbox-chat",
                customer_name="Synthetic Customer",
                metadata={"source": "mock"},
            )
        )
        main.connectors["mock"].sent_messages.clear()

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _register(
        self,
        operation_id: str,
        text: str = "Durable text",
        origin: str = "message",
    ) -> dict[str, object]:
        operation, _ = register_operation(
            chat_id=self.chat_id,
            client_operation_id=operation_id,
            text=text,
            intent_origin=origin,
            author_user_id=int(self.user["id"]),
            author_label="Лия",
        )
        return operation

    def _operation_rows(self) -> list[dict[str, object]]:
        with db.get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM message_send_operations ORDER BY id")]

    def _message_rows(self) -> list[dict[str, object]]:
        with db.get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (self.chat_id,))]

    def test_guarantee_is_honest_and_not_exactly_once(self) -> None:
        self.assertIn("не более одной одновременной", MESSAGE_SEND_GUARANTEE)
        self.assertIn("отсутствие автоматического повтора после неоднозначного исхода", MESSAGE_SEND_GUARANTEE)
        self.assertNotIn("exactly-once", MESSAGE_SEND_GUARANTEE.lower())

    def test_registration_is_durable_and_same_operation_id_is_idempotent(self) -> None:
        first = self._register("same-operation")
        second, deduplicated = register_operation(
            chat_id=self.chat_id,
            client_operation_id="same-operation",
            text="Durable text",
            intent_origin="message",
            author_user_id=999,
            author_label="Spoofed",
        )
        self.assertTrue(deduplicated)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(int(self.user["id"]), second["author_user_id"])
        self.assertEqual("Лия", second["author_label"])
        self.assertEqual(1, count_operations())
        self.assertEqual({"version": 1, "text": "Durable text"}, json.loads(str(first["payload_json"])))

    def test_chat_operation_feed_keeps_older_unresolved_beyond_confirmed_history_limit(self) -> None:
        unresolved = self._register("older-unresolved", "Still waiting")
        confirmed: list[tuple[int, int]] = []
        for index in range(125):
            operation = self._register(
                f"newer-confirmed-{index}",
                f"Confirmed history {index}",
            )
            message_id = repo.add_message(
                self.chat_id,
                "outbound",
                f"Confirmed history {index}",
                author="provider",
                external_message_id=f"confirmed-history-{index}",
                raw={"message_id": f"confirmed-history-{index}"},
            )
            confirmed.append((message_id, int(operation["id"])))

        with db.get_connection() as conn:
            conn.executemany(
                """
                UPDATE message_send_operations
                SET status='confirmed', canonical_message_id=?, confirmed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                confirmed,
            )

        async def exercise() -> httpx.Response:
            client, _headers = await _client_for_user(self.user)
            try:
                return await client.get(
                    f"/api/chats/{self.chat_id}/message-send-operations"
                )
            finally:
                await client.aclose()

        response = asyncio.run(exercise())
        self.assertEqual(200, response.status_code)
        operations = response.json()["operations"]
        self.assertIn(int(unresolved["id"]), {int(item["id"]) for item in operations})
        self.assertTrue(all(item["status"] != "confirmed" for item in operations))

    def test_same_operation_id_with_other_payload_conflicts(self) -> None:
        self._register("conflicting-operation", "First")
        with self.assertRaises(MessageSendOperationConflict):
            self._register("conflicting-operation", "Second")

    def test_two_sqlite_connections_claim_only_once(self) -> None:
        self._register("claim-race")
        barrier = threading.Barrier(2)

        def claim() -> dict[str, object] | None:
            barrier.wait(timeout=5)
            return claim_next_operation()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        claimed = [result for result in results if result]
        self.assertEqual(1, len(claimed))
        self.assertEqual("sending", claimed[0]["status"])
        self.assertEqual(1, claimed[0]["attempt_count"])

    def test_two_pending_same_payload_are_distinct_and_serialized(self) -> None:
        first = self._register("same-payload-1", "Same text", "message")
        second = self._register("same-payload-2", "Same text", "attachment_caption")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["intent_origin"], second["intent_origin"])
        claimed = claim_next_operation()
        self.assertIsNotNone(claimed)
        self.assertIsNone(claim_next_operation())

    def test_accepted_blocks_next_same_payload(self) -> None:
        self._register("accepted-1", "Serialized")
        self._register("accepted-2", "Serialized")
        claimed = claim_next_operation()
        complete_operation_accepted(
            operation_id=int(claimed["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="accepted-external",
        )
        self.assertIsNone(claim_next_operation())

    def test_uncertain_blocks_next_same_payload(self) -> None:
        self._register("uncertain-1", "Serialized")
        self._register("uncertain-2", "Serialized")
        claimed = claim_next_operation()
        complete_operation_error(
            operation_id=int(claimed["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(
                category="ambiguous_timeout",
                safe_summary="unknown",
                side_effect_possible=True,
            ),
        )
        self.assertIsNone(claim_next_operation())

    def test_confirmed_releases_next_same_payload(self) -> None:
        self._register("confirm-1", "Serialized")
        second = self._register("confirm-2", "Serialized")
        claimed = claim_next_operation()
        complete_operation_accepted(
            operation_id=int(claimed["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="echo-release",
        )
        message_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Serialized",
            author="seller",
            external_message_id="echo-release",
            raw={"message_id": "echo-release"},
        )
        self.assertEqual("confirmed", get_operation(int(claimed["id"]))["status"])
        next_claim = claim_next_operation()
        self.assertEqual(second["id"], next_claim["id"])
        self.assertEqual(message_id, get_operation(int(claimed["id"]))["canonical_message_id"])

    def test_crash_after_enqueue_is_drained_after_restart(self) -> None:
        operation = self._register("restart-enqueue", "Restart text")
        self.assertEqual("pending", operation["status"])
        connector = ControlledConnector()
        service = MessageSendService({"mock": connector})
        result = asyncio.run(service.drain_once())
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, connector.calls)
        self.assertEqual("confirmed", get_operation(int(operation["id"]))["status"])

    def test_stale_claim_becomes_uncertain_and_is_not_resent(self) -> None:
        operation = self._register("stale-claim")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE message_send_operations SET lease_until=? WHERE id=?",
                ("2000-01-01T00:00:00.000Z", int(operation["id"])),
            )
        self.assertEqual(1, mark_stale_sending_uncertain())
        self.assertEqual("uncertain", get_operation(int(operation["id"]))["status"])
        self.assertIsNone(claim_next_operation(operation_id=int(operation["id"])))
        self.assertEqual(str(claimed["claim_token"]), get_operation(int(operation["id"]))["claim_token"])

    def test_reaper_before_late_success_accepts_matching_token(self) -> None:
        operation = self._register("late-success")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        with db.get_connection() as conn:
            conn.execute("UPDATE message_send_operations SET lease_until='2000-01-01T00:00:00.000Z' WHERE id=?", (int(operation["id"]),))
        mark_stale_sending_uncertain()
        updated, applied = complete_operation_accepted(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="late-success-id",
        )
        self.assertTrue(applied)
        self.assertEqual("accepted", updated["status"])

    def test_late_error_after_reaper_does_not_overwrite_uncertain(self) -> None:
        operation = self._register("late-error")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        with db.get_connection() as conn:
            conn.execute("UPDATE message_send_operations SET lease_until='2000-01-01T00:00:00.000Z' WHERE id=?", (int(operation["id"]),))
        mark_stale_sending_uncertain()
        updated, applied = complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(category="validation", safe_summary="late", side_effect_possible=False),
        )
        self.assertFalse(applied)
        self.assertEqual("uncertain", updated["status"])

    def test_echo_before_ack_confirms_and_late_ack_cannot_regress(self) -> None:
        operation = self._register("echo-before-ack", "Echo first")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        message_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Echo first",
            author="seller",
            external_message_id="echo-before-ack-id",
            raw={"message_id": "echo-before-ack-id"},
        )
        confirmed = get_operation(int(operation["id"]))
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(message_id, confirmed["canonical_message_id"])
        _, applied = complete_operation_accepted(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="late-ack-id",
        )
        self.assertFalse(applied)
        self.assertEqual("confirmed", get_operation(int(operation["id"]))["status"])

    def test_hash_reconciliation_ignores_old_same_text_then_confirms_fresh_echo(self) -> None:
        operation = self._register("bounded-echo", "Repeated provider text")
        claim_next_operation(operation_id=int(operation["id"]))
        old_message_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Repeated provider text",
            author="provider",
            external_message_id="old-provider-message",
            raw={"message_id": "old-provider-message"},
            created_at="2000-01-01T00:00:00+00:00",
        )
        self.assertEqual("sending", get_operation(int(operation["id"]))["status"])
        with db.get_connection() as conn:
            old_row = conn.execute(
                "SELECT is_crm_sent FROM messages WHERE id=?",
                (old_message_id,),
            ).fetchone()
            self.assertEqual(0, old_row["is_crm_sent"])

        fresh_message_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Repeated provider text",
            author="provider",
            external_message_id="fresh-provider-message",
            raw={"message_id": "fresh-provider-message"},
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        confirmed = get_operation(int(operation["id"]))
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(fresh_message_id, confirmed["canonical_message_id"])
        self.assertNotEqual(old_message_id, fresh_message_id)

    def test_proven_ack_identity_does_not_fall_back_to_other_echo_id(self) -> None:
        operation = self._register("proven-provider-id", "Proven identity")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        complete_operation_accepted(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="history-stable-id",
        )
        now = datetime.now(timezone.utc).isoformat()
        with db.get_connection() as conn:
            self.assertIsNone(
                match_operation_for_echo_conn(
                    conn,
                    chat_id=self.chat_id,
                    direction="outbound",
                    text="Proven identity",
                    provider_external_message_id="different-history-id",
                    created_at=now,
                )
            )
            matched = match_operation_for_echo_conn(
                conn,
                chat_id=self.chat_id,
                direction="outbound",
                text="Proven identity",
                provider_external_message_id="history-stable-id",
                created_at=now,
            )
        self.assertEqual(operation["id"], matched["id"])

    def test_late_error_after_confirmed_cannot_regress(self) -> None:
        operation = self._register("confirmed-late-error", "Confirmed text")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        repo.add_message(
            self.chat_id,
            "outbound",
            "Confirmed text",
            author="provider",
            external_message_id="confirmed-id",
            raw={"message_id": "confirmed-id"},
        )
        _, applied = complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(category="validation", safe_summary="late", side_effect_possible=False),
        )
        self.assertFalse(applied)
        self.assertEqual("confirmed", get_operation(int(operation["id"]))["status"])

    def test_old_claim_token_cannot_finish_new_safe_attempt(self) -> None:
        operation = self._register("old-token")
        first = claim_next_operation(operation_id=int(operation["id"]))
        complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(first["claim_token"]),
            error=MarketplaceSendError(
                category="safe_rejection",
                safe_summary="not accepted",
                side_effect_possible=False,
                retryable=True,
                retry_after_seconds=5,
            ),
        )
        with db.get_connection() as conn:
            conn.execute("UPDATE message_send_operations SET next_attempt_at='2000-01-01T00:00:00.000Z' WHERE id=?", (int(operation["id"]),))
        second = claim_next_operation(operation_id=int(operation["id"]))
        self.assertNotEqual(first["claim_token"], second["claim_token"])
        _, applied = complete_operation_accepted(
            operation_id=int(operation["id"]),
            claim_token=str(first["claim_token"]),
            provider_external_message_id="wrong-token",
        )
        self.assertFalse(applied)
        self.assertEqual("sending", get_operation(int(operation["id"]))["status"])

    def test_production_ambiguous_outcomes_never_retry_automatically(self) -> None:
        for index, (category, status) in enumerate((
            ("ambiguous_timeout", None),
            ("ambiguous_transport", None),
            ("provider_http_error", 429),
            ("provider_http_error", 503),
        )):
            with self.subTest(category=category, status=status):
                operation = self._register(f"ambiguous-{index}", f"Ambiguous {index}")
                claimed = claim_next_operation(operation_id=int(operation["id"]))
                updated, applied = complete_operation_error(
                    operation_id=int(operation["id"]),
                    claim_token=str(claimed["claim_token"]),
                    error=MarketplaceSendError(
                        category=category,
                        safe_summary="provider outcome unknown",
                        side_effect_possible=True,
                        retryable=True,
                        http_status=status,
                    ),
                )
                self.assertTrue(applied)
                self.assertEqual("uncertain", updated["status"])
                self.assertIsNone(updated["next_attempt_at"])

    def test_safe_rejection_uses_bounded_retry_wait(self) -> None:
        operation = self._register("safe-retry")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        updated, applied = complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(
                category="safe_mock_rejection",
                safe_summary="mock proved no side effect",
                side_effect_possible=False,
                retryable=True,
            ),
        )
        self.assertTrue(applied)
        self.assertEqual("retry_wait", updated["status"])
        self.assertIsNotNone(updated["next_attempt_at"])

    def test_attempt_limit_turns_safe_rejection_permanent(self) -> None:
        operation = self._register("attempt-limit")
        with db.get_connection() as conn:
            conn.execute("UPDATE message_send_operations SET attempt_count=5 WHERE id=?", (int(operation["id"]),))
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        updated, _ = complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(
                category="safe_mock_rejection",
                safe_summary="still rejected",
                side_effect_possible=False,
                retryable=True,
            ),
        )
        self.assertEqual("permanent_failed", updated["status"])
        self.assertIsNotNone(updated["failed_at"])

    def test_error_persistence_is_sanitized_and_bounded(self) -> None:
        operation = self._register("sanitized-error")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        updated, _ = complete_operation_error(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            error=MarketplaceSendError(
                category="provider_http_error",
                safe_summary="Authorization: Bearer top-secret user@example.test +7 999 123-45-67\n" + "x" * 600,
                side_effect_possible=True,
                http_status=500,
                provider_code="SAFE_CODE",
                correlation_id="request-123",
            ),
        )
        summary = str(updated["error_summary"])
        self.assertLessEqual(len(summary), 256)
        self.assertNotIn("top-secret", summary)
        self.assertNotIn("user@example.test", summary)
        self.assertNotIn("999 123", summary)
        self.assertEqual("SAFE_CODE", updated["error_provider_code"])

    def test_provider_reserved_markers_are_removed_recursively(self) -> None:
        payload = {
            "is_crm_sent": True,
            "_crm_sent_from_crm": True,
            "context": {
                "sku": "SKU-1",
                "crm_author_label": "Spoofed",
                "nested": [{"client_operation_id": "spoof", "value": 1}],
            },
        }
        sanitized = sanitize_provider_payload(payload)
        self.assertEqual("SKU-1", sanitized["context"]["sku"])
        self.assertNotIn("is_crm_sent", sanitized)
        self.assertNotIn("_crm_sent_from_crm", sanitized)
        self.assertNotIn("crm_author_label", sanitized["context"])
        self.assertNotIn("client_operation_id", sanitized["context"]["nested"][0])
        self.assertIn("is_crm_sent", payload)

    def test_echo_is_outbound_read_and_keeps_server_attribution(self) -> None:
        operation = self._register("attribution-echo", "Trusted author")
        claimed = claim_next_operation(operation_id=int(operation["id"]))
        complete_operation_accepted(
            operation_id=int(operation["id"]),
            claim_token=str(claimed["claim_token"]),
            provider_external_message_id="trusted-echo",
        )
        message_id = repo.add_message(
            self.chat_id,
            "outbound",
            "Trusted author",
            author="provider",
            external_message_id="trusted-echo",
            raw={"message_id": "trusted-echo"},
        )
        message = next(item for item in self._message_rows() if int(item["id"]) == message_id)
        self.assertEqual(1, message["is_crm_sent"])
        self.assertEqual("Лия", message["crm_author_label"])
        self.assertEqual(int(self.user["id"]), message["crm_author_user_id"])
        self.assertEqual("outbound", message["direction"])
        chat = repo.get_chat(self.chat_id, current_user_id=int(self.user["id"]))
        self.assertFalse(bool(chat["is_unread"]))

    def test_fifo_reconciliation_keeps_identical_operations_distinct(self) -> None:
        first = self._register("fifo-first", "Identical", "message")
        second = self._register("fifo-second", "Identical", "attachment_caption")
        first_claim = claim_next_operation()
        complete_operation_accepted(
            operation_id=int(first_claim["id"]),
            claim_token=str(first_claim["claim_token"]),
            provider_external_message_id=None,
        )
        first_message = repo.add_message(self.chat_id, "outbound", "Identical", author="provider", external_message_id="fifo-echo-1", raw={})
        second_claim = claim_next_operation()
        complete_operation_accepted(
            operation_id=int(second_claim["id"]),
            claim_token=str(second_claim["claim_token"]),
            provider_external_message_id=None,
        )
        second_message = repo.add_message(self.chat_id, "outbound", "Identical", author="provider", external_message_id="fifo-echo-2", raw={})
        self.assertEqual(first_message, get_operation(int(first["id"]))["canonical_message_id"])
        self.assertEqual(second_message, get_operation(int(second["id"]))["canonical_message_id"])
        self.assertNotEqual(first_message, second_message)

    def test_concurrent_http_posts_have_one_call_operation_and_message(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response, int]:
            client, headers = await _client_for_user(self.user)
            connector = main.connectors["mock"]
            original_send = connector.send_message
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def delayed_send(external_chat_id: str, text: str):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return await original_send(external_chat_id, text)

            try:
                with mock.patch.object(connector, "send_message", new=delayed_send):
                    payload = {"text": "Concurrent", "operation_id": "concurrent-http-operation"}
                    first_task = asyncio.create_task(client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers))
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    second_task = asyncio.create_task(client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers))
                    await asyncio.sleep(0.05)
                    release.set()
                    first, second = await asyncio.gather(first_task, second_task)
                    return first, second, calls
            finally:
                await client.aclose()

        first, second, calls = asyncio.run(exercise())
        self.assertIn(first.status_code, {200, 202})
        self.assertIn(second.status_code, {200, 202})
        self.assertEqual(1, calls)
        self.assertEqual(1, count_operations())
        self.assertEqual(1, len(self._message_rows()))
        self.assertEqual(
            first.json()["operation"]["id"],
            second.json()["operation"]["id"],
        )

    def test_repeated_http_post_returns_existing_operation(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                payload = {"text": "Repeat", "operation_id": "repeat-http-operation"}
                first = await client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers)
                second = await client.post(f"/api/chats/{self.chat_id}/messages", json=payload, headers=headers)
                return first, second
            finally:
                await client.aclose()

        first, second = asyncio.run(exercise())
        self.assertEqual(first.json()["operation"]["id"], second.json()["operation"]["id"])
        self.assertTrue(second.json()["operation"]["deduplicated"])
        self.assertEqual(1, count_operations())

    def test_message_post_requires_client_operation_id_before_registration(self) -> None:
        async def exercise() -> httpx.Response:
            client, headers = await _client_for_user(self.user)
            try:
                return await client.post(
                    f"/api/chats/{self.chat_id}/messages",
                    json={"text": "Missing operation id"},
                    headers=headers,
                )
            finally:
                await client.aclose()

        response = asyncio.run(exercise())
        self.assertEqual(422, response.status_code)
        self.assertEqual(0, count_operations())

    def test_wildberries_oversize_text_is_rejected_before_enqueue_or_io(self) -> None:
        wb_chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="wildberries",
                external_chat_id="wb-command-size",
                customer_name="Synthetic WB Customer",
                metadata={},
            )
        )

        async def exercise() -> httpx.Response:
            client, headers = await _client_for_user(self.user)
            try:
                return await client.post(
                    f"/api/chats/{wb_chat_id}/messages",
                    json={"text": "x" * 1001, "operation_id": "wb-oversize-operation"},
                    headers=headers,
                )
            finally:
                await client.aclose()

        response = asyncio.run(exercise())
        self.assertEqual(422, response.status_code)
        self.assertEqual(0, count_operations())

    def test_http_payload_conflict_is_409(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                first = await client.post(
                    f"/api/chats/{self.chat_id}/messages",
                    json={"text": "First", "operation_id": "http-conflict"},
                    headers=headers,
                )
                second = await client.post(
                    f"/api/chats/{self.chat_id}/messages",
                    json={"text": "Second", "operation_id": "http-conflict"},
                    headers=headers,
                )
                return first, second
            finally:
                await client.aclose()

        first, second = asyncio.run(exercise())
        self.assertIn(first.status_code, {200, 202})
        self.assertEqual(409, second.status_code)
        self.assertEqual(1, count_operations())

    def test_spoofed_client_author_is_ignored(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                return await client.post(
                    f"/api/chats/{self.chat_id}/messages",
                    json={"text": "Author", "author": "Spoofed", "operation_id": "spoof-author"},
                    headers=headers,
                )
            finally:
                await client.aclose()

        response = asyncio.run(exercise())
        self.assertIn(response.status_code, {200, 202})
        operation = get_operation_by_client_id(self.chat_id, "spoof-author")
        self.assertEqual("Лия", operation["author_label"])
        self.assertEqual(int(self.user["id"]), operation["author_user_id"])

    def test_caption_uses_same_text_command_model(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                return await client.post(
                    f"/api/chats/{self.chat_id}/messages",
                    json={
                        "text": "Caption",
                        "operation_id": "caption-operation",
                        "intent_origin": "attachment_caption",
                    },
                    headers=headers,
                )
            finally:
                await client.aclose()

        response = asyncio.run(exercise())
        self.assertIn(response.status_code, {200, 202})
        operation = get_operation_by_client_id(self.chat_id, "caption-operation")
        self.assertEqual("chat_text", operation["command_kind"])
        self.assertEqual("attachment_caption", operation["intent_origin"])

    def test_attachment_route_rejects_caption_before_file_read_or_io(self) -> None:
        async def exercise():
            client, headers = await _client_for_user(self.user)
            try:
                with mock.patch.object(main, "_read_chat_image", new=mock.AsyncMock()) as reader, mock.patch.object(
                    main.connectors["mock"], "send_file", new=mock.AsyncMock()
                ) as sender:
                    response = await client.post(
                        f"/api/chats/{self.chat_id}/attachments",
                        data={"caption": "Legacy caption", "operation_id": "attachment-operation"},
                        files={"images": ("image.png", b"not-read", "image/png")},
                        headers=headers,
                    )
                    return response, reader.await_count, sender.await_count
            finally:
                await client.aclose()

        response, reads, sends = asyncio.run(exercise())
        self.assertEqual(422, response.status_code)
        self.assertEqual(0, reads)
        self.assertEqual(0, sends)

    def test_repeated_init_db_keeps_one_migration_and_schema(self) -> None:
        db.init_db()
        db.init_db()
        with db.get_connection() as conn:
            assert_message_send_operation_schema(conn)
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations WHERE name=?",
                (MESSAGE_SEND_OPERATION_MIGRATION,),
            ).fetchone()["count"]
            self.assertEqual(1, count)

    def test_empty_partial_outbox_schema_is_rebuilt_with_full_contract(self) -> None:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name=?", (MESSAGE_SEND_OPERATION_MIGRATION,))
            conn.execute("DROP TABLE message_send_operations")
            conn.execute("CREATE TABLE message_send_operations (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            apply_message_send_operation_migration(conn)
            assert_message_send_operation_schema(conn)

    def test_nonempty_partial_outbox_schema_fails_closed_without_synthetic_command(self) -> None:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name=?", (MESSAGE_SEND_OPERATION_MIGRATION,))
            conn.execute("DROP TABLE message_send_operations")
            conn.execute("CREATE TABLE message_send_operations (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            conn.execute("INSERT INTO message_send_operations DEFAULT VALUES")
            with self.assertRaisesRegex(RuntimeError, "cannot be reconstructed safely"):
                apply_message_send_operation_migration(conn)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(message_send_operations)")
            }
            self.assertEqual({"id"}, columns)
            self.assertEqual(
                1,
                conn.execute("SELECT COUNT(*) AS count FROM message_send_operations").fetchone()["count"],
            )
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name=?",
                    (MESSAGE_SEND_OPERATION_MIGRATION,),
                ).fetchone()
            )

    def test_post_slice09_schema_adds_outbox_without_replacing_identity(self) -> None:
        with db.get_connection() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE name=?", (MESSAGE_SEND_OPERATION_MIGRATION,))
            conn.execute("DROP TABLE message_send_operations")
            self.assertTrue(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name='20260806_message_identity'"
                ).fetchone()
            )
        db.init_db()
        with db.get_connection() as conn:
            assert_message_send_operation_schema(conn)
            identity_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(messages)")
            }
            self.assertTrue(
                {"is_crm_sent", "crm_author_user_id", "crm_author_label", "client_operation_id"}
                <= identity_columns
            )

    def test_identity_schema_and_outbox_schema_coexist(self) -> None:
        with db.get_connection() as conn:
            message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
            operation_columns = {row["name"] for row in conn.execute("PRAGMA table_info(message_send_operations)")}
            self.assertTrue({"is_crm_sent", "crm_author_user_id", "crm_author_label", "client_operation_id"} <= message_columns)
            self.assertTrue({"payload_json", "payload_hash", "claim_token", "lease_until", "canonical_message_id"} <= operation_columns)

    def test_startup_schedules_outbox_without_awaiting_marketplace_io(self) -> None:
        startup_source = inspect.getsource(main.on_startup)
        self.assertNotIn("await message_send_service.drain_once", startup_source)
        self.assertIn("create_task(_message_send_outbox_loop())", startup_source)

    def test_frontend_contract_has_one_transient_or_canonical_row(self) -> None:
        source = Path(main.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("mergeMessagesWithSendOperations", source)
        self.assertIn("if (operation?.canonical_message_id) return false", source)
        self.assertIn("canonicalOperationIds.has", source)
        self.assertIn("pending: 'В очереди'", source)
        self.assertIn("uncertain: 'Результат отправки неизвестен'", source)
        self.assertNotIn("JSON.stringify({ text, author: 'manager', operation_id", source)
        self.assertNotIn("sendCurrentChatMessageWithRetry", source)

    def test_frontend_caption_waits_before_upload_and_does_not_post_caption_multipart(self) -> None:
        source = Path(main.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        caption_call = source.index("intentOrigin: imageFiles.length ? 'attachment_caption' : 'message'")
        upload_call = source.index("await uploadCurrentChatImages", caption_call)
        self.assertLess(caption_call, upload_call)
        self.assertIn("assertMessageOperationAllowsAttachmentUpload(messageOperation)", source[caption_call:upload_call])
        self.assertNotIn("formData.append('caption'", source)
        self.assertIn("Подпись отправлена; вложения не загружены. Выберите файлы повторно.", source)

    def test_only_canonical_service_calls_connector_send_message(self) -> None:
        app_root = Path(main.BASE_DIR)
        call_sites: list[str] = []
        for path in app_root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if ".send_message(" in source:
                call_sites.append(path.name)
        self.assertEqual(["message_send_service.py"], sorted(call_sites))


if __name__ == "__main__":
    unittest.main()


def test_pre_slice09_database_migrates_identity_then_outbox(tmp_path) -> None:
    database_path = tmp_path / "pre-slice09.sqlite3"
    conn = foundation._REAL_SQLITE_CONNECT(database_path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                customer_name TEXT,
                customer_public_id TEXT,
                order_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                assigned_to TEXT,
                assigned_user_id INTEGER,
                last_message_at TEXT,
                last_message_preview TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(marketplace, external_chat_id)
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'manager',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                external_message_id TEXT,
                direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound', 'internal')),
                author TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                raw_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );
            INSERT INTO users(id, username, display_name, password_hash)
            VALUES (1, 'legacy-user', 'Legacy User', 'test-hash');
            INSERT INTO chats(id, marketplace, external_chat_id, metadata_json)
            VALUES (1, 'ozon', 'legacy-chat', '{}');
            INSERT INTO messages(
                id, chat_id, direction, author, text, raw_json
            ) VALUES (
                1, 1, 'outbound', 'manager', 'Legacy CRM reply',
                '{"_crm_sent_from_crm":true,"_crm_sent_by_user_id":1,"_crm_client_operation_id":"legacy-op"}'
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    with mock.patch.object(db, "DATABASE_PATH", str(database_path)), mock.patch.object(
        db.sqlite3,
        "connect",
        new=foundation._REAL_SQLITE_CONNECT,
    ):
        db.init_db()
        db.init_db()

    conn = foundation._REAL_SQLITE_CONNECT(database_path)
    conn.row_factory = db.sqlite3.Row
    try:
        assert_message_send_operation_schema(conn)
        message = conn.execute("SELECT * FROM messages WHERE id=1").fetchone()
        self_markers = {
            row["name"] for row in conn.execute("SELECT name FROM schema_migrations")
        }
        assert message["is_crm_sent"] == 1
        assert message["crm_author_user_id"] == 1
        assert message["crm_author_label"] == "Legacy User"
        assert message["client_operation_id"] == "legacy-op"
        assert "20260806_message_identity" in self_markers
        assert MESSAGE_SEND_OPERATION_MIGRATION in self_markers
    finally:
        conn.close()
