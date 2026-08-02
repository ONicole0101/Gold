from datetime import UTC, datetime
import argparse
import os

import pandas as pd

import config
from data_sources import (
    get_finmind_token_status,
    get_finmind_user_info,
    get_per_pbr_60d_stats,
    log_finmind_static_event,
)
from financial_analysis import get_dividend_yield


VALUATION_COLS = [
    "per_latest", "per_60d_high", "per_60d_low",
    "pbr_latest", "pbr_60d_high", "pbr_60d_low",
    "yield_value",
    "per_latest_is_prev", "pbr_latest_is_prev",
    "valuation_updated_at", "valuation_status", "valuation_reason",
    "finmind_token_status", "finmind_token_source", "finmind_token_masked",
    "finmind_user_count", "finmind_api_request_limit", "finmind_remain",
    "finmind_usage_checked_at",
]
ORDERED_COLS = ["stock_id", "name"] + VALUATION_COLS


def now_utc_str():
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def resolve_config_value(env_name, config_name, default=None):
    value = os.getenv(env_name)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    value = getattr(config, config_name, default)
    if value is None:
        return default
    return str(value).strip()


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=ORDERED_COLS)
    df = df.copy()
    df.columns = df.columns.str.strip()
    for col in ORDERED_COLS:
        if col not in df.columns:
            df[col] = None
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df[ORDERED_COLS]


def atomic_write_csv(df: pd.DataFrame, path: str):
    tmp_path = path + ".tmp"
    normalize_df(df).to_csv(tmp_path, index=False, encoding="utf-8-sig")
    os.replace(tmp_path, path)


def load_stock_list(csv_file=None):
    csv_file = csv_file or resolve_config_value(
        "CSV_FILE", "CSV_FILE", "stocks.csv")
    src_df = pd.read_csv(csv_file, sep="\t", encoding="utf-8-sig", dtype=str)
    if len(src_df.columns) == 1:
        src_df = pd.read_csv(csv_file, encoding="utf-8-sig", dtype=str)
    src_df.columns = src_df.columns.str.strip()
    src_df = src_df.rename(columns={"Ticker": "stock_id", "Name": "name"})
    if "stock_id" not in src_df.columns:
        raise ValueError(f"{csv_file} missing Ticker or stock_id column")
    if "name" not in src_df.columns:
        src_df["name"] = ""
    src_df["stock_id"] = src_df["stock_id"].astype(str).str.strip()
    src_df["name"] = src_df["name"].fillna("").astype(str).str.strip()
    src_df = src_df[src_df["stock_id"] != ""]
    return src_df[["stock_id", "name"]].to_dict(orient="records")


def build_row(stock: dict, usage_info: dict | None = None):
    stock_id = str(stock.get("stock_id") or "").strip()
    row = {col: None for col in ORDERED_COLS}
    row["stock_id"] = stock_id
    row["name"] = stock.get("name") or ""
    row["valuation_updated_at"] = now_utc_str()

    try:
        valuation = get_per_pbr_60d_stats(stock_id) or {}
        row["per_latest"] = valuation.get("per")
        row["per_60d_high"] = valuation.get("per_60d_high")
        row["per_60d_low"] = valuation.get("per_60d_low")
        row["pbr_latest"] = valuation.get("pbr")
        row["pbr_60d_high"] = valuation.get("pbr_60d_high")
        row["pbr_60d_low"] = valuation.get("pbr_60d_low")
        row["per_latest_is_prev"] = "True" if valuation.get(
            "per_is_prev") else "False"
        row["pbr_latest_is_prev"] = "True" if valuation.get(
            "pbr_is_prev") else "False"
        yield_raw = get_dividend_yield(stock_id)
        if isinstance(yield_raw, dict):
            row["yield_value"] = yield_raw.get("yield")
        elif isinstance(yield_raw, (int, float)):
            row["yield_value"] = float(yield_raw)

        if row["per_latest"] is None and row["pbr_latest"] is None and row["yield_value"] is None:
            row["valuation_status"] = "no_data"
            row["valuation_reason"] = "empty"
        else:
            row["valuation_status"] = "ok"
            row["valuation_reason"] = ""
    except Exception as exc:
        row["valuation_status"] = "error"
        row["valuation_reason"] = str(exc)[:180]

    usage_info = usage_info or get_finmind_token_status()
    row["finmind_token_status"] = usage_info.get("login_status") or (
        "ok" if usage_info.get("token_present") else "missing_token")
    row["finmind_token_source"] = usage_info.get("token_source") or ""
    row["finmind_token_masked"] = usage_info.get("token_masked") or ""
    row["finmind_user_count"] = usage_info.get("user_count")
    row["finmind_api_request_limit"] = usage_info.get("api_request_limit")
    row["finmind_remain"] = usage_info.get("remain")
    row["finmind_usage_checked_at"] = now_utc_str()
    return row


def build_daily_valuation(stock_list, output_file):
    info = get_finmind_user_info(
        write_log=True, source="generate_static_valuation_csv")
    used = int(info.get("user_count") or 0)
    limit = int(info.get("api_request_limit") or 0)
    remain = info.get("remain")
    remain = int(remain or 0) if remain is not None else 0

    print(
        f"FinMind token: token_present={info.get('token_present')}, source={info.get('token_source')}, token={info.get('token_masked')}, login={info.get('login_status')}",
        flush=True,
    )
    print(f"FinMind usage: {used}/{limit}, remain={remain}", flush=True)

    log_finmind_static_event(
        "generate_static_valuation_start",
        source="generate_static_valuation_csv",
        status=info.get("login_status"),
        message=f"output={output_file}, stocks={len(stock_list)}",
    )

    rows = []
    for idx, stock in enumerate(stock_list, 1):
        stock_id = str(stock.get("stock_id") or "").strip()
        print(
            f"Processing valuation {idx}/{len(stock_list)}: {stock_id} {stock.get('name') or ''}", flush=True)
        rows.append(build_row(stock, usage_info=info))

    final_df = normalize_df(pd.DataFrame(rows))
    atomic_write_csv(final_df, output_file)

    status_counts = final_df["valuation_status"].astype(
        str).str.lower().value_counts().to_dict() if not final_df.empty else {}
    log_finmind_static_event(
        "generate_static_valuation_end",
        source="generate_static_valuation_csv",
        status="completed",
        message=f"rows={len(final_df)}, output={output_file}",
    )
    print(
        f"AllStatic valuation rebuild: {status_counts}, total={len(final_df)}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Daily full refresh for AllStatic valuation fields (PER/PBR).")
    parser.add_argument("--output", default=resolve_config_value(
        "STATIC_VALUATION_OUTPUT_FILE", "STATIC_VALUATION_OUTPUT_FILE", "AllStatic_Valuation.csv"))
    args = parser.parse_args()

    stock_list = load_stock_list()
    build_daily_valuation(stock_list, args.output)


if __name__ == "__main__":
    main()
