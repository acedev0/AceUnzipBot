from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from acebot.bot_api import build_multipart_body


class BotApiTests(unittest.TestCase):
    def test_profile_photo_multipart_body_contains_json_and_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            photo = Path(directory) / "profile.jpg"
            photo.write_bytes(b"jpeg-bytes")
            body, content_type = build_multipart_body(
                {"photo": '{"type":"static","photo":"attach://profile_photo"}'},
                {"profile_photo": photo},
            )
            self.assertIn("multipart/form-data; boundary=AceBoundary", content_type)
            self.assertIn(b'"type":"static"', body)
            self.assertIn(b'name="profile_photo"; filename="profile.jpg"', body)
            self.assertIn(b"Content-Type: image/jpeg", body)
            self.assertIn(b"jpeg-bytes", body)


if __name__ == "__main__":
    unittest.main()
