from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ProgressCallback = Callable[[int, int, str], Awaitable[None]]


SUPPORTED_SUFFIXES = (
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".tar.zst",
    ".gz",
    ".bz2",
    ".xz",
    ".zst",
    ".iso",
    ".cab",
)

CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ArchiveError(RuntimeError):
    def __init__(self, public_message: str, *, detail: str = ""):
        super().__init__(detail or public_message)
        self.public_message = public_message
        self.detail = detail or public_message


class PasswordRequired(ArchiveError):
    pass


class WrongPassword(ArchiveError):
    pass


class JobCancelled(ArchiveError):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_archive_bytes: int
    max_extracted_bytes: int
    max_files: int
    max_ratio: float


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    file_count: int
    declared_size: int
    compression_ratio: float
    encrypted: bool


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    files: tuple[Path, ...]
    total_size: int
    inspection: ArchiveInspection


def safe_filename(name: str, fallback: str = "archive.bin") -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = CONTROL_CHARS_RE.sub("_", name).strip().strip(".")
    if not name:
        name = fallback
    stem, suffix = os.path.splitext(name)
    if len(name.encode("utf-8")) > 220:
        stem = stem.encode("utf-8")[:180].decode("utf-8", "ignore")
        name = stem + suffix[:20]
    return name


def looks_like_archive(filename: str | None) -> bool:
    if not filename:
        return False
    lower = filename.lower()
    return lower.endswith(SUPPORTED_SUFFIXES)


def password_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    match = re.search(r"(?im)^\s*(?:password|pass|pw)\s*[:=]\s*(.+?)\s*$", caption)
    if not match:
        return None
    value = match.group(1).strip()
    return value[:512] if value else None


def _safe_member_path(raw: str) -> bool:
    if not raw or CONTROL_CHARS_RE.search(raw):
        return False
    normalized = raw.replace("\\", "/")
    if normalized.startswith(("/", "~")) or WINDOWS_DRIVE_RE.match(normalized):
        return False
    path = PurePosixPath(normalized)
    return not any(part in {"..", ""} for part in path.parts)


def _parse_7z_listing(output: str, archive_path: Path) -> list[dict[str, str]]:
    # Machine-readable -slt output starts archive members after a dashed separator.
    if "----------" in output:
        output = output.split("----------", 1)[1]
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current.get("Path"):
                entries.append(current)
            current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value.strip()
    if current.get("Path"):
        entries.append(current)

    archive_resolved = str(archive_path.resolve())
    return [
        entry for entry in entries if entry.get("Path") not in {str(archive_path), archive_resolved}
    ]


def _password_error(text: str, password_supplied: bool) -> ArchiveError | None:
    lower = text.lower()
    signals = (
        "wrong password",
        "password is incorrect",
        "can not open encrypted archive",
        "data error in encrypted file",
        "enter password",
    )
    if not any(signal in lower for signal in signals):
        return None
    if password_supplied:
        return WrongPassword("That password did not unlock the archive. Try again.")
    return PasswordRequired(
        "This archive is password-protected. Send /password followed by the password."
    )


