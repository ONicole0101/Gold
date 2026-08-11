from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

CUSTOM_CATEGORY_COLUMN = "自選分類"
CUSTOM_CATEGORY_SEPARATOR = ";"
STOCK_CODE_PATTERN = re.compile(r"(?<![0-9A-Z])(\d{4,6}[A-Z]?)(?![0-9A-Z])")
COMBINED_STOCK_PATTERN = re.compile(
    r"(?<![0-9A-Z])(\d{4,6}[A-Z]?)(?![0-9A-Z])\s*[-:：]?\s*([^\d\s].*)"
)

CATEGORY_ALIAS_MAP = {
    "燕莉2": "燕俐2",
    "ABF載板": "ABF窄板",
}


def _pandas():
    import pandas as pd

    return pd


def _read_csv_flexible(path: Path) -> pd.DataFrame:
    pd = _pandas()
    encodings = ("utf-8-sig", "utf-8", "cp950", "big5")
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            df = pd.read_csv(path, sep="\t", encoding=encoding, dtype=str)
            if len(df.columns) == 1:
                df = pd.read_csv(path, encoding=encoding, dtype=str)
            return df
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError(f"cannot read csv: {path}")


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def _normalize_stock_code(value: Any) -> str:
    text = _clean_text(value).upper()
    if not text:
        return ""

    left6 = re.sub(r"[\s\u3000]+", "", text[:6])
    if re.match(r"^\d{4,6}[A-Z]?$", left6):
        return left6

    compact = re.sub(r"[\s\u3000]+", "", text)
    if re.match(r"^\d{4,6}[A-Z]?$", compact):
        return compact

    return ""


def normalize_category_label(label: Any) -> str:
    """Normalize category labels to avoid option mismatch caused by naming variants."""
    text = _clean_text(label)
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    return CATEGORY_ALIAS_MAP.get(text, text)


def _extract_stock_columns(df: pd.DataFrame) -> pd.DataFrame:
    pd = _pandas()
    df = df.copy()
    df.columns = df.columns.str.strip()

    stock_col = next(
        (c for c in ("Ticker", "stock_id", "代碼", "股票代碼", "證券代號") if c in df.columns),
        None,
    )
    name_col = next(
        (c for c in ("Name", "name", "名稱", "股票名稱", "證券名稱") if c in df.columns),
        None,
    )
    if stock_col and name_col:
        out = df[[stock_col, name_col]].copy()
        out.columns = ["stock_id", "name"]
        out["stock_id"] = out["stock_id"].map(_normalize_stock_code)
        out["name"] = out["name"].map(_clean_text)
        out = out[(out["stock_id"] != "") & (out["name"] != "")]
        return out

    combined_col = next(
        (c for c in ("個股名稱", "股票名稱", "證券名稱", "股票", "標的") if c in df.columns),
        None,
    )
    if combined_col:
        rows = []
        for raw in df[combined_col].tolist():
            ticker, name = _parse_combined_stock_text(raw)
            if ticker and name:
                rows.append({"stock_id": ticker, "name": name})
        if rows:
            return pd.DataFrame(rows).drop_duplicates(subset=["stock_id", "name"]).reset_index(drop=True)

    return pd.DataFrame(columns=["stock_id", "name"])


def _parse_combined_stock_text(raw: Any) -> tuple[str, str]:
    text = _clean_text(raw)
    if len(text) < 5:
        return "", ""

    left6_code = _normalize_stock_code(text[:6])
    if left6_code:
        name = _clean_text(text[6:])
        if name:
            return left6_code, name

    match = COMBINED_STOCK_PATTERN.search(text)
    if match:
        ticker = _normalize_stock_code(match.group(1))
        name = _clean_text(match.group(2))
        if ticker and name:
            return ticker, name

    token_match = re.match(r"^(\d{4,6}[A-Z]?)\s+(.+)$", text, flags=re.IGNORECASE)
    if token_match:
        ticker = _normalize_stock_code(token_match.group(1))
        name = _clean_text(token_match.group(2))
        if ticker and name:
            return ticker, name

    return "", ""


def _looks_like_stock_code(text: str) -> bool:
    return bool(_normalize_stock_code(text))


