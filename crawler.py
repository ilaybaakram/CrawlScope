import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from exporter import (
    export_links_to_csv,
    export_links_to_json,
    export_pages_to_csv,
    export_pages_to_json,
)


def check_link(url: str, headers: dict[str, str]) -> tuple[int | None, str | None]:
    """Return (status_code, error) for a link without following redirects."""
    try:
        response = httpx.head(url, headers=headers, timeout=10.0)
        if response.status_code in (405, 501):
            # Some servers do not support HEAD, so fall back to GET without
            # downloading the response body.
            with httpx.stream("GET", url, headers=headers, timeout=10.0) as get_response:
                return get_response.status_code, None
        return response.status_code, None
    except httpx.RequestError as error:
        return None, str(error)


def classify_link(status_code: int | None, error: str | None) -> str:
    if error is not None or status_code is None:
        return "error"
    if status_code >= 400:
        return "broken"
    return "working"


def is_html_content(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return "text/html" in lowered or "application/xhtml+xml" in lowered


def detect_seo_issues(
    url: str,
    title: str | None,
    meta_description: str | None,
    h1_heading: str | None,
    content_type: str | None,
) -> list[dict[str, str]]:
    if not is_html_content(content_type):
        return []

    issues: list[dict[str, str]] = []
    seen_types: set[str] = set()

    def add_issue(issue_type: str, message: str) -> None:
        if issue_type not in seen_types:
            seen_types.add(issue_type)
            issues.append(
                {
                    "type": issue_type,
                    "severity": "warning",
                    "message": message,
                    "url": url,
                }
            )

    if not title or not title.strip() or title.strip() == "No title found":
        add_issue(
            "missing_title",
            "Page is missing a <title> tag or the title is empty.",
        )

    if not meta_description or not meta_description.strip():
        add_issue(
            "missing_meta_description",
            "Page is missing a <meta name=\"description\"> tag or its content is empty.",
        )

    if not h1_heading or not h1_heading.strip():
        add_issue(
            "missing_h1",
            "Page is missing an <h1> heading or the heading is empty.",
        )

    return issues


def normalize_metadata_value(
    value: str | None,
    is_title: bool = False,
) -> str | None:
    if not value:
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if is_title and collapsed == "No title found":
        return None
    return collapsed.casefold()


def detect_duplicate_seo_issues(results: list[dict[str, Any]]) -> None:
    html_pages = [
        page
        for page in results
        if page.get("error") is None and is_html_content(page.get("content_type"))
    ]

    checks = [
        ("title", "duplicate_title", "Page title", True),
        (
            "meta_description",
            "duplicate_meta_description",
            "Meta description",
            False,
        ),
        ("h1", "duplicate_h1", "H1 heading", False),
    ]

    for field_name, issue_type, label, is_title in checks:
        groups: dict[str, list[dict[str, Any]]] = {}
        for page in html_pages:
            normalized = normalize_metadata_value(
                page.get(field_name),
                is_title=is_title,
            )
            if normalized is None:
                continue
            groups.setdefault(normalized, []).append(page)

        for matching_pages in groups.values():
            if len(matching_pages) < 2:
                continue

            affected_urls = [page["url"] for page in matching_pages]
            urls_text = ", ".join(affected_urls)

            for page in matching_pages:
                seo_issues = page.setdefault("seo_issues", [])
                existing_types = {issue["type"] for issue in seo_issues}
                if issue_type in existing_types:
                    continue

                raw_value = " ".join(str(page.get(field_name, "")).split())
                seo_issues.append(
                    {
                        "type": issue_type,
                        "severity": "warning",
                        "message": (
                            f"{label} '{raw_value}' is duplicated across "
                            f"pages: {urls_text}"
                        ),
                        "url": page["url"],
                    }
                )


def crawl_website(
    start_url: str,
    max_pages: int = 10,
    delay_seconds: float = 1.0,
    max_depth: int = 2,
    max_link_checks: int = 50,
    link_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    clean_start_url, _ = urldefrag(start_url)
    queue = deque([(clean_start_url, 0)])
    queued_urls = {clean_start_url}
    visited = set()
    pages_crawled = 0
    results = []
    if link_results is None:
        link_results = []
    checked_links = set()
    link_requests_made = 0

    parsed_start_url = urlparse(clean_start_url)
    target_hostname = parsed_start_url.hostname
    user_agent = "CrawlScope/0.1 (website auditing learning project)"
    headers = {"User-Agent": user_agent}

    robots_url = f"{parsed_start_url.scheme}://{target_hostname}/robots.txt"
    robot_parser = RobotFileParser()
    robot_parser.set_url(robots_url)

    try:
        robots_response = httpx.get(
            robots_url,
            headers=headers,
            timeout=10.0,
            follow_redirects=True,
        )
        if robots_response.status_code in (401, 403):
            robot_parser.disallow_all = True
        elif 400 <= robots_response.status_code < 500:
            robot_parser.allow_all = True
        elif 500 <= robots_response.status_code < 600:
            robot_parser.disallow_all = True
        else:
            robot_parser.parse(robots_response.text.splitlines())
    except httpx.RequestError as error:
        print(f"Could not fetch {robots_url}: {error}")
        return results

    while queue and pages_crawled < max_pages:
        current_url, current_depth = queue.popleft()

        if current_url in visited:
            continue

        visited.add(current_url)

        if not robot_parser.can_fetch(user_agent, current_url):
            print(f"Skipped (disallowed by robots.txt): {current_url}")
            continue

        if pages_crawled > 0 and delay_seconds > 0:
            time.sleep(delay_seconds)

        pages_crawled += 1

        try:
            response = httpx.get(current_url, headers=headers, timeout=10.0)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            raw_title = (
                soup.title.string.strip()
                if (soup.title and soup.title.string)
                else ""
            )
            if raw_title:
                page_title = raw_title
            else:
                page_title = "No title found"

            meta_tag = soup.find("meta", attrs={"name": "description"})
            meta_description = None
            if meta_tag and meta_tag.get("content"):
                meta_description = meta_tag.get("content").strip() or None

            h1_tag = soup.find("h1")
            h1_heading = None
            if h1_tag:
                h1_heading = h1_tag.get_text(strip=True) or None

            total_links = len(soup.find_all("a"))
            content_type = response.headers.get("content-type")
            seo_issues = detect_seo_issues(
                url=current_url,
                title=page_title,
                meta_description=meta_description,
                h1_heading=h1_heading,
                content_type=content_type,
            )

            results.append(
                {
                    "url": current_url,
                    "depth": current_depth,
                    "status_code": response.status_code,
                    "title": page_title,
                    "meta_description": meta_description,
                    "h1": h1_heading,
                    "total_links": total_links,
                    "content_type": content_type,
                    "seo_issues": seo_issues,
                    "error": None,
                }
            )

            print(
                f"[{pages_crawled}/{max_pages}] (depth {current_depth}) "
                f"{current_url} | Status: {response.status_code} "
                f"| Title: {page_title}"
            )
            print(f"    Meta Description: {meta_description}")
            print(f"    H1: {h1_heading}")
            print(f"    Total Links: {total_links}")
            print(f"    Content-Type: {content_type}")

            anchor_tags = soup.find_all("a")
            discovered_urls = set()

            for tag in anchor_tags:
                href = tag.get("href")
                if href is None:
                    continue

                absolute_url = urljoin(current_url, href)
                clean_url, _ = urldefrag(absolute_url)
                parsed_url = urlparse(clean_url)

                if parsed_url.scheme not in ("http", "https"):
                    continue

                if parsed_url.hostname != target_hostname:
                    continue

                discovered_urls.add(clean_url)

            if current_depth < max_depth:
                for next_url in discovered_urls:
                    if next_url not in visited and next_url not in queued_urls:
                        queued_urls.add(next_url)
                        queue.append((next_url, current_depth + 1))

            # Link checking is separate from crawling: it never adds pages to
            # the queue and never counts toward max_pages.
            crawled_pages = {item["url"]: item for item in results}

            for link_url in sorted(discovered_urls):
                if link_url in checked_links:
                    continue

                if link_url in crawled_pages:
                    status_code = crawled_pages[link_url]["status_code"]
                    error = crawled_pages[link_url]["error"] if status_code is None else None
                elif not robot_parser.can_fetch(user_agent, link_url):
                    checked_links.add(link_url)
                    link_results.append(
                        {
                            "url": link_url,
                            "found_on": current_url,
                            "status_code": None,
                            "result": "skipped",
                            "error": "Disallowed by robots.txt",
                        }
                    )
                    print(f"    Link skipped (disallowed by robots.txt): {link_url}")
                    continue
                else:
                    if link_requests_made >= max_link_checks:
                        break
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                    link_requests_made += 1
                    status_code, error = check_link(link_url, headers)

                checked_links.add(link_url)
                link_result = classify_link(status_code, error)
                link_results.append(
                    {
                        "url": link_url,
                        "found_on": current_url,
                        "status_code": status_code,
                        "result": link_result,
                        "error": error,
                    }
                )
                print(
                    f"    Link {link_result.upper()}: {link_url} "
                    f"| Status: {status_code}"
                )

        except httpx.RequestError as error:
            results.append(
                {
                    "url": current_url,
                    "depth": current_depth,
                    "status_code": None,
                    "title": None,
                    "meta_description": None,
                    "h1": None,
                    "total_links": 0,
                    "content_type": None,
                    "seo_issues": [],
                    "error": str(error),
                }
            )
            print(
                f"[{pages_crawled}/{max_pages}] (depth {current_depth}) "
                f"Network/request error while fetching {current_url}: {error}"
            )

        except httpx.HTTPStatusError as error:
            results.append(
                {
                    "url": current_url,
                    "depth": current_depth,
                    "status_code": error.response.status_code,
                    "title": None,
                    "meta_description": None,
                    "h1": None,
                    "total_links": 0,
                    "content_type": error.response.headers.get("content-type"),
                    "seo_issues": [],
                    "error": str(error),
                }
            )
            print(
                f"[{pages_crawled}/{max_pages}] (depth {current_depth}) "
                f"HTTP status error {error.response.status_code} "
                f"while fetching {current_url}: {error}"
            )

    detect_duplicate_seo_issues(results)

    successful_pages = sum(1 for item in results if item["error"] is None)
    print(
        f"\nCrawl complete. Total attempted requests: {len(results)} "
        f"| Successful pages: {successful_pages}"
    )
    broken_links = sum(1 for item in link_results if item["result"] == "broken")
    print(
        f"Links checked: {len(link_results)} | Broken links: {broken_links}"
    )

    return results


def run_crawl_and_export(
    start_url: str,
    output_dir: str | Path = "output",
    **crawl_options: Any,
) -> dict[str, Path]:
    """Crawl a website, then save page and link results to CSV and JSON files."""
    link_results: list[dict[str, Any]] = []
    page_results = crawl_website(
        start_url, link_results=link_results, **crawl_options
    )

    output_path = Path(output_dir)
    exported_files = {
        "pages_csv": export_pages_to_csv(page_results, output_path / "crawl_results.csv"),
        "pages_json": export_pages_to_json(page_results, output_path / "crawl_results.json"),
        "links_csv": export_links_to_csv(link_results, output_path / "link_results.csv"),
        "links_json": export_links_to_json(link_results, output_path / "link_results.json"),
    }

    print("\nExported files:")
    for file_path in exported_files.values():
        print(f"    {file_path}")

    return exported_files


if __name__ == "__main__":
    starting_url = "https://en.wikipedia.org/wiki/Web_crawler"
    run_crawl_and_export(starting_url, max_pages=10, max_depth=2)
