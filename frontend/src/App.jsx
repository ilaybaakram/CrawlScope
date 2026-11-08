import { useState, useEffect, useMemo } from "react";
import "./App.css";

const rawApiBaseUrl = import.meta.env.VITE_API_BASE_URL;
const API_BASE_URL =
  typeof rawApiBaseUrl === "string"
    ? rawApiBaseUrl.replace(/\/+$/, "")
    : "http://127.0.0.1:8000";

const PAGE_FIELDS = [
  "url",
  "depth",
  "status_code",
  "title",
  "meta_description",
  "h1",
  "total_links",
  "incoming_internal_links",
  "outgoing_internal_links",
  "outgoing_external_links",
  "hreflang_count",
  "has_open_graph",
  "readability_score",
  "content_type",
  "response_time_ms",
  "page_size_bytes",
  "is_https",
  "in_sitemap",
  "seo_issues",
  "error",
];

const SECURITY_ISSUE_TYPES = new Set([
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
]);

const EXTERNAL_LINK_ISSUE_TYPES = new Set([
  "broken_external_link",
  "external_redirect",
  "external_link_timeout",
  "external_link_ssl_error",
  "external_link_target_blank_noopener",
  "external_link_target_blank_noreferrer",
  "external_link_to_http",
  "nofollow_external_link",
  "excessive_external_links",
]);

const HREFLANG_ISSUE_TYPES = new Set([
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
]);

const SOCIAL_ISSUE_TYPES = new Set([
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
]);

const READABILITY_ISSUE_TYPES = new Set([
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
]);

const LINK_FIELDS = ["url", "found_on", "status_code", "result", "error"];

function formatByteSize(bytes) {
  if (bytes === null || bytes === undefined) {
    return "—";
  }
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB (${bytes} B)`;
  }
  if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(1)} KB (${bytes} B)`;
  }
  return `${bytes} B`;
}

function escapeCsvCell(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const text =
    typeof value === "object" ? JSON.stringify(value) : String(value);
  if (
    text.includes(",") ||
    text.includes('"') ||
    text.includes("\n") ||
    text.includes("\r")
  ) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function buildCsvContent(rows, defaultFields) {
  const safeRows = Array.isArray(rows) ? rows : [];
  const fieldnames = [...defaultFields];

  for (const row of safeRows) {
    for (const key of Object.keys(row)) {
      if (!fieldnames.includes(key)) {
        fieldnames.push(key);
      }
    }
  }

  const headerLine = fieldnames.map(escapeCsvCell).join(",");
  const dataLines = safeRows.map((row) =>
    fieldnames.map((field) => escapeCsvCell(row[field])).join(",")
  );

  return [headerLine, ...dataLines].join("\n") + "\n";
}

function buildJsonContent(rows) {
  const safeRows = Array.isArray(rows) ? rows : [];
  return JSON.stringify(safeRows, null, 2) + "\n";
}

function triggerBrowserDownload(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}

function formatErrorMessage(detail) {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const field = Array.isArray(item.loc)
          ? item.loc[item.loc.length - 1]
          : "input";
        return `${field}: ${item.msg}`;
      })
      .join(" | ");
  }
  return "Request failed. Please check your crawl settings and try again.";
}

