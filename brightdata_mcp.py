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
import json
import logging
import os
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from typing import Literal
from urllib.parse import urlencode, urlsplit

import requests
from dotenv import load_dotenv

# ────────────────────────────────────────────────────────────────────
# Silence noisy pydantic-settings warnings
# ────────────────────────────────────────────────────────────────────
warnings.filterwarnings(
    "ignore", message=r".*incomplete definition.*lifespan.*", category=UserWarning
)
warnings.filterwarnings(
    "ignore", message=r".*lifespan.*incomplete definition.*", category=UserWarning
)
warnings.filterwarnings(
    "ignore", module=r"pydantic_settings\.sources\.utils", category=UserWarning
)
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
API_TOKEN = os.getenv("BRIGHTDATA_API_KEY") or os.getenv(
    "BRIGHTDATA_API_TOKEN", "YOUR_API_KEY"
)
BASE_URL = os.getenv("BRIGHTDATA_API_BASE_URL", "https://api.brightdata.com").rstrip(
    "/"
)
DATASETS_SCRAPE = f"{BASE_URL}/datasets/v3/scrape"
DATASETS_TRIGGER = f"{BASE_URL}/datasets/v3/trigger"
DATASETS_SNAPSHOT = f"{BASE_URL}/datasets/v3/snapshot"
DATASETS_PROGRESS = f"{BASE_URL}/datasets/v3/progress"
DATASETS_LIST = f"{BASE_URL}/datasets/list"
DISCOVER_URL = f"{BASE_URL}/discover"
REQUEST_URL = f"{BASE_URL}/request"  # SERP + Web Unlocker

SERP_ZONE = os.getenv("SERP_ZONE", "").strip()
UNLOCKER_ZONE = os.getenv("WEB_UNLOCKER_ZONE", "").strip()

headers = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}


# ─── Dataset Registry ──────────────────────────────────────────
# Strategy: NO hardcoded dataset IDs. Bright Data's catalog changes over
# time, so IDs and friendly names are resolved from the authenticated live
# account catalog.
#
# Instead:
#   1. Bare dataset_ids starting with "gd_" are passed through as-is
#      (always works — call list_datasets() to discover what's available)
#   2. Friendly names are resolved by lazy-fetching from the live catalog
#      (cached 1h via list_datasets()) and fuzzy-matching by name
def _normalized_words(value: str) -> list[str]:
    """Normalize a dataset name without relying on a maintained alias table."""
    words = re.findall(r"[a-z0-9]+", str(value).lower().replace("_", " "))
    return [
        word[:-1] if len(word) > 3 and word.endswith("s") else word for word in words
    ]


def _normalized_name(value: str) -> str:
    return " ".join(_normalized_words(value))


def _dataset_match_score(query: str, candidate: str) -> float:
    """Score live catalog names using normalized text and token overlap."""
    query_name = _normalized_name(query)
    candidate_name = _normalized_name(candidate)
    query_words = set(query_name.split())
    candidate_words = set(candidate_name.split())
    union = query_words | candidate_words
    token_score = len(query_words & candidate_words) / len(union) if union else 0.0
    text_score = SequenceMatcher(None, query_name, candidate_name).ratio()
    return (0.65 * token_score) + (0.35 * text_score)


def _dataset_name_matches(query: str, candidate: str) -> bool:
    """Return whether a live catalog name has a meaningful query overlap."""
    query_name = _normalized_name(query)
    candidate_name = _normalized_name(candidate)
    if not query_name or not candidate_name:
        return False
    if query_name in candidate_name or candidate_name in query_name:
        return True
    return bool(set(query_name.split()) & set(candidate_name.split()))


