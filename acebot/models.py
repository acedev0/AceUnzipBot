from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PendingInput:
    action: str
    prompt_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    chat_id: int
    label: str
    kind: str = "chat"


@dataclass(slots=True)
class PendingOwnerArchive:
    token: str
    source_events: tuple[Any, ...]
    prompt_message: Any
    created_at: float = field(default_factory=time.monotonic)
    destinations: tuple[DeliveryTarget, ...] = ()
    is_bulk: bool = False


@dataclass(frozen=True, slots=True)
class BulkEntry:
    source_event: Any
    filename: str
    size: int


@dataclass(slots=True)
class BulkSession:
    user_id: int
    chat_id: int
    status_message: Any
    entries: list[BulkEntry] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    closed: bool = False
    last_status_edit_at: float = 0.0
    status_updates_blocked_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass(slots=True)
class QueuedArchive:
    job_id: str
    user_id: int
    chat_id: int
    source_message: Any
    archive_name: str
    archive_size: int
    delivery_targets: tuple[DeliveryTarget, ...]
    reporter: Any
    initial_password: str | None = None
    enqueued_at: float = field(default_factory=time.monotonic)
    displayed_position: int | None = None
    is_queued: bool = True


@dataclass(slots=True)
class ActiveJob:
    job_id: str
    user_id: int
    chat_id: int
    source_message: Any
    archive_name: str
    workspace: Path
    reserved_bytes: int = 0
    delivery_targets: tuple[DeliveryTarget, ...] = ()
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    password_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=3))
    task: asyncio.Task[Any] | None = None
    attempts: int = 0
