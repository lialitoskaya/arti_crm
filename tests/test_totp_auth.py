from __future__ import annotations

import asyncio
import hashlib
import os
import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from unittest import mock

import httpx
import pyotp
from cryptography.fernet import Fernet

import test_regression_foundation as foundation  # noqa: E402
from app import totp_auth  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo

_PASSWORD = "totp-test-password-2026"
_FIXED_TIME = 1_800_000_000


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="https://testserver",
    )


async def _login(client: httpx.AsyncClient, username: str, password: str = _PASSWORD) -> httpx.Response:
    return await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"user-agent": "totp-security-test"},
    )


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain test CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


def _run(coroutine):
    # Windows' event loop creates an internal socketpair. Temporarily restore the
    # real socket class only while constructing that local loop; application code
    # still runs under the network-deny guards installed by the test class.
    with mock.patch.object(foundation.socket, "socket", foundation._REAL_SOCKET):
        loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _code(secret: str, at_time: int = _FIXED_TIME) -> str:
    return pyotp.TOTP(secret, digits=6, interval=30, digest=hashlib.sha1).at(at_time)


def _wrong_code(correct_code: str) -> str:
    return "000000" if correct_code != "000000" else "999999"


class TotpAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        main.app.state.security_rate_limits = {}
        foundation._remove_test_runtime_files()
        self.encryption_key = Fernet.generate_key().decode("ascii")
        safe_env = {**foundation._SAFE_ENV, "CRM_TOTP_ENCRYPTION_KEY": self.encryption_key}
        self.env_patcher = mock.patch.dict(
            os.environ,
            safe_env,
            clear=True,
        )
        self.env_patcher.start()
        self.sqlite_patcher = mock.patch.object(
            db.sqlite3,
            "connect",
            side_effect=foundation._guarded_sqlite_connect,
        )
        self.sqlite_patcher.start()
        self.network_patchers = (
            mock.patch.object(foundation.socket, "socket", foundation._NoNetworkSocket),
            mock.patch.object(foundation.socket, "create_connection", foundation._deny_network),
        )
        for patcher in self.network_patchers:
            patcher.start()
        db.init_db()
        self.admin = repo.create_user("totp-admin", _PASSWORD, "TOTP Admin", "admin")
        self.other = repo.create_user("totp-other", _PASSWORD, "TOTP Other", "viewer")

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            foundation._remove_test_runtime_files()
            for patcher in reversed(self.network_patchers):
                patcher.stop()
            self.sqlite_patcher.stop()
            self.env_patcher.stop()

    def _enable_totp(self, user_id: int, *, secret: str | None = None) -> str:
        secret = secret or totp_auth.generate_secret()
        ciphertext = totp_auth.encrypt_secret(secret)
        with db.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO user_totp_credentials
                    (user_id, secret_ciphertext, enrollment_started_at, enabled_at, last_used_step)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
                """,
                (user_id, ciphertext),
            )
        return secret

    def test_password_only_login_contract_is_unchanged_without_totp(self) -> None:
        async def exercise():
            async with await _client() as client:
                response = await _login(client, "totp-admin")
                token = client.cookies.get(main.AUTH_COOKIE_NAME)
                me = await client.get("/api/auth/me")
                return response, token, me

        response, token, me = _run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual(True, response.json()["ok"])
        self.assertNotIn("requires_totp", response.json())
        self.assertTrue(token)
        self.assertEqual(200, me.status_code)
        self.assertIsNotNone(repo.get_user_by_session(token))

    def test_enabled_totp_password_creates_pending_challenge_not_full_session(self) -> None:
        self._enable_totp(int(self.admin["id"]))

        async def exercise():
            async with await _client() as client:
                response = await _login(client, "totp-admin")
                pending_token = client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
                full_token = client.cookies.get(main.AUTH_COOKIE_NAME)
                me = await client.get("/api/auth/me")
                return response, pending_token, full_token, me

        response, pending_token, full_token, me = _run(exercise())
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True, "requires_totp": True}, response.json())
        self.assertTrue(pending_token)
        self.assertIsNone(full_token)
        self.assertEqual(401, me.status_code)

        with db.get_connection() as connection:
            full_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()["c"]
            challenge = connection.execute(
                """
                SELECT challenge_hash, created_at, expires_at, attempts_remaining
                FROM pending_auth_challenges WHERE user_id=?
                """,
                (int(self.admin["id"]),),
            ).fetchone()
        self.assertEqual(0, int(full_sessions))
        self.assertEqual(5, int(challenge["attempts_remaining"]))
        self.assertEqual(hashlib.sha256(pending_token.encode("utf-8")).hexdigest(), challenge["challenge_hash"])
        self.assertNotEqual(pending_token, challenge["challenge_hash"])
        self.assertEqual(
            300,
            int((datetime.fromisoformat(challenge["expires_at"]) - datetime.fromisoformat(challenge["created_at"])).total_seconds()),
        )
        set_cookie = response.headers.get("set-cookie", "")
        for attribute in ("HttpOnly", "Max-Age=300", "Path=/api/auth/totp", "SameSite=lax", "Secure"):
            self.assertIn(attribute, set_cookie)

    def test_totp_primitives_are_sha1_six_digit_thirty_second_and_window_one(self) -> None:
        secret = totp_auth.generate_secret()
        uri = totp_auth.build_otpauth_uri(secret, "totp-admin")
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        self.assertEqual("otpauth", parsed.scheme)
        self.assertEqual("totp", parsed.netloc)
        self.assertEqual(["SHA1"], query["algorithm"])
        self.assertEqual(["6"], query["digits"])
        self.assertEqual(["30"], query["period"])
        self.assertEqual([secret], query["secret"])

        current_step = _FIXED_TIME // 30
        for offset in (-1, 0, 1):
            code = _code(secret, _FIXED_TIME + offset * 30)
            self.assertEqual(
                current_step + offset,
                totp_auth.verify_code(secret, code, at_time=_FIXED_TIME),
            )
        outside_code = _code(secret, _FIXED_TIME + 60)
        self.assertIsNone(totp_auth.verify_code(secret, outside_code, at_time=_FIXED_TIME))
        with self.assertRaises(ValueError):
            totp_auth.verify_code(secret, _code(secret), at_time=_FIXED_TIME, window=2)

    def test_wrong_code_decrements_attempts_and_correct_code_completes_login(self) -> None:
        secret = self._enable_totp(int(self.admin["id"]))
        correct_code = _code(secret)

        async def exercise():
            async with await _client() as client:
                password_response = await _login(client, "totp-admin")
                malformed = await client.post("/api/auth/totp/verify", json={"code": "12345"})
                wrong = await client.post(
                    "/api/auth/totp/verify",
                    json={"code": _wrong_code(correct_code)},
                )
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    verified = await client.post("/api/auth/totp/verify", json={"code": correct_code})
                full_token = client.cookies.get(main.AUTH_COOKIE_NAME)
                pending_token = client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
                me = await client.get("/api/auth/me")
                return password_response, malformed, wrong, verified, full_token, pending_token, me

        password_response, malformed, wrong, verified, full_token, pending_token, me = _run(exercise())
        self.assertEqual(200, password_response.status_code)
        self.assertEqual(422, malformed.status_code)
        self.assertEqual(400, wrong.status_code)
        self.assertEqual(200, verified.status_code)
        self.assertTrue(full_token)
        self.assertIsNone(pending_token)
        self.assertEqual(200, me.status_code)
        with db.get_connection() as connection:
            challenge = connection.execute(
                "SELECT attempts_remaining, consumed_at FROM pending_auth_challenges WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()
            credential = connection.execute(
                "SELECT last_used_step FROM user_totp_credentials WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()
        self.assertEqual(4, int(challenge["attempts_remaining"]))
        self.assertIsNotNone(challenge["consumed_at"])
        self.assertEqual(_FIXED_TIME // 30, int(credential["last_used_step"]))

    def test_expired_consumed_and_exhausted_challenges_are_rejected(self) -> None:
        secret = self._enable_totp(int(self.admin["id"]))
        correct_code = _code(secret)
        wrong_code = _wrong_code(correct_code)

        async def exercise():
            async with await _client() as expired_client:
                await _login(expired_client, "totp-admin")
                expired_token = expired_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
                with db.get_connection() as connection:
                    connection.execute(
                        "UPDATE pending_auth_challenges SET expires_at=? WHERE challenge_hash=?",
                        ("2000-01-01T00:00:00+00:00", hashlib.sha256(expired_token.encode()).hexdigest()),
                    )
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    expired = await expired_client.post("/api/auth/totp/verify", json={"code": correct_code})

            async with await _client() as success_client:
                await _login(success_client, "totp-admin")
                consumed_token = success_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    success = await success_client.post("/api/auth/totp/verify", json={"code": correct_code})

            async with await _client() as replay_client:
                replay = await replay_client.post(
                    "/api/auth/totp/verify",
                    json={"code": correct_code},
                    headers={"cookie": f"{main.TOTP_PENDING_COOKIE_NAME}={consumed_token}"},
                )

            async with await _client() as exhausted_client:
                await _login(exhausted_client, "totp-admin")
                exhausted_responses = []
                for _ in range(5):
                    exhausted_responses.append(
                        await exhausted_client.post("/api/auth/totp/verify", json={"code": wrong_code})
                    )
                after_exhaustion = await exhausted_client.post(
                    "/api/auth/totp/verify",
                    json={"code": _code(secret, _FIXED_TIME + 30)},
                )
            return expired, success, replay, exhausted_responses, after_exhaustion

        expired, success, replay, exhausted_responses, after_exhaustion = _run(exercise())
        self.assertEqual(401, expired.status_code)
        self.assertEqual(200, success.status_code)
        self.assertEqual(401, replay.status_code)
        self.assertEqual([400, 400, 400, 400, 401], [item.status_code for item in exhausted_responses])
        self.assertEqual(401, after_exhaustion.status_code)
        self.assertEqual(expired.json()["detail"], replay.json()["detail"])
        self.assertEqual(expired.json()["detail"], after_exhaustion.json()["detail"])

    def test_totp_step_cannot_be_replayed_across_challenges(self) -> None:
        secret = self._enable_totp(int(self.admin["id"]))
        code = _code(secret)

        async def exercise():
            async with await _client() as first:
                await _login(first, "totp-admin")
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    first_verify = await first.post("/api/auth/totp/verify", json={"code": code})
            async with await _client() as second:
                await _login(second, "totp-admin")
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    replay = await second.post("/api/auth/totp/verify", json={"code": code})
            return first_verify, replay

        first_verify, replay = _run(exercise())
        self.assertEqual(200, first_verify.status_code)
        self.assertEqual(400, replay.status_code)
        with db.get_connection() as connection:
            active_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND revoked_at IS NULL",
                (int(self.admin["id"]),),
            ).fetchone()["c"]
        self.assertEqual(1, int(active_sessions))

    def test_incomplete_enrollment_does_not_enable_totp_or_change_password_login(self) -> None:
        async def exercise():
            async with await _client() as admin:
                await _login(admin, "totp-admin")
                started = await admin.post(
                    "/api/auth/totp/enroll/start",
                    json={"current_password": _PASSWORD},
                    headers=await _csrf_headers(admin),
                )
            async with await _client() as fresh:
                password_login = await _login(fresh, "totp-admin")
                full_token = fresh.cookies.get(main.AUTH_COOKIE_NAME)
            return started, password_login, full_token

        started, password_login, full_token = _run(exercise())
        self.assertEqual(200, started.status_code)
        self.assertEqual(32, len(started.json()["secret"]))
        self.assertTrue(started.json()["otpauth_uri"].startswith("otpauth://totp/"))
        self.assertEqual(200, password_login.status_code)
        self.assertNotIn("requires_totp", password_login.json())
        self.assertTrue(full_token)
        with db.get_connection() as connection:
            credential = connection.execute(
                "SELECT enabled_at, last_used_step FROM user_totp_credentials WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()
        self.assertIsNone(credential["enabled_at"])
        self.assertIsNone(credential["last_used_step"])

    def test_confirm_enrollment_enables_totp_revokes_old_sessions_and_rotates_cookie(self) -> None:
        second_old_token = repo.create_session(int(self.admin["id"]), user_agent="second-old")

        async def exercise():
            async with await _client() as admin:
                await _login(admin, "totp-admin")
                old_token = admin.cookies.get(main.AUTH_COOKIE_NAME)
                started = await admin.post(
                    "/api/auth/totp/enroll/start",
                    json={"current_password": _PASSWORD},
                    headers=await _csrf_headers(admin),
                )
                secret = started.json()["secret"]
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    confirmed = await admin.post(
                        "/api/auth/totp/enroll/confirm",
                        json={"code": _code(secret)},
                        headers=await _csrf_headers(admin),
                    )
                new_token = admin.cookies.get(main.AUTH_COOKIE_NAME)
                me = await admin.get("/api/auth/me")
                return started, confirmed, old_token, new_token, me

        started, confirmed, old_token, new_token, me = _run(exercise())
        self.assertEqual(200, started.status_code)
        self.assertEqual(200, confirmed.status_code)
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(200, me.status_code)
        self.assertIsNone(repo.get_user_by_session(old_token))
        self.assertIsNone(repo.get_user_by_session(second_old_token))
        self.assertIsNotNone(repo.get_user_by_session(new_token))
        with db.get_connection() as connection:
            credential = connection.execute(
                "SELECT enabled_at, last_used_step FROM user_totp_credentials WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()
        self.assertIsNotNone(credential["enabled_at"])
        self.assertEqual(_FIXED_TIME // 30, int(credential["last_used_step"]))

    def test_disable_errors_preserve_state_and_success_revokes_sessions(self) -> None:
        async def exercise():
            async with await _client() as admin:
                await _login(admin, "totp-admin")
                started = await admin.post(
                    "/api/auth/totp/enroll/start",
                    json={"current_password": _PASSWORD},
                    headers=await _csrf_headers(admin),
                )
                secret = started.json()["secret"]
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    await admin.post(
                        "/api/auth/totp/enroll/confirm",
                        json={"code": _code(secret)},
                        headers=await _csrf_headers(admin),
                    )
                active_token = admin.cookies.get(main.AUTH_COOKIE_NAME)
                other_token = repo.create_session(int(self.admin["id"]), user_agent="disable-other")
                next_code = _code(secret, _FIXED_TIME + 30)
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME + 30):
                    wrong_password = await admin.post(
                        "/api/auth/totp/disable",
                        json={"current_password": "wrong-password", "code": next_code},
                        headers=await _csrf_headers(admin),
                    )
                    wrong_code = await admin.post(
                        "/api/auth/totp/disable",
                        json={"current_password": _PASSWORD, "code": _wrong_code(next_code)},
                        headers=await _csrf_headers(admin),
                    )
                state_before_success = (
                    repo.get_totp_status(int(self.admin["id"])),
                    repo.get_user_by_session(active_token) is not None,
                    repo.get_user_by_session(other_token) is not None,
                )
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME + 30):
                    disabled = await admin.post(
                        "/api/auth/totp/disable",
                        json={"current_password": _PASSWORD, "code": next_code},
                        headers=await _csrf_headers(admin),
                    )
                cookie_after = admin.cookies.get(main.AUTH_COOKIE_NAME)
            async with await _client() as password_client:
                password_login = await _login(password_client, "totp-admin")
            return (
                wrong_password,
                wrong_code,
                state_before_success,
                disabled,
                cookie_after,
                active_token,
                other_token,
                password_login,
            )

        (
            wrong_password,
            wrong_code,
            state_before_success,
            disabled,
            cookie_after,
            active_token,
            other_token,
            password_login,
        ) = _run(exercise())
        self.assertEqual(400, wrong_password.status_code)
        self.assertEqual(400, wrong_code.status_code)
        self.assertEqual(({"enabled": True, "enrollment_pending": False}, True, True), state_before_success)
        self.assertEqual(200, disabled.status_code)
        self.assertIsNone(cookie_after)
        self.assertEqual({"enabled": False, "enrollment_pending": False}, repo.get_totp_status(int(self.admin["id"])))
        self.assertIsNone(repo.get_user_by_session(active_token))
        self.assertIsNone(repo.get_user_by_session(other_token))
        self.assertEqual(200, password_login.status_code)
        self.assertNotIn("requires_totp", password_login.json())

    def test_viewer_and_manager_cannot_mutate_totp(self) -> None:
        manager = repo.create_user("totp-manager", _PASSWORD, "TOTP Manager", "manager")

        async def exercise(username: str):
            async with await _client() as client:
                await _login(client, username)
                headers = await _csrf_headers(client)
                start = await client.post(
                    "/api/auth/totp/enroll/start",
                    json={"current_password": _PASSWORD},
                    headers=headers,
                )
                confirm = await client.post(
                    "/api/auth/totp/enroll/confirm",
                    json={"code": "123456"},
                    headers=headers,
                )
                disable = await client.post(
                    "/api/auth/totp/disable",
                    json={"current_password": _PASSWORD, "code": "123456"},
                    headers=headers,
                )
                return start, confirm, disable

        viewer_responses = _run(exercise("totp-other"))
        manager_responses = _run(exercise("totp-manager"))
        self.assertEqual([403, 403, 403], [response.status_code for response in viewer_responses])
        self.assertEqual([403, 403, 403], [response.status_code for response in manager_responses])
        self.assertEqual({"enabled": False, "enrollment_pending": False}, repo.get_totp_status(int(self.other["id"])))
        self.assertEqual({"enabled": False, "enrollment_pending": False}, repo.get_totp_status(int(manager["id"])))

    def test_missing_and_invalid_keys_fail_closed_without_affecting_password_only_users(self) -> None:
        enabled_user = repo.create_user("totp-key-user", _PASSWORD, "Key User", "admin")
        secret = self._enable_totp(int(enabled_user["id"]))
        enabled_session = repo.create_session(int(enabled_user["id"]), user_agent="key-failure-session")
        with db.get_connection() as connection:
            ciphertext = connection.execute(
                "SELECT secret_ciphertext FROM user_totp_credentials WHERE user_id=?",
                (int(enabled_user["id"]),),
            ).fetchone()["secret_ciphertext"]

        async def exercise(key_value: str, *, test_enrollment: bool = True):
            with mock.patch.dict(os.environ, {"CRM_TOTP_ENCRYPTION_KEY": key_value}):
                async with await _client() as normal:
                    normal_login = await _login(normal, "totp-admin")
                    enroll = None
                    if test_enrollment:
                        enroll = await normal.post(
                            "/api/auth/totp/enroll/start",
                            json={"current_password": _PASSWORD},
                            headers=await _csrf_headers(normal),
                        )
                async with await _client() as enabled:
                    enabled_login = await _login(enabled, "totp-key-user")
                async with await _client() as disable_client:
                    disable_client.cookies.set(main.AUTH_COOKIE_NAME, enabled_session)
                    disable = await disable_client.post(
                        "/api/auth/totp/disable",
                        json={"current_password": _PASSWORD, "code": _code(secret, _FIXED_TIME + 30)},
                        headers=await _csrf_headers(disable_client),
                    )
                return normal_login, enroll, enabled_login, disable

        missing = _run(exercise(""))
        invalid = _run(exercise("not-a-fernet-key"))
        wrong_key = _run(exercise(Fernet.generate_key().decode("ascii"), test_enrollment=False))
        for responses in (missing, invalid):
            normal_login, enroll, enabled_login, disable = responses
            self.assertEqual(200, normal_login.status_code)
            self.assertEqual(503, enroll.status_code)
            self.assertEqual(503, enabled_login.status_code)
            self.assertEqual(503, disable.status_code)
            exposed = " ".join(response.text for response in responses)
            self.assertNotIn(ciphertext, exposed)
            self.assertNotIn(secret, exposed)
            self.assertNotIn(self.encryption_key, exposed)
        normal_login, enroll, enabled_login, disable = wrong_key
        self.assertEqual(200, normal_login.status_code)
        self.assertIsNone(enroll)
        self.assertEqual(503, enabled_login.status_code)
        self.assertEqual(503, disable.status_code)
        exposed = " ".join(response.text for response in (normal_login, enabled_login, disable))
        self.assertNotIn(ciphertext, exposed)
        self.assertNotIn(secret, exposed)
        self.assertNotIn(self.encryption_key, exposed)
        self.assertTrue(repo.get_totp_status(int(enabled_user["id"]))["enabled"])
        self.assertEqual(
            {"enabled": False, "enrollment_pending": False},
            repo.get_totp_status(int(self.admin["id"])),
        )
        self.assertIsNotNone(repo.get_user_by_session(enabled_session))
        with db.get_connection() as connection:
            challenges = connection.execute(
                "SELECT COUNT(*) AS c FROM pending_auth_challenges WHERE user_id=?",
                (int(enabled_user["id"]),),
            ).fetchone()["c"]
            sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=?",
                (int(enabled_user["id"]),),
            ).fetchone()["c"]
        self.assertEqual(0, int(challenges))
        self.assertEqual(1, int(sessions))

    def test_admin_password_reset_consumes_pending_challenge_without_touching_other_user(self) -> None:
        target = repo.create_user("totp-reset-target", _PASSWORD, "Reset Target", "admin")
        target_secret = self._enable_totp(int(target["id"]))
        unrelated = repo.create_user("totp-reset-unrelated", _PASSWORD, "Reset Unrelated", "admin")
        self._enable_totp(int(unrelated["id"]))
        actor_token = repo.create_session(int(self.admin["id"]), user_agent="reset-actor")
        new_password = "totp-reset-password-2026"

        async def exercise():
            async with await _client() as target_client:
                await _login(target_client, "totp-reset-target")
                target_pending = target_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
            async with await _client() as unrelated_client:
                await _login(unrelated_client, "totp-reset-unrelated")
                unrelated_pending = unrelated_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
            async with await _client() as actor:
                actor.cookies.set(main.AUTH_COOKIE_NAME, actor_token)
                reset = await actor.post(
                    f"/api/users/{int(target['id'])}/password",
                    json={"password": new_password},
                    headers=await _csrf_headers(actor),
                )
            async with await _client() as replay_client:
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    replay = await replay_client.post(
                        "/api/auth/totp/verify",
                        json={"code": _code(target_secret)},
                        headers={"cookie": f"{main.TOTP_PENDING_COOKIE_NAME}={target_pending}"},
                    )
                replay_session = replay_client.cookies.get(main.AUTH_COOKIE_NAME)
            return reset, replay, replay_session, target_pending, unrelated_pending

        reset, replay, replay_session, target_pending, unrelated_pending = _run(exercise())
        self.assertEqual(200, reset.status_code)
        self.assertEqual(401, replay.status_code)
        self.assertIsNone(replay_session)
        with db.get_connection() as connection:
            target_challenge = connection.execute(
                "SELECT consumed_at FROM pending_auth_challenges WHERE challenge_hash=?",
                (hashlib.sha256(target_pending.encode()).hexdigest(),),
            ).fetchone()
            unrelated_challenge = connection.execute(
                "SELECT consumed_at FROM pending_auth_challenges WHERE challenge_hash=?",
                (hashlib.sha256(unrelated_pending.encode()).hexdigest(),),
            ).fetchone()
            target_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND revoked_at IS NULL",
                (int(target["id"]),),
            ).fetchone()["c"]
        self.assertIsNotNone(target_challenge["consumed_at"])
        self.assertIsNone(unrelated_challenge["consumed_at"])
        self.assertEqual(0, int(target_sessions))

    def test_deactivate_and_reactivate_consumes_old_pending_challenge(self) -> None:
        target = repo.create_user("totp-active-target", _PASSWORD, "Active Target", "admin")
        target_secret = self._enable_totp(int(target["id"]))
        actor_token = repo.create_session(int(self.admin["id"]), user_agent="active-actor")

        async def exercise():
            async with await _client() as target_client:
                await _login(target_client, "totp-active-target")
                pending_token = target_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
            async with await _client() as actor:
                actor.cookies.set(main.AUTH_COOKIE_NAME, actor_token)
                headers = await _csrf_headers(actor)
                deactivated = await actor.patch(
                    f"/api/users/{int(target['id'])}",
                    json={"is_active": False},
                    headers=headers,
                )
                reactivated = await actor.patch(
                    f"/api/users/{int(target['id'])}",
                    json={"is_active": True},
                    headers=headers,
                )
            async with await _client() as replay_client:
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    replay = await replay_client.post(
                        "/api/auth/totp/verify",
                        json={"code": _code(target_secret)},
                        headers={"cookie": f"{main.TOTP_PENDING_COOKIE_NAME}={pending_token}"},
                    )
                replay_session = replay_client.cookies.get(main.AUTH_COOKIE_NAME)
            return deactivated, reactivated, replay, replay_session, pending_token

        deactivated, reactivated, replay, replay_session, pending_token = _run(exercise())
        self.assertEqual(200, deactivated.status_code)
        self.assertEqual(False, bool(deactivated.json()["is_active"]))
        self.assertEqual(200, reactivated.status_code)
        self.assertEqual(True, bool(reactivated.json()["is_active"]))
        self.assertEqual(401, replay.status_code)
        self.assertIsNone(replay_session)
        with db.get_connection() as connection:
            challenge = connection.execute(
                "SELECT consumed_at FROM pending_auth_challenges WHERE challenge_hash=?",
                (hashlib.sha256(pending_token.encode()).hexdigest(),),
            ).fetchone()
            active_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND revoked_at IS NULL",
                (int(target["id"]),),
            ).fetchone()["c"]
        self.assertIsNotNone(challenge["consumed_at"])
        self.assertEqual(0, int(active_sessions))

    def test_self_password_change_consumes_pending_challenge_without_extra_session(self) -> None:
        target = repo.create_user("totp-self-change", _PASSWORD, "Self Change", "admin")
        target_secret = self._enable_totp(int(target["id"]))
        current_session = repo.create_session(int(target["id"]), user_agent="self-change-current")
        new_password = "totp-self-new-password-2026"

        async def exercise():
            async with await _client() as pending_client:
                await _login(pending_client, "totp-self-change")
                pending_token = pending_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)
            async with await _client() as profile_client:
                profile_client.cookies.set(main.AUTH_COOKIE_NAME, current_session)
                changed = await profile_client.patch(
                    "/api/auth/profile",
                    json={"current_password": _PASSWORD, "new_password": new_password},
                    headers=await _csrf_headers(profile_client),
                )
                rotated_session = changed.cookies.get(main.AUTH_COOKIE_NAME)
            async with await _client() as replay_client:
                with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                    replay = await replay_client.post(
                        "/api/auth/totp/verify",
                        json={"code": _code(target_secret)},
                        headers={"cookie": f"{main.TOTP_PENDING_COOKIE_NAME}={pending_token}"},
                    )
                replay_session = replay_client.cookies.get(main.AUTH_COOKIE_NAME)
            return changed, rotated_session, replay, replay_session, pending_token

        changed, rotated_session, replay, replay_session, pending_token = _run(exercise())
        self.assertEqual(200, changed.status_code)
        self.assertTrue(rotated_session)
        self.assertNotEqual(current_session, rotated_session)
        self.assertEqual(401, replay.status_code)
        self.assertIsNone(replay_session)
        with db.get_connection() as connection:
            challenge = connection.execute(
                "SELECT consumed_at FROM pending_auth_challenges WHERE challenge_hash=?",
                (hashlib.sha256(pending_token.encode()).hexdigest(),),
            ).fetchone()
            active_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=? AND revoked_at IS NULL",
                (int(target["id"]),),
            ).fetchone()["c"]
        self.assertIsNotNone(challenge["consumed_at"])
        self.assertEqual(1, int(active_sessions))

    def test_init_db_is_idempotent_and_preserves_existing_users(self) -> None:
        before = repo.get_user_by_id(int(self.admin["id"]))
        db.init_db()
        db.init_db()
        after = repo.get_user_by_id(int(self.admin["id"]))
        self.assertEqual(before, after)
        with db.get_connection() as connection:
            credential_columns = [
                row["name"] for row in connection.execute("PRAGMA table_info(user_totp_credentials)").fetchall()
            ]
            challenge_columns = [
                row["name"] for row in connection.execute("PRAGMA table_info(pending_auth_challenges)").fetchall()
            ]
        self.assertEqual(
            ["user_id", "secret_ciphertext", "enrollment_started_at", "enabled_at", "last_used_step"],
            credential_columns,
        )
        self.assertEqual(
            ["challenge_hash", "user_id", "created_at", "expires_at", "attempts_remaining", "consumed_at"],
            challenge_columns,
        )

    def test_concurrent_verify_of_one_challenge_creates_exactly_one_session(self) -> None:
        secret = self._enable_totp(int(self.admin["id"]))
        code = _code(secret)

        async def exercise():
            async with await _client() as password_client:
                await _login(password_client, "totp-admin")
                pending_token = password_client.cookies.get(main.TOTP_PENDING_COOKIE_NAME)

            async def verify_once():
                async with await _client() as client:
                    return await client.post(
                        "/api/auth/totp/verify",
                        json={"code": code},
                        headers={"cookie": f"{main.TOTP_PENDING_COOKIE_NAME}={pending_token}"},
                    )

            with mock.patch.object(totp_auth, "_current_time", return_value=_FIXED_TIME):
                return await asyncio.gather(verify_once(), verify_once())

        responses = _run(exercise())
        self.assertEqual([401, 200], sorted((response.status_code for response in responses), reverse=True))
        with db.get_connection() as connection:
            full_sessions = connection.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()["c"]
            consumed = connection.execute(
                "SELECT consumed_at FROM pending_auth_challenges WHERE user_id=?",
                (int(self.admin["id"]),),
            ).fetchone()["consumed_at"]
        self.assertEqual(1, int(full_sessions))
        self.assertIsNotNone(consumed)


if __name__ == "__main__":
    unittest.main()
