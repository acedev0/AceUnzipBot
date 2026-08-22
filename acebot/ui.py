from __future__ import annotations

import html
import math
from collections.abc import Sequence
from datetime import datetime, timezone
from string import Formatter
from typing import Any

from telethon import Button

from .models import DeliveryTarget

BRAND_NAME = "Ace Extractor"
CONTACT_USERNAME = "Aceddev"
CONTACT_EMAIL = "Acedevmc@gmail.com"
MIRROR_DESTINATIONS_PER_PAGE = 8

FOOTER = (
    "\n\n<blockquote><b>Made by Ace</b> • "
    '<a href="https://t.me/Aceddev">@Aceddev</a> • '
    "Acedevmc@gmail.com</blockquote>"
)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def human_size(size: int | float) -> str:
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def human_duration(seconds: int | float | None) -> str:
    if seconds is None or not math.isfinite(float(seconds)) or seconds < 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def progress_bar(current: int, total: int, width: int = 12) -> str:
    ratio = min(1.0, max(0.0, current / total)) if total else 0.0
    filled = int(round(ratio * width))
    return "▰" * filled + "▱" * (width - filled)


def render_welcome(
    first_name: str,
    bot_name: str,
    custom_text: str,
    *,
    archives_logged: bool,
) -> str:
    privacy = (
        "private archive logging is enabled"
        if archives_logged
        else "temporary files are cleaned after processing"
    )
    return (
        f"⚡ <b>{esc(bot_name.upper())}</b>\n"
        f"👋 <b>{esc(first_name or 'there')}</b>\n\n"
        f"• {esc(custom_text)}\n"
        "• Flow: <code>DOWNLOAD → EXTRACT → UPLOAD</code>\n"
        "• Formats: ZIP • RAR • 7Z • TAR • GZ • XZ • ZST • ISO\n"
        "• Output: every non-empty file\n"
        "• Bulk: <code>/bulk</code> → files → <code>/donebulk</code>\n"
        "• Safety: passwords • bomb checks • instant cancel\n"
        f"• Privacy: {esc(privacy)}" + FOOTER
    )


def render_help(custom_text: str) -> str:
    return (
        "🧭 <b>QUICK GUIDE</b>\n\n"
        f"• {esc(custom_text)}\n"
        "• Single: send an archive as a <b>document</b>\n"
        "• Queue: send more files; FIFO handles them one by one\n"
        "• Bulk: <code>/bulk</code> → files → <code>/donebulk</code>\n"
        "• Password: caption <code>password: value</code> or <code>/password value</code>\n"
        "• Output: media when verified; otherwise document\n"
        "• Zero-byte files: skipped without stopping the job\n\n"
        "<b>Commands</b>\n"
        "• <code>/start</code> • <code>/settings</code> • <code>/privacy</code>\n"
        "• <code>/bulk</code> • <code>/donebulk</code> • <code>/cancelbulk</code>\n"
        "• <code>/cancel</code> • <code>/myid</code>" + FOOTER
    )


def render_privacy(
    *, archives_logged: bool, cleanup_minutes: int, logging_configured: bool = True
) -> str:
    if archives_logged:
        logging_line = "Archive copies and basic IDs may be retained in the private log channel."
    elif logging_configured:
        logging_line = "Archive copies are off; operational metadata may be logged."
    else:
        logging_line = "No private log channel is active."
    return (
        "🔐 <b>PRIVACY</b>\n\n"
        f"• Logs: {esc(logging_line)}\n"
        f"• Cleanup: working files + stale folders (~{cleanup_minutes} min)\n"
        "• Passwords: memory only\n"
        "• Records: IDs, names, sizes, results, transfer totals\n"
        "• Control: <code>/admin → Logs</code>" + FOOTER
    )


def home_buttons(*, owner: bool = False) -> list[list[Button]]:
    rows: list[list[Button]] = [
        [
            Button.inline("📦 Single archive", b"home:upload"),
            Button.inline("📚 Bulk mode", b"home:bulk"),
        ],
        [Button.inline("⚙️ My settings", b"user:settings"), Button.inline("🧭 Help", b"home:help")],
        [Button.inline("🔐 Privacy", b"home:privacy"), Button.inline("🆔 My ID", b"home:myid")],
    ]
    if owner:
        rows.append([Button.inline("👑 Owner menu", b"admin:home")])
    return rows


