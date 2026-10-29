"""Грузоместа: раскладка, предохранители, передача в Ozon, режим «Дозаполнить» по любым заявкам."""
from __future__ import annotations

import time
import uuid
from typing import Callable, Optional

from app.integrations.ozon_fbo.client import ApiError, AuthError, NetworkError, OzonFboApi, RunLog, parse_orders

from .config import LK_ORDERS_URL, cfg
from .drafts import ArticleResult, ClusterLine, RunOutcome
from .orders import LiveError, order_warehouses
from .rules import cargo_capacity, cargo_layout, layout_text
from .store import FboStateStore, save_order_state
from .util import dicts, norm, short, utc_to_msk_text


def build_cargoes(article: str, sku: int, qty: int) -> list[dict]:
    """Грузоместа по одной поставке (кластеру): моно-товар, по вместимости штук, остаток в последнем.
    Формат /v1/cargoes/create: {"key": "...", "value": {"type": "BOX", "items": [{"offer_id", "quantity", "quant"}]}}."""
    return [{"key": str(uuid.uuid4()),
             "value": {"type": cfg().cargo_type,
                       "items": [{"offer_id": str(article), "quantity": int(q), "quant": 1}]}}
            for q in cargo_layout(article, qty)]


def cargo_qty(cargo: dict) -> int:
    return sum(int(i.get("quantity") or 0) for i in (cargo.get("value") or {}).get("items") or [])


def validate_order_cargoes(article: str, cluster_qty: dict, supplies: list[dict], items_by_supply: dict,
                           cargoes_by_supply: dict) -> list[str]:
    """Предохранители перед грузоместами по заявке, созданной приложением. Ozon сразу назначает поставкам
    склад и не возвращает кластер, поэтому поставки сверяются с подобранными фактами по составу:
    в каждой поставке только этот артикул, набор количеств по активным поставкам совпадает с набором фактов
    (отменённые Ozon поставки вычитаются), коробы по каждой поставке проверяет validate_item_cargoes."""
    errors = []
    want = sorted(int(q) for q in cluster_qty.values() if int(q or 0) > 0)

    def own_qty(s):
        return sum(int(it.get("quantity") or 0) for it in items_by_supply.get(s["supply_id"]) or []
                   if norm(it.get("offer_id")) == norm(article))
    for s in supplies:
        if supply_skipped(s):
            q = own_qty(s)
            if q in want:
                want.remove(q)
    got = []
    for s in supplies:
        if supply_skipped(s):
            continue
        items = items_by_supply.get(s["supply_id"]) or []
        where = f"{article} / поставка {s['supply_id']}" + (f" ({s['storage']})" if s.get("storage") else "")
        foreign = sorted({str(it.get("offer_id")) for it in items if norm(it.get("offer_id")) != norm(article)})
        if foreign:
            errors.append(f"{where}: в составе чужой артикул {foreign}")
        got.append(own_qty(s))
        errors.extend(validate_item_cargoes(article, s, items, cargoes_by_supply.get(s["supply_id"]) or []))
    if sorted(got) != want:
        errors.append(f"{article}: количества по поставкам в заявке {sorted(got)} не совпадают с фактами {want}")
    return errors


def send_cargoes(api: OzonFboApi, supply_id: int, cargoes: list[dict], say: Callable[[str], None],
                 label: str = "") -> list[int]:
    """Передать грузоместа одной поставки (/v1/cargoes/create) и дождаться статуса. Возвращает cargo_id.
    Вызывается только после предохранителей (validate_order_cargoes или validate_item_cargoes)."""
    layout = [cargo_qty(c) for c in cargoes]
    say(f"  поставка {supply_id}{label}: передаю {len(cargoes)} грузомест ({'+'.join(map(str, layout))})...")
    created = api.create_cargoes(supply_id, cargoes, f"cargoes_create_{supply_id}")
    operation_id = created.get("operation_id")
    if not operation_id:
        raise LiveError(f"поставка {supply_id}: Ozon не вернул operation_id: {short(created, 300)}")
    info: dict = {}
    for _ in range(40):
        time.sleep(3)
        info = api.cargoes_create_info(operation_id, f"cargoes_info_{supply_id}")
        st = str(info.get("status", "")).upper()
        if st and not any(w in st for w in ("PROGRESS", "PENDING", "WAIT")):
            break
    st = str(info.get("status", "")).upper()
    errs = [e for e in (info.get("errors") or info.get("error_messages") or []) if e]
    cargo_ids = sorted({int(c["cargo_id"]) for c in dicts(info)
                        if isinstance(c.get("cargo_id"), (int, str)) and str(c["cargo_id"]).isdigit()})
    if errs or "SUCCESS" not in st or not cargo_ids:
        raise LiveError(f"поставка {supply_id}: грузоместа не приняты (статус {st or '?'}"
                        + (f", ошибки: {short(errs, 300)}" if errs else "") + ")")
    say(f"  ✓ поставка {supply_id}: грузомест {len(cargo_ids)}, cargo_id {cargo_ids}")
    return cargo_ids


