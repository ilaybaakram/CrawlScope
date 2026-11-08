import ipaddress
import json
import re
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from bs4.element import (
    CData,
    Comment,
    Declaration,
    NavigableString,
    ProcessingInstruction,
)

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


def _find_json_ld_script_tags(soup: BeautifulSoup) -> list[Any]:
    script_tags: list[Any] = []
    for tag in soup.find_all("script"):
        raw_type = tag.get("type")
        if not isinstance(raw_type, str):
            continue
        mime_type = raw_type.split(";")[0].strip().lower()
        if mime_type == "application/ld+json":
            script_tags.append(tag)
    return script_tags


def _extract_and_validate_schema_type(
    raw_type: Any,
) -> tuple[list[str], str | None]:
    if raw_type is None:
        return [], "missing"

    if isinstance(raw_type, str):
        cleaned = raw_type.strip()
        if not cleaned:
            return [], "missing"
        return [cleaned], None

    if isinstance(raw_type, list):
        if not raw_type:
            return [], "missing"
        cleaned_types: list[str] = []
        for item in raw_type:
            if not isinstance(item, str):
                return [], "invalid_structure"
            cleaned_item = item.strip()
            if not cleaned_item:
                return [], "missing"
            cleaned_types.append(cleaned_item)
        return cleaned_types, None

    return [], "invalid_structure"


def _normalize_schema_type_name(type_name: str) -> str:
    cleaned = type_name.strip()
    for prefix in ("https://schema.org/", "http://schema.org/"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    return cleaned.casefold()


def detect_structured_data_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    script_tags = _find_json_ld_script_tags(soup)
    if not script_tags:
        return [
            {
                "type": "missing_structured_data",
                "severity": "warning",
                "message": (
                    'Page is missing JSON-LD structured data '
                    '(<script type="application/ld+json">).'
                ),
                "url": url,
                "schema_types": [],
            }
        ]

    issues: list[dict[str, Any]] = []
    seen_issue_types: set[str] = set()
    all_discovered_types: list[str] = []
    top_level_type_counts: dict[str, int] = {}
    top_level_type_labels: dict[str, str] = {}

    def add_issue(
        issue_type: str,
        message: str,
        schema_types: list[str] | None = None,
    ) -> None:
        if issue_type in seen_issue_types:
            return
        seen_issue_types.add(issue_type)
        issues.append(
            {
                "type": issue_type,
                "severity": "warning",
                "message": message,
                "url": url,
                "schema_types": list(schema_types)
                if schema_types is not None
                else [],
            }
        )

    def record_schema_types(
        valid_types: list[str],
        is_top_level: bool,
    ) -> None:
        for schema_type in valid_types:
            if schema_type not in all_discovered_types:
                all_discovered_types.append(schema_type)

        if is_top_level:
            seen_in_entity: set[str] = set()
            for schema_type in valid_types:
                normalized = _normalize_schema_type_name(schema_type)
                if not normalized or normalized in seen_in_entity:
                    continue
                seen_in_entity.add(normalized)
                top_level_type_counts[normalized] = (
                    top_level_type_counts.get(normalized, 0) + 1
                )
                top_level_type_labels.setdefault(normalized, schema_type)

    def inspect_object(
        obj: dict[str, Any],
        block_index: int,
        is_top_level: bool,
    ) -> None:
        if not obj:
            add_issue(
                "invalid_schema_structure",
                (
                    f"JSON-LD script block #{block_index} contains an empty "
                    "JSON object."
                ),
            )
            return

        if "@graph" in obj:
            graph_value = obj["@graph"]
            if not isinstance(graph_value, list) or len(graph_value) == 0:
                add_issue(
                    "invalid_schema_structure",
                    (
                        f"JSON-LD script block #{block_index} has an invalid "
                        "'@graph' structure (must be a non-empty array of "
                        "JSON objects)."
                    ),
                )
                return

            if any(
                not isinstance(item, dict) or not item for item in graph_value
            ):
                add_issue(
                    "invalid_schema_structure",
                    (
                        f"JSON-LD script block #{block_index} '@graph' array "
                        "must contain only non-empty JSON objects."
                    ),
                )
                return

            if "@type" in obj:
                valid_types, err_kind = _extract_and_validate_schema_type(
                    obj.get("@type")
                )
                if err_kind == "missing":
                    add_issue(
                        "missing_schema_type",
                        (
                            f"JSON-LD object in script block #{block_index} "
                            "has an empty '@type' property."
                        ),
                    )
                elif err_kind == "invalid_structure":
                    add_issue(
                        "invalid_schema_structure",
                        (
                            f"JSON-LD object in script block #{block_index} "
                            "has an invalid '@type' value (must be a string "
                            "or array of strings)."
                        ),
                    )
                else:
                    record_schema_types(valid_types, is_top_level=True)

            for graph_item in graph_value:
                inspect_object(
                    graph_item,
                    block_index=block_index,
                    is_top_level=True,
                )
            return

        if not is_top_level and "@type" not in obj:
            if "@value" in obj or set(obj.keys()) == {"@id"}:
                return

        if "@type" not in obj:
            add_issue(
                "missing_schema_type",
                (
                    f"JSON-LD object in script block #{block_index} is "
                    "missing an '@type' property."
                ),
            )
        else:
            valid_types, err_kind = _extract_and_validate_schema_type(
                obj.get("@type")
            )
            if err_kind == "missing":
                add_issue(
                    "missing_schema_type",
                    (
                        f"JSON-LD object in script block #{block_index} has "
                        "an empty '@type' property."
                    ),
                )
            elif err_kind == "invalid_structure":
                add_issue(
                    "invalid_schema_structure",
                    (
                        f"JSON-LD object in script block #{block_index} has "
                        "an invalid '@type' value (must be a string or array "
                        "of strings)."
                    ),
                )
            else:
                record_schema_types(valid_types, is_top_level=is_top_level)

        for key, value in obj.items():
            if str(key).startswith("@"):
                continue
            if isinstance(value, dict):
                inspect_object(
                    value,
                    block_index=block_index,
                    is_top_level=False,
                )
            elif isinstance(value, list):
                for element in value:
                    if isinstance(element, dict):
                        inspect_object(
                            element,
                            block_index=block_index,
                            is_top_level=False,
                        )

    for block_index, script_tag in enumerate(script_tags, start=1):
        raw_content = (
            script_tag.string
            if script_tag.string is not None
            else script_tag.get_text()
        )
        cleaned_content = (raw_content or "").strip()

        if not cleaned_content:
            add_issue(
                "invalid_json_ld",
                f"JSON-LD script block #{block_index} is empty.",
            )
            continue

        try:
            parsed_data = json.loads(cleaned_content)
        except json.JSONDecodeError as error:
            add_issue(
                "invalid_json_ld",
                (
                    f"JSON-LD script block #{block_index} contains invalid "
                    f"JSON: {error.msg}."
                ),
            )
            continue

        if isinstance(parsed_data, dict):
            inspect_object(
                parsed_data,
                block_index=block_index,
                is_top_level=True,
            )
        elif isinstance(parsed_data, list):
            if not parsed_data:
                add_issue(
                    "invalid_schema_structure",
                    (
                        f"JSON-LD script block #{block_index} contains an "
                        "empty JSON array."
                    ),
                )
                continue

            if any(not isinstance(item, dict) for item in parsed_data):
                add_issue(
                    "invalid_schema_structure",
                    (
                        f"JSON-LD script block #{block_index} array must "
                        "contain only JSON objects."
                    ),
                )

            for item in parsed_data:
                if isinstance(item, dict):
                    inspect_object(
                        item,
                        block_index=block_index,
                        is_top_level=True,
                    )
        else:
            add_issue(
                "invalid_schema_structure",
                (
                    f"JSON-LD script block #{block_index} must be a JSON "
                    "object or array of objects."
                ),
            )

    duplicate_types = [
        top_level_type_labels[norm_key]
        for norm_key, count in top_level_type_counts.items()
        if count > 1
    ]
    if duplicate_types:
        add_issue(
            "duplicate_schema_type",
            (
                "Multiple JSON-LD structured data blocks declare the same "
                f"schema '@type': {', '.join(duplicate_types)}"
            ),
            schema_types=duplicate_types,
        )

    for issue in issues:
        if not issue["schema_types"] and all_discovered_types and issue["type"] != "missing_structured_data":
            issue["schema_types"] = list(all_discovered_types)

    return issues


_NON_VISIBLE_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "template",
    "head",
    "title",
    "meta",
    "link",
    "iframe",
    "canvas",
    "object",
}


def extract_visible_text(soup: BeautifulSoup) -> str:
    """Extract normalized visible text from parsed HTML, excluding non-visible elements."""
    chunks: list[str] = []
    root = soup.body if soup.body is not None else soup

    for text_node in root.find_all(string=True):
        if isinstance(
            text_node,
            (Comment, Declaration, ProcessingInstruction, CData),
        ):
            continue

        skip_node = False
        parent = text_node.parent
        while parent is not None:
            parent_name = getattr(parent, "name", None)
            if (
                isinstance(parent_name, str)
                and parent_name.lower() in _NON_VISIBLE_TAGS
            ):
                skip_node = True
                break
            parent = getattr(parent, "parent", None)

        if skip_node:
            continue

        cleaned_chunk = str(text_node).strip()
        if cleaned_chunk:
            chunks.append(cleaned_chunk)

    return " ".join(" ".join(chunks).split())


def extract_meaningful_words(visible_text: str) -> list[str]:
    """Return meaningful word tokens containing at least one alphanumeric character."""
    if not visible_text:
        return []
    return [
        token
        for token in visible_text.split()
        if any(char.isalnum() for char in token)
    ]


def normalize_visible_content(visible_text: str | None) -> str | None:
    """Normalize visible text for duplicate comparison; return None if no meaningful words."""
    if not visible_text:
        return None
    words = extract_meaningful_words(visible_text)
    if not words:
        return None
    return " ".join(visible_text.split()).casefold()


def detect_content_quality_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
    visible_text: str | None = None,
    thin_content_threshold: int = 300,
    low_word_count_threshold: int = 500,
    excessive_word_count_threshold: int = 3000,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    text = (
        visible_text
        if visible_text is not None
        else extract_visible_text(soup)
    )
    words = extract_meaningful_words(text)
    word_count = len(words)

    if word_count == 0:
        return [
            {
                "type": "missing_content",
                "severity": "warning",
                "message": (
                    "Page contains no meaningful visible text content "
                    "(0 words)."
                ),
                "url": url,
                "word_count": 0,
            }
        ]

    if word_count < thin_content_threshold:
        return [
            {
                "type": "thin_content",
                "severity": "warning",
                "message": (
                    f"Page has thin content ({word_count} words; minimum "
                    f"recommended is {thin_content_threshold} words)."
                ),
                "url": url,
                "word_count": word_count,
            }
        ]

    if word_count < low_word_count_threshold:
        return [
            {
                "type": "low_word_count",
                "severity": "info",
                "message": (
                    f"Page has a low word count ({word_count} words; "
                    f"recommended is at least {low_word_count_threshold} words)."
                ),
                "url": url,
                "word_count": word_count,
            }
        ]

    if word_count > excessive_word_count_threshold:
        return [
            {
                "type": "excessive_word_count",
                "severity": "warning",
                "message": (
                    f"Page has an excessive word count ({word_count} words; "
                    "maximum recommended is "
                    f"{excessive_word_count_threshold} words)."
                ),
                "url": url,
                "word_count": word_count,
            }
        ]

    return []


def detect_duplicate_content_issues(
    results: list[dict[str, Any]],
    page_contents: dict[str, tuple[str | None, int]] | None = None,
) -> None:
    html_pages = [
        page
        for page in results
        if page.get("error") is None and is_html_content(page.get("content_type"))
    ]

    groups: dict[str, list[tuple[dict[str, Any], int]]] = {}
    for page in html_pages:
        page_url = page["url"]
        normalized_text: str | None = None
        word_count = 0

        if page_contents is not None and page_url in page_contents:
            normalized_text, word_count = page_contents[page_url]
        elif "visible_text" in page:
            raw_vis = page.get("visible_text") or ""
            normalized_text = normalize_visible_content(raw_vis)
            word_count = len(extract_meaningful_words(raw_vis))

        if normalized_text is None or word_count == 0:
            continue

        groups.setdefault(normalized_text, []).append((page, word_count))

    for matching_entries in groups.values():
        if len(matching_entries) < 2:
            continue

        affected_urls = [page["url"] for page, _ in matching_entries]
        urls_text = ", ".join(affected_urls)

        for page, word_count in matching_entries:
            seo_issues = page.setdefault("seo_issues", [])
            existing_types = {issue["type"] for issue in seo_issues}
            if "duplicate_content" in existing_types:
                continue

            seo_issues.append(
                {
                    "type": "duplicate_content",
                    "severity": "warning",
                    "message": (
                        f"Page visible text content ({word_count} words) is "
                        f"duplicated across pages: {urls_text}"
                    ),
                    "url": page["url"],
                    "word_count": word_count,
                    "duplicate_urls": list(affected_urls),
                }
            )


SLOW_RESPONSE_THRESHOLD_SECONDS = 2.0
VERY_SLOW_RESPONSE_THRESHOLD_SECONDS = 5.0
LARGE_PAGE_SIZE_THRESHOLD_BYTES = 1 * 1024 * 1024  # 1 MB (1,048,576 bytes)
VERY_LARGE_PAGE_SIZE_THRESHOLD_BYTES = 3 * 1024 * 1024  # 3 MB (3,145,728 bytes)
# Overall crawl duration threshold (in seconds) for reporting an informational
# slow_crawl insight. A default 10-page crawl with a 1s delay typically finishes
# within 10-15s; exceeding 30s signals slow server responses or heavy checks.
SLOW_CRAWL_THRESHOLD_SECONDS = 30.0


def _extract_response_time_seconds(
    response: httpx.Response,
    measured_elapsed_seconds: float,
) -> float:
    raw_elapsed = getattr(response, "_elapsed", None)
    if raw_elapsed is not None and hasattr(raw_elapsed, "total_seconds"):
        return max(0.0, float(raw_elapsed.total_seconds()))
    return max(0.0, float(measured_elapsed_seconds))


