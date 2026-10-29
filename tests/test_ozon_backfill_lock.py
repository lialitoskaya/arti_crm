from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import test_regression_foundation as foundation


main = foundation.main
_FLAGS = (
    "OZON_EXCLUDE_SUPPORT_CHATS",
    "OZON_DELETE_SUPPORT_CHATS",
    "OZON_EXCLUDE_SYSTEM_HISTORY_CHATS",
    "OZON_DELETE_SYSTEM_HISTORY_CHATS",
)


class OzonBackfillLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.settings = {
            "sync_max_chats": 10,
            "sync_pages_per_variant": 2,
            "sync_variant_mode": "recent",
            "sync_include_closed": False,
            "history_pages": 1,
        }
        self.connector = SimpleNamespace(
            **self.settings, client_id="test-client", api_key="test-key", last_sync_debug={}
        )
        # Cover both existing flags and flags absent before the import.
        self.initial_environment = {_FLAGS[0]: "1", _FLAGS[1]: "0"}
        self.environment = dict(self.initial_environment)
        self.patches.enter_context(mock.patch.object(main, "connectors", {"ozon": self.connector}))
        self.patches.enter_context(mock.patch.object(
            main, "os", SimpleNamespace(environ=self.environment, getenv=self.environment.get)
        ))
        self.patches.enter_context(mock.patch.object(main.app.state, "marketplace_sync_locks", {}, create=True))
        self.patches.enter_context(mock.patch.object(main.app.state, "sync_lock", asyncio.Lock(), create=True))
        self.patches.enter_context(mock.patch.object(main.app.state, "last_sync", None, create=True))
        self.patches.enter_context(mock.patch.object(main, "_local_ozon_chat_stats", return_value={"total": 0}))
        self.sync = self.patches.enter_context(mock.patch.object(
            main, "_sync_marketplace_unlocked", new_callable=mock.AsyncMock,
            side_effect=AssertionError("An import must be mocked by the test"),
        ))
        self.fast_sync = self.patches.enter_context(mock.patch.object(
            main, "_sync_ozon_fast_inbox_unlocked", new_callable=mock.AsyncMock,
            side_effect=AssertionError("A fast import must be mocked by the test"),
        ))

    def _snapshot(self) -> tuple[dict, dict]:
        return (
            {name: getattr(self.connector, name) for name in self.settings},
            dict(self.environment),
        )

    def _assert_restored(self) -> None:
        self.assertEqual((self.settings, self.initial_environment), self._snapshot())

    async def _ordinary_sync(self, fast: bool) -> dict:
        if fast:
            return await main._sync_ozon_fast_inbox_locked(background=True)
        return await main._sync_marketplace_locked("ozon")

    def test_two_backfills_keep_their_own_settings_and_restore_the_originals(self) -> None:
        async def exercise():
            entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
            observations = []

            async def import_chats(marketplace, *, background=False):
                self.assertEqual("ozon", marketplace)
                self.assertFalse(background)
                observations.append(self._snapshot())
                self.connector.last_sync_debug = {"max_chats": self.connector.sync_max_chats}
                entered.set()
                await release.wait()
                return {"ok": True}

            self.sync.side_effect = import_chats
            first = asyncio.create_task(main.debug_ozon_backfill_chats(
                max_chats=111, pages_per_variant=11, history_pages=3
            ))
            await asyncio.wait_for(entered.wait(), 2)

            async def second_import():
                second_started.set()
                return await main.debug_ozon_backfill_chats(
                    max_chats=222, pages_per_variant=22, history_pages=4,
                    include_closed=False, include_service_chats=False,
                )

            second = asyncio.create_task(second_import())
            await asyncio.wait_for(second_started.wait(), 2)
            self.assertEqual(1, len(observations))
            self.assertEqual(observations[0], self._snapshot())
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), 2)
            expected_profiles = [
                dict(sync_max_chats=111, sync_pages_per_variant=11, sync_variant_mode="full",
                     sync_include_closed=True, history_pages=3),
                dict(sync_max_chats=222, sync_pages_per_variant=22, sync_variant_mode="full",
                     sync_include_closed=False, history_pages=4),
            ]
            self.assertEqual(expected_profiles, [settings for settings, _ in observations])
            self.assertEqual(dict.fromkeys(_FLAGS, "0"), observations[0][1])
            self.assertEqual(self.initial_environment, observations[1][1])
            for result, profile in zip(results, expected_profiles):
                self.assertTrue(result["backfill"])
                self.assertEqual(profile, result["backfill_overrides"])
                self.assertEqual(self.settings, result["previous_connector_settings"])
                self.assertEqual({"max_chats": profile["sync_max_chats"]}, result["connector_debug"])
            self._assert_restored()

        asyncio.run(exercise())

    async def _check_serialization_with_sync(self, *, fast: bool, backfill_first: bool) -> None:
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observations = []

        async def import_chats(marketplace="ozon", *, background=False):
            observations.append(self._snapshot())
            if len(observations) == 1:
                entered.set()
                await release.wait()
            return {"ok": True}

        self.sync.side_effect = import_chats
        self.fast_sync.side_effect = import_chats
        backfill = lambda: main.debug_ozon_backfill_chats(max_chats=111)
        ordinary = lambda: self._ordinary_sync(fast)
        first_call, second_call = (backfill, ordinary) if backfill_first else (ordinary, backfill)
        first = asyncio.create_task(first_call())
        await asyncio.wait_for(entered.wait(), 2)

        async def second_import():
            second_started.set()
            return await second_call()

        second = asyncio.create_task(second_import())
        await asyncio.wait_for(second_started.wait(), 2)
        self.assertEqual(1, len(observations))
        self.assertEqual(observations[0], self._snapshot())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        ordinary_index = 1 if backfill_first else 0
        self.assertEqual((self.settings, self.initial_environment), observations[ordinary_index])
        self.assertEqual(111, observations[1 - ordinary_index][0]["sync_max_chats"])
        self._assert_restored()

    def test_backfill_waits_for_manual_sync_before_overriding_settings(self) -> None:
        asyncio.run(self._check_serialization_with_sync(fast=False, backfill_first=False))

    def test_backfill_waits_for_fast_sync_before_overriding_settings(self) -> None:
        asyncio.run(self._check_serialization_with_sync(fast=True, backfill_first=False))

    def test_manual_sync_waits_for_backfill_to_restore_settings(self) -> None:
        asyncio.run(self._check_serialization_with_sync(fast=False, backfill_first=True))

    def test_fast_sync_waits_for_backfill_to_restore_settings(self) -> None:
        asyncio.run(self._check_serialization_with_sync(fast=True, backfill_first=True))

    def test_import_error_restores_settings_and_allows_the_next_import(self) -> None:
        async def exercise():
            self.sync.side_effect = RuntimeError("synthetic import failure")
            with self.assertRaises(main.HTTPException) as raised:
                await main.debug_ozon_backfill_chats()
            self.assertEqual(502, raised.exception.status_code)
            self._assert_restored()
            self.sync.side_effect = None
            self.sync.return_value = {"ok": True}
            result = await asyncio.wait_for(main.debug_ozon_backfill_chats(), 2)
            self.assertTrue(result["ok"])
            self._assert_restored()

        asyncio.run(exercise())

    def test_active_import_cancellation_restores_settings_and_releases_the_lock(self) -> None:
        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def import_chats(*args, **kwargs):
                entered.set()
                await release.wait()
                return {"ok": True}

            self.sync.side_effect = import_chats
            task = asyncio.create_task(main.debug_ozon_backfill_chats())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self._assert_restored()
            release.set()
            result = await asyncio.wait_for(main.debug_ozon_backfill_chats(), 2)
            self.assertTrue(result["ok"])
            self._assert_restored()

        asyncio.run(exercise())

    def test_cancelled_waiter_does_not_change_the_active_import_settings(self) -> None:
        async def exercise():
            entered, release, waiter_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def import_chats(*args, **kwargs):
                entered.set()
                await release.wait()
                return {"ok": True}

            self.sync.side_effect = import_chats
            active = asyncio.create_task(main.debug_ozon_backfill_chats(max_chats=111))
            await asyncio.wait_for(entered.wait(), 2)
            active_snapshot = self._snapshot()

            async def waiting_import():
                waiter_started.set()
                return await main.debug_ozon_backfill_chats(max_chats=222)

            waiter = asyncio.create_task(waiting_import())
            await asyncio.wait_for(waiter_started.wait(), 2)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(1, self.sync.await_count)
            self.assertEqual(active_snapshot, self._snapshot())
            release.set()
            await asyncio.wait_for(active, 2)
            self._assert_restored()

        asyncio.run(exercise())

    def test_backfill_does_not_hold_the_manual_or_other_marketplace_lock(self) -> None:
        async def exercise():
            entered, release = asyncio.Event(), asyncio.Event()

            async def import_chats(marketplace, *, background=False):
                if marketplace == "ozon":
                    entered.set()
                    await release.wait()
                return {"ok": True, "marketplace": marketplace}

            self.sync.side_effect = import_chats
            active = asyncio.create_task(main.debug_ozon_backfill_chats())
            await asyncio.wait_for(entered.wait(), 2)
            result = await asyncio.wait_for(main._sync_marketplace_locked("yandex"), 2)
            self.assertEqual("yandex", result["marketplace"])
            self.assertFalse(active.done())
            release.set()
            await asyncio.wait_for(active, 2)
            self._assert_restored()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
