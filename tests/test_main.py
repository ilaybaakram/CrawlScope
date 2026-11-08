import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

SAMPLE_PAGE = {
    "url": "https://example.com/",
    "depth": 0,
    "status_code": 200,
    "title": "Example Domain",
    "meta_description": None,
    "h1": "Example Domain",
    "total_links": 1,
    "incoming_internal_links": 0,
    "outgoing_internal_links": 1,
    "outgoing_external_links": 0,
    "external_links": [],
    "hreflang_count": 0,
    "hreflang_links": [],
    "has_open_graph": False,
    "has_twitter_card": False,
    "readability_score": None,
    "is_https": True,
    "in_sitemap": True,
    "content_type": "text/html",
    "response_time_ms": 2300.0,
    "page_size_bytes": 1572864,
    "seo_issues": [
        {
            "type": "missing_meta_description",
            "severity": "warning",
            "message": "Page is missing a <meta name=\"description\"> tag or its content is empty.",
            "url": "https://example.com/",
        },
        {
            "type": "duplicate_title",
            "severity": "warning",
            "message": "Page title 'Example Domain' is duplicated across pages: https://example.com/, https://example.com/about",
            "url": "https://example.com/",
        },
        {
            "type": "missing_image_alt",
            "severity": "warning",
            "message": "Image is missing an alt attribute: https://example.com/hero.png",
            "url": "https://example.com/",
            "image_url": "https://example.com/hero.png",
        },
        {
            "type": "broken_image",
            "severity": "warning",
            "message": "Broken image (HTTP 404): https://example.com/missing.png",
            "url": "https://example.com/",
            "image_url": "https://example.com/missing.png",
            "status_code": 404,
        },
        {
            "type": "underscore_url",
            "severity": "warning",
            "message": "URL path contains underscores instead of hyphens: /seo_guide",
            "url": "https://example.com/seo_guide",
        },
        {
            "type": "redirect_chain",
            "severity": "warning",
            "message": "Redirect chain (2 hops, final HTTP 200): https://example.com/old -> https://example.com/mid -> https://example.com/new",
            "url": "https://example.com/",
            "source_url": "https://example.com/old",
            "destination_url": "https://example.com/new",
            "redirect_chain": [
                "https://example.com/old",
                "https://example.com/mid",
                "https://example.com/new",
            ],
            "status_code": 200,
        },
        {
            "type": "cross_domain_canonical",
            "severity": "info",
            "message": "Canonical URL points to a different domain (other.org vs example.com): https://other.org/canonical",
            "url": "https://example.com/",
            "canonical_url": "https://other.org/canonical",
        },
        {
            "type": "noindex_page",
            "severity": "warning",
            "message": "Page blocks search engine indexing via meta_robots: noindex, follow",
            "url": "https://example.com/",
            "robots_directive": "noindex, follow",
            "directive_source": "meta_robots",
        },
        {
            "type": "duplicate_schema_type",
            "severity": "warning",
            "message": "Multiple JSON-LD structured data blocks declare the same schema '@type': Organization",
            "url": "https://example.com/",
            "schema_types": ["Organization"],
        },
        {
            "type": "thin_content",
            "severity": "warning",
            "message": "Page has thin content (120 words; minimum recommended is 300 words).",
            "url": "https://example.com/",
            "word_count": 120,
        },
        {
            "type": "slow_response",
            "severity": "warning",
            "message": "Page response time (2300.0 ms) exceeds the 2.0s threshold.",
            "url": "https://example.com/",
            "response_time_ms": 2300.0,
            "page_size_bytes": 1572864,
        },
        {
            "type": "missing_lang",
            "severity": "warning",
            "message": "The root <html> element is missing a valid lang attribute (e.g., <html lang=\"en\">) to declare the page language for screen readers",
            "url": "https://example.com/",
        },
        {
            "type": "nofollow_internal_link",
            "severity": "warning",
            "message": "Page has 1 internal link(s) with rel=\"nofollow\" (https://example.com/about); remove nofollow on internal links so search engines can follow them and pass link equity.",
            "url": "https://example.com/",
            "nofollow_links": ["https://example.com/about"],
            "outgoing_internal_links": 1,
            "depth": 0,
        },
        {
            "type": "sitemap_url_not_crawled",
            "severity": "info",
            "message": "1 URL(s) listed in the XML sitemap were not discovered or crawled during the crawl: https://example.com/uncrawled",
            "url": "https://example.com/",
            "sitemap_url": "https://example.com/sitemap.xml",
            "sitemap_urls": ["https://example.com/uncrawled"],
            "sitemap_not_crawled_count": 1,
        },
        {
            "type": "missing_hsts",
            "severity": "warning",
            "message": "HTTPS response lacks a Strict-Transport-Security (HSTS) header; add 'Strict-Transport-Security: max-age=31536000; includeSubDomains' so browsers enforce HTTPS connections.",
            "url": "https://example.com/",
            "missing_header": "Strict-Transport-Security",
        },
        {
            "type": "missing_og_title",
            "severity": "warning",
            "message": "Page is missing an 'og:title' Open Graph meta tag; social platforms require a title for rich link previews.",
            "url": "https://example.com/",
        },
    ],
    "error": None,
}

