"""Доступ к таблицам раздела «Поставки FBO» (ozon_fbo_*).

Только SQLite: настройки, память созданных заявок, грузоместа по заявкам, выученные склады кластеров,
прогоны с журналом, таблицей результатов, вопросом пользователю и флагом остановки.
Никаких вызовов Ozon здесь нет.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import get_connection

RUN_STATUSES = ("queued", "running", "awaiting_answer", "finished", "failed", "stopped", "interrupted")
ACTIVE_RUN_STATUSES = ("queued", "running", "awaiting_answer")
RUN_MODES = ("trial", "live", "live_nocargo", "cargo")
SOURCE_KINDS = ("file", "manual", "orders")
QUESTION_KINDS = ("plan", "confirm", "duplicates")
ANSWERS = ("yes", "no")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text: Any, default: Any) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- настройки

def get_settings_raw() -> dict[str, Any]:
    """Сохранённые значения настроек по ключам (пустой словарь — настройки по умолчанию)."""
    with get_connection() as conn:
        rows = conn.execute("SELECT key, value_json FROM ozon_fbo_settings").fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        out[str(row["key"])] = _loads(row["value_json"], None)
    return out


def save_settings_raw(values: dict[str, Any]) -> None:
    now = _now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO ozon_fbo_settings (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (str(key), _dumps(value), now),
            )


# ---------------------------------------------------------------- память по артикулам и заявкам

def get_article_state(sheet_name: str, article: str) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT state_json FROM ozon_fbo_article_state WHERE sheet_name=? AND article=?",
            (str(sheet_name), str(article)),
        ).fetchone()
    return _loads(row["state_json"], {}) if row else {}


def set_article_state(sheet_name: str, article: str, data: dict[str, Any]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ozon_fbo_article_state (sheet_name, article, state_json, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(sheet_name, article) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at
            """,
            (str(sheet_name), str(article), _dumps(data), _now()),
        )


def list_article_states() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT state_json FROM ozon_fbo_article_state ORDER BY updated_at").fetchall()
    return [_loads(row["state_json"], {}) for row in rows]


