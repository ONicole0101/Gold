import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import requests
from loguru import logger
from finmind_auth import (
    get_finmind_auth_headers,
    get_finmind_token_status as _shared_get_finmind_token_status,
    get_finmind_user_info as _shared_get_finmind_user_info,
    mask_token,
    resolve_finmind_token,
)

# Standardize on FINMIND_TOKEN only (runtime-resolved, no import-time cache).
FINMIND_token = ""
FINMIND_TOKEN_SOURCE = ""
headers = {}
API_URL = 'https://api.finmindtrade.com/api/v4/data'
USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"
FINMIND_USAGE_LOG_FILE = os.getenv(
    "FINMIND_USAGE_LOG_FILE", "finmind_token_usage_log.csv")

FINMIND_TOKEN_LOGIN_STATUS = "not_checked"
FINMIND_TOKEN_LOGIN_MESSAGE = "login not checked in this process yet"

FINMIND_API_CALL_COUNT = 0
FINMIND_DATASET_CALL_COUNTS = {}

# 停用所有來自 FinMind 的 Log 訊息
logger.remove()
logging.getLogger('FinMind').setLevel(logging.WARNING)


def _mask_token(token=None):
    """Return a safe token display value, never the full token."""
    token = token if token is not None else FINMIND_token
    return mask_token(token)


def _refresh_finmind_runtime_auth():
    """Resolve FINMIND_TOKEN and auth headers at runtime for each request path."""
    global FINMIND_token, FINMIND_TOKEN_SOURCE, headers
    FINMIND_token = resolve_finmind_token()
    FINMIND_TOKEN_SOURCE = "FINMIND_TOKEN" if FINMIND_token else ""
    headers = get_finmind_auth_headers(FINMIND_token)
    return FINMIND_token, headers


def _append_finmind_usage_event(
    event,
    source="",
    stock_id="",
    dataset="",
    status="",
    status_code="",
    user_count=None,
    api_request_limit=None,
    remain=None,
    message="",
):
    """Append token/login/quota/API usage evidence to a CSV audit log."""
    row = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "source": source,
        "stock_id": stock_id,
        "dataset": dataset,
        "token_present": bool(FINMIND_token),
        "token_source": FINMIND_TOKEN_SOURCE,
        "token_masked": _mask_token(),
        "login_status": FINMIND_TOKEN_LOGIN_STATUS,
        "login_message": FINMIND_TOKEN_LOGIN_MESSAGE,
        "request_count": FINMIND_API_CALL_COUNT,
        "dataset_request_count": FINMIND_DATASET_CALL_COUNTS.get(dataset, 0),
        "user_count": user_count,
        "api_request_limit": api_request_limit,
        "remain": remain,
        "status": status,
        "status_code": status_code,
        "message": str(message or "")[:300],
    }

    try:
        log_path = FINMIND_USAGE_LOG_FILE
        exists = os.path.exists(log_path)
        pd.DataFrame([row]).to_csv(
            log_path,
            mode="a",
            header=not exists,
            index=False,
            encoding="utf-8-sig",
        )
    except Exception as e:
        print(f"⚠️ cannot write FinMind usage log: {e}", flush=True)


def log_finmind_static_event(event, message="", **kwargs):
    """Public helper for generate_static_csv.py to write the same audit log."""
    _append_finmind_usage_event(event=event, message=message, **kwargs)


def _record_finmind_request(source, stock_id="", dataset=""):
    """Count and log each FinMind API/DataLoader call in one place."""
    _refresh_finmind_runtime_auth()

    global FINMIND_API_CALL_COUNT
    FINMIND_API_CALL_COUNT += 1
    FINMIND_DATASET_CALL_COUNTS[dataset] = FINMIND_DATASET_CALL_COUNTS.get(
        dataset, 0) + 1

    _append_finmind_usage_event(
        event="api_call",
        source=source,
        stock_id=stock_id,
        dataset=dataset,
        status="sent",
        message="FinMind request sent with token in query params and/or Authorization header",
    )


def _warn_missing_finmind_token(dataset_name, stock_id=""):
    """Clear warning when a dataset cannot be fetched because the runtime token is missing."""
    if FINMIND_token:
        return False
    logger.warning(
        "FinMind dataset {} skipped for stock_id={} because FINMIND_TOKEN is not set. "
        "Set the environment variable and rerun the report.",
        dataset_name,
        stock_id,
    )
    return True


def get_finmind_token_status():
    """Return runtime token/login evidence, always using fresh runtime secret."""
    global FINMIND_TOKEN_LOGIN_STATUS, FINMIND_TOKEN_LOGIN_MESSAGE
    _refresh_finmind_runtime_auth()
    status = _shared_get_finmind_token_status()
    FINMIND_TOKEN_LOGIN_STATUS = status.get("login_status") or "error"
    FINMIND_TOKEN_LOGIN_MESSAGE = status.get("login_message") or ""
    status["request_count"] = FINMIND_API_CALL_COUNT
    status["dataset_call_counts"] = dict(FINMIND_DATASET_CALL_COUNTS)
    status["usage_log_file"] = FINMIND_USAGE_LOG_FILE
    return status


def get_finmind_user_info(write_log=True, source="user_info"):
    """
    Validate the token against FinMind user_info and return usage information.

    This is the strongest runtime check that the token is accepted by FinMind:
    - token_present/token_source proves the environment variable was loaded
    - HTTP status/body proves FinMind accepted or rejected it
    - user_count/api_request_limit/remain shows actual account quota usage
    """
    global FINMIND_TOKEN_LOGIN_STATUS, FINMIND_TOKEN_LOGIN_MESSAGE
    _refresh_finmind_runtime_auth()

    info = _shared_get_finmind_user_info(write_log=False, source=source)
    FINMIND_TOKEN_LOGIN_STATUS = info.get("login_status") or "error"
    FINMIND_TOKEN_LOGIN_MESSAGE = info.get("login_message") or ""

    if write_log:
        _append_finmind_usage_event(
            event="token_check",
            source=source,
            status="ok" if info.get("ok") else (
                info.get("login_status") or "error"),
            status_code=info.get("status_code"),
            user_count=info.get("user_count"),
            api_request_limit=info.get("api_request_limit"),
            remain=info.get("remain"),
            message=info.get("message") or "",
        )
    return info


def _safe_response_json(res):
    """避免 API 回傳非 JSON 時，印錯誤又讓程式中斷。"""
    try:
        return res.json()
    except Exception:
        return {}


def _print_api_status_error(source, stock_id, res, data=None):
    """非 200/異常 API 狀態時，統一印出 status code 與訊息。"""
    if data is None:
        data = _safe_response_json(res)

    msg = data.get("msg") or data.get(
        "message") or data.get("status") or res.text[:200]
    print(
        f"❌ {source} API error {stock_id}: "
        f"status_code={res.status_code}, msg={msg}"
    )


