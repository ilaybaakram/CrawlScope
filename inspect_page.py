from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


def inspect_webpage(url: str) -> None:
    try:
        headers = {
            "User-Agent": "CrawlScope/0.1 (website auditing learning project)"
        }
        response = httpx.get(url, headers=headers, timeout=10.0)
        print(f"Status Code: {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")

        if soup.title and soup.title.string:
            page_title = soup.title.string.strip()
        else:
            page_title = "No title found"

        print(f"Page Title: {page_title}")

        anchor_tags = soup.find_all("a")
        print(f"Total <a> tags found: {len(anchor_tags)}")

        target_hostname = urlparse(url).hostname
        unique_urls = set()

        for tag in anchor_tags:
            href = tag.get("href")
            if href is None:
                continue

            absolute_url = urljoin(url, href)
            clean_url, _ = urldefrag(absolute_url)
            parsed_url = urlparse(clean_url)

            if parsed_url.scheme not in ("http", "https"):
                continue

            if parsed_url.hostname != target_hostname:
                continue

            unique_urls.add(clean_url)

        print(f"Unique accepted URLs found: {len(unique_urls)}")
        for accepted_url in unique_urls:
            print(f"Accepted URL: {accepted_url}")

    except httpx.RequestError as error:
        print(f"Request failed while trying to reach {url}: {error}")


if __name__ == "__main__":
    target_url = "https://www.wikipedia.org"
    inspect_webpage(target_url)
