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

SERP_ZONE = os.getenv("SERP_ZONE", "serp_api")
UNLOCKER_ZONE = os.getenv("WEB_UNLOCKER_ZONE", "mcp_unlocker")

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

# ─── Dataset Registry ──────────────────────────────────────────
# Strategy: NO hardcoded dataset IDs. Bright Data's catalog changes over
# time; any hardcoded ID may become stale. We saw this firsthand — the
# Amazon dataset ID changed from gd_l7q7dkf244hwjntr0w (old) to
# gd_l7q7dkf244hwjntr0 (current) — a 1-character difference that broke
# scrapes.
#
# Instead:
#   1. Bare dataset_ids starting with "gd_" are passed through as-is
#      (always works — call list_datasets() to discover what's available)
#   2. Friendly names are resolved by lazy-fetching from the live catalog
#      (cached 1h via list_datasets()) and fuzzy-matching by name
#   3. A few common aliases (linkedin, amazon, etc.) shortcut the friendly name

# Short aliases only — these map to substring-matchable fragments of
# catalog names. resolve_dataset() does a case-insensitive substring match
# against the live catalog (cached 1h), so the fragments just need to be
# specific enough to pick the right dataset.
DATASET_ALIASES = {
    # ── LinkedIn ──────────────────────────────────────────────
    "linkedin":         "linkedin people profiles",
    "linkedin_profile": "linkedin people profiles",
    "linkedin_jobs":    "linkedin job listings",
    "linkedin_company": "linkedin company information",

    # ── Amazon ────────────────────────────────────────────────
    "amazon":     "amazon products",
    "amzn":       "amazon products",
    "amazon_product":        "amazon products",
    "amazon_product_reviews": "amazon products - reviews",
    "amazon_product_search":  "amazon products - search",

    # ── Instagram / TikTok / Facebook ─────────────────────────
    "insta":      "instagram - profiles",
    "ig":         "instagram - profiles",
    "instagram":  "instagram - profiles",
    "tt":         "tiktok - posts by profile",
    "tiktok":     "tiktok - profiles",
    "tiktok_posts": "tiktok - posts by profile",
    "fb":         "facebook - posts by post url",
    "facebook":   "facebook - posts by post url",

    # ── X / Twitter / YouTube / Reddit ────────────────────────
    "twitter":    "x (formerly twitter) - posts",
    "x":          "x (formerly twitter) - posts",
    "x_posts":    "x (formerly twitter) - posts",
    "yt":         "youtube - videos posts",
    "youtube":    "youtube - videos posts",
    "reddit":     "reddit- posts",
    "reddit_posts": "reddit- posts",

    # ── Business ──────────────────────────────────────────────
    "crunchbase":     "crunchbase companies information",
    "crunchbase_company": "crunchbase companies information",

    # ── Other ─────────────────────────────────────────────────
    "google":     "google shopping",
    "maps":       "google maps businesses",
    "walmart":    "walmart - products",
    "ebay":       "ebay - products",
    "etsy":       "etsy - products",
    "bestbuy":    "best buy - products",
}


def _get_catalog(force_refresh: bool = False) -> list:
    """Return the live dataset catalog (cached 1h). Each entry: {id, name, ...}"""
    now = time.time()
    cached_data = _DATASET_CATALOG_CACHE.get("data")
    fetched_at = _DATASET_CATALOG_CACHE.get("fetched_at", 0)
    if not force_refresh and cached_data and (now - fetched_at) < _CATALOG_TTL_SECONDS:
        return cached_data.get("datasets", [])
    # Fetch live
    for url in ("https://api.brightdata.com/datasets/list",
                "https://api.brightdata.com/datasets/v3/list"):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                datasets = data if isinstance(data, list) else data.get("datasets", [])
                if datasets:
                    _DATASET_CATALOG_CACHE["data"] = {"datasets": datasets}
                    _DATASET_CATALOG_CACHE["fetched_at"] = now
                    return datasets
        except Exception:
            continue
    return cached_data.get("datasets", []) if cached_data else []


def resolve_dataset(name: str) -> str:
    """
    Resolve a friendly name / alias / bare dataset_id → real dataset_id.

    Strategy:
      - Bare id starting with 'gd_': passed through as-is
      - Alias from DATASET_ALIASES: expanded to a friendly name
      - Friendly name: fuzzy-matched against the live catalog (cached 1h)

    Always works for bare ids. For friendly names, the catalog must be
    populated — either via a prior list_datasets() call or by lazy fetch.
    """
    if not name:
        raise ValueError("Empty dataset name")
    # Bare dataset_id — always works
    if name.startswith("gd_"):
        return name
    # Alias → friendly name
    target = DATASET_ALIASES.get(name.lower(), name.lower())
    # Lazy-fetch the live catalog
    catalog = _get_catalog()
    if not catalog:
        raise ValueError(
            f"Cannot resolve '{name}': dataset catalog unavailable. "
            f"Either pass a bare dataset_id starting with 'gd_', or call "
            f"list_datasets() first to populate the cache."
        )
    # Exact match (case-insensitive)
    for ds in catalog:
        if ds.get("name", "").lower() == target.lower():
            return ds["id"]
    # Substring match — pick the shortest name (most specific)
    matches = [ds for ds in catalog if target.lower() in ds.get("name", "").lower()]
    if matches:
        matches.sort(key=lambda d: len(d.get("name", "")))
        return matches[0]["id"]
    # Nothing matched
    sample = ", ".join(ds.get("name", "") for ds in catalog[:5])
    raise ValueError(
        f"Unknown dataset: '{name}'. Could not find '{target}' in the live catalog. "
        f"Sample of available names: {sample}. "
        f"Try list_datasets() to see all {len(catalog)} available datasets. "
        f"Or pass a bare dataset_id starting with 'gd_'."
    )


