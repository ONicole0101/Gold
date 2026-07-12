from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import pandas as pd

import R_DataSet_Parquet as dataset_core


SOURCE_PATTERN = re.compile(
    r"^TaiwanStockTradingDailyReport_(\d{8})\.parquet$"
)
DEFAULT_TOP_N = 15
DEFAULT_LOT_SIZE = 1000.0
DEFAULT_COMPRESSION = os.getenv("R_DATASET_PARQUET_COMPRESSION", "zstd")

OUTPUT_COLUMNS = [
    "交易日",
    "股票代碼",
    "所有券商分點合計張數",
    "前15券商分點合計買張數",
    "前15券商分點合計賣張數",
    "前15券商分點買賣超張數",
    "前15買賣超占所有成交張數比率",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build per-stock/per-trading-day chip summaries from "
            "TaiwanStockTradingDailyReport_YYYYMMDD.parquet"
        )
    )
    parser.add_argument(
        "--source",
        default="all",
        help="all, YYYYMMDD, or an explicit source parquet path",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--lot-size",
        type=float,
        default=float(os.getenv("TAIWAN_STOCK_LOT_SIZE", DEFAULT_LOT_SIZE)),
        help="Shares per lot; Taiwan stocks default to 1000",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("R_DATASET_CHIPS_OUTPUT_DIR", ""),
        help="Default: <R_DATASET_OUTPUT_DIR>/chips",
    )
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    return parser.parse_args()


def find_source_files(source: str) -> list[Path]:
    value = str(source or "all").strip()
    if value.lower() == "all":
        return sorted(
            path
            for path in dataset_core.OUTPUT_DIR.glob(
                "TaiwanStockTradingDailyReport_*.parquet"
            )
            if SOURCE_PATTERN.match(path.name)
        )
    if re.fullmatch(r"\d{8}", value):
        path = (
            dataset_core.OUTPUT_DIR
            / f"TaiwanStockTradingDailyReport_{value}.parquet"
        )
        return [path] if path.exists() else []
    path = Path(value).expanduser().resolve()
    return [path] if path.exists() else []


def require_columns(frame: pd.DataFrame, source_path: Path) -> None:
    required = {"date", "stock_id", "buy", "sell"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(
            f"{source_path.name} missing required column(s): {', '.join(missing)}"
        )
    if not ({"securities_trader_id", "securities_trader"} & set(frame.columns)):
        raise RuntimeError(
            f"{source_path.name} requires securities_trader_id or securities_trader"
        )


def normalize_source(frame: pd.DataFrame, source_path: Path) -> pd.DataFrame:
    require_columns(frame, source_path)
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.strftime(
        "%Y-%m-%d"
    )
    work["stock_id"] = work["stock_id"].astype("string").str.strip()
    work["buy"] = pd.to_numeric(
        work["buy"].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)
    work["sell"] = pd.to_numeric(
        work["sell"].astype("string").str.replace(",", "", regex=False),
        errors="coerce",
    ).fillna(0.0)

    trader_id = (
        work["securities_trader_id"].astype("string").fillna("").str.strip()
        if "securities_trader_id" in work.columns
        else pd.Series("", index=work.index, dtype="string")
    )
    trader_name = (
        work["securities_trader"].astype("string").fillna("").str.strip()
        if "securities_trader" in work.columns
        else pd.Series("", index=work.index, dtype="string")
    )
    work["branch_key"] = trader_id.where(trader_id.ne(""), trader_name)
    work = work[
        work["date"].notna()
        & work["stock_id"].notna()
        & work["stock_id"].ne("")
        & work["branch_key"].ne("")
    ]
    return work[["date", "stock_id", "branch_key", "buy", "sell"]]


def summarize_group(
    branch_rows: pd.DataFrame,
    trading_date: str,
    stock_id: str,
    top_n: int,
    lot_size: float,
) -> pd.DataFrame:
    all_volume_lots = (
        branch_rows["buy"].sum() + branch_rows["sell"].sum()
    ) / lot_size
    top_buy_lots = branch_rows.nlargest(top_n, "buy")["buy"].sum() / lot_size
    top_sell_lots = -(
        branch_rows.nlargest(top_n, "sell")["sell"].sum() / lot_size
    )
    top_net_lots = top_buy_lots + top_sell_lots
    ratio = top_net_lots / all_volume_lots if all_volume_lots != 0 else pd.NA

    return pd.DataFrame(
        [
            {
                "交易日": trading_date,
                "股票代碼": stock_id,
                "所有券商分點合計張數": all_volume_lots,
                "前15券商分點合計買張數": top_buy_lots,
                "前15券商分點合計賣張數": top_sell_lots,
                "前15券商分點買賣超張數": top_net_lots,
                "前15買賣超占所有成交張數比率": ratio,
            }
        ],
        columns=OUTPUT_COLUMNS,
    )


def process_source(
    source_path: Path,
    output_dir: Path,
    top_n: int,
    lot_size: float,
) -> tuple[int, int]:
    source = pd.read_parquet(source_path, engine="pyarrow")
    work = normalize_source(source, source_path)
    if work.empty:
        print(f"no valid rows: {source_path.name}", flush=True)
        return 0, 0

    branches = (
        work.groupby(
            ["date", "stock_id", "branch_key"],
            as_index=False,
            sort=False,
            dropna=False,
        )[["buy", "sell"]]
        .sum()
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    input_groups = 0
    for (trading_date, stock_id), group in branches.groupby(
        ["date", "stock_id"], sort=True
    ):
        input_groups += 1
        summary = summarize_group(
            group,
            str(trading_date),
            str(stock_id),
            top_n,
            lot_size,
        )
        date_key = str(trading_date).replace("-", "")
        safe_stock_id = re.sub(r"[^0-9A-Za-z._-]", "_", str(stock_id))
        output_path = output_dir / f"{safe_stock_id}_{date_key}.parquet"
        summary.to_parquet(
            output_path,
            index=False,
            engine="pyarrow",
            compression=DEFAULT_COMPRESSION,
        )
        written += 1

    return input_groups, written


def main() -> None:
    args = parse_args()
    if args.top_n <= 0:
        raise RuntimeError("--top-n must be greater than zero")
    if args.lot_size <= 0:
        raise RuntimeError("--lot-size must be greater than zero")

    if not args.no_download:
        dataset_core.download_google_dataset()

    source_files = find_source_files(args.source)
    if not source_files:
        raise RuntimeError(
            "No TaiwanStockTradingDailyReport_YYYYMMDD.parquet source file found"
        )

    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else dataset_core.OUTPUT_DIR / "chips"
    )
    total_groups = 0
    total_written = 0
    for source_path in source_files:
        groups, written = process_source(
            source_path,
            output_dir,
            args.top_n,
            args.lot_size,
        )
        total_groups += groups
        total_written += written
        print(
            f"processed: {source_path.name}, groups={groups}, files={written}",
            flush=True,
        )

    if not args.no_upload:
        dataset_core.upload_google_dataset()

    print(
        f"chip summary completed: source_files={len(source_files)}, "
        f"groups={total_groups}, files={total_written}, output_dir={output_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
