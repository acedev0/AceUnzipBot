from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from acebot.database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def test_defaults_and_settings_round_trip(self) -> None:
        self.assertEqual(await self.db.get_setting("access_mode"), "owner")
        self.assertEqual(await self.db.get_setting("max_concurrent_jobs"), "1")
        await self.db.set_setting("access_mode", "private")
        await self.db.close()
        await self.db.open()
        self.assertEqual(await self.db.get_setting("access_mode"), "private")

    async def test_v11_default_settings_migrate_once(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, 'old')",
            [("access_mode", "private"), ("max_concurrent_jobs", "2")],
        )
        connection.commit()
        connection.close()

        legacy = Database(legacy_path)
        await legacy.open()
        self.assertEqual(await legacy.get_setting("access_mode"), "owner")
        self.assertEqual(await legacy.get_setting("max_concurrent_jobs"), "1")
        await legacy.set_setting("access_mode", "private")
        await legacy.set_setting("max_concurrent_jobs", "2")
        await legacy.close()
        await legacy.open()
        self.assertEqual(await legacy.get_setting("access_mode"), "private")
        self.assertEqual(await legacy.get_setting("max_concurrent_jobs"), "2")
        await legacy.close()

    async def test_v11_custom_public_and_concurrency_are_preserved(self) -> None:
        legacy_path = Path(self.temp.name) / "custom.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, 'old')",
            [("access_mode", "public"), ("max_concurrent_jobs", "2")],
        )
        connection.commit()
        connection.close()

        legacy = Database(legacy_path)
        await legacy.open()
        self.assertEqual(await legacy.get_setting("access_mode"), "public")
        self.assertEqual(await legacy.get_setting("max_concurrent_jobs"), "2")
        await legacy.close()

    async def test_user_flags_and_preferences(self) -> None:
        await self.db.upsert_user(42, "ace", "Ace")
        await self.db.set_user_flag(42, "approved", True)
        await self.db.set_user_setting(42, "upload_mode", "document")
        user = await self.db.get_user(42)
        settings = await self.db.get_user_settings(42)
        self.assertEqual(user["approved"], 1)
        self.assertEqual(settings["upload_mode"], "document")

    async def test_mirror_destinations_are_saved_and_recent_first(self) -> None:
        await self.db.save_mirror_destination(-100100, "Archive Channel", "archive", "channel")
        await self.db.save_mirror_destination(12345, "Alice", "alice", "user")
        destinations = await self.db.mirror_destinations()
        self.assertEqual(destinations[0]["target_id"], 12345)
        self.assertEqual(destinations[1]["label"], "Archive Channel")
        self.assertEqual(len(await self.db.mirror_destinations(limit=None)), 2)
        await self.db.delete_mirror_destination(12345)
        self.assertEqual(
            [item["target_id"] for item in await self.db.mirror_destinations(limit=None)],
            [-100100],
        )

    async def test_reachable_users_excludes_banned_accounts(self) -> None:
        await self.db.upsert_user(10, "allowed", "Allowed")
        await self.db.upsert_user(11, "blocked", "Blocked")
        await self.db.set_user_flag(11, "banned", True)
        self.assertEqual(
            [item["user_id"] for item in await self.db.reachable_users()],
            [10],
        )

    async def test_completed_job_updates_totals(self) -> None:
        await self.db.upsert_user(7, None, "User")
        await self.db.create_job("abcd1234", 7, "a.zip", 100)
        await self.db.finish_job(
            "abcd1234", status="completed", output_size=250, file_count=2, elapsed_seconds=1.5
        )
        stats = await self.db.global_stats()
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["bytes_in"], 100)
        self.assertEqual(stats["bytes_out"], 250)


if __name__ == "__main__":
    unittest.main()
