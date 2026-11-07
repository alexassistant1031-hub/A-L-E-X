"""Tests for the sandboxed Agent Reach adapter.

Every test uses a fake runner, so nothing here touches the network, installs
Agent Reach, or writes outside a temporary directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from tools.reach_tool import (  # noqa: E402
    OPERATIONS,
    ProcessResult,
    ReachConfig,
    ReachTool,
)

SECRET_ENV = {
    "GEMINI_API_KEY": "secret-gemini",
    "GOOGLE_API_KEY": "secret-google",
    "GROQ_API_KEY": "secret-groq",
    "OPENAI_API_KEY": "secret-openai",
    "GH_TOKEN": "secret-gh",
    "GITHUB_TOKEN": "secret-github",
}


def envelope(
    *,
    ok: bool = True,
    channel: str = "github",
    action: str = "search",
    backend: str = "github-api",
    items: list[dict] | None = None,
    error: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "ok": ok,
        "channel": channel,
        "action": action,
        "backend": backend if ok else None,
        "items": items or [],
        "meta": {"elapsed_ms": 12, "attempts": []},
        "error": error,
    }


class FakeRunner:
    """Capture argv/env and return a scripted result."""

    def __init__(self, result: ProcessResult | Exception):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, argv, *, env, cwd, timeout, apply_limits=True):
        self.calls.append(
            {
                "argv": list(argv),
                "env": dict(env),
                "cwd": Path(cwd),
                "timeout": timeout,
                "apply_limits": apply_limits,
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return self.result()
        return self.result


class ReachToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="alex-reach-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = ReachConfig(
            binary=Path("/bin/true"),
            state_root=root / "state",
            home=root / "state" / "home",
            sandbox="rlimit",
            audit=False,
            cache_ttl=300.0,
        )

    def tool(self, result: ProcessResult | Exception) -> tuple[ReachTool, FakeRunner]:
        runner = FakeRunner(result)
        return ReachTool(config=self.config, runner=runner), runner

    def ok_result(self, payload: dict) -> ProcessResult:
        return ProcessResult(
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    async def test_github_search_argv_and_formatting(self) -> None:
        payload = envelope(
            items=[
                {
                    "title": "livekit/agents",
                    "url": "https://github.com/livekit/agents",
                    "author": "livekit",
                    "published_at": "2023-10-19T23:00:55Z",
                    "text": "<p>Framework for voice &amp; video agents.</p>",
                    "raw": {"large": "ignored"},
                }
            ]
        )
        tool, runner = self.tool(self.ok_result(payload))

        output = await tool.execute("github_search", query="livekit agents", limit=2)

        self.assertIn("Untrusted external content", output)
        self.assertIn("livekit/agents", output)
        self.assertIn("Framework for voice & video agents.", output)
        self.assertIn("https://github.com/livekit/agents", output)
        self.assertNotIn("<p>", output)
        self.assertEqual(
            runner.calls[0]["argv"][1:],
            ["github", "search", "--limit", "2", "--timeout", "18", "--", "livekit agents"],
        )
        self.assertTrue(runner.calls[0]["apply_limits"])
        self.assertEqual(runner.calls[0]["cwd"], self.config.work)

    async def test_youtube_subtitle_flags_come_before_separator(self) -> None:
        tool, runner = self.tool(
            self.ok_result(
                envelope(channel="youtube", action="subtitle", backend="yt-dlp", items=[])
            )
        )
        await tool.execute("youtube_subtitle", target="dQw4w9WgXcQ", lang="hi,en")
        self.assertEqual(
            runner.calls[0]["argv"][1:],
            [
                "youtube",
                "subtitle",
                "--lang",
                "hi,en",
                "--timeout",
                "28",
                "--",
                "dQw4w9WgXcQ",
            ],
        )

    async def test_v2ex_hot_has_no_positional_separator(self) -> None:
        tool, runner = self.tool(
            self.ok_result(envelope(channel="v2ex", action="hot", backend="v2ex-api"))
        )
        await tool.execute("v2ex_hot", limit=3)
        self.assertEqual(
            runner.calls[0]["argv"][1:],
            ["v2ex", "hot", "--limit", "3", "--timeout", "18"],
        )

    async def test_linkedin_and_boss_flags(self) -> None:
        tool, runner = self.tool(
            self.ok_result(envelope(channel="linkedin", action="jobs", backend="opencli"))
        )
        tool.config = ReachConfig(
            **{**self.config.__dict__, "allow_browser": True}
        )
        await tool.execute("linkedin_jobs", query="python", location="Berlin", limit=3)
        self.assertEqual(
            runner.calls[0]["argv"][1:],
            [
                "linkedin",
                "jobs",
                "--location",
                "Berlin",
                "--limit",
                "3",
                "--timeout",
                "28",
                "--",
                "python",
            ],
        )

        tool2, runner2 = self.tool(
            self.ok_result(envelope(channel="boss", action="search", backend="opencli"))
        )
        tool2.config = ReachConfig(**{**self.config.__dict__, "allow_browser": True})
        await tool2.execute("boss_search", query="engineer", city="Shanghai", limit=2)
        self.assertEqual(
            runner2.calls[0]["argv"][1:],
            [
                "boss",
                "search",
                "--city",
                "Shanghai",
                "--limit",
                "2",
                "--timeout",
                "28",
                "--",
                "engineer",
            ],
        )

    async def test_error_envelope_is_human_readable(self) -> None:
        payload = envelope(
            ok=False,
            channel="github",
            action="read",
            error={
                "code": "NEED_LOGIN",
                "message": "saved session expired",
                "hint": "log in again",
            },
        )
        tool, _ = self.tool(self.ok_result(payload))
        output = await tool.execute("github_read", target="owner/repo")
        self.assertIn("needs a one-time login", output)
        self.assertIn("NEED_LOGIN", output)
        self.assertIn("agent-reach login github", output)
        self.assertIn("HOME=", output)

    async def test_timeout_is_reported(self) -> None:
        tool, _ = self.tool(ProcessResult(returncode=-1, timed_out=True))
        output = await tool.execute("github_search", query="slow")
        self.assertIn("timed out", output)
        self.assertIn("github/search", output)

    async def test_malformed_output_is_rejected(self) -> None:
        tool, _ = self.tool(ProcessResult(returncode=0, stdout=b"not json at all"))
        output = await tool.execute("github_search", query="x")
        self.assertIn("unexpected response", output)
        self.assertIn("not json at all", output)

    async def test_old_schema_version_is_rejected(self) -> None:
        payload = envelope(items=[])
        payload["schema_version"] = 99
        tool, _ = self.tool(self.ok_result(payload))
        output = await tool.execute("github_search", query="x")
        self.assertIn("unsupported schema version", output)

    async def test_missing_binary_suggests_install(self) -> None:
        config = ReachConfig(
            binary=Path(self.temp.name) / "does-not-exist",
            state_root=Path(self.temp.name) / "state",
            home=Path(self.temp.name) / "state" / "home",
            sandbox="none",
            audit=False,
        )
        tool = ReachTool(config=config, runner=FakeRunner(ProcessResult(0)))
        output = await tool.execute("github_search", query="x")
        self.assertIn("not usable", output)
        self.assertIn("uv tool install", output)

    async def test_file_url_is_rejected_before_spawn(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope()))
        output = await tool.execute("web_read", target="file:///etc/passwd")
        self.assertIn("rejected", output)
        self.assertEqual(runner.calls, [])

    async def test_browser_channels_can_be_switched_off(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope()))
        tool.config = ReachConfig(
            **{**tool.config.__dict__, "allow_browser": False}
        )
        output = await tool.execute("xiaohongshu_search", query="coffee")
        self.assertIn("ALEX_REACH_ALLOW_BROWSER=0", output)
        self.assertIn("agent-reach login xiaohongshu", output)
        self.assertEqual(runner.calls, [])

    async def test_browser_channels_are_on_by_default(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope()))
        self.assertTrue(tool.config.allow_browser)
        output = await tool.execute("xiaohongshu_search", query="coffee")
        self.assertNotIn("ALEX_REACH_ALLOW_BROWSER", output)
        self.assertTrue(runner.calls)

    async def test_management_actions_do_not_exist(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope()))
        for action in ("setup", "login", "update", "uninstall", "add"):
            output = await tool.execute(action)
            self.assertIn("Unknown Agent Reach action", output)
        self.assertEqual(runner.calls, [])

    async def test_required_parameter_is_validated(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope()))
        output = await tool.execute("github_search", query="   ")
        self.assertIn("requires `query`", output)
        self.assertEqual(runner.calls, [])

    async def test_output_is_capped(self) -> None:
        config = ReachConfig(
            **{**self.config.__dict__, "max_output_chars": 500, "max_items": 5}
        )
        runner = FakeRunner(
            self.ok_result(
                envelope(
                    items=[
                        {
                            "title": f"Result {index}",
                            "url": f"https://example.com/{index}",
                            "text": "word " * 300,
                        }
                        for index in range(5)
                    ]
                )
            )
        )
        tool = ReachTool(config=config, runner=runner)
        output = await tool.execute("github_search", query="x", limit=5)
        self.assertLessEqual(len(output), 550)
        self.assertIn("[truncated]", output)

    async def test_every_allowed_action_can_be_prepared(self) -> None:
        tool, _ = self.tool(self.ok_result(envelope(items=[{"title": "ok"}])))
        dummy = {
            "query": "example",
            "target": "https://example.com/page",
            "lang": "en",
            "location": "Berlin",
            "city": "Shanghai",
            "node_name": "python",
            "username": "example",
            "symbol": "AAPL",
            "name": "python",
            "limit": 3,
        }
        for name, op in OPERATIONS.items():
            call = tool._prepare(op, **dummy)
            self.assertIn(op.channel, call.tail)
            self.assertIn(op.action, call.tail)
            if op.positional:
                self.assertIn("--", call.tail)
                index = call.tail.index("--")
                self.assertEqual(len(call.tail) - index - 1, len(op.positional))
                for value in call.tail[index + 1 :]:
                    self.assertFalse(value.startswith("-"))
            else:
                self.assertNotIn("--", call.tail)

    async def test_world_writable_binary_is_rejected(self) -> None:
        binary = Path(self.temp.name) / "agent-reach"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o777)
        config = ReachConfig(
            binary=binary,
            state_root=Path(self.temp.name) / "state",
            home=Path(self.temp.name) / "state" / "home",
            sandbox="none",
            audit=False,
        )
        tool = ReachTool(config=config, runner=FakeRunner(ProcessResult(0)))
        output = await tool.execute("github_search", query="x")
        self.assertIn("world-writable", output)

    async def test_social_identifiers_are_normalized(self) -> None:
        tool, runner = self.tool(
            self.ok_result(envelope(channel="twitter", action="profile", backend="opencli"))
        )
        tool.config = ReachConfig(**{**self.config.__dict__, "allow_browser": True})
        await tool.execute("twitter_profile", username="@example")
        self.assertEqual(runner.calls[0]["argv"][-1], "example")

        await tool.execute("reddit_subreddit", name="r/python")
        self.assertEqual(runner.calls[1]["argv"][-1], "python")

        await tool.execute("xueqiu_quote", symbol="$AAPL")
        self.assertEqual(runner.calls[2]["argv"][-1], "AAPL")

    async def test_cache_avoids_duplicate_work(self) -> None:
        payload = envelope(items=[{"title": "cached", "text": "x"}])
        tool, runner = self.tool(self.ok_result(payload))
        first = await tool.execute("github_search", query="same")
        second = await tool.execute("github_search", query="same")
        self.assertEqual(first, second)
        self.assertEqual(len(runner.calls), 1)

        await tool.execute("github_search", query="different")
        self.assertEqual(len(runner.calls), 2)

    async def test_environment_is_scrubbed(self) -> None:
        tool, runner = self.tool(self.ok_result(envelope(items=[{"title": "ok"}])))
        with mock.patch.dict(os.environ, SECRET_ENV, clear=False):
            await tool.execute("github_search", query="x")

        env = runner.calls[0]["env"]
        for name in SECRET_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], str(self.config.home))
        self.assertEqual(env["TMPDIR"], str(self.config.tmp))
        self.assertEqual(env["GH_TELEMETRY"], "false")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    async def test_github_token_is_opt_in(self) -> None:
        config = ReachConfig(**{**self.config.__dict__, "pass_github_token": True})
        runner = FakeRunner(self.ok_result(envelope(items=[{"title": "ok"}])))
        tool = ReachTool(config=config, runner=runner)
        with mock.patch.dict(os.environ, {"GH_TOKEN": "token"}, clear=False):
            await tool.execute("github_search", query="x")
        self.assertEqual(runner.calls[0]["env"].get("GH_TOKEN"), "token")

    async def test_every_allowed_action_has_a_spec(self) -> None:
        self.assertGreaterEqual(len(OPERATIONS), 35)
        for name, op in OPERATIONS.items():
            self.assertTrue(name.startswith(op.channel + "_"), name)
            self.assertTrue(op.action, name)
            for param in op.positional + op.flags:
                self.assertIn(
                    param,
                    {
                        "query",
                        "target",
                        "limit",
                        "lang",
                        "location",
                        "city",
                        "node_name",
                        "username",
                        "symbol",
                        "name",
                    },
                    f"{name}: {param}",
                )

    def test_config_bounds(self) -> None:
        config = ReachConfig(
            state_root=Path(self.temp.name) / "s",
            home=Path(self.temp.name) / "s" / "h",
            timeout=9999,
            max_output_chars=1,
            max_items=999,
            cache_ttl=-1,
        )
        self.assertEqual(config.timeout, 180.0)
        self.assertEqual(config.max_output_chars, 500)
        self.assertEqual(config.max_items, 10)
        self.assertEqual(config.cache_ttl, 0.0)
        self.assertEqual(config.home, Path(self.temp.name) / "s" / "h")

    def test_home_defaults_under_state_root(self) -> None:
        config = ReachConfig(state_root=Path(self.temp.name) / "only")
        self.assertEqual(config.home, Path(self.temp.name) / "only" / "home")


class ReachConfigEnvTests(unittest.TestCase):
    def test_env_overrides(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "ALEX_REACH_ENABLED": "0",
                "ALEX_REACH_ALLOW_BROWSER": "1",
                "ALEX_REACH_SHARED_HOME": "1",
                "ALEX_REACH_TIMEOUT": "42",
                "ALEX_REACH_MAX_ITEMS": "7",
                "ALEX_REACH_SANDBOX": "rlimit",
            },
            clear=False,
        ):
            config = ReachConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertTrue(config.allow_browser)
        self.assertTrue(config.shared_home)
        self.assertEqual(config.timeout, 42.0)
        self.assertEqual(config.max_items, 7)
        self.assertEqual(config.sandbox, "rlimit")


if __name__ == "__main__":
    unittest.main()
