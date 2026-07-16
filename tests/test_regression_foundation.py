from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock


_TEMP_DIRECTORY = tempfile.TemporaryDirectory(prefix="arti_crm_tests_")
_TEMP_ROOT = Path(_TEMP_DIRECTORY.name).resolve()
_DATABASE_PATH = (_TEMP_ROOT / "crm-test.sqlite3").resolve()
_ATTACHMENTS_PATH = (_TEMP_ROOT / "attachments").resolve()

_SAFE_ENV = {
    "DATABASE_PATH": str(_DATABASE_PATH),
    "CRM_CHAT_ATTACHMENTS_DIR": str(_ATTACHMENTS_PATH),
    "CRM_AUTH_DISABLED": "0",
    "CRM_FORCE_HTTPS": "0",
    "WB_EVENTS_AUTO_IMPORT_ENABLED": "false",
    "WEB_PUSH_ENABLED": "false",
    "WB_RATE_LIMIT_STATE_FILE": str(_TEMP_ROOT / "wb-rate-limit.json"),
    "WB_EVENTS_CURSOR_STATE_FILE": str(_TEMP_ROOT / "wb-cursor.json"),
    "WB_EVENTS_AUTO_IMPORT_PLAN_FILE": str(_TEMP_ROOT / "wb-plan.json"),
    "SUPPLY_PLANNING_CACHE_FILE": str(_TEMP_ROOT / "supply-cache.json"),
    "CRM_ADMIN_USERNAME": "test-admin",
    "CRM_ADMIN_PASSWORD": "test-only-password",
    "PYTHON_DOTENV_DISABLED": "1",
    "OZON_CLIENT_ID": "",
    "OZON_API_KEY": "",
    "WB_BUYERS_CHAT_TOKEN": "",
    "WB_API_TOKEN": "",
    "WB_ANALYTICS_TOKEN": "",
    "WB_STATISTICS_TOKEN": "",
    "WB_SUPPLY_ANALYTICS_TOKEN": "",
    "YANDEX_MARKET_TOKEN": "",
    "YANDEX_MARKET_API_KEY": "",
    "YANDEX_API_KEY": "",
    "YANDEX_OAUTH_TOKEN": "",
    "YANDEX_TOKEN": "",
    "OPENAI_API_KEY": "",
    "WEB_PUSH_VAPID_PRIVATE_KEY": "",
    "WEB_PUSH_VAPID_PUBLIC_KEY": "",
    "VAPID_PRIVATE_KEY": "",
    "VAPID_PUBLIC_KEY": "",
    "CRM_BACKGROUND_TICK_TOKEN": "",
    "WEB_PUSH_BACKGROUND_TICK_TOKEN": "",
    "BACKGROUND_TICK_TOKEN": "",
    "SECRET_KEY": "",
    "DATABASE_URL": "",
}

# Preserve only non-secret Windows runtime paths needed by the already-running
# interpreter. Application configuration is otherwise replaced, not inherited.
for _name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
    if _name in os.environ:
        _SAFE_ENV[_name] = os.environ[_name]

_ENV_PATCHER = mock.patch.dict(os.environ, _SAFE_ENV, clear=True)
_ENV_PATCHER.start()

import dotenv  # noqa: E402  (must be patched before app.db is imported)

_DOTENV_PATCHER = mock.patch.object(dotenv, "load_dotenv", return_value=False)
_DOTENV_PATCHER.start()

_NETWORK_ATTEMPTS: list[str] = []
_REAL_SOCKET = socket.socket
_TEST_EVENT_LOOP = asyncio.new_event_loop()


def _deny_network(*_args, **_kwargs):
    _NETWORK_ATTEMPTS.append("blocked")
    raise AssertionError("Network access is forbidden in regression foundation tests")


class _NoNetworkSocket(_REAL_SOCKET):
    def connect(self, *_args, **_kwargs):
        return _deny_network()

    def connect_ex(self, *_args, **_kwargs):
        return _deny_network()


_NETWORK_PATCHERS = (
    mock.patch.object(socket, "socket", _NoNetworkSocket),
    mock.patch.object(socket, "create_connection", _deny_network),
)
for _patcher in _NETWORK_PATCHERS:
    _patcher.start()

import httpx  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402

from app import db  # noqa: E402

_REAL_SQLITE_CONNECT = db.sqlite3.connect


def _guarded_sqlite_connect(database, *args, **kwargs):
    try:
        candidate = Path(os.fspath(database)).resolve()
    except TypeError as exc:
        raise AssertionError("SQLite must use the temporary test database") from exc
    if candidate != _DATABASE_PATH:
        raise AssertionError("SQLite connection outside the temporary test database is forbidden")
    return _REAL_SQLITE_CONNECT(database, *args, **kwargs)


_SQLITE_PATCHER = mock.patch.object(db.sqlite3, "connect", side_effect=_guarded_sqlite_connect)
_SQLITE_PATCHER.start()

from app import main  # noqa: E402
from app import repository as repo  # noqa: E402
from app.schemas import ChatCreate  # noqa: E402

