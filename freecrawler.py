#!/usr/bin/env python3
"""
Hermes Scraper v2 — alternativa local a Firecrawl con motor crawl4ai.
Modos:
  scrape  <url>            Extrae contenido limpio (Markdown/texto/JSON)
  crawl   <url>            Descubre y extrae múltiples páginas del mismo sitio
  map     <url>            Lista todas las URLs internas descubiertas
  extract <url>            Extrae datos estructurados con schema CSS (JSON)

Flags:
  --format markdown|text|json   Formato de salida (default: markdown)
  --schema "selector={...}"     Schema CSS para --json (ej: "h1=title .price=precio")
  --depth N                Profundidad máxima para crawl (default: 1)
  --limit N                Máximo de páginas para crawl (default: 10)
  --browser                Usa navegador real para JS pesado (Playwright)
  --output ARCHIVO         Guarda a archivo en vez de stdout
  --quiet                  Solo el contenido, sin metadatos
  --pretty                 JSON formateado con indentación
"""

import sys, json, os, time, argparse
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
TIMEOUT = 30

# ── Detectar crawl4ai ───────────────────────────────────────
HAVE_CRAWL4AI = False
try:
    from crawl4ai import AsyncWebCrawler
    from crawl4ai.extraction_strategy import JsonCssExtractionStrategy
    HAVE_CRAWL4AI = True
except ImportError:
    pass

# ── Utilidades base ──────────────────────────────────────────

HAVE_TWSCRAPE = False
try:
    import twscrape
    HAVE_TWSCRAPE = True
except ImportError:
    pass

HAVE_LINKEDIN = False
try:
    from linkedin_scraper import Person, Company, JobSearch
    HAVE_LINKEDIN = True
except ImportError:
    pass

def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s

def _same_domain(base: str, url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False

def _normalize_url(base: str, href: str) -> str | None:
    if not href or href.startswith("#") or href.startswith("javascript:"):
        return None
    href = href.split("#")[0]
    full = urljoin(base, href)
    if not full.startswith(("http://", "https://")):
        return None
    return full

def _discover_urls(session, url: str) -> list[str]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    found = set()
    for a in soup.find_all("a", href=True):
        normalized = _normalize_url(url, a["href"])
        if normalized and _same_domain(url, normalized):
            found.add(normalized)
    return sorted(found)

def _fallback_extract(soup: BeautifulSoup, fmt: str = "markdown") -> str:
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    body = soup.find("body") or soup
    for unwanted in body.select(
        ".sidebar, .menu, .nav, .footer, .header, .ad, .advertisement, "
        ".social-share, .comments, .related-posts"
    ):
        unwanted.decompose()
    if fmt == "markdown":
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0
        return h.handle(str(body))
    text = body.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "\n".join(lines)

# ── Parsear schema CSS ──────────────────────────────────────

def _parse_schema(schema_str: str) -> dict | None:
    """
    Convierte formato simple o JSON a schema de JsonCssExtractionStrategy.

    Formato simple con baseSelector: "baseSelector: selector=nombre selector2=nombre2"
      Ej: "article.product_pod: h3 a=title .price_color=price"

    Formato simple sin baseSelector: "selector=nombre selector2=nombre2"
      Usa body como baseSelector (extrae solo 1 item).

    Formato JSON: '{"baseSelector":"article","fields":[{"name":"t","selector":"h2","type":"text"}]}'
    """
    if not schema_str:
        return None
    # Intentar como JSON primero
    try:
        schema = json.loads(schema_str)
        if isinstance(schema, dict) and "baseSelector" in schema:
            return schema
    except json.JSONDecodeError:
        pass
    # Formato simple
    base_selector = "body"
    field_part = schema_str
    if ":" in schema_str:
        parts = schema_str.split(":", 1)
        base_selector = parts[0].strip()
        field_part = parts[1].strip()
    fields = []
    for part in field_part.split():
        if "=" not in part:
            continue
        selector, name = part.split("=", 1)
        if selector and name:
            fields.append({"name": name.strip(), "selector": selector.strip(), "type": "text"})
    if not fields:
        return None
    return {"name": "extracted_data", "baseSelector": base_selector, "fields": fields}

# ── Motor HTTP directo (fallback) ────────────────────────────

def _scrape_http(url: str, fmt: str = "markdown") -> dict:
    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "http"}
    session = _session()
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        result["error"] = str(e)
        result["time_ms"] = int((time.time() - t0) * 1000)
        return result

    html = r.text
    result["status_code"] = r.status_code
    result["time_ms"] = int((time.time() - t0) * 1000)

    soup = BeautifulSoup(html, "lxml")
    result["title"] = (soup.title.string.strip() if soup.title and soup.title.string else "")

    if fmt == "json":
        # Extracción básica: devuelve el HTML limpio como texto plano
        # El usuario puede procesar después
        import trafilatura
        content = trafilatura.extract(html, output_format="txt")
        if not content:
            content = _fallback_extract(soup, fmt="text")
        result["content"] = content.strip() if content else "[No se pudo extraer contenido]"
    elif fmt == "markdown":
        import trafilatura
        opts = {"include_formatting": True, "include_links": True, "include_images": False, "output_format": "markdown"}
        content = trafilatura.extract(html, **opts)
        if not content:
            content = _fallback_extract(soup, fmt="markdown")
        result["content"] = content.strip() if content else "[No se pudo extraer contenido]"
    else:
        import trafilatura
        content = trafilatura.extract(html, output_format="txt")
        if not content:
            content = _fallback_extract(soup, fmt="text")
        result["content"] = content.strip() if content else "[No se pudo extraer contenido]"

    result["content_length"] = len(result["content"])
    return result

