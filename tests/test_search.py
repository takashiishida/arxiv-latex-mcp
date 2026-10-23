"""Offline search regression tests using a real HTTP client with mocked transport."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from arxiv_latex_mcp import search, server

FEED = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/solv-int/9507001v12</id>
    <title>A title\n with whitespace</title>
    <author><name>A. Author</name></author>
    <summary>An abstract.</summary>
    <published>1995-07-01T00:00:00Z</published>
    <arxiv:primary_category term="nlin.SI"/>
    <category term="nlin.SI"/><category term="math-ph"/>
    <link title="pdf" href="https://arxiv.org/pdf/solv-int/9507001v12"/>
  </entry>
</feed>'''
EMPTY_FEED = '<feed xmlns="http://www.w3.org/2005/Atom"/>'


class SearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 100.0
        self.requests = []
        self.request_times = []
        self.active_requests = 0
        self.max_active_requests = 0
        self.response = httpx.Response(200, text=FEED)
        client_type = httpx.AsyncClient

        async def transport(request):
            self.requests.append(request)
            self.request_times.append(self.now)
            self.active_requests += 1
            self.max_active_requests = max(self.max_active_requests, self.active_requests)
            try:
                await asyncio.sleep(0)
                if isinstance(self.response, list):
                    response = self.response.pop(0)
                else:
                    response = self.response
                if isinstance(response, Exception):
                    raise response
                return response
            finally:
                self.active_requests -= 1

        def make_client(**kwargs):
            return client_type(transport=httpx.MockTransport(transport), **kwargs)

        async def sleep(delay):
            self.now += delay
            await asyncio.sleep(0)

        for target, name, value in (
            (search, "_request_lock", asyncio.Lock()),
            (search, "_last_request_time", 0.0),
            (search, "time", SimpleNamespace(monotonic=lambda: self.now)),
            (search, "asyncio", SimpleNamespace(sleep=sleep)),
            (search.httpx, "AsyncClient", make_client),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, **arguments):
        return (await server.handle_call_tool("search_papers", arguments))[0].text

    async def test_reserved_characters_survive_http_encoding(self):
        query = 'ti:"C# & C++: 100%"'
        result = await self.call(query=query, date_from="2026-01-01", date_to="2026-01-31", categories=["cs.LG"], max_results=5)
        self.assertEqual(json.loads(result)["total_results"], 1)
        url = self.requests[0].url
        self.assertEqual(url.host, "export.arxiv.org")
        self.assertEqual(url.fragment, "")
        self.assertEqual(url.params["search_query"], f'({query}) AND (cat:cs.LG) AND submittedDate:[202601010000 TO 202601312359]')
        self.assertEqual(url.params["max_results"], "5")

    async def test_dated_and_undated_searches_return_identical_metadata(self):
        plain = await self.call(query="integrable systems", categories=["math-ph", "nlin.SI"])
        dated = await self.call(query="integrable systems", categories=["math-ph", "nlin.SI"], date_to="2000-01-01")
        self.assertEqual(json.loads(plain), json.loads(dated))
        self.assertEqual(self.requests[0].url.params["search_query"], "(integrable systems) AND (cat:math-ph OR cat:nlin.SI)")
        self.assertIn("submittedDate:[199107010000 TO 200001012359]", self.requests[1].url.params["search_query"])

    async def test_concurrent_searches_share_one_throttle(self):
        await asyncio.gather(
            self.call(query="one"),
            self.call(query="two"),
            self.call(query="three", date_from="2020-01-01", date_to="2020-12-31"),
        )
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.max_active_requests, 1)
        for first, second in zip(self.request_times, self.request_times[1:]):
            self.assertGreaterEqual(second - first, 3.0)

    async def test_timeout_retry_shares_throttle_with_other_searches(self):
        self.response = [httpx.ReadTimeout("timeout"), httpx.Response(200, text=FEED), httpx.Response(200, text=FEED)]
        results = await asyncio.gather(self.call(query="one"), self.call(query="two", date_to="2020-01-01"))
        self.assertTrue(all(json.loads(result)["total_results"] == 1 for result in results))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.max_active_requests, 1)
        for first, second in zip(self.request_times, self.request_times[1:]):
            self.assertGreaterEqual(second - first, 3.0)

    async def test_timeout_retries_only_once(self):
        self.response = httpx.ReadTimeout("timeout")
        self.assertIn("timed out", await self.call(query="test"))
        self.assertEqual(len(self.requests), 2)

    async def test_rate_limit_is_reported_without_retry(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.response = httpx.Response(status)
                before = len(self.requests)
                self.assertIn("Wait 60s", await self.call(query="test"))
                self.assertEqual(len(self.requests), before + 1)

    async def test_connection_and_http_errors_are_actionable(self):
        self.response = httpx.ConnectError("offline")
        self.assertIn("could not be reached", await self.call(query="test"))
        self.response = httpx.Response(500)
        self.assertIn("HTTP 500", await self.call(query="test"))

    async def test_invalid_inputs_fail_before_network_access(self):
        invalid = [
            {}, {"query": " "}, {"query": 123},
            {"query": "test", "max_results": 0},
            {"query": "test", "max_results": -1},
            {"query": "test", "max_results": True},
            {"query": "test", "max_results": "5"},
            {"query": "test", "sort_by": "unknown"},
            {"query": "test", "date_from": "2026-02-30"},
            {"query": "test", "date_from": "20260101"},
            {"query": "test", "date_to": None},
            {"query": "test", "date_from": "2026-02-01", "date_to": "2026-01-01"},
            {"query": "test", "categories": "cs.LG"},
            {"query": "test", "categories": [None]},
            {"query": "test", "categories": ["unknown.AI"]},
            {"query": "test", "categories": ["cs.AI) OR all:test"]},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertTrue((await self.call(**arguments)).startswith("Error:"))
        self.assertEqual(self.requests, [])

    async def test_maximum_and_sort_order(self):
        await self.call(query="test", max_results=100, sort_by="date")
        self.assertEqual(self.requests[0].url.params["max_results"], "50")
        self.assertEqual(self.requests[0].url.params["sortBy"], "submittedDate")
        self.assertEqual(self.requests[0].url.params["sortOrder"], "descending")

    async def test_empty_results_are_successful(self):
        self.response = httpx.Response(200, text=EMPTY_FEED)
        self.assertEqual(json.loads(await self.call(query="test")), {"total_results": 0, "papers": []})

    async def test_api_error_feed_is_not_returned_as_a_paper(self):
        self.response = httpx.Response(200, text='''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <id>http://arxiv.org/api/errors#bad_query</id><title>Error</title><summary>Invalid search query</summary>
        </entry></feed>''')
        self.assertEqual(await self.call(query="test"), "Error: Invalid search query")

    async def test_malformed_responses_are_errors(self):
        for body in ("not XML", "<html>Service unavailable</html>"):
            with self.subTest(body=body):
                self.response = httpx.Response(200, text=body)
                self.assertTrue((await self.call(query="test")).startswith("Error:"))

    async def test_tool_is_discoverable_and_documented(self):
        tools = {tool.name: tool for tool in await server.handle_list_tools()}
        self.assertEqual(len(tools), 5)
        self.assertEqual(tools["search_papers"].inputSchema["required"], ["query"])
        self.assertIn("search_papers", server.SERVER_INSTRUCTIONS)


class AtomTests(unittest.TestCase):
    def test_legacy_ids_only_lose_trailing_version(self):
        for paper_id, expected in (
            ("solv-int/9507001v12", "solv-int/9507001"),
            ("solv-int/9507001", "solv-int/9507001"),
            ("hep-th/9711200v1", "hep-th/9711200"),
            ("2401.00001v2", "2401.00001"),
        ):
            with self.subTest(paper_id=paper_id):
                feed = FEED.replace("solv-int/9507001v12", paper_id)
                self.assertEqual(search._parse_arxiv_atom(feed)[0]["id"], expected)

    def test_metadata_and_categories(self):
        paper = search._parse_arxiv_atom(FEED)[0]
        self.assertEqual(paper["title"], "A title with whitespace")
        self.assertEqual(paper["authors"], ["A. Author"])
        self.assertEqual(paper["categories"], ["nlin.SI", "math-ph"])
        self.assertEqual(paper["published"], "1995-07-01T00:00:00Z")
        self.assertEqual(paper["url"], "https://arxiv.org/pdf/solv-int/9507001v12")


if __name__ == "__main__":
    unittest.main()
