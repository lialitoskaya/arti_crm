"""SQL regressions on synthetic in-memory data; no runtime config or live DB."""
import importlib.util
from pathlib import Path
import sqlite3
import sys
import types
import unittest
from contextlib import nullcontext
from unittest.mock import patch


def load_analytics():
    spec = importlib.util.spec_from_file_location(
        "analytics_under_test", Path(__file__).resolve().parents[1] / "app/services/analytics.py"
    )
    module = importlib.util.module_from_spec(spec)
    db_stub = types.ModuleType("app.db")
    db_stub.get_connection = lambda: None
    with patch.dict(sys.modules, {"app.db": db_stub}):
        spec.loader.exec_module(module)
    return module


analytics = load_analytics()


class AnalyticsQueryBoundsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE chats (id INTEGER PRIMARY KEY, marketplace TEXT,
                external_chat_id TEXT, status TEXT, customer_public_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, chat_id INTEGER,
                direction TEXT, created_at TEXT, author TEXT, raw_json TEXT);
            CREATE INDEX idx_messages_direction_created_at ON messages(direction, created_at);
            CREATE INDEX idx_messages_chat_id ON messages(chat_id);
        """)
        self.conn.executemany("INSERT INTO chats VALUES (?, 'ozon', 'fixture', 'open', '')",
                              [(1,), (2,), (3,)])
        self.conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, '', '{}')", [
            (1, 1, 'inbound', '2026-11-04T20:00:00Z'),
            (2, 1, 'inbound', '2026-11-04T22:00:00Z'),
            (3, 1, 'outbound', '2026-11-05T08:00:00Z'),
            (4, 2, 'inbound', '2026-11-04T22:00:00Z'),
            (5, 2, 'inbound', '2026-11-05T08:00:00Z'),
            (6, 2, 'outbound', '2026-11-07T08:00:00Z'),
            (7, 3, 'inbound', '2020-01-01T00:00:00Z'),
        ])
        self.kwargs = dict(modifier='+180 minutes', start='2026-11-05',
                           end='2026-11-05', marketplace=None)

    def test_daily_requests_timezone_boundary_and_deduplication(self):
        sql, params = analytics._daily_requests_cte_sql(**self.kwargs)
        rows = self.conn.execute(sql + ' SELECT id FROM daily_requests ORDER BY id', params).fetchall()
        self.assertEqual(rows, [(2,), (4,)])

    def test_response_blocks_keep_preperiod_start_and_future_answer(self):
        sql, params = analytics._response_blocks_cte_sql(**self.kwargs)
        rows = self.conn.execute(sql + ' SELECT chat_id, response_at, inbound_messages_in_block FROM response_blocks', params).fetchall()
        self.assertEqual(rows, [(2, '2026-11-07T08:00:00Z', 2)])

    def test_pruning_matches_unbounded_query_and_reduces_work(self):
        self.conn.executemany("INSERT INTO messages VALUES (?, 3, 'inbound', '2020-01-01T00:00:00Z', '', '{}')",
                              [(i,) for i in range(100, 10100)])
        sql, params = analytics._response_blocks_cte_sql(**self.kwargs)
        original = 'WITH ordered_messages AS (' + sql.split('ordered_messages AS (', 1)[1]
        original = original.replace('AND m.chat_id IN (SELECT chat_id FROM candidate_chats)', '')
        suffix = ' SELECT * FROM response_blocks ORDER BY chat_id, response_block_id'
        def measured(query, arguments):
            ticks = [0]
            def progress():
                ticks[0] += 1
                return 0
            self.conn.set_progress_handler(progress, 1000)
            try:
                return self.conn.execute(query + suffix, arguments).fetchall(), ticks[0]
            finally:
                self.conn.set_progress_handler(None, 0)
        before, before_ticks = measured(original, params[2:])
        after, after_ticks = measured(sql, params)
        self.assertEqual(after, before)
        self.assertLess(after_ticks, before_ticks / 5)

    def prepare_drilldown(self):
        self.conn.row_factory = sqlite3.Row
        for column in ('customer_name', 'order_id', 'assigned_to', 'assigned_user_id'):
            self.conn.execute(f'ALTER TABLE chats ADD COLUMN {column} TEXT')
        self.conn.execute("ALTER TABLE messages ADD COLUMN text TEXT DEFAULT 'Synthetic message'")
        self.conn.execute("INSERT INTO chats VALUES (4, 'yandex', 'other', 'open', '', '', '', '', '')")
        self.conn.executemany("INSERT INTO messages(id, chat_id, direction, created_at, author, raw_json) VALUES (?, ?, 'inbound', ?, ?, '{}')", [
            (20, 1, '2026-11-04T22:10:00Z', 'bot'),
            (21, 1, '2026-11-05T22:00:00Z', 'bot'),
            (22, 4, '2026-11-05T00:15:00+03:00', 'buyer'),
            (23, 4, '2026-11-05T10:00:00+03:00', 'support'),
        ])

    def test_drilldown_and_excluded_count_preserve_filters(self):
        self.prepare_drilldown()
        with patch.object(analytics, 'get_connection', lambda: nullcontext(self.conn)):
            for market, start_hour, end_hour, offset in [
                (None, None, None, 180), ('ozon', 0, 2, 180),
                ('yandex', 0, 12, 180), (None, None, None, -180),
            ]:
                with self.subTest(market=market, hours=(start_hour, end_hour), offset=offset):
                    args = dict(date_from='2026-11-05', date_to='2026-11-05', marketplace=market,
                                hour_from=start_hour, hour_to=end_hour, tz_offset_minutes=offset)
                    count_args = dict(self.kwargs, marketplace=market, hour_from=start_hour,
                                      hour_to=end_hour, modifier=analytics._tz_modifier(offset))
                    bounded = analytics.build_chat_analytics_drilldown(**args)
                    count = analytics._fetch_excluded_service_message_count(self.conn, **count_args)
                    with patch.object(analytics, '_coarse_created_at_bounds', return_value=('0000-00-00', '9999-12-31')):
                        self.assertEqual(bounded, analytics.build_chat_analytics_drilldown(**args))
                        self.assertEqual(count, analytics._fetch_excluded_service_message_count(self.conn, **count_args))
            result = analytics.build_chat_analytics_drilldown(date_from='2026-11-05', date_to='2026-11-05')
            self.assertEqual({row['message_id'] for row in result['items']}, {2, 4, 22})
            self.assertEqual({row['message_id'] for row in result['excluded_service_items']}, {20, 23})
            no_excluded = analytics.build_chat_analytics_drilldown(date_from='2026-11-05', date_to='2026-11-05', include_excluded=False, limit=1)
            self.assertEqual(len(no_excluded['items']), 1)
            self.assertEqual(no_excluded['excluded_service_items'], [])

    def test_historical_messages_do_not_dominate_drilldown_or_excluded_count(self):
        self.prepare_drilldown()
        self.conn.executemany("INSERT INTO messages(id, chat_id, direction, created_at, author, raw_json) VALUES (?, 3, 'inbound', '2020-01-01T00:00:00Z', ?, '{}')",
                              [(i, 'bot' if i % 2 else 'buyer') for i in range(100, 10100)])
        def measured(action):
            ticks = [0]
            def progress():
                ticks[0] += 1
                return 0
            self.conn.set_progress_handler(progress, 1000)
            try:
                return action(), ticks[0]
            finally:
                self.conn.set_progress_handler(None, 0)
        with patch.object(analytics, 'get_connection', lambda: nullcontext(self.conn)):
            for action in (
                lambda: analytics.build_chat_analytics_drilldown(date_from='2026-11-05', date_to='2026-11-05'),
                lambda: analytics._fetch_excluded_service_message_count(self.conn, **self.kwargs, hour_from=None, hour_to=None),
            ):
                with patch.object(analytics, '_coarse_created_at_bounds', return_value=('0000-00-00', '9999-12-31')):
                    before, old_ticks = measured(action)
                after, new_ticks = measured(action)
                self.assertEqual(after, before)
                self.assertLess(new_ticks, old_ticks / 5)


if __name__ == '__main__':
    unittest.main()
