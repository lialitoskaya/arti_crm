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
from starlette.concurrency import run_in_threadpool

from app.integrations.ozon_fbo import client as fbo_client
from app.schemas import OzonFboRunAnswer, OzonFboSettingsUpdate
from app.services.ozon_fbo import runner as fbo_runner
from app.services.ozon_fbo.cargo import load_orders_awaiting_cargo, order_view
from app.services.ozon_fbo.config import LK_ORDERS_URL, parse_settings, settings_to_dict, use_settings
from app.services.ozon_fbo.sheet import load_sheet, parse_manual_rows, workbook_sheet_names
from app.services.ozon_fbo.util import norm

RequireAdminDependency = Callable[[Request], dict[str, Any]]

MAX_WORKBOOK_BYTES = 20 * 1024 * 1024
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _attachment_headers(filename: str, ascii_name: str) -> dict[str, str]:
    """Имя файла: ASCII-вариант для старых клиентов и полное имя в filename* (RFC 5987)."""
    safe_ascii = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "_" for ch in ascii_name) or "file"
    return {"Content-Disposition": f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(filename, safe='')}"}


def _has_api_log(log_dir: str) -> bool:
    if not log_dir:
        return False
    path = Path(log_dir)
    return path.is_dir() and next(path.rglob("*.json"), None) is not None


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
        return {"settings": settings, "errors": errors}

    @router.put("/api/ozon-fbo/settings")
    def api_put_settings(payload: OzonFboSettingsUpdate, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        current, _ = fbo_runner.load_active_settings()
        settings, errors = parse_settings(payload.model_dump(exclude_none=True), current)
        if errors:
            raise HTTPException(status_code=400, detail={"message": "Настройки не сохранены", "errors": errors})
        fbo_repo.save_settings_raw(settings_to_dict(settings))
        return {"settings": settings_to_dict(settings), "errors": []}

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
            sheets = await run_in_threadpool(workbook_sheet_names, payload)
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
                views = [order_view(o) for o in orders]
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
                    _, _, _, rows = await run_in_threadpool(load_sheet, input_bytes, sheet)
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
            filt = {norm(a) for a in articles_filter.split(",") if a}
            if not [r for r in visible if not filt or norm(r.article) in filt]:
                raise HTTPException(status_code=400, detail="Нет ни одной видимой строки с количеством больше нуля"
                                    + (" по указанным артикулам" if filt else ""))
        try:
            run = await run_in_threadpool(
                fbo_repo.create_run, mode=mode, source_kind=source_kind, sheet_name=sheet, articles_filter=articles_filter,
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
        run["has_api_log"] = _has_api_log(fbo_repo.get_run_log_dir(run_id))
        return {"run": run, "log": fbo_repo.get_run_log(run_id, after_seq=after), "rows": fbo_repo.get_run_rows(run_id)}

    @router.post("/api/ozon-fbo/runs/{run_id}/answer")
    def api_answer_run(run_id: int, payload: OzonFboRunAnswer, request: Request) -> dict[str, Any]:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        if not fbo_repo.answer_run(run_id, payload.answer, payload.question_seq):
            raise HTTPException(status_code=409, detail="Прогон сейчас не ждёт ответа или вопрос уже изменился")
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
        return Response(content=payload, media_type=XLSX_MEDIA_TYPE,
                        headers=_attachment_headers(name or "результат.xlsx", f"ozon_fbo_run_{run_id}_result.xlsx"))

    @router.get("/api/ozon-fbo/runs/{run_id}/report.txt")
    def api_run_report(run_id: int, request: Request) -> Response:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        text = fbo_repo.get_run_log_text(run_id)
        return Response(content=text.encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers=_attachment_headers(f"ozon_fbo_run_{run_id}.txt", f"ozon_fbo_run_{run_id}.txt"))

    @router.get("/api/ozon-fbo/runs/{run_id}/api-log")
    def api_run_api_log(run_id: int, request: Request) -> Response:
        require_admin_dependency(request)
        run = fbo_repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Прогон не найден")
        stored_dir = fbo_repo.get_run_log_dir(run_id)
        log_dir = Path(stored_dir)
        if not _has_api_log(stored_dir):
            raise HTTPException(status_code=404, detail="Логи запросов для этого прогона отсутствуют")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(p for p in log_dir.rglob("*.json") if p.is_file()):
                archive.write(path, arcname=str(path.relative_to(log_dir)))
        return Response(content=buffer.getvalue(), media_type="application/zip",
                        headers=_attachment_headers(f"ozon_fbo_run_{run_id}_api_log.zip", f"ozon_fbo_run_{run_id}_api_log.zip"))

    return router
