from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import platform
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import telethon
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.utils import get_peer_id

from . import __version__
from .archives import (
    ArchiveEngine,
    ArchiveError,
    ArchiveLimits,
    JobCancelled,
    PasswordRequired,
    WrongPassword,
    cleanup_workspace,
    looks_like_archive,
    password_from_caption,
    safe_filename,
)
from .bot_api import BotApi, BotApiError
from .bulk import partition_bulk_entries
from .config import Config
from .database import DEFAULT_SETTINGS, Database
from .media import (
    IMAGE_SUFFIXES,
    copy_brand_thumbnail,
    generate_video_thumbnail,
    is_media_validation_error,
    is_streamable,
    is_video,
    normalize_profile_image,
    normalize_start_image,
    normalize_thumbnail,
    probe_photo,
    probe_video,
    split_oversized,
    upload_fallback_variants,
    video_attributes,
)
from .models import (
    ActiveJob,
    BulkEntry,
    BulkSession,
    DeliveryTarget,
    PendingInput,
    PendingOwnerArchive,
    QueuedArchive,
)
from .progress import DeferredStatusMessage, ProgressReporter
from .queueing import next_eligible_index
from .ui import (
    BRAND_NAME,
    FOOTER,
    admin_access_buttons,
    admin_appearance_buttons,
    admin_home_buttons,
    admin_limits_buttons,
    admin_logs_buttons,
    back_home_button,
    esc,
    home_buttons,
    human_duration,
    human_size,
    mirror_destination_buttons,
    owner_delivery_buttons,
    progress_bar,
    render_bulk_summary,
    render_caption,
    render_help,
    render_privacy,
    render_video_caption,
    render_welcome,
    timestamp,
    user_settings_buttons,
    validate_caption_template,
)

LOGGER = logging.getLogger("acebot")
MAX_TELEGRAM_PART = 1_950_000_000
PASSWORD_WAIT_SECONDS = 300


class DeliveryError(ValueError):
    pass


