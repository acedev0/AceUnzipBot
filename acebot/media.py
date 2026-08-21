from __future__ import annotations

import asyncio
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"}
STREAMABLE_SUFFIXES = {".mp4", ".m4v", ".mov"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
TELEGRAM_PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
Image.MAX_IMAGE_PIXELS = 50_000_000


@dataclass(frozen=True, slots=True)
class VideoInfo:
    duration: int
    width: int
    height: int


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_SUFFIXES


def is_streamable(path: Path) -> bool:
    return path.suffix.lower() in STREAMABLE_SUFFIXES


def is_photo_candidate(path: Path) -> bool:
    return path.suffix.lower() in TELEGRAM_PHOTO_SUFFIXES


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _verify_photo(path: Path) -> bool:
    if not is_photo_candidate(path):
        return False
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


async def probe_photo(path: Path) -> bool:
    """Confirm that a Telegram photo candidate contains a real decodable image."""
    return await asyncio.to_thread(_verify_photo, path)


async def probe_video(ffprobe: str, path: Path) -> VideoInfo | None:
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(stdout)
        stream = payload["streams"][0]
        duration = max(0, round(float(payload.get("format", {}).get("duration", 0))))
        return VideoInfo(duration, int(stream["width"]), int(stream["height"]))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _normalize_image(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((320, 320), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", image.size, (5, 9, 20))
        canvas.paste(image, (0, 0))
        # Telethon's MTProto documentation reports the most reliable thumbnails below 20 KB.
        quality = 84
        while True:
            canvas.save(
                destination, format="JPEG", quality=quality, optimize=True, progressive=True
            )
            if destination.stat().st_size <= 19_000 or quality <= 28:
                break
            quality -= 7
        if destination.stat().st_size > 19_000:
            canvas.thumbnail((240, 240), Image.Resampling.LANCZOS)
            canvas.save(destination, format="JPEG", quality=34, optimize=True, progressive=True)


async def normalize_thumbnail(source: Path, destination: Path) -> Path:
    await asyncio.to_thread(_normalize_image, source, destination)
    return destination


def _normalize_start_image(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1600, 900), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=90, optimize=True, progressive=True)


async def normalize_start_image(source: Path, destination: Path) -> Path:
    await asyncio.to_thread(_normalize_start_image, source, destination)
    return destination


def _normalize_profile_image(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        width, height = image.size
        side = min(width, height)
        left = max(0, (width - side) // 2)
        top = max(0, (height - side) // 2)
        image = image.crop((left, top, left + side, top + side))
        image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=92, optimize=True, progressive=True)


async def normalize_profile_image(source: Path, destination: Path) -> Path:
    await asyncio.to_thread(_normalize_profile_image, source, destination)
    return destination


def upload_fallback_variants(kwargs: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Return progressively safer Telegram upload forms without mutating the caller."""
    variants: list[dict[str, object]] = [dict(kwargs)]
    if "thumb" in kwargs:
        without_thumb = dict(kwargs)
        without_thumb.pop("thumb", None)
        variants.append(without_thumb)

    safe_document = dict(kwargs)
    safe_document["force_document"] = True
    safe_document["mime_type"] = "application/octet-stream"
    for key in ("thumb", "attributes", "supports_streaming"):
        safe_document.pop(key, None)

    def signature(item: dict[str, object]) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted((key, repr(value)) for key, value in item.items() if key != "progress_callback")
        )

    if all(signature(existing) != signature(safe_document) for existing in variants):
        variants.append(safe_document)
    return tuple(variants)


def is_media_validation_error(error: BaseException) -> bool:
    text = f"{type(error).__name__}: {error}".upper()
    signals = (
        "MEDIA_FILE_INVALID",
        "MEDIA_EMPTY",
        "DOCUMENT_INVALID",
        "VIDEO_CONTENT_TYPE_INVALID",
        "PHOTO_INVALID",
        "PHOTO_EXT_INVALID",
        "THUMBNAIL_INVALID",
    )
    return any(signal in text for signal in signals)


async def generate_video_thumbnail(
    ffmpeg: str,
    video: Path,
    destination: Path,
    info: VideoInfo | None,
) -> Path | None:
    seek = max(0.2, min(8.0, (info.duration * 0.12) if info and info.duration else 2.0))
    raw_destination = destination.with_name(destination.stem + "-raw.jpg")
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{seek:.2f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=640:640:force_original_aspect_ratio=decrease",
        "-q:v",
        "4",
        "-y",
        str(raw_destination),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=90)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0 or not raw_destination.exists():
        return None
    try:
        await normalize_thumbnail(raw_destination, destination)
    finally:
        raw_destination.unlink(missing_ok=True)
    return destination if destination.exists() else None


def video_attributes(path: Path, info: VideoInfo | None) -> list[object]:
    attributes: list[object] = [DocumentAttributeFilename(path.name)]
    if info:
        attributes.append(
            DocumentAttributeVideo(
                duration=info.duration,
                w=info.width,
                h=info.height,
                supports_streaming=is_streamable(path),
            )
        )
    return attributes


async def split_oversized(path: Path, max_part_bytes: int, parts_dir: Path) -> tuple[Path, ...]:
    """Split an unsendable file into deterministic binary parts without loading it into RAM."""

    def _split() -> tuple[Path, ...]:
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        with path.open("rb") as source:
            index = 1
            while True:
                destination = parts_dir / f"{path.name}.part{index:03d}"
                written = 0
                with destination.open("wb") as target:
                    while written < max_part_bytes:
                        chunk = source.read(min(8 * 1024 * 1024, max_part_bytes - written))
                        if not chunk:
                            break
                        target.write(chunk)
                        written += len(chunk)
                if written == 0:
                    destination.unlink(missing_ok=True)
                    break
                parts.append(destination)
                index += 1
        return tuple(parts)

    return await asyncio.to_thread(_split)


async def copy_brand_thumbnail(source: Path, destination: Path) -> Path | None:
    if not source.exists():
        return None
    try:
        return await normalize_thumbnail(source, destination)
    except Exception:
        return None
