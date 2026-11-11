"""Tests for the GitHub tool."""

from __future__ import annotations

import base64
import json
import os
import unittest
from unittest import mock

from tools.github_tool import (
    ACTIONS,
    GitHubConfig,
    GitHubError,
    GitHubTool,
)


class FakeResponse:
    def __init__(self, status: int = 200, data=None, text: str = "") -> None:
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = text or json.dumps(self._data)
        self.content = self.text.encode("utf-8")
        self.headers = {}

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    async def request(self, method, path, params=None, json=None):
        self.calls.append(
            {"method": method, "path": path, "params": params, "json": json}
        )
        route = self.routes.get((method, path))
        if route is None:
            route = self.routes.get(path)
        if route is None:
            return FakeResponse(404, {"message": "Not Found"})
        if callable(route):
            return route(method, path, params, json)
        if isinstance(route, FakeResponse):
            return route
        return FakeResponse(200, route)


def _config() -> GitHubConfig:
    return GitHubConfig(token="test-token", owner="alex", repo="repo")


def _tool(
    routes: dict | None = None,
    audit=None,
    config: GitHubConfig | None = None,
) -> tuple[GitHubTool, FakeClient, list]:
    events: list[tuple[str, dict]] = []

    def default_audit(event, fields):
        events.append((event, fields))

    client = FakeClient(routes or {})
    tool = GitHubTool(
        audit=audit or default_audit,
        client=client,
        config=config or _config(),
    )
    return tool, client, events


class GitHubConfigTests(unittest.TestCase):
    def test_from_env(self):
        with mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "token",
                "GITHUB_OWNER": "alex",
                "GITHUB_REPO": "repo",
            },
            clear=True,
        ):
            config = GitHubConfig.from_env()
        self.assertEqual(config.token, "token")
        self.assertEqual(config.slug, "alex/repo")

    def test_from_env_combined_slug(self):
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "alex/repo"},
            clear=True,
        ):
            config = GitHubConfig.from_env()
        self.assertEqual(config.owner, "alex")
        self.assertEqual(config.repo, "repo")

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(GitHubError):
                GitHubConfig.from_env()

    def test_bad_slug(self):
        with mock.patch.dict(
            os.environ,
            {"GH_TOKEN": "token", "GITHUB_OWNER": "alex", "GITHUB_REPO": ""},
            clear=True,
        ):
            with self.assertRaises(GitHubError):
                GitHubConfig.from_env()


class GitHubToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_repo_metadata(self):
        tool, client, events = _tool(
            {
                ("GET", "/repos/alex/repo"): {
                    "full_name": "alex/repo",
                    "private": True,
                    "default_branch": "main",
                    "description": "Alex",
                    "stargazers_count": 3,
                    "forks_count": 1,
                    "open_issues_count": 2,
                    "html_url": "https://github.com/alex/repo",
                    "permissions": {"admin": True, "push": True},
                    "archived": False,
                }
            }
        )
        result = await tool.execute("repo")
        self.assertIn("alex/repo", result)
        self.assertIn("default branch: main", result)
        self.assertIn("admin", result)
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertEqual(events[-1][0], "github_repo")

    async def test_file_read_decodes_and_marks_untrusted(self):
        body = "hello from GitHub"
        tool, _client, events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/README.md"): {
                    "path": "README.md",
                    "size": len(body),
                    "sha": "abc123",
                    "encoding": "base64",
                    "content": base64.b64encode(body.encode()).decode(),
                }
            }
        )
        result = await tool.execute("file_read", path="README.md")
        self.assertIn("Untrusted GitHub content", result)
        self.assertIn(body, result)
        self.assertEqual(events[-1][0], "github_file_read")

    async def test_file_write_requires_confirmation(self):
        tool, client, _events = _tool()
        result = await tool.execute(
            "file_write", path="note.txt", content="hi", message="add note"
        )
        self.assertIn("Confirmation required", result)
        self.assertEqual(client.calls, [])

    async def test_file_write_updates_existing_file(self):
        tool, client, events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/note.txt"): {"sha": "old-sha"},
                ("PUT", "/repos/alex/repo/contents/note.txt"): {
                    "commit": {"sha": "new-commit"}
                },
            }
        )
        result = await tool.execute(
            "file_write",
            path="note.txt",
            content="new content",
            message="update note",
            confirmed=True,
        )
        self.assertIn("Wrote 11 bytes", result)
        self.assertIn("new-commit", result)
        put = [call for call in client.calls if call["method"] == "PUT"][0]
        self.assertEqual(put["json"]["sha"], "old-sha")
        self.assertEqual(
            base64.b64decode(put["json"]["content"]).decode(), "new content"
        )
        self.assertIn("github_file_write", [event for event, _ in events])

    async def test_file_write_new_file_omits_sha(self):
        tool, client, _events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/new.txt"): FakeResponse(
                    404, {"message": "Not Found"}
                ),
                ("PUT", "/repos/alex/repo/contents/new.txt"): {
                    "commit": {"sha": "abc"}
                },
            }
        )
        result = await tool.execute(
            "file_write",
            path="new.txt",
            content="x",
            message="create",
            confirmed=True,
        )
        self.assertIn("Wrote 1 bytes", result)
        put = [call for call in client.calls if call["method"] == "PUT"][0]
        self.assertNotIn("sha", put["json"])

    async def test_file_delete_requires_sha(self):
        tool, client, _events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/gone.txt"): FakeResponse(
                    404, {"message": "Not Found"}
                ),
            }
        )
        result = await tool.execute(
            "file_delete",
            path="gone.txt",
            message="remove",
            confirmed=True,
        )
        self.assertIn("File not found", result)
        self.assertFalse(
            [call for call in client.calls if call["method"] == "DELETE"]
        )

    async def test_issue_create_requires_title_and_confirmation(self):
        tool, client, _events = _tool()
        result = await tool.execute("issue_create", confirmed=True)
        self.assertIn("`title` is required", result)
        result = await tool.execute("issue_create", title="Bug")
        self.assertIn("Confirmation required", result)
        self.assertEqual(client.calls, [])

    async def test_issue_create(self):
        tool, _client, events = _tool(
            {
                ("POST", "/repos/alex/repo/issues"): {
                    "number": 7,
                    "html_url": "https://github.com/alex/repo/issues/7",
                }
            }
        )
        result = await tool.execute(
            "issue_create",
            title="Bug",
            body="details",
            labels="bug,urgent",
            confirmed=True,
        )
        self.assertIn("#7", result)
        self.assertIn("github_issue_create", [event for event, _ in events])

    async def test_repo_delete_requires_exact_confirm(self):
        tool, client, _events = _tool(
            {
                ("DELETE", "/repos/alex/repo"): FakeResponse(204),
            }
        )
        result = await tool.execute(
            "repo_delete", confirmed=True, confirm="wrong"
        )
        self.assertIn("Refusing to delete", result)
        self.assertEqual(client.calls, [])

        result = await tool.execute(
            "repo_delete", confirmed=True, confirm="alex/repo"
        )
        self.assertIn("Deleted repository", result)
        self.assertEqual(client.calls[0]["method"], "DELETE")

    async def test_search(self):
        tool, _client, events = _tool(
            {
                ("GET", "/search/repositories"): {
                    "total_count": 1,
                    "items": [
                        {
                            "full_name": "alex/repo",
                            "stargazers_count": 5,
                            "description": "Alex",
                        }
                    ],
                }
            }
        )
        result = await tool.execute("search", query="alex", kind="repos")
        self.assertIn("alex/repo", result)
        self.assertEqual(events[-1][0], "github_search")

    async def test_api_post_requires_confirmation(self):
        tool, client, _events = _tool()
        result = await tool.execute(
            "api", path="/user/repos", method="POST", api_body='{"name":"x"}'
        )
        self.assertIn("Confirmation required", result)
        self.assertEqual(client.calls, [])

    async def test_api_get_is_read_only(self):
        tool, _client, events = _tool(
            {("GET", "/rate_limit"): {"rate": {"remaining": 10}}}
        )
        result = await tool.execute("api", path="/rate_limit")
        self.assertIn("remaining", result)
        self.assertIn("github_api", [event for event, _ in events])

    async def test_audit_failure_blocks_write(self):
        def broken_audit(event, fields):
            raise OSError("audit down")

        tool, client, _events = _tool(audit=broken_audit)
        result = await tool.execute(
            "issue_create", title="Bug", confirmed=True
        )
        self.assertIn("audit log is unavailable", result)
        self.assertEqual(client.calls, [])

    async def test_unknown_action(self):
        tool, _client, _events = _tool()
        result = await tool.execute("destroy-everything")
        self.assertIn("Unknown GitHub action", result)

    async def test_action_schema_covers_actions(self):
        self.assertEqual(
            GitHubTool.parameters["properties"]["action"]["enum"], list(ACTIONS)
        )
        self.assertIn("confirmed", GitHubTool.parameters["properties"])

    async def test_401_does_not_echo_token(self):
        tool, _client, _events = _tool(
            {("GET", "/repos/alex/repo"): FakeResponse(401, {"message": "Bad credentials"})}
        )
        result = await tool.execute("repo")
        self.assertIn("rejected the token", result)
        self.assertNotIn("test-token", result)

    async def test_me_works_without_a_configured_repo(self):
        tool, _client, events = _tool(
            {("GET", "/user"): {"login": "octocat", "id": 1}},
            config=GitHubConfig(token="token"),
        )
        result = await tool.execute("me")
        self.assertIn("octocat", result)
        self.assertEqual(events[-1][0], "github_me")

    async def test_global_search_works_without_a_configured_repo(self):
        tool, _client, events = _tool(
            {("GET", "/search/repositories"): {"total_count": 0, "items": []}},
            config=GitHubConfig(token="token"),
        )
        result = await tool.execute("search", query="alex")
        self.assertIn("search for 'alex'", result)
        self.assertEqual(events[-1][0], "github_search")

    async def test_api_get_works_without_a_configured_repo(self):
        tool, _client, events = _tool(
            {("GET", "/rate_limit"): {"rate": {"remaining": 10}}},
            config=GitHubConfig(token="token"),
        )
        result = await tool.execute("api", path="/rate_limit")
        self.assertIn("remaining", result)
        self.assertEqual(events[-1][0], "github_api")

    async def test_repo_update_can_clear_private_and_archived(self):
        tool, client, _events = _tool(
            {
                ("PATCH", "/repos/alex/repo"): {
                    "full_name": "alex/repo",
                    "private": False,
                    "archived": False,
                }
            }
        )
        result = await tool.execute(
            "repo_update",
            description="public again",
            private=False,
            archived=False,
            confirmed=True,
        )
        self.assertIn("Repository updated", result)
        patch = client.calls[0]
        self.assertEqual(patch["json"]["private"], False)
        self.assertEqual(patch["json"]["archived"], False)

    async def test_repo_update_omits_booleans_when_not_provided(self):
        tool, client, _events = _tool(
            {("PATCH", "/repos/alex/repo"): {"full_name": "alex/repo"}}
        )
        await tool.execute("repo_update", description="new", confirmed=True)
        payload = client.calls[0]["json"]
        self.assertNotIn("private", payload)
        self.assertNotIn("archived", payload)

    async def test_repo_update_topics_uses_the_topics_endpoint(self):
        tool, client, _events = _tool(
            {
                ("PATCH", "/repos/alex/repo"): {"full_name": "alex/repo"},
                ("PUT", "/repos/alex/repo/topics"): {"names": ["ai", "voice"]},
            }
        )
        result = await tool.execute(
            "repo_update",
            description="new description",
            topics="ai, voice",
            confirmed=True,
        )
        self.assertIn("Repository updated", result)
        patch = [call for call in client.calls if call["method"] == "PATCH"][0]
        self.assertNotIn("topics", patch["json"])
        topics = [call for call in client.calls if call["method"] == "PUT"][0]
        self.assertEqual(topics["path"], "/repos/alex/repo/topics")
        self.assertEqual(topics["json"], {"names": ["ai", "voice"]})

    async def test_variable_set_propagates_a_non_404_error(self):
        tool, client, _events = _tool(
            {
                ("GET", "/repos/alex/repo/actions/variables/FOO"): FakeResponse(
                    403, {"message": "Forbidden"}
                )
            }
        )
        result = await tool.execute(
            "variable_set", name="FOO", value="bar", confirmed=True
        )
        self.assertIn("403", result)
        self.assertFalse([call for call in client.calls if call["method"] == "POST"])

    async def test_variable_set_creates_only_on_404(self):
        tool, client, events = _tool(
            {
                ("GET", "/repos/alex/repo/actions/variables/FOO"): FakeResponse(
                    404, {"message": "Not Found"}
                ),
                ("POST", "/repos/alex/repo/actions/variables"): {"name": "FOO"},
            }
        )
        result = await tool.execute(
            "variable_set", name="FOO", value="bar", confirmed=True
        )
        self.assertIn("Created variable FOO", result)
        self.assertIn("github_variable_set", [event for event, _ in events])

    async def test_file_write_propagates_a_sha_lookup_auth_error(self):
        tool, client, _events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/note.txt"): FakeResponse(
                    403, {"message": "Forbidden"}
                )
            }
        )
        result = await tool.execute(
            "file_write",
            path="note.txt",
            content="x",
            message="update",
            confirmed=True,
        )
        self.assertIn("403", result)
        self.assertFalse([call for call in client.calls if call["method"] == "PUT"])

    async def test_contents_honours_the_branch_argument(self):
        tool, client, _events = _tool(
            {("GET", "/repos/alex/repo/contents/docs"): [{"name": "a.md"}]}
        )
        await tool.execute("contents", path="docs", branch="dev")
        self.assertEqual(client.calls[0]["params"], {"ref": "dev"})

    async def test_contents_caps_a_large_file_read(self):
        body = "x" * 20_000
        tool, _client, _events = _tool(
            {
                ("GET", "/repos/alex/repo/contents/big.txt"): {
                    "path": "big.txt",
                    "size": len(body),
                    "encoding": "base64",
                    "content": base64.b64encode(body.encode()).decode(),
                }
            }
        )
        result = await tool.execute("contents", path="big.txt")
        self.assertIn("[truncated]", result)
        self.assertLessEqual(len(result), 12_000)

    async def test_dispatch_rejects_non_object_inputs(self):
        tool, client, _events = _tool()
        result = await tool.execute(
            "dispatch",
            workflow="ci.yml",
            inputs='["not", "an", "object"]',
            confirmed=True,
        )
        self.assertIn("must be a JSON object", result)
        self.assertEqual(client.calls, [])

    async def test_repo_delete_invalid_slug_is_user_facing(self):
        tool, client, _events = _tool()
        result = await tool.execute(
            "repo_delete", repo="not-a-slug", confirm="not-a-slug", confirmed=True
        )
        self.assertIn("GitHub error", result)
        self.assertIn("owner/name", result)
        self.assertEqual(client.calls, [])

    async def test_bad_number_is_reported(self):
        tool, client, _events = _tool()
        result = await tool.execute("issue", number="not-a-number")
        self.assertIn("`number` must be an integer", result)
        self.assertEqual(client.calls, [])

    async def test_webhook_create_defaults_to_push_for_blank_events(self):
        tool, client, _events = _tool(
            {("POST", "/repos/alex/repo/hooks"): {"id": 5}}
        )
        await tool.execute(
            "webhook_create",
            url="https://example.com/hook",
            events=" , ",
            confirmed=True,
        )
        self.assertEqual(client.calls[0]["json"]["events"], ["push"])


if __name__ == "__main__":
    unittest.main()
