from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from test_chat_read_state_ui import _extract_function, _run_node  # noqa: E402
import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


main = foundation.main


class ChatListInfiniteScrollTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.user = repo.create_user(
            "infinite-scroll-manager",
            "infinite-scroll-manager-password",
            "Infinite Scroll Manager",
            "manager",
        )
        start = datetime.now(timezone.utc) - timedelta(seconds=205)
        self.chat_ids: list[int] = []
        for index in range(205):
            chat_id = repo.upsert_chat(
                ChatCreate(
                    marketplace="ozon",
                    external_chat_id=f"infinite-scroll-chat-{index:03d}",
                    customer_name=f"Synthetic {index:03d}",
                    metadata={"synthetic": True},
                )
            )
            repo.add_message(
                chat_id,
                "inbound",
                f"message {index:03d}",
                author="synthetic-customer",
                external_message_id=f"infinite-scroll-message-{index:03d}",
                raw={"synthetic": True},
                created_at=(start + timedelta(seconds=index)).isoformat(),
            )
            self.chat_ids.append(chat_id)

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_repository_returns_thirty_item_batches_and_canonical_counts(self) -> None:
        first = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=30,
            offset=0,
        )
        last = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=30,
            offset=180,
        )

        self.assertEqual(205, first["total"])
        self.assertEqual(205, first["unread_total"])
        self.assertEqual(30, len(first["items"]))
        self.assertTrue(first["has_more"])
        self.assertFalse(first["has_previous"])
        self.assertEqual(30, first["next_offset"])
        self.assertEqual(self.chat_ids[-1], first["items"][0]["id"])

        self.assertEqual(180, last["offset"])
        self.assertEqual(25, len(last["items"]))
        self.assertFalse(last["has_more"])
        self.assertTrue(last["has_previous"])
        self.assertIsNone(last["next_offset"])
        self.assertEqual(self.chat_ids[0], last["items"][-1]["id"])

    def test_out_of_range_offset_is_moved_to_last_batch(self) -> None:
        batch = repo.list_chats_page(
            current_user_id=int(self.user["id"]),
            limit=30,
            offset=9999,
        )

        self.assertEqual(180, batch["offset"])
        self.assertEqual(25, len(batch["items"]))

    def test_api_keeps_legacy_list_and_defaults_paginated_contract_to_thirty(self) -> None:
        token = repo.create_session(int(self.user["id"]), user_agent="infinite-scroll-test")

        async def exercise():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="https://testserver",
            ) as client:
                client.cookies.set(main.AUTH_COOKIE_NAME, token)
                legacy = await client.get("/api/chats")
                first = await client.get("/api/chats", params={"paginated": "true"})
                second = await client.get(
                    "/api/chats",
                    params={"paginated": "true", "limit": 30, "offset": 30},
                )
                return legacy, first, second

        legacy, first, second = asyncio.run(exercise())

        self.assertEqual(200, legacy.status_code)
        self.assertIsInstance(legacy.json(), list)
        self.assertEqual(205, len(legacy.json()))

        self.assertEqual(200, first.status_code)
        first_payload = first.json()
        self.assertEqual(205, first_payload["total"])
        self.assertEqual(0, first_payload["offset"])
        self.assertEqual(30, first_payload["limit"])
        self.assertEqual(30, len(first_payload["items"]))
        self.assertEqual(30, first_payload["next_offset"])

        self.assertEqual(200, second.status_code)
        second_payload = second.json()
        self.assertEqual(30, second_payload["offset"])
        self.assertEqual(30, len(second_payload["items"]))
        self.assertEqual(60, second_payload["next_offset"])


class ChatListInfiniteScrollUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        cls.merge_batch = _extract_function(cls.source, "mergeChatListBatch")

    def test_frontend_uses_thirty_item_infinite_scroll_without_page_buttons(self) -> None:
        self.assertIn("const CHAT_LIST_BATCH_SIZE = 30;", self.source)
        self.assertIn("params.set('paginated', 'true')", self.source)
        self.assertIn("params.set('offset', String(requestOffset))", self.source)
        self.assertIn("function loadMoreChats()", self.source)
        self.assertIn("const preserveScrollTop = requestMode === 'replace' ? null : Number(list?.scrollTop || 0);", self.source)
        self.assertIn("list.addEventListener('scroll', scheduleChatListInfiniteLoad, { passive: true })", self.source)
        self.assertIn('id="chatListLoadState"', self.html)
        self.assertNotIn('id="chatListPager"', self.html)
        self.assertNotIn('id="chatListPrevBtn"', self.html)
        self.assertNotIn('id="chatListNextBtn"', self.html)

    def test_batch_merge_deduplicates_append_and_preserves_loaded_tail_on_refresh(self) -> None:
        _run_node(
            f"""
            {self.merge_batch}
            const current = [{{ id: 3 }}, {{ id: 2 }}, {{ id: 1 }}];
            const appended = mergeChatListBatch(current, [{{ id: 1 }}, {{ id: 5 }}], 'append');
            if (appended.map(item => item.id).join(',') !== '3,2,1,5') {{
              throw new Error('append did not deduplicate ids');
            }}

            const refreshed = mergeChatListBatch(current, [{{ id: 4 }}, {{ id: 3 }}], 'refresh');
            if (refreshed.map(item => item.id).join(',') !== '4,3,2,1') {{
              throw new Error('refresh did not preserve the loaded tail');
            }}
            """
        )


if __name__ == "__main__":
    unittest.main()
