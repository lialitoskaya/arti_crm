from __future__ import annotations

import asyncio
import ast
import contextlib
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx
from starlette.requests import Request

import test_regression_foundation as foundation  # noqa: E402
from app import yandex_oauth  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo

_PASSWORD = "oauth-password-2026"
_OAUTH_ENV_NAMES = (
    "YANDEX_OAUTH_ENABLED",
    "YANDEX_OAUTH_CLIENT_ID",
    "YANDEX_OAUTH_CLIENT_SECRET",
    "YANDEX_OAUTH_REDIRECT_URI",
    "YANDEX_OAUTH_USER_MAP",
)


def _run(coroutine):
    shared_loop = foundation._TEST_EVENT_LOOP
    if not shared_loop.is_closed():
        return shared_loop.run_until_complete(coroutine)
    isolated_loop = asyncio.new_event_loop()
    try:
        return isolated_loop.run_until_complete(coroutine)
    finally:
        isolated_loop.close()


def _oauth_environment(user_map: dict[str, str] | str) -> dict[str, str]:
    return {
        "YANDEX_OAUTH_ENABLED": "true",
        "YANDEX_OAUTH_CLIENT_ID": "local-test-client",
        "YANDEX_OAUTH_CLIENT_SECRET": "local-test-secret-not-real",
        "YANDEX_OAUTH_REDIRECT_URI": "https://testserver/api/auth/yandex/callback",
        "YANDEX_OAUTH_USER_MAP": user_map if isinstance(user_map, str) else json.dumps(user_map),
    }


def _oauth_disabled_environment() -> dict[str, str]:
    return {name: "" for name in _OAUTH_ENV_NAMES}


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
        follow_redirects=False,
    )