def _pick_name_candidate(cells: list[str], code_index: int) -> str:
    preferred_indexes = [code_index + 1, code_index - 1]
    for idx in preferred_indexes:
        if idx < 0 or idx >= len(cells):
            continue
        cell = _clean_text(cells[idx])
        if cell and not _looks_like_stock_code(cell):
            parsed_code, parsed_name = _parse_combined_stock_text(cell)
            if parsed_code and parsed_name:
                continue
            return cell

    for idx, cell in enumerate(cells):
        if idx == code_index:
            continue
        cell = _clean_text(cell)
        if cell and not _looks_like_stock_code(cell):
            parsed_code, parsed_name = _parse_combined_stock_text(cell)
            if parsed_code and parsed_name:
                continue
            return cell
    return ""


def _extract_stock_columns_from_excel(df: pd.DataFrame) -> pd.DataFrame:
    pd = _pandas()
    explicit = _extract_stock_columns(df)
    if not explicit.empty:
        return explicit

    if df is None or df.empty:
        return pd.DataFrame(columns=["stock_id", "name"])

    work = df.copy()
    work.columns = [str(c).strip() for c in work.columns]
    rows: list[dict[str, str]] = []

    for _, row in work.iterrows():
        cells = [_clean_text(value) for value in row.tolist()]
        cells = [cell for cell in cells if cell]
        if not cells:
            continue

        row_pairs: list[tuple[str, str]] = []

        for cell in cells:
            ticker, name = _parse_combined_stock_text(cell)
            if ticker and name:
                row_pairs.append((ticker, name))

        if not row_pairs:
            for code_index, cell in enumerate(cells):
                if not _looks_like_stock_code(cell):
                    continue
                name = _pick_name_candidate(cells, code_index)
                if name:
                    row_pairs.append((cell, name))

        if not row_pairs:
            for cell in cells:
                for match in STOCK_CODE_PATTERN.finditer(cell):
                    ticker = _normalize_stock_code(match.group(1))
                    remainder = _clean_text(cell[match.end():])
                    if ticker and remainder:
                        row_pairs.append((ticker, remainder))

        for ticker, name in row_pairs:
            if ticker and name:
                rows.append({"stock_id": ticker, "name": name})

    if not rows:
        return pd.DataFrame(columns=["stock_id", "name"])

    out = pd.DataFrame(rows)
    out["stock_id"] = out["stock_id"].map(_clean_text)
    out["name"] = out["name"].map(_clean_text)
    out = out[(out["stock_id"] != "") & (out["name"] != "")]
    return out.drop_duplicates(subset=["stock_id", "name"]).reset_index(drop=True)


def _resolve_goldsheet_path(base: Path) -> Path | None:
    for name in ("GoldSheet.xls", "GoldSheet.xlsx"):
        path = base / name
        if path.is_file() and not path.name.startswith("~$"):
            return path
    return None


def _load_custom_category_entries_from_workbook(allcsv_dir: str | os.PathLike[str]) -> pd.DataFrame:
    pd = _pandas()
    base = Path(allcsv_dir)
    workbook_path = _resolve_goldsheet_path(base)
    if workbook_path is None:
        return pd.DataFrame(columns=["stock_id", "name", "category"])

    rows: list[pd.DataFrame] = []
    workbook = pd.ExcelFile(workbook_path)
    for sheet_name in workbook.sheet_names:
        try:
            df = pd.read_excel(workbook, sheet_name=sheet_name, dtype=str)
        except Exception as exc:
            print(f"skip category sheet {sheet_name}: {exc}", flush=True)
            continue
        entry_df = _extract_stock_columns_from_excel(df)
        if entry_df.empty:
            continue
        entry_df["category"] = _clean_text(sheet_name)
        rows.append(entry_df)

    if not rows:
        return pd.DataFrame(columns=["stock_id", "name", "category"])
    return pd.concat(rows, ignore_index=True)


def load_custom_category_entries(allcsv_dir: str | os.PathLike[str] = "Allcsv") -> pd.DataFrame:
    pd = _pandas()
    base = Path(allcsv_dir)
    if not base.is_dir():
        return pd.DataFrame(columns=["stock_id", "name", "category"])

    workbook_path = _resolve_goldsheet_path(base)
    if workbook_path is not None:
        workbook_entries = _load_custom_category_entries_from_workbook(
            allcsv_dir)
        if workbook_entries.empty:
            print(
                f"GoldSheet exists but no stock rows parsed: {workbook_path}. CSV fallback disabled.",
                flush=True,
            )
        return workbook_entries

    rows: list[pd.DataFrame] = []

    for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.suffix.lower() not in {".csv", ".txt"}:
            continue
        try:
            df = _extract_stock_columns(_read_csv_flexible(path))
        except Exception as exc:
            print(f"skip category file {path.name}: {exc}", flush=True)
            continue
        if df.empty:
            continue
        df["category"] = path.stem
        rows.append(df)

    if not rows:
        return pd.DataFrame(columns=["stock_id", "name", "category"])
    return pd.concat(rows, ignore_index=True)


