import unittest
from typing import Any
from unittest.mock import patch

import httpx

import crawler

START_URL = "https://example.com/"

ROBOTS_TXT = (
    "User-agent: *\n"
    "Disallow: /private\n"
    "Sitemap: https://example.com/sitemap.xml\n"
)

DEFAULT_BODY_WORDS = " ".join(f"word{i}" for i in range(550))
SECOND_BODY_WORDS = " ".join(f"secondword{i}" for i in range(550))

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


from xml.sax.saxutils import escape as xml_escape

DEFAULT_SECURITY_HEADERS = {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "content-security-policy": "default-src 'self'; frame-ancestors 'self'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=(), microphone=(), camera=()",
}


def make_response(
    method: str,
    url: str,
    status: int,
    text: str = "",
    content_type: str = "text/html",
    location: str | None = None,
    include_security_headers: bool = True,
    extra_headers: dict[str, str] | list[tuple[str, str]] | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {"content-type": content_type}
    if include_security_headers:
        headers.update(DEFAULT_SECURITY_HEADERS)
    if location is not None:
        headers["location"] = location
    if isinstance(extra_headers, dict):
        headers.update(extra_headers)
    return httpx.Response(
        status,
        text=text,
        headers=headers,
        request=httpx.Request(method, url),
    )


def make_sitemap_response(
    url: str,
    loc_urls: list[str] | tuple[str, ...] | set[str] = (START_URL,),
    status: int = 200,
) -> httpx.Response:
    entries = "".join(f"<url><loc>{xml_escape(u)}</loc></url>" for u in loc_urls)
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}"
        "</urlset>"
    )
    return httpx.Response(
        status,
        text=xml_text,
        headers={"content-type": "application/xml"},
        request=httpx.Request("GET", url),
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
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
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
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
        page_fetches = [
            url
            for url in get_calls
            if not url.endswith(("/robots.txt", "/sitemap.xml"))
        ]
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
            html = html.replace("<html>", '<html lang="en">', 1)
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
            html = html.replace("<body>", "<body><main>", 1)
            html = html.replace(
                "</body>",
                f"<p>{DEFAULT_BODY_WORDS}</p></main></body>",
                1,
            )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response("GET", url, 200, html, content_type=content_type)

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
            first_html = first_html.replace("<html>", '<html lang="en">', 1)
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
            first_html = first_html.replace("<body>", "<body><main>", 1)
            first_html = first_html.replace(
                "</body>",
                f"<p>{DEFAULT_BODY_WORDS}</p></main></body>",
                1,
            )
        if "text/html" in second_content_type and 'rel="canonical"' not in second_html:
            second_html = second_html.replace("<html>", '<html lang="en">', 1)
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
            second_html = second_html.replace("<body>", "<body><main>", 1)
            second_html = second_html.replace(
                "</body>",
                f"<p>{SECOND_BODY_WORDS}</p></main></body>",
                1,
            )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                sm_urls = [START_URL]
                if second_status == 200 and "text/html" in second_content_type:
                    sm_urls.append(page_two_url)
                return make_sitemap_response(url, sm_urls)
            if url == START_URL:
                return make_response(
                    "GET",
                    url,
                    200,
                    first_html,
                    content_type="text/html; charset=utf-8",
                )
            return make_response(
                "GET",
                url,
                second_status,
                second_html,
                content_type=second_content_type,
            )

        def fake_head(url, **_):
            return make_response("HEAD", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
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
            html = html.replace("<html>", '<html lang="en">', 1)
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
            html = html.replace("<body>", "<body><main>", 1)
            html = html.replace(
                "</body>",
                f"<p>{DEFAULT_BODY_WORDS}</p></main></body>",
                1,
            )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response(
                "GET", url, status_code, html, content_type=content_type
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url, (START_URL, "https://example.com/page-2")
                )
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
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
            '<html lang="en"><head><title>Title</title>'
            '<meta name="description" content="Desc">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f'<body><main><h1>Heading</h1><p>{DEFAULT_BODY_WORDS}</p><img src="/timeout.png" alt="Timeout"></main></body></html>'
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
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
        '<html lang="en"><head><title>Valid Title</title>'
        '<meta name="description" content="Valid description"></head>'
        f"<body><main><h1>Valid H1</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (target_url,))
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
            '<html lang="en"><head><title>Home</title>'
            '<meta name="description" content="Home description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><main><h1>Home Heading</h1><p>{DEFAULT_BODY_WORDS}</p>{links_html}</main></body></html>"
        )
        head_calls: list[str] = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response("GET", url, 200, html)

        def fake_head(url, **_):
            head_calls.append(url)
            outcome = head_map[url]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
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
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url, (START_URL, "https://example.com/page-2")
                )
            if url == START_URL:
                return make_response("GET", url, 200, page_one_html)
            return make_response("GET", url, 200, page_two_html)

        def fake_head(url, **_):
            head_calls.append(url)
            return head_map[url]

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
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
            f'<html lang="en"><head><title>Page Title</title>'
            f'<meta name="description" content="Page description">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{head_tags}</head>"
            f"<body><main><h1>Page Heading</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
        )
        head_calls: list[str] = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                if any(
                    non_self in head_tags
                    for non_self in (
                        "/canonical-target",
                        "/blog/canonical-post",
                        "old-canonical",
                        "syndication.other.org",
                        "EXAMPLE.COM/clean",
                    )
                ):
                    return make_sitemap_response(url, ())
                return make_sitemap_response(url, (page_url,))
            return make_response(
                "GET", url, 200, html, content_type=content_type
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
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
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
            f'<html lang="en"><head><title>Page Title</title>'
            f'<meta name="description" content="Page description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{meta_tags}</head>"
            f"<body><main><h1>Page Heading</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
        )
        headers: list[tuple[str, str]] = [
            ("content-type", content_type),
            *DEFAULT_SECURITY_HEADERS.items(),
        ]
        if isinstance(x_robots_headers, str):
            headers.append(("X-Robots-Tag", x_robots_headers))
        elif isinstance(x_robots_headers, list):
            for header_val in x_robots_headers:
                headers.append(("X-Robots-Tag", header_val))

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                combined_robots = f"{meta_tags} {x_robots_headers}".lower()
                if "noindex" in combined_robots or '"none"' in combined_robots:
                    return make_sitemap_response(url, ())
                return make_sitemap_response(url, (START_URL,))
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
            f'<html lang="en"><head><title>Page Title</title>'
            f'<meta name="description" content="Page description">'
            f'<link rel="canonical" href="{START_URL}">'
            f"{scripts_html}</head>"
            f"<body><main><h1>Page Heading</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response(
                "GET", url, 200, html, content_type=content_type
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


class ContentQualitySeoAuditTests(unittest.TestCase):
    def build_html(
        self,
        body_inner_html: str,
        canonical_url: str = START_URL,
        title: str = "Page Title",
        description: str = "Page description",
    ) -> str:
        return (
            f'<html lang="en"><head><title>{title}</title>'
            f'<meta name="description" content="{description}">'
            f'<link rel="canonical" href="{canonical_url}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><main>{body_inner_html}</main></body></html>"
        )

    def crawl_single_page(
        self,
        body_inner_html: str,
        content_type: str = "text/html; charset=utf-8",
    ):
        html = self.build_html(body_inner_html)

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response(
                "GET", url, 200, html, content_type=content_type
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_missing_content_is_detected_when_no_meaningful_visible_text(self):
        page = self.crawl_single_page(
            "<!-- comment only -->"
            "<script>const words = '" + (" ".join(["scriptword"] * 600)) + "';</script>"
            "<style>body { content: '" + (" ".join(["styleword"] * 600)) + "'; }</style>"
            "<noscript>" + (" ".join(["noscriptword"] * 600)) + "</noscript>"
            "<svg><text>" + (" ".join(["svgword"] * 600)) + "</text></svg>"
            "<p>   ... --- !!!   </p>"
        )
        content_issues = [
            issue
            for issue in page["seo_issues"]
            if issue["type"]
            in (
                "missing_content",
                "thin_content",
                "low_word_count",
                "excessive_word_count",
            )
        ]
        self.assertEqual(len(content_issues), 1)
        issue = content_issues[0]
        self.assertEqual(issue["type"], "missing_content")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["word_count"], 0)

    def test_thin_content_is_detected_without_duplicate_low_word_count(self):
        words_120 = " ".join(f"topic{i}" for i in range(119))
        page = self.crawl_single_page(f"<h1>Heading</h1><p>{words_120}</p>")
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "thin_content")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["word_count"], 120)
        self.assertIn("120", issue["message"])

        # Boundary: 299 meaningful words is still thin_content
        words_299 = " ".join(f"item{i}" for i in range(298))
        boundary_page = self.crawl_single_page(
            f"<h1>Heading</h1><p>{words_299}</p>"
        )
        self.assertEqual(
            [i["type"] for i in boundary_page["seo_issues"]],
            ["thin_content"],
        )
        self.assertEqual(boundary_page["seo_issues"][0]["word_count"], 299)

    def test_low_word_count_is_detected_as_info_between_300_and_499_words(self):
        # Boundary: 300 meaningful words triggers low_word_count (not thin_content)
        words_300 = " ".join(f"term{i}" for i in range(299))
        page_300 = self.crawl_single_page(f"<h1>Heading</h1><p>{words_300}</p>")
        self.assertEqual(len(page_300["seo_issues"]), 1)
        issue_300 = page_300["seo_issues"][0]
        self.assertEqual(issue_300["type"], "low_word_count")
        self.assertEqual(issue_300["severity"], "info")
        self.assertEqual(issue_300["url"], START_URL)
        self.assertEqual(issue_300["word_count"], 300)

        # Boundary: 499 meaningful words triggers low_word_count
        words_499 = " ".join(f"term{i}" for i in range(498))
        page_499 = self.crawl_single_page(f"<h1>Heading</h1><p>{words_499}</p>")
        self.assertEqual(
            [i["type"] for i in page_499["seo_issues"]],
            ["low_word_count"],
        )
        self.assertEqual(page_499["seo_issues"][0]["word_count"], 499)

    def test_normal_word_count_between_500_and_3000_produces_no_issues(self):
        for total_words in (500, 1200, 3000):
            paragraph = " ".join(f"normal{i}" for i in range(total_words - 1))
            page = self.crawl_single_page(f"<h1>Heading</h1><p>{paragraph}</p>")
            self.assertEqual(
                page["seo_issues"],
                [],
                msg=f"Unexpected issues for {total_words} words",
            )

    def test_excessive_word_count_over_3000_is_detected(self):
        paragraph = " ".join(f"longword{i}" for i in range(3000))
        page = self.crawl_single_page(f"<h1>Heading</h1><p>{paragraph}</p>")
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "excessive_word_count")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["word_count"], 3001)
        self.assertIn("3001", issue["message"])

    def test_script_style_noscript_and_svg_are_excluded_from_word_count(self):
        visible_words = " ".join(f"visible{i}" for i in range(549))
        ignored_words = " ".join(f"hidden{i}" for i in range(3000))
        page = self.crawl_single_page(
            f"<h1>Heading</h1><p>{visible_words}</p>"
            f"<script>{ignored_words}</script>"
            f"<style>{ignored_words}</style>"
            f"<noscript>{ignored_words}</noscript>"
            f"<svg><text>{ignored_words}</text></svg>"
        )
        self.assertEqual(page["seo_issues"], [])

    def test_duplicate_content_detected_across_multiple_urls_with_normalization(self):
        page_two_url = "https://example.com/page-2"
        shared_words = [f"sharedArticleWord{i}" for i in range(500)]
        text_one = "   ".join(shared_words)
        text_two = "\n\t ".join(word.lower() for word in shared_words)

        html_one = self.build_html(
            f'<h1>Same Heading</h1><p>{text_one}</p>'
            '<script>console.log("page 1 script");</script>'
            '<a href="/page-2">Next</a>',
            canonical_url=START_URL,
            title="Title One",
            description="Description One",
        )
        html_two = self.build_html(
            f"<h1>same   heading</h1><p>{text_two}</p>"
            '<style>.page2 { display: block; }</style>'
            '<a href="/page-2"> next </a>',
            canonical_url=page_two_url,
            title="Title Two",
            description="Description Two",
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL, page_two_url))
            if url == START_URL:
                return make_response("GET", url, 200, html_one)
            return make_response("GET", url, 200, html_two)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", page_two_url, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=2, max_depth=1)

        self.assertEqual(len(pages), 2)
        for page in pages:
            dup_issues = [
                i for i in page["seo_issues"] if i["type"] == "duplicate_content"
            ]
            self.assertEqual(len(dup_issues), 1)
            issue = dup_issues[0]
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], page["url"])
            self.assertEqual(issue["word_count"], 503)
            self.assertEqual(
                issue["duplicate_urls"],
                [START_URL, page_two_url],
            )
            self.assertIn(START_URL, issue["message"])
            self.assertIn(page_two_url, issue["message"])

    def test_empty_pages_are_not_flagged_as_duplicate_content(self):
        page_two_url = "https://example.com/page-2"
        html_one = self.build_html(
            '<a href="/page-2">   </a>',
            canonical_url=START_URL,
            title="Title 1",
            description="Desc 1",
        )
        html_two = self.build_html(
            "<script>const x = 1;</script>",
            canonical_url=page_two_url,
            title="Title 2",
            description="Desc 2",
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL, page_two_url))
            if url == START_URL:
                return make_response("GET", url, 200, html_one)
            return make_response("GET", url, 200, html_two)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", page_two_url, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=2, max_depth=1)

        for page in pages:
            issue_types = [i["type"] for i in page["seo_issues"]]
            self.assertIn("missing_content", issue_types)
            self.assertNotIn("duplicate_content", issue_types)

    def test_non_html_page_is_excluded_from_content_quality_audit(self):
        page = self.crawl_single_page(
            "short text",
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])


