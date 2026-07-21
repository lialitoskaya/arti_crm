from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from typing import Any


_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from app import marketplace_sender  # noqa: E402
from app.connectors.ozon import OzonConnector  # noqa: E402


main = foundation.main
repo = foundation.repo


def _legacy_normalize_system_sender(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").strip().lower())


def _legacy_system_sender_matches(value: Any, markers: tuple[str, ...]) -> bool:
    normalized = _legacy_normalize_system_sender(value)
    if not normalized:
        return False
    normalized_markers = {_legacy_normalize_system_sender(marker) for marker in markers if marker}
    return normalized in normalized_markers


def _legacy_extract_sender_designations(value: Any, depth: int = 0) -> list[str]:
    if depth > 4 or value in (None, ""):
        return []
    values: list[str] = []
    name_keys = {
        "name", "login", "username", "user_name", "display_name", "displayname",
        "nickname", "author_name", "authorname", "sender_name", "sendername",
        "from_name", "fromname", "system_name", "systemname",
    }
    container_keys = {"user", "author", "sender", "from", "participant", "profile"}
    if isinstance(value, dict):
        for key, nested in value.items():
            key_l = str(key).lower()
            if key_l in name_keys and isinstance(nested, (str, int, float)):
                values.append(str(nested))
            elif key_l in container_keys:
                if isinstance(nested, (str, int, float)):
                    values.append(str(nested))
                elif isinstance(nested, dict):
                    values.extend(_legacy_extract_sender_designations(nested, depth + 1))
    elif isinstance(value, list):
        for item in value[:20]:
            values.extend(_legacy_extract_sender_designations(item, depth + 1))
    return values


class MarketplaceSenderParityTests(unittest.TestCase):
    normalize_seams = (
        marketplace_sender.normalize_system_sender,
        main._normalize_system_sender,
        repo._normalize_system_sender,
        OzonConnector._normalize_system_sender,
    )
    match_seams = (
        marketplace_sender.system_sender_matches,
        main._system_sender_matches,
        repo._system_sender_matches,
        OzonConnector._system_sender_matches,
    )
    extract_seams = (
        marketplace_sender.extract_sender_designations,
        main._extract_sender_designations,
        repo._extract_sender_designations,
        OzonConnector._exact_sender_designations_from_payload,
    )

    def test_normalization_parity_for_empty_case_and_separators(self) -> None:
        cases = (
            (None, ""),
            ("", ""),
            (" \t\n ", ""),
            ("Chat_Bot", "chatbot"),
            (" Notification- User ", "notificationuser"),
            (0, ""),
            (17, "17"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, _legacy_normalize_system_sender(value))
                for seam in self.normalize_seams:
                    self.assertEqual(expected, seam(value))

    def test_matching_parity_remains_exact_not_substring(self) -> None:
        cases = (
            (None, ("chatbot",), False),
            ("CHAT_BOT", ("chat bot",), True),
            ("notification-user", ("notification_user",), True),
            ("chatbot-user", ("chatbot",), False),
            ("prefixchatbot", ("chatbot",), False),
            ("chatbot", ("", "CHAT-BOT"), True),
        )
        for value, markers, expected in cases:
            with self.subTest(value=value, markers=markers):
                self.assertEqual(expected, _legacy_system_sender_matches(value, markers))
                for seam in self.match_seams:
                    self.assertEqual(expected, seam(value, markers))

    def test_extraction_parity_for_scalars_collections_and_allowed_keys(self) -> None:
        name_payload = {
            "name": "n1",
            "LOGIN": "n2",
            "username": "n3",
            "user_name": "n4",
            "display_name": "n5",
            "displayname": "n6",
            "nickname": "n7",
            "author_name": "n8",
            "authorname": "n9",
            "sender_name": "n10",
            "sendername": "n11",
            "from_name": "n12",
            "fromname": "n13",
            "system_name": "n14",
            "systemname": "n15",
        }
        cases = (
            (None, []),
            ("", []),
            ("chatbot", []),
            ({"sender": "Chat Bot"}, ["Chat Bot"]),
            (name_payload, [f"n{i}" for i in range(1, 16)]),
            (
                [{"author": {"user_name": "First"}}, {"profile": {"displayName": "Second"}}],
                ["First", "Second"],
            ),
            ({"text": "chatbot", "status": "notificationuser", "type": "system"}, []),
            ({"customer": {"name": "ignored"}, "sender": [{"name": "also ignored"}]}, []),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(expected, _legacy_extract_sender_designations(payload))
                for seam in self.extract_seams:
                    self.assertEqual(expected, seam(payload))

    def test_extraction_parity_preserves_depth_and_list_limits(self) -> None:
        depth_four = {"user": {"user": {"user": {"user": {"name": "kept"}}}}}
        depth_five = {"user": {"user": {"user": {"user": {"user": {"name": "ignored"}}}}}}
        long_list = [{"name": str(index)} for index in range(25)]
        cases = (
            (depth_four, ["kept"]),
            (depth_five, []),
            (long_list, [str(index) for index in range(20)]),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(expected, _legacy_extract_sender_designations(payload))
                for seam in self.extract_seams:
                    self.assertEqual(expected, seam(payload))

    def test_mixed_chatbot_and_customer_messages_preserve_customer_message(self) -> None:
        chatbot = SimpleNamespace(author="chatbot", raw={"sender": {"name": "chatbot"}})
        customer = SimpleNamespace(author="customer", raw={"text": "chatbot", "status": "system"})
        with mock.patch.dict(
            foundation.os.environ,
            {"OZON_EXCLUDE_CHATBOT_MESSAGES": "1", "OZON_CHATBOT_MARKERS": "chatbot"},
        ):
            filtered = main._filter_ozon_chatbot_messages([chatbot, customer])
        self.assertEqual([customer], filtered)


if __name__ == "__main__":
    unittest.main()