def set_order_state(order_number: str, data: dict[str, Any]) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO ozon_fbo_order_state (order_number, state_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(order_number) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at
            """,
            (str(order_number), _dumps(data), _now()),
        )


def get_order_state(order_number: str) -> dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT state_json FROM ozon_fbo_order_state WHERE order_number=?", (str(order_number),)
        ).fetchone()
    return _loads(row["state_json"], {}) if row else {}


def learned_warehouse_map() -> dict[int, int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT warehouse_id, cluster_id FROM ozon_fbo_warehouse_map").fetchall()
    return {int(row["warehouse_id"]): int(row["cluster_id"]) for row in rows}


def learned_clusters() -> set[int]:
    with get_connection() as conn:
        rows = conn.execute("SELECT cluster_id FROM ozon_fbo_learned_clusters").fetchall()
    return {int(row["cluster_id"]) for row in rows}


def save_learned_warehouses(cluster_id: int, cluster_name: str | None, warehouses: dict[int, str]) -> None:
    now = _now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for warehouse_id, name in warehouses.items():
            conn.execute(
                """
                INSERT INTO ozon_fbo_warehouse_map (warehouse_id, cluster_id, cluster_name, warehouse_name, learned_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(warehouse_id) DO UPDATE SET cluster_id=excluded.cluster_id, cluster_name=excluded.cluster_name,
                    warehouse_name=excluded.warehouse_name, learned_at=excluded.learned_at
                """,
                (int(warehouse_id), int(cluster_id), cluster_name, str(name or ""), now),
            )
        conn.execute(
            """
            INSERT INTO ozon_fbo_learned_clusters (cluster_id, learned_at) VALUES (?, ?)
            ON CONFLICT(cluster_id) DO UPDATE SET learned_at=excluded.learned_at
            """,
            (int(cluster_id), now),
        )


class RepositoryStateStore:
    """Хранилище состояния конвейера поверх таблиц ozon_fbo_* (реализует FboStateStore)."""

    def get_article_state(self, sheet: str, article: str) -> dict[str, Any]:
        return get_article_state(sheet, article)

    def set_article_state(self, sheet: str, article: str, data: dict[str, Any]) -> None:
        set_article_state(sheet, article, data)

    def list_article_states(self) -> list[dict[str, Any]]:
        return list_article_states()

    def set_order_state(self, number: str, data: dict[str, Any]) -> None:
        set_order_state(number, data)

    def learned_warehouse_map(self) -> dict[int, int]:
        return learned_warehouse_map()

    def learned_clusters(self) -> set[int]:
        return learned_clusters()

    def save_learned_warehouses(self, cluster_id: int, cluster_name: str | None, warehouses: dict[int, str]) -> None:
        save_learned_warehouses(cluster_id, cluster_name, warehouses)


# ---------------------------------------------------------------- прогоны

class RunConflictError(ValueError):
    """Уже идёт другой прогон — одновременно допускается только один."""


def _run_row(row: sqlite3.Row | None, *, with_files: bool = False) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    data = dict(row)
    data["options"] = _loads(data.pop("options_json", None), {})
    data["summary"] = _loads(data.pop("summary_json", None), {})
    data["is_active"] = bool(data.get("is_active"))
    data["stop_requested"] = bool(data.get("stop_requested"))
    data["has_input_file"] = bool(data.pop("has_input_file", 0))
    data["has_result_file"] = bool(data.pop("has_result_file", 0))
    data.pop("log_dir", None)   # серверный путь наружу не отдаётся; каталог читает get_run_log_dir
    if not with_files:
        data.pop("input_file", None)
        data.pop("result_file", None)
    return data


_RUN_COLUMNS = """
    id, created_at, started_at, finished_at, heartbeat_at, status, is_active, mode, source_kind, sheet_name,
    articles_filter, options_json, created_by_user_id, created_by_label, input_file_name, result_file_name,
    question_kind, question_text, question_seq, answer, stop_requested, summary_json, error_text, log_dir,
    (input_file IS NOT NULL) AS has_input_file, (result_file IS NOT NULL) AS has_result_file
"""


def create_run(*, mode: str, source_kind: str, sheet_name: str, articles_filter: str, options: dict[str, Any],
               created_by_user_id: int | None, created_by_label: str, input_file_name: str | None = None,
               input_file: bytes | None = None) -> dict[str, Any]:
    """Регистрирует прогон. Уникальный индекс по is_active гарантирует «не больше одного активного прогона»."""
    if mode not in RUN_MODES:
        raise ValueError(f"Неизвестный режим прогона: {mode}")
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"Неизвестный источник плана: {source_kind}")
    now = _now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """
                INSERT INTO ozon_fbo_runs (
                    created_at, status, is_active, mode, source_kind, sheet_name, articles_filter, options_json,
                    created_by_user_id, created_by_label, input_file_name, input_file, heartbeat_at
                ) VALUES (?, 'queued', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now, mode, source_kind, str(sheet_name or ""), str(articles_filter or ""), _dumps(options or {}),
                 created_by_user_id, str(created_by_label or ""), input_file_name, input_file, now),
            )
        except sqlite3.IntegrityError as exc:
            raise RunConflictError("Сейчас уже идёт другой прогон. Дождитесь его окончания.") from exc
        row = conn.execute(f"SELECT {_RUN_COLUMNS} FROM ozon_fbo_runs WHERE id=?", (cur.lastrowid,)).fetchone()
        return _run_row(row)  # type: ignore[return-value]


def get_run(run_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(f"SELECT {_RUN_COLUMNS} FROM ozon_fbo_runs WHERE id=?", (int(run_id),)).fetchone()
    return _run_row(row)


def get_run_log_dir(run_id: int) -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT log_dir FROM ozon_fbo_runs WHERE id=?", (int(run_id),)).fetchone()
    return str(row["log_dir"] or "") if row else ""


def get_run_file(run_id: int, kind: str) -> tuple[str, bytes] | None:
    column = {"input": "input_file", "result": "result_file"}[kind]
    name_column = {"input": "input_file_name", "result": "result_file_name"}[kind]
    with get_connection() as conn:
        row = conn.execute(f"SELECT {name_column} AS name, {column} AS data FROM ozon_fbo_runs WHERE id=?", (int(run_id),)).fetchone()
    if row is None or row["data"] is None:
        return None
    return str(row["name"] or ""), bytes(row["data"])


def get_active_run() -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(f"SELECT {_RUN_COLUMNS} FROM ozon_fbo_runs WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
    return _run_row(row)


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM ozon_fbo_runs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 200)),)
        ).fetchall()
    return [_run_row(row) for row in rows]  # type: ignore[misc]


