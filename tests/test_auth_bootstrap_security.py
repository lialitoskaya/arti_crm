from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import Request
from starlette.responses import JSONResponse
from starlette.datastructures import State

import test_regression_foundation as foundation  # noqa: E402
from app import auth_bootstrap  # noqa: E402


main = foundation.main


@contextmanager
def _hosting_application(entrypoint: str, environment: dict[str, str]):
    """Execute real entrypoint code with isolated storage, dotenv and networking."""
    from a2wsgi import ASGIMiddleware

    project_root = Path(__file__).resolve().parents[1]
    previous_cwd = Path.cwd()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as site_directory:
            site = Path(site_directory)
            safe_environment = {
                **foundation._SAFE_ENV,
                "BOOTSTRAP_ADMIN_USERNAME": "",
                "BOOTSTRAP_ADMIN_PASSWORD": "",
                "BOOTSTRAP_ADMIN_DISPLAY_NAME": "",
                "CRM_CHAT_ATTACHMENTS_DIR": str(site / "attachments"),
                **environment,
            }
            with mock.patch.dict(os.environ, safe_environment, clear=True), mock.patch(
                "a2wsgi.ASGIMiddleware",
                side_effect=lambda app: ASGIMiddleware(app, loop=loop, wait_time=5),
            ), mock.patch.object(main.app, "state", State()), mock.patch.object(
                main, "on_startup", side_effect=AssertionError("Full ASGI startup is forbidden")
            ) as startup, mock.patch.object(
                main.repo, "delete_mock_chats", side_effect=AssertionError("Startup cleanup is forbidden")
            ) as cleanup:
                namespace = {"__name__": "test_hosting_entry", "__file__": str(site / entrypoint)}
                try:
                    exec(compile((project_root / entrypoint).read_text(encoding="utf-8"),
                                 str(site / entrypoint), "exec"), namespace)
                    yield namespace["application"]
                finally:
                    startup.assert_not_called()
                    cleanup.assert_not_called()
                    assert not hasattr(main.app.state, "background_sync_task")
                    os.chdir(previous_cwd)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.close()


class HostedBootstrapSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS)
        finally:
            foundation._remove_test_runtime_files()

    def test_wsgi_empty_database_bootstraps_admin_and_password_login_works(self) -> None:
        environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "hosted-first-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "Synthetic-hosting-password-2026!",
        }
        for entrypoint in ("server.py", "passenger_wsgi.py"):
            with self.subTest(entrypoint=entrypoint):
                foundation._remove_test_runtime_files()
                with _hosting_application(entrypoint, environment) as application:
                    with httpx.Client(transport=httpx.WSGITransport(app=application),
                                      base_url="https://testserver") as client:
                        response = client.post("/api/auth/login", json={
                            "username": environment["BOOTSTRAP_ADMIN_USERNAME"],
                            "password": environment["BOOTSTRAP_ADMIN_PASSWORD"],
                        })
                        self.assertEqual(200, response.status_code)
                        self.assertEqual("admin", response.json()["user"]["role"])
                        auth_cookie = next(cookie for cookie in client.cookies.jar if cookie.name == main.AUTH_COOKIE_NAME)
                        self.assertTrue(auth_cookie.secure)
                        self.assertEqual(200, client.get("/api/auth/me").status_code)
                users = foundation.repo.list_users()
                self.assertEqual(1, len(users))
                self.assertEqual("hosted-first-admin", users[0]["username"])
                with foundation.db.get_connection() as connection:
                    before = [dict(row) for row in connection.execute("SELECT * FROM users")]
                for repeated_environment in ({}, {
                    "BOOTSTRAP_ADMIN_USERNAME": "replacement-admin",
                    "BOOTSTRAP_ADMIN_PASSWORD": "Replacement-password-2026!",
                }):
                    with _hosting_application(entrypoint, repeated_environment):
                        with foundation.db.get_connection() as connection:
                            self.assertEqual(before, [dict(row) for row in connection.execute("SELECT * FROM users")])
                        self.assertIsNotNone(foundation.repo.authenticate_user(
                            environment["BOOTSTRAP_ADMIN_USERNAME"], environment["BOOTSTRAP_ADMIN_PASSWORD"],
                        ))
                        self.assertIsNone(foundation.repo.authenticate_user(
                            environment["BOOTSTRAP_ADMIN_USERNAME"], "Replacement-password-2026!",
                        ))

    def test_wsgi_empty_database_refuses_missing_partial_and_invalid_credentials(self) -> None:
        configurations = (
            {},
            {"BOOTSTRAP_ADMIN_USERNAME": "hosted-first-admin"},
            {"BOOTSTRAP_ADMIN_PASSWORD": "Synthetic-hosting-password-2026!"},
            {"BOOTSTRAP_ADMIN_USERNAME": "hosted-first-admin", "BOOTSTRAP_ADMIN_PASSWORD": "short"},
            {"BOOTSTRAP_ADMIN_USERNAME": "invalid user", "BOOTSTRAP_ADMIN_PASSWORD": "Synthetic-hosting-password-2026!"},
        )
        for entrypoint in ("server.py", "passenger_wsgi.py"):
            for environment in configurations:
                with self.subTest(entrypoint=entrypoint, keys=tuple(environment)):
                    foundation._remove_test_runtime_files()
                    if entrypoint == "server.py":
                        with _hosting_application(entrypoint, environment) as application:
                            with httpx.Client(transport=httpx.WSGITransport(app=application),
                                              base_url="http://testserver") as client:
                                response = client.get("/health")
                            self.assertEqual(500, response.status_code)
                            error_message = response.text
                    else:
                        with self.assertRaises(auth_bootstrap.AuthBootstrapError) as error:
                            with _hosting_application(entrypoint, environment):
                                self.fail("Invalid bootstrap must stop Passenger startup")
                        error_message = str(error.exception)
                    self.assertIn("BOOTSTRAP_ADMIN_USERNAME", error_message)
                    self.assertIn("BOOTSTRAP_ADMIN_PASSWORD", error_message)
                    for value in environment.values():
                        self.assertNotIn(value, error_message)
                    self.assertEqual([], foundation.repo.list_users())

    def test_wsgi_existing_users_are_unchanged_without_or_with_changed_settings(self) -> None:
        foundation.db.init_db()
        user = foundation.repo.create_user("existing-viewer", "Existing-password-2026!", "Existing", "viewer")
        foundation.repo.update_user(int(user["id"]), is_active=False)
        with foundation.db.get_connection() as connection:
            before = [dict(row) for row in connection.execute("SELECT * FROM users")]
        for entrypoint in ("server.py", "passenger_wsgi.py"):
            for environment in ({}, {
                "BOOTSTRAP_ADMIN_USERNAME": "different-admin",
                "BOOTSTRAP_ADMIN_PASSWORD": "Different-password-2026!",
            }):
                with self.subTest(entrypoint=entrypoint, keys=tuple(environment)):
                    with _hosting_application(entrypoint, environment) as application:
                        with httpx.Client(transport=httpx.WSGITransport(app=application),
                                          base_url="http://testserver") as client:
                            self.assertEqual(200, client.get("/health").status_code)
                    with foundation.db.get_connection() as connection:
                        self.assertEqual(before, [dict(row) for row in connection.execute("SELECT * FROM users")])

    def test_concurrent_bootstrap_serializes_empty_check_on_separate_connections(self) -> None:
        foundation.db.init_db()
        original_connection = foundation.repo.get_connection
        start = threading.Barrier(2)
        unlocked_reads = threading.Barrier(2)
        connections = []

        class ObservedConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, statement, *args):
                cursor = self.connection.execute(statement, *args)
                # Force the old unlocked COUNT -> INSERT race deterministically.
                # With BEGIN IMMEDIATE the second worker waits before its check.
                if statement.startswith("SELECT COUNT(*) AS c FROM users") and not self.connection.in_transaction:
                    unlocked_reads.wait(timeout=5)
                return cursor

        @contextmanager
        def observed_connection():
            with original_connection() as connection:
                connections.append(connection)
                yield ObservedConnection(connection)

        def bootstrap(username):
            start.wait(timeout=5)
            return foundation.repo.ensure_initial_admin(username, "Concurrent-password-2026!")

        with mock.patch.object(foundation.repo, "get_connection", observed_connection):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(bootstrap, ("first-candidate", "second-candidate")))
        self.assertEqual(2, len(connections))
        self.assertIsNot(connections[0], connections[1])
        self.assertEqual(1, sum(result is not None for result in results))
        self.assertEqual(1, len(foundation.repo.list_users()))


def _request(
    *,
    client_host: str = "127.0.0.1",
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/chats",
            "raw_path": b"/api/chats",
            "query_string": b"",
            "headers": list(headers),
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (client_host, 12345),
        }
    )


