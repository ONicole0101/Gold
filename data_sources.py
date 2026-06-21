import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import requests
from FinMind.data import DataLoader
from loguru import logger

# Standardize on FINMIND_TOKEN only.
FINMIND_token = os.getenv("FINMIND_TOKEN")
FINMIND_TOKEN_SOURCE = "FINMIND_TOKEN" if FINMIND_token else ""
headers = {"Authorization": f"Bearer {FINMIND_token}"} if FINMIND_token else {}
API_URL = 'https://api.finmindtrade.com/api/v4/data'
USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"
FINMIND_USAGE_LOG_FILE = os.getenv(
    "FINMIND_USAGE_LOG_FILE", "finmind_token_usage_log.csv")

api = DataLoader()
FINMIND_TOKEN_LOGIN_STATUS = "missing_token"
FINMIND_TOKEN_LOGIN_MESSAGE = "FINMIND_TOKEN is not set"
if FINMIND_token:
    try:
        api.login_by_token(api_token=FINMIND_token)
        FINMIND_TOKEN_LOGIN_STATUS = "ok"
        FINMIND_TOKEN_LOGIN_MESSAGE = "DataLoader.login_by_token succeeded"
    except Exception as e:
        FINMIND_TOKEN_LOGIN_STATUS = "error"
        FINMIND_TOKEN_LOGIN_MESSAGE = str(e)

_INITIAL_QUOTA_PRINTED = False
FINMIND_API_CALL_COUNT = 0
FINMIND_DATASET_CALL_COUNTS = {}

# 停用所有來自 FinMind 的 Log 訊息
logger.remove()
logging.getLogger('FinMind').setLevel(logging.WARNING)


def _mask_token(token=None):
    """Return a safe token display value, never the full token."""
    token = token if token is not None else FINMIND_token
    if not token:
        return ""
    token = str(token)
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "..." + token[-4:]


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


def get_finmind_token_status():
    """Return local evidence that the FinMind token was loaded and DataLoader login ran."""
    return {
        "token_present": bool(FINMIND_token),
        "token_source": FINMIND_TOKEN_SOURCE,
        "token_masked": _mask_token(),
        "login_status": FINMIND_TOKEN_LOGIN_STATUS,
        "login_message": FINMIND_TOKEN_LOGIN_MESSAGE,
        "request_count": FINMIND_API_CALL_COUNT,
        "dataset_call_counts": dict(FINMIND_DATASET_CALL_COUNTS),
        "usage_log_file": FINMIND_USAGE_LOG_FILE,
    }


def get_finmind_user_info(write_log=True, source="user_info"):
    """
    Validate the token against FinMind user_info and return usage information.

    This is the strongest runtime check that the token is accepted by FinMind:
    - token_present/token_source proves the environment variable was loaded
    - HTTP status/body proves FinMind accepted or rejected it
    - user_count/api_request_limit/remain shows actual account quota usage
    """
    if not FINMIND_token:
        info = {
            "ok": False,
            "token_present": False,
            "token_source": "",
            "token_masked": "",
            "login_status": FINMIND_TOKEN_LOGIN_STATUS,
            "login_message": FINMIND_TOKEN_LOGIN_MESSAGE,
            "user_count": None,
            "api_request_limit": None,
            "remain": None,
            "status_code": None,
            "message": "FINMIND_TOKEN is not set",
        }
        if write_log:
            _append_finmind_usage_event(
                event="token_check",
                source=source,
                status="missing_token",
                message=info["message"],
            )
        return info

    try:
        res = requests.get(USER_INFO_URL, headers=headers, timeout=300)
        data = _safe_response_json(res)
        used = data.get("user_count")
        limit = data.get("api_request_limit")

        try:
            used_int = int(used or 0)
            limit_int = int(limit or 0)
            remain = max(limit_int - used_int, 0) if limit_int else None
        except Exception:
            used_int = used
            limit_int = limit
            remain = None

        ok = res.status_code == 200 and not data.get("error")
        msg = data.get("msg") or data.get(
            "message") or data.get("status") or res.text[:200]
        info = {
            "ok": ok,
            "token_present": True,
            "token_source": FINMIND_TOKEN_SOURCE,
            "token_masked": _mask_token(),
            "login_status": FINMIND_TOKEN_LOGIN_STATUS,
            "login_message": FINMIND_TOKEN_LOGIN_MESSAGE,
            "user_count": used_int,
            "api_request_limit": limit_int,
            "remain": remain,
            "status_code": res.status_code,
            "message": msg,
        }

        if write_log:
            _append_finmind_usage_event(
                event="token_check",
                source=source,
                status="ok" if ok else "error",
                status_code=res.status_code,
                user_count=used_int,
                api_request_limit=limit_int,
                remain=remain,
                message=msg,
            )
        return info

    except Exception as e:
        info = {
            "ok": False,
            "token_present": True,
            "token_source": FINMIND_TOKEN_SOURCE,
            "token_masked": _mask_token(),
            "login_status": FINMIND_TOKEN_LOGIN_STATUS,
            "login_message": FINMIND_TOKEN_LOGIN_MESSAGE,
            "user_count": None,
            "api_request_limit": None,
            "remain": None,
            "status_code": None,
            "message": str(e),
        }
        if write_log:
            _append_finmind_usage_event(
                event="token_check",
                source=source,
                status="error",
                message=str(e),
            )
        return info


