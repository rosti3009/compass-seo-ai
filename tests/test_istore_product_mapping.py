import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base, get_db
from app.db.models import IStoreProduct, IStoreProductMapping, IStoreSEOApproval
from app.main import app
from app.services.istore_mapping import (
    assign_product_mapping,
    map_fix_to_products,
    publishable_mapping,
    sync_istore_products,
    verify_fix_mapping,
    verify_pending_istore_mappings,
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class ProductCatalogClient:
    def list_products(self) -> dict[str, object]:
        return {
            "products": [
                {
                    "product_id": "sku-1",
                    "name": "Gas Grill 3000",
                    "slug": "gas-grill-3000",
                    "canonical_url": "https://shop.example.com/products/gas-grill-3000",
                    "url": "https://shop.example.com/products/gas-grill-3000?utm=ignored",
                    "brand": "BrandCo",
                    "category": "Grills",
                    "meta_title": "Buy Gas Grill",
                    "meta_description": "A gas grill for patios.",
                    "keyword": "gas grill 3000",
                },
                {
                    "product_id": "sku-2",
                    "name": "Charcoal Grill",
                    "slug": "charcoal-grill",
                    "canonical_url": "https://shop.example.com/products/charcoal-grill",
                },
            ]
        }


def _fix(db: Session, url: str, *, status: str = "APPROVED") -> IStoreSEOApproval:
    fix = IStoreSEOApproval(
        target_type="product",
        target_id="pending",
        target_url=url,
        source_url=url,
        field_path="meta_title",
        current_value="Old",
        proposed_value="New",
        seo_reason="Improve title",
        status=status,
        proposed_payload_json=json.dumps({"meta_title": "New"}),
        rollback_payload_json=json.dumps({"meta_title": "Old"}),
    )
    db.add(fix)
    db.commit()
    db.refresh(fix)
    return fix


def test_product_sync_persists_catalog_and_mapping(db_session: Session) -> None:
    result = sync_istore_products(db_session, client=ProductCatalogClient())

    assert result["synced_products"] == 2
    product = db_session.query(IStoreProduct).filter_by(istore_product_id="sku-1").one()
    assert product.product_name == "Gas Grill 3000"
    assert product.brand == "BrandCo"
    assert product.meta_description == "A gas grill for patios."
    mapping = db_session.query(IStoreProductMapping).filter_by(istore_product_id="sku-1").one()
    assert mapping.normalized_slug == "gas-grill-3000"
    assert mapping.active is True


def test_exact_url_mapping_scores_100_and_is_verified(db_session: Session) -> None:
    sync_istore_products(db_session, client=ProductCatalogClient())
    fix = _fix(db_session, "https://shop.example.com/products/gas-grill-3000")

    result = verify_fix_mapping(db_session, fix)

    assert result.status == "verified"
    assert result.candidates[0].confidence == 100
    assert result.candidates[0].source == "exact_url"


def test_slug_mapping_scores_95_for_exact_slug(db_session: Session) -> None:
    sync_istore_products(db_session, client=ProductCatalogClient())
    fix = _fix(db_session, "https://other.example.com/items/charcoal-grill")

    candidate = map_fix_to_products(fix, db_session.query(IStoreProduct).all())[0]

    assert candidate.product_id == "sku-2"
    assert candidate.confidence == 95
    assert candidate.source == "exact_slug"


def test_confidence_scoring_blocks_normalized_slug_publishability(db_session: Session) -> None:
    product = IStoreProduct(istore_product_id="sku-3", slug="Gas Grill 4000")
    db_session.add(product)
    fix = _fix(db_session, "https://shop.example.com/products/gas-grill-4000")
    db_session.commit()

    result = verify_pending_istore_mappings(db_session)
    db_session.refresh(fix)

    assert result["conflicts"][0]["candidates"][0]["mapping_confidence"] == 85
    assert fix.publish_mapping_verified is False
    assert publishable_mapping(fix) is False


def test_ambiguous_mapping_blocks_verification(db_session: Session) -> None:
    db_session.add_all(
        [
            IStoreProduct(istore_product_id="sku-a", canonical_url="https://shop.example.com/products/same"),
            IStoreProduct(istore_product_id="sku-b", canonical_url="https://shop.example.com/products/same"),
        ]
    )
    fix = _fix(db_session, "https://shop.example.com/products/same")
    db_session.commit()

    result = verify_pending_istore_mappings(db_session)
    db_session.refresh(fix)

    assert result["conflicts"]
    assert fix.mapping_conflict is True
    assert fix.publish_mapping_verified is False
    assert publishable_mapping(fix) is False


def test_publishable_only_when_verified_with_high_confidence(db_session: Session) -> None:
    sync_istore_products(db_session, client=ProductCatalogClient())
    fix = _fix(db_session, "https://shop.example.com/products/gas-grill-3000")

    verify_pending_istore_mappings(db_session)
    db_session.refresh(fix)

    assert fix.istore_product_id == "sku-1"
    assert fix.publish_mapping_verified is True
    assert fix.mapping_confidence == 100
    assert publishable_mapping(fix) is True


def test_manual_mapping_assignment_persists_and_allows_publishable(db_session: Session) -> None:
    db_session.add(IStoreProduct(istore_product_id="sku-manual", product_name="Manual Product", slug="manual-product"))
    fix = _fix(db_session, "https://shop.example.com/products/unknown")
    db_session.commit()

    candidate = assign_product_mapping(db_session, fix, "sku-manual")
    db_session.refresh(fix)

    assert candidate.source == "manual_override"
    assert fix.istore_product_id == "sku-manual"
    assert fix.mapping_confidence == 100
    assert db_session.query(IStoreProductMapping).filter_by(istore_product_id="sku-manual").count() == 1
    assert publishable_mapping(fix) is True


def test_sync_and_assign_endpoints(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.istore_mapping.IStoreClient.from_settings", lambda: ProductCatalogClient())
    fix = _fix(db_session, "https://shop.example.com/products/missing")

    sync_response = client.post("/istore/sync-products")
    products_response = client.get("/istore/products?q=gas")
    assign_response = client.post(f"/seo/fixes/{fix.id}/assign-product", json={"istore_product_id": "sku-1"})

    assert sync_response.status_code == 200
    assert products_response.status_code == 200
    products_payload = products_response.json()
    assert products_payload["count"] == 1
    assert products_payload["publishable_threshold"] == 90
    assert products_payload["products"][0]["mapping_count"] == 1
    assert products_payload["products"][0]["mappings"][0]["normalized_slug"] == "gas-grill-3000"
    assert assign_response.status_code == 200
    assert assign_response.json()["fix"]["publish_mapping_verified"] is True