def update_run(run_id: int, **fields: Any) -> None:
    allowed = {"status", "started_at", "finished_at", "heartbeat_at", "question_kind", "question_text", "answer",
               "stop_requested", "summary_json", "error_text", "log_dir", "result_file_name", "result_file", "is_active"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Недопустимые поля прогона: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{key}=?" for key in fields)
    with get_connection() as conn:
        conn.execute(f"UPDATE ozon_fbo_runs SET {assignments} WHERE id=?", (*fields.values(), int(run_id)))


def mark_run_started(run_id: int, log_dir: str) -> None:
    update_run(run_id, status="running", started_at=_now(), heartbeat_at=_now(), log_dir=log_dir)


def heartbeat_run(run_id: int) -> None:
    update_run(run_id, heartbeat_at=_now())


def finish_run(run_id: int, status: str, *, summary: dict[str, Any] | None = None, error_text: str = "",
               result_file_name: str | None = None, result_file: bytes | None = None) -> None:
    if status not in ("finished", "failed", "stopped", "interrupted"):
        raise ValueError(f"Недопустимый итоговый статус: {status}")
    fields: dict[str, Any] = dict(status=status, is_active=None, finished_at=_now(), heartbeat_at=_now(),
                                  summary_json=_dumps(summary or {}), error_text=str(error_text or ""),
                                  question_kind=None, question_text=None, answer=None)
    if result_file is not None:
        fields["result_file_name"] = result_file_name
        fields["result_file"] = result_file
    update_run(run_id, **fields)


def append_run_log(run_id: int, text: str) -> int:
    """Добавляет строку журнала с очередным seq (под BEGIN IMMEDIATE — seq уникален при конкуренции)."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM ozon_fbo_run_log WHERE run_id=?", (int(run_id),)).fetchone()
        seq = int(row["seq"])
        conn.execute(
            "INSERT INTO ozon_fbo_run_log (run_id, seq, text, created_at) VALUES (?, ?, ?, ?)",
            (int(run_id), seq, str(text), _now()),
        )
        conn.execute("UPDATE ozon_fbo_runs SET heartbeat_at=? WHERE id=?", (_now(), int(run_id)))
    return seq


def get_run_log(run_id: int, after_seq: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT seq, text FROM ozon_fbo_run_log WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
            (int(run_id), int(after_seq), max(1, min(int(limit), 5000))),
        ).fetchall()
    return [{"seq": int(row["seq"]), "text": str(row["text"])} for row in rows]


def get_run_log_text(run_id: int) -> str:
    with get_connection() as conn:
        rows = conn.execute("SELECT text FROM ozon_fbo_run_log WHERE run_id=? ORDER BY seq", (int(run_id),)).fetchall()
    return "\n".join(str(row["text"]) for row in rows)


def _insert_run_rows(conn: sqlite3.Connection, run_id: int, rows: list[dict[str, Any]]) -> None:
    row = conn.execute("SELECT COALESCE(MAX(position), 0) AS pos FROM ozon_fbo_run_rows WHERE run_id=?", (int(run_id),)).fetchone()
    position = int(row["pos"])
    for item in rows:
        position += 1
        conn.execute(
            """
            INSERT INTO ozon_fbo_run_rows (run_id, position, article, cluster, plan, fact, note, tone)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(run_id), position, str(item.get("article") or ""), str(item.get("cluster") or ""),
             int(item.get("plan") or 0), item.get("fact"), str(item.get("note") or ""), str(item.get("tone") or "none")),
        )


def add_run_rows(run_id: int, rows: list[dict[str, Any]]) -> None:
    """Добавляет строки таблицы результатов (по артикулу, в порядке расчёта)."""
    if not rows:
        return
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _insert_run_rows(conn, run_id, rows)


