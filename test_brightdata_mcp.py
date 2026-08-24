import unittest
from unittest.mock import patch

import requests

import brightdata_mcp as server


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class BrightDataMCPTests(unittest.TestCase):
    def setUp(self):
        self.original_config = (
            server.API_TOKEN,
            server.SERP_ZONE,
            server.UNLOCKER_ZONE,
        )
        server.API_TOKEN = "test-api-key"
        server.SERP_ZONE = "test-serp-zone"
        server.UNLOCKER_ZONE = "test-unlocker-zone"
        server._DATASET_CATALOG_CACHE.update(data=None, fetched_at=0.0, error=None)

    def tearDown(self):
        server.API_TOKEN, server.SERP_ZONE, server.UNLOCKER_ZONE = self.original_config

    @patch.object(server.requests, "post")
    def test_search_engine_uses_serp_contract(self, post):
        post.return_value = FakeResponse(json_data={"organic": [{"title": "Result"}]})

        result = server.search_engine("test query", country="US")

        self.assertEqual(result["organic"][0]["title"], "Result")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["format"], "raw")
        self.assertEqual(payload["data_format"], "parsed_light")
        self.assertEqual(payload["country"], "us")

    @patch.object(server.requests, "post")
    def test_bing_search_uses_markdown_and_pagination(self, post):
        post.return_value = FakeResponse(text="# Results")

        result = server.search_engine("coffee, machines", engine="bing", cursor=2)

        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["data_format"], "markdown")
        self.assertIn("q=coffee%2C+machines", payload["url"])
        self.assertIn("first=21", payload["url"])
        self.assertEqual(result["markdown"], "# Results")

    @patch.object(server.requests, "post")
    def test_search_error_includes_bright_data_body(self, post):
        post.return_value = FakeResponse(
            status_code=400,
            json_data={"error": "invalid zone"},
        )

        with self.assertRaisesRegex(requests.HTTPError, "invalid zone"):
            server.search_engine("test")

    @patch.object(server, "search_engine")
    def test_search_engine_batch_preserves_input_order(self, search):
        search.side_effect = lambda query, *_: {"query": query}

        result = server.search_engine_batch(["first", "second"])

        self.assertEqual(
            [item["query"] for item in result["results"]], ["first", "second"]
        )
        self.assertTrue(all(item["ok"] for item in result["results"]))

    @patch.object(server, "search_engine")
    def test_search_engine_batch_accepts_per_query_options(self, search):
        search.return_value = {"markdown": "ok"}

        server.search_engine_batch(
            [
                {
                    "query": "second page",
                    "engine": "yandex",
                    "country": "DE",
                    "cursor": 3,
                }
            ]
        )

        self.assertEqual(search.call_args.args[0], "second page")
        self.assertEqual(search.call_args.args[1], "yandex")
        self.assertEqual(search.call_args.args[2], "DE")
        self.assertEqual(search.call_args.args[5], 3)

    def test_plain_commas_are_not_split_into_multiple_items(self):
        self.assertEqual(
            server._coerce_to_list("coffee, machines"), ["coffee, machines"]
        )

    @patch.object(server.requests, "post")
    def test_scrape_as_markdown_requests_native_markdown(self, post):
        post.return_value = FakeResponse(text="# Example")

        result = server.scrape_as_markdown("https://example.com")

        self.assertEqual(result, "# Example")
        self.assertEqual(post.call_args.kwargs["json"]["data_format"], "markdown")

    @patch.object(server.requests, "post")
    def test_scrape_as_html_requests_raw_content(self, post):
        content = "<h1>Example</h1>" + ("x" * 50000)
        post.return_value = FakeResponse(text=content)

        result = server.scrape_as_html("https://example.com")

        self.assertEqual(result, content)
        self.assertNotIn("data_format", post.call_args.kwargs["json"])

    def test_page_tools_reject_non_http_urls(self):
        with self.assertRaisesRegex(ValueError, "HTTP or HTTPS"):
            server.scrape_as_markdown("file:///etc/passwd")

    @patch.object(server.requests, "post")
    def test_scrape_batch_requests_markdown_and_preserves_order(self, post):
        post.side_effect = lambda *_, **kwargs: FakeResponse(
            text=f"# {kwargs['json']['url'].rsplit('/', 1)[-1]}"
        )

        result = server.scrape_batch(["https://example.com/a", "https://example.com/b"])

        self.assertEqual(
            [item["url"] for item in result["results"]],
            ["https://example.com/a", "https://example.com/b"],
        )
        self.assertTrue(
            all(item["markdown"].startswith("# ") for item in result["results"])
        )
        self.assertTrue(
            all(
                call.kwargs["json"]["data_format"] == "markdown"
                for call in post.call_args_list
            )
        )

    @patch.object(server.requests, "get")
    @patch.object(server.requests, "post")
    def test_discover_uses_trigger_then_poll_contract(self, post, get):
        post.return_value = FakeResponse(
            json_data={"status": "ok", "task_id": "task-1"}
        )
        get.return_value = FakeResponse(
            json_data={"status": "done", "results": [{"title": "A"}]}
        )

        result = server.discover("AI trends", intent="authoritative", limit=5)

        self.assertEqual(result["status"], "done")
        self.assertEqual(post.call_args.args[0], server.DISCOVER_URL)
        self.assertEqual(post.call_args.kwargs["json"]["num_results"], 5)
        self.assertEqual(post.call_args.kwargs["json"]["mode"], "standard")
        self.assertEqual(get.call_args.kwargs["params"], {"task_id": "task-1"})

    @patch.object(server.requests, "post")
    def test_discover_supports_current_search_modes(self, post):
        post.return_value = FakeResponse(json_data={"task_id": "task-1"})

        server.discover("broad research", mode="zeroRanking", max_wait_seconds=0)

        self.assertEqual(post.call_args.kwargs["json"]["mode"], "zeroRanking")

    def test_discover_rejects_unsupported_zero_ranking_content(self):
        with self.assertRaisesRegex(ValueError, "include_content"):
            server.discover("broad research", mode="zeroRanking", include_content=True)

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    @patch.object(server.requests, "post")
    def test_scrape_sync_handles_json(self, post, _resolve):
        post.return_value = FakeResponse(json_data=[{"name": "item"}])

        result = server.scrape("example", ["https://example.com/item"])

        self.assertEqual(result["results"], [{"name": "item"}])
        self.assertEqual(
            post.call_args.kwargs["json"], [{"url": "https://example.com/item"}]
        )
        self.assertEqual(
            post.call_args.kwargs["params"], {"dataset_id": "gd_test", "format": "json"}
        )

    @patch.object(server, "resolve_dataset", return_value="gd_shopping")
    @patch.object(server.requests, "post")
    def test_scrape_accepts_dataset_specific_inputs(self, post, _resolve):
        post.return_value = FakeResponse(json_data=[{"title": "Headphones"}])
        inputs = [
            {
                "url": "https://www.google.com/search?ibp=oshop&q=headphones",
                "country": "US",
            }
        ]

        result = server.scrape("google shopping", inputs=inputs)

        self.assertEqual(result["results"], [{"title": "Headphones"}])
        self.assertEqual(post.call_args.kwargs["json"], inputs)

    @patch.object(server, "resolve_dataset", return_value="gd_shopping")
    @patch.object(server.requests, "post")
    def test_scrape_surfaces_validation_details_without_async_retry(
        self, post, _resolve
    ):
        post.return_value = FakeResponse(
            status_code=400,
            json_data={
                "error": "Invalid input provided",
                "code": "validation_error",
                "errors": [["url", "Value should match pattern ^https://..."]],
            },
        )

        result = server.scrape(
            "google shopping", ["https://google.com/search?tbm=shop&q=x"]
        )

        self.assertEqual(result["status_code"], 400)
        self.assertEqual(result["details"]["code"], "validation_error")
        self.assertIn("inputs=[{...}]", result["hint"])
        self.assertFalse(result["retryable"])
        post.assert_called_once()

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    @patch.object(server.requests, "post")
    def test_scrape_explains_non_collectable_catalog_entry(self, post, _resolve):
        post.return_value = FakeResponse(
            status_code=400,
            json_data=ValueError("not JSON"),
            text="This dataset does not support collection",
        )

        result = server.scrape("example test", ["https://example.com"])

        self.assertEqual(result["details"], "This dataset does not support collection")
        self.assertIn("cannot be collected", result["hint"])
        self.assertIn("Retrying asynchronously will not fix", result["hint"])
        post.assert_called_once()

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    def test_scrape_rejects_urls_and_inputs_together(self, _resolve):
        with self.assertRaisesRegex(ValueError, "either urls or inputs"):
            server.scrape(
                "example",
                urls=["https://example.com"],
                inputs=[{"url": "https://example.org"}],
            )

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    @patch.object(server.requests, "post")
    def test_scrape_sync_handles_202_as_snapshot(self, post, _resolve):
        post.return_value = FakeResponse(
            status_code=202, json_data={"snapshot_id": "s_123"}
        )

        result = server.scrape("example", ["https://example.com/item"])

        self.assertEqual(result["snapshot_id"], "s_123")
        self.assertEqual(result["status"], "running")

    @patch.object(server.requests, "get")
    def test_scrape_poll_checks_progress_then_downloads(self, get):
        get.side_effect = [
            FakeResponse(json_data={"status": "ready", "snapshot_id": "s_123"}),
            FakeResponse(json_data=[{"name": "item"}]),
        ]

        result = server.scrape_poll("s_123", max_wait_seconds=0)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["results"], [{"name": "item"}])
        self.assertIn("/progress/s_123", get.call_args_list[0].args[0])
        self.assertIn("/snapshot/s_123", get.call_args_list[1].args[0])

    def test_scrape_poll_rejects_path_injection(self):
        with self.assertRaisesRegex(ValueError, "snapshot_id is invalid"):
            server.scrape_poll("../account", max_wait_seconds=0)

    @patch.object(server.requests, "get")
    def test_list_datasets_uses_documented_endpoint(self, get):
        get.return_value = FakeResponse(
            json_data=[{"id": "gd_test", "name": "Test", "size": 1}]
        )

        result = server.list_datasets(force_refresh=True)

        self.assertEqual(result["count"], 1)
        self.assertEqual(get.call_args.args[0], server.DATASETS_LIST)

    @patch.object(server.requests, "get")
    def test_list_datasets_searches_and_paginates_live_catalog(self, get):
        get.return_value = FakeResponse(
            json_data=[
                {"id": "gd_a", "name": "Amazon Products", "size": 1},
                {"id": "gd_b", "name": "LinkedIn Job Listings", "size": 1},
            ]
        )

        result = server.list_datasets(
            force_refresh=True, query="linkedin jobs", limit=1
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["catalog_total"], 2)
        self.assertEqual(result["datasets"][0]["id"], "gd_b")
        self.assertIn("match_score", result["datasets"][0])

    @patch.object(server.requests, "get")
    def test_list_datasets_does_not_return_unrelated_fuzzy_matches(self, get):
        get.return_value = FakeResponse(
            json_data=[
                {"id": "gd_a", "name": "Amazon Products", "size": 1},
            ]
        )

        result = server.list_datasets(force_refresh=True, query="linkedin jobs")

        self.assertEqual(result["count"], 0)

    def test_dynamic_names_match_live_catalog_without_alias_ids(self):
        server._DATASET_CATALOG_CACHE.update(
            data={
                "datasets": [
                    {"id": "gd_reviews", "name": "Amazon Reviews"},
                    {"id": "gd_instagram", "name": "Instagram - Profiles"},
                ]
            },
            fetched_at=server.time.time(),
        )

        self.assertEqual(server.resolve_dataset("amazon_product_reviews"), "gd_reviews")
        self.assertEqual(server.resolve_dataset("instagram_profile"), "gd_instagram")

    def test_ambiguous_dataset_name_is_not_silently_selected(self):
        server._DATASET_CATALOG_CACHE.update(
            data={
                "datasets": [
                    {"id": "gd_products", "name": "Amazon Products"},
                    {"id": "gd_search", "name": "Amazon Products Search"},
                ]
            },
            fetched_at=server.time.time(),
        )

        with self.assertRaisesRegex(ValueError, "unknown or ambiguous"):
            server.resolve_dataset("amazon")

    def test_missing_zone_fails_before_network_call(self):
        server.SERP_ZONE = ""
        with self.assertRaisesRegex(RuntimeError, "SERP_ZONE"):
            server.search_engine("test")


if __name__ == "__main__":
    unittest.main()
