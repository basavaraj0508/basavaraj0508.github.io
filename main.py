"""
Job Aggregator API (Indeed + Dice via RSS)

Run locally:
  pip install -r requirements.txt
  uvicorn main:app --reload --port 8000

Example:
  http://localhost:8000/jobs?q=devops&location=Hartford%2C%20CT&limit=30
"""

from __future__ import annotations

import re
import time
import html
import hashlib
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlencode, quote_plus

import requests
import feedparser
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------
# App / CORS (so GitHub Pages can call it)
# ----------------------------
app = FastAPI(title="Job Aggregator API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your GitHub Pages domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Basic in-memory cache
# ----------------------------
_CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes

def cache_get(key: str) -> Optional[Any]:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return val

def cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)

def make_cache_key(*parts: str) -> str:
    raw = "||".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------------------
# Data model
# ----------------------------
@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    url: str
    published: Optional[str] = None
    summary: Optional[str] = None
    tags: Optional[List[str]] = None

# ----------------------------
# HTTP helper
# ----------------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (JobAggregator/1.0; +https://example.com)"
})

def fetch_feed(url: str, timeout: int = 20) -> feedparser.FeedParserDict:
    """
    Downloads RSS/Atom and returns parsed feed.
    """
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return feedparser.parse(r.text)

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def soup_text(html_snippet: str) -> str:
    if not html_snippet:
        return ""
    soup = BeautifulSoup(html_snippet, "html.parser")
    return clean_text(soup.get_text(" "))

# ----------------------------
# Source: Indeed (RSS)
# ----------------------------
def indeed_rss_url(q: str, location: str, radius_miles: int = 25, start: int = 0) -> str:
    """
    Indeed RSS supports format=rss in many regions.
    If this stops working in your region, switch this adapter to a partner API or aggregator API.
    """
    base = "https://www.indeed.com/jobs"
    params = {
        "q": q,
        "l": location,
        "radius": radius_miles,
        "start": start,
        "format": "rss",
    }
    return f"{base}?{urlencode(params)}"

def parse_indeed(q: str, location: str, limit: int, radius_miles: int = 25) -> List[Job]:
    """
    Fetches multiple pages (RSS start offsets) until `limit` is satisfied or pages are exhausted.
    """
    results: List[Job] = []
    start = 0
    page_size_guess = 10  # RSS pages often ~10
    while len(results) < limit and start <= 50:  # safeguard
        url = indeed_rss_url(q=q, location=location, radius_miles=radius_miles, start=start)
        feed = fetch_feed(url)
        if not feed.entries:
            break

        for e in feed.entries:
            title = clean_text(getattr(e, "title", "") or "")
            link = clean_text(getattr(e, "link", "") or "")
            published = clean_text(getattr(e, "published", "") or "")
            summary_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            summary = soup_text(summary_raw)

            # Indeed titles often look like: "DevOps Engineer - Company"
            # We'll try to split but keep safe defaults.
            company = ""
            job_title = title
            if " - " in title:
                parts = title.split(" - ", 1)
                job_title = parts[0].strip()
                company = parts[1].strip()

            results.append(Job(
                source="indeed",
                title=job_title,
                company=company or "Unknown",
                location=location or "Unknown",
                url=link,
                published=published or None,
                summary=summary[:800] if summary else None,
                tags=derive_tags(job_title, summary),
            ))

            if len(results) >= limit:
                break

        start += page_size_guess

    return results

# ----------------------------
# Source: Dice (RSS)
# ----------------------------
def dice_rss_url(q: str, location: str, radius_miles: int = 30, page: int = 1) -> str:
    """
    Dice RSS endpoints can change. Many setups used:
      https://www.dice.com/jobs/rss?text=devops&location=Hartford%2C%20CT&radius=30

    If this URL does not return RSS in your area, update ONLY this function.
    """
    base = "https://www.dice.com/jobs/rss"
    params = {
        "text": q,
        "location": location,
        "radius": radius_miles,
        # Some RSS endpoints accept paging; if not, it will just ignore.
        "page": page,
    }
    return f"{base}?{urlencode(params)}"