class PerformanceSeoAuditTests(unittest.TestCase):
    def build_valid_html(self, target_size_bytes: int | None = None) -> str:
        prefix = (
            '<html lang="en"><head><title>Performance Page</title>'
            '<meta name="description" content="Performance description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><main><h1>Performance Heading</h1><p>{DEFAULT_BODY_WORDS}</p><!--"
        )
        suffix = "--></main></body></html>"
        base_len = len((prefix + suffix).encode("utf-8"))
        if target_size_bytes is None or target_size_bytes <= base_len:
            return prefix + suffix
        padding = "x" * (target_size_bytes - base_len)
        return prefix + padding + suffix

    def crawl_with_performance(
        self,
        elapsed_seconds: float = 0.25,
        target_size_bytes: int | None = None,
        content_type: str = "text/html; charset=utf-8",
        crawl_duration_seconds: float | None = None,
    ):
        from datetime import timedelta

        html = self.build_valid_html(target_size_bytes=target_size_bytes)

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            resp = make_response(
                "GET", url, 200, html, content_type=content_type
            )
            resp._elapsed = timedelta(seconds=elapsed_seconds)
            return resp

        perf_values = [0.0, elapsed_seconds]
        if crawl_duration_seconds is not None:
            perf_values.append(crawl_duration_seconds)
        else:
            perf_values.append(elapsed_seconds)

        perf_iter = iter(perf_values)

        def fake_perf_counter():
            return next(perf_iter, perf_values[-1])

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.time.perf_counter", side_effect=fake_perf_counter), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_normal_response_time_and_page_size_produce_no_issues(self):
        page = self.crawl_with_performance(
            elapsed_seconds=0.45,
            target_size_bytes=50_000,
        )
        self.assertEqual(page["seo_issues"], [])
        self.assertEqual(page["response_time_ms"], 450.0)
        self.assertEqual(page["page_size_bytes"], 50_000)

        # Exact threshold boundaries (2.0s and 1 MB) are not flagged
        boundary_page = self.crawl_with_performance(
            elapsed_seconds=2.0,
            target_size_bytes=crawler.LARGE_PAGE_SIZE_THRESHOLD_BYTES,
        )
        self.assertEqual(boundary_page["seo_issues"], [])
        self.assertEqual(boundary_page["response_time_ms"], 2000.0)
        self.assertEqual(
            boundary_page["page_size_bytes"],
            crawler.LARGE_PAGE_SIZE_THRESHOLD_BYTES,
        )

    def test_slow_response_detected_between_2_and_5_seconds(self):
        page = self.crawl_with_performance(elapsed_seconds=2.35)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "slow_response")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["response_time_ms"], 2350.0)
        self.assertIn("2350.0", issue["message"])

        # Exact 5.0s boundary is still slow_response (not very_slow_response)
        boundary_5s = self.crawl_with_performance(elapsed_seconds=5.0)
        self.assertEqual(
            [i["type"] for i in boundary_5s["seo_issues"]],
            ["slow_response"],
        )
        self.assertEqual(boundary_5s["seo_issues"][0]["response_time_ms"], 5000.0)

    def test_very_slow_response_over_5_seconds_avoids_duplicate_slow_response(self):
        page = self.crawl_with_performance(elapsed_seconds=5.25)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "very_slow_response")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["response_time_ms"], 5250.0)
        self.assertIn("5250.0", issue["message"])

    def test_large_page_size_detected_between_1mb_and_3mb(self):
        size_over_1mb = crawler.LARGE_PAGE_SIZE_THRESHOLD_BYTES + 1
        page = self.crawl_with_performance(target_size_bytes=size_over_1mb)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "large_page_size")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["page_size_bytes"], size_over_1mb)

        # Exact 3 MB boundary is still large_page_size (not very_large_page_size)
        boundary_3mb = self.crawl_with_performance(
            target_size_bytes=crawler.VERY_LARGE_PAGE_SIZE_THRESHOLD_BYTES
        )
        self.assertEqual(
            [i["type"] for i in boundary_3mb["seo_issues"]],
            ["large_page_size"],
        )

    def test_very_large_page_size_over_3mb_avoids_duplicate_large_page_size(self):
        size_over_3mb = crawler.VERY_LARGE_PAGE_SIZE_THRESHOLD_BYTES + 1
        page = self.crawl_with_performance(target_size_bytes=size_over_3mb)
        self.assertEqual(len(page["seo_issues"]), 1)
        issue = page["seo_issues"][0]
        self.assertEqual(issue["type"], "very_large_page_size")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["page_size_bytes"], size_over_3mb)

    def test_slow_crawl_informational_issue_when_overall_duration_exceeds_threshold(self):
        # At or below threshold (30.0s): no slow_crawl issue
        normal_crawl_page = self.crawl_with_performance(
            elapsed_seconds=0.5,
            crawl_duration_seconds=crawler.SLOW_CRAWL_THRESHOLD_SECONDS,
        )
        self.assertEqual(normal_crawl_page["seo_issues"], [])

        # Above threshold (> 30.0s): slow_crawl informational issue reported
        slow_crawl_page = self.crawl_with_performance(
            elapsed_seconds=0.5,
            crawl_duration_seconds=34.5,
        )
        self.assertEqual(len(slow_crawl_page["seo_issues"]), 1)
        issue = slow_crawl_page["seo_issues"][0]
        self.assertEqual(issue["type"], "slow_crawl")
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["url"], START_URL)
        self.assertEqual(issue["crawl_duration_seconds"], 34.5)
        self.assertIn("34.5s", issue["message"])

    def test_non_html_page_is_excluded_from_html_page_size_audit(self):
        size_over_3mb = crawler.VERY_LARGE_PAGE_SIZE_THRESHOLD_BYTES + 100
        page = self.crawl_with_performance(
            elapsed_seconds=0.5,
            target_size_bytes=size_over_3mb,
            content_type="application/json",
        )
        self.assertEqual(page["seo_issues"], [])
        self.assertEqual(page["page_size_bytes"], size_over_3mb)
        self.assertEqual(page["response_time_ms"], 500.0)