def detect_performance_seo_issues(
    url: str,
    content_type: str | None,
    response_time_seconds: float,
    page_size_bytes: int,
    slow_response_threshold: float = SLOW_RESPONSE_THRESHOLD_SECONDS,
    very_slow_response_threshold: float = VERY_SLOW_RESPONSE_THRESHOLD_SECONDS,
    large_page_size_threshold: int = LARGE_PAGE_SIZE_THRESHOLD_BYTES,
    very_large_page_size_threshold: int = VERY_LARGE_PAGE_SIZE_THRESHOLD_BYTES,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    issues: list[dict[str, Any]] = []
    response_time_ms = round(response_time_seconds * 1000, 2)

    if response_time_seconds > very_slow_response_threshold:
        issues.append(
            {
                "type": "very_slow_response",
                "severity": "warning",
                "message": (
                    f"Page response time ({response_time_ms} ms) exceeds the "
                    f"{very_slow_response_threshold}s threshold."
                ),
                "url": url,
                "response_time_ms": response_time_ms,
                "page_size_bytes": page_size_bytes,
            }
        )
    elif response_time_seconds > slow_response_threshold:
        issues.append(
            {
                "type": "slow_response",
                "severity": "warning",
                "message": (
                    f"Page response time ({response_time_ms} ms) exceeds the "
                    f"{slow_response_threshold}s threshold."
                ),
                "url": url,
                "response_time_ms": response_time_ms,
                "page_size_bytes": page_size_bytes,
            }
        )

    if page_size_bytes > very_large_page_size_threshold:
        issues.append(
            {
                "type": "very_large_page_size",
                "severity": "warning",
                "message": (
                    f"HTML response body size ({page_size_bytes} bytes) "
                    "exceeds the 3 MB "
                    f"({very_large_page_size_threshold} bytes) threshold."
                ),
                "url": url,
                "response_time_ms": response_time_ms,
                "page_size_bytes": page_size_bytes,
            }
        )
    elif page_size_bytes > large_page_size_threshold:
        issues.append(
            {
                "type": "large_page_size",
                "severity": "warning",
                "message": (
                    f"HTML response body size ({page_size_bytes} bytes) "
                    "exceeds the 1 MB "
                    f"({large_page_size_threshold} bytes) threshold."
                ),
                "url": url,
                "response_time_ms": response_time_ms,
                "page_size_bytes": page_size_bytes,
            }
        )

    return issues


def detect_slow_crawl_issue(
    results: list[dict[str, Any]],
    crawl_duration_seconds: float,
    threshold_seconds: float = SLOW_CRAWL_THRESHOLD_SECONDS,
) -> None:
    if crawl_duration_seconds <= threshold_seconds or not results:
        return

    target_page = next(
        (
            page
            for page in results
            if page.get("error") is None
            and is_html_content(page.get("content_type"))
        ),
        results[0],
    )
    seo_issues = target_page.setdefault("seo_issues", [])
    existing_types = {issue["type"] for issue in seo_issues}
    if "slow_crawl" in existing_types:
        return

    rounded_duration = round(crawl_duration_seconds, 2)
    duration_ms = round(crawl_duration_seconds * 1000, 2)
    seo_issues.append(
        {
            "type": "slow_crawl",
            "severity": "info",
            "message": (
                f"Overall crawl duration ({rounded_duration}s) exceeded the "
                f"{threshold_seconds}s threshold across {len(results)} page(s)."
            ),
            "url": target_page["url"],
            "crawl_duration_seconds": rounded_duration,
            "response_time_ms": duration_ms,
            "page_size_bytes": target_page.get("page_size_bytes"),
        }
    )


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


VAGUE_LINK_TEXTS = {
    "click here",
    "here",
    "read more",
    "more",
    "learn more",
    "link",
    "this link",
    "continue",
    "continue reading",
    "go",
    "details",
}


def _get_attr_str(tag: Any, attr_name: str) -> str:
    if not hasattr(tag, "get"):
        return ""
    raw = tag.get(attr_name)
    if raw is None:
        return ""
    if isinstance(raw, list):
        return " ".join(str(item) for item in raw).strip()
    return str(raw).strip()


def _is_element_hidden(tag: Any) -> bool:
    current = tag
    while current is not None and getattr(current, "name", None):
        if hasattr(current, "has_attr") and current.has_attr("hidden"):
            return True
        aria_hidden = _get_attr_str(current, "aria-hidden").lower()
        if aria_hidden == "true":
            return True
        style = _get_attr_str(current, "style").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        current = getattr(current, "parent", None)
    return False


def _extract_visible_element_text(tag: Any) -> str:
    if tag is None:
        return ""
    parts: list[str] = []
    for descendant in tag.descendants:
        if isinstance(
            descendant, (CData, Comment, Declaration, ProcessingInstruction)
        ):
            continue
        if isinstance(descendant, NavigableString):
            parent = getattr(descendant, "parent", None)
            skip = False
            curr = parent
            while curr is not None and getattr(curr, "name", None):
                if curr.name.lower() in _NON_VISIBLE_TAGS:
                    skip = True
                    break
                if hasattr(curr, "has_attr") and curr.has_attr("hidden"):
                    skip = True
                    break
                if _get_attr_str(curr, "aria-hidden").lower() == "true":
                    skip = True
                    break
                style = _get_attr_str(curr, "style").lower().replace(" ", "")
                if "display:none" in style or "visibility:hidden" in style:
                    skip = True
                    break
                curr = getattr(curr, "parent", None)
            if not skip:
                text = str(descendant).strip()
                if text:
                    parts.append(text)
    return " ".join(" ".join(parts).split())


def _resolve_aria_labelledby_text(soup: BeautifulSoup, raw_ids: str) -> str:
    if not raw_ids or not raw_ids.strip():
        return ""
    parts: list[str] = []
    for ref_id in raw_ids.split():
        target = soup.find(id=ref_id)
        if target is not None:
            text = " ".join(target.get_text(separator=" ", strip=True).split())
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def _get_accessible_name(tag: Any, soup: BeautifulSoup) -> str:
    aria_labelledby = _get_attr_str(tag, "aria-labelledby")
    if aria_labelledby:
        resolved = _resolve_aria_labelledby_text(soup, aria_labelledby)
        if resolved:
            return resolved

    aria_label = _get_attr_str(tag, "aria-label")
    if aria_label:
        return " ".join(aria_label.split())

    visible_text = _extract_visible_element_text(tag)
    if visible_text:
        return visible_text

    # Check child images, SVGs, or elements with aria-label / alt
    for img in tag.find_all("img"):
        if _is_element_hidden(img):
            continue
        alt_text = _get_attr_str(img, "alt")
        if alt_text:
            return " ".join(alt_text.split())
        img_aria = _get_attr_str(img, "aria-label")
        if img_aria:
            return " ".join(img_aria.split())

    for svg in tag.find_all("svg"):
        if _is_element_hidden(svg):
            continue
        svg_aria = _get_attr_str(svg, "aria-label")
        if svg_aria:
            return " ".join(svg_aria.split())
        svg_labelledby = _get_attr_str(svg, "aria-labelledby")
        if svg_labelledby:
            resolved_svg = _resolve_aria_labelledby_text(soup, svg_labelledby)
            if resolved_svg:
                return resolved_svg
        title_tag = svg.find("title")
        if title_tag is not None:
            svg_title = " ".join(title_tag.get_text(separator=" ", strip=True).split())
            if svg_title:
                return svg_title

    for child in tag.find_all(True):
        if _is_element_hidden(child):
            continue
        child_aria = _get_attr_str(child, "aria-label")
        if child_aria:
            return " ".join(child_aria.split())

    title_attr = _get_attr_str(tag, "title")
    if title_attr:
        return " ".join(title_attr.split())

    return ""


def _has_accessible_form_label(control: Any, soup: BeautifulSoup) -> bool:
    aria_labelledby = _get_attr_str(control, "aria-labelledby")
    if aria_labelledby and _resolve_aria_labelledby_text(soup, aria_labelledby):
        return True

    if _get_attr_str(control, "aria-label"):
        return True

    control_id = _get_attr_str(control, "id")
    if control_id:
        for label_tag in soup.find_all("label"):
            if _get_attr_str(label_tag, "for") == control_id:
                label_name = _get_accessible_name(label_tag, soup)
                if label_name:
                    return True

    parent_label = control.find_parent("label")
    if parent_label is not None:
        label_name = _get_accessible_name(parent_label, soup)
        if label_name:
            return True

    if _get_attr_str(control, "title"):
        return True

    return False


def detect_accessibility_seo_issues(
    url: str,
    soup: BeautifulSoup,
    content_type: str | None,
) -> list[dict[str, Any]]:
    if not is_html_content(content_type):
        return []

    issues: list[dict[str, Any]] = []

    # 1. missing_lang: Root <html> element missing a valid lang attribute
    html_tag = soup.find("html")
    lang_value = ""
    if html_tag is not None:
        lang_value = _get_attr_str(html_tag, "lang") or _get_attr_str(
            html_tag, "xml:lang"
        )
    if not lang_value:
        issues.append(
            {
                "type": "missing_lang",
                "severity": "warning",
                "message": (
                    "The root <html> element is missing a valid lang attribute "
                    "(e.g., <html lang=\"en\">) to declare the page language for "
                    "screen readers"
                ),
                "url": url,
            }
        )

    # 2. missing_form_label: Form inputs lacking an associated accessible label
    for control in soup.find_all(["input", "select", "textarea"]):
        if _is_element_hidden(control):
            continue

        tag_name = control.name.lower()
        input_type = "text"
        if tag_name == "input":
            raw_type = _get_attr_str(control, "type").lower()
            input_type = raw_type if raw_type else "text"

            # Hidden inputs do not need labels; button/submit/reset inputs are
            # buttons rather than data-entry form fields.
            if input_type in ("hidden", "button", "submit", "reset"):
                continue

            if input_type == "image":
                has_image_name = bool(
                    _get_attr_str(control, "alt")
                    or _get_attr_str(control, "aria-label")
                    or (
                        _get_attr_str(control, "aria-labelledby")
                        and _resolve_aria_labelledby_text(
                            soup, _get_attr_str(control, "aria-labelledby")
                        )
                    )
                    or _get_attr_str(control, "title")
                )
                if not has_image_name:
                    issues.append(
                        {
                            "type": "missing_form_label",
                            "severity": "warning",
                            "message": (
                                "Image input (<input type=\"image\">) is missing an "
                                "alt attribute or ARIA label describing its action"
                            ),
                            "url": url,
                        }
                    )
                continue

        if not _has_accessible_form_label(control, soup):
            control_name = (
                _get_attr_str(control, "name")
                or _get_attr_str(control, "id")
                or (f"input[type={input_type}]" if tag_name == "input" else tag_name)
            )
            issues.append(
                {
                    "type": "missing_form_label",
                    "severity": "warning",
                    "message": (
                        f"Form control '{control_name}' (<{tag_name}>) is missing an "
                        f"associated <label>, aria-label, or aria-labelledby attribute"
                    ),
                    "url": url,
                }
            )

    # 3. empty_link and 4. vague_link_text
    for link in soup.find_all("a"):
        if not link.has_attr("href"):
            continue
        if _is_element_hidden(link):
            continue

        accessible_name = _get_accessible_name(link, soup)
        href_value = _get_attr_str(link, "href")
        if not accessible_name:
            issues.append(
                {
                    "type": "empty_link",
                    "severity": "warning",
                    "message": (
                        f"Link to '{href_value or '(empty href)'}' has no accessible "
                        f"name (provide visible text, image alt text, or aria-label)"
                    ),
                    "url": url,
                }
            )
            continue

        normalized_link_text = " ".join(accessible_name.split()).strip(
            " .,!?:;'\"-_»›→()[]"
        ).casefold()
        if normalized_link_text in VAGUE_LINK_TEXTS:
            issues.append(
                {
                    "type": "vague_link_text",
                    "severity": "info",
                    "message": (
                        f"Link to '{href_value or '(empty href)'}' uses generic text "
                        f"'{accessible_name}'; use descriptive anchor text that "
                        f"explains the link destination"
                    ),
                    "url": url,
                }
            )

    # 5. heading_hierarchy: Heading levels skip in a problematic way (e.g. H1 -> H3)
    previous_level: int | None = None
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if _is_element_hidden(heading):
            continue
        current_level = int(heading.name[1])
        if previous_level is not None and current_level > previous_level + 1:
            heading_text = _get_accessible_name(heading, soup) or "(empty heading)"
            issues.append(
                {
                    "type": "heading_hierarchy",
                    "severity": "warning",
                    "message": (
                        f"Heading hierarchy skips from <h{previous_level}> to "
                        f"<h{current_level}> ('{heading_text}'); use sequential "
                        f"heading levels without skipping"
                    ),
                    "url": url,
                }
            )
        previous_level = current_level

    # 6. empty_button: Buttons lack an accessible name
    seen_buttons: set[int] = set()
    for button in soup.find_all("button"):
        if _is_element_hidden(button):
            continue
        seen_buttons.add(id(button))
        if not _get_accessible_name(button, soup):
            issues.append(
                {
                    "type": "empty_button",
                    "severity": "warning",
                    "message": (
                        "Button (<button>) has no accessible name; add visible text, "
                        "an aria-label, or an accessible icon label"
                    ),
                    "url": url,
                }
            )

    for input_btn in soup.find_all("input"):
        if _is_element_hidden(input_btn):
            continue
        input_type = _get_attr_str(input_btn, "type").lower()
        if input_type not in ("button", "submit", "reset"):
            continue
        seen_buttons.add(id(input_btn))

        has_explicit_name = bool(
            _get_attr_str(input_btn, "value")
            or _get_attr_str(input_btn, "aria-label")
            or (
                _get_attr_str(input_btn, "aria-labelledby")
                and _resolve_aria_labelledby_text(
                    soup, _get_attr_str(input_btn, "aria-labelledby")
                )
            )
            or _get_attr_str(input_btn, "title")
        )
        # Submit and reset inputs have browser-provided default labels ("Submit",
        # "Reset") unless the author explicitly sets an empty value="".
        if input_type in ("submit", "reset") and not input_btn.has_attr("value"):
            has_explicit_name = True

        if not has_explicit_name:
            issues.append(
                {
                    "type": "empty_button",
                    "severity": "warning",
                    "message": (
                        f"Button input (<input type=\"{input_type}\">) has no "
                        f"accessible name; provide a non-empty value or aria-label"
                    ),
                    "url": url,
                }
            )

    for role_btn in soup.find_all(attrs={"role": True}):
        if id(role_btn) in seen_buttons:
            continue
        if _is_element_hidden(role_btn):
            continue
        role_tokens = _get_attr_str(role_btn, "role").lower().split()
        if "button" not in role_tokens:
            continue
        if not _get_accessible_name(role_btn, soup):
            issues.append(
                {
                    "type": "empty_button",
                    "severity": "warning",
                    "message": (
                        f"Element <{role_btn.name} role=\"button\"> has no accessible "
                        f"name; provide visible text or an aria-label"
                    ),
                    "url": url,
                }
            )

    # 7. missing_landmark: Page lacks a main content landmark (<main> or role="main")
    has_main_landmark = False
    for main_tag in soup.find_all("main"):
        if not _is_element_hidden(main_tag):
            has_main_landmark = True
            break
    if not has_main_landmark:
        for role_el in soup.find_all(attrs={"role": True}):
            if _is_element_hidden(role_el):
                continue
            role_tokens = _get_attr_str(role_el, "role").lower().split()
            if "main" in role_tokens:
                has_main_landmark = True
                break
    if not has_main_landmark:
        issues.append(
            {
                "type": "missing_landmark",
                "severity": "info",
                "message": (
                    "Page is missing a main content landmark; wrap primary content "
                    "in a <main> element or an element with role=\"main\""
                ),
                "url": url,
            }
        )

    # 8. duplicate_id: Multiple elements share the same non-empty id attribute
    id_counts: dict[str, int] = {}
    for el in soup.find_all(attrs={"id": True}):
        el_id = _get_attr_str(el, "id")
        if not el_id:
            continue
        id_counts[el_id] = id_counts.get(el_id, 0) + 1

    for dup_id, count in id_counts.items():
        if count > 1:
            issues.append(
                {
                    "type": "duplicate_id",
                    "severity": "warning",
                    "message": (
                        f"Duplicate id '{dup_id}' appears {count} times on the page; "
                        f"element IDs must be unique so label and ARIA references "
                        f"work reliably"
                    ),
                    "url": url,
                }
            )

    return issues


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


# Thresholds for Internal Link Structure & Crawlability Audit:
# - Pages (excluding start_url) with fewer than 3 incoming internal links from
#   other crawled pages have weak internal link support.
# - Pages more than 4 clicks/levels deep from the start URL are harder for
#   users and search engines to reach.
# - Pages with more than 100 unique outgoing internal links dilute link equity.
LOW_INTERNAL_LINK_COUNT_THRESHOLD = 3
DEEP_PAGE_DEPTH_THRESHOLD = 4
EXCESSIVE_INTERNAL_LINKS_THRESHOLD = 100


def _normalize_internal_url(url: str) -> str:
    clean_url, _ = urldefrag(str(url).strip())
    try:
        parsed = urlparse(clean_url)
    except ValueError:
        return clean_url
    if not parsed.scheme or not parsed.hostname:
        return clean_url
    hostname = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            parsed.params or "",
            parsed.query or "",
            "",
        )
    )


def _extract_rel_tokens(tag: Any) -> set[str]:
    if not hasattr(tag, "get"):
        return set()
    raw_rel = tag.get("rel")
    if raw_rel is None:
        return set()
    if isinstance(raw_rel, (list, tuple)):
        tokens: set[str] = set()
        for item in raw_rel:
            for part in str(item).split():
                if part.strip():
                    tokens.add(part.strip().lower())
        return tokens
    return {
        part.strip().lower()
        for part in str(raw_rel).split()
        if part.strip()
    }


def _extract_page_internal_link_data(
    page_url: str,
    soup: BeautifulSoup,
    target_hostname: str | None,
    response_headers: Any = None,
) -> dict[str, Any]:
    normalized_self = _normalize_internal_url(page_url)
    target_host = (
        target_hostname or urlparse(page_url).hostname or ""
    ).casefold()

    outgoing_urls: list[str] = []
    seen_outgoing: set[str] = set()
    nofollow_internal_urls: list[str] = []
    seen_nofollow: set[str] = set()

    for tag in soup.find_all("a"):
        raw_href = tag.get("href")
        if raw_href is None:
            continue
        resolved_url, is_valid = _resolve_canonical_url(page_url, raw_href)
        if not is_valid or not resolved_url:
            continue
        parsed_link = urlparse(resolved_url)
        if (parsed_link.hostname or "").casefold() != target_host:
            continue

        normalized_target = _normalize_internal_url(resolved_url)
        rel_tokens = _extract_rel_tokens(tag)
        if "nofollow" in rel_tokens:
            if normalized_target not in seen_nofollow:
                seen_nofollow.add(normalized_target)
                nofollow_internal_urls.append(normalized_target)

        # Exclude self-links from outgoing/incoming inter-page link counts
        if normalized_target == normalized_self:
            continue

        if normalized_target not in seen_outgoing:
            seen_outgoing.add(normalized_target)
            outgoing_urls.append(normalized_target)

    canonical_url: str | None = None
    resolved_canonical_url: str | None = None
    canonical_tags = _find_canonical_link_tags(soup)
    if len(canonical_tags) == 1:
        resolved_canon, canon_valid = _resolve_canonical_url(
            page_url, canonical_tags[0].get("href")
        )
        if canon_valid and resolved_canon:
            resolved_canonical_url = _normalize_internal_url(resolved_canon)
            canon_host = (urlparse(resolved_canon).hostname or "").casefold()
            if canon_host == target_host:
                canonical_url = resolved_canonical_url

    sitemap_link_urls: list[str] = []
    for link_tag in soup.find_all("link"):
        rel_tokens = _extract_rel_tokens(link_tag)
        if "sitemap" not in rel_tokens:
            continue
        raw_sitemap_href = link_tag.get("href")
        if not isinstance(raw_sitemap_href, str) or not raw_sitemap_href.strip():
            continue
        resolved_sm, _ = urldefrag(urljoin(page_url, raw_sitemap_href.strip()))
        if _validate_http_url(resolved_sm) and resolved_sm not in sitemap_link_urls:
            sitemap_link_urls.append(resolved_sm)

    # Check if page has any meta robots / X-Robots-Tag directive preventing link following
    has_nofollow_directive = False
    nofollow_directives: list[str] = []
    nofollow_sources: list[str] = []

    for tag in soup.find_all("meta"):
        raw_name = tag.get("name")
        raw_content = tag.get("content")
        if not isinstance(raw_name, str) or raw_content is None:
            continue
        bot_name = raw_name.strip().lower()
        if bot_name in ("robots", "googlebot", "bingbot", "slurp"):
            cleaned_content = str(raw_content).strip()
            if not cleaned_content:
                continue
            tokens = _parse_robots_directives(cleaned_content)
            if "nofollow" in tokens:
                has_nofollow_directive = True
                nofollow_directives.append(cleaned_content)
                source_label = f"meta_{bot_name}"
                if source_label not in nofollow_sources:
                    nofollow_sources.append(source_label)

    for header_val in _extract_x_robots_headers(response_headers):
        tokens = _parse_robots_directives(header_val)
        if "nofollow" in tokens:
            has_nofollow_directive = True
            nofollow_directives.append(header_val)
            if "x_robots_tag" not in nofollow_sources:
                nofollow_sources.append("x_robots_tag")

    return {
        "outgoing_urls": outgoing_urls,
        "nofollow_internal_urls": nofollow_internal_urls,
        "canonical_url": canonical_url,
        "resolved_canonical_url": resolved_canonical_url,
        "sitemap_link_urls": sitemap_link_urls,
        "has_nofollow_directive": has_nofollow_directive,
        "nofollow_directive": " | ".join(nofollow_directives)
        if nofollow_directives
        else None,
        "directive_source": ", ".join(nofollow_sources)
        if nofollow_sources
        else None,
    }


def _find_isolated_sections(
    html_page_urls: list[str],
    start_page_url: str,
    crawled_outgoing: dict[str, set[str]],
) -> dict[str, tuple[list[str], int]]:
    """Identify pages belonging to isolated multi-page sections in the crawled link graph."""
    if len(html_page_urls) < 3:
        return {}

    url_set = set(html_page_urls)
    order_index = {u: idx for idx, u in enumerate(html_page_urls)}
    isolated_map: dict[str, tuple[list[str], int]] = {}

    # 1. Check weakly connected components that do not contain start_page_url
    undirected_adj: dict[str, set[str]] = {u: set() for u in html_page_urls}
    for u, targets in crawled_outgoing.items():
        for v in targets:
            if v in url_set and v != u:
                undirected_adj[u].add(v)
                undirected_adj[v].add(u)

    visited_weak: set[str] = set()
    for node in html_page_urls:
        if node in visited_weak:
            continue
        comp: set[str] = set()
        bfs_queue = deque([node])
        visited_weak.add(node)
        while bfs_queue:
            curr = bfs_queue.popleft()
            comp.add(curr)
            for neighbor in undirected_adj[curr]:
                if neighbor not in visited_weak:
                    visited_weak.add(neighbor)
                    bfs_queue.append(neighbor)

        if start_page_url not in comp and len(comp) >= 2:
            sorted_comp = sorted(comp, key=lambda u: order_index[u])
            for member in sorted_comp:
                isolated_map[member] = (sorted_comp, 0)

    # 2. Check Strongly Connected Components (SCCs) of size >= 2 (excluding
    # start_page_url's SCC) that have 0 outgoing links to the rest of the site
    # and at most 1 incoming link from outside the component.
    reachable: dict[str, set[str]] = {}
    for u in html_page_urls:
        seen_r: set[str] = {u}
        q_r = deque([u])
        while q_r:
            curr = q_r.popleft()
            for nxt in crawled_outgoing.get(curr, set()):
                if nxt in url_set and nxt not in seen_r:
                    seen_r.add(nxt)
                    q_r.append(nxt)
        reachable[u] = seen_r

    visited_scc: set[str] = set()
    for u in html_page_urls:
        if u in visited_scc:
            continue
        scc = {
            v
            for v in html_page_urls
            if v in reachable[u] and u in reachable[v]
        }
        visited_scc.update(scc)

        if start_page_url in scc or len(scc) < 2:
            continue

        outside_outgoing = sum(
            1
            for member in scc
            for target in crawled_outgoing.get(member, set())
            if target in url_set and target not in scc
        )
        outside_incoming = sum(
            1
            for other in html_page_urls
            if other not in scc
            for target in crawled_outgoing.get(other, set())
            if target in scc
        )

        if outside_outgoing == 0 and outside_incoming <= 1:
            sorted_scc = sorted(scc, key=lambda item: order_index[item])
            for member in sorted_scc:
                if member not in isolated_map:
                    isolated_map[member] = (sorted_scc, outside_incoming)

    return isolated_map


