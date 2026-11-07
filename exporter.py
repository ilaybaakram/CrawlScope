import csv
import json
from pathlib import Path
from typing import Any

# Fields produced by crawl_website() for each page result.
PAGE_FIELDS = [
    "url",
    "depth",
    "status_code",
    "title",
    "meta_description",
    "h1",
    "total_links",
    "content_type",
    "seo_issues",
    "error",
]

# Fields produced by crawl_website() for each link check result.
LINK_FIELDS = [
    "url",
    "found_on",
    "status_code",
    "result",
    "error",
]


def _get_fieldnames(rows: list[dict[str, Any]], default_fields: list[str]) -> list[str]:
    """Return the default fields, followed by any extra keys found in the rows."""
    fieldnames = list(default_fields)
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def _prepare_file_path(file_path: str | Path) -> Path:
    """Convert to a Path and create the parent folder if it does not exist."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(
    rows: list[dict[str, Any]],
    file_path: str | Path,
    default_fields: list[str],
) -> Path:
    path = _prepare_file_path(file_path)
    fieldnames = _get_fieldnames(rows, default_fields)

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            serialized_row = {
                key: (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                )
                for key, value in row.items()
            }
            writer.writerow(serialized_row)

    return path


def _write_json(rows: list[dict[str, Any]], file_path: str | Path) -> Path:
    path = _prepare_file_path(file_path)

    with path.open("w", encoding="utf-8") as json_file:
        json.dump(rows, json_file, indent=2, ensure_ascii=False)
        json_file.write("\n")

    return path


def export_pages_to_csv(results: list[dict[str, Any]], file_path: str | Path) -> Path:
    """Write crawled page results to a CSV file. Empty results write only the header."""
    return _write_csv(results, file_path, PAGE_FIELDS)


def export_pages_to_json(results: list[dict[str, Any]], file_path: str | Path) -> Path:
    """Write crawled page results to a JSON file. Empty results write an empty list."""
    return _write_json(results, file_path)


def export_links_to_csv(link_results: list[dict[str, Any]], file_path: str | Path) -> Path:
    """Write link check results to a CSV file. Empty results write only the header."""
    return _write_csv(link_results, file_path, LINK_FIELDS)


def export_links_to_json(link_results: list[dict[str, Any]], file_path: str | Path) -> Path:
    """Write link check results to a JSON file. Empty results write an empty list."""
    return _write_json(link_results, file_path)

