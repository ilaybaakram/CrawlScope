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


def _find_canonical_link_tags(soup: BeautifulSoup) -> list[Any]:
    canonical_tags: list[Any] = []
    for tag in soup.find_all("link"):
        rel_attr = tag.get("rel")
        if isinstance(rel_attr, list):
            rel_values = [str(item).strip().lower() for item in rel_attr]
        elif isinstance(rel_attr, str):
            rel_values = [item.strip().lower() for item in rel_attr.split()]
        else:
            rel_values = []
        if "canonical" in rel_values:
            canonical_tags.append(tag)
    return canonical_tags


def _resolve_canonical_url(
    page_url: str,
    raw_href: Any,
) -> tuple[str | None, bool]:
    if raw_href is None:
        return None, False

    href_text = str(raw_href).strip()
    if not href_text:
        return None, False

    if any(char.isspace() for char in href_text):
        return href_text, False

    if "://" in href_text:
        scheme_part, _, _ = href_text.partition("://")
        if not scheme_part or scheme_part.lower() not in ("http", "https"):
            return href_text, False

    try:
        parsed_raw = urlparse(href_text)
        if parsed_raw.scheme and parsed_raw.scheme.lower() not in (
            "http",
            "https",
        ):
            return href_text, False
        if (
            parsed_raw.scheme.lower() in ("http", "https")
            and not parsed_raw.hostname
        ):
            return href_text, False
        if href_text.startswith("//") and not parsed_raw.hostname:
            return href_text, False

        resolved = urljoin(page_url, href_text)
        clean_resolved, _ = urldefrag(resolved)
        parsed = urlparse(clean_resolved)
        _ = parsed.port
    except ValueError:
        return href_text, False

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return clean_resolved or href_text, False

    return clean_resolved, True


def detect_canonical_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
    headers: dict[str, str] | None = None,
    checked_redirects: dict[str, dict[str, Any] | None] | None = None,
    initial_link_hops: dict[str, tuple[int | None, str | None, str | None]]
    | None = None,
    crawled_pages: dict[str, dict[str, Any]] | None = None,
    max_redirect_checks: int = 50,
    delay_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    canonical_tags = _find_canonical_link_tags(soup)

    if not canonical_tags:
        return [
            {
                "type": "missing_canonical",
                "severity": "warning",
                "message": 'Page is missing a <link rel="canonical"> tag.',
                "url": url,
                "canonical_url": None,
            }
        ]

    if len(canonical_tags) > 1:
        discovered_urls: list[str] = []
        first_canonical_url: str | None = None
        for tag in canonical_tags:
            resolved_url, is_valid = _resolve_canonical_url(
                url, tag.get("href")
            )
            if resolved_url:
                discovered_urls.append(resolved_url)
                if first_canonical_url is None and is_valid:
                    first_canonical_url = resolved_url
                elif first_canonical_url is None:
                    first_canonical_url = resolved_url
            else:
                discovered_urls.append("(empty)")

        return [
            {
                "type": "multiple_canonicals",
                "severity": "warning",
                "message": (
                    f"Page has multiple canonical link tags "
                    f"({len(canonical_tags)}): {', '.join(discovered_urls)}"
                ),
                "url": url,
                "canonical_url": first_canonical_url,
            }
        ]

    raw_href = canonical_tags[0].get("href")
    canonical_url, is_valid = _resolve_canonical_url(url, raw_href)

    if not is_valid or canonical_url is None:
        if canonical_url is None:
            message = (
                "Canonical link tag has an empty or missing href attribute."
            )
        else:
            message = (
                f"Invalid canonical URL '{canonical_url}': "
                "must resolve to a valid HTTP or HTTPS URL."
            )
        return [
            {
                "type": "invalid_canonical",
                "severity": "warning",
                "message": message,
                "url": url,
                "canonical_url": canonical_url,
            }
        ]

    issues: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    page_hostname = (urlparse(url).hostname or "").casefold()
    canonical_hostname = (urlparse(canonical_url).hostname or "").casefold()

    if canonical_hostname != page_hostname:
        seen_types.add("cross_domain_canonical")
        issues.append(
            {
                "type": "cross_domain_canonical",
                "severity": "info",
                "message": (
                    f"Canonical URL points to a different domain "
                    f"({canonical_hostname} vs {page_hostname}): {canonical_url}"
                ),
                "url": url,
                "canonical_url": canonical_url,
            }
        )

    if headers is not None and checked_redirects is not None:
        already_crawled_non_redirect = (
            canonical_url == url
            or (
                crawled_pages is not None
                and canonical_url in crawled_pages
                and crawled_pages[canonical_url].get("status_code") is not None
                and not (
                    300 <= crawled_pages[canonical_url]["status_code"] < 400
                )
            )
        )

        redirect_finding: dict[str, Any] | None = None
        if not already_crawled_non_redirect:
            if canonical_url in checked_redirects:
                redirect_finding = checked_redirects[canonical_url]
            elif len(checked_redirects) < max_redirect_checks:
                initial_hop = (
                    initial_link_hops.get(canonical_url)
                    if initial_link_hops is not None
                    else None
                )
                if initial_hop is None and delay_seconds > 0:
                    time.sleep(delay_seconds)
                redirect_finding = trace_redirect_chain(
                    source_url=canonical_url,
                    headers=headers,
                    delay_seconds=delay_seconds,
                    initial_hop=initial_hop,
                )
                checked_redirects[canonical_url] = redirect_finding

        if (
            redirect_finding is not None
            and "canonical_redirect" not in seen_types
        ):
            seen_types.add("canonical_redirect")
            destination_url = redirect_finding.get("destination_url")
            if destination_url:
                redirect_message = (
                    f"Canonical URL redirects from {canonical_url} "
                    f"to {destination_url}"
                )
            else:
                chain_str = " -> ".join(
                    redirect_finding.get("redirect_chain", [canonical_url])
                )
                redirect_message = (
                    f"Canonical URL {canonical_url} redirects in a loop: "
                    f"{chain_str}"
                )

            issues.append(
                {
                    "type": "canonical_redirect",
                    "severity": "warning",
                    "message": redirect_message,
                    "url": url,
                    "canonical_url": canonical_url,
                    "destination_url": destination_url,
                    "redirect_chain": list(
                        redirect_finding.get("redirect_chain", [])
                    ),
                    "status_code": redirect_finding.get("status_code"),
                }
            )

    return issues


