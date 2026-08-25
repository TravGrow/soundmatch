"""Pixabay music search.

Pixabay's public API covers images and video only -- there is no documented
music endpoint, and pixabay.com pages sit behind Cloudflare. The track data we
need is present in the page's React props, so we drive a real browser to read
it. The resulting CDN audio URLs are *not* Cloudflare-protected, so previews can
then be fetched with a plain HTTP client.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from certs import ssl_context

BASE = "https://pixabay.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Collect every track object React rendered, without relying on hashed CSS
# class names (which Pixabay rotates on each deploy).
_HARVEST_JS = """
() => {
  const seen = new Map();
  const isTrack = (t) => t && typeof t === 'object' && t.sources &&
                          typeof t.sources.src === 'string' &&
                          t.sources.src.includes('.mp3');

  // Prefer the React container (one per app) and walk it once. Collecting a
  // fiber from every DOM node instead would re-traverse the same tree hundreds
  // of times for no extra coverage.
  const roots = [];
  for (const el of document.querySelectorAll('*')) {
    for (const k in el) {
      if (k.startsWith('__reactContainer$')) { roots.push(el[k]); break; }
    }
    if (roots.length) break;
  }
  if (!roots.length) {
    for (const el of document.querySelectorAll('*')) {
      for (const k in el) {
        if (k.startsWith('__reactFiber$')) { roots.push(el[k]); break; }
      }
      if (roots.length >= 40) break;
    }
  }

  const visit = (fiber, depth) => {
    let n = 0;
    while (fiber && n++ < 20000) {
      const p = fiber.memoizedProps;
      if (p && isTrack(p.track) && !seen.has(p.track.id)) seen.set(p.track.id, p.track);
      if (fiber.child) visit(fiber.child, depth + 1);
      fiber = fiber.sibling;
      if (depth === 0) break;
    }
  };
  roots.forEach(r => { try { visit(r, 1); } catch (e) {} });

  return [...seen.values()].map(t => ({
    id: t.id,
    name: t.name,
    href: t.href,
    src: t.sources.src,
    thumbnail: t.sources.thumbnailUrl || null,
    downloadUrl: t.sources.downloadUrl || null,
    filename: t.sources.filename || null,
    duration: t.duration,
    tags: (t.tagList || []).map(x => x[0]),
    likes: t.likeCount,
    isAiGenerated: !!t.isAiGenerated,
    attribution: t.attributionHtml || null,
    description: t.description || null,
  }));
}
"""


def search_url(query: str, *, kind: str = "q", page: int = 1) -> str:
    """Build a Pixabay music search URL.

    kind: "q" for free text, "genre" or "mood" for their faceted browse pages.
    """
    if kind == "q":
        path = f"/music/search/{quote(query.lower())}/"
    else:
        path = f"/music/search/{kind}/{quote(query.title())}/"
    return f"{BASE}{path}" + (f"?pagi={page}" if page > 1 else "")


class PixabayMusic:
    """A pooled headless browser for scraping music search results."""

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled", "--mute-audio"],
        )
        self._ctx = await self._browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 2200},
            locale="en-US",
        )
        # Block heavy assets we never look at; keeps scraping fast.
        await self._ctx.route(
            re.compile(r"\.(png|jpe?g|gif|webp|svg|woff2?|mp3)($|\?)"),
            lambda route: route.abort(),
        )
        return self

    async def __aexit__(self, *exc):
        for closer in (self._ctx, self._browser):
            if closer:
                await closer.close()
        if self._pw:
            await self._pw.stop()

    async def search(self, query: str, *, kind: str = "q", pages: int = 1) -> list[dict]:
        """Return track dicts for a query, across ``pages`` result pages."""
        results: dict[int, dict] = {}
        async with self._lock:
            page = await self._ctx.new_page()
            try:
                for p in range(1, pages + 1):
                    url = search_url(query, kind=kind, page=p)
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        break
                    # Results hydrate client-side; wait for props to appear.
                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('a[href^=\"/music/\"]').length > 5",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    await page.wait_for_timeout(1200)
                    # Scroll so lazily-mounted rows render their props too.
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(900)

                    batch = await page.evaluate(_HARVEST_JS)
                    for i, t in enumerate(batch):
                        t["query"] = query
                        # Pixabay's own relevance order is a useful prior when we
                        # have to choose which candidates to spend analysis on.
                        t["rank"] = (p - 1) * 20 + i
                        results.setdefault(t["id"], t)
                    if not batch:
                        break
            finally:
                await page.close()
        return list(results.values())

    async def search_many(self, queries: list[tuple[str, str]], *, pages: int = 1,
                          progress=None) -> list[dict]:
        """Run several (query, kind) searches and merge, keeping first-seen order."""
        merged: dict[int, dict] = {}
        for i, (q, kind) in enumerate(queries, 1):
            if progress:
                progress(f"Searching Pixabay for \u201c{q}\u201d ({i}/{len(queries)})")
            for t in await self.search(q, kind=kind, pages=pages):
                if t["id"] in merged:
                    prev = merged[t["id"]]
                    prev.setdefault("also_matched", []).append(q)
                    prev["rank"] = min(prev["rank"], t["rank"])
                else:
                    merged[t["id"]] = t
        return list(merged.values())


async def download_preview(track: dict, cache_dir: str | Path,
                           client: httpx.AsyncClient | None = None) -> str | None:
    """Fetch a track's mp3 from the CDN into the cache, returning its path."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.blake2b(track["src"].encode(), digest_size=10).hexdigest()
    dest = cache_dir / f"px_{track['id']}_{key}.mp3"
    if dest.exists() and dest.stat().st_size > 10_000:
        return str(dest)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True,
                                         verify=ssl_context())
    try:
        r = await client.get(track["src"], headers={"User-Agent": UA})
        if r.status_code != 200 or len(r.content) < 10_000:
            return None
        dest.write_bytes(r.content)
        return str(dest)
    except Exception:
        return None
    finally:
        if owns_client:
            await client.aclose()
