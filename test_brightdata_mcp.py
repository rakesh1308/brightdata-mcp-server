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
        server._DATASET_CATALOG_CACHE.update(data=None, fetched_at=0.0)

    @patch.object(server.requests, "post")
    def test_search_engine_uses_serp_contract(self, post):
        post.return_value = FakeResponse(json_data={"organic": [{"title": "Result"}]})

        result = server.search_engine("test query", country="US")

        self.assertEqual(result["organic"][0]["title"], "Result")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["format"], "raw")
        self.assertEqual(payload["data_format"], "parsed_light")
        self.assertEqual(payload["country"], "us")

    @patch.object(server, "search_engine")
    def test_search_engine_batch_preserves_input_order(self, search):
        search.side_effect = lambda query, *_: {"query": query}

        result = server.search_engine_batch(["first", "second"])

        self.assertEqual([item["query"] for item in result["results"]], ["first", "second"])
        self.assertTrue(all(item["ok"] for item in result["results"]))

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

    @patch.object(server.requests, "post")
    def test_scrape_batch_requests_markdown_and_preserves_order(self, post):
        post.side_effect = lambda *_, **kwargs: FakeResponse(
            text=f"# {kwargs['json']['url'].rsplit('/', 1)[-1]}"
        )

        result = server.scrape_batch(["https://example.com/a", "https://example.com/b"])

        self.assertEqual([item["url"] for item in result["results"]], [
            "https://example.com/a", "https://example.com/b"
        ])
        self.assertTrue(all(item["markdown"].startswith("# ") for item in result["results"]))
        self.assertTrue(all(call.kwargs["json"]["data_format"] == "markdown" for call in post.call_args_list))

    @patch.object(server.requests, "get")
    @patch.object(server.requests, "post")
    def test_discover_uses_trigger_then_poll_contract(self, post, get):
        post.return_value = FakeResponse(json_data={"status": "ok", "task_id": "task-1"})
        get.return_value = FakeResponse(json_data={"status": "done", "results": [{"title": "A"}]})

        result = server.discover("AI trends", intent="authoritative", limit=5)

        self.assertEqual(result["status"], "done")
        self.assertEqual(post.call_args.args[0], server.DISCOVER_URL)
        self.assertEqual(post.call_args.kwargs["json"]["num_results"], 5)
        self.assertEqual(get.call_args.kwargs["params"], {"task_id": "task-1"})

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    @patch.object(server.requests, "post")
    def test_scrape_sync_handles_json(self, post, _resolve):
        post.return_value = FakeResponse(json_data=[{"name": "item"}])

        result = server.scrape("example", ["https://example.com/item"])

        self.assertEqual(result["results"], [{"name": "item"}])
        self.assertEqual(post.call_args.kwargs["json"], [{"url": "https://example.com/item"}])

    @patch.object(server, "resolve_dataset", return_value="gd_test")
    @patch.object(server.requests, "post")
    def test_scrape_sync_handles_202_as_snapshot(self, post, _resolve):
        post.return_value = FakeResponse(status_code=202, json_data={"snapshot_id": "s_123"})

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

    @patch.object(server.requests, "get")
    def test_list_datasets_uses_documented_endpoint(self, get):
        get.return_value = FakeResponse(json_data=[{"id": "gd_test", "name": "Test", "size": 1}])

        result = server.list_datasets(force_refresh=True)

        self.assertEqual(result["count"], 1)
        self.assertEqual(get.call_args.args[0], server.DATASETS_LIST)

    def test_known_aliases_match_live_catalog_names(self):
        server._DATASET_CATALOG_CACHE.update(
            data={"datasets": [
                {"id": "gd_reviews", "name": "Amazon Reviews"},
                {"id": "gd_instagram", "name": "Instagram - Profiles"},
            ]},
            fetched_at=server.time.time(),
        )

        self.assertEqual(server.resolve_dataset("amazon_product_reviews"), "gd_reviews")
        self.assertEqual(server.resolve_dataset("instagram_profile"), "gd_instagram")


if __name__ == "__main__":
    unittest.main()