def _get_catalog(force_refresh: bool = False) -> list:
    """Return the live dataset catalog (cached 1h). Each entry: {id, name, ...}"""
    now = time.time()
    cached_data = _DATASET_CATALOG_CACHE.get("data")
    fetched_at = _DATASET_CATALOG_CACHE.get("fetched_at", 0)
    if not force_refresh and cached_data and (now - fetched_at) < _CATALOG_TTL_SECONDS:
        return cached_data.get("datasets", [])
    try:
        _require_api_key()
        r = requests.get(DATASETS_LIST, headers=headers, timeout=30)
        _raise_for_api_error(r, "Dataset catalog")
        data = r.json()
        datasets = data if isinstance(data, list) else data.get("datasets", [])
        if datasets:
            _DATASET_CATALOG_CACHE["data"] = {"datasets": datasets}
            _DATASET_CATALOG_CACHE["fetched_at"] = now
            _DATASET_CATALOG_CACHE["error"] = None
            return datasets
        _DATASET_CATALOG_CACHE["error"] = (
            "Bright Data returned an empty dataset catalog."
        )
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        _DATASET_CATALOG_CACHE["error"] = str(exc)
    return cached_data.get("datasets", []) if cached_data else []


def resolve_dataset(name: str) -> str:
    """
    Resolve a friendly name or bare dataset_id → real dataset_id.

    Strategy:
      - Bare id starting with 'gd_': passed through as-is
      - Friendly name: matched against the live account catalog (cached 1h)

    Always works for bare ids. For friendly names, the catalog must be
    populated — either via a prior list_datasets() call or by lazy fetch.
    """
    if not name:
        raise ValueError("Empty dataset name")
    # Bare dataset_id — always works
    name = str(name).strip()
    if name.startswith("gd_"):
        if not re.fullmatch(r"gd_[A-Za-z0-9]+", name):
            raise ValueError(
                "dataset IDs may contain only letters and numbers after 'gd_'"
            )
        return name
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
        if _normalized_name(ds.get("name", "")) == _normalized_name(name):
            return ds["id"]
    ranked = sorted(
        ((_dataset_match_score(name, ds.get("name", "")), ds) for ds in catalog),
        key=lambda item: item[0],
        reverse=True,
    )
    if ranked:
        best_score, best = ranked[0]
        next_score = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score >= 0.68 and best_score - next_score >= 0.08:
            return best["id"]
        suggestions = ", ".join(
            f"{ds.get('name', '')} ({ds.get('id', '')})" for _, ds in ranked[:5]
        )
        raise ValueError(
            f"Dataset name '{name}' is unknown or ambiguous. Closest live matches: "
            f"{suggestions}. Pass the exact name or a bare gd_* ID."
        )
    # Nothing matched
    sample = ", ".join(ds.get("name", "") for ds in catalog[:5])
    raise ValueError(
        f"Unknown dataset: '{name}'. Could not find it in the live catalog. "
        f"Sample of available names: {sample}. "
        f"Try list_datasets() to see all {len(catalog)} available datasets. "
        f"Or pass a bare dataset_id starting with 'gd_'."
    )


