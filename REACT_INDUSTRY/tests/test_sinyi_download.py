from pathlib import Path

from sinyi_DownloadRealEstateReviews import parse_review_links, safe_filename


def test_parse_review_links_extracts_review_rows() -> None:
    html = """
    <table><tr><td>2026/05/11</td><td>2026年第一季 - 信義不動產評論</td><td>3849KB</td>
    <td><a href='/download?file_path=files_storage%2Freal_estate%2F2026%2F05%2Fa.pdf&file_uuid=abc'>PDF</a></td></tr></table>
    """.encode("utf-8")
    rows = parse_review_links(html)
    assert len(rows) == 1
    assert rows[0].period == "2026年第一季 - 信義不動產評論"
    assert rows[0].uploaded_at == "2026-05-11"
    assert rows[0].file_uuid == "abc"
    assert rows[0].url.startswith("https://www.sinyinews.com.tw/download?")


def test_safe_filename_removes_windows_path_characters() -> None:
    assert safe_filename("2026年第一季: 信義/評論") == "2026年第一季_ 信義_評論"


def test_parse_review_links_keeps_latest_duplicate_period() -> None:
    html = """
    <dl><dt><p>2025/01/02</p></dt><dd><p>2024年第四季 - 信義不動產評論</p></dd>
    <dd><a href='/download?file_path=old.pdf&file_uuid=old'>PDF</a></dd></dl>
    <dl><dt><p>2025/02/02</p></dt><dd><p>2024年第四季 - 信義不動產評論</p></dd>
    <dd><a href='/download?file_path=new.pdf&file_uuid=new'>PDF</a></dd></dl>
    """.encode("utf-8")
    rows = parse_review_links(html)
    assert len(rows) == 1
    assert rows[0].uploaded_at == "2025-02-02"
    assert rows[0].file_uuid == "new"