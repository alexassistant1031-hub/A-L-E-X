"""Tests for the Telegram user-account tool."""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from tools.telegram.tool import (
    HARD_LIMIT,
    TelegramConfig,
    TelegramError,
    TelegramTool,
)

HASH = "0123456789abcdef0123456789abcdef"


def _entity(title="Bob", entity_id=123, username=None):
    return SimpleNamespace(
        id=entity_id,
        title=title,
        username=username,
        first_name=title,
        last_name="",
    )


def _message(text, name="Bob"):
    return SimpleNamespace(
        id=1,
        message=text,
        text=text,
        sender=_entity(name),
        sender_id=123,
        date=datetime(2026, 11, 5, 14, 30),
        media=None,
    )


class FakeClient:
    def __init__(self):
        self.entity = _entity()
        self.messages = [_message("newer"), _message("older")]
        self.dialogs = [
            SimpleNamespace(entity=_entity("Bob", 123), unread_count=2),
            SimpleNamespace(entity=_entity("Work", 456), unread_count=0),
        ]
        self.me = SimpleNamespace(
            id=42, first_name="Alex", last_name="", username="alex"
        )
        self.sent = []
        self.last_search = None
        self.last_limit = None
        self.last_entity_query = None
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return self.me

    async def get_entity(self, chat):
        self.last_entity_query = chat
        if self.entity is None:
            raise ValueError("cannot find any entity")
        return self.entity

    async def iter_messages(self, entity, limit=5, search=None):
        self.last_search = search
        self.last_limit = limit
        for message in self.messages[:limit]:
            yield message

    async def get_dialogs(self, limit=5):
        return self.dialogs[:limit]

    async def send_message(self, entity, text, reply_to=None):
        sent = SimpleNamespace(id=99, message=text)
        self.sent.append((entity, text, reply_to))
        return sent


def _tool(client=None, allowed=()):
    events = []

    def audit(event, fields):
        events.append((event, fields))

    config = TelegramConfig(
        api_id=1,
        api_hash=HASH,
        session="session",
        allowed_chats=tuple(allowed),
    )
    return TelegramTool(audit=audit, client=client, config=config), events


class TelegramConfigTests(unittest.TestCase):
    def test_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "38215380",
                "TELEGRAM_API_HASH": HASH,
                "TELEGRAM_SESSION": "session",
                "TELEGRAM_ALLOWED_CHAT_IDS": "123, @alice",
            },
            clear=False,
        ):
            config = TelegramConfig.from_env()
        self.assertEqual(config.api_id, 38215380)
        self.assertEqual(config.api_hash, HASH)
        self.assertEqual(config.session, "session")
        self.assertEqual(config.allowed_chats, ("123", "@alice"))

    def test_missing_session(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": HASH,
                "TELEGRAM_SESSION": "",
            },
            clear=False,
        ):
            with self.assertRaises(TelegramError):
                TelegramConfig.from_env()

    def test_invalid_api_id(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "not-a-number",
                "TELEGRAM_API_HASH": HASH,
                "TELEGRAM_SESSION": "session",
            },
            clear=False,
        ):
            with self.assertRaises(TelegramError):
                TelegramConfig.from_env()

    def test_invalid_hash(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "1",
                "TELEGRAM_API_HASH": "too-short",
                "TELEGRAM_SESSION": "session",
            },
            clear=False,
        ):
            with self.assertRaises(TelegramError):
                TelegramConfig.from_env()


class TelegramToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_returns_fenced_messages_in_order(self):
        client = FakeClient()
        tool, events = _tool(client)
        result = await tool.execute("read", chat="bob", limit=2)
        self.assertIn("Untrusted Telegram content", result)
        self.assertIn("1. [2026-11-05 14:30] Bob: older", result)
        self.assertIn("2. [2026-11-05 14:30] Bob: newer", result)
        self.assertEqual(events[0][0], "telegram_read")

    async def test_search_passes_query(self):
        client = FakeClient()
        tool, _ = _tool(client)
        result = await tool.execute("search", chat="bob", query="hello", limit=3)
        self.assertEqual(client.last_search, "hello")
        self.assertEqual(client.last_limit, 3)
        self.assertIn("search 'hello'", result)

    async def test_read_clamps_limit(self):
        client = FakeClient()
        tool, _ = _tool(client)
        await tool.execute("read", chat="bob", limit=999)
        self.assertEqual(client.last_limit, HARD_LIMIT)

    async def test_numeric_chat_id_is_resolved_as_int(self):
        client = FakeClient()
        tool, _ = _tool(client)
        await tool.execute("read", chat="-1001234567890")
        self.assertEqual(client.last_entity_query, -1001234567890)

    async def test_send_requires_confirmation(self):
        client = FakeClient()
        tool, _ = _tool(client)
        result = await tool.execute("send", chat="bob", text="hello")
        self.assertIn("Confirmation required", result)
        self.assertEqual(client.sent, [])

    async def test_send_with_confirmation(self):
        client = FakeClient()
        tool, events = _tool(client)
        result = await tool.execute(
            "send", chat="bob", text="hello", confirmed=True, reply_to=7
        )
        self.assertIn("Sent to Bob", result)
        self.assertEqual(client.sent[0][1], "hello")
        self.assertEqual(client.sent[0][2], 7)
        self.assertEqual(
            [event for event, _ in events],
            ["telegram_send_requested", "telegram_send"],
        )

    async def test_send_fails_closed_when_audit_is_unavailable(self):
        client = FakeClient()

        def broken_audit(event, fields):
            raise OSError("audit down")

        tool = TelegramTool(
            audit=broken_audit,
            client=client,
            config=TelegramConfig(1, HASH, "session"),
        )
        result = await tool.execute(
            "send", chat="bob", text="hello", confirmed=True
        )
        self.assertIn("audit log is unavailable", result)
        self.assertEqual(client.sent, [])

    async def test_chat_allowlist_blocks_unlisted_chat(self):
        client = FakeClient()
        tool, _ = _tool(client, allowed=("999",))
        result = await tool.execute("read", chat="bob")
        self.assertIn("TELEGRAM_ALLOWED_CHAT_IDS", result)

    async def test_chat_allowlist_allows_matching_username(self):
        client = FakeClient()
        client.entity = _entity("Bob", 123, "bob")
        tool, _ = _tool(client, allowed=("@bob",))
        result = await tool.execute("read", chat="bob")
        self.assertNotIn("TELEGRAM_ALLOWED_CHAT_IDS", result)
        self.assertIn("Bob (@bob)", result)

    async def test_me(self):
        client = FakeClient()
        tool, _ = _tool(client)
        result = await tool.execute("me")
        self.assertIn("Alex (@alex)", result)
        self.assertIn("id 42", result)

    async def test_chats(self):
        client = FakeClient()
        tool, _ = _tool(client)
        result = await tool.execute("chats", limit=2)
        self.assertIn("Telegram chats (2)", result)
        self.assertIn("Bob (id 123) — 2 unread", result)
        self.assertIn("Work (id 456)", result)

    async def test_unknown_action(self):
        tool, _ = _tool(FakeClient())
        result = await tool.execute("delete-everything")
        self.assertIn("Unknown Telegram action", result)

    async def test_missing_chat(self):
        tool, _ = _tool(FakeClient())
        result = await tool.execute("read")
        self.assertIn("`chat` is required", result)

    async def test_missing_query(self):
        tool, _ = _tool(FakeClient())
        result = await tool.execute("search", chat="bob")
        self.assertIn("`query` is required", result)

    async def test_missing_config_is_user_facing(self):
        tool = TelegramTool()
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_API_ID": "",
                "TELEGRAM_API_HASH": "",
                "TELEGRAM_SESSION": "",
            },
            clear=False,
        ):
            result = await tool.execute("read", chat="bob")
        self.assertIn("Telegram is not configured", result)
        self.assertIn("scripts/telegram_login.py", result)

    async def test_unresolvable_chat_is_user_facing(self):
        client = FakeClient()
        client.entity = None
        tool, _ = _tool(client)
        result = await tool.execute("read", chat="missing")
        self.assertIn("could not find chat", result)

    async def test_tool_parameters_cover_actions(self):
        self.assertEqual(
            TelegramTool.parameters["properties"]["action"]["enum"],
            ["read", "search", "send", "chats", "me"],
        )
        self.assertIn("confirmed", TelegramTool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
