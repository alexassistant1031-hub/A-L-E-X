"""Tests for the email diagnostic's credential reporting.

The report exists because empty ``EMAIL_ADDRESS`` / ``EMAIL_APP_PASSWORD``
placeholders in ``.env`` made one-off SMTP tests fail with Gmail's
"Authentication failed", which looks like a broken app password.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "email_check.py"


def _load_script():
    """Import the diagnostic script without leaking its side effects.

    ``email_check`` is a CLI: importing it changes directory and loads ``.env``
    plus ``.env.local`` into ``os.environ``.  The suite must stay hermetic, so
    both are saved and restored around the import.
    """
    saved_env = os.environ.copy()
    saved_cwd = os.getcwd()
    try:
        spec = importlib.util.spec_from_file_location("alex_email_check", _SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)


email_check = _load_script()


class CredentialReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-env-report-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, name: str, body: str) -> None:
        (self.root / name).write_text(body, encoding="utf-8")

    def test_names_the_file_that_supplies_the_password(self) -> None:
        self._write(".env", "EMAIL_ADDRESS=\nEMAIL_APP_PASSWORD=\n")
        self._write(
            ".env.local", "EMAIL_ADDRESS=shop@example.com\nEMAIL_APP_PASSWORD=abcdefghijklmnop\n"
        )
        text = "\n".join(email_check.credential_report(self.root))
        self.assertIn("EMAIL_APP_PASSWORD: .env.local", text)
        self.assertIn("EMAIL_ADDRESS: .env.local", text)

    def test_warns_about_empty_placeholders_in_dot_env(self) -> None:
        self._write(".env", "EMAIL_ADDRESS=\nEMAIL_APP_PASSWORD=\n")
        self._write(
            ".env.local", "EMAIL_ADDRESS=shop@example.com\nEMAIL_APP_PASSWORD=abcdefghijklmnop\n"
        )
        text = "\n".join(email_check.credential_report(self.root))
        self.assertIn("WARNING", text)
        self.assertIn("Authentication", text)
        self.assertIn("EMAIL_APP_PASSWORD", text)

    def test_quiet_when_dot_env_is_clean(self) -> None:
        self._write(".env", "# credentials live in .env.local\n")
        self._write(
            ".env.local", "EMAIL_ADDRESS=shop@example.com\nEMAIL_APP_PASSWORD=abcdefghijklmnop\n"
        )
        text = "\n".join(email_check.credential_report(self.root))
        self.assertNotIn("WARNING", text)

    def test_missing_files_are_reported_not_crashed(self) -> None:
        text = "\n".join(email_check.credential_report(self.root))
        self.assertIn("credential sources", text)
        self.assertIn("the process environment", text)

    def test_never_includes_the_password_value(self) -> None:
        self._write(".env", "EMAIL_ADDRESS=\nEMAIL_APP_PASSWORD=\n")
        self._write(
            ".env.local", "EMAIL_ADDRESS=shop@example.com\nEMAIL_APP_PASSWORD=supersecretvalue1\n"
        )
        text = "\n".join(email_check.credential_report(self.root))
        self.assertNotIn("supersecretvalue1", text)


if __name__ == "__main__":
    unittest.main()
