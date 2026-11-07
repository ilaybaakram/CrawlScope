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


def _request_link_hop(
    url: str,
    headers: dict[str, str],
) -> tuple[int | None, str | None, str | None]:
    """Return (status_code, location, error) for a single URL hop without following redirects."""
    try:
        response = httpx.head(url, headers=headers, timeout=10.0)
        if response.status_code in (405, 501):
            # Some servers do not support HEAD, so fall back to GET without
            # downloading the response body.
            with httpx.stream("GET", url, headers=headers, timeout=10.0) as get_response:
                response_headers = getattr(get_response, "headers", {})
                return (
                    get_response.status_code,
                    response_headers.get("location"),
                    None,
                )
        return response.status_code, response.headers.get("location"), None
    except httpx.RequestError as error:
        return None, None, str(error)


def check_link(url: str, headers: dict[str, str]) -> tuple[int | None, str | None]:
    """Return (status_code, error) for a link without following redirects."""
    status_code, _, error = _request_link_hop(url, headers)
    return status_code, error


def classify_link(status_code: int | None, error: str | None) -> str:
    if error is not None or status_code is None:
        return "error"
    if status_code >= 400:
        return "broken"
    return "working"


def check_image(
    image_url: str,
    headers: dict[str, str],
) -> tuple[int | None, str | None]:
    """Return (status_code, error) for an image URL."""
    try:
        response = httpx.get(
            image_url,
            headers=headers,
            timeout=10.0,
            follow_redirects=True,
        )
        return response.status_code, None
    except httpx.RequestError as error:
        return None, str(error)


def classify_image(status_code: int | None, error: str | None) -> str:
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


def detect_url_seo_issues(
    url: str,
    max_url_length: int = 100,
) -> list[dict[str, str]]:
    parsed = urlparse(url)
    path = parsed.path or ""

    issues: list[dict[str, str]] = []
    seen_types: set[str] = set()

    def add_issue(issue_type: str, severity: str, message: str) -> None:
        if issue_type not in seen_types:
            seen_types.add(issue_type)
            issues.append(
                {
                    "type": issue_type,
                    "severity": severity,
                    "message": message,
                    "url": url,
                }
            )

    if any(char.isupper() for char in path):
        add_issue(
            "uppercase_url",
            "warning",
            f"URL path contains uppercase characters: {path}",
        )

    if "_" in path:
        add_issue(
            "underscore_url",
            "warning",
            f"URL path contains underscores instead of hyphens: {path}",
        )

    if len(url) > max_url_length:
        add_issue(
            "long_url",
            "warning",
            f"URL length ({len(url)} characters) exceeds the {max_url_length}-character threshold.",
        )

    if parsed.query:
        add_issue(
            "query_parameter_url",
            "info",
            f"URL contains query parameters (?{parsed.query}).",
        )

    return issues


def detect_image_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
) -> list[dict[str, str]]:
    if not is_html_content(content_type):
        return []

    image_issues: list[dict[str, str]] = []
    seen_image_issues: set[tuple[str, str]] = set()

    for img_tag in soup.find_all("img"):
        raw_src = img_tag.get("src")
        if raw_src is None or not str(raw_src).strip():
            continue

        resolved_image_url = urljoin(url, str(raw_src).strip())
        alt_value = img_tag.get("alt")

        if alt_value is None:
            issue_type = "missing_image_alt"
            message = f"Image is missing an alt attribute: {resolved_image_url}"
        elif not str(alt_value).strip():
            issue_type = "empty_image_alt"
            message = f"Image has an empty alt attribute: {resolved_image_url}"
        else:
            continue

        dedup_key = (issue_type, resolved_image_url)
        if dedup_key in seen_image_issues:
            continue
        seen_image_issues.add(dedup_key)

        image_issues.append(
            {
                "type": issue_type,
                "severity": "warning",
                "message": message,
                "url": url,
                "image_url": resolved_image_url,
            }
        )

    return image_issues


def detect_broken_image_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
    headers: dict[str, str],
    checked_images: dict[str, tuple[int | None, str | None]],
    max_image_checks: int = 50,
    delay_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    broken_issues: list[dict[str, Any]] = []
    seen_on_page: set[str] = set()

    for img_tag in soup.find_all("img"):
        raw_src = img_tag.get("src")
        if raw_src is None or not str(raw_src).strip():
            continue

        resolved_url = urljoin(url, str(raw_src).strip())
        clean_image_url, _ = urldefrag(resolved_url)
        parsed_image_url = urlparse(clean_image_url)

        if parsed_image_url.scheme not in ("http", "https"):
            continue

        if clean_image_url in seen_on_page:
            continue
        seen_on_page.add(clean_image_url)

        if clean_image_url in checked_images:
            status_code, error = checked_images[clean_image_url]
        else:
            if len(checked_images) >= max_image_checks:
                continue
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            status_code, error = check_image(clean_image_url, headers)
            checked_images[clean_image_url] = (status_code, error)

        if classify_image(status_code, error) == "broken":
            broken_issues.append(
                {
                    "type": "broken_image",
                    "severity": "warning",
                    "message": (
                        f"Broken image (HTTP {status_code}): {clean_image_url}"
                    ),
                    "url": url,
                    "image_url": clean_image_url,
                    "status_code": status_code,
                }
            )

    return broken_issues


