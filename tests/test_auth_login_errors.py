from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock

import httpx

import test_regression_foundation as foundation  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo

_PASSWORD = "safe-login-errors-2026"


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


def _error_code(response: httpx.Response) -> str:
    return str(response.json()["detail"]["code"])


class AuthLoginErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        main.app.state.security_rate_limits = {}
        foundation._remove_test_runtime_files()
        db.init_db()
        self.active_user = repo.create_user("known-user", _PASSWORD, "Known User", "viewer")
        self.inactive_user = repo.create_user("inactive-user", _PASSWORD, "Inactive User", "viewer")
        repo.update_user(int(self.inactive_user["id"]), is_active=False)

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted real network access")
        finally:
            foundation._remove_test_runtime_files()

    def test_unknown_username_and_wrong_password_are_indistinguishable(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                unknown = await client.post(
                    "/api/auth/login",
                    json={"username": "not-a-user", "password": "wrong-password"},
                )
                wrong_password = await client.post(
                    "/api/auth/login",
                    json={"username": "known-user", "password": "wrong-password"},
                )
                return unknown, wrong_password

        unknown, wrong_password = _run(exercise())

        self.assertEqual(401, unknown.status_code)
        self.assertEqual(unknown.status_code, wrong_password.status_code)
        self.assertEqual({"detail": {"code": "invalid_credentials"}}, unknown.json())
        self.assertEqual(unknown.json(), wrong_password.json())

    def test_inactive_user_is_reported_only_after_correct_credentials(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                correct = await client.post(
                    "/api/auth/login",
                    json={"username": "inactive-user", "password": _PASSWORD},
                )
                wrong = await client.post(
                    "/api/auth/login",
                    json={"username": "inactive-user", "password": "wrong-password"},
                )
                return correct, wrong

        correct, wrong = _run(exercise())

        self.assertEqual(403, correct.status_code)
        self.assertEqual("password_account_inactive", _error_code(correct))
        self.assertEqual(401, wrong.status_code)
        self.assertEqual("invalid_credentials", _error_code(wrong))
        with db.get_connection() as connection:
            self.assertEqual(0, int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]))

    def test_persistent_lockout_returns_stable_code_and_retry_after(self) -> None:
        async def exercise() -> tuple[httpx.Response, httpx.Response]:
            async with await _client() as client:
                first = await client.post(
                    "/api/auth/login",
                    json={"username": "known-user", "password": "wrong-password"},
                )
                second = await client.post(
                    "/api/auth/login",
                    json={"username": "known-user", "password": "wrong-password"},
                )
                return first, second

        with mock.patch.dict(
            os.environ,
            {
                "CRM_LOGIN_MAX_ATTEMPTS": "2",
                "CRM_LOGIN_WINDOW_SECONDS": "600",
                "CRM_LOGIN_LOCKOUT_SECONDS": "900",
            },
            clear=False,
        ):
            first, second = _run(exercise())

        self.assertEqual(401, first.status_code)
        self.assertEqual(429, second.status_code)
        self.assertEqual("login_rate_limited", _error_code(second))
        self.assertGreater(int(second.headers["Retry-After"]), 0)

    def test_unexpected_login_failure_returns_only_generic_machine_code(self) -> None:
        internal_detail = "SELECT password_hash FROM users at C:\\private\\crm.sqlite3"

        async def exercise() -> httpx.Response:
            async with await _client() as client:
                with mock.patch.object(repo, "get_login_lockout", side_effect=RuntimeError(internal_detail)):
                    return await client.post(
                        "/api/auth/login",
                        json={"username": "known-user", "password": _PASSWORD},
                    )

        response = _run(exercise())
        response_text = response.text.lower()

        self.assertEqual(500, response.status_code)
        self.assertEqual({"detail": {"code": "login_failed"}}, response.json())
        self.assertNotIn(internal_detail.lower(), response_text)
        self.assertNotIn("traceback", response_text)
        self.assertNotIn("sqlite3", response_text)

    def test_frontend_maps_auth_codes_to_safe_messages_with_generic_fallbacks(self) -> None:
        source = Path(main.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        for text in (
            "Неверный логин или пароль",
            "Учетная запись отключена. Обратитесь к администратору",
            "Слишком много попыток входа. Повторите позже",
            "Не удалось выполнить вход. Повторите попытку позже",
            "Вход через Яндекс отменён",
            "Этот аккаунт Яндекса не имеет доступа к CRM. Выберите другой аккаунт или обратитесь к администратору",
            "Доступ к CRM для этого аккаунта отключён",
            "Сессия входа истекла. Повторите вход через Яндекс",
            "Слишком много попыток входа через Яндекс. Повторите позже",
            "Яндекс временно недоступен. Повторите попытку позже или войдите по логину и паролю",
            "Не удалось войти через Яндекс",
        ):
            self.assertIn(text, source)
        self.assertIn("|| PASSWORD_LOGIN_ERROR_MESSAGES.login_failed;", source)
        self.assertIn("|| YANDEX_OAUTH_ERROR_MESSAGES.failed;", source)
        auth_messages = source[
            source.index("const PASSWORD_LOGIN_ERROR_MESSAGES"):
            source.index("async function refreshYandexOAuthStatus")
        ]
        login_handlers = source[
            source.index("function setupAuthUi()"):
            source.index("const doLogout = async")
        ]
        self.assertNotIn("alert(", auth_messages)
        self.assertNotIn("alert(", login_handlers)


if __name__ == "__main__":
    unittest.main()