def create_cargoes_for_order(api: OzonFboApi, res: ArticleResult, cluster_qty: dict,
                             say: Callable[[str], None]) -> None:
    """Передать грузоместа по каждой поставке (кластеру) заявки, только что созданной приложением.
    Сначала обновляет сведения о заявке и читает состав каждой поставки, затем предохранители
    validate_order_cargoes (состав поставок должен совпасть с подобранными фактами), затем /v1/cargoes/create."""
    order_ids = [o["order_id"] for o in res.order_details if o.get("order_id")]
    if not order_ids:
        raise LiveError("нет order_id заявки — сначала создайте заявку")
    res.order_details = parse_orders(api.supply_orders(order_ids, "cargo_orders_before"))
    supplies = [s for o in res.order_details for s in o.get("supplies") or []]
    # состав каждой поставки читаем у Ozon: кластер в поставках не приходит, склад назначен сразу
    items_by_supply, cargoes_by_supply = {}, {}
    for s in supplies:
        items = api.bundle_items(s["bundle_id"], f"cargo_bundle_{s['supply_id']}") if s.get("bundle_id") else []
        items_by_supply[s["supply_id"]] = items
        skipped = supply_skipped(s)
        if skipped:
            say(f"  поставка {s['supply_id']} ({supply_where(s)}): {skipped}")
        else:
            cargoes_by_supply[s["supply_id"]] = build_item_cargoes(items)
    errors = validate_order_cargoes(res.article, cluster_qty, supplies, items_by_supply, cargoes_by_supply)
    if errors:
        raise LiveError("предохранители грузомест: " + "; ".join(errors))
    try:
        api.cargo_rules(list(cargoes_by_supply), "cargo_rules")     # ответ целиком — в журнале запросов (RunLog)
    except ApiError as e:
        say(f"  правила грузомест не получены (не критично): {short(e.data, 200)}")
    for supply_id, cargoes in cargoes_by_supply.items():
        s = next(x for x in supplies if x["supply_id"] == supply_id)
        cargo_ids = send_cargoes(api, supply_id, cargoes, say, f" ({supply_where(s)})")
        res.cargoes[str(supply_id)] = {"cluster_id": s.get("cluster_id"), "storage": s.get("storage"),
                                       "layout": [cargo_qty(c) for c in cargoes], "cargo_ids": cargo_ids}
    res.order_details = parse_orders(api.supply_orders(order_ids, "cargo_orders_after"))
    res.warehouses = order_warehouses(res) or res.warehouses
    for o in res.order_details:
        say(f"  заявка №{o['number']}: состояние {o.get('state') or '?'}"
            + (f", срок заполнения данных {utc_to_msk_text(o['deadline_utc'])}" if o.get("deadline_utc") else ""))


def build_item_cargoes(items: list[dict]) -> list[dict]:
    """Грузоместа по составу поставки: каждый артикул отдельно, коробы по его вместимости."""
    cargoes: list[dict] = []
    for it in items:
        cargoes.extend(build_cargoes(it["offer_id"], it.get("sku") or 0, int(it["quantity"])))
    return cargoes


