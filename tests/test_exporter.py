import csv
import json
import tempfile
import unittest
from pathlib import Path

import exporter

SAMPLE_PAGES = [
    {
        "url": "https://example.com/",
        "depth": 0,
        "status_code": 200,
        "title": "Home, \"Sweet\" Home",
        "meta_description": "Line one\nLine two",
        "h1": "Welcome",
        "total_links": 12,
        "incoming_internal_links": 2,
        "outgoing_internal_links": 8,
        "outgoing_external_links": 2,
        "hreflang_count": 0,
        "has_open_graph": False,
        "is_https": True,
        "in_sitemap": True,
        "content_type": "text/html; charset=utf-8",
        "response_time_ms": 140.25,
        "page_size_bytes": 4096,
        "seo_issues": [],
        "error": None,
    },
    {
        "url": "https://example.com/missing",
        "depth": 1,
        "status_code": 404,
        "title": None,
        "meta_description": None,
        "h1": None,
        "total_links": 0,
        "incoming_internal_links": 0,
        "outgoing_internal_links": 0,
        "outgoing_external_links": 0,
        "hreflang_count": 0,
        "has_open_graph": False,
        "is_https": True,
        "in_sitemap": False,
        "content_type": "text/html",
        "response_time_ms": 85.0,
        "page_size_bytes": 512,
        "seo_issues": [],
        "error": "Client error '404 Not Found'",
    },
    {
        "url": "https://example.com/café",
        "depth": 1,
        "status_code": 200,
        "title": "Café – 日本語",
        "meta_description": None,
        "h1": None,
        "total_links": 3,
        "incoming_internal_links": 1,
        "outgoing_internal_links": 2,
        "outgoing_external_links": 1,
        "hreflang_count": 0,
        "has_open_graph": False,
        "is_https": True,
        "in_sitemap": False,
        "content_type": "text/html",
        "response_time_ms": 2450.5,
        "page_size_bytes": 1572864,
        "seo_issues": [
            {
                "type": "missing_meta_description",
                "severity": "warning",
                "message": "Page is missing a <meta name=\"description\"> tag or its content is empty.",
                "url": "https://example.com/café",
            },
            {
                "type": "missing_h1",
                "severity": "warning",
                "message": "Page is missing an <h1> heading or the heading is empty.",
                "url": "https://example.com/café",
            },
            {
                "type": "duplicate_title",
                "severity": "warning",
                "message": "Page title 'Café – 日本語' is duplicated across pages: https://example.com/café, https://example.com/café-2",
                "url": "https://example.com/café",
            },
            {
                "type": "missing_image_alt",
                "severity": "warning",
                "message": "Image is missing an alt attribute: https://example.com/images/cup.jpg",
                "url": "https://example.com/café",
                "image_url": "https://example.com/images/cup.jpg",
            },
            {
                "type": "broken_image",
                "severity": "warning",
                "message": "Broken image (HTTP 404): https://example.com/images/broken.jpg",
                "url": "https://example.com/café",
                "image_url": "https://example.com/images/broken.jpg",
                "status_code": 404,
            },
            {
                "type": "query_parameter_url",
                "severity": "info",
                "message": "URL contains query parameters (?ref=menu).",
                "url": "https://example.com/café?ref=menu",
            },
            {
                "type": "redirect_url",
                "severity": "info",
                "message": "Redirecting URL (HTTP 301, final HTTP 200): https://example.com/old -> https://example.com/café",
                "url": "https://example.com/café",
                "source_url": "https://example.com/old",
                "destination_url": "https://example.com/café",
                "redirect_chain": [
                    "https://example.com/old",
                    "https://example.com/café",
                ],
                "status_code": 200,
            },
            {
                "type": "canonical_redirect",
                "severity": "warning",
                "message": "Canonical URL redirects from https://example.com/old-café to https://example.com/café",
                "url": "https://example.com/café",
                "canonical_url": "https://example.com/old-café",
                "destination_url": "https://example.com/café",
                "redirect_chain": [
                    "https://example.com/old-café",
                    "https://example.com/café",
                ],
                "status_code": 200,
            },
            {
                "type": "noindex_page",
                "severity": "warning",
                "message": "Page blocks search engine indexing via meta_robots: noindex, follow",
                "url": "https://example.com/café",
                "robots_directive": "noindex, follow",
                "directive_source": "meta_robots",
            },
            {
                "type": "duplicate_schema_type",
                "severity": "warning",
                "message": "Multiple JSON-LD structured data blocks declare the same schema '@type': Organization",
                "url": "https://example.com/café",
                "schema_types": ["Organization"],
            },
            {
                "type": "thin_content",
                "severity": "warning",
                "message": "Page has thin content (120 words; minimum recommended is 300 words).",
                "url": "https://example.com/café",
                "word_count": 120,
            },
            {
                "type": "slow_response",
                "severity": "warning",
                "message": "Page response time (2450.5 ms) exceeds the 2.0s threshold.",
                "url": "https://example.com/café",
                "response_time_ms": 2450.5,
                "page_size_bytes": 1572864,
            },
            {
                "type": "missing_lang",
                "severity": "warning",
                "message": "The root <html> element is missing a valid lang attribute (e.g., <html lang=\"en\">) to declare the page language for screen readers",
                "url": "https://example.com/café",
            },
            {
                "type": "low_internal_link_count",
                "severity": "warning",
                "message": "Page has only 1 incoming internal link(s) from other crawled pages (recommended minimum is 3); add contextual internal links to improve crawlability.",
                "url": "https://example.com/café",
                "incoming_internal_links": 1,
                "outgoing_internal_links": 2,
                "depth": 1,
            },
            {
                "type": "crawled_url_missing_from_sitemap",
                "severity": "warning",
                "message": "Crawled indexable HTML page is missing from the XML sitemap; add it so search engines can discover it reliably.",
                "url": "https://example.com/café",
                "sitemap_url": "https://example.com/sitemap.xml",
            },
            {
                "type": "missing_hsts",
                "severity": "warning",
                "message": "HTTPS response lacks a Strict-Transport-Security (HSTS) header; add 'Strict-Transport-Security: max-age=31536000; includeSubDomains' so browsers enforce HTTPS connections.",
                "url": "https://example.com/café",
                "missing_header": "Strict-Transport-Security",
            },
            {
                "type": "missing_og_image",
                "severity": "warning",
                "message": "Page is missing an 'og:image' Open Graph meta tag; social platforms need a preview image to generate rich visual cards.",
                "url": "https://example.com/café",
            },
        ],
        "error": None,
    },
]

