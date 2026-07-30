import argparse
import json
from pathlib import Path

import pandas as pd


INPUT_DIR = "直營_客戶資訊"
OUTPUT_EXCEL = "直營(截至20260630)_客戶資訊彙整.xlsx"
INDUSTRY_CODE_EXCEL = "115年主計處產業代碼(四碼).xlsx"

OUTPUT_COLUMNS = [
    "customer_id_no",
    "success",
    "industryCd",
    "industryNm",
    "industryCd1",
    "industryNm1",
    "industryCd2",
    "industryNm2",
    "industryCd3",
    "industryNm3",
]

EXPANDED_COLUMNS = [
    "customer_id_no",
    "success",
    "industry_code",
    "mid_industry_code",
    "detail_indsutry_code",
    "industry_name",
]


def normalize_industry_code(value) -> str:
    if pd.isna(value):
        return ""

    text = str(value).strip()
    if not text:
        return ""

    return text.zfill(6)

def load_row(json_path: Path) -> dict:
    with json_path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)

    data = payload.get("data") or {}

    return {
        "customer_id_no": payload.get("customer_id_no", ""),
        "success": payload.get("success", ""),
        "industryCd": data.get("industryCd", ""),
        "industryNm": data.get("industryNm", ""),
        "industryCd1": data.get("industryCd1", ""),
        "industryNm1": data.get("industryNm1", ""),
        "industryCd2": data.get("industryCd2", ""),
        "industryNm2": data.get("industryNm2", ""),
        "industryCd3": data.get("industryCd3", ""),
        "industryNm3": data.get("industryNm3", ""),
    }


def load_industry_code_df(industry_code_excel: Path) -> pd.DataFrame:
    industry_code_df = pd.read_excel(industry_code_excel, usecols=["id", "name"])
    industry_code_df["id"] = (
        pd.to_numeric(industry_code_df["id"], errors="raise")
        .astype("int64")
        .astype(str)
        .str.zfill(4)
    )
    industry_code_df["name"] = industry_code_df["name"].fillna("").astype(str)
    return industry_code_df.drop_duplicates(subset="id", keep="first")


def build_expanded_industry_df(df: pd.DataFrame) -> pd.DataFrame:
    expanded_rows = []

    industry_pairs = [
        ("industryCd", "industryNm"),
        ("industryCd1", "industryNm1"),
        ("industryCd2", "industryNm2"),
        ("industryCd3", "industryNm3"),
    ]

    for row in df.to_dict(orient="records"):
        for code_col, name_col in industry_pairs:
            industry_code = normalize_industry_code(row.get(code_col, ""))
            industry_name = row.get(name_col, "")

            if pd.isna(industry_name):
                industry_name = ""

            if not industry_code and not industry_name:
                continue

            mid_industry_code = industry_code[:2] if industry_code else ""
            detail_indsutry_code = industry_code[:4] if industry_code else ""

            expanded_rows.append(
                {
                    "customer_id_no": row["customer_id_no"],
                    "success": row["success"],
                    "industry_code": industry_code,
                    "mid_industry_code": mid_industry_code,
                    "detail_indsutry_code": detail_indsutry_code,
                    "industry_name": industry_name,
                }
            )

    return pd.DataFrame(expanded_rows, columns=EXPANDED_COLUMNS)


def main(input_dir: Path, output_excel: Path) -> None:
    if not input_dir.exists():
        raise FileNotFoundError(f"找不到資料夾: {input_dir}")

    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        raise FileNotFoundError(f"資料夾內沒有 JSON 檔案: {input_dir}")

    rows = [load_row(json_path) for json_path in json_files]
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    expanded_df = build_expanded_industry_df(df)

    industry_code_excel = Path(__file__).resolve().parent / INDUSTRY_CODE_EXCEL
    industry_code_df = load_industry_code_df(industry_code_excel)

    expanded_df = expanded_df.merge(
        industry_code_df,
        how="left",
        left_on="detail_indsutry_code",
        right_on="id",
    )

    expanded_df = expanded_df[
        [
            "customer_id_no",
            "success",
            "mid_industry_code",
            "detail_indsutry_code",
            "name",
            "industry_code",
            "industry_name",
        ]
    ].rename(
        columns={
            "mid_industry_code": "head2_industry_code",
            "detail_indsutry_code": "head4_indsutry_code",
            "name": "head4_indsutry_name",
        }
    )

    with pd.ExcelWriter(output_excel) as writer:
        df.to_excel(writer, sheet_name="raw_data", index=False)
        expanded_df.to_excel(writer, sheet_name="industry_expanded", index=False)

    print(f"已輸出 Excel: {output_excel}")
    print(f"共彙整 {len(df)} 筆 JSON 資料")
    print(f"共展開 {len(expanded_df)} 筆產業資料")


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="彙整客戶登記 JSON 資料為 Excel。")
    parser.add_argument(
        "--input_dir",
        type=Path,
        default=base_dir / INPUT_DIR,
        help=f"JSON 資料夾路徑，預設為 {INPUT_DIR}",
    )
    parser.add_argument(
        "--output_excel",
        type=Path,
        default=base_dir / OUTPUT_EXCEL,
        help=f"輸出 Excel 路徑，預設為 {OUTPUT_EXCEL}",
    )
    args = parser.parse_args()

    # 範例: python aggregate_business_registration.py --input_dir="直營_客戶資訊" --output_excel="直營_客戶資訊彙整.xlsx"
    main(args.input_dir, args.output_excel)