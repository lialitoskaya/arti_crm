from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse

import test_regression_foundation as foundation  # noqa: E402
from app import auth_dependencies  # noqa: E402


main = foundation.main


_VIEWER = {"id": 7, "username": "viewer", "role": "viewer", "is_active": True}
_ADMIN = {"id": 1, "username": "admin", "role": "admin", "is_active": True}

_VIEWER_READ_ROUTES = (
    "/api/chats",
    "/api/tasks",
    "/api/analytics/chats",
    "/api/knowledge/articles",
    "/api/chat-settings",
)
_VIEWER_SELF_SERVICE_ROUTES = (
    ("POST", "/api/auth/logout"),
    ("PATCH", "/api/auth/profile"),
    ("POST", "/api/push/subscribe"),
    ("POST", "/api/push/unsubscribe"),
    ("POST", "/api/notifications/91/read"),
    ("POST", "/api/notifications/read-all"),
)
_ADMIN_ONLY_ROUTES = (
    ("GET", "/api/debug/local"),
    ("GET", "/api/debug/messages/1"),
    ("GET", "/api/debug/ozon/fast-sync"),
    ("GET", "/api/supply-planning/status"),
    ("GET", "/api/supply-planning/sync"),
    ("GET", "/api/supply-planning/sync/"),
    ("GET", "/api/chat-uploads/example.jpg"),
    ("POST", "/api/chats/1/messages"),
    ("POST", "/api/reviews/1/reply"),
    ("POST", "/api/questions/1/answer"),
    ("POST", "/api/chats/1/ai-reply"),
    ("POST", "/api/tasks"),
    ("PATCH", "/api/tasks/1"),
    ("DELETE", "/api/tasks/1"),
    ("POST", "/api/chats/1/attachments"),
    ("POST", "/api/knowledge/articles"),
    ("PATCH", "/api/knowledge/articles/1"),
    ("POST", "/api/sync/operator"),
    ("POST", "/api/sync/ozon"),
    ("POST", "/api/push/test"),
    ("POST", "/api/task-types"),
    ("PATCH", "/api/task-types/1"),
    ("DELETE", "/api/task-types/1"),
    ("POST", "/api/reply-templates"),
)


