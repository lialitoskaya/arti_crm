from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Any


_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from app.chat_settings_router import create_chat_settings_router  # noqa: E402
from app.schemas import ChatFunnelCreate, ChatFunnelUpdate, ChatStatusCreate, ChatStatusUpdate  # noqa: E402


main = foundation.main


def _request_for(user: dict[str, Any] | None = None, *, include_user: bool = True) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/chat-settings",
            "raw_path": b"/api/chat-settings",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )
    if include_user:
        request.state.user = user
    return request


def _route(router, path: str, method: str) -> APIRoute:
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path == path and method in route.methods
    )


class _RecordingRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.errors: dict[str, ValueError] = {}
        self.update_funnel_result: Any = {"id": 11, "title": "Renamed"}
        self.delete_funnel_result = True
        self.update_status_result: Any = {"id": 21, "title": "Done"}
        self.delete_status_result = True

    def _raise(self, method: str) -> None:
        if method in self.errors:
            raise self.errors[method]

    def get_chat_settings(self) -> dict[str, Any]:
        self.calls.append(("get_chat_settings",))
        return {"funnels": [{"id": 1}], "statuses": [{"id": 2}]}

    def create_chat_funnel(self, title: str, sort_order: int) -> dict[str, Any]:
        self.calls.append(("create_chat_funnel", title, sort_order))
        self._raise("create_chat_funnel")
        return {"id": 10, "title": title}

    def update_chat_funnel(self, funnel_id: int, updates: dict[str, Any]) -> Any:
        self.calls.append(("update_chat_funnel", funnel_id, updates))
        self._raise("update_chat_funnel")
        return self.update_funnel_result

    def delete_chat_funnel(self, funnel_id: int) -> bool:
        self.calls.append(("delete_chat_funnel", funnel_id))
        self._raise("delete_chat_funnel")
        return self.delete_funnel_result

    def create_chat_status(
        self,
        title: str,
        key: str,
        funnel_id: int,
        color: str,
        sort_order: int,
    ) -> dict[str, Any]:
        self.calls.append(("create_chat_status", title, key, funnel_id, color, sort_order))
        self._raise("create_chat_status")
        return {"id": 20, "title": title}

    def update_chat_status(self, status_id: int, updates: dict[str, Any]) -> Any:
        self.calls.append(("update_chat_status", status_id, updates))
        return self.update_status_result

    def delete_chat_status(self, status_id: int) -> bool:
        self.calls.append(("delete_chat_status", status_id))
        return self.delete_status_result


class ChatSettingsRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RecordingRepository()
        self.admin = {"id": 1, "role": "admin", "is_active": True}
        self.admin_calls: list[Request] = []

        def require_admin(request: Request) -> dict[str, Any]:
            self.admin_calls.append(request)
            return main._require_admin(request)

        self.router = create_chat_settings_router(self.repo, require_admin)
        self.admin_request = _request_for(self.admin)
        self.funnel_create = ChatFunnelCreate.model_construct(title="Sales", sort_order=3)
        self.funnel_update = ChatFunnelUpdate.model_construct(title="Renamed")
        self.status_create = ChatStatusCreate.model_construct(
            title="New", key="new", funnel_id=11, color="#ffffff", sort_order=4
        )
        self.status_update = ChatStatusUpdate.model_construct(title="Done")

    def test_exact_routes_methods_and_no_router_level_dependency(self) -> None:
        expected = {
            ("/api/chat-settings", "GET"),
            ("/api/chat-settings/funnels", "POST"),
            ("/api/chat-settings/funnels/{funnel_id}", "PATCH"),
            ("/api/chat-settings/funnels/{funnel_id}", "DELETE"),
            ("/api/chat-settings/statuses", "POST"),
            ("/api/chat-settings/statuses/{status_id}", "PATCH"),
            ("/api/chat-settings/statuses/{status_id}", "DELETE"),
        }
        actual = {
            (route.path, method)
            for route in self.router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        self.assertEqual(expected, actual)
        self.assertEqual([], self.router.dependencies)
        for path, method in expected:
            matches = [
                route
                for route in main.app.routes
                if isinstance(route, APIRoute) and route.path == path and method in route.methods
            ]
            self.assertEqual(1, len(matches))
            self.assertIsNone(matches[0].status_code)

    def test_public_get_has_exact_payload_without_admin_dependency(self) -> None:
        router = create_chat_settings_router(
            self.repo,
            lambda _request: self.fail("public GET must not call admin dependency"),
        )
        result = _route(router, "/api/chat-settings", "GET").endpoint()
        self.assertEqual({"funnels": [{"id": 1}], "statuses": [{"id": 2}]}, result)
        self.assertEqual([("get_chat_settings",)], self.repo.calls)

    def test_admin_crud_preserves_exact_repository_calls_and_payloads(self) -> None:
        create_funnel = _route(self.router, "/api/chat-settings/funnels", "POST").endpoint
        update_funnel = _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "PATCH").endpoint
        delete_funnel = _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "DELETE").endpoint
        create_status = _route(self.router, "/api/chat-settings/statuses", "POST").endpoint
        update_status = _route(self.router, "/api/chat-settings/statuses/{status_id}", "PATCH").endpoint
        delete_status = _route(self.router, "/api/chat-settings/statuses/{status_id}", "DELETE").endpoint

        self.assertEqual({"id": 10, "title": "Sales"}, create_funnel(self.funnel_create, self.admin_request))
        self.assertIs(self.repo.update_funnel_result, update_funnel(11, self.funnel_update, self.admin_request))
        self.assertEqual({"ok": True}, delete_funnel(12, self.admin_request))
        self.assertEqual({"id": 20, "title": "New"}, create_status(self.status_create, self.admin_request))
        self.assertIs(self.repo.update_status_result, update_status(21, self.status_update, self.admin_request))
        self.assertEqual({"ok": True}, delete_status(22, self.admin_request))

        self.assertEqual(
            [
                ("create_chat_funnel", "Sales", 3),
                ("update_chat_funnel", 11, {"title": "Renamed"}),
                ("delete_chat_funnel", 12),
                ("create_chat_status", "New", "new", 11, "#ffffff", 4),
                ("update_chat_status", 21, {"title": "Done"}),
                ("delete_chat_status", 22),
            ],
            self.repo.calls,
        )
        self.assertEqual(6, len(self.admin_calls))

    def test_viewer_and_missing_user_preserve_403_for_every_crud_handler(self) -> None:
        invocations = (
            lambda request: _route(self.router, "/api/chat-settings/funnels", "POST").endpoint(
                self.funnel_create, request
            ),
            lambda request: _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "PATCH").endpoint(
                11, self.funnel_update, request
            ),
            lambda request: _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "DELETE").endpoint(11, request),
            lambda request: _route(self.router, "/api/chat-settings/statuses", "POST").endpoint(
                self.status_create, request
            ),
            lambda request: _route(self.router, "/api/chat-settings/statuses/{status_id}", "PATCH").endpoint(
                21, self.status_update, request
            ),
            lambda request: _route(self.router, "/api/chat-settings/statuses/{status_id}", "DELETE").endpoint(21, request),
        )
        for label, request in (
            ("viewer", _request_for({"id": 2, "role": "viewer", "is_active": True})),
            ("missing", _request_for(include_user=False)),
        ):
            for index, invoke in enumerate(invocations):
                with self.subTest(label=label, handler=index), self.assertRaises(HTTPException) as error:
                    invoke(request)
                self.assertEqual(403, error.exception.status_code)
                self.assertEqual("Нужны права администратора", error.exception.detail)
        self.assertEqual([], self.repo.calls)

    def test_existing_value_errors_preserve_400_contract(self) -> None:
        cases = (
            ("create_chat_funnel", lambda: _route(self.router, "/api/chat-settings/funnels", "POST").endpoint(
                self.funnel_create, self.admin_request
            )),
            ("update_chat_funnel", lambda: _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "PATCH").endpoint(
                11, self.funnel_update, self.admin_request
            )),
            ("delete_chat_funnel", lambda: _route(self.router, "/api/chat-settings/funnels/{funnel_id}", "DELETE").endpoint(
                11, self.admin_request
            )),
            ("create_chat_status", lambda: _route(self.router, "/api/chat-settings/statuses", "POST").endpoint(
                self.status_create, self.admin_request
            )),
        )
        for method, invoke in cases:
            self.repo.errors = {method: ValueError(f"{method} error")}
            with self.subTest(method=method), self.assertRaises(HTTPException) as error:
                invoke()
            self.assertEqual(400, error.exception.status_code)
            self.assertEqual(f"{method} error", error.exception.detail)

    def test_existing_not_found_results_preserve_404_contract(self) -> None:
        cases = (
            ("Funnel not found", "update_funnel_result", None, lambda: _route(
                self.router, "/api/chat-settings/funnels/{funnel_id}", "PATCH"
            ).endpoint(11, self.funnel_update, self.admin_request)),
            ("Funnel not found", "delete_funnel_result", False, lambda: _route(
                self.router, "/api/chat-settings/funnels/{funnel_id}", "DELETE"
            ).endpoint(11, self.admin_request)),
            ("Status not found", "update_status_result", None, lambda: _route(
                self.router, "/api/chat-settings/statuses/{status_id}", "PATCH"
            ).endpoint(21, self.status_update, self.admin_request)),
            ("Status not found", "delete_status_result", False, lambda: _route(
                self.router, "/api/chat-settings/statuses/{status_id}", "DELETE"
            ).endpoint(21, self.admin_request)),
        )
        for detail, attribute, value, invoke in cases:
            setattr(self.repo, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(HTTPException) as error:
                invoke()
            self.assertEqual(404, error.exception.status_code)
            self.assertEqual(detail, error.exception.detail)

    def test_router_has_no_main_db_network_or_environment_imports(self) -> None:
        module_path = Path(sys.modules[create_chat_settings_router.__module__].__file__).resolve()
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
