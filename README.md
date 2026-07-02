# Freecrawler 🔥

**100% local, free, and unlimited alternative to Firecrawl.**

Freecrawler is an all-in-one scraper for AI agents. It extracts web content, generates structured JSON, searches the web, X/Twitter, and LinkedIn — no API keys, no credits, no external dependencies.

Built by and for AI agents that need to scrape without being charged per page.

## Features

### 🕷️ Web Scraping
- **Scrape mode**: extract clean content from any URL as text, markdown, or JSON
- **Crawl mode**: discover and extract multiple pages from the same site with configurable depth
- **Map mode**: build a sitemap of all internal URLs
- **JS rendering**: optional Playwright support for SPAs and heavy JavaScript sites

### 🔍 Web Search
- Search via DuckDuckGo API — no API key required
- Integrated into the CLI: `python freecrawler.py search "query"`

### 📊 Structured JSON (no LLM required)
Uses `crawl4ai` or BeautifulSoup with CSS schemas to extract structured data without any language model:

```bash
python freecrawler.py extract https://books.toscrape.com \
  --schema "article.product_pod: h3 a=title .price_color=price"
```

### 🐦 X/Twitter
- Search tweets by keywords
- Extract user timelines
- No X API key — uses twscrape with GraphQL

### 💼 LinkedIn
- Scrape public LinkedIn profile data
- Requires `linkedin-scraper` package and credentials

## Installation

```bash
# Core requirements
pip install requests beautifulsoup4 lxml trafilatura html2text markdownify

# Optional features
pip install duckduckgo_search     # Web search
pip install crawl4ai              # JS rendering + advanced extraction
pip install twscrape              # X/Twitter scraping
pip install playwright            # Browser automation
python -m playwright install chromium

# LinkedIn
pip install linkedin-scraper
export LINKEDIN_EMAIL="your@email.com"
export LINKEDIN_PASS="your_password"
```

## Quick Start

```bash
# Search the web
python freecrawler.py search "artificial intelligence" --limit 5

# Extract a URL
python freecrawler.py scrape https://example.com

# With JS rendering
python freecrawler.py scrape https://example.com --browser

# Plain text
python freecrawler.py scrape https://example.com --format text

# Crawl
python freecrawler.py crawl https://example.com --depth 2 --limit 20

# Site map
python freecrawler.py map https://example.com

# Structured JSON
python freecrawler.py extract https://books.toscrape.com \
  --schema "article.product_pod: h3 a=title .price_color=price"

# Search X/Twitter
python freecrawler.py xsearch "artificial intelligence" --limit 10

# User tweets
python freecrawler.py xuser @username

# LinkedIn profile
python freecrawler.py linkedin https://www.linkedin.com/in/username
```

## Comparison with Firecrawl

| Feature | Firecrawl | Freecrawler |
|---|---|---|
| Cost | Per credit/page | **Free** |
| API Key | Required | **Not needed** |
| JS Rendering | Automatic | With `--browser` |
| Web Search | Limited | ✅ (DuckDuckGo) |
| Markdown output | ✅ | ✅ |
| Crawl | ✅ | ✅ |
| Map (sitemap) | ✅ | ✅ |
| Structured JSON | ✅ (with schema) | ✅ (with CSS schema) |
| X/Twitter scraping | ❌ | ✅ |
| LinkedIn scraping | ❌ | ✅ |
| Usage limits | Based on plan | **Unlimited** |
| Dependencies | Remote server | **100% local** |
| Availability | Service dependent | **Always works** |

## Architecture

Freecrawler has 3 interchangeable engines:

1. **HTTP Direct** (requests + trafilatura): fast, for static HTML
2. **crawl4ai**: JS rendering, JSON extraction via CSS schema, advanced crawling
3. **Playwright**: full browser automation for complex JS sites

The engine is automatically selected based on the mode and flags.

## Changelog

### v2.1 (2026-07-02)

**Performance, safety, and compatibility improvements** — fused with improvements from community review.

- **F2**: Enhanced schema parser — now auto-detects CSV format (`name=selector, name=selector`) vs legacy whitespace format (`selector=name selector=name`). Both work, no breaking changes.
- **F6**: Added `urllib.error.HTTPError` handling in stdlib fallback — no crash on 4xx/5xx when `requests` is absent.
- **F7**: Shared HTTP session across crawl requests (connection reuse) + `_html` internal cache (no more double-fetch).
- **F8**: Added `--delay` parameter (default 0.5s) and binary extension filter (PDF, images, zips, etc. are skipped before fetching).
- **LinkedIn**: Added `driver.quit()` in `finally` block — no more orphaned Chrome processes. Added ToS warning.
- **Playwright**: Added `finally` block to ensure browser closes even on error.
- **Internal**: Added `_clean()` to strip internal keys before output.

Feedback and improvements by Agabilan — code review and practical enhancements for robustness.

## Credits

- [@miguelitoxrox](https://github.com/miguelitoxrox) — thorough code audit and bug report (Issue #1)
- Agabilan — code review, performance improvements, and robustness enhancements
- Built by [@GustavoRinconTinoco](https://github.com/GustavoRinconTinoco)

### v2.0 (2026-07-02)

**Bug fixes and reliability improvements** — 8 verified issues resolved.

- **F1**: Added missing `import urllib.request` for the stdlib fallback path
- **F2**: New CSV schema format (`title=h3 a, price=.price_color`) preserves CSS selectors with spaces
- **F3**: Unified schema parser grammar — same format works for both crawl4ai and BeautifulSoup engines
- **F4**: Fixed `xuser` command — now resolves username to numeric user ID before calling the API
- **F5**: Fixed `linkedin` command — uses Selenium driver + `actions.login()` instead of invalid constructor kwargs
- **F6**: Captures real HTTP status codes instead of hardcoding 200
- **F7**: Optimized crawler — reduces double-fetching, preserves child links on content errors
- **F8**: (Pending) robots.txt respect, rate limiting, and retry with exponential backoff

Full audit and fixes by [@miguelitoxrox](https://github.com/miguelitoxrox) — comprehensive code review with reproductions, consequences, and remediations for each finding.

## Credits

- [@miguelitoxrox](https://github.com/miguelitoxrox) — thorough code audit and bug report (Issue #1)
- Agabilan — code review, performance improvements, and robustness enhancements
- Built by [@GustavoRinconTinoco](https://github.com/GustavoRinconTinoco)

## License

MIT — do whatever you want.