class AccessibilitySeoAuditTests(unittest.TestCase):
    def crawl_accessibility_page(
        self,
        body_inner_html: str = "",
        html_open_tag: str = '<html lang="en">',
        use_main_landmark: bool = True,
        content_type: str = "text/html; charset=utf-8",
    ):
        main_content = (
            f"<h1>Main Page Heading</h1><p>{DEFAULT_BODY_WORDS}</p>{body_inner_html}"
        )
        if use_main_landmark:
            wrapped_body = f"<body><main>{main_content}</main></body>"
        else:
            wrapped_body = f"<body>{main_content}</body>"

        html = (
            f"{html_open_tag}<head><title>Accessible Page</title>"
            '<meta name="description" content="Accessible page description">'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"{wrapped_body}</html>"
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response(
                "GET", url, 200, html, content_type=content_type
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_clean_accessible_page_produces_no_issues(self):
        page = self.crawl_accessibility_page(
            '<h2>Contact Us</h2>'
            '<form>'
            '  <label for="full-name">Full Name</label>'
            '  <input id="full-name" name="name" type="text">'
            '  <label>Email <input name="email" type="email"></label>'
            '  <input type="hidden" name="csrf" value="token123">'
            '  <input type="submit" value="Send Message">'
            '  <button type="button">Cancel</button>'
            '</form>'
            '<a href="/pricing">View Pricing Plans</a>'
        )
        self.assertEqual(page["seo_issues"], [])

    def test_missing_lang_attribute_on_html_is_detected(self):
        for tag in ("<html>", '<html lang="">', '<html lang="   ">'):
            page = self.crawl_accessibility_page(html_open_tag=tag)
            self.assertEqual(len(page["seo_issues"]), 1)
            issue = page["seo_issues"][0]
            self.assertEqual(issue["type"], "missing_lang")
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)
            self.assertIn("lang", issue["message"])

        # xml:lang is also accepted as a valid language declaration
        xml_lang_page = self.crawl_accessibility_page(
            html_open_tag='<html xml:lang="en-US">'
        )
        self.assertEqual(xml_lang_page["seo_issues"], [])

    def test_missing_form_label_and_accessible_form_label_variants(self):
        # Unlabeled text input, select, textarea, and image input without alt
        bad_page = self.crawl_accessibility_page(
            '<form>'
            '  <input type="text" name="username">'
            '  <select name="country"><option>USA</option></select>'
            '  <textarea id="bio"></textarea>'
            '  <input type="image" src="/ok-btn.png">'
            '</form>'
        )
        label_issues = [
            i for i in bad_page["seo_issues"] if i["type"] == "missing_form_label"
        ]
        self.assertEqual(len(label_issues), 4)
        for issue in label_issues:
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)

        # Positive cases: label[for], wrapping label, aria-label, aria-labelledby,
        # title, hidden inputs, submit/reset/button inputs, and image input with alt
        good_page = self.crawl_accessibility_page(
            '<span id="search-lbl">Search Documentation</span>'
            '<form>'
            '  <label for="user-field">Username</label>'
            '  <input id="user-field" type="text">'
            '  <label>Topic <select name="topic"><option>SEO</option></select></label>'
            '  <input type="search" aria-labelledby="search-lbl">'
            '  <textarea aria-label="Additional comments"></textarea>'
            '  <input type="text" title="Filter results">'
            '  <input type="hidden" name="session_id" value="1">'
            '  <input type="text" name="hidden_field" hidden>'
            '  <input type="text" name="aria_hidden_field" aria-hidden="true">'
            '  <input type="submit">'
            '  <input type="reset">'
            '  <input type="button" value="Reset Filters">'
            '  <input type="image" src="/submit.png" alt="Submit form">'
            '</form>'
        )
        self.assertEqual(good_page["seo_issues"], [])

    def test_empty_link_and_accessible_link_names(self):
        bad_page = self.crawl_accessibility_page(
            '<a href="/empty"></a>'
            '<a href="/whitespace">   \n\t  </a>'
            '<a href="/hidden-only"><span aria-hidden="true">Hidden text</span></a>'
        )
        empty_links = [
            i for i in bad_page["seo_issues"] if i["type"] == "empty_link"
        ]
        self.assertEqual(len(empty_links), 3)
        for issue in empty_links:
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)

        # Positive cases: aria-label, aria-labelledby, child img alt, SVG title, title
        good_page = self.crawl_accessibility_page(
            '<span id="nav-home">Home Dashboard</span>'
            '<a href="/home" aria-labelledby="nav-home"></a>'
            '<a href="/profile" aria-label="User Profile"></a>'
            '<a href="/gallery"><img src="/thumb.jpg" alt="View photo gallery"></a>'
            '<a href="/feed"><svg><title>RSS Feed</title></svg></a>'
            '<a href="/settings" title="Account Settings"></a>'
            '<a name="anchor-without-href"></a>'
        )
        # Mock image status for /thumb.jpg by filtering only accessibility issues
        a11y_issues = [
            i
            for i in good_page["seo_issues"]
            if i["type"] in ("empty_link", "vague_link_text")
        ]
        self.assertEqual(a11y_issues, [])

    def test_vague_link_text_is_detected(self):
        page = self.crawl_accessibility_page(
            '<a href="/doc-1">Click here</a>'
            '<a href="/doc-2">  read more... </a>'
            '<a href="/doc-3">HERE!</a>'
            '<a href="/doc-4">Learn more →</a>'
            '<a href="/doc-5">Read our complete SEO auditing guide</a>'
        )
        vague_issues = [
            i for i in page["seo_issues"] if i["type"] == "vague_link_text"
        ]
        self.assertEqual(len(vague_issues), 4)
        for issue in vague_issues:
            self.assertEqual(issue["severity"], "info")
            self.assertEqual(issue["url"], START_URL)

    def test_heading_hierarchy_skips_are_detected(self):
        # H1 (from helper) -> H3 skips H2; then H3 -> H5 skips H4
        bad_page = self.crawl_accessibility_page(
            "<h3>Skipped H2 Section</h3>"
            "<h5>Skipped H4 Subsection</h5>"
        )
        hierarchy_issues = [
            i for i in bad_page["seo_issues"] if i["type"] == "heading_hierarchy"
        ]
        self.assertEqual(len(hierarchy_issues), 2)
        self.assertEqual(hierarchy_issues[0]["severity"], "warning")
        self.assertIn("<h1> to <h3>", hierarchy_issues[0]["message"])
        self.assertIn("<h3> to <h5>", hierarchy_issues[1]["message"])

        # Valid hierarchy: H1 -> H2 -> H3 -> H2 -> H3 (moving up to H2 is valid)
        good_page = self.crawl_accessibility_page(
            "<h2>Section One</h2>"
            "<h3>Subsection A</h3>"
            "<h2>Section Two</h2>"
            "<h3>Subsection B</h3>"
        )
        self.assertEqual(good_page["seo_issues"], [])

    def test_empty_button_and_accessible_button_variants(self):
        bad_page = self.crawl_accessibility_page(
            "<button></button>"
            "<button>   </button>"
            '<input type="button">'
            '<input type="submit" value="   ">'
            '<div role="button"></div>'
        )
        button_issues = [
            i for i in bad_page["seo_issues"] if i["type"] == "empty_button"
        ]
        self.assertEqual(len(button_issues), 5)
        for issue in button_issues:
            self.assertEqual(issue["severity"], "warning")
            self.assertEqual(issue["url"], START_URL)

        # Positive cases: visible text, aria-label, aria-labelledby, svg title, input value
        good_page = self.crawl_accessibility_page(
            '<span id="close-modal-lbl">Close Modal</span>'
            "<button>Save Changes</button>"
            '<button aria-label="Open menu"></button>'
            '<button aria-labelledby="close-modal-lbl"></button>'
            "<button><svg><title>Search</title></svg></button>"
            '<input type="button" value="Apply">'
            '<input type="submit">'
            '<div role="button" aria-label="Toggle theme"></div>'
            "<button hidden></button>"
        )
        self.assertEqual(good_page["seo_issues"], [])

    def test_missing_landmark_and_valid_main_landmarks(self):
        no_landmark_page = self.crawl_accessibility_page(
            use_main_landmark=False
        )
        self.assertEqual(len(no_landmark_page["seo_issues"]), 1)
        issue = no_landmark_page["seo_issues"][0]
        self.assertEqual(issue["type"], "missing_landmark")
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["url"], START_URL)

        # role="main" satisfies the landmark check even without <main>
        role_main_page = self.crawl_accessibility_page(
            body_inner_html='<div role="main"><p>Primary content</p></div>',
            use_main_landmark=False,
        )
        self.assertEqual(role_main_page["seo_issues"], [])

    def test_duplicate_id_is_detected(self):
        bad_page = self.crawl_accessibility_page(
            '<div id="shared-id">First</div>'
            '<section id="shared-id">Second</section>'
            '<p id="unique-id">Unique</p>'
        )
        dup_id_issues = [
            i for i in bad_page["seo_issues"] if i["type"] == "duplicate_id"
        ]
        self.assertEqual(len(dup_id_issues), 1)
        issue = dup_id_issues[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["url"], START_URL)
        self.assertIn("shared-id", issue["message"])
        self.assertIn("2 times", issue["message"])

    def test_malformed_html_and_non_html_pages_handled_safely(self):
        # Malformed HTML should not crash the accessibility audit
        malformed_page = self.crawl_accessibility_page(
            '<div><form><input type="text" name="unclosed"><a href="/broken">'
        )
        issue_types = [i["type"] for i in malformed_page["seo_issues"]]
        self.assertIn("missing_form_label", issue_types)
        self.assertIn("empty_link", issue_types)

        # Non-HTML page should not trigger accessibility checks
        non_html_page = self.crawl_accessibility_page(
            html_open_tag="<html>",
            use_main_landmark=False,
            content_type="application/json",
        )
        self.assertEqual(non_html_page["seo_issues"], [])


class InternalLinkStructureAuditTests(unittest.TestCase):
    def make_valid_page_html(
        self,
        url: str,
        label: str,
        links_html: str = "",
        extra_head_html: str = "",
    ) -> str:
        words = " ".join(f"{label}word{i}" for i in range(520))
        return (
            f'<html lang="en"><head><title>Title {label}</title>'
            f'<meta name="description" content="Description for {label}">'
            f'<link rel="canonical" href="{url}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{extra_head_html}</head>"
            f"<body><main><h1>Heading {label}</h1><p>{words}</p>{links_html}</main></body></html>"
        )

    def crawl_site_map(
        self,
        page_map: dict[str, str],
        max_pages: int = 10,
        max_depth: int = 5,
    ) -> list[dict]:
        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, list(page_map.keys()))
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        def fake_head(url, **_):
            if url in page_map:
                return make_response("HEAD", url, 200)
            return make_response("HEAD", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
             patch("crawler.time.sleep"):
            return crawler.crawl_website(
                START_URL,
                max_pages=max_pages,
                max_depth=max_depth,
            )

    def test_orphan_page_detected_excluding_start_url_and_self_links_ignored(self):
        orphan_url = "https://example.com/orphan-page"
        linked_url = "https://example.com/linked-page"
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    f'<a href="{linked_url}">Linked Page</a>'
                    f"<loc>{orphan_url}</loc>"
                ),
            ),
            linked_url: self.make_valid_page_html(
                linked_url,
                "linked",
                links_html=f'<a href="{START_URL}">Back Home</a>',
            ),
            # Orphan page links only to itself (self-link) and back to home,
            # with 0 incoming <a href> links from any other crawled page
            orphan_url: self.make_valid_page_html(
                orphan_url,
                "orphan",
                links_html=(
                    f'<a href="{orphan_url}">Self Link</a>'
                    f'<a href="{orphan_url}#section">Self Fragment</a>'
                    f'<a href="{START_URL}">Home</a>'
                ),
            ),
        }
        pages = self.crawl_site_map(page_map, max_pages=5, max_depth=2)
        by_url = {p["url"]: p for p in pages}

        # Start URL should never be flagged as orphan_page
        home_types = [i["type"] for i in by_url[START_URL]["seo_issues"]]
        self.assertNotIn("orphan_page", home_types)

        # Orphan page has 0 incoming links from other crawled pages (self-links excluded)
        orphan_page = by_url[orphan_url]
        self.assertEqual(orphan_page["incoming_internal_links"], 0)
        self.assertEqual(orphan_page["outgoing_internal_links"], 1)
        orphan_issues = [
            i for i in orphan_page["seo_issues"] if i["type"] == "orphan_page"
        ]
        self.assertEqual(len(orphan_issues), 1)
        self.assertEqual(orphan_issues[0]["severity"], "warning")
        self.assertEqual(orphan_issues[0]["incoming_internal_links"], 0)
        # Orphan page should not also report duplicate low_internal_link_count
        self.assertNotIn(
            "low_internal_link_count",
            [i["type"] for i in orphan_page["seo_issues"]],
        )

    def test_incoming_link_counts_and_low_internal_link_count_threshold(self):
        hub_url = "https://example.com/hub"
        p1_url = "https://example.com/p1"
        p2_url = "https://example.com/p2"
        weak_url = "https://example.com/weak"

        # hub receives 3 incoming links (from START_URL, p1, p2) -> no low_internal_link_count
        # weak receives only 1 incoming link (from START_URL) -> flagged as low_internal_link_count
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    f'<a href="{hub_url}">Hub</a>'
                    f'<a href="{p1_url}">P1</a>'
                    f'<a href="{p2_url}">P2</a>'
                    f'<a href="{weak_url}">Weak</a>'
                ),
            ),
            hub_url: self.make_valid_page_html(
                hub_url,
                "hub",
                links_html=(
                    f'<a href="{START_URL}">Home</a>'
                    f'<a href="{p1_url}">P1</a>'
                    f'<a href="{p2_url}">P2</a>'
                ),
            ),
            p1_url: self.make_valid_page_html(
                p1_url,
                "p1",
                links_html=(
                    f'<a href="{START_URL}">Home</a>'
                    f'<a href="{hub_url}">Hub</a>'
                    f'<a href="{p2_url}">P2</a>'
                ),
            ),
            p2_url: self.make_valid_page_html(
                p2_url,
                "p2",
                links_html=(
                    f'<a href="{START_URL}">Home</a>'
                    f'<a href="{hub_url}">Hub</a>'
                    f'<a href="{p1_url}">P1</a>'
                ),
            ),
            weak_url: self.make_valid_page_html(
                weak_url,
                "weak",
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
        }

        pages = self.crawl_site_map(page_map, max_pages=5, max_depth=2)
        by_url = {p["url"]: p for p in pages}

        self.assertEqual(by_url[hub_url]["incoming_internal_links"], 3)
        self.assertEqual(by_url[hub_url]["seo_issues"], [])

        self.assertEqual(by_url[weak_url]["incoming_internal_links"], 1)
        weak_issues = [
            i
            for i in by_url[weak_url]["seo_issues"]
            if i["type"] == "low_internal_link_count"
        ]
        self.assertEqual(len(weak_issues), 1)
        self.assertEqual(weak_issues[0]["severity"], "warning")
        self.assertEqual(weak_issues[0]["incoming_internal_links"], 1)

    def test_deep_page_threshold_over_4_levels(self):
        urls = [START_URL] + [
            f"https://example.com/level-{d}" for d in range(1, 6)
        ]
        page_map = {}
        for idx, u in enumerate(urls):
            next_link = (
                f'<a href="{urls[idx + 1]}">Next Level</a>'
                if idx + 1 < len(urls)
                else f'<a href="{START_URL}">Back Home</a>'
            )
            page_map[u] = self.make_valid_page_html(
                u, f"lvl{idx}", links_html=next_link
            )

        pages = self.crawl_site_map(page_map, max_pages=6, max_depth=5)
        by_url = {p["url"]: p for p in pages}

        # Depth 0..4 should not trigger deep_page
        for d in range(1, 5):
            u = f"https://example.com/level-{d}"
            self.assertEqual(by_url[u]["depth"], d)
            deep_issues = [
                i for i in by_url[u]["seo_issues"] if i["type"] == "deep_page"
            ]
            self.assertEqual(deep_issues, [])

        # Depth 5 (> 4) triggers deep_page
        level_5 = by_url["https://example.com/level-5"]
        self.assertEqual(level_5["depth"], 5)
        deep_5 = [
            i for i in level_5["seo_issues"] if i["type"] == "deep_page"
        ]
        self.assertEqual(len(deep_5), 1)
        self.assertEqual(deep_5[0]["severity"], "warning")
        self.assertEqual(deep_5[0]["depth"], 5)

    def test_nofollow_internal_link_and_nofollow_page_without_duplication(self):
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    '<a href="/internal-a" rel="nofollow">Internal A</a>'
                    '<a href="/internal-a#sec" rel="nofollow">Internal A Dup</a>'
                    '<a href="/internal-b" rel="noopener nofollow">Internal B</a>'
                    '<a href="https://external.org/out" rel="nofollow">External Nofollow</a>'
                ),
                extra_head_html='<meta name="robots" content="index, nofollow">',
            )
        }
        pages = self.crawl_site_map(page_map, max_pages=1, max_depth=0)
        page = pages[0]

        nf_link_issues = [
            i
            for i in page["seo_issues"]
            if i["type"] == "nofollow_internal_link"
        ]
        self.assertEqual(len(nf_link_issues), 1)
        self.assertEqual(nf_link_issues[0]["severity"], "warning")
        self.assertEqual(
            nf_link_issues[0]["nofollow_links"],
            [
                "https://example.com/internal-a",
                "https://example.com/internal-b",
            ],
        )

        # nofollow_page is reported exactly once (no duplicate with detect_robots_seo_issues)
        nf_page_issues = [
            i for i in page["seo_issues"] if i["type"] == "nofollow_page"
        ]
        self.assertEqual(len(nf_page_issues), 1)

    def test_excessive_internal_links_and_duplicate_anchor_and_external_exclusion(self):
        # 100 unique internal links + 30 duplicate anchors + 50 external links -> 100 unique internal links (not flagged)
        links_100 = "".join(
            f'<a href="/item-{i}">Item {i}</a>' for i in range(1, 101)
        )
        dup_anchors = "".join(
            f'<a href="/item-1#frag-{i}">Dup {i}</a>' for i in range(30)
        )
        external_anchors = "".join(
            f'<a href="https://external.org/ext-{i}">Ext {i}</a>'
            for i in range(50)
        )
        boundary_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "boundary",
                links_html=links_100 + dup_anchors + external_anchors,
            )
        }
        boundary_page = self.crawl_site_map(
            boundary_map, max_pages=1, max_depth=0
        )[0]
        self.assertEqual(boundary_page["outgoing_internal_links"], 100)
        self.assertEqual(boundary_page["seo_issues"], [])

        # 101 unique internal links -> flagged as excessive_internal_links
        links_101 = links_100 + '<a href="/item-101">Item 101</a>'
        excessive_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "excessive",
                links_html=links_101 + dup_anchors + external_anchors,
            )
        }
        excessive_page = self.crawl_site_map(
            excessive_map, max_pages=1, max_depth=0
        )[0]
        self.assertEqual(excessive_page["outgoing_internal_links"], 101)
        excessive_issues = [
            i
            for i in excessive_page["seo_issues"]
            if i["type"] == "excessive_internal_links"
        ]
        self.assertEqual(len(excessive_issues), 1)
        self.assertEqual(excessive_issues[0]["severity"], "warning")
        self.assertEqual(excessive_issues[0]["outgoing_internal_links"], 101)

    def test_cycles_and_isolated_section_detection(self):
        main_url = "https://example.com/main"
        island_a = "https://example.com/island-a"
        island_b = "https://example.com/island-b"

        # START_URL <-> main_url are mutually connected;
        # START_URL has a single one-way link into island_a, and island_a <-> island_b
        # cycle between themselves with 0 outgoing links back to the main site.
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    f'<a href="{main_url}">Main</a>'
                    f'<a href="{island_a}">Island A</a>'
                ),
            ),
            main_url: self.make_valid_page_html(
                main_url,
                "main",
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
            island_a: self.make_valid_page_html(
                island_a,
                "islandA",
                links_html=f'<a href="{island_b}">Island B</a>',
            ),
            island_b: self.make_valid_page_html(
                island_b,
                "islandB",
                links_html=f'<a href="{island_a}">Island A</a>',
            ),
        }
        pages = self.crawl_site_map(page_map, max_pages=5, max_depth=3)
        by_url = {p["url"]: p for p in pages}

        self.assertNotIn(
            "isolated_section",
            [i["type"] for i in by_url[START_URL]["seo_issues"]],
        )
        self.assertNotIn(
            "isolated_section",
            [i["type"] for i in by_url[main_url]["seo_issues"]],
        )

        for island_url in (island_a, island_b):
            iso_issues = [
                i
                for i in by_url[island_url]["seo_issues"]
                if i["type"] == "isolated_section"
            ]
            self.assertEqual(len(iso_issues), 1)
            self.assertEqual(iso_issues[0]["severity"], "warning")
            self.assertEqual(
                iso_issues[0]["isolated_pages"],
                [island_a, island_b],
            )