def validate_item_cargoes(number: str, supply: dict, items: list[dict], cargoes: list[dict]) -> list[str]:
    """Предохранители перед передачей грузомест по составу поставки у Ozon: состав не пустой, поставка
    в состоянии DATA_FILLING, по каждому артикулу сумма по коробам равна количеству в составе, коробы
    не пустые и не больше вместимости, лишних артикулов нет."""
    errors = []
    where = f"заявка №{number} / поставка {supply.get('supply_id')}"
    if not items:
        errors.append(f"{where}: состав пустой")
    for it in items:
        if int(it.get("quantity") or 0) <= 0:
            errors.append(f"{where}: у {it.get('offer_id')} в составе количество {it.get('quantity')}")
    if items and not cargoes:
        errors.append(f"{where}: грузоместа не сформированы")
    state = str(supply.get("state") or "").upper()
    if "DATA_FILLING" not in state:
        errors.append(f"{where}: состояние {state or '?'}, а не DATA_FILLING")
    want: dict[str, int] = {}
    for it in items:
        want[it["offer_id"]] = want.get(it["offer_id"], 0) + int(it["quantity"])
    got: dict[str, int] = {}
    for c in cargoes:
        box_items = (c.get("value") or {}).get("items") or []
        if len(box_items) != 1:
            errors.append(f"{where}: грузоместо не моно-товар ({len(box_items)} позиций)")
        for i in box_items:
            got[i["offer_id"]] = got.get(i["offer_id"], 0) + int(i["quantity"])
        offer = box_items[0]["offer_id"] if box_items else ""
        q = cargo_qty(c)
        if q <= 0 or q > cargo_capacity(offer):
            errors.append(f"{where}: грузоместо {offer} на {q} шт. при вместимости {cargo_capacity(offer)}")
    for offer, q in want.items():
        if got.get(offer, 0) != q:
            errors.append(f"{where}: {offer} — в грузоместах {got.get(offer, 0)} шт., а в составе {q}")
    for offer in got:
        if offer not in want:
            errors.append(f"{where}: в грузоместах лишний артикул {offer}")
    return errors


def load_orders_awaiting_cargo(api: OzonFboApi, say: Callable[[str], None],
                               keys: Optional[list[str]] = None) -> list[dict]:
    """Заявки Ozon в состоянии DATA_FILLING (ждут грузомест) с составом каждой поставки.
    keys — оставить только заявки с этими номерами или идентификаторами (пусто — все)."""
    ids = api.supply_order_ids(["DATA_FILLING"], "orders_list")
    orders: list[dict] = []
    for i in range(0, len(ids), 50):
        orders.extend(parse_orders(api.supply_orders(ids[i:i + 50], f"orders_get_{i // 50 + 1}")))
    orders = [o for o in orders if "DATA_FILLING" in str(o.get("state") or "").upper()]
    if keys:
        wanted = {str(k).strip() for k in keys if str(k).strip()}
        found = [o for o in orders if str(o["number"]) in wanted or str(o["order_id"]) in wanted]
        missing = wanted - {str(o["number"]) for o in found} - {str(o["order_id"]) for o in found}
        if missing:
            say(f"Заявки не найдены среди ожидающих грузомест (возможно, уже заполнены или номер неверный): "
                f"{sorted(missing)}")
        orders = found
    for o in orders:
        for s in o["supplies"]:
            s["items"] = (api.bundle_items(s["bundle_id"], f"bundle_{s['supply_id']}")
                          if s.get("bundle_id") and not supply_skipped(s) else [])
    return orders


def supply_skipped(s: dict) -> str:
    """Почему поставка внутри заявки не требует грузомест: '' — требует (DATA_FILLING); иначе текст причины.
    Ozon может отменить одну поставку из многих — остальные должны быть заполнены, поэтому такие поставки
    пропускаются, а не останавливают заявку."""
    state = str(s.get("state") or "").upper()
    if "DATA_FILLING" in state:
        return ""
    if "CANCEL" in state:
        return "поставка отменена — грузоместа не нужны"
    if "READY" in state:
        return "грузоместа уже переданы"
    return f"поставка в состоянии {state or '?'} — грузоместа не передаются"


def supply_where(s: dict) -> str:
    return s.get("storage") or (f"кластер {s['cluster_id']}" if s.get("cluster_id") else f"поставка {s['supply_id']}")


