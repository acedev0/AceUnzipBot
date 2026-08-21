from __future__ import annotations

from collections.abc import Iterable

from .archives import looks_like_archive
from .models import BulkEntry


def bulk_rejection_reason(entry: BulkEntry, max_archive_bytes: int) -> str | None:
    """Return a user-facing pre-check failure, or ``None`` for a queue candidate."""
    if not looks_like_archive(entry.filename):
        return "unsupported format"
    if entry.size <= 0:
        return "Telegram reported an invalid file size"
    if entry.size > max_archive_bytes:
        return "larger than the owner’s per-archive limit"
    return None


def partition_bulk_entries(
    entries: Iterable[BulkEntry], max_archive_bytes: int
) -> tuple[tuple[BulkEntry, ...], tuple[tuple[BulkEntry, str], ...]]:
    """Preserve arrival order while separating queueable and ignored documents."""
    accepted: list[BulkEntry] = []
    rejected: list[tuple[BulkEntry, str]] = []
    for entry in entries:
        reason = bulk_rejection_reason(entry, max_archive_bytes)
        if reason is None:
            accepted.append(entry)
        else:
            rejected.append((entry, reason))
    return tuple(accepted), tuple(rejected)
