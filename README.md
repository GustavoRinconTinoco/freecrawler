# Freecrawler 🔥

**100% local, free, and unlimited alternative to Firecrawl.**

Freecrawler is an all-in-one scraper for AI agents. It extracts web content, generates structured JSON, searches X/Twitter, and browses the web autonomously — no API keys, no credits, no external dependencies.

Built by and for AI agents that need to scrape without being charged per page.

## Features

### 🕷️ Web Scraping
- **Scrape mode**: extract clean content from any URL as Markdown, text, or JSON
- **Crawl mode**: discover and extract multiple pages from the same site with configurable depth
- **Map mode**: build a sitemap of all internal URLs
- **JS rendering**: optional Playwright support for SPAs and heavy JavaScript sites

### 📊 Structured JSON (no LLM required)
Uses `crawl4ai` with CSS schemas to extract structured data without any language model:

```bash
python freecrawler.py extract https://books.toscrape.com \
  --schema "article.product_pod: h3 a=title .price_color=price"
```

### 🐦 X/Twitter
- Search tweets by keywords
- Extract user timelines
- No X API key — uses twscrape with GraphQL

### 🧭 Autonomous Browsing
- Integrated browser-use for autonomous web navigation
- The AI agent decides what to do: search, click, fill forms, extract data

## Installation

```bash
# Core requirements
pip install requests beautifulsoup4 lxml trafilatura html2text markdownify

# Full functionality
pip install crawl4ai twscrape playwright "browser-use[core]"
python -m playwright install chromium
```

## Quick Start

```bash
# Extract a URL
python freecrawler.py scrape https://example.com

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

# Autonomous browsing
python freecrawler.py browse "Find the current price of bitcoin"
```

## Comparison with Firecrawl

| Feature | Firecrawl | Freecrawler |
|---|---|---|
| Cost | Per credit/page | **Free** |
| API Key | Required | **Not needed** |
| JS Rendering | Automatic | With `--browser` |
| Markdown output | ✅ | ✅ |
| Crawl | ✅ | ✅ |
| Map (sitemap) | ✅ | ✅ |
| Structured JSON | ✅ (with schema) | ✅ (with CSS schema) |
| X/Twitter scraping | ❌ | ✅ |
| Autonomous browsing | ❌ | ✅ (browser-use) |
| Usage limits | Based on plan | **Unlimited** |
| Dependencies | Remote server | **100% local** |
| Availability | Service dependent | **Always works** |

## Architecture

Freecrawler has 3 interchangeable engines:

1. **HTTP Direct** (requests + trafilatura): fast, for static HTML
2. **crawl4ai**: JS rendering, JSON extraction via CSS schema, advanced crawling
3. **browser-use**: AI-powered autonomous browsing

The engine is automatically selected based on the mode and flags.

## License

MIT — do whatever you want.
