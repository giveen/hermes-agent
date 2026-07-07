"""Crawl4AI web extraction plugin — bundled, auto-loaded.

Backed by ``crawl4ai.AsyncWebCrawler`` with a local headless Chromium
browser. No API keys, no external services — runs entirely on the
user's machine. Extracts clean markdown or HTML from any URL,
including JS-rendered pages.
"""

from __future__ import annotations

from plugins.web.crawl4ai.provider import Crawl4AIWebProvider


def register(ctx) -> None:
    """Register the Crawl4AI provider with the plugin context."""
    ctx.register_web_search_provider(Crawl4AIWebProvider())
