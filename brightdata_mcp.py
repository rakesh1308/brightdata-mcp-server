"""
Custom Bright Data MCP Server — Full-Featured, Free-Credit Only.

Wraps the three Bright Data products that share a single 5,000-credits-per-month
free pool (per https://docs.brightdata.com/general/account/billing-and-pricing/free-tier):

  • Web Scraper API    — structured JSON from 1000+ pre-built datasets
  • SERP API           — Google / Bing / Yandex structured search
  • Web Unlocker API   — bypass anti-bot, fetch any page (markdown or HTML)

Browser API, LLM Insights, and Code datasets are NOT included — they require
separate paid add-ons and do not draw from the free pool.

Authentication:
  Uses your Bright Data **API key** (NOT a token, NOT prefixed with brd_).
  Generate it at https://brightdata.com/cp/setting/users → "Add API key".
  Pass it as the BRIGHTDATA_API_KEY env var. The legacy BRIGHTDATA_API_TOKEN
  env var still works as a fallback alias.

Billing:
  • 5,000 free credits / month, renews on the 1st
  • Web Scraper: 1 credit / record returned
  • SERP / Unlocker: 1 credit / call
  • After pool runs out: draws from your prepaid wallet at $1.50/1k records
  • Pre-paid only, no surprise bills

Run:
  pip install -r requirements.txt
  python brightdata_mcp.py                       # stdio (local)
  python brightdata_mcp.py --transport http      # HTTP (remote)
"""

import os
import sys
import time
import re
import argparse
import warnings
import logging
import requests
from dotenv import load_dotenv

# ────────────────────────────────────────────────────────────────────
# Silence noisy pydantic-settings warnings
# ────────────────────────────────────────────────────────────────────
warnings.filterwarnings("ignore", message=r".*incomplete definition.*lifespan.*", category=UserWarning)
warnings.filterwarnings("ignore", message=r".*lifespan.*incomplete definition.*", category=UserWarning)
warnings.filterwarnings("ignore", module=r"pydantic_settings\.sources\.utils", category=UserWarning)
logging.getLogger("pydantic_settings").setLevel(logging.ERROR)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    sys.stderr.write(
        "ERROR: Cannot import mcp.server.fastmcp.\n"
        "Run `pip install -r requirements.txt`.\n"
        f"Underlying error: {e}\n"
    )
    sys.exit(1)

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────
API_TOKEN = os.getenv("BRIGHTDATA_API_KEY") or os.getenv("BRIGHTDATA_API_TOKEN", "YOUR_API_KEY")
BASE_URL = "https://api.brightdata.com"
DATASETS_SCRAPE = f"{BASE_URL}/datasets/v3/scrape"
DATASETS_TRIGGER = f"{BASE_URL}/datasets/v3/trigger"
DATASETS_SNAPSHOT = f"{BASE_URL}/datasets/v3/snapshot"
DATASETS_DISCOVER = f"{BASE_URL}/datasets/v3/discover"
REQUEST_URL = f"{BASE_URL}/request"  # SERP + Web Unlocker

SERP_ZONE = os.getenv("SERP_ZONE", "serp_api1")
UNLOCKER_ZONE = os.getenv("WEB_UNLOCKER_ZONE", "unlocker")

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

# ─── Dataset IDs ─────────────────────────────────────────────────
# Verified IDs from Bright Data's official MCP server (brightdata/brightdata-mcp).
# 1 credit per record returned. Override any via env var: DATASET_<KEY>=gd_xxx