def _coerce_to_list(value) -> list:
    """
    Coerce various input shapes to a list of strings. Handles:
      - None    → []
      - str     → [str]   (also parses JSON arrays)
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
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if x is not None]
            except ValueError:
                pass
        # A plain string is one item. Splitting on commas corrupts valid URLs
        # and natural-language queries; callers can pass a list or JSON array.
        return [v]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value if x is not None]
    return [str(value)]


def _coerce_items(value) -> list:
    """Normalize a single item, JSON array, or native sequence without stringifying objects."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
            except ValueError as exc:
                raise ValueError(
                    "value looks like a JSON array but is invalid JSON"
                ) from exc
            if isinstance(parsed, list):
                return parsed
        return [stripped]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _coerce_scrape_inputs(value) -> list[dict]:
    """Normalize dataset-specific scraper inputs to a non-empty list of objects."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(
                "inputs must be valid JSON when provided as a string"
            ) from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("inputs must contain at least one object")
    if not all(isinstance(item, dict) and item for item in value):
        raise ValueError("every item in inputs must be a non-empty object")
    return [dict(item) for item in value]


def _validate_choice(value: str, allowed: set, parameter: str) -> str:
    normalized = str(value).lower()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Invalid {parameter} '{value}'. Expected one of: {choices}.")
    return normalized


def _require_api_key() -> None:
    if not API_TOKEN or API_TOKEN == "YOUR_API_KEY":
        raise RuntimeError("BRIGHTDATA_API_KEY is not configured")


def _require_zone(value: str, env_name: str) -> str:
    _require_api_key()
    if not value:
        raise RuntimeError(f"{env_name} is not configured")
    return value


def _validate_url(value: str, parameter: str = "url") -> str:
    value = str(value).strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{parameter} must be an absolute HTTP or HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{parameter} must not contain embedded credentials")
    return value


def _validate_country(value: str | None, parameter: str = "country") -> str | None:
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if not re.fullmatch(r"[A-Za-z]{2}", value):
        raise ValueError(f"{parameter} must be a 2-letter country code")
    return value.lower()


def _validate_cursor(value) -> int:
    if value in (None, ""):
        return 0
    try:
        cursor = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("cursor must be a non-negative page number") from exc
    if cursor < 0:
        raise ValueError("cursor must be a non-negative page number")
    return cursor


def _build_search_url(
    engine: str,
    query: str,
    cursor=0,
    country: str | None = None,
    language: str | None = None,
) -> str:
    """Build the documented engine URL while preserving optional targeting."""
    cursor = _validate_cursor(cursor)
    country = _validate_country(country)
    params = {"q": query}
    if engine == "google":
        params["start"] = cursor * 10
        if country:
            params["gl"] = country
        if language:
            params["hl"] = str(language).strip()
        return f"https://www.google.com/search?{urlencode(params)}"
    if engine == "bing":
        params["first"] = (cursor * 10) + 1
        if country:
            params["cc"] = country
        if language:
            params["setlang"] = str(language).strip()
        return f"https://www.bing.com/search?{urlencode(params)}"
    yandex_params = {"text": query, "p": cursor}
    if language:
        yandex_params["lang"] = str(language).strip()
    return f"https://yandex.com/search/?{urlencode(yandex_params)}"


def _response_json(response: requests.Response):
    """Decode an API JSON response and include useful context on malformed data."""
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"Bright Data returned non-JSON content (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        ) from exc


def _error_details(response: requests.Response):
    """Return a bounded JSON or text error body without exposing request headers."""
    try:
        return response.json()
    except ValueError:
        return response.text[:2000] or "Bright Data returned an empty error response."


def _raise_for_api_error(response: requests.Response, operation: str) -> None:
    """Raise a bounded, actionable error that retains Bright Data's response body."""
    if response.status_code < 400:
        return
    details = _error_details(response)
    rendered = (
        details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
    )
    raise requests.HTTPError(
        f"{operation} failed (HTTP {response.status_code}): {rendered[:2000]}",
        response=response,
    )


