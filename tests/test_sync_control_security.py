from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from fastapi import Request
from fastapi.routing import APIRoute

import test_regression_foundation as foundation  # noqa: E402


main = foundation.main
httpx = foundation.httpx

_ADMIN = {"id": 1, "username": "admin", "role": "admin", "is_active": True}
_VIEWER = {"id": 7, "username": "viewer", "role": "viewer", "is_active": True}
_SESSION_TOKEN = "test-session"
_CONTROL_PATHS = (
    "/api/debug/ozon/fast-sync",
    "/api/debug/ozon/backfill-chats",
    "/api/debug/wb/import-events",
    "/api/debug/wb/import-events-auto",
    "/api/supply-planning/sync",
    "/api/supply-planning/sync/",
    "/api/background/tick",
)

with mock.patch.object(foundation.socket, "socket", foundation._REAL_SOCKET):
    _TEST_EVENT_LOOP = asyncio.new_event_loop()

_SAFE_ENV = dict(foundation._SAFE_ENV)
_SAFE_ENV.update(
    {
        "CRM_CSRF_ENABLED": "true",
        "CRM_RATE_LIMIT_ENABLED": "false",
        "CRM_AUDIT_LOG_ENABLED": "false",
        "CRM_FORCE_HTTPS": "false",
        "SECRET_KEY": "test-only-csrf-secret",
    }
)


def _safe_getenv(name: str, default=None):
    return _SAFE_ENV.get(name, default)


_ENV_PATCHER = mock.patch.object(foundation.os, "getenv", side_effect=_safe_getenv)
_SQLITE_PATCHER = mock.patch.object(
    foundation.db.sqlite3,
    "connect",
    side_effect=foundation._guarded_sqlite_connect,
)
_NETWORK_PATCHERS = (
    mock.patch.object(foundation.socket, "socket", foundation._NoNetworkSocket),
    mock.patch.object(foundation.socket, "create_connection", foundation._deny_network),
)


def setUpModule() -> None:
    _ENV_PATCHER.start()
    _SQLITE_PATCHER.start()
    for patcher in _NETWORK_PATCHERS:
        patcher.start()


def tearDownModule() -> None:
    for patcher in reversed(_NETWORK_PATCHERS):
        patcher.stop()
    _SQLITE_PATCHER.stop()
    _ENV_PATCHER.stop()
    _TEST_EVENT_LOOP.close()


async def _asgi_request(
    method: str,
    path: str,
    *,
    user: dict[str, object] | None = None,
    csrf: bool = False,
    headers: dict[str, str] | None = None,
    forbid_session_lookup: bool = False,
):
    request_headers = dict(headers or {})
    cookies: dict[str, str] = {}
    if user is not None:
        cookies[main.AUTH_COOKIE_NAME] = _SESSION_TOKEN
        if csrf:
            request_headers[main.CSRF_HEADER_NAME] = main._csrf_token_for_session(_SESSION_TOKEN)

    transport = httpx.ASGITransport(app=main.app)
    session_lookup_error = (
        AssertionError("background tick queried the browser session")
        if forbid_session_lookup
        else None
    )
    with mock.patch.object(
        main.repo,
        "get_user_by_session",
        return_value=user,
        side_effect=session_lookup_error,
    ), mock.patch.object(
        main.repo, "write_audit_log"
    ):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            cookies=cookies,
        ) as client:
            return await client.request(method, path, headers=request_headers)


class SyncControlSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        main.app.state.security_rate_limits = {}

    def tearDown(self) -> None:
        self.assertEqual([], foundation._NETWORK_ATTEMPTS, "A test attempted to access the network")

    def test_known_state_changing_controls_are_registered_post_only(self) -> None:
        inventory: dict[str, set[str]] = {}
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            inventory.setdefault(route.path, set()).update(route.methods)
        for path in _CONTROL_PATHS:
            with self.subTest(path=path):
                self.assertEqual({"POST"}, inventory.get(path))

    def test_get_returns_405_for_every_state_changing_control(self) -> None:
        for path in _CONTROL_PATHS:
            user = None if path == "/api/background/tick" else _ADMIN
            with self.subTest(path=path):
                response = _TEST_EVENT_LOOP.run_until_complete(_asgi_request("GET", path, user=user))
                self.assertEqual(405, response.status_code)

    def test_admin_supply_post_preserves_query_arguments_and_success_payload(self) -> None:
        upstream = {
            "ok": True,
            "marketplace": "ozon",
            "configured": True,
            "rows": [
                {
                    "marketplace": "ozon",
                    "sku": "SKU-1",
                    "product": "Product",
                    "warehouse": "Warehouse",
                    "current_stock": 3,
                    "avg_daily_sales": 1.5,
                    "target_days": 45,
                }
            ],
            "rows_count": 1,
            "note": "preserved",
        }
        with mock.patch.object(main, "_supply_fetch_ozon", new=mock.AsyncMock(return_value=upstream)) as fetch, mock.patch.object(
            main, "_supply_status", return_value={"ozon": True}
        ):
            response = _TEST_EVENT_LOOP.run_until_complete(
                _asgi_request(
                    "POST",
                    "/api/supply-planning/sync?marketplace=ozon&target_days=45&sales_days=30",
                    user=_ADMIN,
                    csrf=True,
                )
            )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(upstream["rows"], payload["rows"])
        self.assertEqual(1, payload["rows_count"])
        self.assertEqual(30, payload["sales_days"])
        self.assertEqual(45, payload["target_days"])
        self.assertEqual({"ozon": True}, payload["configured"])
        self.assertEqual([{key: value for key, value in upstream.items() if key != "rows"}], payload["results"])
        fetch.assert_awaited_once_with(sales_days=30, target_days=45)

    def test_browser_post_requires_csrf_and_preserves_401_403_role_contracts(self) -> None:
        path = "/api/supply-planning/sync?marketplace=ozon"
        with mock.patch.object(main, "_supply_fetch_ozon", new=mock.AsyncMock()) as fetch:
            no_csrf = _TEST_EVENT_LOOP.run_until_complete(_asgi_request("POST", path, user=_ADMIN))
            viewer = _TEST_EVENT_LOOP.run_until_complete(
                _asgi_request("POST", path, user=_VIEWER, csrf=True)
            )
            anonymous = _TEST_EVENT_LOOP.run_until_complete(_asgi_request("POST", path))
        self.assertEqual(403, no_csrf.status_code)
        self.assertIn("CSRF", str(no_csrf.json().get("detail")))
        self.assertEqual(403, viewer.status_code)
        self.assertEqual("Нужны права администратора", viewer.json().get("detail"))
        self.assertEqual(401, anonymous.status_code)
        self.assertEqual("Требуется авторизация", anonymous.json().get("detail"))
        fetch.assert_not_awaited()

    def test_background_tick_is_header_only_without_session_or_csrf(self) -> None:
        success_payload = {"ok": True, "source": "api", "processed": 3}
        with mock.patch.object(main, "_background_tick_token", return_value="server-token"), mock.patch.object(
            main, "_run_background_tick_once", new=mock.AsyncMock(return_value=success_payload)
        ) as run_tick:
            response = _TEST_EVENT_LOOP.run_until_complete(
                _asgi_request(
                    "POST",
                    "/api/background/tick",
                    headers={"X-Background-Token": "server-token"},
                    forbid_session_lookup=True,
                )
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(success_payload, response.json())
        run_tick.assert_awaited_once_with(source="api")

    def test_background_tick_rejects_query_missing_invalid_and_unconfigured_tokens_without_disclosure(self) -> None:
        secret = "server-token-value"
        attempts = (
            (f"/api/background/tick?token={secret}", {}),
            ("/api/background/tick", {}),
            ("/api/background/tick", {"X-Background-Token": "invalid-token"}),
        )
        with mock.patch.object(main, "_background_tick_token", return_value=secret), mock.patch.object(
            main, "_run_background_tick_once", new=mock.AsyncMock()
        ) as run_tick:
            for path, headers in attempts:
                with self.subTest(path=path, headers=tuple(headers)):
                    response = _TEST_EVENT_LOOP.run_until_complete(
                        _asgi_request("POST", path, headers=headers)
                    )
                    self.assertEqual(403, response.status_code)
                    self.assertNotIn(secret, response.text)
            run_tick.assert_not_awaited()

        with mock.patch.object(main, "_background_tick_token", return_value=""):
            response = _TEST_EVENT_LOOP.run_until_complete(
                _asgi_request("POST", "/api/background/tick", headers={"X-Background-Token": secret})
            )
        self.assertEqual(503, response.status_code)
        self.assertNotIn(secret, response.text)

    def test_background_tick_uses_constant_time_comparison(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/background/tick",
                "raw_path": b"/api/background/tick",
                "query_string": b"",
                "headers": [(b"x-background-token", b"server-token")],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 12345),
            }
        )
        with mock.patch.object(main, "_background_tick_token", return_value="server-token"), mock.patch.object(
            main.hmac, "compare_digest", return_value=True
        ) as compare:
            main._require_background_tick_access(request)
        compare.assert_called_once_with("server-token", "server-token")

    def test_yandex_webhook_and_read_only_get_routes_are_unchanged(self) -> None:
        inventory = {
            (method, route.path)
            for route in main.app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        self.assertIn(("POST", "/api/webhooks/yandex"), inventory)
        self.assertIn(("GET", "/api/sync/status"), inventory)
        self.assertIn(("GET", "/api/supply-planning/status"), inventory)
        self.assertFalse(main._csrf_exempt_path("/api/webhooks/yandex"))

    def test_frontend_supply_sync_uses_existing_post_and_csrf_helper(self) -> None:
        source = (Path(main.STATIC_DIR) / "app.js").read_text(encoding="utf-8")
        start = source.index("async function syncSupplyPlanningFromApi")
        end = source.index("function supplyPlanningValueHtml", start)
        function_source = source[start:end]
        expected_url = (
            "`/api/supply-planning/sync?marketplace=${encodeURIComponent(marketplace)}"
            "&target_days=${encodeURIComponent(targetDays)}&sales_days=30`"
        )
        self.assertIn(expected_url, function_source)
        self.assertIn("method: 'POST'", function_source)
        self.assertNotIn("method: 'GET'", function_source)
        self.assertIn("timeoutMs: 120000", function_source)
        self.assertIn("const rows = Array.isArray(result.rows)", function_source)
        self.assertIn("supplyPlanningApiSyncInFlight = false", function_source)

        api_start = source.index("async function api(path, options = {})")
        api_end = source.index("async function apiForm", api_start)
        api_source = source[api_start:api_end]
        self.assertIn("ensureCsrfTokenForUnsafeMethod(method, path)", api_source)
        self.assertIn("headers: withCsrfHeader", api_source)


if __name__ == "__main__":
    unittest.main()
