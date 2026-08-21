from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from acebot.app import AceBot
from acebot.archives import ArchiveError
from acebot.media import VideoInfo
from acebot.models import ActiveJob, DeliveryTarget


class FakeReporter:
    def __init__(self) -> None:
        self.stage = "queued"
        self.updates: list[tuple[int, int, str]] = []
        self.relocations = 0

    def set_stage(self, stage: str) -> None:
        self.stage = stage

    async def update(
        self,
        current: int,
        total: int,
        *,
        detail: str = "",
        force: bool = False,
    ) -> None:
        self.updates.append((current, total, detail))

    async def relocate(self) -> None:
        self.relocations += 1


class FakeDatabase:
    def __init__(self, thumbnail_path: str | None = None, mode: str = "auto") -> None:
        self.thumbnail_path = thumbnail_path
        self.mode = mode

    async def get_user_settings(self, user_id: int) -> dict[str, str | None]:
        return {
            "upload_mode": self.mode,
            "caption_template": None,
            "thumbnail_path": self.thumbnail_path,
        }

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        return default


def make_job(workspace: Path) -> ActiveJob:
    return ActiveJob(
        job_id="deadbeef",
        user_id=1,
        chat_id=1,
        source_message=SimpleNamespace(id=99),
        archive_name="archive.zip",
        workspace=workspace,
        delivery_targets=(DeliveryTarget(1, "This chat", "user"),),
        cancel_event=asyncio.Event(),
    )


def make_bot(data_dir: Path, *, thumbnail_path: str | None = None, mode: str = "auto") -> AceBot:
    bot = AceBot.__new__(AceBot)
    bot.config = SimpleNamespace(
        data_dir=data_dir,
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
    )
    bot.db = FakeDatabase(thumbnail_path, mode)
    bot.me = SimpleNamespace(first_name="Ace Extractor")
    bot.send_file_with_retry = AsyncMock(return_value=SimpleNamespace(media=object()))
    bot.copy_sent_media_with_retry = AsyncMock()
    return bot


class UploadPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_byte_members_are_skipped_without_dropping_other_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            extracted = workspace / "extracted"
            extracted.mkdir()
            ordinary = extracted / "program.custom-extension"
            binary = extracted / "blob.bin"
            empty = extracted / "placeholder.txt"
            ordinary.write_bytes(b"custom")
            binary.write_bytes(b"binary")
            empty.touch()

            bot = AceBot.__new__(AceBot)
            prepared, skipped = await bot.prepare_upload_files(
                (ordinary, empty, binary), extracted, workspace
            )

            self.assertEqual(prepared, (ordinary, binary))
            self.assertEqual(skipped, 1)
            self.assertFalse((workspace / "empty-files.zip").exists())

    async def test_unknown_file_type_is_plain_document_without_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            extracted = workspace / "extracted"
            extracted.mkdir()
            path = extracted / "payload.unknown-type"
            path.write_bytes(b"payload")
            custom_thumb = workspace / "custom.jpg"
            Image.new("RGB", (32, 32), "red").save(custom_thumb)

            bot = make_bot(workspace, thumbnail_path=str(custom_thumb))
            reporter = FakeReporter()
            summary = await bot.upload_files(
                make_job(workspace), reporter, extracted, (path,)
            )

            self.assertEqual(summary, (len(b"payload"), 1, 0, 0))
            kwargs = bot.send_file_with_retry.await_args.args[2]
            self.assertTrue(kwargs["force_document"])
            self.assertNotIn("thumb", kwargs)
            self.assertNotIn("attributes", kwargs)
            self.assertNotIn("supports_streaming", kwargs)

    async def test_verified_photo_is_media_but_does_not_receive_video_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            extracted = workspace / "extracted"
            extracted.mkdir()
            path = extracted / "photo.png"
            Image.new("RGB", (64, 64), "blue").save(path)
            custom_thumb = workspace / "custom.jpg"
            Image.new("RGB", (32, 32), "red").save(custom_thumb)

            bot = make_bot(workspace, thumbnail_path=str(custom_thumb))
            reporter = FakeReporter()
            await bot.upload_files(make_job(workspace), reporter, extracted, (path,))

            kwargs = bot.send_file_with_retry.await_args.args[2]
            self.assertFalse(kwargs["force_document"])
            self.assertNotIn("thumb", kwargs)

    async def test_verified_streamable_video_uses_media_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            extracted = workspace / "extracted"
            extracted.mkdir()
            path = extracted / "clip.mp4"
            path.write_bytes(b"video")
            custom_thumb = workspace / "custom.jpg"
            Image.new("RGB", (32, 32), "red").save(custom_thumb)

            bot = make_bot(workspace, thumbnail_path=str(custom_thumb))
            reporter = FakeReporter()
            with patch(
                "acebot.app.probe_video",
                AsyncMock(return_value=VideoInfo(duration=4, width=1280, height=720)),
            ):
                await bot.upload_files(make_job(workspace), reporter, extracted, (path,))

            kwargs = bot.send_file_with_retry.await_args.args[2]
            self.assertFalse(kwargs["force_document"])
            self.assertEqual(kwargs["thumb"], str(custom_thumb))
            self.assertTrue(kwargs["supports_streaming"])
            self.assertIn("attributes", kwargs)

    async def test_failed_file_does_not_cancel_remaining_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            extracted = workspace / "extracted"
            extracted.mkdir()
            first = extracted / "first.bin"
            second = extracted / "second.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            bot = make_bot(workspace)
            bot.send_file_with_retry = AsyncMock(
                side_effect=[
                    ArchiveError("Telegram rejected first.bin"),
                    SimpleNamespace(media=object()),
                ]
            )
            reporter = FakeReporter()
            summary = await bot.upload_files(
                make_job(workspace), reporter, extracted, (first, second)
            )

            self.assertEqual(bot.send_file_with_retry.await_count, 2)
            self.assertEqual(summary, (second.stat().st_size, 1, 1, 1))
            self.assertTrue(any("Skipped 1/2" in detail for _, _, detail in reporter.updates))
            self.assertTrue(any("Delivered 2/2" in detail for _, _, detail in reporter.updates))


if __name__ == "__main__":
    unittest.main()