def _scrape_error(
    response: requests.Response, dataset_id: str, async_mode: bool
) -> dict:
    """Build an actionable MCP result for a rejected Web Scraper API request."""
    details = _error_details(response)
    searchable = (
        json.dumps(details, ensure_ascii=True)
        if not isinstance(details, str)
        else details
    )
    searchable = searchable.lower()

    if (
        "does not support collection" in searchable
        or "not allowed for api" in searchable
    ):
        hint = (
            "This catalog entry cannot be collected through the Web Scraper API. "
            "Choose a collectable scraper dataset from Bright Data's Scrapers Library. "
            "Retrying asynchronously will not fix this error."
        )
    elif response.status_code == 400 and (
        "validation" in searchable or "invalid input" in searchable
    ):
        hint = (
            "The input does not match this dataset's schema. Inspect details.errors for "
            "the required fields or URL pattern, then call scrape with inputs=[{...}]. "
            "Retrying asynchronously will not fix an invalid input."
        )
    elif response.status_code in {401, 403}:
        hint = "Check the Bright Data API key and this account's access to the selected dataset."
    elif response.status_code == 429:
        hint = "Bright Data rate-limited the request. Wait before retrying."
    elif response.status_code >= 500:
        hint = "Bright Data returned a server error. Retry later or use async mode for a large valid job."
    else:
        hint = "Review the Bright Data error details and the selected dataset's input schema."

    return {
        "error": "Bright Data Web Scraper API rejected the request.",
        "status_code": response.status_code,
        "dataset_id": dataset_id,
        "mode": "async" if async_mode else "sync",
        "details": details,
        "hint": hint,
        "retryable": response.status_code == 429 or response.status_code >= 500,
    }


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
SERVER_VERSION = "4.0.0"
TOOL_NAMES = (
    "search_engine",
    "search_engine_batch",
    "scrape_as_markdown",
    "scrape_as_html",
    "scrape_batch",
    "discover",
    "scrape",
    "scrape_poll",
    "list_datasets",
)

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
    return JSONResponse(
        {
            "status": "ok",
            "server": "brightdata-free",
            "transport": MCP_TRANSPORT,
            "token_configured": bool(API_TOKEN and API_TOKEN != "YOUR_API_KEY"),
            "serp_zone_configured": bool(SERP_ZONE),
            "unlocker_zone_configured": bool(UNLOCKER_ZONE),
        }
    )


@mcp.custom_route("/", methods=["GET"])
def root(request):
    return JSONResponse(
        {
            "name": "brightdata-free-mcp",
            "version": SERVER_VERSION,
            "mcp_endpoint": MCP_PATH,
            "transport": MCP_TRANSPORT,
            "billing": "5,000 monthly free credits: Web Scraper + SERP + Web Unlocker. "
            "Discover API is separate and account-gated.",
            "auth_env_var": "BRIGHTDATA_API_KEY",
            "auth_legacy_alias": "BRIGHTDATA_API_TOKEN",
            "tool_count": len(TOOL_NAMES),
        }
    )


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
    parser.add_argument(
        "--transport", choices=["stdio", "http", "sse"], default=MCP_TRANSPORT
    )
    parser.add_argument("--host", default=MCP_HOST)
    parser.add_argument("--port", type=int, default=MCP_PORT)
    parser.add_argument("--path", default=MCP_PATH)
    return parser.parse_args()


# Catalog cache (used by list_datasets)
_DATASET_CATALOG_CACHE = {"data": None, "fetched_at": 0.0, "error": None}
_CATALOG_TTL_SECONDS = 3600


# ═════════════════════════════════════════════════════════════════
# BASE TOOLS  (always-on equivalents of Bright Data's hosted MCP)
# ═════════════════════════════════════════════════════════════════


@mcp.tool()
def search_engine(
    query: str,
    engine: Literal["google", "bing", "yandex"] = "google",
    country: str | None = None,
    language: str | None = None,
    output_format: Literal["auto", "json", "markdown"] = "auto",
    cursor: int = 0,
) -> dict:
    """
    Search Google, Bing, or Yandex — structured SERP results.
    Cost: 1 credit / call.

    Google supports parsed JSON; Bing and Yandex return Markdown, matching
    Bright Data's official MCP contract. cursor is a zero-based page number.
    """
    engine = _validate_choice(engine, {"google", "bing", "yandex"}, "engine")
    output_format = _validate_choice(
        output_format, {"auto", "json", "markdown"}, "output_format"
    )
    if not query or not query.strip():
        raise ValueError("query must not be empty")
    query = query.strip()
    country = _validate_country(country)
    url = _build_search_url(engine, query, cursor, country, language)
    effective_format = (
        ("json" if engine == "google" else "markdown")
        if output_format == "auto"
        else output_format
    )
    if effective_format == "json" and engine != "google":
        raise ValueError(
            "Bright Data returns Bing and Yandex searches as Markdown; use output_format='auto' or 'markdown'."
        )
    payload = {
        "zone": _require_zone(SERP_ZONE, "SERP_ZONE"),
        "url": url,
        "format": "raw",
        "data_format": "parsed_light" if effective_format == "json" else "markdown",
    }
    if country:
        payload["country"] = country
    r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    _raise_for_api_error(r, f"{engine} search")
    if effective_format == "json":
        return _response_json(r)
    return {
        "engine": engine,
        "query": query,
        "cursor": _validate_cursor(cursor),
        "format": "markdown",
        "markdown": r.text,
    }


