"""Fetch and build market index rows (加權指數 / 上櫃指數) for HTML report.

TAIEX : TaiwanStockPrice           data_id=TAIEX  (完整 OHLC)
TPEx  : TaiwanStockTotalReturnIndex data_id=TPEx  (僅 price，收盤合成 OHLC)

Produces a stock-like dict (same keys as process_stock output) so the existing
renderCell / renderRow logic in template.html can render index rows unchanged.
"""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from data_sources import (
    API_URL,
    _record_finmind_request,
    _refresh_finmind_runtime_auth,
    _safe_response_json,
)
from technical_indicators import (
    add_indicators,
    clean_ohlc_data,
    get_kd_trend,
)

# ──────────────────────────────────────────────
# JSON 安全型別轉換
# ──────────────────────────────────────────────


def _sanitize_for_json(row):
    """Convert numpy / pandas types to Python native types for Jinja2 tojson.

    numpy 2.x numpy.bool_ is no longer a subclass of Python bool/int, so
    standard json.dumps (used by Jinja2 tojson) cannot serialize it.
    """
    result = {}
    for k, v in row.items():
        if isinstance(v, (np.bool_,)):
            result[k] = bool(v)
        elif isinstance(v, np.integer):
            result[k] = int(v)
        elif isinstance(v, np.floating):
            result[k] = None if np.isnan(v) else float(v)
        elif isinstance(v, np.ndarray):
            result[k] = v.tolist()
        elif v is pd.NA or v is pd.NaT:
            result[k] = None
        else:
            result[k] = v
    return result


# FinMind 正確 dataset:
#   加權指數 (TAIEX): dataset=TaiwanStockPrice, data_id=TAIEX       → 完整 OHLC
#   上櫃指數 (TPEx) : dataset=TaiwanStockTotalReturnIndex, data_id=TPEx → 僅 price，合成 OHLC
INDEX_CONFIGS = [
    {
        "id": "TAIEX",
        "name": "加權指數",
        "dataset": "TaiwanStockPrice",
        "data_id": "TAIEX",
    },
    {
        "id": "TPEx",
        "name": "上櫃指數",
        "dataset": "TaiwanStockTotalReturnIndex",
        "data_id": "TPEx",
    },
]

_LOOKBACK_DAYS = 500  # 約2年，確保有足夠資料計算 MA60 / Bias60 / 90日LH


def _empty_index_row(index_id, name, reason="資料無法取得"):
    return {
        "code": index_id,
        "name": name,
        "is_index": True,
        "price": None,
        "chg": None,
        "chgPct": None,
        "amp": None,
        "volume": None,
        "prev_volume": None,
        "volume_ratio": None,
        "volume_add": None,
        "k": None,
        "d": None,
        "k_t1": None,
        "d_t1": None,
        "kd_trend": None,
        "kd_3d_up": None,
        "vr_t0": None,
        "vr_t1": None,
        "vr_t2": None,
        "obv_t0": None,
        "obv_t1": None,
        "obv_t2": None,
        "bb_pct": None,
        "bb_pct_t1": None,
        "bb_pct_60d_low": None,
        "bb_pct_60d_high": None,
        "bias5": None,
        "bias5_t1": None,
        "bias20": None,
        "bias20_t1": None,
        "bias60": None,
        "bias60_t1": None,
        "bias5_60d_low": None,
        "bias5_60d_high": None,
        "bias20_60d_low": None,
        "bias20_60d_high": None,
        "bias60_60d_low": None,
        "bias60_60d_high": None,
        "price_60d_low": None,
        "price_60d_high": None,
        "ma5": None,
        "ma20": None,
        "ma60": None,
        "sig": 0,
        "signal": "-",
        "signal_text": reason,
        "score": 0,
        "strategy": "",
        "entry_note": "",
        "news_summary": "-",
        # Chip stubs (not available for indices)
        "chip_date_t0": None,
        "chip_date_t1": None,
        "chip_date_t2": None,
        "chip_concentration_pct_t0": None,
        "main_force_net_t0": None,
        "broker_diff_t0": None,
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
        "short_margin_ratio_pct_t0": None,
        "short_margin_ratio_pct_t1": None,
        "short_margin_ratio_pct_t2": None,
        "short_margin_ratio_score": None,
        # Fundamental stubs
        "eps_Y": None,
        "roe": None,
        "per_latest": None,
        "pbr_latest": None,
        "yield_value": None,
        "gross_margin": None,
        "operating_margin": None,
        "net_margin": None,
        # CB stubs
        "cb_count": 0,
    }


