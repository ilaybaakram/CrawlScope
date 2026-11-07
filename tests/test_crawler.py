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


class SeoAuditTests(unittest.TestCase):
    def crawl_single_page(self, html: str, content_type: str = "text/html; charset=utf-8"):
        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return httpx.Response(
                200,
                text=html,
                headers={"content-type": content_type},
                request=httpx.Request("GET", url),
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_missing_title_is_detected(self):
        page = self.crawl_single_page(
            '<html><head><meta name="description" content="Desc"></head>'
            "<body><h1>Heading</h1></body></html>"
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(issue_types, ["missing_title"])
        self.assertEqual(page["seo_issues"][0]["severity"], "warning")
        self.assertEqual(page["seo_issues"][0]["url"], START_URL)

    def test_missing_meta_description_is_detected(self):
        page = self.crawl_single_page(
            "<html><head><title>Valid Title</title></head>"
            "<body><h1>Heading</h1></body></html>"
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(issue_types, ["missing_meta_description"])

    def test_missing_h1_is_detected(self):
        page = self.crawl_single_page(
            '<html><head><title>Valid Title</title>'
            '<meta name="description" content="Desc"></head>'
            "<body><p>No H1 here</p></body></html>"
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(issue_types, ["missing_h1"])

    def test_whitespace_only_metadata_is_treated_as_missing(self):
        page = self.crawl_single_page(
            '<html><head><title>   \n\t  </title>'
            '<meta name="description" content="   "></head>'
            "<body><h1>   </h1></body></html>"
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(
            issue_types,
            ["missing_title", "missing_meta_description", "missing_h1"],
        )
        self.assertIsNone(page["meta_description"])
        self.assertIsNone(page["h1"])

    def test_html_page_with_complete_metadata_has_no_seo_issues(self):
        page = self.crawl_single_page(
            '<html><head><title>Complete Page</title>'
            '<meta name="description" content="A helpful description."></head>'
            "<body><h1>Main Heading</h1></body></html>"
        )
        self.assertEqual(page["seo_issues"], [])

    def test_non_html_page_is_not_flagged_for_seo_issues(self):
        page = self.crawl_single_page(
            '{"status": "ok"}',
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])


class DuplicateSeoAuditTests(unittest.TestCase):
    def crawl_two_pages(
        self,
        first_html: str,
        second_html: str,
        second_content_type: str = "text/html; charset=utf-8",
        second_status: int = 200,
    ):
        page_two_url = "https://example.com/page-2"

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return httpx.Response(
                    200,
                    text=first_html,
                    headers={"content-type": "text/html; charset=utf-8"},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                second_status,
                text=second_html,
                headers={"content-type": second_content_type},
                request=httpx.Request("GET", url),
            )

        def fake_head(url, **_):
            return make_response("HEAD", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch("crawler.time.sleep"):
            return crawler.crawl_website(
                START_URL,
                max_pages=2,
                max_depth=1,
            )

    def test_duplicate_titles_detected_across_pages(self):
        pages = self.crawl_two_pages(
            '<html><head><title>Shared Title</title>'
            '<meta name="description" content="Desc 1"></head>'
            '<body><h1>H1 One</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Shared Title</title>'
            '<meta name="description" content="Desc 2"></head>'
            "<body><h1>H1 Two</h1></body></html>",
        )
        self.assertEqual(len(pages), 2)
        for page in pages:
            types = [issue["type"] for issue in page["seo_issues"]]
            self.assertIn("duplicate_title", types)
            dup_issue = next(
                issue for issue in page["seo_issues"] if issue["type"] == "duplicate_title"
            )
            self.assertEqual(dup_issue["severity"], "warning")
            self.assertEqual(dup_issue["url"], page["url"])
            self.assertIn("https://example.com/", dup_issue["message"])
            self.assertIn("https://example.com/page-2", dup_issue["message"])

    def test_duplicate_meta_descriptions_detected_across_pages(self):
        pages = self.crawl_two_pages(
            '<html><head><title>Title 1</title>'
            '<meta name="description" content="Same description here"></head>'
            '<body><h1>H1 One</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Title 2</title>'
            '<meta name="description" content="Same description here"></head>'
            "<body><h1>H1 Two</h1></body></html>",
        )
        for page in pages:
            types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(types, ["duplicate_meta_description"])

    def test_duplicate_h1_detected_across_pages(self):
        pages = self.crawl_two_pages(
            '<html><head><title>Title 1</title>'
            '<meta name="description" content="Desc 1"></head>'
            '<body><h1>Identical Heading</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Title 2</title>'
            '<meta name="description" content="Desc 2"></head>'
            "<body><h1>Identical Heading</h1></body></html>",
        )
        for page in pages:
            types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(types, ["duplicate_h1"])

    def test_case_and_whitespace_differences_still_match_as_duplicates(self):
        pages = self.crawl_two_pages(
            '<html><head><title>  Welcome   To   CrawlScope  </title>'
            '<meta name="description" content="  Best   SEO   Tool "></head>'
            '<body><h1>  Main   Header </h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>welcome to crawlscope</title>'
            '<meta name="description" content="best seo tool"></head>'
            "<body><h1>main\n\theader</h1></body></html>",
        )
        for page in pages:
            types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(
                types,
                [
                    "duplicate_title",
                    "duplicate_meta_description",
                    "duplicate_h1",
                ],
            )

    def test_empty_or_missing_metadata_is_not_classified_as_duplicate(self):
        pages = self.crawl_two_pages(
            '<html><head><title>   </title></head>'
            '<body><a href="/page-2">Next</a></body></html>',
            "<html><head></head><body><h1>   </h1></body></html>",
        )
        for page in pages:
            types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(
                types,
                ["missing_title", "missing_meta_description", "missing_h1"],
            )

    def test_unique_metadata_produces_no_duplicate_issues(self):
        pages = self.crawl_two_pages(
            '<html><head><title>Title One</title>'
            '<meta name="description" content="Description One"></head>'
            '<body><h1>Heading One</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Title Two</title>'
            '<meta name="description" content="Description Two"></head>'
            "<body><h1>Heading Two</h1></body></html>",
        )
        for page in pages:
            self.assertEqual(page["seo_issues"], [])

    def test_non_html_and_failed_pages_excluded_from_duplicate_comparison(self):
        non_html_pages = self.crawl_two_pages(
            '<html><head><title>Shared Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Shared Title</title></head></html>',
            second_content_type="application/json",
        )
        self.assertEqual(non_html_pages[0]["seo_issues"], [])
        self.assertEqual(non_html_pages[1]["seo_issues"], [])

        failed_pages = self.crawl_two_pages(
            '<html><head><title>Shared Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><a href="/page-2">Next</a></body></html>',
            '<html><head><title>Shared Title</title></head></html>',
            second_status=404,
        )
        self.assertEqual(failed_pages[0]["seo_issues"], [])
        self.assertEqual(failed_pages[1]["seo_issues"], [])


class ImageSeoAuditTests(unittest.TestCase):
    def crawl_page(
        self,
        html: str,
        content_type: str = "text/html; charset=utf-8",
        status_code: int = 200,
    ):
        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return httpx.Response(
                status_code,
                text=html,
                headers={"content-type": content_type},
                request=httpx.Request("GET", url),
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_image_without_alt_attribute_is_detected(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><img src="https://example.com/photo.jpg"></body></html>'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "missing_image_alt")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["image_url"], "https://example.com/photo.jpg")

    def test_image_with_empty_alt_attribute_is_detected(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><img src="https://example.com/photo.jpg" alt=""></body></html>'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "empty_image_alt")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["image_url"], "https://example.com/photo.jpg")

    def test_image_with_whitespace_only_alt_is_detected_as_empty(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><img src="https://example.com/photo.jpg" alt="   \n\t "></body></html>'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        self.assertEqual(page["seo_issues"][0]["type"], "empty_image_alt")

    def test_image_with_meaningful_alt_text_produces_no_issue(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1>'
            '<img src="https://example.com/photo.jpg" alt="Company team photo">'
            "</body></html>"
        )
        self.assertEqual(page["seo_issues"], [])

    def test_relative_and_protocol_relative_image_urls_are_resolved(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1>'
            '<img src="/assets/logo.png">'
            '<img src="//cdn.example.com/banner.webp" alt="">'
            "</body></html>"
        )
        self.assertEqual(len(page["seo_issues"]), 2)
        self.assertEqual(page["seo_issues"][0]["type"], "missing_image_alt")
        self.assertEqual(
            page["seo_issues"][0]["image_url"],
            "https://example.com/assets/logo.png",
        )
        self.assertEqual(page["seo_issues"][1]["type"], "empty_image_alt")
        self.assertEqual(
            page["seo_issues"][1]["image_url"],
            "https://cdn.example.com/banner.webp",
        )

    def test_image_without_src_is_skipped_and_duplicates_deduplicated(self):
        page = self.crawl_page(
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1>'
            "<img>"
            '<img src="   ">'
            '<img src="/dup.png">'
            '<img src="/dup.png">'
            "</body></html>"
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        self.assertEqual(
            page["seo_issues"][0]["image_url"],
            "https://example.com/dup.png",
        )

    def test_non_html_and_failed_pages_are_excluded_from_image_audit(self):
        non_html_page = self.crawl_page(
            '<img src="/logo.png">',
            content_type="application/json",
        )
        self.assertEqual(non_html_page["seo_issues"], [])

        failed_page = self.crawl_page(
            '<html><body><img src="/logo.png"></body></html>',
            status_code=500,
        )
        self.assertEqual(failed_page["seo_issues"], [])


class BrokenImageDetectionTests(unittest.TestCase):
    def test_image_404_500_and_200_statuses(self):
        html = (
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            "<body><h1>Heading</h1>"
            '<img src="/ok.png" alt="OK">'
            '<img src="/not-found.png" alt="Missing">'
            '<img src="//cdn.example.com/server-error.png" alt="Error">'
            "</body></html>"
        )
        image_statuses = {
            "https://example.com/ok.png": 200,
            "https://example.com/not-found.png": 404,
            "https://cdn.example.com/server-error.png": 500,
        }

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return make_response("GET", url, 200, html)
            return make_response("GET", url, image_statuses[url])

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)

        broken_issues = [
            issue
            for issue in pages[0]["seo_issues"]
            if issue["type"] == "broken_image"
        ]
        self.assertEqual(len(broken_issues), 2)

        first, second = broken_issues
        self.assertEqual(first["type"], "broken_image")
        self.assertEqual(first["severity"], "warning")
        self.assertEqual(first["url"], START_URL)
        self.assertEqual(first["image_url"], "https://example.com/not-found.png")
        self.assertEqual(first["status_code"], 404)
        self.assertIn("404", first["message"])
        self.assertIn("https://example.com/not-found.png", first["message"])

        self.assertEqual(
            second["image_url"],
            "https://cdn.example.com/server-error.png",
        )
        self.assertEqual(second["status_code"], 500)
        self.assertIn("500", second["message"])

    def test_repeated_image_across_pages_is_checked_once_and_reused(self):
        page_one_html = (
            '<html><head><title>Page 1</title>'
            '<meta name="description" content="Desc 1"></head>'
            '<body><h1>H1 1</h1>'
            '<img src="/shared-broken.png" alt="Shared">'
            '<a href="/page-2">Page 2</a></body></html>'
        )
        page_two_html = (
            '<html><head><title>Page 2</title>'
            '<meta name="description" content="Desc 2"></head>'
            '<body><h1>H1 2</h1>'
            '<img src="/shared-broken.png" alt="Shared">'
            "</body></html>"
        )
        get_calls = []

        def fake_get(url, **_):
            get_calls.append(url)
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return make_response("GET", url, 200, page_one_html)
            if url == "https://example.com/page-2":
                return make_response("GET", url, 200, page_two_html)
            if url == "https://example.com/shared-broken.png":
                return make_response("GET", url, 404)
            return make_response("GET", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", "https://example.com/page-2", 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=2, max_depth=1)

        self.assertEqual(
            get_calls.count("https://example.com/shared-broken.png"),
            1,
        )
        for page in pages:
            broken = [
                i for i in page["seo_issues"] if i["type"] == "broken_image"
            ]
            self.assertEqual(len(broken), 1)
            self.assertEqual(broken[0]["url"], page["url"])
            self.assertEqual(broken[0]["status_code"], 404)

    def test_unique_image_check_limit_is_respected(self):
        html = (
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            "<body><h1>Heading</h1>"
            '<img src="/img1.png" alt="1">'
            '<img src="/img2.png" alt="2">'
            '<img src="/img3.png" alt="3">'
            "</body></html>"
        )
        checked_urls = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return make_response("GET", url, 200, html)
            checked_urls.append(url)
            return make_response("GET", url, 404)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(
                START_URL,
                max_pages=1,
                max_depth=0,
                max_image_checks=2,
            )

        self.assertEqual(len(checked_urls), 2)
        broken = [
            i for i in pages[0]["seo_issues"] if i["type"] == "broken_image"
        ]
        self.assertEqual(len(broken), 2)

    def test_image_request_failure_is_handled_gracefully(self):
        html = (
            '<html><head><title>Title</title>'
            '<meta name="description" content="Desc"></head>'
            '<body><h1>Heading</h1><img src="/timeout.png" alt="Timeout"></body></html>'
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return make_response("GET", url, 200, html)
            raise httpx.ConnectError("image host unreachable")

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)

        self.assertEqual(len(pages), 1)
        self.assertIsNone(pages[0]["error"])
        self.assertEqual(pages[0]["seo_issues"], [])

    def test_non_html_and_failed_pages_do_not_check_images(self):
        image_fetches = []

        def fake_get_non_html(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return httpx.Response(
                    200,
                    text='<img src="/broken.png" alt="x">',
                    headers={"content-type": "application/json"},
                    request=httpx.Request("GET", url),
                )
            image_fetches.append(url)
            return make_response("GET", url, 404)

        with patch("crawler.httpx.get", side_effect=fake_get_non_html), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)

        self.assertEqual(image_fetches, [])
        self.assertEqual(pages[0]["seo_issues"], [])


if __name__ == "__main__":
    unittest.main()