def trace_redirect_chain(
    source_url: str,
    headers: dict[str, str],
    delay_seconds: float = 1.0,
    max_hops: int = 10,
    initial_hop: tuple[int | None, str | None, str | None] | None = None,
) -> dict[str, Any] | None:
    if initial_hop is not None:
        status_code, location, error = initial_hop
    else:
        status_code, location, error = _request_link_hop(source_url, headers)

    if error is not None or status_code is None:
        return None

    if not (300 <= status_code < 400) or not location or not location.strip():
        return None

    first_status_code = status_code
    chain: list[str] = [source_url]
    visited_in_chain: set[str] = {source_url}
    current_hop_url = source_url
    next_location = location.strip()
    final_status_code: int | None = status_code

    while len(chain) <= max_hops:
        resolved_next = urljoin(current_hop_url, next_location)
        clean_next_url, _ = urldefrag(resolved_next)
        parsed_next = urlparse(clean_next_url)

        if parsed_next.scheme not in ("http", "https"):
            break

        chain.append(clean_next_url)

        if clean_next_url in visited_in_chain:
            chain_text = " -> ".join(chain)
            return {
                "type": "redirect_loop",
                "severity": "error",
                "message": f"Redirect loop detected: {chain_text}",
                "source_url": source_url,
                "destination_url": None,
                "redirect_chain": chain,
                "status_code": None,
            }

        visited_in_chain.add(clean_next_url)

        if delay_seconds > 0:
            time.sleep(delay_seconds)

        hop_status, hop_location, hop_error = _request_link_hop(
            clean_next_url, headers
        )
        if hop_error is not None or hop_status is None:
            final_status_code = None
            break

        final_status_code = hop_status
        if 300 <= hop_status < 400 and hop_location and hop_location.strip():
            current_hop_url = clean_next_url
            next_location = hop_location.strip()
            continue

        break

    if len(chain) < 2:
        return None

    destination_url = chain[-1]
    chain_text = " -> ".join(chain)
    status_suffix = (
        f"final HTTP {final_status_code}"
        if final_status_code is not None
        else "final request failed"
    )

    if len(chain) > 2:
        return {
            "type": "redirect_chain",
            "severity": "warning",
            "message": (
                f"Redirect chain ({len(chain) - 1} hops, {status_suffix}): "
                f"{chain_text}"
            ),
            "source_url": source_url,
            "destination_url": destination_url,
            "redirect_chain": chain,
            "status_code": final_status_code,
        }

    return {
        "type": "redirect_url",
        "severity": "info",
        "message": (
            f"Redirecting URL (HTTP {first_status_code}, {status_suffix}): "
            f"{chain_text}"
        ),
        "source_url": source_url,
        "destination_url": destination_url,
        "redirect_chain": chain,
        "status_code": final_status_code,
    }


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
    max_image_checks: int = 50,
    max_url_length: int = 100,
    max_redirect_checks: int = 50,
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
    checked_images: dict[str, tuple[int | None, str | None]] = {}
    initial_link_hops: dict[str, tuple[int | None, str | None, str | None]] = {}
    checked_redirects: dict[str, dict[str, Any] | None] = {}

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
            seo_issues.extend(
                detect_url_seo_issues(
                    url=current_url,
                    max_url_length=max_url_length,
                )
            )
            seo_issues.extend(
                detect_image_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                )
            )
            seo_issues.extend(
                detect_broken_image_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                    headers=headers,
                    checked_images=checked_images,
                    max_image_checks=max_image_checks,
                    delay_seconds=delay_seconds,
                )
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
                    hop_status, hop_location, hop_error = _request_link_hop(
                        link_url, headers
                    )
                    initial_link_hops[link_url] = (
                        hop_status,
                        hop_location,
                        hop_error,
                    )
                    status_code, error = hop_status, hop_error

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

            seen_redirect_sources: set[str] = set()
            for link_url in sorted(discovered_urls):
                if link_url in seen_redirect_sources:
                    continue
                seen_redirect_sources.add(link_url)

                if not robot_parser.can_fetch(user_agent, link_url):
                    continue

                if link_url in checked_redirects:
                    redirect_finding = checked_redirects[link_url]
                else:
                    if len(checked_redirects) >= max_redirect_checks:
                        continue
                    if link_url in crawled_pages:
                        checked_redirects[link_url] = None
                        continue
                    if link_url not in initial_link_hops:
                        continue

                    redirect_finding = trace_redirect_chain(
                        source_url=link_url,
                        headers=headers,
                        delay_seconds=delay_seconds,
                        initial_hop=initial_link_hops[link_url],
                    )
                    checked_redirects[link_url] = redirect_finding

                if redirect_finding is not None:
                    seo_issues.append(
                        {
                            "type": redirect_finding["type"],
                            "severity": redirect_finding["severity"],
                            "message": redirect_finding["message"],
                            "url": current_url,
                            "source_url": redirect_finding["source_url"],
                            "destination_url": redirect_finding["destination_url"],
                            "redirect_chain": list(
                                redirect_finding["redirect_chain"]
                            ),
                            "status_code": redirect_finding["status_code"],
                        }
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