# ── Motor crawl4ai ───────────────────────────────────────────

async def _scrape_crawl4ai(url: str, fmt: str = "markdown", use_browser: bool = False,
                           schema: dict = None) -> dict:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
    from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

    t0 = time.time()
    result = {"url": url, "format": fmt, "error": None, "engine": "crawl4ai"}

    config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        word_count_threshold=10,
        extraction_strategy=None,
        verbose=False,
    )

    if use_browser:
        config.browser_type = "chromium"
        config.headless = True
        config.js_code = None

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

    if fmt == "json" and schema:
        # Extracción estructurada
        try:
            result["data"] = json.loads(crawl_result.extracted_content) if crawl_result.extracted_content else {}
        except (json.JSONDecodeError, TypeError):
            result["data"] = crawl_result.extracted_content or {}
        result["content"] = json.dumps(result["data"], ensure_ascii=False)
    else:
        result["content"] = crawl_result.markdown or crawl_result.fit_markdown or "[No se pudo extraer contenido]"

    result["content_length"] = len(result["content"])
    return result

# ── API pública ──────────────────────────────────────────────

def scrape(url: str, fmt: str = "markdown", use_browser: bool = False,
           schema_str: str = None) -> dict:
    """
    Extrae contenido de una URL.
    Usa crawl4ai si está disponible; fallback a HTTP directo.
    Si fmt=json y hay schema_str, extrae datos estructurados.
    """
    if HAVE_CRAWL4AI and (use_browser or fmt == "json"):
        import asyncio
        schema = _parse_schema(schema_str) if schema_str else None
        return asyncio.run(_scrape_crawl4ai(url, fmt=fmt, use_browser=use_browser, schema=schema))
    return _scrape_http(url, fmt=fmt)

def crawl(url: str, fmt: str = "markdown", depth: int = 1, limit: int = 10,
          quiet: bool = False, use_browser: bool = False) -> list:
    visited = set()
    to_visit = [(url, 0)]
    results = []
    session = _session()

    while to_visit and len(results) < limit:
        current_url, current_depth = to_visit.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)
        if not quiet:
            print(f"  → {current_url}", file=sys.stderr)
        page = scrape(current_url, fmt=fmt, use_browser=use_browser)
        if page.get("error"):
            continue
        results.append(page)
        if current_depth < depth:
            discovered = _discover_urls(session, current_url)
            for u in discovered:
                if u not in visited:
                    to_visit.append((u, current_depth + 1))
    return results

def site_map(url: str, depth: int = 2, limit: int = 50) -> dict:
    session = _session()
    visited = set()
    to_visit = [(url, 0)]
    tree = {url: []}
    while to_visit and len(visited) < limit:
        current_url, current_depth = to_visit.pop(0)
        if current_url in visited:
            continue
        visited.add(current_url)
        discovered = _discover_urls(session, current_url)
        tree[current_url] = discovered
        if current_depth < depth:
            for u in discovered:
                if u not in visited:
                    to_visit.append((u, current_depth + 1))
    return {"root": url, "total_urls": len(visited), "urls": sorted(visited), "tree": tree}

