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
    "content_type": "text/html",
    "seo_issues": [
        {
            "type": "missing_meta_description",
            "severity": "warning",
            "message": "Page is missing a <meta name=\"description\"> tag or its content is empty.",
            "url": "https://example.com/",
        }
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
        self.assertEqual(data["pages"], [SAMPLE_PAGE])
        self.assertEqual(
            data["pages"][0]["seo_issues"][0]["type"],
            "missing_meta_description",
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

