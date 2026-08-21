from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sqlite3
import sys
import time

from .config import Config, ConfigError, redacted_summary


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)


def healthcheck() -> int:
    try:
        config = Config.load(create_dirs=False)
        heartbeat = float(config.heartbeat_path.read_text(encoding="utf-8").strip())
    except (ConfigError, OSError, ValueError):
        return 1
    return 0 if time.time() - heartbeat < 45 else 1


def doctor(config: Config) -> int:
    print("Ace Extractor Bot configuration: OK")
    for key, value in redacted_summary(config).items():
        print(f"  {key}: {value}")
    try:
        import cryptg  # noqa: F401

        print("  cryptg: loaded (native MTProto acceleration active)")
    except ImportError:
        print("  cryptg: MISSING")
        return 1
    print("No bot token or API hash was printed.")
    return 0


async def verify_telegram(config: Config) -> int:
    """Validate API credentials and the bot token without touching the live session."""
    from telethon import TelegramClient
    from telethon.sessions import MemorySession

    client = TelegramClient(
        MemorySession(),
        config.api_id,
        config.api_hash,
        connection_retries=2,
        request_retries=2,
        timeout=15,
    )
    try:
        await asyncio.wait_for(client.start(bot_token=config.bot_token), timeout=45)
        me = await asyncio.wait_for(client.get_me(), timeout=15)
    finally:
        await client.disconnect()
    print(f"Telegram credentials: OK (@{me.username or 'unnamed_bot'})")
    print("No API hash or bot token was printed.")
    return 0


def sync_log_channel(config: Config) -> int:
    """Make a terminal reconfiguration authoritative over an existing database override."""
    if not config.database_path.exists():
        print("Log channel will be initialized when the bot first starts.")
        return 0
    connection = sqlite3.connect(config.database_path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()
        if table is None:
            print("Log channel will be initialized when the bot first starts.")
            return 0
        value = str(config.log_channel_id) if config.log_channel_id is not None else "disabled"
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES ('log_channel_id', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (value, now),
        )
        connection.commit()
    finally:
        connection.close()
    print("Log-channel preference synchronized.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ace Telegram Archive Extractor Bot")
    parser.add_argument(
        "--check", action="store_true", help="validate configuration and dependencies"
    )
    parser.add_argument(
        "--healthcheck", action="store_true", help="check the running bot heartbeat"
    )
    parser.add_argument(
        "--verify-telegram",
        action="store_true",
        help="test API credentials and bot token using an in-memory Telegram session",
    )
    parser.add_argument(
        "--sync-log-channel",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.healthcheck:
        return healthcheck()
    try:
        config = Config.load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Run ./setup.sh and then ./install.sh.", file=sys.stderr)
        return 2
    configure_logging(config.log_level)
    if args.check:
        return doctor(config)
    if args.sync_log_channel:
        return sync_log_channel(config)
    if args.verify_telegram:
        try:
            return asyncio.run(verify_telegram(config))
        except Exception as exc:
            print(
                f"Telegram credential verification failed ({type(exc).__name__}).",
                file=sys.stderr,
            )
            print("The active configuration was not changed.", file=sys.stderr)
            return 3

    try:
        import uvloop

        uvloop.install()
    except ImportError:
        logging.getLogger("acebot").warning("uvloop unavailable; using standard asyncio")

    from .app import AceBot

    return asyncio.run(AceBot(config).run())


if __name__ == "__main__":
    raise SystemExit(main())