def _request(method: str, path: str, *, with_session: bool = True) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if with_session:
        headers.append((b"cookie", f"{main.AUTH_COOKIE_NAME}=test-session".encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


async def _run_auth_guard(
    method: str,
    path: str,
    user: dict[str, object] | None,
    *,
    with_session: bool = True,
) -> tuple[object, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    async def call_next(request: Request):
        calls.append((request.method, request.url.path))
        return JSONResponse({"preserved": True, "path": request.url.path})

    request = _request(method, path, with_session=with_session)
    with mock.patch.object(main.repo, "get_user_by_session", return_value=user):
        response = await main.require_auth_for_api(request, call_next)
    return response, calls


def _json_body(response: object) -> dict[str, object]:
    return json.loads(response.body.decode("utf-8"))


def _run_without_event_loop(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("auth guard unexpectedly suspended")


class RouteRbacTests(unittest.TestCase):
    def test_missing_session_preserves_401_contract(self) -> None:
        response, calls = _run_without_event_loop(
            _run_auth_guard("POST", "/api/tasks", None, with_session=False)
        )
        self.assertEqual(401, response.status_code)
        self.assertEqual({"detail": "Требуется авторизация"}, _json_body(response))
        self.assertEqual([], calls)

    def test_viewer_keeps_read_only_and_self_service_access(self) -> None:
        routes = tuple(("GET", path) for path in _VIEWER_READ_ROUTES) + _VIEWER_SELF_SERVICE_ROUTES
        for method, path in routes:
            with self.subTest(method=method, path=path):
                response, calls = _run_without_event_loop(_run_auth_guard(method, path, _VIEWER))
                self.assertEqual(200, response.status_code)
                self.assertEqual({"preserved": True, "path": path}, _json_body(response))
                self.assertEqual([(method, path)], calls)

    def test_viewer_gets_exact_403_on_admin_only_routes(self) -> None:
        for method, path in _ADMIN_ONLY_ROUTES:
            with self.subTest(method=method, path=path):
                response, calls = _run_without_event_loop(_run_auth_guard(method, path, _VIEWER))
                self.assertEqual(403, response.status_code)
                self.assertEqual({"detail": "Нужны права администратора"}, _json_body(response))
                self.assertEqual([], calls)

    def test_admin_workflows_continue_unchanged_through_auth_guard(self) -> None:
        for method, path in _ADMIN_ONLY_ROUTES:
            with self.subTest(method=method, path=path):
                response, calls = _run_without_event_loop(_run_auth_guard(method, path, _ADMIN))
                self.assertEqual(200, response.status_code)
                self.assertEqual({"preserved": True, "path": path}, _json_body(response))
                self.assertEqual([(method, path)], calls)

    def test_background_tick_and_public_routes_do_not_gain_session_auth(self) -> None:
        routes = (
            ("GET", "/"),
            ("GET", "/health"),
            ("POST", "/api/auth/login"),
            ("GET", "/api/background/tick"),
            ("POST", "/api/background/tick"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                with mock.patch.object(
                    main.repo,
                    "get_user_by_session",
                    side_effect=AssertionError("public/token-only route queried the session repository"),
                ):
                    response, calls = _run_without_event_loop(
                        _run_auth_guard(method, path, None, with_session=False)
                    )
                self.assertEqual(200, response.status_code)
                self.assertEqual([(method, path)], calls)

    def test_yandex_webhook_keeps_existing_session_contract(self) -> None:
        response, calls = _run_without_event_loop(
            _run_auth_guard("POST", "/api/webhooks/yandex", _VIEWER)
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([("POST", "/api/webhooks/yandex")], calls)

        response, calls = _run_without_event_loop(
            _run_auth_guard("POST", "/api/webhooks/yandex", None, with_session=False)
        )
        self.assertEqual(401, response.status_code)
        self.assertEqual([], calls)

    def test_route_inventory_has_explicit_safe_defaults_and_exceptions(self) -> None:
        inventory = {
            (method, route.path)
            for route in main.app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
            if method != "HEAD"
        }
        self.assertTrue(inventory)
        for method, path in inventory:
            access_class = auth_dependencies.route_access_class(method, path)
            with self.subTest(method=method, path=path):
                self.assertIn(
                    access_class,
                    {
                        "public",
                        "session_self_check",
                        "authenticated_read",
                        "viewer_self_service",
                        "admin_only",
                        "token_only",
                        "webhook_unchanged",
                    },
                )
                if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/api/"):
                    expected_exceptions = {
                        ("POST", "/api/auth/login"),
                        ("POST", "/api/auth/logout"),
                        ("PATCH", "/api/auth/profile"),
                        ("POST", "/api/push/subscribe"),
                        ("POST", "/api/push/unsubscribe"),
                        ("POST", "/api/notifications/{notification_id}/read"),
                        ("POST", "/api/notifications/read-all"),
                        ("POST", "/api/background/tick"),
                        ("POST", "/api/webhooks/yandex"),
                    }
                    if (method, path) not in expected_exceptions:
                        self.assertEqual("admin_only", access_class)

        expected_special_routes = {
            ("GET", "/api/chat-settings"): "authenticated_read",
            ("POST", "/api/notifications/{notification_id}/read"): "viewer_self_service",
            ("POST", "/api/notifications/read-all"): "viewer_self_service",
            ("GET", "/api/background/tick"): "token_only",
            ("POST", "/api/background/tick"): "token_only",
            ("POST", "/api/webhooks/yandex"): "webhook_unchanged",
        }
        for route, expected in expected_special_routes.items():
            self.assertIn(route, inventory)
            self.assertEqual(expected, auth_dependencies.route_access_class(*route))

    def test_sensitive_get_inventory_is_admin_only(self) -> None:
        sensitive_gets = {
            (method, route.path)
            for route in main.app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
            if method == "GET"
            and (
                route.path.startswith("/api/debug/")
                or route.path.startswith("/api/chat-uploads/")
                or route.path.rstrip("/")
                in {"/api/supply-planning/status", "/api/supply-planning/sync"}
            )
        }
        self.assertTrue(sensitive_gets)
        for method, path in sensitive_gets:
            with self.subTest(path=path):
                self.assertEqual("admin_only", auth_dependencies.route_access_class(method, path))


if __name__ == "__main__":
    unittest.main()
