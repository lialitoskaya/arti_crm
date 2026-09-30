from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402

repo = foundation.repo
db = foundation.db


@pytest.fixture
def notification_db(tmp_path, monkeypatch):
    database_path = (tmp_path / "notifications.sqlite3").resolve()
    monkeypatch.setattr(foundation, "_DATABASE_PATH", database_path)
    monkeypatch.setattr(db, "DATABASE_PATH", database_path)
    db.init_db()
    with db.get_connection() as conn:
        conn.executemany(
            "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, 'test-only', 'manager')",
            [(101, "notification-test-one"), (102, "notification-test-two")],
        )
    monkeypatch.setattr(repo, "_utcnow", lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc))
    return database_path


def _insert(created_at: str, *, user_id: int = 101, kind: str = "task_assigned", read: bool = False) -> int:
    with db.get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO notifications (user_id, type, title, created_at, is_read) VALUES (?, ?, 'Fixture', ?, ?)",
            (user_id, kind, created_at, int(read)),
        )
        return int(cursor.lastrowid)


def _rows() -> list[tuple]:
    with db.get_connection() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM notifications ORDER BY id")]


@pytest.mark.parametrize("kind", ["new_message", "new_question", "task_assigned", "task_updated", "event"])
def test_day_scope_all_types_bounds_offsets_and_full_count(notification_db, kind):
    included = {
        _insert("2026-09-26T21:00:00Z", kind=kind),
        _insert("2026-09-27T00:00:00+03:00", kind=kind),
        _insert("2026-09-27 12:00:00", kind=kind),
        _insert("2026-09-27T23:59:59.999999+03:00", kind=kind),
        _insert("2026-09-26T17:00:00-04:00", kind=kind),
    }
    read_id = _insert("2026-09-27T11:00:00Z", kind=kind, read=True)
    for timestamp in (
        "2026-09-26T20:59:59.999999Z", "2026-09-20T12:00:00Z",
        "2026-09-27T21:00:00Z", "2026-10-01T12:00:00Z", "not-a-date", "",
    ):
        _insert(timestamp, kind=kind)
    _insert("2026-09-27T12:00:00Z", user_id=102, kind=kind)
    before = _rows()

    result = repo.list_notifications(101, limit=100)
    assert {row["id"] for row in result["items"]} == included | {read_id}
    assert result["unread_count"] == len(included)
    unread = repo.list_notifications(101, unread_only=True)
    assert {row["id"] for row in unread["items"]} == included
    limited = repo.list_notifications(101, limit=1)
    assert len(limited["items"]) == 1
    assert limited["unread_count"] == len(included)
    assert repo.list_notifications(102)["unread_count"] == 1
    assert _rows() == before


@pytest.mark.parametrize("midnight", [
    datetime(2026, 9, 27, 21, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 21, tzinfo=timezone.utc),
    datetime(2026, 12, 31, 21, tzinfo=timezone.utc),
])
def test_next_poll_rolls_day_at_moscow_midnight(notification_db, monkeypatch, midnight):
    old = _insert((midnight - timedelta(microseconds=1)).isoformat())
    new = _insert(midnight.isoformat())
    before = _rows()
    monkeypatch.setattr(repo, "_utcnow", lambda: midnight - timedelta(microseconds=1))
    assert [row["id"] for row in repo.list_notifications(101)["items"]] == [old]
    monkeypatch.setattr(repo, "_utcnow", lambda: midnight)
    result = repo.list_notifications(101)
    assert [row["id"] for row in result["items"]] == [new]
    assert result["unread_count"] == 1
    assert _rows() == before


def test_list_samples_clock_once_for_items_and_count(notification_db, monkeypatch):
    today = _insert("2026-09-27T20:59:59Z")
    _insert("2026-09-27T21:00:00Z")
    calls = []

    def clock():
        calls.append(True)
        return datetime(2026, 9, 27, 20 if len(calls) == 1 else 21, tzinfo=timezone.utc)

    monkeypatch.setattr(repo, "_utcnow", clock)
    result = repo.list_notifications(101)
    assert [row["id"] for row in result["items"]] == [today]
    assert result["unread_count"] == 1
    assert len(calls) == 1


def test_read_all_only_today_for_current_user_preserves_history(notification_db):
    yesterday = _insert("2026-09-26T20:59:59Z")
    today = _insert("2026-09-26T21:00:00Z")
    future = _insert("2026-09-27T21:00:00Z")
    other_user = _insert("2026-09-27T11:00:00Z", user_id=102)
    read_id = _insert("2026-09-27T11:00:00Z", read=True)
    malformed = _insert("invalid")
    before = {row[0]: row for row in _rows()}
    assert repo.mark_all_notifications_read(101) == 1
    assert repo.mark_all_notifications_read(101) == 0
    after = {row[0]: row for row in _rows()}
    assert after.keys() == before.keys()
    for identity in (yesterday, future, other_user, read_id, malformed):
        assert after[identity] == before[identity]
    assert after[today] != before[today]
    assert repo.list_notifications(101)["unread_count"] == 0
    assert not repo.mark_notification_read(yesterday, 102)
    assert repo.mark_notification_read(yesterday, 101)


