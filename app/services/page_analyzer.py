import cloudscraper
from bs4 import BeautifulSoup


class PageAnalyzer:
    def analyze(self, url: str) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        scraper = cloudscraper.create_scraper()

        response = scraper.get(
            url,
            headers=headers,
            timeout=20,
            allow_redirects=True,
        )

        html = response.text
        soup = BeautifulSoup(html, "html.parser")

        title = self._extract_title(soup)
        meta_description = self._extract_meta_description(soup)
        h1 = self._extract_h1(soup)
        h2_list = self._extract_h2_list(soup)
        word_count = self._word_count(soup)
        internal_links = self._internal_links(soup)
        image_count = len(soup.find_all("img"))

        issues = []

        if not title:
            issues.append("Missing title tag")
        if title and len(title) < 30:
            issues.append("Title too short")
        if title and len(title) > 65:
            issues.append("Title too long")
        if not meta_description:
            issues.append("Missing meta description")
        if meta_description and len(meta_description) < 70:
            issues.append("Meta description too short")
        if meta_description and len(meta_description) > 160:
            issues.append("Meta description too long")
        if not h1:
            issues.append("Missing H1")
        if word_count < 500:
            issues.append("Low content length")
        if len(internal_links) < 3:
            issues.append("Low internal linking")

        seo_score = max(0, 100 - (len(issues) * 8))

        return {
            "url": url,
            "status_code": response.status_code,
            "title": title,
            "meta_description": meta_description,
            "h1": h1,
            "h2_list": h2_list,
            "word_count": word_count,
            "internal_links_count": len(internal_links),
            "image_count": image_count,
            "issues": issues,
            "seo_score": seo_score,
        }

    def _extract_title(self, soup: BeautifulSoup) -> str | None:
        if soup.title and soup.title.string:
            return soup.title.string.strip()
        return None

    def _extract_meta_description(self, soup: BeautifulSoup) -> str | None:
        tag = soup.find("meta", attrs={"name": "description"})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    def _extract_h1(self, soup: BeautifulSoup) -> str | None:
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        return None

    def _extract_h2_list(self, soup: BeautifulSoup) -> list[str]:
        return [h2.get_text(strip=True) for h2 in soup.find_all("h2")]

    def _word_count(self, soup: BeautifulSoup) -> int:
        text = soup.get_text(separator=" ", strip=True)
        return len(text.split())

    def _internal_links(self, soup: BeautifulSoup) -> list[str]:
        links = []

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]

            if href.startswith("/") or "compassgrill.co.il" in href:
                links.append(href)

        return list(set(links))