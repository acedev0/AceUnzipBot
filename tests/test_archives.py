from __future__ import annotations

import unittest
from pathlib import Path

from acebot.archives import (
    _parse_7z_listing,
    _safe_member_path,
    looks_like_archive,
    password_from_caption,
    safe_filename,
)


class ArchiveSecurityTests(unittest.TestCase):
    def test_rejects_traversal_and_absolute_members(self) -> None:
        unsafe = [
            "../secret.txt",
            "folder/../../secret.txt",
            "/etc/passwd",
            "C:/Windows/system.ini",
            "~/.ssh/id_rsa",
            "folder\\..\\secret.txt",
            "folder/line\nbreak.txt",
            "folder/tab\tname.txt",
        ]
        for member in unsafe:
            with self.subTest(member=member):
                self.assertFalse(_safe_member_path(member))

    def test_accepts_normal_nested_members(self) -> None:
        for member in ["movie.mp4", "Season 01/Episode 01.mp4", "資料/映像.mkv"]:
            with self.subTest(member=member):
                self.assertTrue(_safe_member_path(member))

    def test_safe_filename_removes_paths_and_controls(self) -> None:
        self.assertEqual(safe_filename("../../bad\x00name.zip"), "bad_name.zip")
        self.assertEqual(safe_filename("C:\\folder\\archive.zip"), "archive.zip")

    def test_supported_archive_names(self) -> None:
        for name in ["x.zip", "X.RAR", "backup.tar.gz", "video.7z", "data.iso"]:
            self.assertTrue(looks_like_archive(name))
        self.assertFalse(looks_like_archive("photo.jpg"))

    def test_password_caption_parser(self) -> None:
        self.assertEqual(password_from_caption("password: correct horse"), "correct horse")
        self.assertEqual(password_from_caption("Title\nPW = s3cret"), "s3cret")
        self.assertIsNone(password_from_caption("ordinary caption"))

    def test_7z_slt_parser_ignores_archive_header(self) -> None:
        listing = """Path = /tmp/a.zip
Type = zip

----------
Path = folder/file.txt
Size = 12
Folder = -
Encrypted = -

Path = empty
Size = 0
Folder = +
Encrypted = -
"""
        entries = _parse_7z_listing(listing, Path("/tmp/a.zip"))
        self.assertEqual([entry["Path"] for entry in entries], ["folder/file.txt", "empty"])


if __name__ == "__main__":
    unittest.main()