class SitemapAndRobotsAuditTests(unittest.TestCase):
    def make_valid_page_html(
        self,
        url: str,
        label: str,
        links_html: str = "",
        canonical_url: str | None = None,
        extra_head_html: str = "",
    ) -> str:
        words = " ".join(f"{label}word{i}" for i in range(520))
        canon = canonical_url if canonical_url is not None else url
        return (
            f'<html lang="en"><head><title>Title {label}</title>'
            f'<meta name="description" content="Description for {label}">'
            f'<link rel="canonical" href="{canon}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{extra_head_html}</head>"
            f"<body><main><h1>Heading {label}</h1><p>{words}</p>{links_html}</main></body></html>"
        )

    def test_valid_sitemap_and_robots_produce_zero_issues_and_full_coverage(self):
        about_url = "https://example.com/about"
        contact_url = "https://example.com/contact"
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    f'<a href="{about_url}">About</a>'
                    f'<a href="{contact_url}">Contact</a>'
                ),
            ),
            about_url: self.make_valid_page_html(
                about_url,
                "about",
                links_html=(
                    f'<a href="{START_URL}">Home</a>'
                    f'<a href="{contact_url}">Contact</a>'
                ),
            ),
            contact_url: self.make_valid_page_html(
                contact_url,
                "contact",
                links_html=(
                    f'<a href="{START_URL}">Home</a>'
                    f'<a href="{about_url}">About</a>'
                ),
            ),
        }

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url, [START_URL, about_url, contact_url]
                )
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=5, max_depth=2)

        for p in pages:
            self.assertTrue(p["in_sitemap"])
            sitemap_or_robots_issues = [
                i
                for i in p["seo_issues"]
                if "sitemap" in i["type"] or "robots" in i["type"]
            ]
            self.assertEqual(sitemap_or_robots_issues, [])

        summary = crawler.summarize_sitemap_coverage(pages)
        self.assertEqual(
            summary,
            {
                "sitemap_urls_count": 3,
                "crawled_urls_count": 3,
                "missing_from_sitemap_count": 0,
                "sitemap_not_crawled_count": 0,
            },
        )

    def test_sitemap_index_and_url_normalization_and_duplicate_and_external_filtering(self):
        # Sitemap index pointing to two child sitemaps (with a duplicate child reference),
        # plus uppercase host, #fragment, duplicate <loc>, and external domain <loc>.
        page_b = "https://example.com/page-b"
        index_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "  <sitemap><loc>https://example.com/sitemap-main.xml</loc></sitemap>"
            "  <sitemap><loc>https://EXAMPLE.COM/sitemap-main.xml#dup</loc></sitemap>"
            "  <sitemap><loc>https://example.com/sitemap-extra.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        child_main_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "  <url><loc>https://EXAMPLE.COM</loc></url>"
            "  <url><loc>https://example.com/#top</loc></url>"
            "  <url><loc>https://external.org/other-site</loc></url>"
            "</urlset>"
        )
        child_extra_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"  <url><loc>{page_b}#section</loc></url>"
            f"  <url><loc>{page_b}</loc></url>"
            "</urlset>"
        )
        robots_txt = (
            "User-agent: *\n"
            "Allow: /\n"
            "Sitemap: https://example.com/sitemap_index.xml\n"
        )
        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL, "home", links_html=f'<a href="{page_b}">Page B</a>'
            ),
            page_b: self.make_valid_page_html(
                page_b, "b", links_html=f'<a href="{START_URL}">Home</a>'
            ),
        }
        fetched_sitemaps: list[str] = []

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, robots_txt)
            if url.endswith(".xml"):
                fetched_sitemaps.append(url)
                if url.endswith("/sitemap_index.xml"):
                    return make_response(
                        "GET", url, 200, index_xml, content_type="application/xml"
                    )
                if url.endswith("/sitemap-main.xml"):
                    return make_response(
                        "GET", url, 200, child_main_xml, content_type="application/xml"
                    )
                if url.endswith("/sitemap-extra.xml"):
                    return make_response(
                        "GET", url, 200, child_extra_xml, content_type="application/xml"
                    )
                return make_response("GET", url, 404, "")
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=5, max_depth=2)

        # Duplicate child sitemap URL should only be fetched once
        self.assertEqual(
            fetched_sitemaps.count("https://example.com/sitemap-main.xml"), 1
        )
        for p in pages:
            self.assertTrue(p["in_sitemap"])
            sitemap_issues = [
                i for i in p["seo_issues"] if "sitemap" in i["type"]
            ]
            self.assertEqual(sitemap_issues, [])

        summary = crawler.summarize_sitemap_coverage(pages)
        self.assertEqual(summary["sitemap_urls_count"], 2)
        self.assertEqual(summary["crawled_urls_count"], 2)
        self.assertEqual(summary["missing_from_sitemap_count"], 0)
        self.assertEqual(summary["sitemap_not_crawled_count"], 0)

    def test_missing_sitemap_and_missing_robots_txt_are_detected(self):
        html = self.make_valid_page_html(START_URL, "home")

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 404, "Not Found")
            if url.endswith("/sitemap.xml"):
                return make_response("GET", url, 404, "Not Found")
            return make_response("GET", url, 200, html)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)

        page = pages[0]
        self.assertFalse(page["in_sitemap"])
        issue_types = [i["type"] for i in page["seo_issues"]]
        self.assertIn("missing_robots_txt", issue_types)
        self.assertIn("missing_sitemap", issue_types)

        missing_robots = next(
            i for i in page["seo_issues"] if i["type"] == "missing_robots_txt"
        )
        self.assertEqual(missing_robots["severity"], "warning")
        self.assertEqual(missing_robots["status_code"], 404)

        missing_sitemap = next(
            i for i in page["seo_issues"] if i["type"] == "missing_sitemap"
        )
        self.assertEqual(missing_sitemap["severity"], "warning")
        self.assertEqual(
            missing_sitemap["sitemap_url"], "https://example.com/sitemap.xml"
        )

    def test_invalid_sitemap_cases_are_detected_without_crashing(self):
        html = self.make_valid_page_html(START_URL, "home")
        bad_sitemaps = [
            "<urlset><url><loc>https://example.com/unclosed",
            "   ",
            "<html><body>Not an XML sitemap</body></html>",
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></sitemapindex>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url></url></urlset>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>not-a-valid-url</loc></url></urlset>',
        ]

        for xml_payload in bad_sitemaps:
            def fake_get(url, body=xml_payload, **_):
                if url.endswith("/robots.txt"):
                    return make_response("GET", url, 200, ROBOTS_TXT)
                if url.endswith("/sitemap.xml"):
                    return make_response(
                        "GET", url, 200, body, content_type="application/xml"
                    )
                return make_response("GET", url, 200, html)

            with patch("crawler.httpx.get", side_effect=fake_get), \
                 patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
                 patch("crawler.time.sleep"):
                pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)

            issue_types = [i["type"] for i in pages[0]["seo_issues"]]
            self.assertIn(
                "invalid_sitemap",
                issue_types,
                msg=f"Expected invalid_sitemap for payload: {xml_payload}",
            )
            self.assertNotIn("missing_sitemap", issue_types)

    def test_sitemap_coverage_mismatches_not_crawled_and_missing_from_sitemap(self):
        crawled_not_in_sitemap = "https://example.com/in-crawl-only"
        uncrawled_in_sitemap_1 = "https://example.com/in-sitemap-only-1"
        uncrawled_in_sitemap_2 = "https://example.com/in-sitemap-only-2"

        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=f'<a href="{crawled_not_in_sitemap}">Crawl Only</a>',
            ),
            crawled_not_in_sitemap: self.make_valid_page_html(
                crawled_not_in_sitemap,
                "crawlonly",
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
        }

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url,
                    [START_URL, uncrawled_in_sitemap_1, uncrawled_in_sitemap_2],
                )
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=5, max_depth=2)

        by_url = {p["url"]: p for p in pages}
        self.assertTrue(by_url[START_URL]["in_sitemap"])
        self.assertFalse(by_url[crawled_not_in_sitemap]["in_sitemap"])

        not_crawled_issues = [
            i
            for i in by_url[START_URL]["seo_issues"]
            if i["type"] == "sitemap_url_not_crawled"
        ]
        self.assertEqual(len(not_crawled_issues), 1)
        self.assertEqual(not_crawled_issues[0]["severity"], "warning")
        self.assertEqual(not_crawled_issues[0]["sitemap_not_crawled_count"], 2)
        self.assertEqual(
            not_crawled_issues[0]["sitemap_urls"],
            [uncrawled_in_sitemap_1, uncrawled_in_sitemap_2],
        )

        missing_issues = [
            i
            for i in by_url[crawled_not_in_sitemap]["seo_issues"]
            if i["type"] == "crawled_url_missing_from_sitemap"
        ]
        self.assertEqual(len(missing_issues), 1)
        self.assertEqual(missing_issues[0]["severity"], "warning")
        self.assertEqual(missing_issues[0]["url"], crawled_not_in_sitemap)

        summary = crawler.summarize_sitemap_coverage(pages)
        self.assertEqual(
            summary,
            {
                "sitemap_urls_count": 3,
                "crawled_urls_count": 2,
                "missing_from_sitemap_count": 1,
                "sitemap_not_crawled_count": 2,
            },
        )

    def test_sitemap_noncanonical_url_and_sitemap_non_indexable_url(self):
        non_canon_page = "https://example.com/dup-page"
        canonical_target = "https://example.com/preferred-page"
        noindex_page = "https://example.com/noindex-page"
        redirect_in_sitemap = "https://example.com/old-redirect"

        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=(
                    f'<a href="{non_canon_page}">Dup</a>'
                    f'<a href="{noindex_page}">Noindex</a>'
                    f'<a href="{redirect_in_sitemap}">Old Redirect</a>'
                ),
            ),
            non_canon_page: self.make_valid_page_html(
                non_canon_page,
                "dup",
                canonical_url=canonical_target,
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
            noindex_page: self.make_valid_page_html(
                noindex_page,
                "private",
                extra_head_html='<meta name="robots" content="noindex, follow">',
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
        }

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url,
                    [
                        START_URL,
                        non_canon_page,
                        noindex_page,
                        redirect_in_sitemap,
                    ],
                )
            if url == redirect_in_sitemap:
                return make_response(
                    "GET", url, 301, "", location=START_URL
                )
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        def fake_head(url, **_):
            if url == redirect_in_sitemap:
                return make_response(
                    "HEAD", url, 301, location=START_URL
                )
            return make_response("HEAD", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=5, max_depth=2)

        by_url = {p["url"]: p for p in pages}

        # 1. non_canon_page has canonical pointing to canonical_target, and is in sitemap
        dup_noncanon = [
            i
            for i in by_url[non_canon_page]["seo_issues"]
            if i["type"] == "sitemap_noncanonical_url"
        ]
        self.assertEqual(len(dup_noncanon), 1)
        self.assertEqual(dup_noncanon[0]["canonical_url"], canonical_target)

        # 2. redirect_in_sitemap redirected to START_URL and was listed in sitemap
        redir_noncanon = [
            i
            for p in pages
            for i in p["seo_issues"]
            if i["type"] == "sitemap_noncanonical_url"
            and i.get("sitemap_url") == redirect_in_sitemap
        ]
        self.assertEqual(len(redir_noncanon), 1)
        self.assertEqual(redir_noncanon[0]["source_url"], redirect_in_sitemap)
        self.assertEqual(redir_noncanon[0]["destination_url"], START_URL)

        # 3. noindex_page is marked noindex and is listed in sitemap -> sitemap_non_indexable_url
        #    also not flagged as missing from sitemap
        private_issues = by_url[noindex_page]["seo_issues"]
        non_indexable = [
            i
            for i in private_issues
            if i["type"] == "sitemap_non_indexable_url"
        ]
        self.assertEqual(len(non_indexable), 1)
        self.assertEqual(non_indexable[0]["severity"], "warning")
        self.assertEqual(
            non_indexable[0]["robots_directive"], "noindex, follow"
        )

    def test_invalid_robots_txt_and_robots_sitemap_missing_and_blocked_urls(self):
        # 1. Invalid robots.txt syntax (directive before User-agent, missing colon, unknown directive, invalid sitemap URL)
        bad_robots = (
            "Disallow: /orphan-rule\n"
            "User-agent: *\n"
            "malformed line without colon\n"
            "Unknown-directive: value\n"
            "Crawl-delay: not-a-number\n"
            "Sitemap: relative-sitemap.xml\n"
        )
        # 2. Discoverable /sitemap.xml exists, but robots.txt has no valid Sitemap: directive -> robots_sitemap_missing
        # 3. Sitemap includes /blocked-in-sitemap which is disallowed in robots.txt -> robots_blocked_sitemap_url
        # 4. Start page is disallowed for Googlebot (while allowed for CrawlScope) -> robots_blocked_crawled_url
        robots_with_blocks = (
            "User-agent: CrawlScope\n"
            "Allow: /\n"
            "Disallow: /blocked-in-sitemap\n\n"
            "User-agent: Googlebot\n"
            "Disallow: /google-blocked\n"
        )
        google_blocked_url = "https://example.com/google-blocked"
        blocked_sitemap_url = "https://example.com/blocked-in-sitemap"

        page_map = {
            START_URL: self.make_valid_page_html(
                START_URL,
                "home",
                links_html=f'<a href="{google_blocked_url}">Google Blocked</a>',
            ),
            google_blocked_url: self.make_valid_page_html(
                google_blocked_url,
                "gblocked",
                links_html=f'<a href="{START_URL}">Home</a>',
            ),
        }

        def fake_get_bad_robots(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, bad_robots)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, [START_URL])
            return make_response("GET", url, 200, page_map[START_URL])

        with patch("crawler.httpx.get", side_effect=fake_get_bad_robots), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            bad_robots_pages = crawler.crawl_website(
                START_URL, max_pages=1, max_depth=0
            )

        bad_robots_types = [
            i["type"] for i in bad_robots_pages[0]["seo_issues"]
        ]
        self.assertIn("invalid_robots_txt", bad_robots_types)
        self.assertIn("robots_sitemap_missing", bad_robots_types)

        def fake_get_blocks(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, robots_with_blocks)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(
                    url,
                    [START_URL, google_blocked_url, blocked_sitemap_url],
                )
            if url in page_map:
                return make_response("GET", url, 200, page_map[url])
            return make_response("GET", url, 404, "")

        with patch("crawler.httpx.get", side_effect=fake_get_blocks), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", START_URL, 200)), \
             patch("crawler.time.sleep"):
            block_pages = crawler.crawl_website(
                START_URL, max_pages=5, max_depth=2
            )

        by_url = {p["url"]: p for p in block_pages}

        # robots_sitemap_missing on start page
        start_issues = by_url[START_URL]["seo_issues"]
        self.assertTrue(
            any(i["type"] == "robots_sitemap_missing" for i in start_issues)
        )

        # robots_blocked_sitemap_url for /blocked-in-sitemap and /google-blocked
        all_blocked_sitemap_issues = [
            i
            for p in block_pages
            for i in p["seo_issues"]
            if i["type"] == "robots_blocked_sitemap_url"
        ]
        blocked_sitemap_locs = [
            i["sitemap_url"] for i in all_blocked_sitemap_issues
        ]
        self.assertIn(blocked_sitemap_url, blocked_sitemap_locs)
        self.assertIn(google_blocked_url, blocked_sitemap_locs)

        # robots_blocked_crawled_url on google_blocked_url (crawled because CrawlScope is allowed, but blocked for Googlebot)
        gblocked_issues = [
            i
            for i in by_url[google_blocked_url]["seo_issues"]
            if i["type"] == "robots_blocked_crawled_url"
        ]
        self.assertEqual(len(gblocked_issues), 1)
        self.assertEqual(gblocked_issues[0]["severity"], "warning")
        self.assertEqual(
            gblocked_issues[0]["directive_source"], "robots_txt"
        )