def parse_dice(q: str, location: str, limit: int, radius_miles: int = 30) -> List[Job]:
    results: List[Job] = []
    page = 1

    while len(results) < limit and page <= 10:  # safeguard
        url = dice_rss_url(q=q, location=location, radius_miles=radius_miles, page=page)
        feed = fetch_feed(url)
        if not feed.entries:
            break

        for e in feed.entries:
            title = clean_text(getattr(e, "title", "") or "")
            link = clean_text(getattr(e, "link", "") or "")
            published = clean_text(getattr(e, "published", "") or "")

            summary_raw = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            summary = soup_text(summary_raw)

            # Dice description sometimes contains company/location; we attempt extraction.
            company = extract_company_from_text(summary) or "Unknown"
            loc = extract_location_from_text(summary) or location or "Unknown"

            results.append(Job(
                source="dice",
                title=title or "Unknown",
                company=company,
                location=loc,
                url=link,
                published=published or None,
                summary=summary[:800] if summary else None,
                tags=derive_tags(title, summary),
            ))

            if len(results) >= limit:
                break

        page += 1

    return results

# ----------------------------
# Normalization helpers
# ----------------------------
TAG_KEYWORDS = {
    "aws": ["aws", "eks", "ecs", "cloudformation", "iam"],
    "azure": ["azure", "aks", "bicep"],
    "gcp": ["gcp", "gke"],
    "kubernetes": ["kubernetes", "k8s", "helm"],
    "terraform": ["terraform", "tf"],
    "jenkins": ["jenkins"],
    "gitlab": ["gitlab", "gitlab ci"],
    "ci/cd": ["ci/cd", "cicd", "pipeline"],
    "sre": ["sre", "site reliability"],
    "observability": ["splunk", "datadog", "dynatrace", "prometheus", "grafana"],
    "security": ["oauth", "oidc", "sso", "mfa", "zero trust", "vault"],
}

def derive_tags(title: str, summary: str) -> List[str]:
    text = f"{title} {summary}".lower()
    tags = []
    for tag, keys in TAG_KEYWORDS.items():
        if any(k in text for k in keys):
            tags.append(tag)
    return tags

def extract_company_from_text(text: str) -> Optional[str]:
    # Very lightweight heuristic; adjust based on real feed content.
    # Examples in some RSS entries: "Company: XYZ" or "at XYZ"
    m = re.search(r"Company:\s*([A-Za-z0-9&.,' -]{2,80})", text, re.IGNORECASE)
    if m:
        return clean_text(m.group(1))
    m = re.search(r"\bat\s+([A-Za-z0-9&.,' -]{2,80})\b", text, re.IGNORECASE)
    if m:
        return clean_text(m.group(1))
    return None

def extract_location_from_text(text: str) -> Optional[str]:
    m = re.search(r"Location:\s*([A-Za-z0-9, .-]{2,80})", text, re.IGNORECASE)
    if m:
        return clean_text(m.group(1))
    return None

def dedupe_jobs(jobs: List[Job]) -> List[Job]:
    seen = set()
    out = []
    for j in jobs:
        key = (j.source, j.url) if j.url else (j.source, j.title, j.company, j.location)
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out

# ----------------------------
# API endpoints
# ----------------------------
@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}

@app.get("/jobs")
def get_jobs(
    q: str = Query("devops", description="Search keyword, e.g. devops, platform engineer, sre"),
    location: str = Query("United States", description="Location string, e.g. Hartford, CT"),
    limit: int = Query(30, ge=1, le=100),
    radius_miles: int = Query(30, ge=0, le=200),
    sources: str = Query("indeed,dice", description="Comma list: indeed,dice"),
) -> Dict[str, Any]:
    """
    Aggregates jobs across sources and returns normalized JSON.
    """
    sources_list = [s.strip().lower() for s in sources.split(",") if s.strip()]
    cache_key = make_cache_key(q, location, str(limit), str(radius_miles), ",".join(sorted(sources_list)))
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    jobs: List[Job] = []
    per_source_limit = max(5, limit)  # fetch a bit extra to allow dedupe

    if "indeed" in sources_list:
        try:
            jobs.extend(parse_indeed(q=q, location=location, limit=per_source_limit, radius_miles=radius_miles))
        except Exception as e:
            jobs.append(Job(source="indeed", title="ERROR", company=str(e), location=location, url=""))

    if "dice" in sources_list:
        try:
            jobs.extend(parse_dice(q=q, location=location, limit=per_source_limit, radius_miles=radius_miles))
        except Exception as e:
            jobs.append(Job(source="dice", title="ERROR", company=str(e), location=location, url=""))

    jobs = dedupe_jobs(jobs)

    # Basic sort: published date may not be consistent; keep stable
    out_jobs = [asdict(j) for j in jobs[:limit]]

    payload = {
        "query": {"q": q, "location": location, "limit": limit, "radius_miles": radius_miles, "sources": sources_list},
        "count": len(out_jobs),
        "jobs": out_jobs,
    }
    cache_set(cache_key, payload)
    return payload
