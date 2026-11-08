import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crawler

EXPECTED_FILES = {
    "crawl_results.csv",
    "crawl_results.json",
    "link_results.csv",
    "link_results.json",
}

PAGE = {
    "url": "https://example.com/",
    "depth": 0,
    "status_code": 200,
    "title": "Home",
    "meta_description": "A site",
    "h1": "Welcome",
    "total_links": 2,
    "incoming_internal_links": 0,
    "outgoing_internal_links": 1,
    "is_https": True,
    "in_sitemap": True,
    "content_type": "text/html",
    "response_time_ms": 125.4,
    "page_size_bytes": 2048,
    "seo_issues": [],
    "error": None,
}

LINK = {
    "url": "https://example.com/missing",
    "found_on": "https://example.com/",
    "status_code": 404,
    "result": "broken",
    "error": None,
}


def fake_crawl_with_results(start_url, link_results=None, **options):
    link_results.append(dict(LINK))
    return [dict(PAGE)]


def fake_crawl_empty(start_url, link_results=None, **options):
    return []


class RunCrawlAndExportTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.output_dir = Path(temp_dir.name) / "output"

    def test_creates_all_four_files_in_output_directory(self):
        with patch("crawler.crawl_website", side_effect=fake_crawl_with_results):
            files = crawler.run_crawl_and_export(
                "https://example.com/", output_dir=self.output_dir
            )

        created = {path.name for path in self.output_dir.iterdir()}
        self.assertEqual(created, EXPECTED_FILES)
        self.assertEqual({path.name for path in files.values()}, EXPECTED_FILES)

    def test_exports_page_and_link_results(self):
        with patch("crawler.crawl_website", side_effect=fake_crawl_with_results):
            crawler.run_crawl_and_export(
                "https://example.com/", output_dir=self.output_dir
            )

        pages = json.loads((self.output_dir / "crawl_results.json").read_text(encoding="utf-8"))
        links = json.loads((self.output_dir / "link_results.json").read_text(encoding="utf-8"))
        self.assertEqual(pages, [PAGE])
        self.assertEqual(links, [LINK])

        with (self.output_dir / "crawl_results.csv").open(encoding="utf-8", newline="") as f:
            page_rows = list(csv.DictReader(f))
        with (self.output_dir / "link_results.csv").open(encoding="utf-8", newline="") as f:
            link_rows = list(csv.DictReader(f))
        self.assertEqual(page_rows[0]["title"], "Home")
        self.assertEqual(page_rows[0]["total_links"], "2")
        self.assertEqual(link_rows[0]["result"], "broken")
        self.assertEqual(link_rows[0]["status_code"], "404")

    def test_empty_results_are_exported_correctly(self):
        with patch("crawler.crawl_website", side_effect=fake_crawl_empty):
            crawler.run_crawl_and_export(
                "https://example.com/", output_dir=self.output_dir
            )

        for name in ("crawl_results.json", "link_results.json"):
            data = json.loads((self.output_dir / name).read_text(encoding="utf-8"))
            self.assertEqual(data, [])

        for name in ("crawl_results.csv", "link_results.csv"):
            lines = (self.output_dir / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_crawl_options_are_passed_to_crawl_website(self):
        with patch("crawler.crawl_website", side_effect=fake_crawl_empty) as mock_crawl:
            crawler.run_crawl_and_export(
                "https://example.com/",
                output_dir=self.output_dir,
                max_pages=3,
                max_depth=1,
            )

        args, kwargs = mock_crawl.call_args
        self.assertEqual(args, ("https://example.com/",))
        self.assertEqual(kwargs["max_pages"], 3)
        self.assertEqual(kwargs["max_depth"], 1)
        self.assertIsInstance(kwargs["link_results"], list)

    def test_default_output_directory_is_output(self):
        original_dir = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                import os

                os.chdir(temp_dir)
                with patch("crawler.crawl_website", side_effect=fake_crawl_empty):
                    crawler.run_crawl_and_export("https://example.com/")
                created = {path.name for path in Path("output").iterdir()}
            finally:
                os.chdir(original_dir)

        self.assertEqual(created, EXPECTED_FILES)

    def test_crawl_website_signature_is_unchanged(self):
        import inspect

        parameters = list(inspect.signature(crawler.crawl_website).parameters)
        self.assertEqual(
            parameters,
            [
                "start_url",
                "max_pages",
                "delay_seconds",
                "max_depth",
                "max_link_checks",
                "link_results",
                "max_image_checks",
                "max_url_length",
                "max_redirect_checks",
            ],
        )


if __name__ == "__main__":
    unittest.main()

