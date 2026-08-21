from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when deployment settings are incomplete or invalid."""


def _path_from_env(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _int_env(name: str, *, optional: bool = False) -> int | None:
    raw = os.getenv(name, "").strip()
    if optional and not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a numeric Telegram ID") from exc
    return value


@dataclass(frozen=True, slots=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    log_channel_id: int | None
    data_dir: Path
    work_dir: Path
    assets_dir: Path
    log_level: str
    seven_zip: str
    ffmpeg: str
    ffprobe: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / "acebot.sqlite3"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "acebot"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    @classmethod
    def load(cls, *, create_dirs: bool = True) -> Config:
        env_file = Path(os.getenv("ACE_ENV_FILE", PROJECT_ROOT / ".env")).expanduser()
        load_dotenv(env_file, override=False)

        api_id = _int_env("API_ID")
        owner_id = _int_env("OWNER_ID")
        assert api_id is not None and owner_id is not None
        api_hash = os.getenv("API_HASH", "").strip()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        log_channel_id = _int_env("LOG_CHANNEL_ID", optional=True)

        if api_id <= 0:
            raise ConfigError("API_ID must be positive")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
            raise ConfigError("API_HASH must be the 32-character value from my.telegram.org")
        if not re.fullmatch(r"\d{6,15}:[A-Za-z0-9_-]{20,}", bot_token):
            raise ConfigError("BOT_TOKEN does not look like a token from @BotFather")
        if owner_id <= 0:
            raise ConfigError("OWNER_ID must be your positive numeric Telegram user ID")
        if log_channel_id is not None and log_channel_id >= 0:
            raise ConfigError("LOG_CHANNEL_ID should be a negative channel/group ID")

        # Prefer plugin-capable 7z over standalone 7zz so Ubuntu's RAR plugin is used.
        seven_zip = shutil.which("7z") or shutil.which("7zz") or ""
        ffmpeg = shutil.which("ffmpeg") or ""
        ffprobe = shutil.which("ffprobe") or ""
        missing = [
            label
            for label, value in (("7z", seven_zip), ("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
            if not value
        ]
        if missing:
            raise ConfigError(f"Missing system tools: {', '.join(missing)}. Run ./install.sh")

        data_dir = _path_from_env("DATA_DIR", "./data")
        work_dir = _path_from_env("WORK_DIR", "./work")
        if data_dir == work_dir:
            raise ConfigError("DATA_DIR and WORK_DIR must be different directories")

        if create_dirs:
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            owner_id=owner_id,
            log_channel_id=log_channel_id,
            data_dir=data_dir,
            work_dir=work_dir,
            assets_dir=PROJECT_ROOT / "assets",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            seven_zip=seven_zip,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )


def redacted_summary(config: Config) -> dict[str, str | int]:
    """Return deploy facts without ever including API secrets."""
    return {
        "api_id": config.api_id,
        "owner_id": config.owner_id,
        "log_channel": config.log_channel_id or "not set",
        "data_dir": str(config.data_dir),
        "work_dir": str(config.work_dir),
        "7z": config.seven_zip,
        "ffmpeg": config.ffmpeg,
    }
