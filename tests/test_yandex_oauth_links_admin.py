from __future__ import annotations

import asyncio
import json
import os
import subprocess
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402
from app import yandex_oauth  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo

_PASSWORD = "managed-oauth-password-2026"
_OAUTH_ENVIRONMENT = {
    "YANDEX_OAUTH_ENABLED": "true",
    "YANDEX_OAUTH_CLIENT_ID": "managed-local-client",
    "YANDEX_OAUTH_CLIENT_SECRET": "managed-local-secret-not-real",
    "YANDEX_OAUTH_REDIRECT_URI": "https://testserver/api/auth/yandex/callback",
    "YANDEX_OAUTH_USER_MAP": json.dumps({"login:managed-login": "fallback-user"}),
}


def _javascript_function(source: str, name: str) -> str:
    signature = f"function {name}("
    start = source.index(signature)
    async_start = start - len("async ")
    if async_start >= 0 and source[async_start:start] == "async ":
        start = async_start
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run(coroutine):
    shared_loop = foundation._TEST_EVENT_LOOP
    if not shared_loop.is_closed():
        return shared_loop.run_until_complete(coroutine)
    isolated_loop = asyncio.new_event_loop()
    try:
        return isolated_loop.run_until_complete(coroutine)
    finally:
        isolated_loop.close()


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
        follow_redirects=False,
    )


async def _login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    if response.status_code != 200:
        raise AssertionError(f"test login failed: {response.status_code}")


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


