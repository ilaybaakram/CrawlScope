import unittest
from unittest.mock import patch

import httpx

import crawler

START_URL = "https://example.com/"

ROBOTS_TXT = "User-agent: *\nDisallow: /private\n"

START_HTML = """
<html><head><title>Home</title></head><body>
<a href="/ok">ok</a>
<a href="/ok#section">ok again</a>
<a href="/missing">missing</a>
<a href="/private/page">private</a>
<a href="/head-unsupported">no head</a>
<a href="/down">down</a>
<a href="https://other.com/page">external</a>
<a href="mailto:hi@example.com">mail</a>
<a>no href</a>
</body></html>
"""


def make_response(method: str, url: str, status: int, text: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        text=text,
        headers={"content-type": "text/html"},
        request=httpx.Request(method, url),
    )


class FakeStream:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CheckLinkTests(unittest.TestCase):
    def test_head_status_is_returned(self):
        with patch("crawler.httpx.head", return_value=make_response("HEAD", "https://a.com/", 404)):
            self.assertEqual(crawler.check_link("https://a.com/", {}), (404, None))

    def test_falls_back_to_get_when_head_not_allowed(self):
        with patch("crawler.httpx.head", return_value=make_response("HEAD", "https://a.com/", 405)), \
             patch("crawler.httpx.stream", return_value=FakeStream(200)):
            self.assertEqual(crawler.check_link("https://a.com/", {}), (200, None))

    def test_request_error_is_returned_not_raised(self):
        with patch("crawler.httpx.head", side_effect=httpx.ConnectError("boom")):
            status, error = crawler.check_link("https://a.com/", {})
        self.assertIsNone(status)
        self.assertIn("boom", error)

    def test_classify_link(self):
        self.assertEqual(crawler.classify_link(200, None), "working")
        self.assertEqual(crawler.classify_link(301, None), "working")
        self.assertEqual(crawler.classify_link(400, None), "broken")
        self.assertEqual(crawler.classify_link(500, None), "broken")
        self.assertEqual(crawler.classify_link(None, "timeout"), "error")


class CrawlLinkCheckTests(unittest.TestCase):
    def run_crawl(self, **kwargs):
        get_calls = []
        head_calls = []

        def fake_get(url, **_):
            get_calls.append(url)
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return make_response("GET", url, 200, START_HTML)

        head_statuses = {
            "https://example.com/ok": 200,
            "https://example.com/missing": 404,
            "https://example.com/head-unsupported": 405,
        }

        def fake_head(url, **_):
            head_calls.append(url)
            if url.endswith("/down"):
                raise httpx.ConnectError("connection refused")
            return make_response("HEAD", url, head_statuses[url])

        link_results = []
        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch("crawler.httpx.stream", return_value=FakeStream(200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(
                START_URL, link_results=link_results, **kwargs
            )
        return pages, link_results, get_calls, head_calls

    def test_links_are_checked_and_classified(self):
        pages, link_results, _, _ = self.run_crawl(max_pages=1, max_depth=0)
        by_url = {item["url"]: item for item in link_results}

        self.assertEqual(by_url["https://example.com/ok"]["result"], "working")
        self.assertEqual(by_url["https://example.com/missing"]["result"], "broken")
        self.assertEqual(by_url["https://example.com/missing"]["status_code"], 404)
        self.assertEqual(by_url["https://example.com/head-unsupported"]["result"], "working")
        self.assertEqual(by_url["https://example.com/down"]["result"], "error")
        self.assertIsNone(by_url["https://example.com/down"]["status_code"])
        self.assertEqual(by_url["https://example.com/private/page"]["result"], "skipped")

    def test_filtered_links_are_not_checked(self):
        _, link_results, _, head_calls = self.run_crawl(max_pages=1, max_depth=0)
        checked = {item["url"] for item in link_results}

        self.assertNotIn("https://other.com/page", checked)
        self.assertFalse(any("mailto" in url for url in checked))
        self.assertNotIn("https://example.com/private/page", head_calls)

    def test_duplicate_links_checked_once(self):
        _, link_results, _, head_calls = self.run_crawl(max_pages=1, max_depth=0)

        urls = [item["url"] for item in link_results]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(head_calls.count("https://example.com/ok"), 1)

    def test_link_checking_does_not_crawl_extra_pages(self):
        pages, _, get_calls, _ = self.run_crawl(max_pages=1, max_depth=0)

        self.assertEqual(len(pages), 1)
        page_fetches = [url for url in get_calls if not url.endswith("/robots.txt")]
        self.assertEqual(page_fetches, [START_URL])

    def test_max_link_checks_limits_requests(self):
        _, link_results, _, head_calls = self.run_crawl(
            max_pages=1, max_depth=0, max_link_checks=2
        )

        self.assertLessEqual(len(head_calls), 2)
        requested = [item for item in link_results if item["result"] != "skipped"]
        self.assertLessEqual(len(requested), 2)


if __name__ == "__main__":
    unittest.main()
