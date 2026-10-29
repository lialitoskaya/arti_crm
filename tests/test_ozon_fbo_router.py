"""Поставки FBO: HTTP-контракт /api/ozon-fbo/* — инвентарь маршрутов, admin-only, настройки, создание прогона,
ответ/остановка/снятие, файлы результата. Через in-memory ASGI-клиент с реальной сессией администратора."""
from __future__ import annotations

import asyncio
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import httpx

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from test_ozon_fbo_core import Sheet  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from app import auth_dependencies  # noqa: E402
from app import db  # noqa: E402
from app import ozon_fbo_repository as fbo_repo  # noqa: E402
from app import repository as repo  # noqa: E402
from app.ozon_fbo_router import create_ozon_fbo_router  # noqa: E402
from app.services.ozon_fbo import runner as fbo_runner  # noqa: E402

main = foundation.main

EXPECTED_ROUTES = {
    ("/api/ozon-fbo/status", "GET"),
    ("/api/ozon-fbo/settings", "GET"),
    ("/api/ozon-fbo/settings", "PUT"),
    ("/api/ozon-fbo/check-connection", "POST"),
    ("/api/ozon-fbo/workbook-sheets", "POST"),
    ("/api/ozon-fbo/awaiting-cargo-orders", "GET"),
    ("/api/ozon-fbo/runs", "POST"),
    ("/api/ozon-fbo/runs", "GET"),
    ("/api/ozon-fbo/runs/{run_id}", "GET"),
    ("/api/ozon-fbo/runs/{run_id}/answer", "POST"),
    ("/api/ozon-fbo/runs/{run_id}/stop", "POST"),
    ("/api/ozon-fbo/runs/{run_id}/release", "POST"),
    ("/api/ozon-fbo/runs/{run_id}/result.xlsx", "GET"),
    ("/api/ozon-fbo/runs/{run_id}/report.txt", "GET"),
    ("/api/ozon-fbo/runs/{run_id}/api-log.zip", "GET"),
}


def _routes(router) -> set[tuple[str, str]]:
    return {(route.path, method) for route in router.routes if isinstance(route, APIRoute) for method in route.methods}


