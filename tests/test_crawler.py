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
        if "text/html" in content_type and 'rel="canonical"' not in html:
            html = html.replace(
                "<head>",
                (
                    f'<head><link rel="canonical" href="{START_URL}">'
                    '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"WebPage"}'
                    "</script>"
                ),
                1,
            )

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
        if 'rel="canonical"' not in first_html:
            first_html = first_html.replace(
                "<head>",
                (
                    f'<head><link rel="canonical" href="{START_URL}">'
                    '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"WebPage"}'
                    "</script>"
                ),
                1,
            )
        if "text/html" in second_content_type and 'rel="canonical"' not in second_html:
            second_html = second_html.replace(
                "<head>",
                (
                    f'<head><link rel="canonical" href="{page_two_url}">'
                    '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"WebPage"}'
                    "</script>"
                ),
                1,
            )

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
        if "text/html" in content_type and 'rel="canonical"' not in html:
            html = html.replace(
                "<head>",
                (
                    f'<head><link rel="canonical" href="{START_URL}">'
                    '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"WebPage"}'
                    "</script>"
                ),
                1,
            )

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
            '<meta name="description" content="Desc">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
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


class UrlSeoAuditTests(unittest.TestCase):
    COMPLETE_HTML = (
        '<html><head><title>Valid Title</title>'
        '<meta name="description" content="Valid description"></head>'
        "<body><h1>Valid H1</h1></body></html>"
    )

    def crawl_url(self, target_url: str, **kwargs):
        html_with_canonical = self.COMPLETE_HTML.replace(
            "<head>",
            (
                f'<head><link rel="canonical" href="{target_url}">'
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"WebPage"}'
                "</script>"
            ),
            1,
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return make_response("GET", url, 200, html_with_canonical)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(
                target_url,
                max_pages=1,
                max_depth=0,
                **kwargs,
            )
        return pages[0]

    def test_uppercase_url_path_is_detected(self):
        url = "https://example.com/Products/Shoes"
        page = self.crawl_url(url)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "uppercase_url")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], url)

    def test_underscore_in_url_path_is_detected(self):
        url = "https://example.com/product_guide/seo_tips"
        page = self.crawl_url(url)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "underscore_url")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], url)

    def test_long_url_exceeding_default_threshold_is_detected(self):
        url = "https://example.com/" + ("a" * 85)
        self.assertGreater(len(url), 100)
        page = self.crawl_url(url)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "long_url")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], url)

    def test_configurable_long_url_threshold(self):
        url = "https://example.com/short-path"
        page_flagged = self.crawl_url(url, max_url_length=20)
        self.assertEqual(
            [issue["type"] for issue in page_flagged["seo_issues"]],
            ["long_url"],
        )

        page_ok = self.crawl_url(url, max_url_length=50)
        self.assertEqual(page_ok["seo_issues"], [])

    def test_query_parameters_in_url_are_detected_as_info(self):
        url = "https://example.com/search?page=2&sort=asc"
        page = self.crawl_url(url)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "query_parameter_url")
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["url"], url)

    def test_uppercase_hostname_only_does_not_trigger_uppercase_path_issue(self):
        url = "https://EXAMPLE.COM/clean-path"
        page = self.crawl_url(url)
        self.assertEqual(page["seo_issues"], [])

    def test_clean_url_produces_no_url_issues(self):
        url = "https://example.com/blog/seo-best-practices"
        page = self.crawl_url(url)
        self.assertEqual(page["seo_issues"], [])


