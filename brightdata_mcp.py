"""
Custom Bright Data MCP Server.

Wraps the three Bright Data products that share a single 5,000-credits-per-month
free pool (per https://docs.brightdata.com/general/account/billing-and-pricing/free-tier):

  • Web Scraper API    — structured JSON from 1000+ pre-built datasets
  • SERP API           — Google / Bing / Yandex structured search
  • Web Unlocker API   — bypass anti-bot, fetch any page (markdown or HTML)

Discover API is also exposed, but it is a separate account-gated product and is
not part of the documented monthly free-credit pool.

Authentication:
  Uses your Bright Data **API key** (NOT a token, NOT prefixed with brd_).
  Generate it at https://brightdata.com/cp/setting/users → "Add API key".
  Pass it as the BRIGHTDATA_API_KEY env var. The legacy BRIGHTDATA_API_TOKEN
  env var still works as a fallback alias.

Billing:
  • 5,000 free credits / month, renews on the 1st
  • Web Scraper: 1 credit / API call
  • SERP / Unlocker: 1 credit / call
  • With no deposited funds, usage stops when the free pool is exhausted
  • With deposited funds, usage continues at the account's PAYG rates

Run:
  pip install -r requirements.txt
  python brightdata_mcp.py                       # stdio (local)
  python brightdata_mcp.py --transport http      # HTTP (remote)
"""

import argparse
import logging
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

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
DATASETS_PROGRESS = f"{BASE_URL}/datasets/v3/progress"
DATASETS_LIST = f"{BASE_URL}/datasets/list"
DISCOVER_URL = f"{BASE_URL}/discover"
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
    "amazon_product_reviews": "amazon reviews",
    "amazon_product_search":  "amazon products search",

    # ── Instagram / TikTok / Facebook ─────────────────────────
    "insta":      "instagram - profiles",
    "ig":         "instagram - profiles",
    "instagram":  "instagram - profiles",
    "instagram_profile": "instagram - profiles",
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
    try:
        r = requests.get(DATASETS_LIST, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        datasets = data if isinstance(data, list) else data.get("datasets", [])
        if datasets:
            _DATASET_CATALOG_CACHE["data"] = {"datasets": datasets}
            _DATASET_CATALOG_CACHE["fetched_at"] = now
            return datasets
    except (requests.RequestException, ValueError):
        pass
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
            except ValueError:
                pass
        # Comma-separated?
        if "," in v and "\n" not in v:
            return [s.strip() for s in v.split(",") if s.strip()]
        # Single string
        return [v]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x is not None]
    return [str(value)]


def _validate_choice(value: str, allowed: set, parameter: str) -> str:
    normalized = str(value).lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Invalid {parameter} '{value}'. Expected one of: {choices}.")
    return normalized


def _response_json(response: requests.Response):
    """Decode an API JSON response and include useful context on malformed data."""
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"Bright Data returned non-JSON content (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        ) from exc


