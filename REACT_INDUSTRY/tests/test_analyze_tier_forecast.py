import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tier_AnalyzeTIERForecast as analyzer


def make_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\nmock pdf")
    return path


def test_select_pdf_defaults_to_latest(tmp_path: Path) -> None:
    source_dir = tmp_path / "Original"
    source_dir.mkdir()
    make_pdf(source_dir / "台經院_景氣動向_202601.pdf")
    latest = make_pdf(source_dir / "台經院_景氣動向_202607.pdf")

    assert analyzer.select_pdf(None, source_dir) == latest


def test_select_pdf_rejects_invalid_file(tmp_path: Path) -> None:
    invalid = tmp_path / "bad.pdf"
    invalid.write_bytes(b"not a pdf")

    with pytest.raises(ValueError, match="不是有效 PDF"):
        analyzer.select_pdf(str(invalid))


def test_select_pdfs_all_returns_sorted_valid_pdfs(tmp_path: Path) -> None:
    source_dir = tmp_path / "Original"
    source_dir.mkdir()
    first = make_pdf(source_dir / "台經院_景氣動向_202508.pdf")
    last = make_pdf(source_dir / "台經院_景氣動向_202607.pdf")

    assert analyzer.select_pdfs(None, True, source_dir) == [first, last]


def test_analyze_pdf_writes_json_and_logs_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_path = make_pdf(tmp_path / "台經院_景氣動向_202607.pdf")
    result_dir = tmp_path / "Result"
    usage_path = tmp_path / "logs" / "usage.jsonl"
    response = SimpleNamespace(
        text=json.dumps(
            {
                "industry_outlook": [
                    {
                        "ct_status": "好",
                        "ft_in6mon": "好",
                        "industries": ["電子零組件業", "資料儲存及處理設備"],
                    }
                ]
            }
        ),
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=30,
        ),
    )
    client = SimpleNamespace(
        files=SimpleNamespace(upload=lambda **kwargs: SimpleNamespace(uri="file-uri", mime_type="application/pdf")),
        models=SimpleNamespace(generate_content=lambda **kwargs: response),
    )
    monkeypatch.setenv("GEMINI_INPUT_PRICE_PER_MILLION", "1")
    monkeypatch.setenv("GEMINI_OUTPUT_PRICE_PER_MILLION", "2")

    output_path = analyzer.analyze_pdf(
        pdf_path,
        client=client,
        model_name="models/gemini-3.5-flash-lite",
        result_dir=result_dir,
        usage_log_path=usage_path,
    )

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    assert output_path.name == "台經院_景氣動向_202607.json"
    assert saved["analysis"]["industry_outlook"][0]["industries"] == [
        "電子零組件業",
        "資料儲存及處理設備",
    ]
    assert usage["step"] == "tier_pdf_analysis"
    assert usage["estimated_cost"] == 0.00005