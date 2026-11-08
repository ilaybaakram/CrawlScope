# CrawlScope

**CrawlScope** is an open-source website auditing and technical intelligence platform. It crawls web applications, systematically analyzes technical health across 9 auditing dimensions, calculates an overall Site Health Score (0–100), and delivers actionable recommendations through an interactive web dashboard and flexible export options (CSV & JSON).

---

## Key Capabilities & Features

### 1. Robust Web Crawler
- **Configurable Crawl Boundaries**: Control crawl limits via maximum page count (1–100), maximum link depth (0–5), and politeness delay intervals (0.5–10s).
- **Safety & SSRF Protection**: Built-in safeguards prevent requests to loopback addresses (`localhost`, `127.0.0.1`), private RFC 1918 subnets, link-local addresses, and cloud metadata endpoints.
- **Protocol & Network Resilience**: Handles network timeouts, TLS/SSL verification errors, redirect chains, and HTTP errors gracefully.

### 2. Comprehensive 9-Category SEO & Technical Audit
CrawlScope executes automated, deterministic audit checks across nine core categories:

1. **Technical SEO**: Missing titles, duplicated titles, long/short titles, missing meta descriptions, duplicate descriptions, canonical tag validation, canonical loops, and indexability conflicts.
2. **Content Quality & Readability**: Primary H1 checks, heading hierarchy (H1–H6), heading repetition, content-heading mismatch heuristics, keyword stuffing detection, and Flesch reading ease scoring.
3. **Crawlability & Internal Linking**: Orphan pages, low internal link counts, deep pages (> 4 levels), `rel="nofollow"` links, and nofollow page directives.
4. **Sitemap & Robots.txt**: XML sitemap discovery (`/sitemap.xml`, robots.txt references), sitemap parsing, uncrawled sitemap URLs, missing crawled URLs from sitemap, noncanonical sitemap targets, and conflicting robots.txt directives.
5. **Security & HTTPS**: Insecure HTTP pages, internal HTTP links on HTTPS sites, mixed active/passive content (images, scripts, styles, iframes), insecure form `action` targets, and missing HTTP security headers (`Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`).
6. **External Link & Outbound Safety**: Broken external links (4xx/5xx), external redirects, timeouts, SSL validation failures, and `target="_blank"` safety without `rel="noopener noreferrer"`.
7. **International SEO & Hreflang**: Missing hreflang on multilingual sites, invalid ISO 639-1 language or ISO 3166-1 alpha-2 region codes, missing reciprocal return links, uncrawled targets, and canonical conflicts.
8. **Social Sharing & Open Graph**: Missing Open Graph tags (`og:title`, `og:description`, `og:image`, `og:url`, `og:type`), Twitter Card tags, malformed social image URLs, and canonical mismatches.
9. **Accessibility & Usability**: Missing image alt text, empty button labels, empty anchor tags, missing form field labels, missing page language attributes, duplicate element IDs, and viewport configuration.

### 3. Site Health Scoring Engine
- **Deterministic 0–100 Health Score**: Evaluates severity-weighted deductions (`error`: -5 pts, `warning`: -2 pts, `info`: -0.5 pts) with per-page normalization across active categories.
- **Health Rating Tiers**: Clear qualitative classification:
  - **90–100**: *Excellent*
  - **75–89.9**: *Good*
  - **50–74.9**: *Fair*
  - **0–49.9**: *Poor*
- **Prioritized Actionable Findings**: Automatically aggregates and ranks issues by impact and frequency to provide high-leverage fixes first.

### 4. Interactive Dashboard & UI/UX
- **Visual Overview Cards**: Quick metrics on total pages crawled, links checked, broken links, and site health rating.
- **Jump Navigation**: Sticky section navigation for fast jumping across Crawl Settings, Site Health, Top Findings, Crawled Pages, SEO Audit, Broken Links, and History.
- **Client-Side Search & Filtering**: Real-time filtering by status codes (2xx, 3xx, errors), severity levels (error, warning, info), and text searches across URLs, titles, and messages.
- **Persistent Session History**: Local browser persistence for previous crawl runs with one-click URL re-loading.
- **Data Exporting**: Instant browser downloads for Pages CSV/JSON, Link Results CSV/JSON, and Site Health Summary JSON.

