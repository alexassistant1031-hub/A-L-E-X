"""Tests for the email tool and live listener helpers."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tools.email_listener import (
    EmailListener,
    _sender_allowed,
    live_enabled,
    start_email_listener,
)
from tools.email_tool import (
    HARD_LIMIT,
    EmailConfig,
    EmailError,
    EmailRecord,
    EmailTool,
)


def _config(**overrides):
    values = {
        "address": "alex@example.com",
        "password": "apppass",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "from_name": "Alex",
    }
    values.update(overrides)
    return EmailConfig(**values)


def _raw(
    sender="Rahul <rahul@example.com>",
    subject="Hello",
    body="Body text",
    date="Wed, 05 Nov 2026 14:30:00 +0000",
    content_type="text/plain; charset=utf-8",
):
    return (
        f"From: {sender}\r\n"
        "To: alex@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        "Message-ID: <m1@example.com>\r\n"
        f"Content-Type: {content_type}\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


class FakeIMAP:
    def __init__(self):
        self.raw = {}
        self.all_uids = []
        self.unread_uids = []
        self.search_result = []
        self.new_uids = []
        self.header_uids = []
        self.folders = ["INBOX", "[Gmail]/Sent Mail"]
        self.selected = None
        self.readonly = None
        self.logged_in = None
        self.logged_out = False
        self.calls = []
        self.appended = []
        self.state = "AUTH"
        self.capabilities = (b"IMAP4REV1", b"IDLE")

    def login(self, user, password):
        self.logged_in = (user, password)
        self.state = "AUTH"
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        self.readonly = readonly
        self.state = "SELECTED"
        self.calls.append(("select", mailbox, readonly))
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.calls.append((command, tuple(args)))
        command = command.upper()
        if command == "SEARCH":
            criteria = [str(arg) for arg in args]
            if "UNSEEN" in criteria:
                uids = self.unread_uids
            elif "TEXT" in criteria:
                uids = self.search_result
            elif "HEADER" in criteria:
                uids = self.header_uids
            elif "UID" in criteria:
                uids = self.new_uids
            else:
                uids = self.all_uids
            return ("OK", [" ".join(uids).encode()])
        if command == "FETCH":
            uid = str(args[0])
            raw = self.raw.get(uid, b"")
            meta = f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode()
            return ("OK", [(meta, raw), b")"])
        return ("NO", [b"unsupported"])

    def list(self):
        self.calls.append(("list",))
        return (
            "OK",
            [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders],
        )

    def append(self, mailbox, flags, date_time, message):
        self.calls.append(("append", mailbox, flags))
        self.appended.append((mailbox, flags, date_time, message))
        return ("OK", [b"APPEND completed"])

    def logout(self):
        self.logged_out = True
        self.state = "LOGOUT"
        return ("BYE", [b"bye"])

    def shutdown(self):
        self.logged_out = True


class FakeSMTP:
    def __init__(self):
        self.sent = []
        self.logged_in = None
        self.quit_called = False
        self.refused = {}

    def login(self, user, password):
        self.logged_in = (user, password)
        return ("235", b"ok")

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent.append((message, from_addr, list(to_addrs or [])))
        return dict(self.refused)

    def quit(self):
        self.quit_called = True


def _tool(imap=None, smtp=None, audit_fn=None, config=None):
    events = []

    def default_audit(event, fields):
        events.append((event, fields))

    imap = imap or FakeIMAP()
    smtp = smtp or FakeSMTP()
    tool = EmailTool(
        audit=audit_fn or default_audit,
        config=config or _config(),
        imap_factory=lambda _cfg: imap,
        smtp_factory=lambda _cfg: smtp,
    )
    return tool, events, imap, smtp


class EmailConfigTests(unittest.TestCase):
    def test_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "EMAIL_ADDRESS": "alex@example.com",
                "EMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
                "EMAIL_IMAP_HOST": "imap.example.com",
                "EMAIL_IMAP_PORT": "993",
                "EMAIL_SMTP_HOST": "smtp.example.com",
                "EMAIL_SMTP_PORT": "465",
                "EMAIL_FROM_NAME": "Alex",
                "EMAIL_ALLOWED_SENDERS": "bob@example.com, @work.example",
            },
            clear=True,
        ):
            config = EmailConfig.from_env()
        self.assertEqual(config.address, "alex@example.com")
        self.assertEqual(config.password, "abcdefghijklmnop")
        self.assertEqual(config.allowed_senders, ("bob@example.com", "@work.example"))

    def test_missing_password(self):
        with mock.patch.dict(
            os.environ, {"EMAIL_ADDRESS": "alex@example.com"}, clear=True
        ):
            with self.assertRaises(EmailError):
                EmailConfig.from_env()

    def test_invalid_address(self):
        with mock.patch.dict(
            os.environ,
            {
                "EMAIL_ADDRESS": "not-an-address",
                "EMAIL_APP_PASSWORD": "pass",
            },
            clear=True,
        ):
            with self.assertRaises(EmailError):
                EmailConfig.from_env()

    def test_invalid_port(self):
        with mock.patch.dict(
            os.environ,
            {
                "EMAIL_ADDRESS": "alex@example.com",
                "EMAIL_APP_PASSWORD": "pass",
                "EMAIL_IMAP_PORT": "nope",
            },
            clear=True,
        ):
            with self.assertRaises(EmailError):
                EmailConfig.from_env()


class EmailToolTests(unittest.IsolatedAsyncioTestCase):
    def _read_tool(self, raw=None):
        imap = FakeIMAP()
        imap.all_uids = ["1", "2"]
        imap.raw = {"1": _raw(body="older"), "2": _raw(body="newer")}
        if raw is not None:
            imap.raw["2"] = raw
        return _tool(imap=imap), imap

    async def test_read_formats_untrusted_messages(self):
        (tool, events, _imap, _smtp), imap = self._read_tool()
        result = await tool.execute("read", limit=2)
        self.assertIn("Untrusted email content", result)
        self.assertIn("uid 1", result)
        self.assertIn("uid 2", result)
        self.assertIn("Subject: Hello", result)
        self.assertIn("newer", result)
        self.assertEqual(imap.readonly, True)
        self.assertEqual(events[0][0], "email_read")

    async def test_read_unread_only(self):
        (tool, _events, _imap, _smtp), imap = self._read_tool()
        imap.unread_uids = ["2"]
        await tool.execute("read", unread_only=True)
        self.assertIn(("SEARCH", ("UNSEEN",)), imap.calls)

    async def test_read_clamps_limit(self):
        imap = FakeIMAP()
        imap.all_uids = [str(i) for i in range(1, 31)]
        imap.raw = {str(i): _raw(body=f"body {i}") for i in range(1, 31)}
        tool, _events, _imap, _smtp = _tool(imap=imap)
        result = await tool.execute("read", limit=999)
        self.assertIn(f"({HARD_LIMIT})", result)

    async def test_search_quotes_the_query(self):
        (tool, _events, _imap, _smtp), imap = self._read_tool()
        imap.search_result = ["2"]
        await tool.execute("search", query='hello "world"')
        searches = [call for call in imap.calls if call[0] == "SEARCH"]
        self.assertTrue(searches)
        self.assertIn("TEXT", searches[-1][1])
        self.assertIn('"hello \\"world\\""', searches[-1][1])

    async def test_search_adds_utf8_charset(self):
        (tool, _events, _imap, _smtp), imap = self._read_tool()
        imap.search_result = ["1"]
        await tool.execute("search", query="नमस्ते")
        searches = [call for call in imap.calls if call[0] == "SEARCH"]
        self.assertIn("CHARSET", searches[-1][1])
        self.assertIn("UTF-8", searches[-1][1])

    async def test_missing_query(self):
        tool, _events, _imap, _smtp = _tool()
        result = await tool.execute("search")
        self.assertIn("`query` is required", result)

    async def test_send_requires_confirmation(self):
        tool, _events, _imap, smtp = _tool()
        result = await tool.execute(
            "send", to="bob@example.com", subject="Hi", body="Hello"
        )
        self.assertIn("Confirmation required", result)
        self.assertEqual(smtp.sent, [])

    async def test_send_with_confirmation(self):
        tool, events, _imap, smtp = _tool()
        result = await tool.execute(
            "send",
            to="Bob <bob@example.com>, carol@example.com",
            cc="dave@example.com",
            subject="Hi",
            body="Hello",
            confirmed=True,
        )
        self.assertIn("Sent to 3 recipient(s)", result)
        self.assertIn("Copy saved to '[Gmail]/Sent Mail'", result)
        message, from_addr, recipients = smtp.sent[0]
        self.assertEqual(from_addr, "alex@example.com")
        self.assertEqual(len(recipients), 3)
        self.assertIn("Alex <alex@example.com>", message["From"])
        self.assertEqual(message["Subject"], "Hi")
        self.assertIn("Hello", message.get_content())
        self.assertEqual(
            [event for event, _ in events],
            ["email_send_requested", "email_send", "email_send_copy"],
        )

    async def test_send_appends_copy_to_sent_mail(self):
        tool, _events, imap, _smtp = _tool()
        await tool.execute(
            "send",
            to="bob@example.com",
            subject="Copy test",
            body="Body",
            confirmed=True,
        )
        self.assertEqual(len(imap.appended), 1)
        folder, flags, _date, raw = imap.appended[0]
        self.assertEqual(folder, '"[Gmail]/Sent Mail"')
        self.assertEqual(flags, r"(\Seen)")
        self.assertIn(b"Subject: Copy test", raw)
        self.assertIn(b"To: bob@example.com", raw)

    async def test_send_does_not_duplicate_when_copy_exists(self):
        imap = FakeIMAP()
        imap.header_uids = ["7"]
        tool, _events, _imap, _smtp = _tool(imap=imap)
        result = await tool.execute(
            "send",
            to="bob@example.com",
            subject="No duplicate",
            body="Body",
            confirmed=True,
        )
        self.assertEqual(imap.appended, [])
        self.assertIn("Copy saved to '[Gmail]/Sent Mail'", result)

    async def test_send_reports_copy_failure_but_still_sends(self):
        class BrokenAppend(FakeIMAP):
            def append(self, mailbox, flags, date_time, message):
                return ("NO", [b"append not allowed"])

        imap = BrokenAppend()
        tool, events, _imap, smtp = _tool(imap=imap)
        result = await tool.execute(
            "send",
            to="bob@example.com",
            subject="Hi",
            body="Hello",
            confirmed=True,
        )
        self.assertEqual(len(smtp.sent), 1)
        self.assertIn("Sent to 1 recipient(s)", result)
        self.assertIn("no copy could be saved to Sent Mail", result)
        self.assertIn("email_send_copy_failed", [event for event, _ in events])

    async def test_send_blocks_credentials(self):
        tool, _events, _imap, smtp = _tool()
        for secret in (
            "ghp_" + "a" * 36,
            "github_pat_" + "A" * 30,
            "AKIA" + "A" * 16,
            "-----BEGIN RSA PRIVATE KEY-----",
        ):
            result = await tool.execute(
                "send",
                to="bob@example.com",
                subject="Token",
                body=f"Here: {secret}",
                confirmed=True,
            )
            self.assertIn("Refusing to send", result)
        self.assertEqual(smtp.sent, [])

    async def test_send_fails_closed_when_audit_is_unavailable(self):
        smtp = FakeSMTP()

        def broken_audit(event, fields):
            raise OSError("audit down")

        tool, _events, _imap, smtp = _tool(smtp=smtp, audit_fn=broken_audit)
        result = await tool.execute(
            "send",
            to="bob@example.com",
            subject="Hi",
            body="Hello",
            confirmed=True,
        )
        self.assertIn("audit log is unavailable", result)
        self.assertEqual(smtp.sent, [])

    async def test_send_rejects_invalid_address(self):
        tool, _events, _imap, smtp = _tool()
        result = await tool.execute(
            "send", to="not-an-address", subject="Hi", body="Hello", confirmed=True
        )
        self.assertIn("invalid email address", result)
        self.assertEqual(smtp.sent, [])

    async def test_send_strips_header_newlines(self):
        tool, _events, _imap, smtp = _tool()
        await tool.execute(
            "send",
            to="bob@example.com",
            subject="Hi\r\nBcc: evil@example.com",
            body="Hello",
            confirmed=True,
        )
        message = smtp.sent[0][0]
        self.assertIsNone(message["Bcc"])
        self.assertEqual(message["Subject"], "Hi Bcc: evil@example.com")

    async def test_html_body_is_stripped(self):
        raw = _raw(
            body="<html><body><p>Hello <b>there</b></p>"
            "<script>bad()</script></body></html>",
            content_type="text/html; charset=utf-8",
        )
        (tool, _events, _imap, _smtp), _imap = self._read_tool(raw=raw)
        result = await tool.execute("read", limit=1)
        self.assertIn("Hello there", result)
        self.assertNotIn("bad()", result)

    async def test_folders(self):
        tool, events, imap, _smtp = _tool()
        result = await tool.execute("folders")
        self.assertIn("INBOX", result)
        self.assertIn("[Gmail]/Sent Mail", result)
        self.assertEqual(events[0][0], "email_folders")

    async def test_me_reports_unread(self):
        imap = FakeIMAP()
        imap.unread_uids = ["1", "2", "3"]
        tool, _events, _imap, _smtp = _tool(imap=imap)
        result = await tool.execute("me")
        self.assertIn("alex@example.com", result)
        self.assertIn("3 unread", result)

    async def test_unknown_action(self):
        tool, _events, _imap, _smtp = _tool()
        result = await tool.execute("delete-everything")
        self.assertIn("Unknown email action", result)

    async def test_missing_config_is_user_facing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            tool = EmailTool(config=None)
            result = await tool.execute("read")
        self.assertIn("Email is not configured", result)
        self.assertIn("EMAIL_APP_PASSWORD", result)

    async def test_parameters_cover_actions(self):
        self.assertEqual(
            EmailTool.parameters["properties"]["action"]["enum"],
            ["read", "search", "send", "folders", "me"],
        )
        self.assertIn("confirmed", EmailTool.parameters["properties"])


class EmailListenerTests(unittest.IsolatedAsyncioTestCase):
    def _record(self, sender):
        return EmailRecord(
            uid="1",
            sender=sender,
            subject="Hi",
            date="?",
            snippet="body",
        )

    def test_sender_allowlist(self):
        self.assertTrue(_sender_allowed(self._record("a@b.com"), ()))
        self.assertTrue(
            _sender_allowed(self._record("Bob <bob@example.com>"), ("bob@example.com",))
        )
        self.assertTrue(
            _sender_allowed(self._record("Bob <bob@work.example>"), ("@work.example",))
        )
        self.assertFalse(
            _sender_allowed(self._record("Bob <bob@evil.example>"), ("@work.example",))
        )

    def test_live_enabled_flag(self):
        with mock.patch.dict(os.environ, {"EMAIL_LIVE": "false"}, clear=False):
            self.assertFalse(live_enabled())
        with mock.patch.dict(os.environ, {"EMAIL_LIVE": "true"}, clear=False):
            self.assertTrue(live_enabled())

    async def test_start_returns_none_without_credentials(self):
        async def callback(records):
            return None

        with mock.patch.dict(
            os.environ, {"EMAIL_LIVE": "true", "EMAIL_ADDRESS": ""}, clear=True
        ):
            self.assertIsNone(start_email_listener(callback))

    async def test_stop_without_start_is_safe(self):
        listener = EmailListener(_config(), lambda records: None)
        await listener.stop()


if __name__ == "__main__":
    unittest.main()