def _find_meta_robots_tags(soup: BeautifulSoup) -> list[tuple[str, Any]]:
    robots_tags: list[tuple[str, Any]] = []
    for tag in soup.find_all("meta"):
        raw_name = tag.get("name")
        if not isinstance(raw_name, str):
            continue
        bot_name = raw_name.strip().lower()
        if bot_name in ("robots", "googlebot"):
            robots_tags.append((bot_name, tag))
    return robots_tags


def _extract_x_robots_headers(response_headers: Any) -> list[str]:
    if response_headers is None:
        return []

    values: list[str] = []
    if hasattr(response_headers, "get_list"):
        raw_list = response_headers.get_list("x-robots-tag")
        for item in raw_list:
            if item is not None and str(item).strip():
                values.append(str(item).strip())
        return values

    if hasattr(response_headers, "items"):
        for key, value in response_headers.items():
            if str(key).strip().lower() != "x-robots-tag" or value is None:
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    if item is not None and str(item).strip():
                        values.append(str(item).strip())
            elif str(value).strip():
                values.append(str(value).strip())

    return values


def _parse_robots_directives(raw_value: str) -> set[str]:
    parameterized_prefixes = {
        "max-snippet",
        "max-image-preview",
        "max-video-preview",
        "unavailable_after",
    }
    directives: set[str] = set()

    for segment in raw_value.split(","):
        token = segment.strip().lower()
        if not token:
            continue

        if ":" in token:
            prefix, _, remainder = token.partition(":")
            prefix = prefix.strip()
            remainder = remainder.strip()
            if prefix not in parameterized_prefixes and remainder:
                token = remainder

        for part in token.split():
            if part == "none":
                directives.add("none")
                directives.add("noindex")
                directives.add("nofollow")
            elif part == "all":
                directives.add("all")
                directives.add("index")
                directives.add("follow")
            else:
                directives.add(part)

    return directives


