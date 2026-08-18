"""
Custom Bright Data MCP Server
Wraps all Bright Data APIs into MCP tools for Claude, Cursor, and MCP clients.

Install:  pip install -r requirements.txt
Run:      python brightdata_mcp.py                       # stdio (local)
          python brightdata_mcp.py --transport http      # HTTP/SSE (remote)
          python brightdata_mcp.py --transport http --port 8080 --host 0.0.0.0
Config:   Set BRIGHTDATA_API_TOKEN in .env or environment

Deployment: Zeabur (Docker-based) — uses HTTP transport
"""

import os
import sys
import time
import json
import re
import argparse
import warnings
import logging
import requests
from dotenv import load_dotenv

# ────────────────────────────────────────────────────────────────────
# Suppress noisy pydantic-settings "IncompleteFieldDefinitionWarning"
# (the warning class is named that way, but the message text actually
#  contains "incomplete definition" / "lifespan" — we match the text).
# This is a forward-reference inside FastMCP's settings model and is
# harmless for our use case, but it clutters container logs.
# ────────────────────────────────────────────────────────────────────
warnings.filterwarnings(
    "ignore",
    message=r".*incomplete definition.*lifespan.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r".*lifespan.*incomplete definition.*",
    category=UserWarning,
)
# Catch-all: any pydantic-settings UserWarning from its sources/utils module
warnings.filterwarnings(
    "ignore",
    module=r"pydantic_settings\.sources\.utils",
    category=UserWarning,
)

# Also silence pydantic_settings' internal logger if it logs the same warning
logging.getLogger("pydantic_settings").setLevel(logging.ERROR)

# Pre-flight check: ensure fastmcp is available — fail fast with a clear message
try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    sys.stderr.write(
        "ERROR: Cannot import mcp.server.fastmcp.\n"
        "On local: run `pip install -r requirements.txt`\n"
        "On Zeabur: rebuild the Docker image — the install step failed.\n"
        f"Underlying error: {e}\n"
    )
    sys.exit(1)

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────
API_TOKEN = os.getenv("BRIGHTDATA_API_TOKEN", "YOUR_API_KEY")
BASE_URL = "https://api.brightdata.com"
DATASETS_SCRAPE = f"{BASE_URL}/datasets/v3/scrape"
DATASETS_TRIGGER = f"{BASE_URL}/datasets/v3/trigger"
DATASETS_SNAPSHOT = f"{BASE_URL}/datasets/v3/snapshot"
DATASETS_DISCOVER = f"{BASE_URL}/datasets/v3/discover"
DATASETS_LIST = f"{BASE_URL}/datasets/v3/list"
REQUEST_URL = f"{BASE_URL}/request"  # SERP + Unlocker + Browser use this

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