def get_index_ohlc_data(dataset, data_id=None):
    """Fetch index OHLC data from FinMind and return a normalised DataFrame."""
    token, headers = _refresh_finmind_runtime_auth()
    start_date = (datetime.now() - timedelta(days=_LOOKBACK_DAYS)
                  ).strftime("%Y-%m-%d")

    params = {
        "dataset": dataset,
        "start_date": start_date,
        "token": token,
    }
    if data_id:
        params["data_id"] = data_id

    try:
        _record_finmind_request("get_index_ohlc_data",
                                data_id or dataset, dataset)
        res = requests.get(API_URL, params=params, headers=headers, timeout=60)
        data = _safe_response_json(res)

        if res.status_code != 200:
            print(
                f"⚠️ index data HTTP {res.status_code} [{dataset}]: {data.get('msg', '')}")
            return pd.DataFrame()

        rows = data.get("data") or []
        if not rows:
            print(f"⚠️ index data empty [{dataset}]")
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        # ── Normalise column names ────────────────────────────────────────
        col_map = {}
        cols_lower = {c.lower(): c for c in df.columns}

        # close: prefer 'close', then 'price'
        if "close" not in df.columns and "price" in cols_lower:
            col_map[cols_lower["price"]] = "close"

        # open / max / min – may already exist with correct case
        for want in ("open", "max", "min"):
            if want not in df.columns and want in cols_lower:
                col_map[cols_lower[want]] = want

        # volume
        for candidate in ("trading_volume", "trading_volume_1000"):
            if candidate in cols_lower and "volume" not in df.columns:
                col_map[cols_lower[candidate]] = "volume"
                break

        if col_map:
            df = df.rename(columns=col_map)

        # 若資料僅有 close（如 TaiwanStockTotalReturnIndex），就用收盤合成 open/max/min
        # KD 會因 high=low=close 而顯示為空，但 BB% / Bias / MA 仍可正常計算
        for synthetic_col in ("open", "max", "min"):
            if synthetic_col not in df.columns and "close" in df.columns:
                df[synthetic_col] = df["close"]

        required = ["close", "max", "min", "open"]
        if any(c not in df.columns for c in required):
            missing = [c for c in required if c not in df.columns]
            print(
                f"⚠️ index data missing columns {missing} [{dataset}], have: {list(df.columns)}")
            return pd.DataFrame()

        df["date"] = pd.to_datetime(df["date"])
        for col in ("open", "close", "max", "min"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if "volume" not in df.columns:
            df["volume"] = None
        else:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
            # Index Trading_Volume is in lots (千股); large values → already in units
            max_vol = df["volume"].max()
            if pd.notna(max_vol) and max_vol > 1_000_000:
                df["volume"] = df["volume"] / 1000

        df = df.dropna(subset=["close", "max", "min"])
        df = df[df["close"] > 0].sort_values("date").reset_index(drop=True)
        return df

    except Exception as exc:
        print(f"❌ get_index_ohlc_data error [{dataset}]: {exc}")
        return pd.DataFrame()


def build_index_row(index_id, name, df):
    """Build a stock-like dict from an index OHLC DataFrame."""
    base = _empty_index_row(index_id, name)

    if df is None or df.empty:
        return base

    df = clean_ohlc_data(df)
    if df is None or df.empty or len(df) < 5:
        return base

    df = add_indicators(df)
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else latest
    prev2 = df.iloc[-3] if len(df) >= 3 else prev

    def _f(row, key):
        v = row.get(key)
        return float(v) if pd.notna(v) else None

    price = _f(latest, "close")
    prev_close = _f(prev, "close")
    if price is None:
        return base

    chg = round(price - prev_close, 2) if prev_close is not None else None
    chg_pct = round((chg / prev_close) * 100,
                    2) if prev_close and chg is not None else None
    high = _f(latest, "max")
    low = _f(latest, "min")
    amp = round((high - low) / prev_close * 100,
                2) if high and low and prev_close else None

    # 90-day price LH
    df_90 = df.tail(90)
    price_60d_high = float(df_90["max"].max()) if not df_90.empty else None
    price_60d_low = float(df_90["min"].min()) if not df_90.empty else None

    # KD
    kd = get_kd_trend(df)
    k = round(_f(latest, "K"), 2) if _f(latest, "K") is not None else None
    d = round(_f(latest, "D"), 2) if _f(latest, "D") is not None else None
    k_t1 = round(_f(prev, "K"), 2) if _f(prev, "K") is not None else None
    d_t1 = round(_f(prev, "D"), 2) if _f(prev, "D") is not None else None

    # Volume indicators
    vr_t0 = round(_f(latest, "VR"), 2) if _f(latest, "VR") is not None else None
    vr_t1 = round(_f(prev, "VR"), 2) if _f(prev, "VR") is not None else None
    vr_t2 = round(_f(prev2, "VR"), 2) if _f(prev2, "VR") is not None else None
    obv_t0 = round(_f(latest, "OBV"), 0) if _f(latest, "OBV") is not None else None
    obv_t1 = round(_f(prev, "OBV"), 0) if _f(prev, "OBV") is not None else None
    obv_t2 = round(_f(prev2, "OBV"), 0) if _f(prev2, "OBV") is not None else None

    # BB%
    def _bb_pct(row):
        u = _f(row, "BB_upper")
        l = _f(row, "BB_lower")
        c = _f(row, "close")
        if u and l and c and u != l:
            return round((c - l) / (u - l) * 100, 2)
        return None

    bb_pct = _bb_pct(latest)
    bb_pct_t1 = _bb_pct(prev)

    # BB% 90-day LH
    bbu = pd.to_numeric(df["BB_upper"], errors="coerce")
    bbl = pd.to_numeric(df["BB_lower"], errors="coerce")
    cls = pd.to_numeric(df["close"], errors="coerce")
    denom = bbu - bbl
    bb_pct_series = (
        (cls - bbl) / denom.where(denom.abs() > 0.001) * 100).tail(90)
    bb_pct_60d_high = round(float(bb_pct_series.max()),
                            2) if not bb_pct_series.dropna().empty else None
    bb_pct_60d_low = round(float(bb_pct_series.min()),
                           2) if not bb_pct_series.dropna().empty else None

    # Bias
    def _bias(row, key):
        v = _f(row, key)
        return round(v, 2) if v is not None else None

    def _bias_lh(col_hi, col_lo):
        hi = _f(latest, col_hi)
        lo = _f(latest, col_lo)
        return (round(hi, 2) if hi is not None else None,
                round(lo, 2) if lo is not None else None)

    bias5, bias5_t1 = _bias(latest, "BIAS5"), _bias(prev, "BIAS5")
    bias20, bias20_t1 = _bias(latest, "BIAS20"), _bias(prev, "BIAS20")
    bias60, bias60_t1 = _bias(latest, "BIAS60"), _bias(prev, "BIAS60")
    bias5_hi, bias5_lo = _bias_lh("BIAS5_60D_HIGH", "BIAS5_60D_LOW")
    bias20_hi, bias20_lo = _bias_lh("BIAS20_60D_HIGH", "BIAS20_60D_LOW")
    bias60_hi, bias60_lo = _bias_lh("BIAS60_60D_HIGH", "BIAS60_60D_LOW")

    # Volume
    vol = _f(latest, "volume")
    prev_vol = _f(prev, "volume")
    vol_ratio = round((vol / prev_vol - 1) * 100,
                      2) if vol and prev_vol and prev_vol > 0 else None
    vol_add = round(
        vol - prev_vol, 0) if vol is not None and prev_vol is not None else None

    # MA
    ma5 = round(_f(latest, "MA5"), 2) if _f(
        latest, "MA5") is not None else None
    ma20 = round(_f(latest, "MA20"), 2) if _f(
        latest, "MA20") is not None else None
    ma60 = round(_f(latest, "MA60"), 2) if _f(
        latest, "MA60") is not None else None

    # price_lh_trend (3-day close)
    price_lh_trend_t0 = _f(latest, "close")
    price_lh_trend_t1 = _f(prev, "close")
    price_lh_trend_t2 = _f(prev2, "close")

    base.update({
        "price": price,
        "chg": chg,
        "chgPct": chg_pct,
        "amp": amp,
        "volume": round(vol, 0) if vol is not None else None,
        "prev_volume": round(prev_vol, 0) if prev_vol is not None else None,
        "volume_ratio": vol_ratio,
        "volume_add": vol_add,
        # KD
        "k": k, "d": d, "k_t1": k_t1, "d_t1": d_t1,
        "kd_trend": kd.get("kd_trend"),
        "kd_3d_up": kd.get("kd_3d_up"),
        "vr_t0": vr_t0, "vr_t1": vr_t1, "vr_t2": vr_t2,
        "obv_t0": obv_t0, "obv_t1": obv_t1, "obv_t2": obv_t2,
        # BB%
        "bb_pct": bb_pct,
        "bb_pct_t1": bb_pct_t1,
        "bb_pct_60d_high": bb_pct_60d_high,
        "bb_pct_60d_low": bb_pct_60d_low,
        # Bias
        "bias5": bias5, "bias5_t1": bias5_t1,
        "bias20": bias20, "bias20_t1": bias20_t1,
        "bias60": bias60, "bias60_t1": bias60_t1,
        "bias5_60d_high": bias5_hi, "bias5_60d_low": bias5_lo,
        "bias20_60d_high": bias20_hi, "bias20_60d_low": bias20_lo,
        "bias60_60d_high": bias60_hi, "bias60_60d_low": bias60_lo,
        # Price LH
        "price_60d_high": price_60d_high,
        "price_60d_low": price_60d_low,
        "price_t0": price_lh_trend_t0,
        "price_t1": price_lh_trend_t1,
        "price_t2": price_lh_trend_t2,
        "price_60d_high_t1": None,
        "price_60d_low_t1": None,
        # MA
        "ma5": ma5, "ma20": ma20, "ma60": ma60,
        "signal_text": "-",
    })
    return _sanitize_for_json(base)


# ──────────────────────────────────────────────
# 市場總籌碼（全市場三大法人 + 融資融券）
# ──────────────────────────────────────────────

_CHIP_LOOKBACK_DAYS = 20  # 保留 T/T-1/T-2 三日


def _fetch_market_total_chips() -> dict:
    """Fetch TaiwanStockTotalMarginPurchaseShortSale & TaiwanStockTotalInstitutionalInvestors."""
    token, headers = _refresh_finmind_runtime_auth()
    start_date = (datetime.now() -
                  timedelta(days=_CHIP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    result: dict = {"margin_rows": [], "institutional_rows": []}

    for key, dataset in [
        ("margin_rows", "TaiwanStockTotalMarginPurchaseShortSale"),
        ("institutional_rows", "TaiwanStockTotalInstitutionalInvestors"),
    ]:
        try:
            _record_finmind_request("market_total_chips", "total", dataset)
            params = {"dataset": dataset,
                      "start_date": start_date, "token": token}
            res = requests.get(API_URL, params=params,
                               headers=headers, timeout=30)
            data = _safe_response_json(res)
            if res.status_code == 200:
                result[key] = data.get("data") or []
            else:
                print(f"[index chips] {dataset} HTTP {res.status_code}")
        except Exception as exc:
            print(f"[index chips] {dataset} error: {exc}")

    return result


def _parse_total_chips(chip_raw: dict) -> list:
    """Parse raw API rows into per-date dicts, sorted newest-first.

    Each entry has keys:
        date, short_margin_ratio_pct, margin_balance, short_balance,
        foreign_investor_net, investment_trust_net, dealer_net, institutional_total_net
    Institutional values are in 億元 (÷1e8).
    Margin/short values are in 張 (lots).
    """
    margin_rows = chip_raw.get("margin_rows") or []
    institutional_rows = chip_raw.get("institutional_rows") or []

    def _n(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    margin_by_date: dict = {}
    for row in margin_rows:
        d, n = row.get("date"), row.get("name")
        if d and n:
            margin_by_date.setdefault(d, {})[n] = row

    inst_by_date: dict = {}
    for row in institutional_rows:
        d, n = row.get("date"), row.get("name")
        if d and n:
            inst_by_date.setdefault(d, {})[n] = row

    all_dates = sorted(
        set(list(margin_by_date.keys()) + list(inst_by_date.keys())), reverse=True
    )

    parsed = []
    for date in all_dates:
        m = margin_by_date.get(date, {})
        inst = inst_by_date.get(date, {})

        mp = m.get("MarginPurchase", {})
        ss = m.get("ShortSale", {})
        mp_bal = _n(mp.get("TodayBalance"))
        ss_bal = _n(ss.get("TodayBalance"))
        short_margin_pct = (
            round(ss_bal / mp_bal * 100,
                  2) if mp_bal and ss_bal and mp_bal > 0 else None
        )

        def _net_yi(name_key: str):
            row = inst.get(name_key, {})
            buy, sell = _n(row.get("buy")), _n(row.get("sell"))
            if buy is None or sell is None:
                return None
            return round((buy - sell) / 1e8, 1)

        fi_net = _net_yi("Foreign_Investor")
        it_net = _net_yi("Investment_Trust")

        ds, dh = inst.get("Dealer_self", {}), inst.get("Dealer_Hedging", {})
        ds_b, ds_s = _n(ds.get("buy")), _n(ds.get("sell"))
        dh_b, dh_s = _n(dh.get("buy")), _n(dh.get("sell"))
        dealer_net = (
            round((ds_b + dh_b - ds_s - dh_s) / 1e8, 1)
            if all(v is not None for v in [ds_b, ds_s, dh_b, dh_s])
            else None
        )
        total_net = _net_yi("total")

        parsed.append({
            "date": date,
            "short_margin_ratio_pct": short_margin_pct,
            "margin_balance": int(mp_bal) if mp_bal is not None else None,
            "short_balance": int(ss_bal) if ss_bal is not None else None,
            "foreign_investor_net": fi_net,
            "investment_trust_net": it_net,
            "dealer_net": dealer_net,
            "institutional_total_net": total_net,
        })

    return parsed  # newest first


def _build_index_chip_fields(parsed: list) -> dict:
    """Extract T / T-1 / T-2 values and trend scores from parsed chip list."""
    if not parsed:
        return {}

    def _score(v):
        if v is None:
            return None
        return 1 if v > 0 else (-1 if v < 0 else 0)

    t0 = parsed[0] if len(parsed) > 0 else {}
    t1 = parsed[1] if len(parsed) > 1 else {}
    t2 = parsed[2] if len(parsed) > 2 else {}

    fields: dict = {
        "chip_date_t0": t0.get("date"),
        "chip_date_t1": t1.get("date"),
        "chip_date_t2": t2.get("date"),
    }

    for key in ("foreign_investor_net", "investment_trust_net",
                "dealer_net", "institutional_total_net"):
        v0, v1, v2 = t0.get(key), t1.get(key), t2.get(key)
        fields[key] = v0
        fields[f"{key}_t0"] = v0
        fields[f"{key}_t1"] = v1
        fields[f"{key}_t2"] = v2
        fields[f"{key}_score"] = _score(v0)

    v0 = t0.get("short_margin_ratio_pct")
    fields["short_margin_ratio_pct_t0"] = v0
    fields["short_margin_ratio_pct_t1"] = t1.get("short_margin_ratio_pct")
    fields["short_margin_ratio_pct_t2"] = t2.get("short_margin_ratio_pct")
    fields["short_margin_ratio_score"] = _score(v0)

    return fields


def get_market_index_rows():
    """Return list of index row dicts for TAIEX and TPEx, with market-wide chip data."""
    chip_fields: dict = {}
    try:
        print("[index] fetching market-wide chip data...")
        chip_raw = _fetch_market_total_chips()
        parsed = _parse_total_chips(chip_raw)
        chip_fields = _build_index_chip_fields(parsed)
        print(
            f"[index] chips: T={chip_fields.get('chip_date_t0', '?')}, "
            f"fi={chip_fields.get('foreign_investor_net_t0')} yi, "
            f"it={chip_fields.get('investment_trust_net_t0')} yi, "
            f"smpct={chip_fields.get('short_margin_ratio_pct_t0')}"
        )
    except Exception as exc:
        print(f"[index] chips error: {exc}")

    rows = []
    for cfg in INDEX_CONFIGS:
        index_id = cfg["id"]
        name = cfg["name"]
        dataset = cfg["dataset"]
        data_id = cfg.get("data_id")
        print(f"[index] loading: {name} [{dataset}]")
        try:
            df = get_index_ohlc_data(dataset, data_id)
            if df is None or df.empty:
                print(f"[index] empty: {name}")
                rows.append(_empty_index_row(index_id, name))
            else:
                row = build_index_row(index_id, name, df)
                if chip_fields:
                    row.update(chip_fields)
                print(
                    f"  ✅ {name}: price={row.get('price')}, "
                    f"chgPct={row.get('chgPct')}, K={row.get('k')}, BB%={row.get('bb_pct')}"
                )
                rows.append(row)
        except Exception as exc:
            print(f"[index] error: {name}: {exc}")
            empty = _empty_index_row(index_id, name, f"error: {exc}")
            if chip_fields:
                empty.update(chip_fields)
            rows.append(empty)
    return rows
