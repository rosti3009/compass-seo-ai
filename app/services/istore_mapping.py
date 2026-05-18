from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.db.models import IStoreProduct, IStoreProductMapping, IStoreSEOApproval
from app.integrations.istore import IStoreClient

_PRODUCT_ID_KEYS = ("istore_product_id", "product_id", "id", "productid", "productId")
_PRODUCT_NAME_KEYS = ("product_name", "name", "title", "productName")
_PRODUCT_URL_KEYS = ("url", "product_url", "productUrl", "link", "href")
_PRODUCT_CANONICAL_KEYS = ("canonical", "canonical_url", "canonicalUrl")
_PRODUCT_SLUG_KEYS = ("slug", "keyword", "seo_keyword", "seoKeyword")
_BRAND_KEYS = ("brand", "manufacturer", "vendor")
_CATEGORY_KEYS = ("category", "category_name", "categoryName")
_META_TITLE_KEYS = ("meta_title", "metaTitle", "seo_title", "seoTitle")
_META_DESCRIPTION_KEYS = ("meta_description", "metaDescription", "seo_description", "seoDescription")
_SPACE_RE = re.compile(r"\s+")
_NON_SLUG_RE = re.compile(r"[^\w\u0590-\u05ff-]+", re.UNICODE)
PUBLISHABLE_CONFIDENCE_THRESHOLD = 90


@dataclass(frozen=True)
class MappingCandidate:
    product_id: str
    confidence: int
    source: str
    product: IStoreProduct | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        return (self.source,)


@dataclass(frozen=True)
class MappingResult:
    status: str
    candidates: tuple[MappingCandidate, ...]

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def missing(self) -> bool:
        return self.status == "missing"


def sync_istore_products(db: Session, client: IStoreClient | None = None) -> dict[str, object]:
    """Fetch products from ISTORE and upsert a local catalog plus lookup mappings."""
    client = client or IStoreClient.from_settings()
    products = [_normalize_product_payload(item) for item in _extract_products(client.list_products())]
    products = [product for product in products if product["istore_product_id"]]
    now = datetime.now(UTC)
    synced = 0

    for payload in products:
        product = (
            db.query(IStoreProduct)
            .filter(IStoreProduct.istore_product_id == payload["istore_product_id"])
            .one_or_none()
        )
        if product is None:
            product = IStoreProduct(istore_product_id=payload["istore_product_id"])
            db.add(product)
        for key, value in payload.items():
            setattr(product, key, value)
        product.updated_at = now
        synced += 1
        db.flush()
        _upsert_catalog_mapping(db, product, product.product_url or product.canonical_url or product.slug or "", now)

    db.commit()
    return {"success": True, "synced_products": synced, "auto_publish": False}


def list_synced_products(db: Session, *, q: str | None = None, limit: int = 100) -> list[IStoreProduct]:
    query = db.query(IStoreProduct).order_by(IStoreProduct.updated_at.desc(), IStoreProduct.id.desc())
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            IStoreProduct.istore_product_id.ilike(like)
            | IStoreProduct.product_name.ilike(like)
            | IStoreProduct.slug.ilike(like)
            | IStoreProduct.keyword.ilike(like)
            | IStoreProduct.product_url.ilike(like)
            | IStoreProduct.canonical_url.ilike(like)
        )
    return query.limit(limit).all()


def verify_pending_istore_mappings(db: Session, client: IStoreClient | None = None) -> dict[str, object]:
    """Verify crawler-derived ISTORE product mappings against the synchronized catalog."""
    if db.query(IStoreProduct).count() == 0:
        sync_istore_products(db, client=client)

    fixes = (
        db.query(IStoreSEOApproval)
        .filter(IStoreSEOApproval.status.in_(["PENDING_APPROVAL", "APPROVED"]))
        .order_by(IStoreSEOApproval.created_at.desc(), IStoreSEOApproval.id.desc())
        .all()
    )

    mapped: list[dict[str, object]] = []
    unmapped: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []

    for fix in fixes:
        result = verify_fix_mapping(db, fix)
        if result.verified:
            candidate = result.candidates[0]
            _apply_candidate(db, fix, candidate, manual=False)
            mapped.append(_candidate_payload(fix, candidate))
        elif result.ambiguous:
            _clear_mapping(fix, conflict=True)
            conflicts.append(
                {
                    "fix_id": fix.id,
                    "source_url": fix.source_url or fix.target_url,
                    "candidate_product_ids": [candidate.product_id for candidate in result.candidates],
                    "candidates": [_candidate_payload(fix, candidate) for candidate in result.candidates],
                }
            )
        else:
            _clear_mapping(fix, conflict=False)
            unmapped.append(
                {"fix_id": fix.id, "source_url": fix.source_url or fix.target_url, "reason": "no_verified_match"}
            )
        db.add(fix)

    db.commit()
    return {"mapped": mapped, "unmapped": unmapped, "conflicts": conflicts, "duplicates": [], "auto_publish": False}