class RedirectAuditTests(unittest.TestCase):
    def make_head_response(
        self,
        url: str,
        status: int,
        location: str | None = None,
    ) -> httpx.Response:
        headers = {"content-type": "text/html"}
        if location is not None:
            headers["location"] = location
        return httpx.Response(
            status,
            headers=headers,
            request=httpx.Request("HEAD", url),
        )

    def crawl_with_links(
        self,
        links_html: str,
        head_map: dict[str, httpx.Response | Exception],
        **kwargs,
    ):
        html = (
            '<html><head><title>Home</title>'
            '<meta name="description" content="Home description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><h1>Home Heading</h1>{links_html}</body></html>"
        )
        head_calls: list[str] = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return make_response("GET", url, 200, html)

        def fake_head(url, **_):
            head_calls.append(url)
            outcome = head_map[url]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(
                START_URL,
                max_pages=1,
                max_depth=0,
                **kwargs,
            )
        return pages[0], head_calls

    def test_internal_link_301_and_302_redirects_ending_in_200_and_404(self):
        head_map = {
            "https://example.com/old-301": self.make_head_response(
                "https://example.com/old-301", 301, "/new-200"
            ),
            "https://example.com/new-200": self.make_head_response(
                "https://example.com/new-200", 200
            ),
            "https://example.com/old-302": self.make_head_response(
                "https://example.com/old-302", 302, "/target-404"
            ),
            "https://example.com/target-404": self.make_head_response(
                "https://example.com/target-404", 404
            ),
        }
        page, _ = self.crawl_with_links(
            '<a href="/old-301">301</a><a href="/old-302">302</a>',
            head_map,
        )
        issues = {i["source_url"]: i for i in page["seo_issues"]}
        self.assertEqual(len(issues), 2)

        r301 = issues["https://example.com/old-301"]
        self.assertEqual(r301["type"], "redirect_url")
        self.assertEqual(r301["severity"], "info")
        self.assertEqual(r301["url"], START_URL)
        self.assertEqual(r301["destination_url"], "https://example.com/new-200")
        self.assertEqual(
            r301["redirect_chain"],
            ["https://example.com/old-301", "https://example.com/new-200"],
        )
        self.assertEqual(r301["status_code"], 200)
        self.assertIn("200", r301["message"])

        r302 = issues["https://example.com/old-302"]
        self.assertEqual(r302["type"], "redirect_url")
        self.assertEqual(r302["severity"], "info")
        self.assertEqual(
            r302["destination_url"], "https://example.com/target-404"
        )
        self.assertEqual(r302["status_code"], 404)
        self.assertIn("404", r302["message"])

    def test_redirect_chain_with_multiple_hops(self):
        head_map = {
            "https://example.com/step-1": self.make_head_response(
                "https://example.com/step-1", 301, "/step-2"
            ),
            "https://example.com/step-2": self.make_head_response(
                "https://example.com/step-2", 302, "/step-3"
            ),
            "https://example.com/step-3": self.make_head_response(
                "https://example.com/step-3", 200
            ),
        }
        page, _ = self.crawl_with_links('<a href="/step-1">Chain</a>', head_map)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "redirect_chain")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["source_url"], "https://example.com/step-1")
        self.assertEqual(issue["destination_url"], "https://example.com/step-3")
        expected_chain = [
            "https://example.com/step-1",
            "https://example.com/step-2",
            "https://example.com/step-3",
        ]
        self.assertEqual(issue["redirect_chain"], expected_chain)
        self.assertIn(" -> ".join(expected_chain), issue["message"])

    def test_redirect_loop_is_detected_and_handled_gracefully(self):
        head_map = {
            "https://example.com/loop-a": self.make_head_response(
                "https://example.com/loop-a", 302, "/loop-b"
            ),
            "https://example.com/loop-b": self.make_head_response(
                "https://example.com/loop-b", 302, "/loop-a"
            ),
        }
        page, _ = self.crawl_with_links('<a href="/loop-a">Loop</a>', head_map)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "redirect_loop")
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["source_url"], "https://example.com/loop-a")
        self.assertIsNone(issue["destination_url"])
        self.assertEqual(
            issue["redirect_chain"],
            [
                "https://example.com/loop-a",
                "https://example.com/loop-b",
                "https://example.com/loop-a",
            ],
        )

    def test_repeated_source_url_checked_only_once_across_pages(self):
        page_one_html = (
            '<html><head><title>Page 1</title>'
            '<meta name="description" content="Desc 1"></head>'
            '<body><h1>H1 1</h1>'
            '<a href="/legacy">Legacy</a>'
            '<a href="/page-2">Page 2</a></body></html>'
        )
        page_two_html = (
            '<html><head><title>Page 2</title>'
            '<meta name="description" content="Desc 2"></head>'
            '<body><h1>H1 2</h1>'
            '<a href="/legacy">Legacy Again</a></body></html>'
        )
        head_calls = []
        head_map = {
            "https://example.com/legacy": self.make_head_response(
                "https://example.com/legacy", 301, "/modern"
            ),
            "https://example.com/modern": self.make_head_response(
                "https://example.com/modern", 200
            ),
            "https://example.com/page-2": self.make_head_response(
                "https://example.com/page-2", 200
            ),
        }

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url == START_URL:
                return make_response("GET", url, 200, page_one_html)
            return make_response("GET", url, 200, page_two_html)

        def fake_head(url, **_):
            head_calls.append(url)
            return head_map[url]

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=2, max_depth=1)

        self.assertEqual(head_calls.count("https://example.com/legacy"), 1)
        self.assertEqual(head_calls.count("https://example.com/modern"), 1)
        for page in pages:
            redirects = [
                i for i in page["seo_issues"] if i["type"] == "redirect_url"
            ]
            self.assertEqual(len(redirects), 1)
            self.assertEqual(redirects[0]["url"], page["url"])

    def test_unique_redirect_check_limit_is_respected(self):
        head_map = {
            "https://example.com/r1": self.make_head_response(
                "https://example.com/r1", 301, "/d1"
            ),
            "https://example.com/d1": self.make_head_response(
                "https://example.com/d1", 200
            ),
            "https://example.com/r2": self.make_head_response(
                "https://example.com/r2", 301, "/d2"
            ),
            "https://example.com/d2": self.make_head_response(
                "https://example.com/d2", 200
            ),
            "https://example.com/r3": self.make_head_response(
                "https://example.com/r3", 301, "/d3"
            ),
            "https://example.com/d3": self.make_head_response(
                "https://example.com/d3", 200
            ),
        }
        page, head_calls = self.crawl_with_links(
            '<a href="/r1">1</a><a href="/r2">2</a><a href="/r3">3</a>',
            head_map,
            max_redirect_checks=2,
        )
        self.assertEqual(len(page["seo_issues"]), 2)
        self.assertNotIn("https://example.com/d3", head_calls)

    def test_request_failure_and_external_links_excluded(self):
        head_map = {
            "https://example.com/broken-conn": httpx.ConnectError("failed"),
            "https://example.com/mid-fail": self.make_head_response(
                "https://example.com/mid-fail", 301, "/unreachable-dest"
            ),
            "https://example.com/unreachable-dest": httpx.ConnectError(
                "dest down"
            ),
        }
        page, head_calls = self.crawl_with_links(
            '<a href="/broken-conn">Fail</a>'
            '<a href="/mid-fail">Mid Fail</a>'
            '<a href="https://external.org/redirect">External</a>',
            head_map,
        )
        self.assertNotIn("https://external.org/redirect", head_calls)
        self.assertEqual(len(page["seo_issues"]), 1)
        self.assertEqual(
            page["seo_issues"][0]["source_url"],
            "https://example.com/mid-fail",
        )
        self.assertIsNone(page["seo_issues"][0]["status_code"])