def order_cargo_lines(o: dict) -> list[str]:
    """Строки плана по одной заявке: склад, артикулы, коробы."""
    lines = [f"№{o['number']}" + (f" — слот {utc_to_msk_text(o['slot_from_utc'])}" if o.get("slot_from_utc") else "")
             + (f", заполнить до {utc_to_msk_text(o['deadline_utc'])}" if o.get("deadline_utc") else "")
             + (f", точка {o['drop_off']}" if o.get("drop_off") else "")]
    for s in o["supplies"]:
        where = supply_where(s)
        skipped = supply_skipped(s)
        if skipped:
            lines.append(f"    {where}: {skipped}")
            continue
        if not s.get("items"):
            lines.append(f"    {where}: состав пустой")
        for it in s.get("items") or []:
            lines.append(f"    {where}: {it['offer_id']} {it['quantity']} шт. — {layout_text(it['offer_id'], it['quantity'])}")
    return lines


def cargo_plan_text(orders: list[dict]) -> str:
    active = [s for o in orders for s in o["supplies"] if not supply_skipped(s)]
    boxes = sum(len(cargo_layout(it["offer_id"], it["quantity"])) for s in active for it in s.get("items") or [])
    qty = sum(it["quantity"] for s in active for it in s.get("items") or [])
    lines = [f"ГРУЗОМЕСТА: заявок {len(orders)}, коробов {boxes}, товара {qty} шт. "
             f"(вместимость короба {cfg().default_cargo_capacity} шт., для отдельных артикулов — из настроек)"]
    for o in orders:
        lines.extend(order_cargo_lines(o))
    return "\n".join(lines)


def fill_order_cargoes(api: OzonFboApi, order: dict, say: Callable[[str], None]) -> dict:
    """Передать грузоместа по одной заявке по её составу у Ozon. Перед отправкой заявка и состав
    перечитываются, затем предохранители validate_item_cargoes по каждой поставке; при любом нарушении
    ничего не отправляется. Возвращает {supply_id: {"storage", "layout", "cargo_ids", "items"}}."""
    fresh = parse_orders(api.supply_orders([order["order_id"]], f"order_before_{order['order_id']}"))
    if not fresh:
        raise LiveError(f"заявка №{order['number']}: Ozon не вернул сведения о ней")
    cur = fresh[0]
    plan: dict[int, tuple[dict, list, list]] = {}
    errors: list[str] = []
    for s in cur["supplies"]:
        skipped = supply_skipped(s)
        if skipped:
            say(f"  поставка {s['supply_id']} ({supply_where(s)}): {skipped}")
            continue
        items = api.bundle_items(s["bundle_id"], f"bundle_before_{s['supply_id']}") if s.get("bundle_id") else []
        cargoes = build_item_cargoes(items)
        errors.extend(validate_item_cargoes(cur["number"], s, items, cargoes))
        plan[int(s["supply_id"])] = (s, items, cargoes)
    if errors:
        raise LiveError("предохранители грузомест: " + "; ".join(errors))
    if not plan:
        raise LiveError(f"заявка №{cur['number']}: нет поставок, ожидающих грузомест (все отменены или уже заполнены)")
    result: dict = {}
    for supply_id, (s, items, cargoes) in plan.items():
        label = f" ({s['storage']})" if s.get("storage") else ""
        cargo_ids = send_cargoes(api, supply_id, cargoes, say, label)
        result[str(supply_id)] = {"storage": s.get("storage"), "cluster_id": s.get("cluster_id"),
                                  "layout": [cargo_qty(c) for c in cargoes], "cargo_ids": cargo_ids, "items": items}
    for o in parse_orders(api.supply_orders([order["order_id"]], f"order_after_{order['order_id']}")):
        say(f"  заявка №{o['number']}: состояние {o.get('state') or '?'}"
            + (f", срок заполнения данных {utc_to_msk_text(o['deadline_utc'])}" if o.get("deadline_utc") else ""))
    return result


