from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from acebot.__main__ import sync_log_channel
from acebot.config import Config


class ConfigurationCliTests(unittest.TestCase):
    def test_custom_ace_env_file_and_optional_log_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / "ace.env"
            fake_token = "1234567890:" + ("a" * 30)
            env_file.write_text(
                "\n".join(
                    (
                        "API_ID=123456",
                        "API_HASH=0123456789abcdef0123456789abcdef",
                        f"BOT_TOKEN={fake_token}",
                        "OWNER_ID=8846919574",
                        "LOG_CHANNEL_ID=",
                        f"DATA_DIR={root / 'data'}",
                        f"WORK_DIR={root / 'work'}",
                        "LOG_LEVEL=INFO",
                    )
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"ACE_ENV_FILE": str(env_file)}, clear=True),
                patch("acebot.config.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            ):
                config = Config.load(create_dirs=False)
            self.assertEqual(config.api_id, 123456)
            self.assertEqual(config.owner_id, 8846919574)
            self.assertIsNone(config.log_channel_id)
            self.assertEqual(config.data_dir, root / "data")

    def test_sync_log_channel_updates_existing_database_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "acebot.sqlite3"
            connection = sqlite3.connect(database_path)
            connection.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO settings VALUES ('log_channel_id', '-1001', 'old')"
            )
            connection.commit()
            connection.close()

            config = SimpleNamespace(database_path=database_path, log_channel_id=-1002)
            self.assertEqual(sync_log_channel(config), 0)
            connection = sqlite3.connect(database_path)
            value = connection.execute(
                "SELECT value FROM settings WHERE key='log_channel_id'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(value, "-1002")

            config.log_channel_id = None
            self.assertEqual(sync_log_channel(config), 0)
            connection = sqlite3.connect(database_path)
            value = connection.execute(
                "SELECT value FROM settings WHERE key='log_channel_id'"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(value, "disabled")


if __name__ == "__main__":
    unittest.main()
