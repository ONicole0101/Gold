from datetime import datetime, timedelta
import argparse
import os
import time

import pandas as pd
import requests
import config

from data_sources import (
    get_revenue_raw,
    get_per_pbr_60d_stats,
    get_disposition_securities_period,
    get_finmind_user_info,
    get_finmind_token_status,
    log_finmind_static_event,
)
from financial_analysis import (
    get_eps_analysis,
    get_roe_analysis,
    get_profit_ratio,
    extract_metric,
    extract_metric_is_prev,
)

DATA_COLS = [
    "eps_Y", "eps_ttm",
    "roe_last_year", "roe_ttm",
    "rev", "rev_mom", "rev_qoq", "rev_yoy",
    "gross_margin", "gross_margin_qoq", "gross_margin_yoy_diff",
    "operating_margin", "operating_margin_qoq", "operating_margin_yoy_diff",
    "net_margin", "net_margin_qoq", "net_margin_yoy_diff",
    "per_latest", "per_60d_high", "per_60d_low",
    "pbr_latest", "pbr_60d_high", "pbr_60d_low",
]

DISPOSITION_COLS = [
    "period_start", "period_end",
]

GROUPS = {
    # Required fields for status. Optional derived fields such as per_Y/per_ttm,
    # QoQ/YoY and 60D high/low should not make a row look empty.
    "eps": ["eps_Y", "eps_ttm"],
    "roe": ["roe_last_year", "roe_ttm"],
    "revenue": ["rev"],
    "profit": ["gross_margin", "operating_margin", "net_margin"],
    "valuation": ["per_latest", "pbr_latest"],
}

PREV_FLAG_COLS = [
    "eps_Y_is_prev", "eps_ttm_is_prev",
    "gross_margin_is_prev", "operating_margin_is_prev", "net_margin_is_prev",
    "per_latest_is_prev", "pbr_latest_is_prev",
]

BASE_COLS = ["stock_id", "name"] + DATA_COLS + DISPOSITION_COLS + PREV_FLAG_COLS + [
    "static_updated_at", "static_status", "static_reason",
]

SOURCE_META_COLS = []
for g in GROUPS:
    SOURCE_META_COLS += [f"{g}_status", f"{g}_reason"]

FINMIND_META_COLS = [
    "finmind_token_status",
    "finmind_token_source",
    "finmind_token_masked",
    "finmind_user_count",
    "finmind_api_request_limit",
    "finmind_remain",
    "finmind_usage_checked_at",
]

ORDERED_COLS = BASE_COLS + SOURCE_META_COLS + FINMIND_META_COLS

_LAST_FINMIND_USAGE_INFO = None

TERMINAL_STATUSES = {"ok", "partial_ok"}
SOURCE_TERMINAL_STATUSES = {"ok"}
DEFAULT_REFRESH_DAYS = 7


def now_utc_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def is_blank_value(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "null"}


def is_finmind_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()

    limit_keywords = [
        "requests reach the upper limit",
        "requests reach the upper limit.",
        "reach the upper limit",
        "upper limit",
        "api_request_limit",
        "429",
    ]
    return any(keyword in msg for keyword in limit_keywords)


