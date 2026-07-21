from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402

from app import auth_dependencies  # noqa: E402


main = foundation.main


def _request_for(user: Any = None, *, include_user: bool = True) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/test",
            "raw_path": b"/api/test",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
        }
    )
    if include_user:
        request.state.user = user
    return request


def _legacy_current_user(request: Request, *, auth_disabled: bool) -> dict[str, Any]:
    if auth_disabled:
        return {"id": 0, "username": "local", "display_name": "Local", "role": "admin", "is_active": True}
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def _legacy_require_admin(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Нужны права администратора")
    return user


class AuthDependenciesParityTests(unittest.TestCase):
    def assert_http_error(self, callable_obj, *, status_code: int, detail: str) -> None:
        with self.assertRaises(HTTPException) as error:
            callable_obj()
        self.assertEqual(status_code, error.exception.status_code)
        self.assertEqual(detail, error.exception.detail)

    def test_missing_user_preserves_401_contract(self) -> None:
        request = _request_for(include_user=False)
        self.assert_http_error(
            lambda: _legacy_current_user(request, auth_disabled=False),
            status_code=401,
            detail="Требуется авторизация",
        )
        self.assert_http_error(
            lambda: auth_dependencies.current_user(request),
            status_code=401,
            detail="Требуется авторизация",
        )
        with mock.patch.object(main, "AUTH_DISABLED", False):
            self.assert_http_error(
                lambda: main._current_user(request),
                status_code=401,
                detail="Требуется авторизация",
            )

    def test_viewer_and_admin_current_user_parity(self) -> None:
        for user in (
            {"id": 1, "role": "viewer", "is_active": True},
            {"id": 2, "role": "admin", "is_active": True},
        ):
            request = _request_for(user)
            with self.subTest(role=user["role"]), mock.patch.object(main, "AUTH_DISABLED", False):
                self.assertIs(user, _legacy_current_user(request, auth_disabled=False))
                self.assertIs(user, auth_dependencies.current_user(request))
                self.assertIs(user, main._current_user(request))

    def test_admin_guard_preserves_403_and_admin_identity(self) -> None:
        for user in (None, {"id": 1, "role": "viewer", "is_active": True}):
            request = _request_for(user)
            with self.subTest(user=user):
                self.assert_http_error(
                    lambda: _legacy_require_admin(request),
                    status_code=403,
                    detail="Нужны права администратора",
                )
                self.assert_http_error(
                    lambda: auth_dependencies.require_admin(request),
                    status_code=403,
                    detail="Нужны права администратора",
                )
                self.assert_http_error(
                    lambda: main._require_admin(request),
                    status_code=403,
                    detail="Нужны права администратора",
                )

        admin = {"id": 2, "role": "admin", "is_active": True}
        request = _request_for(admin)
        self.assertIs(admin, _legacy_require_admin(request))
        self.assertIs(admin, auth_dependencies.require_admin(request))
        self.assertIs(admin, main._require_admin(request))

    def test_existing_disabled_user_behavior_is_unchanged(self) -> None:
        disabled_viewer = {"id": 3, "role": "viewer", "is_active": False}
        viewer_request = _request_for(disabled_viewer)
        with mock.patch.object(main, "AUTH_DISABLED", False):
            self.assertIs(disabled_viewer, auth_dependencies.current_user(viewer_request))
            self.assertIs(disabled_viewer, main._current_user(viewer_request))
        self.assert_http_error(
            lambda: auth_dependencies.require_admin(viewer_request),
            status_code=403,
            detail="Нужны права администратора",
        )

        disabled_admin = {"id": 4, "role": "admin", "is_active": False}
        admin_request = _request_for(disabled_admin)
        self.assertIs(disabled_admin, auth_dependencies.require_admin(admin_request))
        self.assertIs(disabled_admin, main._require_admin(admin_request))

    def test_auth_disabled_local_admin_contract_is_unchanged(self) -> None:
        expected = {"id": 0, "username": "local", "display_name": "Local", "role": "admin", "is_active": True}
        request = _request_for(include_user=False)
        self.assertEqual(expected, _legacy_current_user(request, auth_disabled=True))
        self.assertEqual(expected, auth_dependencies.current_user(request, auth_disabled=True))
        with mock.patch.object(main, "AUTH_DISABLED", True):
            self.assertEqual(expected, main._current_user(request))

    def test_shared_module_has_no_db_network_or_environment_imports(self) -> None:
        source_path = Path(auth_dependencies.__file__).resolve()
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertEqual({"__future__", "typing", "fastapi"}, imported_roots)
        self.assertNotIn("os", source_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
