"""Поставки FBO: таблицы ozon_fbo_* создаются идемпотентно, настройки и память хранятся в БД,
активный прогон только один, вопрос/ответ/остановка и forward recovery работают через SQLite."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402

from app import db  # noqa: E402
from app import ozon_fbo_repository as fbo_repo  # noqa: E402
from app.services.ozon_fbo.config import FboSettings, parse_settings, settings_to_dict  # noqa: E402
from app.services.ozon_fbo.drafts import ArticleResult, ClusterLine  # noqa: E402
from app.services.ozon_fbo.store import load_state, save_state  # noqa: E402


class OzonFboSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._remove_test_runtime_files()
        db.init_db()

    def test_init_db_is_idempotent_and_creates_tables(self) -> None:
        db.init_db()
        with db.get_connection() as conn:
            names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        for table in ("ozon_fbo_settings", "ozon_fbo_runs", "ozon_fbo_run_log", "ozon_fbo_run_rows",
                      "ozon_fbo_article_state", "ozon_fbo_order_state", "ozon_fbo_warehouse_map", "ozon_fbo_learned_clusters"):
            self.assertIn(table, names)
        self.assertIn("idx_ozon_fbo_runs_single_active", indexes)

    def test_settings_roundtrip(self) -> None:
        self.assertEqual(fbo_repo.get_settings_raw(), {})
        settings, errs = parse_settings({"default_cargo_capacity": 40, "all_clusters": ["Тверь"], "cargo_capacity": {"AT-X": 7}})
        self.assertEqual(errs, [])
        fbo_repo.save_settings_raw(settings_to_dict(settings))
        loaded, errs = parse_settings(fbo_repo.get_settings_raw())
        self.assertEqual((errs, loaded), ([], settings))
        fbo_repo.save_settings_raw({"default_cargo_capacity": 45})
        loaded, _ = parse_settings(fbo_repo.get_settings_raw())
        self.assertEqual((loaded.default_cargo_capacity, loaded.all_clusters), (45, ["Тверь"]), "частичное сохранение не стирает остальное")
        self.assertEqual(FboSettings().default_cargo_capacity, 50)

    def test_state_store_persists_articles_orders_and_warehouses(self) -> None:
        store = fbo_repo.RepositoryStateStore()
        res = ArticleResult("AT-P-P3", 1, [ClusterLine(1, "Тверь", 4072, 40, fact=40)], orders=["N-1"], created=True,
                            order_details=[{"order_id": 5, "number": "N-1"}])
        save_state(store, "лист", res, "live")
        self.assertEqual(load_state(store, "лист", "AT-P-P3")["orders"], ["N-1"])
        self.assertEqual(load_state(store, "лист", "AT-X"), {})
        res2 = ArticleResult("AT-P-P3", 1, [ClusterLine(1, "Тверь", 4072, 40, fact=35)])
        save_state(store, "лист", res2, "trial")
        again = load_state(store, "лист", "AT-P-P3")
        self.assertEqual((again["orders"], again["facts"]), (["N-1"], {"Тверь": 35}), "номер прежней заявки сохранён")
        save_state(store, "лист", res2, "trial", keep_prev_orders=False)
        self.assertEqual(load_state(store, "лист", "AT-P-P3")["orders"], [])
        self.assertEqual([s["article"] for s in store.list_article_states()], ["AT-P-P3"])

        store.set_order_state("2000070409945", {"number": "2000070409945", "cargoes": {"1": {"cargo_ids": [7]}}})
        self.assertEqual(fbo_repo.get_order_state("2000070409945")["cargoes"]["1"]["cargo_ids"], [7])

        store.save_learned_warehouses(4072, "Тверь", {999: "ВЛАДИВОСТОК_РФЦ", 23021125185000: "ТВЕРЬ_РФЦ"})
        self.assertEqual(store.learned_warehouse_map(), {999: 4072, 23021125185000: 4072})
        self.assertEqual(store.learned_clusters(), {4072})
        store.save_learned_warehouses(4072, "Тверь", {999: "ВЛАДИВОСТОК_РФЦ"})
        self.assertEqual(store.learned_warehouse_map(), {999: 4072, 23021125185000: 4072}, "повтор не ломает карту")


class OzonFboRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._remove_test_runtime_files()
        db.init_db()

    def _create(self, **over):
        params = dict(mode="trial", source_kind="manual", sheet_name="ручной ввод", articles_filter="",
                      options={"force": False}, created_by_user_id=None, created_by_label="admin")
        params.update(over)
        return fbo_repo.create_run(**params)

    def test_single_active_run_enforced_by_database(self) -> None:
        run = self._create()
        self.assertEqual((run["status"], run["is_active"], run["mode"]), ("queued", True, "trial"))
        with self.assertRaises(fbo_repo.RunConflictError):
            self._create(mode="live")
        self.assertEqual(fbo_repo.get_active_run()["id"], run["id"])
        fbo_repo.finish_run(run["id"], "finished", summary={"created": 0})
        finished = fbo_repo.get_run(run["id"])
        self.assertEqual((finished["status"], finished["is_active"], finished["summary"]), ("finished", False, {"created": 0}))
        self.assertIsNone(fbo_repo.get_active_run())
        second = self._create(mode="cargo", source_kind="orders")
        self.assertEqual(second["status"], "queued")
        self.assertEqual([r["id"] for r in fbo_repo.list_runs()], [second["id"], run["id"]])

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._create(mode="unknown")
        with self.assertRaises(ValueError):
            fbo_repo.finish_run(1, "running")

    def test_log_rows_and_files(self) -> None:
        run = self._create(input_file_name="план.xlsx", input_file=b"PK\x03\x04")
        self.assertTrue(run["has_input_file"])
        self.assertNotIn("input_file", run, "бинарное содержимое не отдаётся в обычном ответе")
        self.assertEqual(fbo_repo.append_run_log(run["id"], "первая"), 1)
        self.assertEqual(fbo_repo.append_run_log(run["id"], "вторая"), 2)
        self.assertEqual(fbo_repo.get_run_log(run["id"], after_seq=1), [{"seq": 2, "text": "вторая"}])
        self.assertEqual(fbo_repo.get_run_log_text(run["id"]), "первая\nвторая")
        fbo_repo.add_run_rows(run["id"], [{"article": "AT-P-P3", "cluster": "Тверь", "plan": 40, "fact": 40, "note": "ok", "tone": "ok"}])
        fbo_repo.add_run_rows(run["id"], [{"article": "AT-P-P3", "cluster": "Казань", "plan": 20, "fact": 0, "note": "исключён", "tone": "zero"}])
        rows = fbo_repo.get_run_rows(run["id"])
        self.assertEqual([(r["position"], r["cluster"], r["fact"], r["tone"]) for r in rows], [(1, "Тверь", 40, "ok"), (2, "Казань", 0, "zero")])
        fbo_repo.replace_run_rows(run["id"], [{"article": "AT-P-P3", "cluster": "Тверь", "plan": 40, "fact": None, "note": "", "tone": "none"}])
        self.assertEqual([(r["position"], r["fact"]) for r in fbo_repo.get_run_rows(run["id"])], [(1, None)])
        self.assertEqual(fbo_repo.get_run_file(run["id"], "input"), ("план.xlsx", b"PK\x03\x04"))
        self.assertIsNone(fbo_repo.get_run_file(run["id"], "result"))
        fbo_repo.finish_run(run["id"], "finished", result_file_name="план_результат.xlsx", result_file=b"RESULT")
        self.assertEqual(fbo_repo.get_run_file(run["id"], "result"), ("план_результат.xlsx", b"RESULT"))
        self.assertTrue(fbo_repo.get_run(run["id"])["has_result_file"])

    def test_question_answer_and_stop(self) -> None:
        run = self._create(mode="live")
        fbo_repo.mark_run_started(run["id"], "logs/1")
        self.assertEqual(fbo_repo.get_run(run["id"])["status"], "running")
        self.assertIsNone(fbo_repo.take_run_answer(run["id"]), "без вопроса ответа нет")
        self.assertFalse(fbo_repo.answer_run(run["id"], "yes"), "ответ без вопроса отклоняется")
        seq = fbo_repo.set_run_question(run["id"], "plan", "ПЛАН ПОСТАВОК…")
        self.assertEqual(seq, 1)
        waiting = fbo_repo.get_run(run["id"])
        self.assertEqual((waiting["status"], waiting["question_kind"], waiting["question_text"], waiting["question_seq"]),
                         ("awaiting_answer", "plan", "ПЛАН ПОСТАВОК…", 1))
        self.assertIsNone(fbo_repo.take_run_answer(run["id"]))
        self.assertFalse(fbo_repo.answer_run(run["id"], "yes", question_seq=0), "ответ на уже сменившийся вопрос отклоняется")
        self.assertTrue(fbo_repo.answer_run(run["id"], "yes", question_seq=1))
        self.assertFalse(fbo_repo.answer_run(run["id"], "no"), "повторный ответ на тот же вопрос не принимается")
        self.assertEqual(fbo_repo.take_run_answer(run["id"]), "yes")
        after = fbo_repo.get_run(run["id"])
        self.assertEqual((after["status"], after["question_kind"], after["answer"]), ("running", None, None))
        with self.assertRaises(ValueError):
            fbo_repo.answer_run(run["id"], "maybe")
        self.assertFalse(fbo_repo.is_stop_requested(run["id"]))
        self.assertTrue(fbo_repo.request_stop(run["id"]))
        self.assertTrue(fbo_repo.is_stop_requested(run["id"]))
        fbo_repo.finish_run(run["id"], "stopped")
        self.assertFalse(fbo_repo.request_stop(run["id"]), "завершённый прогон остановить нельзя")

    def test_late_answer_is_not_discarded_by_clear(self) -> None:
        run = self._create()
        fbo_repo.mark_run_started(run["id"], "")
        fbo_repo.set_run_question(run["id"], "duplicates", "СЕГОДНЯ УЖЕ ЕСТЬ…")
        self.assertTrue(fbo_repo.clear_run_question(run["id"]), "без ответа вопрос снимается")
        self.assertEqual(fbo_repo.get_run(run["id"])["status"], "running")
        seq = fbo_repo.set_run_question(run["id"], "plan", "ПЛАН…")
        self.assertEqual(seq, 2, "номер вопроса растёт")
        self.assertTrue(fbo_repo.answer_run(run["id"], "no", question_seq=2))
        self.assertFalse(fbo_repo.clear_run_question(run["id"]), "ответ уже пришёл — вопрос не снимается")
        self.assertEqual(fbo_repo.take_run_answer(run["id"]), "no")

    def test_concurrent_create_run_allows_exactly_one(self) -> None:
        import threading
        results: list[object] = []
        barrier = threading.Barrier(4)

        def worker(index: int) -> None:
            barrier.wait()
            try:
                results.append(self._create(sheet_name=f"поток {index}")["id"])
            except fbo_repo.RunConflictError:
                results.append("conflict")
            except Exception as exc:  # noqa: BLE001 — любая другая ошибка это провал теста
                results.append(exc)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        created = [r for r in results if isinstance(r, int)]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(results.count("conflict"), 3, results)
        self.assertEqual(fbo_repo.get_active_run()["id"], created[0])

    def test_stale_runs_are_interrupted(self) -> None:
        run = self._create()
        fbo_repo.mark_run_started(run["id"], "")
        self.assertEqual(fbo_repo.mark_stale_runs_interrupted(120), [], "свежий heartbeat — прогон живой")
        fbo_repo.update_run(run["id"], heartbeat_at="2020-01-01 00:00:00")
        self.assertEqual(fbo_repo.mark_stale_runs_interrupted(120), [run["id"]])
        stale = fbo_repo.get_run(run["id"])
        self.assertEqual((stale["status"], stale["is_active"]), ("interrupted", False))
        self.assertIn("прерван", stale["error_text"])
        again = self._create()
        self.assertEqual(fbo_repo.mark_stale_runs_interrupted(120, force_run_id=again["id"]), [again["id"]])
        self.assertEqual(fbo_repo.get_run(again["id"])["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
