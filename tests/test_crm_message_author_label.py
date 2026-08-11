from __future__ import annotations

import unittest

import test_regression_foundation as foundation  # noqa: E402
from app import db  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402


class CrmMessageAuthorLabelTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        db.init_db()
        self.employee = repo.create_user(
            "author-label-employee",
            "author-label-password",
            "Лия",
            "manager",
        )
        self.chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="author-label-chat",
                customer_name="Synthetic Customer",
                metadata={},
            )
        )

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def _messages(self) -> list[dict[str, object]]:
        chat = repo.get_chat(self.chat_id, current_user_id=int(self.employee["id"]))
        self.assertIsNotNone(chat)
        return chat["messages"]

    def test_crm_outbound_uses_saved_employee_label(self) -> None:
        repo.add_message(
            self.chat_id,
            "outbound",
            "crm reply",
            author="seller",
            external_message_id="crm-author-label",
            raw={
                "_crm_sent_from_crm": True,
                "_crm_sent_by_label": "Лия",
                "_crm_sent_by_user_id": self.employee["id"],
            },
        )

        message = self._messages()[0]
        self.assertEqual("Лия", message["crm_author_label"])

    def test_crm_outbound_resolves_user_label_without_n_plus_one_fallback(self) -> None:
        repo.add_message(
            self.chat_id,
            "outbound",
            "crm reply by user id",
            author="manager",
            external_message_id="crm-author-user-id",
            raw={
                "_crm_sent_from_crm": True,
                "_crm_sent_by_user_id": self.employee["id"],
            },
        )

        message = self._messages()[0]
        self.assertEqual("Лия", message["crm_author_label"])

    def test_marketplace_outbound_does_not_fake_employee_identity(self) -> None:
        repo.add_message(
            self.chat_id,
            "outbound",
            "marketplace-origin outbound",
            author="seller",
            external_message_id="marketplace-outbound",
            raw={"marketplace_payload": True},
        )

        message = self._messages()[0]
        self.assertNotIn("crm_author_label", message)

    def test_inbound_message_never_gets_crm_employee_label(self) -> None:
        # Bypass add_message's outbound-direction repair so this test can prove
        # that presentation never trusts CRM markers on an inbound row.
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    chat_id, external_message_id, direction, author, text, created_at, raw_json
                ) VALUES (?, ?, 'inbound', 'customer', ?, CURRENT_TIMESTAMP, ?)
                """,
                (
                    self.chat_id,
                    "inbound-with-bad-marker",
                    "customer message",
                    '{"_crm_sent_from_crm": true, "_crm_sent_by_label": "Лия"}',
                ),
            )

        message = self._messages()[0]
        self.assertNotIn("crm_author_label", message)

    def test_local_crm_attachment_uses_nontechnical_author_fallback(self) -> None:
        repo.add_message(
            self.chat_id,
            "outbound",
            "local attachment",
            author="Лия",
            external_message_id="local-attachment",
            raw={"_crm_local_attachment": True},
        )

        message = self._messages()[0]
        self.assertEqual("Лия", message["crm_author_label"])


if __name__ == "__main__":
    unittest.main()
