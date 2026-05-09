from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CrawlRun, PageAudit
from app.services.browser_fetcher import fetch_rendered_html


@dataclass(frozen=True)
class PageSEOResult:
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
    page_type: str
    is_product: bool
    is_category: bool


class SEOCrawler:
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
                missing_fields = list(result.missing_fields)

                if result.is_product:
                    missing_fields.append("page_type:product")
                elif result.is_category:
                    missing_fields.append("page_type:category")
                else:
                    missing_fields.append(f"page_type:{result.page_type}")

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
                    missing_fields=",".join(missing_fields),
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

        except Exception as exc:  # noqa: BLE001
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

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        with httpx.Client(
            follow_redirects=True,
            timeout=settings.crawler_timeout_seconds,
            headers=headers,
        ) as client:
            while queue and len(visited) < self.max_pages:
                url = queue.popleft()

                if url in visited:
                    continue

                visited.add(url)

                try:
                    html = fetch_rendered_html(url)

                    class FakeResponse:
                        status_code = 200
                        headers = {"content-type": "text/html"}

                    response = FakeResponse()

                except Exception:  # noqa: BLE001
                    try:
                        response = client.get(url)
                        html = response.text

                    except httpx.HTTPError:
                        results.append(
                            self._empty_result(
                                url=url,
                                status_code=0,
                                missing_fields=["request_failed"],
                            )
                        )
                        continue

                content_type = response.headers.get("content-type", "")

                if "text/html" not in content_type:
                    results.append(
                        self._empty_result(
                            url=url,
                            status_code=response.status_code,
                            missing_fields=["html_content"],
                        )
                    )
                    continue

                soup = BeautifulSoup(html, "html.parser")
                discovered_links = self._discover_internal_links(soup, url)

                for link in discovered_links:
                    if link not in visited and len(visited) + len(queue) < self.max_pages:
                        queue.append(link)

                results.append(
                    self._audit_page(
                        url=url,
                        status_code=response.status_code,
                        soup=soup,
                        internal_links=len(discovered_links),
                    )
                )

        return results

    def _audit_page(
        self,
        url: str,
        status_code: int,
        soup: BeautifulSoup,
        internal_links: int,
    ) -> PageSEOResult:
        title = self._text_or_none(soup.title.string if soup.title else None)

        description_tag = soup.find("meta", attrs={"name": "description"})
        meta_description = self._text_or_none(description_tag.get("content") if description_tag else None)

        h1_tag = soup.find("h1")
        h1 = self._text_or_none(h1_tag.get_text(" ") if h1_tag else None)

        canonical_tag = soup.find("link", attrs={"rel": "canonical"})
        canonical = self._text_or_none(canonical_tag.get("href") if canonical_tag else None)

        word_count = len(soup.get_text(" ").split())
        page_type, is_product, is_category = self._detect_page_type(url, soup)

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

        if title and len(title) > 70:
            missing.append("title_too_long")

        if meta_description and len(meta_description) > 170:
            missing.append("meta_description_too_long")

        if word_count < 250:
            missing.append("thin_content")

        seo_score = self._score(
            status_code=status_code,
            title=title,
            meta_description=meta_description,
            h1=h1,
            canonical=canonical,
            word_count=word_count,
            page_type=page_type,
        )

        return PageSEOResult(
            url=url,
            status_code=status_code,
            title=title,
            meta_description=meta_description,
            h1=h1,
            canonical=canonical,
            word_count=word_count,
            internal_links=internal_links,
            missing_fields=missing,
            seo_score=seo_score,
            page_type=page_type,
            is_product=is_product,
            is_category=is_category,
        )

    def _discover_internal_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        links: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]

            if href.startswith(("tel:", "mailto:", "javascript:", "#")):
                continue

            normalized = self._normalize_url(urljoin(base_url, href))
            parsed = urlparse(normalized)

            if parsed.scheme in {"http", "https"} and parsed.netloc == self.allowed_host:
                links.add(normalized)

        return sorted(links)

    def _detect_page_type(self, url: str, soup: BeautifulSoup) -> tuple[str, bool, bool]:
        path = urlparse(url).path.lower()

        og_type = soup.find("meta", attrs={"property": "og:type"})
        og_type_content = ""
        if og_type:
            og_type_content = str(og_type.get("content", "")).lower()

        has_price = bool(
            soup.find(attrs={"itemprop": "price"})
            or soup.find("meta", attrs={"property": "product:price:amount"})
            or soup.find(string=lambda text: bool(text and "₪" in text))
        )

        is_product = bool(og_type_content == "product" or has_price or "/product" in path or "/products" in path)

        is_category = bool("/category" in path or "/categories" in path or "/brand/" in path or "/collections" in path)

        if is_product:
            return "product", True, False

        if is_category:
            return "category", False, True

        if "/blog" in path:
            return "blog", False, False

        if any(token in path for token in ["/about", "/contact", "/accessibility", "/login", "/account"]):
            return "information", False, False

        return "page", False, False

    def _empty_result(
        self,
        url: str,
        status_code: int,
        missing_fields: list[str],
    ) -> PageSEOResult:
        return PageSEOResult(
            url=url,
            status_code=status_code,
            title=None,
            meta_description=None,
            h1=None,
            canonical=None,
            word_count=0,
            internal_links=0,
            missing_fields=missing_fields,
            seo_score=0.0,
            page_type="unknown",
            is_product=False,
            is_category=False,
        )

    def _score(
        self,
        status_code: int,
        title: str | None,
        meta_description: str | None,
        h1: str | None,
        canonical: str | None,
        word_count: int,
        page_type: str,
    ) -> float:
        score = 0

        score += 20 if 200 <= status_code < 300 else 0
        score += 20 if title and 10 <= len(title) <= 70 else 5 if title else 0
        score += 20 if meta_description and 50 <= len(meta_description) <= 170 else 5 if meta_description else 0
        score += 20 if h1 else 0
        score += 10 if canonical else 0
        score += 10 if word_count >= 250 else 5 if word_count >= 100 else 0

        if page_type == "product" and title and any(word in title for word in ["מחיר", "גריל", "שיפוד", "בשר"]):
            score += 5

        return float(min(score, 100))

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        clean = parsed._replace(fragment="", query="")
        return urldefrag(clean.geturl().rstrip("/") or clean.geturl())[0]

    def _text_or_none(self, value: str | None) -> str | None:
        if value is None:
            return None

        cleaned = " ".join(value.split())
        return cleaned or None