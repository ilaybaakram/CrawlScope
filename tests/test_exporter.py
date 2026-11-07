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
        "content_type": "text/html; charset=utf-8",
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
        "content_type": "text/html",
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
        "content_type": "text/html",
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