def verify_fix_mapping(db: Session, fix: IStoreSEOApproval) -> MappingResult:
    candidates = map_fix_to_products(fix, db.query(IStoreProduct).all())
    if not candidates:
        return MappingResult("missing", ())
    best_confidence = candidates[0].confidence
    publishable_candidates = tuple(
        candidate for candidate in candidates if candidate.confidence >= PUBLISHABLE_CONFIDENCE_THRESHOLD
    )
    best = tuple(candidate for candidate in candidates if candidate.confidence == best_confidence)
    if len(publishable_candidates) > 1:
        return MappingResult("ambiguous", tuple(candidates[:5]))
    if best_confidence >= PUBLISHABLE_CONFIDENCE_THRESHOLD and len(best) == 1:
        return MappingResult("verified", best)
    if len(candidates) > 1 or best_confidence < PUBLISHABLE_CONFIDENCE_THRESHOLD:
        return MappingResult("ambiguous", tuple(candidates[:5]))
    return MappingResult("missing", ())


def map_fix_to_products(
    fix: IStoreSEOApproval, products: list[IStoreProduct | dict[str, Any]]
) -> list[MappingCandidate]:
    source_url = fix.source_url or fix.target_url or ""
    source_canonical = _canonicalize_url(source_url)
    source_path = _normalize_path(source_url)
    source_slug = _slug_from_url(source_url)
    matches: dict[str, MappingCandidate] = {}

    for raw_product in products:
        product = raw_product if isinstance(raw_product, IStoreProduct) else None
        product_payload = _model_payload(product) if product else _normalize_product_payload(raw_product)
        product_id = product_payload["istore_product_id"]
        if not product_id:
            continue

        scored: list[tuple[int, str]] = []
        product_urls = [product_payload.get("canonical_url") or "", product_payload.get("product_url") or ""]
        product_slugs = [product_payload.get("slug") or "", product_payload.get("keyword") or ""]

        for value in product_urls:
            if source_canonical and _canonicalize_url(value) == source_canonical:
                scored.append((100, "exact_url"))
            if source_path and _normalize_path(value) == source_path:
                scored.append((90, "path"))

        for value in product_slugs:
            normalized = _normalize_slug(value)
            if source_slug and value and value.strip("/").lower() == source_slug:
                scored.append((95, "exact_slug"))
            if source_slug and normalized == source_slug:
                scored.append((85, "normalized_slug"))

        if fix.istore_product_id and str(fix.istore_product_id) == product_id:
            scored.append((100, "known_istore_product_id"))

        fuzzy_values = [*product_urls, *product_slugs, product_payload.get("product_name") or ""]
        fuzzy_score = max(
            (_similarity(source_slug, _normalize_slug(value)) for value in fuzzy_values if value), default=0
        )
        if fuzzy_score >= 0.72:
            scored.append((60, "fuzzy_similarity"))

        if scored:
            confidence, source = max(scored, key=lambda item: item[0])
            matches[product_id] = MappingCandidate(
                product_id=product_id, confidence=confidence, source=source, product=product
            )

    return sorted(matches.values(), key=lambda candidate: (-candidate.confidence, candidate.product_id))


def assign_product_mapping(db: Session, fix: IStoreSEOApproval, istore_product_id: str) -> MappingCandidate:
    product = db.query(IStoreProduct).filter(IStoreProduct.istore_product_id == istore_product_id).one_or_none()
    if product is None:
        raise ValueError("ISTORE product is not synchronized")
    candidate = MappingCandidate(
        product_id=product.istore_product_id,
        confidence=100,
        source="manual_override",
        product=product,
    )
    _apply_candidate(db, fix, candidate, manual=True)
    db.commit()
    db.refresh(fix)
    return candidate


def publishable_mapping(fix: IStoreSEOApproval) -> bool:
    return bool(
        fix.target_type == "product"
        and fix.publish_mapping_verified
        and fix.istore_product_id
        and fix.target_id == fix.istore_product_id
        and not fix.mapping_conflict
        and ((fix.mapping_confidence or 100) if fix.publish_mapping_verified else 0) >= PUBLISHABLE_CONFIDENCE_THRESHOLD
    )


def _apply_candidate(db: Session, fix: IStoreSEOApproval, candidate: MappingCandidate, *, manual: bool) -> None:
    now = datetime.now(UTC)
    fix.istore_product_id = candidate.product_id
    fix.target_id = candidate.product_id
    fix.publish_mapping_verified = True
    fix.mapping_conflict = False
    fix.mapping_confidence = candidate.confidence
    fix.mapping_source = candidate.source
    fix.approval_metadata_json = _merged_metadata(
        fix.approval_metadata_json,
        {
            "mapping_source": candidate.source,
            "mapping_confidence": candidate.confidence,
            "mapping_status": "verified",
            "manual_override": manual,
            "last_verified_at": now.isoformat(),
        },
    )
    if candidate.product is not None:
        _upsert_catalog_mapping(
            db, candidate.product, fix.source_url or fix.target_url or "", now, candidate.confidence, candidate.source
        )
    db.add(fix)


