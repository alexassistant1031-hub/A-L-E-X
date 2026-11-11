"""
tools/search/operations.py

Exposes DuckDuckGo search to Alex as a LiveKit function tool.
"""

from __future__ import annotations

import logging
from typing import Literal

from livekit.agents import llm

from termviz import get_visualizer

from .tool import SearchTool

from recovery import SafetyTag, execute_tool

logger = logging.getLogger("alex.tools.search_ops")

_search = SearchTool()


@llm.function_tool
async def web_search(
    action: Literal["search", "news", "images", "videos", "books", "extract"] = "search",
    query: str = "",
    max_results: int = 5,
    region: str = "",
    timelimit: Literal["any", "d", "w", "m", "y"] = "any",
    safesearch: Literal["on", "moderate", "off"] = "moderate",
    url: str = "",
) -> str:
    """Search the internet with DuckDuckGo, or read a web page.

    Use this whenever the answer depends on current or external facts: news,
    prices, weather, sports, products, people, releases, definitions, "search
    for ...", "look up ...", "what's happening with ...". Prefer it over
    guessing, and never claim you searched unless you called this tool.

    Actions:
        search  - normal web results (title, url, snippet)
        news    - recent articles with source and date
        images  - image links with dimensions and source
        videos  - video results with publisher and duration
        books   - book results with author and publisher
        extract - download a page and return its readable text

    Args:
        action: Which kind of search to run. Defaults to 'search'.
        query: What to look for. Required for every action except 'extract'.
        max_results: Number of results, 1-20. Keep it small for voice.
        region: Region code like 'in-en' or 'us-en'. Empty = default.
        timelimit: Recency filter: 'd' day, 'w' week, 'm' month, 'y' year,
            'any' no filter.
        safesearch: 'on', 'moderate' or 'off'.
        url: The page to read when action='extract'.
    """
    label = (query or url or action)[:60]
    with get_visualizer().task(f"search.{action}", label):
        result = await execute_tool(
            tool="search", operation=action,
            safety_tag=SafetyTag.READ,
            func=_search.execute,
            error_prefixes=(),
            action=action, query=query, max_results=max_results,
            region=region, timelimit=timelimit, safesearch=safesearch,
            url=url,
        )
    logger.info("web_search %s %r -> %s", action, (query or url)[:80], result[:100])
    return result


SEARCH_TOOLS = [web_search]

__all__ = ["web_search", "SEARCH_TOOLS"]