class _Client:
    """Админ или менеджер с реальной сессией, cookie и CSRF-заголовком."""

    def __init__(self, role: str = "admin") -> None:
        self.role = role

    async def __aenter__(self) -> "_Client":
        user = repo.create_user(f"fbo-{self.role}", f"fbo-{self.role}-password-2026", f"FBO {self.role}", self.role)
        self.user = user
        token = repo.create_session(int(user["id"]), user_agent="ozon-fbo-router-test")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://testserver")
        self.client.cookies.set(main.AUTH_COOKIE_NAME, token)
        response = await self.client.get("/api/security/csrf")
        assert response.status_code == 200, response.text
        self.headers = {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()

    async def get(self, path: str, **kw):
        return await self.client.get(path, **kw)

    async def post(self, path: str, **kw):
        return await self.client.post(path, headers=self.headers, **kw)

    async def put(self, path: str, **kw):
        return await self.client.put(path, headers=self.headers, **kw)


def _run(coro):
    return asyncio.run(coro)


class OzonFboRouterContractTests(unittest.TestCase):
    def test_route_inventory_and_registration(self) -> None:
        router = create_ozon_fbo_router(fbo_repo, lambda request: {"id": 1, "role": "admin"})
        self.assertEqual(_routes(router), EXPECTED_ROUTES)
        app_routes = [(route.path, method) for route in main.app.routes if isinstance(route, APIRoute) for method in route.methods]
        for item in EXPECTED_ROUTES:
            self.assertEqual(app_routes.count(item), 1, item)

    def test_every_route_is_admin_only(self) -> None:
        for path, method in EXPECTED_ROUTES:
            concrete = path.replace("{run_id}", "1")
            self.assertEqual("admin_only", auth_dependencies.route_access_class(method, concrete), (method, concrete))


class OzonFboRouterHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        main.app.state.security_rate_limits = {}
        db.init_db()
        self.started: list[int] = []
        patcher = mock.patch.object(fbo_runner, "start_run", side_effect=lambda run_id: self.started.append(run_id))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")

    def test_viewer_and_anonymous_are_rejected(self) -> None:
        async def scenario():
            anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://testserver")
            try:
                self.assertEqual((await anonymous.get("/api/ozon-fbo/status")).status_code, 401)
            finally:
                await anonymous.aclose()
            async with _Client("manager") as manager:
                self.assertEqual((await manager.get("/api/ozon-fbo/status")).status_code, 403)
                self.assertEqual((await manager.get("/api/ozon-fbo/runs")).status_code, 403)
                self.assertEqual((await manager.put("/api/ozon-fbo/settings", json={"min_qty": 5})).status_code, 403)
        _run(scenario())

    def test_status_and_settings(self) -> None:
        async def scenario():
            async with _Client() as admin:
                status = await admin.get("/api/ozon-fbo/status")
                self.assertEqual(status.status_code, 200, status.text)
                body = status.json()
                self.assertFalse(body["keys_configured"], "в тестовом окружении ключи Ozon пустые")
                self.assertIsNone(body["active_run"])
                self.assertEqual(body["settings"]["default_cargo_capacity"], 50)
                self.assertIn("seller.ozon.ru", body["lk_url"])

                got = await admin.get("/api/ozon-fbo/settings")
                self.assertEqual(got.json()["labels"]["min_qty"], "Минимум штук в кластер")

                bad = await admin.put("/api/ozon-fbo/settings", json={"min_qty": 0, "default_cargo_capacity": "abc"})
                self.assertEqual(bad.status_code, 400, bad.text)
                self.assertEqual(len(bad.json()["detail"]["errors"]), 2)
                self.assertEqual(fbo_repo.get_settings_raw(), {}, "ошибочный запрос ничего не сохраняет")

                ok = await admin.put("/api/ozon-fbo/settings", json={"default_cargo_capacity": 40, "cargo_capacity": "AT-X = 7",
                                                                      "all_clusters": "Тверь\nКазань"})
                self.assertEqual(ok.status_code, 200, ok.text)
                saved = ok.json()["settings"]
                self.assertEqual((saved["default_cargo_capacity"], saved["cargo_capacity"], saved["all_clusters"]),
                                 (40, {"AT-X": 7}, ["Тверь", "Казань"]))
                again = await admin.get("/api/ozon-fbo/status")
                self.assertEqual(again.json()["settings"]["default_cargo_capacity"], 40)
        _run(scenario())

    def test_create_run_validation(self) -> None:
        async def scenario():
            async with _Client() as admin:
                no_keys = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "manual", "manual_text": "A; Тверь; 10"})
                self.assertEqual(no_keys.status_code, 400)
                self.assertIn("Ключи Ozon", no_keys.json()["detail"])
                with mock.patch.dict("os.environ", {"OZON_CLIENT_ID": "id", "OZON_API_KEY": "key"}):
                    unknown = await admin.post("/api/ozon-fbo/runs", data={"mode": "weird", "source_kind": "manual", "manual_text": "A; Тверь; 10"})
                    self.assertEqual(unknown.status_code, 400)
                    bad_rows = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "manual", "manual_text": "A; Тверь"})
                    self.assertEqual(bad_rows.status_code, 400)
                    self.assertIn("Ручной ввод", bad_rows.json()["detail"])
                    empty = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "manual", "manual_text": "A; Тверь; 0"})
                    self.assertEqual(empty.status_code, 400)
                    self.assertIn("видимой строки", empty.json()["detail"])
                    filtered = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "manual",
                                                                             "manual_text": "A; Тверь; 10", "articles": "B"})
                    self.assertEqual(filtered.status_code, 400)
                    no_file = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "file", "sheet_name": "x"})
                    self.assertEqual(no_file.status_code, 400)
                    cargo_empty = await admin.post("/api/ozon-fbo/runs", data={"mode": "cargo", "order_keys": ""})
                    self.assertEqual(cargo_empty.status_code, 400)
                self.assertEqual(self.started, [])
                self.assertEqual(fbo_repo.list_runs(), [])
        _run(scenario())

    def test_create_manual_run_then_conflict_answer_stop(self) -> None:
        async def scenario():
            async with _Client() as admin:
                with mock.patch.dict("os.environ", {"OZON_CLIENT_ID": "id", "OZON_API_KEY": "key"}):
                    created = await admin.post("/api/ozon-fbo/runs", data={
                        "mode": "live_nocargo", "source_kind": "manual", "manual_text": "AT-P-P3; Тверь; 40",
                        "list_name": "список 1", "articles": "AT-P-P3, AT-X", "force": "true", "confirm_each": "false",
                        "auto_confirm": "0"})
                    self.assertEqual(created.status_code, 201, created.text)
                    run = created.json()
                    self.assertEqual((run["mode"], run["source_kind"], run["sheet_name"], run["articles_filter"], run["status"]),
                                     ("live_nocargo", "manual", "список 1", "AT-P-P3,AT-X", "queued"))
                    self.assertEqual(run["options"], {"force": True, "confirm_each": False, "auto_confirm": False,
                                                      "manual_text": "AT-P-P3; Тверь; 40"})
                    self.assertEqual(run["created_by_label"], "FBO admin")
                    self.assertEqual(self.started, [run["id"]])

                    conflict = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "manual", "manual_text": "A; Тверь; 10"})
                    self.assertEqual(conflict.status_code, 409)

                status = await admin.get("/api/ozon-fbo/status")
                self.assertEqual(status.json()["active_run"]["id"], run["id"])

                fbo_repo.mark_run_started(run["id"], "")
                fbo_repo.append_run_log(run["id"], "первая строка")
                fbo_repo.append_run_log(run["id"], "вторая строка")
                fbo_repo.add_run_rows(run["id"], [{"article": "AT-P-P3", "cluster": "Тверь", "plan": 40, "fact": 40, "note": "", "tone": "ok"}])
                detail = await admin.get(f"/api/ozon-fbo/runs/{run['id']}", params={"after": 1})
                body = detail.json()
                self.assertEqual(body["log"], [{"seq": 2, "text": "вторая строка"}])
                self.assertEqual(body["rows"][0]["cluster"], "Тверь")
                self.assertEqual(body["run"]["status"], "running")

                not_waiting = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/answer", json={"answer": "yes"})
                self.assertEqual(not_waiting.status_code, 409)
                invalid = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/answer", json={"answer": "maybe"})
                self.assertEqual(invalid.status_code, 422)
                fbo_repo.set_run_question(run["id"], "plan", "ПЛАН ПОСТАВОК…")
                answered = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/answer", json={"answer": "no"})
                self.assertEqual(answered.status_code, 200, answered.text)
                self.assertEqual(fbo_repo.take_run_answer(run["id"]), "no")

                release_alive = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/release")
                self.assertEqual(release_alive.status_code, 409, "живой прогон снять нельзя")
                stopped = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertTrue(fbo_repo.is_stop_requested(run["id"]))

                report = await admin.get(f"/api/ozon-fbo/runs/{run['id']}/report.txt")
                self.assertEqual(report.status_code, 200)
                self.assertIn("первая строка", report.text)
                self.assertIn("attachment", report.headers["content-disposition"])
                self.assertEqual((await admin.get(f"/api/ozon-fbo/runs/{run['id']}/result.xlsx")).status_code, 404)
                self.assertEqual((await admin.get(f"/api/ozon-fbo/runs/{run['id']}/api-log.zip")).status_code, 404)
                self.assertEqual((await admin.get("/api/ozon-fbo/runs/999")).status_code, 404)

                fbo_repo.finish_run(run["id"], "stopped", result_file_name="план_результат.xlsx", result_file=b"XLSX")
                after = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/stop")
                self.assertEqual(after.status_code, 409)
                result = await admin.get(f"/api/ozon-fbo/runs/{run['id']}/result.xlsx")
                self.assertEqual((result.status_code, result.content), (200, b"XLSX"))
                self.assertIn("spreadsheetml", result.headers["content-type"])
                listing = await admin.get("/api/ozon-fbo/runs")
                self.assertEqual([r["id"] for r in listing.json()], [run["id"]])
                self.assertNotIn("input_file", listing.json()[0])
        _run(scenario())

    def test_workbook_sheets_and_file_run(self) -> None:
        payload = Sheet.make_book()

        async def scenario():
            async with _Client() as admin:
                sheets = await admin.post("/api/ozon-fbo/workbook-sheets", files={"file": ("Поставки.xlsx", payload, "application/octet-stream")})
                self.assertEqual(sheets.status_code, 200, sheets.text)
                self.assertEqual(sheets.json(), {"file_name": "Поставки.xlsx", "sheets": ["Поставка 11.09"]})
                not_xlsx = await admin.post("/api/ozon-fbo/workbook-sheets", files={"file": ("план.csv", b"a;b", "text/csv")})
                self.assertEqual(not_xlsx.status_code, 400)
                broken = await admin.post("/api/ozon-fbo/workbook-sheets", files={"file": ("план.xlsx", b"not a workbook", "application/octet-stream")})
                self.assertEqual(broken.status_code, 400)
                with mock.patch.dict("os.environ", {"OZON_CLIENT_ID": "id", "OZON_API_KEY": "key"}):
                    wrong_sheet = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "file", "sheet_name": "нет такого"},
                                                   files={"file": ("Поставки.xlsx", payload, "application/octet-stream")})
                    self.assertEqual(wrong_sheet.status_code, 400)
                    self.assertIn("Не удалось прочитать таблицу", wrong_sheet.json()["detail"])
                    created = await admin.post("/api/ozon-fbo/runs", data={"mode": "trial", "source_kind": "file", "sheet_name": "Поставка 11.09"},
                                               files={"file": ("Поставки.xlsx", payload, "application/octet-stream")})
                    self.assertEqual(created.status_code, 201, created.text)
                    run = created.json()
                    self.assertEqual((run["source_kind"], run["sheet_name"], run["input_file_name"], run["has_input_file"]),
                                     ("file", "Поставка 11.09", "Поставки.xlsx", True))
                    self.assertEqual(fbo_repo.get_run_file(run["id"], "input"), ("Поставки.xlsx", payload))
                    self.assertEqual(self.started, [run["id"]])
        _run(scenario())

    def test_cargo_run_and_stale_release(self) -> None:
        async def scenario():
            async with _Client() as admin:
                with mock.patch.dict("os.environ", {"OZON_CLIENT_ID": "id", "OZON_API_KEY": "key"}):
                    created = await admin.post("/api/ozon-fbo/runs", data={"mode": "cargo", "order_keys": "2000070409945, 2000070409946\n999"})
                    self.assertEqual(created.status_code, 201, created.text)
                    run = created.json()
                    self.assertEqual((run["mode"], run["source_kind"], run["options"]["order_keys"]),
                                     ("cargo", "orders", ["2000070409945", "2000070409946", "999"]))
                fbo_repo.mark_run_started(run["id"], "")
                fbo_repo.update_run(run["id"], heartbeat_at="2020-01-01 00:00:00")
                status = await admin.get("/api/ozon-fbo/status")
                self.assertEqual(status.json()["recovered_runs"], [run["id"]], "статус сам снимает прогон без heartbeat")
                self.assertIsNone(status.json()["active_run"])
                self.assertEqual(fbo_repo.get_run(run["id"])["status"], "interrupted")
                released_again = await admin.post(f"/api/ozon-fbo/runs/{run['id']}/release")
                self.assertEqual(released_again.status_code, 409)
        _run(scenario())

    def test_check_connection_and_awaiting_orders_with_injected_api(self) -> None:
        from test_ozon_fbo_core import DAYS
        from test_ozon_fbo_pipeline import OrdersApi, PipelineApi

        class FailingAuth(PipelineApi):
            def cluster_list(self):
                from app.integrations.ozon_fbo.client import AuthError
                raise AuthError("/v2/cluster/list", 403, {"code": 7})

        holder = {"api": PipelineApi({}, DAYS)}
        router = create_ozon_fbo_router(fbo_repo, lambda request: {"id": 1, "role": "admin", "display_name": "A"},
                                        api_factory=lambda log: holder["api"], start_run=lambda run_id: None)
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router)

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as client:
                ok = await client.post("/api/ozon-fbo/check-connection")
                self.assertEqual((ok.status_code, ok.json()), (200, {"ok": True, "clusters": 3}))
                status = await client.get("/api/ozon-fbo/status")
                self.assertTrue(status.json()["keys_configured"], "с внедрённым API ключи считаются заданными")
                holder["api"] = OrdersApi()
                with mock.patch("time.sleep", lambda s: None):
                    orders = await client.get("/api/ozon-fbo/awaiting-cargo-orders")
                self.assertEqual(orders.status_code, 200, orders.text)
                numbers = [o["number"] for o in orders.json()["orders"]]
                self.assertEqual(numbers, ["2000070409945", "2000070409946"])
                second = orders.json()["orders"][1]
                self.assertEqual(second["supplies"][0]["where"], "НОГИНСК_РФЦ")
                self.assertEqual(second["supplies"][0]["items"][0]["layout"], "3 короба — 50+50+10 шт.")
                self.assertEqual(second["slot"], "08.12.2026 19:00 МСК")
                holder["api"] = FailingAuth({}, DAYS)
                failed = await client.post("/api/ozon-fbo/check-connection")
                self.assertEqual(failed.status_code, 502)
                self.assertIn("не принял ключи", failed.json()["detail"])
        _run(scenario())


if __name__ == "__main__":
    unittest.main()
