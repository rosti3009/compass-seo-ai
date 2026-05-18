from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CrawlRun, PageAudit, PageScoreSnapshot
from app.services.browser_fetcher import fetch_rendered_html

VALID_PAGE_TYPES = {"product", "category", "brand", "blog", "article", "home", "system", "unknown"}
SYSTEM_PATH_TOKENS = (
    "/about",
    "/contact",
    "/accessibility",
    "/login",
    "/account",
    "/cart",
    "/checkout",
    "/search",
    "/sitemap",
    "/privacy",
    "/terms",
    "/policy",
    "/404",
)
GENERIC_SLUGS = {
    "p",
    "page",
    "pages",
    "product",
    "products",
    "category",
    "categories",
    "collection",
    "collections",
    "brand",
    "brands",
    "blog",
    "article",
    "articles",
    "shop",
    "store",
    "item",
    "items",
}


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
                previous_snapshot = (
                    db.query(PageScoreSnapshot)
                    .filter(PageScoreSnapshot.url == result.url)
                    .order_by(PageScoreSnapshot.created_at.desc(), PageScoreSnapshot.id.desc())
                    .first()
                )
                previous_score = previous_snapshot.seo_score if previous_snapshot else None
                score_delta = round(result.seo_score - previous_score, 2) if previous_score is not None else 0.0
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
                    seo_score_delta=score_delta,
                    page_type=result.page_type,
                )
                db.add(audit)
                db.flush()
                db.add(
                    PageScoreSnapshot(
                        page_audit_id=audit.id,
                        crawl_run_id=crawl_run.id,
                        url=result.url,
                        seo_score=result.seo_score,
                        previous_seo_score=previous_score,
                        seo_score_delta=score_delta,
                    )
                )
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
        meta_description_counts: Counter[str] = Counter()

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

                result = self._audit_page(
                    url=url,
                    status_code=response.status_code,
                    soup=soup,
                    internal_links=len(discovered_links),
                )

                if result.meta_description:
                    normalized_description = result.meta_description.casefold()
                    meta_description_counts[normalized_description] += 1
                    if meta_description_counts[normalized_description] > 1:
                        missing_fields = [*result.missing_fields, "duplicate_meta_description"]
                        result = replace(
                            result,
                            missing_fields=missing_fields,
                            seo_score=self._score(
                                status_code=result.status_code,
                                title=result.title,
                                meta_description=result.meta_description,
                                h1=result.h1,
                                canonical=result.canonical,
                                word_count=result.word_count,
                                page_type=result.page_type,
                                missing_fields=missing_fields,
                            ),
                        )

                results.append(result)

        return results

    def _audit_page(
        self,
        url: str,
        status_code: int,
        soup: BeautifulSoup,
        internal_links: int,
    ) -> PageSEOResult:
        title = self._text_or_none(soup.title.string if soup.title else None)

        description_tags = soup.find_all("meta", attrs={"name": "description"})
        description_tag = description_tags[0] if description_tags else None
        meta_description = self._text_or_none(description_tag.get("content") if description_tag else None)

        h1_tags = soup.find_all("h1")
        h1 = self._text_or_none(h1_tags[0].get_text(" ") if h1_tags else None)

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

        if title and len(title) < 30:
            missing.append("title_too_short")

        if title and len(title) > 60:
            missing.append("title_too_long")

        if meta_description and len(meta_description) < 120:
            missing.append("meta_description_too_short")

        if meta_description and len(meta_description) > 160:
            missing.append("meta_description_too_long")

        if len(description_tags) > 1:
            missing.append("duplicate_meta_description_tags")

        if len(h1_tags) > 1:
            missing.append("multiple_h1")

        minimum_words = self._minimum_word_count(page_type)
        if word_count < minimum_words:
            missing.append("thin_content")

        missing.extend(self._slug_validation_issues(url, page_type))

        seo_score = self._score(
            status_code=status_code,
            title=title,
            meta_description=meta_description,
            h1=h1,
            canonical=canonical,
            word_count=word_count,
            page_type=page_type,
            missing_fields=missing,
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
        parsed = urlparse(url)
        path = parsed.path.lower()
        path_segments = [segment for segment in path.split("/") if segment]

        og_type = soup.find("meta", attrs={"property": "og:type"})
        og_type_content = str(og_type.get("content", "")).lower() if og_type else ""
        schema_types = {
            str(node.get("itemtype", "")).lower()
            for node in soup.find_all(attrs={"itemtype": True})
            if node.get("itemtype")
        }
        body_text = soup.get_text(" ").casefold()

        has_product_schema = any("product" in schema_type for schema_type in schema_types)
        has_article_schema = any("article" in schema_type or "posting" in schema_type for schema_type in schema_types)
        has_price = bool(
            soup.find(attrs={"itemprop": "price"})
            or soup.find("meta", attrs={"property": "product:price:amount"})
            or soup.find(string=lambda text: bool(text and "₪" in text))
        )
        has_add_to_cart = any(token in body_text for token in ("add to cart", "הוסף לסל", "הוספה לסל"))

        is_product = bool(
            og_type_content == "product"
            or has_product_schema
            or has_price
            or has_add_to_cart
            or "/product" in path
            or "/products" in path
            or "/p/" in path
        )
        is_category = bool(
            "/category" in path
            or "/categories" in path
            or "/collections" in path
            or "/catalog" in path
            or path.rstrip("/").endswith("/shop")
        )

        if path in {"", "/"}:
            return "home", False, False

        if any(token in path for token in SYSTEM_PATH_TOKENS):
            return "system", False, False

        if is_product:
            return "product", True, False

        if is_category:
            return "category", False, True

        if "/brand" in path or "/brands" in path:
            return "brand", False, False

        if og_type_content == "article" or has_article_schema:
            return "article", False, False

        if "/blog" in path:
            return ("article" if len(path_segments) > 1 else "blog"), False, False

        if any(segment in {"news", "guides", "guide", "articles"} for segment in path_segments):
            return "article", False, False

        return "unknown", False, False

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

    def _minimum_word_count(self, page_type: str) -> int:
        return {
            "product": 200,
            "category": 150,
            "brand": 200,
            "blog": 300,
            "article": 600,
            "home": 250,
            "system": 50,
            "unknown": 250,
        }.get(page_type, 250)

    def _slug_validation_issues(self, url: str, page_type: str) -> list[str]:
        if page_type in {"home", "system"}:
            return []

        path_segments = [unquote(segment) for segment in urlparse(url).path.strip("/").split("/") if segment]
        if not path_segments:
            return []

        issues: list[str] = []
        descriptive_segments = 0

        for segment in path_segments:
            normalized_segment = segment.strip().casefold()
            if not normalized_segment:
                continue

            if normalized_segment not in GENERIC_SLUGS and any(char.isalpha() for char in normalized_segment):
                descriptive_segments += 1

            if (
                " " in segment
                or "_" in segment
                or len(segment) > 80
                or normalized_segment.startswith("-")
                or normalized_segment.endswith("-")
                or "--" in normalized_segment
            ):
                issues.append("invalid_slug")
                break

        if descriptive_segments == 0:
            issues.append("non_descriptive_slug")

        return issues

    def _score(
        self,
        status_code: int,
        title: str | None,
        meta_description: str | None,
        h1: str | None,
        canonical: str | None,
        word_count: int,
        page_type: str,
        missing_fields: list[str] | None = None,
    ) -> float:
        issues = set(missing_fields or [])
        score = 100.0

        if not 200 <= status_code < 300:
            score -= 45

        if not title:
            score -= 18
        elif not 30 <= len(title) <= 60:
            score -= 8

        if not meta_description:
            score -= 18
        elif not 120 <= len(meta_description) <= 160:
            score -= 8

        if not h1:
            score -= 14

        if not canonical:
            score -= 10

        minimum_words = self._minimum_word_count(page_type)
        if word_count < minimum_words:
            score -= 14 if word_count < minimum_words / 2 else 8

        issue_penalties = {
            "duplicate_meta_description": 10,
            "duplicate_meta_description_tags": 8,
            "multiple_h1": 6,
            "invalid_slug": 8,
            "non_descriptive_slug": 5,
            "title_too_short": 4,
            "title_too_long": 4,
            "meta_description_too_short": 4,
            "meta_description_too_long": 4,
        }
        score -= sum(issue_penalties.get(issue, 0) for issue in issues)

        if page_type == "product" and title and any(word in title for word in ["מחיר", "גריל", "שיפוד", "בשר"]):
            score += 3

        return round(max(0.0, min(score, 100.0)), 2)

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        clean = parsed._replace(fragment="", query="")
        return urldefrag(clean.geturl().rstrip("/") or clean.geturl())[0]

    def _text_or_none(self, value: str | None) -> str | None:
        if value is None:
            return None

        cleaned = " ".join(value.split())
        return cleaned or None
