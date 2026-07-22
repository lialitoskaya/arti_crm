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

from app.reply_templates_router import create_reply_templates_router  # noqa: E402
from app.schemas import ReplyTemplateCreate  # noqa: E402


main = foundation.main


def _request_without_user() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/reply-templates",
            "raw_path": b"/api/reply-templates",
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
        self.create_result: Any = {"id": 12, "title": "Created"}
        self.create_error: ValueError | None = None

    def list_reply_templates(self, q: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(("list_reply_templates", q))
        return [{"id": 11, "title": "Existing"}]

    def create_reply_template(
        self,
        *,
        title: str,
        content: str,
        sort_order: int,
        user_id: int,
    ) -> dict[str, Any]:
        self.calls.append(("create_reply_template", title, content, sort_order, user_id))
        if self.create_error is not None:
            raise self.create_error
        return self.create_result


class ReplyTemplatesRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RecordingRepository()
        self.user = {"id": 7, "role": "viewer", "is_active": True}
        self.router = create_reply_templates_router(self.repo, lambda _request: self.user)

    def test_route_paths_methods_and_main_registration_are_unchanged(self) -> None:
        expected = {
            ("/api/reply-templates", "GET"),
            ("/api/reply-templates", "POST"),
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
        router = create_reply_templates_router(self.repo, main._current_user)
        endpoint = _route(router, "/api/reply-templates", "GET").endpoint
        with mock.patch.object(main, "AUTH_DISABLED", False):
            with self.assertRaises(HTTPException) as error:
                endpoint(_request_without_user(), None)
        self.assertEqual(401, error.exception.status_code)
        self.assertEqual("Требуется авторизация", error.exception.detail)
        self.assertEqual([], self.repo.calls)

    def test_lists_templates_for_viewer_and_admin_with_exact_arguments(self) -> None:
        for role in ("viewer", "admin"):
            repo = _RecordingRepository()
            user = {"id": 7, "role": role, "is_active": True}
            router = create_reply_templates_router(repo, lambda _request, user=user: user)
            endpoint = _route(router, "/api/reply-templates", "GET").endpoint
            with self.subTest(role=role):
                self.assertEqual([{"id": 11, "title": "Existing"}], endpoint(_request_without_user(), "hello"))
                self.assertEqual([("list_reply_templates", "hello")], repo.calls)

    def test_create_preserves_repository_arguments_and_exact_payload(self) -> None:
        payload = ReplyTemplateCreate.model_construct(title="Greeting", content="Hello", sort_order=9)
        endpoint = _route(self.router, "/api/reply-templates", "POST").endpoint
        result = endpoint(payload, _request_without_user())
        self.assertIs(self.repo.create_result, result)
        self.assertEqual(
            [("create_reply_template", "Greeting", "Hello", 9, 7)],
            self.repo.calls,
        )

    def test_create_preserves_value_error_contract(self) -> None:
        payload = ReplyTemplateCreate.model_construct(title="Greeting", content="Hello", sort_order=9)
        endpoint = _route(self.router, "/api/reply-templates", "POST").endpoint
        self.repo.create_error = ValueError("duplicate template")
        with self.assertRaises(HTTPException) as error:
            endpoint(payload, _request_without_user())
        self.assertEqual(400, error.exception.status_code)
        self.assertEqual("duplicate template", error.exception.detail)
        self.assertEqual(
            [("create_reply_template", "Greeting", "Hello", 9, 7)],
            self.repo.calls,
        )

    def test_auth_disabled_semantics_preserve_local_user_id(self) -> None:
        router = create_reply_templates_router(self.repo, main._current_user)
        endpoint = _route(router, "/api/reply-templates", "POST").endpoint
        payload = ReplyTemplateCreate.model_construct(title="Local", content="Text", sort_order=1)
        with mock.patch.object(main, "AUTH_DISABLED", True):
            endpoint(payload, _request_without_user())
        self.assertEqual(
            [("create_reply_template", "Local", "Text", 1, 0)],
            self.repo.calls,
        )

    def test_router_has_no_main_db_network_or_environment_imports(self) -> None:
        module_path = Path(sys.modules[create_reply_templates_router.__module__].__file__).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual({"__future__", "collections.abc", "typing", "fastapi", "app.schemas"}, imported_modules)


if __name__ == "__main__":
    unittest.main()