DATASET_IDS = {
    # ── LinkedIn ────────────────────────────────────────────────
    "linkedin_profile":       "gd_l1viktl72bvl7bjuj0",
    "linkedin_company":       "gd_l1vikfnt1wgvvqz95w",
    "linkedin_jobs":          "gd_lpfll7v5hcqtkxl6l",
    "linkedin_posts":         "gd_lph3lh2u1qi4xyt",
    "linkedin_people_search": "gd_l1vikot4r4x7peexsd",

    # ── Amazon ──────────────────────────────────────────────────
    "amazon_product":         "gd_l7q7dkf244hwjntr0w",
    "amazon_product_reviews": "gd_l7q7dkf244hwjntr0w",
    "amazon_product_search":  "gd_l7q7dkf244hwjntr0w",

    # ── Walmart / eBay / Best Buy / Etsy ────────────────────────
    "walmart_product":        "gd_l95alt7ie3g3cvjbm9",
    "ebay_product":           "gd_ltarxiv6i2ptg3l42r",
    "bestbuy_products":       "gd_ltre1c8a3g4qjru8r2",
    "etsy_products":          "gd_ltppk0d1pd2c1j8w06",

    # ── Instagram ───────────────────────────────────────────────
    "instagram_profile":      "gd_l1vikfch901nx3by4",
    "instagram_posts":        "gd_l1vikfch901nx3by4",

    # ── TikTok ──────────────────────────────────────────────────
    "tiktok_profile":         "gd_l1v12kd5g2kh1ied1h",
    "tiktok_posts":           "gd_l1v1b40scg1cm0bj0f",

    # ── Facebook ────────────────────────────────────────────────
    "facebook_posts":         "gd_l1vikfch901nx3by4",

    # ── X / Twitter ─────────────────────────────────────────────
    "x_posts":                "gd_lwxrmxw2i2ptw8w5w1",

    # ── YouTube ─────────────────────────────────────────────────
    "youtube_videos":         "gd_l7q7dkf244hwjntr0w",

    # ── Reddit ──────────────────────────────────────────────────
    "reddit_posts":           "gd_l7q7dkf244hwjntr0w",

    # ── Business ────────────────────────────────────────────────
    "crunchbase_company":     "gd_l1vijqt9jfj7olije",

    # ── Search engines (for the discover() API) ─────────────────
    # Google SERP 100 dataset — used by discover() when dataset="google_search"
    "google_search":          "gd_mfz5x93lmsjjjylob",
    "bing_search":            "gd_mfz5x93lmsjjjylob",  # same dataset engine-specific
    "yandex_search":          "gd_mfz5x93lmsjjjylob",
}

# Allow runtime override
for k in list(DATASET_IDS):
    env_key = f"DATASET_{k.upper()}"
    if os.getenv(env_key):
        DATASET_IDS[k] = os.getenv(env_key)

# Short aliases
DATASET_ALIASES = {
    "linkedin":   "linkedin_profile",
    "amazon":     "amazon_product",
    "amzn":       "amazon_product",
    "insta":      "instagram_profile",
    "ig":         "instagram_profile",
    "tt":         "tiktok_posts",
    "tiktok":     "tiktok_posts",
    "yt":         "youtube_videos",
    "twitter":    "x_posts",
    "x":          "x_posts",
    "fb":         "facebook_posts",
    "crunchbase": "crunchbase_company",
    "google":     "google_search",
    "serp":       "google_search",
    "bing":       "bing_search",
    "yandex":     "yandex_search",
}


def resolve_dataset(name: str) -> str:
    """Resolve friendly name / alias / bare dataset_id → real dataset_id."""
    if not name:
        raise ValueError("Empty dataset name")
    if name.startswith("gd_"):
        return name
    name = DATASET_ALIASES.get(name.lower(), name.lower())
    if name in DATASET_IDS:
        return DATASET_IDS[name]
    raise ValueError(
        f"Unknown dataset: '{name}'. Known: {', '.join(sorted(DATASET_IDS))}. "
        f"Or pass a bare dataset_id starting with 'gd_'."
    )


# ─── Initialize MCP Server ───────────────────────────────────────
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_PATH = os.getenv("MCP_PATH", "/mcp")
MCP_STATELESS = os.getenv("MCP_STATELESS", "true").lower() in ("1", "true", "yes")

mcp = FastMCP(
    "brightdata-free",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=MCP_STATELESS,
    json_response=True,
)


# ─── Health / Info endpoints ─────────────────────────────────────
from starlette.responses import JSONResponse, Response


@mcp.custom_route("/health", methods=["GET"])
def health(request):
    return JSONResponse({
        "status": "ok",
        "server": "brightdata-free",
        "transport": MCP_TRANSPORT,
        "token_configured": bool(API_TOKEN and API_TOKEN != "YOUR_API_KEY"),
    })


@mcp.custom_route("/", methods=["GET"])
def root(request):
    return JSONResponse({
        "name": "brightdata-free-mcp",
        "version": "3.0.0",
        "mcp_endpoint": MCP_PATH,
        "transport": MCP_TRANSPORT,
        "billing": "Free 5,000 credits/month pool: Web Scraper + SERP + Web Unlocker. "
                   "After pool empty, draws from your prepaid wallet at $1.50/1k records. "
                   "No surprise bills.",
        "auth_env_var": "BRIGHTDATA_API_KEY",
        "auth_legacy_alias": "BRIGHTDATA_API_TOKEN",
        "tool_count": 9,
    })


@mcp.custom_route("/favicon.ico", methods=["GET"])
def favicon(request):
    favicon_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000100000001008060000001ff3ff"
        "610000004849444154789c63600100000005000157d7b1bd0000000049454e44ae"
        "426082"
    )
    return Response(content=favicon_bytes, media_type="image/png")


def parse_args():
    parser = argparse.ArgumentParser(description="Bright Data Free-Credit MCP Server")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"], default=MCP_TRANSPORT)
    parser.add_argument("--host", default=MCP_HOST)
    parser.add_argument("--port", type=int, default=MCP_PORT)
    parser.add_argument("--path", default=MCP_PATH)
    return parser.parse_args()