@mcp.tool()
def search_engine_batch(
    queries: list[str | dict],
    engine: Literal["google", "bing", "yandex"] = "google",
    country: str | None = None,
    language: str | None = None,
    output_format: Literal["auto", "json", "markdown"] = "auto",
) -> dict:
    """
    Run up to 10 search queries in parallel. Cost: 1 credit / query.
    Each item may be a string or an object with query, engine, country,
    language, output_format, and cursor. Global values are fallbacks.
    """
    qs = _coerce_items(queries)
    if not qs or len(qs) > 10:
        raise ValueError("queries must contain between 1 and 10 items")

    jobs = []
    for item in qs:
        if isinstance(item, str):
            jobs.append({"query": item})
        elif isinstance(item, dict):
            jobs.append(dict(item))
        else:
            raise TypeError("each query must be a string or an object")

    def worker(job):
        q = str(job.get("query", "")).strip()
        try:
            data = search_engine(
                q,
                job.get("engine", engine),
                job.get("country", country),
                job.get("language", language),
                job.get("output_format", output_format),
                job.get("cursor", 0),
            )
            return {
                "query": q,
                "engine": job.get("engine", engine),
                "ok": True,
                "data": data,
            }
        except (requests.RequestException, RuntimeError, ValueError) as e:
            return {"query": q, "ok": False, "error": str(e)}

    results = _run_parallel(jobs, worker)
    return {"results": results, "count": len(results)}


@mcp.tool()
def scrape_as_markdown(url: str) -> str:
    """
    Fetch any URL → clean markdown. Bypasses anti-bot/CAPTCHA.
    Cost: 1 credit / call.
    """
    payload = {
        "zone": _require_zone(UNLOCKER_ZONE, "WEB_UNLOCKER_ZONE"),
        "url": _validate_url(url),
        "format": "raw",
        "data_format": "markdown",
    }
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    _raise_for_api_error(response, "Markdown scrape")
    return response.text


@mcp.tool()
def scrape_as_html(url: str) -> str:
    """
    Fetch any URL → raw HTML. Cost: 1 credit / call.
    """
    payload = {
        "zone": _require_zone(UNLOCKER_ZONE, "WEB_UNLOCKER_ZONE"),
        "url": _validate_url(url),
        "format": "raw",
    }
    response = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
    _raise_for_api_error(response, "HTML scrape")
    return response.text


@mcp.tool()
def scrape_batch(urls: list[str]) -> dict:
    """
    Fetch up to 10 URLs in parallel → markdown. Cost: 1 credit / URL.
    Accepts a list, JSON array string, or a single URL.
    """
    url_list = _coerce_to_list(urls)
    if not url_list or len(url_list) > 10:
        raise ValueError("urls must contain between 1 and 10 items")

    def worker(u):
        try:
            u = _validate_url(u)
            payload = {
                "zone": _require_zone(UNLOCKER_ZONE, "WEB_UNLOCKER_ZONE"),
                "url": u,
                "format": "raw",
                "data_format": "markdown",
            }
            r = requests.post(REQUEST_URL, headers=headers, json=payload, timeout=60)
            _raise_for_api_error(r, "Batch Markdown scrape")
            return {"url": u, "ok": True, "markdown": r.text}
        except (requests.RequestException, RuntimeError, ValueError) as e:
            return {"url": u, "ok": False, "error": str(e)}

    results = _run_parallel(url_list, worker)
    return {"results": results, "count": len(results)}