def _clear_mapping(fix: IStoreSEOApproval, *, conflict: bool) -> None:
    fix.publish_mapping_verified = False
    fix.mapping_conflict = conflict
    fix.istore_product_id = None
    fix.mapping_confidence = 0
    fix.mapping_source = "ambiguous" if conflict else "missing"
    if fix.source_page_audit_id or fix.source_url:
        fix.target_id = ""


def _upsert_catalog_mapping(
    db: Session,
    product: IStoreProduct,
    target_url: str,
    verified_at: datetime,
    confidence: int = 100,
    source: str = "catalog_sync",
) -> IStoreProductMapping:
    target_url = target_url or product.product_url or product.canonical_url or product.slug or product.istore_product_id
    mapping = (
        db.query(IStoreProductMapping)
        .filter(
            IStoreProductMapping.istore_product_id == product.istore_product_id,
            IStoreProductMapping.target_url == target_url,
        )
        .one_or_none()
    )
    if mapping is None:
        mapping = IStoreProductMapping(istore_product_id=product.istore_product_id, target_url=target_url)
        db.add(mapping)
    mapping.canonical_url = product.canonical_url
    mapping.slug = product.slug
    mapping.normalized_slug = _normalize_slug(product.slug or product.keyword or target_url)
    mapping.last_verified_at = verified_at
    mapping.active = True
    mapping.mapping_confidence = confidence
    mapping.mapping_source = source
    return mapping


def _candidate_payload(fix: IStoreSEOApproval, candidate: MappingCandidate) -> dict[str, object]:
    return {
        "fix_id": fix.id,
        "source_url": fix.source_url or fix.target_url,
        "istore_product_id": candidate.product_id,
        "confidence": candidate.confidence,
        "mapping_confidence": candidate.confidence,
        "source": candidate.source,
        "mapping_source": candidate.source,
        "reasons": [candidate.source],
    }


def _normalize_product_payload(product: dict[str, Any]) -> dict[str, str | None]:
    description = product.get("product_description") if isinstance(product.get("product_description"), dict) else {}
    nested = next((value for value in description.values() if isinstance(value, dict)), {}) if description else {}
    return {
        "istore_product_id": _first_value(product, _PRODUCT_ID_KEYS),
        "product_name": _first_value(product, _PRODUCT_NAME_KEYS) or _first_value(nested, _PRODUCT_NAME_KEYS),
        "slug": _first_value(product, _PRODUCT_SLUG_KEYS),
        "canonical_url": _first_value(product, _PRODUCT_CANONICAL_KEYS),
        "product_url": _first_value(product, _PRODUCT_URL_KEYS),
        "brand": _first_value(product, _BRAND_KEYS),
        "category": _first_value(product, _CATEGORY_KEYS),
        "meta_title": _first_value(product, _META_TITLE_KEYS) or _first_value(nested, _META_TITLE_KEYS),
        "meta_description": _first_value(product, _META_DESCRIPTION_KEYS)
        or _first_value(nested, _META_DESCRIPTION_KEYS),
        "keyword": _first_value(product, ("keyword", "seo_keyword", "seoKeyword")),
    }


def _model_payload(product: IStoreProduct) -> dict[str, str | None]:
    return {
        "istore_product_id": product.istore_product_id,
        "product_name": product.product_name,
        "slug": product.slug,
        "canonical_url": product.canonical_url,
        "product_url": product.product_url,
        "brand": product.brand,
        "category": product.category,
        "meta_title": product.meta_title,
        "meta_description": product.meta_description,
        "keyword": product.keyword,
    }


def _first_value(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


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


def _canonicalize_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://placeholder.local/{value.lstrip('/')}")
    path = "/".join(part for part in parsed.path.split("/") if part)
    host = parsed.netloc.lower()
    if host == "placeholder.local":
        host = ""
    return f"{host}/{path}".strip("/").lower()


def _normalize_path(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://placeholder.local/{value.lstrip('/')}")
    return "/".join(part for part in parsed.path.split("/") if part).lower()


def _slug_from_url(value: str) -> str:
    path = urlparse(value).path if "://" in value else value
    parts = [part for part in path.split("/") if part]
    return _normalize_slug(parts[-1] if parts else value)


def _normalize_slug(value: str) -> str:
    value = (value or "").strip().split("?")[0].strip("/")
    value = value.rsplit("/", 1)[-1]
    value = _SPACE_RE.sub("-", value)
    value = _NON_SLUG_RE.sub("-", value)
    return value.strip("-").lower()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _merged_metadata(existing_json: str, updates: dict[str, object]) -> str:
    import json

    try:
        metadata = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    metadata.update(updates)
    return json.dumps(metadata, ensure_ascii=False)
