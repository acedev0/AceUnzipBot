from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from acebot.media import (
    is_media_validation_error,
    normalize_profile_image,
    normalize_thumbnail,
    probe_photo,
    upload_fallback_variants,
)
from acebot.models import DeliveryTarget
from acebot.progress import PROGRESS_WIDTH, ProgressReporter, pipeline_text
from acebot.ui import (
    admin_access_buttons,
    admin_appearance_buttons,
    admin_home_buttons,
    admin_limits_buttons,
    admin_logs_buttons,
    home_buttons,
    mirror_destination_buttons,
    owner_delivery_buttons,
    progress_bar,
    render_caption,
    render_help,
    render_privacy,
    render_video_caption,
    render_welcome,
    user_settings_buttons,
    validate_caption_template,
)


class UiTests(unittest.TestCase):
    def test_start_caption_stays_within_telegram_media_limit(self) -> None:
        caption = render_welcome("Max", "Ace Extractor", "x" * 300, archives_logged=True)
        self.assertLessEqual(len(caption), 1024)

    def test_primary_ui_copy_is_compact_and_bullet_based(self) -> None:
        welcome = render_welcome("Ace", "Ace Extractor", "Send a file.", archives_logged=False)
        help_text = render_help("Send supported archives.")
        privacy = render_privacy(
            archives_logged=False,
            cleanup_minutes=15,
            logging_configured=False,
        )
        self.assertIn("• Flow:", welcome)
        self.assertIn("• Single:", help_text)
        self.assertIn("• Cleanup:", privacy)
        self.assertNotIn("Three-stage turbo flow", welcome)
        self.assertLessEqual(welcome.count("\n"), 12)

    def test_caption_template_is_validated_and_escaped(self) -> None:
        self.assertTrue(validate_caption_template("{name} • {index}/{total}")[0])
        self.assertFalse(validate_caption_template("{unknown}")[0])
        caption = render_caption(
            "<b>{name}</b>",
            name="<script>.mp4",
            path="x",
            size="1 MB",
            index=1,
            total=1,
            bot="Ace",
        )
        self.assertIn("&lt;b&gt;", caption)
        self.assertNotIn("<script>", caption)

    def test_video_caption_is_only_the_safe_literal_filename(self) -> None:
        self.assertEqual(render_video_caption("<movie>.mp4"), "&lt;movie&gt;.mp4")

    def test_all_callback_payloads_fit_telegram_limit(self) -> None:
        keyboards = [
            home_buttons(owner=True),
            user_settings_buttons("auto", True, True),
            admin_home_buttons(),
            admin_access_buttons("private", False),
            admin_appearance_buttons(),
            admin_logs_buttons(True, True),
            admin_limits_buttons(),
            owner_delivery_buttons("1234abcd"),
            mirror_destination_buttons(
                "1234abcd",
                "both",
                [DeliveryTarget(-1001234567890, "Archive Channel", "channel")],
            ),
        ]
        for keyboard in keyboards:
            for row in keyboard:
                for button in row:
                    data = getattr(button, "data", None)
                    if data is not None:
                        self.assertLessEqual(len(data), 64)

    def test_mirror_destination_picker_is_paginated_without_manual_entry(self) -> None:
        destinations = [
            DeliveryTarget(index + 1000, f"User {index}", "user") for index in range(19)
        ]
        keyboard = mirror_destination_buttons("1234abcd", "mirror", destinations, page=1)
        labels = [str(getattr(button, "text", "")) for row in keyboard for button in row]
        payloads = [
            getattr(button, "data", b"") for row in keyboard for button in row
        ]
        self.assertTrue(any("2/3" in label for label in labels))
        self.assertFalse(any(b"enter" in payload.lower() for payload in payloads))
        target_payloads = [payload for payload in payloads if b"mirror:target:" in payload]
        self.assertEqual(len(target_payloads), 8)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_thumbnail_is_jpeg_small_and_within_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            destination = Path(directory) / "thumb.jpg"
            image = Image.effect_noise((1200, 800), 80).convert("RGB")
            image.save(source)
            await normalize_thumbnail(source, destination)
            self.assertLessEqual(destination.stat().st_size, 20_000)
            with Image.open(destination) as result:
                self.assertEqual(result.format, "JPEG")
                self.assertLessEqual(result.width, 320)
                self.assertLessEqual(result.height, 320)

    async def test_profile_picture_is_square_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "profile.png"
            destination = Path(directory) / "profile.jpg"
            Image.new("RGB", (1400, 900), "navy").save(source)
            await normalize_profile_image(source, destination)
            with Image.open(destination) as result:
                self.assertEqual(result.format, "JPEG")
                self.assertEqual(result.width, result.height)
                self.assertLessEqual(result.width, 1024)

    async def test_photo_probe_rejects_fake_images_and_accepts_real_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.png"
            fake = Path(directory) / "fake.png"
            Image.new("RGB", (32, 32), "blue").save(real)
            fake.write_bytes(b"not-an-image")
            self.assertTrue(await probe_photo(real))
            self.assertFalse(await probe_photo(fake))

    def test_media_validation_fallback_ends_as_plain_document(self) -> None:
        marker = object()
        variants = upload_fallback_variants(
            {
                "caption": "movie.mp4",
                "thumb": "thumb.jpg",
                "force_document": False,
                "supports_streaming": True,
                "attributes": ["video"],
                "progress_callback": marker,
            }
        )
        self.assertEqual(len(variants), 3)
        self.assertNotIn("thumb", variants[1])
        self.assertTrue(variants[-1]["force_document"])
        self.assertEqual(variants[-1]["mime_type"], "application/octet-stream")
        self.assertNotIn("attributes", variants[-1])
        self.assertIs(variants[-1]["progress_callback"], marker)
        self.assertTrue(is_media_validation_error(Exception("MEDIA_FILE_INVALID")))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_card_is_compact_for_telegram_phone_width(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.text = ""

            async def edit(self, text: str, **kwargs: object) -> None:
                self.text = text

        message = FakeMessage()
        reporter = ProgressReporter(
            message,
            "deadbeef",
            "a-very-long-archive-name-that-needs-to-fit-on-a-phone-screen.zip",
            interval=0,
        )
        reporter.set_stage("upload")
        await reporter.update(50, 100, detail="File 2/10 • nested/folder/movie.mp4")

        pipeline, _ = pipeline_text("upload")
        self.assertLessEqual(len(pipeline), 32)
        self.assertEqual(len(progress_bar(50, 100)), PROGRESS_WIDTH)
        self.assertIn(progress_bar(50, 100), message.text)
        self.assertNotIn("ACE LIVE TASK", message.text)
        self.assertIn("<b>UPLOAD</b>", message.text)
        self.assertIn("• File:", message.text)
        self.assertIn("• ETA:", message.text)

    async def test_completion_reports_skipped_empty_files_without_failure(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.text = ""

            async def edit(self, text: str, **kwargs: object) -> None:
                self.text = text

        message = FakeMessage()
        reporter = ProgressReporter(message, "deadbeef", "archive.zip")
        reporter.skipped_empty_files = 1
        await reporter.complete(10, 1, 2)
        self.assertIn("DELIVERY COMPLETE WITH NOTES", message.text)
        self.assertIn("1 zero-byte file skipped", message.text)

    async def test_relocate_keeps_one_progress_message_below_new_files(self) -> None:
        class FakeMessage:
            def __init__(self) -> None:
                self.deleted = False
                self.text = ""

            async def edit(self, text: str, **kwargs: object) -> None:
                self.text = text

            async def delete(self) -> None:
                self.deleted = True

        class FakeClient:
            def __init__(self) -> None:
                self.sent: list[FakeMessage] = []

            async def send_message(self, chat_id: int, text: str, **kwargs: object) -> FakeMessage:
                message = FakeMessage()
                message.text = text
                self.sent.append(message)
                return message

        old_message = FakeMessage()
        client = FakeClient()
        reporter = ProgressReporter(
            old_message,
            "deadbeef",
            "movie.zip",
            client=client,
            chat_id=123,
            interval=0,
        )
        await reporter.update(50, 100, detail="Uploading")
        await reporter.relocate()
        self.assertTrue(old_message.deleted)
        self.assertEqual(len(client.sent), 1)
        self.assertIs(reporter.message, client.sent[0])


if __name__ == "__main__":
    unittest.main()
