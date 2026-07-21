from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from app.notifications_router import create_notifications_router  # noqa: E402


main = foundation.main


def _request_without_user() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/notifications",
            "raw_path": b"/api/notifications",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def _route(router, path: str, method: str) -> APIRoute:
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path == path and method in route.methods
    )


class _RecordingRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_notifications(self, user_id: int, limit: int = 30, unread_only: bool = False) -> dict[str, Any]:
        self.calls.append(("list_notifications", user_id, limit, unread_only))
        return {"items": [{"id": 11}], "unread_count": 4}

    def mark_notification_read(self, notification_id: int, user_id: int) -> bool:
        self.calls.append(("mark_notification_read", notification_id, user_id))
        return notification_id == 41

    def mark_all_notifications_read(self, user_id: int) -> int:
        self.calls.append(("mark_all_notifications_read", user_id))
        return 3


class NotificationsRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RecordingRepository()
        self.user = {"id": 7, "role": "viewer", "is_active": True}
        self.router = create_notifications_router(self.repo, lambda _request: self.user)

    def test_route_paths_methods_and_main_registration_are_unchanged(self) -> None:
        expected = {
            ("/api/notifications", "GET"),
            ("/api/notifications/{notification_id}/read", "POST"),
            ("/api/notifications/read-all", "POST"),
        }
        actual = {
            (route.path, method)
            for route in self.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        self.assertEqual(expected, actual)
        for path, method in expected:
            matches = [
                route
                for route in main.app.routes
                if isinstance(route, APIRoute) and route.path == path and method in route.methods
            ]
            self.assertEqual(1, len(matches))
            self.assertIsNone(matches[0].status_code)

    def test_missing_user_preserves_401_contract_without_repository_calls(self) -> None:
        router = create_notifications_router(self.repo, main._current_user)
        endpoint = _route(router, "/api/notifications", "GET").endpoint
        with mock.patch.object(main, "AUTH_DISABLED", False):
            with self.assertRaises(HTTPException) as error:
                endpoint(_request_without_user(), limit=30, unread_only=False)
        self.assertEqual(401, error.exception.status_code)
        self.assertEqual("Требуется авторизация", error.exception.detail)
        self.assertEqual([], self.repo.calls)

    def test_lists_only_current_user_notifications_with_exact_arguments(self) -> None:
        endpoint = _route(self.router, "/api/notifications", "GET").endpoint
        result = endpoint(_request_without_user(), limit=12, unread_only=True)
        self.assertEqual({"items": [{"id": 11}], "unread_count": 4}, result)
        self.assertEqual([("list_notifications", 7, 12, True)], self.repo.calls)

    def test_marks_one_notification_and_returns_exact_payload(self) -> None:
        endpoint = _route(self.router, "/api/notifications/{notification_id}/read", "POST").endpoint
        result = endpoint(41, _request_without_user())
        self.assertEqual({"ok": True, "unread_count": 4}, result)
        self.assertEqual(
            [("mark_notification_read", 41, 7), ("list_notifications", 7, 1, False)],
            self.repo.calls,
        )

    def test_marks_all_notifications_and_returns_exact_payload(self) -> None:
        endpoint = _route(self.router, "/api/notifications/read-all", "POST").endpoint
        result = endpoint(_request_without_user())
        self.assertEqual({"ok": True, "marked": 3, "unread_count": 0}, result)
        self.assertEqual([("mark_all_notifications_read", 7)], self.repo.calls)

    def test_auth_disabled_semantics_are_preserved_through_injected_dependency(self) -> None:
        router = create_notifications_router(self.repo, main._current_user)
        endpoint = _route(router, "/api/notifications", "GET").endpoint
        with mock.patch.object(main, "AUTH_DISABLED", True):
            result = endpoint(_request_without_user(), limit=30, unread_only=False)
        self.assertEqual({"items": [{"id": 11}], "unread_count": 4}, result)
        self.assertEqual([("list_notifications", 0, 30, False)], self.repo.calls)

    def test_router_module_has_no_app_db_network_or_environment_imports(self) -> None:
        module_path = Path(sys.modules[create_notifications_router.__module__].__file__).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual({"__future__", "collections", "typing", "fastapi"}, imported_roots)


if __name__ == "__main__":
    unittest.main()