def html_to_markdown(text: str, max_chars: int = 10000) -> str:
    """Strip HTML to readable text/markdown."""
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '\n', text)
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
    return text[:max_chars]


# Catalog cache (used by list_datasets)
_DATASET_CATALOG_CACHE = {"data": None, "fetched_at": 0.0}
_CATALOG_TTL_SECONDS = 3600


# ═════════════════════════════════════════════════════════════════
# BASE TOOLS  (always-on equivalents of Bright Data's hosted MCP)
# ═════════════════════════════════════════════════════════════════

@mcp.tool()
def search_engine(
    query: str,
    engine: str = "google",
    country: str = "in",
    language: str = "en",
    output_format: str = "json",
) -> dict:
    """
    Search Google, Bing, or Yandex — structured SERP results.
    Cost: 1 credit / call.

    Robust fallback: tries parsed_light JSON first; if that fails (zone issue
    or empty body), automatically falls back to markdown format. Returns
    markdown wrapped in a dict in that case.
    """
    engines = {
        "google": f"https://www.google.com/search?q={requests.utils.quote(query)}&hl={language}&gl={country}",
        "bing":   f"https://www.bing.com/search?q={requests.utils.quote(query)}&setlang={language}&cc={country}",
        "yandex": f"https://yandex.com/search/?text={requests.utils.quote(query)}&lr={country}",
    }
    url = engines.get(engine, engines["google"])

    if output_format == "markdown":
        payload = {"zone": SERP_ZONE, "url": url, "format": "raw", "data_format": "markdown"}
        r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return {"markdown": r.text}

    # JSON path — try parsed_light, fall back to markdown on failure
    payload = {"zone": SERP_ZONE, "url": url, "format": "raw", "data_format": "parsed_light"}
    r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)

    if r.status_code >= 400:
        return {
            "error": f"SERP API returned HTTP {r.status_code}",
            "body": r.text[:500],
            "hint": "Check that SERP_ZONE exists in your dashboard (https://brightdata.com/cp/zones).",
        }

    # Try JSON parse; if it fails, fall back to markdown
    try:
        return r.json()
    except ValueError:
        payload_md = {"zone": SERP_ZONE, "url": url, "format": "raw", "data_format": "markdown"}
        r2 = requests.post(REQUEST_URL, headers=headers, json=payload_md, timeout=60)
        r2.raise_for_status()
        return {
            "markdown": r2.text,
            "note": "JSON parse failed for parsed_light; fell back to markdown.",
        }


@mcp.tool()
def search_engine_batch(
    queries: list,
    engine: str = "google",
    country: str = "in",
    language: str = "en",
) -> dict:
    """
    Run up to 10 search queries in parallel. Cost: 1 credit / query.
    """
    if len(queries) > 10:
        raise ValueError("Max 10 queries per batch")
    results = []
    for q in queries:
        try:
            engines = {
                "google": f"https://www.google.com/search?q={requests.utils.quote(q)}&hl={language}&gl={country}",
                "bing":   f"https://www.bing.com/search?q={requests.utils.quote(q)}&setlang={language}&cc={country}",
                "yandex": f"https://yandex.com/search/?text={requests.utils.quote(q)}&lr={country}",
            }
            url = engines.get(engine, engines["google"])
            payload = {"zone": SERP_ZONE, "url": url, "format": "raw", "data_format": "parsed_light"}
            r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            results.append({"query": q, "ok": True, "data": r.json()})
        except Exception as e:
            results.append({"query": q, "ok": False, "error": str(e)})
    return {"results": results, "count": len(results)}


@mcp.tool()
def scrape_as_markdown(url: str) -> str:
    """
    Fetch any URL → clean markdown. Bypasses anti-bot/CAPTCHA.
    Cost: 1 credit / call.
    """
    payload = {"zone": UNLOCKER_ZONE, "url": url, "format": "raw"}
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return html_to_markdown(response.text)


@mcp.tool()
def scrape_as_html(url: str) -> str:
    """
    Fetch any URL → raw HTML. Cost: 1 credit / call.
    """
    payload = {"zone": UNLOCKER_ZONE, "url": url, "format": "raw"}
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.text[:50000]


@mcp.tool()
def scrape_batch(urls: list) -> dict:
    """
    Fetch up to 10 URLs in parallel → markdown. Cost: 1 credit / URL.
    """
    if len(urls) > 10:
        raise ValueError("Max 10 URLs per batch")
    results = []
    for u in urls:
        try:
            payload = {"zone": UNLOCKER_ZONE, "url": u, "format": "raw"}
            r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            results.append({"url": u, "ok": True, "markdown": html_to_markdown(r.text)})
        except Exception as e:
            results.append({"url": u, "ok": False, "error": str(e)})
    return {"results": results, "count": len(results)}