async def _read_stream(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    chunks: list[bytes] = []
    while True:
        chunk = await process.stdout.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


class ArchiveEngine:
    def __init__(self, seven_zip: str):
        self.seven_zip = seven_zip

    async def inspect(
        self,
        archive_path: Path,
        password: str | None,
        limits: ArchiveLimits,
    ) -> ArchiveInspection:
        archive_size = archive_path.stat().st_size
        if archive_size > limits.max_archive_bytes:
            raise ArchiveError(
                f"Archive is larger than the configured limit ({limits.max_archive_bytes // 1024 // 1024} MB)."
            )
        command = [self.seven_zip, "l", "-slt", "-ba", str(archive_path)]
        if password:
            command.insert(3, f"-p{password}")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output = await asyncio.wait_for(_read_stream(process), timeout=180)
            return_code = await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ArchiveError("Archive inspection timed out.") from exc
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        if return_code != 0:
            password_error = _password_error(output, bool(password))
            if password_error:
                raise password_error
            clean_detail = output.replace(password or "\0", "***")[-2000:]
            raise ArchiveError(
                "Telegram delivered the file, but 7-Zip could not read it. It may be corrupt or missing split parts.",
                detail=clean_detail,
            )

        entries = _parse_7z_listing(output, archive_path)
        if not entries:
            raise ArchiveError("The archive is empty or its format is not supported by this VPS.")

        file_count = 0
        declared_size = 0
        encrypted = False
        for entry in entries:
            member_path = entry.get("Path", "")
            if not _safe_member_path(member_path):
                raise ArchiveError(
                    "Unsafe archive rejected: a member attempted to write outside its task folder."
                )
            attributes = entry.get("Attributes", "").lower()
            attribute_words = attributes.split()
            normalized_fields = {key.casefold(): value for key, value in entry.items()}
            if (
                attributes.startswith("l")
                or any(word.startswith("l") for word in attribute_words)
                or normalized_fields.get("symbolic link")
                or normalized_fields.get("hard link")
            ):
                raise ArchiveError(
                    "Unsafe archive rejected: symbolic or hard links are not allowed."
                )
            is_folder = entry.get("Folder") == "+" or attributes.startswith("d")
            encrypted = encrypted or entry.get("Encrypted") == "+"
            if not is_folder:
                file_count += 1
                with contextlib.suppress(ValueError):
                    declared_size += max(0, int(entry.get("Size", "0")))

        ratio = declared_size / max(archive_size, 1)
        if file_count > limits.max_files:
            raise ArchiveError(
                f"Archive contains {file_count} files; the owner limit is {limits.max_files}."
            )
        if declared_size > limits.max_extracted_bytes:
            raise ArchiveError(
                "Archive expands beyond the configured extracted-size limit "
                f"({limits.max_extracted_bytes // 1024 // 1024} MB)."
            )
        if ratio > limits.max_ratio:
            raise ArchiveError(
                f"Archive expansion ratio ({ratio:.0f}×) exceeds the safety limit ({limits.max_ratio:.0f}×)."
            )
        return ArchiveInspection(file_count, declared_size, ratio, encrypted)

    async def extract(
        self,
        archive_path: Path,
        output_dir: Path,
        password: str | None,
        limits: ArchiveLimits,
        cancel_event: asyncio.Event,
        progress: ProgressCallback,
    ) -> ExtractedContent:
        inspection = await self.inspect(archive_path, password, limits)
        output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        command = [
            self.seven_zip,
            "x",
            "-y",
            "-aoa",
            "-bb1",
            "-bsp1",
            "-bso1",
            "-bse1",
            f"-o{output_dir}",
            str(archive_path),
        ]
        if password:
            command.insert(-2, f"-p{password}")

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(archive_path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        limit_violation: str | None = None

        async def monitor_size() -> None:
            nonlocal limit_violation
            while process.returncode is None:
                await asyncio.sleep(1.5)
                total = 0
                file_count = 0
                try:
                    for path in output_dir.rglob("*"):
                        if path.is_symlink():
                            limit_violation = "Unsafe archive rejected: a symbolic link appeared during extraction."
                            process.kill()
                            return
                        if path.is_dir():
                            continue
                        if not path.is_file():
                            limit_violation = (
                                "Unsafe archive rejected: a special filesystem object appeared "
                                "during extraction."
                            )
                            process.kill()
                            return
                        file_stat = path.stat()
                        if file_stat.st_nlink > 1:
                            limit_violation = (
                                "Unsafe archive rejected: a hard link appeared during extraction."
                            )
                            process.kill()
                            return
                        file_count += 1
                        total += file_stat.st_size
                        if file_count > limits.max_files:
                            limit_violation = "Extraction crossed the configured file-count limit and was stopped."
                            process.kill()
                            return
                        if total > limits.max_extracted_bytes:
                            limit_violation = (
                                "Extraction crossed the configured size limit and was stopped."
                            )
                            process.kill()
                            return
                except (FileNotFoundError, PermissionError):
                    continue

        monitor_task = asyncio.create_task(monitor_size())
        output_chunks: list[str] = []
        try:
            assert process.stdout is not None
            while True:
                if cancel_event.is_set():
                    process.kill()
                    raise JobCancelled("Task cancelled.")
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=1.0)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                output_chunks.append(text)
                output_chunks = output_chunks[-30:]
                matches = re.findall(r"(?<!\d)(\d{1,3})%", text)
                if matches:
                    percent = min(100, int(matches[-1]))
                    await progress(
                        int(inspection.declared_size * percent / 100),
                        inspection.declared_size,
                        f"Verifying and unpacking • {percent}%",
                    )
            return_code = await process.wait()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

        combined_output = "".join(output_chunks)
        if limit_violation:
            raise ArchiveError(limit_violation)
        if return_code != 0:
            password_error = _password_error(combined_output, bool(password))
            if password_error:
                raise password_error
            clean_detail = combined_output.replace(password or "\0", "***")[-2000:]
            raise ArchiveError(
                "7-Zip could not finish extracting this archive.", detail=clean_detail
            )

        root = output_dir.resolve()
        files: list[Path] = []
        total_size = 0
        for path in output_dir.rglob("*"):
            if path.is_symlink():
                raise ArchiveError(
                    "Unsafe archive rejected: an extracted symbolic link was detected."
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise ArchiveError(
                    "Unsafe archive rejected: a special filesystem object was detected."
                )
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise ArchiveError(
                    "Unsafe archive rejected: an output path escaped its task folder."
                ) from exc
            file_stat = path.stat()
            if file_stat.st_nlink > 1:
                raise ArchiveError("Unsafe archive rejected: an extracted hard link was detected.")
            total_size += file_stat.st_size
            files.append(path)
            if len(files) > limits.max_files or total_size > limits.max_extracted_bytes:
                raise ArchiveError("Extracted output crossed an owner safety limit.")

        if not files:
            raise ArchiveError("The archive contained no uploadable files.")
        files.sort(key=lambda item: item.relative_to(root).as_posix().casefold())
        await progress(
            total_size,
            max(total_size, 1),
            "Extraction verified",
        )
        return ExtractedContent(tuple(files), total_size, inspection)


def cleanup_workspace(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
