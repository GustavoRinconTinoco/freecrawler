#!/usr/bin/env python3
"""
Freecrawler 🔥 — 100% local, free, unlimited web scraper. Alternative to Firecrawl.
No API keys, no credits, no limits.

Modes:
  search "query"           Web search (DuckDuckGo API, no key needed)
  scrape URL               Extract content from a URL
  crawl URL                Crawl multiple pages from the same site
  map URL                  Discover all internal URLs
  extract URL --schema ... Structured CSS extraction
  xsearch "query"          Search X/Twitter
  xuser @username          Get tweets from an X user
  linkedin URL             Get LinkedIn profile data

Common flags:
  --format text|json|markdown   Output format
  --browser                     Use Playwright for JS rendering
  --depth N                     Crawl/map depth (default: 1)
  --limit N                     Max results (default: 10)
  --delay N                     Seconds to wait between requests when crawling (default: 0.5)
  --output FILE                 Save to file
  --quiet                       Content only, no metadata

Schema format for --schema (extract / scrape --format json):
  "baseSelector: name=selector, name2=selector2"
  Selectors may contain spaces (descendant selectors like "h3 a" work correctly).
  Example:
    python freecrawler.py extract https://books.toscrape.com \\
      --schema "article.product_pod: title=h3 a, price=.price_color"

Examples:
  python freecrawler.py search "artificial intelligence" --limit 5
  python freecrawler.py scrape https://example.com
  python freecrawler.py scrape https://example.com --browser
  python freecrawler.py scrape https://example.com --format json
  python freecrawler.py crawl https://example.com --depth 2 --limit 20
  python freecrawler.py xsearch "politics France" --limit 5
  python freecrawler.py xuser @username --limit 10
"""

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlparse

# ── Optional engines ──────────────────────────────────────
HAS_BS4 = False
HAS_TRAFILATURA = False
HAS_PLAYWRIGHT = False
HAS_CRAWL4AI = False
HAS_TWSCRAPE = False
HAS_LINKEDIN = False
HAS_REQUESTS = False
HAS_HTML2TEXT = False
HAS_DDGS = False
HAS_WEBSOCKET = False

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    pass

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    pass

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    pass

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    pass

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    pass

try:
    from crawl4ai import AsyncWebCrawler
    from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
    HAS_CRAWL4AI = True
except ImportError:
    pass

try:
    import twscrape
    HAS_TWSCRAPE = True
except ImportError:
    pass

try:
    from linkedin_scraper import Person, actions as li_actions
    HAS_LINKEDIN = True
except ImportError:
    pass

try:
    import html2text
    HAS_HTML2TEXT = True
except ImportError:
    pass

try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
TIMEOUT = 30  # single source of truth for network timeouts (seconds)

# Concurrency caps for parallel crawl/map (keeps us polite + avoids exhausting sockets)
CRAWL_CONCURRENCY = 8
MAP_CONCURRENCY = 8

# Extensions we should never try to parse as HTML
BINARY_EXTENSIONS = (
    ".pdf", ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".dmg",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
)

# ═══════════════════════════════════════════════════════════
# BASE UTILITIES
# ═══════════════════════════════════════════════════════════

def _describe_fetch_error(e):
    """Turn a raw exception into a short, useful error string."""
    if HAS_REQUESTS:
        if isinstance(e, requests.exceptions.Timeout):
            return f"Timeout after {TIMEOUT}s"
        if isinstance(e, requests.exceptions.ConnectionError):
            return f"Connection error: {e}"
        if isinstance(e, requests.exceptions.RequestException):
            return f"Request error: {e}"
    if isinstance(e, urllib.error.URLError):
        return f"URL error: {getattr(e, 'reason', e)}"
    return str(e)


def _fetch(url, timeout=TIMEOUT, session=None):
    """HTTP GET with User-Agent and cookie handling.

    Returns (html, final_url, status_code) with a real HTTP status code,
    even on HTTP error responses (4xx/5xx) or urllib HTTPError.
    """
    if HAS_REQUESTS:
        s = session or requests.Session()
        s.headers.update({"User-Agent": UA})
        resp = s.get(url, timeout=timeout)
        return resp.text, resp.url, resp.status_code

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return body, e.url or url, e.code
    html = resp.read().decode("utf-8", errors="replace")
    return html, resp.geturl(), resp.getcode()