def detect_robots_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
    response_headers: Any = None,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    issues: list[dict[str, Any]] = []
    meta_robots_tags = _find_meta_robots_tags(soup)
    x_robots_values = _extract_x_robots_headers(response_headers)

    tags_by_bot: dict[str, list[Any]] = {}
    for bot_name, tag in meta_robots_tags:
        tags_by_bot.setdefault(bot_name, []).append(tag)

    for bot_name, tags in tags_by_bot.items():
        if len(tags) > 1:
            tag_values: list[str] = []
            for tag in tags:
                raw_content = tag.get("content")
                cleaned = (
                    str(raw_content).strip() if raw_content is not None else ""
                )
                tag_values.append(cleaned if cleaned else "(empty)")
            combined_values = " | ".join(tag_values)
            issues.append(
                {
                    "type": "multiple_meta_robots",
                    "severity": "warning",
                    "message": (
                        f'Page has multiple <meta name="{bot_name}"> tags '
                        f"({len(tags)}): {combined_values}"
                    ),
                    "url": url,
                    "robots_directive": combined_values,
                    "directive_source": f"meta_{bot_name}",
                }
            )

    directive_entries: list[tuple[str, str, set[str]]] = []
    seen_empty_sources: set[str] = set()

    for bot_name, tag in meta_robots_tags:
        source_name = f"meta_{bot_name}"
        raw_content = tag.get("content")
        cleaned_content = (
            str(raw_content).strip() if raw_content is not None else ""
        )
        if not cleaned_content:
            if source_name not in seen_empty_sources:
                seen_empty_sources.add(source_name)
                issues.append(
                    {
                        "type": "empty_meta_robots",
                        "severity": "warning",
                        "message": (
                            f'Page has a <meta name="{bot_name}"> tag with '
                            "an empty or missing content attribute."
                        ),
                        "url": url,
                        "robots_directive": None,
                        "directive_source": source_name,
                    }
                )
            continue

        parsed_tokens = _parse_robots_directives(cleaned_content)
        directive_entries.append((source_name, cleaned_content, parsed_tokens))

    for header_value in x_robots_values:
        parsed_tokens = _parse_robots_directives(header_value)
        directive_entries.append(("x_robots_tag", header_value, parsed_tokens))

    if not directive_entries:
        return issues

    all_tokens: set[str] = set()
    for _, _, tokens in directive_entries:
        all_tokens.update(tokens)

    conflicts: list[str] = []
    if "index" in all_tokens and "noindex" in all_tokens:
        conflicts.append("index vs noindex")
    if "follow" in all_tokens and "nofollow" in all_tokens:
        conflicts.append("follow vs nofollow")

    if conflicts:
        combined_directives = " | ".join(
            raw_val for _, raw_val, _ in directive_entries
        )
        unique_sources = list(
            dict.fromkeys(src for src, _, _ in directive_entries)
        )
        combined_source = ", ".join(unique_sources)
        issues.append(
            {
                "type": "conflicting_robots_directives",
                "severity": "warning",
                "message": (
                    f"Page has conflicting robots directives "
                    f"({', '.join(conflicts)}): {combined_directives}"
                ),
                "url": url,
                "robots_directive": combined_directives,
                "directive_source": combined_source,
            }
        )

    if "noindex" in all_tokens:
        noindex_entries = [
            (src, raw_val)
            for src, raw_val, tokens in directive_entries
            if "noindex" in tokens
        ]
        noindex_directive = " | ".join(raw_val for _, raw_val in noindex_entries)
        noindex_sources = list(
            dict.fromkeys(src for src, _ in noindex_entries)
        )
        noindex_source = ", ".join(noindex_sources)
        issues.append(
            {
                "type": "noindex_page",
                "severity": "warning",
                "message": (
                    f"Page blocks search engine indexing via {noindex_source}: "
                    f"{noindex_directive}"
                ),
                "url": url,
                "robots_directive": noindex_directive,
                "directive_source": noindex_source,
            }
        )

    if "nofollow" in all_tokens:
        nofollow_entries = [
            (src, raw_val)
            for src, raw_val, tokens in directive_entries
            if "nofollow" in tokens
        ]
        nofollow_directive = " | ".join(
            raw_val for _, raw_val in nofollow_entries
        )
        nofollow_sources = list(
            dict.fromkeys(src for src, _ in nofollow_entries)
        )
        nofollow_source = ", ".join(nofollow_sources)
        issues.append(
            {
                "type": "nofollow_page",
                "severity": "warning",
                "message": (
                    f"Page prevents search engines from following links via "
                    f"{nofollow_source}: {nofollow_directive}"
                ),
                "url": url,
                "robots_directive": nofollow_directive,
                "directive_source": nofollow_source,
            }
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

            seo_issues.extend(
                detect_canonical_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                    headers=headers,
                    checked_redirects=checked_redirects,
                    initial_link_hops=initial_link_hops,
                    crawled_pages=crawled_pages,
                    max_redirect_checks=max_redirect_checks,
                    delay_seconds=delay_seconds,
                )
            )
            seo_issues.extend(
                detect_robots_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                    response_headers=response.headers,
                )
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