def _safe_response_json(res):
    """避免 API 回傳非 JSON 時，印錯誤又讓程式中斷。"""
    try:
        return res.json()
    except Exception:
        return {}


def _extract_remaining_quota(data, res=None):
    """從 FinMind 回傳 body/header 中盡量抓出剩餘次數/額度資訊。"""
    if res is not None:
        for key in [
            "X-RateLimit-Remaining", "X-Rate-Limit-Remaining",
            "RateLimit-Remaining", "x-ratelimit-remaining"
        ]:
            value = res.headers.get(key)
            if value not in [None, ""]:
                return f"header {key}={value}"

    for key in [
        "api_usage", "api_remaining", "remaining", "remaining_count",
        "quota", "limit", "msg", "message", "status"
    ]:
        value = data.get(key) if isinstance(data, dict) else None
        if value not in [None, ""]:
            text = str(value)
            if any(word in text.lower() for word in [
                "remaining", "quota", "limit", "api", "剩餘", "次數", "額度"
            ]):
                return text
    return None


def _print_initial_quota_once(data, res=None):
    """第一次收到 API 回應時，印出起始剩餘次數。"""
    global _INITIAL_QUOTA_PRINTED
    if _INITIAL_QUOTA_PRINTED:
        return

    quota_msg = _extract_remaining_quota(data, res)
    if quota_msg is None:
        quota_msg = "API 回應未提供剩餘次數欄位"

    print(f"🔢 FinMind API 起始剩餘次數: {quota_msg}")
    _INITIAL_QUOTA_PRINTED = True


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
        _print_initial_quota_once(data, res)

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

        if volume_col:
            df['volume'] = pd.to_numeric(df[volume_col], errors='coerce')
            if df['volume'].max() > 100000:
                df['volume'] = df['volume'] / 1000
        else:
            df['volume'] = None

        df = df.dropna(subset=['open', 'close', 'max',
                       'min']).sort_values('date')

        return df
    except RuntimeError:
        raise
    except Exception as e:
        print(f'❌ get_stock_data error {stock_id}: {e}')
        return pd.DataFrame()


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
        _print_initial_quota_once(res_data, res)

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
        _record_finmind_request("profit source", stock_id, "TaiwanStockFinancialStatements")
        res = requests.get(API_URL, params=params, headers=headers, timeout=300)
        data = _safe_response_json(res)
        _print_initial_quota_once(data, res)

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
        _record_finmind_request("balance sheet source", stock_id, "TaiwanStockBalanceSheet")
        res = requests.get(API_URL, params=params, headers=headers, timeout=300)
        data = _safe_response_json(res)
        _print_initial_quota_once(data, res)

        if res.status_code != 200:
            _print_api_status_error('balance sheet source', stock_id, res, data)
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
        _print_initial_quota_once(data, res)

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
        _print_initial_quota_once(data, res)

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
        _print_initial_quota_once(data, res)

        if res.status_code != 200:
            _print_api_status_error('PER source', stock_id, res, data)
            return []

        return data.get('data', [])
    except Exception as e:
        print(f'❌ PER source error {stock_id}: {e}')
        return []


