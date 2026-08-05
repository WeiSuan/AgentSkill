import json
from pathlib import Path

import pytest

import tier_MapTierIndustries as mapper
from industry_repository import IndustryCatalogItem


TIER_NAMES = {"塑膠製品", "稻作栽培業"}
CATALOG = [
    {"head4_industry_code": "0111", "head4_industry_name": "稻作栽培業"},
    {"head4_industry_code": "2209", "head4_industry_name": "其他塑膠製品製造業"},
]


def test_extract_tier_industry_names_flattens_deduplicates_and_trims() -> None:
    payload = {"analysis": {"industry_outlook": [
        {"industries": [" 塑膠製品 ", "稻作栽培業", ""]},
        {"industries": ["塑膠製品"]},
    ]}}

    assert mapper.extract_tier_industry_names(payload) == TIER_NAMES


def test_catalog_entries_preserves_every_code_name_pair() -> None:
    items = [
        IndustryCatalogItem("0111", ("稻作栽培業",)),
        IndustryCatalogItem("2209", ("其他塑膠製品製造業",)),
    ]

    assert mapper.catalog_entries(items) == CATALOG


def test_exact_mappings_keeps_unmatched_master_item() -> None:
    results = mapper.exact_mappings(CATALOG, {"稻作栽培業"})

    assert len(results) == 2
    assert results[0]["match_status"] == "matched"
    assert results[0]["matches"][0]["tier_industry_name"] == "稻作栽培業"
    assert results[1]["match_status"] == "unmatched"
    assert results[1]["matches"] == []
    assert results[1]["unmatched_reason"]


def test_normalize_model_results_rejects_unknown_tier_name() -> None:
    response = json.dumps({"results": [{
        "head4_industry_code": "2209",
        "head4_industry_name": "其他塑膠製品製造業",
        "matches": [{"tier_industry_name": "不存在", "match_type": "涵蓋", "confidence": 0.9, "reason": "測試"}],
        "unmatched_reason": "",
    }]})

    with pytest.raises(ValueError, match="不存在的台經院產業名稱"):
        mapper.normalize_model_results(response, [CATALOG[1]], TIER_NAMES)


def test_normalize_model_results_requires_every_master_item() -> None:
    response = json.dumps({"results": [{
        "head4_industry_code": "2209",
        "head4_industry_name": "其他塑膠製品製造業",
        "matches": [],
        "unmatched_reason": "沒有足夠關聯",
    }]})

    with pytest.raises(ValueError, match="遺漏主表項目"):
        mapper.normalize_model_results(response, CATALOG, TIER_NAMES)


def test_build_output_contains_all_master_items() -> None:
    entries = mapper.exact_mappings(CATALOG, {"稻作栽培業"})
    output = mapper.build_output(TIER_NAMES, ["source.json"], "hash", "model", entries)

    assert output["summary"]["master_industry_count"] == 2
    assert output["summary"]["master_industry_name_count"] == 2
    assert output["summary"]["unmatched_master_count"] == 1
    assert len(output["mappings"]) == 2
    assert output["mappings"][1]["head4_industry_code"] == "2209"


def test_load_cache_requires_matching_catalog_and_prompt_version(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({
        "catalog_sha256": "same", "prompt_version": mapper.PROMPT_VERSION,
        "mappings": CATALOG, "source_jsons": ["source.json"],
    }), encoding="utf-8")

    mappings, sources = mapper.load_cache(path, catalog_hash="same", force=False)

    assert mappings == CATALOG
    assert sources == ["source.json"]
    assert mapper.load_cache(path, catalog_hash="changed", force=False) == ([], [])


def test_atomic_write_json_replaces_invalid_prior_file(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text("not-json", encoding="utf-8")

    mapper.atomic_write_json(path, {"valid": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"valid": True}