def detect_internal_link_structure_issues(
    results: list[dict[str, Any]],
    start_url: str,
    page_link_data: dict[str, dict[str, Any]] | None = None,
) -> None:
    if page_link_data is None:
        page_link_data = {}

    for page in results:
        page.setdefault("incoming_internal_links", 0)
        page.setdefault("outgoing_internal_links", 0)

    html_pages = [
        page
        for page in results
        if page.get("error") is None and is_html_content(page.get("content_type"))
    ]
    if not html_pages:
        return

    clean_start_norm = _normalize_internal_url(start_url)

    # Map normalized URLs and unambiguous same-domain canonical URLs to crawled page URLs
    norm_to_page_url: dict[str, str] = {}
    for page in html_pages:
        page_url = page["url"]
        norm_to_page_url[_normalize_internal_url(page_url)] = page_url

    for page in html_pages:
        page_url = page["url"]
        info = page_link_data.get(page_url, {})
        canon_norm = info.get("canonical_url")
        if canon_norm and canon_norm not in norm_to_page_url:
            norm_to_page_url[canon_norm] = page_url

    start_page_url = norm_to_page_url.get(clean_start_norm, clean_start_norm)
    html_page_urls = [page["url"] for page in html_pages]
    html_url_set = set(html_page_urls)

    incoming_sources: dict[str, set[str]] = {u: set() for u in html_page_urls}
    crawled_outgoing: dict[str, set[str]] = {u: set() for u in html_page_urls}
    resolved_outgoing_by_page: dict[str, list[str]] = {}

    for page in html_pages:
        page_url = page["url"]
        info = page_link_data.get(page_url, {})
        raw_outgoing = info.get("outgoing_urls", [])

        dedup_outgoing: list[str] = []
        seen_targets: set[str] = set()
        for target_norm in raw_outgoing:
            mapped_target = norm_to_page_url.get(target_norm, target_norm)
            if mapped_target == page_url:
                continue
            if mapped_target not in seen_targets:
                seen_targets.add(mapped_target)
                dedup_outgoing.append(mapped_target)

        resolved_outgoing_by_page[page_url] = dedup_outgoing
        page["outgoing_internal_links"] = len(dedup_outgoing)

        for target_url in dedup_outgoing:
            if target_url in html_url_set and target_url != page_url:
                incoming_sources[target_url].add(page_url)
                crawled_outgoing[page_url].add(target_url)

    for page in html_pages:
        page_url = page["url"]
        page["incoming_internal_links"] = len(incoming_sources[page_url])

    isolated_sections = _find_isolated_sections(
        html_page_urls=html_page_urls,
        start_page_url=start_page_url,
        crawled_outgoing=crawled_outgoing,
    )

    for page in html_pages:
        page_url = page["url"]
        page_depth = page.get("depth", 0)
        incoming_count = page["incoming_internal_links"]
        outgoing_count = page["outgoing_internal_links"]
        info = page_link_data.get(page_url, {})

        seo_issues = page.setdefault("seo_issues", [])
        existing_types = {issue.get("type") for issue in seo_issues}
        is_start_page = (
            page_url == start_page_url
            or _normalize_internal_url(page_url) == clean_start_norm
        )

        # 1. orphan_page: 0 incoming internal links from other crawled pages (excluding start_url)
        if (
            not is_start_page
            and incoming_count == 0
            and "orphan_page" not in existing_types
        ):
            existing_types.add("orphan_page")
            seo_issues.append(
                {
                    "type": "orphan_page",
                    "severity": "warning",
                    "message": (
                        "Crawled page has 0 incoming internal links from other "
                        "crawled pages; add internal links from relevant pages "
                        "so users and search engines can discover it."
                    ),
                    "url": page_url,
                    "incoming_internal_links": incoming_count,
                    "outgoing_internal_links": outgoing_count,
                    "depth": page_depth,
                }
            )

        # 2. low_internal_link_count: fewer than 3 incoming internal links from other crawled pages
        if (
            not is_start_page
            and len(html_pages) >= LOW_INTERNAL_LINK_COUNT_THRESHOLD
            and 0 < incoming_count < LOW_INTERNAL_LINK_COUNT_THRESHOLD
            and "orphan_page" not in existing_types
            and "low_internal_link_count" not in existing_types
        ):
            existing_types.add("low_internal_link_count")
            seo_issues.append(
                {
                    "type": "low_internal_link_count",
                    "severity": "warning",
                    "message": (
                        f"Page has only {incoming_count} incoming internal "
                        f"link(s) from other crawled pages (recommended "
                        f"minimum is {LOW_INTERNAL_LINK_COUNT_THRESHOLD}); add "
                        f"contextual internal links to improve crawlability."
                    ),
                    "url": page_url,
                    "incoming_internal_links": incoming_count,
                    "outgoing_internal_links": outgoing_count,
                    "depth": page_depth,
                }
            )

        # 3. deep_page: more than 4 link-depth levels away from starting URL
        if (
            page_depth > DEEP_PAGE_DEPTH_THRESHOLD
            and "deep_page" not in existing_types
        ):
            existing_types.add("deep_page")
            seo_issues.append(
                {
                    "type": "deep_page",
                    "severity": "warning",
                    "message": (
                        f"Page is {page_depth} link-depth levels away from the "
                        f"starting URL (recommended maximum is "
                        f"{DEEP_PAGE_DEPTH_THRESHOLD}); flatten site "
                        f"architecture so key pages are reachable in fewer "
                        f"clicks."
                    ),
                    "url": page_url,
                    "depth": page_depth,
                    "incoming_internal_links": incoming_count,
                    "outgoing_internal_links": outgoing_count,
                }
            )

        # 4. nofollow_internal_link: internal links on the page use rel="nofollow"
        nofollow_urls = info.get("nofollow_internal_urls", [])
        if nofollow_urls and "nofollow_internal_link" not in existing_types:
            existing_types.add("nofollow_internal_link")
            seo_issues.append(
                {
                    "type": "nofollow_internal_link",
                    "severity": "warning",
                    "message": (
                        f"Page has {len(nofollow_urls)} internal link(s) with "
                        f'rel="nofollow" ({", ".join(nofollow_urls)}); remove '
                        f"nofollow on internal links so search engines can "
                        f"follow them and pass link equity."
                    ),
                    "url": page_url,
                    "nofollow_links": list(nofollow_urls),
                    "outgoing_internal_links": outgoing_count,
                    "depth": page_depth,
                }
            )

        # 5. nofollow_page: page-level directive prevents following links (if not already emitted)
        if (
            info.get("has_nofollow_directive")
            and "nofollow_page" not in existing_types
        ):
            existing_types.add("nofollow_page")
            directive_val = info.get("nofollow_directive") or "nofollow"
            source_val = info.get("directive_source") or "meta_robots"
            seo_issues.append(
                {
                    "type": "nofollow_page",
                    "severity": "warning",
                    "message": (
                        f"Page prevents search engines from following links via "
                        f"{source_val}: {directive_val}"
                    ),
                    "url": page_url,
                    "robots_directive": directive_val,
                    "directive_source": source_val,
                }
            )

        # 6. excessive_internal_links: more than 100 unique internal links on the page
        if (
            outgoing_count > EXCESSIVE_INTERNAL_LINKS_THRESHOLD
            and "excessive_internal_links" not in existing_types
        ):
            existing_types.add("excessive_internal_links")
            seo_issues.append(
                {
                    "type": "excessive_internal_links",
                    "severity": "warning",
                    "message": (
                        f"Page contains {outgoing_count} unique outgoing "
                        f"internal links, exceeding the threshold of "
                        f"{EXCESSIVE_INTERNAL_LINKS_THRESHOLD}; reduce link "
                        f"clutter to focus crawl budget and link equity."
                    ),
                    "url": page_url,
                    "outgoing_internal_links": outgoing_count,
                    "incoming_internal_links": incoming_count,
                    "depth": page_depth,
                }
            )

        # 7. isolated_section: page belongs to a weakly connected / isolated internal section
        if (
            page_url in isolated_sections
            and "isolated_section" not in existing_types
        ):
            existing_types.add("isolated_section")
            comp_urls, outside_incoming = isolated_sections[page_url]
            seo_issues.append(
                {
                    "type": "isolated_section",
                    "severity": "warning",
                    "message": (
                        f"Page is part of an isolated internal section "
                        f"({', '.join(comp_urls)}) with {outside_incoming} "
                        f"incoming and 0 outgoing internal links to the rest "
                        f"of the site; add reciprocal internal links to "
                        f"integrate this section."
                    ),
                    "url": page_url,
                    "isolated_pages": list(comp_urls),
                    "incoming_internal_links": incoming_count,
                    "outgoing_internal_links": outgoing_count,
                    "depth": page_depth,
                }
            )


MAX_SITEMAP_FETCHES = 10

_VALID_ROBOTS_DIRECTIVES = {
    "user-agent",
    "disallow",
    "allow",
    "sitemap",
    "crawl-delay",
    "host",
    "clean-param",
    "request-rate",
    "visit-time",
    "noindex",
}


def _validate_http_url(url: str) -> bool:
    """Return True if url has a valid http/https scheme and non-empty hostname."""
    if not url or any(ch.isspace() for ch in url):
        return False
    parsed = urlparse(url)
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname)


def _parse_and_validate_robots_txt(
    robots_text: str | None,
    robots_url: str,
) -> dict[str, Any]:
    """Parse robots.txt content for Sitemap directives and syntax/parsing issues."""
    sitemap_urls: list[str] = []
    seen_sitemaps: set[str] = set()
    invalid_lines: list[str] = []

    if not robots_text:
        return {
            "sitemap_urls": sitemap_urls,
            "invalid_lines": invalid_lines,
        }

    seen_user_agent = False

    for line_num, raw_line in enumerate(robots_text.splitlines(), start=1):
        # Remove inline comments and surrounding whitespace
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        if ":" not in line:
            invalid_lines.append(
                f"Line {line_num} ('{line}'): missing ':' separator"
            )
            continue

        directive_part, value_part = line.split(":", 1)
        directive = directive_part.strip().lower()
        value = value_part.strip()

        if directive not in _VALID_ROBOTS_DIRECTIVES:
            invalid_lines.append(
                f"Line {line_num} ('{line}'): unknown directive '{directive_part.strip()}'"
            )
            continue

        if directive == "user-agent":
            if not value:
                invalid_lines.append(
                    f"Line {line_num} ('{line}'): empty User-agent value"
                )
            else:
                seen_user_agent = True
        elif directive in ("disallow", "allow"):
            if not seen_user_agent:
                invalid_lines.append(
                    f"Line {line_num} ('{line}'): '{directive_part.strip()}' rule before any 'User-agent'"
                )
            elif value and not (value.startswith("/") or value.startswith("*")):
                invalid_lines.append(
                    f"Line {line_num} ('{line}'): path must start with '/' or '*'"
                )
        elif directive == "sitemap":
            if not value:
                invalid_lines.append(
                    f"Line {line_num} ('{line}'): empty Sitemap URL"
                )
            else:
                clean_sm, _ = urldefrag(value)
                if not _validate_http_url(clean_sm):
                    invalid_lines.append(
                        f"Line {line_num} ('{line}'): invalid Sitemap URL '{value}'"
                    )
                else:
                    norm_sm = _normalize_internal_url(clean_sm)
                    if norm_sm not in seen_sitemaps:
                        seen_sitemaps.add(norm_sm)
                        sitemap_urls.append(clean_sm)
        elif directive == "crawl-delay":
            try:
                delay_val = float(value)
                if delay_val < 0:
                    invalid_lines.append(
                        f"Line {line_num} ('{line}'): negative Crawl-delay"
                    )
            except ValueError:
                invalid_lines.append(
                    f"Line {line_num} ('{line}'): non-numeric Crawl-delay '{value}'"
                )

    return {
        "sitemap_urls": sitemap_urls,
        "invalid_lines": invalid_lines,
    }


def _is_url_blocked_by_robots(
    url: str,
    robot_parser: RobotFileParser | None,
    user_agent: str = "CrawlScope/0.1 (website auditing learning project)",
) -> bool:
    """Return True if url is disallowed by robots.txt for CrawlScope, '*', or 'Googlebot'."""
    if robot_parser is None:
        return False
    if getattr(robot_parser, "allow_all", False):
        return False
    if getattr(robot_parser, "disallow_all", False):
        return True

    for agent in (user_agent, "*", "Googlebot"):
        try:
            if not robot_parser.can_fetch(agent, url):
                return True
        except Exception:
            continue
    return False


def _local_xml_tag_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1].strip().lower()
    if ":" in tag:
        return tag.split(":", 1)[1].strip().lower()
    return tag.strip().lower()


def _fetch_and_parse_sitemaps(
    candidate_sitemap_urls: list[str],
    target_hostname: str | None,
    headers: dict[str, str],
    max_fetches: int = MAX_SITEMAP_FETCHES,
) -> dict[str, Any]:
    """Fetch and parse XML sitemaps and nested sitemap index files safely."""
    target_host_fold = (target_hostname or "").casefold()
    queue: deque[str] = deque()
    queued_norm: set[str] = set()

    for sm_url in candidate_sitemap_urls:
        clean_sm, _ = urldefrag(sm_url.strip())
        norm_sm = _normalize_internal_url(clean_sm)
        if norm_sm not in queued_norm:
            queued_norm.add(norm_sm)
            queue.append(clean_sm)

    visited_sitemaps: set[str] = set()
    fetched_sitemap_urls: list[str] = []
    valid_sitemap_urls: list[str] = []
    missing_sitemap_urls: list[tuple[str, int | None]] = []
    invalid_sitemaps: list[tuple[str, str]] = []

    internal_loc_urls: list[str] = []
    internal_norm_to_raw: dict[str, str] = {}
    external_loc_urls: list[str] = []

    while queue and len(visited_sitemaps) < max_fetches:
        current_sm_url = queue.popleft()
        norm_current = _normalize_internal_url(current_sm_url)
        if norm_current in visited_sitemaps:
            continue
        visited_sitemaps.add(norm_current)
        fetched_sitemap_urls.append(current_sm_url)

        try:
            sm_response = httpx.get(
                current_sm_url,
                headers=headers,
                timeout=10.0,
                follow_redirects=True,
            )
        except httpx.RequestError:
            missing_sitemap_urls.append((current_sm_url, None))
            continue

        if sm_response.status_code >= 400:
            missing_sitemap_urls.append(
                (current_sm_url, sm_response.status_code)
            )
            continue

        raw_xml = (sm_response.text or "").strip()
        if not raw_xml:
            invalid_sitemaps.append((current_sm_url, "empty sitemap response"))
            continue

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            invalid_sitemaps.append(
                (current_sm_url, f"XML parse error: {exc}")
            )
            continue

        root_name = _local_xml_tag_name(root.tag)
        if root_name not in ("urlset", "sitemapindex"):
            invalid_sitemaps.append(
                (
                    current_sm_url,
                    f"unexpected root element <{root_name or 'unknown'}> (expected <urlset> or <sitemapindex>)",
                )
            )
            continue

        if root_name == "sitemapindex":
            child_sitemaps: list[str] = []
            sitemap_entry_Error: str | None = None
            children = list(root)
            if not children:
                invalid_sitemaps.append(
                    (
                        current_sm_url,
                        "<sitemapindex> contains no <sitemap> entries",
                    )
                )
                continue

            for child in children:
                child_name = _local_xml_tag_name(child.tag)
                if child_name != "sitemap":
                    sitemap_entry_Error = f"<sitemapindex> contains invalid child element <{child_name}>"
                    break
                loc_text: str | None = None
                for sub in child:
                    if _local_xml_tag_name(sub.tag) == "loc":
                        loc_text = (sub.text or "").strip()
                        break
                if not loc_text:
                    sitemap_entry_Error = (
                        "<sitemap> entry in <sitemapindex> is missing <loc>"
                    )
                    break
                clean_child_sm, _ = urldefrag(loc_text)
                if not _validate_http_url(clean_child_sm):
                    sitemap_entry_Error = f"<sitemap> entry has invalid <loc> URL '{loc_text}'"
                    break
                child_sitemaps.append(clean_child_sm)

            if sitemap_entry_Error:
                invalid_sitemaps.append((current_sm_url, sitemap_entry_Error))
                continue

            valid_sitemap_urls.append(current_sm_url)
            for child_sm_url in child_sitemaps:
                norm_child = _normalize_internal_url(child_sm_url)
                if (
                    norm_child not in visited_sitemaps
                    and norm_child not in queued_norm
                ):
                    queued_norm.add(norm_child)
                    queue.append(child_sm_url)

        elif root_name == "urlset":
            url_entry_error: str | None = None
            parsed_locs: list[str] = []
            for child in list(root):
                child_name = _local_xml_tag_name(child.tag)
                if child_name != "url":
                    url_entry_error = (
                        f"<urlset> contains invalid child element <{child_name}>"
                    )
                    break
                loc_text = None
                for sub in child:
                    if _local_xml_tag_name(sub.tag) == "loc":
                        loc_text = (sub.text or "").strip()
                        break
                if not loc_text:
                    url_entry_error = "<url> entry in <urlset> is missing <loc>"
                    break
                clean_loc, _ = urldefrag(loc_text)
                if not _validate_http_url(clean_loc):
                    url_entry_error = (
                        f"<url> entry has invalid <loc> URL '{loc_text}'"
                    )
                    break
                parsed_locs.append(clean_loc)

            if url_entry_error:
                invalid_sitemaps.append((current_sm_url, url_entry_error))
                continue

            valid_sitemap_urls.append(current_sm_url)
            for clean_loc in parsed_locs:
                parsed_loc_obj = urlparse(clean_loc)
                loc_host = (parsed_loc_obj.hostname or "").casefold()
                if loc_host != target_host_fold:
                    if clean_loc not in external_loc_urls:
                        external_loc_urls.append(clean_loc)
                    continue

                norm_loc = _normalize_internal_url(clean_loc)
                if norm_loc not in internal_norm_to_raw:
                    internal_norm_to_raw[norm_loc] = clean_loc
                    internal_loc_urls.append(norm_loc)

    return {
        "fetched_sitemap_urls": fetched_sitemap_urls,
        "valid_sitemap_urls": valid_sitemap_urls,
        "missing_sitemap_urls": missing_sitemap_urls,
        "invalid_sitemaps": invalid_sitemaps,
        "internal_loc_urls": internal_loc_urls,
        "internal_norm_to_raw": internal_norm_to_raw,
        "external_loc_urls": external_loc_urls,
    }