def compact_text(text: str, max_len: int = 120) -> str:
    text = " ".join(str(text or "").replace("\n", " ").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def compact_missing_summary(missing: list[str], max_items: int = 8) -> str:
    """Return a short static_reason summary for missing legacy data columns."""
    missing_set = set(missing or [])
    parts = []

    for group, cols in GROUPS.items():
        group_missing = [c for c in cols if c in missing_set]
        if not group_missing:
            continue
        if len(group_missing) == len(cols):
            parts.append(group)
        else:
            parts.extend(group_missing)

    if not parts:
        parts = list(missing or [])

    shown = parts[:max_items]
    suffix = f",+{len(parts) - max_items}" if len(parts) > max_items else ""
    return ",".join(shown) + suffix


def compact_group_reason(group: str, status: str, reason: str = "") -> str:
    status = str(status or "").strip().lower()
    reason = compact_text(reason, 80)

    if status in {"api_limited", "limited"}:
        return f"{group}:limited"
    if status == "error":
        return f"{group}:error" + (f"({reason})" if reason else "")
    if status == "incomplete":
        return f"{group}:incomplete" + (f"({reason})" if reason else "")
    if status == "pending":
        return f"{group}:pending"
    if status == "no_data":
        return group
    return f"{group}:{status or 'pending'}"


def parse_static_updated_at(value):
    if is_blank_value(value):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", utc=False)
        if pd.isna(ts):
            return None
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if getattr(ts, "tzinfo", None) is not None:
            ts = ts.replace(tzinfo=None)
        return ts
    except Exception:
        return None


def is_stale_ok_row(row: dict, refresh_hours: int) -> bool:
    """Return True when an OK row is older than refresh_hours and should be refreshed."""
    if refresh_hours is None or refresh_hours <= 0:
        return False

    status = str(row.get("static_status", "")).strip().lower()
    if status != "ok":
        return False

    updated_at = parse_static_updated_at(row.get("static_updated_at"))
    if updated_at is None:
        return True

    return datetime.utcnow() - updated_at > timedelta(hours=refresh_hours)


def all_blank(row: dict, cols: list[str]) -> bool:
    return all(is_blank_value(row.get(c)) for c in cols)


def any_blank(row: dict, cols: list[str]) -> bool:
    return any(is_blank_value(row.get(c)) for c in cols)


def _normalize_finmind_usage_info(info: dict | None) -> dict:
    info = info or {}
    return {
        "finmind_token_status": info.get("login_status") or ("ok" if info.get("token_present") else "missing_token"),
        "finmind_token_source": info.get("token_source") or "",
        "finmind_token_masked": info.get("token_masked") or "",
        "finmind_user_count": info.get("user_count"),
        "finmind_api_request_limit": info.get("api_request_limit"),
        "finmind_remain": info.get("remain"),
        "finmind_usage_checked_at": now_utc_str(),
    }


def apply_finmind_usage_to_row(row: dict, info: dict | None = None) -> dict:
    """Attach latest FinMind token/quota evidence to one AllStatic row."""
    global _LAST_FINMIND_USAGE_INFO
    if info is None:
        info = _LAST_FINMIND_USAGE_INFO or get_finmind_token_status()
    row.update(_normalize_finmind_usage_info(info))
    return row


def get_finmind_usage():
    """
    Check FinMind user_info with the same token configured in data_sources.py.

    This confirms:
    1. FINMIND_TOKEN was loaded.
    2. DataLoader.login_by_token() was attempted.
    3. FinMind user_info accepts the token and returns usage/quota.
    """
    global _LAST_FINMIND_USAGE_INFO

    info = get_finmind_user_info(write_log=True, source="generate_static_csv")
    _LAST_FINMIND_USAGE_INFO = info

    used = int(info.get("user_count") or 0)
    limit = int(info.get("api_request_limit") or 0)
    remain = info.get("remain")
    remain = int(remain or 0) if remain is not None else 0

    token_msg = (
        f"token_present={info.get('token_present')}, "
        f"source={info.get('token_source')}, "
        f"token={info.get('token_masked')}, "
        f"login={info.get('login_status')}"
    )
    print(f"FinMind token: {token_msg}", flush=True)
    print(f"FinMind usage: {used}/{limit}, remain={remain}", flush=True)

    if not info.get("ok"):
        print(
            f"⚠️ FinMind token/user_info check failed: {info.get('message')}", flush=True)

    return used, limit, remain


def set_group_status(row: dict, group: str, status: str, reason: str = ""):
    row[f"{group}_status"] = status
    row[f"{group}_reason"] = compact_text(reason or "", 160)


def empty_static_row(s: dict) -> dict:
    row = {c: None for c in ORDERED_COLS}
    row["stock_id"] = str(s["stock_id"]).strip()
    row["name"] = s.get("name")
    row["static_updated_at"] = now_utc_str()
    row["static_status"] = "incomplete"
    row["static_reason"] = "not processed yet"
    for c in PREV_FLAG_COLS:
        row[c] = "False"
    for g in GROUPS:
        set_group_status(row, g, "pending", "not processed yet")
    apply_finmind_usage_to_row(row)
    return row


def get_revenue_trend(stock_id):
    data = get_revenue_raw(stock_id)
    if not data:
        return None, "empty"

    df = pd.DataFrame(data)
    if "revenue" not in df.columns:
        if "value" in df.columns:
            df["revenue"] = df["value"]
        else:
            return None, "missing revenue/value col"

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df = df.sort_values("date").dropna(subset=["date", "revenue"])

    if df.empty:
        return None, "empty after clean"

    curr = df.iloc[-1]["revenue"]

    def pct_by_offset(offset: int):
        if len(df) <= offset:
            return None
        prev = df.iloc[-1 - offset]["revenue"]
        if prev is None or pd.isna(prev) or prev == 0:
            return None
        return round((curr - prev) / prev * 100, 2)

    return {
        "rev": round((curr / 1e9), 2),  # 改為bn顯示
        "mom": pct_by_offset(1),
        "qoq": pct_by_offset(3),
        "yoy": pct_by_offset(12),
    }, ""


def finalize_static_status(row: dict) -> dict:
    row = apply_finmind_usage_to_row(row)
    problems = []
    no_data_groups = []

    for g, cols in GROUPS.items():
        g_status = str(row.get(f"{g}_status", "")).strip().lower()
        g_reason = str(row.get(f"{g}_reason", "")).strip()

        if g_status == "ok":
            missing = [c for c in cols if is_blank_value(row.get(c))]
            if missing:
                short_missing = compact_missing_summary(missing)
                set_group_status(row, g, "incomplete",
                                 "missing:" + short_missing)
                problems.append(f"{g}:missing")
        elif g_status == "no_data":
            no_data_groups.append(compact_group_reason(g, "no_data", g_reason))
        elif g_status in {"api_limited", "limited"}:
            problems.append(compact_group_reason(g, "api_limited", g_reason))
        elif g_status == "error":
            problems.append(compact_group_reason(g, "error", g_reason))
        elif g_status == "incomplete":
            problems.append(compact_group_reason(g, "incomplete", g_reason))
        else:
            problems.append(compact_group_reason(
                g, g_status or "pending", g_reason))

    if problems:
        if any("limited" in p for p in problems):
            row["static_status"] = "api_limited"
        elif any("error" in p for p in problems):
            row["static_status"] = "error"
        else:
            row["static_status"] = "incomplete"
        row["static_reason"] = compact_text(";".join(problems[:6]), 180)
    elif no_data_groups:
        row["static_status"] = "partial_ok"
        row["static_reason"] = compact_text(
            "no_data:" + ",".join(no_data_groups[:6]), 180)
    else:
        row["static_status"] = "ok"
        row["static_reason"] = ""

    return row


def build_static_row(s: dict) -> dict:
    stock_id = str(s["stock_id"]).strip()
    name = s.get("name")
    row = empty_static_row(s)
    row["static_updated_at"] = now_utc_str()

    # EPS and annual/TTM PER.
    try:
        eps_res = get_eps_analysis(stock_id, None)
        print("EPS =", stock_id, eps_res, flush=True)
        eps_res = tuple(eps_res) if isinstance(eps_res, tuple) else (None,) * 6
        eps_res = eps_res + (None,) * (6 - len(eps_res))
        eps_last, eps_ttm, per_last, per_ttm, eps_y_is_prev, eps_ttm_is_prev = eps_res[:6]
        row["eps_Y"] = eps_last
        row["eps_ttm"] = eps_ttm
        # per_Y/per_ttm removed from AllStatic.csv. Daily PER is sourced from TaiwanStockPER.
        row["eps_Y_is_prev"] = "True" if eps_y_is_prev else "False"
        row["eps_ttm_is_prev"] = "True" if eps_ttm_is_prev else "False"
        if all_blank(row, GROUPS["eps"]):
            set_group_status(row, "eps", "no_data",
                             "empty")
        elif any_blank(row, GROUPS["eps"]):
            set_group_status(row, "eps", "incomplete",
                             "partial")
        else:
            set_group_status(row, "eps", "ok", "")
    except Exception as e:
        if is_finmind_limit_error(e):
            set_group_status(row, "eps", "api_limited", str(e))
            return finalize_static_status(row)
        set_group_status(row, "eps", "error", str(e))

    # ROE: last complete year and trailing twelve months.
    try:
        roe_res = get_roe_analysis(stock_id) or {}
        print("ROE =", stock_id, roe_res, flush=True)
        row["roe_last_year"] = roe_res.get("roe_last_year")
        row["roe_ttm"] = roe_res.get("roe_ttm")
        if all_blank(row, GROUPS["roe"]):
            set_group_status(row, "roe", "no_data", "empty")
        elif any_blank(row, GROUPS["roe"]):
            set_group_status(row, "roe", "incomplete", "partial")
        else:
            set_group_status(row, "roe", "ok", "")
    except Exception as e:
        if is_finmind_limit_error(e):
            set_group_status(row, "roe", "api_limited", str(e))
            return finalize_static_status(row)
        set_group_status(row, "roe", "error", str(e))

    # Monthly revenue trend.
    try:
        rev, reason = get_revenue_trend(stock_id)
        rev = rev or {}
        row["rev"] = rev.get("rev")
        row["rev_mom"] = rev.get("mom")
        row["rev_qoq"] = rev.get("qoq")
        row["rev_yoy"] = rev.get("yoy")
        if rev:
            if any_blank(row, GROUPS["revenue"]):
                set_group_status(row, "revenue", "incomplete",
                                 "partial")
            else:
                set_group_status(row, "revenue", "ok", "")
        else:
            set_group_status(row, "revenue", "no_data",
                             reason or "empty")
    except Exception as e:
        if is_finmind_limit_error(e):
            set_group_status(row, "revenue", "api_limited", str(e))
            return finalize_static_status(row)
        set_group_status(row, "revenue", "error", str(e))

    # Profit ratios.
    try:
        profit_res = get_profit_ratio(stock_id)
        cur_g, qoq_g, yoy_g = extract_metric(profit_res, "gross")
        cur_o, qoq_o, yoy_o = extract_metric(profit_res, "op")
        cur_n, qoq_n, yoy_n = extract_metric(profit_res, "net")
        row["gross_margin"] = cur_g
        row["gross_margin_qoq"] = qoq_g
        row["gross_margin_yoy_diff"] = yoy_g
        row["operating_margin"] = cur_o
        row["operating_margin_qoq"] = qoq_o
        row["operating_margin_yoy_diff"] = yoy_o
        row["net_margin"] = cur_n
        row["net_margin_qoq"] = qoq_n
        row["net_margin_yoy_diff"] = yoy_n
        row["gross_margin_is_prev"] = "True" if extract_metric_is_prev(
            profit_res, "gross") else "False"
        row["operating_margin_is_prev"] = "True" if extract_metric_is_prev(
            profit_res, "op") else "False"
        row["net_margin_is_prev"] = "True" if extract_metric_is_prev(
            profit_res, "net") else "False"
        if all_blank(row, GROUPS["profit"]):
            set_group_status(row, "profit", "no_data",
                             "empty")
        elif any_blank(row, GROUPS["profit"]):
            set_group_status(row, "profit", "incomplete",
                             "partial")
        else:
            set_group_status(row, "profit", "ok", "")
    except Exception as e:
        if is_finmind_limit_error(e):
            set_group_status(row, "profit", "api_limited", str(e))
            return finalize_static_status(row)
        set_group_status(row, "profit", "error", str(e))

    # 60-day PER/PBR.
    try:
        per_pbr = get_per_pbr_60d_stats(stock_id) or {}
        row["per_latest"] = per_pbr.get("per")
        row["per_60d_high"] = per_pbr.get("per_60d_high")
        row["per_60d_low"] = per_pbr.get("per_60d_low")
        row["pbr_latest"] = per_pbr.get("pbr")
        row["pbr_60d_high"] = per_pbr.get("pbr_60d_high")
        row["pbr_60d_low"] = per_pbr.get("pbr_60d_low")
        row["per_latest_is_prev"] = "True" if per_pbr.get(
            "per_is_prev") else "False"
        row["pbr_latest_is_prev"] = "True" if per_pbr.get(
            "pbr_is_prev") else "False"
        if all_blank(row, GROUPS["valuation"]):
            set_group_status(row, "valuation", "no_data",
                             "empty")
        elif any_blank(row, GROUPS["valuation"]):
            set_group_status(row, "valuation", "incomplete",
                             "partial")
        else:
            set_group_status(row, "valuation", "ok", "")
    except Exception as e:
        if is_finmind_limit_error(e):
            set_group_status(row, "valuation", "api_limited", str(e))
            return finalize_static_status(row)
        set_group_status(row, "valuation", "error", str(e))

    # Disposition securities period. Optional: it does not affect static_status.
    # Only write period_start/period_end when today or tomorrow is inside the range.
    try:
        disposition = get_disposition_securities_period(stock_id) or {}
        row["period_start"] = disposition.get("period_start")
        row["period_end"] = disposition.get("period_end")
    except Exception as e:
        print(f"❌ disposition period static error {stock_id}: {e}", flush=True)

    return finalize_static_status(row)


def normalize_static_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=ORDERED_COLS)
    df = df.copy()
    df.columns = df.columns.str.strip()
    for c in ORDERED_COLS:
        if c not in df.columns:
            df[c] = None
    for c in PREV_FLAG_COLS:
        df[c] = df[c].apply(lambda v: "True" if str(
            v).strip().lower() == "true" else "False")
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df[ORDERED_COLS]


