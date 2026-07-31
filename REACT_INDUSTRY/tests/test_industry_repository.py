from pathlib import Path

import pandas as pd

from industry_repository import IndustryRepository


def test_get_catalog_loads_and_normalizes_excel_codes(tmp_path: Path) -> None:
    catalog_path = tmp_path / "industry-catalog.xlsx"
    pd.DataFrame(
        {
            "head4_industry_code": [111, 5611, 5611],
            "head4_industry_name": ["稻作栽培業", "餐館", "麵店、小吃店"],
        }
    ).to_excel(catalog_path, index=False)

    catalog = IndustryRepository("unused-for-catalog", catalog_path).get_catalog()

    assert [(item.code, item.labels) for item in catalog] == [
        ("0111", ("稻作栽培業",)),
        ("5611", ("餐館", "麵店、小吃店")),
    ]
