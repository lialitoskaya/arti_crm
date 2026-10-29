"""Память раздела: созданные заявки по спискам и артикулам, грузоместа по заявкам, выученные склады кластеров.

В desktop-приложении это были файлы `state/`; в CRM — таблицы `ozon_fbo_*`. Конвейер получает
объект-хранилище с небольшим интерфейсом, поэтому тесты могут использовать память в процессе,
а приложение — репозиторий."""
from __future__ import annotations

from typing import Any, Callable, Protocol

from app.integrations.ozon_fbo.client import ApiError, draft_create_problem, parse_orders

from .config import DELETION_MODE
from .drafts import ArticleResult
from .rules import cargo_layout
from .sheet import warehouse_cluster_map
from .util import now_msk, short


class FboStateStore(Protocol):
    def get_article_state(self, sheet: str, article: str) -> dict[str, Any]: ...
    def set_article_state(self, sheet: str, article: str, data: dict[str, Any]) -> None: ...
    def list_article_states(self) -> list[dict[str, Any]]: ...
    def set_order_state(self, number: str, data: dict[str, Any]) -> None: ...
    def learned_warehouse_map(self) -> dict[int, int]: ...
    def learned_clusters(self) -> set[int]: ...
    def save_learned_warehouses(self, cluster_id: int, cluster_name: str | None, warehouses: dict[int, str]) -> None: ...