def read_existing_static(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame(columns=ORDERED_COLS)
    return normalize_static_df(pd.read_csv(path, encoding="utf-8-sig", dtype=str))


def atomic_write_csv(df: pd.DataFrame, path: str):
    tmp_path = path + ".tmp"
    df = normalize_static_df(df)
    df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
    os.replace(tmp_path, path)


def legacy_missing_data_cols(row: dict) -> list[str]:
    return [c for c in DATA_COLS if is_blank_value(row.get(c))]


def should_update(row, retry_errors: bool, retry_no_data: bool, force: bool, refresh_hours: int) -> bool:
    if force or row is None:
        return True
    if isinstance(row, pd.Series):
        row = row.to_dict()

    static_status = str(row.get("static_status", "")).strip().lower()

    # 24HR refresh: completed OK rows older than refresh_24HR are refreshed.
    # partial_ok/no_data remains terminal by default unless --retry-no-data is used.
    if is_stale_ok_row(row, refresh_hours):
        return True

    # Rows created by v3 have source statuses. Trust them more than field blankness.
    has_source_meta = any(not is_blank_value(
        row.get(f"{g}_status")) for g in GROUPS)
    if has_source_meta:
        source_statuses = [
            str(row.get(f"{g}_status", "")).strip().lower() for g in GROUPS]
        if all(s in SOURCE_TERMINAL_STATUSES for s in source_statuses):
            # ok: all data present. partial_ok: some source confirmed no_data. Both are terminal by default.
            if static_status == "partial_ok" and retry_no_data:
                return True
            return False
        if any(s == "no_data" for s in source_statuses) and retry_no_data:
            return True
        if static_status == "error" and not retry_errors:
            return False
        return True

    # Legacy rows do not know whether blanks are true no_data, so blanks must be rechecked once.
    if static_status == "error" and not retry_errors:
        return False
    if static_status in TERMINAL_STATUSES and not legacy_missing_data_cols(row):
        return False
    return True


def repair_legacy_status_only(df: pd.DataFrame) -> pd.DataFrame:
    repaired = []
    for _, r in df.iterrows():
        row = r.to_dict()
        has_source_meta = any(not is_blank_value(
            row.get(f"{g}_status")) for g in GROUPS)
        if has_source_meta:
            row = finalize_static_status(row)
        else:
            missing = legacy_missing_data_cols(row)
            if missing:
                row["static_status"] = "incomplete"
                row["static_reason"] = "missing:" + \
                    compact_missing_summary(missing)
            else:
                row["static_status"] = "ok"
                row["static_reason"] = ""
        repaired.append(row)
    return normalize_static_df(pd.DataFrame(repaired))


def build_incremental(stock_list, output_file, max_rows=None, min_remain=None, retry_errors=False, retry_no_data=False, force=False, sleep_sec=0.2, repair_only=False, check_every=10, refresh_hours=0):
    """
    Full rebuild AllStatic.csv on every run.

    Project rule:
    - Do not use incremental catch-up/cache logic.
    - Do not stop by FinMind token quota/remain.
    - Do not keep old rows from the existing AllStatic.csv.
    - per_Y/per_ttm are removed from output columns.
    """
    token_status = get_finmind_token_status()
    log_finmind_static_event(
        "generate_static_start",
        source="generate_static_csv",
        status=token_status.get("login_status"),
        message=f"full_rebuild=1, output={output_file}, token={token_status.get('token_masked')}",
    )

    if repair_only:
        print("repair_only is ignored: full rebuild mode always refreshes all rows.", flush=True)

    print("Full rebuild mode: existing AllStatic.csv will not be reused.", flush=True)
    print(f"Total source stocks: {len(stock_list)}", flush=True)

    # Token usage is logged for evidence only. It is no longer used as a stop condition.
    try:
        get_finmind_usage()
    except Exception as e:
        print(f"Cannot check FinMind usage, continue full rebuild: {e}", flush=True)

    rows = []
    processed = 0

    for i, s in enumerate(stock_list, 1):
        sid = str(s.get("stock_id", "")).strip()
        print(f"Processing {i}/{len(stock_list)}: {sid} {s.get('name')}", flush=True)

        row = build_static_row(s)
        apply_finmind_usage_to_row(row)
        rows.append(row)
        processed += 1

        if sleep_sec and sleep_sec > 0:
            time.sleep(sleep_sec)

    final_df = normalize_static_df(pd.DataFrame(rows))
    atomic_write_csv(final_df, output_file)

    status_counts = final_df["static_status"].astype(str).str.lower().value_counts().to_dict() if not final_df.empty else {}

    log_finmind_static_event(
        "generate_static_end",
        source="generate_static_csv",
        status="completed",
        message=f"full_rebuild=1, updated={processed}, output={output_file}",
    )

    print("Run stopped: completed", flush=True)
    print(f"Updated this run: {processed}", flush=True)
    print(f"AllStatic full rebuild: {status_counts}, total={len(final_df)}", flush=True)

def resolve_config_value(env_name, config_name, default=None):
    value = os.getenv(env_name)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    value = getattr(config, config_name, default)
    if value is None:
        return default
    return str(value).strip()


def load_stock_list(csv_file=None):
    csv_file = csv_file or resolve_config_value("CSV_FILE", "CSV_FILE", "stocks.csv")
    src_df = pd.read_csv(csv_file, sep="\t", encoding="utf-8-sig", dtype=str)
    src_df.columns = src_df.columns.str.strip()
    src_df = src_df.rename(columns={"Ticker": "stock_id", "Name": "name"})
    src_df["stock_id"] = src_df["stock_id"].astype(str).str.strip()
    return src_df.to_dict(orient="records")


def main():
    parser = argparse.ArgumentParser(
        description="Full rebuild AllStatic.csv every run.")
    parser.add_argument("--output", default=resolve_config_value("STATIC_OUTPUT_FILE", "STATIC_OUTPUT_FILE", "AllStatic.csv"))
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Ignored in full rebuild mode; kept for workflow compatibility.")
    parser.add_argument("--min-remain", type=int, default=0,
                        help="Ignored in full rebuild mode; API quota no longer stops the run.")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Ignored in full rebuild mode; all rows are refreshed.")
    parser.add_argument("--retry-no-data", action="store_true",
                        help="Ignored in full rebuild mode; all rows are refreshed.")
    parser.add_argument("--force", action="store_true",
                        help="Ignored in full rebuild mode; all rows are refreshed.")
    parser.add_argument("--repair-only", action="store_true",
                        help="Ignored in full rebuild mode; APIs are called for all rows.")
    parser.add_argument("--sleep-sec", type=float,
                        default=0.2, help="Sleep between stocks.")
    parser.add_argument("--check-every", type=int, default=10,
                        help="Check FinMind usage before first stock and every N processed stocks. Use 1 for every stock.")
    parser.add_argument("--refresh-hours", type=int, default=0,
                        help="Ignored in full rebuild mode; all rows are refreshed.")
    args = parser.parse_args()

    try:
        stock_list = load_stock_list()
    except Exception as e:
        print(f"Failed to read source CSV/config: {e}", flush=True)
        return

    build_incremental(
        stock_list=stock_list,
        output_file=args.output,
        max_rows=args.max_rows,
        min_remain=args.min_remain,
        retry_errors=args.retry_errors,
        retry_no_data=args.retry_no_data,
        force=args.force,
        sleep_sec=args.sleep_sec,
        repair_only=args.repair_only,
        check_every=max(args.check_every, 1),
        refresh_hours=args.refresh_hours,
    )


if __name__ == "__main__":
    main()
