"""Incrementally download Sinyi real-estate review PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
PAGE_URL = "https://www.sinyinews.com.tw/real_estate_review"
OUTPUT_DIR = BASE_DIR / "信義不動產評論" / "Original"
METADATA_PATH = OUTPUT_DIR / ".metadata.json"
DOCUMENT_PATH = BASE_DIR / "create_agent" / "agent-sinyi-analyze.md"
CUTOFF_DATE = datetime(2025, 1, 1).date()


@dataclass(frozen=True)
class ReviewLink:
    period: str
    uploaded_at: str
    file_size: str
    url: str
    file_uuid: str


class ReviewPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[ReviewLink] = []
        self._row_cells: list[str] = []
        self._row_link: str | None = None
        self._row_link_text = ""
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"tr", "dl"}:
            self._in_row = True
            self._row_cells = []
            self._row_link = None
            self._row_link_text = ""
        elif self._in_row and tag in {"td", "dd", "dt"}:
            self._in_cell = True
            self._cell_text = []
        elif self._in_row and tag == "a":
            href = dict(attrs).get("href")
            if href and "download" in href.lower():
                self._row_link = urljoin(PAGE_URL, href)

    def handle_data(self, data: str) -> None:
        if self._in_row and self._in_cell:
            self._cell_text.append(data)
        if self._in_row and self._row_link:
            self._row_link_text += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "dd", "dt"} and self._in_cell:
            self._row_cells.append(" ".join("".join(self._cell_text).split()))
            self._in_cell = False
        elif tag in {"tr", "dl"} and self._in_row:
            self._finish_row()
            self._in_row = False

    def _finish_row(self) -> None:
        date_match = next((re.search(r"\d{4}/\d{2}/\d{2}", cell) for cell in self._row_cells), None)
        period = next((cell for cell in self._row_cells if "信義不動產評論" in cell), "")
        size = next((cell for cell in self._row_cells if re.search(r"\d+\s*KB", cell, re.I)), "")
        if not date_match or not period or not self._row_link:
            return
        url = self._row_link
        query = parse_qs(urlparse(url).query)
        file_path = query.get("file_path", [""])[0]
        if ".pdf" not in file_path.lower() and ".pdf" not in url.lower():
            return
        uploaded_at = datetime.strptime(date_match.group(), "%Y/%m/%d").date().isoformat()
        file_uuid = query.get("file_uuid", [""])[0]
        self.rows.append(ReviewLink(period, uploaded_at, size, url, file_uuid))


def parse_review_links(page_html: bytes) -> list[ReviewLink]:
    parser = ReviewPageParser()
    parser.feed(page_html.decode("utf-8", errors="replace"))
    unique: dict[str, ReviewLink] = {}
    for item in parser.rows:
        previous = unique.get(item.period)
        if previous is None or item.uploaded_at > previous.uploaded_at:
            unique[item.period] = item
    return sorted(unique.values(), key=lambda item: (item.uploaded_at, item.period))


def fetch_bytes(url: str, timeout: int = 30, *, insecure: bool = False) -> bytes:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    context = ssl._create_unverified_context() if insecure else None
    with urlopen(request, timeout=timeout, context=context) as response:
        return response.read()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(period: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", period).strip(" .")


def load_metadata(path: Path = METADATA_PATH) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def update_documentation(status: str, details: list[str], error: str | None = None) -> None:
    if not DOCUMENT_PATH.is_file():
        return
    content = DOCUMENT_PATH.read_text(encoding="utf-8")
    body = "## 最近執行摘要\n\n" + f"- 執行結果：{status}\n" + "\n".join(f"- {detail}" for detail in details)
    if error:
        body += f"\n- 最近錯誤：{error}"
    updated, count = re.subn(
        r"## 最近執行摘要\n.*?(?=<!-- RECENT_RUN_SUMMARY_END -->)",
        body + "\n",
        content,
        count=1,
        flags=re.DOTALL,
    )
    if count:
        DOCUMENT_PATH.write_text(updated, encoding="utf-8")


def download_reviews(
    *,
    destination: Path = OUTPUT_DIR,
    page_url: str = PAGE_URL,
    period: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    insecure: bool = False,
    timeout: int = 30,
) -> dict[str, int]:
    destination.mkdir(parents=True, exist_ok=True)
    metadata_path = destination / ".metadata.json"
    links = [item for item in parse_review_links(fetch_bytes(page_url, timeout, insecure=insecure)) if datetime.fromisoformat(item.uploaded_at).date() > CUTOFF_DATE]
    if period:
        links = [item for item in links if item.period == period]
        if not links:
            raise ValueError(f"找不到符合日期的期數：{period}")
    metadata = load_metadata(metadata_path)
    counts = {"found": len(links), "downloaded": 0, "skipped": 0, "failed": 0}
    for item in links:
        output = destination / f"{safe_filename(item.period)}.pdf"
        old = metadata.get(item.period, {})
        if output.is_file() and not force and output.read_bytes().startswith(b"%PDF-") and old.get("source_url") == item.url:
            counts["skipped"] += 1
            print(f"SKIP {output.name}")
            continue
        if dry_run:
            print(f"DRY-RUN {item.period} -> {output.name}")
            continue
        try:
            data = fetch_bytes(item.url, timeout, insecure=insecure)
            if not data.startswith(b"%PDF-"):
                raise ValueError("下載內容不是有效 PDF")
            temporary = output.with_suffix(".pdf.part")
            temporary.write_bytes(data)
            temporary.replace(output)
            metadata[item.period] = {
                **asdict(item),
                "filename": output.name,
                "sha256": sha256_bytes(data),
                "size_bytes": len(data),
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(metadata_path, metadata)
            counts["downloaded"] += 1
            print(f"OK {output.name} ({len(data)} bytes)")
        except Exception as exc:
            counts["failed"] += 1
            print(f"FAILED {item.period}: {exc}")
    update_documentation(
        "成功" if not counts["failed"] else "部分失敗",
        [f"下載狀態：發現 {counts['found']}、下載 {counts['downloaded']}、跳過 {counts['skipped']}、失敗 {counts['failed']} 筆。", "分析狀態：尚未執行。", "資料庫同步：尚未執行。", "待辦：執行信義 PDF Gemini 分析。"],
    )
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="處理所有符合日期的報告")
    parser.add_argument("--period", help="指定完整期數名稱")
    parser.add_argument("--dry-run", action="store_true", help="只列出待處理報告，不下載")
    parser.add_argument("--force", action="store_true", help="忽略既有檔案並重新下載")
    parser.add_argument("--insecure", action="store_true", help="停用 HTTPS 憑證驗證，僅供憑證鏈異常環境使用")
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.all and not args.period:
        raise ValueError("請指定 --all 或 --period")
    try:
        download_reviews(period=args.period, force=args.force, dry_run=args.dry_run, insecure=args.insecure, timeout=args.timeout)
    except Exception as error:
        update_documentation("失敗", ["下載狀態：未完成。", "分析狀態：尚未執行。", "資料庫同步：尚未執行。"], str(error))
        raise


if __name__ == "__main__":
    main()