class SecurityAndHttpsAuditTests(unittest.TestCase):
    def build_valid_html(
        self,
        url: str = START_URL,
        body_inner_html: str = "",
        extra_head_html: str = "",
    ) -> str:
        return (
            f'<html lang="en"><head><title>Secure Page</title>'
            '<meta name="description" content="Secure page description">'
            f'<link rel="canonical" href="{url}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script>"
            f"{extra_head_html}</head>"
            f"<body><main><h1>Secure Heading</h1><p>{DEFAULT_BODY_WORDS}</p>{body_inner_html}</main></body></html>"
        )

    def crawl_security_page(
        self,
        page_url: str = START_URL,
        body_inner_html: str = "",
        extra_head_html: str = "",
        response_headers: dict[str, str] | list[tuple[str, str]] | None = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> dict:
        html = self.build_valid_html(
            url=page_url,
            body_inner_html=body_inner_html,
            extra_head_html=extra_head_html,
        )
        if response_headers is None:
            headers_to_use: dict[str, str] | list[tuple[str, str]] = {
                "content-type": content_type,
                **DEFAULT_SECURITY_HEADERS,
            }
        elif isinstance(response_headers, dict):
            headers_to_use = {
                "content-type": content_type,
                **response_headers,
            }
        else:
            headers_to_use = [("content-type", content_type), *response_headers]

        robots_txt = (
            "User-agent: *\n"
            "Disallow: /private\n"
            f"Sitemap: {page_url.split('//')[0]}//example.com/sitemap.xml\n"
        )

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, robots_txt)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (page_url,))
            return httpx.Response(
                200,
                text=html,
                headers=headers_to_use,
                request=httpx.Request("GET", url),
            )

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", return_value=make_response("HEAD", page_url, 200)), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(page_url, max_pages=1, max_depth=0)
        return pages[0]

    def test_clean_https_page_with_all_security_headers_produces_no_issues(self):
        page = self.crawl_security_page(
            page_url=START_URL,
            body_inner_html=(
                '<a href="https://example.com/about">About</a>'
                '<img src="https://example.com/logo.png" alt="Logo">'
                '<script src="https://cdn.example.org/app.js"></script>'
                '<form action="https://example.com/login" method="post">'
                '  <label for="pw">Password</label>'
                '  <input id="pw" type="password" name="password">'
                "</form>"
            ),
        )
        self.assertTrue(page["is_https"])
        self.assertEqual(page["seo_issues"], [])

        summary = crawler.summarize_security_audit([page])
        self.assertEqual(
            summary,
            {
                "https_pages_count": 1,
                "http_pages_count": 0,
                "mixed_content_pages_count": 0,
                "missing_security_headers_pages_count": 0,
                "total_security_issues": 0,
                "severity_counts": {"error": 0, "warning": 0, "info": 0},
            },
        )

    def test_http_page_detected_and_missing_hsts_not_reported_on_http(self):
        http_url = "http://example.com/"
        # Provide all non-HSTS security headers; HTTP page should report http_page
        # and should NOT report missing_hsts or mixed_content even when referencing HTTP images/scripts
        non_hsts_headers = {
            k: v
            for k, v in DEFAULT_SECURITY_HEADERS.items()
            if k != "strict-transport-security"
        }
        page = self.crawl_security_page(
            page_url=http_url,
            body_inner_html=(
                '<a href="/relative-link">Relative Link</a>'
                '<img src="http://example.com/banner.jpg" alt="Banner">'
                '<script src="http://cdn.external.org/lib.js"></script>'
            ),
            response_headers=non_hsts_headers,
        )
        self.assertFalse(page["is_https"])
        issue_types = [i["type"] for i in page["seo_issues"]]
        self.assertEqual(issue_types, ["http_page"])
        self.assertEqual(page["seo_issues"][0]["severity"], "warning")
        self.assertEqual(page["seo_issues"][0]["url"], http_url)

    def test_http_internal_link_detected_without_flagging_external_http_links(self):
        page = self.crawl_security_page(
            page_url=START_URL,
            body_inner_html=(
                '<a href="http://example.com/insecure-page">Insecure Internal 1</a>'
                '<a href="http://example.com/insecure-page#sec">Insecure Internal Dup</a>'
                '<a href="http://EXAMPLE.COM/second-insecure">Insecure Internal 2</a>'
                '<a href="https://example.com/secure-page">Secure Internal</a>'
                '<a href="http://external.org/out">External HTTP Link</a>'
            ),
        )
        security_issues = [
            i for i in page["seo_issues"] if i["type"] in crawler.SECURITY_ISSUE_TYPES
        ]
        self.assertEqual(len(security_issues), 1)
        issue = security_issues[0]
        self.assertEqual(issue["type"], "http_internal_link")
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(
            issue["insecure_urls"],
            [
                "http://example.com/insecure-page",
                "http://EXAMPLE.COM/second-insecure",
            ],
        )
        self.assertTrue(
            any(i["type"] == "external_link_to_http" for i in page["seo_issues"])
        )

    def test_mixed_content_and_internal_vs_external_http_resources(self):
        # 1. Internal-only HTTP subresources on an HTTPS page -> mixed_content only
        internal_mixed_page = self.crawl_security_page(
            page_url=START_URL,
            extra_head_html='<link rel="stylesheet" href="http://example.com/styles.css">',
            body_inner_html=(
                '<img src="http://example.com/photo.jpg" alt="Photo">'
                '<script src="http://example.com/bundle.js"></script>'
                '<iframe src="http://example.com/embed" title="Embed"></iframe>'
            ),
        )
        internal_types = [i["type"] for i in internal_mixed_page["seo_issues"]]
        self.assertEqual(internal_types, ["mixed_content"])
        mixed_issue = internal_mixed_page["seo_issues"][0]
        self.assertEqual(mixed_issue["severity"], "warning")
        self.assertEqual(
            mixed_issue["insecure_urls"],
            [
                "http://example.com/bundle.js",
                "http://example.com/styles.css",
                "http://example.com/photo.jpg",
                "http://example.com/embed",
            ],
        )

        # 2. External HTTP subresources on an HTTPS page -> mixed_content AND insecure_external_resource
        external_mixed_page = self.crawl_security_page(
            page_url=START_URL,
            extra_head_html='<link rel="stylesheet" href="http://cdn.external.org/theme.css">',
            body_inner_html=(
                '<script src="http://cdn.external.org/tracker.js"></script>'
                '<img src="http://example.com/local.jpg" alt="Local">'
            ),
        )
        ext_types = [i["type"] for i in external_mixed_page["seo_issues"]]
        self.assertEqual(
            ext_types, ["mixed_content", "insecure_external_resource"]
        )
        ext_issue = next(
            i
            for i in external_mixed_page["seo_issues"]
            if i["type"] == "insecure_external_resource"
        )
        self.assertEqual(ext_issue["severity"], "warning")
        self.assertEqual(
            ext_issue["insecure_urls"],
            [
                "http://cdn.external.org/tracker.js",
                "http://cdn.external.org/theme.css",
            ],
        )

    def test_insecure_form_action_and_sensitive_form_over_http(self):
        # 1. Non-sensitive form on HTTPS page submitting to HTTP -> insecure_form_action only
        non_sensitive_form_page = self.crawl_security_page(
            page_url=START_URL,
            body_inner_html=(
                '<form action="http://example.com/search" method="get">'
                '  <label for="q">Search</label>'
                '  <input id="q" type="search" name="query">'
                "</form>"
            ),
        )
        self.assertEqual(
            [i["type"] for i in non_sensitive_form_page["seo_issues"]],
            ["insecure_form_action"],
        )
        self.assertEqual(
            non_sensitive_form_page["seo_issues"][0]["form_action"],
            "http://example.com/search",
        )

        # 2. Sensitive password form on HTTPS page submitting to HTTP -> both insecure_form_action and sensitive_form_over_http
        https_sensitive_page = self.crawl_security_page(
            page_url=START_URL,
            body_inner_html=(
                '<form action="http://example.com/auth">'
                '  <label for="user-pass">Password</label>'
                '  <input id="user-pass" type="password" name="pwd">'
                "</form>"
            ),
        )
        self.assertEqual(
            [i["type"] for i in https_sensitive_page["seo_issues"]],
            ["insecure_form_action", "sensitive_form_over_http"],
        )

        # 3. Sensitive form (by name/autocomplete) on an HTTP page with relative action -> http_page and sensitive_form_over_http
        http_url = "http://example.com/"
        non_hsts_headers = {
            k: v
            for k, v in DEFAULT_SECURITY_HEADERS.items()
            if k != "strict-transport-security"
        }
        http_sensitive_page = self.crawl_security_page(
            page_url=http_url,
            body_inner_html=(
                '<form action="/checkout" method="post">'
                '  <label for="cc">Card Number</label>'
                '  <input id="cc" type="text" name="credit_card" autocomplete="cc-number">'
                "</form>"
            ),
            response_headers=non_hsts_headers,
        )
        self.assertEqual(
            [i["type"] for i in http_sensitive_page["seo_issues"]],
            ["http_page", "sensitive_form_over_http"],
        )
        sens_issue = http_sensitive_page["seo_issues"][1]
        self.assertEqual(sens_issue["severity"], "warning")
        self.assertEqual(
            sens_issue["form_action"], "http://example.com/checkout"
        )

        # 4. Non-sensitive form on an HTTP page -> http_page only (no false-positive sensitive_form_over_http or insecure_form_action)
        http_search_page = self.crawl_security_page(
            page_url=http_url,
            body_inner_html=(
                '<form action="/search" method="get">'
                '  <label for="s">Keyword</label>'
                '  <input id="s" type="text" name="keyword">'
                "</form>"
            ),
            response_headers=non_hsts_headers,
        )
        self.assertEqual(
            [i["type"] for i in http_search_page["seo_issues"]],
            ["http_page"],
        )

    def test_missing_security_headers_and_case_insensitive_and_csp_frame_ancestors(self):
        # 1. Missing all security headers on HTTPS page -> all 6 header issues reported
        no_sec_headers_page = self.crawl_security_page(
            page_url=START_URL,
            response_headers={},
        )
        issue_types = [i["type"] for i in no_sec_headers_page["seo_issues"]]
        self.assertEqual(
            issue_types,
            [
                "missing_hsts",
                "missing_content_security_policy",
                "missing_x_content_type_options",
                "missing_x_frame_options",
                "missing_referrer_policy",
                "missing_permissions_policy",
            ],
        )

        summary = crawler.summarize_security_audit([no_sec_headers_page])
        self.assertEqual(
            summary,
            {
                "https_pages_count": 1,
                "http_pages_count": 0,
                "mixed_content_pages_count": 0,
                "missing_security_headers_pages_count": 1,
                "total_security_issues": 6,
                "severity_counts": {"error": 0, "warning": 4, "info": 2},
            },
        )

        # 2. Case-insensitive headers and CSP frame-ancestors satisfying X-Frame-Options
        csp_frame_headers = [
            ("STRICT-TRANSPORT-SECURITY", "max-age=63072000"),
            (
                "Content-Security-Policy",
                "default-src 'self'; FRAME-ANCESTORS 'none'",
            ),
            ("x-content-type-options", "nosniff"),
            ("REFERRER-POLICY", "no-referrer"),
            ("Permissions-Policy", "camera=()"),
        ]
        csp_frame_page = self.crawl_security_page(
            page_url=START_URL,
            response_headers=csp_frame_headers,
        )
        self.assertEqual(csp_frame_page["seo_issues"], [])

        # 3. CSP without frame-ancestors and without X-Frame-Options still reports missing_x_frame_options
        csp_without_frame_headers = {
            "Strict-Transport-Security": "max-age=31536000",
            "Content-Security-Policy": "default-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": "geolocation=()",
        }
        missing_frame_page = self.crawl_security_page(
            page_url=START_URL,
            response_headers=csp_without_frame_headers,
        )
        self.assertEqual(
            [i["type"] for i in missing_frame_page["seo_issues"]],
            ["missing_x_frame_options"],
        )

    def test_non_html_response_excluded_from_security_audit(self):
        json_page = self.crawl_security_page(
            page_url="http://example.com/api/data.json",
            response_headers={},
            content_type="application/json",
        )
        self.assertEqual(json_page["seo_issues"], [])


