from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from acebot.archives import ArchiveEngine, ArchiveError, ArchiveLimits

FAKE_7Z = r"""
#!/usr/bin/env python3
import pathlib
import sys

if sys.argv[1] == "l":
    print("Path = folder/file.txt")
    print("Size = 5")
    print("Folder = -")
    print("Encrypted = -")
    print()
    raise SystemExit(0)

if sys.argv[1] == "x":
    output = next(arg[2:] for arg in sys.argv if arg.startswith("-o"))
    target = pathlib.Path(output) / "folder" / "file.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello", encoding="utf-8")
    print("100%")
    raise SystemExit(0)

raise SystemExit(2)
"""


MALICIOUS_7Z = r"""
#!/usr/bin/env python3
print("Path = ../escape.txt")
print("Size = 5")
print("Folder = -")
print("Encrypted = -")
print()
"""


LINK_7Z = r"""
#!/usr/bin/env python3
print("Path = link")
print("Size = 0")
print("Folder = -")
print("Symbolic Link = ../../outside")
print("Encrypted = -")
print()
"""


class EngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_executable(self, directory: Path, name: str, source: str) -> Path:
        path = directory / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    async def test_inspect_extract_and_verify_flow(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            executable = self.make_executable(directory, "fake7z", FAKE_7Z)
            archive = directory / "archive.zip"
            archive.write_bytes(b"archive")
            output = directory / "output"
            updates: list[tuple[int, int]] = []

            async def progress(current: int, total: int, detail: str) -> None:
                updates.append((current, total))

            result = await ArchiveEngine(str(executable)).extract(
                archive,
                output,
                None,
                ArchiveLimits(1000, 1000, 10, 100),
                asyncio.Event(),
                progress,
            )
            self.assertEqual(result.total_size, 5)
            self.assertEqual(result.files[0].read_text(encoding="utf-8"), "hello")
            self.assertTrue(updates)

    async def test_malicious_listing_is_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            executable = self.make_executable(directory, "bad7z", MALICIOUS_7Z)
            archive = directory / "archive.zip"
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(ArchiveError, "Unsafe archive rejected"):
                await ArchiveEngine(str(executable)).inspect(
                    archive, None, ArchiveLimits(1000, 1000, 10, 100)
                )

    async def test_symbolic_link_field_is_rejected_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            executable = self.make_executable(directory, "link7z", LINK_7Z)
            archive = directory / "archive.zip"
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(ArchiveError, "symbolic or hard links"):
                await ArchiveEngine(str(executable)).inspect(
                    archive, None, ArchiveLimits(1000, 1000, 10, 100)
                )


if __name__ == "__main__":
    unittest.main()