def get_stock_data(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockPrice',
            'data_id': str(stock_id),
            'start_date': '2023-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request("get_stock_data", stock_id, "TaiwanStockPrice")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code == 402:
            _print_api_status_error('get_stock_data', stock_id, res, data)
            raise RuntimeError(
                f"FinMind quota exceeded for {stock_id}: {data.get('msg')}")

        if res.status_code != 200:
            _print_api_status_error('get_stock_data', stock_id, res, data)
            return pd.DataFrame()

        if 'data' not in data or len(data['data']) == 0:
            print(
                f"⚠️ get_stock_data empty {stock_id}: status={res.status_code}, msg={data.get('msg')}")
            return pd.DataFrame()

        df = pd.DataFrame(data['data'])

        volume_col = None
        for c in ['Trading_Volume', 'trading_volume', 'Trading_Volume_1000']:
            if c in df.columns:
                volume_col = c
                break

        required_cols = ['date', 'open', 'close', 'max', 'min']
        if volume_col:
            required_cols.append(volume_col)

        df = df[required_cols].copy()
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'close', 'max', 'min']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        if volume_col:
            df['volume'] = pd.to_numeric(df[volume_col], errors='coerce')
            if df['volume'].max() > 100000:
                df['volume'] = df['volume'] / 1000
        else:
            df['volume'] = None

        df = df.dropna(subset=['open', 'close', 'max', 'min'])
        valid_prices = df[['open', 'close', 'max', 'min']].gt(0).all(axis=1)
        valid_prices &= df['max'] >= df['min']
        valid_prices &= df['close'].between(df['min'], df['max'])
        valid_prices &= df['open'].between(df['min'], df['max'])
        df = df.loc[valid_prices].sort_values('date')

        return df
    except RuntimeError:
        raise
    except Exception as e:
        print(f'❌ get_stock_data error {stock_id}: {e}')
        return pd.DataFrame()


def get_latest_convertible_bond_overview(
    lookback_days=14, trend_days=3, monthly_points=3
):
    """Fetch latest, T/T-1/T-2 and M/M-1/M-2 all-market CB snapshots.

    Daily snapshots drive conversion-incentive trends.  Monthly snapshots use
    the latest available business day of each month because FinMind's
    ``OutstandingAmount`` is an end-of-prior-month balance and should not be
    presented as a genuinely changing daily value.
    """
    dataset = "TaiwanStockConvertibleBondDailyOverview"
    today = (datetime.utcnow() + timedelta(hours=8)).date()
    last_reason = ""
    cache = {}

    def fetch_date(query_date):
        nonlocal last_reason
        date_text = query_date.isoformat()
        if date_text in cache:
            return cache[date_text]
        if query_date.weekday() >= 5:
            cache[date_text] = {"status": "no_data",
                                "date": date_text, "rows": []}
            return cache[date_text]
        try:
            params = {
                "dataset": dataset,
                "start_date": date_text,
                "token": FINMIND_token,
            }
            _record_finmind_request("CB daily overview", "", dataset)
            res = requests.get(
                API_URL, params=params, headers=headers, timeout=300)
            payload = _safe_response_json(res)
            if res.status_code == 402:
                last_reason = str(payload.get(
                    "msg") or "FinMind quota exceeded")
                result = {
                    "status": "limited", "date": date_text,
                    "reason": last_reason, "rows": [],
                }
                cache[date_text] = result
                return result
            if res.status_code != 200:
                last_reason = str(
                    payload.get("msg") or payload.get("message") or
                    f"HTTP {res.status_code}"
                )
                result = {
                    "status": "error", "date": date_text,
                    "reason": last_reason, "rows": [],
                }
                cache[date_text] = result
                return result
            rows = payload.get("data") or []
            if not rows:
                last_reason = str(payload.get("msg") or f"{date_text} no data")
                result = {"status": "no_data", "date": date_text, "rows": []}
                cache[date_text] = result
                return result
            newest = max(
                (str(row.get("date") or "") for row in rows),
                default=date_text,
            )
            latest_rows = [
                row for row in rows
                if str(row.get("date") or newest) == newest
            ]
            result = {"status": "ok", "date": newest, "rows": latest_rows}
            cache[date_text] = result
            return result
        except Exception as exc:
            last_reason = str(exc)
            result = {
                "status": "error", "date": date_text,
                "reason": last_reason, "rows": [],
            }
            cache[date_text] = result
            return result

    daily_snapshots = []
    seen_dates = set()
    for offset in range(max(int(lookback_days or 1), 1)):
        result = fetch_date(today - timedelta(days=offset))
        if result.get("status") == "limited" and not daily_snapshots:
            return {
                "status": "limited", "as_of": "", "reason": last_reason,
                "rows": [], "daily_snapshots": [], "monthly_snapshots": [],
            }
        if result.get("status") != "ok" or result.get("date") in seen_dates:
            continue
        daily_snapshots.append(result)
        seen_dates.add(result.get("date"))
        if len(daily_snapshots) >= max(int(trend_days or 1), 1):
            break

    if not daily_snapshots:
        return {
            "status": "no_data", "as_of": "",
            "reason": last_reason or "No CB overview found in lookback window",
            "rows": [], "daily_snapshots": [], "monthly_snapshots": [],
        }

    monthly_snapshots = [daily_snapshots[0]]
    cursor = daily_snapshots[0]["date"]
    cursor = datetime.strptime(cursor, "%Y-%m-%d").date()
    # Search backwards from each prior calendar month-end.  Seven calendar
    # days covers weekends and ordinary exchange holidays without daily scans.
    while len(monthly_snapshots) < max(int(monthly_points or 1), 1):
        first_this_month = cursor.replace(day=1)
        month_end = first_this_month - timedelta(days=1)
        found = None
        for offset in range(7):
            result = fetch_date(month_end - timedelta(days=offset))
            if result.get("status") == "ok":
                found = result
                break
            if result.get("status") == "limited":
                break
        if not found:
            break
        monthly_snapshots.append(found)
        cursor = datetime.strptime(found["date"], "%Y-%m-%d").date()

    complete = (
        len(daily_snapshots) >= max(int(trend_days or 1), 1)
        and len(monthly_snapshots) >= max(int(monthly_points or 1), 1)
    )
    return {
        "status": "ok" if complete else "partial_ok",
        "as_of": daily_snapshots[0]["date"],
        "reason": "" if complete else (last_reason or "CB history is incomplete"),
        "rows": daily_snapshots[0]["rows"],
        "daily_snapshots": daily_snapshots,
        "monthly_snapshots": monthly_snapshots,
    }


def get_revenue_raw(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockMonthRevenue',  # 🔥 月營收
            'data_id': stock_id,
            'start_date': '2022-01-01',
            'token': FINMIND_token,
        }

        _record_finmind_request(
            "revenue source", stock_id, "TaiwanStockMonthRevenue")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        res_data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('revenue source', stock_id, res, res_data)
            return []

        data = res_data.get('data', [])
        return data

    except Exception as e:
        print(f'❌ revenue source error {stock_id}: {e}')
        return []


def get_profit_ratio(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockFinancialStatements',
            'data_id': stock_id,
            'start_date': '2020-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request("profit source", stock_id,
                                "TaiwanStockFinancialStatements")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('profit source', stock_id, res, data)
            return pd.DataFrame()

        return pd.DataFrame(data.get('data', []))
    except Exception as e:
        print(f'❌ profit source error {stock_id}: {e}')
        return pd.DataFrame()


def get_balance_sheet_raw(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockBalanceSheet',
            'data_id': stock_id,
            'start_date': '2020-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request("balance sheet source",
                                stock_id, "TaiwanStockBalanceSheet")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error(
                'balance sheet source', stock_id, res, data)
            return pd.DataFrame()

        return pd.DataFrame(data.get('data', []))
    except Exception as e:
        print(f'❌ balance sheet source error {stock_id}: {e}')
        return pd.DataFrame()


def get_eps_raw(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockFinancialStatements',
            'data_id': stock_id,
            'start_date': '2020-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request("EPS source", stock_id,
                                "TaiwanStockFinancialStatements")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('EPS source', stock_id, res, data)
            return []

        return data.get('data', [])
    except Exception as e:
        print(f'❌ EPS source error {stock_id}: {e}')
        return []