def detect_sitemap_and_robots_issues(
    results: list[dict[str, Any]],
    start_url: str,
    robots_url: str | None = None,
    robots_status_code: int | None = 200,
    robots_text: str | None = None,
    robot_parser: RobotFileParser | None = None,
    headers: dict[str, str] | None = None,
    page_link_data: dict[str, dict[str, Any]] | None = None,
    checked_redirects: dict[str, dict[str, Any] | None] | None = None,
    initial_link_hops: dict[str, tuple[int | None, str | None, str | None]]
    | None = None,
    link_results: list[dict[str, Any]] | None = None,
    user_agent: str = "CrawlScope/0.1 (website auditing learning project)",
) -> dict[str, int]:
    """Perform the XML Sitemap & Robots.txt Audit and update page results with findings and coverage."""
    for page in results:
        page.setdefault("in_sitemap", False)

    html_pages = [
        page
        for page in results
        if page.get("error") is None and is_html_content(page.get("content_type"))
    ]
    if not html_pages:
        return {
            "sitemap_urls_count": 0,
            "crawled_urls_count": 0,
            "missing_from_sitemap_count": 0,
            "sitemap_not_crawled_count": 0,
        }

    if headers is None:
        headers = {"User-Agent": user_agent}
    if page_link_data is None:
        page_link_data = {}
    if checked_redirects is None:
        checked_redirects = {}
    if initial_link_hops is None:
        initial_link_hops = {}
    if link_results is None:
        link_results = []

    clean_start_url, _ = urldefrag(start_url)
    parsed_start = urlparse(clean_start_url)
    target_hostname = parsed_start.hostname
    if not robots_url:
        robots_url = f"{parsed_start.scheme}://{target_hostname}/robots.txt"
    default_sitemap_url = (
        f"{parsed_start.scheme}://{target_hostname}/sitemap.xml"
    )

    primary_page = html_pages[0]
    primary_url = primary_page["url"]
    primary_issues = primary_page.setdefault("seo_issues", [])
    primary_issue_types = {issue.get("type") for issue in primary_issues}

    # 1. Analyze robots.txt availability and syntax
    robots_available = (
        robots_status_code is not None and 200 <= robots_status_code < 300
    )
    robots_parsed = _parse_and_validate_robots_txt(
        robots_text if robots_available else None,
        robots_url,
    )
    robots_sitemaps = robots_parsed["sitemap_urls"]
    invalid_robots_lines = robots_parsed["invalid_lines"]

    if not robots_available and "missing_robots_txt" not in primary_issue_types:
        primary_issue_types.add("missing_robots_txt")
        status_label = (
            f"HTTP {robots_status_code}"
            if robots_status_code is not None
            else "unreachable"
        )
        primary_issues.append(
            {
                "type": "missing_robots_txt",
                "severity": "warning",
                "message": (
                    f"Website robots.txt file is unavailable at {robots_url} "
                    f"({status_label}); add a valid robots.txt file to guide "
                    f"search engine crawlers and declare your XML sitemap."
                ),
                "url": primary_url,
                "robots_txt_url": robots_url,
                "status_code": robots_status_code,
            }
        )

    if (
        robots_available
        and invalid_robots_lines
        and "invalid_robots_txt" not in primary_issue_types
    ):
        primary_issue_types.add("invalid_robots_txt")
        primary_issues.append(
            {
                "type": "invalid_robots_txt",
                "severity": "warning",
                "message": (
                    f"robots.txt at {robots_url} contains {len(invalid_robots_lines)} "
                    f"parsing issue(s) ({'; '.join(invalid_robots_lines[:3])}); "
                    f"fix directive syntax so search engines interpret crawl rules accurately."
                ),
                "url": primary_url,
                "robots_txt_url": robots_url,
                "invalid_lines": list(invalid_robots_lines),
            }
        )

    #Build an auxiliary RobotFileParser from robots_text if needed so we can check '*' and 'Googlebot' rules
    audit_robot_parser = robot_parser
    if audit_robot_parser is None and robots_available and robots_text:
        audit_robot_parser = RobotFileParser()
        audit_robot_parser.set_url(robots_url)
        audit_robot_parser.parse(robots_text.splitlines())

    # 2. Check if any crawled pages are disallowed by robots.txt rules (robots_blocked_crawled_url)
    for page in html_pages:
        page_url = page["url"]
        if _is_url_blocked_by_robots(page_url, audit_robot_parser, user_agent):
            p_issues = page.setdefault("seo_issues", [])
            p_types = {i.get("type") for i in p_issues}
            if "robots_blocked_crawled_url" not in p_types:
                p_types.add("robots_blocked_crawled_url")
                p_issues.append(
                    {
                        "type": "robots_blocked_crawled_url",
                        "severity": "warning",
                        "message": (
                            f"Crawled page {page_url} is disallowed by rules in "
                            f"{robots_url}; update robots.txt if this page should "
                            f"be crawlable by search engines."
                        ),
                        "url": page_url,
                        "robots_txt_url": robots_url,
                        "directive_source": "robots_txt",
                    }
                )

    # 3. Discover and fetch XML sitemaps
    html_declared_sitemaps: list[str] = []
    for page in html_pages:
        info = page_link_data.get(page["url"], {})
        for sm_link in info.get("sitemap_link_urls", []):
            if sm_link not in html_declared_sitemaps:
                html_declared_sitemaps.append(sm_link)

    candidate_sitemaps: list[str] = []
    for sm in robots_sitemaps + html_declared_sitemaps:
        if sm not in candidate_sitemaps:
            candidate_sitemaps.append(sm)

    if not candidate_sitemaps:
        candidate_sitemaps.append(default_sitemap_url)

    sitemap_data = _fetch_and_parse_sitemaps(
        candidate_sitemap_urls=candidate_sitemaps,
        target_hostname=target_hostname,
        headers=headers,
    )

    valid_sitemap_urls = sitemap_data["valid_sitemap_urls"]
    invalid_sitemaps = sitemap_data["invalid_sitemaps"]
    missing_sitemap_urls = sitemap_data["missing_sitemap_urls"]
    internal_loc_norms: list[str] = sitemap_data["internal_loc_urls"]
    internal_norm_to_raw: dict[str, str] = sitemap_data["internal_norm_to_raw"]

    # Report invalid_sitemap for any malformed sitemaps
    for bad_sm_url, reason in invalid_sitemaps:
        primary_issues.append(
            {
                "type": "invalid_sitemap",
                "severity": "warning",
                "message": (
                    f"XML sitemap at {bad_sm_url} could not be parsed ({reason}); "
                    f"ensure the file is valid XML with <urlset> or <sitemapindex> and valid <loc> tags."
                ),
                "url": primary_url,
                "sitemap_url": bad_sm_url,
            }
        )
        primary_issue_types.add("invalid_sitemap")

    # Report missing_sitemap if no valid sitemap was found and no invalid_sitemap was already reported,
    # or if explicitly declared sitemaps were missing (404/unreachable)
    if not valid_sitemap_urls and not invalid_sitemaps:
        checked_url = (
            missing_sitemap_urls[0][0]
            if missing_sitemap_urls
            else default_sitemap_url
        )
        checked_status = (
            missing_sitemap_urls[0][1] if missing_sitemap_urls else None
        )
        if "missing_sitemap" not in primary_issue_types:
            primary_issue_types.add("missing_sitemap")
            primary_issues.append(
                {
                    "type": "missing_sitemap",
                    "severity": "warning",
                    "message": (
                        f"No discoverable XML sitemap was found (checked {checked_url}); "
                        f"create an XML sitemap and reference it in robots.txt."
                    ),
                    "url": primary_url,
                    "sitemap_url": checked_url,
                    "status_code": checked_status,
                }
            )
    elif valid_sitemap_urls and missing_sitemap_urls:
        # A child sitemap in a sitemapindex or a second declared sitemap was missing
        for missing_sm_url, missing_status in missing_sitemap_urls:
            primary_issues.append(
                {
                    "type": "invalid_sitemap",
                    "severity": "warning",
                    "message": (
                        f"Referenced XML sitemap at {missing_sm_url} is unreachable "
                        f"(HTTP {missing_status or 'ERR'}); remove broken sitemap references "
                        f"or restore the missing sitemap file."
                    ),
                    "url": primary_url,
                    "sitemap_url": missing_sm_url,
                    "status_code": missing_status,
                }
            )
            primary_issue_types.add("invalid_sitemap")

    # Report robots_sitemap_missing if a valid sitemap was discovered, robots.txt is available,
    # but robots.txt does not declare any Sitemap: directive
    if (
        valid_sitemap_urls
        and robots_available
        and not robots_sitemaps
        and "robots_sitemap_missing" not in primary_issue_types
    ):
        primary_issue_types.add("robots_sitemap_missing")
        primary_issues.append(
            {
                "type": "robots_sitemap_missing",
                "severity": "warning",
                "message": (
                    f"XML sitemap ({valid_sitemap_urls[0]}) is discoverable, but "
                    f"{robots_url} does not declare a 'Sitemap:' directive; add "
                    f"'Sitemap: {valid_sitemap_urls[0]}' to robots.txt."
                ),
                "url": primary_url,
                "robots_txt_url": robots_url,
                "sitemap_url": valid_sitemap_urls[0],
            }
        )

    # Map normalized crawled URLs to page dicts
    norm_to_crawled_page: dict[str, dict[str, Any]] = {}
    norm_to_any_page: dict[str, dict[str, Any]] = {}
    for page in results:
        norm_p = _normalize_internal_url(page["url"])
        norm_to_any_page[norm_p] = page
        if page.get("error") is None and is_html_content(page.get("content_type")):
            norm_to_crawled_page[norm_p] = page

    sitemap_norm_set = set(internal_loc_norms)
    for page in html_pages:
        norm_p = _normalize_internal_url(page["url"])
        page["in_sitemap"] = norm_p in sitemap_norm_set

    # Map normalized redirecting URLs and broken links from already collected crawl data
    norm_redirects: dict[str, dict[str, Any]] = {}
    for src_url, redir_info in checked_redirects.items():
        if redir_info is not None:
            norm_redirects[_normalize_internal_url(src_url)] = redir_info
    for src_url, hop_tuple in initial_link_hops.items():
        hop_status, hop_location, _ = hop_tuple
        norm_src = _normalize_internal_url(src_url)
        if (
            norm_src not in norm_redirects
            and hop_status in (301, 302, 303, 307, 308)
            and hop_location
        ):
            dest_abs, _ = urldefrag(urljoin(src_url, hop_location))
            norm_redirects[norm_src] = {
                "source_url": src_url,
                "destination_url": dest_abs,
                "status_code": hop_status,
            }

    norm_broken_links: dict[str, int | None] = {}
    for link_item in link_results:
        if link_item.get("result") == "broken":
            norm_broken_links[_normalize_internal_url(link_item["url"])] = (
                link_item.get("status_code")
            )

    # 4. Inspect each internal URL in the valid sitemap(s)
    uncrawled_sitemap_urls: list[str] = []
    blocked_sitemap_urls: list[str] = []

    for norm_loc in internal_loc_norms:
        raw_loc = internal_norm_to_raw.get(norm_loc, norm_loc)
        crawled_page = norm_to_crawled_page.get(norm_loc)
        any_page = norm_to_any_page.get(norm_loc)

        is_blocked = _is_url_blocked_by_robots(
            raw_loc, audit_robot_parser, user_agent
        )
        if is_blocked:
            blocked_sitemap_urls.append(raw_loc)
            target_page = crawled_page if crawled_page is not None else primary_page
            t_issues = target_page.setdefault("seo_issues", [])
            t_issues.append(
                {
                    "type": "robots_blocked_sitemap_url",
                    "severity": "warning",
                    "message": (
                        f"Sitemap URL {raw_loc} is blocked by robots.txt disallow "
                        f"rules; remove the URL from the XML sitemap or allow it in robots.txt."
                    ),
                    "url": target_page["url"],
                    "sitemap_url": raw_loc,
                    "robots_txt_url": robots_url,
                }
            )

        if crawled_page is None:
            # Check if it was attempted and failed (e.g. HTTP 404/500 in results) or broken in link_results
            failed_status: int | None = None
            any_status = (
                any_page.get("status_code") if any_page is not None else None
            )
            if any_page is not None and (
                (any_status is not None and any_status >= 400)
                or (any_status is None and any_page.get("error") is not None)
            ):
                failed_status = any_status
            elif norm_loc in norm_broken_links:
                failed_status = norm_broken_links[norm_loc]

            if failed_status is not None:
                primary_issues.append(
                    {
                        "type": "sitemap_non_indexable_url",
                        "severity": "warning",
                        "message": (
                            f"Sitemap URL {raw_loc} returned HTTP {failed_status} "
                            f"and is non-indexable; remove broken URLs from the XML sitemap."
                        ),
                        "url": primary_url,
                        "sitemap_url": raw_loc,
                        "status_code": failed_status,
                    }
                )
            elif norm_loc in norm_redirects:
                redir = norm_redirects[norm_loc]
                dest_url = redir.get("destination_url")
                primary_issues.append(
                    {
                        "type": "sitemap_noncanonical_url",
                        "severity": "warning",
                        "message": (
                            f"Sitemap URL {raw_loc} redirects to {dest_url}; "
                            f"replace redirecting URLs in the XML sitemap with the final canonical destination URL."
                        ),
                        "url": primary_url,
                        "sitemap_url": raw_loc,
                        "source_url": raw_loc,
                        "destination_url": dest_url,
                        "canonical_url": dest_url,
                    }
                )

            if any_page is None and not is_blocked:
                uncrawled_sitemap_urls.append(raw_loc)
            continue

        # The sitemap URL was crawled as an HTML page: check indexability and canonical alignment
        page_issues = crawled_page.setdefault("seo_issues", [])
        page_issue_types = {i.get("type") for i in page_issues}

        # Check sitemap_non_indexable_url (marked noindex or non-200 status)
        has_noindex = "noindex_page" in page_issue_types
        page_status = crawled_page.get("status_code")
        is_error_status = page_status is not None and page_status >= 400

        if (
            (has_noindex or is_error_status)
            and "sitemap_non_indexable_url" not in page_issue_types
        ):
            page_issue_types.add("sitemap_non_indexable_url")
            noindex_issue = next(
                (i for i in page_issues if i.get("type") == "noindex_page"),
                None,
            )
            directive_desc = (
                noindex_issue.get("robots_directive")
                if noindex_issue
                else f"HTTP {page_status}"
            )
            page_issues.append(
                {
                    "type": "sitemap_non_indexable_url",
                    "severity": "warning",
                    "message": (
                        f"Sitemap URL {raw_loc} is marked non-indexable "
                        f"({directive_desc}); remove non-indexable pages from "
                        f"the XML sitemap or make the page indexable."
                    ),
                    "url": crawled_page["url"],
                    "sitemap_url": raw_loc,
                    "robots_directive": directive_desc,
                }
            )

        # Check sitemap_noncanonical_url (page canonical points to a different URL or redirects)
        info = page_link_data.get(crawled_page["url"], {})
        resolved_canon = info.get("resolved_canonical_url") or info.get(
            "canonical_url"
        )
        has_canon_redirect = "canonical_redirect" in page_issue_types
        has_cross_domain_canon = "cross_domain_canonical" in page_issue_types

        is_noncanonical = False
        target_canon_url: str | None = resolved_canon
        if resolved_canon and _normalize_internal_url(resolved_canon) != norm_loc:
            is_noncanonical = True
        elif has_canon_redirect or has_cross_domain_canon:
            is_noncanonical = True
            for existing_i in page_issues:
                if existing_i.get("type") in (
                    "canonical_redirect",
                    "cross_domain_canonical",
                ):
                    target_canon_url = existing_i.get(
                        "destination_url"
                    ) or existing_i.get("canonical_url")
                    break
        elif norm_loc in norm_redirects or page_status in (
            301,
            302,
            303,
            307,
            308,
        ):
            is_noncanonical = True
            target_canon_url = (
                norm_redirects[norm_loc].get("destination_url")
                if norm_loc in norm_redirects
                else resolved_canon
            )

        if is_noncanonical and "sitemap_noncanonical_url" not in page_issue_types:
            page_issue_types.add("sitemap_noncanonical_url")
            page_issues.append(
                {
                    "type": "sitemap_noncanonical_url",
                    "severity": "warning",
                    "message": (
                        f"Sitemap URL {raw_loc} points to a non-canonical page "
                        f"(canonical URL is {target_canon_url}); include only "
                        f"self-canonical URLs in the XML sitemap."
                    ),
                    "url": crawled_page["url"],
                    "sitemap_url": raw_loc,
                    "source_url": raw_loc,
                    "destination_url": target_canon_url,
                    "canonical_url": target_canon_url,
                }
            )

    if uncrawled_sitemap_urls and "sitemap_url_not_crawled" not in primary_issue_types:
        primary_issue_types.add("sitemap_url_not_crawled")
        preview_urls = ", ".join(uncrawled_sitemap_urls[:5])
        primary_issues.append(
            {
                "type": "sitemap_url_not_crawled",
                "severity": "warning",
                "message": (
                    f"{len(uncrawled_sitemap_urls)} URL(s) listed in the XML sitemap "
                    f"were not discovered or crawled during the crawl ({preview_urls}); "
                    f"add internal links to these pages so crawlers and users can reach them."
                ),
                "url": primary_url,
                "sitemap_url": uncrawled_sitemap_urls[0],
                "sitemap_urls": list(uncrawled_sitemap_urls),
                "sitemap_not_crawled_count": len(uncrawled_sitemap_urls),
            }
        )

    # 5. Check crawled HTML pages missing from valid XML sitemap (crawled_url_missing_from_sitemap)
    missing_from_sitemap_count = 0
    if valid_sitemap_urls:
        for page in html_pages:
            page_url = page["url"]
            norm_p = _normalize_internal_url(page_url)
            if norm_p in sitemap_norm_set:
                continue

            page_issues = page.setdefault("seo_issues", [])
            page_issue_types = {i.get("type") for i in page_issues}

            # Skip pages that should NOT be in the sitemap (non-indexable, blocked by robots, or non-canonical)
            if (
                "noindex_page" in page_issue_types
                or "robots_blocked_crawled_url" in page_issue_types
                or "cross_domain_canonical" in page_issue_types
                or "canonical_redirect" in page_issue_types
            ):
                continue

            info = page_link_data.get(page_url, {})
            resolved_canon = info.get("resolved_canonical_url") or info.get(
                "canonical_url"
            )
            if resolved_canon and _normalize_internal_url(resolved_canon) != norm_p:
                continue

            missing_from_sitemap_count += 1
            if "crawled_url_missing_from_sitemap" not in page_issue_types:
                page_issue_types.add("crawled_url_missing_from_sitemap")
                page_issues.append(
                    {
                        "type": "crawled_url_missing_from_sitemap",
                        "severity": "warning",
                        "message": (
                            f"Crawled indexable HTML page {page_url} is missing "
                            f"from the XML sitemap ({valid_sitemap_urls[0]}); "
                            f"add this URL to your sitemap to improve search engine discovery."
                        ),
                        "url": page_url,
                        "sitemap_url": valid_sitemap_urls[0],
                    }
                )
    else:
        missing_from_sitemap_count = len(html_pages)

    sitemap_not_crawled_count = sum(
        1 for norm_loc in internal_loc_norms if norm_loc not in norm_to_crawled_page
    )
    coverage = {
        "sitemap_urls_count": len(internal_loc_norms),
        "crawled_urls_count": len(html_pages),
        "missing_from_sitemap_count": missing_from_sitemap_count,
        "sitemap_not_crawled_count": sitemap_not_crawled_count,
    }
    return coverage


def summarize_sitemap_coverage(
    results: list[dict[str, Any]],
) -> dict[str, int]:
    """Calculate sitemap coverage summary statistics from crawled page results."""
    html_pages = [
        page
        for page in results
        if page.get("error") is None and is_html_content(page.get("content_type"))
    ]
    crawled_urls_count = len(html_pages)
    crawled_norms = {_normalize_internal_url(p["url"]) for p in html_pages}

    in_sitemap_crawled_count = sum(
        1 for p in html_pages if p.get("in_sitemap") is True
    )
    missing_issue_pages = sum(
        1
        for p in html_pages
        if any(
            i.get("type") == "crawled_url_missing_from_sitemap"
            for i in p.get("seo_issues", [])
        )
    )
    has_missing_sitemap_issue = any(
        i.get("type") in ("missing_sitemap", "invalid_sitemap")
        for p in html_pages
        for i in p.get("seo_issues", [])
    )
    if missing_issue_pages > 0:
        missing_from_sitemap_count = missing_issue_pages
    elif has_missing_sitemap_issue and in_sitemap_crawled_count == 0:
        missing_from_sitemap_count = crawled_urls_count
    else:
        missing_from_sitemap_count = sum(
            1
            for p in html_pages
            if p.get("in_sitemap") is False
            and not any(
                i.get("type")
                in (
                    "noindex_page",
                    "robots_blocked_crawled_url",
                    "cross_domain_canonical",
                    "canonical_redirect",
                )
                for i in p.get("seo_issues", [])
            )
        )

    uncrawled_norms: set[str] = set()
    for page in results:
        for issue in page.get("seo_issues", []):
            issue_type = issue.get("type")
            if issue_type == "sitemap_url_not_crawled":
                for u in issue.get("sitemap_urls") or (
                    [issue["sitemap_url"]] if issue.get("sitemap_url") else []
                ):
                    norm_u = _normalize_internal_url(u)
                    if norm_u not in crawled_norms:
                        uncrawled_norms.add(norm_u)
            elif issue_type in (
                "robots_blocked_sitemap_url",
                "sitemap_non_indexable_url",
                "sitemap_noncanonical_url",
            ):
                sm_u = issue.get("sitemap_url")
                if sm_u:
                    norm_u = _normalize_internal_url(sm_u)
                    if norm_u not in crawled_norms:
                        uncrawled_norms.add(norm_u)

    sitemap_not_crawled_count = len(uncrawled_norms)
    sitemap_urls_count = in_sitemap_crawled_count + sitemap_not_crawled_count

    return {
        "sitemap_urls_count": sitemap_urls_count,
        "crawled_urls_count": crawled_urls_count,
        "missing_from_sitemap_count": missing_from_sitemap_count,
        "sitemap_not_crawled_count": sitemap_not_crawled_count,
    }


SECURITY_ISSUE_TYPES = {
    "http_page",
    "http_internal_link",
    "mixed_content",
    "insecure_form_action",
    "missing_hsts",
    "missing_content_security_policy",
    "missing_x_content_type_options",
    "missing_x_frame_options",
    "missing_referrer_policy",
    "missing_permissions_policy",
    "insecure_external_resource",
    "sensitive_form_over_http",
}

MISSING_SECURITY_HEADER_ISSUE_TYPES = {
    "missing_hsts",
    "missing_content_security_policy",
    "missing_x_content_type_options",
    "missing_x_frame_options",
    "missing_referrer_policy",
    "missing_permissions_policy",
}

_SUBRESOURCE_LINK_RELS = {
    "stylesheet",
    "preload",
    "modulepreload",
    "icon",
    "shortcut",
    "apple-touch-icon",
    "manifest",
    "prefetch",
}

_SENSITIVE_AUTOCOMPLETE_TOKENS = {
    "current-password",
    "new-password",
    "cc-number",
    "cc-csc",
    "cc-exp",
    "cc-exp-month",
    "cc-exp-year",
    "one-time-code",
}

_SENSITIVE_FIELD_TOKENS = {
    "password",
    "passwd",
    "passcode",
    "pin",
    "cvv",
    "cvc",
    "csc",
    "ssn",
    "secret",
    "token",
    "cardnumber",
    "creditcard",
}

_SENSITIVE_FIELD_SUBSTRINGS = (
    "password",
    "passwd",
    "passcode",
    "credit_card",
    "credit-card",
    "card_number",
    "card-number",
)


def _normalize_response_headers(
    response_headers: Any,
) -> dict[str, list[str]]:
    """Normalize HTTP response header names case-insensitively to non-empty value lists."""
    normalized: dict[str, list[str]] = {}
    if response_headers is None:
        return normalized

    items: list[tuple[Any, Any]] = []
    if hasattr(response_headers, "multi_items"):
        items = list(response_headers.multi_items())
    elif isinstance(response_headers, dict):
        for k, v in response_headers.items():
            if isinstance(v, (list, tuple)):
                for sub_v in v:
                    items.append((k, sub_v))
            else:
                items.append((k, v))
    elif isinstance(response_headers, (list, tuple)):
        for entry in response_headers:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                items.append((entry[0], entry[1]))

    for key, val in items:
        if key is None or val is None:
            continue
        k_lower = str(key).strip().lower()
        v_clean = str(val).strip()
        if k_lower and v_clean:
            normalized.setdefault(k_lower, []).append(v_clean)

    return normalized


def _has_csp_frame_ancestors(norm_headers: dict[str, list[str]]) -> bool:
    """Return True if any Content-Security-Policy header declares a valid frame-ancestors directive."""
    csp_values = norm_headers.get("content-security-policy", [])
    for csp_val in csp_values:
        for raw_directive in csp_val.split(";"):
            parts = raw_directive.strip().split()
            if len(parts) >= 2 and parts[0].lower() == "frame-ancestors":
                return True
    return False


def _extract_srcset_urls(srcset_value: str) -> list[str]:
    """Extract candidate URLs from an HTML srcset attribute."""
    urls: list[str] = []
    for part in srcset_value.split(","):
        tokens = part.strip().split()
        if tokens and tokens[0]:
            urls.append(tokens[0])
    return urls


