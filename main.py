from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from crawler import (
    crawl_website,
    summarize_external_links,
    summarize_hreflang_audit,
    summarize_security_audit,
    summarize_sitemap_coverage,
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
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


@app.post("/crawl")
def run_crawl(request: CrawlRequest) -> dict[str, Any]:
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
        "pages": pages,
        "links": links,
    }
