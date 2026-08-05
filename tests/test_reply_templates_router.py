from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx


_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from app.reply_templates_router import create_reply_templates_router  # noqa: E402
from app.schemas import ReplyTemplateCreate, ReplyTemplateUpdate  # noqa: E402


main = foundation.main
db = foundation.db
repo = foundation.repo
_TEST_EVENT_LOOP = foundation._TEST_EVENT_LOOP


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
        self.update_result: Any = {"id": 12, "title": "Updated"}
        self.delete_result = True

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

    def update_reply_template(
        self,
        template_id: int,
        *,
        title: str | None,
        content: str | None,
        sort_order: int | None,
        is_active: bool | None,
        user_id: int,
    ) -> dict[str, Any] | None:
        self.calls.append(
            (
                "update_reply_template",
                template_id,
                title,
                content,
                sort_order,
                is_active,
                user_id,
            )
        )
        return self.update_result

    def delete_reply_template(self, template_id: int) -> bool:
        self.calls.append(("delete_reply_template", template_id))
        return self.delete_result


class ReplyTemplatesRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _RecordingRepository()
        self.user = {"id": 7, "role": "admin", "is_active": True}
        self.router = create_reply_templates_router(
            self.repo,
            lambda _request: self.user,
            lambda _request: self.user,
        )

    def test_route_paths_methods_and_main_registration_are_unchanged(self) -> None:
        expected = {
            ("/api/reply-templates", "GET"),
            ("/api/reply-templates", "POST"),
            ("/api/reply-templates/{template_id}", "PATCH"),
            ("/api/reply-templates/{template_id}", "DELETE"),
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
        router = create_reply_templates_router(self.repo, main._current_user, main._require_admin)
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
            router = create_reply_templates_router(
                repo,
                lambda _request, user=user: user,
                lambda _request, user=user: user,
            )
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
        router = create_reply_templates_router(self.repo, main._current_user, main._require_admin)
        endpoint = _route(router, "/api/reply-templates", "POST").endpoint
        payload = ReplyTemplateCreate.model_construct(title="Local", content="Text", sort_order=1)
        with mock.patch.object(main, "AUTH_DISABLED", True):
            endpoint(payload, _request_without_user())
        self.assertEqual(
            [("create_reply_template", "Local", "Text", 1, 0)],
            self.repo.calls,
        )

    def test_viewer_is_denied_reply_template_creation(self) -> None:
        viewer = {"id": 8, "role": "viewer", "is_active": True}
        request = _request_without_user()
        request.state.user = viewer
        router = create_reply_templates_router(self.repo, main._current_user, main._require_admin)
        endpoint = _route(router, "/api/reply-templates", "POST").endpoint
        payload = ReplyTemplateCreate.model_construct(title="Viewer", content="Denied", sort_order=1)
        with self.assertRaises(HTTPException) as error:
            endpoint(payload, request)
        self.assertEqual(403, error.exception.status_code)
        self.assertEqual("Нужны права администратора", error.exception.detail)
        self.assertEqual([], self.repo.calls)

    def test_update_and_delete_preserve_repository_contract(self) -> None:
        update_payload = ReplyTemplateUpdate.model_construct(
            title="Updated",
            content="Updated text",
            sort_order=4,
            is_active=True,
        )
        update_endpoint = _route(
            self.router,
            "/api/reply-templates/{template_id}",
            "PATCH",
        ).endpoint
        delete_endpoint = _route(
            self.router,
            "/api/reply-templates/{template_id}",
            "DELETE",
        ).endpoint

        self.assertIs(
            self.repo.update_result,
            update_endpoint(12, update_payload, _request_without_user()),
        )
        self.assertEqual(
            {
                "ok": True,
                "template_id": 12,
            },
            delete_endpoint(12, _request_without_user()),
        )
        self.assertEqual(
            [
                (
                    "update_reply_template",
                    12,
                    "Updated",
                    "Updated text",
                    4,
                    True,
                    7,
                ),
                ("delete_reply_template", 12),
            ],
            self.repo.calls,
        )

    def test_viewer_and_manager_are_denied_update_and_delete(self) -> None:
        payload = ReplyTemplateUpdate.model_construct(title="Denied")
        for role in ("viewer", "manager"):
            with self.subTest(role=role):
                restricted_repo = _RecordingRepository()
                user = {"id": 8, "role": role, "is_active": True}
                router = create_reply_templates_router(
                    restricted_repo,
                    lambda _request, user=user: user,
                    main._require_admin,
                )
                request = _request_without_user()
                request.state.user = user
                update_endpoint = _route(
                    router,
                    "/api/reply-templates/{template_id}",
                    "PATCH",
                ).endpoint
                delete_endpoint = _route(
                    router,
                    "/api/reply-templates/{template_id}",
                    "DELETE",
                ).endpoint

                with self.assertRaises(HTTPException) as update_error:
                    update_endpoint(12, payload, request)
                with self.assertRaises(HTTPException) as delete_error:
                    delete_endpoint(12, request)

                self.assertEqual(403, update_error.exception.status_code)
                self.assertEqual(403, delete_error.exception.status_code)
                self.assertEqual([], restricted_repo.calls)

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


async def _client_for_user(user: dict[str, Any]) -> httpx.AsyncClient:
    token = repo.create_session(int(user["id"]), user_agent="reply-template-test")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )
    client.cookies.set(main.AUTH_COOKIE_NAME, token)
    return client


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


class ReplyTemplatesHttpSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.admin = repo.create_user("reply-admin", "reply-admin-password", "Admin", "admin")
        self.viewer = repo.create_user("reply-viewer", "reply-viewer-password", "Viewer", "viewer")
        self.manager = repo.create_user("reply-manager", "reply-manager-password", "Manager", "manager")

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_admin_update_and_delete_work_without_page_reload(self) -> None:
        async def exercise():
            async with await _client_for_user(self.admin) as client:
                headers = await _csrf_headers(client)
                created = await client.post(
                    "/api/reply-templates",
                    json={"title": "Greeting", "content": "Hello", "sort_order": 0},
                    headers=headers,
                )
                template_id = int(created.json()["id"])
                updated = await client.patch(
                    f"/api/reply-templates/{template_id}",
                    json={"title": "Updated greeting", "content": "Updated text"},
                    headers=headers,
                )
                deleted = await client.delete(
                    f"/api/reply-templates/{template_id}",
                    headers=headers,
                )
                listed = await client.get("/api/reply-templates")
                return created, updated, deleted, listed, template_id

        created, updated, deleted, listed, template_id = _TEST_EVENT_LOOP.run_until_complete(exercise())
        self.assertEqual(200, created.status_code)
        self.assertEqual(200, updated.status_code)
        self.assertEqual("Updated greeting", updated.json()["title"])
        self.assertEqual("Updated text", updated.json()["content"])
        self.assertEqual(200, deleted.status_code)
        self.assertEqual({"ok": True, "template_id": template_id}, deleted.json())
        self.assertNotIn(template_id, [item["id"] for item in listed.json()])

    def test_viewer_and_manager_mutations_are_forbidden(self) -> None:
        template = repo.create_reply_template(title="Protected", content="Text", user_id=int(self.admin["id"]))

        async def exercise(user: dict[str, Any]):
            async with await _client_for_user(user) as client:
                headers = await _csrf_headers(client)
                updated = await client.patch(
                    f"/api/reply-templates/{template['id']}",
                    json={"title": "Denied"},
                    headers=headers,
                )
                deleted = await client.delete(
                    f"/api/reply-templates/{template['id']}",
                    headers=headers,
                )
                return updated, deleted

        for user in (self.viewer, self.manager):
            with self.subTest(role=user["role"]):
                updated, deleted = _TEST_EVENT_LOOP.run_until_complete(exercise(user))
                self.assertEqual(403, updated.status_code)
                self.assertEqual(403, deleted.status_code)

        preserved = repo.get_reply_template(int(template["id"]))
        self.assertIsNotNone(preserved)
        self.assertEqual("Protected", preserved["title"])

    def test_update_and_delete_require_csrf(self) -> None:
        template = repo.create_reply_template(title="Protected", content="Text", user_id=int(self.admin["id"]))

        async def exercise():
            async with await _client_for_user(self.admin) as client:
                updated = await client.patch(
                    f"/api/reply-templates/{template['id']}",
                    json={"title": "Blocked"},
                )
                deleted = await client.delete(f"/api/reply-templates/{template['id']}")
                return updated, deleted

        updated, deleted = _TEST_EVENT_LOOP.run_until_complete(exercise())
        self.assertEqual(403, updated.status_code)
        self.assertEqual(403, deleted.status_code)
        self.assertIn("CSRF", updated.json()["detail"])
        self.assertIn("CSRF", deleted.json()["detail"])
        preserved = repo.get_reply_template(int(template["id"]))
        self.assertIsNotNone(preserved)
        self.assertEqual("Protected", preserved["title"])


if __name__ == "__main__":
    unittest.main()
