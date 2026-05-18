import json
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
GENERIC_AI_PATTERNS = (
    "פתרון איכותי",
    "ביצועים מעולים",
    "מוצר המיועד לאנשים",
    "מקסימום נוחות",
    "מוצרים איכותיים",
    "מתאים לשימוש מקצועי וביתי",
)

CONTEXT_KEYWORD_GROUPS = {
    "grills": ("גריל", "grill", "barbecue", "bbq"),
    "smokers": ("מעשנה", "smoker", "smoking"),
    "butcher_tools": ("קצב", "butcher", "מטחנת", "מסור", "קצבים"),
    "knives": ("סכין", "סכינים", "knife", "knives"),
    "charcoal": ("פחם", "charcoal"),
    "pellets": ("פלט", "פלטים", "pellet", "pellets"),
    "wood_chunks": ("צ׳אנק", "צ'אנק", "chunk", "chunks"),
    "wood_chips": ("שבבי עץ", "wood chips", "chips"),
    "pizza_ovens": ("טאבון", "פיצה", "pizza oven", "oven"),
    "meat_products": ("בשר", "סטייק", "אנטריקוט", "meat", "steak"),
}

COMMERCIAL_TERMS = ("מחיר", "לקנייה", "קנה", "הוסף לסל", "משלוח", "₪", "מבצע", "shop", "buy", "sale")

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
    seo_risk_level: str = "low"
    remediation_suggestions: list[str] | None = None
    context_keywords: list[str] | None = None
    primary_intent: str = "general"
    commercial_intent_score: float = 0.0


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
                    seo_risk_level=result.seo_risk_level,
                    remediation_suggestions=json.dumps(result.remediation_suggestions or [], ensure_ascii=False),
                    context_keywords=json.dumps(result.context_keywords or [], ensure_ascii=False),
                    primary_intent=result.primary_intent,
                    commercial_intent_score=result.commercial_intent_score,
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
                        result = self._replace_with_issues(result, missing_fields)

                results.append(result)

        return self._apply_similarity_detection(results)

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
        missing.extend(self._generic_ai_issues(title, meta_description, soup))

        if canonical and self._has_invalid_canonical(url, canonical):
            missing.append("invalid_canonical")

        if page_type == "system" and not self._has_noindex(soup):
            missing.append("system_page_indexable")

        context_keywords, primary_intent, commercial_intent_score = self._detect_context(
            url=url, title=title, h1=h1, soup=soup
        )

        remediation_suggestions = self._remediation_suggestions(missing, page_type)
        seo_risk_level = self._risk_level(missing, status_code)

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
            seo_risk_level=seo_risk_level,
            remediation_suggestions=remediation_suggestions,
            context_keywords=context_keywords,
            primary_intent=primary_intent,
            commercial_intent_score=commercial_intent_score,
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
        schema_types = self._schema_types(soup)
        body_text = soup.get_text(" ").casefold()

        has_product_schema = any("product" in schema_type for schema_type in schema_types)
        has_brand_schema = any(
            token in schema_type for schema_type in schema_types for token in ("brand", "manufacturer")
        )
        has_article_schema = any("article" in schema_type or "posting" in schema_type for schema_type in schema_types)
        has_price = bool(
            soup.find(attrs={"itemprop": "price"})
            or soup.find("meta", attrs={"property": "product:price:amount"})
            or soup.find(string=lambda text: bool(text and "₪" in text))
        )
        has_add_to_cart = any(token in body_text for token in ("add to cart", "הוסף לסל", "הוספה לסל"))
        product_cards = soup.find_all(
            lambda tag: bool(
                tag.name in {"article", "div", "li"}
                and (
                    "product" in " ".join(tag.get("class", [])).casefold()
                    or tag.get("data-product-id")
                    or tag.find(attrs={"itemtype": re.compile("Product", re.I)})
                )
            )
        )
        brand_mentions = [
            self._text_or_none(str(node.get("content") or node.get_text(" ")))
            for node in soup.find_all(attrs={"itemprop": re.compile("brand|manufacturer", re.I)})
        ]
        repeated_brand_mentions = len({mention.casefold() for mention in brand_mentions if mention}) == 1 and len(
            [mention for mention in brand_mentions if mention]
        ) >= 2

        is_brand = bool(
            "/brand/" in path
            or path.rstrip("/").endswith("/brand")
            or "/brands/" in path
            or path.rstrip("/").endswith("/brands")
            or any(segment in {"manufacturer", "manufacturers", "vendor", "vendors"} for segment in path_segments)
            or has_brand_schema and (len(product_cards) >= 2 or not has_product_schema)
            or repeated_brand_mentions
        )
        is_category = bool(
            "/category" in path
            or "/categories" in path
            or "/collections" in path
            or "/catalog" in path
            or path.rstrip("/").endswith("/shop")
        )
        is_product = bool(
            og_type_content == "product"
            or has_product_schema
            or has_price
            or has_add_to_cart
            or "/product" in path
            or "/products" in path
            or "/p/" in path
        )

        if path in {"", "/"}:
            return "home", False, False

        if any(token in path for token in SYSTEM_PATH_TOKENS) or any(
            segment in {"newsletter", "login", "account", "cart", "checkout", "privacy", "accessibility", "search"}
            for segment in path_segments
        ):
            return "system", False, False

        if is_brand:
            return "brand", False, False

        if og_type_content == "article" or has_article_schema:
            return "article", False, False

        if "/blog" in path:
            return ("article" if len(path_segments) > 1 else "blog"), False, False

        if is_product:
            return "product", True, False

        if is_category:
            return "category", False, True

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
            seo_risk_level=self._risk_level(missing_fields, status_code),
            remediation_suggestions=self._remediation_suggestions(missing_fields, "unknown"),
            context_keywords=[],
            primary_intent="unknown",
            commercial_intent_score=0.0,
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
            "generic_ai_meta": 12,
            "generic_ai_title": 8,
            "repetitive_ai_content": 8,
            "duplicate_title_similarity": 8,
            "duplicate_meta_similarity": 10,
            "invalid_canonical": 18,
            "system_page_indexable": 18,
        }
        score -= sum(issue_penalties.get(issue, 0) for issue in issues)

        if page_type == "product" and title and any(word in title for word in ["מחיר", "גריל", "שיפוד", "בשר"]):
            score += 3

        return round(max(0.0, min(score, 100.0)), 2)


    def _apply_similarity_detection(self, results: list[PageSEOResult]) -> list[PageSEOResult]:
        processed = results[:]
        title_openings: dict[str, list[int]] = defaultdict(list)
        meta_openings: dict[str, list[int]] = defaultdict(list)

        for index, result in enumerate(processed):
            if result.title:
                title_openings[self._opening_pattern(result.title)].append(index)
            if result.meta_description:
                meta_openings[self._opening_pattern(result.meta_description)].append(index)

        for index, current in enumerate(processed):
            issues = list(current.missing_fields)
            for previous in processed[:index]:
                if current.title and previous.title and self._similarity(current.title, previous.title) >= 0.82:
                    issues.append("duplicate_title_similarity")
                    break
            for previous in processed[:index]:
                if (
                    current.meta_description
                    and previous.meta_description
                    and self._similarity(current.meta_description, previous.meta_description) >= 0.82
                ):
                    issues.append("duplicate_meta_similarity")
                    break

            if current.title and len(title_openings[self._opening_pattern(current.title)]) >= 3:
                issues.append("duplicate_title_similarity")
            if current.meta_description and len(meta_openings[self._opening_pattern(current.meta_description)]) >= 3:
                issues.append("duplicate_meta_similarity")

            issues = list(dict.fromkeys(issues))
            if issues != current.missing_fields:
                processed[index] = self._replace_with_issues(current, issues)

        return processed

    def _replace_with_issues(self, result: PageSEOResult, issues: list[str]) -> PageSEOResult:
        return replace(
            result,
            missing_fields=issues,
            seo_score=self._score(
                status_code=result.status_code,
                title=result.title,
                meta_description=result.meta_description,
                h1=result.h1,
                canonical=result.canonical,
                word_count=result.word_count,
                page_type=result.page_type,
                missing_fields=issues,
            ),
            seo_risk_level=self._risk_level(issues, result.status_code),
            remediation_suggestions=self._remediation_suggestions(issues, result.page_type),
        )

    def _schema_types(self, soup: BeautifulSoup) -> set[str]:
        schema_types = {
            str(node.get("itemtype", "")).lower()
            for node in soup.find_all(attrs={"itemtype": True})
            if node.get("itemtype")
        }
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw_json = script.string or script.get_text()
            try:
                payload = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError):
                continue
            schema_types.update(self._extract_jsonld_types(payload))
        return schema_types

    def _extract_jsonld_types(self, payload: object) -> set[str]:
        found: set[str] = set()
        if isinstance(payload, dict):
            value = payload.get("@type")
            if isinstance(value, str):
                found.add(value.casefold())
            elif isinstance(value, list):
                found.update(str(item).casefold() for item in value)
            for child in payload.values():
                found.update(self._extract_jsonld_types(child))
        elif isinstance(payload, list):
            for child in payload:
                found.update(self._extract_jsonld_types(child))
        return found

    def _generic_ai_issues(self, title: str | None, meta_description: str | None, soup: BeautifulSoup) -> list[str]:
        issues: list[str] = []
        title_text = title or ""
        meta_text = meta_description or ""
        body_text = soup.get_text(" ")

        if any(pattern in title_text for pattern in GENERIC_AI_PATTERNS):
            issues.append("generic_ai_title")
        if any(pattern in meta_text for pattern in GENERIC_AI_PATTERNS):
            issues.append("generic_ai_meta")
        body_hits = sum(body_text.count(pattern) for pattern in GENERIC_AI_PATTERNS)
        if body_hits >= 2 or self._has_repetitive_phrases(body_text):
            issues.append("repetitive_ai_content")
        return issues

    def _has_repetitive_phrases(self, text: str) -> bool:
        normalized = self._normalize_text(text)
        words = normalized.split()
        if len(words) < 12:
            return False
        phrases = Counter(" ".join(words[index : index + 4]) for index in range(len(words) - 3))
        return any(count >= 3 for phrase, count in phrases.items() if len(phrase) > 12)

    def _has_invalid_canonical(self, url: str, canonical: str) -> bool:
        parsed = urlparse(urljoin(url, canonical))
        return parsed.scheme not in {"http", "https"} or not parsed.netloc

    def _has_noindex(self, soup: BeautifulSoup) -> bool:
        robots = soup.find("meta", attrs={"name": re.compile("robots", re.I)})
        return bool(robots and "noindex" in str(robots.get("content", "")).casefold())

    def _detect_context(
        self, url: str, title: str | None, h1: str | None, soup: BeautifulSoup
    ) -> tuple[list[str], str, float]:
        breadcrumb_selector = '[class*="breadcrumb"], nav[aria-label*="breadcrumb" i]'
        breadcrumb_text = " ".join(node.get_text(" ") for node in soup.select(breadcrumb_selector))
        schema_text = " ".join(sorted(self._schema_types(soup)))
        haystack = " ".join([url, title or "", h1 or "", breadcrumb_text, schema_text, soup.get_text(" ")]).casefold()
        keywords = [
            keyword
            for keyword, aliases in CONTEXT_KEYWORD_GROUPS.items()
            if any(alias.casefold() in haystack for alias in aliases)
        ]
        primary_intent = keywords[0] if keywords else "general"
        commercial_hits = sum(1 for term in COMMERCIAL_TERMS if term.casefold() in haystack)
        if any(token in urlparse(url).path.casefold() for token in ("product", "shop", "cart")):
            commercial_hits += 1
        commercial_intent_score = min(1.0, round(commercial_hits / 5, 2))
        return keywords, primary_intent, commercial_intent_score

    def _remediation_suggestions(self, issues: list[str], page_type: str) -> list[str]:
        issue_set = set(issues)
        suggestions: list[str] = []
        meta_rewrite_issues = {
            "generic_ai_meta",
            "duplicate_meta_description",
            "duplicate_meta_similarity",
            "meta_description",
        }
        if issue_set & meta_rewrite_issues:
            suggestions.append("rewrite_meta_description")
        if issue_set & {"generic_ai_title", "duplicate_title_similarity", "title_too_long"}:
            suggestions.append("shorten_title")
        if "invalid_slug" in issue_set or "non_descriptive_slug" in issue_set:
            suggestions.append("improve_slug")
        if "thin_content" in issue_set:
            suggestions.append("expand_content")
        if page_type == "brand" or issue_set & {"generic_ai_meta", "duplicate_meta_similarity"}:
            suggestions.append("add_unique_brand_context")
        return list(dict.fromkeys(suggestions))

    def _risk_level(self, issues: list[str], status_code: int) -> str:
        issue_set = set(issues)
        if (
            not 200 <= status_code < 300
            or "title" in issue_set
            or "invalid_canonical" in issue_set
            or "system_page_indexable" in issue_set
            or ({"duplicate_meta_description", "duplicate_meta_similarity"} <= issue_set)
        ):
            return "critical"
        if issue_set & {"generic_ai_meta", "generic_ai_title", "invalid_slug", "thin_content", "repetitive_ai_content"}:
            return "high"
        if issue_set:
            return "medium"
        return "low"

    def _similarity(self, left: str, right: str) -> float:
        left_normalized = self._normalize_text(left)
        right_normalized = self._normalize_text(right)
        if not left_normalized or not right_normalized:
            return 0.0
        left_tokens = left_normalized.split()
        right_tokens = right_normalized.split()
        token_overlap = len(set(left_tokens) & set(right_tokens)) / max(len(set(left_tokens) | set(right_tokens)), 1)
        return max(SequenceMatcher(None, left_normalized, right_normalized).ratio(), token_overlap)

    def _opening_pattern(self, text: str) -> str:
        words = self._normalize_text(text).split()[:5]
        return " ".join(words)

    def _normalize_text(self, text: str) -> str:
        normalized = re.sub(r"[\d\W_]+", " ", text.casefold(), flags=re.UNICODE)
        stopwords = {"איכותי", "מקצועי", "מעולה", "חדש", "premium", "quality", "professional"}
        return " ".join(word for word in normalized.split() if word not in stopwords)

    def _normalize_url(self, url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        clean = parsed._replace(fragment="", query="")
        return urldefrag(clean.geturl().rstrip("/") or clean.geturl())[0]

    def _text_or_none(self, value: str | None) -> str | None:
        if value is None:
            return None

        cleaned = " ".join(value.split())
        return cleaned or None
