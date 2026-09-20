"""Authenticated end-to-end smoke test for all nine MCP tools.

This is intentionally excluded from CI because it consumes Bright Data requests.
It prints only pass/fail metadata, never scraped content or credentials.
"""

import json
import os
import sys

import brightdata_mcp as server


def _assert_success(value, label: str):
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"{label}: {value['error']}")
    if isinstance(value, dict) and str(value.get("status", "")).lower() in {
        "failed",
        "error",
    }:
        raise RuntimeError(f"{label}: returned status {value['status']}")
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        failures = [
            item
            for item in value["results"]
            if isinstance(item, dict) and item.get("ok") is False
        ]
        if failures:
            raise RuntimeError(f"{label}: {len(failures)} batch item(s) failed")
    return value


def main() -> int:
    checks = {}

    def run(name, operation):
        try:
            value = _assert_success(operation(), name)
            checks[name] = {"ok": True, "type": type(value).__name__}
            return value
        except Exception as exc:  # noqa: BLE001 - smoke runner must report every tool
            checks[name] = {"ok": False, "error": str(exc)[:500]}
            return None

    run("search_engine", lambda: server.search_engine("Bright Data MCP", country="US"))
    run(
        "search_engine_batch",
        lambda: server.search_engine_batch(
            [
                {"query": "Bright Data API", "engine": "google", "country": "US"},
                {
                    "query": "Bright Data Web Unlocker",
                    "engine": "bing",
                    "country": "US",
                },
            ]
        ),
    )
    run("scrape_as_markdown", lambda: server.scrape_as_markdown("https://example.com"))
    run("scrape_as_html", lambda: server.scrape_as_html("https://example.com"))
    run(
        "scrape_batch",
        lambda: server.scrape_batch(
            [
                "https://example.com",
                "https://example.org",
            ]
        ),
    )
    catalog = run(
        "list_datasets",
        lambda: server.list_datasets(
            force_refresh=True,
            query=os.getenv("BRIGHTDATA_SMOKE_DATASET_NAME", "google shopping"),
            limit=10,
        ),
    )

    dataset_name = os.getenv("BRIGHTDATA_SMOKE_DATASET_NAME", "google shopping")
    input_text = os.getenv(
        "BRIGHTDATA_SMOKE_INPUTS",
        '[{"url":"https://www.google.com/search?ibp=oshop&q=wireless+headphones","country":"US"}]',
    )
    try:
        scraper_inputs = json.loads(input_text)
    except ValueError as exc:
        checks["scrape"] = {
            "ok": False,
            "error": f"Invalid BRIGHTDATA_SMOKE_INPUTS: {exc}",
        }
        checks["scrape_poll"] = {"ok": False, "error": "scrape was not started"}
    else:
        if not catalog:
            checks["scrape"] = {"ok": False, "error": "dataset catalog failed"}
            checks["scrape_poll"] = {"ok": False, "error": "scrape was not started"}
        else:
            run("scrape", lambda: server.scrape(dataset_name, inputs=scraper_inputs))
            async_result = run(
                "scrape_async_trigger",
                lambda: server.scrape(
                    dataset_name, inputs=scraper_inputs, async_mode=True
                ),
            )
            if async_result and async_result.get("snapshot_id"):
                run(
                    "scrape_poll",
                    lambda: server.scrape_poll(
                        async_result["snapshot_id"],
                        max_wait_seconds=180,
                    ),
                )
            else:
                checks["scrape_poll"] = {
                    "ok": False,
                    "error": "async trigger returned no snapshot_id",
                }

    passed = sum(1 for item in checks.values() if item.get("ok"))
    failed = len(checks) - passed
    print(json.dumps({"passed": passed, "failed": failed, "checks": checks}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
