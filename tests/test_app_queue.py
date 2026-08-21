from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from acebot.app import AceBot
from acebot.models import DeliveryTarget, QueuedArchive


def queued(job_id: str, user_id: int) -> QueuedArchive:
    return QueuedArchive(
        job_id=job_id,
        user_id=user_id,
        chat_id=user_id,
        source_message=SimpleNamespace(),
        archive_name=f"{job_id}.zip",
        archive_size=100,
        delivery_targets=(DeliveryTarget(user_id, "This chat", "user"),),
        reporter=SimpleNamespace(),
    )


class QueueDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_never_starts_two_jobs_for_the_same_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = queued("first", 1)
            same_user = queued("second", 1)
            other_user = queued("third", 2)
            bot = AceBot.__new__(AceBot)
            bot.shutting_down = False
            bot.job_queue = [first, same_user, other_user]
            bot.queued_jobs_by_id = {
                item.job_id: item for item in bot.job_queue
            }
            bot.active_jobs = {}
            bot.jobs_by_id = {}
            bot.queue_dispatch_lock = asyncio.Lock()
            bot.running_slots = 0
            bot.config = SimpleNamespace(work_dir=Path(directory))
            bot.setting_int = AsyncMock(return_value=2)
            bot.required_disk_bytes = AsyncMock(return_value=1)
            bot.reserve_disk = AsyncMock(return_value=True)
            bot.release_disk = AsyncMock()
            bot.refresh_queue_statuses = AsyncMock()

            async def fake_wrapper(*args: object) -> None:
                return None

            def fake_spawn(coroutine: object) -> None:
                coroutine.close()
                return None

            bot.job_wrapper = fake_wrapper
            bot.spawn = fake_spawn

            await bot.dispatch_jobs()

            self.assertEqual(bot.running_slots, 2)
            self.assertEqual(bot.active_jobs[1].job_id, "first")
            self.assertEqual(bot.active_jobs[2].job_id, "third")
            self.assertEqual([item.job_id for item in bot.job_queue], ["second"])
            self.assertEqual(set(bot.queued_jobs_by_id), {"second"})
            self.assertFalse(first.is_queued)
            self.assertTrue(same_user.is_queued)
            self.assertFalse(other_user.is_queued)


if __name__ == "__main__":
    unittest.main()
