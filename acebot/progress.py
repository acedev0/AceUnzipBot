from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telethon import Button
from telethon.errors import FloodWaitError, MessageNotModifiedError

from .ui import esc, human_duration, human_size, progress_bar

STAGE_ICONS = {
    "queued": "⏳",
    "download": "📥",
    "verify": "🛡",
    "extract": "📦",
    "upload": "📤",
    "finish": "✅",
}
PIPELINE = ("download", "verify", "extract", "upload")
PIPELINE_LABELS = {
    "download": "DL",
    "verify": "CHECK",
    "extract": "UNZIP",
    "upload": "SEND",
}
PROGRESS_WIDTH = 12


class DeferredStatusMessage:
    """Create a Telegram status message only when its first edit is requested."""

    def __init__(
        self,
        sender: Callable[[str, Any], Awaitable[Any]],
    ) -> None:
        self._sender = sender
        self._message: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def materialized(self) -> bool:
        return self._message is not None

    async def edit(
        self,
        text: str,
        *,
        buttons: Any = None,
        parse_mode: str | None = None,
    ) -> Any:
        async with self._lock:
            if self._message is None:
                self._message = await self._sender(text, buttons)
                return self._message
            return await self._message.edit(
                text,
                buttons=buttons,
                parse_mode=parse_mode,
            )

    async def delete(self) -> None:
        async with self._lock:
            if self._message is None:
                return
            await self._message.delete()