async def _fetch_async(url, timeout=TIMEOUT, session=None):
    """Non-blocking wrapper around _fetch() using the default thread executor,
    so multiple pages can be fetched concurrently without adding dependencies."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch, url, timeout, session)


def _is_probably_binary(url):
    """Skip known binary/non-HTML extensions before fetching/parsing."""
    path = urlparse(url).path.lower()
    return path.endswith(BINARY_EXTENSIONS)


def _extract_text(html):
    """Extract clean text: trafilatura > bs4 > regex."""
    if HAS_TRAFILATURA:
        result = trafilatura.extract(html, output_format="markdown", include_links=True,
                                      include_tables=True, favor_recall=True)
        if result:
            return result.strip()
    if HAS_BS4:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:10000]
    return html[:5000]


def _extract_title(html):
    if HAS_BS4:
        soup = BeautifulSoup(html, "lxml")
        t = soup.find("title")
        if t:
            return t.get_text(strip=True)
    m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
    return m.group(1).strip() if m else ""


def _same_domain(base, url):
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


def _normalize_url(base, href):
    if not href or href.startswith("#") or href.startswith("javascript:"):
        return None
    href = href.split("#")[0]
    full = urljoin(base, href)
    if not full.startswith(("http://", "https://")):
        return None
    return full


def _discover_urls_from_html(base_url, html):
    """Parse links out of HTML we already fetched, instead of re-fetching."""
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "lxml")
    found = set()
    for a in soup.find_all("a", href=True):
        normalized = _normalize_url(base_url, a["href"])
        if normalized and _same_domain(base_url, normalized) and not _is_probably_binary(normalized):
            found.add(normalized)
    return sorted(found)


def _parse_schema(schema_str):
    """
    Convert schema string to a normalized dict:
      {"baseSelector": "...", "fields": [{"name": ..., "selector": ..., "type": "text"}, ...]}

    Grammar (single source of truth, used by every engine):
      "baseSelector: name=selector, name2=selector2"
    Fields are comma-separated so selectors may contain spaces
    (descendant selectors like "h3 a" are supported).

    A raw JSON schema (crawl4ai's native format) is also accepted as-is.
    """
    if not schema_str:
        return None

    try:
        schema = json.loads(schema_str)
        if isinstance(schema, dict) and "baseSelector" in schema:
            return schema
    except json.JSONDecodeError:
        pass

    base_selector = "body"
    field_part = schema_str
    if ":" in schema_str:
        parts = schema_str.split(":", 1)
        base_selector = parts[0].strip()
        field_part = parts[1].strip()

    fields = []

    if "," in field_part:
        # CSV format: name=selector, name=selector
        for chunk in field_part.split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            name, selector = chunk.split("=", 1)
            name, selector = name.strip(), selector.strip()
            if name and selector:
                fields.append({"name": name, "selector": selector, "type": "text"})
    else:
        # Legacy whitespace format: selector=name selector=name
        for part in field_part.split():
            if "=" not in part:
                continue
            selector, name = part.split("=", 1)
            if selector and name:
                fields.append({"name": name.strip(), "selector": selector.strip(), "type": "text"})

    if not fields:
        return None
    return {"name": "extracted_data", "baseSelector": base_selector, "fields": fields}


def _fallback_extract(soup, fmt="text"):
    """Extraction without trafilatura."""
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    for unwanted in body.select(
        ".sidebar, .menu, .nav, .footer, .header, .ad, .advertisement, "
        ".social-share, .comments, .related-posts"
    ):
        unwanted.decompose()

    if fmt == "markdown" and HAS_HTML2TEXT:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        return h.handle(str(body))

    text = body.get_text(separator="\n", strip=True)
    return "\n".join(l for l in text.split("\n") if l.strip())


def _scrape_http_full(url, fmt="text", session=None):
    """HTTP direct engine (fast, no JS).

    Returns (result_dict, html_or_None). The raw HTML is returned as a
    separate value instead of being stashed inside the result dict, so
    callers never need to strip an internal key before emitting output.
    """
    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "http"}
    try:
        html, final_url, status_code = _fetch(url, session=session)
    except Exception as e:
        result["error"] = _describe_fetch_error(e)
        result["time_ms"] = int((time.time() - t0) * 1000)
        return result, None

    result["final_url"] = final_url
    result["status_code"] = status_code
    result["time_ms"] = int((time.time() - t0) * 1000)
    result["title"] = _extract_title(html)

    if status_code >= 400:
        result["error"] = f"HTTP {status_code}"

    if fmt == "json":
        content = _extract_text(html)
        if not content and HAS_BS4:
            content = _fallback_extract(BeautifulSoup(html, "lxml"), fmt="text")
        result["content"] = content.strip() if content else "[No content]"
    elif fmt == "markdown":
        content = None
        if HAS_TRAFILATURA:
            opts = {"include_formatting": True, "include_links": True,
                    "include_images": False, "output_format": "markdown"}
            content = trafilatura.extract(html, **opts)
        if not content and HAS_BS4:
            content = _fallback_extract(BeautifulSoup(html, "lxml"), fmt="markdown")
        result["content"] = (content or _extract_text(html)).strip()
    else:
        content = _extract_text(html)
        result["content"] = (content or "[No content]").strip()

    result["content_length"] = len(result["content"])
    return result, html


def _scrape_http(url, fmt="text", session=None):
    result, _html = _scrape_http_full(url, fmt=fmt, session=session)
    return result


def _scrape_playwright_full(url, fmt="text"):
    """Playwright engine (JS rendering). Returns (result_dict, html_or_None)."""
    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "playwright"}
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=TIMEOUT * 1000)
            try:
                accept_btn = page.query_selector(
                    'button:has-text("Accept"), button:has-text("I Accept"), '
                    'button:has-text("Aceptar"), button:has-text("Accept all"), '
                    'button:has-text("Accept cookies")'
                )
                if accept_btn:
                    accept_btn.click()
                    page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            time.sleep(2)
            html = page.content()
            title = page.title()
            final_url = page.url
            browser.close()
            browser = None
    except Exception as e:
        result["error"] = f"Playwright error: {e}"
        result["time_ms"] = int((time.time() - t0) * 1000)
        return result, None
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass

    result["final_url"] = final_url
    result["title"] = title
    result["time_ms"] = int((time.time() - t0) * 1000)
    content = _extract_text(html)
    result["content"] = (content[:15000] if content else "[No content]").strip()
    result["content_length"] = len(result["content"])
    return result, html


def _scrape_playwright(url, fmt="text"):
    result, _html = _scrape_playwright_full(url, fmt=fmt)
    return result


async def _scrape_http_full_async(url, fmt="text", session=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _scrape_http_full, url, fmt, session)


async def _scrape_playwright_full_async(url, fmt="text"):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _scrape_playwright_full, url, fmt)


async def _scrape_crawl4ai(url, fmt="markdown", schema=None):
    """crawl4ai engine."""
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode

    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "crawl4ai"}
    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, word_count_threshold=10,
        extraction_strategy=None, verbose=False,
    )
    if fmt == "json" and schema:
        config.extraction_strategy = JsonCssExtractionStrategy(schema)

    try:
        async with AsyncWebCrawler() as crawler:
            crawl_result = await asyncio.wait_for(crawler.arun(url, config=config), timeout=TIMEOUT * 2)
    except Exception as e:
        result["error"] = str(e)
        result["time_ms"] = int((time.time() - t0) * 1000)
        return result

    result["time_ms"] = int((time.time() - t0) * 1000)
    result["title"] = crawl_result.metadata.get("title", "") if crawl_result.metadata else ""
    result["final_url"] = url

    if fmt == "json" and schema:
        try:
            result["data"] = json.loads(crawl_result.extracted_content) if crawl_result.extracted_content else {}
        except (json.JSONDecodeError, TypeError):
            result["data"] = crawl_result.extracted_content or {}
        result["content"] = json.dumps(result["data"], ensure_ascii=False)
    else:
        result["content"] = crawl_result.markdown or crawl_result.fit_markdown or "[No content]"

    result["content_length"] = len(result["content"])
    return result


# ═══════════════════════════════════════════════════════════
# MAIN MODES
# ═══════════════════════════════════════════════════════════

_SEARCH_CACHE = {}
_SEARCH_CACHE_MAX = 128


def search(query, limit=5):
    """Search the web using DuckDuckGo API (no key needed, no blocking).

    Repeated identical (query, limit) calls are served from a small
    in-memory cache instead of hitting the network again.
    """
    cache_key = (query, limit)
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not HAS_DDGS:
        return [{"error": "DuckDuckGo Search library not installed. pip install duckduckgo_search"}]

    try:
        with DDGS() as ddgs:
            results = []
            for r in ddgs.text(query, max_results=limit):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "description": r.get("body", ""),
                })
    except Exception as e:
        return [{"error": f"DDGS search failed: {e}"}]

    results = results if results else [{"error": "No results found"}]

    if not any("error" in r for r in results):
        if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.pop(next(iter(_SEARCH_CACHE)))
        _SEARCH_CACHE[cache_key] = results

    return results


def scrape(url, fmt="text", browser=False, schema_str=None, session=None):
    """
    Extract content from a URL.
    Order: crawl4ai (if JSON+schema) > Playwright (if --browser) > HTTP direct.
    """
    if fmt == "json" and schema_str and HAS_CRAWL4AI:
        schema = _parse_schema(schema_str)
        if schema:
            return asyncio.run(_scrape_crawl4ai(url, fmt=fmt, schema=schema))

    if browser:
        if HAS_PLAYWRIGHT:
            result = _scrape_playwright(url, fmt=fmt)
            return result if not result.get("error") else _scrape_http(url, fmt=fmt, session=session)
        else:
            return {"error": "Browser mode requires Playwright. pip install playwright && python -m playwright install chromium"}

    return _scrape_http(url, fmt=fmt, session=session)


async def _crawl_async(url, fmt="text", depth=1, limit=10, quiet=False, browser=False, delay=0.5):
    session = requests.Session() if HAS_REQUESTS else None
    visited = set()
    results = []
    current_level = [url]
    current_depth = 0
    sem = asyncio.Semaphore(min(CRAWL_CONCURRENCY, max(1, limit)))

    async def fetch_one(u):
        async with sem:
            res, html = (None, None)
            if browser:
                if HAS_PLAYWRIGHT:
                    res, html = await _scrape_playwright_full_async(u, fmt=fmt)
                    if res.get("error"):
                        res, html = await _scrape_http_full_async(u, fmt=fmt, session=session)
                else:
                    res, html = ({"url": u, "error": "Browser mode requires Playwright."}, None)
            else:
                res, html = await _scrape_http_full_async(u, fmt=fmt, session=session)
            return u, res, html

    while current_level and len(results) < limit:
        candidates = list(dict.fromkeys(
            u for u in current_level if u not in visited and not _is_probably_binary(u)
        ))
        remaining = limit - len(results)
        batch = candidates[:remaining] if remaining > 0 else []
        if not batch:
            break
        for u in batch:
            visited.add(u)

        if not quiet:
            for u in batch:
                print(f"  → {u}", file=sys.stderr)

        batch_results = await asyncio.gather(*(fetch_one(u) for u in batch), return_exceptions=True)

        next_level = []
        for item in batch_results:
            if isinstance(item, Exception) or item is None:
                continue
            u, res, html = item
            if res.get("error") and not html:
                continue
            results.append(res)
            if current_depth < depth and html:
                for link in _discover_urls_from_html(u, html):
                    if link not in visited:
                        next_level.append(link)
            if len(results) >= limit:
                break

        current_level = next_level
        current_depth += 1
        if current_level and delay:
            await asyncio.sleep(delay)

    return results[:limit]


def crawl(url, fmt="text", depth=1, limit=10, quiet=False, browser=False, delay=0.5):
    """Crawl multiple pages from the same site.

    Pages within the same BFS level are fetched concurrently (bounded by
    CRAWL_CONCURRENCY) instead of one at a time, while still respecting
    `depth`, `limit` and a politeness `delay` between levels.
    """
    return asyncio.run(_crawl_async(url, fmt, depth, limit, quiet, browser, delay))


async def _site_map_async(url, depth=2, limit=50, delay=0.5):
    session = requests.Session() if HAS_REQUESTS else None
    visited = set()
    tree = {url: []}
    current_level = [url]
    current_depth = 0
    sem = asyncio.Semaphore(min(MAP_CONCURRENCY, max(1, limit)))

    async def fetch_links(u):
        async with sem:
            try:
                html, _final_url, _status = await _fetch_async(u, session=session)
            except Exception:
                return u, []
            return u, _discover_urls_from_html(u, html)

    while current_level and len(visited) < limit:
        candidates = list(dict.fromkeys(
            u for u in current_level if u not in visited and not _is_probably_binary(u)
        ))
        remaining = limit - len(visited)
        batch = candidates[:remaining] if remaining > 0 else []
        if not batch:
            break
        for u in batch:
            visited.add(u)

        batch_results = await asyncio.gather(*(fetch_links(u) for u in batch), return_exceptions=True)

        next_level = []
        for item in batch_results:
            if isinstance(item, Exception) or item is None:
                continue
            u, discovered = item
            tree[u] = discovered
            if current_depth < depth:
                for d in discovered:
                    if d not in visited:
                        next_level.append(d)

        current_level = next_level
        current_depth += 1
        if current_level and delay:
            await asyncio.sleep(delay)

    return {"root": url, "total_urls": len(visited), "urls": sorted(visited), "tree": tree}


def site_map(url, depth=2, limit=50, delay=0.5):
    """Build a sitemap of internal URLs (parallelized per BFS level)."""
    return asyncio.run(_site_map_async(url, depth, limit, delay))


def _extract_with_schema(soup, schema):
    """Shared extraction logic used by both the crawl4ai path and the bs4
    fallback, so both honor the exact same schema grammar."""
    base_selector = schema.get("baseSelector", "body")
    fields = schema.get("fields", [])
    items = []
    for el in soup.select(base_selector)[:50]:
        item = {}
        for f in fields:
            sub = el.select_one(f["selector"])
            item[f["name"]] = sub.get_text(strip=True) if sub else ""
        items.append(item)
    return items


def extract(url, schema_str, browser=False):
    """Structured CSS extraction. Uses the same schema grammar regardless of
    which engine is available."""
    schema = _parse_schema(schema_str)
    if not schema:
        return {"error": "Could not parse --schema. Expected format: 'baseSelector: name=selector, name2=selector2'"}

    if HAS_CRAWL4AI:
        return asyncio.run(_scrape_crawl4ai(url, fmt="json", schema=schema))

    try:
        html, _final_url, status_code = _fetch(url)
    except Exception as e:
        return {"error": f"HTTP error: {_describe_fetch_error(e)}"}
    if status_code >= 400:
        return {"error": f"HTTP {status_code}"}
    if not HAS_BS4:
        return {"error": "BeautifulSoup is required for CSS extraction"}

    soup = BeautifulSoup(html, "lxml")
    items = _extract_with_schema(soup, schema)
    return {"data": items} if items else {"error": "No data extracted"}


# ═══════════════════════════════════════════════════════════
# X/TWITTER
# ═══════════════════════════════════════════════════════════

def x_search(query, limit=10):
    if not HAS_TWSCRAPE:
        return [{"error": "twscrape not installed. pip install twscrape"}]
    return asyncio.run(_x_search_async(query, limit))


async def _x_search_async(query, limit=10):
    api = twscrape.API()
    tweets = []
    try:
        async for tweet in api.search(query, limit=limit):
            tweets.append({
                "id": tweet.id, "date": str(tweet.date),
                "user": tweet.user.username if tweet.user else "unknown",
                "fullname": tweet.user.displayname if tweet.user else "",
                "content": tweet.rawContent, "likes": tweet.likeCount,
                "retweets": tweet.retweetCount, "replies": tweet.replyCount,
                "url": tweet.url,
            })
    except Exception as e:
        return [{"error": f"X search failed: {e}"}]
    return tweets if tweets else [{"error": "No tweets found"}]


def x_user(username, limit=20):
    if not HAS_TWSCRAPE:
        return [{"error": "twscrape not installed. pip install twscrape"}]
    return asyncio.run(_x_user_async(username, limit))


async def _x_user_async(username, limit=20):
    """twscrape's user_tweets() expects a numeric user id, not a handle.
    Resolve the handle to an id first via user_by_login()."""
    api = twscrape.API()
    try:
        user = await api.user_by_login(username)
        if user is None:
            return [{"error": f"User not found: {username}"}]

        tweets = []
        async for tweet in api.user_tweets(user.id, limit=limit):
            tweets.append({
                "id": tweet.id, "date": str(tweet.date),
                "content": tweet.rawContent, "likes": tweet.likeCount,
                "retweets": tweet.retweetCount, "replies": tweet.replyCount,
                "url": tweet.url,
            })
    except Exception as e:
        return [{"error": f"X user lookup failed: {e}"}]
    return tweets if tweets else [{"error": "No tweets found"}]


# ═══════════════════════════════════════════════════════════
# LINKEDIN
# ═══════════════════════════════════════════════════════════

def linkedin_profile(url, email=None, password=None):
    """linkedin-scraper's Person does not accept email/password kwargs.
    It needs an authenticated Selenium driver. This also requires `selenium`
    and a Chromedriver to be installed and on PATH.

    NOTE: scraping LinkedIn with automated credentials is against LinkedIn's
    Terms of Service and can get the account suspended. Use at your own risk.
    """
    if not HAS_LINKEDIN:
        return {"error": "linkedin-scraper not installed. pip install linkedin-scraper selenium"}

    email = email or os.environ.get("LINKEDIN_EMAIL", "")
    password = password or os.environ.get("LINKEDIN_PASS", "")
    if not email or not password:
        return {"error": "LINKEDIN_EMAIL and LINKEDIN_PASS must be set"}

    try:
        from selenium import webdriver
    except ImportError:
        return {"error": "selenium not installed. pip install selenium (and a matching chromedriver)"}

    driver = None
    try:
        driver = webdriver.Chrome()
        li_actions.login(driver, email, password)
        person = Person(url, driver=driver, close_on_complete=False)
        return {
            "name": person.name, "about": person.about, "headline": person.headline,
            "location": person.location, "company": person.company, "job_title": person.job_title,
            "experiences": [
                {"title": e.position, "company": e.company, "duration": f"{e.from_date} - {e.to_date}"}
                for e in (person.experiences or [])
            ],
            "education": [
                {"school": e.institution, "degree": e.degree}
                for e in (person.educations or [])
            ],
        }
    except Exception as e:
        return {"error": f"LinkedIn scraping failed: {e}"}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════
# CDP BROWSER — connect to Chrome DevTools on localhost:9222
# ═══════════════════════════════════════════════════════════

class CDPBrowser:
    """Control Chrome via DevTools Protocol (CDP) on ws://localhost:9222."""

    def __init__(self, cdp_port=9222):
        self.cdp_http = f"http://localhost:{cdp_port}"
        self.ws = None
        self.target_id = None
        self._msg_id = 0
        self._responses = {}
        self._lock = threading.Lock()
        self._events = []
        self._listener_alive = False
        self._targets_cache = []

    # ── Discovery ──────────────────────────────────────────

    def _get_json(self, path):
        resp = urllib.request.urlopen(f"{self.cdp_http}{path}", timeout=5)
        return json.loads(resp.read())

    def list_targets(self):
        self._targets_cache = self._get_json("/json")
        return self._targets_cache

    def _new_tab(self, url="about:blank"):
        """Create new tab via Target.createTarget CDP command."""
        # Get browser WebSocket URL from version endpoint
        ver = self._get_json("/json/version")
        browser_ws = ver.get("webSocketDebuggerUrl")
        if browser_ws:
            try:
                temp_ws = websocket.create_connection(browser_ws, timeout=5,
                                                      suppress_origin=True)
                cmd = json.dumps({"id": 1, "method": "Target.createTarget",
                                  "params": {"url": url}})
                temp_ws.send(cmd)
                resp = json.loads(temp_ws.recv())
                temp_ws.close()
                if "result" in resp:
                    return {"id": resp["result"]["targetId"],
                            "webSocketDebuggerUrl": f"{self.cdp_http}/devtools/page/{resp['result']['targetId']}"}
            except Exception:
                pass
        # Fallback: HTTP /json/new endpoint
        params = json.dumps({"url": url}).encode()
        req = urllib.request.Request(
            f"{self.cdp_http}/json/new", data=params,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())

    # ── Connection ────────────────────────────────────────

    def connect(self, target_id=None):
        """Open WebSocket to a tab. Creates a new one if none given."""
        targets = self.list_targets()
        ws_url = None
        if target_id:
            for t in targets:
                if t["id"] == target_id:
                    ws_url = t["webSocketDebuggerUrl"]
                    break
        if not ws_url:
            for t in targets:
                ws_url = t["webSocketDebuggerUrl"]
                target_id = t["id"]
                break
        if not ws_url:
            tab = self._new_tab()
            ws_url = tab["webSocketDebuggerUrl"]
            target_id = tab["id"]

        self.target_id = target_id
        self.ws = websocket.create_connection(ws_url, timeout=10,
                                              suppress_origin=True)
        self._listener_alive = True
        t = threading.Thread(target=self._listener, daemon=True)
        t.start()
        # Enable domains
        time.sleep(0.1)  # brief settling for listener thread
        self.call("Page.enable", timeout=5)
        self.call("Runtime.enable", timeout=5)
        return target_id

    def _listener(self):
        while self._listener_alive:
            try:
                msg = json.loads(self.ws.recv())
                mid = msg.get("id")
                if mid is not None:
                    with self._lock:
                        self._responses[mid] = msg
                elif msg.get("method"):
                    self._events.append(msg)
            except Exception:
                self._listener_alive = False
                break

    # ── CDP Commands ──────────────────────────────────────

    def send(self, method, params=None):
        """Send a CDP command, return the request id (thread-safe)."""
        if params is None:
            params = {}
        with self._lock:
            self._msg_id += 1
            mid = self._msg_id
        try:
            self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        except websocket.WebSocketConnectionClosedException:
            self._listener_alive = False
            return None
        return mid

    def call(self, method, params=None, timeout=30):
        """Send a command and wait for the response."""
        mid = self.send(method, params)
        if mid is None:
            return {"error": {"message": "WebSocket closed on send", "code": -32001,
                              "data": "Cannot send command"}}
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._listener_alive and self.ws:
                return {"error": {"message": "WebSocket closed", "code": -32001,
                                  "data": "Listener thread died"}}
            with self._lock:
                if mid in self._responses:
                    return self._responses.pop(mid)
            time.sleep(0.05)
        return {"error": {"message": f"Timeout after {timeout}s", "code": -32000}}

    def eval(self, expression):
        """Evaluate JavaScript in the page context."""
        return self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })

    def navigate(self, url):
        return self.call("Page.navigate", {"url": url})

    def go_back(self):
        return self.call("Page.goBack")

    def go_forward(self):
        return self.call("Page.goForward")

    def screenshot(self, fmt="png"):
        result = self.call("Page.captureScreenshot", {"format": fmt})
        if "result" in result and "data" in result["result"]:
            return result["result"]["data"]
        return None

    def get_url(self):
        r = self.call("Page.getNavigationHistory")
        try:
            idx = r["result"]["currentIndex"]
            return r["result"]["entries"][idx]["url"]
        except Exception:
            return ""

    def get_title(self):
        r = self.eval("document.title")
        try:
            val = r["result"]["result"]["value"]
            return val if val else ""
        except Exception:
            exc = r.get("result", {}).get("exceptionDetails", {})
            if exc:
                return f"[CDP Error: {exc.get('text', str(exc))}]"
            return ""

    def get_page_text(self):
        r = self.eval("document.body ? document.body.innerText.substring(0, 15000) : ''")
        try:
            val = r["result"]["result"]["value"]
            return val if val else ""
        except Exception:
            exc = r.get("result", {}).get("exceptionDetails", {})
            if exc:
                return f"[CDP Error: {exc.get('text', str(exc))}]"
            return ""

    def get_links(self):
        r = self.eval("Array.from(document.querySelectorAll('a[href]')).map(a => ({href: a.href, text: a.textContent.trim().substring(0, 80)}))")
        try:
            val = r["result"]["result"]["value"]
            return val if val else []
        except Exception:
            exc = r.get("result", {}).get("exceptionDetails", {})
            if exc:
                return [{"error": f"CDP Error: {exc.get('text', str(exc))}"}]
            return []

    # ── Interaction ───────────────────────────────────────

    def _get_element_rect(self, selector):
        """Get center coordinates of an element via JS."""
        expr = json.dumps(selector)
        r = self.eval(f"""
            (() => {{
                const el = document.querySelector({expr});
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {{x: r.x + r.width/2, y: r.y + r.height/2,
                         w: r.width, h: r.height, tag: el.tagName,
                         visible: r.width > 0 && r.height > 0}};
            }})()
        """)
        try:
            return r["result"]["result"]["value"]
        except Exception:
            return None

    def click(self, selector):
        """Click an element by CSS selector."""
        pos = self._get_element_rect(selector)
        if not pos:
            return {"error": f"Element '{selector}' not found"}
        self.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1
        })
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": pos["x"], "y": pos["y"],
            "button": "left", "clickCount": 1
        })
        time.sleep(0.3)
        return {"success": True, "tag": pos.get("tag", ""), "pos": f"{pos['x']:.0f},{pos['y']:.0f}"}

    def type_text(self, selector, text):
        """Focus an element, clear it, type text via Input.insertText."""
        pos = self._get_element_rect(selector)
        if not pos:
            return {"error": f"Element '{selector}' not found"}
        # Click it first to focus
        self.click(selector)
        time.sleep(0.15)
        # Clear via JS
        self.eval(f"document.querySelector({json.dumps(selector)}).value = ''")
        time.sleep(0.1)
        # Insert text in one shot (faster, no synthetic key events)
        self.call("Input.insertText", {"text": text})
        return {"success": True, "length": len(text)}

    def press_key(self, key):
        """Press a raw key (Enter, Tab, Escape, etc.)."""
        key_map = {
            "enter": "Enter", "tab": "Tab", "esc": "Escape",
            "escape": "Escape", "backspace": "Backspace",
            "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
            "right": "ArrowRight", "delete": "Delete",
        }
        k = key_map.get(key.lower(), key)
        self.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": k})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": k})
        return {"success": True}

    def scroll(self, amount=600):
        self.eval(f"window.scrollBy(0, {amount})")
        time.sleep(0.3)
        return {"success": True}

    def scroll_up(self, amount=600):
        self.eval(f"window.scrollBy(0, -{amount})")
        time.sleep(0.3)
        return {"success": True}

    def switch_tab(self, target_id):
        self.close()
        return self.connect(target_id)

    def close(self):
        self._listener_alive = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    def _wait_for_page_load(self, timeout=15):
        """Wait for document.readyState == 'complete'. Returns True if loaded."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._listener_alive:
                return False
            r = self.eval("document.readyState")
            try:
                state = r["result"]["result"]["value"]
                if state == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def snapshot(self):
        """Return a text summary of the page."""
        lines = []
        lines.append(f"URL:  {self.get_url()}")
        lines.append(f"Title: {self.get_title()}")
        lines.append("─" * 50)
        text = self.get_page_text()
        if text:
            lines.append(text[:8000])
        else:
            links = self.get_links()
            if links:
                lines.append("Links found:")
                for l in links[:30]:
                    t = l.get("text", "") or "[no text]"
                    lines.append(f"  {t[:60]} → {l['href'][:80]}")
            else:
                lines.append("[Empty page]")
        return "\n".join(lines)

    # ── REPL ──────────────────────────────────────────────

    def repl(self):
        """Interactive browse session."""
        print("\n🔥 Freecrawler CDP Browser — connected. Commands:")
        print("  goto <url>  |  click <selector>  |  type <sel> <text>")
        print("  back | forward | scroll | scroll-up | press <key>")
        print("  snap | ss (screenshot) | text | links | source | url | title")
        print("  tabs | switch <id> | new <url>  |  eval <js>")
        print("  help | exit\n")

        while True:
            try:
                cmd = input("browse> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not cmd:
                continue

            parts = cmd.split(maxsplit=1)
            action = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if action in ("exit", "quit", "q"):
                print("Bye.")
                break

            elif action == "help":
                print("  goto <url>      Navigate to URL")
                print("  click <sel>     Click element by CSS selector")
                print("  type <sel> txt  Type text into element")
                print("  press <key>     Key: enter, tab, esc, backspace, up/down/left/right")
                print("  back            Go back in history")
                print("  forward         Go forward in history")
                print("  scroll          Scroll down 600px")
                print("  scroll-up       Scroll up 600px")
                print("  snap            Page text snapshot")
                print("  ss              Take screenshot (saves to file)")
                print("  text            Get page text")
                print("  links           List all links on page")
                print("  source          Get page HTML")
                print("  url             Show current URL")
                print("  title           Show page title")
                print("  tabs            List all Chrome tabs")
                print("  switch <id>     Switch to another tab (first 8 chars of id)")
                print("  new <url>       Open a new tab")
                print("  eval <js>       Run JavaScript and show result")
                print("  exit            Exit browse mode")

            elif action == "goto":
                url = arg if arg else input("URL: ").strip()
                if not url.startswith("http"):
                    url = "https://" + url
                print(f" Navigating to {url}...")
                self.navigate(url)
                if not self._wait_for_page_load():
                    print("  ⚠ Page load timeout, continuing anyway...")
                print(f" Title: {self.get_title()}")
                print(f" URL:   {self.get_url()}")

            elif action == "click":
                sel = arg if arg else input("Selector: ").strip()
                print(f" Clicking '{sel}'...")
                r = self.click(sel)
                if "error" in r:
                    print(f" ✗ {r['error']}")
                else:
                    print(f" ✓ Clicked {r['tag']} at {r['pos']}")
                    time.sleep(0.5)
                    print(f" Title: {self.get_title()}")

            elif action == "type":
                if not arg:
                    sel = input("Selector: ").strip()
                    txt = input("Text: ")
                else:
                    parts2 = cmd.split(maxsplit=2)
                    if len(parts2) >= 3:
                        sel = parts2[1]
                        txt = parts2[2]
                    else:
                        sel = parts2[1] if len(parts2) > 1 else ""
                        txt = input("Text: ")
                print(f" Typing into '{sel}'...")
                r = self.type_text(sel, txt)
                if "error" in r:
                    print(f" ✗ {r['error']}")
                else:
                    print(f" ✓ Typed {r['length']} chars")

            elif action == "press":
                key = arg or input("Key: ").strip()
                r = self.press_key(key)
                if "error" in r:
                    print(f" ✗ {r['error']}")
                else:
                    print(f" ✓ Pressed {key}")

            elif action == "back":
                self.go_back()
                time.sleep(1)
                print(f" Title: {self.get_title()}")
                print(f" URL:   {self.get_url()}")

            elif action == "forward":
                self.go_forward()
                time.sleep(1)
                print(f" Title: {self.get_title()}")
                print(f" URL:   {self.get_url()}")

            elif action == "scroll":
                self.scroll()
                print(" ✓ Scrolled down")

            elif action in ("scroll-up", "scrollup"):
                self.scroll_up()
                print(" ✓ Scrolled up")

            elif action == "snap":
                print(self.snapshot())

            elif action == "ss":
                data = self.screenshot()
                if data:
                    from datetime import datetime
                    ss_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
                    os.makedirs(ss_dir, exist_ok=True)
                    fname = os.path.join(ss_dir, f"fc_ss_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
                    with open(fname, "wb") as f:
                        f.write(base64.b64decode(data))
                    print(f" ✓ Screenshot saved → {fname}")
                else:
                    print(" ✗ Screenshot failed")

            elif action == "text":
                text = self.get_page_text()
                print(text[:5000] if text else "[No text]")

            elif action == "links":
                links = self.get_links()
                if links:
                    for i, l in enumerate(links[:40], 1):
                        t = l.get("text", "") or "[no text]"
                        print(f"  {i:2d}. {t[:60]}")
                        print(f"       {l['href'][:80]}")
                else:
                    print(" No links found")

            elif action == "source":
                r = self.eval("document.documentElement.outerHTML")
                try:
                    html = r["result"]["result"]["value"]
                    print(html[:5000])
                except Exception:
                    print(" Failed to get source")

            elif action == "url":
                print(self.get_url())

            elif action == "title":
                print(self.get_title())

            elif action == "tabs":
                targets = self.list_targets()
                print(f"\n {len(targets)} tabs open:")
                for t in targets:
                    tid = t["id"][:8]
                    title = (t.get("title") or "")[:60]
                    url = (t.get("url") or "")[:60]
                    active = "◉" if t["id"] == self.target_id else "○"
                    print(f"  {active} [{tid}] {title or url}")
                print()

            elif action == "switch":
                tid = arg.strip()
                targets = self.list_targets()
                found = None
                for t in targets:
                    if t["id"].startswith(tid):
                        found = t["id"]
                        break
                if found:
                    print(f" Switching to tab {tid}...")
                    self.switch_tab(found)
                    print(f" Title: {self.get_title()}")
                else:
                    print(f" No tab matching '{tid}'")
                    print(" Use 'tabs' to see available tabs")

            elif action == "new":
                url = arg if arg else "about:blank"
                if not url.startswith("http") and url != "about:blank":
                    url = "https://" + url
                tab = self._new_tab(url)
                tid = tab["id"][:8]
                print(f" ✓ New tab [{tid}]: {url}")

            elif action == "eval":
                js = arg if arg else input("JS: ").strip()
                r = self.eval(js)
                try:
                    val = r["result"]["result"]
                    print(json.dumps(val, indent=2, ensure_ascii=False)[:2000])
                except Exception as e:
                    print(json.dumps(r, indent=2, ensure_ascii=False)[:2000])

            else:
                # Try as URL shorthand
                if "." in action or action.startswith("http"):
                    url = action if action.startswith("http") else "https://" + action
                    print(f" Navigating to {url}...")
                    self.navigate(url)
                    if not self._wait_for_page_load():
                        print("  ⚠ Page load timeout, continuing anyway...")
                    print(f" Title: {self.get_title()}")
                else:
                    print(f" Unknown command: {action}  (try 'help')")


# ═══════════════════════════════════════════════════════════
# Login automation (CDP)
# ═══════════════════════════════════════════════════════════

CREDS_DIR = os.environ.get(
    "FREECRAWLER_CREDS_DIR",
    os.path.join(os.path.expanduser("~"), ".hermes", "profiles", "morpheus", "creds"),
)

_USER_FIELD_JS = """
(() => {
    const sels = [
        'input[type=email]',
        'input[autocomplete=username]',
        'input[name*=email i]', 'input[id*=email i]',
        'input[name*=user i]', 'input[id*=user i]',
        'input[name*=login i]', 'input[id*=login i]',
        'input[type=text]'
    ];
    for (const s of sels) {
        const els = Array.from(document.querySelectorAll(s));
        const el = els.find(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && !e.disabled;
        });
        if (el) {
            el.setAttribute('data-fc-user', '1');
            return s;
        }
    }
    return null;
})()
"""

_PASS_FIELD_JS = """
(() => {
    const els = Array.from(document.querySelectorAll('input[type=password]'));
    const el = els.find(e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !e.disabled;
    });
    if (el) {
        el.setAttribute('data-fc-pass', '1');
        return true;
    }
    return false;
})()
"""

_SUBMIT_JS = """
(() => {
    const texts = ['se connecter','connexion','log in','login','sign in',
                   'iniciar','entrar','continuer','continue','next','suivant','submit',
                   'connexion','s\'identifier'];
    const btns = Array.from(document.querySelectorAll(
        'button[type=submit], input[type=submit], button, div[role=button]'));
    const vis = btns.filter(b => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && !b.disabled && !b.closest('[hidden]');
    });
    // 1. Explicit submit buttons
    let el = vis.find(b => b.type === 'submit');
    // 2. Match by text
    if (!el) {
        el = vis.find(b => {
            const t = ((b.innerText || b.value || b.getAttribute('aria-label') || '') +
                       ' ' + (b.querySelector('span')?.innerText || '')).toLowerCase().trim();
            return texts.some(x => t.includes(x));
        });
    }
    // 3. Last-resort: any visible button that's not a cancel/back link
    if (!el) {
        const skip = ['forgot','cancel','back','create','register','sign up','s\'inscrire',
                      'phone','google','apple','facebook','microsoft'];
        el = vis.find(b => {
            const t = ((b.innerText || b.value || b.getAttribute('aria-label') || '') +
                       ' ' + (b.querySelector('span')?.innerText || '')).toLowerCase().trim();
            if (!t) return false;   // skip truly empty buttons
            return !skip.some(x => t.includes(x));
        });
    }
    if (el) {
        el.setAttribute('data-fc-submit', '1');
        return true;
    }
    return false;
})()
"""


def load_creds(name):
    """Load {username/email, password} from a creds JSON file by site name."""
    path = os.path.join(CREDS_DIR, f"{name}.json")
    if not os.path.exists(path):
        available = []
        if os.path.isdir(CREDS_DIR):
            available = [f[:-5] for f in os.listdir(CREDS_DIR) if f.endswith(".json")]
        return {"error": f"No creds file '{name}.json' in {CREDS_DIR}",
                "available": available}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    user = data.get("username") or data.get("email") or data.get("user")
    password = data.get("password") or data.get("pass")
    if not user or not password:
        return {"error": f"'{name}.json' missing username/email or password"}
    return {"user": user, "password": password}


def _eval_value(browser, js):
    """Run JS and return the raw value (or None)."""
    r = browser.eval(js)
    try:
        res = r["result"]["result"]
        if "exceptionDetails" in r.get("result", {}):
            return None
        return res.get("value")
    except Exception:
        return None


def cdp_login(url, user, password, cdp_port=9222,
              user_sel=None, pass_sel=None, submit_sel=None, timeout=25):
    """Non-interactive login via CDP. Auto-detects fields; supports
    two-step flows (email -> continue -> password). Returns a result dict."""
    browser = CDPBrowser(cdp_port=cdp_port)
    try:
        browser.connect()
    except Exception as e:
        return {"error": f"CDP connect failed: {e}. Chrome on :{cdp_port}?"}

    steps = []
    try:
        browser.navigate(url)
        browser._wait_for_page_load(timeout=timeout)
        time.sleep(1.0)
        start_url = _eval_value(browser, "location.href") or url

        # ── Step 1: user/email field ──
        sel = user_sel
        if not sel:
            found = _eval_value(browser, _USER_FIELD_JS)
            sel = "[data-fc-user='1']" if found else None
        if sel:
            r = browser.type_text(sel, user)
            steps.append({"fill_user": r})
        else:
            steps.append({"fill_user": {"skipped": "no user field visible"}})

        # ── Password on same page? ──
        has_pass = bool(pass_sel) or bool(_eval_value(browser, _PASS_FIELD_JS))

        if not has_pass:
            # Two-step flow: submit email first, wait for password page
            if _eval_value(browser, _SUBMIT_JS):
                browser.click("[data-fc-submit='1']")
                steps.append({"submit_email_step": True})
            else:
                browser.press_key("enter")
                steps.append({"submit_email_step": "enter_key"})
            time.sleep(2.5)
            browser._wait_for_page_load(timeout=10)
            has_pass = bool(_eval_value(browser, _PASS_FIELD_JS))

        # ── Step 2: password ──
        if has_pass:
            psel = pass_sel or "[data-fc-pass='1']"
            if not pass_sel:
                _eval_value(browser, _PASS_FIELD_JS)  # re-tag after nav
            r = browser.type_text(psel, password)
            steps.append({"fill_password": r})
        else:
            return {"error": "No password field found (captcha? unknown flow?)",
                    "url": _eval_value(browser, "location.href"), "steps": steps}

        # ── Submit ──
        ssel = submit_sel
        if not ssel:
            ssel = "[data-fc-submit='1']" if _eval_value(browser, _SUBMIT_JS) else None
        if ssel:
            browser.click(ssel)
            steps.append({"submit": True})
        else:
            browser.press_key("enter")
            steps.append({"submit": "enter_key"})

        time.sleep(3.0)
        browser._wait_for_page_load(timeout=timeout)
        time.sleep(1.0)

        # ── Verify ──
        end_url = _eval_value(browser, "location.href")
        still_pass = bool(_eval_value(
            browser,
            "!!Array.from(document.querySelectorAll('input[type=password]'))"
            ".find(e => e.getBoundingClientRect().width > 0)"))
        error_text = _eval_value(browser, """
            (() => {
                const sels = ['[class*=error i]','[id*=error i]','[role=alert]'];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.innerText.trim()) return el.innerText.trim().slice(0, 200);
                }
                return null;
            })()
        """)

        success = (not still_pass) and (end_url != start_url or not error_text)
        return {
            "success": success,
            "url": end_url,
            "title": browser.get_title(),
            "password_field_still_visible": still_pass,
            "error_message_on_page": error_text,
            "steps": steps,
        }
    finally:
        browser.close()


def browse_mode(target_id=None, cdp_port=9222, start_url=None):
    """Entry point for the browse subcommand."""
    if not HAS_WEBSOCKET:
        print("Error: websocket-client not installed.")
        print("Install: pip install websocket-client")
        sys.exit(1)

    browser = CDPBrowser(cdp_port=cdp_port)

    print(f"🔍 Connecting to Chrome on ws://localhost:{cdp_port}...")
    try:
        browser.connect(target_id)
        print(f"✓ Connected to tab: {browser.get_title() or browser.target_id[:8]}")
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        print("  Make sure Chrome is running with --remote-debugging-port=9222")
        sys.exit(1)

    # Auto-navigate if URL was provided as CLI argument
    if start_url:
        url = start_url if start_url.startswith("http") else "https://" + start_url
        print(f"→ Navigating to {url}...")
        browser.navigate(url)
        loaded = browser._wait_for_page_load()
        if not loaded:
            print("  ⚠ Page load timeout, continuing anyway...")
        else:
            try:
                print(f"  Title: {browser.get_title()}")
            except Exception:
                print("  (page loaded, title unavailable)")

    try:
        browser.repl()
    finally:
        browser.close()


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def _dump(data, pretty):
    return json.dumps(data, ensure_ascii=False, indent=2 if pretty else None)


def _emit(result, quiet=False, pretty=False, fp=None, item_label="items", quiet_fn=None):
    """Unified output for every command: handles list results (search/crawl/
    xsearch/xuser) and dict results (scrape/extract/map/linkedin) the same way,
    so main() doesn't need to duplicate the save/print logic per command."""
    if isinstance(result, list):
        if quiet:
            for item in result:
                print(_dump(item, pretty=False))
            return
        output = _dump(result, pretty)
        if fp:
            fp.write(output)
            print(f"Saved → {fp.name} ({len(result)} {item_label})")
        else:
            print(output)
        return

    if not isinstance(result, dict):
        print(str(result))
        return

    if quiet:
        content = result.get("data") or result.get("content", "")
        if isinstance(content, (dict, list)):
            content = _dump(content, pretty)
        if fp:
            fp.write(content)
        else:
            print(content)
    else:
        output = _dump(result, pretty)
        if fp:
            fp.write(output)
            print(f"Saved → {fp.name}")
        else:
            print(output)