def render_bulk_summary(
    accepted: Sequence[tuple[str, int]],
    rejected: Sequence[tuple[str, str]],
    *,
    max_length: int = 3900,
) -> tuple[str, ...]:
    """Render every bulk filename across Telegram-safe HTML message pages."""
    heading = (
        "📚 <b>BULK CHECK</b>\n\n"
        f"• Supported: <b>{len(accepted)}</b>\n"
        f"• Ignored: <b>{len(rejected)}</b>\n"
        "• Order: FIFO • content check at worker start"
    )
    lines: list[str] = []
    if accepted:
        lines.extend(("", "<b>WILL EXTRACT — ONE BY ONE</b>"))
        lines.extend(
            f"✅ {index}. <code>{esc(name)}</code> • {human_size(size)}"
            for index, (name, size) in enumerate(accepted, start=1)
        )
    if rejected:
        lines.extend(("", "<b>IGNORED</b>"))
        lines.extend(
            f"⚠️ {index}. <code>{esc(name)}</code> — {esc(reason)}"
            for index, (name, reason) in enumerate(rejected, start=1)
        )
    if not accepted and not rejected:
        lines.extend(("", "No documents were collected."))

    pages: list[str] = []
    current = heading
    continuation = "📚 <b>BULK CHECK • CONTINUED</b>"
    for line in lines:
        candidate = current + "\n" + line
        if len(candidate + FOOTER) > max_length and current != heading:
            pages.append(current + FOOTER)
            current = continuation + "\n" + line
        else:
            current = candidate
    pages.append(current + FOOTER)
    return tuple(pages)


def back_home_button() -> list[list[Button]]:
    return [[Button.inline("‹ Home", b"home:start")]]


def user_settings_buttons(mode: str, has_thumb: bool, has_caption: bool) -> list[list[Button]]:
    labels = {
        "auto": "✨ Auto media",
        "media": "🎬 Force media",
        "document": "📄 Force document",
    }
    mode_rows = [
        [
            Button.inline(("✓ " if mode == key else "") + label, f"user:mode:{key}".encode())
            for key, label in labels.items()
        ]
    ]
    return mode_rows + [
        [
            Button.inline(
                "🖼 Replace video thumb" if has_thumb else "🖼 Set video thumb",
                b"user:thumb:set",
            ),
            Button.inline("🗑 Remove" if has_thumb else "Auto video thumb ✓", b"user:thumb:del"),
        ],
        [
            Button.inline(
                "✍️ Edit caption" if has_caption else "✍️ Custom caption", b"user:caption:set"
            ),
            Button.inline("↺ Default caption", b"user:caption:del"),
        ],
        [Button.inline("‹ Back home", b"home:start")],
    ]


def admin_home_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("🔐 Access", b"admin:access"),
            Button.inline("🎨 Appearance", b"admin:appearance"),
        ],
        [Button.inline("📡 Logs", b"admin:logs"), Button.inline("⚙️ Limits", b"admin:limits")],
        [
            Button.inline("📊 Live stats", b"admin:stats"),
            Button.inline("📣 Broadcast", b"admin:broadcast"),
        ],
        [
            Button.inline("🧹 Clean temp", b"admin:clean"),
            Button.inline("♻️ Restart", b"admin:restart:ask"),
        ],
        [Button.inline("‹ Back home", b"home:start")],
    ]


def admin_access_buttons(mode: str, maintenance: bool) -> list[list[Button]]:
    return [
        [
            Button.inline(("✓ " if mode == "public" else "") + "Public", b"admin:mode:public"),
            Button.inline(("✓ " if mode == "private" else "") + "Private", b"admin:mode:private"),
            Button.inline(("✓ " if mode == "owner" else "") + "Owner only", b"admin:mode:owner"),
        ],
        [Button.inline(f"🛠 Maintenance: {'ON' if maintenance else 'OFF'}", b"admin:maintenance")],
        [
            Button.inline("➕ Approve ID", b"admin:user:approve"),
            Button.inline("➖ Remove ID", b"admin:user:remove"),
        ],
        [
            Button.inline("🚫 Ban ID", b"admin:user:ban"),
            Button.inline("✅ Unban ID", b"admin:user:unban"),
        ],
        [
            Button.inline("🛡 Add admin", b"admin:user:admin"),
            Button.inline("👥 View access list", b"admin:user:list"),
        ],
        [Button.inline("‹ Owner menu", b"admin:home")],
    ]


def admin_appearance_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("🤖 Bot name", b"admin:appearance:name"),
            Button.inline("📝 Short bio", b"admin:appearance:short"),
        ],
        [Button.inline("📖 Bot description", b"admin:appearance:description")],
        [
            Button.inline("👋 Welcome message", b"admin:appearance:welcome"),
            Button.inline("🧭 Help message", b"admin:appearance:help"),
        ],
        [
            Button.inline("🌌 Start picture", b"admin:appearance:startphoto"),
            Button.inline("🖼 Video thumbnail", b"admin:appearance:thumb"),
        ],
        [Button.inline("👤 Set profile picture", b"admin:appearance:profile")],
        [Button.inline("‹ Owner menu", b"admin:home")],
    ]


