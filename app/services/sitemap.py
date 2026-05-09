from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import requests

SitemapType = Literal["products", "categories", "brands", "information", "other"]


@dataclass
class SitemapUrl:
    url: str
    type: SitemapType


def classify_sitemap_url(url: str) -> SitemapType:
    path = urlparse(url).path.lower()

    if "product" in path or "/products" in path:
        return "products"
    if "categor" in path or "/category" in path:
        return "categories"
    if "brand" in path:
        return "brands"
    if "information" in path or "blog" in path or "about" in path:
        return "information"

    return "other"


async def fetch_xml(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/xml,text/xml,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=True,
    )

    response.raise_for_status()

    return response.text


def parse_sitemap_index(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)  # noqa: S314
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    urls: list[str] = []
    for loc in root.findall(".//sm:sitemap/sm:loc", namespace):
        if loc.text:
            urls.append(loc.text.strip())

    return urls


def parse_urlset(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)  # noqa: S314
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    urls: list[str] = []
    for loc in root.findall(".//sm:url/sm:loc", namespace):
        if loc.text:
            urls.append(loc.text.strip())

    return urls


async def discover_sitemap_urls(root_sitemap_url: str) -> list[SitemapUrl]:
    index_xml = await fetch_xml(root_sitemap_url)
    sitemap_files = parse_sitemap_index(index_xml)

    discovered: list[SitemapUrl] = []

    for sitemap_url in sitemap_files:
        sitemap_type = classify_sitemap_url(sitemap_url)
        xml_text = await fetch_xml(sitemap_url)
        page_urls = parse_urlset(xml_text)

        for page_url in page_urls:
            discovered.append(SitemapUrl(url=page_url, type=sitemap_type))

    return discovered