def _session_count() -> int:
    with db.get_connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _provider_client(
    profile: dict[str, object],
    *,
    token_status: int = 200,
    profile_status: int = 200,
) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == yandex_oauth.TOKEN_URL:
            payload = parse_qs(request.content.decode("utf-8"))
            if token_status == 200:
                if payload.get("code") != ["local-code"] or not payload.get("code_verifier"):
                    raise AssertionError("callback did not send the code and PKCE verifier")
                return httpx.Response(200, json={"access_token": "local-provider-token"})
            return httpx.Response(token_status, json={"error": "provider_failure"})
        if request.url.copy_with(query=None) == httpx.URL(yandex_oauth.PROFILE_URL):
            if request.headers.get("Authorization") != "OAuth local-provider-token":
                raise AssertionError("profile request did not use the exchanged token")
            if profile_status == 200:
                return httpx.Response(200, json=profile)
            return httpx.Response(profile_status, json={"error": "provider_failure"})
        raise AssertionError(f"unexpected mocked provider URL: {request.url.host}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _start_flow(client: httpx.AsyncClient) -> tuple[httpx.Response, str]:
    response = await client.get("/api/auth/yandex/start")
    query = parse_qs(urlparse(response.headers["location"]).query)
    return response, query["state"][0]


class YandexOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        main.app.state.security_rate_limits = {}
        foundation._remove_test_runtime_files()
        db.init_db()
        self.user = repo.create_user("oauth-admin", _PASSWORD, "OAuth Admin", "admin")

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted real network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_password_login_is_independent_when_yandex_environment_is_absent(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                status = await client.get("/api/auth/yandex/status")
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "oauth-admin", "password": _PASSWORD},
                )
                return status, login

        with mock.patch.dict(os.environ, _oauth_disabled_environment(), clear=False):
            status, login = _run(exercise())

        self.assertEqual({"enabled": False}, status.json())
        self.assertEqual(200, login.status_code)
        self.assertEqual("oauth-admin", login.json()["user"]["username"])

    def test_invalid_user_map_disables_only_oauth_and_password_login_still_works(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
            async with await _client() as client:
                status = await client.get("/api/auth/yandex/status")
                start = await client.get("/api/auth/yandex/start")
                login = await client.post(
                    "/api/auth/login",
                    json={"username": "oauth-admin", "password": _PASSWORD},
                )
                return status, start, login

        with mock.patch.dict(os.environ, _oauth_environment("{not-json"), clear=False):
            status, start, login = _run(exercise())

        self.assertEqual({"enabled": False}, status.json())
        self.assertEqual(303, start.status_code)
        self.assertEqual("/?yandex_oauth=provider_unavailable", start.headers["location"])
        self.assertEqual(200, login.status_code)

    def test_invalid_oauth_configuration_does_not_break_startup(self) -> None:
        async def exercise() -> None:
            with mock.patch.object(main, "_ensure_wb_events_auto_plan_from_env"):
                await main.on_startup()
                await main.on_shutdown()

        with mock.patch.dict(os.environ, _oauth_environment("[]"), clear=False):
            _run(exercise())
            self.assertIsNone(yandex_oauth.get_config())
        self.assertIsNotNone(repo.get_user_by_id(int(self.user["id"])))

    def test_oauth_rate_limit_redirects_with_only_safe_error_code(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})
        internal_detail = "rate bucket path=C:\\private token=provider-secret"

        async def exercise() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                with mock.patch.object(
                    main,
                    "_rate_limit",
                    side_effect=main.HTTPException(status_code=429, detail=internal_detail),
                ):
                    start = await client.get("/api/auth/yandex/start")
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"state": "private-state", "error": "provider-private-error"},
                    )
                return start, callback

        with mock.patch.dict(os.environ, environment, clear=False):
            start, callback = _run(exercise())

        for response in (start, callback):
            self.assertEqual(303, response.status_code)
            self.assertEqual("/?yandex_oauth=oauth_rate_limited", response.headers["location"])
            self.assertNotIn(internal_detail, response.text)
            self.assertNotIn("private-state", response.headers["location"])
            self.assertNotIn("provider-private-error", response.headers["location"])

    def test_mapped_callback_creates_session_only_after_successful_profile(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})
        profile = {
            "id": "yandex-user-1",
            "login": "ignored-login",
            "default_email": "ignored@yandex.ru",
        }

        async def exercise() -> tuple[httpx.Response, httpx.Response, httpx.Response, int, int]:
            async with await _client() as client:
                start, state = await _start_flow(client)
                before_callback = _session_count()
                with mock.patch.object(
                    yandex_oauth,
                    "_provider_client",
                    side_effect=lambda: _provider_client(profile),
                ):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": state},
                    )
                after_callback = _session_count()
                me = await client.get("/api/auth/me")
                return start, callback, me, before_callback, after_callback

        with mock.patch.dict(os.environ, environment, clear=False):
            start, callback, me, before_callback, after_callback = _run(exercise())

        self.assertEqual(302, start.status_code)
        authorization_query = parse_qs(urlparse(start.headers["location"]).query)
        self.assertEqual(["S256"], authorization_query["code_challenge_method"])
        self.assertEqual(["yes"], authorization_query["force_confirm"])
        self.assertTrue(authorization_query["code_challenge"][0])
        self.assertEqual(0, before_callback)
        self.assertEqual(303, callback.status_code)
        self.assertEqual("/", callback.headers["location"])
        self.assertEqual(1, after_callback)
        self.assertEqual(200, me.status_code)
        self.assertEqual("oauth-admin", me.json()["user"]["username"])
        self.assertNotIn("local-provider-token", json.dumps(me.json()))

    def test_wsgi_bootstrap_upgrades_old_schema_before_oauth_callback(self) -> None:
        yandex_user_id = "upgraded-yandex-user"
        profile = {"id": yandex_user_id}
        first_environment = _oauth_environment({f"id:{yandex_user_id}": "oauth-admin"})
        repeated_environment = _oauth_environment({"id:unrelated-user": "unused-user"})

        # The parent schema differs only by the absence of the OAuth link table.
        with db.get_connection() as connection:
            connection.execute("DROP TABLE yandex_oauth_links")
            table_before = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='yandex_oauth_links'"
            ).fetchone()[0]
        self.assertEqual(0, int(table_before))

        db.init_db()

        with db.get_connection() as connection:
            table_after = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='yandex_oauth_links'"
            ).fetchone()[0]
            index_after = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='index' AND name='idx_yandex_oauth_links_crm_user_id'"
            ).fetchone()[0]
        self.assertEqual(1, int(table_after))
        self.assertEqual(1, int(index_after))

        async def oauth_login() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                _start, state = await _start_flow(client)
                with mock.patch.object(
                    yandex_oauth,
                    "_provider_client",
                    side_effect=lambda: _provider_client(profile),
                ):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": state},
                    )
                return callback, await client.get("/api/auth/me")

        with mock.patch.dict(os.environ, first_environment, clear=False):
            first_callback, first_me = _run(oauth_login())
        with mock.patch.dict(os.environ, repeated_environment, clear=False):
            repeated_callback, repeated_me = _run(oauth_login())

        self.assertEqual((303, "/"), (first_callback.status_code, first_callback.headers["location"]))
        self.assertEqual((303, "/"), (repeated_callback.status_code, repeated_callback.headers["location"]))
        self.assertEqual(200, first_me.status_code)
        self.assertEqual(200, repeated_me.status_code)
        self.assertEqual(int(self.user["id"]), int(first_me.json()["user"]["id"]))
        self.assertEqual(int(self.user["id"]), int(repeated_me.json()["user"]["id"]))
        with db.get_connection() as connection:
            links = connection.execute(
                "SELECT crm_user_id FROM yandex_oauth_links WHERE yandex_user_id=?",
                (yandex_user_id,),
            ).fetchall()
            session_user_ids = {
                int(row["user_id"])
                for row in connection.execute("SELECT user_id FROM sessions ORDER BY id").fetchall()
            }
        self.assertEqual([int(self.user["id"])], [int(row["crm_user_id"]) for row in links])
        self.assertEqual({int(self.user["id"])}, session_user_ids)
        self.assertEqual(2, _session_count())

    def test_wsgi_entrypoints_initialize_schema_before_serving(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for relative_path in ("server.py", "passenger_wsgi.py"):
            with self.subTest(entrypoint=relative_path):
                source = (project_root / relative_path).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative_path)
                init_lines = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "init_db"
                ]
                serve_lines = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ASGIMiddleware"
                ]
                self.assertTrue(init_lines, f"{relative_path} must initialize the database")
                self.assertTrue(serve_lines, f"{relative_path} must construct the WSGI adapter")
                self.assertLess(min(init_lines), min(serve_lines))

    def test_persisted_link_survives_username_rename_and_reuse(self) -> None:
        yandex_user_id = "stable-yandex-user"
        environment = _oauth_environment({f"id:{yandex_user_id}": "oauth-admin"})
        profile = {"id": yandex_user_id}

        async def oauth_login() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                _start, state = await _start_flow(client)
                with mock.patch.object(
                    yandex_oauth,
                    "_provider_client",
                    side_effect=lambda: _provider_client(profile),
                ):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": state},
                    )
                return callback, await client.get("/api/auth/me")

        with mock.patch.dict(os.environ, environment, clear=False):
            first_callback, first_me = _run(oauth_login())
        self.assertEqual(303, first_callback.status_code)
        self.assertEqual(int(self.user["id"]), int(first_me.json()["user"]["id"]))

        repo.update_user_profile(int(self.user["id"]), username="oauth-admin-renamed")
        replacement = repo.create_user("oauth-admin", "replacement-password", "Replacement", "admin")

        with mock.patch.dict(os.environ, environment, clear=False):
            second_callback, second_me = _run(oauth_login())

        self.assertEqual(303, second_callback.status_code)
        self.assertEqual(int(self.user["id"]), int(second_me.json()["user"]["id"]))
        self.assertNotEqual(int(replacement["id"]), int(second_me.json()["user"]["id"]))
        with db.get_connection() as connection:
            link = connection.execute(
                "SELECT crm_user_id FROM yandex_oauth_links WHERE yandex_user_id=?",
                (yandex_user_id,),
            ).fetchone()
            replacement_sessions = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                (int(replacement["id"]),),
            ).fetchone()[0]
        self.assertEqual(int(self.user["id"]), int(link["crm_user_id"]))
        self.assertEqual(0, int(replacement_sessions))

    def test_inactive_or_missing_linked_user_cannot_receive_a_new_session(self) -> None:
        scenarios = ("inactive", "missing")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                foundation._remove_test_runtime_files()
                db.init_db()
                linked_user = repo.create_user("linked-user", _PASSWORD, "Linked User", "admin")
                yandex_user_id = f"linked-{scenario}"
                environment = _oauth_environment({f"id:{yandex_user_id}": "linked-user"})
                profile = {"id": yandex_user_id}

                async def oauth_callback() -> httpx.Response:
                    async with await _client() as client:
                        _start, state = await _start_flow(client)
                        with mock.patch.object(
                            yandex_oauth,
                            "_provider_client",
                            side_effect=lambda: _provider_client(profile),
                        ):
                            return await client.get(
                                "/api/auth/yandex/callback",
                                params={"code": "local-code", "state": state},
                            )

                with mock.patch.dict(os.environ, environment, clear=False):
                    first_callback = _run(oauth_callback())
                self.assertEqual(303, first_callback.status_code)
                sessions_before = _session_count()

                if scenario == "inactive":
                    repo.update_user(int(linked_user["id"]), is_active=False)
                    expected_code = "account_inactive"
                else:
                    with db.get_connection() as connection:
                        connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute(
                            "UPDATE yandex_oauth_links SET crm_user_id=? WHERE yandex_user_id=?",
                            (999_999_999, yandex_user_id),
                        )
                    expected_code = "account_not_allowed"

                with mock.patch.dict(os.environ, environment, clear=False):
                    rejected_callback = _run(oauth_callback())

                self.assertEqual(303, rejected_callback.status_code)
                self.assertEqual(
                    f"/?yandex_oauth={expected_code}",
                    rejected_callback.headers["location"],
                )
                self.assertEqual(sessions_before, _session_count())

    def test_concurrent_first_link_creation_never_rebinds_yandex_identity(self) -> None:
        user_a = self.user
        user_b = repo.create_user("oauth-second", _PASSWORD, "OAuth Second", "admin")
        start_barrier = threading.Barrier(2)
        connections_ready = threading.Barrier(2)
        connection_ids: set[int] = set()
        connection_ids_lock = threading.Lock()
        real_get_connection = repo.get_connection

        @contextlib.contextmanager
        def tracked_connection():
            with real_get_connection() as connection:
                with connection_ids_lock:
                    connection_ids.add(id(connection))
                connections_ready.wait(timeout=5)
                yield connection

        def create_link(bootstrap_username: str):
            start_barrier.wait(timeout=5)
            return repo.create_session_for_yandex_identity(
                "concurrent-yandex-id",
                bootstrap_username=bootstrap_username,
                user_agent="concurrent-test",
                ip="127.0.0.1",
                seconds=3600,
            )

        with (
            mock.patch.object(repo, "get_connection", tracked_connection),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(
                executor.map(
                    create_link,
                    (str(user_a["username"]), str(user_b["username"])),
                )
            )

        self.assertEqual(2, len(connection_ids))
        self.assertTrue(all(result is not None for result in results))
        result_user_ids = {int(result[0]["id"]) for result in results if result is not None}
        with db.get_connection() as connection:
            links = connection.execute(
                "SELECT crm_user_id FROM yandex_oauth_links WHERE yandex_user_id=?",
                ("concurrent-yandex-id",),
            ).fetchall()
            session_user_ids = {
                int(row["user_id"])
                for row in connection.execute(
                    "SELECT user_id FROM sessions ORDER BY id ASC"
                ).fetchall()
            }
        self.assertEqual(1, len(links))
        linked_user_ids = {int(links[0]["crm_user_id"])}
        self.assertEqual(linked_user_ids, result_user_ids)
        self.assertEqual(linked_user_ids, session_user_ids)

    def test_logout_revokes_oauth_crm_session_deletes_cookie_and_protected_route_returns_401(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})
        profile = {"id": "yandex-user-1"}

        async def exercise() -> tuple[str, httpx.Response, httpx.Response, str | None]:
            async with await _client() as client:
                _start, state = await _start_flow(client)
                with mock.patch.object(
                    yandex_oauth,
                    "_provider_client",
                    side_effect=lambda: _provider_client(profile),
                ):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": state},
                    )
                self.assertEqual(303, callback.status_code)
                session_token = client.cookies.get(main.AUTH_COOKIE_NAME)
                self.assertTrue(session_token)
                csrf = await client.get("/api/security/csrf")
                self.assertEqual(200, csrf.status_code)
                logout = await client.post(
                    "/api/auth/logout",
                    headers={main.CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
                )
                protected = await client.get("/api/chats")
                return str(session_token), logout, protected, client.cookies.get(main.AUTH_COOKIE_NAME)

        with mock.patch.dict(os.environ, environment, clear=False):
            session_token, logout, protected, remaining_cookie = _run(exercise())

        with db.get_connection() as connection:
            session = connection.execute(
                "SELECT revoked_at FROM sessions WHERE session_token=?",
                (session_token,),
            ).fetchone()
        self.assertEqual(200, logout.status_code)
        self.assertIsNotNone(session)
        self.assertIsNotNone(session["revoked_at"])
        self.assertIsNone(remaining_cookie)
        self.assertEqual(401, protected.status_code)

    def test_unmapped_and_inactive_users_receive_specific_safe_error_codes(self) -> None:
        scenarios = (
            (
                _oauth_environment({"id:different-id": "oauth-admin"}),
                {"id": "unmapped-id", "login": "unmapped"},
                "account_not_allowed",
            ),
            (
                _oauth_environment({"login:YANDEX-LOGIN": "oauth-admin"}),
                {"id": "inactive-id", "login": " yandex-login "},
                "account_inactive",
            ),
        )
        for index, (environment, profile, error_code) in enumerate(scenarios):
            with self.subTest(index=index):
                if index == 1:
                    repo.update_user(int(self.user["id"]), is_active=False)

                async def exercise() -> httpx.Response:
                    async with await _client() as client:
                        _start, state = await _start_flow(client)
                        with mock.patch.object(
                            yandex_oauth,
                            "_provider_client",
                            side_effect=lambda: _provider_client(profile),
                        ):
                            return await client.get(
                                "/api/auth/yandex/callback",
                                params={"code": "local-code", "state": state},
                            )

                with mock.patch.dict(os.environ, environment, clear=False):
                    callback = _run(exercise())
                self.assertEqual(303, callback.status_code)
                self.assertEqual(f"/?yandex_oauth={error_code}", callback.headers["location"])
                self.assertEqual(0, _session_count())
                self.assertEqual(1, len(repo.list_users()))

    def test_provider_cancel_redirects_with_only_the_cancelled_code(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})

        async def exercise() -> tuple[httpx.Response, mock.Mock]:
            async with await _client() as client:
                _start, state = await _start_flow(client)
                provider = mock.Mock(side_effect=AssertionError("provider must not be called"))
                with mock.patch.object(yandex_oauth, "_provider_client", provider):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={
                            "error": "access_denied",
                            "error_description": "provider-internal-details",
                            "state": state,
                        },
                    )
                return callback, provider

        with mock.patch.dict(os.environ, environment, clear=False):
            callback, provider = _run(exercise())

        self.assertEqual(303, callback.status_code)
        self.assertEqual("/?yandex_oauth=cancelled", callback.headers["location"])
        self.assertNotIn("provider-internal-details", callback.headers["location"])
        provider.assert_not_called()
        self.assertEqual(0, _session_count())

    def test_state_mismatch_stops_before_provider_and_session_creation(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})

        async def exercise() -> tuple[httpx.Response, mock.Mock]:
            async with await _client() as client:
                await _start_flow(client)
                provider = mock.Mock(side_effect=AssertionError("provider must not be called"))
                with mock.patch.object(yandex_oauth, "_provider_client", provider):
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": "wrong-state"},
                    )
                return callback, provider

        with mock.patch.dict(os.environ, environment, clear=False):
            callback, provider = _run(exercise())

        self.assertEqual(303, callback.status_code)
        self.assertEqual("/?yandex_oauth=flow_expired", callback.headers["location"])
        provider.assert_not_called()
        self.assertEqual(0, _session_count())

    def test_missing_flow_cookie_returns_expired_code_without_internal_details(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})

        async def exercise() -> httpx.Response:
            async with await _client() as client:
                return await client.get(
                    "/api/auth/yandex/callback",
                    params={"code": "local-code", "state": "missing-cookie-state"},
                )

        with mock.patch.dict(os.environ, environment, clear=False):
            callback = _run(exercise())

        self.assertEqual(303, callback.status_code)
        self.assertEqual("/?yandex_oauth=flow_expired", callback.headers["location"])
        self.assertNotIn("missing-cookie-state", callback.headers["location"])
        self.assertEqual(0, _session_count())

    def test_token_and_profile_errors_do_not_create_sessions(self) -> None:
        environment = _oauth_environment({"id:yandex-user-1": "oauth-admin"})
        profile = {"id": "yandex-user-1"}
        for token_status, profile_status in ((502, 200), (200, 502)):
            with self.subTest(token_status=token_status, profile_status=profile_status):
                with db.get_connection() as connection:
                    connection.execute("DELETE FROM sessions")

                async def exercise() -> tuple[httpx.Response, int, httpx.Response]:
                    async with await _client() as client:
                        _start, state = await _start_flow(client)
                        with mock.patch.object(
                            yandex_oauth,
                            "_provider_client",
                            side_effect=lambda: _provider_client(
                                profile,
                                token_status=token_status,
                                profile_status=profile_status,
                            ),
                        ):
                            callback = await client.get(
                                "/api/auth/yandex/callback",
                                params={"code": "local-code", "state": state},
                            )
                        sessions_after_callback = _session_count()
                        password_login = await client.post(
                            "/api/auth/login",
                            json={"username": "oauth-admin", "password": _PASSWORD},
                        )
                        return callback, sessions_after_callback, password_login

                with mock.patch.dict(os.environ, environment, clear=False):
                    callback, sessions_after_callback, password_login = _run(exercise())
                self.assertEqual(303, callback.status_code)
                self.assertEqual("/?yandex_oauth=provider_unavailable", callback.headers["location"])
                self.assertNotIn("provider_failure", callback.headers["location"])
                self.assertNotIn("local-provider-token", callback.headers["location"])
                self.assertEqual(0, sessions_after_callback)
                self.assertEqual(200, password_login.status_code)

    def test_callback_logs_only_allowlisted_failure_stage(self) -> None:
        environment = _oauth_environment({"id:mapped-id": "oauth-admin"})
        sensitive_profile = {
            "id": "unmapped-private-id",
            "login": "private-login",
            "default_email": "private@example.test",
        }
        internal_details = (
            "local-code",
            "local-provider-token",
            "local-test-secret-not-real",
            "provider_failure",
            "unmapped-private-id",
            "private-login",
            "private@example.test",
            "database-private-detail",
            "session-private-detail",
        )

        async def invoke(stage: str) -> tuple[httpx.Response, str]:
            async with await _client() as client:
                _start, state = await _start_flow(client)
                provider_profile: object = {"id": "mapped-id"}
                token_status = 200
                profile_status = 200
                if stage == "token_exchange":
                    token_status = 502
                elif stage == "profile_request":
                    profile_status = 502
                elif stage == "profile_validation":
                    provider_profile = []
                elif stage == "user_mapping":
                    provider_profile = sensitive_profile

                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch.object(
                            yandex_oauth,
                            "_provider_client",
                            side_effect=lambda: _provider_client(
                                provider_profile,  # type: ignore[arg-type]
                                token_status=token_status,
                                profile_status=profile_status,
                            ),
                        )
                    )
                    if stage == "database_link":
                        stack.enter_context(
                            mock.patch.object(
                                repo,
                                "get_yandex_oauth_linked_user_id",
                                side_effect=RuntimeError("database-private-detail"),
                            )
                        )
                    elif stage == "session_creation":
                        stack.enter_context(
                            mock.patch.object(
                                repo,
                                "create_session_for_yandex_identity",
                                side_effect=RuntimeError("session-private-detail"),
                            )
                        )
                    callback = await client.get(
                        "/api/auth/yandex/callback",
                        params={"code": "local-code", "state": state},
                    )
                return callback, state

        for stage in (
            "token_exchange",
            "profile_request",
            "profile_validation",
            "user_mapping",
            "database_link",
            "session_creation",
        ):
            with self.subTest(stage=stage):
                with (
                    mock.patch.dict(os.environ, environment, clear=False),
                    self.assertLogs("arti_crm.yandex_oauth", level="WARNING") as captured,
                ):
                    callback, state = _run(invoke(stage))

                self.assertEqual(303, callback.status_code)
                self.assertIn(
                    callback.headers["location"],
                    {
                        "/?yandex_oauth=account_not_allowed",
                        "/?yandex_oauth=failed",
                        "/?yandex_oauth=provider_unavailable",
                    },
                )
                self.assertEqual(
                    [f"WARNING:arti_crm.yandex_oauth:Yandex OAuth callback failed stage={stage}"],
                    captured.output,
                )
                logged = "\n".join(captured.output)
                self.assertNotIn(state, logged)
                for detail in internal_details:
                    self.assertNotIn(detail, logged)

    def test_unknown_oauth_error_is_reduced_to_generic_code_with_frontend_fallback(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "path": "/api/auth/yandex/callback",
                "headers": [],
                "server": ("testserver", 443),
                "client": ("127.0.0.1", 12345),
                "query_string": b"",
            }
        )
        response = main._yandex_oauth_error_response(
            request,
            "provider email=private@example.test token=internal",
        )
        frontend_source = Path(main.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertEqual("/?yandex_oauth=failed", response.headers["location"])
        self.assertNotIn("private@example.test", response.headers["location"])
        self.assertNotIn("internal", response.headers["location"])
        self.assertIn("function yandexOAuthErrorMessage(code)", frontend_source)
        self.assertIn("|| YANDEX_OAUTH_ERROR_MESSAGES.failed;", frontend_source)
        self.assertIn("Не удалось войти через Яндекс", frontend_source)

    def test_mapping_priority_is_id_then_login_then_email_with_casefolding(self) -> None:
        environment = _oauth_environment(
            {
                "email: PERSON@YANDEX.RU ": "email-user",
                "login: Yandex.Login ": "login-user",
                "id:stable-id": "oauth-admin",
            }
        )
        with mock.patch.dict(os.environ, environment, clear=False):
            config = yandex_oauth.get_config()
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(
            "oauth-admin",
            yandex_oauth.resolve_crm_username(
                config,
                {
                    "id": "stable-id",
                    "login": " yandex.login ",
                    "default_email": "person@yandex.ru",
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
