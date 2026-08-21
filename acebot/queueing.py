from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol


class UserOwnedItem(Protocol):
    user_id: int


def next_eligible_index(
    items: Sequence[UserOwnedItem], active_user_ids: Collection[int]
) -> int | None:
    """Return the earliest FIFO item whose user does not already have a worker."""
    for index, item in enumerate(items):
        if item.user_id not in active_user_ids:
            return index
    return None