def get_dividend_raw(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockDividend',
            'data_id': stock_id,
            'start_date': '2020-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request(
            "dividend source", stock_id, "TaiwanStockDividend")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('dividend source', stock_id, res, data)
            return []
        return data.get('data', [])
    except Exception as e:
        print(f'❌ dividend source error {stock_id}: {e}')
        return []


def get_per_raw(stock_id):
    try:
        params = {
            'dataset': 'TaiwanStockPER',
            'data_id': stock_id,
            'start_date': '2023-01-01',
            'token': FINMIND_token,
        }
        _record_finmind_request("PER source", stock_id, "TaiwanStockPER")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('PER source', stock_id, res, data)
            return []

        return data.get('data', [])
    except Exception as e:
        print(f'❌ PER source error {stock_id}: {e}')
        return []


def get_per_pbr_60d_stats(stock_id, days=90):
    """
    Latest valid PER/PBR plus rolling high/low.
    If the newest FinMind row has blank PER/PBR, walk backward to the newest valid value.
    """
    def safe_round(x):
        try:
            if pd.isna(x):
                return None
            return round(float(x), 2)
        except Exception:
            return None

    empty = {
        "per": None, "per_60d_high": None, "per_60d_low": None, "per_is_prev": False,
        "pbr": None, "pbr_60d_high": None, "pbr_60d_low": None, "pbr_is_prev": False,
    }

    try:
        params = {
            "dataset": "TaiwanStockPER",
            "data_id": stock_id,
            "start_date": (datetime.today() - timedelta(days=max(days * 3, 240))).strftime("%Y-%m-%d"),
            "token": FINMIND_token,
        }
        _record_finmind_request("PER/PBR 90D", stock_id, "TaiwanStockPER")
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        res_data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error('PER/PBR 90D', stock_id, res, res_data)
            return empty

        data = res_data.get("data", [])
        if not data:
            return empty

        df = pd.DataFrame(data)
        if "date" not in df.columns:
            return empty
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date")
        if df.empty:
            return empty

        latest_row_date = df["date"].max()
        # Use the latest N trading rows instead of calendar-day subtraction.
        trading_window = max(int(days or 90), 1)
        df_win = df.tail(trading_window).copy()
        if df_win.empty:
            df_win = df.copy()

        per_col = next(
            (c for c in ["price_earning_ratio", "PER", "per"] if c in df_win.columns), None)
        pbr_col = next(
            (c for c in ["price_book_ratio", "PBR", "pbr"] if c in df_win.columns), None)

        def latest_valid(col):
            if not col:
                return None, None, False
            s = pd.to_numeric(df_win[col], errors="coerce")
            valid = df_win.loc[s.notna(), ["date", col]].copy()
            if valid.empty:
                return None, None, False
            latest_valid_date = valid["date"].max()
            latest_value = valid.loc[valid["date"]
                                     == latest_valid_date, col].iloc[-1]
            return safe_round(latest_value), latest_valid_date, bool(latest_valid_date < latest_row_date)

        per, per_date, per_is_prev = latest_valid(per_col)
        pbr, pbr_date, pbr_is_prev = latest_valid(pbr_col)

        if per_col:
            per_s = pd.to_numeric(df_win[per_col], errors="coerce").dropna()
            per_high = safe_round(per_s.max()) if not per_s.empty else per
            per_low = safe_round(per_s.min()) if not per_s.empty else per
        else:
            per_high = per_low = None

        if pbr_col:
            pbr_s = pd.to_numeric(df_win[pbr_col], errors="coerce").dropna()
            pbr_high = safe_round(pbr_s.max()) if not pbr_s.empty else pbr
            pbr_low = safe_round(pbr_s.min()) if not pbr_s.empty else pbr
        else:
            pbr_high = pbr_low = None

        return {
            "per": per,
            "per_60d_high": per_high if per_high is not None else per,
            "per_60d_low": per_low if per_low is not None else per,
            "per_is_prev": per_is_prev,
            "pbr": pbr,
            "pbr_60d_high": pbr_high if pbr_high is not None else pbr,
            "pbr_60d_low": pbr_low if pbr_low is not None else pbr,
            "pbr_is_prev": pbr_is_prev,
        }
    except Exception as e:
        print(f"❌ PER/PBR 90D error {stock_id}: {e}")
        return empty


def _env_int(name, default, min_value=None, max_value=None):
    """Read an integer environment value with safe fallback."""
    try:
        value = int(str(os.getenv(name, default)).strip())
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(value, int(min_value))
    if max_value is not None:
        value = min(value, int(max_value))
    return value


def _env_float(name, default, min_value=None, max_value=None):
    """Read a float environment value with safe fallback."""
    try:
        value = float(str(os.getenv(name, default)).strip())
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(value, float(min_value))
    if max_value is not None:
        value = min(value, float(max_value))
    return value


def get_chip_config(trend_days=None, concentration_threshold=None):
    """
    籌碼判斷參數。

    可由環境參數設定預設值，也可由呼叫端/UI 傳入覆寫：
    - CHIP_TREND_DAYS：連續判斷天數，預設 3
    - CHIP_CONCENTRATION_THRESHOLD：籌碼集中度門檻百分比，預設 15
    """
    days = trend_days if trend_days is not None else _env_int(
        "CHIP_TREND_DAYS", 3, min_value=1, max_value=20
    )
    threshold = concentration_threshold if concentration_threshold is not None else _env_float(
        "CHIP_CONCENTRATION_THRESHOLD", 15, min_value=0, max_value=100
    )
    try:
        days = max(1, min(int(days), 20))
    except Exception:
        days = 3
    try:
        threshold = max(0.0, min(float(threshold), 100.0))
    except Exception:
        threshold = 15.0
    return days, threshold


def _score_by_ratio(ratio):
    """Convert a -1..1 ratio into the same arrow score style used by KD/BB."""
    if ratio >= 0.999:
        return 1
    if ratio > 0:
        return 0.5
    if ratio <= -0.999:
        return -1
    if ratio < 0:
        return -0.5
    return 0