---

## Tech Stack

- **Backend**: Python 3.11+, [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/), [Requests](https://requests.readthedocs.io/)
- **Frontend**: [React 18](https://react.dev/), [Vite](https://vitejs.dev/), vanilla CSS with responsive layouts
- **Containerization**: Docker, Docker Compose, Nginx Alpine
- **Testing**: Python standard `unittest` library (223 comprehensive automated tests)

---

## Project Structure

```
CrawlScope/
├── Dockerfile           # Production container configuration for FastAPI backend
├── .dockerignore        # Excludes development artifacts from Docker context
├── docker-compose.yml   # Multi-container orchestration (Backend + Frontend)
├── .env.example         # Template for backend environment variables
├── crawler.py           # Core crawl engine, audit checks, scoring, and data aggregation
├── exporter.py          # CSV and JSON export routines
├── main.py              # FastAPI REST API endpoints, CORS, and server entrypoint
├── requirements.txt     # Python production and development dependencies
├── tests/               # Python unit and integration test suite
│   ├── test_crawler.py
│   ├── test_exporter.py
│   ├── test_main.py
│   └── ...
├── frontend/            # React + Vite frontend application
│   ├── Dockerfile       # Multi-stage production container with Nginx
│   ├── nginx.conf       # Nginx SPA routing and gzip compression config
│   ├── .env.example     # Template for frontend environment variables
│   ├── src/
│   │   ├── App.jsx      # Dashboard component, data tables, and state management
│   │   ├── App.css      # Design system, cards, tables, badges, and responsive styles
│   │   └── main.jsx     # Frontend entrypoint
│   ├── package.json
│   └── vite.config.js
└── README.md
```

---

## Local Development Setup

### Prerequisites
- **Python**: Version 3.11 or higher
- **Node.js**: Version 18.0 or higher (with `npm`)

### 1. Backend Setup
1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the backend API server:
   ```bash
   uvicorn main:app --reload --port 8000
   ```
   *Alternative:* Run directly with Python:
   ```bash
   python main.py
   ```
   The API will be available at `http://127.0.0.1:8000`. You can test the health endpoint at:
   ```bash
   curl http://127.0.0.1:8000/health
   ```

### 2. Frontend Setup
1. Navigate to the `frontend` folder and install dependencies:
   ```bash
   cd frontend
   npm install
   ```
2. Start the Vite development server:
   ```bash
   npm run dev
   ```
   Open your browser at `http://localhost:5173`.

---

## Running Automated Tests

Run the complete Python test suite:
```bash
.venv/bin/python -m unittest discover -s tests -v
```

Run frontend production build verification:
```bash
npm --prefix frontend run build
```

---

## Environment Variables

### Backend (`.env`)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8000` | Port for the FastAPI Uvicorn server in production. Bound dynamically by PaaS platforms. |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated list of allowed frontend domains (or `*` for public access). |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | (Build-time) URL of the backend API without trailing slash (e.g. `https://crawlscope-api.onrender.com`). |

---

## Live Deployment Guide

### Architecture Overview

CrawlScope is architected as a decoupled client-server application:
- **FastAPI Backend**: A stateless REST API that handles HTTP crawling, parsing, passive auditing, and scoring calculations.
- **React Frontend**: A Single Page Application (SPA) compiled into static HTML, JavaScript, and CSS assets.

This architecture enables flexible hosting with generous free tiers or self-hosted container infrastructure.

---

### Method 1: Free-Tier Cloud Deployment (Recommended)

This is the fastest, lowest-maintenance, and most cost-effective path to take CrawlScope live with 100% free hosting.

#### Step 1: Deploy Backend to Render (Free Web Service)
1. Sign up or log in to [Render](https://render.com).
2. Click **New +** > **Web Service**.
3. Connect your GitHub repository: `https://github.com/ilaybaakram/CrawlScope`.
4. Configure the service settings:
   - **Name**: `crawlscope-api` (or your preferred name)
   - **Region**: Select the region closest to you (e.g. Frankfurt, Oregon, Ohio)
   - **Branch**: `main`
   - **Root Directory**: Leave blank (uses repo root)
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py` *(or `uvicorn main:app --host 0.0.0.0 --port $PORT`)*
   - **Instance Type**: `Free`
5. In **Advanced** > **Health Check Path**, enter: `/health`
6. Click **Create Web Service**.
7. Once deployed, copy your backend URL (e.g. `https://crawlscope-api.onrender.com`).

#### Step 2: Deploy Frontend to Vercel (Free Hobby Tier)
1. Sign up or log in to [Vercel](https://vercel.com).
2. Click **Add New...** > **Project**.
3. Import your GitHub repository: `CrawlScope`.
4. Configure the project settings:
   - **Framework Preset**: `Vite`
   - **Root Directory**: Click *Edit* and select `frontend`
   - **Build Command**: `npm run build` (auto-detected)
   - **Output Directory**: `dist` (auto-detected)
5. Expand **Environment Variables**:
   - **Key**: `VITE_API_BASE_URL`
   - **Value**: Your Render backend URL from Step 1 (e.g. `https://crawlscope-api.onrender.com`)
6. Click **Deploy**.
7. Once deployment finishes, copy your live frontend URL (e.g. `https://crawlscope.vercel.app`).

#### Step 3: Connect Frontend to Backend (CORS)
1. Return to your Render dashboard for `crawlscope-api`.
2. Go to **Environment** > **Add Environment Variable**:
   - **Key**: `CORS_ORIGINS`
   - **Value**: Your live Vercel frontend URL (e.g. `https://crawlscope.vercel.app`)
3. Click **Save Changes**. Render will automatically redeploy with the updated CORS policy.
4. Visit your live Vercel URL and run a test crawl!

---

### Method 2: Self-Hosted Docker Compose (Single Server / VPS)

For deploying onto a cloud VPS (DigitalOcean Droplet, Hetzner, AWS EC2, Linode) or on-premise server:

1. Clone the repository onto your host:
   ```bash
   git clone https://github.com/ilaybaakram/CrawlScope.git
   cd CrawlScope
   ```
2. Launch the backend and frontend services:
   ```bash
   docker compose up --build -d
   ```
3. Access CrawlScope:
   - **Frontend**: `http://<your-server-ip>:3000`
   - **Backend API**: `http://<your-server-ip>:8000`
   - **Backend Health Check**: `http://<your-server-ip>:8000/health`

To stop the services:
```bash
docker compose down
```

---

### Method 3: Cloud Container Deployment (Google Cloud Run / AWS App Runner)

To run the backend as a scalable, serverless container:

1. Build and tag the Docker image:
   ```bash
   docker build -t crawlscope-backend .
   ```
2. Push to your container registry (e.g. Google Artifact Registry, AWS ECR, Docker Hub).
3. Deploy the container with:
   - Target port: `8000` (or platform default `$PORT`)
   - Environment variable: `CORS_ORIGINS=https://your-frontend-domain.com`
   - Healthcheck path: `/health`

---

## Production Pre-Flight Checklist

Before launching to live users:

- [x] **SSRF Protection Verified**: Internal IP addresses, private subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local, and cloud metadata endpoints (`169.254.169.254`) are blocked deterministically.
- [x] **CORS Restrictive Mode**: Production domain explicitly configured in `CORS_ORIGINS`.
- [x] **Zero Hardcoded Secrets**: No API keys, credentials, tokens, or personal paths in code.
- [x] **TLS/HTTPS Ready**: All frontend assets served securely via HTTPS; backend endpoints accept HTTPS traffic.
- [x] **Health Check Endpoint**: `/health` and `/api/health` respond with HTTP 200 `{"status": "healthy"}` for container orchestrators and uptime monitors.
- [x] **Automated Tests**: 100% test pass rate across all audit modules and API endpoints.

---

## License

This project is licensed under the MIT License.
