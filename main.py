import os
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from crawler import (
    calculate_site_health_summary,
    crawl_website,
    summarize_external_links,
    summarize_hreflang_audit,
    summarize_readability_audit,
    summarize_security_audit,
    summarize_sitemap_coverage,
    summarize_social_audit,
)

app = FastAPI(title="CrawlScope API", version="0.1.0")

cors_origins_env = os.getenv("CORS_ORIGINS", "")
allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
allow_all_origins = False
if cors_origins_env:
    for origin in cors_origins_env.split(","):
        cleaned = origin.strip()
        if cleaned == "*":
            allow_all_origins = True
        elif cleaned and cleaned not in allowed_origins:
            allowed_origins.append(cleaned)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all_origins else allowed_origins,
    allow_credentials=not allow_all_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class CrawlRequest(BaseModel):
    start_url: str
    max_pages: int = Field(default=10, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=5)
    delay_seconds: float = Field(default=1.0, ge=0.5, le=10.0)

    @field_validator("start_url")
    @classmethod
    def validate_start_url(cls, value: str) -> str:
        cleaned = value.strip()
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(
                "start_url must be a valid HTTP or HTTPS URL with a hostname"
            )
        return cleaned


@app.get("/")
def read_root():
    return {
        "project": "CrawlScope",
        "message": "Welcome to CrawlScope!",
        "status": "running",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
    }


@app.get("/api/health")
def api_health_check():
    return health_check()


@app.post("/crawl")
def run_crawl(request: CrawlRequest) -> dict[str, Any]:
    try:
        links: list[dict[str, Any]] = []
        pages = crawl_website(
            start_url=request.start_url,
            max_pages=request.max_pages,
            delay_seconds=request.delay_seconds,
            max_depth=request.max_depth,
            link_results=links,
        )
        return {
            "start_url": request.start_url,
            "pages_crawled": len(pages),
            "links_checked": len(links),
            "sitemap_coverage": summarize_sitemap_coverage(pages),
            "security_summary": summarize_security_audit(pages),
            "external_link_summary": summarize_external_links(pages),
            "hreflang_summary": summarize_hreflang_audit(pages),
            "social_summary": summarize_social_audit(pages),
            "readability_summary": summarize_readability_audit(pages),
            "site_health_summary": calculate_site_health_summary(pages),
            "pages": pages,
            "links": links,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Crawl failed: {str(exc)}",
        ) from exc


@app.post("/api/crawl")
def run_api_crawl(request: CrawlRequest) -> dict[str, Any]:
    return run_crawl(request)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