SAMPLE_LINK = {
    "url": "https://example.com/about",
    "found_on": "https://example.com/",
    "status_code": 200,
    "result": "working",
    "error": None,
}


def fake_crawl_website(
    start_url: str,
    max_pages: int = 10,
    delay_seconds: float = 1.0,
    max_depth: int = 2,
    max_link_checks: int = 50,
    link_results=None,
):
    if link_results is not None:
        link_results.append(dict(SAMPLE_LINK))
    return [dict(SAMPLE_PAGE)]


class FastAPIEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_root_endpoint(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "project": "CrawlScope",
                "message": "Welcome to CrawlScope!",
                "status": "running",
            },
        )

    def test_crawl_failure_returns_500_with_detail(self) -> None:
        with patch("main.crawl_website", side_effect=RuntimeError("Connection aborted by remote host")):
            response = self.client.post(
                "/crawl",
                json={
                    "start_url": "https://example.com/",
                    "max_pages": 5,
                    "max_depth": 1,
                    "delay_seconds": 1.0,
                },
            )
        self.assertEqual(response.status_code, 500)
        data = response.json()
        self.assertIn("detail", data)
        self.assertIn("Crawl failed: Connection aborted by remote host", data["detail"])

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    def test_successful_crawl_request(self) -> None:
        with patch("main.crawl_website", side_effect=fake_crawl_website) as mock_crawl:
            response = self.client.post(
                "/crawl",
                json={
                    "start_url": "https://example.com/",
                    "max_pages": 5,
                    "max_depth": 1,
                    "delay_seconds": 1.5,
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["start_url"], "https://example.com/")
        self.assertEqual(data["pages_crawled"], 1)
        self.assertEqual(data["links_checked"], 1)
        self.assertEqual(
            data["sitemap_coverage"],
            {
                "sitemap_urls_count": 2,
                "crawled_urls_count": 1,
                "missing_from_sitemap_count": 0,
                "sitemap_not_crawled_count": 1,
            },
        )
        self.assertEqual(
            data["security_summary"],
            {
                "https_pages_count": 1,
                "http_pages_count": 0,
                "mixed_content_pages_count": 0,
                "missing_security_headers_pages_count": 1,
                "total_security_issues": 1,
                "severity_counts": {
                    "error": 0,
                    "warning": 1,
                    "info": 0,
                },
            },
        )
        self.assertEqual(
            data["external_link_summary"],
            {
                "unique_external_urls": 0,
                "verified_count": 0,
                "broken_count": 0,
                "redirect_count": 0,
                "verification_failures_count": 0,
                "blocked_count": 0,
                "unverified_count": 0,
                "verification_enabled": True,
                "verification_disabled": False,
                "limit_reached": False,
                "max_external_checks": 50,
            },
        )
        self.assertEqual(
            data["hreflang_summary"],
            {
                "pages_with_hreflang_count": 0,
                "invalid_hreflang_count": 0,
                "missing_return_links_count": 0,
                "uncrawled_targets_count": 0,
                "canonical_conflicts_count": 0,
                "total_hreflang_issues": 0,
                "severity_counts": {
                    "error": 0,
                    "warning": 0,
                    "info": 0,
                },
            },
        )
        self.assertEqual(
            data["social_summary"],
            {
                "pages_with_og_count": 0,
                "pages_with_twitter_card_count": 0,
                "missing_required_og_count": 1,
                "invalid_social_metadata_count": 0,
                "total_social_issues": 1,
                "severity_counts": {
                    "error": 0,
                    "warning": 1,
                    "info": 0,
                },
            },
        )
        self.assertEqual(
            data["readability_summary"],
            {
                "pages_audited_count": 1,
                "pages_with_issues_count": 0,
                "keyword_stuffing_count": 0,
                "low_readability_count": 0,
                "heading_issues_count": 0,
                "total_readability_issues": 0,
                "severity_counts": {
                    "error": 0,
                    "warning": 0,
                    "info": 0,
                },
            },
        )
        self.assertIn("site_health_summary", data)
        health = data["site_health_summary"]
        self.assertIn("site_health_score", health)
        self.assertIn("health_rating", health)
        self.assertIn("categories", health)
        self.assertIn("prioritized_issues", health)
        self.assertEqual(len(health["categories"]), 9)
        self.assertEqual(data["pages"], [SAMPLE_PAGE])
        self.assertEqual(
            [issue["type"] for issue in data["pages"][0]["seo_issues"]],
            [
                "missing_meta_description",
                "duplicate_title",
                "missing_image_alt",
                "broken_image",
                "underscore_url",
                "redirect_chain",
                "cross_domain_canonical",
                "noindex_page",
                "duplicate_schema_type",
                "thin_content",
                "slow_response",
                "missing_lang",
                "nofollow_internal_link",
                "sitemap_url_not_crawled",
                "missing_hsts",
                "missing_og_title",
            ],
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][7]["robots_directive"],
            "noindex, follow",
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][7]["directive_source"],
            "meta_robots",
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][8]["schema_types"],
            ["Organization"],
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][9]["word_count"],
            120,
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][10]["response_time_ms"],
            2300.0,
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][10]["page_size_bytes"],
            1572864,
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][6]["canonical_url"],
            "https://other.org/canonical",
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][5]["destination_url"],
            "https://example.com/new",
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][2]["image_url"],
            "https://example.com/hero.png",
        )
        self.assertEqual(
            data["pages"][0]["seo_issues"][3]["status_code"],
            404,
        )
        self.assertEqual(data["links"], [SAMPLE_LINK])

        mock_crawl.assert_called_once()
        _, kwargs = mock_crawl.call_args
        self.assertEqual(kwargs["start_url"], "https://example.com/")
        self.assertEqual(kwargs["max_pages"], 5)
        self.assertEqual(kwargs["max_depth"], 1)
        self.assertEqual(kwargs["delay_seconds"], 1.5)

    def test_invalid_url_returns_422(self) -> None:
        for bad_url in ("not-a-url", "ftp://example.com", "https://", ""):
            with self.subTest(bad_url=bad_url):
                response = self.client.post("/crawl", json={"start_url": bad_url})
                self.assertEqual(response.status_code, 422)

    def test_invalid_max_pages_returns_422(self) -> None:
        for bad_pages in (0, -1, 101):
            with self.subTest(bad_pages=bad_pages):
                response = self.client.post(
                    "/crawl",
                    json={"start_url": "https://example.com", "max_pages": bad_pages},
                )
                self.assertEqual(response.status_code, 422)

    def test_invalid_max_depth_returns_422(self) -> None:
        for bad_depth in (-1, 6):
            with self.subTest(bad_depth=bad_depth):
                response = self.client.post(
                    "/crawl",
                    json={"start_url": "https://example.com", "max_depth": bad_depth},
                )
                self.assertEqual(response.status_code, 422)

    def test_invalid_delay_seconds_returns_422(self) -> None:
        for bad_delay in (0.0, 0.4, 10.5):
            with self.subTest(bad_delay=bad_delay):
                response = self.client.post(
                    "/crawl",
                    json={"start_url": "https://example.com", "delay_seconds": bad_delay},
                )
                self.assertEqual(response.status_code, 422)

    def test_cors_allows_local_vite_origin(self) -> None:
        response = self.client.options(
            "/crawl",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://localhost:5173",
        )


if __name__ == "__main__":
    unittest.main()