class CanonicalSeoAuditTests(unittest.TestCase):
    def make_head_response(
        self,
        url: str,
        status: int = 200,
        location: str | None = None,
    ) -> httpx.Response:
        headers = {"content-type": "text/html"}
        if location is not None:
            headers["location"] = location
        return httpx.Response(
            status,
            headers=headers,
            request=httpx.Request("HEAD", url),
        )

    def crawl_page_with_head(
        self,
        head_tags: str,
        page_url: str = START_URL,
        head_map: dict[str, httpx.Response | Exception] | None = None,
        content_type: str = "text/html; charset=utf-8",
    ):
        html = (
            f"<html><head><title>Page Title</title>"
            f'<meta name="description" content="Page description">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{head_tags}</head>"
            f"<body><h1>Page Heading</h1></body></html>"
        )
        head_calls: list[str] = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return httpx.Response(
                200,
                text=html,
                headers={"content-type": content_type},
                request=httpx.Request("GET", url),
            )

        def fake_head(url, **_):
            head_calls.append(url)
            if head_map and url in head_map:
                outcome = head_map[url]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return self.make_head_response(url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(page_url, max_pages=1, max_depth=0)
        return pages[0], head_calls

    def test_missing_canonical_tag_is_detected(self):
        page, _ = self.crawl_page_with_head("")
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "missing_canonical")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertIsNone(issue["canonical_url"])

    def test_one_valid_self_referencing_canonical_tag_produces_no_issues(self):
        page, head_calls = self.crawl_page_with_head(
            f'<link rel="canonical" href="{START_URL}">'
        )
        self.assertEqual(page["seo_issues"], [])
        self.assertEqual(head_calls, [])

    def test_multiple_canonical_tags_are_detected(self):
        page, _ = self.crawl_page_with_head(
            '<link rel="canonical" href="https://example.com/first">'
            '<link rel="canonical" href="/second">'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "multiple_canonicals")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["canonical_url"], "https://example.com/first")
        self.assertIn("https://example.com/first", issue["message"])
        self.assertIn("https://example.com/second", issue["message"])

    def test_empty_canonical_url_is_detected_as_invalid(self):
        for tag in (
            '<link rel="canonical" href="">',
            '<link rel="canonical" href="   ">',
            '<link rel="canonical">',
        ):
            page, _ = self.crawl_page_with_head(tag)
            self.assertEqual(len(page["seo_issues"]), 1)
            issue = page["seo_issues"][0]
            self.assertEqual(issue["type"], "invalid_canonical")
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)
            self.assertIsNone(issue["canonical_url"])

    def test_malformed_canonical_url_is_detected_as_invalid(self):
        for bad_href in (
            "mailto:info@example.com",
            "javascript:void(0)",
            "ftp://example.com/file",
            "http://",
            "https://",
            "://missing-scheme",
            "http://[invalid-ipv6",
            "https://exa mple.com/page",
        ):
            page, _ = self.crawl_page_with_head(
                f'<link rel="canonical" href="{bad_href}">'
            )
            self.assertEqual(len(page["seo_issues"]), 1)
            issue = page["seo_issues"][0]
            self.assertEqual(issue["type"], "invalid_canonical")
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)
            self.assertEqual(issue["canonical_url"], bad_href)

    def test_relative_canonical_url_is_resolved_against_page_url(self):
        page_url = "https://example.com/blog/post-1"
        page, head_calls = self.crawl_page_with_head(
            '<link rel="canonical" href="/blog/canonical-post">',
            page_url=page_url,
            head_map={
                "https://example.com/blog/canonical-post": self.make_head_response(
                    "https://example.com/blog/canonical-post", 301, "/blog/final-post"
                ),
                "https://example.com/blog/final-post": self.make_head_response(
                    "https://example.com/blog/final-post", 200
                ),
            },
        )
        self.assertIn("https://example.com/blog/canonical-post", head_calls)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "canonical_redirect")
        self.assertEqual(
            issue["canonical_url"],
            "https://example.com/blog/canonical-post",
        )

    def test_valid_relative_and_absolute_canonicals_without_redirect_pass(self):
        rel_page, rel_heads = self.crawl_page_with_head(
            '<link rel="canonical" href="/canonical-target">',
            page_url="https://example.com/source-page",
        )
        self.assertEqual(rel_page["seo_issues"], [])
        self.assertEqual(rel_heads, ["https://example.com/canonical-target"])

        abs_page, abs_heads = self.crawl_page_with_head(
            '<link rel="canonical" href="https://example.com/canonical-target">',
            page_url="https://example.com/source-page",
        )
        self.assertEqual(abs_page["seo_issues"], [])
        self.assertEqual(abs_heads, ["https://example.com/canonical-target"])

    def test_cross_domain_canonical_url_is_detected_case_insensitively(self):
        cross_page, _ = self.crawl_page_with_head(
            '<link rel="canonical" href="  https://syndication.other.org/article  ">'
        )
        self.assertEqual(len(cross_page["seo_issues"]), 1)
        issue = cross_page["seo_issues"][0]
        self.assertEqual(issue["type"], "cross_domain_canonical")
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(
            issue["canonical_url"],
            "https://syndication.other.org/article",
        )

        # Uppercase matching hostname should NOT be flagged as cross-domain
        same_domain_page, _ = self.crawl_page_with_head(
            '<link rel="canonical" href="https://EXAMPLE.COM/clean">'
        )
        self.assertEqual(same_domain_page["seo_issues"], [])

    def test_canonical_url_returning_redirect_is_detected(self):
        head_map = {
            "https://example.com/old-canonical": self.make_head_response(
                "https://example.com/old-canonical",
                301,
                "https://example.com/new-canonical",
            ),
            "https://example.com/new-canonical": self.make_head_response(
                "https://example.com/new-canonical",
                200,
            ),
        }
        page, _ = self.crawl_page_with_head(
            '<link rel="canonical" href="https://example.com/old-canonical">',
            head_map=head_map,
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "canonical_redirect")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(
            issue["canonical_url"],
            "https://example.com/old-canonical",
        )
        self.assertIn("https://example.com/old-canonical", issue["message"])
        self.assertIn("https://example.com/new-canonical", issue["message"])

    def test_non_html_page_is_excluded_from_canonical_audit(self):
        page, head_calls = self.crawl_page_with_head(
            "",
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])
        self.assertEqual(head_calls, [])