@mcp.tool()
def discover(
    query: str,
    intent: str | None = None,
    mode: Literal["standard", "zeroRanking", "deep", "fast"] = "standard",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    country: str | None = None,
    city: str | None = None,
    language: str | None = None,
    filter_keywords: list[str] | None = None,
    include_content: bool = False,
    include_images: bool = False,
    remove_duplicates: bool = True,
    output_format: Literal["json", "md"] = "json",
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
    query = query.strip()
    if len(query) > 1500:
        raise ValueError("query must contain at most 1500 characters")
    if intent and len(intent) > 3000:
        raise ValueError("intent must contain at most 3000 characters")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    mode = _validate_choice(mode, {"standard", "zeroranking", "deep", "fast"}, "mode")
    if mode == "zeroranking" and include_content:
        raise ValueError("include_content is not supported when mode='zeroRanking'")
    output_format = _validate_choice(output_format, {"json", "md"}, "output_format")
    normalized_country = _validate_country(country)
    payload = {
        "query": query,
        "num_results": limit,
        "format": output_format,
        "mode": "zeroRanking" if mode == "zeroranking" else mode,
        "include_content": include_content,
        "include_images": include_images,
        "remove_duplicates": remove_duplicates,
    }
    if normalized_country:
        payload["country"] = normalized_country.upper()
    if language:
        payload["language"] = str(language).strip()
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

    _require_api_key()
    response = requests.post(
        DISCOVER_URL,
        headers=headers,
        json=payload,
        timeout=60,
    )
    if response.status_code == 403:
        return {
            "error": "Discover API is not enabled for this Bright Data account.",
            "hint": "Ask your Bright Data account manager to enable Discover API, or use search_engine.",
        }
    _raise_for_api_error(response, "Discover trigger")
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
        _raise_for_api_error(result_response, "Discover polling")
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
    urls: list[str] | None = None,
    inputs: list[dict] | None = None,
    async_mode: bool = False,
    output_format: Literal["json", "ndjson", "jsonl", "csv"] = "json",
) -> dict:
    """
    Generic Web Scraper entrypoint for collectable Bright Data scraper datasets.
    Cost: 1 free-tier credit / API call for eligible accounts.

    Args:
        dataset: Friendly name or bare gd_* id. Resolved against live catalog.
        urls: List of collect-by-URL targets (or a single URL / JSON-array string).
              This shorthand creates one {"url": value} object per URL.
        inputs: Dataset-specific input objects. Use instead of urls when the
                dataset requires fields such as country, keyword, or product ID.
                Example: [{"url": "https://...", "country": "US"}].
        async_mode: True returns snapshot_id (poll with scrape_poll).
        output_format: "json", "ndjson", "jsonl", or "csv".

    Returns:
        sync:  {"dataset_id": "...", "results": [...]}
        async: {"dataset_id": "...", "snapshot_id": "..."}
    """
    _require_api_key()
    output_format = _validate_choice(
        output_format, {"json", "ndjson", "jsonl", "csv"}, "output_format"
    )
    real_id = resolve_dataset(dataset)

    if urls is not None and inputs is not None:
        raise ValueError("Pass either urls or inputs, not both.")
    if inputs is not None:
        input_rows = _coerce_scrape_inputs(inputs)
    else:
        url_list = _coerce_to_list(urls)
        if not url_list:
            return {
                "error": "No scraper inputs provided.",
                "hint": "Pass urls=[...] or dataset-specific inputs=[{...}].",
            }
        input_rows = [{"url": _validate_url(url, "scraper URL")} for url in url_list]

    if not async_mode and len(input_rows) > 20:
        raise ValueError(
            "Synchronous scraping accepts at most 20 inputs; set async_mode=True for larger batches."
        )
    endpoint = DATASETS_TRIGGER if async_mode else DATASETS_SCRAPE
    timeout = 60 if async_mode else 120
    response = requests.post(
        endpoint,
        params={"dataset_id": real_id, "format": output_format},
        headers=headers,
        json=input_rows,
        timeout=timeout,
    )
    if response.status_code >= 400:
        return _scrape_error(response, real_id, async_mode)
    if async_mode or response.status_code == 202:
        data = _response_json(response)
        snapshot_id = data.get("snapshot_id") if isinstance(data, dict) else None
        if not snapshot_id:
            raise ValueError(
                f"Bright Data accepted the scrape but returned no snapshot_id: {data}"
            )
        return {
            "dataset_id": real_id,
            "snapshot_id": snapshot_id,
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
    output_format: Literal["json", "ndjson", "jsonl", "csv"] = "json",
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
    _require_api_key()
    output_format = _validate_choice(
        output_format, {"json", "ndjson", "jsonl", "csv"}, "output_format"
    )
    snapshot_id = str(snapshot_id).strip()
    if not snapshot_id or any(char in snapshot_id for char in "/?#"):
        raise ValueError("snapshot_id is invalid")
    if max_wait_seconds < 0:
        raise ValueError("max_wait_seconds must be non-negative")
    deadline = time.monotonic() + max(0, max_wait_seconds)
    while True:
        r = requests.get(
            f"{DATASETS_PROGRESS}/{snapshot_id}", headers=headers, timeout=30
        )
        if r.status_code == 404:
            return {"error": "Snapshot not found", "snapshot_id": snapshot_id}
        _raise_for_api_error(r, "Snapshot progress")
        progress = _response_json(r)
        status = str(progress.get("status", "")).lower()
        if status == "ready":
            download = requests.get(
                f"{DATASETS_SNAPSHOT}/{snapshot_id}",
                headers=headers,
                params={"format": output_format},
                timeout=120,
            )
            _raise_for_api_error(download, "Snapshot download")
            results = (
                _response_json(download) if output_format == "json" else download.text
            )
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
def list_datasets(
    force_refresh: bool = False,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """
    List datasets from the live account catalog (cached 1h). Use query to find
    matching names dynamically. Pagination prevents oversized MCP responses.
    """
    _require_api_key()
    if not 1 <= limit <= 2000:
        raise ValueError("limit must be between 1 and 2000")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    datasets = _get_catalog(force_refresh=force_refresh)
    if not datasets:
        return {
            "count": 0,
            "total": 0,
            "datasets": [],
            "error": _DATASET_CATALOG_CACHE.get("error")
            or "Live dataset catalog unavailable or empty.",
            "note": "Try again, or pass a bare dataset_id starting with 'gd_'.",
        }
    if query and query.strip():
        ranked = sorted(
            (
                (_dataset_match_score(query, ds.get("name", "")), ds)
                for ds in datasets
                if _dataset_name_matches(query, ds.get("name", ""))
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        filtered = [dict(ds, match_score=round(score, 4)) for score, ds in ranked]
    else:
        filtered = datasets
    page = filtered[offset : offset + limit]
    return {
        "count": len(page),
        "total": len(filtered),
        "catalog_total": len(datasets),
        "offset": offset,
        "limit": limit,
        "datasets": page,
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
    print(
        f"[brightdata-free-mcp] Host: {args.host}, Port: {args.port}, Path: {args.path}",
        file=sys.stderr,
    )
    print(
        "[brightdata-free-mcp] Free pool: 5,000 credits/month "
        "(Web Scraper + SERP + Web Unlocker); Discover is separate",
        file=sys.stderr,
    )
    print(
        f"[brightdata-free-mcp] API token configured: "
        f"{bool(API_TOKEN and API_TOKEN != 'YOUR_API_KEY')}",
        file=sys.stderr,
    )

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