SAMPLE_LINKS = [
    {
        "url": "https://example.com/ok",
        "found_on": "https://example.com/",
        "status_code": 200,
        "result": "working",
        "error": None,
    },
    {
        "url": "https://example.com/missing",
        "found_on": "https://example.com/",
        "status_code": 404,
        "result": "broken",
        "error": None,
    },
    {
        "url": "https://example.com/down",
        "found_on": "https://example.com/",
        "status_code": None,
        "result": "error",
        "error": "connection refused",
    },
]


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as json_file:
        return json.load(json_file)


class ExporterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.folder = Path(self.temp_dir.name)


class PageExportTests(ExporterTestCase):
    def test_csv_contains_all_fields_and_rows(self):
        path = exporter.export_pages_to_csv(SAMPLE_PAGES, self.folder / "pages.csv")
        rows = read_csv_rows(path)

        self.assertEqual(len(rows), 3)
        self.assertEqual(list(rows[0].keys()), exporter.PAGE_FIELDS)
        self.assertEqual(rows[0]["url"], "https://example.com/")
        self.assertEqual(rows[0]["status_code"], "200")
        self.assertEqual(rows[0]["total_links"], "12")
        self.assertEqual(rows[0]["content_type"], "text/html; charset=utf-8")
        self.assertEqual(rows[1]["status_code"], "404")
        self.assertEqual(rows[1]["error"], "Client error '404 Not Found'")

    def test_csv_handles_special_characters(self):
        path = exporter.export_pages_to_csv(SAMPLE_PAGES, self.folder / "pages.csv")
        rows = read_csv_rows(path)

        self.assertEqual(rows[0]["title"], "Home, \"Sweet\" Home")
        self.assertEqual(rows[0]["meta_description"], "Line one\nLine two")

    def test_csv_none_values_become_empty_strings(self):
        path = exporter.export_pages_to_csv(SAMPLE_PAGES, self.folder / "pages.csv")
        rows = read_csv_rows(path)

        self.assertEqual(rows[1]["title"], "")
        self.assertEqual(rows[1]["h1"], "")
        self.assertEqual(rows[0]["error"], "")

    def test_csv_is_utf_8(self):
        path = exporter.export_pages_to_csv(SAMPLE_PAGES, self.folder / "pages.csv")
        text = path.read_bytes().decode("utf-8")

        self.assertIn("Café – 日本語", text)

    def test_csv_preserves_extra_fields(self):
        pages = [dict(SAMPLE_PAGES[0], word_count=250)]
        path = exporter.export_pages_to_csv(pages, self.folder / "pages.csv")
        rows = read_csv_rows(path)

        self.assertEqual(rows[0]["word_count"], "250")

    def test_csv_empty_results_write_header_only(self):
        path = exporter.export_pages_to_csv([], self.folder / "pages.csv")

        self.assertEqual(read_csv_rows(path), [])
        header = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header, ",".join(exporter.PAGE_FIELDS))

    def test_json_round_trip_preserves_data(self):
        path = exporter.export_pages_to_json(SAMPLE_PAGES, self.folder / "pages.json")

        self.assertEqual(read_json(path), SAMPLE_PAGES)

    def test_json_keeps_none_as_null_and_utf_8(self):
        path = exporter.export_pages_to_json(SAMPLE_PAGES, self.folder / "pages.json")
        text = path.read_bytes().decode("utf-8")
        data = read_json(path)

        self.assertIsNone(data[1]["title"])
        self.assertIn("null", text)
        self.assertIn("Café – 日本語", text)

    def test_json_empty_results_write_empty_list(self):
        path = exporter.export_pages_to_json([], self.folder / "pages.json")

        self.assertEqual(read_json(path), [])

    def test_seo_issues_appear_in_csv_and_json_exports(self):
        csv_path = exporter.export_pages_to_csv(SAMPLE_PAGES, self.folder / "pages.csv")
        json_path = exporter.export_pages_to_json(SAMPLE_PAGES, self.folder / "pages.json")

        rows = read_csv_rows(csv_path)
        self.assertIn("seo_issues", rows[0])
        self.assertEqual(json.loads(rows[0]["seo_issues"]), [])
        parsed_issues = json.loads(rows[2]["seo_issues"])
        self.assertEqual(len(parsed_issues), 17)
        self.assertEqual(parsed_issues[0]["type"], "missing_meta_description")
        self.assertEqual(parsed_issues[2]["type"], "duplicate_title")
        self.assertEqual(parsed_issues[3]["type"], "missing_image_alt")
        self.assertEqual(
            parsed_issues[3]["image_url"],
            "https://example.com/images/cup.jpg",
        )
        self.assertEqual(parsed_issues[4]["type"], "broken_image")
        self.assertEqual(parsed_issues[4]["status_code"], 404)
        self.assertEqual(parsed_issues[5]["type"], "query_parameter_url")
        self.assertEqual(parsed_issues[5]["severity"], "info")
        self.assertEqual(parsed_issues[6]["type"], "redirect_url")
        self.assertEqual(
            parsed_issues[6]["redirect_chain"],
            ["https://example.com/old", "https://example.com/café"],
        )
        self.assertEqual(parsed_issues[7]["type"], "canonical_redirect")
        self.assertEqual(
            parsed_issues[7]["canonical_url"],
            "https://example.com/old-café",
        )
        self.assertEqual(parsed_issues[8]["type"], "noindex_page")
        self.assertEqual(
            parsed_issues[8]["robots_directive"],
            "noindex, follow",
        )
        self.assertEqual(
            parsed_issues[8]["directive_source"],
            "meta_robots",
        )
        self.assertEqual(parsed_issues[9]["type"], "duplicate_schema_type")
        self.assertEqual(
            parsed_issues[9]["schema_types"],
            ["Organization"],
        )
        self.assertEqual(parsed_issues[10]["type"], "thin_content")
        self.assertEqual(parsed_issues[10]["word_count"], 120)
        self.assertEqual(parsed_issues[11]["type"], "slow_response")
        self.assertEqual(parsed_issues[11]["response_time_ms"], 2450.5)
        self.assertEqual(parsed_issues[11]["page_size_bytes"], 1572864)
        self.assertEqual(parsed_issues[12]["type"], "missing_lang")
        self.assertEqual(parsed_issues[12]["severity"], "warning")
        self.assertEqual(parsed_issues[13]["type"], "low_internal_link_count")
        self.assertEqual(parsed_issues[13]["incoming_internal_links"], 1)
        self.assertEqual(parsed_issues[13]["outgoing_internal_links"], 2)
        self.assertEqual(parsed_issues[13]["depth"], 1)
        self.assertEqual(
            parsed_issues[14]["type"], "crawled_url_missing_from_sitemap"
        )
        self.assertEqual(
            parsed_issues[14]["sitemap_url"],
            "https://example.com/sitemap.xml",
        )
        self.assertEqual(parsed_issues[15]["type"], "missing_hsts")
        self.assertEqual(
            parsed_issues[15]["missing_header"], "Strict-Transport-Security"
        )
        self.assertEqual(parsed_issues[16]["type"], "missing_og_image")
        self.assertEqual(parsed_issues[16]["severity"], "warning")

        json_data = read_json(json_path)
        self.assertEqual(json_data[2]["seo_issues"], SAMPLE_PAGES[2]["seo_issues"])


