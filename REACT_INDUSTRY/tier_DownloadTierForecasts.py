"""Download monthly TIER economic outlook survey PDF files."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


PAGE_URL = "https://www.tier.org.tw/forecast/forecast.aspx"
FORECAST_URL = "https://www.tier.org.tw/forecast/"
START_MONTH = (114, 8)
END_MONTH = (115, 7)
FILE_PREFIX = "台經院景氣動向_"


class ForecastLinkParser(HTMLParser):
    """Collect forecast PDF links from the forecast page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.add(urljoin(PAGE_URL, href))


def month_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = start
    while (year, month) <= end:
        months.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def western_month(roc_year: int, month: int) -> str:
    return f"{roc_year + 1911:04d}{month:02d}"


def forecast_url_map(page_html: bytes) -> dict[str, str]:
    parser = ForecastLinkParser()
    parser.feed(page_html.decode("utf-8", errors="replace"))
    result: dict[str, str] = {}
    for url in parser.links:
        path = urlparse(url).path
        filename = Path(path).name
        if len(filename) == 10 and filename[:6].isdigit() and filename.endswith(".pdf"):
            result[filename[:6]] = url
    return result


def fetch_bytes(url: str, timeout: int) -> bytes:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_pdf(url: str, timeout: int) -> bytes:
    data = fetch_bytes(url, timeout)
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"下載內容不是有效 PDF：{url}")
    return data


def download_forecasts(
    destination: Path,
    page_url: str = PAGE_URL,
    overwrite: bool = False,
    timeout: int = 30,
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    page_data = fetch_bytes(page_url, timeout)
    links = forecast_url_map(page_data)
    saved_files: list[Path] = []

    for roc_year, month in month_range(START_MONTH, END_MONTH):
        western = western_month(roc_year, month)
        url = links.get(western) or f"{FORECAST_URL}{western}.pdf"
        output = destination / f"{FILE_PREFIX}{western}.pdf"
        if output.exists() and not overwrite:
            if not output.read_bytes().startswith(b"%PDF-"):
                raise ValueError(f"既有檔案不是有效 PDF，請使用 --overwrite：{output}")
            saved_files.append(output)
            print(f"SKIP {output.name}")
            continue

        data = fetch_pdf(url, timeout)
        temporary = output.with_suffix(output.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(output)
        saved_files.append(output)
        print(f"OK {output.name} ({len(data)} bytes)")

    return saved_files


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("台經院景氣動向") / "Original",
        help="PDF 輸出資料夾",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆蓋既有 PDF")
    parser.add_argument("--timeout", type=int, default=30, help="每次下載的逾時秒數")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    download_forecasts(args.destination, overwrite=args.overwrite, timeout=args.timeout)


if __name__ == "__main__":
    main()