def _coerce_to_list(value) -> list:
    """
    Coerce various input shapes to a list of strings. Handles:
      - None    → []
      - str     → [str]   (also parses JSON arrays and comma-separated lists)
      - list/tuple → [str(x) for x in value]
    This makes MCP tool parameters robust to clients that send single
    strings instead of single-element lists.
    """
    if value is None:
        return []
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return []
        # JSON array?
        if v.startswith("[") and v.endswith("]"):
            try:
                import json as _json
                parsed = _json.loads(v)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if x is not None]
            except Exception:
                pass
        # Comma-separated?
        if "," in v and "\n" not in v:
            return [s.strip() for s in v.split(",") if s.strip()]
        # Single string
        return [v]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x is not None]
    return [str(value)]


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
    Accepts either a list of strings or a single comma-separated string.
    """
    qs = _coerce_to_list(queries)
    if len(qs) > 10:
        raise ValueError("Max 10 queries per batch")
    results = []
    for q in qs:
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
    Accepts either a list of strings or a single comma-separated string.
    """
    url_list = _coerce_to_list(urls)
    if len(url_list) > 10:
        raise ValueError("Max 10 URLs per batch")
    results = []
    for u in url_list:
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
    dataset: str = "linkedin_jobs",
    intent: str = None,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
) -> dict:
    """
    AI-relevance-ranked discovery across Bright Data datasets. Use for research.
    Cost: 1 credit / record.

    NOTE: Only some datasets support the Discover API (where you supply a query
    instead of URLs). Verified working datasets include:
      - linkedin_jobs    ("Lead Android Developer India")
      - linkedin_profile (search by criteria)
      - linkedin_company
      - crunchbase_company
    The google_search dataset (SERP) does NOT support discover — use
    search_engine() instead for Google queries.

    Args:
        query: Natural-language query.
        dataset: Dataset to discover from. Default "linkedin_jobs".
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
    if response.status_code == 404:
        return {
            "error": f"Dataset '{dataset}' (id: {real_id}) does not support the Discover API.",
            "hint": "Use a discovery-enabled dataset like 'linkedin_jobs', 'linkedin_profile', "
                    "'linkedin_company', or 'crunchbase_company'. For Google search, use "
                    "the search_engine() tool instead.",
        }
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

    Args:
        dataset: Friendly name or bare gd_* id. Resolved against live catalog.
        urls: List of URLs (or a single string, or comma-separated string).
              Max 20 URLs for sync, thousands for async.
        async_mode: True returns snapshot_id (poll with scrape_poll).
        output_format: "json", "ndjson", or "csv".

    Returns:
        sync:  {"dataset_id": "...", "results": [...]}
        async: {"dataset_id": "...", "snapshot_id": "..."}
    """
    real_id = resolve_dataset(dataset)
    url_list = _coerce_to_list(urls)
    if not url_list:
        return {"error": "No URLs provided. Pass a list, single string, or comma-separated string."}
    endpoint = DATASETS_TRIGGER if async_mode else DATASETS_SCRAPE
    timeout = 60 if async_mode else 120
    response = requests.post(
        f"{endpoint}?dataset_id={real_id}&format={output_format}",
        headers=headers, json=[{"url": u} for u in url_list], timeout=timeout,
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

    Args:
        snapshot_id: ID returned by scrape(..., async_mode=True).
        max_wait_seconds: Max wait (default 5 min).

    Returns:
        {"status": "ready", ...} on success,
        {"status": "failed", ...} on failure,
        {"error": "Timeout after Ns — still processing"} if still running,
        {"error": "Snapshot not found", "snapshot_id": "..."} if 404.
    """
    for _ in range(max_wait_seconds // 5):
        r = requests.get(f"{DATASETS_SNAPSHOT}/{snapshot_id}", headers=headers, timeout=30)
        if r.status_code == 404:
            return {"error": "Snapshot not found", "snapshot_id": snapshot_id}
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
    Returns ~1700+ datasets with their current IDs.
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
                        "aliases": DATASET_ALIASES,
                    }
                    _DATASET_CATALOG_CACHE["data"] = result
                    _DATASET_CATALOG_CACHE["fetched_at"] = now
                    return result
        except Exception:
            continue

    return {
        "count": 0,
        "datasets": [],
        "aliases": DATASET_ALIASES,
        "note": "Live catalog unavailable. Try again, or pass a bare dataset_id starting with 'gd_'.",
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
