"""API раздела «Поставки FBO»: настройки, прогоны, вопросы пользователю, файлы результата.

Маршруты только валидируют вход, вызывают сервисы/репозиторий и формируют ответ.
Все маршруты (включая GET) — только для администратора: префикс /api/ozon-fbo/ входит в
`_ADMIN_GET_PREFIXES`, а unsafe-методы под /api/ admin-only по общему правилу.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.integrations.ozon_fbo import client as fbo_client
from app.schemas import OzonFboRunAnswer
from app.services.ozon_fbo import runner as fbo_runner
from app.services.ozon_fbo.cargo import load_orders_awaiting_cargo, supply_skipped, supply_where
from app.services.ozon_fbo.config import LK_ORDERS_URL, SETTING_LABELS, parse_settings, settings_to_dict, use_settings
from app.services.ozon_fbo.rules import layout_text
from app.services.ozon_fbo.sheet import load_sheet, parse_manual_rows, workbook_sheet_names
from app.services.ozon_fbo.util import utc_to_msk_text

RequireAdminDependency = Callable[[Request], dict[str, Any]]

MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _attachment_headers(filename: str) -> dict[str, str]:
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "_" for ch in filename) or "file"
    return {"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"}


def _parse_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "да")


async def _read_workbook(upload: UploadFile) -> tuple[str, bytes]:
    name = Path(str(upload.filename or "план.xlsx")).name
    if not name.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Нужен файл .xlsx (лист, скачанный из Google Таблиц)")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_WORKBOOK_BYTES:
            raise HTTPException(status_code=413, detail="Файл больше 20 МБ")
        chunks.append(chunk)
    payload = b"".join(chunks)
    if not payload:
        raise HTTPException(status_code=400, detail="Файл пустой")
    return name, payload


def _order_view(order: dict[str, Any]) -> dict[str, Any]:
    supplies = []
    for s in order.get("supplies") or []:
        skipped = supply_skipped(s)
        supplies.append({
            "supply_id": s.get("supply_id"),
            "where": supply_where(s),
            "skipped": skipped,
            "items": [{"offer_id": it["offer_id"], "quantity": it["quantity"],
                       "layout": layout_text(it["offer_id"], it["quantity"])} for it in s.get("items") or []],
        })
    return {
        "order_id": order.get("order_id"),
        "number": order.get("number"),
        "state": order.get("state"),
        "created": utc_to_msk_text(order["created_utc"])[:10] if order.get("created_utc") else "",
        "slot": utc_to_msk_text(order["slot_from_utc"]) if order.get("slot_from_utc") else "",
        "deadline": utc_to_msk_text(order["deadline_utc"]) if order.get("deadline_utc") else "",
        "drop_off": order.get("drop_off") or "",
        "supplies": supplies,
    }


def create_ozon_fbo_router(fbo_repo: Any, require_admin_dependency: RequireAdminDependency, *,
                           start_run: Optional[Callable[[int], Any]] = None,
                           api_factory: Optional[Callable[[fbo_client.RunLog], Any]] = None) -> APIRouter:
    router = APIRouter()

    def _api(log: fbo_client.RunLog) -> Any:
        if api_factory is not None:
            return api_factory(log)
        client_id, api_key = fbo_client.get_credentials()
        if not client_id or not api_key:
            raise HTTPException(status_code=400, detail="Ключи Ozon не заданы: нужны переменные среды OZON_CLIENT_ID и OZON_API_KEY")
        return fbo_client.OzonFboApi(client_id, api_key, log)

    def _settings() -> tuple[dict[str, Any], list[str]]:
        settings, errors = fbo_runner.load_active_settings()
        return settings_to_dict(settings), errors

    @router.get("/api/ozon-fbo/status")
    def api_status(request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        recovered = fbo_runner.recover_interrupted_runs()
        settings, errors = _settings()
        return {
            "keys_configured": fbo_client.credentials_configured() or api_factory is not None,
            "active_run": fbo_repo.get_active_run(),
            "recovered_runs": recovered,
            "settings": settings,
            "settings_errors": errors,
            "lk_url": LK_ORDERS_URL,
            "stale_after_seconds": fbo_runner.STALE_HEARTBEAT_SECONDS,
        }

    @router.get("/api/ozon-fbo/settings")
    def api_get_settings(request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        settings, errors = _settings()
        return {"settings": settings, "labels": SETTING_LABELS, "errors": errors}

    @router.put("/api/ozon-fbo/settings")
    def api_put_settings(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        current, _ = fbo_runner.load_active_settings()
        settings, errors = parse_settings(payload or {}, current)
        if errors:
            raise HTTPException(status_code=400, detail={"message": "Настройки не сохранены", "errors": errors})
        fbo_repo.save_settings_raw(settings_to_dict(settings))
        return {"settings": settings_to_dict(settings), "labels": SETTING_LABELS, "errors": []}

    @router.post("/api/ozon-fbo/check-connection")
    def api_check_connection(request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        log = fbo_client.RunLog(None)
        api = _api(log)
        try:
            clusters = api.cluster_list()
        except fbo_client.AuthError as exc:
            raise HTTPException(status_code=502, detail=f"Ozon не принял ключи API ({exc.path}): {fbo_client.short(exc.data, 200)}")
        except (fbo_client.ApiError, fbo_client.NetworkError) as exc:
            raise HTTPException(status_code=502, detail=f"Ozon не ответил как надо: {exc}")
        finally:
            if hasattr(api, "close"):
                api.close()
        return {"ok": True, "clusters": len(clusters)}

    @router.post("/api/ozon-fbo/workbook-sheets")
    async def api_workbook_sheets(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        require_admin_dependency(request)
        name, payload = await _read_workbook(file)
        try:
            sheets = workbook_sheet_names(payload)
        except Exception:  # noqa: BLE001 — любой сбой разбора книги это ошибка входных данных
            raise HTTPException(status_code=400, detail="Не удалось открыть файл как книгу .xlsx")
        return {"file_name": name, "sheets": sheets}

    @router.get("/api/ozon-fbo/awaiting-cargo-orders")
    def api_awaiting_cargo_orders(request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        settings, _ = fbo_runner.load_active_settings()
        log = fbo_client.RunLog(None)
        api = _api(log)
        try:
            with use_settings(settings):
                orders = load_orders_awaiting_cargo(api, log.say)
                views = [_order_view(o) for o in orders]
        except fbo_client.AuthError as exc:
            raise HTTPException(status_code=502, detail=f"Ozon не принял ключи API ({exc.path}): {fbo_client.short(exc.data, 200)}")
        except (fbo_client.ApiError, fbo_client.NetworkError) as exc:
            raise HTTPException(status_code=502, detail=f"Ozon не ответил как надо: {exc}")
        finally:
            if hasattr(api, "close"):
                api.close()
        return {"orders": views, "messages": list(log.report_lines)}

    @router.post("/api/ozon-fbo/runs", status_code=201)
    async def api_create_run(
        request: Request,
        mode: str = Form(...),
        source_kind: str = Form("manual"),
        sheet_name: str = Form(""),
        list_name: str = Form(""),
        manual_text: str = Form(""),
        articles: str = Form(""),
        order_keys: str = Form(""),
        force: str = Form("false"),
        confirm_each: str = Form("false"),
        auto_confirm: str = Form("false"),
        file: Optional[UploadFile] = File(None),
    ) -> dict[str, Any]:
        user = require_admin_dependency(request)
        if mode not in fbo_repo.RUN_MODES:
            raise HTTPException(status_code=400, detail="Неизвестный режим прогона")
        if not fbo_client.credentials_configured() and api_factory is None:
            raise HTTPException(status_code=400, detail="Ключи Ozon не заданы: нужны переменные среды OZON_CLIENT_ID и OZON_API_KEY")
        options: dict[str, Any] = {"force": _parse_flag(force), "confirm_each": _parse_flag(confirm_each),
                                   "auto_confirm": _parse_flag(auto_confirm)}
        input_name: Optional[str] = None
        input_bytes: Optional[bytes] = None
        if mode == "cargo":
            source_kind = "orders"
            keys = [k.strip() for k in str(order_keys or "").replace("\n", ",").split(",") if k.strip()]
            if not keys:
                raise HTTPException(status_code=400, detail="Выберите заявки, по которым передать грузоместа")
            options["order_keys"] = keys
            sheet = "грузоместа"
            articles_filter = ""
        else:
            articles_filter = ",".join(a.strip() for a in str(articles or "").split(",") if a.strip())
            if source_kind == "file":
                if file is None:
                    raise HTTPException(status_code=400, detail="Выберите файл .xlsx и лист")
                sheet = str(sheet_name or "").strip()
                if not sheet:
                    raise HTTPException(status_code=400, detail="Выберите лист")
                input_name, input_bytes = await _read_workbook(file)
                try:
                    _, _, _, rows = load_sheet(input_bytes, sheet)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"Не удалось прочитать таблицу: {exc}")
                except Exception:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail="Не удалось открыть файл как книгу .xlsx")
            elif source_kind == "manual":
                try:
                    rows = parse_manual_rows(manual_text)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=f"Ручной ввод: {exc}")
                options["manual_text"] = str(manual_text or "")
                sheet = str(list_name or "").strip() or "ручной ввод"
            else:
                raise HTTPException(status_code=400, detail="Неизвестный источник плана")
            visible = [r for r in rows if not r.hidden]
            filt = {a.lower() for a in articles_filter.split(",") if a}
            if not [r for r in visible if not filt or r.article.lower() in filt]:
                raise HTTPException(status_code=400, detail="Нет ни одной видимой строки с количеством больше нуля"
                                    + (" по указанным артикулам" if filt else ""))
        try:
            run = fbo_repo.create_run(mode=mode, source_kind=source_kind, sheet_name=sheet, articles_filter=articles_filter,
                                      options=options, created_by_user_id=user.get("id") if isinstance(user, dict) else None,
                                      created_by_label=str((user or {}).get("display_name") or (user or {}).get("username") or ""),
                                      input_file_name=input_name, input_file=input_bytes)
        except fbo_repo.RunConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        (start_run or fbo_runner.start_run)(int(run["id"]))
        return run

    @router.get("/api/ozon-fbo/runs")
    def api_list_runs(request: Request, limit: int = 30) -> list[dict[str, Any]]:
        require_admin_dependency(request)
        return fbo_repo.list_runs(limit=limit)

    @router.get("/api/ozon-fbo/runs/{run_id}")
    def api_get_run(run_id: int, request: Request, after: int = 0) -> dict[str, Any]:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        return {"run": run, "log": fbo_repo.get_run_log(run_id, after_seq=after), "rows": fbo_repo.get_run_rows(run_id)}

    @router.post("/api/ozon-fbo/runs/{run_id}/answer")
    def api_answer_run(run_id: int, payload: OzonFboRunAnswer, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        if not fbo_repo.answer_run(run_id, payload.answer):
            raise HTTPException(status_code=409, detail="Прогон сейчас не ждёт ответа")
        return {"ok": True, "run": fbo_repo.get_run(run_id)}

    @router.post("/api/ozon-fbo/runs/{run_id}/stop")
    def api_stop_run(run_id: int, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        if not fbo_repo.request_stop(run_id):
            raise HTTPException(status_code=409, detail="Прогон уже завершён")
        return {"ok": True, "run": fbo_repo.get_run(run_id)}

    @router.post("/api/ozon-fbo/runs/{run_id}/release")
    def api_release_run(run_id: int, request: Request) -> dict[str, Any]:
        """Снять зависший прогон (без heartbeat): forward recovery вручную."""
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        if not run["is_active"]:
            raise HTTPException(status_code=409, detail="Прогон уже завершён")
        stale = fbo_repo.mark_stale_runs_interrupted(fbo_runner.STALE_HEARTBEAT_SECONDS)
        if run_id not in stale:
            raise HTTPException(status_code=409, detail="Прогон ещё отвечает: дождитесь его окончания или нажмите «Остановить»")
        return {"ok": True, "run": fbo_repo.get_run(run_id)}

    @router.get("/api/ozon-fbo/runs/{run_id}/result.xlsx")
    def api_run_result(run_id: int, request: Request) -> Response:
        require_admin_dependency(request)
        stored = fbo_repo.get_run_file(run_id, "result")
        if stored is None:
            raise HTTPException(status_code=404, detail="Файл результата отсутствует")
        name, payload = stored
        return Response(content=payload, media_type=XLSX_MEDIA_TYPE, headers=_attachment_headers(name or "результат.xlsx"))

    @router.get("/api/ozon-fbo/runs/{run_id}/report.txt")
    def api_run_report(run_id: int, request: Request) -> Response:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        text = fbo_repo.get_run_log_text(run_id)
        return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers=_attachment_headers(f"ozon_fbo_run_{run_id}.txt"))

    @router.get("/api/ozon-fbo/runs/{run_id}/api-log.zip")
    def api_run_api_log(run_id: int, request: Request) -> Response:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        log_dir = Path(str(run.get("log_dir") or ""))
        if not run.get("log_dir") or not log_dir.is_dir():
            raise HTTPException(status_code=404, detail="Логи запросов для этого прогона отсутствуют")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in log_dir.rglob("*.json") if p.is_file()):
                archive.write(path, arcname=str(path.relative_to(log_dir)))
        return Response(content=buffer.getvalue(), media_type="application/zip",
                        headers=_attachment_headers(f"ozon_fbo_run_{run_id}_api_log.zip"))

    return router