function App() {
  const [startUrl, setStartUrl] = useState("https://example.com");
  const [maxPages, setMaxPages] = useState(10);
  const [maxDepth, setMaxDepth] = useState(2);
  const [delaySeconds, setDelaySeconds] = useState(1.0);

  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [crawlData, setCrawlData] = useState(null);
  const [crawlHistory, setCrawlHistory] = useState(() => {
    try {
      const stored = localStorage.getItem("crawlscope_history");
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });

  const [pageSearch, setPageSearch] = useState("");
  const [pageStatusFilter, setPageStatusFilter] = useState("all");
  const [pageSortField, setPageSortField] = useState("url");
  const [pageSortDir, setPageSortDir] = useState("asc");

  const [issueSearch, setIssueSearch] = useState("");
  const [issueSeverityFilter, setIssueSeverityFilter] = useState("all");

  const [brokenLinkSearch, setBrokenLinkSearch] = useState("");

  useEffect(() => {
    try {
      localStorage.setItem(
        "crawlscope_history",
        JSON.stringify(crawlHistory.slice(0, 50))
      );
    } catch {
      // ignore
    }
  }, [crawlHistory]);

  function handleLoadHistoryUrl(url) {
    setStartUrl(url);
    const formEl = document.getElementById("crawl-form");
    if (formEl) {
      formEl.scrollIntoView({ behavior: "smooth" });
    }
  }

  function handleClearHistory() {
    setCrawlHistory([]);
    try {
      localStorage.removeItem("crawlscope_history");
    } catch {
      // ignore
    }
  }

  async function handleStartCrawl(event) {
    event.preventDefault();
    setIsLoading(true);
    setErrorMessage("");

    try {
      const response = await fetch(`${API_BASE_URL}/crawl`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          start_url: startUrl,
          max_pages: Number(maxPages),
          max_depth: Number(maxDepth),
          delay_seconds: Number(delaySeconds),
        }),
      });

      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        const message = payload?.detail
          ? formatErrorMessage(payload.detail)
          : `Server returned HTTP ${response.status}`;
        throw new Error(message);
      }

      setCrawlData(payload);

      const payloadLinks = Array.isArray(payload?.links) ? payload.links : [];
      const brokenCount = payloadLinks.filter(
        (link) => link.result === "broken" || link.result === "error"
      ).length;

      const historySummary = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        start_url: payload.start_url || startUrl,
        timestamp: new Date().toLocaleString(),
        pages_crawled: payload.pages_crawled ?? 0,
        links_checked: payload.links_checked ?? 0,
        broken_links: brokenCount,
      };

      setCrawlHistory((previousHistory) => [
        historySummary,
        ...previousHistory,
      ]);
    } catch (error) {
      setErrorMessage(
        error.message || "Could not connect to the CrawlScope backend API."
      );
    } finally {
      setIsLoading(false);
    }
  }

  const pages = crawlData?.pages || [];
  const links = crawlData?.links || [];
  const brokenLinks = links.filter(
    (link) => link.result === "broken" || link.result === "error"
  );
  const pagesWithSeoIssues = pages.filter(
    (page) => Array.isArray(page.seo_issues) && page.seo_issues.length > 0
  );
  const seoIssues = pages.flatMap((page) =>
    Array.isArray(page.seo_issues) ? page.seo_issues : []
  );

  const filteredAndSortedPages = useMemo(() => {
    return pages
      .filter((page) => {
        if (pageStatusFilter === "2xx") {
          if (!page.status_code || page.status_code < 200 || page.status_code >= 300) return false;
        } else if (pageStatusFilter === "3xx") {
          if (!page.status_code || page.status_code < 300 || page.status_code >= 400) return false;
        } else if (pageStatusFilter === "errors") {
          if (!page.status_code || page.status_code < 400) return false;
        }
        if (pageSearch.trim()) {
          const q = pageSearch.toLowerCase().trim();
          const matchUrl = typeof page.url === "string" && page.url.toLowerCase().includes(q);
          const matchTitle = typeof page.title === "string" && page.title.toLowerCase().includes(q);
          const matchH1 = typeof page.h1 === "string" && page.h1.toLowerCase().includes(q);
          if (!matchUrl && !matchTitle && !matchH1) return false;
        }
        return true;
      })
      .sort((a, b) => {
        let valA = a[pageSortField];
        let valB = b[pageSortField];
        if (valA === null || valA === undefined) valA = pageSortDir === "asc" ? Infinity : -Infinity;
        if (valB === null || valB === undefined) valB = pageSortDir === "asc" ? Infinity : -Infinity;
        if (typeof valA === "string") valA = valA.toLowerCase();
        if (typeof valB === "string") valB = valB.toLowerCase();
        if (valA < valB) return pageSortDir === "asc" ? -1 : 1;
        if (valA > valB) return pageSortDir === "asc" ? 1 : -1;
        return 0;
      });
  }, [pages, pageSearch, pageStatusFilter, pageSortField, pageSortDir]);

  const filteredSeoIssues = useMemo(() => {
    return seoIssues.filter((issue) => {
      if (issueSeverityFilter !== "all" && issue.severity !== issueSeverityFilter) {
        return false;
      }
      if (issueSearch.trim()) {
        const q = issueSearch.toLowerCase().trim();
        const matchType = typeof issue.type === "string" && issue.type.toLowerCase().includes(q);
        const matchUrl = typeof issue.url === "string" && issue.url.toLowerCase().includes(q);
        const matchMsg = typeof issue.message === "string" && issue.message.toLowerCase().includes(q);
        if (!matchType && !matchUrl && !matchMsg) return false;
      }
      return true;
    });
  }, [seoIssues, issueSeverityFilter, issueSearch]);

  const filteredBrokenLinks = useMemo(() => {
    return brokenLinks.filter((link) => {
      if (brokenLinkSearch.trim()) {
        const q = brokenLinkSearch.toLowerCase().trim();
        const matchUrl = typeof link.url === "string" && link.url.toLowerCase().includes(q);
        const matchFoundOn = typeof link.found_on === "string" && link.found_on.toLowerCase().includes(q);
        const matchError = typeof link.error === "string" && link.error.toLowerCase().includes(q);
        if (!matchUrl && !matchFoundOn && !matchError) return false;
      }
      return true;
    });
  }, [brokenLinks, brokenLinkSearch]);
  const sitemapCoverage = crawlData?.sitemap_coverage || {
    sitemap_urls_count: pages.filter((p) => p.in_sitemap).length,
    crawled_urls_count: pages.length,
    missing_from_sitemap_count: pages.filter((p) => p.in_sitemap === false)
      .length,
    sitemap_not_crawled_count: 0,
  };
  const securitySummary = crawlData?.security_summary || {
    https_pages_count: pages.filter(
      (p) =>
        p.is_https === true ||
        (p.is_https === undefined &&
          typeof p.url === "string" &&
          p.url.startsWith("https://"))
    ).length,
    http_pages_count: pages.filter(
      (p) =>
        p.is_https === false ||
        (p.is_https === undefined &&
          typeof p.url === "string" &&
          p.url.startsWith("http://"))
    ).length,
    mixed_content_pages_count: pages.filter(
      (p) =>
        Array.isArray(p.seo_issues) &&
        p.seo_issues.some((i) => i.type === "mixed_content")
    ).length,
    missing_security_headers_pages_count: pages.filter(
      (p) =>
        Array.isArray(p.seo_issues) &&
        p.seo_issues.some(
          (i) => typeof i.type === "string" && i.type.startsWith("missing_") && SECURITY_ISSUE_TYPES.has(i.type)
        )
    ).length,
    total_security_issues: seoIssues.filter((i) =>
      SECURITY_ISSUE_TYPES.has(i.type)
    ).length,
    severity_counts: {
      error: seoIssues.filter(
        (i) => SECURITY_ISSUE_TYPES.has(i.type) && i.severity === "error"
      ).length,
      warning: seoIssues.filter(
        (i) => SECURITY_ISSUE_TYPES.has(i.type) && i.severity === "warning"
      ).length,
      info: seoIssues.filter(
        (i) => SECURITY_ISSUE_TYPES.has(i.type) && i.severity === "info"
      ).length,
    },
  };
  const externalLinkSummary = crawlData?.external_link_summary || {
    unique_external_urls: 0,
    verified_count: 0,
    broken_count: 0,
    redirect_count: 0,
    verification_failures_count: 0,
    blocked_count: 0,
    unverified_count: 0,
    verification_enabled: true,
    verification_disabled: false,
    limit_reached: false,
    max_external_checks: 50,
  };
  const hreflangSummary = crawlData?.hreflang_summary || {
    pages_with_hreflang_count: pages.filter(
      (p) => (p.hreflang_count || 0) > 0
    ).length,
    invalid_hreflang_count: seoIssues.filter(
      (i) => i.type === "invalid_hreflang"
    ).length,
    missing_return_links_count: seoIssues.filter(
      (i) => i.type === "hreflang_missing_return_link"
    ).length,
    uncrawled_targets_count: seoIssues.filter(
      (i) => i.type === "hreflang_target_not_crawled"
    ).length,
    canonical_conflicts_count: seoIssues.filter(
      (i) => i.type === "hreflang_canonical_conflict"
    ).length,
    total_hreflang_issues: seoIssues.filter((i) =>
      HREFLANG_ISSUE_TYPES.has(i.type)
    ).length,
    severity_counts: {
      error: seoIssues.filter(
        (i) => HREFLANG_ISSUE_TYPES.has(i.type) && i.severity === "error"
      ).length,
      warning: seoIssues.filter(
        (i) => HREFLANG_ISSUE_TYPES.has(i.type) && i.severity === "warning"
      ).length,
      info: seoIssues.filter(
        (i) => HREFLANG_ISSUE_TYPES.has(i.type) && i.severity === "info"
      ).length,
    },
  };
  const socialSummary = crawlData?.social_summary || {
    pages_with_og_count: pages.filter((p) => p.has_open_graph).length,
    pages_with_twitter_card_count: pages.filter((p) => p.has_twitter_card).length,
    missing_required_og_count: seoIssues.filter((i) =>
      [
        "missing_og_title",
        "missing_og_description",
        "missing_og_image",
        "missing_og_url",
        "missing_og_type",
      ].includes(i.type)
    ).length,
    invalid_social_metadata_count: seoIssues.filter((i) =>
      [
        "invalid_og_url",
        "og_image_invalid_url",
        "invalid_twitter_card",
        "og_url_canonical_conflict",
        "duplicate_og_tags",
        "empty_og_metadata",
      ].includes(i.type)
    ).length,
    total_social_issues: seoIssues.filter((i) =>
      SOCIAL_ISSUE_TYPES.has(i.type)
    ).length,
    severity_counts: {
      error: seoIssues.filter(
        (i) => SOCIAL_ISSUE_TYPES.has(i.type) && i.severity === "error"
      ).length,
      warning: seoIssues.filter(
        (i) => SOCIAL_ISSUE_TYPES.has(i.type) && i.severity === "warning"
      ).length,
      info: seoIssues.filter(
        (i) => SOCIAL_ISSUE_TYPES.has(i.type) && i.severity === "info"
      ).length,
    },
  };
  const readabilitySummary = crawlData?.readability_summary || {
    pages_audited_count: pages.filter((p) => p.status_code === 200).length,
    pages_with_issues_count: pages.filter(
      (p) =>
        Array.isArray(p.seo_issues) &&
        p.seo_issues.some((i) => READABILITY_ISSUE_TYPES.has(i.type))
    ).length,
    keyword_stuffing_count: seoIssues.filter(
      (i) => i.type === "keyword_stuffing"
    ).length,
    low_readability_count: seoIssues.filter(
      (i) => i.type === "low_content_readability"
    ).length,
    heading_issues_count: seoIssues.filter((i) =>
      [
        "missing_primary_heading",
        "multiple_primary_headings",
        "heading_content_mismatch",
        "missing_subheadings",
        "heading_repetition",
      ].includes(i.type)
    ).length,
    total_readability_issues: seoIssues.filter((i) =>
      READABILITY_ISSUE_TYPES.has(i.type)
    ).length,
    severity_counts: {
      error: seoIssues.filter(
        (i) => READABILITY_ISSUE_TYPES.has(i.type) && i.severity === "error"
      ).length,
      warning: seoIssues.filter(
        (i) => READABILITY_ISSUE_TYPES.has(i.type) && i.severity === "warning"
      ).length,
      info: seoIssues.filter(
        (i) => READABILITY_ISSUE_TYPES.has(i.type) && i.severity === "info"
      ).length,
    },
  };

  const siteHealth = crawlData?.site_health_summary || {
    site_health_score: 100,
    health_rating: "Excellent",
    total_pages_audited: pages.length,
    total_findings: seoIssues.length,
    severity_counts: {
      error: seoIssues.filter((i) => i.severity === "error").length,
      warning: seoIssues.filter((i) => i.severity === "warning").length,
      info: seoIssues.filter((i) => i.severity === "info").length,
    },
    categories: {
      technical_seo: { name: "Technical SEO", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      content_quality: { name: "Content Quality", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      crawlability: { name: "Crawlability & Internal Linking", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      accessibility: { name: "Accessibility", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      performance: { name: "Performance", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      security: { name: "Security & HTTPS", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      international_seo: { name: "International SEO", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      social_sharing: { name: "Social Sharing", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
      structured_data: { name: "Structured Data", score: 100, pages_audited_count: 0, total_findings: 0, severity_counts: { error: 0, warning: 0, info: 0 }, status: "healthy" },
    },
    prioritized_issues: [],
    top_priority_issues: [],
  };

  const canDownload = Boolean(crawlData) && !isLoading;

  function handleDownloadPagesCsv() {
    if (!crawlData) return;
    const content = buildCsvContent(pages, PAGE_FIELDS);
    triggerBrowserDownload(content, "crawl_results.csv", "text/csv;charset=utf-8");
  }

  function handleDownloadPagesJson() {
    if (!crawlData) return;
    const content = buildJsonContent(pages);
    triggerBrowserDownload(
      content,
      "crawl_results.json",
      "application/json;charset=utf-8"
    );
  }

  function handleDownloadLinksCsv() {
    if (!crawlData) return;
    const content = buildCsvContent(links, LINK_FIELDS);
    triggerBrowserDownload(content, "link_results.csv", "text/csv;charset=utf-8");
  }

  function handleDownloadLinksJson() {
    if (!crawlData) return;
    const content = buildJsonContent(links);
    triggerBrowserDownload(
      content,
      "link_results.json",
      "application/json;charset=utf-8"
    );
  }

  function handleDownloadSiteHealthJson() {
    if (!crawlData) return;
    const content = JSON.stringify(siteHealth, null, 2);
    triggerBrowserDownload(
      content,
      "site_health_summary.json",
      "application/json;charset=utf-8"
    );
  }

  return (
    <div className="dashboard-container">
      <header className="dashboard-header">
        <div>
          <h1>CrawlScope</h1>
          <p className="subtitle">
            Website Auditing &amp; Technical Intelligence Platform
          </p>
        </div>
        <span className="api-badge">API: {API_BASE_URL}</span>
      </header>

      <nav className="dashboard-nav" aria-label="Dashboard Sections">
        <a href="#crawl-form" className="nav-pill">Crawl Settings</a>
        <a href="#site-health" className="nav-pill">Site Health</a>
        <a href="#prioritized-findings" className="nav-pill">Top Findings</a>
        <a href="#crawled-pages" className="nav-pill">Pages ({pages.length})</a>
        <a href="#seo-issues" className="nav-pill">SEO Audit ({seoIssues.length})</a>
        <a href="#broken-links" className="nav-pill">Broken Links ({brokenLinks.length})</a>
        <a href="#session-history" className="nav-pill">History ({crawlHistory.length})</a>
      </nav>

      <section className="card" id="crawl-form">
        <h2>Crawl Configuration</h2>
        <form className="crawl-form" onSubmit={handleStartCrawl}>
          <div className="form-group form-group-url">
            <label htmlFor="start-url">Website URL</label>
            <input
              id="start-url"
              type="url"
              required
              placeholder="https://example.com"
              value={startUrl}
              onChange={(event) => setStartUrl(event.target.value)}
              disabled={isLoading}
            />
          </div>

          <div className="form-row">
            <div className="form-group">
              <label htmlFor="max-pages">Maximum Pages (1–100)</label>
              <input
                id="max-pages"
                type="number"
                min="1"
                max="100"
                required
                value={maxPages}
                onChange={(event) => setMaxPages(event.target.value)}
                disabled={isLoading}
              />
            </div>

            <div className="form-group">
              <label htmlFor="max-depth">Maximum Depth (0–5)</label>
              <input
                id="max-depth"
                type="number"
                min="0"
                max="5"
                required
                value={maxDepth}
                onChange={(event) => setMaxDepth(event.target.value)}
                disabled={isLoading}
              />
            </div>

            <div className="form-group">
              <label htmlFor="delay-seconds">Crawl Delay (0.5–10s)</label>
              <input
                id="delay-seconds"
                type="number"
                step="0.1"
                min="0.5"
                max="10.0"
                required
                value={delaySeconds}
                onChange={(event) => setDelaySeconds(event.target.value)}
                disabled={isLoading}
              />
            </div>
          </div>

          <div className="form-actions">
            <button
              type="submit"
              className="primary-button"
              disabled={isLoading}
            >
              {isLoading ? "Running Crawl..." : "Start Crawl"}
            </button>
            {isLoading && (
              <span className="loading-indicator" role="status">
                Crawling target website and checking links, please wait...
              </span>
            )}
          </div>
        </form>

        {errorMessage && (
          <div className="error-banner" role="alert">
            <strong>Error:</strong> {errorMessage}
          </div>
        )}
      </section>

      <section className="summary-grid">
        <div className="summary-card">
          <span className="summary-label">Pages Crawled</span>
          <span className="summary-value">
            {crawlData ? crawlData.pages_crawled : 0}
          </span>
        </div>

        <div className="summary-card">
          <span className="summary-label">Links Checked</span>
          <span className="summary-value">
            {crawlData ? crawlData.links_checked : 0}
          </span>
        </div>

        <div className="summary-card summary-card-danger">
          <span className="summary-label">Broken Links</span>
          <span className="summary-value">{brokenLinks.length}</span>
        </div>
      </section>

      <section className="card site-health-card" id="site-health">
        <div className="section-header">
          <h2>Site Health &amp; Audit Overview</h2>
          <span className="section-hint">
            Comprehensive audit scoring across all 9 technical categories
          </span>
        </div>

        <div className="site-health-header">
          <div className="site-health-main">
            <div
              className={`site-health-score-circle ${
                !crawlData
                  ? "score-good"
                  : siteHealth.site_health_score >= 90
                  ? "score-excellent"
                  : siteHealth.site_health_score >= 70
                  ? "score-good"
                  : siteHealth.site_health_score >= 50
                  ? "score-fair"
                  : "score-poor"
              }`}
            >
              <span>{crawlData ? siteHealth.site_health_score : "—"}</span>
            </div>
            <div className="site-health-meta">
              <span className="site-health-rating">
                {crawlData ? `${siteHealth.health_rating} Health Rating` : "Run crawl to calculate"}
              </span>
              <span className="site-health-subtitle">
                {crawlData
                  ? `${siteHealth.total_pages_audited} pages evaluated · ${siteHealth.total_findings} total findings`
                  : "Comprehensive 0–100 score based on weighted error, warning, and notice findings"}
              </span>
            </div>
          </div>

          <div className="seo-metrics">
            <span className="metric-badge">
              Pages Audited: <strong>{crawlData ? siteHealth.total_pages_audited : 0}</strong>
            </span>
            <span className="metric-badge">
              Errors: <strong style={{ color: "#dc2626" }}>{crawlData ? siteHealth.severity_counts.error : 0}</strong>
            </span>
            <span className="metric-badge">
              Warnings: <strong style={{ color: "#d97706" }}>{crawlData ? siteHealth.severity_counts.warning : 0}</strong>
            </span>
            <span className="metric-badge">
              Notices: <strong style={{ color: "#0284c7" }}>{crawlData ? siteHealth.severity_counts.info : 0}</strong>
            </span>
          </div>
        </div>

        <h3 style={{ marginTop: "1.25rem", marginBottom: "0.25rem", fontSize: "1.05rem" }}>
          Category Health Breakdown
        </h3>
        <div className="category-scores-grid">
          {Object.entries(siteHealth.categories || {}).map(([key, cat]) => (
            <div key={key} className="category-score-card">
              <div className="category-score-card-header">
                <span className="category-title">{cat.name}</span>
                <span
                  className={`category-score-val ${
                    !crawlData
                      ? "status-ok"
                      : cat.score >= 90
                      ? "status-ok"
                      : cat.score >= 70
                      ? "status-warning"
                      : "status-bad"
                  }`}
                >
                  {crawlData ? `${cat.score}` : "—"}
                </span>
              </div>
              <div className="category-details">
                <span>Audited: {crawlData ? cat.pages_audited_count : 0}</span>
                <span>Findings: {crawlData ? cat.total_findings : 0}</span>
              </div>
              <div className="category-severities">
                {cat.severity_counts.error > 0 && (
                  <span className="status-pill status-bad">{cat.severity_counts.error} error(s)</span>
                )}
                {cat.severity_counts.warning > 0 && (
                  <span className="status-pill status-warning">{cat.severity_counts.warning} warning(s)</span>
                )}
                {cat.severity_counts.info > 0 && (
                  <span className="status-pill status-info">{cat.severity_counts.info} notice(s)</span>
                )}
                {cat.total_findings === 0 && (
                  <span className="status-pill status-ok">Pass / Healthy</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="card" id="prioritized-findings">
        <div className="section-header">
          <h2>Prioritized Actionable Findings</h2>
          <span className="section-hint">
            Top recommendations ranked by severity and site-wide impact
          </span>
        </div>
        {crawlData && (siteHealth.prioritized_issues || []).length > 0 ? (
          <div className="prioritized-issues-list">
            {(siteHealth.prioritized_issues || []).slice(0, 8).map((issue) => (
              <div key={issue.issue_type} className="prioritized-issue-item">
                <div className="prioritized-issue-header">
                  <code>{issue.issue_type}</code>
                  <span
                    className={
                      issue.severity === "error"
                        ? "status-pill status-bad"
                        : issue.severity === "info"
                        ? "status-pill status-info"
                        : "status-pill status-warning"
                    }
                  >
                    {issue.severity}
                  </span>
                  <span className="metric-badge">{issue.category_name}</span>
                  <span className="affected-badge">
                    {issue.affected_pages_count} page(s) affected ({issue.total_occurrences} occurrence{issue.total_occurrences > 1 ? "s" : ""})
                  </span>
                </div>
                <div className="prioritized-recommendation">
                  <strong>Recommendation:</strong> {issue.recommendation}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="empty-state">
            {crawlData
              ? "No priority actionable issues found. All audited categories are healthy!"
              : "Priority recommendations ranked by severity and impact will appear here after a crawl."}
          </p>
        )}
      </section>

      <section className="card">
        <div className="section-header">
          <h2>Sitemap Coverage Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              URLs Listed in Sitemap:{" "}
              <strong>{crawlData ? sitemapCoverage.sitemap_urls_count : 0}</strong>
            </span>
            <span className="metric-badge">
              URLs Crawled:{" "}
              <strong>{crawlData ? sitemapCoverage.crawled_urls_count : 0}</strong>
            </span>
            <span className="metric-badge">
              URLs Missing from Sitemap:{" "}
              <strong>
                {crawlData ? sitemapCoverage.missing_from_sitemap_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Sitemap URLs Not Crawled:{" "}
              <strong>
                {crawlData ? sitemapCoverage.sitemap_not_crawled_count : 0}
              </strong>
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>Security &amp; HTTPS Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              HTTPS Pages:{" "}
              <strong>
                {crawlData ? securitySummary.https_pages_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              HTTP Pages:{" "}
              <strong>
                {crawlData ? securitySummary.http_pages_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Mixed Content Pages:{" "}
              <strong>
                {crawlData ? securitySummary.mixed_content_pages_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Missing Security Headers:{" "}
              <strong>
                {crawlData
                  ? securitySummary.missing_security_headers_pages_count
                  : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Security Findings:{" "}
              <strong>
                {crawlData ? securitySummary.total_security_issues : 0}
              </strong>{" "}
              (
              {crawlData
                ? `${securitySummary.severity_counts?.warning ?? 0} warning, ${securitySummary.severity_counts?.info ?? 0} info, ${securitySummary.severity_counts?.error ?? 0} error`
                : "0 warning, 0 info, 0 error"}
              )
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>External Link &amp; Safety Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Unique External URLs:{" "}
              <strong>
                {crawlData ? externalLinkSummary.unique_external_urls : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Verified:{" "}
              <strong>
                {crawlData ? externalLinkSummary.verified_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Broken External:{" "}
              <strong>
                {crawlData ? externalLinkSummary.broken_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Redirects:{" "}
              <strong>
                {crawlData ? externalLinkSummary.redirect_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Failures / Timeouts / SSL:{" "}
              <strong>
                {crawlData ? externalLinkSummary.verification_failures_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Blocked (SSRF):{" "}
              <strong>
                {crawlData ? externalLinkSummary.blocked_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Unverified:{" "}
              <strong>
                {crawlData ? externalLinkSummary.unverified_count : 0}
              </strong>
              {crawlData?.external_link_summary?.limit_reached && " (Limit Reached)"}
              {crawlData?.external_link_summary?.verification_disabled && " (Disabled)"}
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>International SEO &amp; Hreflang Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Pages with Hreflang:{" "}
              <strong>
                {crawlData ? hreflangSummary.pages_with_hreflang_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Invalid Hreflang:{" "}
              <strong>
                {crawlData ? hreflangSummary.invalid_hreflang_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Missing Reciprocal Links:{" "}
              <strong>
                {crawlData ? hreflangSummary.missing_return_links_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Uncrawled Targets:{" "}
              <strong>
                {crawlData ? hreflangSummary.uncrawled_targets_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Canonical Conflicts:{" "}
              <strong>
                {crawlData ? hreflangSummary.canonical_conflicts_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Total Hreflang Findings:{" "}
              <strong>
                {crawlData ? hreflangSummary.total_hreflang_issues : 0}
              </strong>
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>Social Sharing Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Pages with Open Graph:{" "}
              <strong>
                {crawlData ? socialSummary.pages_with_og_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Pages with Twitter Card:{" "}
              <strong>
                {crawlData ? socialSummary.pages_with_twitter_card_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Missing Required OG:{" "}
              <strong>
                {crawlData ? socialSummary.missing_required_og_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Invalid Social Metadata:{" "}
              <strong>
                {crawlData ? socialSummary.invalid_social_metadata_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Total Social Findings:{" "}
              <strong>
                {crawlData ? socialSummary.total_social_issues : 0}
              </strong>
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>Content Readability & Keyword Summary</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Pages Audited:{" "}
              <strong>
                {crawlData ? readabilitySummary.pages_audited_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Pages with Issues:{" "}
              <strong>
                {crawlData ? readabilitySummary.pages_with_issues_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Keyword Stuffing:{" "}
              <strong>
                {crawlData ? readabilitySummary.keyword_stuffing_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Low Readability:{" "}
              <strong>
                {crawlData ? readabilitySummary.low_readability_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Heading Issues:{" "}
              <strong>
                {crawlData ? readabilitySummary.heading_issues_count : 0}
              </strong>
            </span>
            <span className="metric-badge">
              Total Readability Findings:{" "}
              <strong>
                {crawlData ? readabilitySummary.total_readability_issues : 0}
              </strong>
            </span>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="section-header">
          <h2>Download Crawl Results</h2>
          {!crawlData && (
            <span className="section-hint">
              Run a crawl first to enable result downloads
            </span>
          )}
        </div>
        <div className="download-grid">
          <button
            type="button"
            className="secondary-button"
            onClick={handleDownloadPagesCsv}
            disabled={!canDownload}
          >
            Download Pages CSV
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleDownloadPagesJson}
            disabled={!canDownload}
          >
            Download Pages JSON
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleDownloadLinksCsv}
            disabled={!canDownload}
          >
            Download Links CSV
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleDownloadLinksJson}
            disabled={!canDownload}
          >
            Download Links JSON
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={handleDownloadSiteHealthJson}
            disabled={!canDownload}
          >
            Download Site Health JSON
          </button>
        </div>
      </section>

      <section className="card" id="crawled-pages">
        <div className="section-header">
          <h2>Crawled Pages ({pages.length})</h2>
          {pages.length > 0 && (
            <span className="toolbar-count">
              Showing {filteredAndSortedPages.length} of {pages.length} pages
            </span>
          )}
        </div>

        {pages.length > 0 && (
          <div className="table-toolbar">
            <div className="toolbar-controls">
              <input
                type="text"
                className="toolbar-input"
                placeholder="Search URL, title, H1..."
                value={pageSearch}
                onChange={(e) => setPageSearch(e.target.value)}
                aria-label="Search pages"
              />
              <select
                className="toolbar-select"
                value={pageStatusFilter}
                onChange={(e) => setPageStatusFilter(e.target.value)}
                aria-label="Filter by status"
              >
                <option value="all">All Status Codes</option>
                <option value="2xx">2xx Success</option>
                <option value="3xx">3xx Redirects</option>
                <option value="errors">4xx / 5xx Errors</option>
              </select>
              <select
                className="toolbar-select"
                value={pageSortField}
                onChange={(e) => setPageSortField(e.target.value)}
                aria-label="Sort pages by"
              >
                <option value="url">Sort: URL</option>
                <option value="depth">Sort: Depth</option>
                <option value="status_code">Sort: Status</option>
                <option value="response_time_ms">Sort: Response Time</option>
                <option value="page_size_bytes">Sort: Page Size</option>
                <option value="readability_score">Sort: Readability</option>
              </select>
              <select
                className="toolbar-select"
                value={pageSortDir}
                onChange={(e) => setPageSortDir(e.target.value)}
                aria-label="Sort direction"
              >
                <option value="asc">Ascending</option>
                <option value="desc">Descending</option>
              </select>
            </div>
          </div>
        )}

        {pages.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No pages were returned for this crawl."
              : "Submit a URL above and click Start Crawl to inspect pages."}
          </p>
        ) : filteredAndSortedPages.length === 0 ? (
          <p className="empty-state">
            No pages match your search or filter criteria.
          </p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>URL</th>
                  <th>Depth</th>
                  <th>Status</th>
                  <th>Title</th>
                  <th>H1</th>
                  <th>Meta Description</th>
                  <th>Links</th>
                  <th>Incoming Internal</th>
                  <th>Outgoing Internal</th>
                  <th>Outgoing External</th>
                  <th>Hreflang</th>
                  <th>Open Graph</th>
                  <th>Readability</th>
                  <th>HTTPS</th>
                  <th>Security</th>
                  <th>In Sitemap</th>
                  <th>Response Time</th>
                  <th>Page Size</th>
                  <th>Content-Type</th>
                </tr>
              </thead>
              <tbody>
                {filteredAndSortedPages.map((page) => (
                  <tr key={page.url}>
                    <td className="url-cell">
                      <a
                        href={page.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {page.url}
                      </a>
                    </td>
                    <td>{page.depth}</td>
                    <td>
                      <span
                        className={
                          page.status_code && page.status_code < 400
                            ? "status-pill status-ok"
                            : "status-pill status-bad"
                        }
                      >
                        {page.status_code ?? "ERR"}
                      </span>
                    </td>
                    <td>{page.title || "—"}</td>
                    <td>{page.h1 || "—"}</td>
                    <td>{page.meta_description || "—"}</td>
                    <td>{page.total_links ?? 0}</td>
                    <td>{page.incoming_internal_links ?? 0}</td>
                    <td>{page.outgoing_internal_links ?? 0}</td>
                    <td>{page.outgoing_external_links ?? 0}</td>
                    <td>
                      {page.hreflang_count ? (
                        <span className="status-pill status-ok">
                          {page.hreflang_count} tag(s)
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {page.has_open_graph ? (
                        <span className="status-pill status-ok">Yes</span>
                      ) : (
                        <span className="status-pill status-warning">No</span>
                      )}
                    </td>
                    <td>
                      {page.readability_score !== null &&
                      page.readability_score !== undefined ? (
                        <span
                          className={`status-pill ${
                            page.readability_score >= 60
                              ? "status-ok"
                              : page.readability_score >= 30
                              ? "status-warning"
                              : "status-bad"
                          }`}
                        >
                          {page.readability_score}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      <span
                        className={
                          page.is_https ||
                          (page.is_https === undefined &&
                            typeof page.url === "string" &&
                            page.url.startsWith("https://"))
                            ? "status-pill status-ok"
                            : "status-pill status-warning"
                        }
                      >
                        {page.is_https ||
                        (page.is_https === undefined &&
                          typeof page.url === "string" &&
                          page.url.startsWith("https://"))
                          ? "HTTPS"
                          : "HTTP"}
                      </span>
                    </td>
                    <td>
                      {(() => {
                        const pageSecCount = Array.isArray(page.seo_issues)
                          ? page.seo_issues.filter((i) =>
                              SECURITY_ISSUE_TYPES.has(i.type)
                            ).length
                          : 0;
                        return (
                          <span
                            className={
                              pageSecCount === 0
                                ? "status-pill status-ok"
                                : "status-pill status-warning"
                            }
                          >
                            {pageSecCount === 0
                              ? "Secure"
                              : `${pageSecCount} issue(s)`}
                          </span>
                        );
                      })()}
                    </td>
                    <td>
                      <span
                        className={
                          page.in_sitemap
                            ? "status-pill status-ok"
                            : "status-pill status-warning"
                        }
                      >
                        {page.in_sitemap ? "Yes" : "No"}
                      </span>
                    </td>
                    <td>
                      {page.response_time_ms !== undefined &&
                      page.response_time_ms !== null
                        ? `${page.response_time_ms} ms`
                        : "—"}
                    </td>
                    <td>{formatByteSize(page.page_size_bytes)}</td>
                    <td>
                      {page.content_type || (page.error ? page.error : "—")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card" id="seo-issues">
        <div className="section-header">
          <h2>SEO Audit ({seoIssues.length})</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Total SEO Issues: <strong>{seoIssues.length}</strong>
            </span>
            <span className="metric-badge">
              Pages with Issues: <strong>{pagesWithSeoIssues.length}</strong>
            </span>
            {seoIssues.length > 0 && (
              <span className="toolbar-count">
                Showing {filteredSeoIssues.length} of {seoIssues.length} findings
              </span>
            )}
          </div>
        </div>

        {seoIssues.length > 0 && (
          <div className="table-toolbar">
            <div className="toolbar-controls">
              <input
                type="text"
                className="toolbar-input"
                placeholder="Search type, URL, message..."
                value={issueSearch}
                onChange={(e) => setIssueSearch(e.target.value)}
                aria-label="Search SEO issues"
              />
              <select
                className="toolbar-select"
                value={issueSeverityFilter}
                onChange={(e) => setIssueSeverityFilter(e.target.value)}
                aria-label="Filter by severity"
              >
                <option value="all">All Severities</option>
                <option value="error">Errors only</option>
                <option value="warning">Warnings only</option>
                <option value="info">Info / Notices only</option>
              </select>
            </div>
          </div>
        )}

        {seoIssues.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No SEO or accessibility issues detected in this crawl."
              : "SEO, content, performance, and accessibility audit issues will appear here after a crawl."}
          </p>
        ) : filteredSeoIssues.length === 0 ? (
          <p className="empty-state">
            No SEO issues match your search or filter criteria.
          </p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Page URL</th>
                  <th>Issue Type</th>
                  <th>Severity</th>
                  <th>Depth</th>
                  <th>Incoming Internal</th>
                  <th>Outgoing Internal</th>
                  <th>External URLs</th>
                  <th>Hreflang / Alternate URL</th>
                  <th>Missing Header</th>
                  <th>Insecure / Form URL</th>
                  <th>Sitemap URL</th>
                  <th>Canonical URL</th>
                  <th>Robots Directive</th>
                  <th>Schema Types</th>
                  <th>Word Count</th>
                  <th>Response Time</th>
                  <th>Page Size</th>
                  <th>Source URL</th>
                  <th>Destination URL</th>
                  <th>Redirect Chain</th>
                  <th>Image URL</th>
                  <th>HTTP Status</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {filteredSeoIssues.map((issue, index) => (
                  <tr
                    key={`${issue.url}-${issue.type}-${index}-${issue.message || ""}-${issue.canonical_url || ""}-${issue.robots_directive || ""}-${issue.directive_source || ""}-${Array.isArray(issue.schema_types) ? issue.schema_types.join(",") : ""}-${issue.word_count ?? ""}-${issue.response_time_ms ?? ""}-${issue.page_size_bytes ?? ""}-${issue.image_url || ""}-${issue.source_url || ""}`}
                  >
                    <td className="url-cell">
                      <a
                        href={issue.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {issue.url}
                      </a>
                    </td>
                    <td>
                      <code>{issue.type}</code>
                    </td>
                    <td>
                      <span
                        className={
                          issue.severity === "error"
                            ? "status-pill status-bad"
                            : issue.severity === "info"
                              ? "status-pill status-info"
                              : "status-pill status-warning"
                        }
                      >
                        {issue.severity}
                      </span>
                    </td>
                    <td>
                      {issue.depth !== undefined && issue.depth !== null
                        ? issue.depth
                        : "—"}
                    </td>
                    <td>
                      {issue.incoming_internal_links !== undefined &&
                      issue.incoming_internal_links !== null
                        ? issue.incoming_internal_links
                        : "—"}
                    </td>
                    <td>
                      {issue.outgoing_internal_links !== undefined &&
                      issue.outgoing_internal_links !== null
                        ? issue.outgoing_internal_links
                        : "—"}
                    </td>
                    <td className="url-cell">
                      {Array.isArray(issue.external_urls) &&
                      issue.external_urls.length > 0 ? (
                        <code>{issue.external_urls.join(", ")}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {issue.hreflang_url ? (
                        <code>{issue.hreflang_url}</code>
                      ) : Array.isArray(issue.missing_return_urls) &&
                        issue.missing_return_urls.length > 0 ? (
                        <code>{issue.missing_return_urls.join(", ")}</code>
                      ) : Array.isArray(issue.uncrawled_urls) &&
                        issue.uncrawled_urls.length > 0 ? (
                        <code>{issue.uncrawled_urls.join(", ")}</code>
                      ) : Array.isArray(issue.invalid_hreflangs) &&
                        issue.invalid_hreflangs.length > 0 ? (
                        <code>{issue.invalid_hreflangs.join(", ")}</code>
                      ) : Array.isArray(issue.duplicate_languages) &&
                        issue.duplicate_languages.length > 0 ? (
                        <code>{issue.duplicate_languages.join(", ")}</code>
                      ) : Array.isArray(issue.noncanonical_targets) &&
                        issue.noncanonical_targets.length > 0 ? (
                        <code>
                          {issue.noncanonical_targets
                            .map((nt) => `${nt.target_url} -> ${nt.canonical_url}`)
                            .join(", ")}
                        </code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {issue.missing_header ? (
                        <code>{issue.missing_header}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {issue.form_action ? (
                        <code>{issue.form_action}</code>
                      ) : Array.isArray(issue.insecure_urls) &&
                        issue.insecure_urls.length > 0 ? (
                        <code>{issue.insecure_urls.join(", ")}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {issue.sitemap_url ? (
                        <a
                          href={issue.sitemap_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {issue.sitemap_url}
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {issue.canonical_url ? (
                        issue.canonical_url.startsWith("http://") ||
                        issue.canonical_url.startsWith("https://") ? (
                          <a
                            href={issue.canonical_url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {issue.canonical_url}
                          </a>
                        ) : (
                          <code>{issue.canonical_url}</code>
                        )
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {issue.robots_directive ? (
                        <code>{issue.robots_directive}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {Array.isArray(issue.schema_types) &&
                      issue.schema_types.length > 0 ? (
                        <code>{issue.schema_types.join(", ")}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {issue.word_count !== undefined &&
                      issue.word_count !== null
                        ? issue.word_count
                        : "—"}
                    </td>
                    <td>
                      {issue.response_time_ms !== undefined &&
                      issue.response_time_ms !== null
                        ? `${issue.response_time_ms} ms`
                        : "—"}
                    </td>
                    <td>{formatByteSize(issue.page_size_bytes)}</td>
                    <td className="url-cell">
                      {issue.source_url ? (
                        <a
                          href={issue.source_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {issue.source_url}
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {issue.destination_url ? (
                        <a
                          href={issue.destination_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {issue.destination_url}
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="url-cell">
                      {Array.isArray(issue.redirect_chain) &&
                      issue.redirect_chain.length > 0
                        ? issue.redirect_chain.join(" → ")
                        : "—"}
                    </td>
                    <td className="url-cell">
                      {issue.image_url ? (
                        <a
                          href={issue.image_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {issue.image_url}
                        </a>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {issue.status_code !== undefined &&
                      issue.status_code !== null ? (
                        <span
                          className={
                            issue.status_code < 400
                              ? "status-pill status-ok"
                              : "status-pill status-bad"
                          }
                        >
                          {issue.status_code}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>{issue.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card" id="broken-links">
        <div className="section-header">
          <h2>Broken Links ({brokenLinks.length})</h2>
          {brokenLinks.length > 0 && (
            <span className="toolbar-count">
              Showing {filteredBrokenLinks.length} of {brokenLinks.length} links
            </span>
          )}
        </div>

        {brokenLinks.length > 0 && (
          <div className="table-toolbar">
            <div className="toolbar-controls">
              <input
                type="text"
                className="toolbar-input"
                placeholder="Search broken URL, source page, error..."
                value={brokenLinkSearch}
                onChange={(e) => setBrokenLinkSearch(e.target.value)}
                aria-label="Search broken links"
              />
            </div>
          </div>
        )}

        {brokenLinks.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No broken links detected in this crawl."
              : "Broken or unreachable links will appear here after a crawl."}
          </p>
        ) : filteredBrokenLinks.length === 0 ? (
          <p className="empty-state">
            No broken links match your search query.
          </p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Broken Link URL</th>
                  <th>Found On Page</th>
                  <th>Status</th>
                  <th>Result</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {filteredBrokenLinks.map((link) => (
                  <tr key={`${link.found_on}-${link.url}`}>
                    <td className="url-cell">
                      <a
                        href={link.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {link.url}
                      </a>
                    </td>
                    <td className="url-cell">{link.found_on}</td>
                    <td>
                      <span className="status-pill status-bad">
                        {link.status_code ?? "ERR"}
                      </span>
                    </td>
                    <td>{link.result}</td>
                    <td>{link.error || `HTTP ${link.status_code}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card" id="session-history">
        <div className="section-header">
          <h2>Session Crawl History ({crawlHistory.length})</h2>
          {crawlHistory.length > 0 && (
            <button
              type="button"
              className="clear-button"
              onClick={handleClearHistory}
            >
              Clear History
            </button>
          )}
        </div>
        {crawlHistory.length === 0 ? (
          <p className="empty-state">
            No crawls recorded in this browser session yet. Completed crawls
            will appear here with newest first.
          </p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Website URL</th>
                  <th>Date / Time</th>
                  <th>Pages Crawled</th>
                  <th>Links Checked</th>
                  <th>Broken Links</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {crawlHistory.map((entry) => (
                  <tr key={entry.id}>
                    <td className="url-cell">
                      <a
                        href={entry.start_url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {entry.start_url}
                      </a>
                    </td>
                    <td>{entry.timestamp}</td>
                    <td>{entry.pages_crawled}</td>
                    <td>{entry.links_checked}</td>
                    <td>
                      <span
                        className={
                          entry.broken_links > 0
                            ? "status-pill status-bad"
                            : "status-pill status-ok"
                        }
                      >
                        {entry.broken_links}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="action-button"
                        onClick={() => handleLoadHistoryUrl(entry.start_url)}
                      >
                        Load URL
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

export default App;
