"""Поставки FBO: конвейер целиком на подменённом Ozon — пробный прогон, боевой режим с общим планом,
дозаполнение грузомест, кластер «все», защита от дублей. Сети нет, состояние — в памяти."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402,F401
from test_ozon_fbo_core import DAYS, NOW, FakeLiveApi  # noqa: E402

from app.integrations.ozon_fbo.client import ApiError, RunLog, parse_orders  # noqa: E402
from app.services.ozon_fbo.cargo import build_item_cargoes, cargo_qty, run_cargo_orders_pipeline, validate_item_cargoes  # noqa: E402
from app.services.ozon_fbo.config import ACTIVE_ORDER_STATES, FboSettings, parse_settings, use_settings  # noqa: E402
from app.services.ozon_fbo.dedupe import supplied_today, supply_key  # noqa: E402
from app.services.ozon_fbo.drafts import ArticleResult, ClusterLine, RunOptions  # noqa: E402
from app.services.ozon_fbo.pipeline import outcome_rows, run_live_batch, run_pipeline  # noqa: E402
from app.services.ozon_fbo.rules import SupplyLine, validate_before_create  # noqa: E402
from app.services.ozon_fbo.sheet import expand_all_clusters, parse_manual_rows, resolve_products  # noqa: E402
from app.services.ozon_fbo.store import InMemoryStateStore, save_state  # noqa: E402


class PipelineApi(FakeLiveApi):
    def cluster_list(self):
        return [{"id": 4072, "name": "Тверь", "country": "Россия", "warehouses": {23021125185000: "ТВЕРЬ_РФЦ"}},
                {"id": 4041, "name": "Казань", "country": "Россия", "warehouses": {18044494830000: "КАЗАНЬ_РФЦ_НОВЫЙ"}},
                {"id": 4001, "name": "Беларусь", "country": "Беларусь", "warehouses": {23402539267000: "МИНСК_МПСЦ"}}]

    def drop_off_point(self, name):
        return {"warehouse_id": 1, "warehouse_type": "DELIVERY_POINT"}

    def skus_by_offer(self, offer_ids):
        return {"AT-P-P3": 1891251877}

    def offers_by_sku(self, skus):
        return {1891251877: "AT-P-P3"}


def run_fake_pipeline(api, rows_text, mode="trial", with_cargo=True, confirm=lambda t: True, articles=None,
                      prepare=None, force=False, on_result=None, allow_duplicates=False, ask_duplicates=None,
                      store=None):
    """run_pipeline/run_live_batch на подменённом API с хранилищем в памяти. prepare(store) — подготовить память.
    Возвращает (api, outcome, строки журнала, store)."""
    rows = parse_manual_rows(rows_text)
    out_lines = []
    store = store or InMemoryStateStore()
    if prepare:
        prepare(store)
    log = RunLog(None)
    options = RunOptions(mode=mode, with_cargo=with_cargo, articles=articles, force=force, on_result=on_result,
                         allow_duplicates=allow_duplicates, ask_duplicates=ask_duplicates)
    with mock.patch("time.sleep", lambda s: None):
        if mode == "live":
            outcome = run_live_batch(api, store, rows, "тест", options, NOW, out_lines.append, confirm, log)
        else:
            outcome = run_pipeline(api, store, rows, "тест", options, NOW, out_lines.append, confirm, log)
    return api, outcome, out_lines, store


def state_keys(store: InMemoryStateStore) -> list[str]:
    return sorted(f"{sheet}_{article}" for sheet, article in store.articles)


class Pipeline(unittest.TestCase):
    """run_pipeline целиком на подменённом API."""

    def run_it(self, mode="trial", with_cargo=True, confirm=lambda t: True, articles=None):
        return run_fake_pipeline(PipelineApi({4041: 9}, DAYS),
                                 "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20\nAT-P-P3; Луна; 5",
                                 mode=mode, with_cargo=with_cargo, confirm=confirm, articles=articles)

    def test_trial(self):
        api, out, lines, store = self.run_it()
        self.assertEqual(len(out.results), 1)
        res = out.results[0]
        self.assertEqual([ln.fact for ln in res.lines], [40, 0, None])
        self.assertEqual(out.facts, {1: 40, 2: 0})
        self.assertIn("РУЧНАЯ ПРОВЕРКА", out.notes[1])
        self.assertTrue(out.notes[2].startswith("исключён"))
        self.assertEqual(state_keys(store), ["тест_AT-P-P3"])
        self.assertEqual(api.supply_calls, [])
        self.assertTrue(any("Луна" in ln_ for ln_ in lines))

    def test_on_result_events(self):
        events = []
        run_fake_pipeline(PipelineApi({4041: 9}, DAYS), "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20",
                          on_result=lambda res, stage: events.append((stage, res.article, [ln.fact for ln in res.lines])))
        self.assertEqual(events, [("calc", "AT-P-P3", [40, 0])])
        events.clear()
        run_fake_pipeline(PipelineApi({}, DAYS), "AT-P-P3; Тверь; 40", mode="live",
                          on_result=lambda res, stage: events.append((stage, res.article, res.created)))
        self.assertEqual(events, [("calc", "AT-P-P3", False), ("order", "AT-P-P3", True)])

    def test_live_not_created_when_manual(self):
        api, out, _, _ = self.run_it(mode="live")
        self.assertEqual(api.supply_calls, [], "артикул с ненайденным кластером в боевом режиме не создаётся")

    def test_sku_input_resolves_to_article(self):
        api = PipelineApi({}, DAYS)
        api, out, lines, store = run_fake_pipeline(api, "1891251877; Тверь; 40\nat-p-p3; Казань; 20\n777; Тверь; 5")
        by = {r.article: r for r in out.results}
        self.assertIn("AT-P-P3", by, "строки по SKU и по артикулу в другом регистре слились в один артикул Ozon")
        self.assertEqual(sorted(ln.cluster for ln in by["AT-P-P3"].lines), ["Казань", "Тверь"])
        self.assertEqual(by["AT-P-P3"].sku, 1891251877)
        self.assertTrue(any("SKU 1891251877 — это артикул AT-P-P3" in ln_ for ln_ in lines), lines)
        self.assertIn("не найден SKU", by["777"].manual[0])
        self.assertIn("тест_AT-P-P3", state_keys(store))
        resolved = resolve_products(api, ["AT-P-P3", "at-p-p3", "1891251877", "777", "AT-X"])
        self.assertEqual(resolved, {"AT-P-P3": ("AT-P-P3", 1891251877), "at-p-p3": ("AT-P-P3", 1891251877),
                                    "1891251877": ("AT-P-P3", 1891251877), "777": (None, None), "AT-X": (None, None)})

    def test_cancelled_previous_order_does_not_block(self):
        def prepare(store):
            save_state(store, "тест", ArticleResult("AT-P-P3", 1891251877, [ClusterLine(1, "Тверь", 4072, 40, fact=40)],
                                                    orders=["OLD-1"], order_details=[{"order_id": 777, "number": "OLD-1"}],
                                                    created=True), "live")
        rows = "AT-P-P3; Тверь; 40"
        api = PipelineApi({}, DAYS)
        api.today_orders = {777: {"order_id": 777, "order_number": "OLD-1", "state": "CANCELLED",
                                  "created_date": "2026-09-11T06:00:00Z", "supplies": []}}
        api, out, lines, store = run_fake_pipeline(api, rows, mode="live", prepare=prepare)
        self.assertTrue(any("отменена в кабинете Ozon" in ln_ for ln_ in lines), lines)
        self.assertTrue(out.results[0].created)
        self.assertEqual(len(api.supply_calls), 1)
        self.assertEqual(store.articles[("тест", "AT-P-P3")]["orders"], ["N901"], "старый отменённый номер забыт, записан новый")
        api = PipelineApi({}, DAYS)
        api.today_orders = {777: {"order_id": 777, "order_number": "OLD-1", "state": "DATA_FILLING",
                                  "created_date": "2026-09-11T06:00:00Z", "supplies": []}}
        api, out, lines, store = run_fake_pipeline(api, rows, mode="live", prepare=prepare)
        self.assertTrue(out.results[0].skipped)
        self.assertEqual(api.supply_calls, [])
        self.assertEqual(store.articles[("тест", "AT-P-P3")]["orders"], ["OLD-1"])

    def test_article_filter(self):
        _, out, lines, _ = self.run_it(articles={"at-x"})
        self.assertEqual(out.results, [])
        self.assertTrue(any("Нечего делать" in ln_ for ln_ in lines))

    def test_stop_between_articles(self):
        api = PipelineApi({}, DAYS)
        rows = parse_manual_rows("AT-P-P3; Тверь; 40")
        with mock.patch("time.sleep", lambda s: None):
            out = run_pipeline(api, InMemoryStateStore(), rows, "тест", RunOptions(), NOW, lambda s: None,
                               lambda t: True, RunLog(None), should_stop=lambda: True)
        self.assertTrue(out.stopped)
        self.assertEqual(out.results, [])


class BatchApi(PipelineApi):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.refuse_first_supply = False

    def skus_by_offer(self, offer_ids):
        return {"AT-P-P3": 1891251877, "AT-L-A3": 1716053944}

    def create_supply(self, draft_id, cluster_ids, slot, name):
        if self.refuse_first_supply and not self.supply_calls:
            self.supply_calls.append(("refused", draft_id))
            return {"error_reasons": ["DRAFT_NOT_FOUND"], "draft_id": draft_id}
        return super().create_supply(draft_id, cluster_ids, slot, name)


class LiveBatch(unittest.TestCase):
    """Боевой режим с одним общим подтверждением: расчёт всех артикулов → план → заявки подряд."""

    rows_text = "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20\nAT-L-A3; Тверь; 10\nAT-X; Тверь; 10\n"

    def run_batch(self, api, confirm=lambda t: True, auto=False, age_drafts=False):
        rows = parse_manual_rows(self.rows_text)
        lines, asked = [], []

        def conf(text):
            asked.append(text)
            return confirm(text)
        options = RunOptions(mode="live", auto_confirm=auto)
        with mock.patch("time.sleep", lambda s: None):
            # draft_ttl_min=5 — «срок» черновика 0 минут: любой черновик считается устаревшим
            out = run_live_batch(api, InMemoryStateStore(), rows, "тест", options, NOW, lines.append, conf, RunLog(None),
                                 draft_ttl_min=5 if age_drafts else 30)
        return out, lines, asked

    def test_plan_then_create_all(self):
        api = BatchApi({4041: 9}, DAYS)
        out, lines, asked = self.run_batch(api)
        self.assertEqual(len(asked), 1, "одно общее подтверждение")
        plan = asked[0]
        self.assertIn("ПЛАН ПОСТАВОК: заявок 2", plan)
        self.assertIn("Тверь: 40 шт. — 1 короб — 40 шт.", plan)
        self.assertIn("Казань: 0 шт. (исключён", plan)
        self.assertIn("Не будут созданы:", plan)
        self.assertIn("AT-X — РУЧНАЯ ПРОВЕРКА", plan)
        self.assertEqual(len(api.supply_calls), 2)
        self.assertEqual(len(api.cargo_calls), 2)
        by_article = {r.article: r for r in out.results}
        self.assertTrue(by_article["AT-P-P3"].created and by_article["AT-L-A3"].created)
        self.assertFalse(by_article["AT-X"].created)
        self.assertIn("заявка №", out.notes[1])
        self.assertTrue(out.notes[2].startswith("исключён"))
        self.assertIn("заявка №", out.notes[3])
        self.assertIn("РУЧНАЯ ПРОВЕРКА", out.notes[4])

    def test_declined_plan_creates_nothing(self):
        api = BatchApi({}, DAYS)
        out, lines, asked = self.run_batch(api, confirm=lambda t: False)
        self.assertEqual(len(asked), 1)
        self.assertEqual(api.supply_calls, [])
        self.assertTrue(all("отменено пользователем" in out.notes[r.row] for r in parse_manual_rows(self.rows_text) if r.article != "AT-X"))

    def test_auto_confirm_skips_question(self):
        api = BatchApi({}, DAYS)
        out, lines, asked = self.run_batch(api, auto=True)
        self.assertEqual(asked, [])
        self.assertEqual(len(api.supply_calls), 2)

    def test_old_draft_is_rebuilt_before_order(self):
        api = BatchApi({}, DAYS)
        out, lines, asked = self.run_batch(api, age_drafts=True)
        self.assertEqual(len(api.supply_calls), 2)
        self.assertTrue(any("пересобираю" in ln_ for ln_ in lines))
        self.assertEqual(len(api.compositions), 4)
        self.assertTrue(all(r.created for r in out.results if r.article != "AT-X"))

    def test_draft_refused_then_rebuilt_and_retried(self):
        api = BatchApi({}, DAYS)
        api.refuse_first_supply = True
        out, lines, asked = self.run_batch(api)
        self.assertEqual(api.supply_calls[0][0], "refused")
        self.assertEqual(len(api.supply_calls), 3, "отказ, повтор после пересборки, вторая заявка")
        self.assertTrue(all(r.created for r in out.results if r.article != "AT-X"))
        self.assertTrue(any("пробую ещё раз" in ln_ for ln_ in lines))


class OrdersApi(FakeLiveApi):
    def __init__(self):
        super().__init__({}, DAYS)
        self.list_calls = []
        self.bundles = {"bA": [{"offer_id": "AT-B-A2L-1M-WH", "sku": 5343797117, "quantity": 40}],
                        "bB": [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 110}],
                        "bC": [{"offer_id": "AT-L-AM-S", "sku": 2, "quantity": 12}]}

        def order(oid, number, state, supplies):
            return {"order_id": oid, "order_number": number, "state": state,
                    "created_date": "2026-10-14T11:33:31Z", "data_filling_deadline": "2026-12-08T15:00:00Z",
                    "drop_off_warehouse": {"warehouse_id": 1, "name": "КИЕВСКИЙ_2"},
                    "timeslot": {"timeslot": {"from": "2026-12-08T16:00:00Z", "to": "2026-12-08T17:00:00Z"}},
                    "supplies": supplies}

        def supply(sid, bundle, storage, state="DATA_FILLING"):
            return {"supply_id": sid, "state": state, "bundle_id": bundle, "macrolocal_cluster_id": None,
                    "storage_warehouse": {"warehouse_id": sid, "name": storage}, "is_crossdock": True}
        self.orders = {
            101: order(101, "2000070409945", "DATA_FILLING", [supply(7001, "bA", "СПБ_БУГРЫ_РФЦ")]),
            102: order(102, "2000070409946", "DATA_FILLING", [supply(7002, "bB", "НОГИНСК_РФЦ"),
                                                              supply(7003, "bC", "ПЕРМЬ_РФЦ")]),
            103: order(103, "2000070409788", "READY_TO_SUPPLY", [supply(7004, "bA", "СПБ_ПОРОШКИНО_РФЦ", "READY_TO_SUPPLY")]),
        }

    def supply_order_ids(self, states, name):
        self.list_calls.append(list(states))
        return [oid for oid, o in self.orders.items() if o["state"] in states]

    def supply_orders(self, order_ids, name):
        return {"orders": [self.orders[int(i)] for i in order_ids if int(i) in self.orders]}

    def bundle_items(self, bundle_id, name):
        return [dict(i) for i in self.bundles.get(bundle_id, [])]

    def create_cargoes(self, supply_id, cargoes, name):
        out = super().create_cargoes(supply_id, cargoes, name)
        for o in self.orders.values():
            for s in o["supplies"]:
                if s["supply_id"] == supply_id:
                    s["state"] = "READY_TO_SUPPLY"
            if all(s["state"] == "READY_TO_SUPPLY" for s in o["supplies"]):
                o["state"] = "READY_TO_SUPPLY"
        return out


class CargoOrders(unittest.TestCase):
    """Дозаполнение грузомест по заявкам Ozon, ожидающим данных (DATA_FILLING), по их составу."""

    def run_it(self, api, keys=None, confirm=lambda t: True, confirm_each=False, auto=False, stop=None):
        lines, asked = [], []

        def conf(text):
            asked.append(text)
            return confirm(text)
        store = InMemoryStateStore()
        with mock.patch("time.sleep", lambda s: None):
            out = run_cargo_orders_pipeline(api, store, keys, lines.append, conf, RunLog(None), stop,
                                            confirm_each=confirm_each, auto_confirm=auto)
        return out, lines, asked, sorted(store.orders)

    def test_plan_then_fill_all_waiting_orders(self):
        api = OrdersApi()
        out, lines, asked, order_keys = self.run_it(api)
        self.assertEqual(api.list_calls, [["DATA_FILLING"]], "берём только заявки, которые ждут данных")
        self.assertEqual(len(asked), 1, "одно общее подтверждение")
        plan = asked[0]
        self.assertIn("ГРУЗОМЕСТА: заявок 2, коробов 7, товара 162 шт.", plan)
        self.assertIn("№2000070409945 — слот 08.12.2026 19:00 МСК, заполнить до 08.12.2026 18:00 МСК, точка КИЕВСКИЙ_2", plan)
        self.assertIn("СПБ_БУГРЫ_РФЦ: AT-B-A2L-1M-WH 40 шт. — 1 короб — 40 шт.", plan)
        self.assertIn("НОГИНСК_РФЦ: AT-P-P3 110 шт. — 3 короба — 50+50+10 шт.", plan)
        self.assertIn("ПЕРМЬ_РФЦ: AT-L-AM-S 12 шт. — 3 короба — 5+5+2 шт.", plan)
        self.assertNotIn("2000070409788", plan, "готовая заявка не предлагается")
        self.assertEqual([(s, [cargo_qty(c) for c in cs]) for s, cs in api.cargo_calls],
                         [(7001, [40]), (7002, [50, 50, 10]), (7003, [5, 5, 2])])
        for _, cargoes in api.cargo_calls:
            for c in cargoes:
                self.assertEqual(c["value"]["items"][0]["quant"], 1)
                self.assertEqual(len(c["value"]["items"]), 1, "каждое грузоместо — один артикул")
        self.assertEqual(len(out.results), 2)
        self.assertEqual(out.results[0].orders, ["2000070409945"])
        self.assertEqual(out.results[0].cargoes["7001"]["cargo_ids"], [7001])
        self.assertEqual([(ln.cluster, ln.plan, ln.fact) for ln in out.results[1].lines],
                         [("НОГИНСК_РФЦ", 110, 110), ("ПЕРМЬ_РФЦ", 12, 12)])
        self.assertIn("грузомест 3 (50+50+10 шт.)", out.results[1].lines[0].note)
        self.assertEqual(order_keys, ["2000070409945", "2000070409946"])
        self.assertTrue(any("ИТОГ: грузоместа переданы по 2 из 2 заявок, всего грузомест 7" in ln_ for ln_ in lines))
        self.assertTrue(any("READY_TO_SUPPLY" in ln_ for ln_ in lines), "после передачи показываем новое состояние")

    def test_filter_by_number_and_unknown_number(self):
        api = OrdersApi()
        out, lines, asked, _ = self.run_it(api, keys=["2000070409946", "999"])
        self.assertEqual([s for s, _ in api.cargo_calls], [7002, 7003])
        self.assertTrue(any("не найдены" in ln_ and "999" in ln_ for ln_ in lines))
        out, lines, asked, _ = self.run_it(OrdersApi(), keys=["101"])
        self.assertEqual(len(out.results), 1, "по идентификатору тоже находится")

    def test_declined_and_stopped(self):
        api = OrdersApi()
        out, lines, asked, order_keys = self.run_it(api, confirm=lambda t: False)
        self.assertEqual(api.cargo_calls, [])
        self.assertEqual(out.results, [])
        self.assertEqual(order_keys, [])
        api = OrdersApi()
        out, lines, asked, _ = self.run_it(api, confirm_each=True, confirm=lambda t: "2000070409945" in t)
        self.assertEqual([s for s, _ in api.cargo_calls], [7001], "по каждой заявке отдельный вопрос")
        self.assertEqual(len(asked), 2)
        api = OrdersApi()
        out, lines, asked, _ = self.run_it(api, stop=lambda: True)
        self.assertEqual(api.cargo_calls, [])
        self.assertTrue(out.stopped)
        api = OrdersApi()
        out, lines, asked, _ = self.run_it(api, auto=True)
        self.assertEqual(asked, [])
        self.assertEqual(len(api.cargo_calls), 3)

    def test_safeguards_block_sending(self):
        api = OrdersApi()
        api.bundles["bB"] = [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 0}]
        out, lines, asked, _ = self.run_it(api)
        self.assertEqual([s for s, _ in api.cargo_calls], [7001], "заявка с пустым количеством не отправлена целиком")
        self.assertTrue(out.results[1].manual and "AT-P-P3" in out.results[1].manual[0], out.results[1].manual)
        self.assertTrue(any("ИТОГ: грузоместа переданы по 1 из 2 заявок" in ln_ for ln_ in lines))
        api = OrdersApi()
        api.orders[101]["supplies"][0]["state"] = "READY_TO_SUPPLY"
        out, lines, asked, _ = self.run_it(api, keys=["2000070409945"])
        self.assertEqual(api.cargo_calls, [])
        self.assertTrue(any("нет поставок, ожидающих грузомест" in r for r in out.results[0].manual), out.results[0].manual)

    def test_cancelled_supply_inside_order_is_skipped(self):
        api = OrdersApi()
        api.orders[102]["supplies"][1]["state"] = "CANCELLED"
        out, lines, asked, _ = self.run_it(api, keys=["2000070409946"])
        self.assertIn("ПЕРМЬ_РФЦ: поставка отменена — грузоместа не нужны", asked[0])
        self.assertIn("ГРУЗОМЕСТА: заявок 1, коробов 3, товара 110 шт.", asked[0], "отменённая поставка не считается")
        self.assertEqual([(s, [cargo_qty(c) for c in cs]) for s, cs in api.cargo_calls], [(7002, [50, 50, 10])])
        res = out.results[0]
        self.assertEqual(res.manual, [])
        self.assertEqual([(ln.cluster, ln.plan, ln.fact) for ln in res.lines], [("НОГИНСК_РФЦ", 110, 110), ("ПЕРМЬ_РФЦ", 0, 0)])
        self.assertIn("поставка отменена", res.lines[1].note)
        self.assertTrue(any("ИТОГ: грузоместа переданы по 1 из 1 заявок, всего грузомест 3" in ln_ for ln_ in lines), lines)

    def test_validate_item_cargoes(self):
        supply = {"supply_id": 1, "state": "DATA_FILLING"}
        items = [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 110}, {"offer_id": "AT-L-AM-S", "sku": 2, "quantity": 7}]
        cargoes = build_item_cargoes(items)
        self.assertEqual([cargo_qty(c) for c in cargoes], [50, 50, 10, 5, 2])
        self.assertEqual(validate_item_cargoes("N", supply, items, cargoes), [])
        errs = validate_item_cargoes("N", supply, items, cargoes[:-1])
        self.assertTrue(any("AT-L-AM-S — в грузоместах 5 шт., а в составе 7" in e for e in errs), errs)
        big = [{"key": "k", "value": {"type": "BOX", "items": [{"offer_id": "AT-L-AM-S", "quantity": 7, "quant": 1}]}}]
        errs = validate_item_cargoes("N", supply, items[1:], big)
        self.assertTrue(any("вместимости 5" in e for e in errs), errs)
        errs = validate_item_cargoes("N", supply, [], [])
        self.assertTrue(any("состав пустой" in e for e in errs), errs)
        extra = build_item_cargoes(items)
        errs = validate_item_cargoes("N", supply, items[:1], extra)
        self.assertTrue(any("лишний артикул AT-L-AM-S" in e for e in errs), errs)
        errs = validate_item_cargoes("N", {"supply_id": 1, "state": "IN_TRANSIT"}, items, cargoes)
        self.assertTrue(any("а не DATA_FILLING" in e for e in errs), errs)

    def test_parse_orders_keeps_storage_and_slot(self):
        orders = parse_orders({"orders": [OrdersApi().orders[101]]})
        self.assertEqual(orders[0]["number"], "2000070409945")
        self.assertEqual(orders[0]["supplies"][0]["storage"], "СПБ_БУГРЫ_РФЦ")
        self.assertIsNone(orders[0]["supplies"][0]["cluster_id"])
        self.assertEqual(orders[0]["slot_from_utc"], "2026-12-08T16:00:00Z")
        self.assertEqual(orders[0]["drop_off"], "КИЕВСКИЙ_2")


class AllClusters(unittest.TestCase):
    """Кластер «все» в таблице или ручном вводе → строка на каждый кластер из настройки (по умолчанию — Россия)."""

    def setUp(self):
        self.clusters = PipelineApi({}, DAYS).cluster_list()

    def test_expand_default_russia_and_explicit_rows_win(self):
        rows = parse_manual_rows("AT-P-P3; все; 10\nAT-L-A3; ВСЕ КЛАСТЕРЫ; 5\nAT-L-A3; Тверь; 40")
        out, msgs = expand_all_clusters(rows, self.clusters)
        self.assertEqual([(r.row, r.article, r.cluster, r.plan) for r in out],
                         [(1, "AT-P-P3", "Тверь", 10), (1, "AT-P-P3", "Казань", 10),
                          (2, "AT-L-A3", "Казань", 5), (3, "AT-L-A3", "Тверь", 40)],
                         "Беларусь не входит, явная строка «Тверь 40» важнее, чем «все; 5»")
        self.assertTrue(any("2 кластеров по 10" in s for s in msgs), msgs)
        self.assertEqual(expand_all_clusters(rows[2:], self.clusters), ([rows[2]], []))

    def test_expand_custom_list(self):
        settings, errs = parse_settings({"all_clusters": "Казань\n\n Луна "})
        self.assertEqual((errs, settings.all_clusters), ([], ["Казань", "Луна"]))
        rows = parse_manual_rows("AT-P-P3; все; 10")
        with use_settings(settings):
            out, msgs = expand_all_clusters(rows, self.clusters)
        self.assertEqual([r.cluster for r in out], ["Казань"])
        self.assertTrue(any("Луна" in s for s in msgs), msgs)
        settings, _ = parse_settings({"all_clusters": ["Луна"]})
        with use_settings(settings):
            out, msgs = expand_all_clusters(rows, self.clusters)
        self.assertEqual(out, [])
        self.assertTrue(any("пропущена" in s for s in msgs), msgs)

    def test_pipeline_all_sums_fact_in_source_row(self):
        api = PipelineApi({4041: 9}, DAYS)
        api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; все; 10")
        res = out.results[0]
        self.assertEqual([(ln.row, ln.cluster, ln.fact) for ln in res.lines], [(1, "Тверь", 10), (1, "Казань", 0)])
        self.assertEqual(out.facts, {1: 10}, "в исходной строке «все» факт — сумма по кластерам")
        self.assertTrue(out.notes[1].startswith("по кластерам: Тверь 10; Казань 0 (исключён"), out.notes[1])
        self.assertIn("ЗАЯВКА НЕ СОЗДАНА", out.notes[1])
        self.assertTrue(any("«Все кластеры»" in ln_ and "2 кластеров по 10" in ln_ for ln_ in lines))
        facts, notes = outcome_rows(res, "trial")
        self.assertEqual(facts, {1: 10})


def api_with_today_orders(cls=None):
    """Подменённый Ozon с заявками за сегодня: A-1 — заявка приложения на Казань по AT-P-P3; L-2 — заявка из ЛК
    на склад ТВЕРЬ_РФЦ по AT-L-A3; C-3 отменена; Y-4 вчерашняя."""
    api = (cls or BatchApi)({}, DAYS)

    def order(oid, number, created, state, supplies):
        return {"order_id": oid, "order_number": number, "created_date": created, "state": state, "supplies": supplies}
    api.today_orders = {
        501: order(501, "A-1", "2026-09-11T06:00:00Z", "DATA_FILLING",
                   [{"supply_id": 1, "state": "DATA_FILLING", "bundle_id": "t1", "macrolocal_cluster_id": "4041"}]),
        502: order(502, "L-2", "2026-09-10T21:30:00Z", "READY_TO_SUPPLY",
                   [{"supply_id": 2, "state": "READY_TO_SUPPLY", "bundle_id": "t2", "macrolocal_cluster_id": None,
                     "storage_warehouse": {"warehouse_id": 23021125185000, "name": "ТВЕРЬ_РФЦ"}}]),
        503: order(503, "C-3", "2026-09-11T07:00:00Z", "CANCELLED",
                   [{"supply_id": 3, "state": "CANCELLED", "bundle_id": "t3", "macrolocal_cluster_id": "4072"}]),
        504: order(504, "Y-4", "2026-09-10T12:00:00Z", "COMPLETED",
                   [{"supply_id": 4, "state": "COMPLETED", "bundle_id": "t4", "macrolocal_cluster_id": "4072"}]),
    }
    api.today_bundles = {"t1": [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 20}],
                         "t2": [{"offer_id": "at-l-a3", "sku": 2, "quantity": 10}],
                         "t3": [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 5}],
                         "t4": [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 5}]}
    return api


class Duplicates(unittest.TestCase):
    """Защита от дублей: артикул + кластер не больше одной заявки в день — по заявкам самого Ozon."""

    def test_do_anyway_after_explicit_answer(self):
        asked = []

        def ask(text):
            asked.append(text)
            return True
        api = api_with_today_orders()
        api, out, lines, store = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20\nAT-L-A3; Тверь; 10",
                                                   mode="live", ask_duplicates=ask)
        self.assertEqual(len(asked), 1, "один вопрос на весь прогон")
        self.assertIn("СЕГОДНЯ УЖЕ ЕСТЬ ЗАЯВКИ по 2 строкам", asked[0])
        self.assertIn("AT-P-P3 — Казань: план 20 шт.; сегодня уже есть №A-1", asked[0])
        self.assertIn("AT-L-A3 — Тверь: план 10 шт.; сегодня уже есть №L-2", asked[0])
        self.assertIn("всё равно сделать", asked[0])
        self.assertTrue(out.duplicates_allowed)
        self.assertEqual([(a, ln.cluster) for a, ln in out.duplicates], [("AT-P-P3", "Казань"), ("AT-L-A3", "Тверь")])
        by = {r.article: r for r in out.results}
        self.assertEqual([(ln.cluster, ln.fact, ln.duplicate, ln.dup_allowed) for ln in by["AT-P-P3"].lines],
                         [("Тверь", 40, False, False), ("Казань", 20, True, True)])
        self.assertTrue(by["AT-P-P3"].created and by["AT-L-A3"].created)
        self.assertEqual([sorted(c[1]) for c in api.supply_calls], [[4041, 4072], [4072]], "Казань в заявке, несмотря на A-1")
        self.assertEqual(out.facts, {1: 40, 2: 20, 3: 10})
        self.assertTrue(out.notes[2].startswith("дубль по решению пользователя (сегодня уже есть такая заявка на этот кластер: №A-1); заявка №"),
                        out.notes[2])
        self.assertTrue(out.notes[3].startswith("дубль по решению пользователя (сегодня уже есть такая заявка на этот кластер: №L-2); заявка №"),
                        out.notes[3])
        plan = next(ln_ for ln_ in lines if ln_.startswith("ПЛАН ПОСТАВОК"))
        self.assertIn("⚠ ДУБЛИ ПО ВАШЕМУ РЕШЕНИЮ: 2 строк", plan)
        self.assertTrue(any("Казань: 20 шт." in p and "⚠ ДУБЛЬ: сегодня уже есть такая заявка на этот кластер: №A-1" in p
                            for p in plan.splitlines()), plan)
        self.assertTrue(any("решение пользователя: всё равно сделать" in ln_ for ln_ in lines))
        self.assertEqual(store.articles[("тест", "AT-P-P3")]["duplicates"], {"Казань": {"orders": ["A-1"], "allowed": True}})

    def test_skip_answer_keeps_protection(self):
        asked = []
        api = api_with_today_orders()
        api, out, lines, store = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live",
                                                   ask_duplicates=lambda t: (asked.append(t), False)[1])
        self.assertEqual(len(asked), 1)
        self.assertFalse(out.duplicates_allowed)
        res = out.results[0]
        self.assertEqual([(ln.cluster, ln.fact, ln.duplicate, ln.dup_allowed) for ln in res.lines],
                         [("Тверь", 40, False, False), ("Казань", 0, True, False)])
        self.assertEqual(api.supply_calls[0][1], [4072])
        self.assertTrue(out.notes[2].startswith("сегодня уже есть такая заявка на этот кластер: №A-1 — пропущен"), out.notes[2])
        self.assertTrue(any("решение пользователя: пропустить" in ln_ for ln_ in lines))
        self.assertEqual(store.articles[("тест", "AT-P-P3")]["duplicates"], {"Казань": {"orders": ["A-1"], "allowed": False}})

    def test_remembered_order_in_same_question(self):
        def prepare(store):
            save_state(store, "тест", ArticleResult("AT-P-P3", 1891251877, [ClusterLine(1, "Тверь", 4072, 40, fact=40)],
                                                    orders=["OLD-1"], order_details=[{"order_id": 777, "number": "OLD-1"}],
                                                    created=True), "live")

        def api_remembering():
            api = PipelineApi({}, DAYS)
            api.today_orders = {777: {"order_id": 777, "order_number": "OLD-1", "state": "READY_TO_SUPPLY",
                                      "created_date": "2026-09-11T06:00:00Z", "supplies": []}}
            return api
        asked = []
        api, out, lines, store = run_fake_pipeline(api_remembering(), "AT-P-P3; Казань; 20", mode="live", prepare=prepare,
                                                   ask_duplicates=lambda t: (asked.append(t), True)[1])
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0].startswith("ПРИЛОЖЕНИЕ УЖЕ СОЗДАВАЛО ЗАЯВКУ по этому списку для 1 артикулов:\n    AT-P-P3 — №OLD-1 (создана "),
                        asked[0])
        self.assertIn("Пропустить эти артикулы", asked[0])
        self.assertEqual(out.remembered[0][:2], ("AT-P-P3", ["OLD-1"]))
        res = out.results[0]
        self.assertFalse(res.skipped)
        self.assertTrue(res.created, "«всё равно» снимает память и при расчёте, и при создании")
        self.assertEqual(api.supply_calls[0][1], [4041])
        self.assertTrue(any("уже есть — создаю ещё одну по решению пользователя" in ln_ for ln_ in lines), lines)
        self.assertEqual(store.articles[("тест", "AT-P-P3")]["orders"], ["N901"], "записана новая заявка")
        api, out, lines, _ = run_fake_pipeline(api_remembering(), "AT-P-P3; Казань; 20", mode="live", prepare=prepare,
                                               ask_duplicates=lambda t: False)
        self.assertTrue(out.results[0].skipped)
        self.assertIn("заявка уже создана: OLD-1 (пропущено)", out.results[0].note)
        self.assertEqual(api.supply_calls, [])
        api = api_with_today_orders()
        api.today_orders[777] = {"order_id": 777, "order_number": "OLD-1", "state": "READY_TO_SUPPLY",
                                 "created_date": "2026-09-11T06:00:00Z", "supplies": []}
        asked.clear()
        api, out, _, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live", prepare=prepare,
                                           ask_duplicates=lambda t: (asked.append(t), True)[1])
        self.assertIn("СЕГОДНЯ УЖЕ ЕСТЬ ЗАЯВКИ по 2 строкам", asked[0])
        self.assertIn("ПРИЛОЖЕНИЕ УЖЕ СОЗДАВАЛО ЗАЯВКУ по этому списку для 1 артикулов", asked[0])
        self.assertIn("Пропустить эти строки и артикулы", asked[0])
        self.assertTrue(out.results[0].created)
        self.assertEqual(sorted(api.supply_calls[0][1]), [4041, 4072])

    def test_allow_duplicates_flag_and_no_asker(self):
        api = api_with_today_orders()
        api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live",
                                               allow_duplicates=True, ask_duplicates=lambda t: self.fail("вопрос не нужен"))
        self.assertTrue(out.duplicates_allowed)
        self.assertEqual(sorted(api.supply_calls[0][1]), [4041, 4072])
        self.assertTrue(any("разрешено заранее" in ln_ for ln_ in lines))
        api = api_with_today_orders()
        api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live")
        self.assertFalse(out.duplicates_allowed)
        self.assertEqual(api.supply_calls[0][1], [4072])
        self.assertTrue(any("разрешить можно только явно" in ln_ for ln_ in lines))
        asked = []
        api = api_with_today_orders()
        api, out, _, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20",
                                           ask_duplicates=lambda t: (asked.append(t), True)[1])
        self.assertIn("Заявки в пробном режиме не создаются", asked[0])
        self.assertEqual([ln.fact for ln in out.results[0].lines], [40, 20])
        self.assertEqual(api.supply_calls, [])
        api, out, _, _ = run_fake_pipeline(PipelineApi({}, DAYS), "AT-P-P3; Тверь; 40",
                                           ask_duplicates=lambda t: self.fail("дублей нет — вопроса быть не должно"))
        self.assertEqual(out.duplicates, [])

    def test_supplied_today_keys_by_warehouse_or_cluster(self):
        api = api_with_today_orders()
        store = InMemoryStateStore()
        said = []
        found = supplied_today(api, store, api.cluster_list(), NOW.date(), said.append)
        self.assertEqual(found, {("at-p-p3", "кластер", 4041): {"A-1"},
                                 ("at-l-a3", "склад", 23021125185000): {"L-2"},
                                 ("at-l-a3", "кластер", 4072): {"L-2"}})
        self.assertEqual(api.list_calls[0], ACTIVE_ORDER_STATES)
        self.assertNotIn("CANCELLED", ACTIVE_ORDER_STATES)
        self.assertIn("«артикул + склад» — 1, «артикул + кластер» — 2", said[0])
        self.assertEqual(supply_key("at-l-a3", 4072, 23021125185000), ("at-l-a3", "склад", 23021125185000))
        self.assertEqual(supply_key("AT-L-A3", 4072, None), ("at-l-a3", "кластер", 4072))
        self.assertIn(supply_key("AT-L-A3", 4072, 23021125185000), found, "та же поставка на тот же склад — дубль")
        self.assertIn(supply_key("AT-L-A3", 4072, None), found, "поставка приложения в кластер с сегодняшней заявкой — дубль")
        self.assertNotIn(supply_key("AT-L-A3", 4072, 6048863967000), found, "другой склад того же кластера — не дубль")
        api.today_orders[505] = dict(api.today_orders[501], order_id=505, order_number="B-5",
                                     supplies=[{"supply_id": 6, "state": "DATA_FILLING", "bundle_id": "t1",
                                                "macrolocal_cluster_id": "4072",
                                                "storage_warehouse": {"warehouse_id": 23021125185000, "name": "ТВЕРЬ_РФЦ"}}])
        found = supplied_today(api, store, api.cluster_list(), NOW.date(), lambda s: None)
        self.assertEqual(found[("at-p-p3", "кластер", 4072)], {"B-5"})
        self.assertEqual(found[("at-p-p3", "склад", 23021125185000)], {"B-5"})
        api.today_orders[506] = dict(api.today_orders[501], order_id=506, order_number="133200663-1",
                                     supplies=[{"supply_id": 7, "state": "READY_TO_SUPPLY", "bundle_id": "t2",
                                                "macrolocal_cluster_id": None,
                                                "storage_warehouse": {"warehouse_id": 18044494830000, "name": "КАЗАНЬ_РФЦ_НОВЫЙ"}}])
        found = supplied_today(api, store, api.cluster_list(), NOW.date(), lambda s: None)
        self.assertEqual(found[("at-l-a3", "кластер", 4041)], {"133200663-1"})
        self.assertEqual(found[("at-l-a3", "склад", 18044494830000)], {"133200663-1"})
        api.today_orders[507] = dict(api.today_orders[501], order_id=507, order_number="N-7", supplies=[])
        store.set_article_state("лист", "AT-P-P3", {"article": "AT-P-P3", "orders": ["N-7"], "facts": {"Тверь": 10, "Казань": 0},
                                                    "cluster_ids": {"Тверь": 4072, "Казань": 4041}})
        store.set_article_state("лист", "AT-X", {"article": "AT-X", "orders": ["OLD-1"], "facts": {"Тверь": 10},
                                                 "cluster_ids": {"Тверь": 4072}})
        found = supplied_today(api, store, api.cluster_list(), NOW.date(), lambda s: None)
        self.assertEqual(found[("at-p-p3", "кластер", 4072)], {"B-5", "N-7"})
        self.assertNotIn(("at-x", "кластер", 4072), found, "заявка не из сегодняшних активных — не считается")

    def test_pipeline_excludes_cluster_with_any_order_today(self):
        api = api_with_today_orders()
        api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20\nAT-L-A3; Тверь; 10", mode="live")
        by = {r.article: r for r in out.results}
        self.assertEqual([(ln.cluster, ln.fact, ln.duplicate) for ln in by["AT-P-P3"].lines],
                         [("Тверь", 40, False), ("Казань", 0, True)])
        self.assertIn("на этот кластер: №A-1", by["AT-P-P3"].lines[1].note)
        self.assertTrue(by["AT-P-P3"].created)
        self.assertEqual(api.supply_calls[0][1], [4072], "заявка только по Твери, Казань исключена")
        self.assertTrue(by["AT-L-A3"].lines[0].duplicate)
        self.assertIn("по всем кластерам сегодня уже есть такие же заявки", by["AT-L-A3"].note)
        self.assertFalse(by["AT-L-A3"].created)
        self.assertEqual(len(api.supply_calls), 1)
        self.assertEqual(out.facts, {1: 40, 2: 0, 3: 0})
        self.assertTrue(out.notes[2].startswith("сегодня уже есть такая заявка на этот кластер: №A-1"), out.notes[2])
        self.assertTrue(out.notes[3].startswith("сегодня уже есть такая заявка на этот кластер: №L-2"), out.notes[3])
        self.assertTrue(any("Проверка дублей: заявок Ozon за сегодня — 2" in ln_ for ln_ in lines), lines)

    def test_unknown_warehouse_is_learned_via_direct_draft(self):
        class LearnApi(BatchApi):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.learn_calls = []

            def post(self, path, body, name):
                cid = body["cluster_info"]["macrolocal_cluster_id"]
                self.learn_calls.append((path, cid, body["cluster_info"]["items"][0]["quantity"]))
                return {"draft_id": 1000 + cid, "errors": []}

            def wait_draft(self, draft_id, name, tries=20):
                if draft_id >= 1000:
                    cid = draft_id - 1000
                    whs = ([{"warehouse_id": 999, "name": "ВЛАДИВОСТОК_РФЦ"}, {"warehouse_id": 23021125185000, "name": "ТВЕРЬ_РФЦ"}]
                           if cid == 4072 else [{"warehouse_id": 18044494830000, "name": "КАЗАНЬ_РФЦ_НОВЫЙ"}])
                    return {"status": "SUCCESS", "errors": [], "clusters": [{"macrolocal_cluster_id": cid, "supply_type": "DIRECT",
                            "warehouses": [{"bundle_id": f"d{w['warehouse_id']}", "storage_warehouse": w,
                                            "availability_status": {"state": "FULL_AVAILABLE", "invalid_reason": "UNSPECIFIED"}} for w in whs]}]}
                return super().wait_draft(draft_id, name, tries)

        api = api_with_today_orders(LearnApi)
        api.today_orders = {508: {"order_id": 508, "order_number": "L-8", "created_date": "2026-09-11T06:00:00Z", "state": "DATA_FILLING",
                                  "supplies": [{"supply_id": 8, "state": "DATA_FILLING", "bundle_id": "t8", "macrolocal_cluster_id": None,
                                                "storage_warehouse": {"warehouse_id": 999, "name": "ВЛАДИВОСТОК_РФЦ"}}]}}
        api.today_bundles = {"t8": [{"offer_id": "AT-P-P3", "sku": 1, "quantity": 50}]}
        api, out, lines, store = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live")
        self.assertEqual([(p, c) for p, c, _ in api.learn_calls], [("/v1/draft/direct/create", 4041), ("/v1/draft/direct/create", 4072)])
        self.assertTrue(all(q == 1 for _, _, q in api.learn_calls), "учебный черновик — 1 шт.")
        self.assertEqual(store.warehouses[999]["cluster_id"], 4072)
        self.assertEqual(set(store.clusters), {4041, 4072})
        res = out.results[0]
        self.assertEqual([(ln.cluster, ln.fact, ln.duplicate) for ln in res.lines], [("Тверь", 0, True), ("Казань", 20, False)])
        self.assertIn("№L-8", res.lines[0].note)
        self.assertEqual(api.supply_calls[0][1], [4041])
        self.assertTrue(any("Узнаю склады 2 кластеров" in ln_ for ln_ in lines), lines)

    def test_all_clusters_duplicated_today(self):
        api = api_with_today_orders()
        api.today_orders[501]["supplies"].append({"supply_id": 5, "state": "DATA_FILLING", "bundle_id": "t1",
                                                  "macrolocal_cluster_id": "4072"})
        api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40\nAT-P-P3; Казань; 20", mode="live")
        res = out.results[0]
        self.assertEqual([ln.duplicate for ln in res.lines], [True, True])
        self.assertIn("по всем кластерам сегодня уже есть такие же заявки", res.note)
        self.assertEqual(api.supply_calls, [])
        self.assertEqual(res.manual, [])

    def test_safeguard_and_trial_without_check(self):
        from datetime import date
        errs = validate_before_create("AT-P-P3", [SupplyLine("Казань", 20, 20, 20, duplicate=True)],
                                      date(2026, 9, 25), NOW, False)
        self.assertTrue(any("уже есть у Ozon" in e for e in errs), errs)
        self.assertEqual(validate_before_create("AT-P-P3", [SupplyLine("Казань", 20, 0, 0, duplicate=True)],
                                                date(2026, 9, 25), NOW, False), [])
        self.assertEqual(validate_before_create("AT-P-P3", [SupplyLine("Казань", 20, 20, 20, duplicate=True, dup_allowed=True)],
                                                date(2026, 9, 25), NOW, False), [])
        errs = validate_before_create("AT-P-P3", [SupplyLine("Казань", 20, 20, 15, duplicate=True, dup_allowed=True)],
                                      date(2026, 9, 25), NOW, False)
        self.assertEqual(len(errs), 1)
        self.assertIn("в черновике 15 шт., а факт 20", errs[0])

        class Broken(PipelineApi):
            def supply_order_ids(self, states, name):
                raise ApiError("/v3/supply-order/list", 500, {"message": "boom"})
        api, out, lines, _ = run_fake_pipeline(Broken({}, DAYS), "AT-P-P3; Тверь; 40")
        self.assertEqual([ln.fact for ln in out.results[0].lines], [40])
        self.assertTrue(any("Проверка дублей по заявкам Ozon не удалась" in ln_ for ln_ in lines))
        with self.assertRaises(ApiError):
            run_fake_pipeline(Broken({}, DAYS), "AT-P-P3; Тверь; 40", mode="live")


class SettingsInPipeline(unittest.TestCase):
    """Настройки раздела действуют на конвейер: точка сдачи и вместимость берутся из активного набора."""

    def test_drop_off_and_capacity_from_settings(self):
        class DropApi(PipelineApi):
            def drop_off_point(self, name):
                self.drop_off_asked = name
                return {"warehouse_id": 1, "warehouse_type": "DELIVERY_POINT"}

        api = DropApi({}, DAYS)
        settings, errs = parse_settings({"drop_off_name": "ТЕСТ_ТОЧКА", "default_cargo_capacity": 10})
        self.assertEqual(errs, [])
        with use_settings(settings):
            api, out, lines, _ = run_fake_pipeline(api, "AT-P-P3; Тверь; 40", mode="live")
        self.assertEqual(api.drop_off_asked, "ТЕСТ_ТОЧКА")
        self.assertEqual([(s, [cargo_qty(c) for c in cs]) for s, cs in api.cargo_calls], [(9072, [10, 10, 10, 10])])
        self.assertEqual(FboSettings().default_cargo_capacity, 50)


if __name__ == "__main__":
    unittest.main()