def extract_structured(url: str, schema_str: str, use_browser: bool = False) -> dict:
    """
    Extrae datos estructurados (JSON) de una URL usando un schema CSS.
    El schema_str tiene formato: "h1=title .price=precio div.desc=descripcion"
    """
    if HAVE_CRAWL4AI:
        import asyncio
        return asyncio.run(_scrape_crawl4ai(url, fmt="json", use_browser=use_browser,
                                            schema=_parse_schema(schema_str)))
    # Fallback: scrape normal y devuelve raw
    result = _scrape_http(url, fmt="text")
    result["warning"] = "crawl4ai no instalado, devuelve texto plano. Instala: pip install crawl4ai"
    return result

# ── X/Twitter ──────────────────────────────────────────────────

def x_search(query: str, limit: int = 10) -> list:
    if not HAVE_TWSCRAPE:
        return [{"error": "twscrape no instalado. pip install twscrape"}]
    import asyncio
    return asyncio.run(_x_search_async(query, limit))

async def _x_search_async(query: str, limit: int = 10) -> list:
    api = twscrape.API()
    tweets = []
    async for tweet in api.search(query, limit=limit):
        tweets.append({
            "id": tweet.id,
            "date": str(tweet.date),
            "user": tweet.user.username if tweet.user else "unknown",
            "fullname": tweet.user.displayname if tweet.user else "",
            "content": tweet.rawContent,
            "likes": tweet.likeCount,
            "retweets": tweet.retweetCount,
            "replies": tweet.replyCount,
            "url": tweet.url,
        })
    return tweets

def x_user(username: str, limit: int = 20) -> list:
    if not HAVE_TWSCRAPE:
        return [{"error": "twscrape no instalado"}]
    import asyncio
    return asyncio.run(_x_user_async(username, limit))

async def _x_user_async(username: str, limit: int = 20) -> list:
    api = twscrape.API()
    tweets = []
    async for tweet in api.user_tweets(username, limit=limit):
        tweets.append({
            "id": tweet.id,
            "date": str(tweet.date),
            "content": tweet.rawContent,
            "likes": tweet.likeCount,
            "retweets": tweet.retweetCount,
            "replies": tweet.replyCount,
            "url": tweet.url,
        })
    return tweets

# ── LinkedIn ──────────────────────────────────────────────────

def linkedin_profile(url: str, email: str = None, password: str = None) -> dict:
    if not HAVE_LINKEDIN:
        return {"error": "linkedin-scraper no instalado"}
    email = email or os.environ.get("LINKEDIN_EMAIL", "")
    password = password or os.environ.get("LINKEDIN_PASS", "")
    if not email or not password:
        return {"error": "Se necesita LINKEDIN_EMAIL y LINKEDIN_PASS en .env"}
    person = Person(url, email=email, password=password)
    return {
        "name": person.name,
        "about": person.about,
        "headline": person.headline,
        "location": person.location,
        "company": person.company,
        "job_title": person.job_title,
        "experiences": [{"title": e.position, "company": e.company, "duration": f"{e.from_date} - {e.to_date}"} for e in (person.experiences or [])],
        "education": [{"school": e.institution, "degree": e.degree} for e in (person.educations or [])],
    }

# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hermes Scraper v2 — alternativa local a Firecrawl con JSON, X/Twitter y LinkedIn",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python scraper.py scrape https://ejemplo.com
  python scraper.py scrape https://ejemplo.com --format text
  python scraper.py scrape https://ejemplo.com --format json
  python scraper.py scrape https://ejemplo.com --browser        # JS rendering
  python scraper.py extract https://ejemplo.com --schema "article: h2=title .price=precio"
  python scraper.py crawl https://ejemplo.com --depth 2 --limit 20
  python scraper.py map https://ejemplo.com --depth 1
  python scraper.py xsearch "Venezuela France" --limit 5
  python scraper.py xuser @Gustavo34965333 --limit 10
  python scraper.py linkedin https://linkedin.com/in/perfil
        """
    )
    parser.add_argument("mode", choices=["scrape", "crawl", "map", "extract",
                                          "xsearch", "xuser", "linkedin"],
                        help="Modo de operación")
    parser.add_argument("url", nargs="?", help="URL (para scrape/crawl/map/extract/linkedin)")
    parser.add_argument("--format", choices=["markdown", "text", "json"], default="markdown",
                        help="Formato de salida (default: markdown)")
    parser.add_argument("--schema", help="Schema CSS para extract. Formato: 'baseSelector: selector=nombre ...' "
                        "Ej: 'article.product_pod: h3 a=title .price_color=price'. "
                        "O JSON: '{\"baseSelector\":\"tr\",\"fields\":[{\"name\":\"x\",\"selector\":\"td\",\"type\":\"text\"}]}'")
    parser.add_argument("--depth", type=int, default=1,
                        help="Profundidad para crawl/map (default: 1)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Máx resultados/páginas (default: 10)")
    parser.add_argument("--browser", action="store_true",
                        help="Usar navegador real (Playwright) para JS pesado")
    parser.add_argument("--output", "-o", help="Guardar a archivo en vez de stdout")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Solo contenido, sin metadatos")
    parser.add_argument("--pretty", action="store_true",
                        help="JSON formateado con indentación")

    args = parser.parse_args()

    output = None
    if args.output:
        output = open(args.output, "w", encoding="utf-8")

    try:
        if args.mode == "extract":
            if not args.schema:
                print("ERROR: extract requiere --schema", file=sys.stderr)
                sys.exit(1)
            result = extract_structured(args.url, args.schema, use_browser=args.browser)
            _emit(result, quiet=args.quiet, pretty=args.pretty, fp=output)

        elif args.mode == "scrape":
            if not args.url:
                print("ERROR: scrape requiere URL", file=sys.stderr)
                sys.exit(1)
            result = scrape(args.url, fmt=args.format, use_browser=args.browser, schema_str=args.schema)
            _emit(result, quiet=args.quiet, pretty=args.pretty, fp=output)

        elif args.mode == "crawl":
            if not args.url:
                print("ERROR: crawl requiere URL", file=sys.stderr)
                sys.exit(1)
            results = crawl(args.url, fmt=args.format, depth=args.depth,
                            limit=args.limit, quiet=args.quiet, use_browser=args.browser)
            if output:
                json.dump(results, output, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Guardado → {args.output} ({len(results)} páginas)")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.mode == "map":
            if not args.url:
                print("ERROR: map requiere URL", file=sys.stderr)
                sys.exit(1)
            result = site_map(args.url, depth=args.depth, limit=args.limit)
            if output:
                json.dump(result, output, ensure_ascii=False, indent=2 if args.pretty else None)
                print(f"Guardado → {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.mode == "xsearch":
            query = args.url or input("Búsqueda en X: ")
            results = x_search(query, limit=args.limit)
            if output:
                json.dump(results, output, ensure_ascii=False, indent=2)
                print(f"Guardado → {args.output} ({len(results)} tweets)")
            elif args.quiet:
                for t in results:
                    print(f"[{t.get('date','')[:10]}] @{t.get('user','')}: {t.get('content','')[:200]}")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.mode == "xuser":
            username = args.url or input("Usuario de X: ")
            username = username.lstrip("@")
            results = x_user(username, limit=args.limit)
            if output:
                json.dump(results, output, ensure_ascii=False, indent=2)
                print(f"Guardado → {args.output} ({len(results)} tweets)")
            elif args.quiet:
                for t in results:
                    print(f"[{t.get('date','')[:10]}] {t.get('content','')[:200]}")
            else:
                print(json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None))

        elif args.mode == "linkedin":
            if not args.url:
                print("ERROR: linkedin requiere URL del perfil", file=sys.stderr)
                sys.exit(1)
            result = linkedin_profile(args.url)
            if output:
                json.dump(result, output, ensure_ascii=False, indent=2)
                print(f"Guardado → {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))

    finally:
        if output:
            output.close()

def _emit(result: dict, quiet: bool = False, pretty: bool = False, fp=None):
    if quiet:
        content = result.get("data") or result.get("content", "")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False, indent=2 if pretty else None)
        if fp:
            fp.write(content)
        else:
            print(content)
    else:
        j = json.dumps(result, ensure_ascii=False, indent=2 if pretty else None)
        if fp:
            fp.write(j)
            print(f"Guardado → {fp.name}")
        else:
            print(j)

if __name__ == "__main__":
    main()
