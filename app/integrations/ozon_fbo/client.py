"""Запросы к Ozon Seller API для поставок FBO, журнал запросов, разбор ответов о заявках.

Единственный модуль, который ходит в сеть для раздела «Поставки FBO». Ключи берутся из
переменных среды и никуда не записываются; в дампы запросов попадают только путь метода,
тело, HTTP-код и ответ — без заголовков.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.services.ozon_fbo.config import (
    BASE_URL, DELETION_MODE, KNOWN_CREATE_REASONS, RATE_LIMITS, REQUEST_INTERVAL, SLOT_WINDOW_DAYS, SUPPLY_TYPE, cfg,
)
from app.services.ozon_fbo.util import dicts, safe_name, short

HTTP_TIMEOUT_SECONDS = 60.0


def parse_order_ids(status: dict) -> list[int]:
    """Идентификаторы заявок (order_id / order_ids) из ответа статуса создания, где бы они ни лежали."""
    ids = set()
    for d in dicts(status):
        for key in ("order_id", "order_ids"):
            v = d.get(key)
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, (int, str)) and str(x).isdigit() and int(x) > 0:
                    ids.add(int(x))
    return sorted(ids)


def parse_orders(data: dict) -> list[dict]:
    """Краткие сведения о заявках из ответа /v3/supply-order/get (формат ответа читаем нестрого)."""
    out = []
    for d in dicts(data):
        if not isinstance(d.get("order_id"), (int, str)):
            continue
        if not any(k in d for k in ("supply_order_number", "order_number", "state")):
            continue
        supplies = []
        for s in d.get("supplies") or []:
            if isinstance(s, dict) and s.get("supply_id"):
                cid = str(s.get("macrolocal_cluster_id") or "")
                storage = s.get("storage_warehouse") or {}
                supplies.append({"supply_id": s["supply_id"], "cluster_id": int(cid) if cid.isdigit() else None,
                                 "state": s.get("state"), "bundle_id": s.get("bundle_id"),
                                 # у заявок из ЛК склад выбран явно (storage_warehouse), кластер null
                                 "storage": storage.get("name"), "storage_id": storage.get("warehouse_id")})
        drop_off = d.get("drop_off_warehouse") or d.get("dropoff_warehouse") or {}
        out.append({
            "order_id": d.get("order_id"),
            "number": str(d.get("supply_order_number") or d.get("order_number") or d.get("order_id")),
            "state": d.get("state"),
            "deadline_utc": d.get("data_filling_deadline_utc") or d.get("data_filling_deadline"),
            "created_utc": d.get("created_date"),
            "slot_from_utc": ((d.get("timeslot") or {}).get("timeslot") or {}).get("from"),
            "drop_off": drop_off.get("name"),
            "supply_ids": [s["supply_id"] for s in supplies],
            "supplies": supplies,
        })
    return out


def slot_days(ts_response) -> list[dict]:
    res = (ts_response or {}).get("result") or {}
    return ((res.get("drop_off_warehouse_timeslots") or {}).get("days")) or []


class ApiError(Exception):
    def __init__(self, path, status, data):
        super().__init__(f"[{path}] HTTP {status}: {short(data, 600)}")
        self.path, self.status, self.data = path, status, data


class AuthError(ApiError):
    pass


class NetworkError(Exception):
    pass


class RunLog:
    """Сырые запросы/ответы Ozon в JSON-файлы (без заголовков) + строки отчёта.

    `base_dir=None` отключает дампы (например, в тестах). `on_line` получает каждую строку
    журнала; прогон в CRM через него пишет строки в БД."""

    def __init__(self, base_dir: Optional[Path], on_line: Optional[Callable[[str], None]] = None):
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.dir = self.base_dir
        self.counter = 0
        self.report_lines: list[str] = []
        self.on_line = on_line

    def set_dir(self, sub: str) -> None:
        if self.base_dir is not None:
            self.dir = self.base_dir / safe_name(sub)
        self.counter = 0

    def dump(self, name, path, body, status, data) -> Optional[Path]:
        if self.dir is None:
            return None
        self.counter += 1
        f = self.dir / f"{self.counter:03d}_{safe_name(name)}.json"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"method": path, "request": body, "http": status, "response": data},
                                    ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except OSError as e:
            # недоступный каталог логов не должен останавливать прогон: дальше работаем без дампов
            self.base_dir = self.dir = None
            self.say(f"      журнал запросов Ozon недоступен ({type(e).__name__}) — продолжаю без него")
            return None
        return f

    def say(self, text="") -> None:
        self.report_lines.append(text)
        if self.on_line is not None:
            self.on_line(text)


class OzonFboApi:
    """HTTP-клиент Ozon Seller API для поставок FBO (синхронный httpx, вызывается из рабочего потока)."""

    def __init__(self, client_id: str, api_key: str, log: RunLog, *, transport: Optional[httpx.BaseTransport] = None):
        self.log = log
        self._last = 0.0
        self._calls: dict[str, list[float]] = {}   # успешные вызовы методов с отдельным лимитом
        # Ozon доступен из России напрямую; системный прокси не используем (trust_env=False).
        self.session = httpx.Client(
            base_url=BASE_URL,
            headers={"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 — закрытие клиента не должно ронять прогон
            pass

    def _wait_for_limit(self, path: str) -> None:
        limit = RATE_LIMITS.get(path)
        if not limit:
            return
        n, window = limit
        times = [t for t in self._calls.get(path, []) if time.monotonic() - t < window]
        if len(times) >= n:
            wait = window - (time.monotonic() - times[0])
            if wait > 0:
                if wait >= 3:
                    self.log.say(f"      лимит Ozon на создание черновиков ({n} в минуту): жду {wait:.0f} с...")
                time.sleep(wait)
        self._calls[path] = times

    def post(self, path: str, body: dict, name: str) -> dict:
        for attempt in range(10):
            self._wait_for_limit(path)
            wait = REQUEST_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.post(path, json=body)
            except httpx.HTTPError as e:
                raise NetworkError(f"Не удалось связаться с Ozon ({path}): {type(e).__name__}") from None
            self._last = time.monotonic()
            text = r.text
            try:
                data = r.json()
            except ValueError:
                data = {"raw": text[:2000]}
            self.log.dump(name, path, body, r.status_code, data)
            code = data.get("code") if isinstance(data, dict) else None
            if r.status_code == 429 or code == 8 or "rate limit" in text.lower():
                pause = 15 if path in RATE_LIMITS else 3 * (attempt + 1)
                self.log.say(f"      лимит запросов Ozon, пауза {pause} с...")
                time.sleep(pause)
                continue
            if path in RATE_LIMITS and r.status_code == 200:
                self._calls.setdefault(path, []).append(time.monotonic())
            if code == 5 or r.status_code in (401, 403) or "invalid api-key" in text.lower():
                raise AuthError(path, r.status_code, data)
            if r.status_code != 200:
                raise ApiError(path, r.status_code, data)
            return data if isinstance(data, dict) else {"result": data}
        raise ApiError(path, 429, {"message": "лимит запросов Ozon: не удалось после нескольких попыток"})

    # --- справочники
    def cluster_list(self) -> list[dict]:
        """Кластеры Ozon: [{id, name, country, warehouses: {warehouse_id: name}}] (/v2/cluster/list)."""
        data = self.post("/v2/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"}, "clusters")
        out = []
        for item in data.get("result") or []:
            cid = item.get("macrolocal_cluster_id")
            d = item.get("data") or {}
            mc = d.get("macrolocal_cluster") or {}
            name = mc.get("name")
            if not cid or not isinstance(name, str):
                continue
            warehouses = {int(w["warehouse_id"]): str(w.get("name") or "")
                          for w in d.get("fulfillments") or [] if w.get("warehouse_id")}
            out.append({"id": int(cid), "name": name.strip(), "country": str((mc.get("country") or {}).get("name") or ""),
                        "warehouses": warehouses})
        return out

    def drop_off_point(self, name: str) -> dict:
        data = self.post("/v1/warehouse/fbo/list",
                         {"filter_by_supply_type": ["CREATE_TYPE_CROSSDOCK"], "search": name}, "dropoff")
        points = data.get("search") or []
        point = next((p for p in points if str(p.get("name", "")).strip().upper() == name.upper()), None)
        if not point:
            raise RuntimeError(f"Точка сдачи «{name}» не найдена. Ozon вернул: "
                               f"{[p.get('name') for p in points]}")
        wtype = str(point.get("warehouse_type") or "").replace("WAREHOUSE_TYPE_", "")
        return {"warehouse_id": point["warehouse_id"], "warehouse_type": wtype}

    def skus_by_offer(self, offer_ids: list[str]) -> dict[str, int]:
        """Артикул продавца → SKU Ozon через /v3/product/info/list."""
        result: dict[str, int] = {}
        for i in range(0, len(offer_ids), 500):
            chunk = offer_ids[i:i + 500]
            data = self.post("/v3/product/info/list",
                             {"offer_id": chunk, "product_id": [], "sku": []}, "product_info")
            items = data.get("items") or (data.get("result") or {}).get("items") or []
            for item in items:
                oid = str(item.get("offer_id") or "").strip()
                sku = item.get("sku") or 0
                if not sku:
                    for src in item.get("sources") or []:
                        if src.get("sku"):
                            sku = src["sku"]
                            if "fbo" in str(src.get("source", "")).lower():
                                break
                if oid and sku:
                    result[oid] = int(sku)
        return result

    def offers_by_sku(self, skus: list[int]) -> dict[int, str]:
        """SKU Ozon → артикул продавца (/v3/product/info/list по sku). Учитываются и SKU из sources[]."""
        result: dict[int, str] = {}
        for i in range(0, len(skus), 500):
            data = self.post("/v3/product/info/list",
                             {"offer_id": [], "product_id": [], "sku": [int(s) for s in skus[i:i + 500]]}, "product_info_sku")
            items = data.get("items") or (data.get("result") or {}).get("items") or []
            for item in items:
                oid = str(item.get("offer_id") or "").strip()
                if not oid:
                    continue
                for s in [item.get("sku")] + [src.get("sku") for src in item.get("sources") or []]:
                    if s:
                        result[int(s)] = oid
        return result

    # --- черновики
    def create_draft(self, sku: int, cluster_qty: dict[int, int], drop_off: dict, name: str) -> tuple[int, dict]:
        body = {
            "clusters_info": [{"macrolocal_cluster_id": cid, "items": [{"sku": sku, "quantity": int(q)}]}
                              for cid, q in cluster_qty.items()],
            "deletion_sku_mode": DELETION_MODE,
            "delivery_info": {"drop_off_warehouse": drop_off, "type": "DROPOFF"},
        }
        data = self.post("/v1/draft/multi-cluster/create", body, name)
        if not data.get("draft_id") and "NOT_AVAILABLE_CLUSTERS" in create_error_reasons(data):
            # такой отказ бывает временным — повторяем один раз
            retry_sec = cfg().unavailable_retry_sec
            self.log.say(f"      Ozon: кластер недоступен — повторю через {retry_sec} с...")
            time.sleep(retry_sec)
            data = self.post("/v1/draft/multi-cluster/create", body, f"{name}_retry")
        return int(data.get("draft_id") or 0), data

    def wait_draft(self, draft_id: int, name: str, tries: int = 20) -> dict:
        info: dict = {}
        for _ in range(tries):
            time.sleep(2)
            info = self.post("/v2/draft/create/info", {"draft_id": draft_id}, name)
            if "PROGRESS" not in str(info.get("status", "")).upper():
                break
        return info

    def bundle_items(self, bundle_id: str, name: str) -> list[dict]:
        """Состав поставки или черновика: [{offer_id, sku, quantity}] (с пагинацией)."""
        items: list[dict] = []
        last_id = None
        for _ in range(50):
            body: dict[str, Any] = {"bundle_ids": [bundle_id], "limit": 100}
            if last_id:
                body["last_id"] = last_id
            data = self.post("/v1/supply-order/bundle", body, name)
            for it in data.get("items") or []:
                items.append({"offer_id": str(it.get("offer_id") or "").strip(), "sku": int(it.get("sku") or 0),
                              "quantity": int(it.get("quantity") or 0)})
            if not data.get("has_next") or not data.get("last_id"):
                break
            last_id = data["last_id"]
        return items

    def bundle_qty(self, bundle_id: str, sku: int, name: str) -> int:
        return sum(i["quantity"] for i in self.bundle_items(bundle_id, name) if i["sku"] == int(sku))

    def timeslots(self, draft_id: int, cluster_ids: list[int], today: date, name: str,
                  date_from: Optional[date] = None) -> dict:
        start = date_from or today
        body = {"draft_id": draft_id,
                "date_from": start.isoformat(),
                "date_to": (start + timedelta(days=SLOT_WINDOW_DAYS)).isoformat(),
                "supply_type": SUPPLY_TYPE,
                "selected_cluster_warehouses": [{"macrolocal_cluster_id": int(c)} for c in cluster_ids]}
        return self.post("/v2/draft/timeslot/info", body, name)

    def timeslot_days(self, draft_id: int, cluster_ids: list[int], today: date, name: str) -> tuple[list, Optional[str]]:
        """Все дни со слотами вперёд: окна по 28 дней, пока Ozon возвращает слоты (не больше slot_max_windows).
        Возвращает (days, error_reason). Нужно, чтобы выбрать действительно самый поздний слот."""
        days, seen = [], set()
        start = today
        for n in range(max(1, cfg().slot_max_windows)):
            try:
                ts = self.timeslots(draft_id, cluster_ids, today, f"{name}_{n + 1}" if n else name, date_from=start)
            except ApiError:
                if n == 0:
                    raise
                break                       # дальше Ozon не отвечает — берём, что уже есть
            reason = ts.get("error_reason")
            if reason not in (None, "", "UNSPECIFIED"):
                if n == 0:
                    return [], str(reason)
                break
            new = [d for d in slot_days(ts) if d.get("date_in_timezone") not in seen]
            seen.update(d.get("date_in_timezone") for d in new)
            days.extend(new)
            if not any(d.get("timeslots") for d in new):
                break                       # в этом окне слотов нет — дальше смотреть незачем
            start = start + timedelta(days=SLOT_WINDOW_DAYS + 1)
        return days, None

    # --- заявки на поставку: эти методы вызываются ТОЛЬКО из боевого режима (create_order)
    def create_supply(self, draft_id: int, cluster_ids: list[int], slot: dict, name: str) -> dict:
        body = {"draft_id": int(draft_id), "supply_type": SUPPLY_TYPE,
                "selected_cluster_warehouses": [{"macrolocal_cluster_id": int(c)} for c in cluster_ids],
                "timeslot": {"from_in_timezone": slot["from"], "to_in_timezone": slot["to"]}}
        return self.post("/v2/draft/supply/create", body, name)

    def supply_create_status(self, draft_id: int, operation_id, name: str) -> dict:
        body = {"operation_id": operation_id} if operation_id else {"draft_id": int(draft_id)}
        return self.post("/v2/draft/supply/create/status", body, name)

    def supply_orders(self, order_ids: list[int], name: str) -> dict:
        """Сведения о заявках. Метод только читает; если формат запроса не тот — пробуем по одной."""
        try:
            return self.post("/v3/supply-order/get", {"order_ids": [int(x) for x in order_ids]}, name)
        except ApiError as e:
            if (e.data or {}).get("code") != 3 and e.status not in (400, 404):
                raise
        return {"orders": [self.post("/v3/supply-order/get", {"order_id": int(oid)}, f"{name}_{oid}")
                           for oid in order_ids]}

    def supply_order_ids(self, states: list[str], name: str) -> list[int]:
        """Идентификаторы заявок в указанных состояниях (/v3/supply-order/list, постранично, новые первыми)."""
        ids: list[int] = []
        last_id = ""
        for n in range(50):
            body: dict[str, Any] = {"filter": {"states": list(states)}, "limit": 100, "sort_by": "ORDER_CREATION", "sort_dir": "DESC"}
            if last_id:
                body["last_id"] = last_id
            data = self.post("/v3/supply-order/list", body, f"{name}_{n + 1}" if n else name)
            page = [int(x) for x in data.get("order_ids") or [] if str(x).isdigit()]
            ids.extend(x for x in page if x not in ids)
            last_id = str(data.get("last_id") or "")
            if not page or not last_id or len(page) < 100:
                break
        return ids

    # --- грузоместа: только из боевых режимов (create_cargoes_for_order, fill_order_cargoes)
    def cargo_rules(self, supply_ids: list[int], name: str) -> dict:
        return self.post("/v1/cargoes/rules/get", {"supply_ids": [int(x) for x in supply_ids]}, name)

    def create_cargoes(self, supply_id: int, cargoes: list[dict], name: str) -> dict:
        body = {"supply_id": int(supply_id), "delete_current_version": True, "cargoes": cargoes}
        return self.post("/v1/cargoes/create", body, name)

    def cargoes_create_info(self, operation_id: str, name: str) -> dict:
        return self.post("/v2/cargoes/create/info", {"operation_id": operation_id}, name)


def create_error_reasons(created: dict) -> set[str]:
    """Коды причин из ответа создания черновика (errors[].error_reasons)."""
    reasons = set()
    for e in created.get("errors") or []:
        if isinstance(e, dict):
            reasons.update(str(r) for r in e.get("error_reasons") or [])
    return reasons


def draft_create_problem(created: dict) -> str:
    """Понятный текст, почему черновик не создан."""
    parts = []
    for e in created.get("errors") or []:
        if not isinstance(e, dict):
            parts.append(str(e))
            continue
        reasons = [str(r) for r in e.get("error_reasons") or []]
        text = "; ".join(f"{KNOWN_CREATE_REASONS[r]} ({r})" if r in KNOWN_CREATE_REASONS else r for r in reasons)
        msg = e.get("error_message") or e.get("message") or ""
        parts.append(text or msg or short(e))
    return "черновик не создан: " + ("; ".join(p for p in parts if p) or short(created))


def get_credentials() -> tuple[str, str]:
    """Ключи только из переменных среды. Значения нигде не печатаются и не сохраняются.
    OZON_FBO_CLIENT_ID / OZON_FBO_API_KEY имеют приоритет над общими OZON_CLIENT_ID / OZON_API_KEY."""
    client_id = os.environ.get("OZON_FBO_CLIENT_ID", "").strip() or os.environ.get("OZON_CLIENT_ID", "").strip()
    api_key = os.environ.get("OZON_FBO_API_KEY", "").strip() or os.environ.get("OZON_API_KEY", "").strip()
    return client_id, api_key


def credentials_configured() -> bool:
    client_id, api_key = get_credentials()
    return bool(client_id and api_key)
