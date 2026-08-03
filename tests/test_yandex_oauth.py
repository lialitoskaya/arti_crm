from __future__ import annotations

import asyncio
import json
import os
import unittest
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx

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
        self.assertEqual(503, start.status_code)
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
        self.assertTrue(authorization_query["code_challenge"][0])
        self.assertEqual(0, before_callback)
        self.assertEqual(303, callback.status_code)
        self.assertEqual("/", callback.headers["location"])
        self.assertEqual(1, after_callback)
        self.assertEqual(200, me.status_code)
        self.assertEqual("oauth-admin", me.json()["user"]["username"])
        self.assertNotIn("local-provider-token", json.dumps(me.json()))

    def test_unmapped_and_inactive_users_never_receive_sessions(self) -> None:
        scenarios = (
            (
                _oauth_environment({"id:different-id": "oauth-admin"}),
                {"id": "unmapped-id", "login": "unmapped"},
            ),
            (
                _oauth_environment({"login:YANDEX-LOGIN": "oauth-admin"}),
                {"id": "inactive-id", "login": " yandex-login "},
            ),
        )
        for index, (environment, profile) in enumerate(scenarios):
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
                self.assertEqual("/?yandex_oauth=failed", callback.headers["location"])
                self.assertEqual(0, _session_count())
                self.assertEqual(1, len(repo.list_users()))

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
        self.assertEqual("/?yandex_oauth=failed", callback.headers["location"])
        provider.assert_not_called()
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
                self.assertEqual("/?yandex_oauth=failed", callback.headers["location"])
                self.assertEqual(0, sessions_after_callback)
                self.assertEqual(200, password_login.status_code)

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
