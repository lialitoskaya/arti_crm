"""Создание заявки на поставку по черновику (единственный путь к /v2/draft/supply/create)."""
from __future__ import annotations

import time
from typing import Callable

from app.integrations.ozon_fbo.client import ApiError, NetworkError, OzonFboApi, parse_order_ids, parse_orders

from .drafts import ArticleResult
from .rules import layout_text
from .util import short, utc_to_msk_text


def order_warehouses(res: ArticleResult) -> dict[str, str]:
    """Кластер → склад размещения, который Ozon назначил поставкам созданной заявки (он известен
    сразу после создания; в расчётном черновике единой отгрузки склада нет — его выбирает Ozon)."""
    by_id = {ln.cluster_id: ln.cluster for ln in res.lines if ln.cluster_id is not None}
    out: dict[str, str] = {}
    for o in res.order_details:
        for s in o.get("supplies") or []:
            if not s.get("storage"):
                continue
            cid = s.get("cluster_id")
            if cid is None and s.get("storage_id") is not None:
                cid = res.wh_map.get(int(s["storage_id"]))
            out[by_id.get(cid) or f"поставка {s.get('supply_id')}"] = str(s["storage"])
    return out


def warehouses_text(res: ArticleResult) -> str:
    return "; ".join(f"{c} → {w}" for c, w in res.warehouses.items())


class LiveError(Exception):
    pass


class DraftError(LiveError):
    """Ozon не принял сам черновик (устарел, не найден): заявка точно не создана, можно пересобрать и повторить."""


def create_order(api: OzonFboApi, res: ArticleResult, say: Callable[[str], None]) -> None:
    """Боевой режим: создать заявку на поставку по итоговому черновику и получить её номер.
    Грузоместа на этом этапе НЕ передаются. Вызывать только после validate_before_create."""
    cluster_ids = [ln.cluster_id for ln in res.lines if ln.fact]
    say(f"  создаю заявку по черновику {res.draft_id}...")
    try:
        created = api.create_supply(res.draft_id, cluster_ids, res.slot, "live_supply_create")
    except ApiError as e:
        if (e.data or {}).get("code") == 3:
            raise
        raise DraftError(f"Ozon не принял черновик {res.draft_id}: {short(e.data, 200)}") from None
    reasons = [r for r in (created.get("error_reasons") or []) if r]
    if reasons:
        raise DraftError(f"Ozon не принял черновик {res.draft_id}: {', '.join(map(str, reasons))}")
    operation_id = created.get("operation_id")
    status: dict = {}
    for _ in range(40):
        time.sleep(3)
        status = api.supply_create_status(res.draft_id, operation_id, "live_supply_status")
        st = str(status.get("status", "")).upper()
        if st and not any(w in st for w in ("PROGRESS", "PENDING", "WAIT")):
            break
    st = str(status.get("status", "")).upper()
    order_ids = parse_order_ids(status)
    errors = status.get("error_messages") or status.get("errors") or []
    if not order_ids:
        raise LiveError(f"Ozon не вернул идентификатор заявки (статус {st or '?'}"
                        + (f", ошибки: {short(errors, 300)}" if errors else "")
                        + "). Проверьте раздел «Заявки на поставку» в ЛК, прежде чем запускать снова.")
    res.created = True
    res.orders = [str(x) for x in order_ids]        # пока order_id; ниже заменим на номера из ЛК
    if errors:
        say(f"  ! Ozon сообщил об ошибках: {short(errors, 300)}")
    try:
        data = api.supply_orders(order_ids, "live_supply_orders")
        res.order_details = parse_orders(data)
    except (ApiError, NetworkError) as e:
        # заявка уже создана: сбой чтения сведений не должен прервать прогон до сохранения её номера
        say(f"  ! заявка создана, но сведения о ней получить не удалось: {e}")
    numbers = [o["number"] for o in res.order_details if o.get("number")]
    if numbers:
        res.orders = numbers
    say("  ✓ заявка создана: " + ", ".join(f"№{n}" for n in res.orders))
    for o in res.order_details:
        say(f"     order_id {o['order_id']}: состояние {o.get('state') or '?'}"
            + (f", данные заполнить до {utc_to_msk_text(o['deadline_utc'])}" if o.get("deadline_utc") else "")
            + (f", supply_id {o['supply_ids']}" if o.get("supply_ids") else ""))
    res.warehouses = order_warehouses(res)
    if res.warehouses:
        say("  склады размещения, которые назначил Ozon: " + warehouses_text(res))


def cargo_summary(res: ArticleResult) -> str:
    return "; ".join(f"{ln.cluster}: {layout_text(res.article, ln.fact)}" for ln in res.lines if ln.fact)