def get_per_pbr_60d_stats(stock_id, days=60):
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
        _record_finmind_request("PER/PBR 60D", stock_id, "TaiwanStockPER")
        res = requests.get(API_URL, params=params, headers=headers, timeout=300)
        res_data = _safe_response_json(res)
        _print_initial_quota_once(res_data, res)

        if res.status_code != 200:
            _print_api_status_error('PER/PBR 60D', stock_id, res, res_data)
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
        cutoff = latest_row_date - pd.Timedelta(days=days)
        df_win = df[df["date"] >= cutoff].copy()
        if df_win.empty:
            df_win = df.copy()

        per_col = next((c for c in ["price_earning_ratio", "PER", "per"] if c in df_win.columns), None)
        pbr_col = next((c for c in ["price_book_ratio", "PBR", "pbr"] if c in df_win.columns), None)

        def latest_valid(col):
            if not col:
                return None, None, False
            s = pd.to_numeric(df_win[col], errors="coerce")
            valid = df_win.loc[s.notna(), ["date", col]].copy()
            if valid.empty:
                return None, None, False
            latest_valid_date = valid["date"].max()
            latest_value = valid.loc[valid["date"] == latest_valid_date, col].iloc[-1]
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
        print(f"❌ PER/PBR 60D error {stock_id}: {e}")
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
            "CHIP_LOOKBACK_DAYS", max(days * 7, 21), min_value=3, max_value=120
        )
    except Exception:
        lookback_days = max(days * 7, 21)
    lookback_days = max(days, min(int(lookback_days), 120))

    empty = {
        "chip_trend_days": days,
        "chip_concentration_threshold": threshold,
        "chip_latest_date": None,
        "chip_available_days": 0,
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

    try:
        start_date = datetime.today().date() - timedelta(days=lookback_days)
        end_date = datetime.today().date()
        daily = []
        current_date = start_date

        suppress_api_logs = str(os.getenv("CHIP_SUPPRESS_API_LOGS", "1")).strip().lower() in {
            "1", "true", "yes", "y", "on"
        }

        while current_date <= end_date:
            date_str = current_date.strftime("%Y-%m-%d")
            params = {
                "dataset": "TaiwanStockTradingDailyReport",
                "data_id": str(stock_id),
                "start_date": date_str,
                "token": FINMIND_token,
            }

            if not suppress_api_logs:
                print(
                    f"🔎 chip analysis request: dataset={params['dataset']} stock_id={stock_id} date={date_str} token_present={bool(FINMIND_token)}"
                )

            _record_finmind_request(
                "chip analysis", stock_id, "TaiwanStockTradingDailyReport"
            )
            res = requests.get(API_URL, params=params, headers=headers, timeout=300)

            if not suppress_api_logs:
                print(f"🔄 chip analysis response status: {res.status_code}")

            res_data = _safe_response_json(res)
            _print_initial_quota_once(res_data, res)

            if res.status_code != 200:
                _print_api_status_error("chip analysis", stock_id, res, res_data)
                return empty

            data = res_data.get("data", [])
            if not data:
                current_date += timedelta(days=1)
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
                    print(f"⚠️ chip analysis missing cols {stock_id} on {date_str}: cols={list(df.columns)}")
                current_date += timedelta(days=1)
                continue

            df = df[df["stock_id"].astype(str) == str(stock_id)]
            if df.empty:
                current_date += timedelta(days=1)
                continue

            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["buy"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0)
            df["sell"] = pd.to_numeric(df["sell"], errors="coerce").fillna(0)
            df = df.dropna(subset=["date"])
            if df.empty:
                current_date += timedelta(days=1)
                continue

            df["net_buy"] = df["buy"] - df["sell"]
            active_buyers = df.loc[df["buy"] > 0, broker_column].nunique()
            active_sellers = df.loc[df["sell"] > 0, broker_column].nunique()
            broker_diff = int(active_buyers - active_sellers)

            sorted_group = df.sort_values("net_buy", ascending=False)
            top_buy = sorted_group.head(15)["net_buy"].sum()
            top_sell = sorted_group.tail(15)["net_buy"].sum()
            main_force_net = float(top_buy + top_sell)
            total_turnover = float((df["buy"] + df["sell"]).sum())
            concentration_pct = (
                abs(main_force_net) / total_turnover * 100
            ) if total_turnover else None

            actual_date = df["date"].max().date()
            daily.append({
                "date": actual_date,
                "chip_concentration_pct": concentration_pct,
                "main_force_net": main_force_net,
                "broker_diff": broker_diff,
            })

            current_date += timedelta(days=1)

        report = pd.DataFrame(daily).sort_values("date", ascending=False).head(days)
        if report.empty:
            return empty

        main_pos = int((report["main_force_net"] > 0).sum())
        main_neg = int((report["main_force_net"] < 0).sum())
        diff_pos = int((report["broker_diff"] > 0).sum())
        diff_neg = int((report["broker_diff"] < 0).sum())
        conc_ok = report["chip_concentration_pct"].fillna(0) >= threshold
        conc_pos = int(((report["main_force_net"] > 0) & conc_ok).sum())
        conc_neg = int(((report["main_force_net"] < 0) & conc_ok).sum())

        main_score = _score_by_ratio((main_pos - main_neg) / len(report))
        broker_score = _score_by_ratio((diff_pos - diff_neg) / len(report))
        concentration_score = _score_by_ratio((conc_pos - conc_neg) / len(report))

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
            date_text = date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value)[:10]
            recent_rows.append({
                "date": date_text,
                "chip_concentration_pct": _round_or_none(r["chip_concentration_pct"], 2),
                "main_force_net": _int_or_none(r["main_force_net"]),
                "broker_diff": _int_or_none(r["broker_diff"]),
            })

        result = {
            "chip_trend_days": days,
            "chip_concentration_threshold": threshold,
            "chip_latest_date": recent_rows[0]["date"] if recent_rows else None,
            "chip_available_days": len(report),
            "chip_concentration_pct": _round_or_none(latest["chip_concentration_pct"], 2),
            "chip_concentration_score": concentration_score,
            "main_force_net": _int_or_none(latest["main_force_net"]),
            "main_force_score": main_score,
            "broker_diff": _int_or_none(latest["broker_diff"]),
            "broker_diff_score": broker_score,
            "chip_signal_state": state,
            "chip_signal_text": text,
            "recent_rows": recent_rows,
        }

        for idx, rec in enumerate(recent_rows[:3]):
            suffix = f"t{idx}"
            result[f"chip_date_{suffix}"] = rec["date"]
            result[f"chip_concentration_pct_{suffix}"] = rec["chip_concentration_pct"]
            result[f"main_force_net_{suffix}"] = rec["main_force_net"]
            result[f"broker_diff_{suffix}"] = rec["broker_diff"]

        for suffix in ("t0", "t1", "t2"):
            result.setdefault(f"chip_date_{suffix}", None)
            result.setdefault(f"chip_concentration_pct_{suffix}", None)
            result.setdefault(f"main_force_net_{suffix}", None)
            result.setdefault(f"broker_diff_{suffix}", None)

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
        res = requests.get(API_URL, params=params, headers=headers, timeout=300)
        res_data = _safe_response_json(res)
        _print_initial_quota_once(res_data, res)

        if res.status_code != 200:
            _print_api_status_error("disposition period", stock_id, res, res_data)
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

        df["period_start"] = pd.to_datetime(df["period_start"], errors="coerce").dt.date
        df["period_end"] = pd.to_datetime(df["period_end"], errors="coerce").dt.date
        df = df.dropna(subset=["period_start", "period_end"])
        if df.empty:
            return empty

        mask = (
            ((df["period_start"] <= today) & (today <= df["period_end"]))
            | ((df["period_start"] <= tomorrow) & (tomorrow <= df["period_end"]))
        )
        active = df.loc[mask].sort_values(["period_end", "period_start"], ascending=[False, False])
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