def _provider_client(profile: dict[str, object]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == yandex_oauth.TOKEN_URL:
            return httpx.Response(200, json={"access_token": "managed-provider-token"})
        if request.url.copy_with(query=None) == httpx.URL(yandex_oauth.PROFILE_URL):
            return httpx.Response(200, json=profile)
        raise AssertionError(f"unexpected mocked provider URL: {request.url.host}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _oauth_callback(client: httpx.AsyncClient, profile: dict[str, object]) -> httpx.Response:
    start = await client.get("/api/auth/yandex/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    with mock.patch.object(
        yandex_oauth,
        "_provider_client",
        side_effect=lambda: _provider_client(profile),
    ):
        return await client.get(
            "/api/auth/yandex/callback",
            params={"code": "managed-code", "state": state},
        )


class YandexOAuthManagedLinksAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        main.app.state.security_rate_limits = {}
        foundation._remove_test_runtime_files()
        db.init_db()
        self.admin = repo.create_user("managed-admin", _PASSWORD, "Managed Admin", "admin")
        self.manager = repo.create_user("managed-manager", _PASSWORD, "Managed Manager", "manager")
        self.viewer = repo.create_user("managed-viewer", _PASSWORD, "Managed Viewer", "viewer")
        self.target = repo.create_user("managed-target", _PASSWORD, "Managed Target", "manager")
        self.fallback = repo.create_user("fallback-user", _PASSWORD, "Fallback User", "manager")

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted real network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_admin_can_list_create_toggle_and_delete_without_config_disclosure(self) -> None:
        async def exercise():
            async with await _client() as client:
                await _login(client, "managed-admin")
                headers = await _csrf_headers(client)
                initial = await client.get("/api/admin/yandex-oauth-links")
                created = await client.post(
                    "/api/admin/yandex-oauth-links",
                    json={
                        "identifier_type": "login",
                        "identifier": "  Managed.Login  ",
                        "crm_user_id": self.target["id"],
                    },
                    headers=headers,
                )
                duplicate = await client.post(
                    "/api/admin/yandex-oauth-links",
                    json={
                        "identifier_type": "login",
                        "identifier": "managed.login",
                        "crm_user_id": self.admin["id"],
                    },
                    headers=headers,
                )
                link_id = created.json()["id"]
                disabled = await client.patch(
                    f"/api/admin/yandex-oauth-links/{link_id}",
                    json={"is_active": False},
                    headers=headers,
                )
                deleted = await client.delete(
                    f"/api/admin/yandex-oauth-links/{link_id}",
                    headers=headers,
                )
                missing = await client.delete(
                    "/api/admin/yandex-oauth-links/999999999",
                    headers=headers,
                )
                final = await client.get("/api/admin/yandex-oauth-links")
                return initial, created, duplicate, disabled, deleted, missing, final

        with mock.patch.dict(os.environ, _OAUTH_ENVIRONMENT, clear=False):
            initial, created, duplicate, disabled, deleted, missing, final = _run(exercise())

        self.assertEqual(200, initial.status_code)
        self.assertEqual([], initial.json()["links"])
        self.assertTrue(any(int(user["id"]) == int(self.target["id"]) for user in initial.json()["users"]))
        self.assertEqual(200, created.status_code)
        self.assertEqual("Managed.Login", created.json()["identifier"])
        self.assertEqual("managed.login", created.json()["normalized_identifier"])
        self.assertFalse(created.json()["confirmed"])
        self.assertEqual(409, duplicate.status_code)
        self.assertEqual(200, disabled.status_code)
        self.assertFalse(disabled.json()["is_active"])
        self.assertEqual({"ok": True}, deleted.json())
        self.assertEqual(404, missing.status_code)
        self.assertEqual(1, len(final.json()["links"]))
        self.assertEqual(created.json()["id"], final.json()["links"][0]["id"])
        self.assertFalse(final.json()["links"][0]["is_active"])
        serialized = json.dumps([initial.json(), created.json(), disabled.json()]).lower()
        for forbidden in (
            "client_secret",
            "client_id",
            "redirect_uri",
            "managed-local-secret-not-real",
            "yandex_user_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_viewer_and_manager_are_forbidden_and_mutations_require_csrf(self) -> None:
        existing = repo.create_yandex_oauth_managed_link(
            identifier_type="login",
            identifier="protected-link",
            crm_user_id=int(self.target["id"]),
        )

        async def exercise():
            responses: list[httpx.Response] = []
            for username in ("managed-viewer", "managed-manager"):
                async with await _client() as client:
                    await _login(client, username)
                    headers = await _csrf_headers(client)
                    responses.append(await client.get("/api/admin/yandex-oauth-links"))
                    responses.append(
                        await client.post(
                            "/api/admin/yandex-oauth-links",
                            json={
                                "identifier_type": "email",
                                "identifier": f"{username}@example.test",
                                "crm_user_id": self.target["id"],
                            },
                            headers=headers,
                        )
                    )
                    responses.append(
                        await client.patch(
                            f"/api/admin/yandex-oauth-links/{existing['id']}",
                            json={"is_active": False},
                            headers=headers,
                        )
                    )
                    responses.append(
                        await client.delete(
                            f"/api/admin/yandex-oauth-links/{existing['id']}",
                            headers=headers,
                        )
                    )
            async with await _client() as admin_client:
                await _login(admin_client, "managed-admin")
                no_csrf = [
                    await admin_client.post(
                        "/api/admin/yandex-oauth-links",
                        json={
                            "identifier_type": "login",
                            "identifier": "csrf-blocked",
                            "crm_user_id": self.target["id"],
                        },
                    ),
                    await admin_client.patch(
                        f"/api/admin/yandex-oauth-links/{existing['id']}",
                        json={"is_active": False},
                    ),
                    await admin_client.delete(
                        f"/api/admin/yandex-oauth-links/{existing['id']}",
                    ),
                ]
            return responses, no_csrf

        responses, no_csrf = _run(exercise())
        self.assertTrue(all(response.status_code == 403 for response in responses))
        self.assertTrue(all(response.status_code == 403 for response in no_csrf))
        with db.get_connection() as connection:
            rows = connection.execute(
                "SELECT id, is_active FROM yandex_oauth_managed_links ORDER BY id"
            ).fetchall()
        self.assertEqual([(int(existing["id"]), 1)], [(int(row["id"]), int(row["is_active"])) for row in rows])

    def test_disabled_managed_match_denies_callback_without_environment_fallback(self) -> None:
        managed = repo.create_yandex_oauth_managed_link(
            identifier_type="login",
            identifier="managed-login",
            crm_user_id=int(self.target["id"]),
        )
        repo.update_yandex_oauth_managed_link(int(managed["id"]), is_active=False)

        async def exercise():
            async with await _client() as client:
                return await _oauth_callback(
                    client,
                    {"id": "disabled-managed-yid", "login": "managed-login"},
                )

        with (
            mock.patch.dict(os.environ, _OAUTH_ENVIRONMENT, clear=False),
            mock.patch.object(
                yandex_oauth,
                "resolve_crm_username",
                wraps=yandex_oauth.resolve_crm_username,
            ) as resolver,
        ):
            callback = _run(exercise())

        self.assertEqual(303, callback.status_code)
        self.assertEqual("/?yandex_oauth=account_not_allowed", callback.headers["location"])
        resolver.assert_not_called()
        with db.get_connection() as connection:
            self.assertEqual(0, int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]))
            self.assertEqual(0, int(connection.execute("SELECT COUNT(*) FROM yandex_oauth_links").fetchone()[0]))

    def test_transaction_rechecks_managed_precedence_after_preliminary_lookup(self) -> None:
        scenarios = (
            ("disabled", False, "account_not_allowed"),
            ("active", True, None),
        )
        for scenario, is_active, error_code in scenarios:
            with self.subTest(scenario=scenario):
                foundation._remove_test_runtime_files()
                db.init_db()
                target = repo.create_user("managed-target", _PASSWORD, "Managed Target", "manager")
                repo.create_user("fallback-user", _PASSWORD, "Fallback User", "manager")
                created_link: dict[str, object] = {}

                def create_rule_after_preliminary_managed_lookup(_yandex_user_id: str) -> None:
                    link = repo.create_yandex_oauth_managed_link(
                        identifier_type="login",
                        identifier="managed-login",
                        crm_user_id=int(target["id"]),
                    )
                    if not is_active:
                        link = repo.update_yandex_oauth_managed_link(
                            int(link["id"]),
                            is_active=False,
                        )
                    created_link.update(link or {})
                    return None

                async def exercise():
                    async with await _client() as client:
                        callback = await _oauth_callback(
                            client,
                            {"id": f"race-{scenario}-yid", "login": "managed-login"},
                        )
                        me = await client.get("/api/auth/me")
                        return callback, me

                environment = dict(_OAUTH_ENVIRONMENT)
                if is_active:
                    environment["YANDEX_OAUTH_USER_MAP"] = json.dumps(
                        {"id:unrelated-yandex-user": "fallback-user"}
                    )
                with (
                    mock.patch.dict(os.environ, environment, clear=False),
                    mock.patch.object(
                        repo,
                        "get_yandex_oauth_linked_user_id",
                        side_effect=create_rule_after_preliminary_managed_lookup,
                    ),
                ):
                    callback, me = _run(exercise())

                self.assertTrue(created_link)
                self.assertEqual(303, callback.status_code)
                if error_code:
                    self.assertEqual(f"/?yandex_oauth={error_code}", callback.headers["location"])
                    self.assertEqual(401, me.status_code)
                else:
                    self.assertEqual("/", callback.headers["location"])
                    self.assertEqual(int(target["id"]), int(me.json()["user"]["id"]))
                with db.get_connection() as connection:
                    managed = connection.execute(
                        "SELECT yandex_user_id, crm_user_id FROM yandex_oauth_managed_links WHERE id=?",
                        (created_link["id"],),
                    ).fetchone()
                    sessions = connection.execute(
                        "SELECT user_id FROM sessions ORDER BY id"
                    ).fetchall()
                if is_active:
                    self.assertEqual(f"race-{scenario}-yid", managed["yandex_user_id"])
                    self.assertEqual(int(target["id"]), int(managed["crm_user_id"]))
                    self.assertEqual([int(target["id"])], [int(row["user_id"]) for row in sessions])
                else:
                    self.assertIsNone(managed["yandex_user_id"])
                    self.assertEqual([], sessions)

    def test_active_match_pins_identity_and_username_rename_does_not_change_owner(self) -> None:
        managed = repo.create_yandex_oauth_managed_link(
            identifier_type="email",
            identifier="managed@example.test",
            crm_user_id=int(self.target["id"]),
        )
        profile = {
            "id": "active-managed-yid",
            "login": "managed-login",
            "default_email": "MANAGED@example.test",
        }

        async def exercise():
            async with await _client() as first_client:
                first = await _oauth_callback(first_client, profile)
                first_me = await first_client.get("/api/auth/me")
            repo.update_user_profile(int(self.target["id"]), username="managed-target-renamed")
            repo.create_user("managed-target", _PASSWORD, "Replacement", "manager")
            async with await _client() as second_client:
                second = await _oauth_callback(second_client, {"id": "active-managed-yid"})
                second_me = await second_client.get("/api/auth/me")
            return first, first_me, second, second_me

        with mock.patch.dict(os.environ, _OAUTH_ENVIRONMENT, clear=False):
            first, first_me, second, second_me = _run(exercise())

        self.assertEqual((303, "/"), (first.status_code, first.headers["location"]))
        self.assertEqual((303, "/"), (second.status_code, second.headers["location"]))
        self.assertEqual(int(self.target["id"]), int(first_me.json()["user"]["id"]))
        self.assertEqual(int(self.target["id"]), int(second_me.json()["user"]["id"]))
        with db.get_connection() as connection:
            pinned = connection.execute(
                "SELECT yandex_user_id, crm_user_id FROM yandex_oauth_managed_links WHERE id=?",
                (managed["id"],),
            ).fetchone()
            immutable = connection.execute(
                "SELECT crm_user_id FROM yandex_oauth_links WHERE yandex_user_id=?",
                ("active-managed-yid",),
            ).fetchone()
        self.assertEqual("active-managed-yid", pinned["yandex_user_id"])
        self.assertEqual(int(self.target["id"]), int(pinned["crm_user_id"]))
        self.assertEqual(int(self.target["id"]), int(immutable["crm_user_id"]))

    def test_inactive_or_missing_managed_user_cannot_receive_session(self) -> None:
        for scenario in ("inactive", "missing"):
            with self.subTest(scenario=scenario):
                foundation._remove_test_runtime_files()
                db.init_db()
                target = repo.create_user(f"target-{scenario}", _PASSWORD, "Target", "manager")
                repo.create_yandex_oauth_managed_link(
                    identifier_type="login",
                    identifier=f"managed-{scenario}",
                    crm_user_id=int(target["id"]),
                )
                if scenario == "inactive":
                    repo.update_user(int(target["id"]), is_active=False)
                    expected = "account_inactive"
                else:
                    with db.get_connection() as connection:
                        connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute(
                            "UPDATE yandex_oauth_managed_links SET crm_user_id=?",
                            (999_999_999,),
                        )
                    expected = "account_not_allowed"

                async def exercise():
                    async with await _client() as client:
                        return await _oauth_callback(
                            client,
                            {"id": f"managed-{scenario}-yid", "login": f"managed-{scenario}"},
                        )

                with mock.patch.dict(os.environ, _OAUTH_ENVIRONMENT, clear=False):
                    callback = _run(exercise())
                self.assertEqual(f"/?yandex_oauth={expected}", callback.headers["location"])
                with db.get_connection() as connection:
                    self.assertEqual(0, int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]))

    def test_delete_archives_confirmed_link_blocks_fallback_and_reenable_uses_original_user(self) -> None:
        managed = repo.create_yandex_oauth_managed_link(
            identifier_type="login",
            identifier="managed-login",
            crm_user_id=int(self.target["id"]),
        )
        profile = {"id": "delete-confirmed-yid", "login": "managed-login"}

        async def exercise():
            async with await _client() as oauth_client:
                initial_callback = await _oauth_callback(oauth_client, profile)
                initial_me = await oauth_client.get("/api/auth/me")
                with db.get_connection() as connection:
                    target_sessions_before_delete = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                            (self.target["id"],),
                        ).fetchone()[0]
                    )

                async with await _client() as admin_client:
                    await _login(admin_client, "managed-admin")
                    headers = await _csrf_headers(admin_client)
                    deleted = await admin_client.delete(
                        f"/api/admin/yandex-oauth-links/{managed['id']}",
                        headers=headers,
                    )

                existing_session_after_delete = await oauth_client.get("/api/auth/me")

                async with await _client() as denied_client:
                    denied_callback = await _oauth_callback(denied_client, profile)
                    denied_me = await denied_client.get("/api/auth/me")

                with db.get_connection() as connection:
                    target_sessions_after_denial = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                            (self.target["id"],),
                        ).fetchone()[0]
                    )
                    fallback_sessions_after_denial = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
                            (self.fallback["id"],),
                        ).fetchone()[0]
                    )
                    tombstone_after_denial = connection.execute(
                        """
                        SELECT yandex_user_id, crm_user_id, is_active
                        FROM yandex_oauth_managed_links
                        WHERE id=?
                        """,
                        (managed["id"],),
                    ).fetchone()
                    immutable_after_denial = connection.execute(
                        "SELECT crm_user_id FROM yandex_oauth_links WHERE yandex_user_id=?",
                        ("delete-confirmed-yid",),
                    ).fetchone()

                async with await _client() as admin_client:
                    await _login(admin_client, "managed-admin")
                    headers = await _csrf_headers(admin_client)
                    reenabled = await admin_client.patch(
                        f"/api/admin/yandex-oauth-links/{managed['id']}",
                        json={"is_active": True},
                        headers=headers,
                    )

                async with await _client() as reenabled_client:
                    reenabled_callback = await _oauth_callback(reenabled_client, profile)
                    reenabled_me = await reenabled_client.get("/api/auth/me")

            return (
                initial_callback,
                initial_me,
                target_sessions_before_delete,
                deleted,
                existing_session_after_delete,
                denied_callback,
                denied_me,
                target_sessions_after_denial,
                fallback_sessions_after_denial,
                tombstone_after_denial,
                immutable_after_denial,
                reenabled,
                reenabled_callback,
                reenabled_me,
            )

        with (
            mock.patch.dict(os.environ, _OAUTH_ENVIRONMENT, clear=False),
            mock.patch.object(
                yandex_oauth,
                "resolve_crm_username",
                wraps=yandex_oauth.resolve_crm_username,
            ) as resolver,
        ):
            results = _run(exercise())

        (
            initial_callback,
            initial_me,
            target_sessions_before_delete,
            deleted,
            existing_session_after_delete,
            denied_callback,
            denied_me,
            target_sessions_after_denial,
            fallback_sessions_after_denial,
            tombstone_after_denial,
            immutable_after_denial,
            reenabled,
            reenabled_callback,
            reenabled_me,
        ) = results

        self.assertEqual((303, "/"), (initial_callback.status_code, initial_callback.headers["location"]))
        self.assertEqual(int(self.target["id"]), int(initial_me.json()["user"]["id"]))
        self.assertEqual(1, target_sessions_before_delete)
        self.assertEqual(200, deleted.status_code)
        self.assertEqual(int(self.target["id"]), int(existing_session_after_delete.json()["user"]["id"]))
        self.assertEqual(
            (303, "/?yandex_oauth=account_not_allowed"),
            (denied_callback.status_code, denied_callback.headers["location"]),
        )
        self.assertEqual(401, denied_me.status_code)
        self.assertEqual(target_sessions_before_delete, target_sessions_after_denial)
        self.assertEqual(0, fallback_sessions_after_denial)
        self.assertEqual("delete-confirmed-yid", tombstone_after_denial["yandex_user_id"])
        self.assertEqual(int(self.target["id"]), int(tombstone_after_denial["crm_user_id"]))
        self.assertEqual(0, int(tombstone_after_denial["is_active"]))
        self.assertEqual(int(self.target["id"]), int(immutable_after_denial["crm_user_id"]))
        self.assertEqual(200, reenabled.status_code)
        self.assertTrue(reenabled.json()["is_active"])
        self.assertEqual((303, "/"), (reenabled_callback.status_code, reenabled_callback.headers["location"]))
        self.assertEqual(int(self.target["id"]), int(reenabled_me.json()["user"]["id"]))
        resolver.assert_not_called()
        with db.get_connection() as connection:
            archived = connection.execute(
                """
                SELECT yandex_user_id, crm_user_id, is_active
                FROM yandex_oauth_managed_links
                WHERE id=?
                """,
                (managed["id"],),
            ).fetchone()
            immutable = connection.execute(
                """
                SELECT crm_user_id
                FROM yandex_oauth_links
                WHERE yandex_user_id=?
                """,
                ("delete-confirmed-yid",),
            ).fetchone()
        self.assertEqual("delete-confirmed-yid", archived["yandex_user_id"])
        self.assertEqual(int(self.target["id"]), int(archived["crm_user_id"]))
        self.assertEqual(1, int(archived["is_active"]))
        self.assertEqual(int(self.target["id"]), int(immutable["crm_user_id"]))

    def test_delete_archives_unconfirmed_login_and_email_and_blocks_environment_fallback(self) -> None:
        managed_login = repo.create_yandex_oauth_managed_link(
            identifier_type="login",
            identifier="tombstone-login",
            crm_user_id=int(self.target["id"]),
        )
        managed_email = repo.create_yandex_oauth_managed_link(
            identifier_type="email",
            identifier="tombstone@example.test",
            crm_user_id=int(self.target["id"]),
        )

        async def exercise():
            async with await _client() as admin_client:
                await _login(admin_client, "managed-admin")
                headers = await _csrf_headers(admin_client)
                deleted_login = await admin_client.delete(
                    f"/api/admin/yandex-oauth-links/{managed_login['id']}", headers=headers
                )
                deleted_email = await admin_client.delete(
                    f"/api/admin/yandex-oauth-links/{managed_email['id']}", headers=headers
                )
            async with await _client() as callback_client:
                login_callback = await _oauth_callback(
                    callback_client,
                    {"id": "unconfirmed-login-yid", "login": "tombstone-login"},
                )
                email_callback = await _oauth_callback(
                    callback_client,
                    {
                        "id": "unconfirmed-email-yid",
                        "login": "unrelated-login",
                        "default_email": "TOMBSTONE@example.test",
                    },
                )
            return deleted_login, deleted_email, login_callback, email_callback

        environment = dict(_OAUTH_ENVIRONMENT)
        environment["YANDEX_OAUTH_USER_MAP"] = json.dumps(
            {
                "login:tombstone-login": "fallback-user",
                "email:tombstone@example.test": "fallback-user",
            }
        )
        with (
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(
                yandex_oauth,
                "resolve_crm_username",
                wraps=yandex_oauth.resolve_crm_username,
            ) as resolver,
        ):
            deleted_login, deleted_email, login_callback, email_callback = _run(exercise())

        self.assertEqual(200, deleted_login.status_code)
        self.assertEqual(200, deleted_email.status_code)
        self.assertEqual(
            ["/?yandex_oauth=account_not_allowed", "/?yandex_oauth=account_not_allowed"],
            [login_callback.headers["location"], email_callback.headers["location"]],
        )
        resolver.assert_not_called()
        with db.get_connection() as connection:
            tombstones = connection.execute(
                """
                SELECT id, yandex_user_id, is_active
                FROM yandex_oauth_managed_links
                WHERE id IN (?, ?)
                ORDER BY id
                """,
                (managed_login["id"], managed_email["id"]),
            ).fetchall()
            oauth_links = int(connection.execute("SELECT COUNT(*) FROM yandex_oauth_links").fetchone()[0])
            oauth_sessions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE user_id IN (?, ?)",
                    (self.target["id"], self.fallback["id"]),
                ).fetchone()[0]
            )
        self.assertEqual(
            [
                (int(managed_login["id"]), None, 0),
                (int(managed_email["id"]), None, 0),
            ],
            [(int(row["id"]), row["yandex_user_id"], int(row["is_active"])) for row in tombstones],
        )
        self.assertEqual(0, oauth_links)
        self.assertEqual(0, oauth_sessions)

    def test_frontend_has_admin_only_managed_link_controls_without_oauth_secret_fields(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
        script = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        start = html.index('id="yandexOAuthLinksCard"')
        section = html[start : html.index("</section>", start) + len("</section>")]
        self.assertIn("Авторизация через Яндекс", section)
        self.assertIn("admin-only hidden", section)
        self.assertIn('id="yandexOAuthIdentifierType"', section)
        self.assertIn('id="yandexOAuthIdentifier"', section)
        self.assertIn('id="yandexOAuthCrmUser"', section)
        self.assertIn("новые входы через Яндекс", section)
        self.assertIn("деактивируйте сотрудника", section)
        self.assertIn("loadYandexOAuthManagedLinks", script)
        self.assertIn("Запретить вход", script)
        self.assertNotIn("Удалить эту связь для входа через Яндекс?", script)
        reset_start = script.index("function resetYandexOAuthManagedLinksState()")
        reset_section = script[reset_start : script.index("\n}\n", reset_start) + 3]
        self.assertIn("yandexOAuthManagedLinksCache = [];", reset_section)
        self.assertIn("yandexOAuthManagedUsersCache = [];", reset_section)
        self.assertIn("list.innerHTML = '';", reset_section)
        self.assertIn("userSelect.innerHTML = '';", reset_section)
        self.assertIn("identifierInput.value = '';", reset_section)
        show_login = script[script.index("function showLogin(") : script.index("function showApp(")]
        show_app = script[script.index("function showApp(") : script.index("async function checkAuth(")]
        self.assertIn("resetYandexOAuthManagedLinksState();", show_login)
        self.assertIn("currentUser?.role !== 'admin'", show_app)
        self.assertIn("resetYandexOAuthManagedLinksState();", show_app)
        for forbidden in ("client secret", "client_secret", "redirect uri", "redirect_uri"):
            self.assertNotIn(forbidden, section.lower())

    def test_frontend_ignores_managed_link_responses_after_reset_or_newer_load(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
        declarations = "\n".join(
            line
            for line in source.splitlines()
            if line.startswith("let yandexOAuthManaged")
        )
        production_code = "\n\n".join(
            (
                declarations,
                _javascript_function(source, "resetYandexOAuthManagedLinksState"),
                _javascript_function(source, "renderYandexOAuthManagedLinks"),
                _javascript_function(source, "loadYandexOAuthManagedLinks"),
            )
        )
        harness = f"""
const assert = require('node:assert/strict');
const elements = {{
  yandexOAuthLinksList: {{ innerHTML: 'initial-list' }},
  yandexOAuthCrmUser: {{ innerHTML: 'initial-users' }},
  yandexOAuthIdentifier: {{ value: 'initial-identifier' }},
  yandexOAuthIdentifierType: {{ value: 'email' }},
}};
function $(id) {{ return elements[id] || null; }}
function escapeHtml(value) {{ return String(value); }}
let currentUser = {{ role: 'admin' }};
const pending = [];
function api() {{ return new Promise((resolve) => pending.push(resolve)); }}

{production_code}

(async () => {{
  const loggedOutLoad = loadYandexOAuthManagedLinks();
  assert.equal(pending.length, 1);
  resetYandexOAuthManagedLinksState();
  currentUser = {{ role: 'manager' }};
  pending.shift()({{
    links: [{{ id: 1, identifier_type: 'login', identifier: 'late-after-reset', crm_user_id: 1 }}],
    users: [{{ id: 1, username: 'late-after-reset', display_name: 'Late', is_active: true }}],
  }});
  await loggedOutLoad;
  assert.deepEqual(yandexOAuthManagedLinksCache, []);
  assert.deepEqual(yandexOAuthManagedUsersCache, []);
  assert.equal(elements.yandexOAuthLinksList.innerHTML, '');
  assert.equal(elements.yandexOAuthCrmUser.innerHTML, '');

  currentUser = {{ role: 'admin' }};
  const oldLoad = loadYandexOAuthManagedLinks();
  const newLoad = loadYandexOAuthManagedLinks();
  assert.equal(pending.length, 2);
  pending[1]({{
    links: [{{ id: 2, identifier_type: 'login', identifier: 'new-response', crm_user_id: 2, is_active: true }}],
    users: [{{ id: 2, username: 'new-user', display_name: 'New', is_active: true }}],
  }});
  await newLoad;
  pending[0]({{
    links: [{{ id: 3, identifier_type: 'login', identifier: 'old-response', crm_user_id: 3, is_active: true }}],
    users: [{{ id: 3, username: 'old-user', display_name: 'Old', is_active: true }}],
  }});
  await oldLoad;
  assert.equal(yandexOAuthManagedLinksCache[0].identifier, 'new-response');
  assert.match(elements.yandexOAuthLinksList.innerHTML, /new-response/);
  assert.doesNotMatch(elements.yandexOAuthLinksList.innerHTML, /old-response/);
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
        result = subprocess.run(
            ["node", "-e", harness],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