def test_read_all_covers_today_beyond_feed_limit_without_reading_history_or_other_user(notification_db):
    today_ids = {_insert("2026-09-27T12:00:00Z") for _ in range(35)}
    yesterday = _insert("2026-09-26T20:59:59Z")
    other_user = _insert("2026-09-27T12:00:00Z", user_id=102)
    before = {row[0]: row for row in _rows()}

    feed = repo.list_notifications(101)
    visible_ids = {row["id"] for row in feed["items"]}
    assert len(visible_ids) == 30
    assert len(today_ids - visible_ids) == 5
    assert feed["unread_count"] == 35

    assert repo.mark_all_notifications_read(101) == 35
    assert repo.mark_all_notifications_read(101) == 0
    with db.get_connection() as conn:
        read_ids = {row["id"] for row in conn.execute("SELECT id FROM notifications WHERE is_read=1")}
    assert read_ids == today_ids
    after = {row[0]: row for row in _rows()}
    assert after.keys() == before.keys()
    assert after[yesterday] == before[yesterday]
    assert after[other_user] == before[other_user]
    assert repo.list_notifications(101, unread_only=True) == {"items": [], "unread_count": 0}
    assert repo.list_notifications(102)["unread_count"] == 1


def test_day_scope_and_midnight_preserve_notification_and_push_deduplication(notification_db, monkeypatch):
    notification = {
        "user_id": 101,
        "type": "task_assigned",
        "title": "Idempotent fixture",
        "dedupe_key": "fixture:notification-day-scope:user:101",
    }
    notification_id = repo.create_notification(**notification)
    assert notification_id is not None
    with db.get_connection() as conn:
        conn.execute("UPDATE notifications SET created_at='2026-09-27T12:00:00Z' WHERE id=?", (notification_id,))
        push_before = [tuple(row) for row in conn.execute("SELECT * FROM push_outbox ORDER BY id")]
    assert len(push_before) == 1
    assert repo.create_notification(**notification) is None
    assert [row["id"] for row in repo.list_notifications(101)["items"]] == [notification_id]
    assert repo.mark_all_notifications_read(101) == 1
    assert repo.create_notification(**notification) is None
    notifications_before = _rows()

    monkeypatch.setattr(repo, "_utcnow", lambda: datetime(2026, 9, 27, 21, tzinfo=timezone.utc))
    assert repo.list_notifications(101) == {"items": [], "unread_count": 0}
    assert repo.mark_all_notifications_read(101) == 0
    assert repo.create_notification(**notification) is None
    assert _rows() == notifications_before
    assert len(notifications_before) == 1
    with db.get_connection() as conn:
        push_after = [tuple(row) for row in conn.execute("SELECT * FROM push_outbox ORDER BY id")]
    assert push_after == push_before
    assert [row["notification_id"] for row in repo.get_pending_push_outbox()] == [notification_id]


def test_feed_and_read_all_do_not_expire_or_rewrite_push_outbox(notification_db):
    old = repo.create_notification(user_id=101, type="task_assigned", title="Old task", task_id=None)
    today = repo.create_notification(user_id=101, type="new_question", title="New question", entity_type="question", entity_id="fixture-1")
    with db.get_connection() as conn:
        conn.execute("UPDATE notifications SET created_at='2026-09-25T12:00:00Z' WHERE id=?", (old,))
        conn.execute("UPDATE notifications SET created_at='2026-09-27T12:00:00Z' WHERE id=?", (today,))
    pending = repo.get_pending_push_outbox()
    assert {row["notification_id"] for row in pending} == {old, today}
    retry_id = next(row["id"] for row in pending if row["notification_id"] == old)
    repo.mark_push_outbox_failed(retry_id, "synthetic timeout")
    with db.get_connection() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM push_outbox ORDER BY id")]
    repo.list_notifications(101)
    repo.mark_all_notifications_read(101)
    repo.list_notifications(101)
    with db.get_connection() as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM push_outbox ORDER BY id")]
    assert after == before
    pending_after = repo.get_pending_push_outbox()
    assert [row["notification_id"] for row in pending_after] == [today]
    assert pending_after[0]["payload"] == next(row["payload"] for row in pending if row["notification_id"] == today)


def test_creation_recency_rule_remains_rolling_24_hours(notification_db, monkeypatch):
    monkeypatch.setenv("CRM_NOTIFICATION_RECENT_MESSAGE_HOURS", "24")
    now = datetime.now(timezone.utc)
    assert repo._message_recent_enough_for_notification((now - timedelta(hours=23)).isoformat())
    assert not repo._message_recent_enough_for_notification((now - timedelta(hours=25)).isoformat())