def _extract_subresource_urls(soup: BeautifulSoup) -> list[str]:
    """Extract embedded subresource URLs (scripts, stylesheets, images, iframes, media) from HTML."""
    raw_urls: list[str] = []

    for script_tag in soup.find_all("script"):
        src = script_tag.get("src")
        if isinstance(src, str) and src.strip():
            raw_urls.append(src.strip())

    for link_tag in soup.find_all("link"):
        href = link_tag.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        rel_tokens = _extract_rel_tokens(link_tag)
        if rel_tokens & _SUBRESOURCE_LINK_RELS:
            raw_urls.append(href.strip())

    for img_tag in soup.find_all("img"):
        src = img_tag.get("src")
        if isinstance(src, str) and src.strip():
            raw_urls.append(src.strip())
        srcset = img_tag.get("srcset")
        if isinstance(srcset, str) and srcset.strip():
            raw_urls.extend(_extract_srcset_urls(srcset))

    for tag_name in ("iframe", "audio", "video", "source", "embed"):
        for media_tag in soup.find_all(tag_name):
            src = media_tag.get("src")
            if isinstance(src, str) and src.strip():
                raw_urls.append(src.strip())
            if tag_name == "video":
                poster = media_tag.get("poster")
                if isinstance(poster, str) and poster.strip():
                    raw_urls.append(poster.strip())
            if tag_name == "source":
                srcset = media_tag.get("srcset")
                if isinstance(srcset, str) and srcset.strip():
                    raw_urls.extend(_extract_srcset_urls(srcset))

    for obj_tag in soup.find_all("object"):
        data_attr = obj_tag.get("data")
        if isinstance(data_attr, str) and data_attr.strip():
            raw_urls.append(data_attr.strip())

    return raw_urls


def _tokenize_identifier(text: str) -> set[str]:
    """Split an attribute identifier into lowercase alphanumeric tokens."""
    cleaned = []
    for ch in text.lower():
        cleaned.append(ch if ch.isalnum() else " ")
    return set("".join(cleaned).split())


def _is_sensitive_form(form: Any) -> bool:
    """Return True if a <form> appears to collect passwords, credentials, or payment details."""
    for input_tag in form.find_all("input"):
        input_type = (input_tag.get("type") or "text").strip().lower()
        if input_type in ("hidden", "submit", "button", "reset", "image"):
            continue
        if input_type == "password":
            return True

        autocomplete = (input_tag.get("autocomplete") or "").strip().lower()
        if autocomplete:
            ac_tokens = set(autocomplete.split())
            if ac_tokens & _SENSITIVE_AUTOCOMPLETE_TOKENS:
                return True

        for attr_name in ("name", "id"):
            raw_val = (input_tag.get(attr_name) or "").strip().lower()
            if not raw_val:
                continue
            if any(sub in raw_val for sub in _SENSITIVE_FIELD_SUBSTRINGS):
                return True
            if _tokenize_identifier(raw_val) & _SENSITIVE_FIELD_TOKENS:
                return True

    return False


def detect_security_seo_issues(
    url: str,
    soup: BeautifulSoup | None,
    content_type: str | None,
    response_headers: Any = None,
    target_hostname: str | None = None,
    https_expected: bool = False,
) -> list[dict[str, Any]]:
    """Run passive Website Security & HTTPS audit checks on a crawled HTML page."""
    if not is_html_content(content_type) or soup is None:
        return []

    issues: list[dict[str, Any]] = []
    parsed_page = urlparse(url)
    page_scheme = (parsed_page.scheme or "").lower()
    is_https_page = page_scheme == "https"
    is_http_page = page_scheme == "http"
    page_host_fold = (
        target_hostname or parsed_page.hostname or ""
    ).casefold()

    norm_headers = _normalize_response_headers(response_headers)

    # 1. http_page: Crawled page is served over HTTP instead of HTTPS
    if is_http_page:
        issues.append(
            {
                "type": "http_page",
                "severity": "warning",
                "message": (
                    f"Page is served over unencrypted HTTP ({url}); serve pages "
                    f"over HTTPS and redirect HTTP traffic to HTTPS to protect transport security."
                ),
                "url": url,
            }
        )

    # 2. http_internal_link: Internal link points to an HTTP URL when HTTPS is available or expected
    http_internal_links: list[str] = []
    seen_http_internal: set[str] = set()
    for a_tag in soup.find_all("a"):
        raw_href = a_tag.get("href")
        if not isinstance(raw_href, str):
            continue
        stripped_href = raw_href.strip()
        if not stripped_href or stripped_href.startswith(
            ("#", "mailto:", "tel:", "javascript:", "data:")
        ):
            continue

        is_explicit_http = stripped_href.lower().startswith("http://")
        if not (is_https_page or https_expected or is_explicit_http):
            continue

        abs_href, _ = urldefrag(urljoin(url, stripped_href))
        parsed_href = urlparse(abs_href)
        if (
            parsed_href.scheme.lower() == "http"
            and (parsed_href.hostname or "").casefold() == page_host_fold
        ):
            norm_href = _normalize_internal_url(abs_href)
            if norm_href not in seen_http_internal:
                seen_http_internal.add(norm_href)
                http_internal_links.append(abs_href)

    if http_internal_links:
        issues.append(
            {
                "type": "http_internal_link",
                "severity": "warning",
                "message": (
                    f"Page contains {len(http_internal_links)} internal link(s) pointing "
                    f"to HTTP URL(s) ({', '.join(http_internal_links[:5])}); update "
                    f"internal links to HTTPS so users and crawlers avoid unnecessary insecure hops."
                ),
                "url": url,
                "insecure_urls": list(http_internal_links),
            }
        )

    # 3. mixed_content & 11. insecure_external_resource (only on HTTPS pages)
    if is_https_page:
        mixed_content_urls: list[str] = []
        seen_mixed: set[str] = set()
        insecure_external_urls: list[str] = []
        seen_external: set[str] = set()

        for raw_res in _extract_subresource_urls(soup):
            if raw_res.startswith(("#", "data:", "blob:", "javascript:", "about:")):
                continue
            abs_res, _ = urldefrag(urljoin(url, raw_res))
            parsed_res = urlparse(abs_res)
            if parsed_res.scheme.lower() != "http" or not parsed_res.hostname:
                continue

            if abs_res not in seen_mixed:
                seen_mixed.add(abs_res)
                mixed_content_urls.append(abs_res)

            res_host_fold = (parsed_res.hostname or "").casefold()
            if res_host_fold != page_host_fold and abs_res not in seen_external:
                seen_external.add(abs_res)
                insecure_external_urls.append(abs_res)

        if mixed_content_urls:
            issues.append(
                {
                    "type": "mixed_content",
                    "severity": "warning",
                    "message": (
                        f"HTTPS page references {len(mixed_content_urls)} insecure HTTP "
                        f"subresource(s) ({', '.join(mixed_content_urls[:5])}); update all "
                        f"embedded images, scripts, stylesheets, and iframes to HTTPS."
                    ),
                    "url": url,
                    "insecure_urls": list(mixed_content_urls),
                }
            )

        if insecure_external_urls:
            issues.append(
                {
                    "type": "insecure_external_resource",
                    "severity": "warning",
                    "message": (
                        f"HTTPS page loads {len(insecure_external_urls)} external "
                        f"resource(s) over HTTP ({', '.join(insecure_external_urls[:5])}); "
                        f"load third-party resources over HTTPS to prevent tampering."
                    ),
                    "url": url,
                    "insecure_urls": list(insecure_external_urls),
                }
            )

    # 4. insecure_form_action & 12. sensitive_form_over_http
    insecure_form_actions: list[str] = []
    seen_insecure_actions: set[str] = set()
    sensitive_http_actions: list[str] = []
    seen_sensitive_actions: set[str] = set()

    for form_tag in soup.find_all("form"):
        raw_action = (form_tag.get("action") or "").strip()
        if raw_action.lower().startswith("javascript:"):
            continue
        if raw_action:
            resolved_action, _ = urldefrag(urljoin(url, raw_action))
        else:
            resolved_action = url

        parsed_action = urlparse(resolved_action)
        action_is_http = parsed_action.scheme.lower() == "http"

        if is_https_page and action_is_http:
            if resolved_action not in seen_insecure_actions:
                seen_insecure_actions.add(resolved_action)
                insecure_form_actions.append(resolved_action)

        if action_is_http and _is_sensitive_form(form_tag):
            if resolved_action not in seen_sensitive_actions:
                seen_sensitive_actions.add(resolved_action)
                sensitive_http_actions.append(resolved_action)

    if insecure_form_actions:
        issues.append(
            {
                "type": "insecure_form_action",
                "severity": "warning",
                "message": (
                    f"HTTPS page contains a form submitting data to an insecure HTTP "
                    f"endpoint ({', '.join(insecure_form_actions)}); use an HTTPS form "
                    f"action URL so submitted data is encrypted in transit."
                ),
                "url": url,
                "form_action": insecure_form_actions[0],
                "insecure_urls": list(insecure_form_actions),
            }
        )

    if sensitive_http_actions:
        issues.append(
            {
                "type": "sensitive_form_over_http",
                "severity": "warning",
                "message": (
                    f"Form collecting credentials or sensitive input submits over "
                    f"unencrypted HTTP ({', '.join(sensitive_http_actions)}); serve "
                    f"both the page and the form submission endpoint over HTTPS."
                ),
                "url": url,
                "form_action": sensitive_http_actions[0],
                "insecure_urls": list(sensitive_http_actions),
            }
        )

    # 5. missing_hsts (HTTPS responses only)
    if is_https_page and "strict-transport-security" not in norm_headers:
        issues.append(
            {
                "type": "missing_hsts",
                "severity": "warning",
                "message": (
                    "HTTPS response lacks a Strict-Transport-Security (HSTS) header; "
                    "add 'Strict-Transport-Security: max-age=31536000; includeSubDomains' "
                    "so browsers enforce HTTPS connections."
                ),
                "url": url,
                "missing_header": "Strict-Transport-Security",
            }
        )

    # 6. missing_content_security_policy
    if "content-security-policy" not in norm_headers:
        issues.append(
            {
                "type": "missing_content_security_policy",
                "severity": "warning",
                "message": (
                    "Response lacks a Content-Security-Policy header; configure a "
                    "Content-Security-Policy header to restrict permitted script and "
                    "resource origins."
                ),
                "url": url,
                "missing_header": "Content-Security-Policy",
            }
        )

    # 7. missing_x_content_type_options
    if "x-content-type-options" not in norm_headers:
        issues.append(
            {
                "type": "missing_x_content_type_options",
                "severity": "warning",
                "message": (
                    "Response lacks an X-Content-Type-Options header; add "
                    "'X-Content-Type-Options: nosniff' to prevent MIME-type sniffing."
                ),
                "url": url,
                "missing_header": "X-Content-Type-Options",
            }
        )

    # 8. missing_x_frame_options (satisfied by X-Frame-Options or CSP frame-ancestors)
    if (
        "x-frame-options" not in norm_headers
        and not _has_csp_frame_ancestors(norm_headers)
    ):
        issues.append(
            {
                "type": "missing_x_frame_options",
                "severity": "warning",
                "message": (
                    "Response lacks X-Frame-Options and an equivalent CSP frame-ancestors "
                    "directive; add 'X-Frame-Options: SAMEORIGIN' or a CSP 'frame-ancestors' "
                    "policy to control framing."
                ),
                "url": url,
                "missing_header": "X-Frame-Options",
            }
        )

    # 9. missing_referrer_policy
    if "referrer-policy" not in norm_headers:
        issues.append(
            {
                "type": "missing_referrer_policy",
                "severity": "info",
                "message": (
                    "Response lacks a Referrer-Policy header; add "
                    "'Referrer-Policy: strict-origin-when-cross-origin' to control "
                    "referrer data sent to external sites."
                ),
                "url": url,
                "missing_header": "Referrer-Policy",
            }
        )

    # 10. missing_permissions_policy
    if "permissions-policy" not in norm_headers:
        issues.append(
            {
                "type": "missing_permissions_policy",
                "severity": "info",
                "message": (
                    "Response lacks a Permissions-Policy header; add a Permissions-Policy "
                    "header to explicitly restrict powerful browser features."
                ),
                "url": url,
                "missing_header": "Permissions-Policy",
            }
        )

    return issues


