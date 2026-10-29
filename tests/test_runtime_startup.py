from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import httpx

import test_regression_foundation as foundation
from app import runtime_config, startup_diagnostics


class RuntimeStartupTests(unittest.TestCase):
    def _execute_entrypoint(self, entrypoint: str, site: Path, *, failure: Exception | None = None):
        """Execute the real entrypoint without starting workers or opening SQLite."""
        source = Path(__file__).resolve().parents[1] / entrypoint
        namespace = {"__name__": "isolated_startup", "__file__": str(site / entrypoint)}
        previous_cwd = Path.cwd()
        observed = []

        def initialize():
            observed.append(os.environ.get("DATABASE_PATH"))
            if failure is not None:
                raise failure

        with ExitStack() as stack:
            environment = dict(foundation._SAFE_ENV)
            environment.pop("DATABASE_PATH", None)
            environment["CRM_CHAT_ATTACHMENTS_DIR"] = str(site / "attachments")
            stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
            stack.enter_context(mock.patch.object(runtime_config, "PROJECT_ROOT", site))
            dotenv = stack.enter_context(mock.patch("dotenv.load_dotenv", return_value=False))
            stack.enter_context(mock.patch.object(foundation.db, "init_db", side_effect=initialize))
            bootstrap = stack.enter_context(mock.patch.object(foundation.main, "ensure_initial_admin"))
            adapter = stack.enter_context(mock.patch("a2wsgi.ASGIMiddleware", return_value=object()))
            try:
                exec(compile(source.read_text(encoding="utf-8"), str(site / entrypoint), "exec"), namespace)
            finally:
                os.chdir(previous_cwd)
        return namespace, observed, dotenv, bootstrap, adapter

    def test_server_default_uses_project_root_database_not_data_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as directory:
            site = Path(directory)
            namespace, observed, _dotenv, bootstrap, adapter = self._execute_entrypoint("server.py", site)
            self.assertEqual([str(site / "crm.sqlite3")], observed)
            self.assertFalse((site / "data").exists())
            self.assertFalse((site / "crm.sqlite3").exists())
            bootstrap.assert_called_once_with()
            adapter.assert_called_once_with(foundation.main.app)
            self.assertIs(namespace["application"], adapter.return_value)

    def test_server_dotenv_cannot_override_hosting_panel_environment(self) -> None:
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as directory:
            site = Path(directory)
            _namespace, _observed, dotenv, _bootstrap, _adapter = self._execute_entrypoint("server.py", site)
            self.assertEqual(site / ".env", dotenv.call_args.args[0])
            self.assertIs(False, dotenv.call_args.kwargs.get("override"))

    def test_server_failure_response_has_no_traceback_password_or_internal_path(self) -> None:
        secret = "synthetic-password-not-for-response"
        private_path = "/private/synthetic/crm.sqlite3"
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as directory:
            site = Path(directory)
            for entrypoint in ("server.py", "passenger_wsgi.py"):
                with self.subTest(entrypoint=entrypoint), self.assertLogs("arti_crm.startup", level="ERROR") as logs:
                    namespace, _observed, _dotenv, bootstrap, adapter = self._execute_entrypoint(
                        entrypoint, site, failure=RuntimeError(secret + " " + private_path),
                    )
                with httpx.Client(transport=httpx.WSGITransport(app=namespace["application"]),
                                  base_url="https://testserver") as client:
                    response = client.get("/health")
                self.assertEqual(500, response.status_code)
                self.assertEqual(startup_diagnostics.STARTUP_FAILURE_MESSAGE, response.text)
                self.assertEqual("no-store", response.headers["cache-control"])
                for forbidden in (secret, private_path, "Traceback", str(site)):
                    self.assertNotIn(forbidden, response.text)
                for forbidden in (secret, private_path, "raise failure", "RuntimeError(secret"):
                    self.assertNotIn(forbidden, "\n".join(logs.output))
                self.assertIn("RuntimeError", "\n".join(logs.output))
                self.assertIn("in initialize", "\n".join(logs.output))
                self.assertFalse((site / "startup_error.log").exists())
                bootstrap.assert_not_called()
                adapter.assert_not_called()

    def test_both_wsgi_entrypoints_use_same_default_and_do_not_create_storage_on_import(self) -> None:
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as directory:
            site = Path(directory)
            for entrypoint in ("server.py", "passenger_wsgi.py"):
                with self.subTest(entrypoint=entrypoint):
                    _namespace, observed, dotenv, _bootstrap, _adapter = self._execute_entrypoint(entrypoint, site)
                    self.assertEqual([str(site / "crm.sqlite3")], observed)
                    dotenv.assert_called_once_with(site / ".env", override=False)
                    self.assertFalse((site / "crm.sqlite3").exists())
                    self.assertFalse((site / "data").exists())

    def test_logging_failure_still_produces_safe_http_response(self) -> None:
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as directory, mock.patch.object(
            startup_diagnostics._LOGGER, "error", side_effect=OSError("synthetic logging failure"),
        ), mock.patch.object(startup_diagnostics.sys.stderr, "write") as fallback:
            namespace, _observed, _dotenv, _bootstrap, _adapter = self._execute_entrypoint(
                "server.py", Path(directory), failure=RuntimeError("synthetic private value"),
            )
            with httpx.Client(transport=httpx.WSGITransport(app=namespace["application"]),
                              base_url="https://testserver") as client:
                self.assertEqual(startup_diagnostics.STARTUP_FAILURE_MESSAGE, client.get("/").text)
            fallback.assert_called_once_with("CRM startup failed; private diagnostics are unavailable.\n")


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root_patch = mock.patch.object(runtime_config, "PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_default_is_absolute_project_root_and_independent_of_cwd(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(dir=foundation._TEMP_ROOT) as elsewhere:
            try:
                os.chdir(elsewhere)
                with mock.patch("dotenv.load_dotenv", return_value=False) as dotenv:
                    selected = runtime_config.load_runtime_config()
                self.assertEqual(self.root / "crm.sqlite3", selected)
                self.assertEqual(str(selected), os.environ["DATABASE_PATH"])
                dotenv.assert_called_once_with(self.root / ".env", override=False)
                self.assertFalse(selected.exists())
                self.assertEqual([], list(self.root.iterdir()))
            finally:
                os.chdir(previous_cwd)

    def test_existing_root_database_is_selected_without_moving_or_creating_another(self) -> None:
        database = self.root / "crm.sqlite3"
        database.touch()
        self.assertEqual(database, runtime_config.load_runtime_config())
        self.assertEqual([database], list(self.root.iterdir()))

    def test_explicit_absolute_and_relative_paths_resolve_from_project_root(self) -> None:
        for configured in ("chosen.sqlite3", str(self.root / "chosen.sqlite3")):
            with self.subTest(configured=configured):
                os.environ["DATABASE_PATH"] = configured
                self.assertEqual(self.root / "chosen.sqlite3", runtime_config.load_runtime_config())
        self.assertEqual([], list(self.root.iterdir()))

    def test_pathlike_database_values_keep_the_canonical_resolution_contract(self) -> None:
        for configured in (Path("chosen.sqlite3"), self.root / "chosen.sqlite3"):
            with self.subTest(configured=configured):
                self.assertEqual(self.root / "chosen.sqlite3", runtime_config.resolve_database_path(configured))
        self.assertEqual([], list(self.root.iterdir()))

    def test_hosting_environment_wins_dotenv_and_repeated_load_is_stable(self) -> None:
        os.environ.update({"DATABASE_PATH": "panel.sqlite3", "BOOTSTRAP_ADMIN_USERNAME": "panel-user"})

        def synthetic_dotenv(path, *, override):
            self.assertEqual(self.root / ".env", path)
            self.assertIs(False, override)
            for key, value in {"DATABASE_PATH": "file.sqlite3", "BOOTSTRAP_ADMIN_USERNAME": "file-user",
                               "BOOTSTRAP_ADMIN_DISPLAY_NAME": "Synthetic owner"}.items():
                if override or key not in os.environ:
                    os.environ[key] = value
            return True

        with mock.patch("dotenv.load_dotenv", side_effect=synthetic_dotenv):
            first = runtime_config.load_runtime_config()
            self.assertEqual(first, runtime_config.load_runtime_config())
        self.assertEqual(self.root / "panel.sqlite3", first)
        self.assertEqual("panel-user", os.environ["BOOTSTRAP_ADMIN_USERNAME"])
        self.assertEqual("Synthetic owner", os.environ["BOOTSTRAP_ADMIN_DISPLAY_NAME"])

    def test_legacy_only_or_ambiguous_database_requires_explicit_choice(self) -> None:
        legacy = self.root / "data" / "crm.sqlite3"
        legacy.parent.mkdir()
        legacy.touch()
        for root_exists in (False, True):
            if root_exists:
                (self.root / "crm.sqlite3").touch()
            with self.subTest(root_exists=root_exists), self.assertRaises(runtime_config.RuntimeConfigurationError):
                runtime_config.load_runtime_config()
        os.environ["DATABASE_PATH"] = str(legacy)
        self.assertEqual(legacy, runtime_config.load_runtime_config())
        self.assertTrue((self.root / "crm.sqlite3").exists())

    def test_missing_selected_database_does_not_create_second_database(self) -> None:
        existing = self.root / "crm.sqlite3"
        existing.touch()
        os.environ["DATABASE_PATH"] = "new-folder/mistyped.sqlite3"
        with self.assertRaises(runtime_config.RuntimeConfigurationError):
            runtime_config.load_runtime_config()
        self.assertFalse((self.root / "new-folder").exists())
        self.assertEqual([existing], list(self.root.iterdir()))

    def test_invalid_database_values_fail_without_leaking_values(self) -> None:
        for value in ("", " ", ":memory:", "file:private.sqlite3"):
            with self.subTest(value=value):
                os.environ["DATABASE_PATH"] = value
                with self.assertRaises(runtime_config.RuntimeConfigurationError) as error:
                    runtime_config.load_runtime_config()
                if value.strip():
                    self.assertNotIn(value, str(error.exception))
                self.assertEqual([], list(self.root.iterdir()))
        with self.assertRaises(runtime_config.RuntimeConfigurationError) as error:
            runtime_config.resolve_database_path("synthetic\x00private")
        self.assertNotIn("synthetic", str(error.exception))
        self.assertEqual([], list(self.root.iterdir()))

    def test_directory_or_unresolvable_path_is_rejected_safely(self) -> None:
        with self.assertRaises(runtime_config.RuntimeConfigurationError):
            runtime_config.resolve_database_path(str(self.root))
        for error in (OSError("synthetic-private-path"), RuntimeError("synthetic-private-path")):
            with self.subTest(error_type=type(error)), mock.patch.object(Path, "resolve", side_effect=error):
                with self.assertRaises(runtime_config.RuntimeConfigurationError) as caught:
                    runtime_config.resolve_database_path("chosen.sqlite3")
                self.assertNotIn("synthetic-private-path", str(caught.exception))

    def test_explicit_existing_database_wins_even_with_both_historical_candidates(self) -> None:
        (self.root / "crm.sqlite3").touch()
        legacy = self.root / "data" / "crm.sqlite3"
        legacy.parent.mkdir()
        legacy.touch()
        selected = self.root / "chosen.sqlite3"
        selected.touch()
        os.environ["DATABASE_PATH"] = str(selected)
        self.assertEqual(selected, runtime_config.load_runtime_config())
        self.assertEqual(0, selected.stat().st_size)
        self.assertTrue(legacy.exists())
        self.assertTrue((self.root / "crm.sqlite3").exists())

    def test_db_module_resolves_before_schema_import_and_keeps_init_lazy(self) -> None:
        source = Path(__file__).resolve().parents[1] / "app" / "db.py"
        os.environ["DATABASE_PATH"] = "chosen.sqlite3"
        namespace = {"__name__": "isolated_db", "__file__": str(self.root / "app" / "db.py")}
        with mock.patch("dotenv.load_dotenv", return_value=False), mock.patch.object(
            foundation.db.sqlite3, "connect", side_effect=AssertionError("Import must not connect"),
        ):
            exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
        self.assertEqual(str(self.root / "chosen.sqlite3"), namespace["DATABASE_PATH"])
        self.assertFalse((self.root / "chosen.sqlite3").exists())

    def test_real_connection_creates_only_the_canonical_temporary_database(self) -> None:
        with mock.patch("dotenv.load_dotenv", return_value=False), mock.patch.object(
            foundation.db, "DATABASE_PATH", runtime_config.load_runtime_config(),
        ), mock.patch.object(
            foundation, "_DATABASE_PATH", self.root / "crm.sqlite3",
        ):
            with foundation.db.get_connection() as connection:
                connection.execute("CREATE TABLE synthetic_path_check(id INTEGER PRIMARY KEY)")
                actual = connection.execute("PRAGMA database_list").fetchone()[2]
            self.assertEqual(self.root / "crm.sqlite3", Path(actual))
        self.assertTrue((self.root / "crm.sqlite3").is_file())
        self.assertFalse((self.root / "data").exists())

    def test_asgi_import_loads_configuration_before_auth_settings_are_read(self) -> None:
        source = Path(__file__).resolve().parents[1] / "app" / "main.py"
        namespace = {"__name__": "isolated_main", "__file__": str(source)}

        def synthetic_dotenv(_path, *, override):
            self.assertIs(False, override)
            os.environ.setdefault("CRM_AUTH_DISABLED", "1")
            os.environ.setdefault("ALLOW_INSECURE_DEV_AUTH", "true")
            os.environ.setdefault("APP_ENV", "test")
            return True

        with mock.patch("dotenv.load_dotenv", side_effect=synthetic_dotenv), mock.patch.object(
            foundation.db.sqlite3, "connect", side_effect=AssertionError("Import must not connect"),
        ):
            exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
        self.assertTrue(namespace["AUTH_DISABLED"])
        self.assertFalse((self.root / "crm.sqlite3").exists())