def owner_delivery_buttons(token: str) -> list[list[Button]]:
    return [
        [Button.inline("👤 Send to you, Sir", f"mirror:mode:{token}:self".encode())],
        [
            Button.inline("🪞 Mirror only", f"mirror:mode:{token}:mirror".encode()),
            Button.inline("👤 You + mirror", f"mirror:mode:{token}:both".encode()),
        ],
        [Button.inline("✖ Cancel", f"mirror:cancel:{token}".encode())],
    ]


def mirror_destination_buttons(
    token: str,
    mode: str,
    destinations: Sequence[DeliveryTarget],
    page: int = 0,
) -> list[list[Button]]:
    total_pages = max(1, math.ceil(len(destinations) / MIRROR_DESTINATIONS_PER_PAGE))
    page = min(max(0, page), total_pages - 1)
    start = page * MIRROR_DESTINATIONS_PER_PAGE
    visible = destinations[start : start + MIRROR_DESTINATIONS_PER_PAGE]
    icons = {"user": "👤", "group": "👥", "channel": "📢"}
    rows: list[list[Button]] = []
    for destination in visible:
        target_id = int(destination.chat_id)
        label = str(destination.label)[:38]
        rows.append(
            [
                Button.inline(
                    f"{icons.get(destination.kind, '📡')} {label}",
                    f"mirror:target:{token}:{mode}:{target_id}".encode(),
                )
            ]
        )
    if total_pages > 1:
        navigation: list[Button] = []
        if page > 0:
            navigation.append(
                Button.inline("‹ Previous", f"mirror:page:{token}:{mode}:{page - 1}".encode())
            )
        navigation.append(Button.inline(f"{page + 1}/{total_pages}", b"mirror:noop"))
        if page + 1 < total_pages:
            navigation.append(
                Button.inline("Next ›", f"mirror:page:{token}:{mode}:{page + 1}".encode())
            )
        rows.append(navigation)
    rows.append(
        [
            Button.inline(
                "↻ Refresh accessible chats",
                f"mirror:refresh:{token}:{mode}".encode(),
            )
        ]
    )
    rows.append([Button.inline("‹ Delivery choices", f"mirror:back:{token}".encode())])
    return rows


def admin_logs_buttons(events_enabled: bool, archives_enabled: bool) -> list[list[Button]]:
    return [
        [
            Button.inline("📡 Set log channel", b"admin:logs:channel"),
            Button.inline("🧪 Test channel", b"admin:logs:test"),
        ],
        [Button.inline(f"Event logs: {'ON' if events_enabled else 'OFF'}", b"admin:logs:events")],
        [
            Button.inline(
                f"Copy archives: {'ON' if archives_enabled else 'OFF'}", b"admin:logs:archives"
            )
        ],
        [Button.inline("‹ Owner menu", b"admin:home")],
    ]


def admin_limits_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("📥 Max archive", b"admin:limit:max_archive_mb"),
            Button.inline("📤 Max extracted", b"admin:limit:max_extracted_mb"),
        ],
        [
            Button.inline("📚 Max files", b"admin:limit:max_files"),
            Button.inline("🧨 Max ratio", b"admin:limit:max_ratio"),
        ],
        [
            Button.inline("⚡ Concurrent jobs", b"admin:limit:max_concurrent_jobs"),
            Button.inline("⏱ Timeout", b"admin:limit:job_timeout_seconds"),
        ],
        [Button.inline("🧹 Cleanup delay", b"admin:limit:cleanup_minutes")],
        [Button.inline("‹ Owner menu", b"admin:home")],
    ]


ALLOWED_CAPTION_FIELDS = {"name", "size", "index", "total", "bot", "path"}


def validate_caption_template(template: str) -> tuple[bool, str]:
    if len(template) > 700:
        return False, "Caption templates must be 700 characters or shorter."
    try:
        fields = {name for _, name, _, _ in Formatter().parse(template) if name}
    except ValueError:
        return False, "The caption contains an unmatched { or }."
    unknown = fields - ALLOWED_CAPTION_FIELDS
    if unknown:
        return False, f"Unknown placeholders: {', '.join(sorted(unknown))}"
    return True, ""


def render_caption(template: str, **values: Any) -> str:
    safe_values = {key: esc(value) for key, value in values.items()}
    try:
        result = esc(template).format_map(safe_values)
    except (KeyError, ValueError):
        result = "Extracted by {bot} • {index}/{total}".format_map(safe_values)
    return result[:780] + FOOTER


def render_video_caption(filename: str) -> str:
    """Use only the literal filename as a video's visible Telegram title/caption."""
    return esc(filename)[:1024]


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
