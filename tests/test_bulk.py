from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.errors import FloodWaitError

from acebot.app import AceBot
from acebot.bulk import partition_bulk_entries
from acebot.models import BulkEntry, BulkSession, DeliveryTarget, PendingOwnerArchive
from acebot.progress import DeferredStatusMessage, ProgressReporter
from acebot.ui import render_bulk_summary


def source_event(name: str, size: int, message_id: int) -> SimpleNamespace:
    file = SimpleNamespace(name=name, size=size)
    message = SimpleNamespace(id=message_id, file=file)
    return SimpleNamespace(sender_id=1, chat_id=1, message=message, raw_text="")


class BulkValidationTests(unittest.TestCase):
    def test_partition_preserves_order_and_flags_unsupported_or_oversized(self) -> None:
        entries = (
            BulkEntry(source_event("first.zip", 10, 1), "first.zip", 10),
            BulkEntry(source_event("photo.jpg", 10, 2), "photo.jpg", 10),
            BulkEntry(source_event("second.7z", 20, 3), "second.7z", 20),
            BulkEntry(source_event("huge.rar", 101, 4), "huge.rar", 101),
        )
        accepted, rejected = partition_bulk_entries(entries, 100)
        self.assertEqual([item.filename for item in accepted], ["first.zip", "second.7z"])
        self.assertEqual(
            [(item.filename, reason) for item, reason in rejected],
            [
                ("photo.jpg", "unsupported format"),
                ("huge.rar", "larger than the owner’s per-archive limit"),
            ],
        )

    def test_summary_paginates_without_losing_any_filename(self) -> None:
        accepted = [(f"supported-{index:03d}.zip", 1024) for index in range(120)]
        rejected = [(f"ignored-{index:03d}.txt", "unsupported format") for index in range(45)]
        pages = render_bulk_summary(accepted, rejected)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 3900 for page in pages))
        combined = "\n".join(pages)
        for name, _ in accepted + rejected:
            self.assertIn(name, combined)


class BulkFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_collects_documents_without_starting_the_queue(self) -> None:
        status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        bot = AceBot.__new__(AceBot)
        bot.bulk_sessions = {}
        bot.is_authorized = AsyncMock(return_value=True)
        bot.reply_with_flood_wait = AsyncMock(return_value=status)
        bot.handle_archive = AsyncMock()
        command = SimpleNamespace(sender_id=1, chat_id=1, reply=AsyncMock())

        await bot.command_bulk(command)
        document = source_event("waiting.zip", 25, 10)
        await bot.collect_bulk_document(document, bot.bulk_sessions[1])

        self.assertEqual(
            [entry.filename for entry in bot.bulk_sessions[1].entries],
            ["waiting.zip"],
        )
        bot.handle_archive.assert_not_awaited()
        status.edit.assert_awaited_once()

    async def test_large_forward_burst_keeps_one_throttled_collection_card(self) -> None:
        status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        session = BulkSession(1, 1, status)
        bot = AceBot.__new__(AceBot)
        bot.bulk_sessions = {1: session}

        for index in range(100):
            await bot.collect_bulk_document(
                source_event(f"archive-{index:03d}.zip", 25, index + 1),
                session,
            )

        self.assertEqual(len(session.entries), 100)
        status.edit.assert_awaited_once()

    async def test_bulk_counter_does_not_retry_during_a_floodwait_window(self) -> None:
        status = SimpleNamespace(
            edit=AsyncMock(side_effect=FloodWaitError(30)),
            delete=AsyncMock(),
        )
        session = BulkSession(1, 1, status)
        bot = AceBot.__new__(AceBot)
        bot.bulk_sessions = {1: session}

        await bot.collect_bulk_document(source_event("one.zip", 25, 1), session)
        await bot.collect_bulk_document(source_event("two.zip", 25, 2), session)

        self.assertEqual(len(session.entries), 2)
        status.edit.assert_awaited_once()
        self.assertGreater(session.status_updates_blocked_until, session.last_status_edit_at)

    async def test_large_bulk_defers_every_queue_card_and_dispatches_once(self) -> None:
        bot = AceBot.__new__(AceBot)
        bot.handle_archive = AsyncMock(return_value=True)
        bot.dispatch_jobs = AsyncMock()
        entries = tuple(
            source_event(f"archive-{index:03d}.zip", 25, index + 1)
            for index in range(100)
        )
        targets = (DeliveryTarget(1, "This chat", "user"),)
        aggregate_status = SimpleNamespace(edit=AsyncMock())

        result = await bot.enqueue_archive_events(
            entries,
            targets,
            status_message=aggregate_status,
            aggregate_queue=True,
        )

        self.assertEqual(result, (100, 0))
        self.assertEqual(bot.handle_archive.await_count, 100)
        for call in bot.handle_archive.await_args_list:
            self.assertFalse(call.kwargs["dispatch"])
            self.assertTrue(call.kwargs["defer_queue_card"])
            self.assertIs(call.kwargs["flood_status_message"], aggregate_status)
        bot.dispatch_jobs.assert_awaited_once()

    async def test_deferred_bulk_progress_materializes_only_when_worker_starts(self) -> None:
        live_message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        aggregate_status = SimpleNamespace(edit=AsyncMock())
        event = source_event("waiting.zip", 25, 10)
        event.reply = AsyncMock()

        bot = AceBot.__new__(AceBot)
        bot.setting_int = AsyncMock(return_value=1900)
        bot.db = SimpleNamespace(
            create_job=AsyncMock(),
            get_setting=AsyncMock(return_value="0"),
            finish_job=AsyncMock(),
        )
        bot.client = SimpleNamespace(send_message=AsyncMock())
        bot.reply_with_flood_wait = AsyncMock()
        bot.send_message_with_flood_wait = AsyncMock(return_value=live_message)
        bot.log_archive_received = AsyncMock()
        bot.job_queue = []
        bot.queued_jobs_by_id = {}
        bot.queue_dispatch_lock = asyncio.Lock()

        created = await bot.handle_archive(
            event,
            dispatch=False,
            flood_status_message=aggregate_status,
            defer_queue_card=True,
        )

        self.assertTrue(created)
        event.reply.assert_not_awaited()
        bot.reply_with_flood_wait.assert_not_awaited()
        bot.send_message_with_flood_wait.assert_not_awaited()
        reporter = bot.job_queue[0].reporter
        self.assertIsInstance(reporter.message, DeferredStatusMessage)
        self.assertFalse(reporter.message.materialized)
        self.assertIs(
            bot.log_archive_received.await_args.kwargs["status_message"],
            aggregate_status,
        )

        await reporter.queued(100)
        bot.send_message_with_flood_wait.assert_not_awaited()

        await reporter.start("download", "Starting encrypted MTProto transfer")
        bot.send_message_with_flood_wait.assert_awaited_once()
        self.assertTrue(reporter.message.materialized)
        bot.client.send_message.assert_not_awaited()

    async def test_donebulk_queues_only_supported_entries_in_arrival_order(self) -> None:
        supported = source_event("one.zip", 10, 1)
        ignored = source_event("notes.txt", 5, 2)
        later = source_event("two.rar", 20, 3)
        status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        session = BulkSession(1, 1, status)
        session.entries.extend(
            [
                BulkEntry(later, "two.rar", 20),
                BulkEntry(supported, "one.zip", 10),
                BulkEntry(ignored, "notes.txt", 5),
            ]
        )
        bot = AceBot.__new__(AceBot)
        bot.config = SimpleNamespace(owner_id=999)
        bot.bulk_sessions = {1: session}
        bot.is_authorized = AsyncMock(return_value=True)
        bot.setting_int = AsyncMock(return_value=1900)
        sent_messages: list[SimpleNamespace] = []

        async def send_message(*args: object, **kwargs: object) -> SimpleNamespace:
            message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
            sent_messages.append(message)
            return message

        bot.send_message_with_flood_wait = send_message
        bot.enqueue_archive_events = AsyncMock(return_value=(2, 0))
        event = SimpleNamespace(sender_id=1, chat_id=1, reply=AsyncMock())

        await bot.command_donebulk(event)

        queued_events = bot.enqueue_archive_events.await_args.args[0]
        self.assertEqual(queued_events, (supported, later))
        self.assertNotIn(1, bot.bulk_sessions)
        status.delete.assert_awaited_once()
        self.assertGreaterEqual(len(sent_messages), 2)
        self.assertTrue(bot.enqueue_archive_events.await_args.kwargs["aggregate_queue"])

    async def test_owner_gets_one_delivery_choice_for_the_whole_batch(self) -> None:
        first = source_event("one.zip", 10, 1)
        second = source_event("two.7z", 20, 2)
        status = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        session = BulkSession(1, 1, status)
        session.entries.extend(
            [BulkEntry(first, "one.zip", 10), BulkEntry(second, "two.7z", 20)]
        )
        bot = AceBot.__new__(AceBot)
        bot.config = SimpleNamespace(owner_id=1)
        bot.bulk_sessions = {1: session}
        bot.owner_archive_choices = {}
        bot.is_authorized = AsyncMock(return_value=True)
        bot.setting_int = AsyncMock(return_value=1900)
        bot.enqueue_archive_events = AsyncMock()

        async def send_message(*args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())

        bot.send_message_with_flood_wait = send_message
        event = SimpleNamespace(sender_id=1, chat_id=1, reply=AsyncMock())

        await bot.command_donebulk(event)

        self.assertEqual(len(bot.owner_archive_choices), 1)
        request = next(iter(bot.owner_archive_choices.values()))
        self.assertTrue(request.is_bulk)
        self.assertEqual(request.source_events, (first, second))
        bot.enqueue_archive_events.assert_not_awaited()

    async def test_owner_bulk_route_uses_one_aggregate_queue_confirmation(self) -> None:
        prompt = SimpleNamespace(edit=AsyncMock())
        request = PendingOwnerArchive(
            "bulk1234",
            (source_event("one.zip", 10, 1), source_event("two.7z", 20, 2)),
            prompt,
            is_bulk=True,
        )
        bot = AceBot.__new__(AceBot)
        bot.config = SimpleNamespace(owner_id=1)
        bot.owner_archive_choices = {request.token: request}
        bot.enqueue_archive_events = AsyncMock(return_value=(2, 0))
        bot.edit_message_with_flood_wait = AsyncMock()

        await bot.start_owner_archive(request, "self", None)

        self.assertTrue(bot.enqueue_archive_events.await_args.kwargs["aggregate_queue"])
        confirmation = bot.edit_message_with_flood_wait.await_args.args[1]
        self.assertIn("ALL BULK ARCHIVES QUEUED", confirmation)
        self.assertIn("All <b>2</b> archives queued", confirmation)


class FloodWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_floodwait_retries_without_consuming_rpc_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mp4"
            path.touch()
            client = SimpleNamespace(
                send_file=AsyncMock(side_effect=[FloodWaitError(0), "sent"])
            )
            reporter = SimpleNamespace(flood_wait=AsyncMock(return_value=False))
            job = SimpleNamespace(cancel_event=asyncio.Event())
            bot = AceBot.__new__(AceBot)
            bot.client = client

            result = await bot.send_file_with_retry(1, path, {}, job, reporter)

            self.assertEqual(result, "sent")
            self.assertEqual(client.send_file.await_count, 2)
            reporter.flood_wait.assert_awaited_once()

    async def test_zero_second_countdown_renders_retry_card_without_sleep(self) -> None:
        message = SimpleNamespace(edit=AsyncMock())
        reporter = ProgressReporter(message, "deadbeef", "batch.zip")
        cancelled = await reporter.flood_wait(0, "uploading batch.zip")
        self.assertFalse(cancelled)
        rendered = message.edit.await_args.args[0]
        self.assertIn("TELEGRAM FLOODWAIT", rendered)
        self.assertIn("Retrying in", rendered)


if __name__ == "__main__":
    unittest.main()