class AceBot:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.database_path)
        self.client = TelegramClient(
            str(config.session_path),
            config.api_id,
            config.api_hash,
            auto_reconnect=True,
            connection_retries=10,
            retry_delay=2,
            request_retries=5,
            flood_sleep_threshold=60,
            sequential_updates=False,
        )
        self.client.parse_mode = "html"
        self.bot_api = BotApi(config.bot_token)
        self.archive_engine = ArchiveEngine(config.seven_zip)
        self.active_jobs: dict[int, ActiveJob] = {}
        self.jobs_by_id: dict[str, ActiveJob] = {}
        self.job_queue: list[QueuedArchive] = []
        self.queued_jobs_by_id: dict[str, QueuedArchive] = {}
        self.bulk_sessions: dict[int, BulkSession] = {}
        self.pending_inputs: dict[int, PendingInput] = {}
        self.owner_archive_choices: dict[str, PendingOwnerArchive] = {}
        self.mirror_destination_cache: tuple[DeliveryTarget, ...] = ()
        self.mirror_destination_cached_at = 0.0
        self.disabled_log_channel_id: int | None = None
        self.peer_registry_seen_at: dict[int, float] = {}
        self.broadcast_drafts: dict[int, str] = {}
        self.access_requests: dict[int, float] = {}
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.queue_dispatch_lock = asyncio.Lock()
        self.running_slots = 0
        self.disk_reservation_lock = asyncio.Lock()
        self.reserved_disk_bytes = 0
        self.me: Any = None
        self.started_at = time.monotonic()
        self.exit_code = 0
        self.shutting_down = False

        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self.on_callback, events.CallbackQuery())
        self.client.add_event_handler(self.on_chat_action, events.ChatAction())

    def spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def run(self) -> int:
        await self.db.open()
        if self.config.log_channel_id is not None:
            current = await self.db.get_setting("log_channel_id")
            if current is None:
                await self.db.set_setting("log_channel_id", str(self.config.log_channel_id))
        await self.cleanup_stale_workspaces(force=True)
        await self.prepare_brand_assets()

        LOGGER.info("Connecting to Telegram with Telethon %s", telethon.__version__)
        await self.client.start(bot_token=self.config.bot_token)
        self.me = await self.client.get_me()
        LOGGER.info("Ace Extractor online as @%s", self.me.username)

        try:
            await self.bot_api.install_commands()
        except BotApiError as exc:
            LOGGER.warning("Could not install BotFather command list: %s", exc)
        await self.initialize_bot_profile()

        self.spawn(self.heartbeat_loop())
        self.spawn(self.cleanup_loop())
        await self.log_event(
            "🚀 <b>BOT ONLINE</b>\n\n"
            f"🤖 @{esc(self.me.username or 'unknown')}\n"
            f"🧩 Ace Extractor v{__version__}\n"
            f"🐍 Python {esc(platform.python_version())}\n"
            f"📡 Telethon {esc(telethon.__version__)} + cryptg\n"
            f"🖥 {esc(platform.node() or 'VPS')}\n"
            f"🕒 {timestamp()}"
        )

        try:
            await self.client.run_until_disconnected()
        finally:
            self.shutting_down = True
            for job in list(self.active_jobs.values()):
                job.cancel_event.set()
                if job.task:
                    job.task.cancel()
            if self.active_jobs:
                await asyncio.gather(
                    *(job.task for job in self.active_jobs.values() if job.task),
                    return_exceptions=True,
                )
            for queued in list(self.job_queue):
                await self.db.finish_job(
                    queued.job_id,
                    status="interrupted",
                    error="Bot service stopped before this queued task started.",
                )
            self.job_queue.clear()
            self.queued_jobs_by_id.clear()
            self.bulk_sessions.clear()
            for task in list(self.background_tasks):
                task.cancel()
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
            await self.db.close()
        return self.exit_code

    async def initialize_bot_profile(self) -> None:
        if await self.db.get_setting("profile_initialized") != "1":
            try:
                await self.bot_api.set_name("Ace Extractor")
                await self.bot_api.set_short_description(
                    "Ultra-fast archive extraction with live progress. Made by Ace • @Aceddev"
                )
                await self.bot_api.set_description(
                    "• Secure archive extraction\n"
                    "• Live progress + FIFO bulk queue\n"
                    "• Passwords + video thumbnails\n"
                    "• Private owner controls\n\n"
                    "Made by Ace • @Aceddev\nAcedevmc@gmail.com"
                )
                self.me.first_name = "Ace Extractor"
                await self.db.set_setting("profile_initialized", "1")
            except BotApiError as exc:
                LOGGER.warning("Could not initialize Telegram profile text: %s", exc)

        if not await self.db.get_setting("profile_photo_asset_version"):
            profile_path = self.config.data_dir / "brand" / "profile.jpg"
            try:
                await self.bot_api.set_profile_photo(profile_path)
                await self.db.set_setting("profile_photo_asset_version", "bundled-v1")
            except BotApiError as exc:
                LOGGER.warning("Could not automatically apply the bundled profile picture: %s", exc)

    async def prepare_brand_assets(self) -> None:
        brand_dir = self.config.data_dir / "brand"
        brand_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        default_thumb = brand_dir / "default_thumb.jpg"
        if not default_thumb.exists():
            await copy_brand_thumbnail(self.config.assets_dir / "bot_profile.png", default_thumb)
        profile_photo = brand_dir / "profile.jpg"
        if not profile_photo.exists():
            await normalize_profile_image(self.config.assets_dir / "bot_profile.png", profile_photo)

    async def heartbeat_loop(self) -> None:
        while True:
            self.config.heartbeat_path.write_text(str(time.time()), encoding="utf-8")
            await asyncio.sleep(10)

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            await self.cleanup_stale_workspaces(force=False)
            await self.cleanup_stale_bulk_sessions()

    async def cleanup_stale_bulk_sessions(self) -> int:
        cutoff = time.monotonic() - 3600
        expired: list[BulkSession] = []
        for user_id, session in list(self.bulk_sessions.items()):
            if session.created_at >= cutoff:
                continue
            async with session.lock:
                if self.bulk_sessions.get(user_id) is not session:
                    continue
                self.bulk_sessions.pop(user_id, None)
                session.closed = True
                expired.append(session)
        for session in expired:
            with contextlib.suppress(RPCError):
                await session.status_message.edit(
                    "⌛ <b>BULK EXPIRED</b>\n\n"
                    "• Unsubmitted list: discarded after 1 hour\n"
                    "• Restart: <code>/bulk</code>",
                    buttons=None,
                )
        return len(expired)

    async def cleanup_stale_workspaces(self, *, force: bool) -> int:
        active_paths = {job.workspace.resolve() for job in self.active_jobs.values()}
        cleanup_minutes = await self.setting_int("cleanup_minutes") if self.db._connection else 15
        cutoff = time.time() - cleanup_minutes * 60

        def _clean() -> int:
            removed = 0
            self.config.work_dir.mkdir(parents=True, exist_ok=True)
            for child in self.config.work_dir.iterdir():
                if not child.is_dir() or child.resolve() in active_paths:
                    continue
                try:
                    stale = child.stat().st_mtime < cutoff
                except FileNotFoundError:
                    continue
                if force or stale:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            return removed

        return await asyncio.to_thread(_clean)

    async def on_message(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            await self.remember_accessible_chat(event)
            return
        if event.sender_id is None:
            return
        sender = await event.get_sender()
        if sender is None or getattr(sender, "bot", False):
            return
        user_id = int(event.sender_id)
        await self.db.upsert_user(user_id, getattr(sender, "username", None), sender.first_name)

        text = (event.raw_text or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        command_handlers = {
            "/start": self.command_start,
            "/help": self.command_help,
            "/settings": self.command_settings,
            "/privacy": self.command_privacy,
            "/myid": self.command_myid,
            "/bulk": self.command_bulk,
            "/donebulk": self.command_donebulk,
            "/cancelbulk": self.command_cancelbulk,
            "/cancel": self.command_cancel,
            "/password": self.command_password,
            "/admin": self.command_admin,
        }
        if command in command_handlers:
            if command not in {"/password", "/cancel"}:
                self.pending_inputs.pop(user_id, None)
            await command_handlers[command](event)
            return

        if not await self.is_authorized(user_id):
            await self.send_access_denied(event)
            return

        bulk_session = self.bulk_sessions.get(user_id)
        if bulk_session is not None:
            if event.message.document:
                await self.collect_bulk_document(event, bulk_session)
                return
            if event.photo:
                await event.reply(
                    "⚠️ Bulk mode accepts Telegram <b>files/documents</b>. Send images with "
                    "the paperclip’s File option, or finish with <code>/donebulk</code>."
                )
                return
            if text:
                await event.reply(
                    f"📚 Bulk collection is active with <b>{len(bulk_session.entries)}</b> "
                    "document(s). Send more files, <code>/donebulk</code>, or "
                    "<code>/cancelbulk</code>."
                )
                return

        pending = self.pending_inputs.get(user_id)
        if (
            pending
            and (event.photo or event.message.document)
            and await self.handle_pending_media(event, pending)
        ):
            return
        if pending and text:
            await self.handle_pending_text(event, pending, text)
            return
        if event.message.document:
            if user_id == self.config.owner_id:
                await self.offer_owner_delivery(event)
            else:
                await self.handle_archive(event)
            return
        if event.photo:
            await event.reply(
                "Use <code>/settings</code> to save a photo as your upload thumbnail."
            )
            return
        if text:
            await event.reply(
                "Send an archive as a <b>document</b>, or open the controls below.",
                buttons=home_buttons(owner=user_id == self.config.owner_id),
            )

    async def on_callback(self, event: events.CallbackQuery.Event) -> None:
        if event.sender_id is None:
            return
        if not event.is_private:
            await self.remember_accessible_chat(event)
        await event.answer()
        user_id = int(event.sender_id)
        data = event.data.decode("utf-8", "replace")
        if data in {"home:start", "user:settings", "admin:home"}:
            self.pending_inputs.pop(user_id, None)
        sender = await event.get_sender()
        await self.db.upsert_user(
            user_id, getattr(sender, "username", None), getattr(sender, "first_name", None)
        )

        if data == "access:request":
            await self.request_access(event)
            return
        if data.startswith("access:"):
            if user_id != self.config.owner_id:
                return
            await self.handle_access_decision(event, data)
            return
        if data.startswith("job:cancel:"):
            await self.cancel_job_callback(event, data)
            return
        if data.startswith("mirror:"):
            if user_id != self.config.owner_id:
                return
            await self.handle_owner_delivery_callback(event, data)
            return
        if not await self.is_authorized(user_id):
            await self.edit_access_denied(event)
            return

        if data.startswith("home:"):
            await self.handle_home_callback(event, data)
        elif data.startswith("user:"):
            await self.handle_user_callback(event, data)
        elif data.startswith("admin:"):
            if user_id != self.config.owner_id:
                await self.safe_edit(event, "Owner access only." + FOOTER, back_home_button())
                return
            await self.handle_admin_callback(event, data)

    async def on_chat_action(self, event: events.ChatAction.Event) -> None:
        if self.me is None or event.chat_id is None:
            return
        removed = bool(
            getattr(event, "user_kicked", False) or getattr(event, "user_left", False)
        )
        affected_ids = set(getattr(event, "user_ids", None) or ())
        affected_user = getattr(event, "user_id", None)
        if affected_user is not None:
            affected_ids.add(affected_user)
        if removed and int(self.me.id) in affected_ids:
            await self.db.delete_mirror_destination(int(event.chat_id))
            self.peer_registry_seen_at.pop(int(event.chat_id), None)
            self.mirror_destination_cached_at = 0.0
            return
        await self.remember_accessible_chat(event, force=True)

    async def remember_accessible_chat(self, event: Any, *, force: bool = False) -> None:
        """Remember group/channel peers delivered through Telegram's bot update stream."""
        if self.me is None:
            return
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        now = time.monotonic()
        if (
            not force
            and chat_id in self.peer_registry_seen_at
            and now - self.peer_registry_seen_at[chat_id] < 300
        ):
            return
        try:
            entity = await event.get_chat()
            await self.delivery_target_from_entity(
                entity,
                persist=True,
            )
            if chat_id:
                self.peer_registry_seen_at[chat_id] = now
            self.mirror_destination_cached_at = 0.0
        except (DeliveryError, RPCError, ValueError, TypeError, OSError):
            return

    async def is_authorized(self, user_id: int) -> bool:
        if user_id == self.config.owner_id:
            return True
        user = await self.db.get_user(user_id)
        if user and user["banned"]:
            return False
        is_admin = bool(user and user["is_admin"])
        if await self.setting_bool("maintenance"):
            return is_admin
        mode = await self.db.get_setting("access_mode", "owner")
        if mode == "public":
            return True
        if mode == "owner":
            return False
        return bool(user and (user["approved"] or user["is_admin"]))

    async def send_access_denied(self, event: events.NewMessage.Event) -> None:
        mode = await self.db.get_setting("access_mode", "owner")
        text = (
            "🔒 <b>PRIVATE BOT</b>\n\n"
            "• Access: owner-selected IDs\n"
            "• Use the button to request access"
            if mode == "private"
            else "🔒 <b>OWNER ONLY</b>\n\n• Access: closed"
        )
        buttons = (
            [[Button.inline("🔔 Request access", b"access:request")]] if mode == "private" else None
        )
        await event.reply(text + FOOTER, buttons=buttons)

    async def edit_access_denied(self, event: events.CallbackQuery.Event) -> None:
        await self.safe_edit(
            event,
            "🔒 <b>ACCESS REQUIRED</b>\n\n• Ask the owner to approve your ID" + FOOTER,
            [[Button.inline("🔔 Request access", b"access:request")]],
        )

    async def request_access(self, event: events.CallbackQuery.Event) -> None:
        user_id = int(event.sender_id)
        now = time.monotonic()
        if now - self.access_requests.get(user_id, 0) < 300:
            await self.client.send_message(
                user_id, "Your access request is already waiting for the owner."
            )
            return
        self.access_requests[user_id] = now
        sender = await event.get_sender()
        username = f"@{sender.username}" if getattr(sender, "username", None) else "no username"
        try:
            await self.client.send_message(
                self.config.owner_id,
                "🔔 <b>ACCESS REQUEST</b>\n\n"
                f"👤 {esc(sender.first_name or 'Unknown')} ({esc(username)})\n"
                f"🆔 <code>{user_id}</code>",
                buttons=[
                    [
                        Button.inline("✅ Approve", f"access:approve:{user_id}".encode()),
                        Button.inline("❌ Deny", f"access:deny:{user_id}".encode()),
                    ]
                ],
            )
        except RPCError:
            await self.client.send_message(
                user_id,
                "The owner has not opened this bot yet, so the request could not be delivered. "
                "Please contact the owner directly.",
            )
            return
        await self.client.send_message(
            user_id, "✅ Request sent. The owner will decide from the control centre."
        )

    async def handle_access_decision(self, event: events.CallbackQuery.Event, data: str) -> None:
        _, action, raw_id = data.split(":", 2)
        try:
            target = int(raw_id)
        except ValueError:
            return
        if action == "approve":
            await self.db.set_user_flag(target, "approved", True)
            message = "✅ Access approved. Send /start to begin."
            label = "Approved"
        else:
            message = "Your access request was not approved."
            label = "Denied"
        with contextlib.suppress(RPCError):
            await self.client.send_message(target, message)
        await self.safe_edit(event, f"{label} <code>{target}</code>." + FOOTER, None)

    async def command_start(self, event: events.NewMessage.Event) -> None:
        if not await self.is_authorized(int(event.sender_id)):
            await self.send_access_denied(event)
            return
        await self.send_start(event)

    async def send_start(self, event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        welcome = await self.db.get_setting("welcome_text", DEFAULT_SETTINGS["welcome_text"])
        text = render_welcome(
            sender.first_name or "there",
            self.me.first_name if self.me else BRAND_NAME,
            welcome or DEFAULT_SETTINGS["welcome_text"],
            archives_logged=await self.archive_logging_enabled(),
        )
        start_photo = self.config.data_dir / "brand" / "start.jpg"
        if not start_photo.exists():
            start_photo = self.config.assets_dir / "start_banner.png"
        try:
            await self.client.send_file(
                event.chat_id,
                start_photo,
                caption=text,
                buttons=home_buttons(owner=event.sender_id == self.config.owner_id),
            )
        except RPCError:
            await event.reply(
                text, buttons=home_buttons(owner=event.sender_id == self.config.owner_id)
            )

    async def command_help(self, event: events.NewMessage.Event) -> None:
        if not await self.is_authorized(int(event.sender_id)):
            await self.send_access_denied(event)
            return
        await event.reply(
            render_help(
                await self.db.get_setting("help_text", DEFAULT_SETTINGS["help_text"]) or ""
            ),
            buttons=back_home_button(),
        )

    async def command_privacy(self, event: events.NewMessage.Event) -> None:
        if not await self.is_authorized(int(event.sender_id)):
            await self.send_access_denied(event)
            return
        await event.reply(
            render_privacy(
                archives_logged=await self.archive_logging_enabled(),
                cleanup_minutes=await self.setting_int("cleanup_minutes"),
                logging_configured=await self.get_log_channel() is not None,
            ),
            buttons=back_home_button(),
        )

    async def command_myid(self, event: events.NewMessage.Event) -> None:
        await event.reply(f"🆔 Your Telegram ID is <code>{event.sender_id}</code>.")

    async def command_settings(self, event: events.NewMessage.Event) -> None:
        if not await self.is_authorized(int(event.sender_id)):
            await self.send_access_denied(event)
            return
        await self.show_user_settings(event, edit=False)

    async def command_bulk(self, event: events.NewMessage.Event) -> None:
        user_id = int(event.sender_id)
        if not await self.is_authorized(user_id):
            await self.send_access_denied(event)
            return
        existing = self.bulk_sessions.get(user_id)
        if existing is not None and not existing.closed:
            await event.reply(
                "📚 <b>BULK ACTIVE</b>\n\n"
                f"• Files: <b>{len(existing.entries)}</b>\n"
                "• Finish: <code>/donebulk</code>\n"
                "• Cancel: <code>/cancelbulk</code>"
            )
            return
        status = await self.reply_with_flood_wait(
            event,
            "📚 <b>BULK STARTED</b>\n\n"
            "• Send or forward all documents\n"
            "• Finish: <code>/donebulk</code>\n"
            "• Cancel: <code>/cancelbulk</code>\n"
            "• Accepted archives run one by one\n"
            "• Unsupported files are listed + ignored"
            + FOOTER,
            action="starting bulk collection",
        )
        self.bulk_sessions[user_id] = BulkSession(
            user_id=user_id,
            chat_id=int(event.chat_id),
            status_message=status,
        )

    async def collect_bulk_document(
        self, event: events.NewMessage.Event, session: BulkSession
    ) -> None:
        filename = safe_filename(event.message.file.name or f"document-{event.message.id}.bin")
        size = int(event.message.file.size or 0)
        async with session.lock:
            if session.closed or self.bulk_sessions.get(session.user_id) is not session:
                await event.reply(
                    "This bulk collection has already closed. Send <code>/bulk</code> to start "
                    "another one."
                )
                return
            session.entries.append(BulkEntry(event, filename, size))
            count = len(session.entries)
            now = time.monotonic()
            # A large forwarded album can arrive in a burst. Keep one counter card, but do not
            # issue an edit RPC for every document and create a FloodWait before /donebulk.
            if now < session.status_updates_blocked_until or (
                count > 1 and now - session.last_status_edit_at < 1.25
            ):
                return
            session.last_status_edit_at = now
            text = (
                "📚 <b>BULK ACTIVE</b>\n\n"
                f"• Received: <b>{count}</b>\n"
                f"• Latest: <code>{esc(filename)}</code> • {human_size(size)}\n"
                "• Finish: <code>/donebulk</code>"
                + FOOTER
            )
            try:
                await session.status_message.edit(text, buttons=None, parse_mode="html")
            except FloodWaitError as exc:
                session.status_updates_blocked_until = max(
                    session.status_updates_blocked_until,
                    time.monotonic() + max(1, exc.seconds),
                )
                LOGGER.warning(
                    "Bulk counter update paused by FloodWait for %ss", exc.seconds
                )
            except RPCError:
                pass

    async def command_donebulk(self, event: events.NewMessage.Event) -> None:
        user_id = int(event.sender_id)
        if not await self.is_authorized(user_id):
            await self.send_access_denied(event)
            return
        session = self.bulk_sessions.get(user_id)
        if session is None:
            await event.reply(
                "There is no active bulk collection. Send <code>/bulk</code> first."
            )
            return
        async with session.lock:
            if session.closed:
                await event.reply("That bulk collection is already being finalized.")
                return
            session.closed = True
            self.bulk_sessions.pop(user_id, None)
            # Event handlers run concurrently for transfer speed. Private-chat message IDs are
            # monotonic, so sorting here makes FIFO order deterministic even if two document
            # handlers acquired the collector lock in the opposite scheduling order.
            entries = tuple(
                sorted(
                    session.entries,
                    key=lambda item: int(
                        getattr(getattr(item.source_event, "message", None), "id", 0) or 0
                    ),
                )
            )

        max_archive = await self.setting_int("max_archive_mb") * 1024 * 1024
        accepted, rejected = partition_bulk_entries(entries, max_archive)
        pages = render_bulk_summary(
            [(entry.filename, entry.size) for entry in accepted],
            [(entry.filename, reason) for entry, reason in rejected],
        )
        summary_message: Any | None = None
        for page in pages:
            summary_message = await self.send_message_with_flood_wait(
                int(event.chat_id),
                page,
                status_message=summary_message or session.status_message,
                action="publishing the bulk validation list",
            )
        with contextlib.suppress(RPCError):
            await session.status_message.delete()

        if not accepted:
            return
        if user_id == self.config.owner_id:
            token = secrets.token_hex(4)
            prompt = await self.send_message_with_flood_wait(
                int(event.chat_id),
                "👑 <b>BULK ROUTE</b>\n\n"
                f"• Archives: <b>{len(accepted)}</b>\n"
                "• Choose one route for the batch, Sir"
                + FOOTER,
                buttons=owner_delivery_buttons(token),
                status_message=summary_message,
                action="opening the bulk delivery menu",
            )
            self.owner_archive_choices[token] = PendingOwnerArchive(
                token,
                tuple(entry.source_event for entry in accepted),
                prompt,
                is_bulk=True,
            )
            return

        targets = (DeliveryTarget(int(event.chat_id), "This chat", "user"),)
        queued, failed = await self.enqueue_archive_events(
            tuple(entry.source_event for entry in accepted),
            targets,
            status_message=summary_message,
            aggregate_queue=True,
        )
        await self.send_message_with_flood_wait(
            int(event.chat_id),
            "✅ <b>ALL BULK ARCHIVES QUEUED</b>\n\n"
            f"• All <b>{queued}</b> archive{'s' if queued != 1 else ''} queued\n"
            f"• Failed: <b>{failed}</b>\n"
            "• Order: FIFO • one live card per active archive"
            + FOOTER,
            status_message=summary_message,
            action="confirming the bulk queue",
        )

    async def command_cancelbulk(self, event: events.NewMessage.Event) -> None:
        user_id = int(event.sender_id)
        if not await self.is_authorized(user_id):
            await self.send_access_denied(event)
            return
        count = await self.cancel_bulk_session(user_id)
        if count is None:
            await event.reply("There is no active bulk collection to cancel.")
            return
        await event.reply(
            "✖ <b>BULK CANCELLED</b>\n\n"
            f"• Discarded: <b>{count}</b> unsubmitted document(s)"
        )

    async def cancel_bulk_session(self, user_id: int) -> int | None:
        session = self.bulk_sessions.get(user_id)
        if session is None:
            return None
        async with session.lock:
            if self.bulk_sessions.pop(user_id, None) is not session:
                return None
            session.closed = True
            count = len(session.entries)
        with contextlib.suppress(RPCError):
            await session.status_message.edit(
                "✖ <b>BULK CANCELLED</b>\n\n"
                f"• Discarded: <b>{count}</b> unsubmitted document(s)",
                buttons=None,
            )
        return count

    async def command_cancel(self, event: events.NewMessage.Event) -> None:
        user_id = int(event.sender_id)
        pending = self.pending_inputs.pop(user_id, None)
        cancelled_choice = False
        if user_id == self.config.owner_id:
            for token, request in list(self.owner_archive_choices.items()):
                self.owner_archive_choices.pop(token, None)
                cancelled_choice = True
                with contextlib.suppress(RPCError):
                    await request.prompt_message.edit(
                        "✖ Owner archive delivery cancelled.", buttons=None
                    )
        job = self.active_jobs.get(user_id)
        if not job:
            queued = next((item for item in self.job_queue if item.user_id == user_id), None)
            if queued and await self.cancel_queued_job(
                queued.job_id, "Queued task cancelled before download."
            ):
                await event.reply(f"✖ Cancelled queued task <code>{queued.job_id}</code>.")
                return
            job = self.active_jobs.get(user_id)
        if not job:
            if pending or cancelled_choice:
                await event.reply("Menu input cancelled.")
            elif user_id in self.bulk_sessions:
                await event.reply(
                    "There is no active or queued extraction to cancel. Your bulk collection "
                    "is still active; use <code>/cancelbulk</code> to discard it."
                )
            else:
                await event.reply("There is no active or queued task to cancel.")
            return
        job.cancel_event.set()
        if job.task:
            job.task.cancel()
        await event.reply(f"✖ Cancelling task <code>{job.job_id}</code>…")

    async def command_password(self, event: events.NewMessage.Event) -> None:
        if not await self.is_authorized(int(event.sender_id)):
            await self.send_access_denied(event)
            return
        parts = (event.raw_text or "").split(maxsplit=1)
        job = self.active_jobs.get(int(event.sender_id))
        if not job:
            await event.reply("No active archive is waiting for a password.")
            return
        if len(parts) != 2 or not parts[1].strip():
            await event.reply("Use <code>/password your-password</code>.")
            return
        try:
            job.password_queue.put_nowait(parts[1].strip()[:512])
            with contextlib.suppress(RPCError):
                await event.delete()
            await self.client.send_message(
                event.chat_id, "🔐 Password received in memory. It will not be saved or logged."
            )
        except asyncio.QueueFull:
            await event.reply("Too many password attempts. Cancel and send the archive again.")

    async def command_admin(self, event: events.NewMessage.Event) -> None:
        if event.sender_id != self.config.owner_id:
            await event.reply("Owner access only.")
            return
        await self.show_admin_home(event, edit=False)

    async def offer_owner_delivery(self, event: events.NewMessage.Event) -> None:
        filename = safe_filename(event.message.file.name or f"archive-{event.message.id}.bin")
        if not looks_like_archive(filename):
            await self.handle_archive(event)
            return

        # Keep every fresh choice alive so the owner can forward a batch of archives quickly.
        # Only stale prompts are retired; a newer file never replaces an earlier one.
        for token, request in list(self.owner_archive_choices.items()):
            if time.monotonic() - request.created_at > 600:
                self.owner_archive_choices.pop(token, None)
                with contextlib.suppress(RPCError):
                    await request.prompt_message.edit(
                        "⌛ This delivery choice expired. Send the archive again.", buttons=None
                    )

        token = secrets.token_hex(4)
        prompt = await self.reply_with_flood_wait(
            event,
            "👑 <b>DELIVERY ROUTE</b>\n\n"
            f"• Archive: <code>{esc(filename)}</code>\n"
            "• Choose owner, mirror, or both\n"
            "• Destinations: known accessible chats"
            + FOOTER,
            buttons=owner_delivery_buttons(token),
            action=f"opening the delivery menu for {filename}",
        )
        self.owner_archive_choices[token] = PendingOwnerArchive(token, (event,), prompt)

    async def handle_owner_delivery_callback(
        self, event: events.CallbackQuery.Event, data: str
    ) -> None:
        parts = data.split(":")
        if len(parts) < 3:
            return
        action, token = parts[1], parts[2]
        request = self.owner_archive_choices.get(token)
        if request is None or time.monotonic() - request.created_at > 600:
            self.owner_archive_choices.pop(token, None)
            await self.safe_edit(
                event,
                "⌛ This delivery choice expired. Send the archive again." + FOOTER,
                None,
            )
            return

        if action == "cancel":
            self.owner_archive_choices.pop(token, None)
            await self.safe_edit(event, "✖ Owner archive delivery cancelled." + FOOTER, None)
            return
        if action == "back":
            title = (
                "👑 <b>BULK ROUTE</b>\n\n"
                f"• Archives: <b>{len(request.source_events)}</b>\n"
                "• Choose one route, Sir"
                if request.is_bulk
                else "👑 <b>DELIVERY ROUTE</b>\n\n• Choose a route, Sir"
            )
            await self.safe_edit(
                event,
                title + FOOTER,
                owner_delivery_buttons(token),
            )
            return
        if action == "mode" and len(parts) == 4:
            mode = parts[3]
            if mode == "self":
                await self.start_owner_archive(request, mode, None)
                return
            if mode not in {"mirror", "both"}:
                return
            await self.show_mirror_destinations(event, request, mode, 0)
            return
        if action == "refresh" and len(parts) == 4:
            mode = parts[3]
            if mode not in {"mirror", "both"}:
                return
            await self.show_mirror_destinations(event, request, mode, 0, refresh=True)
            return
        if action == "page" and len(parts) == 5:
            mode = parts[3]
            if mode not in {"mirror", "both"}:
                return
            try:
                page = int(parts[4])
            except ValueError:
                return
            await self.show_mirror_destinations(event, request, mode, page)
            return
        if action == "target" and len(parts) == 5:
            mode = parts[3]
            if mode not in {"mirror", "both"}:
                return
            try:
                target_id = int(parts[4])
            except ValueError:
                return
            listed = next(
                (target for target in request.destinations if target.chat_id == target_id),
                None,
            )
            if listed is None:
                await self.show_mirror_destinations(
                    event,
                    request,
                    mode,
                    0,
                    refresh=True,
                    error="That destination is no longer in the verified list.",
                )
                return
            try:
                target = await self.resolve_delivery_target(target_id)
            except DeliveryError as exc:
                request.destinations = tuple(
                    target for target in request.destinations if target.chat_id != target_id
                )
                await self.db.delete_mirror_destination(target_id)
                self.mirror_destination_cached_at = 0.0
                await self.show_mirror_destinations(
                    event,
                    request,
                    mode,
                    0,
                    error=str(exc),
                )
                return
            await self.start_owner_archive(request, mode, target)

    async def show_mirror_destinations(
        self,
        event: events.CallbackQuery.Event,
        request: PendingOwnerArchive,
        mode: str,
        page: int,
        *,
        refresh: bool = False,
        error: str | None = None,
    ) -> None:
        if refresh or not request.destinations:
            cache_is_fresh = (
                not refresh
                and time.monotonic() - self.mirror_destination_cached_at < 60
            )
            if cache_is_fresh:
                request.destinations = self.mirror_destination_cache
            else:
                await self.safe_edit(
                    event,
                    "🔎 <b>LOADING CHATS</b>\n\n"
                    "• Reading known accessible destinations…"
                    + FOOTER,
                    None,
                )
                request.destinations = tuple(await self.discover_mirror_destinations())
                self.mirror_destination_cache = request.destinations
                self.mirror_destination_cached_at = time.monotonic()

        per_page = 8
        pages = max(1, (len(request.destinations) + per_page - 1) // per_page)
        page = min(max(0, page), pages - 1)
        prefix = f"❌ {esc(error)}\n\n" if error else ""
        availability = (
            f"• Found: <b>{len(request.destinations)}</b>\n"
            f"• Page: <b>{page + 1}/{pages}</b>"
            if request.destinations
            else "• No destination known\n"
            "• Add the bot, send one chat message, then refresh"
        )
        await self.safe_edit(
            event,
            prefix
            + "🪞 <b>MIRROR DESTINATION</b>\n\n"
            + availability
            + "\n• Tap to revalidate • no manual IDs"
            + FOOTER,
            mirror_destination_buttons(
                token=request.token,
                mode=mode,
                destinations=request.destinations,
                page=page,
            ),
        )

    async def discover_mirror_destinations(self) -> list[DeliveryTarget]:
        discovered: dict[int, DeliveryTarget] = {}
        excluded_ids = {
            self.config.owner_id,
            int(getattr(self.me, "id", 0) or 0),
        }
        for item in await self.db.mirror_destinations(limit=None):
            target_id = int(item["target_id"])
            kind = str(item["kind"])
            if target_id in excluded_ids or kind not in {"user", "group", "channel"}:
                continue
            label = str(item["label"]).strip()[:100] or str(target_id)
            discovered.setdefault(target_id, DeliveryTarget(target_id, label, kind))

        for item in await self.db.reachable_users():
            target_id = int(item["user_id"])
            if target_id in excluded_ids or target_id in discovered:
                continue
            username = item.get("username")
            label = (
                item.get("first_name")
                or (f"@{username}" if username else None)
                or str(target_id)
            )
            discovered[target_id] = DeliveryTarget(target_id, str(label)[:100], "user")

        log_channel = await self.get_log_channel()
        if log_channel and log_channel not in excluded_ids and log_channel not in discovered:
            try:
                entity = await self.client.get_entity(log_channel)
                target = await self.delivery_target_from_entity(
                    entity,
                    persist=False,
                )
            except (DeliveryError, RPCError, ValueError, TypeError, OSError):
                pass
            else:
                discovered[target.chat_id] = target
        return list(discovered.values())

    async def resolve_delivery_target(self, target_id: int) -> DeliveryTarget:
        try:
            entity = await self.client.get_entity(target_id)
        except (RPCError, ValueError, TypeError, OSError) as exc:
            raise DeliveryError(
                "I can no longer access that destination. Refresh after restoring bot access."
            ) from exc
        return await self.delivery_target_from_entity(entity, persist=True)

    async def delivery_target_from_entity(
        self,
        entity: Any,
        *,
        persist: bool,
    ) -> DeliveryTarget:
        if entity is None:
            raise DeliveryError("Telegram did not return a usable destination.")
        if getattr(entity, "bot", False):
            raise DeliveryError("Bot accounts are not accepted as mirror destinations.")
        if getattr(entity, "deleted", False) or getattr(entity, "left", False) or getattr(
            entity, "deactivated", False
        ):
            raise DeliveryError("That destination is no longer active for this bot.")
        try:
            target_id = int(get_peer_id(entity))
        except (TypeError, ValueError) as exc:
            raise DeliveryError("Telegram did not return a usable destination ID.") from exc
        if target_id == int(getattr(self.me, "id", 0) or 0):
            raise DeliveryError("The bot cannot mirror files to itself.")
        if target_id == self.config.owner_id:
            raise DeliveryError("Use “Send to you, Sir” for the owner chat.")

        username = getattr(entity, "username", None)
        label = (
            getattr(entity, "title", None)
            or getattr(entity, "first_name", None)
            or (f"@{username}" if username else str(target_id))
        )
        broadcast = bool(getattr(entity, "broadcast", False))
        group = bool(
            getattr(entity, "megagroup", False) or entity.__class__.__name__ == "Chat"
        )
        kind = "channel" if broadcast else "group" if group else "user"
        if kind in {"channel", "group"}:
            try:
                permissions = await self.client.get_permissions(entity, self.me)
            except (RPCError, ValueError, TypeError, OSError) as exc:
                raise DeliveryError(
                    "I can resolve that chat but cannot verify the bot’s membership or "
                    "posting rights."
                ) from exc
            if permissions is None or getattr(permissions, "is_banned", False) or getattr(
                permissions, "has_left", False
            ):
                raise DeliveryError("Add the bot to that destination before using it as a mirror.")
            if broadcast and (
                not getattr(permissions, "is_creator", False)
                and (
                    not getattr(permissions, "is_admin", False)
                    or not getattr(permissions, "post_messages", False)
                )
            ):
                raise DeliveryError("The bot must be a channel administrator allowed to post.")
        clean_label = str(label).strip()[:100] or str(target_id)
        if persist:
            await self.db.save_mirror_destination(target_id, clean_label, username, kind)
        return DeliveryTarget(target_id, clean_label, kind)

    async def start_owner_archive(
        self,
        request: PendingOwnerArchive,
        mode: str,
        mirror_target: DeliveryTarget | None,
    ) -> None:
        owner_target = DeliveryTarget(self.config.owner_id, "You, Sir", "user")
        if mode == "self":
            targets = (owner_target,)
        elif mode == "mirror" and mirror_target:
            targets = (mirror_target,)
        elif mode == "both" and mirror_target:
            targets = (owner_target, mirror_target)
        else:
            raise DeliveryError("The selected delivery route is incomplete.")

        unique_targets = tuple({target.chat_id: target for target in targets}.values())
        self.owner_archive_choices.pop(request.token, None)
        labels = " + ".join(target.label for target in unique_targets)
        count = len(request.source_events)
        await self.edit_message_with_flood_wait(
            request.prompt_message,
            "⏳ <b>QUEUEING</b>\n\n"
            f"• Route: <b>{esc(labels)}</b>\n"
            f"• Archives: <b>{count}</b>\n"
            "• Creating FIFO entries…",
            buttons=None,
            action="committing the owner’s delivery route",
        )
        queued, failed = await self.enqueue_archive_events(
            request.source_events,
            unique_targets,
            status_message=request.prompt_message,
            aggregate_queue=request.is_bulk,
        )
        if request.is_bulk:
            confirmation = (
                "✅ <b>ALL BULK ARCHIVES QUEUED</b>\n\n"
                f"• Route: <b>{esc(labels)}</b>\n"
                f"• All <b>{queued}</b> archive{'s' if queued != 1 else ''} queued\n"
                f"• Failed: <b>{failed}</b>\n"
                "• Order: FIFO • one live card when active"
                + FOOTER
            )
        else:
            confirmation = (
                "✅ <b>QUEUED</b>\n\n"
                f"• Route: <b>{esc(labels)}</b>\n"
                f"• Queued: <b>{queued}</b>\n"
                f"• Failed: <b>{failed}</b>\n"
                "• Order: FIFO"
                + FOOTER
            )
        await self.edit_message_with_flood_wait(
            request.prompt_message,
            confirmation,
            buttons=None,
            action="confirming the owner’s FIFO queue",
        )

    async def handle_home_callback(self, event: events.CallbackQuery.Event, data: str) -> None:
        sender = await event.get_sender()
        if data == "home:start":
            welcome = await self.db.get_setting("welcome_text", DEFAULT_SETTINGS["welcome_text"])
            text = render_welcome(
                sender.first_name or "there",
                self.me.first_name if self.me else BRAND_NAME,
                welcome or DEFAULT_SETTINGS["welcome_text"],
                archives_logged=await self.archive_logging_enabled(),
            )
            await self.safe_edit(
                event,
                text,
                home_buttons(owner=event.sender_id == self.config.owner_id),
            )
        elif data == "home:upload":
            await self.safe_edit(
                event,
                "📦 <b>SEND ARCHIVE</b>\n\n"
                "• Telegram: paperclip → <b>File</b>\n"
                "• Password: caption <code>password: value</code>\n"
                "• Owner: choose normal or mirror route\n"
                "• Processing starts after delivery selection" + FOOTER,
                back_home_button(),
            )
        elif data == "home:bulk":
            await self.safe_edit(
                event,
                "📚 <b>BULK FIFO</b>\n\n"
                "• Start: <code>/bulk</code>\n"
                "• Send: all documents\n"
                "• Finish: <code>/donebulk</code>\n"
                "• Cancel: <code>/cancelbulk</code>\n"
                "• Result: supported/ignored list + FIFO queue\n"
                "• Owner: one route for the full batch"
                + FOOTER,
                back_home_button(),
            )
        elif data == "home:help":
            await self.safe_edit(
                event,
                render_help(
                    await self.db.get_setting("help_text", DEFAULT_SETTINGS["help_text"]) or ""
                ),
                back_home_button(),
            )
        elif data == "home:privacy":
            await self.safe_edit(
                event,
                render_privacy(
                    archives_logged=await self.archive_logging_enabled(),
                    cleanup_minutes=await self.setting_int("cleanup_minutes"),
                    logging_configured=await self.get_log_channel() is not None,
                ),
                back_home_button(),
            )
        elif data == "home:myid":
            await self.safe_edit(
                event,
                f"🆔 <b>YOUR TELEGRAM ID</b>\n\n<code>{event.sender_id}</code>" + FOOTER,
                back_home_button(),
            )

    async def show_user_settings(self, event: Any, *, edit: bool) -> None:
        user_id = int(event.sender_id)
        settings = await self.db.get_user_settings(user_id)
        mode = settings["upload_mode"]
        text = (
            "⚙️ <b>UPLOAD SETTINGS</b>\n\n"
            f"• Mode: <b>{esc(mode.title())}</b>\n"
            f"• Video thumb: <b>{'Custom' if settings['thumbnail_path'] else 'Automatic'}</b>\n"
            f"• Caption: <b>{'Custom' if settings['caption_template'] else 'Default'}</b>\n"
            "• Auto: verified media • everything else: document"
            + FOOTER
        )
        buttons = user_settings_buttons(
            mode, bool(settings["thumbnail_path"]), bool(settings["caption_template"])
        )
        if edit:
            await self.safe_edit(event, text, buttons)
        else:
            await event.reply(text, buttons=buttons)

    async def handle_user_callback(self, event: events.CallbackQuery.Event, data: str) -> None:
        user_id = int(event.sender_id)
        if data == "user:settings":
            await self.show_user_settings(event, edit=True)
            return
        if data.startswith("user:mode:"):
            mode = data.rsplit(":", 1)[1]
            if mode in {"auto", "media", "document"}:
                await self.db.set_user_setting(user_id, "upload_mode", mode)
            await self.show_user_settings(event, edit=True)
        elif data == "user:thumb:set":
            await self.prompt_input(
                user_id,
                "user_thumb",
                "🖼 Send one JPG/PNG photo now. It will be normalized for Telegram thumbnails.",
            )
        elif data == "user:thumb:del":
            settings = await self.db.get_user_settings(user_id)
            path = settings.get("thumbnail_path")
            if path:
                Path(path).unlink(missing_ok=True)
                await self.db.set_user_setting(user_id, "thumbnail_path", None)
            await self.show_user_settings(event, edit=True)
        elif data == "user:caption:set":
            await self.prompt_input(
                user_id,
                "user_caption",
                "✍️ Reply with your caption template. Available placeholders:\n"
                "<code>{name} {path} {size} {index} {total} {bot}</code>\n\n"
                "Example: <code>{name} • {index}/{total}</code>",
            )
        elif data == "user:caption:del":
            await self.db.set_user_setting(user_id, "caption_template", None)
            await self.show_user_settings(event, edit=True)

    async def show_admin_home(self, event: Any, *, edit: bool) -> None:
        mode = await self.db.get_setting("access_mode", "owner")
        log_channel = await self.configured_log_channel()
        text = (
            "👑 <b>OWNER MENU</b>\n\n"
            f"• Bot: @{esc(self.me.username or 'unknown')} • v{__version__}\n"
            f"• Access: <b>{esc((mode or 'owner').title())}</b>\n"
            f"• Logs: <code>{log_channel or 'off'}</code>\n"
            f"• Running / queued: <b>{len(self.active_jobs)} / {len(self.job_queue)}</b>"
            + FOOTER
        )
        if edit:
            await self.safe_edit(event, text, admin_home_buttons())
        else:
            await event.reply(text, buttons=admin_home_buttons())

    async def handle_admin_callback(self, event: events.CallbackQuery.Event, data: str) -> None:
        if data == "admin:home":
            await self.show_admin_home(event, edit=True)
        elif data == "admin:access":
            await self.show_admin_access(event)
        elif data.startswith("admin:mode:"):
            mode = data.rsplit(":", 1)[1]
            if mode in {"public", "private", "owner"}:
                await self.db.set_setting("access_mode", mode)
                await self.log_event(f"🔐 Access mode changed to <b>{esc(mode)}</b> by owner.")
            await self.show_admin_access(event)
        elif data == "admin:maintenance":
            enabled = not await self.setting_bool("maintenance")
            await self.db.set_setting("maintenance", "1" if enabled else "0")
            await self.show_admin_access(event)
        elif data.startswith("admin:user:"):
            await self.handle_admin_user_callback(event, data)
        elif data == "admin:appearance":
            await self.safe_edit(
                event,
                "🎨 <b>APPEARANCE</b>\n\n"
                "• Text: name • bio • description • welcome • help\n"
                "• Images: banner • profile • video thumbnail" + FOOTER,
                admin_appearance_buttons(),
            )
        elif data.startswith("admin:appearance:"):
            await self.handle_appearance_callback(event, data)
        elif data == "admin:logs":
            await self.show_admin_logs(event)
        elif data.startswith("admin:logs:"):
            await self.handle_logs_callback(event, data)
        elif data == "admin:limits":
            await self.show_admin_limits(event)
        elif data.startswith("admin:limit:"):
            key = data.rsplit(":", 1)[1]
            await self.prompt_input(
                self.config.owner_id,
                f"owner_limit:{key}",
                f"Reply with the new numeric value for <code>{esc(key)}</code>.",
            )
        elif data == "admin:stats":
            await self.show_admin_stats(event)
        elif data == "admin:broadcast":
            await self.prompt_input(
                self.config.owner_id,
                "owner_broadcast",
                "📣 Reply with the plain-text broadcast. You will see a confirmation before it sends.",
            )
        elif data == "admin:broadcast:confirm":
            draft = self.broadcast_drafts.pop(self.config.owner_id, None)
            if draft:
                self.spawn(self.broadcast(draft, event.chat_id))
                await self.safe_edit(
                    event,
                    "📣 <b>BROADCAST STARTED</b>\n\nProgress will arrive in a new message."
                    + FOOTER,
                    [[Button.inline("‹ Owner menu", b"admin:home")]],
                )
        elif data == "admin:broadcast:cancel":
            self.broadcast_drafts.pop(self.config.owner_id, None)
            await self.show_admin_home(event, edit=True)
        elif data == "admin:clean":
            removed = await self.cleanup_stale_workspaces(force=True)
            await self.safe_edit(
                event,
                f"🧹 Removed <b>{removed}</b> inactive temporary folder(s). Active jobs were untouched."
                + FOOTER,
                [[Button.inline("‹ Owner menu", b"admin:home")]],
            )
        elif data == "admin:restart:ask":
            await self.safe_edit(
                event,
                "♻️ <b>RESTART SERVICE?</b>\n\nActive tasks will be cancelled and temporary files cleaned."
                + FOOTER,
                [
                    [
                        Button.inline("Restart now", b"admin:restart:confirm"),
                        Button.inline("Cancel", b"admin:home"),
                    ]
                ],
            )
        elif data == "admin:restart:confirm":
            await self.safe_edit(event, "♻️ Restarting Ace Extractor…", None)
            self.exit_code = 75
            self.spawn(self.disconnect_soon())

    async def show_admin_access(self, event: events.CallbackQuery.Event) -> None:
        mode = await self.db.get_setting("access_mode", "owner") or "owner"
        maintenance = await self.setting_bool("maintenance")
        users = await self.db.allowed_users()
        text = (
            "🔐 <b>ACCESS CONTROL</b>\n\n"
            f"• Mode: <b>{esc(mode.title())}</b>\n"
            f"• Maintenance: <b>{'ON' if maintenance else 'OFF'}</b>\n"
            f"• Approved/admin IDs: <b>{len(users)}</b>\n"
            "• Private: owner + admins + approved IDs" + FOOTER
        )
        await self.safe_edit(event, text, admin_access_buttons(mode, maintenance))

    async def handle_admin_user_callback(
        self, event: events.CallbackQuery.Event, data: str
    ) -> None:
        action = data.rsplit(":", 1)[1]
        prompts = {
            "approve": "Reply with the numeric ID to approve.",
            "remove": "Reply with the numeric ID to remove from the allowlist.",
            "ban": "Reply with the numeric ID to ban.",
            "unban": "Reply with the numeric ID to unban.",
            "admin": "Reply with the numeric ID to grant bot-admin access.",
        }
        if action == "list":
            users = await self.db.allowed_users()
            lines = []
            for item in users[:40]:
                role = "admin" if item["is_admin"] else "approved"
                username = f" @{item['username']}" if item.get("username") else ""
                lines.append(f"• <code>{item['user_id']}</code>{esc(username)} — {role}")
            text = "👥 <b>ACCESS LIST</b>\n\n" + ("\n".join(lines) or "No selected IDs yet.")
            if len(users) > 40:
                text += f"\n\n…and {len(users) - 40} more."
            await self.safe_edit(
                event, text + FOOTER, [[Button.inline("‹ Access", b"admin:access")]]
            )
            return
        if action in prompts:
            await self.prompt_input(self.config.owner_id, f"owner_user:{action}", prompts[action])

    async def handle_appearance_callback(
        self, event: events.CallbackQuery.Event, data: str
    ) -> None:
        action = data.rsplit(":", 1)[1]
        prompts = {
            "name": "Reply with the new Telegram bot name (1–64 characters).",
            "short": "Reply with the new short bot bio (up to 120 characters).",
            "description": "Reply with the full BotFather description (up to 512 characters).",
            "welcome": "Reply with the new plain-text welcome paragraph (up to 600 characters).",
            "help": "Reply with the new plain-text help introduction (up to 700 characters).",
            "startphoto": "Send a landscape JPG/PNG to use above the /start message.",
            "thumb": "Send a JPG/PNG to use as the default verified-video thumbnail.",
            "profile": "Send a square JPG/PNG. I will crop and apply it as the bot profile picture immediately.",
        }
        if action in prompts:
            await self.prompt_input(
                self.config.owner_id, f"owner_appearance:{action}", prompts[action]
            )

    async def show_admin_logs(self, event: events.CallbackQuery.Event) -> None:
        channel = await self.configured_log_channel()
        active_channel = await self.get_log_channel()
        events_enabled = await self.setting_bool("log_events")
        archives_enabled = await self.setting_bool("log_archives")
        status = "not configured"
        if channel is not None:
            status = "ready" if active_channel is not None else "unavailable this run"
        text = (
            "📡 <b>LOGGING</b>\n\n"
            f"• Channel: <code>{channel or 'off'}</code>\n"
            f"• Status: <b>{esc(status.upper())}</b>\n"
            f"• Events: <b>{'ON' if events_enabled else 'OFF'}</b>\n"
            f"• Archive copies: <b>{'ON' if archives_enabled else 'OFF'}</b>\n"
            "• Captions/passwords: never copied" + FOOTER
        )
        await self.safe_edit(event, text, admin_logs_buttons(events_enabled, archives_enabled))

    async def handle_logs_callback(self, event: events.CallbackQuery.Event, data: str) -> None:
        action = data.rsplit(":", 1)[1]
        if action == "channel":
            await self.prompt_input(
                self.config.owner_id,
                "owner_log_channel",
                "Reply with the private channel/group ID (example: <code>-1001234567890</code>). "
                "The bot must already be an administrator there. Send <code>off</code> to "
                "disable log delivery completely.",
            )
        elif action == "test":
            channel = await self.configured_log_channel()
            if not channel:
                await self.client.send_message(self.config.owner_id, "Set a log channel first.")
                return
            try:
                await self.client.send_message(
                    channel,
                    "🧪 <b>ACE LOG TEST PASSED</b>\n\n"
                    f"Bot @{esc(self.me.username or 'unknown')} can post here.\n🕒 {timestamp()}",
                )
                self.disabled_log_channel_id = None
                await self.client.send_message(self.config.owner_id, "✅ Log channel test passed.")
            except RPCError as exc:
                self.disable_log_channel(channel, exc)
                await self.client.send_message(
                    self.config.owner_id,
                    f"❌ Test failed: <code>{esc(type(exc).__name__)}</code>. Check the ID and admin rights.",
                )
        elif action in {"events", "archives"}:
            key = f"log_{action}"
            enabled = not await self.setting_bool(key)
            await self.db.set_setting(key, "1" if enabled else "0")
            await self.show_admin_logs(event)

    async def show_admin_limits(self, event: events.CallbackQuery.Event) -> None:
        settings = await self.db.settings()
        text = (
            "⚙️ <b>LIMITS</b>\n\n"
            f"• Archive: <b>{esc(settings['max_archive_mb'])} MB</b>\n"
            f"• Extracted: <b>{esc(settings['max_extracted_mb'])} MB</b>\n"
            f"• Files/task: <b>{esc(settings['max_files'])}</b>\n"
            f"• Expansion: <b>{esc(settings['max_ratio'])}×</b>\n"
            f"• Concurrent: <b>{esc(settings['max_concurrent_jobs'])}</b>\n"
            f"• Timeout: <b>{human_duration(int(settings['job_timeout_seconds']))}</b>\n"
            f"• Cleanup: <b>{esc(settings['cleanup_minutes'])} min</b>\n"
            "• Per-user order: always FIFO"
            + FOOTER
        )
        await self.safe_edit(event, text, admin_limits_buttons())

    async def show_admin_stats(self, event: events.CallbackQuery.Event) -> None:
        stats = await self.db.global_stats()
        process = psutil.Process(os.getpid())
        memory = process.memory_info().rss
        disk = shutil.disk_usage(self.config.work_dir)
        text = (
            "📊 <b>LIVE ACE STATUS</b>\n\n"
            f"🟢 Uptime: <b>{human_duration(time.monotonic() - self.started_at)}</b>\n"
            f"⚡ Running / queued: <b>{self.running_slots} / {len(self.job_queue)}</b>\n"
            f"👥 Known users: <b>{stats['users']}</b>\n"
            f"✅ Completed jobs: <b>{stats['completed']}</b>\n"
            f"📥 Received: <b>{human_size(stats['bytes_in'])}</b>\n"
            f"📤 Returned: <b>{human_size(stats['bytes_out'])}</b>\n"
            f"🧠 Bot RAM: <b>{human_size(memory)}</b>\n"
            f"💽 Work disk free: <b>{human_size(disk.free)}</b>\n"
            f"🔐 cryptg: <b>{'loaded' if 'cryptg' in sys.modules else 'fallback'}</b>" + FOOTER
        )
        await self.safe_edit(
            event,
            text,
            [
                [
                    Button.inline("↻ Refresh", b"admin:stats"),
                    Button.inline("‹ Owner menu", b"admin:home"),
                ]
            ],
        )

    async def prompt_input(self, user_id: int, action: str, prompt: str) -> None:
        message = await self.client.send_message(
            user_id,
            prompt
            + "\n\n• Cancel: <code>/cancel</code> • New prompts replace old ones",
            buttons=Button.force_reply(single_use=True, selective=True),
        )
        self.pending_inputs[user_id] = PendingInput(action, message.id)

    async def handle_pending_media(
        self, event: events.NewMessage.Event, pending: PendingInput
    ) -> bool:
        action = pending.action
        media_actions = {
            "user_thumb",
            "owner_appearance:startphoto",
            "owner_appearance:thumb",
            "owner_appearance:profile",
        }
        if action not in media_actions:
            await event.reply("I’m waiting for a text or numeric reply to the previous prompt.")
            return True
        media_size = int(getattr(event.message.file, "size", 0) or 0)
        if media_size > 20 * 1024 * 1024:
            await event.reply("Please send an image smaller than 20 MB.")
            return True
        suffix = (getattr(event.message.file, "ext", None) or ".jpg").lower()
        if event.message.document and suffix not in IMAGE_SUFFIXES:
            await event.reply("Please send a JPG, PNG, or WEBP image.")
            return True

        incoming_dir = self.config.data_dir / "incoming"
        incoming_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = incoming_dir / f"{event.sender_id}-{secrets.token_hex(4)}{suffix}"
        try:
            downloaded = await self.client.download_media(event.message, file=str(temp))
            if not downloaded:
                raise ValueError("Telegram did not return image bytes")
            if action == "user_thumb":
                destination = self.config.data_dir / "thumbnails" / f"{event.sender_id}.jpg"
                await normalize_thumbnail(temp, destination)
                await self.db.set_user_setting(
                    int(event.sender_id), "thumbnail_path", str(destination)
                )
                confirmation = "✅ Your custom upload thumbnail is active."
            elif action == "owner_appearance:startphoto":
                destination = self.config.data_dir / "brand" / "start.jpg"
                await normalize_start_image(temp, destination)
                confirmation = "✅ The new /start banner is active."
            elif action == "owner_appearance:thumb":
                destination = self.config.data_dir / "brand" / "default_thumb.jpg"
                await normalize_thumbnail(temp, destination)
                confirmation = "✅ The new default video thumbnail is active."
            else:
                destination = self.config.data_dir / "brand" / "profile.jpg"
                await normalize_profile_image(temp, destination)
                await self.bot_api.set_profile_photo(destination)
                await self.db.set_setting("profile_photo_asset_version", "custom")
                confirmation = "✅ The new Telegram bot profile picture is active."
            self.pending_inputs.pop(int(event.sender_id), None)
            await event.reply(confirmation)
        except BotApiError as exc:
            LOGGER.warning("Profile photo update failed: %s", exc)
            await event.reply(
                "The image was prepared, but Telegram refused the profile update. Try again shortly."
            )
        except Exception as exc:
            LOGGER.warning("Image normalization failed: %s", type(exc).__name__)
            await event.reply("That image could not be processed. Try a normal JPG or PNG.")
        finally:
            temp.unlink(missing_ok=True)
        return True

    async def handle_pending_text(
        self, event: events.NewMessage.Event, pending: PendingInput, text: str
    ) -> None:
        user_id = int(event.sender_id)
        action = pending.action
        try:
            if action == "user_caption":
                valid, error = validate_caption_template(text)
                if not valid:
                    await event.reply(f"❌ {esc(error)}")
                    return
                await self.db.set_user_setting(user_id, "caption_template", text)
                await event.reply("✅ Caption template saved.")
            elif action.startswith("owner_appearance:"):
                if user_id != self.config.owner_id:
                    return
                await self.apply_appearance_text(event, action.rsplit(":", 1)[1], text)
            elif action.startswith("owner_user:"):
                if user_id != self.config.owner_id:
                    return
                await self.apply_owner_user_action(event, action.rsplit(":", 1)[1], text)
            elif action == "owner_log_channel":
                if user_id != self.config.owner_id:
                    return
                raw_channel = text.strip().lower()
                if raw_channel in {"off", "none", "disable", "disabled", "0"}:
                    await self.db.set_setting("log_channel_id", "disabled")
                    self.disabled_log_channel_id = None
                    await event.reply("✅ Log delivery is disabled. Jobs will continue normally.")
                else:
                    channel = int(raw_channel)
                    if channel >= 0:
                        raise ValueError("channel ID must be negative")
                    await self.db.set_setting("log_channel_id", str(channel))
                    self.disabled_log_channel_id = None
                    await event.reply(
                        "✅ Log channel saved. Use <code>/admin → Logs → Test channel</code>."
                    )
            elif action.startswith("owner_limit:"):
                if user_id != self.config.owner_id:
                    return
                await self.apply_limit(event, action.rsplit(":", 1)[1], text)
            elif action == "owner_broadcast":
                if user_id != self.config.owner_id:
                    return
                if not 1 <= len(text) <= 3500:
                    await event.reply("Broadcast text must be 1–3500 characters.")
                    return
                self.broadcast_drafts[user_id] = text
                preview = esc(text[:3000])
                await event.reply(
                    "📣 <b>BROADCAST PREVIEW</b>\n\n" + preview + FOOTER,
                    buttons=[
                        [
                            Button.inline("✅ Send to all", b"admin:broadcast:confirm"),
                            Button.inline("❌ Cancel", b"admin:broadcast:cancel"),
                        ]
                    ],
                )
            else:
                await event.reply("That menu input expired. Open the menu again.")
                return
            self.pending_inputs.pop(user_id, None)
        except DeliveryError as exc:
            await event.reply(f"❌ {esc(exc)}")
        except ValueError:
            await event.reply("❌ Please send a valid numeric value in the requested format.")
        except BotApiError as exc:
            await event.reply(f"❌ Telegram refused that profile update: {esc(exc)}")

    async def apply_appearance_text(
        self, event: events.NewMessage.Event, action: str, text: str
    ) -> None:
        lengths = {"name": 64, "short": 120, "description": 512, "welcome": 300, "help": 700}
        if action not in lengths or not 1 <= len(text) <= lengths[action]:
            await event.reply(f"Text must be 1–{lengths.get(action, 700)} characters.")
            return
        if action == "name":
            await self.bot_api.set_name(text)
            self.me.first_name = text
        elif action == "short":
            await self.bot_api.set_short_description(text)
        elif action == "description":
            await self.bot_api.set_description(text)
        elif action == "welcome":
            await self.db.set_setting("welcome_text", text)
        elif action == "help":
            await self.db.set_setting("help_text", text)
        await event.reply(f"✅ {esc(action.title())} updated.")

    async def apply_owner_user_action(
        self, event: events.NewMessage.Event, action: str, text: str
    ) -> None:
        target = int(text.strip())
        if target <= 0 or target == self.config.owner_id:
            await event.reply("The owner ID cannot be changed by this control.")
            return
        if action == "approve":
            await self.db.set_user_flag(target, "approved", True)
            message = "approved"
        elif action == "remove":
            await self.db.set_user_flag(target, "approved", False)
            await self.db.set_user_flag(target, "is_admin", False)
            message = "removed from selected access"
        elif action == "ban":
            await self.db.set_user_flag(target, "banned", True)
            await self.db.set_user_flag(target, "approved", False)
            await self.db.set_user_flag(target, "is_admin", False)
            message = "banned"
        elif action == "unban":
            await self.db.set_user_flag(target, "banned", False)
            message = "unbanned"
        elif action == "admin":
            await self.db.set_user_flag(target, "is_admin", True)
            message = "granted bot-admin access"
        else:
            return
        await event.reply(f"✅ <code>{target}</code> {message}.")

    async def apply_limit(self, event: events.NewMessage.Event, key: str, text: str) -> None:
        ranges: dict[str, tuple[float, float]] = {
            "max_archive_mb": (1, 2000),
            "max_extracted_mb": (10, 50000),
            "max_files": (1, 1000),
            "max_ratio": (1, 10000),
            "max_concurrent_jobs": (1, 8),
            "job_timeout_seconds": (60, 86400),
            "cleanup_minutes": (1, 1440),
        }
        if key not in ranges:
            await event.reply("Unknown limit.")
            return
        value = float(text.strip()) if key == "max_ratio" else int(text.strip())
        minimum, maximum = ranges[key]
        if not minimum <= value <= maximum:
            await event.reply(f"Value must be between {minimum:g} and {maximum:g}.")
            return
        stored = f"{value:g}" if isinstance(value, float) else str(value)
        await self.db.set_setting(key, stored)
        if key == "max_concurrent_jobs":
            await self.dispatch_jobs()
        await event.reply(f"✅ <code>{esc(key)}</code> set to <b>{esc(stored)}</b>.")

    async def handle_archive(
        self,
        event: events.NewMessage.Event,
        *,
        delivery_targets: tuple[DeliveryTarget, ...] | None = None,
        dispatch: bool = True,
        flood_status_message: Any | None = None,
        defer_queue_card: bool = False,
    ) -> bool:
        user_id = int(event.sender_id)
        targets = delivery_targets or (
            DeliveryTarget(int(event.chat_id), "This chat", "user"),
        )
        filename = safe_filename(event.message.file.name or f"archive-{event.message.id}.bin")
        if not looks_like_archive(filename):
            await event.reply(
                "Unsupported document. Send ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ, ZST, ISO, or CAB."
            )
            return False
        size = int(event.message.file.size or 0)
        max_archive = await self.setting_int("max_archive_mb") * 1024 * 1024
        if size <= 0:
            await event.reply("Telegram did not provide a valid file size.")
            return False
        if size > max_archive:
            await event.reply(
                f"Archive is {human_size(size)}; the owner limit is {human_size(max_archive)}."
            )
            return False

        job_id = secrets.token_hex(4)
        try:
            await self.db.create_job(job_id, user_id, filename, size)
            if defer_queue_card:
                progress_chat_id = int(event.chat_id)

                async def send_live_card(text: str, buttons: Any) -> Any:
                    return await self.send_message_with_flood_wait(
                        progress_chat_id,
                        text,
                        buttons=buttons,
                        status_message=flood_status_message,
                        action=f"opening live progress for {filename}",
                    )

                progress_message: Any = DeferredStatusMessage(send_live_card)
            else:
                progress_message = await self.reply_with_flood_wait(
                    event,
                    "⏳ <b>QUEUED</b>\n\n"
                    f"📦 <code>{esc(filename)}</code>\n"
                    f"📏 {human_size(size)}\n"
                    f"📡 {esc(' + '.join(target.label for target in targets))}\n"
                    "♾ Send more anytime - your FIFO order stays protected.\n"
                    f"🆔 <code>{job_id}</code>",
                    buttons=[[Button.inline("✖ Cancel", f"job:cancel:{job_id}".encode())]],
                    status_message=flood_status_message,
                    action=f"creating the queue card for {filename}",
                )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self.db.finish_job(
                    job_id,
                    status="failed",
                    error=f"Could not create queue status: {type(exc).__name__}",
                )
            raise
        reporter = ProgressReporter(
            progress_message,
            job_id,
            filename,
            client=self.client,
            chat_id=int(event.chat_id),
            interval=float(await self.db.get_setting("progress_interval", "1.25") or 1.25),
            defer_queue_updates=defer_queue_card,
        )
        queued = QueuedArchive(
            job_id=job_id,
            user_id=user_id,
            chat_id=int(event.chat_id),
            source_message=event.message,
            archive_name=filename,
            archive_size=size,
            delivery_targets=targets,
            reporter=reporter,
            initial_password=password_from_caption(event.raw_text),
        )
        async with self.queue_dispatch_lock:
            self.job_queue.append(queued)
            self.queued_jobs_by_id[job_id] = queued
        try:
            await self.log_archive_received(
                event,
                job_id,
                filename,
                size,
                targets,
                status_message=(flood_status_message if defer_queue_card else progress_message),
            )
        except Exception:
            # A log-channel or sender lookup failure must never strand or stop a user job.
            LOGGER.exception("Unexpected archive logging failure for %s", job_id)
        if dispatch:
            await self.dispatch_jobs()
        return True

    async def enqueue_archive_events(
        self,
        source_events: tuple[Any, ...],
        delivery_targets: tuple[DeliveryTarget, ...],
        *,
        status_message: Any | None,
        aggregate_queue: bool = False,
    ) -> tuple[int, int]:
        queued = 0
        failed = 0
        for source_event in source_events:
            try:
                created = await self.handle_archive(
                    source_event,
                    delivery_targets=delivery_targets,
                    dispatch=False,
                    flood_status_message=status_message,
                    defer_queue_card=aggregate_queue,
                )
            except Exception:
                failed += 1
                LOGGER.exception("Could not add a bulk archive to the FIFO queue")
            else:
                queued += int(created)
                failed += int(not created)
        await self.dispatch_jobs()
        return queued, failed

    async def required_disk_bytes(self, archive_size: int) -> int:
        limits = await self.archive_limits()
        possible_output = min(
            limits.max_extracted_bytes,
            int(archive_size * limits.max_ratio),
        )
        split_overhead = possible_output if possible_output > MAX_TELEGRAM_PART else 0
        return archive_size + possible_output + split_overhead + 256 * 1024 * 1024

    async def dispatch_jobs(self) -> None:
        if self.shutting_down:
            return
        failed: list[tuple[QueuedArchive, str]] = []
        async with self.queue_dispatch_lock:
            limit = await self.setting_int("max_concurrent_jobs")
            while not self.shutting_down and self.running_slots < limit:
                index = next_eligible_index(self.job_queue, self.active_jobs.keys())
                if index is None:
                    break
                queued = self.job_queue.pop(index)
                self.queued_jobs_by_id.pop(queued.job_id, None)
                queued.is_queued = False
                reservation = await self.required_disk_bytes(queued.archive_size)
                if not await self.reserve_disk(reservation):
                    if self.running_slots:
                        queued.is_queued = True
                        self.job_queue.insert(index, queued)
                        self.queued_jobs_by_id[queued.job_id] = queued
                        break
                    failed.append(
                        (
                            queued,
                            "The VPS does not currently have enough free work space for this task.",
                        )
                    )
                    continue

                workspace = self.config.work_dir / f"{queued.user_id}-{queued.job_id}"
                job_registered = False
                try:
                    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
                    job = ActiveJob(
                        queued.job_id,
                        queued.user_id,
                        queued.chat_id,
                        queued.source_message,
                        queued.archive_name,
                        workspace,
                        reserved_bytes=reservation,
                        delivery_targets=queued.delivery_targets,
                    )
                    self.active_jobs[job.user_id] = job
                    self.jobs_by_id[job.job_id] = job
                    self.running_slots += 1
                    job_registered = True
                    job.task = self.spawn(
                        self.job_wrapper(job, queued.reporter, queued.initial_password)
                    )
                except Exception as exc:
                    if job_registered:
                        self.active_jobs.pop(queued.user_id, None)
                        self.jobs_by_id.pop(queued.job_id, None)
                        self.running_slots = max(0, self.running_slots - 1)
                    await self.release_disk(reservation)
                    await asyncio.to_thread(cleanup_workspace, workspace)
                    failed.append(
                        (queued, f"Could not start the queued task: {type(exc).__name__}.")
                    )
            snapshot = list(self.job_queue)

        for queued, message in failed:
            await self.db.finish_job(queued.job_id, status="failed", error=message)
            await queued.reporter.failed(message)
            await self.log_event(
                "❌ <b>QUEUED JOB STOPPED</b>\n\n"
                f"🆔 <code>{queued.job_id}</code>\n"
                f"👤 <code>{queued.user_id}</code>\n"
                f"📦 <code>{esc(queued.archive_name)}</code>\n"
                f"Reason: <code>{esc(message)}</code>"
            )
        await self.refresh_queue_statuses(snapshot)

    async def refresh_queue_statuses(
        self, snapshot: list[QueuedArchive] | None = None
    ) -> None:
        items = snapshot if snapshot is not None else list(self.job_queue)
        for position, queued in enumerate(items, start=1):
            if not queued.is_queued or self.queued_jobs_by_id.get(queued.job_id) is not queued:
                continue
            if queued.displayed_position == position:
                continue
            queued.displayed_position = position
            await queued.reporter.queued(position)

    async def cancel_queued_job(self, job_id: str, reason: str) -> bool:
        async with self.queue_dispatch_lock:
            queued = self.queued_jobs_by_id.pop(job_id, None)
            if queued is None:
                return False
            queued.is_queued = False
            with contextlib.suppress(ValueError):
                self.job_queue.remove(queued)
        await self.db.finish_job(job_id, status="cancelled", error=reason)
        await queued.reporter.failed(reason)
        await self.log_event(
            "✖ <b>QUEUED JOB CANCELLED</b>\n\n"
            f"🆔 <code>{queued.job_id}</code>\n"
            f"👤 <code>{queued.user_id}</code>\n"
            f"📦 <code>{esc(queued.archive_name)}</code>"
        )
        await self.dispatch_jobs()
        return True

    async def log_archive_received(
        self,
        event: events.NewMessage.Event,
        job_id: str,
        filename: str,
        size: int,
        targets: tuple[DeliveryTarget, ...],
        *,
        status_message: Any | None = None,
    ) -> None:
        channel = await self.get_log_channel()
        if not channel:
            return
        sender = await event.get_sender()
        username = f"@{sender.username}" if getattr(sender, "username", None) else "—"
        caption = (
            "📦 <b>ARCHIVE RECEIVED</b>\n\n"
            f"🆔 Job: <code>{job_id}</code>\n"
            f"👤 {esc(sender.first_name or 'Unknown')} • {esc(username)}\n"
            f"🔢 User ID: <code>{event.sender_id}</code>\n"
            f"📄 <code>{esc(filename)}</code>\n"
            f"📏 {human_size(size)}\n"
            f"📡 {esc(' + '.join(target.label for target in targets))}\n"
            f"🕒 {timestamp()}"
        )
        while True:
            try:
                if await self.setting_bool("log_archives"):
                    # Re-send the existing Telegram media reference with a clean caption. The
                    # user's original caption may contain a password and is never forwarded.
                    await self.client.send_file(
                        channel,
                        event.message.media,
                        caption=caption,
                        force_document=True,
                    )
                elif await self.setting_bool("log_events"):
                    await self.client.send_message(channel, caption)
                return
            except FloodWaitError as exc:
                await self.wait_flood_message(
                    status_message,
                    exc.seconds,
                    "copying the original archive to the log channel",
                )
            except RPCError as exc:
                LOGGER.warning("Archive log copy failed: %s", type(exc).__name__)
                # A stale Telegram media reference can fail even when the channel is healthy.
                # Fall back to sanitized metadata before treating the channel as unreachable.
                try:
                    await self.client.send_message(
                        channel,
                        caption
                        + "\n\n⚠️ <i>The original archive copy was unavailable; processing continued.</i>",
                    )
                except FloodWaitError as flood:
                    await self.wait_flood_message(
                        status_message,
                        flood.seconds,
                        "writing archive metadata to the log channel",
                    )
                    continue
                except RPCError as fallback_error:
                    self.disable_log_channel(channel, fallback_error)
                return

    async def job_wrapper(
        self,
        job: ActiveJob,
        reporter: ProgressReporter,
        initial_password: str | None,
    ) -> None:
        started = time.monotonic()
        status = "failed"
        public_error = "The task stopped unexpectedly."
        detail_error: str | None = None
        output_size = 0
        file_count = 0
        try:
            timeout = await self.setting_int("job_timeout_seconds")
            output_size, file_count = await asyncio.wait_for(
                self.execute_job(job, reporter, initial_password), timeout=timeout
            )
            status = "completed"
        except (JobCancelled, asyncio.CancelledError):
            status = "cancelled"
            public_error = "Task cancelled."
        except TimeoutError:
            status = "timed_out"
            public_error = "The owner’s task time limit was reached."
        except ArchiveError as exc:
            status = "rejected" if "rejected" in exc.public_message.lower() else "failed"
            public_error = exc.public_message
            detail_error = exc.detail
        except Exception as exc:
            public_error = "An internal error stopped this task. The owner received the error type."
            detail_error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Unhandled job error for %s", job.job_id)
        finally:
            elapsed = time.monotonic() - started
            job.cancel_event.set()
            await asyncio.to_thread(cleanup_workspace, job.workspace)
            await self.db.finish_job(
                job.job_id,
                status=status,
                output_size=output_size,
                file_count=file_count,
                elapsed_seconds=elapsed,
                error=detail_error,
            )
            # Free the reservation before exposing a worker slot, otherwise a simultaneous
            # dispatcher could reject the next job while these bytes are still reserved.
            await self.release_disk(job.reserved_bytes)
            async with self.queue_dispatch_lock:
                self.active_jobs.pop(job.user_id, None)
                self.jobs_by_id.pop(job.job_id, None)
                self.running_slots = max(0, self.running_slots - 1)
            await self.dispatch_jobs()
            if status == "completed":
                await reporter.complete(output_size, file_count, elapsed)
                note_parts: list[str] = []
                if reporter.skipped_empty_files:
                    note_parts.append(f"{reporter.skipped_empty_files} zero-byte skipped")
                if reporter.failed_upload_files:
                    note_parts.append(f"{reporter.failed_upload_files} file upload failed")
                if reporter.failed_destinations:
                    note_parts.append(
                        f"{reporter.failed_destinations} destination attempt failed"
                    )
                notes = (
                    f"\n⚠️ Notes: <code>{esc(' • '.join(note_parts))}</code>"
                    if note_parts
                    else ""
                )
                await self.log_event(
                    "✅ <b>JOB COMPLETED</b>\n\n"
                    f"🆔 <code>{job.job_id}</code>\n"
                    f"👤 <code>{job.user_id}</code>\n"
                    f"📦 <code>{esc(job.archive_name)}</code>\n"
                    f"📁 {file_count} files • {human_size(output_size)}\n"
                    f"📡 {esc(' + '.join(target.label for target in job.delivery_targets))}\n"
                    f"⏱ {human_duration(elapsed)}"
                    + notes
                )
            else:
                await reporter.failed(public_error)
                await self.log_event(
                    "❌ <b>JOB STOPPED</b>\n\n"
                    f"🆔 <code>{job.job_id}</code>\n"
                    f"👤 <code>{job.user_id}</code>\n"
                    f"📦 <code>{esc(job.archive_name)}</code>\n"
                    f"📡 {esc(' + '.join(target.label for target in job.delivery_targets))}\n"
                    f"State: <b>{esc(status)}</b>\n"
                    f"Reason: <code>{esc((detail_error or public_error)[:700])}</code>"
                )

    async def execute_job(
        self,
        job: ActiveJob,
        reporter: ProgressReporter,
        initial_password: str | None,
    ) -> tuple[int, int]:
        await self.db.update_job_status(job.job_id, "running")
        await reporter.start("download", "Starting encrypted MTProto transfer")

        incoming_dir = job.workspace / "incoming"
        incoming_dir.mkdir(mode=0o700)
        archive_path = incoming_dir / job.archive_name
        archive_total = int(job.source_message.file.size or 0)

        async def download_progress(current: int, total: int) -> None:
            if job.cancel_event.is_set():
                raise asyncio.CancelledError
            await reporter.update(
                current, total or archive_total, detail="Encrypted MTProto transfer"
            )

        downloaded = await self.download_media_with_flood_retry(
            job,
            reporter,
            archive_path,
            download_progress,
        )
        if not downloaded or not archive_path.exists():
            raise ArchiveError("Telegram download did not produce a local archive.")
        if job.cancel_event.is_set():
            raise JobCancelled("Task cancelled.")

        limits = await self.archive_limits()
        await reporter.status("verify", "Inspecting names, sizes, links, and expansion ratio")
        password = initial_password
        wrong_attempts = 0
        while True:
            output_dir = job.workspace / "extracted"
            if output_dir.exists():
                await asyncio.to_thread(shutil.rmtree, output_dir, True)
            reporter.set_stage("extract")

            async def extract_progress(current: int, total: int, detail: str) -> None:
                if job.cancel_event.is_set():
                    raise JobCancelled("Task cancelled.")
                await reporter.update(current, total, detail=detail)

            try:
                content = await self.archive_engine.extract(
                    archive_path,
                    output_dir,
                    password,
                    limits,
                    job.cancel_event,
                    extract_progress,
                )
                break
            except PasswordRequired:
                password = await self.wait_for_password(job, reporter, wrong=False)
            except WrongPassword as exc:
                wrong_attempts += 1
                if wrong_attempts >= 3:
                    raise ArchiveError("Three password attempts failed; task stopped.") from exc
                password = await self.wait_for_password(job, reporter, wrong=True)

        if job.cancel_event.is_set():
            raise JobCancelled("Task cancelled.")
        files, skipped_empty = await self.prepare_upload_files(
            content.files, output_dir, job.workspace
        )
        reporter.skipped_empty_files = skipped_empty
        sent_bytes, sent_files, failed_files, failed_destinations = await self.upload_files(
            job, reporter, output_dir, files
        )
        reporter.failed_upload_files = failed_files
        reporter.failed_destinations = failed_destinations
        return sent_bytes, sent_files

    async def download_media_with_flood_retry(
        self,
        job: ActiveJob,
        reporter: ProgressReporter,
        archive_path: Path,
        progress_callback: Any,
    ) -> Any:
        while True:
            if job.cancel_event.is_set():
                raise JobCancelled("Task cancelled.")
            try:
                return await self.client.download_media(
                    job.source_message,
                    file=str(archive_path),
                    progress_callback=progress_callback,
                )
            except FloodWaitError as exc:
                cancelled = await reporter.flood_wait(
                    exc.seconds,
                    f"Telegram paused the download of {job.archive_name}",
                    cancel_event=job.cancel_event,
                )
                if cancelled or job.cancel_event.is_set():
                    raise JobCancelled("Task cancelled during Telegram FloodWait.") from exc
                archive_path.unlink(missing_ok=True)
                await reporter.status("download", "FloodWait finished • retrying download")

    async def wait_for_password(
        self, job: ActiveJob, reporter: ProgressReporter, *, wrong: bool
    ) -> str:
        detail = (
            "Wrong password. Waiting for /password (up to 5 minutes)"
            if wrong
            else "Password required. Waiting for /password (up to 5 minutes)"
        )
        await reporter.status("verify", detail)
        await self.send_message_with_flood_wait(
            job.chat_id,
            ("❌ Wrong password.\n\n" if wrong else "🔐 This archive is protected.\n\n")
            + "Send <code>/password your-password</code> within five minutes. "
            "It stays only in memory and is never included in logs.",
            status_message=reporter.message,
            action="asking for the archive password",
        )
        password_task = asyncio.create_task(job.password_queue.get())
        cancel_task = asyncio.create_task(job.cancel_event.wait())
        done, pending = await asyncio.wait(
            {password_task, cancel_task},
            timeout=PASSWORD_WAIT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done or job.cancel_event.is_set():
            password_task.cancel()
            raise JobCancelled("Task cancelled.")
        if password_task not in done:
            raise ArchiveError("Password wait expired after five minutes.")
        return password_task.result()

    async def prepare_upload_files(
        self,
        files: tuple[Path, ...],
        extracted_root: Path,
        workspace: Path,
    ) -> tuple[tuple[Path, ...], int]:
        prepared: list[Path] = []
        skipped_empty = 0
        parts_root = workspace / "large-parts"
        for index, path in enumerate(files):
            if path.stat().st_size == 0:
                # Telegram cannot upload an empty document. Empty members are ignored
                # individually so one placeholder can never cancel the remaining archive.
                skipped_empty += 1
                continue
            if path.stat().st_size <= MAX_TELEGRAM_PART:
                prepared.append(path)
                continue
            parts = await split_oversized(
                path,
                MAX_TELEGRAM_PART,
                parts_root / f"file-{index:04d}",
            )
            prepared.extend(parts)
        return tuple(prepared), skipped_empty

    async def upload_files(
        self,
        job: ActiveJob,
        reporter: ProgressReporter,
        extracted_root: Path,
        files: tuple[Path, ...],
    ) -> tuple[int, int, int, int]:
        settings = await self.db.get_user_settings(job.user_id)
        mode = settings["upload_mode"]
        template = settings["caption_template"] or await self.db.get_setting(
            "completion_caption", DEFAULT_SETTINGS["completion_caption"]
        )
        total_bytes = sum(path.stat().st_size for path in files)
        processed_bytes = 0
        sent_bytes = 0
        sent_files = 0
        failed_files = 0
        failed_destinations = 0
        reporter.set_stage("upload")
        custom_thumb = Path(settings["thumbnail_path"]) if settings["thumbnail_path"] else None
        if custom_thumb and not custom_thumb.exists():
            custom_thumb = None
        default_thumb = self.config.data_dir / "brand" / "default_thumb.jpg"
        targets = job.delivery_targets or (DeliveryTarget(job.chat_id, "This chat"),)
        await reporter.update(
            0,
            total_bytes,
            detail=(
                f"Sending {len(files)} non-empty file{'s' if len(files) != 1 else ''} to "
                f"{' + '.join(target.label for target in targets)}"
            ),
            force=True,
        )

        for index, path in enumerate(files, start=1):
            if job.cancel_event.is_set():
                raise JobCancelled("Task cancelled.")
            size = path.stat().st_size
            try:
                relative = path.relative_to(extracted_root).as_posix()
            except ValueError:
                relative = path.name
            video_candidate = is_video(path)
            info = await probe_video(self.config.ffprobe, path) if video_candidate else None
            video = info is not None
            streamable_video = video and is_streamable(path)
            photo_media = await probe_photo(path)
            supported_media = streamable_video or photo_media

            if video:
                # Telegram displays this caption like a title; keep it predictable and literal.
                caption = render_video_caption(path.name)
            else:
                caption = render_caption(
                    template or DEFAULT_SETTINGS["completion_caption"],
                    name=path.name,
                    path=relative,
                    size=human_size(size),
                    index=index,
                    total=len(files),
                    bot=self.me.first_name if self.me else BRAND_NAME,
                )
            send_as_media = supported_media and mode in {"auto", "media"}
            force_document = not send_as_media

            thumb: Path | None = None
            # Thumbnails are attached only to confirmed Telegram-compatible videos.
            # Unknown extensions and ordinary extracted files remain plain documents.
            if streamable_video:
                if custom_thumb:
                    thumb = custom_thumb
                else:
                    candidate = job.workspace / "thumbs" / f"{index:04d}.jpg"
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    thumb = await generate_video_thumbnail(
                        self.config.ffmpeg, path, candidate, info
                    )
                if thumb is None and default_thumb.exists():
                    thumb = default_thumb

            base = processed_bytes

            async def upload_progress(
                current: int,
                total: int,
                *,
                _base: int = base,
                _index: int = index,
                _relative: str = relative,
                _count: int = len(files),
            ) -> None:
                if job.cancel_event.is_set():
                    raise asyncio.CancelledError
                await reporter.update(
                    _base + current,
                    total_bytes,
                    detail=f"File {_index}/{_count} • {_relative}",
                )

            kwargs: dict[str, Any] = {
                "caption": caption,
                "force_document": force_document,
                "progress_callback": upload_progress,
            }
            primary_target = targets[0]
            if primary_target.chat_id == job.chat_id:
                kwargs["reply_to"] = job.source_message.id
            if thumb:
                kwargs["thumb"] = str(thumb)
            if streamable_video and send_as_media:
                kwargs["attributes"] = video_attributes(path, info)
                kwargs["supports_streaming"] = True

            delivered_targets = 0
            sent_message: Any | None = None
            try:
                sent_message = await self.send_file_with_retry(
                    primary_target.chat_id, path, kwargs, job, reporter
                )
                delivered_targets += 1
            except (JobCancelled, asyncio.CancelledError):
                raise
            except Exception as exc:
                failed_destinations += 1
                LOGGER.warning(
                    "Could not deliver %s to primary target %s; continuing: %s",
                    path.name,
                    primary_target.chat_id,
                    type(exc).__name__,
                )

            if sent_message is not None and len(targets) > 1 and primary_target.chat_id == job.chat_id:
                await reporter.relocate()
            for mirror_target in targets[1:]:
                await reporter.update(
                    processed_bytes + size,
                    total_bytes,
                    detail=(
                        f"Mirroring file {index}/{len(files)} to {mirror_target.label}"
                    ),
                    force=True,
                )
                try:
                    if sent_message is not None:
                        await self.copy_sent_media_with_retry(
                            mirror_target.chat_id,
                            sent_message,
                            path,
                            kwargs,
                            job,
                            reporter,
                        )
                    else:
                        direct_kwargs = dict(kwargs)
                        direct_kwargs.pop("reply_to", None)
                        await self.send_file_with_retry(
                            mirror_target.chat_id,
                            path,
                            direct_kwargs,
                            job,
                            reporter,
                        )
                    delivered_targets += 1
                except (JobCancelled, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    failed_destinations += 1
                    LOGGER.warning(
                        "Could not deliver %s to mirror target %s; continuing: %s",
                        path.name,
                        mirror_target.chat_id,
                        type(exc).__name__,
                    )

            processed_bytes += size
            if delivered_targets:
                sent_bytes += size
                sent_files += 1
                result_detail = (
                    f"Delivered {index}/{len(files)} • "
                    f"{delivered_targets}/{len(targets)} destination"
                    f"{'s' if len(targets) != 1 else ''}"
                )
            else:
                failed_files += 1
                result_detail = (
                    f"Skipped {index}/{len(files)} after Telegram rejected every safe upload form"
                )
            await reporter.update(
                processed_bytes,
                total_bytes,
                detail=result_detail,
                force=True,
            )
            await reporter.relocate()

        return sent_bytes, sent_files, failed_files, failed_destinations

    async def send_file_with_retry(
        self,
        chat_id: int,
        path: Path,
        kwargs: dict[str, Any],
        job: ActiveJob,
        reporter: ProgressReporter,
    ) -> Any:
        last_error: BaseException | None = None
        variants = upload_fallback_variants(kwargs)
        for variant_index, variant in enumerate(variants, start=1):
            attempt = 1
            while attempt <= 3:
                if job.cancel_event.is_set():
                    raise JobCancelled("Task cancelled.")
                try:
                    message = await self.client.send_file(chat_id, str(path), **variant)
                    if variant_index > 1:
                        LOGGER.warning(
                            "Telegram accepted %s with upload fallback %d/%d",
                            path.name,
                            variant_index,
                            len(variants),
                        )
                    return message
                except FloodWaitError as exc:
                    last_error = exc
                    cancelled = await reporter.flood_wait(
                        exc.seconds,
                        f"Telegram paused the upload of {path.name}",
                        cancel_event=job.cancel_event,
                    )
                    if cancelled or job.cancel_event.is_set():
                        raise JobCancelled("Task cancelled during Telegram FloodWait.") from exc
                    # A FloodWait is a required pause, not a failed retry. Repeat the exact
                    # upload form without consuming one of the three ordinary RPC attempts.
                    continue
                except RPCError as exc:
                    last_error = exc
                    if is_media_validation_error(exc):
                        LOGGER.warning(
                            "Telegram rejected upload form %d/%d for %s: %s",
                            variant_index,
                            len(variants),
                            path.name,
                            type(exc).__name__,
                        )
                        break
                    if attempt == 3:
                        break
                    await asyncio.sleep(attempt * 2)
                    attempt += 1
            if last_error and not is_media_validation_error(last_error):
                break

        detail = (
            f"{type(last_error).__name__}: {last_error}" if last_error else "Unknown upload error"
        )
        raise ArchiveError(
            f'Telegram rejected "{path.name}" even after the safe document fallback.',
            detail=detail[:700],
        )

    async def copy_sent_media_with_retry(
        self,
        chat_id: int,
        sent_message: Any,
        path: Path,
        original_kwargs: dict[str, Any],
        job: ActiveJob,
        reporter: ProgressReporter,
    ) -> Any:
        media = getattr(sent_message, "media", None)
        if media is not None:
            attempt = 1
            while attempt <= 3:
                if job.cancel_event.is_set():
                    raise JobCancelled("Task cancelled.")
                try:
                    return await self.client.send_file(
                        chat_id,
                        media,
                        caption=original_kwargs.get("caption"),
                    )
                except FloodWaitError as exc:
                    cancelled = await reporter.flood_wait(
                        exc.seconds,
                        f"Telegram paused mirroring {path.name}",
                        cancel_event=job.cancel_event,
                    )
                    if cancelled or job.cancel_event.is_set():
                        raise JobCancelled("Task cancelled during Telegram FloodWait.") from exc
                    continue
                except RPCError as exc:
                    if is_media_validation_error(exc) or attempt == 3:
                        break
                    await asyncio.sleep(attempt * 2)
                    attempt += 1

        fallback_kwargs = dict(original_kwargs)
        fallback_kwargs.pop("reply_to", None)
        return await self.send_file_with_retry(
            chat_id, path, fallback_kwargs, job, reporter
        )

    async def reserve_disk(self, amount: int) -> bool:
        async with self.disk_reservation_lock:
            free = shutil.disk_usage(self.config.work_dir).free
            if free - self.reserved_disk_bytes < amount:
                return False
            self.reserved_disk_bytes += amount
            return True

    async def release_disk(self, amount: int) -> None:
        async with self.disk_reservation_lock:
            self.reserved_disk_bytes = max(0, self.reserved_disk_bytes - max(0, amount))

    async def cancel_job_callback(self, event: events.CallbackQuery.Event, data: str) -> None:
        job_id = data.rsplit(":", 1)[1]
        queued = self.queued_jobs_by_id.get(job_id)
        if queued:
            if event.sender_id not in {queued.user_id, self.config.owner_id}:
                return
            if await self.cancel_queued_job(job_id, "Queued task cancelled before download."):
                await self.safe_edit(event, "✖ Queued task cancelled.", None)
                return
        job = self.jobs_by_id.get(job_id)
        if not job:
            await self.safe_edit(event, "This task has already finished or expired.", None)
            return
        if event.sender_id not in {job.user_id, self.config.owner_id}:
            return
        job.cancel_event.set()
        if job.task:
            job.task.cancel()
        await self.safe_edit(event, "✖ Cancelling task and cleaning temporary files…", None)

    async def broadcast(self, text: str, owner_chat_id: int) -> None:
        users = await self.db.user_ids()
        status = await self.send_message_with_flood_wait(
            owner_chat_id,
            f"📣 Broadcasting to {len(users)} user(s)…",
            action="starting the broadcast",
        )
        sent = 0
        failed = 0
        for index, user_id in enumerate(users, start=1):
            while True:
                try:
                    await self.client.send_message(user_id, text, parse_mode=None)
                    sent += 1
                    break
                except FloodWaitError as exc:
                    await self.wait_flood_message(
                        status,
                        exc.seconds,
                        f"broadcasting message {index}/{len(users)}",
                    )
                except RPCError:
                    failed += 1
                    break
            if index % 20 == 0:
                try:
                    await status.edit(
                        f"📣 Broadcast: {index}/{len(users)} • ✅ {sent} • ❌ {failed}"
                    )
                except FloodWaitError as exc:
                    await self.wait_flood_message(
                        status,
                        exc.seconds,
                        "updating broadcast progress",
                    )
                except RPCError:
                    pass
            await asyncio.sleep(0.05)
        await self.edit_message_with_flood_wait(
            status,
            f"✅ Broadcast complete. Sent: {sent} • Failed: {failed}",
            action="finishing the broadcast",
        )

    async def disconnect_soon(self) -> None:
        await asyncio.sleep(1)
        await self.client.disconnect()

    async def configured_log_channel(self) -> int | None:
        raw = await self.db.get_setting("log_channel_id")
        if raw is None:
            return self.config.log_channel_id
        if raw.strip().lower() in {"", "off", "none", "disable", "disabled", "0"}:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def get_log_channel(self) -> int | None:
        channel = await self.configured_log_channel()
        if channel == getattr(self, "disabled_log_channel_id", None):
            return None
        return channel

    def disable_log_channel(self, channel: int, error: BaseException) -> None:
        if getattr(self, "disabled_log_channel_id", None) != channel:
            LOGGER.warning(
                "Log channel %s is unavailable (%s); disabling log delivery for this run",
                channel,
                type(error).__name__,
            )
        self.disabled_log_channel_id = channel

    async def archive_logging_enabled(self) -> bool:
        return await self.setting_bool("log_archives") and await self.get_log_channel() is not None

    async def log_event(self, text: str) -> None:
        if not await self.setting_bool("log_events"):
            return
        channel = await self.get_log_channel()
        if not channel:
            return
        while True:
            try:
                await self.client.send_message(channel, text)
                return
            except FloodWaitError as exc:
                await self.wait_flood_message(
                    None,
                    exc.seconds,
                    "writing an operational log event",
                )
            except RPCError as exc:
                self.disable_log_channel(channel, exc)
                return

    async def archive_limits(self) -> ArchiveLimits:
        return ArchiveLimits(
            max_archive_bytes=await self.setting_int("max_archive_mb") * 1024 * 1024,
            max_extracted_bytes=await self.setting_int("max_extracted_mb") * 1024 * 1024,
            max_files=await self.setting_int("max_files"),
            max_ratio=float(await self.db.get_setting("max_ratio", "250") or 250),
        )

    async def setting_bool(self, key: str) -> bool:
        return (await self.db.get_setting(key, "0") or "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    async def setting_int(self, key: str) -> int:
        raw = await self.db.get_setting(key, DEFAULT_SETTINGS.get(key, "0"))
        try:
            return int(float(raw or 0))
        except ValueError:
            return int(float(DEFAULT_SETTINGS.get(key, "0")))

    async def wait_flood_message(
        self,
        message: Any | None,
        seconds: int | float,
        action: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Wait out Telegram rate limits with a conservative ten-second UI cadence."""
        wait_seconds = max(0, int(math.ceil(float(seconds))))
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = max(0, int(math.ceil(deadline - time.monotonic())))
            if message is not None:
                text = (
                    "⏸ <b>TELEGRAM FLOODWAIT</b>\n\n"
                    f"<code>{progress_bar(wait_seconds - remaining, max(wait_seconds, 1))}</code>  "
                    f"<b>Retrying in {human_duration(remaining)}</b>\n"
                    f"• Action: {esc(action[:72])}\n"
                    "• Queue: saved • retry: automatic"
                    + FOOTER
                )
                try:
                    await message.edit(text, buttons=None, parse_mode="html")
                except FloodWaitError as exc:
                    deadline = max(deadline, time.monotonic() + max(1, exc.seconds))
                except RPCError:
                    message = None
            remaining = max(0, int(math.ceil(deadline - time.monotonic())))
            if remaining <= 0:
                return bool(cancel_event and cancel_event.is_set())
            sleep_for = min(10, remaining)
            if cancel_event is None:
                await asyncio.sleep(sleep_for)
                continue
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=sleep_for)
                return True
            except TimeoutError:
                pass

    async def send_message_with_flood_wait(
        self,
        chat_id: int,
        text: str,
        *,
        buttons: Any = None,
        status_message: Any | None = None,
        action: str = "sending a message",
    ) -> Any:
        while True:
            try:
                return await self.client.send_message(
                    chat_id,
                    text,
                    buttons=buttons,
                    parse_mode="html",
                )
            except FloodWaitError as exc:
                await self.wait_flood_message(
                    status_message,
                    exc.seconds,
                    action,
                )

    async def reply_with_flood_wait(
        self,
        event: Any,
        text: str,
        *,
        buttons: Any = None,
        status_message: Any | None = None,
        action: str = "sending a reply",
    ) -> Any:
        while True:
            try:
                return await event.reply(text, buttons=buttons, parse_mode="html")
            except FloodWaitError as exc:
                await self.wait_flood_message(
                    status_message,
                    exc.seconds,
                    action,
                )

    async def edit_message_with_flood_wait(
        self,
        message: Any,
        text: str,
        *,
        buttons: Any = None,
        action: str = "updating a message",
    ) -> Any:
        while True:
            try:
                return await message.edit(text, buttons=buttons, parse_mode="html")
            except FloodWaitError as exc:
                # The edit itself is rate-limited, so wait without repeatedly attempting the
                # same edit merely to display its timer.
                await self.wait_flood_message(None, exc.seconds, action)
            except RPCError:
                return None

    async def safe_edit(self, event: Any, text: str, buttons: Any) -> None:
        try:
            await event.edit(text, buttons=buttons, parse_mode="html")
        except FloodWaitError as exc:
            await self.wait_flood_message(None, exc.seconds, "updating the current menu")
            with contextlib.suppress(RPCError):
                await event.edit(text, buttons=buttons, parse_mode="html")
        except RPCError:
            with contextlib.suppress(RPCError):
                await self.send_message_with_flood_wait(
                    int(event.chat_id),
                    text,
                    buttons=buttons,
                    action="replacing the current menu",
                )
