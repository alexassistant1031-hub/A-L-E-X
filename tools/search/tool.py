"""
tools/search/tool.py

DuckDuckGo search — web, news, images, videos, books, and page extraction.

Uses the `ddgs` package (the maintained successor of `duckduckgo-search`).
No API key, no account, no extra service: queries go straight to DuckDuckGo.

Everything runs in a worker thread so the voice loop never blocks, the number
of results is capped, every call has a timeout, and failures are returned as
readable strings instead of exceptions.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..base import BaseTool

logger = logging.getLogger("alex.tools.search")

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 20
TIMEOUT_SECONDS = 25
RETRY_DELAY = 1.2
MAX_SNIPPET = 300
MAX_EXTRACT_CHARS = 6000

ACTIONS = ("search", "news", "images", "videos", "books", "extract")


# ddgs renamed the package; accept either install.
def _ddgs_class():
    try:
        from ddgs import DDGS

        return DDGS
    except ImportError:  # pragma: no cover - only for older installs
        from duckduckgo_search import DDGS

        return DDGS


def _trim(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _call_sync(action: str, query: str, url: str, kwargs: dict) -> Any:
    DDGS = _ddgs_class()
    with DDGS() as client:
        if action == "extract":
            return client.extract(url, fmt="text_markdown")
        method = "text" if action == "search" else action
        return getattr(client, method)(query, **kwargs)


class SearchTool(BaseTool):
    name = "search"
    description = (
        "INTERNET SEARCH — look things up on DuckDuckGo. Web results, recent "
        "news, images, videos, books, and full-page text extraction. Use it "
        "for anything current, factual, or outside your own knowledge."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": (
                    "'search' = web results, 'news' = recent articles, "
                    "'images' = image results, 'videos' = video results, "
                    "'books' = book results, 'extract' = read a page as text."
                ),
            },
            "query": {
                "type": "string",
                "description": "What to search for. Required for every action except 'extract'.",
            },
            "max_results": {
                "type": "integer",
                "description": f"How many results to return (1-{MAX_RESULTS_CAP}, default {DEFAULT_MAX_RESULTS}).",
            },
            "region": {
                "type": "string",
                "description": "Region code such as 'in-en', 'us-en', 'uk-en'. Empty uses the default.",
            },
            "timelimit": {
                "type": "string",
                "enum": ["any", "d", "w", "m", "y"],
                "description": "Recency filter: d=day, w=week, m=month, y=year, any=no filter.",
            },
            "safesearch": {
                "type": "string",
                "enum": ["on", "moderate", "off"],
                "description": "Adult-content filter. Default 'moderate'.",
            },
            "url": {
                "type": "string",
                "description": "Page to read (only for action='extract').",
            },
        },
        "required": ["action"],
    }

    async def execute(
        self,
        action: str = "search",
        query: str = "",
        max_results: int = DEFAULT_MAX_RESULTS,
        region: str = "",
        timelimit: str = "any",
        safesearch: str = "moderate",
        url: str = "",
    ) -> str:
        action = (action or "search").strip().lower()
        if action not in ACTIONS:
            return f"Error: unknown action {action!r}. Use one of: {', '.join(ACTIONS)}."

        if action == "extract":
            return await self._extract(url)

        query = (query or "").strip()
        if not query:
            return "Error: a search query is required."

        try:
            limit = int(max_results)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_RESULTS
        limit = max(1, min(limit, MAX_RESULTS_CAP))

        kwargs: dict[str, Any] = {"max_results": limit}
        if action != "books":
            if region.strip():
                kwargs["region"] = region.strip()
            kwargs["safesearch"] = safesearch if safesearch in ("on", "moderate", "off") else "moderate"
            if action in ("search", "news", "images", "videos") and timelimit in ("d", "w", "m", "y"):
                kwargs["timelimit"] = timelimit

        rows = await self._run(action, query, "", kwargs)
        if isinstance(rows, str):  # error message
            return rows

        formatter = {
            "search": self._format_web,
            "news": self._format_news,
            "images": self._format_images,
            "videos": self._format_videos,
            "books": self._format_books,
        }[action]
        return formatter(query, rows)

    # ── execution ─────────────────────────────────────────────────────────────

    async def _run(self, action: str, query: str, url: str, kwargs: dict) -> Any:
        """Run one DuckDuckGo call off the event loop, with timeout and retry."""
        last_error = ""
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_call_sync, action, query, url, kwargs),
                    TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                last_error = "the request timed out"
            except Exception as e:
                message = str(e) or e.__class__.__name__
                if "no results" in message.lower():
                    return []
                last_error = message

            if attempt == 1:
                logger.warning("%s search failed (%s); retrying once", action, last_error[:120])
                await asyncio.sleep(RETRY_DELAY)

        logger.error("%s search failed: %s", action, last_error)
        return (
            f"Error: the {action} request failed ({last_error[:200]}). "
            "DuckDuckGo may be rate-limiting; try again in a moment."
        )

    async def _extract(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return "Error: action='extract' needs a URL."
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        result = await self._run("extract", "", url, {})
        if isinstance(result, str):
            return result

        content = str((result or {}).get("content") or "").strip()
        if not content:
            return f"No readable text found at {url}."

        total = len(content)
        if total > MAX_EXTRACT_CHARS:
            content = content[:MAX_EXTRACT_CHARS].rstrip() + "\n\n[truncated]"
        return f"Content of {url} ({total} characters):\n\n{content}"

    # ── formatting ────────────────────────────────────────────────────────────

    @staticmethod
    def _empty(action: str, query: str) -> str:
        return f'No {action} results for "{query}".'

    def _format_web(self, query: str, rows: list[dict]) -> str:
        if not rows:
            return self._empty("web", query)
        lines = [f'Web results for "{query}" ({len(rows)}):']
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. {_trim(row.get('title'), 120)}")
            if row.get("href"):
                lines.append(f"   {row['href']}")
            body = _trim(row.get("body"), MAX_SNIPPET)
            if body:
                lines.append(f"   {body}")
        return "\n".join(lines)

    def _format_news(self, query: str, rows: list[dict]) -> str:
        if not rows:
            return self._empty("news", query)
        lines = [f'News for "{query}" ({len(rows)}):']
        for i, row in enumerate(rows, 1):
            meta = ", ".join(x for x in (row.get("source"), row.get("date")) if x)
            lines.append(f"{i}. {_trim(row.get('title'), 120)}" + (f" — {meta}" if meta else ""))
            if row.get("url"):
                lines.append(f"   {row['url']}")
            body = _trim(row.get("body"), MAX_SNIPPET)
            if body:
                lines.append(f"   {body}")
        return "\n".join(lines)

    def _format_images(self, query: str, rows: list[dict]) -> str:
        if not rows:
            return self._empty("image", query)
        lines = [f'Image results for "{query}" ({len(rows)}):']
        for i, row in enumerate(rows, 1):
            size = ""
            if row.get("width") and row.get("height"):
                size = f" ({row['width']}x{row['height']})"
            source = f" — {row['source']}" if row.get("source") else ""
            lines.append(f"{i}. {_trim(row.get('title'), 100)}{source}{size}")
            if row.get("image"):
                lines.append(f"   image: {row['image']}")
            if row.get("url"):
                lines.append(f"   page: {row['url']}")
        return "\n".join(lines)

    def _format_videos(self, query: str, rows: list[dict]) -> str:
        if not rows:
            return self._empty("video", query)
        lines = [f'Video results for "{query}" ({len(rows)}):']
        for i, row in enumerate(rows, 1):
            meta = ", ".join(x for x in (row.get("publisher"), row.get("duration")) if x)
            lines.append(f"{i}. {_trim(row.get('title'), 120)}" + (f" — {meta}" if meta else ""))
            link = row.get("embed_url")
            if link:
                lines.append(f"   {link}")
            body = _trim(row.get("description") or row.get("content"), 200)
            if body:
                lines.append(f"   {body}")
        return "\n".join(lines)

    def _format_books(self, query: str, rows: list[dict]) -> str:
        if not rows:
            return self._empty("book", query)
        lines = [f'Book results for "{query}" ({len(rows)}):']
        for i, row in enumerate(rows, 1):
            meta = ", ".join(
                str(x) for x in (row.get("author"), row.get("publisher"), row.get("year")) if x
            )
            lines.append(f"{i}. {_trim(row.get('title'), 120)}" + (f" — {meta}" if meta else ""))
            if row.get("url"):
                lines.append(f"   {row['url']}")
            info = _trim(row.get("info"), 200)
            if info:
                lines.append(f"   {info}")
        return "\n".join(lines)
