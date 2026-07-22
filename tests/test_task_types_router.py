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

from app.schemas import TaskTypeCreate, TaskTypeUpdate  # noqa: E402
from app.task_types_router import create_task_types_router  # noqa: E402


main = foundation.main


def _request_without_user() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/task-types",
            "raw_path": b"/api/task-types",
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
        self.create_result: Any = {"id": 11, "name": "Created"}
        self.update_result: Any = {"id": 12, "name": "Updated"}
        self.delete_result = True
        self.create_error: ValueError | None = None

    def list_task_types(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        self.calls.append(("list_task_types", include_inactive))
        return [{"id": 10, "name": "Existing"}]

    def create_task_type(self, payload: TaskTypeCreate) -> dict[str, Any]:
        self.calls.append(("create_task_type", payload))
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def update_task_type(self, type_id: int, payload: TaskTypeUpdate) -> dict[str, Any] | None:
        self.calls.append(("update_task_type", type_id, payload))
        return self.update_result

    def delete_task_type(self, type_id: int) -> bool:
        self.calls.append(("delete_task_type", type_id))
        return self.delete_result


class TaskTypesRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RecordingRepository()
        self.user = {"id": 7, "role": "viewer", "is_active": True}
        self.router = create_task_types_router(self.repo, lambda _request: self.user)

    def test_route_paths_methods_and_main_registration_are_unchanged(self) -> None:
        expected = {
            ("/api/task-types", "GET"),
            ("/api/task-types", "POST"),
            ("/api/task-types/{type_id}", "PATCH"),
            ("/api/task-types/{type_id}", "DELETE"),
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

    def test_list_preserves_permissions_arguments_and_payload(self) -> None:
        for role in ("viewer", "admin"):
            repo = _RecordingRepository()
            user = {"id": 7, "role": role, "is_active": True}
            router = create_task_types_router(repo, lambda _request, user=user: user)
            endpoint = _route(router, "/api/task-types", "GET").endpoint
            with self.subTest(role=role):
                self.assertEqual([{"id": 10, "name": "Existing"}], endpoint(_request_without_user(), True))
                self.assertEqual([("list_task_types", True)], repo.calls)

    def test_create_preserves_repository_call_payload_and_value_error_contract(self) -> None:
        payload = TaskTypeCreate.model_construct()
        endpoint = _route(self.router, "/api/task-types", "POST").endpoint
        result = endpoint(payload, _request_without_user())
        self.assertIs(self.repo.create_result, result)
        self.assertEqual([("create_task_type", payload)], self.repo.calls)

        self.repo.calls.clear()
        self.repo.create_error = ValueError("duplicate task type")
        with self.assertRaises(HTTPException) as error:
            endpoint(payload, _request_without_user())
        self.assertEqual(400, error.exception.status_code)
        self.assertEqual("duplicate task type", error.exception.detail)
        self.assertEqual([("create_task_type", payload)], self.repo.calls)

    def test_update_preserves_repository_call_payload_and_not_found_contract(self) -> None:
        payload = TaskTypeUpdate.model_construct()
        endpoint = _route(self.router, "/api/task-types/{type_id}", "PATCH").endpoint
        result = endpoint(12, payload, _request_without_user())
        self.assertIs(self.repo.update_result, result)
        self.assertEqual([("update_task_type", 12, payload)], self.repo.calls)

        self.repo.calls.clear()
        self.repo.update_result = None
        with self.assertRaises(HTTPException) as error:
            endpoint(404, payload, _request_without_user())
        self.assertEqual(404, error.exception.status_code)
        self.assertEqual("Task type not found", error.exception.detail)
        self.assertEqual([("update_task_type", 404, payload)], self.repo.calls)

    def test_delete_preserves_repository_call_payload_and_not_found_contract(self) -> None:
        endpoint = _route(self.router, "/api/task-types/{type_id}", "DELETE").endpoint
        self.assertEqual({"ok": True}, endpoint(12, _request_without_user()))
        self.assertEqual([("delete_task_type", 12)], self.repo.calls)

        self.repo.calls.clear()
        self.repo.delete_result = False
        with self.assertRaises(HTTPException) as error:
            endpoint(404, _request_without_user())
        self.assertEqual(404, error.exception.status_code)
        self.assertEqual("Task type not found", error.exception.detail)
        self.assertEqual([("delete_task_type", 404)], self.repo.calls)

    def test_missing_user_preserves_401_contract_without_repository_calls(self) -> None:
        router = create_task_types_router(self.repo, main._current_user)
        endpoint = _route(router, "/api/task-types", "GET").endpoint
        with mock.patch.object(main, "AUTH_DISABLED", False):
            with self.assertRaises(HTTPException) as error:
                endpoint(_request_without_user(), False)
        self.assertEqual(401, error.exception.status_code)
        self.assertEqual("Требуется авторизация", error.exception.detail)
        self.assertEqual([], self.repo.calls)

    def test_router_has_no_main_db_network_or_environment_imports(self) -> None:
        module_path = Path(sys.modules[create_task_types_router.__module__].__file__).resolve()
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