def replace_run_rows(run_id: int, rows: list[dict[str, Any]]) -> None:
    """Заменяет строки таблицы результатов одной транзакцией: читатели не видят пустую таблицу между шагами."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ozon_fbo_run_rows WHERE run_id=?", (int(run_id),))
        _insert_run_rows(conn, run_id, rows)


def get_run_rows(run_id: int) -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT position, article, cluster, plan, fact, note, tone FROM ozon_fbo_run_rows WHERE run_id=? ORDER BY position",
            (int(run_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def set_run_question(run_id: int, kind: str, text: str) -> int:
    """Ставит вопрос прогону и возвращает его порядковый номер (question_seq): ответ принимается только на него."""
    if kind not in QUESTION_KINDS:
        raise ValueError(f"Недопустимый вид вопроса: {kind}")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE ozon_fbo_runs SET status='awaiting_answer', question_kind=?, question_text=?, answer=NULL,
                question_seq=question_seq + 1, heartbeat_at=?
            WHERE id=? AND is_active=1
            """,
            (kind, str(text), _now(), int(run_id)),
        )
        row = conn.execute("SELECT question_seq FROM ozon_fbo_runs WHERE id=?", (int(run_id),)).fetchone()
        return int(row["question_seq"]) if row else 0


def answer_run(run_id: int, answer: str, question_seq: Optional[int] = None) -> bool:
    """Ответ пользователя на вопрос прогона. False — вопроса нет, прогон не активен или вопрос уже сменился
    (question_seq не совпал): устаревший ответ не может подтвердить другой вопрос."""
    if answer not in ANSWERS:
        raise ValueError("Ответ должен быть yes или no")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        sql = "UPDATE ozon_fbo_runs SET answer=? WHERE id=? AND is_active=1 AND question_kind IS NOT NULL AND answer IS NULL"
        params: list[Any] = [answer, int(run_id)]
        if question_seq is not None:
            sql += " AND question_seq=?"
            params.append(int(question_seq))
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def take_run_answer(run_id: int) -> Optional[str]:
    """Рабочий поток забирает ответ и снимает вопрос; None — ответа ещё нет или прогон уже не активен."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT answer FROM ozon_fbo_runs WHERE id=? AND is_active=1", (int(run_id),)).fetchone()
        if row is None or row["answer"] is None:
            return None
        conn.execute(
            "UPDATE ozon_fbo_runs SET status='running', question_kind=NULL, question_text=NULL, answer=NULL, heartbeat_at=? "
            "WHERE id=? AND is_active=1",
            (_now(), int(run_id)),
        )
        return str(row["answer"])


def request_stop(run_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("UPDATE ozon_fbo_runs SET stop_requested=1 WHERE id=? AND is_active=1", (int(run_id),))
        return cur.rowcount > 0


def is_stop_requested(run_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT stop_requested FROM ozon_fbo_runs WHERE id=?", (int(run_id),)).fetchone()
    return bool(row and row["stop_requested"])


def mark_stale_runs_interrupted(max_heartbeat_age_seconds: int, *, force_run_id: int | None = None) -> list[int]:
    """Активные прогоны без heartbeat дольше указанного срока (или заданный прогон) помечаются «прерван».
    Это forward recovery после гибели процесса: созданные заявки уже сохранены по каждому артикулу."""
    now = _now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if force_run_id is not None:
            rows = conn.execute("SELECT id FROM ozon_fbo_runs WHERE id=? AND is_active=1", (int(force_run_id),)).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id FROM ozon_fbo_runs
                WHERE is_active=1 AND (heartbeat_at IS NULL OR
                      (julianday(?) - julianday(heartbeat_at)) * 86400 > ?)
                """,
                (now, int(max_heartbeat_age_seconds)),
            ).fetchall()
        ids = [int(row["id"]) for row in rows]
        for run_id in ids:
            conn.execute(
                """
                UPDATE ozon_fbo_runs SET status='interrupted', is_active=NULL, finished_at=?, question_kind=NULL, question_text=NULL,
                    answer=NULL,
                    error_text=CASE WHEN error_text='' THEN 'Прогон прерван: рабочий процесс перестал отвечать.' ELSE error_text END
                WHERE id=?
                """,
                (now, run_id),
            )
        return ids


def clear_run_question(run_id: int) -> bool:
    """Снимает вопрос без ответа (остановка или истечение ожидания); прогон продолжает работать.
    False — ответ уже успел прийти: вопрос не снят, его заберёт take_run_answer."""
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE ozon_fbo_runs SET status='running', question_kind=NULL, question_text=NULL, heartbeat_at=? "
            "WHERE id=? AND is_active=1 AND answer IS NULL",
            (_now(), int(run_id)),
        )
        return cur.rowcount > 0
