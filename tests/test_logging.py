from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.errors import RPCError

from acebot.app import AceBot
from acebot.models import DeliveryTarget


class SettingsDatabase:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


def make_bot(*, configured: int | None = None) -> AceBot:
    bot = AceBot.__new__(AceBot)
    bot.config = SimpleNamespace(log_channel_id=configured)
    bot.db = SettingsDatabase()
    bot.client = SimpleNamespace(send_message=AsyncMock(), send_file=AsyncMock())
    bot.disabled_log_channel_id = None
    bot.setting_bool = AsyncMock(return_value=True)
    bot.wait_flood_message = AsyncMock()
    return bot


class OptionalLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_channel_is_a_clean_noop(self) -> None:
        bot = make_bot()
        await bot.log_event("hello")
        self.assertIsNone(await bot.get_log_channel())
        self.assertFalse(await bot.archive_logging_enabled())
        bot.client.send_message.assert_not_awaited()

    async def test_database_can_disable_environment_default(self) -> None:
        bot = make_bot(configured=-100123)
        bot.db.values["log_channel_id"] = "disabled"
        self.assertIsNone(await bot.configured_log_channel())

    async def test_unreachable_channel_is_disabled_for_current_run(self) -> None:
        bot = make_bot(configured=-100123)
        error = RPCError(None, "CHANNEL_INVALID", 400)
        bot.client.send_message.side_effect = error

        await bot.log_event("first")
        await bot.log_event("second")

        self.assertEqual(bot.disabled_log_channel_id, -100123)
        self.assertEqual(bot.client.send_message.await_count, 1)

    async def test_invalid_archive_media_falls_back_to_metadata(self) -> None:
        bot = make_bot(configured=-100123)
        bot.client.send_file.side_effect = RPCError(None, "MEDIA_FILE_INVALID", 400)
        event = SimpleNamespace(
            sender_id=42,
            message=SimpleNamespace(media=object()),
            get_sender=AsyncMock(
                return_value=SimpleNamespace(first_name="Ada", username="ada")
            ),
        )

        await bot.log_archive_received(
            event,
            "abc123",
            "files.zip",
            512,
            (DeliveryTarget(42, "You, Sir", "user"),),
        )

        bot.client.send_message.assert_awaited_once()
        self.assertIn("original archive copy was unavailable", bot.client.send_message.await_args.args[1])
        self.assertIsNone(bot.disabled_log_channel_id)

    async def test_archive_and_metadata_failure_disables_channel(self) -> None:
        bot = make_bot(configured=-100123)
        error = RPCError(None, "CHANNEL_PRIVATE", 400)
        bot.client.send_file.side_effect = error
        bot.client.send_message.side_effect = error
        event = SimpleNamespace(
            sender_id=42,
            message=SimpleNamespace(media=object()),
            get_sender=AsyncMock(
                return_value=SimpleNamespace(first_name="Ada", username=None)
            ),
        )

        await bot.log_archive_received(
            event,
            "abc123",
            "files.zip",
            512,
            (DeliveryTarget(42, "You, Sir", "user"),),
        )

        self.assertEqual(bot.disabled_log_channel_id, -100123)


if __name__ == "__main__":
    unittest.main()
