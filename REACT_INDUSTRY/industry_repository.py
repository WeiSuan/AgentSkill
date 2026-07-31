from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import psycopg


@dataclass(frozen=True)
class IndustryCatalogItem:
    code: str
    labels: tuple[str, ...]


@dataclass(frozen=True)
class IndustryAssetResult:
    code: str
    asset_amount: Decimal
    total_company_assets: Decimal
    percentage: Decimal | None
    has_data: bool


def load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip())


def database_connection_string() -> str:
    required_keys = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing_keys = [key for key in required_keys if not os.environ.get(key)]
    if missing_keys:
        raise ValueError(f".env 缺少 PostgreSQL 設定: {', '.join(missing_keys)}")

    host = os.environ.get("POSTGRES_HOST") or os.environ.get("POSTGRES_LOCALHOST") or "localhost"
    return (
        f"host={host} "
        f"port={os.environ.get('POSTGRES_PORT', '5432')} "
        f"dbname={os.environ['POSTGRES_DB']} "
        f"user={os.environ['POSTGRES_USER']} "
        f"password={os.environ['POSTGRES_PASSWORD']}"
    )


def calculate_industry_assets(
    requested_codes: Sequence[str],
    company_loans: Iterable[tuple[int, Decimal | int | None]],
    industry_loans: Iterable[tuple[str, int, Decimal | int | None]],
) -> list[IndustryAssetResult]:
    unique_company_loans: dict[int, Decimal] = {}
    for loan_no, amount in company_loans:
        if amount is not None:
            unique_company_loans.setdefault(loan_no, Decimal(amount))

    total_company_assets = sum(unique_company_loans.values(), Decimal())
    unique_industry_loans: dict[str, dict[int, Decimal]] = {
        code: {} for code in requested_codes
    }
    for code, loan_no, amount in industry_loans:
        if code in unique_industry_loans and amount is not None:
            unique_industry_loans[code].setdefault(loan_no, Decimal(amount))

    results = []
    for code in requested_codes:
        asset_amount = sum(unique_industry_loans[code].values(), Decimal())
        has_data = bool(unique_industry_loans[code])
        percentage = (
            asset_amount / total_company_assets * Decimal("100")
            if total_company_assets
            else None
        )
        results.append(
            IndustryAssetResult(
                code=code,
                asset_amount=asset_amount,
                total_company_assets=total_company_assets,
                percentage=percentage,
                has_data=has_data,
            )
        )
    return results


class IndustryRepository:
    def __init__(self, connection_string: str, catalog_path: Path | None = None) -> None:
        self._connection_string = connection_string
        self._catalog_path = catalog_path or (
            Path(__file__).resolve().parent / "115年主計處產業代碼(四碼).xlsx"
        )

    def get_catalog(self) -> list[IndustryCatalogItem]:
        if not self._catalog_path.exists():
            raise FileNotFoundError(f"找不到產業代碼 Excel: {self._catalog_path}")

        required_columns = {"head4_industry_code", "head4_industry_name"}
        catalog_df = pd.read_excel(self._catalog_path)
        missing_columns = required_columns - set(catalog_df.columns)
        if missing_columns:
            raise ValueError(f"產業代碼 Excel 缺少欄位: {sorted(missing_columns)}")

        catalog: dict[str, list[str]] = {}
        for row in catalog_df[list(required_columns)].to_dict("records"):
            raw_code = row["head4_industry_code"]
            raw_name = row["head4_industry_name"]
            if pd.isna(raw_code) or pd.isna(raw_name):
                continue

            try:
                code = str(int(float(raw_code))).zfill(4)
            except (TypeError, ValueError):
                raise ValueError(f"產業代碼 Excel 有無效的 head4_industry_code: {raw_code!r}") from None

            name = str(raw_name).strip()
            if not name:
                continue
            catalog.setdefault(code, [])
            if name not in catalog[code]:
                catalog[code].append(name)

        return [
            IndustryCatalogItem(code=code, labels=tuple(names))
            for code, names in sorted(catalog.items())
        ]

    def analyze_industries(self, requested_codes: Sequence[str]) -> list[IndustryAssetResult]:
        requested_codes = list(dict.fromkeys(requested_codes))
        if not requested_codes:
            return []

        company_sql = """
            SELECT DISTINCT ON (lcb.loan_no)
                lcb.loan_no,
                lcb.loan_rem_capital
            FROM loan_capital_balances lcb
            WHERE lcb.loan_rem_capital IS NOT NULL
            ORDER BY lcb.loan_no, lcb.loan_capital_balance_id
        """
        industry_sql = """
            SELECT DISTINCT ON (ci.head4_industry_code, lcb.loan_no)
                ci.head4_industry_code,
                lcb.loan_no,
                lcb.loan_rem_capital
            FROM loan_capital_balances lcb
            JOIN customer_industries ci
              ON ci.customer_id_no = lcb.customer_id_no
                        WHERE ci.head4_industry_code = ANY(%s)
              AND lcb.loan_rem_capital IS NOT NULL
                        ORDER BY ci.head4_industry_code, lcb.loan_no, lcb.loan_capital_balance_id
        """
        with psycopg.connect(self._connection_string) as connection:
            with connection.cursor() as cursor:
                cursor.execute(company_sql)
                company_loans = cursor.fetchall()
                cursor.execute(industry_sql, (requested_codes,))
                industry_loans = cursor.fetchall()

        return calculate_industry_assets(requested_codes, company_loans, industry_loans)