from __future__ import annotations

import contextlib
import hmac
import json
import threading
import unittest
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo
_TEST_EVENT_LOOP = foundation._TEST_EVENT_LOOP

_OLD_PASSWORD = "old-password-2026"
_NEW_PASSWORD = "new-password-2026"


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"user-agent": "session-revocation-test"},
    )


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain test CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


async def _me_with_token(token: str) -> httpx.Response:
    async with await _client() as client:
        return await client.get(
            "/api/auth/me",
            headers={"cookie": f"{main.AUTH_COOKIE_NAME}={token}"},
        )


def _run(coroutine):
    return _TEST_EVENT_LOOP.run_until_complete(coroutine)


class AuthSessionRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        db.init_db()
        self.admin = repo.create_user("security-admin", _OLD_PASSWORD, "Admin", "admin")
        self.target = repo.create_user("security-target", _OLD_PASSWORD, "Target", "viewer")
        self.other = repo.create_user("security-other", _OLD_PASSWORD, "Other", "viewer")

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_self_service_rotates_current_session_and_revokes_every_old_session(self) -> None:
        async def exercise():
            async with await _client() as session_a, await _client() as session_b, await _client() as other:
                login_a = await _login(session_a, "security-target", _OLD_PASSWORD)
                login_b = await _login(session_b, "security-target", _OLD_PASSWORD)
                await _login(other, "security-other", _OLD_PASSWORD)
                old_a = session_a.cookies.get(main.AUTH_COOKIE_NAME)
                old_b = session_b.cookies.get(main.AUTH_COOKIE_NAME)
                other_token = other.cookies.get(main.AUTH_COOKIE_NAME)

                change_headers = await _csrf_headers(session_a)
                change_headers["user-agent"] = "rotated-session-agent"
                changed = await session_a.patch(
                    "/api/auth/profile",
                    json={
                        "username": "security-target-renamed",
                        "display_name": "Rotated Target",
                        "current_password": _OLD_PASSWORD,
                        "new_password": _NEW_PASSWORD,
                    },
                    headers=change_headers,
                )
                new_a = session_a.cookies.get(main.AUTH_COOKIE_NAME)
                new_me = await session_a.get("/api/auth/me")
                old_a_me = await _me_with_token(old_a)
                old_b_me = await _me_with_token(old_b)
                other_me = await _me_with_token(other_token)
                old_login_client = await _client()
                new_login_client = await _client()
                try:
                    old_login = await _login(old_login_client, "security-target-renamed", _OLD_PASSWORD)
                    new_login = await _login(new_login_client, "security-target-renamed", _NEW_PASSWORD)
                finally:
                    await old_login_client.aclose()
                    await new_login_client.aclose()

            return (
                login_a,
                changed,
                old_a,
                old_b,
                new_a,
                new_me,
                old_a_me,
                old_b_me,
                other_me,
                old_login,
                new_login,
            )

        (
            login_a,
            changed,
            old_a,
            old_b,
            new_a,
            new_me,
            old_a_me,
            old_b_me,
            other_me,
            old_login,
            new_login,
        ) = _run(exercise())

        self.assertEqual(200, changed.status_code)
        self.assertNotEqual(old_a, new_a)
        self.assertNotEqual(old_b, new_a)
        self.assertEqual(200, new_me.status_code)
        self.assertEqual(401, old_a_me.status_code)
        self.assertEqual(401, old_b_me.status_code)
        self.assertEqual(200, other_me.status_code)
        self.assertEqual(401, old_login.status_code)
        self.assertEqual(200, new_login.status_code)
        self.assertEqual("security-target-renamed", changed.json()["user"]["username"])
        self.assertEqual("Rotated Target", changed.json()["user"]["display_name"])

        login_attributes = login_a.headers["set-cookie"].split(";", 1)[1]
        rotated_attributes = changed.headers["set-cookie"].split(";", 1)[1]
        self.assertEqual(login_attributes, rotated_attributes)
        for attribute in ("HttpOnly", "Max-Age=1209600", "Path=/", "SameSite=lax", "Secure"):
            self.assertIn(attribute, changed.headers["set-cookie"])

        response_text = json.dumps(changed.json(), ensure_ascii=False)
        self.assertNotIn(old_a, response_text)
        self.assertNotIn(new_a, response_text)
        self.assertNotIn("password_hash", response_text)
        self.assertNotIn(_OLD_PASSWORD, response_text)
        self.assertNotIn(_NEW_PASSWORD, response_text)
        with db.get_connection() as connection:
            rotated = connection.execute(
                "SELECT user_agent, ip, expires_at FROM sessions WHERE session_token=?",
                (new_a,),
            ).fetchone()
        self.assertEqual("rotated-session-agent", rotated["user_agent"])
        self.assertEqual("127.0.0.1", rotated["ip"])
        self.assertTrue(rotated["expires_at"])

    def test_wrong_current_validation_and_profile_only_requests_preserve_sessions(self) -> None:
        def stored_password_hash() -> str:
            with db.get_connection() as connection:
                row = connection.execute(
                    "SELECT password_hash FROM users WHERE id=?",
                    (int(self.target["id"]),),
                ).fetchone()
            if row is None:
                raise AssertionError("target user disappeared from the test database")
            return str(row["password_hash"])

        async def exercise():
            async with await _client() as session_a, await _client() as session_b:
                await _login(session_a, "security-target", _OLD_PASSWORD)
                await _login(session_b, "security-target", _OLD_PASSWORD)
                token_a = session_a.cookies.get(main.AUTH_COOKIE_NAME)
                token_b = session_b.cookies.get(main.AUTH_COOKIE_NAME)
                headers = await _csrf_headers(session_a)
                hash_before = stored_password_hash()

                wrong = await session_a.patch(
                    "/api/auth/profile",
                    json={
                        "username": "wrong-current-name",
                        "display_name": "Wrong Current Must Roll Back",
                        "current_password": "definitely-wrong",
                        "new_password": _NEW_PASSWORD,
                    },
                    headers=headers,
                )
                after_wrong = repo.get_user_by_id(int(self.target["id"]))
                hash_after_wrong = stored_password_hash()
                wrong_tokens_unchanged = (
                    session_a.cookies.get(main.AUTH_COOKIE_NAME) == token_a,
                    session_b.cookies.get(main.AUTH_COOKIE_NAME) == token_b,
                )
                wrong_sessions_valid = (
                    repo.get_user_by_session(token_a) is not None,
                    repo.get_user_by_session(token_b) is not None,
                )
                invalid = await session_a.patch(
                    "/api/auth/profile",
                    json={
                        "username": "invalid-password-name",
                        "display_name": "Invalid Password Must Roll Back",
                        "current_password": _OLD_PASSWORD,
                        "new_password": "short",
                    },
                    headers=headers,
                )
                after_invalid = repo.get_user_by_id(int(self.target["id"]))
                hash_after_invalid = stored_password_hash()
                invalid_tokens_unchanged = (
                    session_a.cookies.get(main.AUTH_COOKIE_NAME) == token_a,
                    session_b.cookies.get(main.AUTH_COOKIE_NAME) == token_b,
                )
                invalid_sessions_valid = (
                    repo.get_user_by_session(token_a) is not None,
                    repo.get_user_by_session(token_b) is not None,
                )
                profile_only = await session_a.patch(
                    "/api/auth/profile",
                    json={"display_name": "Renamed Target"},
                    headers=headers,
                )
                return {
                    "wrong": wrong,
                    "after_wrong": after_wrong,
                    "invalid": invalid,
                    "after_invalid": after_invalid,
                    "profile_only": profile_only,
                    "token_a": token_a,
                    "token_b": token_b,
                    "hash_before": hash_before,
                    "hash_after_wrong": hash_after_wrong,
                    "hash_after_invalid": hash_after_invalid,
                    "wrong_tokens_unchanged": wrong_tokens_unchanged,
                    "wrong_sessions_valid": wrong_sessions_valid,
                    "invalid_tokens_unchanged": invalid_tokens_unchanged,
                    "invalid_sessions_valid": invalid_sessions_valid,
                }

        observed = _run(exercise())
        wrong = observed["wrong"]
        invalid = observed["invalid"]
        profile_only = observed["profile_only"]
        token_a = observed["token_a"]
        token_b = observed["token_b"]
        self.assertEqual(400, wrong.status_code)
        self.assertEqual("security-target", observed["after_wrong"]["username"])
        self.assertEqual("Target", observed["after_wrong"]["display_name"])
        self.assertTrue(hmac.compare_digest(observed["hash_before"], observed["hash_after_wrong"]))
        self.assertEqual((True, True), observed["wrong_tokens_unchanged"])
        self.assertEqual((True, True), observed["wrong_sessions_valid"])
        self.assertEqual(422, invalid.status_code)
        self.assertEqual("security-target", observed["after_invalid"]["username"])
        self.assertEqual("Target", observed["after_invalid"]["display_name"])
        self.assertTrue(hmac.compare_digest(observed["hash_before"], observed["hash_after_invalid"]))
        self.assertEqual((True, True), observed["invalid_tokens_unchanged"])
        self.assertEqual((True, True), observed["invalid_sessions_valid"])
        self.assertEqual(200, profile_only.status_code)
        self.assertEqual("Renamed Target", profile_only.json()["user"]["display_name"])
        self.assertIsNotNone(repo.get_user_by_session(token_a))
        self.assertIsNotNone(repo.get_user_by_session(token_b))
        self.assertIsNotNone(repo.authenticate_user("security-target", _OLD_PASSWORD))
        self.assertIsNone(repo.authenticate_user("security-target", _NEW_PASSWORD))

        error_text = json.dumps(wrong.json(), ensure_ascii=False)
        self.assertNotIn(token_a, error_text)
        self.assertNotIn(token_b, error_text)
        self.assertNotIn("password_hash", error_text)

    def test_admin_reset_revokes_target_only_and_does_not_rotate_actor_cookie(self) -> None:
        async def exercise():
            async with await _client() as actor, await _client() as target_a, await _client() as target_b, await _client() as other:
                await _login(actor, "security-admin", _OLD_PASSWORD)
                await _login(target_a, "security-target", _OLD_PASSWORD)
                await _login(target_b, "security-target", _OLD_PASSWORD)
                await _login(other, "security-other", _OLD_PASSWORD)
                actor_token = actor.cookies.get(main.AUTH_COOKIE_NAME)
                target_a_token = target_a.cookies.get(main.AUTH_COOKIE_NAME)
                target_b_token = target_b.cookies.get(main.AUTH_COOKIE_NAME)
                other_token = other.cookies.get(main.AUTH_COOKIE_NAME)

                response = await actor.post(
                    f"/api/users/{self.target['id']}/password",
                    json={"password": _NEW_PASSWORD},
                    headers=await _csrf_headers(actor),
                )
                actor_token_after = actor.cookies.get(main.AUTH_COOKIE_NAME)
                return response, actor_token, actor_token_after, target_a_token, target_b_token, other_token

        response, actor_token, actor_token_after, target_a, target_b, other = _run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual(actor_token, actor_token_after)
        self.assertIsNotNone(repo.get_user_by_session(actor_token))
        self.assertIsNone(repo.get_user_by_session(target_a))
        self.assertIsNone(repo.get_user_by_session(target_b))
        self.assertIsNotNone(repo.get_user_by_session(other))
        self.assertIsNone(repo.authenticate_user("security-target", _OLD_PASSWORD))
        self.assertIsNotNone(repo.authenticate_user("security-target", _NEW_PASSWORD))
        self.assertNotIn(main.AUTH_COOKIE_NAME, response.headers.get("set-cookie", ""))

    def test_admin_self_reset_revokes_the_actor_session(self) -> None:
        async def exercise():
            async with await _client() as actor:
                await _login(actor, "security-admin", _OLD_PASSWORD)
                actor_token = actor.cookies.get(main.AUTH_COOKIE_NAME)
                response = await actor.post(
                    f"/api/users/{self.admin['id']}/password",
                    json={"password": _NEW_PASSWORD},
                    headers=await _csrf_headers(actor),
                )
                follow_up = await actor.get("/api/auth/me")
                return response, follow_up, actor_token

        response, follow_up, actor_token = _run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual(401, follow_up.status_code)
        self.assertIsNone(repo.get_user_by_session(actor_token))

    def test_csrf_and_rbac_failures_do_not_revoke_target_sessions(self) -> None:
        async def exercise():
            async with await _client() as admin, await _client() as viewer, await _client() as target:
                await _login(admin, "security-admin", _OLD_PASSWORD)
                await _login(viewer, "security-other", _OLD_PASSWORD)
                await _login(target, "security-target", _OLD_PASSWORD)
                target_token = target.cookies.get(main.AUTH_COOKIE_NAME)
                no_csrf = await admin.post(
                    f"/api/users/{self.target['id']}/password",
                    json={"password": _NEW_PASSWORD},
                )
                forbidden = await viewer.post(
                    f"/api/users/{self.target['id']}/password",
                    json={"password": _NEW_PASSWORD},
                    headers=await _csrf_headers(viewer),
                )
                return no_csrf, forbidden, target_token

        no_csrf, forbidden, target_token = _run(exercise())
        self.assertEqual(403, no_csrf.status_code)
        self.assertIn("CSRF", no_csrf.json()["detail"])
        self.assertEqual(403, forbidden.status_code)
        self.assertIsNotNone(repo.get_user_by_session(target_token))
        self.assertIsNotNone(repo.authenticate_user("security-target", _OLD_PASSWORD))

    def test_in_progress_old_password_login_cannot_survive_concurrent_reset(self) -> None:
        verification_started = threading.Event()
        allow_verification = threading.Event()
        reset_attempting_lock = threading.Event()
        login_result: list[tuple[dict[str, object], str] | None] = []
        thread_errors: list[BaseException] = []
        real_verify_password = repo._verify_password

        def controlled_verify(password: str, encoded: str) -> bool:
            verification_started.set()
            if not allow_verification.wait(timeout=5):
                raise AssertionError("timed out waiting to release password verification")
            return real_verify_password(password, encoded)

        def run_login() -> None:
            try:
                login_result.append(
                    repo.authenticate_user_and_create_session(
                        "security-target",
                        _OLD_PASSWORD,
                        user_agent="concurrent-login",
                        ip="127.0.0.1",
                        seconds=3600,
                    )
                )
            except BaseException as exc:
                thread_errors.append(exc)

        def run_reset() -> None:
            try:
                repo.update_user_password(int(self.target["id"]), _NEW_PASSWORD)
            except BaseException as exc:
                thread_errors.append(exc)

        class TrackingConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, parameters=()):
                if threading.current_thread().name == "password-reset" and sql == "BEGIN IMMEDIATE":
                    reset_attempting_lock.set()
                return self._connection.execute(sql, parameters)

        @contextlib.contextmanager
        def tracked_connection():
            with db.get_connection() as connection:
                yield TrackingConnection(connection)

        with (
            mock.patch.object(repo, "_verify_password", side_effect=controlled_verify),
            mock.patch.object(repo, "get_connection", tracked_connection),
        ):
            login_thread = threading.Thread(target=run_login, name="old-password-login")
            reset_thread = threading.Thread(target=run_reset, name="password-reset")
            login_thread.start()
            self.assertTrue(verification_started.wait(timeout=5))
            reset_thread.start()
            self.assertTrue(reset_attempting_lock.wait(timeout=5))
            allow_verification.set()
            login_thread.join(timeout=5)
            reset_thread.join(timeout=5)

        self.assertFalse(login_thread.is_alive())
        self.assertFalse(reset_thread.is_alive())
        self.assertEqual([], thread_errors)
        self.assertEqual(1, len(login_result))
        self.assertIsNotNone(login_result[0])
        login_user, login_token = login_result[0]
        self.assertEqual(int(self.target["id"]), int(login_user["id"]))
        self.assertIsNone(repo.get_user_by_session(login_token))
        self.assertIsNone(repo.authenticate_user("security-target", _OLD_PASSWORD))
        self.assertIsNotNone(repo.authenticate_user("security-target", _NEW_PASSWORD))

    def test_failed_atomic_login_does_not_create_a_session(self) -> None:
        with db.get_connection() as connection:
            before = int(connection.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"])

        result = repo.authenticate_user_and_create_session(
            "security-target",
            "wrong-password",
            user_agent="failed-login",
            ip="127.0.0.1",
            seconds=3600,
        )

        with db.get_connection() as connection:
            after = int(connection.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"])
        self.assertIsNone(result)
        self.assertEqual(before, after)

    def test_rotation_failure_rolls_back_password_and_session_revocation(self) -> None:
        token_a = repo.create_session(int(self.target["id"]), user_agent="a", ip="127.0.0.1")
        token_b = repo.create_session(int(self.target["id"]), user_agent="b", ip="127.0.0.1")

        class FailingConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, parameters=()):
                if "INSERT INTO sessions" in sql:
                    raise RuntimeError("synthetic session insert failure")
                return self._connection.execute(sql, parameters)

        @contextlib.contextmanager
        def failing_connection():
            with db.get_connection() as connection:
                yield FailingConnection(connection)

        with mock.patch.object(repo, "get_connection", failing_connection):
            with self.assertRaisesRegex(RuntimeError, "synthetic session insert failure"):
                repo.change_user_password_and_rotate_session(
                    int(self.target["id"]),
                    current_password=_OLD_PASSWORD,
                    new_password=_NEW_PASSWORD,
                    username="rollback-target",
                    display_name="Rollback Target",
                    user_agent="rollback-test",
                    ip="127.0.0.1",
                    seconds=3600,
                )

        self.assertIsNotNone(repo.authenticate_user("security-target", _OLD_PASSWORD))
        self.assertIsNone(repo.authenticate_user("security-target", _NEW_PASSWORD))
        rolled_back_user = repo.get_user_by_id(int(self.target["id"]))
        self.assertEqual("security-target", rolled_back_user["username"])
        self.assertEqual("Target", rolled_back_user["display_name"])
        self.assertIsNotNone(repo.get_user_by_session(token_a))
        self.assertIsNotNone(repo.get_user_by_session(token_b))

        class FailingAdminConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, sql, parameters=()):
                if "UPDATE sessions SET revoked_at" in sql:
                    raise RuntimeError("synthetic session revoke failure")
                return self._connection.execute(sql, parameters)

        @contextlib.contextmanager
        def failing_admin_connection():
            with db.get_connection() as connection:
                yield FailingAdminConnection(connection)

        with mock.patch.object(repo, "get_connection", failing_admin_connection):
            with self.assertRaisesRegex(RuntimeError, "synthetic session revoke failure"):
                repo.update_user_password(int(self.target["id"]), _NEW_PASSWORD)

        self.assertIsNotNone(repo.authenticate_user("security-target", _OLD_PASSWORD))
        self.assertIsNone(repo.authenticate_user("security-target", _NEW_PASSWORD))
        self.assertIsNotNone(repo.get_user_by_session(token_a))
        self.assertIsNotNone(repo.get_user_by_session(token_b))


if __name__ == "__main__":
    unittest.main()
