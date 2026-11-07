import { useState } from "react";
import "./App.css";

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

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
                    <td>{page.content_type || (page.error ? page.error : "—")}</td>
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
    </div>
  );
}

export default App;