def main():
    parser = argparse.ArgumentParser(
        description="Freecrawler 🔥 — Local scraper. Free alternative to Firecrawl.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  search "query"           DuckDuckGo search (no API key)
  scrape URL                Extract content from a URL
  crawl URL                 Crawl multiple pages
  map URL                   Build internal URL map
  extract URL --schema ...  Structured CSS extraction
  xsearch "query"           Search X/Twitter
  xuser @username           Get user tweets
  linkedin URL               Get LinkedIn profile

Schema grammar for --schema:
  "baseSelector: name=selector, name2=selector2"
"""
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("search", help="Search the web")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--output", "-o")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("scrape", help="Extract content from a URL")
    p.add_argument("url")
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.add_argument("--browser", action="store_true", help="JS rendering via Playwright")
    p.add_argument("--schema", help="CSS schema for structured JSON")
    p.add_argument("--output", "-o")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("crawl", help="Crawl multiple pages")
    p.add_argument("url")
    p.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    p.add_argument("--browser", action="store_true")
    p.add_argument("--output", "-o")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("map", help="Build sitemap of internal URLs")
    p.add_argument("url")
    p.add_argument("--depth", type=int, default=2)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--delay", type=float, default=0.5)
    p.add_argument("--output", "-o")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("extract", help="Structured CSS extraction")
    p.add_argument("url")
    p.add_argument("--schema", required=True,
                    help="Format: 'baseSelector: name=selector, name2=selector2'")
    p.add_argument("--browser", action="store_true")
    p.add_argument("--output", "-o")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("xsearch", help="Search X/Twitter")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--output", "-o")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("xuser", help="Get tweets from an X user")
    p.add_argument("username")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--output", "-o")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("linkedin", help="Get LinkedIn profile")
    p.add_argument("url")
    p.add_argument("--output", "-o")
    p.add_argument("--pretty", action="store_true")

    p = sub.add_parser("browse", help="Interactive CDP browser (requires Chrome on :9222)")
    p.add_argument("url", nargs="?", help="URL to open (optional)")
    p.add_argument("--tab", help="Target tab ID (partial match)")
    p.add_argument("--port", type=int, default=9222, help="CDP port (default: 9222)")

    p = sub.add_parser("login", help="Automated login via CDP (auto-detects fields)")
    p.add_argument("url", help="Login page URL")
    p.add_argument("--creds", help="Creds file name (e.g. 'wttj' -> creds/wttj.json)")
    p.add_argument("--user", help="Username/email (overrides --creds)")
    p.add_argument("--password", help="Password (overrides --creds)")
    p.add_argument("--user-sel", help="CSS selector for the user field (optional)")
    p.add_argument("--pass-sel", help="CSS selector for the password field (optional)")
    p.add_argument("--submit-sel", help="CSS selector for the submit button (optional)")
    p.add_argument("--port", type=int, default=9222, help="CDP port (default: 9222)")
    p.add_argument("--pretty", action="store_true")

    args = parser.parse_args()
    fp = open(args.output, "w", encoding="utf-8") if getattr(args, "output", None) else None

    try:
        if args.command == "search":
            results = search(args.query, args.limit)
            quiet_fn = lambda r: r.get("error", r.get("url", str(r)))
            _emit(results, quiet=args.quiet, pretty=args.pretty, fp=fp,
                  item_label="results", quiet_fn=quiet_fn)

        elif args.command == "scrape":
            result = scrape(args.url, args.format, args.browser, getattr(args, "schema", None))
            _emit(result, quiet=args.quiet, pretty=args.pretty, fp=fp)

        elif args.command == "crawl":
            results = crawl(args.url, args.format, args.depth, args.limit, args.quiet, args.browser, args.delay)
            # crawl's --quiet only silences progress messages during the crawl
            # itself (handled above); the final output is always the full list.
            _emit(results, quiet=False, pretty=args.pretty, fp=fp, item_label="pages")

        elif args.command == "map":
            result = site_map(args.url, args.depth, args.limit, args.delay)
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)

        elif args.command == "extract":
            result = extract(args.url, args.schema, getattr(args, "browser", False))
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)

        elif args.command == "xsearch":
            results = x_search(args.query, args.limit)
            quiet_fn = lambda t: t.get("error") or f"[{t.get('date','')[:10]}] @{t.get('user','')}: {t.get('content','')[:200]}"
            _emit(results, quiet=args.quiet, pretty=args.pretty, fp=fp,
                  item_label="tweets", quiet_fn=quiet_fn)

        elif args.command == "xuser":
            username = args.username.lstrip("@")
            results = x_user(username, args.limit)
            quiet_fn = lambda t: t.get("error") or f"[{t.get('date','')[:10]}] {t.get('content','')[:200]}"
            _emit(results, quiet=args.quiet, pretty=args.pretty, fp=fp,
                  item_label="tweets", quiet_fn=quiet_fn)

        elif args.command == "linkedin":
            result = linkedin_profile(args.url)
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)

        elif args.command == "browse":
            target_id = None
            if args.tab:
                browser = CDPBrowser(cdp_port=args.port)
                targets = browser.list_targets()
                for t in targets:
                    if t["id"].startswith(args.tab):
                        target_id = t["id"]
                        break
                if not target_id:
                    print(f"No tab matching '{args.tab}'")
                    sys.exit(1)
            browse_mode(target_id=target_id, cdp_port=args.port, start_url=args.url)

        elif args.command == "login":
            user, password = args.user, args.password
            if args.creds and not (user and password):
                c = load_creds(args.creds)
                if "error" in c:
                    _emit(c, quiet=False, pretty=args.pretty, fp=fp)
                    sys.exit(1)
                user = user or c["user"]
                password = password or c["password"]
            if not (user and password):
                print("Error: provide --creds NAME or --user + --password")
                sys.exit(1)
            result = cdp_login(args.url, user, password, cdp_port=args.port,
                               user_sel=args.user_sel, pass_sel=args.pass_sel,
                               submit_sel=args.submit_sel)
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)
            if result.get("error") or not result.get("success"):
                sys.exit(1)

        else:
            parser.print_help()
    finally:
        if fp:
            fp.close()


if __name__ == "__main__":
    main()