def compact_text(value: str, limit: int = 48) -> str:
    """Keep status lines readable on narrow Telegram phone screens."""
    clean = " ".join(str(value).split())
    if len(clean) <= limit:
        return clean
    head = max(1, (limit - 1) // 2)
    tail = max(1, limit - head - 1)
    return clean[:head] + "…" + clean[-tail:]


def pipeline_text(stage: str) -> tuple[str, str]:
    if stage == "finish":
        current_index = len(PIPELINE)
    elif stage in PIPELINE:
        current_index = PIPELINE.index(stage)
    else:
        current_index = -1
    cells: list[str] = []
    for index, name in enumerate(PIPELINE):
        marker = "✓" if index < current_index else "●" if index == current_index else "○"
        cells.append(f"{marker}{PIPELINE_LABELS[name]}")
    if stage == "finish":
        stage_number = str(len(PIPELINE))
    elif current_index >= 0:
        stage_number = str(current_index + 1)
    else:
        stage_number = "0"
    return "  ".join(cells), f"{stage_number}/{len(PIPELINE)}"


@dataclass(slots=True)
class ProgressReporter:
    message: Any
    job_id: str
    filename: str
    client: Any | None = None
    chat_id: int | None = None
    interval: float = 1.25
    defer_queue_updates: bool = False
    stage: str = "queued"
    started_at: float = field(default_factory=time.monotonic)
    stage_started_at: float = field(default_factory=time.monotonic)
    last_edit_at: float = 0.0
    last_current: int = 0
    last_sample_at: float = field(default_factory=time.monotonic)
    smoothed_speed: float = 0.0
    last_text: str = ""
    last_buttons: Any = None
    flood_blocked_until: float = 0.0
    skipped_empty_files: int = 0
    failed_upload_files: int = 0
    failed_destinations: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.stage_started_at = time.monotonic()
        self.last_sample_at = self.stage_started_at
        self.last_current = 0
        self.smoothed_speed = 0.0

    async def update(
        self,
        current: int,
        total: int,
        *,
        detail: str = "",
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if now < self.flood_blocked_until:
            return
        elapsed_sample = max(0.001, now - self.last_sample_at)
        delta = max(0, current - self.last_current)
        instant_speed = delta / elapsed_sample
        if instant_speed > 0:
            self.smoothed_speed = (
                instant_speed
                if self.smoothed_speed == 0
                else self.smoothed_speed * 0.72 + instant_speed * 0.28
            )
        self.last_current = current
        self.last_sample_at = now

        if not force and now - self.last_edit_at < self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            if not force and now - self.last_edit_at < self.interval:
                return
            self.last_edit_at = now
            total = max(total, 0)
            current = min(max(current, 0), total) if total else max(current, 0)
            percent = (current / total * 100) if total else 0
            eta = (
                ((total - current) / self.smoothed_speed) if self.smoothed_speed and total else None
            )
            icon = STAGE_ICONS.get(self.stage, "⚡")
            pipeline, stage_number = pipeline_text(self.stage)
            stage_label = self.stage.replace("_", " ").upper()
            text = (
                f"{icon} <b>{esc(stage_label)}</b>  <code>{stage_number}</code>\n"
                f"<code>{pipeline}</code>\n\n"
                f"<code>{progress_bar(current, total, width=PROGRESS_WIDTH)}</code>  "
                f"<b>{percent:5.1f}%</b>\n"
                f"📦 <code>{esc(compact_text(self.filename))}</code>\n"
                f"↳ {human_size(current)} / {human_size(total)}\n"
                f"⚡ {human_size(self.smoothed_speed)}/s   ETA {human_duration(eta)}\n"
                f"⏱ {human_duration(now - self.started_at)} elapsed"
            )
            if detail:
                text += f"\n<blockquote>{esc(compact_text(detail, 96))}</blockquote>"
            buttons = [[Button.inline("✖ Cancel", f"job:cancel:{self.job_id}".encode())]]
            self.last_text = text
            self.last_buttons = buttons
            try:
                await self.message.edit(text, buttons=buttons, parse_mode="html")
            except MessageNotModifiedError:
                pass
            except FloodWaitError as exc:
                self.flood_blocked_until = max(
                    self.flood_blocked_until, time.monotonic() + max(1, exc.seconds)
                )
            except Exception:
                # A deleted progress message must never kill the file job.
                pass

    async def relocate(self) -> None:
        """Re-send one status message below newly uploaded files, then remove the old copy."""
        if self.client is None or self.chat_id is None or not self.last_text:
            return
        if time.monotonic() < self.flood_blocked_until:
            return
        async with self._lock:
            old_message = self.message
            try:
                new_message = await self.client.send_message(
                    self.chat_id,
                    self.last_text,
                    buttons=self.last_buttons,
                    parse_mode="html",
                    silent=True,
                )
            except FloodWaitError as exc:
                self.flood_blocked_until = max(
                    self.flood_blocked_until, time.monotonic() + max(1, exc.seconds)
                )
                return
            except Exception:
                return
            self.message = new_message
            with contextlib.suppress(Exception):
                await old_message.delete()

    async def flood_wait(
        self,
        seconds: int | float,
        action: str,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        """Show a rate-limit countdown and return ``True`` if the job was cancelled."""
        wait_seconds = max(0, int(math.ceil(float(seconds))))
        deadline = time.monotonic() + wait_seconds
        self.flood_blocked_until = deadline
        buttons = [[Button.inline("✖ Cancel", f"job:cancel:{self.job_id}".encode())]]
        while True:
            remaining = max(0, int(math.ceil(deadline - time.monotonic())))
            text = (
                "⏸ <b>TELEGRAM FLOODWAIT</b>\n"
                f"📦 <code>{esc(compact_text(self.filename))}</code>\n\n"
                f"<code>{progress_bar(wait_seconds - remaining, max(wait_seconds, 1), width=PROGRESS_WIDTH)}</code>  "
                f"<b>Retrying in {human_duration(remaining)}</b>\n"
                f"<blockquote>{esc(compact_text(action, 96))}</blockquote>\n"
                "<i>Queue position is safe. Retrying automatically.</i>"
            )
            async with self._lock:
                self.last_text = text
                self.last_buttons = buttons
                try:
                    await self.message.edit(text, buttons=buttons, parse_mode="html")
                except MessageNotModifiedError:
                    pass
                except FloodWaitError as exc:
                    deadline = max(deadline, time.monotonic() + max(1, exc.seconds))
                    self.flood_blocked_until = deadline
                except Exception:
                    pass
            remaining = max(0, int(math.ceil(deadline - time.monotonic())))
            if remaining <= 0:
                self.flood_blocked_until = 0.0
                self.last_edit_at = 0.0
                return bool(cancel_event and cancel_event.is_set())
            sleep_for = min(10, remaining)
            if cancel_event is None:
                await asyncio.sleep(sleep_for)
                continue
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=sleep_for)
                self.flood_blocked_until = 0.0
                return True
            except TimeoutError:
                pass

    async def status(self, stage: str, detail: str = "") -> None:
        self.set_stage(stage)
        await self.update(0, 0, detail=detail, force=True)

    async def start(self, stage: str, detail: str = "") -> None:
        """Open the live card at worker start without double-sending deferred cards."""
        await self.status(stage, detail)
        if not self.defer_queue_updates:
            await self.relocate()

    async def queued(self, position: int) -> None:
        if self.defer_queue_updates:
            return
        text = (
            f"⏳ <b>QUEUED</b>  <code>#{max(1, position)}</code>\n\n"
            f"📦 <code>{esc(compact_text(self.filename))}</code>\n"
            f"🆔 <code>{self.job_id}</code>\n\n"
            "<i>Your archives run one at a time in arrival order. You can keep sending files.</i>"
        )
        buttons = [[Button.inline("✖ Cancel", f"job:cancel:{self.job_id}".encode())]]
        async with self._lock:
            self.last_text = text
            self.last_buttons = buttons
            with contextlib.suppress(Exception):
                await self.message.edit(text, buttons=buttons, parse_mode="html")

    async def complete(self, output_size: int, file_count: int, elapsed: float) -> None:
        warning_lines: list[str] = []
        if self.skipped_empty_files:
            warning_lines.append(
                f"⚪ {self.skipped_empty_files} zero-byte file"
                f"{'s' if self.skipped_empty_files != 1 else ''} skipped"
            )
        if self.failed_upload_files:
            warning_lines.append(
                f"⚠️ {self.failed_upload_files} file"
                f"{'s' if self.failed_upload_files != 1 else ''} could not be delivered"
            )
        if self.failed_destinations:
            warning_lines.append(
                f"📡 {self.failed_destinations} destination attempt"
                f"{'s' if self.failed_destinations != 1 else ''} failed"
            )
        title = "DELIVERY COMPLETE" if not warning_lines else "DELIVERY COMPLETE WITH NOTES"
        text = (
            f"✅ <b>{title}</b>\n\n"
            f"📦 <code>{esc(compact_text(self.filename))}</code>\n"
            f"📁 {file_count} file{'s' if file_count != 1 else ''} sent\n"
            f"📤 {human_size(output_size)} delivered\n"
            f"⏱ {human_duration(elapsed)} total"
        )
        if warning_lines:
            text += "\n\n" + "\n".join(warning_lines)
        text += "\n\n<i>Temporary server files cleaned.</i>"
        async with self._lock:
            self.last_text = text
            self.last_buttons = None
            with contextlib.suppress(Exception):
                await self.message.edit(text, buttons=None, parse_mode="html")

    async def failed(self, public_message: str) -> None:
        text = (
            "❌ <b>JOB STOPPED</b>\n\n"
            f"📦 <code>{esc(compact_text(self.filename))}</code>\n"
            f"{esc(public_message)}\n\n"
            "<i>Temporary server files cleaned.</i>"
        )
        async with self._lock:
            self.last_text = text
            self.last_buttons = None
            with contextlib.suppress(Exception):
                await self.message.edit(text, buttons=None, parse_mode="html")
