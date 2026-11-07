"""Tests for persistent operator settings."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import settings  # noqa: E402


class ReplyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="alex-settings-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(os.environ.pop, settings.REPLY_ENV_VAR, None)

    def test_default_is_the_new_opencode_reply_file(self) -> None:
        os.environ.pop(settings.REPLY_ENV_VAR, None)
        path = settings.primary_reply_file()
        self.assertEqual(
            path,
            Path.home() / "Desktop" / "OpenCode Reply Text" / "reply.txt",
        )
        self.assertTrue(path.is_absolute())

    def test_env_file_override_wins(self) -> None:
        target = Path(self.temp.name) / "custom.txt"
        os.environ[settings.REPLY_ENV_VAR] = str(target)
        self.assertEqual(settings.primary_reply_file(), target)

    def test_env_directory_resolves_to_reply_txt(self) -> None:
        folder = Path(self.temp.name) / "OpenCode Reply Text"
        folder.mkdir()
        os.environ[settings.REPLY_ENV_VAR] = str(folder)
        self.assertEqual(settings.primary_reply_file(), folder / "reply.txt")

    def test_env_expands_home(self) -> None:
        with mock.patch.dict(os.environ, {settings.REPLY_ENV_VAR: "~/Desktop/x.txt"}):
            path = settings.primary_reply_file()
        self.assertEqual(path, Path.home() / "Desktop" / "x.txt")

    def test_blank_env_falls_back_to_default(self) -> None:
        os.environ[settings.REPLY_ENV_VAR] = "   "
        self.assertIn("OpenCode Reply Text", str(settings.primary_reply_file()))

    def test_obsolete_path_is_never_the_primary(self) -> None:
        os.environ.pop(settings.REPLY_ENV_VAR, None)
        self.assertEqual(
            settings.obsolete_reply_file(),
            Path.home() / "Desktop" / "reply.txt",
        )
        self.assertNotEqual(
            settings.primary_reply_file(), settings.obsolete_reply_file()
        )

    def test_retired_path_redirects_to_primary(self) -> None:
        os.environ.pop(settings.REPLY_ENV_VAR, None)
        old = settings.obsolete_reply_file()
        self.assertEqual(
            settings.retired_reply_redirect(old),
            settings.primary_reply_file(),
        )
        self.assertIsNone(
            settings.retired_reply_redirect(Path("/tmp/not-the-reply.txt"))
        )

    def test_prompt_is_wired_to_the_setting(self) -> None:
        source = (PROJECT / "main.py").read_text(encoding="utf-8")
        self.assertIn("PRIMARY REPLY FILE (OpenCode)", source)
        self.assertIn("settings.primary_reply_file()", source)
        self.assertIn("settings.obsolete_reply_file()", source)


if __name__ == "__main__":
    unittest.main()