class RobotsSeoAuditTests(unittest.TestCase):
    def crawl_page_with_robots(
        self,
        meta_tags: str = "",
        x_robots_headers: str | list[str] | None = None,
        content_type: str = "text/html; charset=utf-8",
    ):
        html = (
            f"<html><head><title>Page Title</title>"
            f'<meta name="description" content="Page description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{meta_tags}</head>"
            f"<body><h1>Page Heading</h1></body></html>"
        )
        headers: list[tuple[str, str]] = [("content-type", content_type)]
        if isinstance(x_robots_headers, str):
            headers.append(("X-Robots-Tag", x_robots_headers))
        elif isinstance(x_robots_headers, list):
            for header_val in x_robots_headers:
                headers.append(("X-Robots-Tag", header_val))

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            return httpx.Response(
                200,
                text=html,
                headers=headers,
                request=httpx.Request("GET", url),
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_clean_indexable_page_produces_no_robots_issues(self):
        page_default = self.crawl_page_with_robots("")
        self.assertEqual(page_default["seo_issues"], [])

        page_index_follow = self.crawl_page_with_robots(
            '<meta name="robots" content="index, follow">'
        )
        self.assertEqual(page_index_follow["seo_issues"], [])

        page_all = self.crawl_page_with_robots(
            '<meta name="robots" content="all, max-snippet:-1">'
        )
        self.assertEqual(page_all["seo_issues"], [])

    def test_meta_robots_noindex_is_detected(self):
        page = self.crawl_page_with_robots(
            '<meta name="robots" content="noindex, follow">'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "noindex_page")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["robots_directive"], "noindex, follow")
        self.assertEqual(issue["directive_source"], "meta_robots")
        self.assertIn("noindex, follow", issue["message"])

    def test_meta_robots_nofollow_is_detected(self):
        page = self.crawl_page_with_robots(
            '<meta name="robots" content="index, nofollow">'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "nofollow_page")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["robots_directive"], "index, nofollow")
        self.assertEqual(issue["directive_source"], "meta_robots")

    def test_meta_robots_none_triggers_noindex_and_nofollow(self):
        page = self.crawl_page_with_robots(
            '<meta name="robots" content="none">'
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(issue_types, ["noindex_page", "nofollow_page"])
        for issue in page["seo_issues"]:
            self.assertEqual(issue["robots_directive"], "none")
            self.assertEqual(issue["directive_source"], "meta_robots")

    def test_googlebot_and_case_insensitive_directives_are_detected(self):
        page = self.crawl_page_with_robots(
            '<meta name="GOOGLEBOT" content="NOINDEX">'
        )
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "noindex_page")
        self.assertEqual(issue["robots_directive"], "NOINDEX")
        self.assertEqual(issue["directive_source"], "meta_googlebot")

    def test_x_robots_tag_header_directives_are_detected(self):
        page = self.crawl_page_with_robots(
            x_robots_headers="googlebot: noindex, nofollow"
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(issue_types, ["noindex_page", "nofollow_page"])
        for issue in page["seo_issues"]:
            self.assertEqual(issue["directive_source"], "x_robots_tag")
            self.assertEqual(
                issue["robots_directive"], "googlebot: noindex, nofollow"
            )

    def test_conflicting_robots_directives_within_tag_and_across_sources(self):
        single_tag_page = self.crawl_page_with_robots(
            '<meta name="robots" content="index, noindex">'
        )
        single_types = [issue["type"] for issue in single_tag_page["seo_issues"]]
        self.assertEqual(
            single_types,
            ["conflicting_robots_directives", "noindex_page"],
        )
        conflict_issue = single_tag_page["seo_issues"][0]
        self.assertEqual(conflict_issue["severity"], "warning")
        self.assertIn("index vs noindex", conflict_issue["message"])

        cross_source_page = self.crawl_page_with_robots(
            meta_tags='<meta name="robots" content="index, follow">',
            x_robots_headers="noindex, nofollow",
        )
        cross_types = [issue["type"] for issue in cross_source_page["seo_issues"]]
        self.assertEqual(
            cross_types,
            ["conflicting_robots_directives", "noindex_page", "nofollow_page"],
        )
        self.assertEqual(
            cross_source_page["seo_issues"][0]["directive_source"],
            "meta_robots, x_robots_tag",
        )

    def test_multiple_meta_robots_tags_are_detected(self):
        page = self.crawl_page_with_robots(
            '<meta name="robots" content="index">'
            '<meta name="robots" content="nofollow">'
        )
        issue_types = [issue["type"] for issue in page["seo_issues"]]
        self.assertEqual(
            issue_types,
            ["multiple_meta_robots", "nofollow_page"],
        )
        mult_issue = page["seo_issues"][0]
        self.assertEqual(mult_issue["severity"], "warning")
        self.assertEqual(mult_issue["robots_directive"], "index | nofollow")
        self.assertEqual(mult_issue["directive_source"], "meta_robots")

    def test_empty_or_missing_meta_robots_content_is_detected(self):
        for tag in (
            '<meta name="robots" content="">',
            '<meta name="robots" content="   ">',
            '<meta name="robots">',
        ):
            page = self.crawl_page_with_robots(tag)
            self.assertEqual(len(page["seo_issues"]), 1)
            issue = page["seo_issues"][0]
            self.assertEqual(issue["type"], "empty_meta_robots")
            self.assertEqual(issue["severity"], "warning")
            self.assertIsNone(issue["robots_directive"])
            self.assertEqual(issue["directive_source"], "meta_robots")

    def test_non_html_page_is_excluded_from_robots_audit(self):
        page = self.crawl_page_with_robots(
            meta_tags='<meta name="robots" content="noindex">',
            x_robots_headers="noindex, nofollow",
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])


