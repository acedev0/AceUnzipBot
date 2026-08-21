from __future__ import annotations

import unittest
from dataclasses import dataclass

from acebot.queueing import next_eligible_index


@dataclass
class Item:
    user_id: int
    name: str


class QueueSelectionTests(unittest.TestCase):
    def test_fifo_selects_oldest_item(self) -> None:
        items = [Item(1, "first"), Item(2, "second"), Item(1, "third")]
        self.assertEqual(next_eligible_index(items, set()), 0)

    def test_active_user_stays_sequential_while_other_users_can_run(self) -> None:
        items = [Item(1, "same-user-next"), Item(2, "other-user")]
        self.assertEqual(next_eligible_index(items, {1}), 1)
        self.assertIsNone(next_eligible_index(items, {1, 2}))

    def test_selection_has_no_queue_length_cap(self) -> None:
        items = [Item(1, f"archive-{index}") for index in range(2500)]
        self.assertEqual(next_eligible_index(items, set()), 0)
        self.assertIsNone(next_eligible_index(items, {1}))


if __name__ == "__main__":
    unittest.main()
