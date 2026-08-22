from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEGACY_WELCOME_TEXT = (
    "Drop an archive here and I’ll securely unpack it, inspect it, and return every file "
    "with live download, extraction, and upload progress."
)
LEGACY_HELP_TEXT = (
    "Send a ZIP, RAR, 7Z, TAR, GZ, BZ2, XZ, ZST, or ISO archive as a document. "
    "For a protected archive, use its caption `password: your-password` or reply with "
    "/password when asked."
)

DEFAULT_SETTINGS: dict[str, str] = {
    "access_mode": "owner",
    "maintenance": "0",
    "log_events": "1",
    "log_archives": "1",
    "welcome_text": "Send an archive; I’ll safely unpack and return its files.",
    "help_text": "Send ZIP, RAR, 7Z, TAR, GZ, XZ, ZST, or ISO as a document.",
    "completion_caption": "Extracted by {bot} • {index}/{total}",
    "max_archive_mb": "1900",
    "max_extracted_mb": "6000",
    "max_files": "250",
    "max_ratio": "250",
    "max_concurrent_jobs": "1",
    "job_timeout_seconds": "7200",
    "progress_interval": "1.25",
    "cleanup_minutes": "15",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    """Small SQLite store with serialized async access and WAL durability."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                jobs INTEGER NOT NULL DEFAULT 0,
                bytes_in INTEGER NOT NULL DEFAULT 0,
                bytes_out INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                upload_mode TEXT NOT NULL DEFAULT 'auto',
                caption_template TEXT,
                thumbnail_path TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                archive_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                input_size INTEGER NOT NULL DEFAULT 0,
                output_size INTEGER NOT NULL DEFAULT 0,
                file_count INTEGER NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_user_id_idx ON jobs(user_id);
            CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs(created_at);

            CREATE TABLE IF NOT EXISTS mirror_destinations (
                target_id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                username TEXT,
                kind TEXT NOT NULL,
                last_used TEXT NOT NULL
            );
            """
        )
        legacy_access = self._connection.execute(
            "SELECT value FROM settings WHERE key='access_mode'"
        ).fetchone()
        legacy_concurrency = self._connection.execute(
            "SELECT value FROM settings WHERE key='max_concurrent_jobs'"
        ).fetchone()
        defaults_migrated = self._connection.execute(
            "SELECT value FROM settings WHERE key='defaults_v12_migrated'"
        ).fetchone()
        compact_ui_migrated = self._connection.execute(
            "SELECT value FROM settings WHERE key='defaults_v21_compact_ui'"
        ).fetchone()
        now = utc_now()
        self._connection.executemany(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            [(key, value, now) for key, value in DEFAULT_SETTINGS.items()],
        )
        if defaults_migrated is None:
            # v1.1 shipped as private/2. Upgrade that combined baseline once, while
            # preserving public mode and its existing worker setting.
            if legacy_access is not None and legacy_access["value"] == "private":
                self._connection.execute(
                    "UPDATE settings SET value='owner', updated_at=? WHERE key='access_mode'",
                    (now,),
                )
            if (
                legacy_access is not None
                and legacy_access["value"] == "private"
                and legacy_concurrency is not None
                and legacy_concurrency["value"] == "2"
            ):
                self._connection.execute(
                    "UPDATE settings SET value='1', updated_at=? "
                    "WHERE key='max_concurrent_jobs'",
                    (now,),
                )
            self._connection.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES "
                "('defaults_v12_migrated', '1', ?)",
                (now,),
            )
        if compact_ui_migrated is None:
            # Replace only untouched legacy copy. Owner-customized welcome/help text is preserved.
            self._connection.execute(
                "UPDATE settings SET value=?, updated_at=? WHERE key='welcome_text' AND value=?",
                (DEFAULT_SETTINGS["welcome_text"], now, LEGACY_WELCOME_TEXT),
            )
            self._connection.execute(
                "UPDATE settings SET value=?, updated_at=? WHERE key='help_text' AND value=?",
                (DEFAULT_SETTINGS["help_text"], now, LEGACY_HELP_TEXT),
            )
            self._connection.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES "
                "('defaults_v21_compact_ui', '1', ?)",
                (now,),
            )
        self._connection.execute(
            "UPDATE jobs SET status='interrupted', finished_at=? WHERE status IN ('queued','running')",
            (now,),
        )
        self._connection.commit()

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                self._connection.commit()
                self._connection.close()
                self._connection = None

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been opened")
        return self._connection

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self._lock:
            row = self._conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            self._conn().execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )
            self._conn().commit()

    async def settings(self) -> dict[str, str]:
        async with self._lock:
            rows = self._conn().execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        now = utc_now()
        async with self._lock:
            self._conn().execute(
                """
                INSERT INTO users(user_id, username, first_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_seen=excluded.last_seen
                """,
                (user_id, username, first_name, now, now),
            )
            self._conn().execute(
                "INSERT OR IGNORE INTO user_settings(user_id) VALUES (?)", (user_id,)
            )
            self._conn().commit()

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self._lock:
            row = self._conn().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    async def set_user_flag(self, user_id: int, flag: str, enabled: bool) -> None:
        if flag not in {"approved", "banned", "is_admin"}:
            raise ValueError("Invalid user flag")
        now = utc_now()
        async with self._lock:
            self._conn().execute(
                """
                INSERT OR IGNORE INTO users(user_id, first_seen, last_seen) VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
            self._conn().execute(
                f"UPDATE users SET {flag}=? WHERE user_id=?", (1 if enabled else 0, user_id)
            )
            self._conn().commit()

    async def allowed_users(self) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._conn()
                .execute(
                    "SELECT user_id, username, first_name, approved, is_admin FROM users "
                    "WHERE approved=1 OR is_admin=1 ORDER BY is_admin DESC, user_id"
                )
                .fetchall()
            )
        return [dict(row) for row in rows]

    async def get_user_settings(self, user_id: int) -> dict[str, Any]:
        async with self._lock:
            row = (
                self._conn()
                .execute(
                    "SELECT upload_mode, caption_template, thumbnail_path FROM user_settings WHERE user_id=?",
                    (user_id,),
                )
                .fetchone()
            )
        if row:
            return dict(row)
        return {"upload_mode": "auto", "caption_template": None, "thumbnail_path": None}

    async def set_user_setting(self, user_id: int, field: str, value: str | None) -> None:
        if field not in {"upload_mode", "caption_template", "thumbnail_path"}:
            raise ValueError("Invalid user setting")
        async with self._lock:
            self._conn().execute(
                "INSERT OR IGNORE INTO user_settings(user_id) VALUES (?)", (user_id,)
            )
            self._conn().execute(
                f"UPDATE user_settings SET {field}=? WHERE user_id=?", (value, user_id)
            )
            self._conn().commit()

    async def create_job(
        self, job_id: str, user_id: int, archive_name: str, input_size: int
    ) -> None:
        async with self._lock:
            self._conn().execute(
                """
                INSERT INTO jobs(job_id, user_id, archive_name, status, created_at, input_size)
                VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (job_id, user_id, archive_name, utc_now(), input_size),
            )
            self._conn().commit()

    async def update_job_status(self, job_id: str, status: str) -> None:
        async with self._lock:
            self._conn().execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
            self._conn().commit()

    async def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        output_size: int = 0,
        file_count: int = 0,
        elapsed_seconds: float = 0,
        error: str | None = None,
    ) -> None:
        safe_error = error[:1000] if error else None
        async with self._lock:
            self._conn().execute(
                """
                UPDATE jobs SET status=?, finished_at=?, output_size=?, file_count=?,
                    elapsed_seconds=?, error=? WHERE job_id=?
                """,
                (
                    status,
                    utc_now(),
                    output_size,
                    file_count,
                    elapsed_seconds,
                    safe_error,
                    job_id,
                ),
            )
            if status == "completed":
                row = (
                    self._conn()
                    .execute("SELECT user_id, input_size FROM jobs WHERE job_id=?", (job_id,))
                    .fetchone()
                )
                if row:
                    self._conn().execute(
                        """
                        UPDATE users SET jobs=jobs+1, bytes_in=bytes_in+?, bytes_out=bytes_out+?
                        WHERE user_id=?
                        """,
                        (row["input_size"], output_size, row["user_id"]),
                    )
            self._conn().commit()

    async def global_stats(self) -> dict[str, int]:
        async with self._lock:
            conn = self._conn()
            user_row = conn.execute(
                "SELECT COUNT(*) users, COALESCE(SUM(jobs),0) jobs, "
                "COALESCE(SUM(bytes_in),0) bytes_in, COALESCE(SUM(bytes_out),0) bytes_out FROM users"
            ).fetchone()
            active_row = conn.execute(
                "SELECT COUNT(*) active FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()
            completed_row = conn.execute(
                "SELECT COUNT(*) completed FROM jobs WHERE status='completed'"
            ).fetchone()
        return {
            "users": int(user_row["users"]),
            "jobs": int(user_row["jobs"]),
            "bytes_in": int(user_row["bytes_in"]),
            "bytes_out": int(user_row["bytes_out"]),
            "active": int(active_row["active"]),
            "completed": int(completed_row["completed"]),
        }

    async def user_ids(self, *, include_banned: bool = False) -> list[int]:
        query = "SELECT user_id FROM users"
        if not include_banned:
            query += " WHERE banned=0"
        async with self._lock:
            rows = self._conn().execute(query).fetchall()
        return [int(row["user_id"]) for row in rows]

    async def reachable_users(self) -> list[dict[str, Any]]:
        """Known non-banned private users, most recently seen first."""
        async with self._lock:
            rows = (
                self._conn()
                .execute(
                    "SELECT user_id, username, first_name, last_seen FROM users "
                    "WHERE banned=0 ORDER BY last_seen DESC, user_id"
                )
                .fetchall()
            )
        return [dict(row) for row in rows]

    async def recent_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._lock:
            rows = (
                self._conn()
                .execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
                .fetchall()
            )
        return [dict(row) for row in rows]

    async def save_mirror_destination(
        self,
        target_id: int,
        label: str,
        username: str | None,
        kind: str,
    ) -> None:
        async with self._lock:
            self._conn().execute(
                """
                INSERT INTO mirror_destinations(target_id, label, username, kind, last_used)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    label=excluded.label,
                    username=excluded.username,
                    kind=excluded.kind,
                    last_used=excluded.last_used
                """,
                (target_id, label[:100], username, kind[:20], utc_now()),
            )
            self._conn().commit()

    async def mirror_destinations(self, limit: int | None = 10) -> list[dict[str, Any]]:
        query = (
            "SELECT target_id, label, username, kind, last_used "
            "FROM mirror_destinations ORDER BY last_used DESC, rowid DESC"
        )
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        async with self._lock:
            rows = self._conn().execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    async def delete_mirror_destination(self, target_id: int) -> None:
        async with self._lock:
            self._conn().execute(
                "DELETE FROM mirror_destinations WHERE target_id=?", (target_id,)
            )
            self._conn().commit()

    async def export_settings_json(self) -> str:
        return json.dumps(await self.settings(), indent=2, ensure_ascii=False, sort_keys=True)

    async def import_settings(self, values: dict[str, Any], allowed: Iterable[str]) -> int:
        count = 0
        allowed_set = set(allowed)
        for key, value in values.items():
            if key in allowed_set and isinstance(value, (str, int, float, bool)):
                await self.set_setting(key, str(value))
                count += 1
        return count
