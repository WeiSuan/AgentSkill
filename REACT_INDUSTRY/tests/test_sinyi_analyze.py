import json
from pathlib import Path

import pytest

from sinyi_AnalyzeRealEstateReviews import normalize_report_date, parse_response_json, report_id_for


class Response:
    def __init__(self, text: str) -> None:
        self.text = text


def test_parse_response_json_accepts_expected_schema() -> None:
    response = Response(json.dumps({"source": "信義不動產評論", "report_date": "2026-01-01", "industry": "不動產", "current_market_status": "持平", "future_market_status": "好", "key_summary": "摘要"}))
    assert parse_response_json(response)["industry"] == "不動產"


def test_parse_response_json_rejects_long_summary() -> None:
    response = Response(json.dumps({"source": "信義不動產評論", "report_date": "2026-01-01", "industry": "不動產", "current_market_status": "持平", "future_market_status": "好", "key_summary": "x" * 501}))
    with pytest.raises(ValueError):
        parse_response_json(response)


def test_report_id_is_stable(tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-test")
    metadata = tmp_path / ".metadata.json"
    metadata.write_text(json.dumps({"report": {"sha256": "abc"}}), encoding="utf-8")
    assert report_id_for(pdf, metadata) == report_id_for(pdf, metadata)


def test_normalize_report_date_converts_quarter_to_quarter_end() -> None:
    assert normalize_report_date("2025-Q3") == "2025-09-30"