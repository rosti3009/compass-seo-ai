from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CrawlRun, PageAudit


@dataclass(frozen=True)
class PageSEOResult:
    """In-memory representation of a page SEO audit."""

    url: str
    status_code: int
    title: str | None
    meta_description: str | None
    h1: str | None
    canonical: str | None
    word_count: int
    internal_links: int
    missing_fields: list[str]
    seo_score: float


class SEOCrawler:
    """Bounded same-host SEO crawler for the target restaurant website."""

    def __init__(self, target_domain: str, max_pages: int = 25) -> None:
        self.start_url = self._normalize_url(target_domain)
        self.allowed_host = urlparse(self.start_url).netloc
        self.max_pages = max_pages

    def run(self, db: Session) -> tuple[CrawlRun, list[PageAudit]]:
        crawl_run = CrawlRun(target_domain=self.start_url, status="running")
        db.add(crawl_run)
        db.commit()
        db.refresh(crawl_run)

        audits: list[PageAudit] = []
        try:
            results = self._crawl()
            for result in results:
                audit = PageAudit(
                    crawl_run_id=crawl_run.id,
                    url=result.url,
                    status_code=result.status_code,
                    title=result.title,
                    meta_description=result.meta_description,
                    h1=result.h1,
                    canonical=result.canonical,
                    word_count=result.word_count,
                    internal_links=result.internal_links,
                    missing_fields=",".join(result.missing_fields),
                    seo_score=result.seo_score,
                )
                db.add(audit)
                audits.append(audit)

            crawl_run.pages_crawled = len(audits)
            crawl_run.average_score = (
                round(sum(audit.seo_score for audit in audits) / len(audits), 2) if audits else 0.0
            )
            crawl_run.status = "completed"
            crawl_run.completed_at = datetime.now(UTC)
            db.commit()
            for audit in audits:
                db.refresh(audit)
            db.refresh(crawl_run)
        except Exception as exc:  # noqa: BLE001 - persist crawler failures for observability.
            crawl_run.status = "failed"
            crawl_run.error_message = str(exc)
            crawl_run.completed_at = datetime.now(UTC)
            db.commit()
            db.refresh(crawl_run)
        return crawl_run, audits

    def _crawl(self) -> list[PageSEOResult]:
        queue: deque[str] = deque([self.start_url])
        visited: set[str] = set()
        results: list[PageSEOResult] = []
        headers = {"User-Agent": "CompassSEOAI/0.1 (+https://compassgrill.co.il)"}
        with httpx.Client(follow_redirects=True, timeout=settings.crawler_timeout_seconds, headers=headers) as client:
            while queue and len(visited) < self.max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                response = client.get(url)
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    results.append(self._empty_result(url, response.status_code, ["html_content"]))
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                discovered_links = self._discover_internal_links(soup, url)
                for link in discovered_links:
                    if link not in visited and len(visited) + len(queue) < self.max_pages:
                        queue.append(link)
                results.append(self._audit_page(url, response.status_code, soup, len(discovered_links)))
        return results

    def _audit_page(self, url: str, status_code: int, soup: BeautifulSoup, internal_links: int) -> PageSEOResult:
        title = self._text_or_none(soup.title.string if soup.title else None)
        description_tag = soup.find("meta", attrs={"name": "description"})
        meta_description = self._text_or_none(description_tag.get("content") if description_tag else None)
        h1_tag = soup.find("h1")
        h1 = self._text_or_none(h1_tag.get_text(" ") if h1_tag else None)
        canonical_tag = soup.find("link", attrs={"rel": "canonical"})
        canonical = self._text_or_none(canonical_tag.get("href") if canonical_tag else None)
        word_count = len(soup.get_text(" ").split())
        missing = [
            field
            for field, value in {
                "title": title,
                "meta_description": meta_description,
                "h1": h1,
                "canonical": canonical,
            }.items()
            if not value
        ]
        seo_score = self._score(status_code, title, meta_description, h1, canonical, word_count)
        return PageSEOResult(
            url, status_code, title, meta_description, h1, canonical, word_count, internal_links, missing, seo_score
        )

    def _discover_internal_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        links: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            normalized = self._normalize_url(urljoin(base_url, anchor["href"]))
            parsed = urlparse(normalized)
            if parsed.scheme in {"http", "https"} and parsed.netloc == self.allowed_host:
                links.add(normalized)
        return sorted(links)

    def _empty_result(self, url: str, status_code: int, missing_fields: list[str]) -> PageSEOResult:
        return PageSEOResult(url, status_code, None, None, None, None, 0, 0, missing_fields, 0.0)

    def _score(
        self,
        status_code: int,
        title: str | None,
        meta_description: str | None,
        h1: str | None,
        canonical: str | None,
        word_count: int,
    ) -> float:
        score = 0
        score += 20 if 200 <= status_code < 300 else 0
        score += 20 if title and 10 <= len(title) <= 70 else 5 if title else 0
        score += 20 if meta_description and 50 <= len(meta_description) <= 170 else 5 if meta_description else 0
        score += 20 if h1 else 0
        score += 10 if canonical else 0
        score += 10 if word_count >= 250 else 5 if word_count >= 100 else 0
        return float(score)

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        clean = parsed._replace(fragment="", query="")
        return urldefrag(clean.geturl().rstrip("/") or clean.geturl())[0]

    def _text_or_none(self, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None