class StructuredDataSeoAuditTests(unittest.TestCase):
    def crawl_page_with_scripts(
        self,
        scripts_html: str = "",
        content_type: str = "text/html; charset=utf-8",
    ):
        html = (
            f"<html><head><title>Page Title</title>"
            f'<meta name="description" content="Page description">'
            f'<link rel="canonical" href="{START_URL}">'
            f"{scripts_html}</head>"
            f"<body><h1>Page Heading</h1></body></html>"
        )

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

    def test_missing_structured_data_is_detected(self):
        page = self.crawl_page_with_scripts("")
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "missing_structured_data")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["schema_types"], [])

    def test_valid_single_and_nested_json_ld_produce_no_issues(self):
        page = self.crawl_page_with_scripts(
            '<script type="application/ld+json">'
            "{"
            '"@context": "https://schema.org",'
            '"@type": "Article",'
            '"headline": "Structured Data Guide",'
            '"author": ['
            '  {"@type": "Person", "name": "Alice"},'
            '  {"@type": "Person", "name": "Bob"}'
            "],"
            '"publisher": {'
            '  "@type": "Organization",'
            '  "name": "CrawlScope"'
            "}"
            "}"
            "</script>"
        )
        self.assertEqual(page["seo_issues"], [])

    def test_valid_graph_and_array_and_multitype_json_ld_produce_no_issues(self):
        graph_page = self.crawl_page_with_scripts(
            '<script type="application/ld+json; charset=utf-8">'
            "{"
            '"@context": "https://schema.org",'
            '"@graph": ['
            '  {"@type": "WebSite", "@id": "https://example.com/#website", "name": "Example"},'
            '  {"@type": ["WebPage", "CollectionPage"], "isPartOf": {"@id": "https://example.com/#website"}}'
            "]"
            "}"
            "</script>"
        )
        self.assertEqual(graph_page["seo_issues"], [])

        array_page = self.crawl_page_with_scripts(
            '<script type="APPLICATION/LD+JSON">'
            "["
            '  {"@context": "https://schema.org", "@type": "Organization", "name": "Example"},'
            '  {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}'
            "]"
            "</script>"
        )
        self.assertEqual(array_page["seo_issues"], [])

    def test_invalid_or_empty_json_ld_is_detected_safely(self):
        for bad_script in (
            '<script type="application/ld+json">{invalid json</script>',
            '<script type="application/ld+json">   \n\t  </script>',
            '<script type="application/ld+json"></script>',
        ):
            page = self.crawl_page_with_scripts(bad_script)
            self.assertEqual(len(page["seo_issues"]), 1)
            issue = page["seo_issues"][0]
            self.assertEqual(issue["type"], "invalid_json_ld")
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)
            self.assertEqual(issue["schema_types"], [])

    def test_missing_schema_type_is_detected_on_root_graph_and_nested_objects(self):
        for payload in (
            '{"@context": "https://schema.org", "name": "Missing type"}',
            '{"@context": "https://schema.org", "@type": "   ", "name": "Blank type"}',
            '{"@context": "https://schema.org", "@type": [], "name": "Empty list type"}',
            '{"@context": "https://schema.org", "@graph": [{"name": "Graph item without type"}]}',
            '{"@context": "https://schema.org", "@type": "Article", "author": {"name": "Author without type"}}',
        ):
            page = self.crawl_page_with_scripts(
                f'<script type="application/ld+json">{payload}</script>'
            )
            issue_types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(issue_types, ["missing_schema_type"])
            self.assertEqual(page["seo_issues"][0]["severity"], "warning")
            self.assertEqual(page["seo_issues"][0]["url"], START_URL)

    def test_duplicate_schema_type_detected_across_blocks_and_graph(self):
        two_blocks_page = self.crawl_page_with_scripts(
            '<script type="application/ld+json">'
            '{"@context": "https://schema.org", "@type": "Organization", "name": "One"}'
            "</script>"
            '<script type="application/ld+json">'
            '{"@context": "https://schema.org", "@type": "https://schema.org/Organization", "name": "Two"}'
            "</script>"
        )
        self.assertEqual(len(two_blocks_page["seo_issues"]), 1)
        dup_issue = two_blocks_page["seo_issues"][0]
        self.assertEqual(dup_issue["type"], "duplicate_schema_type")
        self.assertEqual(dup_issue["severity"], "warning")
        self.assertEqual(dup_issue["url"], START_URL)
        self.assertEqual(dup_issue["schema_types"], ["Organization"])
        self.assertIn("Organization", dup_issue["message"])

        graph_dup_page = self.crawl_page_with_scripts(
            '<script type="application/ld+json">'
            "{"
            '"@context": "https://schema.org",'
            '"@graph": ['
            '  {"@type": "FAQPage", "name": "FAQ 1"},'
            '  {"@type": "FAQPage", "name": "FAQ 2"}'
            "]"
            "}"
            "</script>"
        )
        self.assertEqual(
            [issue["type"] for issue in graph_dup_page["seo_issues"]],
            ["duplicate_schema_type"],
        )
        self.assertEqual(
            graph_dup_page["seo_issues"][0]["schema_types"],
            ["FAQPage"],
        )

    def test_invalid_schema_structure_is_detected(self):
        for bad_structure in (
            '"just a string"',
            "123",
            "true",
            "null",
            "[]",
            "{}",
            '[{"@type": "Organization"}, "not an object"]',
            '{"@context": "https://schema.org", "@graph": "not-a-list"}',
            '{"@context": "https://schema.org", "@graph": []}',
            '{"@context": "https://schema.org", "@graph": [{}]}',
            '{"@context": "https://schema.org", "@type": 123}',
            '{"@context": "https://schema.org", "@type": ["Article", 456]}',
        ):
            page = self.crawl_page_with_scripts(
                f'<script type="application/ld+json">{bad_structure}</script>'
            )
            issue_types = [issue["type"] for issue in page["seo_issues"]]
            self.assertEqual(
                issue_types,
                ["invalid_schema_structure"],
                msg=f"Failed for payload: {bad_structure}",
            )
            self.assertEqual(page["seo_issues"][0]["severity"], "warning")
            self.assertEqual(page["seo_issues"][0]["url"], START_URL)

    def test_non_html_page_is_excluded_from_structured_data_audit(self):
        page = self.crawl_page_with_scripts(
            "",
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])


if __name__ == "__main__":
    unittest.main()

