from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from acebot.app import AceBot, DeliveryError


class FakeDatabase:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str, str | None, str]] = []
        self.mirrors: list[dict[str, object]] = []
        self.users: list[dict[str, object]] = []

    async def save_mirror_destination(
        self, target_id: int, label: str, username: str | None, kind: str
    ) -> None:
        self.saved.append((target_id, label, username, kind))

    async def mirror_destinations(
        self, limit: int | None = 10
    ) -> list[dict[str, object]]:
        return self.mirrors if limit is None else self.mirrors[:limit]

    async def reachable_users(self) -> list[dict[str, object]]:
        return self.users

    async def delete_mirror_destination(self, target_id: int) -> None:
        self.mirrors = [item for item in self.mirrors if item["target_id"] != target_id]


class FakeClient:
    async def get_permissions(self, entity: object, me: object) -> object:
        can_post = bool(getattr(entity, "can_post", True))
        return SimpleNamespace(
            is_banned=False,
            has_left=False,
            is_creator=False,
            is_admin=can_post,
            post_messages=can_post,
        )

    async def get_entity(self, target_id: int) -> object:
        raise ValueError(target_id)


def make_bot() -> AceBot:
    bot = AceBot.__new__(AceBot)
    bot.config = SimpleNamespace(owner_id=1)
    bot.me = SimpleNamespace(id=999)
    bot.db = FakeDatabase()
    bot.client = FakeClient()
    bot.get_log_channel = AsyncMock(return_value=None)
    bot.mirror_destination_cached_at = 1.0
    bot.peer_registry_seen_at = {}
    return bot


class MirrorDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_lists_registered_chats_and_known_users(self) -> None:
        bot = make_bot()
        bot.db.mirrors = [
            {"target_id": -10055, "label": "Archive Channel", "kind": "channel"},
            {"target_id": 1, "label": "Owner", "kind": "user"},
        ]
        bot.db.users = [
            {"user_id": 2, "first_name": "Alice", "username": "alice"},
            {"user_id": 1, "first_name": "Owner", "username": None},
        ]
        destinations = await bot.discover_mirror_destinations()
        self.assertEqual([target.chat_id for target in destinations], [-10055, 2])
        self.assertEqual([target.kind for target in destinations], ["channel", "user"])

    async def test_group_or_channel_update_is_added_to_registry(self) -> None:
        channel = SimpleNamespace(
            id=-10055,
            title="Archive Channel",
            username=None,
            bot=False,
            broadcast=True,
            creator=False,
            admin_rights=SimpleNamespace(post_messages=True),
        )
        event = SimpleNamespace(chat_id=channel.id, get_chat=AsyncMock(return_value=channel))
        bot = make_bot()
        with patch("acebot.app.get_peer_id", return_value=channel.id):
            await bot.remember_accessible_chat(event)
            await bot.remember_accessible_chat(event)
        self.assertEqual(
            bot.db.saved[0],
            (-10055, "Archive Channel", None, "channel"),
        )
        self.assertEqual(len(bot.db.saved), 1)
        self.assertEqual(bot.mirror_destination_cached_at, 0.0)

    async def test_selected_destination_is_revalidated_and_saved(self) -> None:
        channel = SimpleNamespace(
            id=-10077,
            title="Uploads",
            username="uploads",
            bot=False,
            broadcast=True,
            can_post=True,
        )
        bot = make_bot()
        with patch("acebot.app.get_peer_id", return_value=channel.id):
            target = await bot.delivery_target_from_entity(channel, persist=True)
        self.assertEqual((target.chat_id, target.kind), (-10077, "channel"))
        self.assertEqual(bot.db.saved[0], (-10077, "Uploads", "uploads", "channel"))

    async def test_owner_and_non_postable_channel_are_rejected(self) -> None:
        owner = SimpleNamespace(id=1, first_name="Owner", bot=False)
        blocked = SimpleNamespace(
            id=-10088,
            title="Read Only",
            bot=False,
            broadcast=True,
            can_post=False,
        )
        bot = make_bot()
        with patch("acebot.app.get_peer_id", side_effect=lambda entity: entity.id):
            with self.assertRaises(DeliveryError):
                await bot.delivery_target_from_entity(owner, persist=False)
            with self.assertRaises(DeliveryError):
                await bot.delivery_target_from_entity(blocked, persist=False)


if __name__ == "__main__":
    unittest.main()