class ExternalLinkAndSafetyAuditTests(unittest.TestCase):
    def build_test_page_html(self, links_html: str) -> str:
        return (
            "<!DOCTYPE html>"
            '<html lang="en"><head><title>External Test</title>'
            f'<link rel="canonical" href="{START_URL}">'
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><main><h1>External Test</h1><p>{DEFAULT_BODY_WORDS}</p>{links_html}</main></body></html>"
        )

    def crawl_with_external_links(
        self,
        links_html: str,
        external_map: dict[str, Any] | None = None,
        verification_enabled: bool = True,
        max_external_checks: int = 50,
    ) -> dict[str, Any]:
        html = self.build_test_page_html(links_html)
        if external_map is None:
            external_map = {}

        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, (START_URL,))
            return make_response("GET", url, 200, html)

        def fake_head(url, **kwargs):
            if url in external_map:
                outcome = external_map[url]
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, tuple):
                    status, loc = outcome
                    headers = {"location": loc} if loc else {}
                    return make_response("HEAD", url, status, extra_headers=headers)
                if isinstance(outcome, int):
                    return make_response("HEAD", url, outcome)
                return outcome
            return make_response("HEAD", url, 200)

        def fake_resolve(hostname):
            if hostname in ("localhost", "internal.corp") or hostname.endswith(
                (".local", ".internal", ".lan")
            ):
                return ["127.0.0.1"]
            return ["93.184.216.34"]

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "_resolve_hostname_ips", side_effect=fake_resolve), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", verification_enabled), \
             patch.object(crawler, "MAX_EXTERNAL_LINK_CHECKS", max_external_checks), \
             patch("crawler.time.sleep"):
            pages = crawler.crawl_website(START_URL, max_pages=1, max_depth=0)
        return pages[0]

    def test_broken_external_link_detected_4xx_and_5xx(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/missing">Missing</a>'
            '<a href="https://ext.example.org/server-error">Error</a>',
            external_map={
                "https://ext.example.org/missing": 404,
                "https://ext.example.org/server-error": 500,
            },
        )
        broken_issues = [
            i for i in page["seo_issues"] if i["type"] == "broken_external_link"
        ]
        self.assertEqual(len(broken_issues), 1)
        issue = broken_issues[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(
            issue["external_urls"],
            [
                "https://ext.example.org/missing",
                "https://ext.example.org/server-error",
            ],
        )
        self.assertIn("https://ext.example.org/missing (HTTP 404)", issue["message"])

    def test_working_external_link_produces_no_error_issue(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/valid">Working</a>',
            external_map={"https://ext.example.org/valid": 200},
        )
        error_types = {
            "broken_external_link",
            "external_redirect",
            "external_link_timeout",
            "external_link_ssl_error",
        }
        found_error_types = [
            i["type"] for i in page["seo_issues"] if i["type"] in error_types
        ]
        self.assertEqual(found_error_types, [])
        self.assertEqual(page["outgoing_external_links"], 1)

    def test_external_redirect_recorded_with_destination_and_status(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/old">Old Link</a>',
            external_map={
                "https://ext.example.org/old": (301, "https://ext.example.org/new"),
                "https://ext.example.org/new": 200,
            },
        )
        redirect_issues = [
            i for i in page["seo_issues"] if i["type"] == "external_redirect"
        ]
        self.assertEqual(len(redirect_issues), 1)
        issue = redirect_issues[0]
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["source_url"], "https://ext.example.org/old")
        self.assertEqual(issue["destination_url"], "https://ext.example.org/new")
        self.assertEqual(issue["status_code"], 301)
        self.assertEqual(
            issue["redirect_chain"],
            ["https://ext.example.org/old", "https://ext.example.org/new"],
        )

    def test_external_link_timeout_detected(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/slow">Slow External</a>',
            external_map={
                "https://ext.example.org/slow": httpx.TimeoutException("timed out")
            },
        )
        timeout_issues = [
            i for i in page["seo_issues"] if i["type"] == "external_link_timeout"
        ]
        self.assertEqual(len(timeout_issues), 1)
        issue = timeout_issues[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["external_urls"], ["https://ext.example.org/slow"])

    def test_external_link_ssl_error_detected(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/bad-cert">Bad SSL</a>',
            external_map={
                "https://ext.example.org/bad-cert": httpx.RequestError(
                    "certificate verify failed: self signed certificate"
                )
            },
        )
        ssl_issues = [
            i for i in page["seo_issues"] if i["type"] == "external_link_ssl_error"
        ]
        self.assertEqual(len(ssl_issues), 1)
        issue = ssl_issues[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["external_urls"], ["https://ext.example.org/bad-cert"])

    def test_target_blank_rel_noopener_and_noreferrer_rules(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/none" target="_blank">No Rel</a>'
            '<a href="https://ext.example.org/only-noopener" target="_blank" rel="noopener">Only Noopener</a>'
            '<a href="https://ext.example.org/safe" target="_blank" rel="noopener noreferrer">Both</a>'
            '<a href="https://ext.example.org/noreferrer-only" target="_blank" rel="noreferrer">Noreferrer Only</a>'
        )
        by_type = {i["type"]: i for i in page["seo_issues"]}
        self.assertIn("external_link_target_blank_noopener", by_type)
        self.assertEqual(by_type["external_link_target_blank_noopener"]["severity"], "warning")
        self.assertEqual(
            by_type["external_link_target_blank_noopener"]["external_urls"],
            ["https://ext.example.org/none"],
        )

        self.assertIn("external_link_target_blank_noreferrer", by_type)
        self.assertEqual(by_type["external_link_target_blank_noreferrer"]["severity"], "info")
        self.assertEqual(
            by_type["external_link_target_blank_noreferrer"]["external_urls"],
            ["https://ext.example.org/only-noopener"],
        )

    def test_external_link_to_http_detected(self):
        page = self.crawl_with_external_links(
            '<a href="http://ext.example.org/page">Insecure HTTP External</a>'
        )
        http_issues = [
            i for i in page["seo_issues"] if i["type"] == "external_link_to_http"
        ]
        self.assertEqual(len(http_issues), 1)
        self.assertEqual(http_issues[0]["severity"], "warning")
        self.assertEqual(
            http_issues[0]["external_urls"],
            ["http://ext.example.org/page"],
        )

    def test_nofollow_sponsored_ugc_external_links_detected(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/sponsored" rel="sponsored">Ad</a>'
            '<a href="https://ext.example.org/ugc" rel="ugc">Comment</a>'
            '<a href="https://ext.example.org/nf" rel="nofollow">NoFollow</a>'
        )
        rel_issues = [
            i for i in page["seo_issues"] if i["type"] == "nofollow_external_link"
        ]
        self.assertEqual(len(rel_issues), 1)
        issue = rel_issues[0]
        self.assertEqual(issue["severity"], "info")
        self.assertEqual(issue["rel_values"], ["nofollow", "sponsored", "ugc"])
        self.assertEqual(len(issue["external_urls"]), 3)

    def test_excessive_external_links_threshold(self):
        links = "".join(
            f'<a href="https://ext{i}.example.org/p">Ext {i}</a>'
            for i in range(55)
        )
        page = self.crawl_with_external_links(links)
        excessive_issues = [
            i for i in page["seo_issues"] if i["type"] == "excessive_external_links"
        ]
        self.assertEqual(len(excessive_issues), 1)
        issue = excessive_issues[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertEqual(issue["outgoing_external_links"], 55)

    def test_max_external_link_checks_limit_and_unverified_limit(self):
        links = "".join(
            f'<a href="https://ext{i}.example.org/p">Ext {i}</a>'
            for i in range(5)
        )
        page = self.crawl_with_external_links(
            links,
            max_external_checks=2,
        )
        summary = crawler.summarize_external_links([page])
        self.assertEqual(summary["unique_external_urls"], 5)
        self.assertEqual(summary["verified_count"], 2)
        self.assertEqual(summary["unverified_count"], 3)
        self.assertTrue(summary["limit_reached"])
        self.assertFalse(summary["verification_disabled"])

    def test_verification_disabled_marks_links_unverified_disabled(self):
        links = '<a href="https://ext.example.org/a">A</a>'
        page = self.crawl_with_external_links(
            links,
            verification_enabled=False,
        )
        summary = crawler.summarize_external_links([page])
        self.assertEqual(summary["unique_external_urls"], 1)
        self.assertEqual(summary["verified_count"], 0)
        self.assertEqual(summary["unverified_count"], 1)
        self.assertTrue(summary["verification_disabled"])

    def test_ssrf_safety_checks_block_private_and_internal_targets(self):
        unsafe_urls = [
            "http://localhost/admin",
            "http://127.0.0.1/status",
            "http://10.0.0.1/private",
            "http://172.16.0.1/intranet",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/internal",
            "http://service.local/api",
            "http://database.internal/query",
            "http://intranet.lan/home",
            "ftp://example.com/file",
        ]
        for url in unsafe_urls:
            is_safe, reason = crawler._check_external_target_safety(url)
            self.assertFalse(is_safe, f"Expected {url} to be unsafe, reason: {reason}")

        safe_url = "https://93.184.216.34/page"
        is_safe, _ = crawler._check_external_target_safety(safe_url)
        self.assertTrue(is_safe)

    def test_duplicate_external_urls_deduplicated_per_page(self):
        page = self.crawl_with_external_links(
            '<a href="https://ext.example.org/same">Link 1</a>'
            '<a href="https://ext.example.org/same">Link 2</a>'
            '<a href="https://ext.example.org/same#section">Link 3</a>'
        )
        self.assertEqual(page["outgoing_external_links"], 1)
        self.assertEqual(len(page["external_links"]), 1)

    def test_summarize_external_links_aggregation(self):
        pages = [
            {
                "url": "https://example.com/p1",
                "external_links": [
                    {"url": "https://ext1.org/", "verification_status": "working"},
                    {"url": "https://ext2.org/", "verification_status": "broken"},
                    {"url": "https://ext3.org/", "verification_status": "redirect"},
                ],
            },
            {
                "url": "https://example.com/p2",
                "external_links": [
                    {"url": "https://ext1.org/", "verification_status": "working"},
                    {"url": "https://ext4.org/", "verification_status": "timeout"},
                    {"url": "https://ext5.org/", "verification_status": "ssl_error"},
                    {"url": "https://ext6.org/", "verification_status": "blocked"},
                ],
            },
        ]
        summary = crawler.summarize_external_links(pages)
        self.assertEqual(summary["unique_external_urls"], 6)
        self.assertEqual(summary["verified_count"], 3)  # working, broken, redirect
        self.assertEqual(summary["broken_count"], 1)
        self.assertEqual(summary["redirect_count"], 1)
        self.assertEqual(summary["verification_failures_count"], 2)  # timeout, ssl_error
        self.assertEqual(summary["blocked_count"], 1)
        self.assertEqual(summary["unverified_count"], 0)
        self.assertFalse(summary["limit_reached"])
        self.assertFalse(summary["verification_disabled"])


class InternationalSeoAndHreflangAuditTests(unittest.TestCase):
    def make_page_html(
        self,
        url: str,
        canonical_url: str | None = None,
        hreflang_tags: list[tuple[str, str]] | None = None,
        body_links: list[str] | None = None,
        extra_head: str = "",
    ) -> str:
        can_url = canonical_url if canonical_url is not None else url
        hreflangs_html = ""
        if hreflang_tags:
            hreflangs_html = "\n".join(
                f'<link rel="alternate" hreflang="{lang}" href="{href}">'
                for lang, href in hreflang_tags
            )
        links_html = ""
        if body_links:
            links_html = "\n".join(f'<a href="{href}">{href}</a>' for href in body_links)

        return (
            "<!DOCTYPE html>"
            '<html lang="en"><head><title>Hreflang Test</title>'
            f'<link rel="canonical" href="{can_url}">'
            f"{hreflangs_html}\n{extra_head}"
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"WebPage"}'
            "</script></head>"
            f"<body><main><h1>Hreflang Page</h1><p>{DEFAULT_BODY_WORDS}</p>{links_html}</main></body></html>"
        )

    def crawl_hreflang_site(
        self,
        pages_map: dict[str, tuple[str, dict[str, str]]],
        start_url: str = START_URL,
        max_pages: int = 5,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        def fake_get(url, **_):
            if url.endswith("/robots.txt"):
                return make_response("GET", url, 200, ROBOTS_TXT)
            if url.endswith("/sitemap.xml"):
                return make_sitemap_response(url, list(pages_map.keys()))
            if url in pages_map:
                html, headers_dict = pages_map[url]
                headers = {
                    "content-type": "text/html; charset=utf-8",
                    **DEFAULT_SECURITY_HEADERS,
                    **headers_dict,
                }
                return make_response("GET", url, 200, text=html, extra_headers=headers)
            return make_response("GET", url, 404, text="Not Found")

        def fake_head(url, **_):
            return make_response("HEAD", url, 200)

        with patch("crawler.httpx.get", side_effect=fake_get), \
             patch("crawler.httpx.head", side_effect=fake_head), \
             patch.object(crawler, "EXTERNAL_LINK_VERIFICATION_ENABLED", False), \
             patch("crawler.time.sleep"):
            return crawler.crawl_website(
                start_url, max_pages=max_pages, max_depth=max_depth
            )

    def test_is_valid_hreflang_rules(self):
        # Valid language codes
        self.assertTrue(crawler.is_valid_hreflang("en"))
        self.assertTrue(crawler.is_valid_hreflang("es"))
        self.assertTrue(crawler.is_valid_hreflang("zh"))
        self.assertTrue(crawler.is_valid_hreflang("en-US"))
        self.assertTrue(crawler.is_valid_hreflang("fr-CA"))
        self.assertTrue(crawler.is_valid_hreflang("es-419"))
        self.assertTrue(crawler.is_valid_hreflang("zh-Hans"))
        self.assertTrue(crawler.is_valid_hreflang("zh-Hant-TW"))
        self.assertTrue(crawler.is_valid_hreflang("x-default"))
        self.assertTrue(crawler.is_valid_hreflang("X-DEFAULT"))
        self.assertTrue(crawler.is_valid_hreflang("fil"))

        # Invalid language codes
        self.assertFalse(crawler.is_valid_hreflang("en_US"))  # underscore
        self.assertFalse(crawler.is_valid_hreflang("us"))     # country alone
        self.assertFalse(crawler.is_valid_hreflang("english"))
        self.assertFalse(crawler.is_valid_hreflang("en-USA")) # 3-letter region
        self.assertFalse(crawler.is_valid_hreflang("123"))
        self.assertFalse(crawler.is_valid_hreflang(""))
        self.assertFalse(crawler.is_valid_hreflang(None))
        self.assertFalse(crawler.is_valid_hreflang("US-en"))

    def test_extract_link_header_hreflang_support(self):
        # Single Link header
        headers_single = {
            "link": '<https://example.com/fr>; rel="alternate"; hreflang="fr"'
        }
        res_single = crawler._extract_link_header_hreflangs(
            headers_single, "https://example.com/"
        )
        self.assertEqual(len(res_single), 1)
        self.assertEqual(res_single[0]["hreflang"], "fr")
        self.assertEqual(res_single[0]["url"], "https://example.com/fr")
        self.assertEqual(res_single[0]["source"], "header")

        # Multiple comma-separated Link headers (quoted and unquoted)
        headers_multiple = {
            "link": (
                '<https://example.com/es>; rel="alternate"; hreflang="es", '
                '<https://example.com/de>; rel=alternate; hreflang=de, '
                '<https://example.com/style.css>; rel="stylesheet"'
            )
        }
        res_multiple = crawler._extract_link_header_hreflangs(
            headers_multiple, "https://example.com/"
        )
        self.assertEqual(len(res_multiple), 2)
        langs = [r["hreflang"] for r in res_multiple]
        self.assertIn("es", langs)
        self.assertIn("de", langs)

    def test_invalid_hreflang_detected(self):
        page_url = "https://example.com/en"
        html = self.make_page_html(
            page_url,
            hreflang_tags=[
                ("en", page_url),
                ("en_US", "https://example.com/en-us"),
                ("english", "https://example.com/en-full"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        self.assertEqual(len(pages), 1)
        issues = [i for i in pages[0]["seo_issues"] if i["type"] == "invalid_hreflang"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(sorted(issues[0]["invalid_hreflangs"]), ["en_US", "english"])

    def test_duplicate_hreflang_language_detected(self):
        page_url = "https://example.com/en"
        html = self.make_page_html(
            page_url,
            hreflang_tags=[
                ("en", page_url),
                ("es", "https://example.com/es-1"),
                ("es", "https://example.com/es-2"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "duplicate_hreflang_language"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertEqual(issues[0]["duplicate_languages"], ["es"])

    def test_hreflang_missing_self_reference_detected(self):
        page_url = "https://example.com/en"
        html = self.make_page_html(
            page_url,
            hreflang_tags=[
                ("fr", "https://example.com/fr"),
                ("es", "https://example.com/es"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "hreflang_missing_self_reference"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")

    def test_hreflang_missing_x_default_detected(self):
        page_url = "https://example.com/en"
        # 2 distinct language alternates but no x-default
        html = self.make_page_html(
            page_url,
            hreflang_tags=[
                ("en", page_url),
                ("fr", "https://example.com/fr"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "hreflang_missing_x_default"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "info")

    def test_hreflang_http_url_detected_on_https_page(self):
        page_url = "https://example.com/en"
        html = self.make_page_html(
            page_url,
            hreflang_tags=[
                ("en", page_url),
                ("es", "http://example.com/es"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "hreflang_http_url"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "warning")
        self.assertEqual(issues[0]["insecure_urls"], ["http://example.com/es"])

    def test_hreflang_canonical_conflict_detected(self):
        # Page A has canonical pointing to Page B, but declares hreflang tags
        page_url = "https://example.com/en?utm=test"
        canonical_url = "https://example.com/en"
        html = self.make_page_html(
            page_url,
            canonical_url=canonical_url,
            hreflang_tags=[
                ("en", page_url),
                ("fr", "https://example.com/fr"),
            ],
        )
        pages = self.crawl_hreflang_site({page_url: (html, {})}, start_url=page_url)
        issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "hreflang_canonical_conflict"
        ]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertEqual(issues[0]["canonical_url"], canonical_url)

    def test_reciprocal_return_links_and_missing_return_link(self):
        url_en = "https://example.com/en"
        url_fr = "https://example.com/fr"

        # /en declares return to /fr; /fr DOES NOT declare return to /en
        html_en = self.make_page_html(
            url_en,
            hreflang_tags=[
                ("en", url_en),
                ("fr", url_fr),
                ("x-default", url_en),
            ],
            body_links=[url_fr],
        )
        html_fr_no_return = self.make_page_html(
            url_fr,
            hreflang_tags=[
                ("fr", url_fr),
            ],
        )

        site_map = {
            url_en: (html_en, {}),
            url_fr: (html_fr_no_return, {}),
        }
        pages = self.crawl_hreflang_site(site_map, start_url=url_en, max_pages=2, max_depth=1)
        by_url = {p["url"]: p for p in pages}

        en_missing_return = [
            i for i in by_url[url_en]["seo_issues"] if i["type"] == "hreflang_missing_return_link"
        ]
        self.assertEqual(len(en_missing_return), 1)
        self.assertEqual(en_missing_return[0]["severity"], "warning")
        self.assertEqual(en_missing_return[0]["missing_return_urls"], [url_fr])

        # Now test reciprocal when /fr links back to /en
        html_fr_reciprocal = self.make_page_html(
            url_fr,
            hreflang_tags=[
                ("fr", url_fr),
                ("en", url_en),
                ("x-default", url_en),
            ],
        )
        site_map_reciprocal = {
            url_en: (html_en, {}),
            url_fr: (html_fr_reciprocal, {}),
        }
        pages_reciprocal = self.crawl_hreflang_site(
            site_map_reciprocal, start_url=url_en, max_pages=2, max_depth=1
        )
        by_url_rec = {p["url"]: p for p in pages_reciprocal}
        en_clean = [
            i for i in by_url_rec[url_en]["seo_issues"] if i["type"] == "hreflang_missing_return_link"
        ]
        fr_clean = [
            i for i in by_url_rec[url_fr]["seo_issues"] if i["type"] == "hreflang_missing_return_link"
        ]
        self.assertEqual(en_clean, [])
        self.assertEqual(fr_clean, [])

    def test_hreflang_target_not_crawled_detected(self):
        url_en = "https://example.com/en"
        url_de_uncrawled = "https://example.com/de"

        html_en = self.make_page_html(
            url_en,
            hreflang_tags=[
                ("en", url_en),
                ("de", url_de_uncrawled),
                ("x-default", url_en),
            ],
        )
        pages = self.crawl_hreflang_site({url_en: (html_en, {})}, start_url=url_en, max_pages=1)
        uncrawled_issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "hreflang_target_not_crawled"
        ]
        self.assertEqual(len(uncrawled_issues), 1)
        self.assertEqual(uncrawled_issues[0]["severity"], "info")
        self.assertEqual(uncrawled_issues[0]["uncrawled_urls"], [url_de_uncrawled])

    def test_hreflang_noncanonical_target_detected(self):
        url_en = "https://example.com/en"
        url_es_target = "https://example.com/es"
        url_es_canonical = "https://example.com/es-canonical"

        html_en = self.make_page_html(
            url_en,
            hreflang_tags=[
                ("en", url_en),
                ("es", url_es_target),
                ("x-default", url_en),
            ],
            body_links=[url_es_target],
        )
        html_es = self.make_page_html(
            url_es_target,
            canonical_url=url_es_canonical,
            hreflang_tags=[
                ("es", url_es_target),
                ("en", url_en),
            ],
        )
        site_map = {
            url_en: (html_en, {}),
            url_es_target: (html_es, {}),
        }
        pages = self.crawl_hreflang_site(site_map, start_url=url_en, max_pages=2, max_depth=1)
        by_url = {p["url"]: p for p in pages}
        noncanonical_issues = [
            i for i in by_url[url_en]["seo_issues"] if i["type"] == "hreflang_noncanonical_target"
        ]
        self.assertEqual(len(noncanonical_issues), 1)
        self.assertEqual(noncanonical_issues[0]["severity"], "warning")
        self.assertEqual(
            noncanonical_issues[0]["noncanonical_targets"],
            [{"target_url": url_es_target, "canonical_url": url_es_canonical}],
        )

    def test_missing_hreflang_on_multilingual_site_detected(self):
        url_en = "https://example.com/en"
        url_contact = "https://example.com/contact"

        # /en has hreflang; /contact is crawled but has 0 hreflang
        html_en = self.make_page_html(
            url_en,
            hreflang_tags=[("en", url_en), ("x-default", url_en)],
            body_links=[url_contact],
        )
        html_contact = (
            "<!DOCTYPE html>"
            '<html lang="en"><head><title>Contact</title>'
            f'<link rel="canonical" href="{url_contact}">'
            '<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>'
            f"</head><body><main><h1>Contact</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
        )
        site_map = {
            url_en: (html_en, {}),
            url_contact: (html_contact, {}),
        }
        pages = self.crawl_hreflang_site(site_map, start_url=url_en, max_pages=2, max_depth=1)
        by_url = {p["url"]: p for p in pages}
        contact_missing = [
            i for i in by_url[url_contact]["seo_issues"] if i["type"] == "missing_hreflang"
        ]
        self.assertEqual(len(contact_missing), 1)
        self.assertEqual(contact_missing[0]["severity"], "info")

    def test_monolingual_site_does_not_flag_missing_hreflang(self):
        url_home = "https://example.com/"
        html = (
            "<!DOCTYPE html>"
            '<html lang="en"><head><title>Home</title>'
            f'<link rel="canonical" href="{url_home}">'
            '<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>'
            f"</head><body><main><h1>Home</h1><p>{DEFAULT_BODY_WORDS}</p></main></body></html>"
        )
        pages = self.crawl_hreflang_site({url_home: (html, {})}, start_url=url_home, max_pages=1)
        missing_issues = [
            i for i in pages[0]["seo_issues"] if i["type"] == "missing_hreflang"
        ]
        self.assertEqual(missing_issues, [])

    def test_summarize_hreflang_audit(self):
        sample_results = [
            {
                "url": "https://example.com/en",
                "hreflang_count": 2,
                "seo_issues": [
                    {
                        "type": "invalid_hreflang",
                        "severity": "error",
                    },
                    {
                        "type": "hreflang_missing_return_link",
                        "severity": "warning",
                    },
                ],
            },
            {
                "url": "https://example.com/fr",
                "hreflang_count": 0,
                "seo_issues": [
                    {
                        "type": "hreflang_target_not_crawled",
                        "severity": "info",
                    },
                    {
                        "type": "hreflang_canonical_conflict",
                        "severity": "error",
                    },
                ],
            },
        ]
        summary = crawler.summarize_hreflang_audit(sample_results)
        self.assertEqual(summary["pages_with_hreflang_count"], 1)
        self.assertEqual(summary["invalid_hreflang_count"], 1)
        self.assertEqual(summary["missing_return_links_count"], 1)
        self.assertEqual(summary["uncrawled_targets_count"], 1)
        self.assertEqual(summary["canonical_conflicts_count"], 1)
        self.assertEqual(summary["total_hreflang_issues"], 4)
        self.assertEqual(summary["severity_counts"]["error"], 2)
        self.assertEqual(summary["severity_counts"]["warning"], 1)
        self.assertEqual(summary["severity_counts"]["info"], 1)


if __name__ == "__main__":
    unittest.main()