class LinkExportTests(ExporterTestCase):
    def test_csv_contains_all_fields_and_rows(self):
        path = exporter.export_links_to_csv(SAMPLE_LINKS, self.folder / "links.csv")
        rows = read_csv_rows(path)

        self.assertEqual(len(rows), 3)
        self.assertEqual(list(rows[0].keys()), exporter.LINK_FIELDS)
        self.assertEqual(rows[1]["url"], "https://example.com/missing")
        self.assertEqual(rows[1]["found_on"], "https://example.com/")
        self.assertEqual(rows[1]["status_code"], "404")
        self.assertEqual(rows[1]["result"], "broken")
        self.assertEqual(rows[2]["status_code"], "")
        self.assertEqual(rows[2]["error"], "connection refused")

    def test_csv_empty_results_write_header_only(self):
        path = exporter.export_links_to_csv([], self.folder / "links.csv")

        self.assertEqual(read_csv_rows(path), [])
        header = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header, ",".join(exporter.LINK_FIELDS))

    def test_json_round_trip_preserves_data(self):
        path = exporter.export_links_to_json(SAMPLE_LINKS, self.folder / "links.json")

        self.assertEqual(read_json(path), SAMPLE_LINKS)

    def test_json_empty_results_write_empty_list(self):
        path = exporter.export_links_to_json([], self.folder / "links.json")

        self.assertEqual(read_json(path), [])


class FilePathTests(ExporterTestCase):
    def test_missing_parent_folder_is_created(self):
        target = self.folder / "exports" / "nested" / "pages.json"
        path = exporter.export_pages_to_json(SAMPLE_PAGES, target)

        self.assertTrue(path.exists())
        self.assertEqual(path, target)

    def test_accepts_string_paths(self):
        path = exporter.export_links_to_csv(SAMPLE_LINKS, str(self.folder / "links.csv"))

        self.assertTrue(path.exists())

    def test_existing_file_is_overwritten(self):
        target = self.folder / "pages.json"
        exporter.export_pages_to_json(SAMPLE_PAGES, target)
        exporter.export_pages_to_json([], target)

        self.assertEqual(read_json(target), [])


class CrawlerFieldCompatibilityTests(unittest.TestCase):
    def test_default_fields_match_crawler_result_keys(self):
        # Keep exporter field lists in sync with what crawler.py produces.
        source = Path(__file__).resolve().parent.parent.joinpath("crawler.py").read_text(
            encoding="utf-8"
        )

        for field in exporter.PAGE_FIELDS + exporter.LINK_FIELDS:
            self.assertIn(f'"{field}"', source)


if __name__ == "__main__":
    unittest.main()

