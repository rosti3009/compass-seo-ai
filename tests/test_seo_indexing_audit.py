from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook

from scripts import seo_indexing_audit as audit


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_imported_exports_generate_real_actions_without_empty_templates(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "gsc.csv").write_text(
        "URL,Reason,Status\n"
        "https://compassgrill.co.il/product-name-copy-0/,Duplicate without user-selected canonical,200\n"
        "https://compassgrill.co.il/product-name/,Submitted and indexed,200\n"
        "https://compassgrill.co.il/cat/?sort=price,Crawled - currently not indexed,200\n"
        "https://compassgrill.co.il/missing,Not Found (404),404\n",
        encoding="utf-8",
    )

    rows: dict[str, audit.Row] = {}
    audit.import_files([input_dir / "gsc.csv"], rows)
    audit.analyze(rows, output_dir)

    redirects = _read_csv(output_dir / "redirects.csv")
    assert redirects == [
        {
            "old_url": "https://compassgrill.co.il/product-name-copy-0/",
            "new_url": "https://compassgrill.co.il/product-name/",
        }
    ]
    noindex = _read_csv(output_dir / "noindex-rules.csv")
    assert noindex == [{"pattern": "https://compassgrill.co.il/cat/?sort=price"}]
    broken = _read_csv(output_dir / "404-urls.csv")
    assert broken == [
        {
            "url": "https://compassgrill.co.il/missing",
            "recommended_action": "Manual review - target not HTTP-validated",
        }
    ]
    assert not (output_dir / "canonical-rules.csv").exists()
    assert "Analyzed real imported/live URLs: 4" in (output_dir / "implementation-plan.md").read_text(encoding="utf-8")


def test_xlsx_gsc_drilldown_is_inferred_converted_and_counted(tmp_path: Path) -> None:
    xlsx = tmp_path / "https___compassgrill.co.il_-Coverage-Drilldown-2026-06-14.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Crawled - Currently Not Indexed"
    sheet.append(["Coverage report", "Crawled - Currently Not Indexed"])
    sheet.append(["URL", "Status"])
    sheet.append(["https://compassgrill.co.il/products/grill-1/", "200"])
    sheet.append(["https://compassgrill.co.il/blog/smoking-guide", "200"])
    workbook.save(xlsx)

    rows: dict[str, audit.Row] = {}
    audit.import_files([xlsx], rows)
    audit.analyze(rows, tmp_path / "out")

    assert (tmp_path / "https___compassgrill.co.il_-Coverage-Drilldown-2026-06-14.converted.csv").exists()
    nonindexed = _read_csv(tmp_path / "out" / "non-indexed-content.csv")
    assert {row["type"] for row in nonindexed} == {"product", "article"}
    plan = (tmp_path / "out" / "implementation-plan.md").read_text(encoding="utf-8")
    assert "- suffix_1: 1" in plan
    assert "- nonindexed_products: 1" in plan
    assert "- nonindexed_articles: 1" in plan


def test_problem_gsc_rows_do_not_create_self_redirects_and_keep_blank_params(tmp_path: Path) -> None:
    input_file = tmp_path / "gsc.csv"
    output_dir = tmp_path / "output"
    input_file.write_text(
        "URL,Reason\n"
        "https://compassgrill.co.il/brisket-1/?from_admin,Duplicate without user-selected canonical\n"
        "https://compassgrill.co.il/brisket-1/,Duplicate without user-selected canonical\n",
        encoding="utf-8",
    )

    rows: dict[str, audit.Row] = {}
    audit.import_files([input_file], rows)
    audit.analyze(rows, output_dir)

    assert _read_csv(output_dir / "redirects.csv") == []
    noindex = _read_csv(output_dir / "noindex-rules.csv")
    assert noindex == [{"pattern": "https://compassgrill.co.il/brisket-1/?from_admin"}]
    duplicate_rows = _read_csv(output_dir / "duplicate-urls.csv")
    assert duplicate_rows[0]["duplicate_of"] == "manual_review"
    fixes = _read_csv(output_dir / "fix-recommendations.csv")
    assert any(row["suggested_action"] == "Manual review - target not HTTP-validated" for row in fixes)