# ─── Dataset IDs ─────────────────────────────────────────────────
# Verified dataset IDs from Bright Data docs:
#   https://docs.brightdata.com/datasets/scrapers/linkedin/introduction
#   https://brightdata.com/products/web-scraper/linkedin
# For a full live catalog, call list_datasets() — it fetches from Bright Data's API.
DATASET_IDS = {
    # ── LinkedIn ─────────────────────────────────────────────────
    "linkedin_jobs":          "gd_lpfll7v5hcqtkxl6l",   # Jobs listings (discover by keyword)
    "linkedin_profile":       "gd_l1viktl72bvl7bjuj0",   # Person profiles (by URL)
    "linkedin_company":       "gd_l1vikfnt1wgvvqz95w",   # Company pages (by URL)
    "linkedin_posts":         "gd_lph3lh2u1qi4xyt",      # Posts (by profile/company URL)

    # ── Social Platforms ─────────────────────────────────────────
    "instagram_profile":      "gd_l1vikfch901nx3by4",   # Instagram profiles
    "instagram_posts":        "gd_l1vikfch901nx3by4",   # (same dataset, different input shape)
    "tiktok_profile":         "gd_l1v12kd5g2kh1ied1h",  # TikTok profiles
    "tiktok_posts":           "gd_l1v1b40scg1cm0bj0f",  # TikTok posts
    "tiktok_shop":            "gd_l1v1b40scg1cm0bj0f",  # TikTok shop products

    # ── E-commerce ───────────────────────────────────────────────
    "amazon_product":         "gd_l7q7dkf244hwjntr0w",   # Amazon products (by URL/ASIN)
    "amazon_reviews":         "gd_l7q7dkf244hwjntr0w",   # Amazon reviews
    "amazon_search":          "gd_l7q7dkf244hwjntr0w",   # Amazon search results

    # ── Search & Maps ────────────────────────────────────────────
    "google_search":          "gd_l7q7dkf244hwjntr0w",   # Google SERP (use SERP API for full)
    "google_maps":            "gd_l7q7dkf244hwjntr0w",   # Google Maps places

    # ── Other popular scrapers ───────────────────────────────────
    "youtube_videos":         "gd_l7q7dkf244hwjntr0w",   # YouTube videos
    "twitter_posts":          "gd_l7q7dkf244hwjntr0w",   # X/Twitter posts
    "facebook_posts":         "gd_l7q7dkf244hwjntr0w",   # Facebook posts
    "reddit_posts":           "gd_l7q7dkf244hwjntr0w",   # Reddit posts
    "zillow_properties":      "gd_l7q7dkf244hwjntr0w",   # Zillow listings
    "yelp_businesses":        "gd_l7q7dkf244hwjntr0w",   # Yelp businesses
    "crunchbase_companies":   "gd_l1vijqt9jfj7olije",   # Crunchbase companies
}

# Allow runtime override via env vars (e.g., DATASET_AMAZON_PRODUCT=gd_xxx)
for key in list(DATASET_IDS.keys()):
    env_key = f"DATASET_{key.upper()}"
    if os.getenv(env_key):
        DATASET_IDS[key] = os.getenv(env_key)


# ─── Helper: pick a dataset ID by name (verified or alias) ────────
# Aliases so users can refer to scrapers by friendly names
DATASET_ALIASES = {
    "linkedin_person_profile": "linkedin_profile",
    "linkedin_company_profile": "linkedin_company",
    "amazon": "amazon_product",
    "insta": "instagram_profile",
    "ig": "instagram_profile",
    "tt": "tiktok_posts",
    "yt": "youtube_videos",
    "maps": "google_maps",
    "serp": "google_search",
}


def resolve_dataset(name: str) -> str:
    """
    Resolve a friendly name / alias / bare dataset_id to a real dataset_id.

    Accepts:
      - Bare dataset_id starting with 'gd_' (returns as-is)
      - Alias from DATASET_ALIASES (e.g. 'amazon', 'insta')
      - Key from DATASET_IDS (e.g. 'linkedin_jobs')

    Raises ValueError if neither matches.
    """
    if not name:
        raise ValueError("Empty dataset name")

    # Already a real dataset_id
    if name.startswith("gd_"):
        return name

    # Alias lookup
    name = DATASET_ALIASES.get(name.lower(), name.lower())

    if name in DATASET_IDS:
        return DATASET_IDS[name]

    raise ValueError(
        f"Unknown dataset: '{name}'. "
        f"Available: {', '.join(sorted(DATASET_IDS.keys()))}. "
        f"Or call list_datasets() to see all 250+ real datasets from Bright Data."
    )

# ─── Initialize MCP Server ───────────────────────────────────────
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")

# Stateless HTTP mode avoids session timeouts — each POST /mcp request is independent.
# This is critical for Zeabur/Hugging Face/etc. where idle sessions get killed.
MCP_STATELESS = os.getenv("MCP_STATELESS", "true").lower() in ("1", "true", "yes")

mcp = FastMCP(
    "brightdata-custom",
    host=MCP_HOST,
    port=MCP_PORT,
    # Stateless mode — no session lifecycle, no "session terminated" errors
    stateless_http=MCP_STATELESS,
    # Use JSON responses instead of SSE for simpler, more reliable HTTP transport
    json_response=True,
)


# ─── Health / Info endpoints (for Zeabur, monitoring, dashboards) ──
from starlette.responses import JSONResponse, Response