def summarize_security_audit(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate security & HTTPS summary statistics from crawled page results."""
    crawled_pages = [
        page for page in results if page.get("error") is None
    ]
    https_pages_count = sum(
        1
        for p in crawled_pages
        if p.get("is_https") is True
        or (
            p.get("is_https") is None
            and urlparse(p.get("url", "")).scheme.lower() == "https"
        )
    )
    http_pages_count = sum(
        1
        for p in crawled_pages
        if p.get("is_https") is False
        or (
            p.get("is_https") is None
            and urlparse(p.get("url", "")).scheme.lower() == "http"
        )
    )

    mixed_content_pages_count = 0
    missing_security_headers_pages_count = 0
    total_security_issues = 0
    severity_counts: dict[str, int] = {
        "error": 0,
        "warning": 0,
        "info": 0,
    }

    for page in results:
        issues = page.get("seo_issues", [])
        has_mixed = False
        has_missing_header = False
        for issue in issues:
            issue_type = issue.get("type")
            if issue_type in SECURITY_ISSUE_TYPES:
                total_security_issues += 1
                sev = (issue.get("severity") or "warning").lower()
                if sev in severity_counts:
                    severity_counts[sev] += 1
                else:
                    severity_counts[sev] = 1
                if issue_type == "mixed_content":
                    has_mixed = True
                if issue_type in MISSING_SECURITY_HEADER_ISSUE_TYPES:
                    has_missing_header = True
        if has_mixed:
            mixed_content_pages_count += 1
        if has_missing_header:
            missing_security_headers_pages_count += 1

    return {
        "https_pages_count": https_pages_count,
        "http_pages_count": http_pages_count,
        "mixed_content_pages_count": mixed_content_pages_count,
        "missing_security_headers_pages_count": missing_security_headers_pages_count,
        "total_security_issues": total_security_issues,
        "severity_counts": severity_counts,
    }


# ---------------------------------------------------------------------------
# External Link & Link Safety Audit
# ---------------------------------------------------------------------------

# Set to False to skip all outbound HTTP verification (static checks still run).
EXTERNAL_LINK_VERIFICATION_ENABLED = True
# Maximum number of unique external URLs verified per crawl.
MAX_EXTERNAL_LINK_CHECKS = 50
# Timeout (seconds) for each external verification request.
EXTERNAL_LINK_TIMEOUT_SECONDS = 5.0
# Maximum number of external verification requests running at the same time.
EXTERNAL_LINK_MAX_CONCURRENCY = 5
# Maximum number of redirect hops followed for one external URL.
MAX_EXTERNAL_REDIRECT_HOPS = 5
# A page linking to more than this many unique external destinations is flagged.
EXCESSIVE_EXTERNAL_LINKS_THRESHOLD = 50

EXTERNAL_LINK_ISSUE_TYPES = {
    "broken_external_link",
    "external_redirect",
    "external_link_timeout",
    "external_link_ssl_error",
    "external_link_target_blank_noopener",
    "external_link_target_blank_noreferrer",
    "external_link_to_http",
    "nofollow_external_link",
    "excessive_external_links",
}

_QUALIFYING_REL_VALUES = ("nofollow", "sponsored", "ugc")
_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)
_BLOCKED_HOSTNAME_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")

# Verification statuses: verified outcomes vs. verification failures vs. unverified.
VERIFIED_EXTERNAL_STATUSES = {"working", "broken", "redirect"}
FAILED_EXTERNAL_STATUSES = {"timeout", "ssl_error", "error"}
UNVERIFIED_EXTERNAL_STATUSES = {"blocked", "unverified_limit", "unverified_disabled"}


def _normalize_external_url(url: str) -> str:
    """Normalize an external URL so trivial variants (case, fragment, empty path) dedupe."""
    return _normalize_internal_url(url)


def _extract_external_links(
    page_url: str,
    soup: BeautifulSoup,
    target_hostname: str | None,
) -> list[dict[str, Any]]:
    """Return one entry per unique external <a href> destination found on a page."""
    target_host_fold = (target_hostname or "").casefold()
    links: list[dict[str, Any]] = []
    by_norm: dict[str, dict[str, Any]] = {}

    for a_tag in soup.find_all("a"):
        raw_href = a_tag.get("href")
        if not isinstance(raw_href, str) or not raw_href.strip():
            continue
        abs_url, _ = urldefrag(urljoin(page_url, raw_href.strip()))
        try:
            parsed = urlparse(abs_url)
            hostname = parsed.hostname
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https") or not hostname:
            continue
        if hostname.casefold() == target_host_fold:
            continue

        norm_url = _normalize_external_url(abs_url)
        rel_tokens = _extract_rel_tokens(a_tag)
        target_value = str(a_tag.get("target") or "").strip().lower()

        entry = by_norm.get(norm_url)
        if entry is None:
            entry = {
                "url": abs_url,
                "normalized_url": norm_url,
                "scheme": parsed.scheme.lower(),
                "target_blank_without_noopener": False,
                "target_blank_without_noreferrer": False,
                "qualifying_rel": set(),
            }
            by_norm[norm_url] = entry
            links.append(entry)

        # noreferrer implies noopener in modern browsers.
        if target_value == "_blank":
            if "noopener" not in rel_tokens and "noreferrer" not in rel_tokens:
                entry["target_blank_without_noopener"] = True
            elif "noreferrer" not in rel_tokens:
                entry["target_blank_without_noreferrer"] = True
        for rel_value in _QUALIFYING_REL_VALUES:
            if rel_value in rel_tokens:
                entry["qualifying_rel"].add(rel_value)

    return links


def detect_external_link_seo_issues(
    url: str,
    external_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run static (no-network) external link checks for one page."""
    issues: list[dict[str, Any]] = []

    noopener_urls = [e["url"] for e in external_links if e["target_blank_without_noopener"]]
    noreferrer_urls = [e["url"] for e in external_links if e["target_blank_without_noreferrer"]]
    http_urls = [e["url"] for e in external_links if e["scheme"] == "http"]
    rel_links = [e for e in external_links if e["qualifying_rel"]]

    if noopener_urls:
        issues.append(
            {
                "type": "external_link_target_blank_noopener",
                "severity": "warning",
                "message": (
                    f'{len(noopener_urls)} external link(s) open in a new tab with target="_blank" '
                    f'but without rel="noopener" ({", ".join(noopener_urls[:5])}); add '
                    f'rel="noopener noreferrer" so the opened page cannot control this window.'
                ),
                "url": url,
                "external_urls": noopener_urls,
            }
        )
    if noreferrer_urls:
        issues.append(
            {
                "type": "external_link_target_blank_noreferrer",
                "severity": "info",
                "message": (
                    f'{len(noreferrer_urls)} external link(s) use target="_blank" with '
                    f'rel="noopener" but without rel="noreferrer" ({", ".join(noreferrer_urls[:5])}); '
                    f'consider adding "noreferrer" if you do not want to share referrer data.'
                ),
                "url": url,
                "external_urls": noreferrer_urls,
            }
        )
    if http_urls:
        issues.append(
            {
                "type": "external_link_to_http",
                "severity": "warning",
                "message": (
                    f"{len(http_urls)} outbound link(s) point to insecure HTTP URLs "
                    f"({', '.join(http_urls[:5])}); link to the HTTPS version when available."
                ),
                "url": url,
                "external_urls": http_urls,
            }
        )
    if rel_links:
        rel_values = sorted({value for e in rel_links for value in e["qualifying_rel"]})
        rel_urls = [e["url"] for e in rel_links]
        issues.append(
            {
                "type": "nofollow_external_link",
                "severity": "info",
                "message": (
                    f"{len(rel_urls)} external link(s) are qualified with rel="
                    f"{'/'.join(rel_values)} ({', '.join(rel_urls[:5])}); this is fine for "
                    f"paid or user-generated links, but review that editorial links are not "
                    f"qualified unintentionally."
                ),
                "url": url,
                "external_urls": rel_urls,
                "rel_values": rel_values,
            }
        )
    if len(external_links) > EXCESSIVE_EXTERNAL_LINKS_THRESHOLD:
        issues.append(
            {
                "type": "excessive_external_links",
                "severity": "warning",
                "message": (
                    f"Page links to {len(external_links)} unique external destinations, "
                    f"exceeding the threshold of {EXCESSIVE_EXTERNAL_LINKS_THRESHOLD}; review "
                    f"outbound links and remove ones that do not help visitors."
                ),
                "url": url,
                "outgoing_external_links": len(external_links),
            }
        )
    return issues


def _resolve_hostname_ips(hostname: str) -> list[str]:
    """Resolve a hostname to IP address strings. Returns [] if resolution fails."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    ips: list[str] = []
    for info in infos:
        ip_text = str(info[4][0]).split("%", 1)[0]
        if ip_text not in ips:
            ips.append(ip_text)
    return ips


def _is_public_ip(ip_text: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    return ip_obj.is_global and not ip_obj.is_multicast


def _check_external_target_safety(url: str) -> tuple[bool, str | None]:
    """SSRF guard: allow only http(s) URLs whose host resolves exclusively to public IPs.

    Returns (is_safe, reason). A reason starting with "dns:" means the host could
    not be resolved (a verification failure rather than an unsafe target).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return False, "invalid URL"
    if parsed.scheme.lower() not in ("http", "https") or not hostname:
        return False, "unsupported URL scheme"

    host_fold = hostname.casefold().rstrip(".")
    if host_fold == "localhost" or host_fold.endswith(_BLOCKED_HOSTNAME_SUFFIXES):
        return False, f"blocked internal hostname '{hostname}'"

    try:
        ipaddress.ip_address(host_fold)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False

    if is_ip_literal:
        if not _is_public_ip(host_fold):
            return False, f"blocked non-public IP address {hostname}"
        return True, None

    resolved_ips = _resolve_hostname_ips(hostname)
    if not resolved_ips:
        return False, f"dns: could not resolve hostname '{hostname}'"
    for ip_text in resolved_ips:
        if not _is_public_ip(ip_text):
            return False, f"blocked: '{hostname}' resolves to non-public IP {ip_text}"
    return True, None


def _send_external_request(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str | None]:
    """Send one HEAD request (falling back to a streamed GET) without following redirects."""
    response = httpx.head(url, headers=headers, timeout=timeout, follow_redirects=False)
    if response.status_code in (405, 501):
        with httpx.stream(
            "GET", url, headers=headers, timeout=timeout, follow_redirects=False
        ) as get_response:
            return get_response.status_code, get_response.headers.get("location")
    return response.status_code, response.headers.get("location")


def _is_ssl_error(error: BaseException) -> bool:
    current: BaseException | None = error
    for _ in range(6):
        if current is None:
            break
        if isinstance(current, ssl.SSLError):
            return True
        current = current.__cause__ or current.__context__
    text = str(error).lower()
    return any(marker in text for marker in ("certificate", "ssl", "tls handshake"))


def check_external_url(
    url: str,
    headers: dict[str, str],
    timeout: float = EXTERNAL_LINK_TIMEOUT_SECONDS,
    max_redirect_hops: int = MAX_EXTERNAL_REDIRECT_HOPS,
) -> dict[str, Any]:
    """Verify one external URL safely and return its verification result (no retries)."""
    result: dict[str, Any] = {
        "url": url,
        "verification_status": "error",
        "status_code": None,
        "redirect_status_code": None,
        "destination_url": None,
        "redirect_chain": [url],
        "error": None,
    }
    current_url = url

    for hop in range(max_redirect_hops + 1):
        is_safe, reason = _check_external_target_safety(current_url)
        if not is_safe:
            if hop == 0:
                is_dns_failure = bool(reason and reason.startswith("dns:"))
                result["verification_status"] = "error" if is_dns_failure else "blocked"
                result["error"] = reason
            else:
                # The redirect itself was observed; its unsafe target is not requested.
                result["verification_status"] = "redirect"
                result["destination_url"] = current_url
                result["error"] = f"redirect target not verified ({reason})"
            return result

        try:
            status_code, location = _send_external_request(current_url, headers, timeout)
        except httpx.TimeoutException as error:
            result["verification_status"] = "timeout"
            result["error"] = str(error) or "request timed out"
            return result
        except httpx.RequestError as error:
            result["verification_status"] = "ssl_error" if _is_ssl_error(error) else "error"
            result["error"] = str(error) or error.__class__.__name__
            return result

        if status_code in _REDIRECT_STATUS_CODES and location:
            if result["redirect_status_code"] is None:
                result["redirect_status_code"] = status_code
            next_url, _ = urldefrag(urljoin(current_url, location))
            if next_url in result["redirect_chain"]:
                result["verification_status"] = "redirect"
                result["status_code"] = status_code
                result["destination_url"] = next_url
                result["error"] = "redirect loop detected"
                return result
            result["redirect_chain"].append(next_url)
            current_url = next_url
            continue

        result["status_code"] = status_code
        if status_code >= 400:
            result["verification_status"] = "broken"
        elif hop > 0:
            result["verification_status"] = "redirect"
        else:
            result["verification_status"] = "working"
        if hop > 0:
            result["destination_url"] = current_url
        return result

    result["verification_status"] = "redirect"
    result["destination_url"] = current_url
    result["error"] = f"stopped after {max_redirect_hops} redirect hops"
    return result


def verify_external_links(
    urls: list[str],
    headers: dict[str, str],
    enabled: bool = True,
    max_checks: int = MAX_EXTERNAL_LINK_CHECKS,
    timeout: float = EXTERNAL_LINK_TIMEOUT_SECONDS,
    max_concurrency: int = EXTERNAL_LINK_MAX_CONCURRENCY,
) -> dict[str, dict[str, Any]]:
    """Verify unique external URLs with a request cap and a concurrency limit.

    Returns a mapping of normalized URL -> verification result. URLs beyond the
    cap (or all URLs when disabled) are recorded as unverified, never as failures.
    """
    unique_urls: list[str] = []
    seen_norms: set[str] = set()
    for raw_url in urls:
        norm_url = _normalize_external_url(raw_url)
        if norm_url not in seen_norms:
            seen_norms.add(norm_url)
            unique_urls.append(raw_url)

    results: dict[str, dict[str, Any]] = {}
    limit = max(0, int(max_checks)) if enabled else 0
    to_check = unique_urls[:limit]

    for raw_url in unique_urls[limit:]:
        results[_normalize_external_url(raw_url)] = {
            "url": raw_url,
            "verification_status": "unverified_limit" if enabled else "unverified_disabled",
            "status_code": None,
            "redirect_status_code": None,
            "destination_url": None,
            "redirect_chain": [raw_url],
            "error": (
                f"not verified: limit of {limit} external checks reached"
                if enabled
                else "not verified: external link verification is disabled"
            ),
        }

    if to_check:
        workers = max(1, min(int(max_concurrency), len(to_check)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            checked = executor.map(
                lambda u: check_external_url(u, headers, timeout=timeout),
                to_check,
            )
            for raw_url, check_result in zip(to_check, checked):
                results[_normalize_external_url(raw_url)] = check_result

    return results


def apply_external_link_verification(
    results: list[dict[str, Any]],
    page_external_links: dict[str, list[dict[str, Any]]],
    verification_results: dict[str, dict[str, Any]],
) -> None:
    """Attach per-page external link results and verification-based findings."""
    rule_for_status = {
        "broken": "broken_external_link",
        "redirect": "external_redirect",
        "timeout": "external_link_timeout",
        "ssl_error": "external_link_ssl_error",
    }

    for page in results:
        links = page_external_links.get(page["url"])
        if not links:
            continue

        page_checks: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            check = verification_results.get(link["normalized_url"])
            if check is None:
                continue
            page_checks.append(
                {
                    "url": link["url"],
                    "verification_status": check["verification_status"],
                    "status_code": check["status_code"],
                    "redirect_status_code": check["redirect_status_code"],
                    "destination_url": check["destination_url"],
                    "error": check["error"],
                }
            )
            rule = rule_for_status.get(check["verification_status"])
            if rule is not None:
                grouped.setdefault(rule, []).append({**check, "url": link["url"]})
        page["external_links"] = page_checks

        seo_issues = page.setdefault("seo_issues", [])
        existing_types = {issue.get("type") for issue in seo_issues}
        for rule in (
            "broken_external_link",
            "external_redirect",
            "external_link_timeout",
            "external_link_ssl_error",
        ):
            checks = grouped.get(rule)
            if not checks or rule in existing_types:
                continue
            first = checks[0]
            urls = [c["url"] for c in checks]
            preview = ", ".join(urls[:5])
            if rule == "broken_external_link":
                details = ", ".join(f"{c['url']} (HTTP {c['status_code']})" for c in checks[:5])
                message = (
                    f"{len(checks)} external link(s) return an HTTP error ({details}); "
                    f"update or remove links to unavailable pages."
                )
                severity = "warning"
            elif rule == "external_redirect":
                details = ", ".join(
                    f"{c['url']} -> {c['destination_url']} (HTTP {c['redirect_status_code']})"
                    for c in checks[:5]
                )
                message = (
                    f"{len(checks)} external link(s) redirect ({details}); link directly "
                    f"to the final destination URL."
                )
                severity = "info"
            elif rule == "external_link_timeout":
                message = (
                    f"{len(checks)} external link(s) timed out during verification ({preview}); "
                    f"check whether the destination is reliable or still available."
                )
                severity = "warning"
            else:
                message = (
                    f"{len(checks)} external link(s) failed TLS/SSL certificate validation "
                    f"({preview}); link to a destination with a valid certificate."
                )
                severity = "warning"

            issue: dict[str, Any] = {
                "type": rule,
                "severity": severity,
                "message": message,
                "url": page["url"],
                "external_urls": urls,
            }
            if rule in ("broken_external_link", "external_redirect"):
                issue["status_code"] = (
                    first["redirect_status_code"]
                    if rule == "external_redirect"
                    else first["status_code"]
                )
            if rule == "external_redirect":
                issue["source_url"] = first["url"]
                issue["destination_url"] = first["destination_url"]
                issue["redirect_chain"] = list(first["redirect_chain"])
            seo_issues.append(issue)
            existing_types.add(rule)


def summarize_external_links(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate External Link Summary statistics from crawled page results."""
    status_by_norm: dict[str, str] = {}
    for page in results:
        for link in page.get("external_links") or []:
            norm_url = _normalize_external_url(link.get("url", ""))
            status_by_norm.setdefault(norm_url, link.get("verification_status") or "")

    statuses = list(status_by_norm.values())
    unverified_disabled = statuses.count("unverified_disabled")
    unverified_limit = statuses.count("unverified_limit")
    return {
        "unique_external_urls": len(statuses),
        "verified_count": sum(1 for s in statuses if s in VERIFIED_EXTERNAL_STATUSES),
        "broken_count": statuses.count("broken"),
        "redirect_count": statuses.count("redirect"),
        "verification_failures_count": sum(
            1 for s in statuses if s in FAILED_EXTERNAL_STATUSES
        ),
        "blocked_count": statuses.count("blocked"),
        "unverified_count": unverified_disabled + unverified_limit,
        "verification_enabled": EXTERNAL_LINK_VERIFICATION_ENABLED,
        "verification_disabled": unverified_disabled > 0,
        "limit_reached": unverified_limit > 0,
        "max_external_checks": MAX_EXTERNAL_LINK_CHECKS,
    }


# ---------------------------------------------------------------------------
# International SEO & Hreflang Audit
# ---------------------------------------------------------------------------

HREFLANG_ISSUE_TYPES = {
    "missing_hreflang",
    "invalid_hreflang",
    "hreflang_missing_return_link",
    "hreflang_target_not_crawled",
    "hreflang_noncanonical_target",
    "hreflang_missing_self_reference",
    "hreflang_missing_x_default",
    "hreflang_canonical_conflict",
    "duplicate_hreflang_language",
    "hreflang_http_url",
}

# Standard 2-letter ISO 639-1 language codes
_ISO_639_1_CODES = {
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs", "ca", "ce",
    "ch", "co", "cr", "cs", "cu", "cv", "cy", "da", "de", "dv", "dz", "ee",
    "el", "en", "eo", "es", "et", "eu", "fa", "ff", "fi", "fj", "fo", "fr",
    "fy", "ga", "gd", "gl", "gn", "gu", "gv", "ha", "he", "hi", "ho", "hr",
    "ht", "hu", "hy", "hz", "ia", "id", "ie", "ig", "ii", "ik", "io", "is",
    "it", "iu", "ja", "jv", "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn",
    "ko", "kr", "ks", "ku", "kv", "kw", "ky", "la", "lb", "lg", "li", "ln",
    "lo", "lt", "lu", "lv", "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms",
    "mt", "my", "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv",
    "ny", "oc", "oj", "om", "or", "os", "pa", "pi", "pl", "ps", "pt", "qu",
    "rm", "rn", "ro", "ru", "rw", "sa", "sc", "sd", "se", "sg", "si", "sk",
    "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw", "ta",
    "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw",
    "ty", "ug", "uk", "ur", "uz", "ve", "vi", "vo", "wa", "wo", "xh", "yi",
    "yo", "za", "zh", "zu",
}

# Recognized 3-letter ISO 639-2/3 language codes frequently used in web hreflang
_ISO_639_2_3_CODES = {
    "ast", "fil", "gsw", "haw", "kok", "mai", "mni", "nso", "syr", "yue",
}


def is_valid_hreflang(tag: str) -> bool:
    """Validate a BCP 47 language/region tag according to hreflang specifications.

    Accepts 'x-default' (case-insensitive) or valid language tags conforming to
    language[-script][-region], such as 'en', 'en-US', 'es-419', 'zh-Hans',
    or 'zh-Hant-TW'. Rejects underscores (e.g. 'en_US'), country-only codes ('us'),
    names ('english'), numbers, or malformed subtags.
    """
    if not isinstance(tag, str):
        return False
    cleaned = tag.strip().lower()
    if not cleaned:
        return False
    if cleaned == "x-default":
        return True
    if "_" in tag:
        return False

    parts = cleaned.split("-")
    if not (1 <= len(parts) <= 3):
        return False

    lang = parts[0]
    if lang not in _ISO_639_1_CODES and lang not in _ISO_639_2_3_CODES:
        return False

    if len(parts) == 1:
        return True

    if len(parts) == 2:
        subtag = parts[1]
        is_script = len(subtag) == 4 and subtag.isalpha()
        is_region = (len(subtag) == 2 and subtag.isalpha()) or (
            len(subtag) == 3 and subtag.isdigit()
        )
        return is_script or is_region

    if len(parts) == 3:
        script, region = parts[1], parts[2]
        is_script = len(script) == 4 and script.isalpha()
        is_region = (len(region) == 2 and region.isalpha()) or (
            len(region) == 3 and region.isdigit()
        )
        return is_script and is_region

    return False


def _extract_link_header_hreflangs(
    headers: Any, base_url: str
) -> list[dict[str, Any]]:
    """Parse hreflang alternate links from HTTP Link headers."""
    raw_header_values: list[str] = []
    if hasattr(headers, "get_list"):
        raw_header_values = headers.get_list("link")
    elif hasattr(headers, "get"):
        val = headers.get("link")
        if val:
            raw_header_values = [val]

    results: list[dict[str, Any]] = []
    for header_val in raw_header_values:
        entries = re.findall(r'<([^>]+)>(.*?)(?:,|$)', header_val)
        for raw_url, params_str in entries:
            param_matches = re.findall(
                r'([a-zA-Z_-]+)\s*=\s*(?:"([^"]*)"|([^\s;,]+))', params_str
            )
            param_dict = {}
            for k, q_val, u_val in param_matches:
                param_dict[k.lower()] = q_val if q_val else u_val

            rel = param_dict.get("rel", "")
            hreflang = param_dict.get("hreflang", "")
            if "alternate" in rel.lower().split() and hreflang:
                abs_url = urljoin(base_url, raw_url.strip())
                clean_url, _ = urldefrag(abs_url)
                norm_url = _normalize_internal_url(clean_url)
                results.append(
                    {
                        "hreflang": hreflang.strip(),
                        "normalized_hreflang": hreflang.strip().lower(),
                        "url": clean_url,
                        "normalized_url": norm_url,
                        "source": "header",
                    }
                )
    return results


def _extract_hreflang_links(
    page_url: str,
    soup: BeautifulSoup,
    response_headers: Any = None,
) -> list[dict[str, Any]]:
    """Extract hreflang alternate annotations from HTML <link> tags and HTTP Link headers."""
    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for tag in soup.find_all("link"):
        rel = tag.get("rel")
        if isinstance(rel, list):
            rel_tokens = {r.strip().lower() for r in rel if isinstance(r, str)}
        elif isinstance(rel, str):
            rel_tokens = {r.strip().lower() for r in rel.split()}
        else:
            continue

        if "alternate" not in rel_tokens:
            continue

        raw_lang = tag.get("hreflang")
        raw_href = tag.get("href")
        if not raw_lang or not isinstance(raw_lang, str) or not raw_lang.strip():
            continue
        if not raw_href or not isinstance(raw_href, str) or not raw_href.strip():
            continue

        cleaned_lang = raw_lang.strip()
        abs_url = urljoin(page_url, raw_href.strip())
        clean_url, _ = urldefrag(abs_url)
        norm_url = _normalize_internal_url(clean_url)
        key = (cleaned_lang.lower(), norm_url)
        if key not in seen:
            seen.add(key)
            links.append(
                {
                    "hreflang": cleaned_lang,
                    "normalized_hreflang": cleaned_lang.lower(),
                    "url": clean_url,
                    "normalized_url": norm_url,
                    "source": "html",
                }
            )

    if response_headers:
        header_links = _extract_link_header_hreflangs(response_headers, page_url)
        for item in header_links:
            key = (item["normalized_hreflang"], item["normalized_url"])
            if key not in seen:
                seen.add(key)
                links.append(item)

    return links


def detect_page_hreflang_issues(
    url: str,
    hreflang_links: list[dict[str, Any]],
    canonical_url: str | None = None,
    is_https: bool = False,
) -> list[dict[str, Any]]:
    """Run per-page static checks on hreflang annotations."""
    if not hreflang_links:
        return []

    issues: list[dict[str, Any]] = []
    norm_page_url = _normalize_internal_url(url)
    norm_canonical_url = (
        _normalize_internal_url(canonical_url) if canonical_url else None
    )

    # 1. invalid_hreflang
    invalid_tags = [
        link["hreflang"]
        for link in hreflang_links
        if not is_valid_hreflang(link["hreflang"])
    ]
    if invalid_tags:
        issues.append(
            {
                "type": "invalid_hreflang",
                "severity": "error",
                "message": (
                    f"Page declares invalid hreflang code(s): {', '.join(invalid_tags)}; "
                    f"use valid ISO 639-1 language code and optional ISO 3166-1 alpha-2 region (e.g. 'en-US' or 'x-default')."
                ),
                "url": url,
                "invalid_hreflangs": invalid_tags,
            }
        )

    # 2. duplicate_hreflang_language
    lang_counts: dict[str, list[str]] = {}
    for link in hreflang_links:
        lang_counts.setdefault(link["normalized_hreflang"], []).append(link["url"])

    duplicate_langs = [
        lang for lang, urls in lang_counts.items() if len(urls) > 1
    ]
    if duplicate_langs:
        issues.append(
            {
                "type": "duplicate_hreflang_language",
                "severity": "warning",
                "message": (
                    f"Page declares duplicate alternate URLs for the same language/region: "
                    f"{', '.join(duplicate_langs)}; each language or regional variant must have only one target URL."
                ),
                "url": url,
                "duplicate_languages": duplicate_langs,
            }
        )

    # 3. hreflang_missing_self_reference
    has_self_ref = any(
        link["normalized_url"] == norm_page_url
        or (norm_canonical_url and link["normalized_url"] == norm_canonical_url)
        for link in hreflang_links
    )
    if not has_self_ref:
        issues.append(
            {
                "type": "hreflang_missing_self_reference",
                "severity": "warning",
                "message": (
                    "Page declares hreflang annotations but lacks a self-referencing hreflang tag; "
                    "each language version must list itself as well as all other language versions."
                ),
                "url": url,
            }
        )

    # 4. hreflang_missing_x_default
    distinct_languages = {
        link["normalized_hreflang"]
        for link in hreflang_links
        if link["normalized_hreflang"] != "x-default"
    }
    has_x_default = any(
        link["normalized_hreflang"] == "x-default" for link in hreflang_links
    )
    if len(distinct_languages) >= 2 and not has_x_default:
        issues.append(
            {
                "type": "hreflang_missing_x_default",
                "severity": "info",
                "message": (
                    "Multilingual hreflang set lacks an 'x-default' annotation; add an x-default tag "
                    "to target international visitors whose language is not explicitly listed."
                ),
                "url": url,
            }
        )

    # 5. hreflang_http_url
    if is_https:
        http_targets = [
            link["url"]
            for link in hreflang_links
            if link["url"].lower().startswith("http://")
        ]
        if http_targets:
            issues.append(
                {
                    "type": "hreflang_http_url",
                    "severity": "warning",
                    "message": (
                        f"Page is served over HTTPS but references insecure HTTP hreflang target(s): "
                        f"{', '.join(http_targets[:5])}; link to HTTPS version."
                    ),
                    "url": url,
                    "insecure_urls": http_targets,
                }
            )

    # 6. hreflang_canonical_conflict (per-page)
    if norm_canonical_url and norm_canonical_url != norm_page_url:
        issues.append(
            {
                "type": "hreflang_canonical_conflict",
                "severity": "error",
                "message": (
                    f"Page declares hreflang annotations but its canonical URL points to '{canonical_url}'; "
                    f"Google requires hreflang annotations to be declared on canonical pages only."
                ),
                "url": url,
                "canonical_url": canonical_url,
                "hreflang_url": url,
            }
        )
    elif norm_canonical_url:
        self_links = [
            link
            for link in hreflang_links
            if link["normalized_url"] == norm_page_url
            or link["normalized_url"] == norm_canonical_url
        ]
        for sl in self_links:
            if sl["normalized_url"] != norm_canonical_url:
                issues.append(
                    {
                        "type": "hreflang_canonical_conflict",
                        "severity": "error",
                        "message": (
                            f"Self-referencing hreflang URL '{sl['url']}' conflicts with the declared canonical URL "
                            f"'{canonical_url}'; ensure self-referencing hreflang matches canonical URL exactly."
                        ),
                        "url": url,
                        "canonical_url": canonical_url,
                        "hreflang_url": sl["url"],
                    }
                )
                break

    return issues


def detect_cross_page_hreflang_issues(results: list[dict[str, Any]]) -> None:
    """Analyze reciprocal hreflang relationships and missing hreflang across crawled pages."""
    crawled_by_norm: dict[str, dict[str, Any]] = {}
    for page in results:
        if page.get("url"):
            norm = _normalize_internal_url(page["url"])
            crawled_by_norm[norm] = page

    # Check if site appears to be multilingual
    has_hreflang_pages = any((p.get("hreflang_count") or 0) > 0 for p in results)
    has_multilingual_paths = any(
        bool(re.search(r"^/[a-z]{2}(-[a-z]{2})?(/|$)", urlparse(p["url"]).path.lower()))
        for p in results
        if p.get("url")
    )
    is_multilingual_site = has_hreflang_pages or has_multilingual_paths

    # 1. missing_hreflang
    if is_multilingual_site:
        for page in results:
            if page.get("error") is not None:
                continue
            if not is_html_content(page.get("content_type")):
                continue
            if (page.get("hreflang_count") or 0) == 0:
                page_path = urlparse(page["url"]).path.lower()
                has_lang_path = bool(re.search(r"^/[a-z]{2}(-[a-z]{2})?(/|$)", page_path))
                if has_hreflang_pages or has_lang_path:
                    seo_issues = page.setdefault("seo_issues", [])
                    if not any(i.get("type") == "missing_hreflang" for i in seo_issues):
                        seo_issues.append(
                            {
                                "type": "missing_hreflang",
                                "severity": "info",
                                "message": (
                                    "Page appears to be part of a multilingual site but lacks hreflang annotations; "
                                    "add <link rel=\"alternate\" hreflang=\"...\" href=\"...\"> tags to help search engines serve the correct language version."
                                ),
                                "url": page["url"],
                            }
                        )

    # 2. Cross-page reciprocal, uncrawled, and non-canonical target checks
    for page in results:
        hreflang_links = page.get("hreflang_links") or []
        if not hreflang_links:
            continue

        page_norm = _normalize_internal_url(page["url"])
        page_canonical = page.get("canonical_url")
        page_canonical_norm = (
            _normalize_internal_url(page_canonical) if page_canonical else page_norm
        )

        missing_returns: list[str] = []
        uncrawled_targets: list[str] = []
        noncanonical_targets: list[dict[str, str]] = []

        for link in hreflang_links:
            target_norm = link["normalized_url"]
            if target_norm == page_norm or target_norm == page_canonical_norm:
                continue

            target_page = crawled_by_norm.get(target_norm)
            if target_page is None:
                uncrawled_targets.append(link["url"])
                continue

            target_canonical = target_page.get("canonical_url")
            if target_canonical:
                target_canonical_norm = _normalize_internal_url(target_canonical)
                if target_canonical_norm != target_norm:
                    noncanonical_targets.append(
                        {
                            "target_url": link["url"],
                            "canonical_url": target_canonical,
                        }
                    )

            target_hreflang_links = target_page.get("hreflang_links") or []
            has_return_link = any(
                tl["normalized_url"] == page_norm
                or (page_canonical_norm and tl["normalized_url"] == page_canonical_norm)
                for tl in target_hreflang_links
            )
            if not has_return_link:
                missing_returns.append(link["url"])

        seo_issues = page.setdefault("seo_issues", [])
        existing_types = {i.get("type") for i in seo_issues}

        if missing_returns and "hreflang_missing_return_link" not in existing_types:
            seo_issues.append(
                {
                    "type": "hreflang_missing_return_link",
                    "severity": "warning",
                    "message": (
                        f"Alternate language page(s) do not reciprocally link back to this page: "
                        f"{', '.join(missing_returns[:5])}; Google requires reciprocal hreflang annotations."
                    ),
                    "url": page["url"],
                    "missing_return_urls": missing_returns,
                }
            )

        if uncrawled_targets and "hreflang_target_not_crawled" not in existing_types:
            seo_issues.append(
                {
                    "type": "hreflang_target_not_crawled",
                    "severity": "info",
                    "message": (
                        f"Hreflang target(s) were not discovered or crawled during this crawl: "
                        f"{', '.join(uncrawled_targets[:5])}; verify that these targets are indexable and reachable."
                    ),
                    "url": page["url"],
                    "uncrawled_urls": uncrawled_targets,
                }
            )

        if noncanonical_targets and "hreflang_noncanonical_target" not in existing_types:
            preview = ", ".join(
                f"{nt['target_url']} -> {nt['canonical_url']}"
                for nt in noncanonical_targets[:3]
            )
            seo_issues.append(
                {
                    "type": "hreflang_noncanonical_target",
                    "severity": "warning",
                    "message": (
                        f"Hreflang target points to a non-canonical URL ({preview}); "
                        f"Google requires hreflang annotations to point directly to canonical URLs."
                    ),
                    "url": page["url"],
                    "noncanonical_targets": noncanonical_targets,
                }
            )


def summarize_hreflang_audit(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate International SEO & Hreflang Summary statistics from crawled pages."""
    pages_with_hreflang = sum(1 for p in results if (p.get("hreflang_count") or 0) > 0)
    all_issues = [
        issue
        for page in results
        for issue in page.get("seo_issues", [])
        if issue.get("type") in HREFLANG_ISSUE_TYPES
    ]
    invalid_hreflang_count = sum(
        1 for i in all_issues if i.get("type") == "invalid_hreflang"
    )
    missing_return_links_count = sum(
        1 for i in all_issues if i.get("type") == "hreflang_missing_return_link"
    )
    uncrawled_targets_count = sum(
        1 for i in all_issues if i.get("type") == "hreflang_target_not_crawled"
    )
    canonical_conflicts_count = sum(
        1 for i in all_issues if i.get("type") == "hreflang_canonical_conflict"
    )

    return {
        "pages_with_hreflang_count": pages_with_hreflang,
        "invalid_hreflang_count": invalid_hreflang_count,
        "missing_return_links_count": missing_return_links_count,
        "uncrawled_targets_count": uncrawled_targets_count,
        "canonical_conflicts_count": canonical_conflicts_count,
        "total_hreflang_issues": len(all_issues),
        "severity_counts": {
            "error": sum(1 for i in all_issues if i.get("severity") == "error"),
            "warning": sum(1 for i in all_issues if i.get("severity") == "warning"),
            "info": sum(1 for i in all_issues if i.get("severity") == "info"),
        },
    }


# Open Graph & Social Sharing Audit
OPEN_GRAPH_REQUIRED_PROPERTIES = (
    "og:title",
    "og:description",
    "og:image",
    "og:url",
    "og:type",
)

VALID_TWITTER_CARD_TYPES = {
    "summary",
    "summary_large_image",
    "app",
    "player",
}

MIN_OG_TITLE_LENGTH = 15
MAX_OG_TITLE_LENGTH = 90
MIN_OG_DESCRIPTION_LENGTH = 50
MAX_OG_DESCRIPTION_LENGTH = 200

SOCIAL_ISSUE_TYPES = {
    "missing_og_title",
    "missing_og_description",
    "missing_og_image",
    "missing_og_url",
    "missing_og_type",
    "invalid_og_url",
    "og_url_canonical_conflict",
    "og_image_invalid_url",
    "duplicate_og_tags",
    "empty_og_metadata",
    "missing_twitter_card",
    "missing_twitter_title",
    "missing_twitter_description",
    "missing_twitter_image",
    "invalid_twitter_card",
    "short_og_title",
    "long_og_title",
    "short_og_description",
    "long_og_description",
    "social_image_missing_alt",
}

SOCIAL_ISSUE_SEVERITIES = {
    "missing_og_title": "warning",
    "missing_og_description": "warning",
    "missing_og_image": "warning",
    "missing_og_url": "warning",
    "missing_og_type": "warning",
    "invalid_og_url": "error",
    "og_url_canonical_conflict": "warning",
    "og_image_invalid_url": "error",
    "duplicate_og_tags": "warning",
    "empty_og_metadata": "warning",
    "missing_twitter_card": "warning",
    "missing_twitter_title": "warning",
    "missing_twitter_description": "warning",
    "missing_twitter_image": "warning",
    "invalid_twitter_card": "error",
    "short_og_title": "warning",
    "long_og_title": "warning",
    "short_og_description": "warning",
    "long_og_description": "warning",
    "social_image_missing_alt": "info",
}


def _is_valid_absolute_http_url(val: str | None) -> bool:
    """Check if the given string is a valid absolute HTTP or HTTPS URL."""
    if not val or not isinstance(val, str):
        return False
    try:
        parsed = urlparse(val.strip())
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _normalize_sharing_url(raw_url: str | None) -> str:
    """Normalize URL by stripping fragment, lowercasing scheme/host, and normalizing trailing slash."""
    if not raw_url:
        return ""
    clean, _ = urldefrag(str(raw_url).strip())
    parsed = urlparse(clean)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def _extract_social_metadata(
    soup: BeautifulSoup | None,
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Extract Open Graph and Twitter Card metadata from HTML.

    Returns:
        (og_data, twitter_data, empty_tags)
        where og_data and twitter_data map lowercased property names to
        lists of non-empty content strings, and empty_tags lists property
        names with empty or whitespace-only content.
    """
    og_data: dict[str, list[str]] = {}
    twitter_data: dict[str, list[str]] = {}
    empty_tags: list[str] = []

    if not soup:
        return og_data, twitter_data, empty_tags

    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").strip().lower()
        if not prop:
            continue
        content = meta.get("content")
        raw_val = content if content is not None else ""
        cleaned_val = raw_val.strip()

        if prop.startswith("og:"):
            if not cleaned_val:
                empty_tags.append(prop)
            else:
                og_data.setdefault(prop, []).append(cleaned_val)
        elif prop.startswith("twitter:"):
            if not cleaned_val:
                empty_tags.append(prop)
            else:
                twitter_data.setdefault(prop, []).append(cleaned_val)

    return og_data, twitter_data, empty_tags


def detect_social_seo_issues(
    url: str,
    soup: BeautifulSoup | None,
    content_type: str | None = "text/html",
    canonical_url: str | None = None,
) -> list[dict[str, Any]]:
    """Audit Open Graph and Twitter Card social metadata for a crawled HTML page."""
    if not is_html_content(content_type) or soup is None:
        return []

    issues: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    def add_issue(issue_type: str, message: str, **extra: Any) -> None:
        if issue_type not in seen_types:
            seen_types.add(issue_type)
            issue_dict = {
                "type": issue_type,
                "severity": SOCIAL_ISSUE_SEVERITIES.get(issue_type, "warning"),
                "message": message,
                "url": url,
            }
            issue_dict.update(extra)
            issues.append(issue_dict)

    og_data, twitter_data, empty_tags = _extract_social_metadata(soup)

    if not og_data and not twitter_data and not empty_tags:
        return []

    # 1. empty_og_metadata
    if empty_tags:
        empty_props = ", ".join(sorted(set(empty_tags)))
        add_issue(
            "empty_og_metadata",
            f"Social metadata tag(s) exist but have empty or whitespace content: {empty_props}.",
        )

    # Open Graph properties
    og_title_list = og_data.get("og:title", [])
    og_desc_list = og_data.get("og:description", [])
    og_image_list = og_data.get("og:image", [])
    og_url_list = og_data.get("og:url", [])
    og_type_list = og_data.get("og:type", [])

    og_title = og_title_list[0] if og_title_list else None
    og_desc = og_desc_list[0] if og_desc_list else None
    og_image = og_image_list[0] if og_image_list else None
    og_url = og_url_list[0] if og_url_list else None
    og_type = og_type_list[0] if og_type_list else None

    # 2. missing_og_title
    if not og_title:
        add_issue(
            "missing_og_title",
            "Page is missing an 'og:title' Open Graph meta tag; social platforms require a title for rich link previews.",
        )

    # 3. missing_og_description
    if not og_desc:
        add_issue(
            "missing_og_description",
            "Page is missing an 'og:description' Open Graph meta tag; add a summary description for social previews.",
        )

    # 4. missing_og_image
    if not og_image:
        add_issue(
            "missing_og_image",
            "Page is missing an 'og:image' Open Graph meta tag; social platforms need a preview image to generate rich visual cards.",
        )

    # 5. missing_og_url
    if not og_url:
        add_issue(
            "missing_og_url",
            "Page is missing an 'og:url' Open Graph meta tag; specify the canonical sharing URL.",
        )

    # 6. missing_og_type
    if not og_type:
        add_issue(
            "missing_og_type",
            "Page is missing an 'og:type' Open Graph meta tag (e.g. 'website', 'article'); specify the content type for social graphs.",
        )

    # 7. duplicate_og_tags (conflicting multiple values for scalar properties)
    duplicate_conflicts = []
    for prop in ("og:title", "og:description", "og:url", "og:type"):
        vals = og_data.get(prop, [])
        unique_vals = list(dict.fromkeys(v.strip() for v in vals if v.strip()))
        if len(unique_vals) > 1:
            duplicate_conflicts.append(f"{prop} ({', '.join(repr(v) for v in unique_vals)})")
    if duplicate_conflicts:
        add_issue(
            "duplicate_og_tags",
            f"Multiple conflicting values declared for Open Graph property: {'; '.join(duplicate_conflicts)}.",
        )

    # 8. invalid_og_url
    if og_url and not _is_valid_absolute_http_url(og_url):
        add_issue(
            "invalid_og_url",
            f"Open Graph 'og:url' ('{og_url}') is malformed or not an absolute HTTP/HTTPS URL.",
        )

    # 9. og_url_canonical_conflict
    actual_canonical = canonical_url
    if actual_canonical is None and soup:
        can_tags = _find_canonical_link_tags(soup)
        if can_tags:
            resolved_can, is_can_valid = _resolve_canonical_url(url, can_tags[0].get("href"))
            if is_can_valid:
                actual_canonical = resolved_can

    if og_url and actual_canonical and _is_valid_absolute_http_url(og_url):
        if _normalize_sharing_url(og_url) != _normalize_sharing_url(actual_canonical):
            add_issue(
                "og_url_canonical_conflict",
                f"Open Graph URL ('{og_url}') conflicts with declared canonical URL ('{actual_canonical}'); align og:url with canonical URL.",
                canonical_url=actual_canonical,
            )

    # 10. og_image_invalid_url
    if og_image_list:
        invalid_imgs = [img for img in og_image_list if not _is_valid_absolute_http_url(img)]
        if invalid_imgs:
            add_issue(
                "og_image_invalid_url",
                f"Open Graph image URL ('{invalid_imgs[0]}') is malformed or not an absolute HTTP/HTTPS URL.",
                image_url=invalid_imgs[0],
            )

    # Twitter Card Metadata
    twitter_card_list = twitter_data.get("twitter:card", [])
    twitter_card = twitter_card_list[0] if twitter_card_list else None
    twitter_title = twitter_data.get("twitter:title", [None])[0]
    twitter_desc = twitter_data.get("twitter:description", [None])[0]
    twitter_image = twitter_data.get("twitter:image", [None])[0]

    # 11. missing_twitter_card
    if not twitter_card:
        add_issue(
            "missing_twitter_card",
            "Page lacks a 'twitter:card' meta tag; specify 'summary' or 'summary_large_image' to control Twitter/X card presentation.",
        )
    else:
        # 12. invalid_twitter_card
        if twitter_card.lower() not in VALID_TWITTER_CARD_TYPES:
            add_issue(
                "invalid_twitter_card",
                f"Invalid twitter:card value '{twitter_card}'; supported card types are 'summary', 'summary_large_image', 'app', 'player'.",
            )

    # 13. missing_twitter_title (fallback to og:title)
    if not twitter_title and not og_title:
        add_issue(
            "missing_twitter_title",
            "Page lacks a 'twitter:title' meta tag and no 'og:title' fallback was found for Twitter/X previews.",
        )

    # 14. missing_twitter_description (fallback to og:description)
    if not twitter_desc and not og_desc:
        add_issue(
            "missing_twitter_description",
            "Page lacks a 'twitter:description' meta tag and no 'og:description' fallback was found for Twitter/X previews.",
        )

    # 15. missing_twitter_image (fallback to og:image)
    if not twitter_image and not og_image:
        add_issue(
            "missing_twitter_image",
            "Page lacks a 'twitter:image' meta tag and no 'og:image' fallback was found for Twitter/X previews.",
        )

    # Social Sharing Quality
    # 16. short_og_title
    if og_title and len(og_title) < MIN_OG_TITLE_LENGTH:
        add_issue(
            "short_og_title",
            f"Open Graph title is unusually short ({len(og_title)} characters; recommended at least {MIN_OG_TITLE_LENGTH}): '{og_title}'.",
        )

    # 17. long_og_title
    if og_title and len(og_title) > MAX_OG_TITLE_LENGTH:
        add_issue(
            "long_og_title",
            f"Open Graph title exceeds recommended length ({len(og_title)} characters; recommended max {MAX_OG_TITLE_LENGTH} to avoid truncation): '{og_title[:50]}...'.",
        )

    # 18. short_og_description
    if og_desc and len(og_desc) < MIN_OG_DESCRIPTION_LENGTH:
        add_issue(
            "short_og_description",
            f"Open Graph description is unusually short ({len(og_desc)} characters; recommended at least {MIN_OG_DESCRIPTION_LENGTH}): '{og_desc}'.",
        )

    # 19. long_og_description
    if og_desc and len(og_desc) > MAX_OG_DESCRIPTION_LENGTH:
        add_issue(
            "long_og_description",
            f"Open Graph description exceeds recommended length ({len(og_desc)} characters; recommended max {MAX_OG_DESCRIPTION_LENGTH} to avoid truncation): '{og_desc[:50]}...'.",
        )

    # 20. social_image_missing_alt
    social_img = og_image or twitter_image
    if social_img:
        has_img_alt = bool(og_data.get("og:image:alt")) or bool(twitter_data.get("twitter:image:alt"))
        if not has_img_alt:
            add_issue(
                "social_image_missing_alt",
                "Social sharing image is specified but lacks descriptive alternative text ('og:image:alt' or 'twitter:image:alt').",
                image_url=social_img,
            )

    return issues


def summarize_social_audit(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate Open Graph & Social Sharing Summary statistics from crawled pages."""
    pages_with_og = sum(1 for p in results if p.get("has_open_graph"))
    pages_with_twitter = sum(1 for p in results if p.get("has_twitter_card"))

    all_issues = [
        issue
        for page in results
        for issue in page.get("seo_issues", [])
        if issue.get("type") in SOCIAL_ISSUE_TYPES
    ]

    missing_required_og_count = sum(
        1
        for i in all_issues
        if i.get("type")
        in (
            "missing_og_title",
            "missing_og_description",
            "missing_og_image",
            "missing_og_url",
            "missing_og_type",
        )
    )
    invalid_social_metadata_count = sum(
        1
        for i in all_issues
        if i.get("type")
        in (
            "invalid_og_url",
            "og_image_invalid_url",
            "invalid_twitter_card",
            "og_url_canonical_conflict",
            "duplicate_og_tags",
            "empty_og_metadata",
        )
    )

    return {
        "pages_with_og_count": pages_with_og,
        "pages_with_twitter_card_count": pages_with_twitter,
        "missing_required_og_count": missing_required_og_count,
        "invalid_social_metadata_count": invalid_social_metadata_count,
        "total_social_issues": len(all_issues),
        "severity_counts": {
            "error": sum(1 for i in all_issues if i.get("severity") == "error"),
            "warning": sum(1 for i in all_issues if i.get("severity") == "warning"),
            "info": sum(1 for i in all_issues if i.get("severity") == "info"),
        },
    }


# Content Readability & Keyword Optimization Audit
READABILITY_ISSUE_TYPES = {
    "missing_primary_heading",
    "multiple_primary_headings",
    "heading_content_mismatch",
    "keyword_stuffing",
    "low_content_readability",
    "short_paragraphs",
    "long_paragraphs",
    "missing_subheadings",
    "heading_repetition",
    "weak_content_relevance",
}

READABILITY_ISSUE_SEVERITIES = {
    "missing_primary_heading": "warning",
    "multiple_primary_headings": "warning",
    "heading_content_mismatch": "warning",
    "keyword_stuffing": "warning",
    "low_content_readability": "warning",
    "short_paragraphs": "info",
    "long_paragraphs": "warning",
    "missing_subheadings": "warning",
    "heading_repetition": "warning",
    "weak_content_relevance": "warning",
}

STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was",
    "were", "will", "our", "your", "all", "any", "can", "has", "have",
    "had", "not", "but", "about", "into", "over", "after", "its", "their",
    "what", "when", "where", "who", "which", "why", "how", "been", "being",
}

GENERIC_TOPIC_WORDS = {
    "page", "heading", "site", "home", "website", "title", "main",
    "complete", "valid", "test", "demo", "sample", "example", "domain",
}

MIN_WORDS_FOR_READABILITY = 60
MIN_SENTENCES_FOR_READABILITY = 2
MIN_PARAGRAPHS_FOR_SHORT_CHECK = 3
SHORT_PARAGRAPH_WORD_LIMIT = 15
SHORT_PARAGRAPH_RATIO_THRESHOLD = 0.75
LONG_PARAGRAPH_WORD_LIMIT = 150
LONG_CONTENT_SUBHEADING_WORD_THRESHOLD = 600
KEYWORD_STUFFING_DENSITY_THRESHOLD = 0.08
KEYWORD_STUFFING_MIN_COUNT = 8
FLESCH_READING_EASE_LOW_THRESHOLD = 30.0


def _count_syllables_in_word(word: str) -> int:
    """Estimate syllable count in a single English word using deterministic rules."""
    w = word.strip().lower()
    if not w:
        return 0
    clean = "".join(ch for ch in w if ch.isalpha())
    if not clean:
        return 0
    if len(clean) <= 3:
        return 1
    if clean.endswith("es") and not (
        clean.endswith("ces") or clean.endswith("ges") or clean.endswith("ses")
    ):
        clean = clean[:-2]
    elif clean.endswith("e") and not clean.endswith("le"):
        clean = clean[:-1]
    vowels = "aeiouy"
    count = 0
    prev_is_vowel = False
    for ch in clean:
        is_vowel = ch in vowels
        if is_vowel and not prev_is_vowel:
            count += 1
        prev_is_vowel = is_vowel
    return max(1, count)


def calculate_flesch_reading_ease(visible_text: str | None) -> float | None:
    """Calculate the Flesch Reading Ease score for visible text.

    Returns a score between 0.0 and 100.0 (or slightly outside), or None if
    insufficient content / sentences.
    """
    if not visible_text:
        return None
    words = extract_meaningful_words(visible_text)
    if len(words) < MIN_WORDS_FOR_READABILITY:
        return None

    sentences = [
        s.strip()
        for s in re.split(r"[.!?]+", visible_text)
        if len(extract_meaningful_words(s)) > 0
    ]
    if len(sentences) < MIN_SENTENCES_FOR_READABILITY:
        return None

    total_words = len(words)
    total_sentences = len(sentences)
    total_syllables = sum(_count_syllables_in_word(w) for w in words)

    words_per_sentence = total_words / total_sentences
    syllables_per_word = total_syllables / total_words

    score = 206.835 - (1.015 * words_per_sentence) - (84.6 * syllables_per_word)
    return round(score, 1)


def _is_synthetic_dummy_text(words: list[str]) -> bool:
    """Check if text contains synthetic test generator tokens like word0, term1, normal2, etc."""
    if not words:
        return False
    synthetic_count = sum(1 for w in words if re.match(r"^[a-zA-Z]+[0-9]+$", w))
    return synthetic_count >= 5


def detect_readability_and_keyword_issues(
    url: str,
    soup: BeautifulSoup | None,
    content_type: str | None = "text/html",
    title: str | None = None,
    visible_text: str | None = None,
) -> list[dict[str, Any]]:
    """Audit content readability, keyword optimization, and heading structure."""
    if not is_html_content(content_type) or soup is None:
        return []

    text = (
        visible_text
        if visible_text is not None
        else extract_visible_text(soup)
    )
    words = extract_meaningful_words(text)
    if _is_synthetic_dummy_text(words):
        return []

    issues: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    def add_issue(issue_type: str, message: str, **extra: Any) -> None:
        if issue_type not in seen_types:
            seen_types.add(issue_type)
            issue_dict = {
                "type": issue_type,
                "severity": READABILITY_ISSUE_SEVERITIES.get(issue_type, "warning"),
                "message": message,
                "url": url,
            }
            issue_dict.update(extra)
            issues.append(issue_dict)

    word_count = len(words)

    # 1. Heading structure checks
    visible_h1s = [
        h
        for h in soup.find_all("h1")
        if not _is_element_hidden(h) and h.get_text(strip=True)
    ]

    # 1a. missing_primary_heading
    if len(visible_h1s) == 0:
        add_issue(
            "missing_primary_heading",
            "Page lacks a visible <h1> primary heading; every page should have one visible H1 describing the main topic.",
        )

    # 1b. multiple_primary_headings
    if len(visible_h1s) > 1:
        add_issue(
            "multiple_primary_headings",
            f"Page contains {len(visible_h1s)} visible <h1> headings; standard SEO practice recommends a single primary <h1> heading per page.",
        )

    # 1c. heading_content_mismatch
    page_title = title if title is not None else (soup.title.string.strip() if soup.title and soup.title.string else None)
    if page_title and visible_h1s:
        primary_h1_text = visible_h1s[0].get_text(strip=True)
        title_terms = {
            w.casefold()
            for w in extract_meaningful_words(page_title)
            if len(w) >= 3 and w.casefold() not in STOP_WORDS and w.casefold() not in GENERIC_TOPIC_WORDS
        }
        h1_terms = {
            w.casefold()
            for w in extract_meaningful_words(primary_h1_text)
            if len(w) >= 3 and w.casefold() not in STOP_WORDS and w.casefold() not in GENERIC_TOPIC_WORDS
        }
        if title_terms and h1_terms:
            overlap = title_terms & h1_terms
            title_cf = page_title.casefold()
            h1_cf = primary_h1_text.casefold()
            if not overlap and title_cf not in h1_cf and h1_cf not in title_cf:
                add_issue(
                    "heading_content_mismatch",
                    f"Page title ('{page_title}') and primary heading ('{primary_h1_text}') appear substantially unrelated; align title and H1 to reinforce primary page topic.",
                )

    # 1d. heading_repetition
    all_headings = [
        h.get_text(strip=True).casefold()
        for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        if not _is_element_hidden(h) and h.get_text(strip=True)
    ]
    if all_headings:
        heading_counts = Counter(all_headings)
        repeated = [h for h, c in heading_counts.items() if c > 1]
        if repeated:
            preview = ", ".join(repr(h) for h in repeated[:3])
            add_issue(
                "heading_repetition",
                f"Heading text is repeated across multiple sections: {preview}; use distinct headings to organize content clearly.",
            )

    # 1e. missing_subheadings (for long content pages)
    subheadings = [
        h
        for h in soup.find_all(["h2", "h3"])
        if not _is_element_hidden(h) and h.get_text(strip=True)
    ]
    if word_count >= LONG_CONTENT_SUBHEADING_WORD_THRESHOLD and len(subheadings) == 0:
        if not _is_synthetic_dummy_text(words):
            add_issue(
                "missing_subheadings",
                f"Page contains extensive content ({word_count} words) but lacks <h2> or <h3> subheadings; add subheadings to organize content and improve scannability.",
            )

    # 2. Paragraph checks
    visible_paragraphs: list[tuple[str, list[str]]] = []
    for p in soup.find_all("p"):
        if _is_element_hidden(p):
            continue
        p_text = p.get_text(strip=True)
        p_words = extract_meaningful_words(p_text)
        if p_words:
            visible_paragraphs.append((p_text, p_words))

    # 2a. short_paragraphs
    if len(visible_paragraphs) >= MIN_PARAGRAPHS_FOR_SHORT_CHECK:
        short_count = sum(1 for _, pw in visible_paragraphs if len(pw) < SHORT_PARAGRAPH_WORD_LIMIT)
        if short_count / len(visible_paragraphs) >= SHORT_PARAGRAPH_RATIO_THRESHOLD:
            add_issue(
                "short_paragraphs",
                f"Page is dominated by very short paragraphs ({short_count}/{len(visible_paragraphs)} paragraphs have fewer than {SHORT_PARAGRAPH_WORD_LIMIT} words); consider combining related thoughts to improve depth.",
            )

    # 2b. long_paragraphs
    for _, pw in visible_paragraphs:
        if len(pw) > LONG_PARAGRAPH_WORD_LIMIT:
            if not _is_synthetic_dummy_text(pw):
                add_issue(
                    "long_paragraphs",
                    f"Paragraph has excessive length ({len(pw)} words; recommended maximum is {LONG_PARAGRAPH_WORD_LIMIT} words for web readability); consider breaking it into smaller paragraphs.",
                )
                break

    # 3. Readability & keyword optimization checks
    # 3a. keyword_stuffing
    content_terms = [
        w.casefold()
        for w in words
        if len(w) >= 4 and w.casefold() not in STOP_WORDS and not w.isdigit()
    ]
    if len(content_terms) >= MIN_WORDS_FOR_READABILITY and not _is_synthetic_dummy_text(words):
        freq = Counter(content_terms)
        for term, count in freq.most_common(1):
            density = count / len(content_terms)
            if count >= KEYWORD_STUFFING_MIN_COUNT and density >= KEYWORD_STUFFING_DENSITY_THRESHOLD:
                add_issue(
                    "keyword_stuffing",
                    f"Potential keyword stuffing detected: the term '{term}' appears {count} times ({density:.1%} density; recommended density is under 5.0%).",
                )
                break

    # 3b. low_content_readability
    score = calculate_flesch_reading_ease(text)
    if score is not None and score < FLESCH_READING_EASE_LOW_THRESHOLD:
        if not _is_synthetic_dummy_text(words):
            add_issue(
                "low_content_readability",
                f"Content readability score is very low (Flesch Reading Ease: {score:.1f}/100; scores below {FLESCH_READING_EASE_LOW_THRESHOLD:.0f} indicate very difficult-to-read content). Consider shortening sentences and using simpler vocabulary.",
            )

    # 3c. weak_content_relevance
    if word_count >= 100 and page_title and visible_h1s and not _is_synthetic_dummy_text(words):
        topical_words = {
            w.casefold()
            for w in extract_meaningful_words(page_title + " " + " ".join(h.get_text() for h in visible_h1s))
            if len(w) >= 4 and w.casefold() not in STOP_WORDS and w.casefold() not in GENERIC_TOPIC_WORDS
        }
        if topical_words:
            if visible_paragraphs:
                body_terms = {w.casefold() for _, pw in visible_paragraphs for w in pw}
            else:
                heading_terms = {
                    w.casefold()
                    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
                    for w in extract_meaningful_words(h.get_text())
                }
                body_terms = {w.casefold() for w in words if w.casefold() not in heading_terms}
            if not (topical_words & body_terms):
                add_issue(
                    "weak_content_relevance",
                    "Page title, headings, and body content appear weakly aligned; key terms from the title/headings were not found in the body text.",
                )

    return issues


def summarize_readability_audit(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate Content Readability & Keyword Optimization Summary statistics from crawled pages."""
    html_pages = [
        p
        for p in results
        if p.get("status_code", 200) < 400
        and p.get("error") is None
        and (p.get("content_type") is None or is_html_content(p.get("content_type")))
    ]
    pages_audited = len(html_pages)

    all_issues = [
        issue
        for page in results
        for issue in page.get("seo_issues", [])
        if issue.get("type") in READABILITY_ISSUE_TYPES
    ]

    pages_with_issues = sum(
        1
        for p in results
        if any(
            i.get("type") in READABILITY_ISSUE_TYPES
            for i in p.get("seo_issues", [])
        )
    )
    keyword_stuffing_count = sum(
        1 for i in all_issues if i.get("type") == "keyword_stuffing"
    )
    low_readability_count = sum(
        1 for i in all_issues if i.get("type") == "low_content_readability"
    )
    heading_issues_count = sum(
        1
        for i in all_issues
        if i.get("type")
        in (
            "missing_primary_heading",
            "multiple_primary_headings",
            "heading_content_mismatch",
            "heading_repetition",
            "missing_subheadings",
        )
    )

    return {
        "pages_audited_count": pages_audited,
        "pages_with_issues_count": pages_with_issues,
        "keyword_stuffing_count": keyword_stuffing_count,
        "low_readability_count": low_readability_count,
        "heading_issues_count": heading_issues_count,
        "total_readability_issues": len(all_issues),
        "severity_counts": {
            "error": sum(1 for i in all_issues if i.get("severity") == "error"),
            "warning": sum(1 for i in all_issues if i.get("severity") == "warning"),
            "info": sum(1 for i in all_issues if i.get("severity") == "info"),
        },
    }


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
    page_contents: dict[str, tuple[str | None, int]] = {}
    page_link_data: dict[str, dict[str, Any]] = {}
    page_external_links: dict[str, list[dict[str, Any]]] = {}
    crawl_start_time = time.perf_counter()

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
            request_start_time = time.perf_counter()
            response = httpx.get(current_url, headers=headers, timeout=10.0)
            measured_elapsed = time.perf_counter() - request_start_time
            response_time_seconds = _extract_response_time_seconds(
                response, measured_elapsed
            )
            response_time_ms = round(response_time_seconds * 1000, 2)
            page_size_bytes = len(response.content)
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
                    "incoming_internal_links": 0,
                    "outgoing_internal_links": 0,
                    "outgoing_external_links": 0,
                    "external_links": [],
                    "hreflang_count": 0,
                    "hreflang_links": [],
                    "has_open_graph": False,
                    "has_twitter_card": False,
                    "readability_score": None,
                    "canonical_url": None,
                    "content_type": content_type,
                    "response_time_ms": response_time_ms,
                    "page_size_bytes": page_size_bytes,
                    "is_https": urlparse(current_url).scheme.lower() == "https",
                    "in_sitemap": False,
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
            target_host_fold = (target_hostname or "").casefold()

            for tag in anchor_tags:
                href = tag.get("href")
                if href is None:
                    continue

                absolute_url = urljoin(current_url, href)
                clean_url, _ = urldefrag(absolute_url)
                parsed_url = urlparse(clean_url)

                if parsed_url.scheme not in ("http", "https"):
                    continue

                if (parsed_url.hostname or "").casefold() != target_host_fold:
                    continue

                discovered_urls.add(clean_url)

            sitemap_urls = set()
            for loc_tag in soup.find_all("loc"):
                loc_text = loc_tag.get_text(strip=True)
                if not loc_text:
                    continue
                abs_loc = urljoin(current_url, loc_text)
                clean_loc, _ = urldefrag(abs_loc)
                parsed_loc = urlparse(clean_loc)
                if (
                    parsed_loc.scheme in ("http", "https")
                    and (parsed_loc.hostname or "").casefold() == target_host_fold
                ):
                    sitemap_urls.add(clean_loc)

            if current_depth < max_depth:
                for next_url in discovered_urls | sitemap_urls:
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
            seo_issues.extend(
                detect_structured_data_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                )
            )
            if is_html_content(content_type):
                hreflang_links = _extract_hreflang_links(
                    current_url, soup, response.headers
                )
                results[-1]["hreflang_count"] = len(hreflang_links)
                results[-1]["hreflang_links"] = hreflang_links
                canonical_tags = _find_canonical_link_tags(soup)
                page_canonical_url = None
                if canonical_tags:
                    resolved_can, is_can_valid = _resolve_canonical_url(
                        current_url, canonical_tags[0].get("href")
                    )
                    if is_can_valid:
                        page_canonical_url = resolved_can
                results[-1]["canonical_url"] = page_canonical_url
                seo_issues.extend(
                    detect_page_hreflang_issues(
                        url=current_url,
                        hreflang_links=hreflang_links,
                        canonical_url=page_canonical_url,
                        is_https=urlparse(current_url).scheme.lower() == "https",
                    )
                )

                og_data, twitter_data, _ = _extract_social_metadata(soup)
                results[-1]["has_open_graph"] = bool(og_data)
                results[-1]["has_twitter_card"] = bool(twitter_data.get("twitter:card"))
                seo_issues.extend(
                    detect_social_seo_issues(
                        url=current_url,
                        soup=soup,
                        content_type=content_type,
                        canonical_url=page_canonical_url,
                    )
                )

                ext_links = _extract_external_links(
                    current_url, soup, target_hostname
                )
                page_external_links[current_url] = ext_links
                results[-1]["outgoing_external_links"] = len(ext_links)
                results[-1]["external_links"] = ext_links
                seo_issues.extend(
                    detect_external_link_seo_issues(current_url, ext_links)
                )
                page_link_data[current_url] = _extract_page_internal_link_data(
                    page_url=current_url,
                    soup=soup,
                    target_hostname=target_hostname,
                    response_headers=response.headers,
                )
                visible_text = extract_visible_text(soup)
                normalized_content = normalize_visible_content(visible_text)
                word_count = len(extract_meaningful_words(visible_text))
                page_contents[current_url] = (normalized_content, word_count)
                seo_issues.extend(
                    detect_content_quality_seo_issues(
                        url=current_url,
                        soup=soup,
                        content_type=content_type,
                        visible_text=visible_text,
                    )
                )
                readability_issues = detect_readability_and_keyword_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                    title=page_title,
                    visible_text=visible_text,
                )
                if any(i.get("type") == "missing_h1" for i in seo_issues):
                    readability_issues = [
                        i
                        for i in readability_issues
                        if i.get("type") != "missing_primary_heading"
                    ]
                seo_issues.extend(readability_issues)
                results[-1]["readability_score"] = calculate_flesch_reading_ease(visible_text)
            seo_issues.extend(
                detect_performance_seo_issues(
                    url=current_url,
                    content_type=content_type,
                    response_time_seconds=response_time_seconds,
                    page_size_bytes=page_size_bytes,
                )
            )
            seo_issues.extend(
                detect_accessibility_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                )
            )
            seo_issues.extend(
                detect_security_seo_issues(
                    url=current_url,
                    soup=soup,
                    content_type=content_type,
                    response_headers=response.headers,
                    target_hostname=target_hostname,
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
                    "incoming_internal_links": 0,
                    "outgoing_internal_links": 0,
                    "outgoing_external_links": 0,
                    "external_links": [],
                    "hreflang_count": 0,
                    "hreflang_links": [],
                    "has_open_graph": False,
                    "has_twitter_card": False,
                    "readability_score": None,
                    "canonical_url": None,
                    "content_type": None,
                    "response_time_ms": None,
                    "page_size_bytes": None,
                    "is_https": urlparse(current_url).scheme.lower() == "https",
                    "in_sitemap": False,
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
                    "incoming_internal_links": 0,
                    "outgoing_internal_links": 0,
                    "outgoing_external_links": 0,
                    "external_links": [],
                    "hreflang_count": 0,
                    "hreflang_links": [],
                    "has_open_graph": False,
                    "has_twitter_card": False,
                    "readability_score": None,
                    "canonical_url": None,
                    "content_type": error.response.headers.get("content-type"),
                    "response_time_ms": response_time_ms,
                    "page_size_bytes": page_size_bytes,
                    "is_https": urlparse(current_url).scheme.lower() == "https",
                    "in_sitemap": False,
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
    detect_duplicate_content_issues(results, page_contents)
    detect_internal_link_structure_issues(
        results, clean_start_url, page_link_data
    )
    measured_crawl_duration = max(0.0, time.perf_counter() - crawl_start_time)
    summed_page_seconds = sum(
        (page.get("response_time_ms") or 0.0) / 1000.0 for page in results
    )
    total_crawl_duration = max(measured_crawl_duration, summed_page_seconds)
    detect_slow_crawl_issue(results, total_crawl_duration)
    detect_sitemap_and_robots_issues(
        results=results,
        start_url=clean_start_url,
        robots_url=robots_url,
        robots_status_code=robots_response.status_code,
        robots_text=robots_response.text,
        robot_parser=robot_parser,
        headers=headers,
        page_link_data=page_link_data,
        checked_redirects=checked_redirects,
        initial_link_hops=initial_link_hops,
        link_results=link_results,
        user_agent=user_agent,
    )
    all_external_urls = [
        link["url"]
        for ext_list in page_external_links.values()
        for link in ext_list
    ]
    verification_results = verify_external_links(
        urls=all_external_urls,
        headers=headers,
        enabled=EXTERNAL_LINK_VERIFICATION_ENABLED,
        max_checks=MAX_EXTERNAL_LINK_CHECKS,
        timeout=EXTERNAL_LINK_TIMEOUT_SECONDS,
        max_concurrency=EXTERNAL_LINK_MAX_CONCURRENCY,
    )
    apply_external_link_verification(
        results=results,
        page_external_links=page_external_links,
        verification_results=verification_results,
    )
    detect_cross_page_hreflang_issues(results)

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