_DATABASE_CREATED_DURING_IMPORT = _DATABASE_PATH.exists()
_ATTACHMENTS_CREATED_DURING_IMPORT = _ATTACHMENTS_PATH.exists()


def _is_within_temp(path: Path) -> bool:
    try:
        path.resolve().relative_to(_TEMP_ROOT)
        return True
    except ValueError:
        return False


def _remove_test_runtime_files() -> None:
    if not _is_within_temp(_DATABASE_PATH) or not _is_within_temp(_ATTACHMENTS_PATH):
        raise AssertionError("Test runtime paths escaped the temporary directory")
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{_DATABASE_PATH}{suffix}").unlink(missing_ok=True)
    if _ATTACHMENTS_PATH.exists():
        shutil.rmtree(_ATTACHMENTS_PATH)


def tearDownModule() -> None:
    pytest_current_test = os.environ.get("PYTEST_CURRENT_TEST")
    _remove_test_runtime_files()
    _TEST_EVENT_LOOP.close()
    _SQLITE_PATCHER.stop()
    for patcher in reversed(_NETWORK_PATCHERS):
        patcher.stop()
    _DOTENV_PATCHER.stop()
    _ENV_PATCHER.stop()
    if pytest_current_test is not None:
        os.environ["PYTEST_CURRENT_TEST"] = pytest_current_test
    _TEMP_DIRECTORY.cleanup()


class RegressionFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        _NETWORK_ATTEMPTS.clear()
        _remove_test_runtime_files()

    def tearDown(self) -> None:
        try:
            self.assertEqual([], _NETWORK_ATTEMPTS, "A test attempted to access the network")
        finally:
            _remove_test_runtime_files()

    def test_import_is_isolated_and_does_not_create_database(self) -> None:
        self.assertTrue(_is_within_temp(Path(db.DATABASE_PATH)))
        self.assertTrue(_is_within_temp(main.CHAT_ATTACHMENTS_DIR))
        self.assertEqual(_DATABASE_PATH, Path(db.DATABASE_PATH).resolve())
        self.assertEqual(_ATTACHMENTS_PATH, main.CHAT_ATTACHMENTS_DIR.resolve())
        self.assertFalse(_DATABASE_CREATED_DURING_IMPORT)
        self.assertFalse(_ATTACHMENTS_CREATED_DURING_IMPORT)
        self.assertFalse(_DATABASE_PATH.exists())

    def test_health_and_static_assets_are_served_without_lifespan(self) -> None:
        async def exercise_public_routes():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                health = await client.get("/health")
                index = await client.get("/")
                manifest = await client.get("/static/manifest.webmanifest")
            return health, index, manifest

        health, index, manifest = _TEST_EVENT_LOOP.run_until_complete(exercise_public_routes())

        self.assertEqual(200, health.status_code)
        self.assertEqual({"status": "ok"}, health.json())
        self.assertEqual(200, index.status_code)
        self.assertIn("<!doctype html", index.text.lower())
        self.assertEqual(200, manifest.status_code)
        self.assertIn("name", manifest.json())
        self.assertFalse(_DATABASE_PATH.exists())

    def test_admin_guard_rejects_viewer_and_accepts_admin(self) -> None:
        def request_for(user: dict[str, object]) -> Request:
            request = Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/api/users",
                    "raw_path": b"/api/users",
                    "query_string": b"",
                    "headers": [],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "root_path": "",
                }
            )
            request.state.user = user
            return request

        with self.assertRaises(HTTPException) as error:
            main._require_admin(request_for({"id": 1, "role": "viewer"}))
        self.assertEqual(403, error.exception.status_code)

        admin = {"id": 2, "role": "admin"}
        self.assertIs(admin, main._require_admin(request_for(admin)))

    def test_repeated_external_message_id_updates_one_row(self) -> None:
        db.init_db()
        chat_id = repo.upsert_chat(
            ChatCreate(
                marketplace="ozon",
                external_chat_id="characterization-chat",
                customer_name="Synthetic Customer",
                metadata={"unread_count": 1},
            )
        )

        first_id = repo.add_message(
            chat_id,
            "inbound",
            "first synthetic text",
            author="synthetic-buyer",
            external_message_id="external-message-1",
            raw={"sender": "synthetic-buyer"},
            created_at="2026-01-01T00:00:00+00:00",
        )
        second_id = repo.add_message(
            chat_id,
            "inbound",
            "updated synthetic text",
            author="synthetic-buyer",
            external_message_id="external-message-1",
            raw={"sender": "synthetic-buyer", "updated": True},
            created_at="2026-01-01T00:00:01+00:00",
        )

        self.assertEqual(first_id, second_id)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT id, text FROM messages WHERE chat_id=? AND external_message_id=?",
                (chat_id, "external-message-1"),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE chat_id=? AND external_message_id=?",
                (chat_id, "external-message-1"),
            ).fetchone()["count"]
            preview = conn.execute(
                "SELECT last_message_preview FROM chats WHERE id=?", (chat_id,)
            ).fetchone()["last_message_preview"]

        self.assertEqual(1, int(count))
        self.assertEqual(first_id, int(row["id"]))
        self.assertEqual("updated synthetic text", row["text"])
        self.assertEqual("updated synthetic text", preview)
