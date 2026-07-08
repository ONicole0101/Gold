import argparse
import os
from pathlib import Path

import pandas as pd

from custom_categories import CUSTOM_CATEGORY_COLUMN, _extract_stock_columns_from_excel


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _parse_a_column_text(raw: str) -> tuple[str, str]:
    """Parse A column as: first 4 chars = ticker, remaining chars = name."""
    text = _clean_text(raw)
    if not text:
        return "", ""

    ticker = text[:4]
    if len(ticker) != 4 or not ticker.isdigit():
        return "", ""

    name = _clean_text(text[4:])
    return ticker, name


def _build_stock_list_from_excel(excel_path: Path) -> pd.DataFrame:
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    workbook = pd.ExcelFile(excel_path)
    rows: list[dict[str, str]] = []
    report_lines = [f"workbook\t{excel_path.name}",
                    f"sheet_count\t{len(workbook.sheet_names)}"]

    for sheet_name in workbook.sheet_names:
        df = pd.read_excel(workbook, sheet_name=sheet_name, dtype=str)
        if df.empty:
            print(f"sheet {sheet_name}: empty", flush=True)
            report_lines.append(f"{sheet_name}\tempty\t0\t0")
            continue

        parsed = _extract_stock_columns_from_excel(df)
        if parsed.empty:
            print(f"sheet {sheet_name}: parsed 0 rows", flush=True)
            report_lines.append(
                f"{sheet_name}\tparsed_zero\t{len(df)}\t0\tcols="
                + " | ".join(str(c).strip() for c in df.columns.tolist()[:8])
            )
            continue

        print(f"sheet {sheet_name}: parsed {len(parsed)} rows", flush=True)
        report_lines.append(
            f"{sheet_name}\tok\t{len(df)}\t{len(parsed)}\tcols="
            + " | ".join(str(c).strip() for c in df.columns.tolist()[:8])
        )

        for _, row in parsed.iterrows():
            ticker = _clean_text(row.get("stock_id", ""))
            name = _clean_text(row.get("name", ""))
            if not ticker or not name:
                continue
            rows.append(
                {
                    "Ticker": ticker,
                    "Name": name,
                    CUSTOM_CATEGORY_COLUMN: sheet_name,
                }
            )

    if not rows:
        report_path = excel_path.parent.parent / "stocks_parse_report.txt"
        report_lines.append("generated_rows\t0")
        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        raise RuntimeError(
            "GoldSheet parsed zero stock rows. "
            "See stocks_parse_report.txt for per-sheet parse details."
        )

    merged = pd.DataFrame(rows)
    merged["Ticker"] = merged["Ticker"].map(_clean_text)
    merged["Name"] = merged["Name"].map(_clean_text)
    merged[CUSTOM_CATEGORY_COLUMN] = merged[CUSTOM_CATEGORY_COLUMN].map(
        _clean_text)
    merged = merged[merged["Ticker"] != ""]
    merged = (
        merged.groupby(["Ticker", "Name"], dropna=False,
                       sort=False)[CUSTOM_CATEGORY_COLUMN]
        .apply(lambda s: ";".join(sorted({x for x in s if x})))
        .reset_index()
    )
    merged["Ticker_num"] = pd.to_numeric(merged["Ticker"], errors="coerce")
    merged = (
        merged.sort_values(["Ticker_num", "Ticker"], ascending=[
                           True, True], na_position="last")
        .drop(columns=["Ticker_num"])
        .reset_index(drop=True)
    )
    report_path = excel_path.parent.parent / "stocks_parse_report.txt"
    report_lines.append(f"generated_rows\t{len(merged)}")
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    return merged[["Ticker", "Name", CUSTOM_CATEGORY_COLUMN]]


def _resolve_goldsheet_path(allcsv_dir: Path) -> Path:
    candidates = [
        allcsv_dir / "GoldSheet.xls",
        allcsv_dir / "GoldSheet.xlsx",
    ]
    for path in candidates:
        if path.is_file() and not path.name.startswith("~$"):
            return path
    raise FileNotFoundError(
        f"GoldSheet workbook not found under {allcsv_dir}. Expected one of: "
        + ", ".join(str(path.name) for path in candidates)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read Allcsv/GoldSheet workbook and build stocks.csv using sheet names as 自選分類"
    )
    parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    allcsv_dir = base_dir / "Allcsv"
    output_csv = base_dir / "stocks.csv"
    workbook_path = _resolve_goldsheet_path(allcsv_dir)

    print("cwd =", os.getcwd(), flush=True)
    print("script dir =", base_dir, flush=True)
    print("scan_dir =", allcsv_dir, flush=True)
    print("workbook =", workbook_path, flush=True)
    print("output_csv =", output_csv, flush=True)

    if not allcsv_dir.is_dir():
        raise FileNotFoundError(f"Allcsv directory not found: {allcsv_dir}")

    # Prevent stale output from previous runs when current parsing fails.
    output_csv.unlink(missing_ok=True)

    result = _build_stock_list_from_excel(workbook_path)

    result.to_csv(output_csv, sep="\t", index=False, encoding="utf-8-sig")
    category_count = int(
        (result[CUSTOM_CATEGORY_COLUMN].fillna("") != "").sum())
    print(f"written: {output_csv}", flush=True)
    print(f"rows: {len(result)}", flush=True)
    print(f"rows with {CUSTOM_CATEGORY_COLUMN}: {category_count}", flush=True)


if __name__ == "__main__":
    main()