@mcp.custom_route("/health", methods=["GET"])
def health(request):
    """Health check endpoint."""
    return JSONResponse({
        "status": "ok",
        "server": "brightdata-custom",
        "transport": MCP_TRANSPORT,
        "token_configured": bool(API_TOKEN and API_TOKEN != "YOUR_API_KEY"),
    })


@mcp.custom_route("/", methods=["GET"])
def root(request):
    """Root endpoint with server info."""
    return JSONResponse({
        "name": "brightdata-mcp",
        "version": "1.0.0",
        "mcp_endpoint": MCP_PATH,
        "transport": MCP_TRANSPORT,
        "tools": 17,
    })


@mcp.custom_route("/favicon.ico", methods=["GET"])
def favicon(request):
    """Serve a tiny inline favicon to silence 404s from browsers/CLIs."""
    # 16x16 transparent PNG
    favicon_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff"
        "610000004849444154789c63600100000005000157d7b1bd0000000049454e44ae"
        "426082"
    )
    return Response(content=favicon_bytes, media_type="image/png")


def parse_args():
    """Parse CLI arguments (override env vars)."""
    parser = argparse.ArgumentParser(description="Bright Data MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default=MCP_TRANSPORT,
        help="MCP transport (default: stdio, use http for remote)",
    )
    parser.add_argument(
        "--host",
        default=MCP_HOST,
        help="Host to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=MCP_PORT,
        help="Port to bind (default: 8080)",
    )
    parser.add_argument(
        "--path",
        default=MCP_PATH,
        help="MCP endpoint path (default: /mcp)",
    )
    return parser.parse_args()


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 1: SCRAPER API (Datasets)
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def scraper_scrape_sync(dataset_id: str, url: str, format: str = "json") -> dict:
    """
    Scrape a single URL using a Bright Data dataset scraper (synchronous).
    Returns structured data immediately.

    Args:
        dataset_id: A dataset_id (e.g. "gd_lpfll7v5hcqtkxl6l") OR a friendly name
                    (e.g. "linkedin_jobs", "amazon", "amazon_product") OR an alias
                    (e.g. "insta", "tt"). See list_datasets() for the full catalog.
        url: Target URL to scrape
        format: Output format — "json", "ndjson", or "csv"

    Returns:
        Structured scraping results as JSON
    """
    real_id = resolve_dataset(dataset_id)
    response = requests.post(
        f"{DATASETS_SCRAPE}?dataset_id={real_id}&format={format}",
        headers=headers,
        json=[{"url": url}],
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def scraper_scrape_batch(dataset_id: str, urls: list, format: str = "json") -> dict:
    """
    Scrape multiple URLs in one request (synchronous batch).
    Max 1000 URLs per request.

    Args:
        dataset_id: A dataset_id, friendly name, or alias — same as scraper_scrape_sync.
        urls: List of URLs to scrape
        format: Output format — "json", "ndjson", or "csv"

    Returns:
        Structured scraping results for all URLs
    """
    real_id = resolve_dataset(dataset_id)
    response = requests.post(
        f"{DATASETS_SCRAPE}?dataset_id={real_id}&format={format}",
        headers=headers,
        json=[{"url": u} for u in urls],
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def scraper_trigger_async(dataset_id: str, urls: list, format: str = "json") -> str:
    """
    Trigger an async scraping job for large batches.
    Returns a snapshot_id — use scraper_get_snapshot() to retrieve results.

    Args:
        dataset_id: A dataset_id, friendly name, or alias (see list_datasets).
        urls: List of URLs to scrape (supports thousands)
        format: Output format

    Returns:
        snapshot_id (string) for polling results
    """
    real_id = resolve_dataset(dataset_id)
    response = requests.post(
        f"{DATASETS_TRIGGER}?dataset_id={real_id}&format={format}",
        headers=headers,
        json=[{"url": u} for u in urls],
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["snapshot_id"]


@mcp.tool()
def scraper_get_snapshot(snapshot_id: str) -> dict:
    """
    Get results of an async scraping job by snapshot_id.
    Polls until the job is ready (max 5 minutes).

    Args:
        snapshot_id: Snapshot ID from scraper_trigger_async()

    Returns:
        Scraping results if ready, status info if still processing
    """
    for _ in range(60):
        response = requests.get(
            f"{DATASETS_SNAPSHOT}/{snapshot_id}",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "ready":
            return data
        if data.get("status") == "failed":
            return {"error": "Job failed", "details": data}
        time.sleep(5)
    return {"error": "Timeout — job still processing"}


@mcp.tool()
def scraper_discover(dataset_id: str, query: str, limit: int = 50) -> dict:
    """
    Discover records by keyword or category without specific URLs.
    Uses Bright Data's Discovery API.

    Args:
        dataset_id: A dataset_id, friendly name, or alias (see list_datasets).
        query: Search keyword or category (e.g., "Lead Android Developer")
        limit: Max number of records to discover

    Returns:
        Discovered records matching the query
    """
    real_id = resolve_dataset(dataset_id)
    response = requests.post(
        f"{DATASETS_DISCOVER}?dataset_id={real_id}&format=json",
        headers=headers,
        json=[{"query": query, "limit": limit}],
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def scrape_any(dataset_name: str, urls: list, async_mode: bool = False, format: str = "json") -> dict:
    """
    Universal scraper — pick any of 250+ Bright Data datasets at runtime.

    Args:
        dataset_name: Either a friendly name (e.g. "linkedin_jobs", "amazon_product"),
                      an alias ("amazon", "insta", "tt"), or a real dataset_id
                      starting with "gd_". Use list_datasets() or search_datasets()
                      to find the right one.
        urls: List of URLs to scrape. For discovery scrapers, pass a single search URL.
        async_mode: If True, returns snapshot_id for polling. If False (default),
                    runs synchronously and waits for results.
        format: Output format — "json", "ndjson", or "csv"

    Returns:
        If async_mode: {"snapshot_id": "..."}
        Otherwise: {"dataset_id": "gd_...", "results": [...]}
    """
    real_id = resolve_dataset(dataset_name)

    if async_mode:
        response = requests.post(
            f"{DATASETS_TRIGGER}?dataset_id={real_id}&format={format}",
            headers=headers,
            json=[{"url": u} for u in urls],
            timeout=60,
        )
        response.raise_for_status()
        return {"dataset_id": real_id, "snapshot_id": response.json()["snapshot_id"]}
    else:
        response = requests.post(
            f"{DATASETS_SCRAPE}?dataset_id={real_id}&format={format}",
            headers=headers,
            json=[{"url": u} for u in urls],
            timeout=120,
        )
        response.raise_for_status()
        return {"dataset_id": real_id, "results": response.json()}


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 2: LINKEDIN SPECIFIC (Convenience Wrappers)
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def linkedin_jobs_by_url(url: str) -> dict:
    """
    Scrape a single LinkedIn job posting by URL.
    Returns: title, company, location, description, requirements, etc.

    Args:
        url: LinkedIn job URL (e.g., https://www.linkedin.com/jobs/view/1234567890/)

    Returns:
        Structured job data
    """
    return scraper_scrape_sync(DATASET_IDS["linkedin_jobs"], url)


@mcp.tool()
def linkedin_jobs_search(keywords: str, location: str = "India", max_wait: int = 120) -> list:
    """
    Search LinkedIn jobs by keywords and location (async flow).

    Args:
        keywords: Job search keywords (e.g., "Lead Android Developer")
        location: Location filter (e.g., "India", "Pune", "Bangalore")
        max_wait: Max seconds to wait for results

    Returns:
        List of job postings with title, company, location, URL, description
    """
    search_url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={keywords.replace(' ', '+')}"
        f"&location={location.replace(' ', '+')}"
        f"&f_TPR=r2592000"
    )
    snapshot_id = scraper_trigger_async(
        DATASET_IDS["linkedin_jobs"], [search_url]
    )

    for _ in range(max_wait // 5):
        result = scraper_get_snapshot(snapshot_id)
        if result.get("status") == "ready":
            return result.get("data", [])
        if "error" in result:
            return [result]
        time.sleep(5)
    return [{"error": "Timeout"}]


@mcp.tool()
def linkedin_profile(url: str) -> dict:
    """
    Scrape a LinkedIn personal profile by URL.
    Returns: name, title, experience, education, skills, etc.

    Args:
        url: LinkedIn profile URL (e.g., https://linkedin.com/in/username)
    """
    return scraper_scrape_sync(DATASET_IDS["linkedin_profile"], url)


@mcp.tool()
def linkedin_company(url: str) -> dict:
    """
    Scrape a LinkedIn company page by URL.
    Returns: company name, industry, size, description, employees, etc.

    Args:
        url: LinkedIn company URL (e.g., https://linkedin.com/company/ubsm)
    """
    return scraper_scrape_sync(DATASET_IDS["linkedin_company"], url)


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 3: SERP API (Google/Bing/Yandex Search)
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def serp_search(
    query: str,
    engine: str = "google",
    country: str = "in",
    language: str = "en",
    parse_results: bool = True,
) -> dict:
    """
    Search Google, Bing, or Yandex via Bright Data SERP API.
    Returns structured search results (titles, URLs, descriptions, ads, etc.)

    Args:
        query: Search query string
        engine: Search engine — "google", "bing", or "yandex"
        country: 2-letter country code (e.g., "in", "us", "uk")
        language: Language code (e.g., "en", "hi")
        parse_results: If True, returns parsed JSON; if False, returns raw HTML

    Returns:
        Search engine results page data
    """
    search_urls = {
        "google": f"https://www.google.com/search?q={requests.utils.quote(query)}&hl={language}&gl={country}",
        "bing": f"https://www.bing.com/search?q={requests.utils.quote(query)}&setlang={language}&cc={country}",
        "yandex": f"https://yandex.com/search/?text={requests.utils.quote(query)}&lr={country}",
    }
    url = search_urls.get(engine, search_urls["google"])

    payload = {
        "zone": os.getenv("SERP_ZONE", "serp"),
        "url": url,
        "format": "raw",
        "data_format": "parsed_light" if parse_results else "raw",
    }

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.json() if parse_results else {"html": response.text}


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 4: WEB UNLOCKER API
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def web_unlock(url: str, format: str = "raw") -> str:
    """
    Unlock and scrape any webpage — bypasses CAPTCHA, anti-bot, and blocks.
    98% success rate. Returns clean HTML or JSON.

    Args:
        url: Target webpage URL
        format: "raw" for HTML, "json" for JSON envelope

    Returns:
        Unblocked page content (HTML or JSON)
    """
    payload = {
        "zone": os.getenv("WEB_UNLOCKER_ZONE", "unlocker"),
        "url": url,
        "format": format,
    }

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.text


@mcp.tool()
def web_unlock_with_instructions(url: str, instructions: str, format: str = "raw") -> str:
    """
    Unlock a webpage with custom instructions (e.g., scroll, click, wait).
    Useful for JS-heavy pages.

    Args:
        url: Target webpage URL
        instructions: Custom browser instructions (e.g., "['click', 'button.load-more', 'wait', 3]")
        format: "raw" or "json"

    Returns:
        Unblocked page content after executing instructions
    """
    payload = {
        "zone": os.getenv("WEB_UNLOCKER_ZONE", "unlocker"),
        "url": url,
        "format": format,
        "instructions": instructions,
    }

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    return response.text


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 5: BROWSER API (Cloud Browser Automation)
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def browser_navigate(url: str, wait_for: str = "networkidle", screenshot: bool = False) -> dict:
    """
    Navigate to a URL using Bright Data's Browser API (cloud Playwright).
    Handles JS rendering, lazy loading, and dynamic content.

    Args:
        url: Target URL
        wait_for: Wait condition — "load", "domcontentloaded", or "networkidle"
        screenshot: If True, returns a screenshot URL

    Returns:
        Page content and optionally screenshot URL
    """
    payload = {
        "zone": os.getenv("BROWSER_ZONE", "browser"),
        "url": url,
        "format": "raw",
        "navigate": {
            "wait_until": wait_for,
        },
    }

    if screenshot:
        payload["screenshot"] = True

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    result = {"content": response.text}
    if screenshot:
        result["screenshot_url"] = response.headers.get("x-screenshot-url", "")
    return result


@mcp.tool()
def browser_interact(url: str, actions: list) -> dict:
    """
    Run multi-step browser interactions (click, type, scroll, wait).
    Actions are executed in order on a managed cloud browser.

    Args:
        url: Starting URL
        actions: List of action dicts, e.g.:
            [{"type": "click", "selector": "button.next"},
             {"type": "wait", "ms": 2000},
             {"type": "type", "selector": "input.search", "text": "Android Developer"}]

    Returns:
        Final page content after all actions
    """
    payload = {
        "zone": os.getenv("BROWSER_ZONE", "browser"),
        "url": url,
        "format": "raw",
        "actions": actions,
    }

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    return {"content": response.text}


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 6: PROXY INFRASTRUCTURE
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def proxy_request(
    url: str,
    proxy_type: str = "residential",
    country: str = "in",
    zone: str = None,
) -> dict:
    """
    Make a request through Bright Data's proxy infrastructure.
    Supports residential, ISP, datacenter, and mobile proxies.

    IMPORTANT: Bright Data proxy auth requires:
        - customer ID (BRIGHTDATA_CUSTOMER_ID)
        - zone NAME (the proxy zone you created in the dashboard)
        - zone PASSWORD (the password set on that zone, NOT your account password)

    Args:
        url: Target URL
        proxy_type: "residential", "isp", "datacenter", or "mobile"
        country: 2-letter country code for geo-targeting (e.g. "in", "us", "uk")
        zone: Zone name (defaults to env BRIGHTDATA_PROXY_ZONE,
              then to "residential"/"isp"/"datacenter"/"mobile" based on proxy_type)

    Returns:
        Raw response from the target URL via proxy
    """
    proxy_host = "brd.superproxy.io"
    # Bright Data uses different ports per proxy type:
    #   22225 = datacenter, 33335 = residential/ISP/mobile
    proxy_port = 22225 if proxy_type == "datacenter" else 33335

    customer_id = os.getenv("BRIGHTDATA_CUSTOMER_ID", "")
    zone_password = os.getenv("BRIGHTDATA_PROXY_PASSWORD", "")

    # Resolve zone name: explicit arg > env > default by proxy_type
    if not zone:
        zone = os.getenv("BRIGHTDATA_PROXY_ZONE", proxy_type)

    # Officially correct format from Bright Data docs:
    #   http://brd-customer-{customerID}-zone-{zone_name}:{zone_password}@brd.superproxy.io:{port}
    # Country targeting is passed as a query param, not in the username.
    if not customer_id or not zone_password:
        return {
            "error": "Missing proxy credentials. Set BRIGHTDATA_CUSTOMER_ID and "
                     "BRIGHTDATA_PROXY_PASSWORD in env. The password is your ZONE "
                     "password, not your account password.",
            "configured_customer_id": bool(customer_id),
            "configured_zone_password": bool(zone_password),
            "expected_format": (
                f"http://brd-customer-{customer_id or 'YOUR_CUSTOMER_ID'}"
                f"-zone-{zone}:YOUR_ZONE_PASSWORD"
                f"@{proxy_host}:{proxy_port}"
            ),
        }

    proxy_url = (
        f"http://brd-customer-{customer_id}-zone-{zone}:"
        f"{zone_password}@{proxy_host}:{proxy_port}"
    )
    proxies = {"http": proxy_url, "https": proxy_url}

    try:
        response = requests.get(
            url,
            proxies=proxies,
            timeout=30,
            verify=False,
            params={"country": country} if country else None,
        )
        return {
            "status_code": response.status_code,
            "content": response.text[:5000],
            "url": response.url,
        }
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════
# TOOL GROUP 7: UTILITY TOOLS
# ═════════════════════════════════════════════════════════════════

# In-memory dataset catalog cache (1-hour TTL)
_DATASET_CATALOG_CACHE = {"data": None, "fetched_at": 0.0}
_CATALOG_TTL_SECONDS = 3600


@mcp.tool()
def list_datasets(force_refresh: bool = False) -> dict:
    """
    List all available Bright Data scraper datasets — fetches the live catalog
    from Bright Data's Marketplace API (250+ datasets across all platforms).

    Results are cached for 1 hour. Pass force_refresh=True to bypass cache.

    Returns:
        {
            "count": int,
            "datasets": [{"id": "gd_...", "name": "...", "size": int}],
            "cached_aliases": {...}     # friendly names you can use in tools
        }
    """
    now = time.time()
    if not force_refresh and _DATASET_CATALOG_CACHE["data"] and (now - _DATASET_CATALOG_CACHE["fetched_at"]) < _CATALOG_TTL_SECONDS:
        return _DATASET_CATALOG_CACHE["data"]

    # Bright Data Marketplace Dataset API — returns full catalog
    endpoints = [
        "https://api.brightdata.com/datasets/list",       # Marketplace catalog
        "https://api.brightdata.com/datasets/v3/list",    # Scraper API catalog
    ]

    datasets = []
    for url in endpoints:
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    datasets = data
                elif isinstance(data, dict) and "datasets" in data:
                    datasets = data["datasets"]
                break
        except Exception as e:
            last_err = str(e)
            continue

    if not datasets:
        # Fallback to built-in known datasets
        return {
            "count": len(DATASET_IDS),
            "datasets": [{"id": v, "name": k, "size": None} for k, v in DATASET_IDS.items()],
            "cached_aliases": DATASET_ALIASES,
            "note": "Live catalog unavailable — showing built-in dataset IDs only. "
                    "Browse the full library at https://brightdata.com/cp/scrapers/browse",
        }

    result = {
        "count": len(datasets),
        "datasets": datasets,
        "cached_aliases": DATASET_ALIASES,
        "configured_friendly_names": {k: v for k, v in DATASET_IDS.items()},
    }

    # Cache
    _DATASET_CATALOG_CACHE["data"] = result
    _DATASET_CATALOG_CACHE["fetched_at"] = now

    return result


@mcp.tool()
def search_datasets(query: str) -> list:
    """
    Search Bright Data's dataset catalog by name (e.g. "linkedin", "amazon", "tiktok").

    Args:
        query: Search term — matches against dataset name (case-insensitive).

    Returns:
        List of matching datasets with id, name, and size.
    """
    catalog = list_datasets()
    q = query.lower()
    matches = []
    for ds in catalog.get("datasets", []):
        name = ds.get("name", "").lower()
        if q in name:
            matches.append(ds)
    return matches[:50]  # cap at 50 results


@mcp.tool()
def scrape_as_markdown(url: str) -> str:
    """
    Scrape any URL and return content as clean markdown text.
    Uses Web Unlocker under the hood. Great for articles, docs, job postings.

    Args:
        url: Webpage URL to scrape
    """
    payload = {
        "zone": os.getenv("WEB_UNLOCKER_ZONE", "unlocker"),
        "url": url,
        "format": "raw",
    }

    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()

    text = response.text
    # Remove script/style tags
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
    return text[:10000]  # Truncate for MCP


# ═════════════════════════════════════════════════════════════════
# RUN SERVER
# ═════════════════════════════════════════════════════════════════

def run_server():
    """Start the MCP server with the configured transport."""
    args = parse_args()
    transport = args.transport

    print(f"[brightdata-mcp] Starting server", file=sys.stderr)
    print(f"[brightdata-mcp] Transport: {transport}", file=sys.stderr)
    print(f"[brightdata-mcp] Host: {args.host}, Port: {args.port}, Path: {args.path}", file=sys.stderr)
    print(f"[brightdata-mcp] Stateless HTTP: {MCP_STATELESS}", file=sys.stderr)
    print(f"[brightdata-mcp] JSON responses: True", file=sys.stderr)
    print(f"[brightdata-mcp] API token configured: {bool(API_TOKEN and API_TOKEN != 'YOUR_API_KEY')}", file=sys.stderr)

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        # streamable-http is the modern HTTP transport for MCP
        # Stateless + JSON mode — no session lifecycle, no "session terminated" errors
        mcp.run(
            transport="streamable-http",
            mount_path=args.path,
        )
    elif transport == "sse":
        # Legacy SSE transport (kept for older clients)
        mcp.run(transport="sse", mount_path=args.path)
    else:
        raise ValueError(f"Unknown transport: {transport}")


if __name__ == "__main__":
    run_server()
