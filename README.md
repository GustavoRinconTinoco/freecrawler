# Freecrawler 🔥

**Alternativa 100% local, gratuita y sin límites a Firecrawl.**

Freecrawler es un scraper todo-en-uno para AI agents. Extrae contenido web, genera JSON estructurado, busca en X/Twitter, y navega la web autónomamente — todo sin API keys, sin créditos, sin depender de servicios externos.

Hecho por y para AI agents que necesitan scrapear sin que los cobren por página.

## Características

### 🕷️ Scraping Web
- **Modo scrape**: extrae contenido limpio de cualquier URL en Markdown, texto o JSON
- **Modo crawl**: descubre y extrae múltiples páginas del mismo sitio con profundidad configurable
- **Modo map**: construye un sitemap de todas las URLs internas
- **JS rendering**: opcional con Playwright para SPAs y sitios con JavaScript pesado

### 📊 JSON Estructurado (sin LLM)
Usa `crawl4ai` con schemas CSS para extraer datos estructurados sin necesidad de un modelo de lenguaje:

```bash
python freecrawler.py extract https://books.toscrape.com \
  --schema "article.product_pod: h3 a=title .price_color=price"
```

### 🐦 X/Twitter
- Búsqueda de tweets por keywords
- Extracción de timeline de usuarios  
- Sin API key de X — usa twscrape con GraphQL

### 🧭 Navegación Autónoma
- browser-use integrado para tareas de navegación web autónoma
- El agente IA decide qué hacer: buscar, hacer clic, llenar formularios, extraer datos

## Instalación

```bash
# Requisitos básicos
pip install requests beautifulsoup4 lxml trafilatura html2text markdownify

# Para funcionalidad completa
pip install crawl4ai twscrape playwright "browser-use[core]" --user
python -m playwright install chromium
```

## Uso Rápido

```bash
# Extraer una URL
python freecrawler.py scrape https://ejemplo.com

# En texto plano
python freecrawler.py scrape https://ejemplo.com --format text

# Crawl
python freecrawler.py crawl https://ejemplo.com --depth 2 --limit 20

# Mapa del sitio
python freecrawler.py map https://ejemplo.com

# JSON estructurado
python freecrawler.py extract https://books.toscrape.com \
  --schema "article.product_pod: h3 a=title .price_color=price"

# Buscar en X
python freecrawler.py xsearch "inteligencia artificial" --limit 10

# Tweets de un usuario
python freecrawler.py xuser @usuario

# Navegación autónoma
python freecrawler.py browse "Busca el precio del bitcoin"
```

## Comparativa con Firecrawl

| Característica | Firecrawl | Freecrawler |
|---|---|---|
| Costo | Por crédito/página | **Gratis** |
| API Key | Requerida | **No necesita** |
| JS Rendering | Automático | Con `--browser` |
| Markdown output | ✅ | ✅ |
| Crawl | ✅ | ✅ |
| Map (sitemap) | ✅ | ✅ |
| JSON estructurado | ✅ (con schema) | ✅ (con schema CSS) |
| X/Twitter scraping | ❌ | ✅ |
| Navegación autónoma | ❌ | ✅ (browser-use) |
| Límite de uso | Según plan | **Ilimitado** |
| Dependencias | Servidor remoto | **100% local** |
| Disponibilidad | Si el servicio cae, no | **Siempre funciona** |

## Arquitectura

Freecrawler tiene 3 motores intercambiables:

1. **HTTP Direct** (requests + trafilatura): rápido, para HTML estático
2. **crawl4ai**: JS rendering, JSON extraction por schema CSS, crawling avanzado
3. **browser-use**: navegación autónoma con IA

Selecciona automáticamente el motor según el modo y flags.

## Licencia

MIT — haz lo que quieras.
