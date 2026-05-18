from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import IStoreSEOApproval
from app.integrations.istore import IStoreClient

_PRODUCT_ID_KEYS = ("product_id", "id", "productid", "productId")
_PRODUCT_URL_KEYS = ("url", "product_url", "productUrl", "link", "href", "canonical", "canonical_url")
_PRODUCT_SLUG_KEYS = ("slug", "keyword", "seo_keyword", "seoKeyword")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class MappingCandidate:
    product_id: str
    reasons: tuple[str, ...]


def verify_pending_istore_mappings(db: Session, client: IStoreClient | None = None) -> dict[str, object]:
    """Verify crawler-derived ISTORE product mappings without choosing ambiguous matches."""
    client = client or IStoreClient.from_settings()
    products = _extract_products(client.list_products())
    fixes = (
        db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status.in_(["PENDING_APPROVAL", "APPROVED"]))
        .order_by(IStoreSEOApproval.created_at.desc(), IStoreSEOApproval.id.desc())
        .all()
    )

    mapped: list[dict[str, object]] = []
    unmapped: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    duplicate_buckets: dict[str, list[int]] = {}

    for fix in fixes:
        if fix.target_type != "product":
            _clear_mapping(fix, conflict=False)
            unmapped.append({"fix_id": fix.id, "source_url": fix.source_url or fix.target_url, "reason": "not_product"})
            continue

        candidates = map_fix_to_products(fix, products)
        if len(candidates) == 1:
            candidate = candidates[0]
            fix.istore_product_id = candidate.product_id
            fix.target_id = candidate.product_id
            fix.publish_mapping_verified = True
            fix.mapping_conflict = False
            mapped.append(
                {
                    "fix_id": fix.id,
                    "source_url": fix.source_url or fix.target_url,
                    "istore_product_id": candidate.product_id,
                    "reasons": list(candidate.reasons),
                }
            )
            duplicate_buckets.setdefault(candidate.product_id, []).append(fix.id)
        elif len(candidates) > 1:
            _clear_mapping(fix, conflict=True)
            conflicts.append(
                {
                    "fix_id": fix.id,
                    "source_url": fix.source_url or fix.target_url,
                    "candidate_product_ids": [candidate.product_id for candidate in candidates],
                }
            )
        else:
            _clear_mapping(fix, conflict=False)
            unmapped.append(
                {"fix_id": fix.id, "source_url": fix.source_url or fix.target_url, "reason": "no_verified_match"}
            )

        db.add(fix)

    duplicates = [
        {"istore_product_id": product_id, "fix_ids": fix_ids}
        for product_id, fix_ids in duplicate_buckets.items()
        if len(fix_ids) > 1
    ]
    db.commit()
    return {"mapped": mapped, "unmapped": unmapped, "conflicts": conflicts, "duplicates": duplicates}


def map_fix_to_products(fix: IStoreSEOApproval, products: list[dict[str, Any]]) -> list[MappingCandidate]:
    source_url = fix.source_url or fix.target_url or ""
    source_canonical = _canonicalize_url(source_url)
    source_slug = _slug_from_url(source_url)
    matches: dict[str, set[str]] = {}

    for product in products:
        product_id = _product_id(product)
        if not product_id:
            continue
        reasons: set[str] = set()

        for value in _product_urls(product):
            if source_canonical and _canonicalize_url(value) == source_canonical:
                reasons.add("canonical_url")

        for value in _product_slugs(product):
            normalized = _normalize_slug(value)
            if source_slug and normalized == source_slug:
                reasons.add("slug")
            if source_canonical and _canonicalize_url(value) == source_canonical:
                reasons.add("product_url")

        if fix.istore_product_id and str(fix.istore_product_id) == product_id:
            reasons.add("known_istore_product_id")

        if reasons:
            matches.setdefault(product_id, set()).update(reasons)

    return [
        MappingCandidate(product_id=pid, reasons=tuple(sorted(reasons)))
        for pid, reasons in sorted(matches.items())
    ]


def publishable_mapping(fix: IStoreSEOApproval) -> bool:
    return bool(
        fix.target_type == "product"
        and fix.publish_mapping_verified
        and fix.istore_product_id
        and fix.target_id == fix.istore_product_id
        and not fix.mapping_conflict
    )


def _clear_mapping(fix: IStoreSEOApproval, *, conflict: bool) -> None:
    fix.publish_mapping_verified = False
    fix.mapping_conflict = conflict
    fix.istore_product_id = None
    if fix.source_page_audit_id or fix.source_url:
        fix.target_id = ""


def _extract_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("products", "data", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in payload for key in _PRODUCT_ID_KEYS):
            return [payload]
    return []


def _product_id(product: dict[str, Any]) -> str:
    for key in _PRODUCT_ID_KEYS:
        value = product.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _product_urls(product: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _PRODUCT_URL_KEYS:
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _product_slugs(product: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _PRODUCT_SLUG_KEYS:
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _canonicalize_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://placeholder.local/{value.lstrip('/')}")
    path = "/".join(part for part in parsed.path.split("/") if part)
    host = parsed.netloc.lower()
    if host == "placeholder.local":
        host = ""
    return f"{host}/{path}".strip("/").lower()


def _slug_from_url(value: str) -> str:
    path = urlparse(value).path if "://" in value else value
    parts = [part for part in path.split("/") if part]
    return _normalize_slug(parts[-1] if parts else value)


def _normalize_slug(value: str) -> str:
    value = value.strip().split("?")[0].strip("/")
    value = value.rsplit("/", 1)[-1]
    value = _SPACE_RE.sub("-", value)
    return value.lower()
