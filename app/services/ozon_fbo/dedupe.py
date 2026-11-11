"""Защита от дублей по заявкам Ozon за сегодня."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable, Optional

from app.integrations.ozon_fbo.client import parse_orders

from .config import ACTIVE_ORDER_STATES, MSK
from .store import FboStateStore, load_warehouse_map
from .util import norm


def order_created_date_msk(order: dict) -> Optional[date]:
    try:
        dt = datetime.fromisoformat(str(order.get("created_utc") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK).date()
    except (ValueError, TypeError):
        return None


def supply_key(article: str, cluster_id: Optional[int], warehouse_id: Optional[int]) -> tuple:
    """Ключ для защиты от дублей: артикул + склад; если склад не задан (единая отгрузка, склад выбирает
    Ozon) — артикул + кластер. Поставка на конкретный склад и поставка «на кластер» — разные ключи."""
    if warehouse_id:
        return (norm(article), "склад", int(warehouse_id))
    return (norm(article), "кластер", int(cluster_id) if cluster_id is not None else None)


def fetch_today_orders(api, today: date) -> list[dict]:
    """Заявки Ozon, созданные сегодня по Москве и не отменённые, с составом каждой поставки (items)."""
    ids = api.supply_order_ids(ACTIVE_ORDER_STATES, "dup_orders_list")
    todays = []
    for i in range(0, len(ids), 50):
        chunk = parse_orders(api.supply_orders(ids[i:i + 50], f"dup_orders_get_{i // 50 + 1}"))
        dates = [order_created_date_msk(o) for o in chunk]
        todays.extend(o for o, d in zip(chunk, dates) if d == today and "CANCEL" not in str(o.get("state") or "").upper())
        if dates and all(d is not None and d < today for d in dates):
            break       # список отсортирован от новых к старым — дальше только вчерашние и старше
    for o in todays:
        for s in o.get("supplies") or []:
            s["items"] = api.bundle_items(s["bundle_id"], f"dup_bundle_{s['supply_id']}") if s.get("bundle_id") else []
    return todays


def key_today_orders(todays: list[dict], wh_map: dict[int, int]) -> tuple[dict[tuple, set], set]:
    """Ключи защиты от дублей по заявкам за сегодня: (артикул, «склад», warehouse_id) по складу поставки и
    (артикул, «кластер», cluster_id) по кластеру — из macrolocal_cluster_id или по складу через wh_map.
    Поставка приложения «на кластер» блокируется любой сегодняшней заявкой в этот кластер (на любой его склад):
    склад выбирает Ozon, и он может совпасть с уже отгружаемым.
    Возвращает (ключи → номера заявок, склады без кластера: {(артикул, warehouse_id, название)})."""
    found: dict[tuple, set] = {}
    unknown: set = set()
    for o in todays:
        for s in o.get("supplies") or []:
            keys = []
            if s.get("storage_id"):
                keys.append(("склад", int(s["storage_id"])))
            cid = s.get("cluster_id")
            if cid is None and s.get("storage_id"):
                cid = wh_map.get(int(s["storage_id"]))
            if cid is not None:
                keys.append(("кластер", int(cid)))
            for it in s.get("items") or []:
                if not it.get("offer_id") or int(it.get("quantity") or 0) <= 0:
                    continue
                for kind, ident in keys:
                    found.setdefault((norm(it["offer_id"]), kind, ident), set()).add(str(o["number"]))
                if cid is None and s.get("storage_id"):
                    unknown.add((norm(it["offer_id"]), int(s["storage_id"]), str(s.get("storage") or "")))
    return found, unknown


def supplied_today(api, store: FboStateStore, cluster_list: list[dict], today: date, say: Callable[[str], None],
                   todays: Optional[list[dict]] = None, wh_map: Optional[dict[int, int]] = None) -> dict[tuple, set]:
    """Что уже отгружается сегодня по заявкам самого Ozon (в том числе созданным в ЛК):
    supply_key(артикул, кластер, склад) → номера заявок. См. key_today_orders; плюс собственные записи
    памяти за сегодня (кластер известен точно, номер должен быть среди активных заявок Ozon)."""
    if todays is None:
        todays = fetch_today_orders(api, today)
    found, _unknown = key_today_orders(todays, wh_map if wh_map is not None else load_warehouse_map(store, cluster_list))
    numbers_today = {str(o["number"]) for o in todays}
    for st in store.list_article_states():
        nums = {str(n) for n in st.get("orders") or []} & numbers_today
        if not nums or not st.get("article"):
            continue
        cids = st.get("cluster_ids") or {}
        for cname, q in (st.get("facts") or {}).items():
            if int(q or 0) > 0 and cids.get(cname) is not None:
                found.setdefault((norm(st["article"]), "кластер", int(cids[cname])), set()).update(nums)
    n_wh = sum(1 for k in found if k[1] == "склад")
    say(f"Проверка дублей: заявок Ozon за сегодня — {len(todays)}; пар «артикул + склад» — {n_wh}, "
        f"«артикул + кластер» — {len(found) - n_wh}.")
    return found
