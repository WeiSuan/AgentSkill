import argparse
import os
from pathlib import Path

import pandas as pd
import psycopg


INDUSTRY_SHEET = "industry_expanded"
CAPITAL_SHEET = "RawData"
INDUSTRY_COLUMNS = [
    "customer_id_no",
    "success",
    "head2_industry_code",
    "head4_indsutry_code",
    "head4_indsutry_name",
    "industry_code",
    "industry_name",
]


def load_dotenv(env_path: Path) -> None:
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip())


def normalize_customer_id(value: object) -> str | None:
    if pd.isna(value):
        return None

    try:
        return str(int(float(str(value).strip()))).zfill(8)
    except (TypeError, ValueError):
        return None


def normalize_code(value: object, width: int) -> str | None:
    if pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return str(int(float(text))).zfill(width)
    except ValueError:
        return text.zfill(width)


def database_connection_string() -> str:
    return (
        f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ['POSTGRES_DB']} "
        f"user={os.environ['POSTGRES_USER']} "
        f"password={os.environ['POSTGRES_PASSWORD']}"
    )


def import_customer_industries(
    connection: psycopg.Connection, industry_path: Path
) -> int:
    df = pd.read_excel(industry_path, sheet_name=INDUSTRY_SHEET)
    missing_columns = set(INDUSTRY_COLUMNS) - set(df.columns)
    if missing_columns:
        raise ValueError(f"industry_expanded 缺少欄位: {sorted(missing_columns)}")

    rows = []
    for source_row_number, row in enumerate(df[INDUSTRY_COLUMNS].to_dict("records"), start=2):
        customer_id_no = normalize_customer_id(row["customer_id_no"])
        if customer_id_no is None:
            raise ValueError(f"industry_expanded 第 {source_row_number} 列的 customer_id_no 無效")

        rows.append(
            (
                customer_id_no,
                bool(row["success"]),
                normalize_code(row["head2_industry_code"], 2),
                normalize_code(row["head4_indsutry_code"], 4),
                row["head4_indsutry_name"] if not pd.isna(row["head4_indsutry_name"]) else None,
                normalize_code(row["industry_code"], 6),
                row["industry_name"] if not pd.isna(row["industry_name"]) else None,
                industry_path.name,
                source_row_number,
            )
        )

    sql = """
        INSERT INTO customer_industries (
            customer_id_no, api_success, head2_industry_code,
            head4_industry_code, head4_industry_name, industry_code,
            industry_name, source_file, source_row_number
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_file, source_row_number) DO UPDATE SET
            customer_id_no = EXCLUDED.customer_id_no,
            api_success = EXCLUDED.api_success,
            head2_industry_code = EXCLUDED.head2_industry_code,
            head4_industry_code = EXCLUDED.head4_industry_code,
            head4_industry_name = EXCLUDED.head4_industry_name,
            industry_code = EXCLUDED.industry_code,
            industry_name = EXCLUDED.industry_name,
            updated_at = CURRENT_TIMESTAMP
    """
    with connection.cursor() as cursor:
        cursor.executemany(sql, rows)
    return len(rows)


def import_loan_capital_balances(
    connection: psycopg.Connection, capital_path: Path
) -> int:
    df = pd.read_excel(capital_path, sheet_name=CAPITAL_SHEET)
    capital_columns = {
        "群組編號": "examine_group_no",
        "合約編號": "loan_no",
        "本金餘額": "loan_rem_capital",
        "行業別大類": "major_industry_desc",
        "行業別中類": "industry_desc",
        "customer_id_no": "customer_id_no",
        "客戶名稱": "customer_name",
    }
    missing_columns = set(capital_columns) - set(df.columns)
    if missing_columns:
        raise ValueError(f"RawData 缺少欄位: {sorted(missing_columns)}")

    rows = []
    for source_row_number, row in enumerate(df.to_dict("records"), start=2):
        customer_id_no = normalize_customer_id(row["customer_id_no"])
        if customer_id_no is None:
            raise ValueError(f"RawData 第 {source_row_number} 列的 customer_id_no 無效")

        rows.append(
            (
                int(row["群組編號"]),
                int(row["合約編號"]),
                int(row["本金餘額"]) if not pd.isna(row["本金餘額"]) else None,
                row["行業別大類"] if not pd.isna(row["行業別大類"]) else None,
                row["行業別中類"] if not pd.isna(row["行業別中類"]) else None,
                customer_id_no,
                row["客戶名稱"] if not pd.isna(row["客戶名稱"]) else None,
            )
        )

    sql = """
        INSERT INTO loan_capital_balances (
            examine_group_no, loan_no, loan_rem_capital, major_industry_desc,
            industry_desc, customer_id_no, customer_name
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (examine_group_no, loan_no) DO UPDATE SET
            loan_rem_capital = EXCLUDED.loan_rem_capital,
            major_industry_desc = EXCLUDED.major_industry_desc,
            industry_desc = EXCLUDED.industry_desc,
            customer_id_no = EXCLUDED.customer_id_no,
            customer_name = EXCLUDED.customer_name
    """
    with connection.cursor() as cursor:
        cursor.executemany(sql, rows)
    return len(rows)


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="匯入客戶產業與本金餘額 Excel 至 PostgreSQL。")
    parser.add_argument(
        "--industry_excel",
        type=Path,
        default=base_dir / "直營_客戶資訊彙整.xlsx",
    )
    parser.add_argument(
        "--capital_excel",
        type=Path,
        default=base_dir / "微企直營本金餘額_截至20260630.xlsx",
    )
    parser.add_argument("--env_file", type=Path, default=base_dir / ".env")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    with psycopg.connect(database_connection_string()) as connection:
        industry_count = import_customer_industries(connection, args.industry_excel)
        capital_count = import_loan_capital_balances(connection, args.capital_excel)

    print(f"已匯入 customer_industries: {industry_count} 筆")
    print(f"已匯入 loan_capital_balances: {capital_count} 筆")


if __name__ == "__main__":
    main()