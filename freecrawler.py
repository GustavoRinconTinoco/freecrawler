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
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request  # FIX F1: was used in _fetch() fallback but never imported
import urllib.error    # FIX F6 v2.1: for HTTPError handling in stdlib fallback
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
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from ddgs import DDGS
        HAS_DDGS = True
    except ImportError:
        pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
TIMEOUT = 30

# Extensions we should never try to parse as HTML (FIX F8)
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

def _fetch(url, timeout=15, session=None):
    """HTTP GET with User-Agent and cookie handling.

    Returns (html, final_url, status_code).
    FIX F6: status_code is now the real HTTP status instead of a hardcoded 200.
    FIX F7: optionally reuses a requests.Session for connection keep-alive.
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
        # FIX F6 v2.1: capture status_code even on HTTP errors
        body = e.read().decode("utf-8", errors="replace")
        return body, e.url or url, e.code
    html = resp.read().decode("utf-8", errors="replace")
    return html, resp.geturl(), resp.getcode()


def _is_probably_binary(url):
    """FIX F8: skip known binary/non-HTML extensions before fetching/parsing."""
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
    """FIX F7: parse links out of HTML we already fetched, instead of re-fetching."""
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

    Grammar (single source of truth — FIX F3, used by every engine):
      "baseSelector: name=selector, name2=selector2"
    Fields are comma-separated so selectors may contain spaces
    (FIX F2 — "h3 a" no longer gets mangled by whitespace splitting).

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

    # FIX F2 v2.1: comma-based CSV format "name=selector, name2=selector2"
    # vs legacy whitespace format "selector=name selector2=name2"
    if "," in field_part:
        # New CSV format: name=selector, name=selector
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


def _scrape_http(url, fmt="text", session=None):
    """HTTP direct engine (fast, no JS). Returns dict; also stashes raw html
    under result['_html'] so callers (crawl) can reuse it instead of re-fetching (FIX F7)."""
    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "http"}
    try:
        html, final_url, status_code = _fetch(url, session=session)
    except Exception as e:
        result["error"] = str(e)
        result["time_ms"] = int((time.time() - t0) * 1000)
        return result

    result["final_url"] = final_url
    result["status_code"] = status_code  # FIX F6
    result["time_ms"] = int((time.time() - t0) * 1000)
    result["title"] = _extract_title(html)
    result["_html"] = html  # internal use, stripped before final output

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
    return result


def _scrape_playwright(url, fmt="text"):
    """Playwright engine (JS rendering)."""
    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "playwright"}
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
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
        return result
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
    result["_html"] = html
    return result


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
            crawl_result = await crawler.arun(url, config=config)
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

def search(query, limit=5):
    """Search the web using DuckDuckGo API (no key needed, no blocking)."""
    if HAS_DDGS:
        try:
            with DDGS() as ddgs:
                results = []
                for r in ddgs.text(query, max_results=limit):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("href", ""),
                        "description": r.get("body", ""),
                    })
                return results if results else [{"error": "No results found"}]
        except Exception as e:
            return [{"error": f"DDGS search failed: {e}"}]
    return [{"error": "DuckDuckGo Search library not installed. pip install duckduckgo_search"}]


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


def crawl(url, fmt="text", depth=1, limit=10, quiet=False, browser=False, delay=0.5):
    """Crawl multiple pages from the same site.

    FIX F7: fetches each page once (reuses the already-downloaded HTML to discover
    links instead of re-fetching), reuses a single HTTP session for keep-alive, and
    still enqueues links even when content extraction (but not the fetch) had issues.
    FIX F8: skips known binary URLs and waits `delay` seconds between requests.
    """
    session = requests.Session() if HAS_REQUESTS else None
    visited = set()
    to_visit = [(url, 0)]
    results = []

    while to_visit and len(results) < limit:
        current_url, current_depth = to_visit.pop(0)
        if current_url in visited or _is_probably_binary(current_url):
            continue
        visited.add(current_url)

        if not quiet:
            print(f"  → {current_url}", file=sys.stderr)

        page = scrape(current_url, fmt=fmt, browser=browser, session=session)
        html = page.pop("_html", None) if isinstance(page, dict) else None

        if isinstance(page, dict) and page.get("error") and not html:
            # total fetch failure: nothing to extract, nothing to discover links from
            time.sleep(delay)
            continue

        results.append(page)

        if current_depth < depth and html:
            for u in _discover_urls_from_html(current_url, html):
                if u not in visited:
                    to_visit.append((u, current_depth + 1))

        time.sleep(delay)  # FIX F8: basic politeness delay

    return results


def site_map(url, depth=2, limit=50, delay=0.5):
    """Build a sitemap of internal URLs."""
    session = requests.Session() if HAS_REQUESTS else None
    visited = set()
    to_visit = [(url, 0)]
    tree = {url: []}

    while to_visit and len(visited) < limit:
        current_url, current_depth = to_visit.pop(0)
        if current_url in visited or _is_probably_binary(current_url):
            continue
        visited.add(current_url)

        try:
            html, _final_url, _status = _fetch(current_url, session=session)
        except Exception:
            tree[current_url] = []
            continue

        discovered = _discover_urls_from_html(current_url, html)
        tree[current_url] = discovered

        if current_depth < depth:
            for u in discovered:
                if u not in visited:
                    to_visit.append((u, current_depth + 1))

        time.sleep(delay)

    return {"root": url, "total_urls": len(visited), "urls": sorted(visited), "tree": tree}


def _extract_with_schema(soup, schema):
    """Shared extraction logic used by both the crawl4ai path and the bs4 fallback,
    so both honor the exact same schema grammar (FIX F3)."""
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
    which engine is available (FIX F3)."""
    schema = _parse_schema(schema_str)
    if not schema:
        return {"error": "Could not parse --schema. Expected format: 'baseSelector: name=selector, name2=selector2'"}

    if HAS_CRAWL4AI:
        return asyncio.run(_scrape_crawl4ai(url, fmt="json", schema=schema))

    try:
        html, _final_url, status_code = _fetch(url)
    except Exception as e:
        return {"error": f"HTTP error: {e}"}
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
    async for tweet in api.search(query, limit=limit):
        tweets.append({
            "id": tweet.id, "date": str(tweet.date),
            "user": tweet.user.username if tweet.user else "unknown",
            "fullname": tweet.user.displayname if tweet.user else "",
            "content": tweet.rawContent, "likes": tweet.likeCount,
            "retweets": tweet.retweetCount, "replies": tweet.replyCount,
            "url": tweet.url,
        })
    return tweets