@mcp.tool()
def discover(
    query: str,
    dataset: str = "google_search",
    intent: str = None,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
) -> dict:
    """
    AI-relevance-ranked discovery across Bright Data datasets. Use for research.
    Cost: 1 credit / record.

    Args:
        query: Natural-language query.
        dataset: Dataset to discover from.
        intent: Optional AI intent hint.
        start_date / end_date: Optional ISO date filters.
        limit: Max records (default 50).
    """
    real_id = resolve_dataset(dataset)
    payload = [{"query": query, "limit": limit}]
    if intent:
        payload[0]["intent"] = intent
    if start_date:
        payload[0]["start_date"] = start_date
    if end_date:
        payload[0]["end_date"] = end_date
    response = requests.post(
        f"{DATASETS_DISCOVER}?dataset_id={real_id}&format=json",
        headers=headers, json=payload, timeout=120,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def scrape(
    dataset: str,
    urls: list,
    async_mode: bool = False,
    output_format: str = "json",
) -> dict:
    """
    Generic Web Scraper entrypoint — any dataset, any URLs.
    Cost: 1 credit / record returned.

    sync:  {"dataset_id": "...", "results": [...]}
    async: {"dataset_id": "...", "snapshot_id": "..."}
    """
    real_id = resolve_dataset(dataset)
    endpoint = DATASETS_TRIGGER if async_mode else DATASETS_SCRAPE
    timeout = 60 if async_mode else 120
    response = requests.post(
        f"{endpoint}?dataset_id={real_id}&format={output_format}",
        headers=headers, json=[{"url": u} for u in urls], timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if async_mode:
        return {"dataset_id": real_id, "snapshot_id": data["snapshot_id"]}
    return {"dataset_id": real_id, "results": data}


@mcp.tool()
def scrape_poll(snapshot_id: str, max_wait_seconds: int = 300) -> dict:
    """
    Poll an async scrape job until ready. Polling is free.
    """
    for _ in range(max_wait_seconds // 5):
        r = requests.get(f"{DATASETS_SNAPSHOT}/{snapshot_id}", headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "ready":
            return data
        if data.get("status") == "failed":
            return {"status": "failed", "details": data}
        time.sleep(5)
    return {"error": f"Timeout after {max_wait_seconds}s — still processing"}


@mcp.tool()
def list_datasets(force_refresh: bool = False) -> dict:
    """
    List available datasets — fetches the live Bright Data catalog (cached 1h).
    """
    now = time.time()
    if not force_refresh and _DATASET_CATALOG_CACHE["data"] and (now - _DATASET_CATALOG_CACHE["fetched_at"]) < _CATALOG_TTL_SECONDS:
        return _DATASET_CATALOG_CACHE["data"]

    for url in ("https://api.brightdata.com/datasets/list",
                "https://api.brightdata.com/datasets/v3/list"):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                datasets = data if isinstance(data, list) else data.get("datasets", [])
                if datasets:
                    result = {
                        "count": len(datasets),
                        "datasets": datasets,
                        "friendly_names": {k: v for k, v in DATASET_IDS.items()},
                        "aliases": DATASET_ALIASES,
                    }
                    _DATASET_CATALOG_CACHE["data"] = result
                    _DATASET_CATALOG_CACHE["fetched_at"] = now
                    return result
        except Exception:
            continue

    return {
        "count": len(DATASET_IDS),
        "datasets": [{"id": v, "name": k} for k, v in DATASET_IDS.items()],
        "friendly_names": {k: v for k, v in DATASET_IDS.items()},
        "aliases": DATASET_ALIASES,
        "note": "Live catalog unavailable — showing built-in only.",
    }

# ═════════════════════════════════════════════════════════════════
# RUN SERVER
# ═════════════════════════════════════════════════════════════════

def run_server():
    args = parse_args()
    transport = args.transport

    print("[brightdata-free-mcp] Starting server", file=sys.stderr)
    print(f"[brightdata-free-mcp] Transport: {transport}", file=sys.stderr)
    print(f"[brightdata-free-mcp] Host: {args.host}, Port: {args.port}, Path: {args.path}", file=sys.stderr)
    print("[brightdata-free-mcp] Free pool: 5,000 credits/month "
          "(Web Scraper + SERP + Web Unlocker)", file=sys.stderr)
    print(f"[brightdata-free-mcp] API token configured: "
          f"{bool(API_TOKEN and API_TOKEN != 'YOUR_API_KEY')}", file=sys.stderr)

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        mcp.run(transport="streamable-http", mount_path=args.path)
    elif transport == "sse":
        mcp.run(transport="sse", mount_path=args.path)
    else:
        raise ValueError(f"Unknown transport: {transport}")


if __name__ == "__main__":
    run_server()