def order_result(order: dict, cargoes: dict, error: str = "") -> ArticleResult:
    """Результат по заявке в виде ArticleResult — для таблицы результатов и отчёта: строка на каждый артикул поставки."""
    lines = []
    for s in order["supplies"]:
        where = supply_where(s)
        skipped = supply_skipped(s)
        if skipped:
            qty = sum(int(it.get("quantity") or 0) for it in s.get("items") or [])
            lines.append(ClusterLine(0, where, s.get("cluster_id"), qty, fact=0, note=f"заявка №{order['number']}: {skipped}"))
            continue
        done = cargoes.get(str(s["supply_id"]))
        for it in s.get("items") or []:
            note = (f"заявка №{order['number']}: грузомест {len(done['cargo_ids'])} ({'+'.join(map(str, done['layout']))} шт.)"
                    if done else f"заявка №{order['number']}: грузоместа НЕ переданы — {error}")
            lines.append(ClusterLine(0, where, s.get("cluster_id"), it["quantity"], fact=it["quantity"], note=note))
        if not s.get("items"):
            lines.append(ClusterLine(0, where, s.get("cluster_id"), 0, fact=0, note=f"заявка №{order['number']}: состав пустой"))
    res = ArticleResult(article=", ".join(dict.fromkeys(it["offer_id"] for s in order["supplies"] for it in s.get("items") or [])) or "?",
                        sku=None, lines=lines, orders=[str(order["number"])], cargoes=cargoes)
    if error:
        res.manual.append(f"грузоместа не переданы: {error}")
    return res


def run_cargo_orders_pipeline(api: OzonFboApi, store: FboStateStore, keys: Optional[list[str]], say: Callable[[str], None],
                              confirm: Callable[[str], bool], log: RunLog,
                              should_stop: Optional[Callable[[], bool]] = None, confirm_each: bool = False,
                              auto_confirm: bool = False, on_result: Optional[Callable] = None) -> RunOutcome:
    """Дозаполнение грузомест по заявкам Ozon, которые ждут данных (DATA_FILLING), по их составу —
    независимо от того, где заявка создана (приложение или личный кабинет). keys — номера или
    идентификаторы заявок (пусто — все ожидающие). Одно общее подтверждение (или по каждой заявке).
    on_result(res, "cargo") — после каждой заявки."""
    out = RunOutcome()
    log.set_dir("_заявки")
    orders = load_orders_awaiting_cargo(api, say, keys)
    if not orders:
        say("Заявок, ожидающих грузомест, нет" + (" среди указанных номеров." if keys else "."))
        return out
    text = cargo_plan_text(orders)
    say("=" * 100)
    say(text)
    say("")
    if not confirm_each and not auto_confirm:
        if not confirm(text + "\n\nПередать грузоместа по всем заявкам выше?"):
            say("Отменено пользователем — грузоместа не переданы.")
            return out
    for o in orders:
        if should_stop and should_stop():
            out.stopped = True
            say("Остановлено пользователем — остальные заявки не трогаю.")
            break
        log.set_dir(f"заявка_{o['number']}")
        say(f"[заявка №{o['number']}]")
        if confirm_each and not auto_confirm and not confirm("\n".join(order_cargo_lines(o)) + "\n\nПередать грузоместа по этой заявке?"):
            say("  пропущено")
            continue
        cargoes: dict = {}
        error = ""
        try:
            cargoes = fill_order_cargoes(api, o, say)
        except (NetworkError, AuthError, KeyboardInterrupt):
            raise
        except (LiveError, ApiError) as e:
            error = str(e)
            say(f"  ! грузоместа не переданы: {e}")
            if isinstance(e, ApiError) and (e.data or {}).get("code") == 3:
                say("    Похоже, у метода другой формат запроса. Скопируйте из документации Ozon "
                    "«Пример запроса» и «Пример ответа» /v1/cargoes/create и пришлите их.")
        save_order_state(store, o, cargoes, error)
        out.results.append(order_result(o, cargoes, error))
        if on_result:
            on_result(out.results[-1], "cargo")
        say("")
    n_orders = sum(1 for r in out.results if r.cargoes)
    n_cargo = sum(len(c.get("cargo_ids") or []) for r in out.results for c in r.cargoes.values())
    say("=" * 100)
    say(f"ИТОГ: грузоместа переданы по {n_orders} из {len(out.results)} заявок, всего грузомест {n_cargo}. "
        f"Проверьте заявки в ЛК ({LK_ORDERS_URL}).")
    for r in out.results:
        if r.manual:
            say(f"  №{r.orders[0]}: " + "; ".join(r.manual))
    return out