def get_chip_analysis(stock_id, trend_days=None, concentration_threshold=None, lookback_days=None, workers=None):
    """
    取得近 N 個交易日券商分點籌碼，輸出給 AllStatic/template/signals 使用。

    修正重點：
    - 回傳 latest 彙總欄位。
    - 同時回傳最近 3 個交易日明細 recent_rows。
    - 同時展開 chip_date_t0/t1/t2、chip_concentration_pct_t0/t1/t2、
      main_force_net_t0/t1/t2、broker_diff_t0/t1/t2。
    """
    days, threshold = get_chip_config(trend_days, concentration_threshold)
    try:
        lookback_days = int(lookback_days) if lookback_days is not None else _env_int(
            "CHIP_LOOKBACK_DAYS", max(days * 10, 60), min_value=3, max_value=120
        )
    except Exception:
        lookback_days = max(days * 10, 60)
    lookback_days = max(days, min(int(lookback_days), 120))

    empty = {
        "chip_trend_days": days,
        "chip_concentration_threshold": threshold,
        "chip_latest_date": None,
        "chip_available_days": 0,
        "short_margin_ratio_pct": None,
        "short_margin_ratio_pct_t0": None,
        "short_margin_ratio_pct_t1": None,
        "short_margin_ratio_pct_t2": None,
        "short_margin_ratio_score": None,
        "chip_concentration_pct": None,
        "chip_concentration_pct_t0": None,
        "chip_concentration_pct_t1": None,
        "chip_concentration_pct_t2": None,
        "chip_date_t0": None,
        "chip_date_t1": None,
        "chip_date_t2": None,
        "chip_concentration_score": None,
        "main_force_net": None,
        "main_force_net_t0": None,
        "main_force_net_t1": None,
        "main_force_net_t2": None,
        "main_force_score": None,
        "broker_diff": None,
        "broker_diff_t0": None,
        "broker_diff_t1": None,
        "broker_diff_t2": None,
        "broker_diff_score": None,
        "foreign_investor_net": None,
        "foreign_investor_net_t0": None,
        "foreign_investor_net_t1": None,
        "foreign_investor_net_t2": None,
        "foreign_investor_net_score": None,
        "investment_trust_net": None,
        "investment_trust_net_t0": None,
        "investment_trust_net_t1": None,
        "investment_trust_net_t2": None,
        "investment_trust_net_score": None,
        "dealer_net": None,
        "dealer_net_t0": None,
        "dealer_net_t1": None,
        "dealer_net_t2": None,
        "dealer_net_score": None,
        "institutional_total_net": None,
        "institutional_total_net_t0": None,
        "institutional_total_net_t1": None,
        "institutional_total_net_t2": None,
        "institutional_total_net_score": None,
        "main_force_net_5d": None,
        "total_volume_5d": None,
        "main_force_buy_rate_5d_pct": None,
        "chip_concentration_avg_5d": None,
        "chip_concentration_change_5d": None,
        "broker_diff_avg_5d": None,
        "chip_signal_state": "no_data",
        "chip_signal_text": "籌碼資料不足",
        "recent_rows": [],
    }

    def _round_or_none(value, ndigits=2):
        try:
            if pd.isna(value):
                return None
            return round(float(value), ndigits)
        except Exception:
            return None

    def _int_or_none(value):
        try:
            if pd.isna(value):
                return None
            return int(round(float(value)))
        except Exception:
            return None

    def _first_non_na(*values):
        for value in values:
            if value is None:
                continue
            if hasattr(value, "iloc"):
                series = pd.to_numeric(value, errors="coerce")
                series = series.dropna()
                if not series.empty:
                    return series.iloc[0]
                continue
            try:
                if pd.isna(value):
                    continue
            except Exception:
                pass
            return value
        return None

    def _safe_ratio_pct(numerator, denominator):
        try:
            num = float(numerator)
            den = float(denominator)
            if pd.isna(num) or pd.isna(den) or den == 0:
                return None
            return float(num / den * 100)
        except Exception:
            return None

    def _fetch_short_margin_ratio_with_fallback(target_date, max_back_days=5):
        """
        券資比資料有時晚於分點資料發布，若 T 日抓不到，往前回補最近可用日。
        回傳 (ratio_pct, source_date)。
        """
        try:
            max_back_days = max(0, min(int(max_back_days), 10))
        except Exception:
            max_back_days = 5

        for offset in range(max_back_days + 1):
            query_date = target_date - timedelta(days=offset)
            query_date_str = query_date.strftime("%Y-%m-%d")
            margin_params = {
                "dataset": "TaiwanStockMarginPurchaseShortSale",
                "data_id": str(stock_id),
                "start_date": query_date_str,
                "end_date": query_date_str,
                "token": FINMIND_token,
            }

            _record_finmind_request(
                "chip analysis", stock_id, "TaiwanStockMarginPurchaseShortSale"
            )
            margin_res = requests.get(
                API_URL, params=margin_params, headers=headers, timeout=300
            )
            if margin_res.status_code != 200:
                continue

            margin_data = _safe_response_json(margin_res).get("data", [])
            if not margin_data:
                continue

            margin_df = pd.DataFrame(margin_data)
            if "stock_id" in margin_df.columns:
                margin_df = margin_df[
                    margin_df["stock_id"].astype(str) == str(stock_id)
                ]
            if "date" in margin_df.columns:
                margin_df["date"] = pd.to_datetime(
                    margin_df["date"], errors="coerce"
                )
                margin_df = margin_df[
                    margin_df["date"].dt.date == query_date
                ]
            if margin_df.empty:
                continue

            margin_purchase_balance = _first_non_na(
                margin_df.get("MarginPurchaseTodayBalance"),
                margin_df.get("margin_purchase_today_balance"),
                margin_df.get("MarginPurchase"),
                margin_df.get("margin_purchase"),
                margin_df.get("融資今日餘額"),
                margin_df.get("融資餘額"),
            )
            short_sale_balance = _first_non_na(
                margin_df.get("ShortSaleTodayBalance"),
                margin_df.get("short_sale_today_balance"),
                margin_df.get("ShortSale"),
                margin_df.get("short_sale"),
                margin_df.get("融券今日餘額"),
                margin_df.get("融券餘額"),
            )
            short_margin_ratio_pct = _safe_ratio_pct(
                short_sale_balance, margin_purchase_balance
            )
            if short_margin_ratio_pct is not None:
                return short_margin_ratio_pct, query_date

        return None, None

    def _shares_to_lots(value):
        """
        CMoney 顯示主力買賣超以「張」為單位。
        FinMind TaiwanStockTradingDailyReport 的 buy/sell 多數為股數，
        這裡統一換算為張，避免 HTML 把股數誤標成張數。
        """
        try:
            num = float(value)
            if pd.isna(num):
                return None
            return int(round(num / 1000.0))
        except Exception:
            return None

    def _actual_volume_shares(buy_sum, sell_sum):
        """
        券商分點資料的買進合計與賣出合計代表同一批成交的兩邊。
        CMoney 集中度分母使用成交量，不使用 buy+sell 的雙邊加總。
        因此以買進/賣出合計的平均值近似實際成交股數。
        """
        try:
            buy_num = float(buy_sum)
            sell_num = float(sell_sum)
            if pd.isna(buy_num) and pd.isna(sell_num):
                return None
            if pd.isna(buy_num):
                return sell_num
            if pd.isna(sell_num):
                return buy_num
            return (buy_num + sell_num) / 2.0
        except Exception:
            return None

    def _cmoney_concentration_pct(main_force_lots, total_volume_lots):
        """
        CMoney 口徑：集中度帶正負號。
        主力買賣超為正，集中度為正；主力買賣超為負，集中度為負。
        """
        try:
            main_num = float(main_force_lots)
            vol_num = float(total_volume_lots)
            if pd.isna(main_num) or pd.isna(vol_num) or vol_num == 0:
                return None
            return float(main_num / vol_num * 100)
        except Exception:
            return None

    def _broker_trading_top_rows(broker_df, limit=15):
        """
        依 Yahoo broker-trading 口徑拆出當日買超前 N 名與賣超前 N 名。

        買超榜：以淨買超張數由大到小排序。
        賣超榜：以淨賣超張數由大到小排序（也就是淨買超由小到大）。
        """
        if broker_df is None or broker_df.empty:
            return [], []

        work = broker_df.copy()
        work["buy"] = pd.to_numeric(work.get("buy"), errors="coerce").fillna(0)
        work["sell"] = pd.to_numeric(
            work.get("sell"), errors="coerce").fillna(0)
        work["net_buy"] = work["buy"] - work["sell"]

        buy_sorted = (
            work.loc[work["net_buy"] > 0]
            .sort_values(["net_buy", "broker"], ascending=[False, True])
            .head(limit)
            .reset_index(drop=True)
        )
        sell_sorted = (
            work.loc[work["net_buy"] < 0]
            .sort_values(["net_buy", "broker"], ascending=[True, True])
            .head(limit)
            .reset_index(drop=True)
        )

        def _build_rows(frame, side):
            rows = []
            for idx, (_, rec) in enumerate(frame.iterrows(), start=1):
                net_buy = float(rec.get("net_buy") or 0)
                rows.append({
                    "rank": idx,
                    "broker": rec.get("broker"),
                    "buy": _shares_to_lots(rec.get("buy")),
                    "sell": _shares_to_lots(rec.get("sell")),
                    "net_buy": _shares_to_lots(net_buy),
                    "net_sell": _shares_to_lots(abs(net_buy)) if side == "sell" else None,
                    "side": side,
                })
            return rows

        return _build_rows(buy_sorted, "buy"), _build_rows(sell_sorted, "sell")

    def _calc_cmoney_main_force_from_brokers(broker_df):
        """
        CMoney / Yahoo broker-trading 近似口徑：
        先依券商分點彙總指定期間買賣，再分別取買超前 15 大與賣超前 15 大互抵。
        回傳值以「張」為單位。
        """
        if broker_df is None or broker_df.empty:
            return None
        work = broker_df.copy()
        work["buy"] = pd.to_numeric(work.get("buy"), errors="coerce").fillna(0)
        work["sell"] = pd.to_numeric(
            work.get("sell"), errors="coerce").fillna(0)
        work["net_buy"] = work["buy"] - work["sell"]
        top_buy = (
            work.loc[work["net_buy"] > 0]
            .sort_values(["net_buy", "broker"], ascending=[False, True])
            .head(15)["net_buy"]
            .sum()
        )
        top_sell = (
            work.loc[work["net_buy"] < 0]
            .sort_values(["net_buy", "broker"], ascending=[True, True])
            .head(15)["net_buy"]
            .sum()
        )
        return _shares_to_lots(float(top_buy + top_sell))

    def _institutional_row_net(row):
        def _to_lots(value, already_lots=False):
            try:
                number = float(value)
                if pd.isna(number):
                    return None
                if already_lots:
                    return _int_or_none(number)
                return _int_or_none(number / 1000.0)
            except Exception:
                return None

        explicit_candidates = [
            ("buy_sell_lot", True),
            ("buy_sell_diff_lot", True),
            ("net_buy_sell_lot", True),
            ("買賣超張數", True),
            ("buy_sell", False),
            ("buy_sell_diff", False),
            ("net_buy_sell", False),
            ("買賣超", False),
            ("買賣差額", False),
        ]
        for key, already_lots in explicit_candidates:
            value = row.get(key)
            if value in (None, ""):
                continue
            converted = _to_lots(value, already_lots=already_lots)
            if converted is not None:
                return converted

        buy_value = _first_non_na(
            row.get("buy"),
            row.get("Buy"),
            row.get("buy_volume"),
            row.get("買進股數"),
            row.get("買進張數"),
        )
        sell_value = _first_non_na(
            row.get("sell"),
            row.get("Sell"),
            row.get("sell_volume"),
            row.get("賣出股數"),
            row.get("賣出張數"),
        )
        try:
            if buy_value is None or sell_value is None:
                return None

            buy_key = _first_non_na(
                "buy" if row.get("buy") not in (None, "") else None,
                "Buy" if row.get("Buy") not in (None, "") else None,
                "buy_volume" if row.get(
                    "buy_volume") not in (None, "") else None,
                "買進股數" if row.get("買進股數") not in (None, "") else None,
                "買進張數" if row.get("買進張數") not in (None, "") else None,
            )
            sell_key = _first_non_na(
                "sell" if row.get("sell") not in (None, "") else None,
                "Sell" if row.get("Sell") not in (None, "") else None,
                "sell_volume" if row.get(
                    "sell_volume") not in (None, "") else None,
                "賣出股數" if row.get("賣出股數") not in (None, "") else None,
                "賣出張數" if row.get("賣出張數") not in (None, "") else None,
            )
            lots_mode = any(k in ("買進張數", "賣出張數") for k in (buy_key, sell_key))
            diff_value = float(buy_value) - float(sell_value)
            return _to_lots(diff_value, already_lots=lots_mode)
        except Exception:
            return None

    def _classify_institutional_type(row):
        parts = [
            row.get("name"),
            row.get("investor"),
            row.get("institutional_investor"),
            row.get("買賣別"),
            row.get("dealer"),
        ]
        text = " ".join(str(v or "") for v in parts).strip().lower()
        if not text:
            return None
        if "投信" in text or "investment" in text and "trust" in text:
            return "investment_trust_net"
        if "自營商" in text or "dealer" in text:
            return "dealer_net"
        if "外資" in text or "foreign" in text:
            return "foreign_investor_net"
        return None

    def _sum_or_none(*values):
        total = 0
        has_value = False
        for value in values:
            if value is None:
                continue
            try:
                number = float(value)
                if pd.isna(number):
                    continue
                total += number
                has_value = True
            except Exception:
                continue
        if not has_value:
            return None
        return _int_or_none(total)

    def _institutional_trend_score(t0, t1, t2):
        comparisons = []
        pairs = [(t0, t1), (t1, t2)]
        for current, previous in pairs:
            if current is None or previous is None:
                continue
            try:
                cur = float(current)
                prev = float(previous)
                if pd.isna(cur) or pd.isna(prev):
                    continue
                if cur > prev:
                    comparisons.append(1)
                elif cur < prev:
                    comparisons.append(-1)
                else:
                    comparisons.append(0)
            except Exception:
                continue
        if not comparisons:
            return 0
        score = sum(comparisons)
        return _score_by_ratio(score / len(comparisons))

    try:
        start_date = datetime.today().date() - timedelta(days=lookback_days)
        end_date = datetime.today().date()
        daily_by_date = {}
        broker_frames_by_date = {}
        current_date = end_date
        min_required_days = max(days, 5)

        suppress_api_logs = str(os.getenv("CHIP_SUPPRESS_API_LOGS", "1")).strip().lower() in {
            "1", "true", "yes", "y", "on"
        }
        margin_ratio_lookback_days = _env_int(
            "CHIP_MARGIN_RATIO_LOOKBACK_DAYS", 5, min_value=0, max_value=10
        )

        while current_date >= start_date:
            date_str = current_date.strftime("%Y-%m-%d")
            params = {
                "dataset": "TaiwanStockTradingDailyReport",
                "data_id": str(stock_id),
                "start_date": date_str,
                "end_date": date_str,
                "token": FINMIND_token,
            }

            if not suppress_api_logs:
                print(
                    f"🔎 chip analysis request: dataset={params['dataset']} stock_id={stock_id} date={date_str} token_present={bool(FINMIND_token)}"
                )

            _record_finmind_request(
                "chip analysis", stock_id, "TaiwanStockTradingDailyReport"
            )
            res = requests.get(API_URL, params=params,
                               headers=headers, timeout=300)

            if not suppress_api_logs:
                print(f"🔄 chip analysis response status: {res.status_code}")

            res_data = _safe_response_json(res)

            if res.status_code != 200:
                _print_api_status_error(
                    "chip analysis", stock_id, res, res_data)
                return empty

            data = res_data.get("data", [])
            if not data:
                # T 日無資料（例如週末/休市）時，往前遞延取 T-1。
                current_date -= timedelta(days=1)
                continue

            df = pd.DataFrame(data)
            broker_column = None
            if "broker" in df.columns:
                broker_column = "broker"
            elif "securities_trader" in df.columns:
                broker_column = "securities_trader"
            elif "securities_trader_id" in df.columns:
                broker_column = "securities_trader_id"

            required = {"date", "stock_id", "buy", "sell"}
            if broker_column:
                required.add(broker_column)

            if not required.issubset(df.columns) or broker_column is None:
                if not suppress_api_logs:
                    print(
                        f"⚠️ chip analysis missing cols {stock_id} on {date_str}: cols={list(df.columns)}")
                current_date -= timedelta(days=1)
                continue

            df = df[df["stock_id"].astype(str) == str(stock_id)]
            if df.empty:
                current_date -= timedelta(days=1)
                continue

            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
            df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)
            df = df.dropna(subset=["date"])
            if df.empty:
                current_date -= timedelta(days=1)
                continue

            target_day = pd.to_datetime(date_str, errors="coerce")
            if pd.isna(target_day):
                current_date -= timedelta(days=1)
                continue
            df = df[df["date"].dt.date == target_day.date()]
            if df.empty:
                current_date -= timedelta(days=1)
                continue

            broker_daily = (
                df.groupby(broker_column, as_index=False)[["buy", "sell"]]
                .sum()
                .rename(columns={broker_column: "broker"})
            )
            broker_daily["net_buy"] = broker_daily["buy"] - \
                broker_daily["sell"]
            top_buy_rows, top_sell_rows = _broker_trading_top_rows(
                broker_daily)

            active_buyers = broker_daily.loc[broker_daily["buy"] > 0, "broker"].nunique(
            )
            active_sellers = broker_daily.loc[broker_daily["sell"] > 0, "broker"].nunique(
            )
            broker_diff = int(active_buyers - active_sellers)

            main_force_net = _calc_cmoney_main_force_from_brokers(broker_daily)
            total_volume_shares = _actual_volume_shares(
                broker_daily["buy"].sum(), broker_daily["sell"].sum()
            )
            total_volume_lots = _shares_to_lots(total_volume_shares)
            concentration_pct = _cmoney_concentration_pct(
                main_force_net, total_volume_lots)

            short_margin_ratio_pct, short_margin_source_date = _fetch_short_margin_ratio_with_fallback(
                target_day.date(), max_back_days=margin_ratio_lookback_days
            )

            actual_date = df["date"].max().date()
            if (
                not suppress_api_logs
                and short_margin_source_date is not None
                and short_margin_source_date != actual_date
            ):
                print(
                    f"ℹ️ chip analysis short margin fallback {stock_id}: {actual_date} -> {short_margin_source_date}"
                )
            daily_by_date[actual_date] = {
                "date": actual_date,
                "chip_concentration_pct": concentration_pct,
                "short_margin_ratio_pct": short_margin_ratio_pct,
                "main_force_net": main_force_net,
                "broker_diff": broker_diff,
                "total_volume": total_volume_lots,
                "broker_trading_top_buy_15": top_buy_rows,
                "broker_trading_top_sell_15": top_sell_rows,
            }
            broker_daily["date"] = actual_date
            broker_frames_by_date[actual_date] = broker_daily[[
                "date", "broker", "buy", "sell"]].copy()

            current_date -= timedelta(days=1)
            if len(daily_by_date) >= min_required_days:
                break

        if not daily_by_date:
            return empty

        all_report = pd.DataFrame(list(daily_by_date.values()))
        if "date" not in all_report.columns:
            return empty
        all_report = all_report.sort_values("date", ascending=False)
        report = all_report.head(days)
        if report.empty:
            return empty

        institutional_map_by_date = {}
        try:
            institutional_params = {
                "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id": str(stock_id),
                "start_date": all_report["date"].min().strftime("%Y-%m-%d"),
                "end_date": all_report["date"].max().strftime("%Y-%m-%d"),
                "token": FINMIND_token,
            }
            _record_finmind_request(
                "chip analysis", stock_id, "TaiwanStockInstitutionalInvestorsBuySell"
            )
            institutional_res = requests.get(
                API_URL,
                params=institutional_params,
                headers=headers,
                timeout=300,
            )
            if institutional_res.status_code == 200:
                institutional_rows = _safe_response_json(
                    institutional_res).get("data", [])
                if institutional_rows:
                    institutional_df = pd.DataFrame(institutional_rows)
                    if "stock_id" in institutional_df.columns:
                        institutional_df = institutional_df[
                            institutional_df["stock_id"].astype(
                                str) == str(stock_id)
                        ]
                    institutional_df["date"] = pd.to_datetime(
                        institutional_df.get("date"), errors="coerce"
                    )
                    institutional_df = institutional_df.dropna(
                        subset=["date"])

                    for date_value, group_df in institutional_df.groupby(institutional_df["date"].dt.date):
                        day_values = {
                            "foreign_investor_net": None,
                            "investment_trust_net": None,
                            "dealer_net": None,
                        }
                        for _, record in group_df.iterrows():
                            key = _classify_institutional_type(record)
                            if not key:
                                continue
                            net_value = _institutional_row_net(record)
                            if net_value is None:
                                continue
                            current_value = day_values.get(key)
                            day_values[key] = net_value if current_value is None else _int_or_none(
                                current_value + net_value)

                        day_values["institutional_total_net"] = _sum_or_none(
                            day_values.get("foreign_investor_net"),
                            day_values.get("investment_trust_net"),
                            day_values.get("dealer_net"),
                        )
                        institutional_map_by_date[date_value] = day_values
        except Exception:
            institutional_map_by_date = {}

        margin_cost_map_by_date = {}
        if _warn_missing_finmind_token("TaiwanStockMarginMaintenance", stock_id):
            margin_cost_map_by_date = {}
        else:
            try:
                margin_cost_params = {
                    "dataset": "TaiwanStockMarginMaintenance",
                    "data_id": str(stock_id),
                    "start_date": all_report["date"].min().strftime("%Y-%m-%d"),
                    "end_date": all_report["date"].max().strftime("%Y-%m-%d"),
                    "token": FINMIND_token,
                }
                _record_finmind_request(
                    "chip analysis", stock_id, "TaiwanStockMarginMaintenance"
                )
                margin_cost_res = requests.get(
                    API_URL,
                    params=margin_cost_params,
                    headers=headers,
                    timeout=300,
                )
                if margin_cost_res.status_code == 200:
                    margin_cost_rows = _safe_response_json(
                        margin_cost_res).get("data", [])
                    if margin_cost_rows:
                        margin_cost_df = pd.DataFrame(margin_cost_rows)
                        if "stock_id" in margin_cost_df.columns:
                            margin_cost_df = margin_cost_df[
                                margin_cost_df["stock_id"].astype(
                                    str) == str(stock_id)
                            ]
                        date_col = "date" if "date" in margin_cost_df.columns else "Date"
                        if date_col in margin_cost_df.columns:
                            margin_cost_df[date_col] = pd.to_datetime(
                                margin_cost_df[date_col], errors="coerce"
                            )
                            margin_cost_df = margin_cost_df.dropna(
                                subset=[date_col])
                            for date_value, group_df in margin_cost_df.groupby(date_col):
                                # FinMind TaiwanStockMarginMaintenance 融資成本線欄位為 margin_cost
                                candidate_columns = [
                                    col for col in ("margin_cost", "margin_cost_line", "融資成本")
                                    if col in group_df.columns
                                ]
                                for col in group_df.columns:
                                    if col in candidate_columns:
                                        continue
                                    key = str(col).lower().replace(
                                        " ", "").replace("_", "")
                                    if any(token in key for token in (
                                        "margincost",
                                        "costline",
                                    )):
                                        candidate_columns.append(col)
                                if not candidate_columns:
                                    continue
                                value = None
                                for col in candidate_columns:
                                    value = _first_non_na(group_df.get(col))
                                    if value is not None:
                                        break
                                if value is None:
                                    continue
                                margin_cost_map_by_date[date_value.date()] = _round_or_none(
                                    value, 2)
                elif margin_cost_res.status_code != 200:
                    logger.warning(
                        "TaiwanStockMarginMaintenance request failed for stock_id={} status={} body={}",
                        stock_id,
                        margin_cost_res.status_code,
                        (margin_cost_res.text[:200] if hasattr(
                            margin_cost_res, 'text') else ''),
                    )
            except Exception as exc:
                logger.warning(
                    "TaiwanStockMarginMaintenance failed for stock_id={} error={}",
                    stock_id,
                    exc,
                )
                margin_cost_map_by_date = {}

        # FinMind 融資維持率 22:30 才更新，當日無資料時沿用最近可得日期（與其成本線遞延規則一致）。
        margin_cost_dates_desc = sorted(
            margin_cost_map_by_date.keys(), reverse=True)

        def _margin_cost_on_or_before(target_date):
            if target_date is None:
                return None
            if target_date in margin_cost_map_by_date:
                return margin_cost_map_by_date[target_date]
            for available_date in margin_cost_dates_desc:
                if available_date <= target_date:
                    return margin_cost_map_by_date[available_date]
            return None

        def _window_metrics(window_days: int) -> dict:
            window_df = all_report.head(window_days).copy()
            if window_df.empty:
                return {
                    f"main_force_net_{window_days}d": None,
                    f"total_volume_{window_days}d": None,
                    f"main_force_buy_rate_{window_days}d_pct": None,
                    f"chip_concentration_avg_{window_days}d": None,
                    f"chip_concentration_change_{window_days}d": None,
                    f"broker_diff_avg_{window_days}d": None,
                }

            selected_dates = list(window_df["date"])
            frames = [
                broker_frames_by_date.get(d)
                for d in selected_dates
                if broker_frames_by_date.get(d) is not None
            ]

            main_force_window = None
            total_volume_sum = None
            buy_rate_pct = None
            if frames:
                brokers_window = pd.concat(frames, ignore_index=True)
                broker_agg = brokers_window.groupby("broker", as_index=False)[
                    ["buy", "sell"]].sum()
                main_force_window = _calc_cmoney_main_force_from_brokers(
                    broker_agg)

                daily_volume_lots = []
                for frame in frames:
                    actual_volume = _actual_volume_shares(
                        frame["buy"].sum(), frame["sell"].sum())
                    lots = _shares_to_lots(actual_volume)
                    if lots is not None:
                        daily_volume_lots.append(lots)
                total_volume_sum = sum(
                    daily_volume_lots) if daily_volume_lots else None
                buy_rate_pct = _cmoney_concentration_pct(
                    main_force_window, total_volume_sum)

            concentration_series = pd.to_numeric(
                window_df["chip_concentration_pct"], errors="coerce")
            broker_series = pd.to_numeric(
                window_df["broker_diff"], errors="coerce")

            latest_conc = pd.to_numeric(window_df.iloc[0].get(
                "chip_concentration_pct"), errors="coerce")
            oldest_conc = pd.to_numeric(
                window_df.iloc[-1].get("chip_concentration_pct"), errors="coerce")
            concentration_change = None
            if pd.notna(latest_conc) and pd.notna(oldest_conc):
                concentration_change = float(latest_conc - oldest_conc)

            return {
                f"main_force_net_{window_days}d": _int_or_none(main_force_window),
                f"total_volume_{window_days}d": _int_or_none(total_volume_sum),
                f"main_force_buy_rate_{window_days}d_pct": _round_or_none(buy_rate_pct, 2),
                f"chip_concentration_avg_{window_days}d": _round_or_none(concentration_series.mean(), 2),
                f"chip_concentration_change_{window_days}d": _round_or_none(concentration_change, 2),
                f"broker_diff_avg_{window_days}d": _round_or_none(broker_series.mean(), 2),
            }

        main_pos = int((report["main_force_net"] > 0).sum())
        main_neg = int((report["main_force_net"] < 0).sum())
        diff_pos = int((report["broker_diff"] > 0).sum())
        diff_neg = int((report["broker_diff"] < 0).sum())
        conc_ok = report["chip_concentration_pct"].abs().fillna(0) >= threshold
        conc_pos = int(((report["main_force_net"] > 0) & conc_ok).sum())
        conc_neg = int(((report["main_force_net"] < 0) & conc_ok).sum())
        short_ratio_series = pd.to_numeric(
            report["short_margin_ratio_pct"], errors="coerce")
        short_prev = short_ratio_series.shift(-1)
        short_up = int(((short_ratio_series > short_prev)
                       & short_prev.notna()).sum())
        short_down = int(((short_ratio_series < short_prev)
                         & short_prev.notna()).sum())

        main_score = _score_by_ratio((main_pos - main_neg) / len(report))
        broker_score = _score_by_ratio((diff_pos - diff_neg) / len(report))
        concentration_score = _score_by_ratio(
            (conc_pos - conc_neg) / len(report))
        short_margin_ratio_score = _score_by_ratio(
            (short_up - short_down) / len(report)
        )

        latest = report.iloc[0]
        state = "neutral"
        text = "籌碼震盪，方向未定"
        if main_pos == len(report) and diff_neg == len(report) and conc_pos >= 1:
            state = "bullish_concentrated"
            text = f"主力連{days}買、買賣家數差連{days}負，籌碼偏集中"
        elif main_pos == len(report) and diff_pos >= 1:
            state = "bullish_distributed"
            text = f"主力連{days}買但買賣家數差偏正，可能偏分散"
        elif main_neg == len(report) and diff_pos == len(report):
            state = "bearish_distributed"
            text = f"主力連{days}賣、買賣家數差連{days}正，籌碼流向散戶風險高"
        elif main_neg == len(report):
            state = "bearish"
            text = f"主力連{days}賣，籌碼偏弱"

        recent_rows = []
        for _, r in report.head(3).iterrows():
            date_value = r["date"]
            date_text = date_value.strftime(
                "%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value)[:10]
            date_key = date_value.date() if hasattr(date_value, "date") else date_value
            institutional_day = institutional_map_by_date.get(date_key, {})
            broker_top_buy_15, broker_top_sell_15 = _broker_trading_top_rows(
                broker_frames_by_date.get(date_key)
            )
            recent_rows.append({
                "date": date_text,
                "short_margin_ratio_pct": _round_or_none(r.get("short_margin_ratio_pct"), 2),
                "chip_concentration_pct": _round_or_none(r["chip_concentration_pct"], 2),
                "main_force_net": _int_or_none(r["main_force_net"]),
                "broker_diff": _int_or_none(r["broker_diff"]),
                "margin_cost_line": _round_or_none(_margin_cost_on_or_before(date_key), 2),
                "foreign_investor_net": _int_or_none(
                    institutional_day.get("foreign_investor_net")
                ),
                "investment_trust_net": _int_or_none(
                    institutional_day.get("investment_trust_net")
                ),
                "dealer_net": _int_or_none(
                    institutional_day.get("dealer_net")
                ),
                "institutional_total_net": _int_or_none(
                    institutional_day.get("institutional_total_net")
                ),
                "broker_trading_top_buy_15": broker_top_buy_15,
                "broker_trading_top_sell_15": broker_top_sell_15,
            })

        foreign_t0 = _int_or_none(_first_non_na(
            recent_rows[0].get("foreign_investor_net") if len(
                recent_rows) > 0 else None
        ))
        foreign_t1 = _int_or_none(_first_non_na(
            recent_rows[1].get("foreign_investor_net") if len(
                recent_rows) > 1 else None
        ))
        foreign_t2 = _int_or_none(_first_non_na(
            recent_rows[2].get("foreign_investor_net") if len(
                recent_rows) > 2 else None
        ))

        trust_t0 = _int_or_none(_first_non_na(
            recent_rows[0].get("investment_trust_net") if len(
                recent_rows) > 0 else None
        ))
        trust_t1 = _int_or_none(_first_non_na(
            recent_rows[1].get("investment_trust_net") if len(
                recent_rows) > 1 else None
        ))
        trust_t2 = _int_or_none(_first_non_na(
            recent_rows[2].get("investment_trust_net") if len(
                recent_rows) > 2 else None
        ))

        dealer_t0 = _int_or_none(_first_non_na(
            recent_rows[0].get("dealer_net") if len(recent_rows) > 0 else None
        ))
        dealer_t1 = _int_or_none(_first_non_na(
            recent_rows[1].get("dealer_net") if len(recent_rows) > 1 else None
        ))
        dealer_t2 = _int_or_none(_first_non_na(
            recent_rows[2].get("dealer_net") if len(recent_rows) > 2 else None
        ))

        total_t0 = _int_or_none(_first_non_na(
            recent_rows[0].get("institutional_total_net") if len(
                recent_rows) > 0 else None
        ))
        total_t1 = _int_or_none(_first_non_na(
            recent_rows[1].get("institutional_total_net") if len(
                recent_rows) > 1 else None
        ))
        total_t2 = _int_or_none(_first_non_na(
            recent_rows[2].get("institutional_total_net") if len(
                recent_rows) > 2 else None
        ))
        margin_cost_t0 = _round_or_none(recent_rows[0].get(
            "margin_cost_line") if len(recent_rows) > 0 else None, 2)
        margin_cost_t1 = _round_or_none(recent_rows[1].get(
            "margin_cost_line") if len(recent_rows) > 1 else None, 2)
        margin_cost_t2 = _round_or_none(recent_rows[2].get(
            "margin_cost_line") if len(recent_rows) > 2 else None, 2)

        result = {
            "chip_trend_days": days,
            "chip_concentration_threshold": threshold,
            "chip_latest_date": recent_rows[0]["date"] if recent_rows else None,
            "chip_available_days": len(report),
            "short_margin_ratio_pct": _round_or_none(latest.get("short_margin_ratio_pct"), 2),
            "short_margin_ratio_score": short_margin_ratio_score,
            "chip_concentration_pct": _round_or_none(latest["chip_concentration_pct"], 2),
            "chip_concentration_score": concentration_score,
            "main_force_net": _int_or_none(latest["main_force_net"]),
            "main_force_score": main_score,
            "broker_diff": _int_or_none(latest["broker_diff"]),
            "broker_diff_score": broker_score,
            "margin_cost_line": margin_cost_t0,
            "margin_cost_line_t0": margin_cost_t0,
            "margin_cost_line_t1": margin_cost_t1,
            "margin_cost_line_t2": margin_cost_t2,
            "foreign_investor_net": foreign_t0,
            "foreign_investor_net_t0": foreign_t0,
            "foreign_investor_net_t1": foreign_t1,
            "foreign_investor_net_t2": foreign_t2,
            "foreign_investor_net_score": _institutional_trend_score(foreign_t0, foreign_t1, foreign_t2),
            "investment_trust_net": trust_t0,
            "investment_trust_net_t0": trust_t0,
            "investment_trust_net_t1": trust_t1,
            "investment_trust_net_t2": trust_t2,
            "investment_trust_net_score": _institutional_trend_score(trust_t0, trust_t1, trust_t2),
            "dealer_net": dealer_t0,
            "dealer_net_t0": dealer_t0,
            "dealer_net_t1": dealer_t1,
            "dealer_net_t2": dealer_t2,
            "dealer_net_score": _institutional_trend_score(dealer_t0, dealer_t1, dealer_t2),
            "institutional_total_net": total_t0,
            "institutional_total_net_t0": total_t0,
            "institutional_total_net_t1": total_t1,
            "institutional_total_net_t2": total_t2,
            "institutional_total_net_score": _institutional_trend_score(total_t0, total_t1, total_t2),
            "chip_signal_state": state,
            "chip_signal_text": text,
            "recent_rows": recent_rows,
        }

        result.update(_window_metrics(5))

        for idx, rec in enumerate(recent_rows[:3]):
            suffix = f"t{idx}"
            result[f"chip_date_{suffix}"] = rec["date"]
            result[f"short_margin_ratio_pct_{suffix}"] = rec["short_margin_ratio_pct"]
            result[f"chip_concentration_pct_{suffix}"] = rec["chip_concentration_pct"]
            result[f"main_force_net_{suffix}"] = rec["main_force_net"]
            result[f"broker_diff_{suffix}"] = rec["broker_diff"]
            result[f"margin_cost_line_{suffix}"] = rec["margin_cost_line"]

        for suffix in ("t0", "t1", "t2"):
            result.setdefault(f"chip_date_{suffix}", None)
            result.setdefault(f"short_margin_ratio_pct_{suffix}", None)
            result.setdefault(f"chip_concentration_pct_{suffix}", None)
            result.setdefault(f"main_force_net_{suffix}", None)
            result.setdefault(f"broker_diff_{suffix}", None)
            result.setdefault(f"margin_cost_line_{suffix}", None)

        return result

    except Exception as e:
        print(f"❌ chip analysis error {stock_id}: {e}")
        return empty


def get_disposition_securities_period(stock_id):
    """
    Return active disposition period for one stock from FinMind
    TaiwanStockDispositionSecuritiesPeriod.

    Only periods where today or tomorrow is inside [period_start, period_end]
    are returned. Non-matching stocks return blank period fields so AllStatic.csv
    can keep stable columns without showing inactive disposition periods.
    """
    empty = {
        "period_start": None,
        "period_end": None,
        "disposition_period_start": None,
        "disposition_period_end": None,
    }

    try:
        today = datetime.today().date()
        tomorrow = today + timedelta(days=1)
        params = {
            "dataset": "TaiwanStockDispositionSecuritiesPeriod",
            "data_id": str(stock_id),
            "start_date": (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d"),
            "token": FINMIND_token,
        }

        _record_finmind_request(
            "disposition period",
            stock_id,
            "TaiwanStockDispositionSecuritiesPeriod",
        )
        res = requests.get(API_URL, params=params,
                           headers=headers, timeout=300)
        res_data = _safe_response_json(res)

        if res.status_code != 200:
            _print_api_status_error(
                "disposition period", stock_id, res, res_data)
            return empty

        data = res_data.get("data", [])
        if not data:
            return empty

        df = pd.DataFrame(data)
        if "period_start" not in df.columns or "period_end" not in df.columns:
            print(
                f"⚠️ disposition period missing period_start/period_end {stock_id}: cols={list(df.columns)}"
            )
            return empty

        df["period_start"] = pd.to_datetime(
            df["period_start"], errors="coerce").dt.date
        df["period_end"] = pd.to_datetime(
            df["period_end"], errors="coerce").dt.date
        df = df.dropna(subset=["period_start", "period_end"])
        if df.empty:
            return empty

        mask = (
            ((df["period_start"] <= today) & (today <= df["period_end"]))
            | ((df["period_start"] <= tomorrow) & (tomorrow <= df["period_end"]))
        )
        active = df.loc[mask].sort_values(
            ["period_end", "period_start"], ascending=[False, False])
        if active.empty:
            return empty

        latest = active.iloc[0]
        period_start = latest["period_start"].strftime("%Y-%m-%d")
        period_end = latest["period_end"].strftime("%Y-%m-%d")
        return {
            "period_start": period_start,
            "period_end": period_end,
            "disposition_period_start": period_start,
            "disposition_period_end": period_end,
        }
    except Exception as e:
        print(f"❌ disposition period error {stock_id}: {e}")
        return empty