def _run_without_event_loop(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError("coroutine unexpectedly suspended")


async def _run_auth_guard(request: Request):
    calls: list[str] = []

    async def call_next(current_request: Request):
        calls.append(current_request.url.path)
        return JSONResponse({"ok": True})

    response = await main.require_auth_for_api(request, call_next)
    return response, calls


class BootstrapAdminSecurityTests(unittest.TestCase):
    def test_existing_users_do_not_require_bootstrap_settings_or_create_admin(self) -> None:
        with mock.patch.object(main.repo, "users_exist", return_value=True), mock.patch.object(
            main.repo, "ensure_initial_admin"
        ) as ensure_admin, mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(main.ensure_initial_admin())
        ensure_admin.assert_not_called()

    def test_empty_users_without_or_with_partial_credentials_fail_closed(self) -> None:
        configurations = (
            {},
            {"BOOTSTRAP_ADMIN_USERNAME": "unique-admin"},
            {"BOOTSTRAP_ADMIN_PASSWORD": "StrongPassword-2026"},
        )
        for environment in configurations:
            with self.subTest(environment=tuple(environment)), mock.patch.object(
                main.repo, "users_exist", return_value=False
            ), mock.patch.object(main.repo, "ensure_initial_admin") as ensure_admin, mock.patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(auth_bootstrap.AuthBootstrapError) as error:
                    main.ensure_initial_admin()
                password = environment.get("BOOTSTRAP_ADMIN_PASSWORD")
                if password:
                    self.assertNotIn(password, str(error.exception))
                ensure_admin.assert_not_called()

    def test_explicit_valid_credentials_create_exactly_one_admin(self) -> None:
        environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "unique-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "StrongPassword-2026!",
            "BOOTSTRAP_ADMIN_DISPLAY_NAME": "Initial Administrator",
        }
        with mock.patch.object(main.repo, "users_exist", return_value=False), mock.patch.object(
            main.repo,
            "ensure_initial_admin",
            return_value={"id": 1, "username": "unique-admin"},
        ) as ensure_admin, mock.patch("builtins.print") as print_mock, mock.patch.dict(
            os.environ, environment, clear=True
        ):
            created = main.ensure_initial_admin()
        self.assertEqual(1, created["id"])
        ensure_admin.assert_called_once_with("unique-admin", "StrongPassword-2026!", "Initial Administrator")
        output = " ".join(str(value) for call in print_mock.call_args_list for value in call.args)
        self.assertNotIn(environment["BOOTSTRAP_ADMIN_PASSWORD"], output)
        self.assertNotIn(environment["BOOTSTRAP_ADMIN_USERNAME"], output)

    def test_repeated_startup_does_not_duplicate_or_replace_password(self) -> None:
        environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "unique-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "StrongPassword-2026!",
        }
        existing = False
        stored_password: str | None = None

        def users_exist() -> bool:
            return existing

        def create_admin(username: str, password: str, display_name: str):
            nonlocal existing, stored_password
            existing = True
            stored_password = password
            return {"id": 1, "username": username, "display_name": display_name}

        with mock.patch.object(main.repo, "users_exist", side_effect=users_exist), mock.patch.object(
            main.repo, "ensure_initial_admin", side_effect=create_admin
        ) as ensure_admin, mock.patch("builtins.print"), mock.patch.dict(os.environ, environment, clear=True):
            main.ensure_initial_admin()
            main.ensure_initial_admin()
        ensure_admin.assert_called_once()
        self.assertEqual("StrongPassword-2026!", stored_password)

    def test_repeated_bootstrap_against_temporary_sqlite_preserves_first_password(self) -> None:
        foundation._remove_test_runtime_files()
        foundation.db.init_db()
        initial_environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "unique-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "StrongPassword-2026!",
        }
        changed_environment = {
            "BOOTSTRAP_ADMIN_USERNAME": "different-admin",
            "BOOTSTRAP_ADMIN_PASSWORD": "DifferentPassword-2026!",
        }
        try:
            with mock.patch("builtins.print"), mock.patch.dict(os.environ, initial_environment, clear=True):
                self.assertIsNotNone(main.ensure_initial_admin())
            with mock.patch("builtins.print"), mock.patch.dict(os.environ, changed_environment, clear=True):
                self.assertIsNone(main.ensure_initial_admin())
            users = foundation.repo.list_users()
            self.assertEqual(1, len(users))
            self.assertEqual("unique-admin", users[0]["username"])
            self.assertIsNotNone(foundation.repo.authenticate_user("unique-admin", "StrongPassword-2026!"))
            self.assertIsNone(foundation.repo.authenticate_user("unique-admin", "DifferentPassword-2026!"))
        finally:
            foundation._remove_test_runtime_files()

    def test_previous_defaults_weak_passwords_and_invalid_usernames_are_rejected(self) -> None:
        invalid_pairs = (
            ("admin", "StrongPassword-2026!"),
            ("unique-admin", "admin123"),
            ("unique-admin", "change_me_please"),
            ("unique-admin", "short"),
            ("x", "StrongPassword-2026!"),
            ("x" * 121, "StrongPassword-2026!"),
            ("invalid user", "StrongPassword-2026!"),
            ("invalid\nuser", "StrongPassword-2026!"),
        )
        for username, password in invalid_pairs:
            with self.subTest(username_length=len(username), password_length=len(password)):
                with self.assertRaises(auth_bootstrap.AuthBootstrapError) as error:
                    auth_bootstrap.resolve_bootstrap_admin_credentials(username, password)
                self.assertNotIn(username, str(error.exception))
                self.assertNotIn(password, str(error.exception))

    def test_auth_disabled_requires_explicit_safe_environment_and_flag(self) -> None:
        for app_env in (None, "", "production", "staging", "local", "unknown"):
            with self.subTest(app_env=app_env), self.assertRaises(auth_bootstrap.AuthBootstrapError):
                auth_bootstrap.validate_auth_disabled_config(
                    app_env=app_env,
                    auth_disabled="true",
                    allow_insecure_dev_auth="true",
                )
        for app_env in ("development", "test"):
            with self.subTest(app_env=app_env):
                self.assertTrue(
                    auth_bootstrap.validate_auth_disabled_config(
                        app_env=app_env,
                        auth_disabled="true",
                        allow_insecure_dev_auth="true",
                    )
                )
                with self.assertRaises(auth_bootstrap.AuthBootstrapError):
                    auth_bootstrap.validate_auth_disabled_config(
                        app_env=app_env,
                        auth_disabled="true",
                        allow_insecure_dev_auth="false",
                    )

    def test_auth_disabled_false_or_absent_preserves_normal_configuration(self) -> None:
        for value in (None, "", "0", "false", "off"):
            self.assertFalse(
                auth_bootstrap.validate_auth_disabled_config(
                    app_env="production",
                    auth_disabled=value,
                    allow_insecure_dev_auth=None,
                )
            )

    def test_invalid_auth_disabled_configuration_stops_startup_before_database_init(self) -> None:
        error = auth_bootstrap.AuthBootstrapError("Insecure authentication bypass is not allowed")
        with mock.patch.object(main, "_AUTH_DISABLED_CONFIGURATION_ERROR", error), mock.patch.object(
            main, "init_db"
        ) as init_db:
            with self.assertRaises(auth_bootstrap.AuthBootstrapError):
                _run_without_event_loop(main.on_startup())
        init_db.assert_not_called()

    def test_development_bypass_accepts_only_direct_loopback_requests(self) -> None:
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host=host), mock.patch.object(main, "AUTH_DISABLED", True):
                response, calls = _run_without_event_loop(_run_auth_guard(_request(client_host=host)))
                self.assertEqual(200, response.status_code)
                self.assertEqual(["/api/chats"], calls)
        for host in ("127.0.0.2", "10.0.0.4", "192.168.1.8", "example.test"):
            with self.subTest(host=host), mock.patch.object(main, "AUTH_DISABLED", True):
                response, calls = _run_without_event_loop(_run_auth_guard(_request(client_host=host)))
                self.assertEqual(403, response.status_code)
                self.assertEqual([], calls)

    def test_development_bypass_rejects_every_forwarded_header(self) -> None:
        for name in (b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto"):
            with self.subTest(name=name), mock.patch.object(main, "AUTH_DISABLED", True):
                response, calls = _run_without_event_loop(
                    _run_auth_guard(_request(headers=((name, b"127.0.0.1"),)))
                )
                self.assertEqual(403, response.status_code)
                self.assertEqual(
                    {"detail": "Insecure development authentication is limited to direct loopback requests"},
                    json.loads(response.body.decode("utf-8")),
                )
                self.assertEqual([], calls)

    def test_normal_authentication_contract_remains_active(self) -> None:
        request = _request()
        with mock.patch.object(main, "AUTH_DISABLED", False), mock.patch.object(
            main.repo, "get_user_by_session", return_value=None
        ):
            response, calls = _run_without_event_loop(_run_auth_guard(request))
        self.assertEqual(401, response.status_code)
        self.assertEqual({"detail": "Требуется авторизация"}, json.loads(response.body.decode("utf-8")))
        self.assertEqual([], calls)

    def test_example_and_pure_module_contain_no_fallback_credentials_or_side_effect_imports(self) -> None:
        example = (Path(main.BASE_DIR).parent / ".env.example").read_text(encoding="utf-8")
        self.assertIn("BOOTSTRAP_ADMIN_USERNAME=", example)
        self.assertIn("BOOTSTRAP_ADMIN_PASSWORD=", example)
        self.assertNotIn("CRM_ADMIN_USERNAME", example)
        self.assertNotIn("CRM_ADMIN_PASSWORD", example)
        self.assertNotIn("change_me_please", example)

        module_path = Path(auth_bootstrap.__file__).resolve()
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_roots = {
            node.names[0].name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual({"__future__", "dataclasses", "typing"}, imported_roots)


if __name__ == "__main__":
    unittest.main()
