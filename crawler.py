import time
from collections import deque
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup


def crawl_website(
    start_url: str,
    max_pages: int = 10,
    delay_seconds: float = 1.0,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    clean_start_url, _ = urldefrag(start_url)
    queue = deque([(clean_start_url, 0)])
    queued_urls = {clean_start_url}
    visited = set()
    pages_crawled = 0
    results = []

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

            if soup.title and soup.title.string:
                page_title = soup.title.string.strip()
            else:
                page_title = "No title found"

            meta_tag = soup.find("meta", attrs={"name": "description"})
            meta_description = None
            if meta_tag and meta_tag.get("content"):
                meta_description = meta_tag.get("content").strip()

            h1_tag = soup.find("h1")
            h1_heading = None
            if h1_tag:
                h1_heading = h1_tag.get_text(strip=True) or None

            total_links = len(soup.find_all("a"))
            content_type = response.headers.get("content-type")

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

            if current_depth < max_depth:
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

                for next_url in discovered_urls:
                    if next_url not in visited and next_url not in queued_urls:
                        queued_urls.add(next_url)
                        queue.append((next_url, current_depth + 1))

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
                    "error": str(error),
                }
            )
            print(
                f"[{pages_crawled}/{max_pages}] (depth {current_depth}) "
                f"HTTP status error {error.response.status_code} "
                f"while fetching {current_url}: {error}"
            )

    successful_pages = sum(1 for item in results if item["error"] is None)
    print(
        f"\nCrawl complete. Total attempted requests: {len(results)} "
        f"| Successful pages: {successful_pages}"
    )

    return results


if __name__ == "__main__":
    starting_url = "https://en.wikipedia.org/wiki/Web_crawler"
    crawl_website(starting_url, max_pages=10, max_depth=2)
