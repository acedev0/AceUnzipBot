from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from acebot.__main__ import sync_log_channel
from acebot.config import Config


class ConfigurationCliTests(unittest.TestCase):
    @staticmethod
    def write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

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

    def test_control_command_runs_python_from_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            python_bin = project / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            marker = root / "python-cwd"
            arguments = root / "python-args"
            self.write_executable(
                python_bin,
                "#!/bin/sh\n"
                "printf '%s\\n' \"$PWD\" > \"$ACE_TEST_CWD_FILE\"\n"
                "printf '%s\\n' \"$*\" > \"$ACE_TEST_ARGS_FILE\"\n",
            )
            env_file = root / "ace.env"
            env_file.touch()
            record = root / "install.conf"
            record.write_text(
                "\n".join(
                    (
                        f"PROJECT_DIR={project}",
                        f"ENV_FILE={env_file}",
                        "SERVICE_NAME=ace-test.service",
                        "SERVICE_USER=ace-test",
                        "SERVICE_GROUP=ace-test",
                    )
                ),
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_executable(
                fake_bin / "id",
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = '-u' ]; then echo 0; exit 0; fi\n"
                "exec /usr/bin/id \"$@\"\n",
            )
            outside = root / "outside"
            outside.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "ACE_INSTALL_RECORD": str(record),
                    "ACE_TEST_CWD_FILE": str(marker),
                    "ACE_TEST_ARGS_FILE": str(arguments),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )
            result = subprocess.run(
                [str(Path(__file__).parents[1] / "scripts" / "aceunzip"), "doctor"],
                cwd=outside,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(marker.read_text(encoding="utf-8").strip()), project)
            self.assertEqual(arguments.read_text(encoding="utf-8").strip(), "-m acebot --check")

    def test_bootstrap_installer_pins_requested_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote_installer = root / "remote" / "install.sh"
            remote_installer.parent.mkdir()
            remote_installer.write_text(
                (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            remote_installer.chmod(0o755)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_executable(
                fake_bin / "id",
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = '-u' ]; then echo 0; exit 0; fi\n"
                "exec /usr/bin/id \"$@\"\n",
            )
            self.write_executable(fake_bin / "apt-get", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "git",
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" > \"$ACE_TEST_GIT_ARGS\"\n"
                "for value in \"$@\"; do target=\"$value\"; done\n"
                "mkdir -p \"$target\"\n"
                "cat > \"$target/install.sh\" <<'EOF'\n"
                "#!/bin/sh\n"
                "printf '%s|%s\\n' \"${ACE_VERSION:-}\" \"${1:-}\" > \"$ACE_TEST_CLONE_ENV\"\n"
                "EOF\n"
                "chmod +x \"$target/install.sh\"\n",
            )
            install_dir = root / "installed"
            git_args = root / "git-args"
            clone_env = root / "clone-env"
            env = os.environ.copy()
            env.update(
                {
                    "ACE_INSTALL_DIR": str(install_dir),
                    "ACE_TEST_GIT_ARGS": str(git_args),
                    "ACE_TEST_CLONE_ENV": str(clone_env),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                }
            )
            result = subprocess.run(
                [str(remote_installer), "2.0.1"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--branch v2.0.1", git_args.read_text(encoding="utf-8"))
            self.assertEqual(clone_env.read_text(encoding="utf-8").strip(), "v2.0.1|--bootstrap")


if __name__ == "__main__":
    unittest.main()