def category_text(categories: list[str] | tuple[str, ...] | set[str] | str | None) -> str:
    if categories is None:
        return ""
    raw_parts = [categories] if isinstance(
        categories, str) else list(categories)
    parts: list[str] = []
    for raw in raw_parts:
        text = str(raw or "")
        parts.extend(
            text.replace("、", CUSTOM_CATEGORY_SEPARATOR)
            .replace(",", CUSTOM_CATEGORY_SEPARATOR)
            .split(CUSTOM_CATEGORY_SEPARATOR)
        )
    cleaned = sorted({normalize_category_label(x)
                     for x in parts if normalize_category_label(x)})
    return CUSTOM_CATEGORY_SEPARATOR.join(cleaned)


def split_category_text(categories: list[str] | tuple[str, ...] | set[str] | str | None) -> list[str]:
    text = category_text(categories)
    if not text:
        return []
    return [part for part in text.split(CUSTOM_CATEGORY_SEPARATOR) if part]


def load_custom_category_map(allcsv_dir: str | os.PathLike[str] = "Allcsv") -> dict[str, list[str]]:
    entries = load_custom_category_entries(allcsv_dir)
    if entries.empty:
        return {}
    mapping: dict[str, list[str]] = {}
    for stock_id, group in entries.groupby("stock_id", sort=False):
        mapping[str(stock_id)] = sorted(
            {_clean_text(x) for x in group["category"] if _clean_text(x)})
    return mapping


def load_custom_category_text_map(allcsv_dir: str | os.PathLike[str] = "Allcsv") -> dict[str, str]:
    return {stock_id: category_text(categories) for stock_id, categories in load_custom_category_map(allcsv_dir).items()}


def category_options(allcsv_dir: str | os.PathLike[str] = "Allcsv") -> list[str]:
    base = Path(allcsv_dir)
    if not base.is_dir():
        return []

    workbook_path = _resolve_goldsheet_path(base)
    if workbook_path is not None:
        try:
            pd = _pandas()
            return sorted({
                normalize_category_label(name)
                for name in pd.ExcelFile(workbook_path).sheet_names
                if normalize_category_label(name)
            })
        except Exception:
            pass

    return sorted({
        normalize_category_label(path.stem)
        for path in sorted(base.iterdir(), key=lambda p: p.name.lower())
        if path.is_file() and path.suffix.lower() in {".csv", ".txt"} and normalize_category_label(path.stem)
    })


def add_custom_categories_to_df(
    df: pd.DataFrame,
    allcsv_dir: str | os.PathLike[str] = "Allcsv",
    stock_col: str = "stock_id",
) -> pd.DataFrame:
    out = df.copy()
    if stock_col not in out.columns:
        out[CUSTOM_CATEGORY_COLUMN] = ""
        return out
    category_map = load_custom_category_text_map(allcsv_dir)
    out[CUSTOM_CATEGORY_COLUMN] = out[stock_col].astype(
        str).str.strip().map(category_map).fillna("")
    return out


def build_stock_list_from_allcsv(allcsv_dir: str | os.PathLike[str] = "Allcsv") -> pd.DataFrame:
    pd = _pandas()
    entries = load_custom_category_entries(allcsv_dir)
    if entries.empty:
        return pd.DataFrame(columns=["Ticker", "Name", CUSTOM_CATEGORY_COLUMN])

    grouped = (
        entries.groupby("stock_id", sort=False)
        .agg(
            Name=("name", "first"),
            **{CUSTOM_CATEGORY_COLUMN: ("category", lambda s: category_text(list(s)))},
        )
        .reset_index()
        .rename(columns={"stock_id": "Ticker"})
    )
    grouped["Ticker_num"] = pd.to_numeric(grouped["Ticker"], errors="coerce")
    grouped = (
        grouped.sort_values(["Ticker_num", "Ticker"], ascending=[
                            True, True], na_position="last")
        .drop(columns=["Ticker_num"])
        .reset_index(drop=True)
    )
    return grouped[["Ticker", "Name", CUSTOM_CATEGORY_COLUMN]]
