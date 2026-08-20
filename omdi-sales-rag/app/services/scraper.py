from __future__ import annotations

import asyncio
import json
import re
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.services.chunking import normalize_text
from app.services.security import validate_public_url


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
BINARY_EXTENSIONS = {
    ".7z",
    ".avi",
    ".css",
    ".dmg",
    ".doc",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rar",
    ".svg",
    ".tar",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


@dataclass(slots=True)
class ScrapedPage:
    url: str
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CrawlOutput:
    pages: list[ScrapedPage]
    discovered: int
    skipped: int
    failed: list[str]


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in TRACKING_PARAMETERS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def _is_crawlable(url: str, host: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != host:
        return False
    path = parsed.path.casefold()
    return not any(path.endswith(extension) for extension in BINARY_EXTENSIONS)


def _extract_page(html: str, url: str) -> tuple[ScrapedPage, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else url
    description = ""
    description_node = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if description_node:
        description = normalize_text(str(description_node.get("content", "")))

    structured_data: list[str] = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            value = json.loads(node.string or "")
            structured_data.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (json.JSONDecodeError, TypeError):
            continue

    links = [urljoin(url, str(anchor.get("href", ""))) for anchor in soup.find_all("a")]

    for node in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        node.decompose()
    content_node = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    body = normalize_text(content_node.get_text("\n", strip=True))
    parts = [f"Title: {title}"]
    if description:
        parts.append(f"Description: {description}")
    if body:
        parts.append(body)
    if structured_data:
        parts.append("Structured product data:\n" + "\n".join(structured_data))
    page = ScrapedPage(
        url=url,
        title=title,
        text="\n\n".join(parts),
        metadata={"description": description, "structured_data": bool(structured_data)},
    )
    return page, links


class WebsiteScraper:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def _check_url(self, url: str) -> None:
        await asyncio.to_thread(
            validate_public_url,
            url,
            allow_private_networks=self.settings.scraper_allow_private_networks,
        )

    async def _read_limited(self, client: httpx.AsyncClient, url: str) -> tuple[str, str]:
        limit = self.settings.scraper_max_response_mb * 1024 * 1024
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            final_url = canonicalize_url(str(response.url))
            await self._check_url(final_url)
            content_type = response.headers.get("content-type", "").casefold()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                raise ValueError(f"Unsupported website content type: {content_type or 'unknown'}")
            chunks = bytearray()
            async for block in response.aiter_bytes():
                chunks.extend(block)
                if len(chunks) > limit:
                    raise ValueError("Website response exceeded the configured size limit.")
            encoding = response.encoding or "utf-8"
            return chunks.decode(encoding, errors="replace"), final_url

    async def crawl(self, start_url: str, max_pages: int, max_depth: int) -> CrawlOutput:
        start_url = canonicalize_url(start_url)
        await self._check_url(start_url)
        start = urlsplit(start_url)
        host = start.hostname or ""

        timeout = httpx.Timeout(self.settings.scraper_timeout_seconds)
        headers = {
            "User-Agent": self.settings.scraper_user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }
        pages: list[ScrapedPage] = []
        failed: list[str] = []
        skipped = 0
        visited: set[str] = set()
        queued: set[str] = {start_url}
        queue: deque[tuple[str, int]] = deque([(start_url, 0)])
        robots = urllib.robotparser.RobotFileParser()
        robots.set_url(urljoin(start_url, "/robots.txt"))

        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            try:
                robots_response = await client.get(robots.url)
                await self._check_url(canonicalize_url(str(robots_response.url)))
                if robots_response.status_code < 400:
                    robots.parse(robots_response.text.splitlines())
                else:
                    robots.parse([])
            except httpx.HTTPError:
                robots.parse([])

            while queue and len(pages) < max_pages:
                url, depth = queue.popleft()
                queued.discard(url)
                if url in visited:
                    continue
                visited.add(url)
                if not robots.can_fetch(self.settings.scraper_user_agent, url):
                    skipped += 1
                    continue
                try:
                    html, final_url = await self._read_limited(client, url)
                    if urlsplit(final_url).hostname != host:
                        raise ValueError("A cross-domain redirect was blocked.")
                    page, links = _extract_page(html, final_url)
                    if len(page.text) >= 80:
                        pages.append(page)
                    else:
                        skipped += 1
                    if depth < max_depth:
                        for link in links:
                            clean = canonicalize_url(link)
                            if (
                                _is_crawlable(clean, host)
                                and clean not in visited
                                and clean not in queued
                            ):
                                queue.append((clean, depth + 1))
                                queued.add(clean)
                except (httpx.HTTPError, ValueError) as exc:
                    failed.append(f"{url}: {exc}")

        return CrawlOutput(
            pages=pages,
            discovered=len(visited) + len(queue),
            skipped=skipped,
            failed=failed,
        )
