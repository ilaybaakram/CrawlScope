import { useState } from "react";
import "./App.css";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

const PAGE_FIELDS = [
  "url",
  "depth",
  "status_code",
  "title",
  "meta_description",
  "h1",
  "total_links",
  "content_type",
  "response_time_ms",
  "page_size_bytes",
  "seo_issues",
  "error",
];

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
  const [crawlHistory, setCrawlHistory] = useState([]);

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

      <section className="card">
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
        </div>
      </section>

      <section className="card">
        <h2>Crawled Pages ({pages.length})</h2>
        {pages.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No pages were returned for this crawl."
              : "Submit a URL above and click Start Crawl to inspect pages."}
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
                  <th>Response Time</th>
                  <th>Page Size</th>
                  <th>Content-Type</th>
                </tr>
              </thead>
              <tbody>
                {pages.map((page) => (
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

      <section className="card">
        <div className="section-header">
          <h2>SEO Audit ({seoIssues.length})</h2>
          <div className="seo-metrics">
            <span className="metric-badge">
              Total SEO Issues: <strong>{seoIssues.length}</strong>
            </span>
            <span className="metric-badge">
              Pages with Issues: <strong>{pagesWithSeoIssues.length}</strong>
            </span>
          </div>
        </div>
        {seoIssues.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No SEO metadata or image alt text issues detected in this crawl."
              : "Missing or duplicate metadata and image alt text issues will appear here after a crawl."}
          </p>
        ) : (
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Page URL</th>
                  <th>Issue Type</th>
                  <th>Severity</th>
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
                {seoIssues.map((issue) => (
                  <tr
                    key={`${issue.url}-${issue.type}-${issue.canonical_url || ""}-${issue.robots_directive || ""}-${issue.directive_source || ""}-${Array.isArray(issue.schema_types) ? issue.schema_types.join(",") : ""}-${issue.word_count ?? ""}-${issue.response_time_ms ?? ""}-${issue.page_size_bytes ?? ""}-${issue.image_url || ""}-${issue.source_url || ""}`}
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

      <section className="card">
        <h2>Broken Links ({brokenLinks.length})</h2>
        {brokenLinks.length === 0 ? (
          <p className="empty-state">
            {crawlData
              ? "No broken links detected in this crawl."
              : "Broken or unreachable links will appear here after a crawl."}
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
                {brokenLinks.map((link) => (
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

      <section className="card">
        <div className="section-header">
          <h2>Session Crawl History ({crawlHistory.length})</h2>
          {crawlHistory.length > 0 && (
            <button
              type="button"
              className="clear-button"
              onClick={() => setCrawlHistory([])}
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