def x_user(username, limit=20):
    if not HAS_TWSCRAPE:
        return [{"error": "twscrape not installed. pip install twscrape"}]
    return asyncio.run(_x_user_async(username, limit))


async def _x_user_async(username, limit=20):
    """FIX F4: twscrape's user_tweets() expects a numeric user id, not a handle.
    Resolve the handle to an id first via user_by_login()."""
    api = twscrape.API()
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
    return tweets


# ═══════════════════════════════════════════════════════════
# LINKEDIN
# ═══════════════════════════════════════════════════════════

def linkedin_profile(url, email=None, password=None):
    """FIX F5: linkedin-scraper's Person does not accept email/password kwargs.
    It needs an authenticated Selenium driver. This also requires `selenium` and
    a Chromedriver to be installed and on PATH.

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

    driver = webdriver.Chrome()
    try:
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
    finally:
        driver.quit()


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def _clean(result):
    """Strip internal-only keys (like the cached html) before we print/save."""
    if isinstance(result, dict):
        result.pop("_html", None)
    elif isinstance(result, list):
        for r in result:
            if isinstance(r, dict):
                r.pop("_html", None)
    return result


def _emit(result, quiet=False, pretty=False, fp=None):
    result = _clean(result)

    if isinstance(result, list):
        output = json.dumps(result, ensure_ascii=False, indent=2 if pretty else None)
        if fp:
            fp.write(output)
            print(f"Saved → {fp.name} ({len(result)} pages)")
        else:
            print(output)
        return

    if not isinstance(result, dict):
        print(str(result))
        return

    if quiet:
        content = result.get("data") or result.get("content", "")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, indent=2 if pretty else None)
        if fp:
            fp.write(content)
        else:
            print(content)
    else:
        output = json.dumps(result, ensure_ascii=False, indent=2 if pretty else None)
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

    args = parser.parse_args()
    fp = open(args.output, "w", encoding="utf-8") if getattr(args, "output", None) else None

    try:
        if args.command == "search":
            results = search(args.query, args.limit)
            if args.quiet:
                for r in results:
                    print(r.get("error", r.get("url", str(r))))
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.command == "scrape":
            result = scrape(args.url, args.format, args.browser, getattr(args, "schema", None))
            _emit(result, quiet=args.quiet, pretty=args.pretty, fp=fp)

        elif args.command == "crawl":
            results = crawl(args.url, args.format, args.depth, args.limit, args.quiet, args.browser, args.delay)
            results = _clean(results)
            if fp:
                json.dump(results, fp, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Saved → {args.output} ({len(results)} pages)")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.command == "map":
            result = site_map(args.url, args.depth, args.limit, args.delay)
            if fp:
                json.dump(result, fp, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Saved → {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.command == "extract":
            result = extract(args.url, args.schema, getattr(args, "browser", False))
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)

        elif args.command == "xsearch":
            results = x_search(args.query, args.limit)
            if args.quiet:
                for t in results:
                    if t.get("error"):
                        print(t["error"])
                    else:
                        print(f"[{t.get('date','')[:10]}] @{t.get('user','')}: {t.get('content','')[:200]}")
            elif fp:
                json.dump(results, fp, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Saved → {args.output} ({len(results)} tweets)")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.command == "xuser":
            username = args.username.lstrip("@")
            results = x_user(username, args.limit)
            if args.quiet:
                for t in results:
                    if t.get("error"):
                        print(t["error"])
                    else:
                        print(f"[{t.get('date','')[:10]}] {t.get('content','')[:200]}")
            elif fp:
                json.dump(results, fp, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Saved → {args.output} ({len(results)} tweets)")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.command == "linkedin":
            result = linkedin_profile(args.url)
            _emit(result, quiet=False, pretty=args.pretty, fp=fp)

        else:
            parser.print_help()
    finally:
        if fp:
            fp.close()


if __name__ == "__main__":
    main()
