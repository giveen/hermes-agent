"""Crawl4AI web extraction provider — local, JS-rendering, zero API keys.

Uses ``crawl4ai.AsyncWebCrawler`` (backed by Playwright) to extract clean
markdown/HTML from web pages. Runs entirely locally — no external service
dependencies, no API keys required.

Crawl4AI handles:
  - JavaScript-rendered content (SPAs, dynamic pages)
  - Infinite scroll / lazy-loaded content
  - Shadow DOM, iframes
  - Consent popups and overlays
  - Structured extraction (CSS, LLM, regex strategies)

This provider exposes ``supports_extract=True`` only — web search still
uses the legacy search backends (Firecrawl, Tavily, Exa, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Lazy import guard — crawl4ai is a heavy dep pulled by pyproject.toml but
# we still defer the import to avoid paying cold-start cost at tool-discovery
# time. Imported once and cached on first successful use.
_CRAWL4AI_AVAILABLE: Optional[bool] = None


def _check_crawl4ai_importable() -> bool:
    """Cheap import probe — returns True when crawl4ai is installed."""
    global _CRAWL4AI_AVAILABLE
    if _CRAWL4AI_AVAILABLE is not None:
        return _CRAWL4AI_AVAILABLE
    try:
        import crawl4ai  # noqa: F401
        _CRAWL4AI_AVAILABLE = True
    except ImportError:
        _CRAWL4AI_AVAILABLE = False
    return _CRAWL4AI_AVAILABLE


class Crawl4AIWebProvider(WebSearchProvider):
    """Web extraction provider backed by crawl4ai's AsyncWebCrawler.

    Extracts clean markdown or HTML from URLs using a local headless
    Chromium browser. No API keys, no external services.
    """

    @property
    def name(self) -> str:
        return "crawl4ai"

    @property
    def display_name(self) -> str:
        return "Crawl4AI (local)"

    def is_available(self) -> bool:
        """Available when the ``crawl4ai`` package is importable.

        Crawl4AI is a core dependency (installed with Hermes), so this
        returns True on any normal installation. Playwright browsers must
        also be installed (via ``playwright install chromium``) for actual
        extraction to work — we detect a runtime import failure during
        :meth:`extract` and surface a clear error message.
        """
        return _check_crawl4ai_importable()

    def supports_search(self) -> bool:
        """Crawl4AI is an extract/crawl engine, not a search API."""
        return False

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Crawl4AI (local)",
            "badge": "local",
            "tag": (
                "Zero API keys. Installed with Hermes — works out of the box. "
                "Requires ``playwright install chromium`` for first use."
            ),
            "env_vars": [],
        }

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from one or more URLs via Crawl4AI.

        Async; each URL is crawled using a shared ``AsyncWebCrawler``
        instance. The browser is started once per call and torn down after
        all URLs are processed.

        Accepted kwargs (ignored for forward compat):
          - ``format``: ``"markdown"`` (default) or ``"html"``.

        Returns the standard per-URL list-of-results shape::

            [
                {
                    "url": str,
                    "title": str,
                    "content": str,       # cleaned page text (markdown or html)
                    "raw_content": str,   # full page content (same as content)
                    "metadata": dict,     # crawl4ai metadata
                },
                ...
            ]

        Per-URL failures (timeout, browser error, blocked page) become
        items with an ``error`` field rather than raising.
        """
        from tools.interrupt import is_interrupted as _is_interrupted
        from tools.website_policy import check_website_access

        if _is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]

        format = kwargs.get("format", "markdown")

        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        except ImportError as exc:
            logger.error("crawl4ai import failed: %s", exc)
            return [
                {
                    "url": u,
                    "error": (
                        "Crawl4AI is not installed. Run ``pip install crawl4ai`` "
                        "and ``playwright install chromium``."
                    ),
                    "title": "",
                }
                for u in urls
            ]

        results: List[Dict[str, Any]] = []

        # Configure browser — headless, lightweight defaults
        browser_cfg = BrowserConfig(
            headless=True,
            browser_type="chromium",
            verbose=False,
            text_mode=True,
            light_mode=True,
            ignore_https_errors=True,
        )

        # Per-page crawl config
        run_cfg = CrawlerRunConfig(
            word_count_threshold=50,
            remove_consent_popups=True,
            remove_overlay_elements=True,
            scan_full_page=False,          # single page, not infinite scroll
            process_iframes=True,
            exclude_social_media_domains=True,
            page_timeout=30_000,           # 30s per page
            delay_before_return_html=0.2,  # let JS settle
        )

        try:
            async with AsyncWebCrawler(config=browser_cfg) as crawler:
                for url in urls:
                    if _is_interrupted():
                        results.append(
                            {"url": url, "error": "Interrupted", "title": ""}
                        )
                        continue

                    # Pre-scrape website policy gate
                    blocked = check_website_access(url)
                    if blocked:
                        logger.info(
                            "Blocked web_extract for %s by rule %s",
                            blocked["host"],
                            blocked["rule"],
                        )
                        results.append(
                            {
                                "url": url,
                                "title": "",
                                "content": "",
                                "error": blocked["message"],
                                "blocked_by_policy": {
                                    "host": blocked["host"],
                                    "rule": blocked["rule"],
                                    "source": blocked["source"],
                                },
                            }
                        )
                        continue

                    try:
                        logger.info("Crawl4AI extracting: %s", url)

                        # Fetch page
                        crawl_result = await asyncio.wait_for(
                            crawler.arun(url, config=run_cfg),
                            timeout=35,  # slightly more than page_timeout
                        )

                        if not crawl_result.success:
                            error_msg = (
                                crawl_result.error_message
                                or "Crawl4AI returned success=False (no error details)"
                            )
                            logger.warning(
                                "Crawl4AI failed for %s: %s", url, error_msg
                            )
                            results.append(
                                {
                                    "url": url,
                                    "title": "",
                                    "content": "",
                                    "error": error_msg,
                                }
                            )
                            continue

                        # Extract content in requested format
                        if format == "html":
                            content = crawl_result.cleaned_html or ""
                        else:
                            # markdown (default) — use fit_markdown when
                            # available (compact), fall back to raw_markdown
                            md = crawl_result.markdown
                            content = ""
                            if md:
                                content = (
                                    md.fit_markdown
                                    or md.raw_markdown
                                    or ""
                                )
                            if not content:
                                content = crawl_result.cleaned_html or ""

                        title = (
                            (crawl_result.metadata or {}).get("title")
                            or ""
                        )

                        results.append(
                            {
                                "url": url,
                                "title": title,
                                "content": content,
                                "raw_content": content,
                                "metadata": {
                                    "provider": "crawl4ai",
                                    **(
                                        crawl_result.metadata or {}
                                    ),
                                },
                            }
                        )

                    except asyncio.TimeoutError:
                        logger.warning("Crawl4AI timed out for %s", url)
                        results.append(
                            {
                                "url": url,
                                "title": "",
                                "content": "",
                                "error": "Timeout: page did not load within 30s",
                            }
                        )
                        continue
                    except Exception as exc:
                        logger.warning(
                            "Crawl4AI error for %s: %s", url, exc
                        )
                        results.append(
                            {
                                "url": url,
                                "title": "",
                                "content": "",
                                "error": f"Crawl4AI error: {exc}",
                            }
                        )
                        continue

        except Exception as exc:
            logger.error("Crawl4AI browser init failed: %s", exc)
            return [
                {
                    "url": u,
                    "error": (
                        f"Crawl4AI browser failed to start: {exc}"
                    ),
                    "title": "",
                }
                for u in urls
            ]

        return results