def _run_parallel(items: list, worker) -> list:
    """Run one request per item concurrently while preserving input order."""
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(10, len(items))) as pool:
        futures = {pool.submit(worker, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


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
    streamable_http_path=MCP_PATH,
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
        "version": "3.1.0",
        "mcp_endpoint": MCP_PATH,
        "transport": MCP_TRANSPORT,
        "billing": "5,000 monthly free credits: Web Scraper + SERP + Web Unlocker. "
                   "Discover API is separate and account-gated.",
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

    output_format="json" uses Bright Data's parsed_light response. Set it to
    "markdown" to request Bright Data's native Markdown transformation.
    """
    engine = _validate_choice(engine, {"google", "bing", "yandex"}, "engine")
    output_format = _validate_choice(output_format, {"json", "markdown"}, "output_format")
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    country = country.lower()
    engines = {
        "google": f"https://www.google.com/search?q={requests.utils.quote(query)}&hl={language}&gl={country}",
        "bing":   f"https://www.bing.com/search?q={requests.utils.quote(query)}&setlang={language}&cc={country}",
        "yandex": f"https://yandex.com/search/?text={requests.utils.quote(query)}&lang={language}",
    }
    url = engines[engine]

    if output_format == "markdown":
        payload = {
            "zone": SERP_ZONE, "url": url, "format": "raw",
            "data_format": "markdown", "country": country,
        }
        r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return {"markdown": r.text}

    payload = {
        "zone": SERP_ZONE, "url": url, "format": "raw",
        "data_format": "parsed_light", "country": country,
    }
    r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return _response_json(r)


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
    if not qs or len(qs) > 10:
        raise ValueError("queries must contain between 1 and 10 items")

    def worker(q):
        try:
            data = search_engine(q, engine, country, language, "json")
            return {"query": q, "ok": True, "data": data}
        except (requests.RequestException, ValueError) as e:
            return {"query": q, "ok": False, "error": str(e)}

    results = _run_parallel(qs, worker)
    return {"results": results, "count": len(results)}


@mcp.tool()
def scrape_as_markdown(url: str) -> str:
    """
    Fetch any URL → clean markdown. Bypasses anti-bot/CAPTCHA.
    Cost: 1 credit / call.
    """
    payload = {
        "zone": UNLOCKER_ZONE, "url": url, "format": "raw",
        "data_format": "markdown",
    }
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.text


@mcp.tool()
def scrape_as_html(url: str) -> str:
    """
    Fetch any URL → raw HTML. Cost: 1 credit / call.
    """
    payload = {"zone": UNLOCKER_ZONE, "url": url, "format": "raw"}
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.text


@mcp.tool()
def scrape_batch(urls: list) -> dict:
    """
    Fetch up to 10 URLs in parallel → markdown. Cost: 1 credit / URL.
    Accepts either a list of strings or a single comma-separated string.
    """
    url_list = _coerce_to_list(urls)
    if not url_list or len(url_list) > 10:
        raise ValueError("urls must contain between 1 and 10 items")

    def worker(u):
        try:
            payload = {
                "zone": UNLOCKER_ZONE, "url": u, "format": "raw",
                "data_format": "markdown",
            }
            r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
            r.raise_for_status()
            return {"url": u, "ok": True, "markdown": r.text}
        except (requests.RequestException, ValueError) as e:
            return {"url": u, "ok": False, "error": str(e)}

    results = _run_parallel(url_list, worker)
    return {"results": results, "count": len(results)}


@mcp.tool()
def discover(
    query: str,
    intent: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    country: str = "US",
    city: str | None = None,
    language: str = "en",
    filter_keywords: list | None = None,
    include_content: bool = False,
    include_images: bool = False,
    remove_duplicates: bool = True,
    output_format: str = "json",
    max_wait_seconds: int = 120,
) -> dict:
    """
    Search the public web and rank results using an AI-driven intent.

    Discover API is a separate, account-gated Bright Data product. It is not a
    dataset scraper and does not accept dataset IDs. The tool triggers a task and
    polls it until completion. Set max_wait_seconds=0 to return the task ID.

    Args:
        query: Natural-language query.
        intent: Optional AI intent hint.
        start_date / end_date: Optional ISO date filters.
        limit: Exact result count, from 1 to 20.
        output_format: "json" or "md".
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    output_format = _validate_choice(output_format, {"json", "md"}, "output_format")
    payload = {
        "query": query,
        "num_results": limit,
        "format": output_format,
        "country": country.upper(),
        "language": language,
        "include_content": include_content,
        "include_images": include_images,
        "remove_duplicates": remove_duplicates,
    }
    if intent:
        payload["intent"] = intent
    if start_date:
        payload["start_date"] = start_date
    if end_date:
        payload["end_date"] = end_date
    if city:
        payload["city"] = city
    keywords = _coerce_to_list(filter_keywords)
    if keywords:
        payload["filter_keywords"] = keywords

    response = requests.post(
        DISCOVER_URL, headers=headers, json=payload, timeout=60,
    )
    if response.status_code == 403:
        return {
            "error": "Discover API is not enabled for this Bright Data account.",
            "hint": "Ask your Bright Data account manager to enable Discover API, or use search_engine.",
        }
    response.raise_for_status()
    task = _response_json(response)
    task_id = task.get("task_id")
    if not task_id or max_wait_seconds <= 0:
        return task

    deadline = time.monotonic() + max_wait_seconds
    while True:
        result_response = requests.get(
            DISCOVER_URL,
            headers=headers,
            params={"task_id": task_id},
            timeout=60,
        )
        result_response.raise_for_status()
        result = _response_json(result_response)
        status = str(result.get("status", "")).lower()
        if status in {"done", "ready", "completed"}:
            return result
        if status in {"failed", "error", "canceled", "cancelled"}:
            return result
        if time.monotonic() >= deadline:
            return {
                "status": status or "running",
                "task_id": task_id,
                "error": f"Timeout after {max_wait_seconds}s; task is still processing.",
            }
        time.sleep(min(2, max(0, deadline - time.monotonic())))


@mcp.tool()
def scrape(
    dataset: str,
    urls: list,
    async_mode: bool = False,
    output_format: str = "json",
) -> dict:
    """
    Generic Web Scraper entrypoint — any dataset, any URLs.
    Cost: 1 free-tier credit / API call for eligible accounts.

    Args:
        dataset: Friendly name or bare gd_* id. Resolved against live catalog.
        urls: List of URLs (or a single string, or comma-separated string).
              Max 20 URLs for sync, thousands for async.
        async_mode: True returns snapshot_id (poll with scrape_poll).
        output_format: "json", "ndjson", "jsonl", or "csv".

    Returns:
        sync:  {"dataset_id": "...", "results": [...]}
        async: {"dataset_id": "...", "snapshot_id": "..."}
    """
    output_format = _validate_choice(output_format, {"json", "ndjson", "jsonl", "csv"}, "output_format")
    real_id = resolve_dataset(dataset)
    url_list = _coerce_to_list(urls)
    if not url_list:
        return {"error": "No URLs provided. Pass a list, single string, or comma-separated string."}
    if not async_mode and len(url_list) > 20:
        raise ValueError("Synchronous scraping accepts at most 20 URLs; set async_mode=True for larger batches.")
    endpoint = DATASETS_TRIGGER if async_mode else DATASETS_SCRAPE
    timeout = 60 if async_mode else 120
    response = requests.post(
        f"{endpoint}?dataset_id={real_id}&format={output_format}",
        headers=headers, json=[{"url": u} for u in url_list], timeout=timeout,
    )
    response.raise_for_status()
    if async_mode or response.status_code == 202:
        data = _response_json(response)
        return {
            "dataset_id": real_id,
            "snapshot_id": data["snapshot_id"],
            "status": "running",
            "format": output_format,
        }
    if output_format == "json":
        return {"dataset_id": real_id, "results": _response_json(response)}
    return {"dataset_id": real_id, "format": output_format, "content": response.text}


@mcp.tool()
def scrape_poll(
    snapshot_id: str,
    max_wait_seconds: int = 300,
    output_format: str = "json",
) -> dict:
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
    output_format = _validate_choice(output_format, {"json", "ndjson", "jsonl", "csv"}, "output_format")
    deadline = time.monotonic() + max(0, max_wait_seconds)
    while True:
        r = requests.get(f"{DATASETS_PROGRESS}/{snapshot_id}", headers=headers, timeout=30)
        if r.status_code == 404:
            return {"error": "Snapshot not found", "snapshot_id": snapshot_id}
        r.raise_for_status()
        progress = _response_json(r)
        status = str(progress.get("status", "")).lower()
        if status == "ready":
            download = requests.get(
                f"{DATASETS_SNAPSHOT}/{snapshot_id}",
                headers=headers,
                params={"format": output_format},
                timeout=120,
            )
            download.raise_for_status()
            results = _response_json(download) if output_format == "json" else download.text
            return {
                "status": "ready",
                "snapshot_id": snapshot_id,
                "format": output_format,
                "results": results,
            }
        if status == "failed":
            return {"status": "failed", "snapshot_id": snapshot_id, "details": progress}
        if time.monotonic() >= deadline:
            return {
                "status": status or "running",
                "snapshot_id": snapshot_id,
                "error": f"Timeout after {max_wait_seconds}s; snapshot is still processing.",
            }
        time.sleep(min(5, max(0, deadline - time.monotonic())))


@mcp.tool()
def list_datasets(force_refresh: bool = False) -> dict:
    """
    List available datasets — fetches the live Bright Data catalog (cached 1h).
    Returns ~1700+ datasets with their current IDs.
    """
    now = time.time()
    if not force_refresh and _DATASET_CATALOG_CACHE["data"] and (now - _DATASET_CATALOG_CACHE["fetched_at"]) < _CATALOG_TTL_SECONDS:
        return _DATASET_CATALOG_CACHE["data"]

    error = "Bright Data returned an empty dataset catalog."
    try:
        r = requests.get(DATASETS_LIST, headers=headers, timeout=30)
        r.raise_for_status()
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
    except (requests.RequestException, ValueError) as exc:
        error = str(exc)

    return {
        "count": 0,
        "datasets": [],
        "aliases": DATASET_ALIASES,
        "error": error,
        "note": "Live catalog unavailable. Try again, or pass a bare dataset_id starting with 'gd_'.",
    }

# ═════════════════════════════════════════════════════════════════
# RUN SERVER
# ═════════════════════════════════════════════════════════════════

def run_server():
    args = parse_args()
    transport = args.transport
    # FastMCP is initialized before CLI parsing so tools can be decorated.
    # Apply CLI overrides to its runtime settings before starting Uvicorn.
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.streamable_http_path = args.path

    print("[brightdata-free-mcp] Starting server", file=sys.stderr)
    print(f"[brightdata-free-mcp] Transport: {transport}", file=sys.stderr)
    print(f"[brightdata-free-mcp] Host: {args.host}, Port: {args.port}, Path: {args.path}", file=sys.stderr)
    print("[brightdata-free-mcp] Free pool: 5,000 credits/month "
          "(Web Scraper + SERP + Web Unlocker); Discover is separate", file=sys.stderr)
    print(f"[brightdata-free-mcp] API token configured: "
          f"{bool(API_TOKEN and API_TOKEN != 'YOUR_API_KEY')}", file=sys.stderr)

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "http":
        mcp.run(transport="streamable-http")
    elif transport == "sse":
        mcp.run(transport="sse", mount_path=args.path)
    else:
        raise ValueError(f"Unknown transport: {transport}")


if __name__ == "__main__":
    run_server()
