from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class BotApiError(RuntimeError):
    pass


def build_multipart_body(
    fields: dict[str, Any], files: dict[str, Path]
) -> tuple[bytes, str]:
    boundary = f"AceBoundary{secrets.token_hex(16)}"
    body = bytearray()

    def add_line(value: bytes = b"") -> None:
        body.extend(value + b"\r\n")

    for name, value in fields.items():
        add_line(f"--{boundary}".encode())
        add_line(f'Content-Disposition: form-data; name="{name}"'.encode())
        add_line()
        add_line(str(value).encode("utf-8"))

    for field_name, path in files.items():
        filename = path.name.replace('"', "_")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        add_line(f"--{boundary}".encode())
        add_line(
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode()
        )
        add_line(f"Content-Type: {content_type}".encode())
        add_line()
        body.extend(path.read_bytes())
        add_line()

    add_line(f"--{boundary}--".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class BotApi:
    """Tiny official Bot API client for self-customization methods Telethon does not wrap."""

    def __init__(self, token: str):
        self._base_url = f"https://api.telegram.org/bot{token}/"

    def _request_sync(
        self,
        method: str,
        payload: dict[str, Any],
        files: dict[str, Path] | None = None,
    ) -> Any:
        if files:
            body, content_type = build_multipart_body(payload, files)
        else:
            body = urllib.parse.urlencode(payload).encode()
            content_type = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            self._base_url + method,
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BotApiError(
                "Telegram rejected or timed out while updating bot appearance."
            ) from exc
        if not result.get("ok"):
            description = str(result.get("description", "Unknown Telegram error"))
            raise BotApiError(description[:300])
        return result.get("result")

    async def call(
        self,
        method: str,
        *,
        files: dict[str, Path] | None = None,
        **payload: Any,
    ) -> Any:
        return await asyncio.to_thread(self._request_sync, method, payload, files)

    async def set_name(self, name: str) -> None:
        await self.call("setMyName", name=name)

    async def set_short_description(self, text: str) -> None:
        await self.call("setMyShortDescription", short_description=text)

    async def set_description(self, text: str) -> None:
        await self.call("setMyDescription", description=text)

    async def set_profile_photo(self, path: Path) -> None:
        if not path.is_file():
            raise BotApiError("The profile picture file is missing.")
        photo = json.dumps(
            {"type": "static", "photo": "attach://profile_photo"}, separators=(",", ":")
        )
        await self.call(
            "setMyProfilePhoto",
            photo=photo,
            files={"profile_photo": path},
        )

    async def install_commands(self) -> None:
        commands = [
            {"command": "start", "description": "Open Ace Extractor"},
            {"command": "help", "description": "Archive and password guide"},
            {"command": "settings", "description": "Upload mode, thumbnail and caption"},
            {"command": "bulk", "description": "Start collecting a FIFO archive batch"},
            {"command": "donebulk", "description": "Validate and queue the collected batch"},
            {"command": "cancelbulk", "description": "Discard an unfinished bulk batch"},
            {"command": "cancel", "description": "Cancel your active task"},
            {"command": "privacy", "description": "View logging and retention policy"},
            {"command": "myid", "description": "Show your Telegram numeric ID"},
            {"command": "admin", "description": "Owner control centre"},
        ]
        await self.call("setMyCommands", commands=json.dumps(commands, separators=(",", ":")))
