import json
from pathlib import Path

import pandas as pd
import requests


API_URL_TEMPLATE = "https://eip.fia.gov.tw/OAI/api/businessRegistration/{tmp_customer_id}"
INPUT_EXCEL = "微企直營本金餘額_截至20260630.xlsx"
OUTPUT_DIR = "直營_客戶資訊"
CUSTOMER_ID_COLUMNS = ("customer_id_no", "身份證號/統編")


def normalize_customer_id(value):
    """Convert input to integer string, then left-pad to 8 digits."""
    if pd.isna(value):
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        # Handles values like 12345, 12345.0, and "0012345".
        int_value = int(float(text))
    except ValueError:
        return None

    return str(int_value).zfill(8)


def main():
    base_dir = Path(__file__).resolve().parent
    excel_path = base_dir / INPUT_EXCEL
    output_path = base_dir / OUTPUT_DIR
    output_path.mkdir(parents=True, exist_ok=True)

    df = pd.read_excel(excel_path)

    customer_id_column = next(
        (column for column in CUSTOMER_ID_COLUMNS if column in df.columns),
        None,
    )
    if customer_id_column is None:
        raise KeyError(
            "Excel 缺少客戶識別欄位，預期欄位為: "
            f"{list(CUSTOMER_ID_COLUMNS)}，實際欄位為: {list(df.columns)}"
        )

    customer_ids = (
        df[customer_id_column]
        .map(normalize_customer_id)
        .dropna()
        .drop_duplicates()
        .tolist()
    )

    headers = {"accept": "*/*"}

    for sub_customer_id in customer_ids:
        url = API_URL_TEMPLATE.format(tmp_customer_id=sub_customer_id)

        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.exceptions.RequestException as exc:
            payload = {
                "customer_id_no": sub_customer_id,
                "success": False,
                "status_code": None,
                "error": str(exc),
            }
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except json.JSONDecodeError:
                    body = response.text

                payload = {
                    "customer_id_no": sub_customer_id,
                    "success": True,
                    "status_code": response.status_code,
                    "data": body,
                }
            else:
                payload = {
                    "customer_id_no": sub_customer_id,
                    "success": False,
                    "status_code": response.status_code,
                    "error": response.text,
                }

        file_path = output_path / f"{sub_customer_id}.json"
        with file_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)

        print(f"已輸出: {file_path}")


if __name__ == "__main__":
    main()