class InMemoryStateStore:
    """Хранилище в памяти процесса — для тестов и для прогонов без БД."""

    def __init__(self) -> None:
        self.articles: dict[tuple[str, str], dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.warehouses: dict[int, dict[str, Any]] = {}
        self.clusters: dict[int, str] = {}

    def get_article_state(self, sheet: str, article: str) -> dict[str, Any]:
        return dict(self.articles.get((sheet, article), {}))

    def set_article_state(self, sheet: str, article: str, data: dict[str, Any]) -> None:
        self.articles[(sheet, article)] = dict(data)

    def list_article_states(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self.articles.values()]

    def set_order_state(self, number: str, data: dict[str, Any]) -> None:
        self.orders[str(number)] = dict(data)

    def learned_warehouse_map(self) -> dict[int, int]:
        return {int(wid): int(info["cluster_id"]) for wid, info in self.warehouses.items()}

    def learned_clusters(self) -> set[int]:
        return set(self.clusters)

    def save_learned_warehouses(self, cluster_id: int, cluster_name: str | None, warehouses: dict[int, str]) -> None:
        for wid, name in warehouses.items():
            self.warehouses[int(wid)] = {"cluster_id": int(cluster_id), "cluster": cluster_name, "warehouse": name}
        self.clusters[int(cluster_id)] = now_msk().isoformat(timespec="seconds")


def load_warehouse_map(store: FboStateStore, cluster_list: list[dict]) -> dict[int, int]:
    """warehouse_id → id кластера: из /v2/cluster/list (fulfillments) плюс выученное через черновики прямой
    поставки — в fulfillments есть не все склады (например, ВЛАДИВОСТОК_РФЦ)."""
    out = warehouse_cluster_map(cluster_list)
    for wid, cid in store.learned_warehouse_map().items():
        out.setdefault(int(wid), int(cid))
    return out


def learn_cluster_warehouses(api, store: FboStateStore, cluster_list: list[dict], cluster_ids: list[int], sku: int,
                             say: Callable[[str], None]) -> dict[int, int]:
    """Узнать все склады кластеров через черновики прямой поставки (/v1/draft/direct/create перечисляет склады
    кластера со storage_warehouse; черновики бесплатны, лимит 2 в минуту) и запомнить их.
    Нужно для защиты от дублей: заявки из ЛК указывают только склад, а Ozon в справочнике кластеров
    перечисляет не все склады."""
    names = {int(c["id"]): c["name"] for c in cluster_list}
    learned: dict[int, int] = {}
    for cid in cluster_ids:
        body = {"cluster_info": {"macrolocal_cluster_id": int(cid), "items": [{"sku": int(sku), "quantity": 1}]},
                "deletion_sku_mode": DELETION_MODE}
        try:
            created = api.post("/v1/draft/direct/create", body, f"learn_{cid}_create")
        except ApiError as e:
            say(f"  склады кластера {names.get(int(cid), cid)}: черновик не создан ({short(e.data, 150)})")
            continue
        draft_id = int(created.get("draft_id") or 0)
        if not draft_id:
            say(f"  склады кластера {names.get(int(cid), cid)}: {draft_create_problem(created)}")
            continue
        info = api.wait_draft(draft_id, f"learn_{cid}_info")
        found: dict[int, str] = {}
        for cl in info.get("clusters") or []:
            for w in cl.get("warehouses") or []:
                wh = w.get("storage_warehouse") or {}
                if wh.get("warehouse_id"):
                    found[int(wh["warehouse_id"])] = str(wh.get("name") or "")
                    learned[int(wh["warehouse_id"])] = int(cid)
        store.save_learned_warehouses(int(cid), names.get(int(cid)), found)
        say(f"  склады кластера {names.get(int(cid), cid)}: {len(found)}")
    return learned


def previous_order_active(api, prev: dict, say: Callable[[str], None]) -> bool:
    """Заявка из памяти ещё действует у Ozon? Отменённая в ЛК (CANCELLED) не считается созданной, и артикул
    можно обрабатывать заново без принудительного повтора. Если проверить не удалось — считаем созданной."""
    if not prev.get("orders"):
        return False
    ids = [o.get("order_id") for o in prev.get("order_details") or [] if o.get("order_id")]
    if not ids:
        return True
    try:
        orders = parse_orders(api.supply_orders(ids, "prev_orders_check"))
    except ApiError:
        return True
    active = [o for o in orders if "CANCEL" not in str(o.get("state") or "").upper()]
    if active:
        return True
    say("  ранее созданная заявка " + ", ".join("№" + str(n) for n in prev["orders"])
        + " отменена в кабинете Ozon — артикул обрабатывается заново")
    return False


def load_state(store: FboStateStore, sheet: str, article: str) -> dict:
    return store.get_article_state(sheet, article)


def save_state(store: FboStateStore, sheet: str, res: ArticleResult, mode: str, keep_prev_orders: bool = True) -> None:
    """keep_prev_orders=False — прежняя заявка из записи отменена в ЛК: её номер, сведения и грузоместа забываем."""
    prev = load_state(store, sheet, res.article) if keep_prev_orders else {}
    data = {
        "sheet": sheet, "article": res.article, "sku": res.sku, "mode": mode,
        "updated_at": now_msk().isoformat(timespec="seconds"),
        "facts": {ln.cluster: ln.fact for ln in res.lines},
        "cluster_ids": {ln.cluster: ln.cluster_id for ln in res.lines},
        "draft_id": res.draft_id, "slot": res.slot, "manual": res.manual,
        "cargo": {ln.cluster: cargo_layout(res.article, ln.fact or 0) for ln in res.lines},
        "orders": res.orders or prev.get("orders") or [],
        "order_details": res.order_details or prev.get("order_details") or [],
        "orders_created_at": (now_msk().isoformat(timespec="seconds") if res.created
                              else prev.get("orders_created_at")),
        "cargoes": res.cargoes or prev.get("cargoes") or {},
        "warehouses": res.warehouses or prev.get("warehouses") or {},
        # кластеры, куда сегодня уже была заявка: пропущены или созданы всё равно по решению пользователя
        "duplicates": {ln.cluster: {"orders": ln.dup_orders, "allowed": ln.dup_allowed} for ln in res.lines if ln.duplicate},
    }
    store.set_article_state(sheet, res.article, data)


def save_order_state(store: FboStateStore, order: dict, cargoes: dict, error: str = "") -> None:
    data = {"order_id": order.get("order_id"), "number": order.get("number"), "drop_off": order.get("drop_off"),
            "slot_from_utc": order.get("slot_from_utc"), "updated_at": now_msk().isoformat(timespec="seconds"),
            "supplies": [{k: s.get(k) for k in ("supply_id", "storage", "cluster_id", "items")} for s in order.get("supplies") or []],
            "cargoes": cargoes, "error": error}
    store.set_order_state(str(order.get("number")), data)
