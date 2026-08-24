import os
import pandas as pd
import numpy as np

from custom_categories import CUSTOM_CATEGORY_COLUMN, category_text, split_category_text
from data_sources import get_stock_data
from financial_analysis import (
    calc_eps_score,
    calc_margin_score,
    calc_trend_score,
)
from signals import get_tech_signal
from technical_indicators import add_indicators, clean_ohlc_data, get_kd_trend, get_bb_trend, get_MABias, get_support_resistance_levels


STATIC_CSV_PATH = os.getenv("STATIC_CSV_FILE", "AllStatic.csv")
STATIC_VALUATION_CSV_PATH = (
    os.getenv("STATIC_VALUATION_OUTPUT_FILE")
    or os.getenv("STATIC_VALUATION_FILE")
    or os.getenv("STATIC_VALUATION_CSV", "AllStatic_Valuation.csv")
)
STATIC_CHIPS_CSV_PATH = os.getenv("STATIC_CHIPS_FILE", "AllStatic_Chips.csv")
STATIC_NEWS_CSV_PATH = (
    os.getenv("ALLSTATIC_NEWS_OUTPUT_FILE")
    or os.getenv("ALLSTATIC_NEWS_FILE")
    or os.getenv("ALLSTATIC_NEWS_CSV", "AllStatic_news.csv")
)
_STATIC_MAP_CACHE = None
_STATIC_MAP_MTIME = None
_VALUATION_STATIC_MAP_CACHE = None
_VALUATION_STATIC_MAP_MTIME = None
_CHIPS_STATIC_MAP_CACHE = None
_CHIPS_STATIC_MAP_MTIME = None
_NEWS_STATIC_MAP_CACHE = None
_NEWS_STATIC_MAP_MTIME = None


def get_price_60d_high_low(df):
    df = clean_ohlc_data(df)
    if df is None or df.empty:
        return {
            "price_60d_high": None,
            "price_60d_low": None,
        }
    # Keep legacy key names for compatibility, but compute over 90 trading days.
    df_90 = df.tail(90)
    max_price90 = pd.to_numeric(df_90["max"], errors="coerce").max()
    min_price90 = pd.to_numeric(df_90["min"], errors="coerce").min()

    if pd.isna(max_price90) or pd.isna(min_price90):
        return {
            "price_60d_high": None,
            "price_60d_low": None,
        }

    return {
        "price_60d_high": float(max_price90),
        "price_60d_low": float(min_price90),
    }


def load_static_map(static_csv_path=STATIC_CSV_PATH, force_reload=False):
    global _STATIC_MAP_CACHE, _STATIC_MAP_MTIME

    try:
        if not os.path.exists(static_csv_path):
            print(f"⚠️ 找不到靜態資料檔: {static_csv_path}")
            return {}

        mtime = os.path.getmtime(static_csv_path)
        if (not force_reload) and _STATIC_MAP_CACHE is not None and _STATIC_MAP_MTIME == mtime:
            return _STATIC_MAP_CACHE

        df = pd.read_csv(static_csv_path, encoding="utf-8-sig",
                         dtype={"stock_id": str})
        df.columns = df.columns.str.strip()

        if "stock_id" not in df.columns:
            print(f"⚠️ AllStatic.csv 缺少 stock_id 欄位: {static_csv_path}")
            return {}

        # 將 NaN 轉為 None，方便後續使用
        df = df.where(pd.notna(df), None)

        static_map = {}
        for _, row in df.iterrows():
            stock_id = str(row["stock_id"]).strip()
            static_map[stock_id] = row.to_dict()

        valuation_map = load_valuation_static_map(force_reload=force_reload)
        if valuation_map:
            valuation_keys = [
                "per_latest",
                "per_60d_high",
                "per_60d_low",
                "pbr_latest",
                "pbr_60d_high",
                "pbr_60d_low",
                "yield_value",
                "per_latest_is_prev",
                "pbr_latest_is_prev",
                "valuation_updated_at",
                "valuation_status",
                "valuation_reason",
                "finmind_token_status",
                "finmind_token_source",
                "finmind_token_masked",
                "finmind_user_count",
                "finmind_api_request_limit",
                "finmind_remain",
                "finmind_usage_checked_at",
            ]
            for stock_id, valuation_row in valuation_map.items():
                base = static_map.setdefault(stock_id, {"stock_id": stock_id})
                for key in valuation_keys:
                    if key in valuation_row:
                        base[key] = valuation_row.get(key)

        _STATIC_MAP_CACHE = static_map
        _STATIC_MAP_MTIME = mtime
        print(f"✅ 已載入靜態資料: {static_csv_path}, 筆數={len(static_map)}")
        return static_map

    except Exception as e:
        print(f"❌ 讀取 AllStatic.csv 失敗: {e}")
        return {}


def load_valuation_static_map(static_valuation_csv_path=STATIC_VALUATION_CSV_PATH, force_reload=False):
    global _VALUATION_STATIC_MAP_CACHE, _VALUATION_STATIC_MAP_MTIME

    try:
        if not os.path.exists(static_valuation_csv_path):
            return {}

        mtime = os.path.getmtime(static_valuation_csv_path)
        if (not force_reload) and _VALUATION_STATIC_MAP_CACHE is not None and _VALUATION_STATIC_MAP_MTIME == mtime:
            return _VALUATION_STATIC_MAP_CACHE

        df = pd.read_csv(static_valuation_csv_path,
                         encoding="utf-8-sig", dtype={"stock_id": str})
        df.columns = df.columns.str.strip()

        if "stock_id" not in df.columns:
            print(
                f"⚠️ AllStatic_Valuation.csv 缺少 stock_id 欄位: {static_valuation_csv_path}")
            return {}

        df = df.where(pd.notna(df), None)

        valuation_map = {}
        for _, row in df.iterrows():
            stock_id = str(row["stock_id"]).strip()
            valuation_map[stock_id] = row.to_dict()

        _VALUATION_STATIC_MAP_CACHE = valuation_map
        _VALUATION_STATIC_MAP_MTIME = mtime
        print(
            f"✅ 已載入估值靜態資料: {static_valuation_csv_path}, 筆數={len(valuation_map)}")
        return valuation_map

    except Exception as e:
        print(f"❌ 讀取 AllStatic_Valuation.csv 失敗: {e}")
        return {}


def load_chips_static_map(static_chips_csv_path=STATIC_CHIPS_CSV_PATH, force_reload=False):
    global _CHIPS_STATIC_MAP_CACHE, _CHIPS_STATIC_MAP_MTIME

    try:
        if not os.path.exists(static_chips_csv_path):
            print(f"⚠️ 找不到籌碼靜態資料檔: {static_chips_csv_path}")
            return {}

        mtime = os.path.getmtime(static_chips_csv_path)
        if (not force_reload) and _CHIPS_STATIC_MAP_CACHE is not None and _CHIPS_STATIC_MAP_MTIME == mtime:
            return _CHIPS_STATIC_MAP_CACHE

        df = pd.read_csv(static_chips_csv_path,
                         encoding="utf-8-sig", dtype={"stock_id": str})
        df.columns = df.columns.str.strip()

        if "stock_id" not in df.columns:
            print(
                f"⚠️ AllStatic_Chips.csv 缺少 stock_id 欄位: {static_chips_csv_path}")
            return {}

        df = df.where(pd.notna(df), None)

        chips_map = {}
        for _, row in df.iterrows():
            stock_id = str(row["stock_id"]).strip()
            chips_map[stock_id] = row.to_dict()

        _CHIPS_STATIC_MAP_CACHE = chips_map
        _CHIPS_STATIC_MAP_MTIME = mtime
        print(f"✅ 已載入籌碼靜態資料: {static_chips_csv_path}, 筆數={len(chips_map)}")
        return chips_map

    except Exception as e:
        print(f"❌ 讀取 AllStatic_Chips.csv 失敗: {e}")
        return {}


def load_news_static_map(static_news_csv_path=STATIC_NEWS_CSV_PATH, force_reload=False):
    global _NEWS_STATIC_MAP_CACHE, _NEWS_STATIC_MAP_MTIME

    try:
        if not os.path.exists(static_news_csv_path):
            print(f"⚠️ 找不到產業新聞靜態資料檔: {static_news_csv_path}")
            return {}

        mtime = os.path.getmtime(static_news_csv_path)
        if (not force_reload) and _NEWS_STATIC_MAP_CACHE is not None and _NEWS_STATIC_MAP_MTIME == mtime:
            return _NEWS_STATIC_MAP_CACHE

        df = pd.read_csv(static_news_csv_path,
                         encoding="utf-8-sig", dtype={"stock_id": str})
        df.columns = df.columns.str.strip()

        if "stock_id" not in df.columns:
            print(
                f"⚠️ AllStatic_news.csv 缺少 stock_id 欄位: {static_news_csv_path}")
            return {}

        df = df.where(pd.notna(df), None)

        news_map = {}
        for _, row in df.iterrows():
            stock_id = str(row["stock_id"]).strip()
            news_map[stock_id] = row.to_dict()

        _NEWS_STATIC_MAP_CACHE = news_map
        _NEWS_STATIC_MAP_MTIME = mtime
        print(f"✅ 已載入產業新聞靜態資料: {static_news_csv_path}, 筆數={len(news_map)}")
        return news_map

    except Exception as e:
        print(f"❌ 讀取 AllStatic_news.csv 失敗: {e}")
        return {}


def to_float_or_none(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return float(v)
    except Exception:
        return None


def to_int_or_none(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return int(v)
    except Exception:
        return None


def to_str_or_none(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    text = str(v).strip()
    return text if text and text.lower() not in {"nan", "none", "null"} else None


def round_float_or_none(v, ndigits=2):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    try:
        return float(round(float(v), ndigits))
    except Exception:
        return None


def date_text_or_none(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    text = str(v).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text[:10]


def build_recent_technical_fields(*rows):
    fields = {}
    for idx, row in enumerate(rows):
        suffix = f"t{idx}"
        date_text = date_text_or_none(row.get("date"))
        fields[f"date_{suffix}"] = date_text
        fields[f"kd_date_{suffix}"] = date_text
        fields[f"price_date_{suffix}"] = date_text
        fields[f"k_{suffix}"] = round_float_or_none(row.get("K"), 2)
        fields[f"d_{suffix}"] = round_float_or_none(row.get("D"), 2)
        fields[f"price_min_{suffix}"] = round_float_or_none(row.get("min"), 2)
        fields[f"price_max_{suffix}"] = round_float_or_none(row.get("max"), 2)
        fields[f"macd_hist_{suffix}"] = round_float_or_none(
            row.get("MACD_HIST") if "MACD_HIST" in row else row.get(
                "macd_hist"), 4
        )
        fields[f"rsi_{suffix}"] = round_float_or_none(
            row.get("RSI") if "RSI" in row else row.get("rsi"), 2
        )
    return fields


def process_stock(s, static_map=None, chips_map=None, news_map=None):
    stock_id = str(s["stock_id"])
    name = s["name"]
    custom_categories_text = category_text(
        s.get("custom_categories") or s.get(CUSTOM_CATEGORY_COLUMN) or ""
    )
    custom_category_list = split_category_text(custom_categories_text)

    base = {
        "name": name,
        "code": stock_id,
        "price": None,
        "chg": None,
        "chgPct": None,
        "amp": None,
        "sig": 0,
        "signal": "資料異常",
        "score": 0,
        "signal_text": "資料異常",
        "reason": "",
        "entry_note": "",
        "resistance_price": None,
        "support_price": None,
        "resistance_date": None,
        "support_date": None,
        "resistance_distance_pct": None,
        "support_distance_pct": None,
        "resistance_price2": None,
        "support_price2": None,
        "resistance_date2": None,
        "support_date2": None,
        "resistance_distance_pct2": None,
        "support_distance_pct2": None,
        "custom_categories": custom_categories_text,
        "custom_category_list": custom_category_list,
        CUSTOM_CATEGORY_COLUMN: custom_categories_text,
    }

    static_row = (static_map or {}).get(stock_id, {})
    chip_row = (chips_map or {}).get(stock_id, {})
    news_row = (news_map or {}).get(stock_id, {})

    try:
        df = get_stock_data(stock_id)

        if df is None:
            x = base.copy()
            x.update({
                "signal": "無資料",
                "signal_text": "查無資料",
                "reason": "get_stock_data 回傳 None",
            })
            x.update(_build_static_fields(static_row))
            x.update(_build_chip_fields(chip_row))
            x.update(_build_news_fields(news_row))
            return x

        if df.empty:
            x = base.copy()
            x.update({
                "signal": "無資料",
                "signal_text": "查無資料",
                "reason": "股價資料為空",
            })
            x.update(_build_static_fields(static_row))
            x.update(_build_chip_fields(chip_row))
            x.update(_build_news_fields(news_row))
            return x

        df = clean_ohlc_data(df)
        if df is None or df.empty:
            x = base.copy()
            x.update({
                "signal": "資料異常",
                "signal_text": "資料異常",
                "reason": "OHLC 價格資料皆為無效值",
            })
            x.update(_build_static_fields(static_row))
            x.update(_build_chip_fields(chip_row))
            x.update(_build_news_fields(news_row))
            return x

        if len(df) < 90:
            x = base.copy()
            x.update({
                "signal": "資料不足",
                "signal_text": "資料不足",
                "reason": f" {len(df)}/90筆",
            })
            x.update(_build_static_fields(static_row))
            x.update(_build_chip_fields(chip_row))
            x.update(_build_news_fields(news_row))
            return x

        df = add_indicators(df)
        latest, prev, prev2, prev3 = df.iloc[-1], df.iloc[-2], df.iloc[-3], df.iloc[-4]
        recent_technical_fields = build_recent_technical_fields(
            latest, prev, prev2, prev3)
        price_stats = get_price_60d_high_low(df)
        support_resistance = get_support_resistance_levels(df)
        max_price = latest["max"]
        min_price = latest["min"]
        chg = latest["close"] - prev["close"]

        chgPct = round((chg / prev["close"]) * 100, 2)
        chgamp = latest["max"] - latest["min"]
        amp = round((chgamp / prev["close"]) * 100, 2)

        try:
            ma_stats = get_MABias(df) or {}
        except Exception as e:
            print(f"❌ ma bias error {stock_id}: {e}")
            ma_stats = {}

        safe_ma_stats = {}
        for k2, v2 in ma_stats.items():
            if v2 is None or pd.isna(v2):
                safe_ma_stats[k2.lower()] = None
            else:
                safe_ma_stats[k2.lower()] = float(v2)

        k = float(latest["K"]) if pd.notna(latest["K"]) else None
        d = float(latest["D"]) if pd.notna(latest["D"]) else None
        prev_k = float(prev["K"]) if pd.notna(prev["K"]) else None
        prev_d = float(prev["D"]) if pd.notna(prev["D"]) else None
        prev2_k = float(prev2["K"]) if pd.notna(prev2["K"]) else None
        prev2_d = float(prev2["D"]) if pd.notna(prev2["D"]) else None

        kd_trend = get_kd_trend(
            df) or {"kd_3d_up": None, "kd_trend": None}
        bb_trend = get_bb_trend(
            df) or {"bb_3d_up": None, "bb_trend": None, "bb_score": None}
        k_trend = kd_trend.get("kd_trend")
        d_trend = None

        ma5 = latest["MA5"] if "MA5" in latest and pd.notna(
            latest["MA5"]) else None
        prev_ma5 = prev["MA5"] if "MA5" in prev and pd.notna(
            prev["MA5"]) else None
        ma20 = latest["MA20"] if "MA20" in latest and pd.notna(
            latest["MA20"]) else None
        prev_ma20 = prev["MA20"] if "MA20" in prev and pd.notna(
            prev["MA20"]) else None
        ma60 = latest["MA60"] if "MA60" in latest and pd.notna(
            latest["MA60"]) else None
        prev_ma60 = prev["MA60"] if "MA60" in prev and pd.notna(
            prev["MA60"]) else None
        macd_hist = latest["MACD_HIST"] if "MACD_HIST" in latest and pd.notna(
            latest["MACD_HIST"]) else None
        prev_macd_hist = prev["MACD_HIST"] if "MACD_HIST" in prev and pd.notna(
            prev["MACD_HIST"]) else None
        prev2_macd_hist = prev2["MACD_HIST"] if "MACD_HIST" in prev2 and pd.notna(
            prev2["MACD_HIST"]) else None
        rsi = latest["RSI"] if "RSI" in latest and pd.notna(
            latest["RSI"]) else None
        prev_rsi = prev["RSI"] if "RSI" in prev and pd.notna(
            prev["RSI"]) else None
        prev2_rsi = prev2["RSI"] if "RSI" in prev2 and pd.notna(
            prev2["RSI"]) else None
        close = latest["close"]
        prev_close = prev["close"]

        volume = latest.get("volume", None)
        prev_volume = prev.get("volume", None)
        prev2_volume = prev2.get("volume", None)
        volume_ratio = None
        volume_add = None

        if pd.notna(volume) and pd.notna(prev_volume) and prev_volume > 0:
            volume_ratio = round((volume / prev_volume - 1) * 100, 2)
            volume_add = int(volume - prev_volume)

        bb_upper = latest["BB_upper"] if "BB_upper" in latest else None
        bb_lower = latest["BB_lower"] if "BB_lower" in latest else None

        def _bb_pct_from_row(row):
            upper = row["BB_upper"] if "BB_upper" in row else None
            lower = row["BB_lower"] if "BB_lower" in row else None
            row_close = row["close"] if "close" in row else None
            if pd.notna(upper) and pd.notna(lower) and upper != lower and pd.notna(row_close):
                return float(round((row_close - lower) / (upper - lower) * 100, 1))
            return None

        bb_pct = None
        if pd.notna(bb_upper) and pd.notna(bb_lower) and bb_upper != bb_lower:
            bb_pct = round((close - bb_lower) / (bb_upper - bb_lower) * 100, 1)
            bb_pct = float(bb_pct)
        bb_pct_t1 = _bb_pct_from_row(prev)
        bb_pct_t2 = _bb_pct_from_row(prev2)
        bb_pct_window = df.tail(90).apply(_bb_pct_from_row, axis=1)
        bb_pct_window = pd.to_numeric(bb_pct_window, errors="coerce")
        bb_pct_90d_low = float(round(bb_pct_window.min(), 1)
                               ) if bb_pct_window.notna().any() else None
        bb_pct_90d_high = float(
            round(bb_pct_window.max(), 1)) if bb_pct_window.notna().any() else None

        def _bias_from_row(row, period):
            bias_col = f"BIAS{period}"
            if bias_col in row and pd.notna(row[bias_col]):
                return float(round(row[bias_col], 2))
            ma_col = f"MA{period}"
            row_close = row.get("close") if "close" in row else None
            ma_value = row.get(ma_col) if ma_col in row else None
            if pd.notna(row_close) and pd.notna(ma_value) and ma_value != 0:
                return float(round((row_close - ma_value) / ma_value * 100, 2))
            return None

        bias5 = safe_ma_stats.get("bias5")
        bias20 = safe_ma_stats.get("bias20")
        bias60 = safe_ma_stats.get("bias60")
        bias5_t1 = _bias_from_row(prev, 5)
        bias20_t1 = _bias_from_row(prev, 20)
        bias60_t1 = _bias_from_row(prev, 60)
        bias5_t2 = _bias_from_row(prev2, 5)
        bias20_t2 = _bias_from_row(prev2, 20)
        bias60_t2 = _bias_from_row(prev2, 60)
        bias5_min = safe_ma_stats.get(
            "bias5_60d_low") or safe_ma_stats.get("bias5_min")
        bias5_max = safe_ma_stats.get(
            "bias5_60d_high") or safe_ma_stats.get("bias5_max")
        bias20_min = safe_ma_stats.get(
            "bias20_60d_low") or safe_ma_stats.get("bias20_min")
        bias20_max = safe_ma_stats.get(
            "bias20_60d_high") or safe_ma_stats.get("bias20_max")
        bias60_min = safe_ma_stats.get(
            "bias60_60d_low") or safe_ma_stats.get("bias60_min")
        bias60_max = safe_ma_stats.get(
            "bias60_60d_high") or safe_ma_stats.get("bias60_max")

        static_fields = _build_static_fields(static_row)
        chip_fields = _build_chip_fields(chip_row)
        news_fields = _build_news_fields(news_row)
        merged_static_fields = {**static_fields, **chip_fields, **news_fields}

        try:
            signal_res = get_tech_signal(
                close=close,
                chgPct=chgPct,
                amp=amp,
                volume=volume,
                prev_volume=prev_volume,
                prev2_volume=prev2_volume,
                k=k,
                d=d,
                prev_k=prev_k,
                prev_d=prev_d,
                k_trend=k_trend,
                d_trend=d_trend,
                bb_pct=bb_pct,
                bias6=bias5,
                bias18=bias20,
                bias50=bias60,
                bias6_min=bias5_min,
                bias6_max=bias5_max,
                bias18_min=bias20_min,
                bias18_max=bias20_max,
                bias50_min=bias60_min,
                bias50_max=bias60_max,
                ma18=ma20,
                prev_ma18=prev_ma20,
                prev_close=prev_close,
                ma6=ma5,
                prev_ma6=prev_ma5,
                ma50=ma60,
                prev_ma50=prev_ma60,
                macd_hist=macd_hist,
                prev_macd_hist=prev_macd_hist,
                chip_signal_state=merged_static_fields.get(
                    "chip_signal_state"),
                chip_signal_text=merged_static_fields.get("chip_signal_text"),
                chip_concentration_score=merged_static_fields.get(
                    "chip_concentration_score"),
                main_force_score=merged_static_fields.get("main_force_score"),
                broker_diff_score=merged_static_fields.get(
                    "broker_diff_score"),
                chip_concentration_pct=merged_static_fields.get(
                    "chip_concentration_pct"),
                chip_trend_days=merged_static_fields.get("chip_trend_days"),
                chip_concentration_threshold=merged_static_fields.get(
                    "chip_concentration_threshold"),
            ) or {"signal": "等待觀察", "reason": "", "signal_text": "等待觀察"}
        except Exception as e:
            print(f"❌ signal error {stock_id}: {e}")
            signal_res = {"signal": "等待觀察",
                          "reason": f"signal error: {e}", "signal_text": "等待觀察"}

        signal = signal_res.get("signal", "等待觀察")
        reason = signal_res.get("reason", "")
        signal_text = signal_res.get("signal_text", "等待觀察")
        position_zone = signal_res.get("position_zone")
        price_volume_state = signal_res.get("price_volume_state")
        trend_stage = signal_res.get("trend_stage")

        sig = 1 if signal == "買進" else -1 if signal == "賣出" else 0

        kd_buy = bool(None not in (k, d, prev_k, prev_d)
                      and (prev_k <= prev_d) and (k > d))
        ma20_break = bool(
            ma20 is not None and prev_ma20 is not None and prev_close <= prev_ma20 and close > ma20
        )

        entry_note = ""
        if "短線過熱" in reason or "不宜追價" in reason:
            entry_note = "不追價"
        elif signal == "買進" and kd_buy and ma20_break and k is not None and k < 35:
            entry_note = "抄底"
        elif signal == "買進" and ma20_break and chgPct >= 3:
            entry_note = "追漲"

        margin_score = calc_margin_score(
            static_fields.get("gross_margin"),
            static_fields.get("operating_margin"),
            static_fields.get("net_margin"),
        )
        eps_score = calc_eps_score(
            static_fields.get("eps_Y"),
            static_fields.get("eps_ttm"),
        )
        trend_score = calc_trend_score(
            static_fields.get("gross_margin_qoq"),
            static_fields.get("gross_margin_yoy_diff"),
            static_fields.get("net_margin_qoq"),
            static_fields.get("net_margin_yoy_diff"),
        )
        score = round(margin_score * 0.4 + eps_score *
                      0.3 + trend_score * 0.3, 2)

        def to_py(v):
            if isinstance(v, np.bool_):
                return bool(v)
            if isinstance(v, np.integer):
                return int(v)
            if isinstance(v, np.floating):
                return float(v)
            if isinstance(v, (list, tuple, set, dict)):
                return v
            try:
                is_na = pd.isna(v)
            except Exception:
                return v
            if isinstance(is_na, (np.ndarray, list, tuple)):
                return v
            if bool(is_na):
                return None
            return v

        result = {
            "name": name,
            "code": stock_id,
            "price": float(round(close, 2)),
            "price_max": float(round(max_price, 2)),
            "price_min": float(round(min_price, 2)),
            "margin_cost_line": None,
            "margin_cost_line_t0": None,
            "margin_cost_line_t1": None,
            "margin_cost_line_t2": None,
            "price_60d_high": price_stats.get("price_60d_high"),
            "price_60d_low": price_stats.get("price_60d_low"),
            "resistance_price": support_resistance.get("resistance_price"),
            "support_price": support_resistance.get("support_price"),
            "resistance_date": support_resistance.get("resistance_date"),
            "support_date": support_resistance.get("support_date"),
            "resistance_distance_pct": support_resistance.get("resistance_distance_pct"),
            "support_distance_pct": support_resistance.get("support_distance_pct"),
            "resistance_touch_count": support_resistance.get("resistance_touch_count"),
            "support_touch_count": support_resistance.get("support_touch_count"),
            "resistance_price2": support_resistance.get("resistance_price2"),
            "support_price2": support_resistance.get("support_price2"),
            "resistance_date2": support_resistance.get("resistance_date2"),
            "support_date2": support_resistance.get("support_date2"),
            "resistance_distance_pct2": support_resistance.get("resistance_distance_pct2"),
            "support_distance_pct2": support_resistance.get("support_distance_pct2"),
            "resistance_touch_count2": support_resistance.get("resistance_touch_count2"),
            "support_touch_count2": support_resistance.get("support_touch_count2"),
            "chg": float(round(chg, 2)),
            "chgPct": float(chgPct),
            "amp": float(amp),

            **static_fields,
            **chip_fields,
            **news_fields,
            **recent_technical_fields,

            "k": float(round(k, 2)) if k is not None else None,
            "d": float(round(d, 2)) if d is not None else None,
            "prev_k": float(round(prev_k, 2)) if prev_k is not None else None,
            "prev_d": float(round(prev_d, 2)) if prev_d is not None else None,
            "prev2_k": float(round(prev2_k, 2)) if prev2_k is not None else None,
            "prev2_d": float(round(prev2_d, 2)) if prev2_d is not None else None,
            "kd_3d_up": kd_trend.get("kd_3d_up"),
            "kd_trend": kd_trend.get("kd_trend"),
            "k_trend": k_trend,
            "d_trend": d_trend,
            "ma20": float(round(ma20, 2)) if ma20 is not None else None,
            "prev_ma20": float(round(prev_ma20, 2)) if prev_ma20 is not None else None,
            "ma20_break": bool(ma20_break),
            "ma18": float(round(ma20, 2)) if ma20 is not None else None,
            "prev_ma18": float(round(prev_ma20, 2)) if prev_ma20 is not None else None,
            "ma18_break": bool(ma20_break),
            "kd_buy": bool(kd_buy),
            "bb_pct": float(bb_pct) if bb_pct is not None else None,
            "bb_pct_t0": float(bb_pct) if bb_pct is not None else None,
            "bb_pct_t1": float(bb_pct_t1) if bb_pct_t1 is not None else None,
            "bb_pct_t2": float(bb_pct_t2) if bb_pct_t2 is not None else None,
            "bb_pct_90d_low": bb_pct_90d_low,
            "bb_pct_90d_high": bb_pct_90d_high,
            "bb_upper": float(round(bb_upper, 2)) if bb_upper is not None and pd.notna(bb_upper) else None,
            "bb_lower": float(round(bb_lower, 2)) if bb_lower is not None and pd.notna(bb_lower) else None,
            "bb_3d_up": bb_trend.get("bb_3d_up"),
            "bb_trend": bb_trend.get("bb_trend"),
            "bb_score": bb_trend.get("bb_score"),
            "volume": int(round(volume, 0)) if pd.notna(volume) else None,
            "prev_volume": int(round(prev_volume, 0)) if pd.notna(prev_volume) else None,
            "prev2_volume": int(round(prev2_volume, 0)) if pd.notna(prev2_volume) else None,
            "volume_ratio": float(volume_ratio) if volume_ratio is not None else None,
            "volume_add": volume_add if volume_add is not None else None,

            "ma5": float(round(ma5, 2)) if ma5 is not None else safe_ma_stats.get("ma5"),
            "prev_ma5": float(round(prev_ma5, 2)) if prev_ma5 is not None else None,
            "bias5": bias5,
            "bias5_t0": bias5,
            "bias5_t1": bias5_t1,
            "bias5_t2": bias5_t2,
            "bias5_min": bias5_min,
            "bias5_max": bias5_max,
            "bias5_60d_low": bias5_min,
            "bias5_60d_high": bias5_max,
            "ma6": float(round(ma5, 2)) if ma5 is not None else safe_ma_stats.get("ma5"),
            "prev_ma6": float(round(prev_ma5, 2)) if prev_ma5 is not None else None,
            "bias6": bias5,
            "bias6_min": bias5_min,
            "bias6_max": bias5_max,
            "bias20": bias20,
            "bias20_t0": bias20,
            "bias20_t1": bias20_t1,
            "bias20_t2": bias20_t2,
            "bias20_min": bias20_min,
            "bias20_max": bias20_max,
            "bias20_60d_low": bias20_min,
            "bias20_60d_high": bias20_max,
            "bias18": bias20,
            "bias18_min": bias20_min,
            "bias18_max": bias20_max,
            "ma60": float(round(ma60, 2)) if ma60 is not None else safe_ma_stats.get("ma60"),
            "prev_ma60": float(round(prev_ma60, 2)) if prev_ma60 is not None else None,
            "ma50": float(round(ma60, 2)) if ma60 is not None else safe_ma_stats.get("ma60"),
            "prev_ma50": float(round(prev_ma60, 2)) if prev_ma60 is not None else None,
            "macd_hist": float(round(macd_hist, 4)) if macd_hist is not None else None,
            "macd_hist_t0": float(round(macd_hist, 4)) if macd_hist is not None else None,
            "macd_hist_t1": float(round(prev_macd_hist, 4)) if prev_macd_hist is not None else None,
            "macd_hist_t2": float(round(prev2_macd_hist, 4)) if prev2_macd_hist is not None else None,
            "prev_macd_hist": float(round(prev_macd_hist, 4)) if prev_macd_hist is not None else None,
            "macd_hist_delta": float(round(macd_hist - prev_macd_hist, 4)) if macd_hist is not None and prev_macd_hist is not None else None,
            "rsi": float(round(rsi, 2)) if rsi is not None else None,
            "rsi_t0": float(round(rsi, 2)) if rsi is not None else None,
            "rsi_t1": float(round(prev_rsi, 2)) if prev_rsi is not None else None,
            "rsi_t2": float(round(prev2_rsi, 2)) if prev2_rsi is not None else None,
            "bias60": bias60,
            "bias60_t0": bias60,
            "bias60_t1": bias60_t1,
            "bias60_t2": bias60_t2,
            "bias60_min": bias60_min,
            "bias60_max": bias60_max,
            "bias60_60d_low": bias60_min,
            "bias60_60d_high": bias60_max,
            "bias50": bias60,
            "bias50_min": bias60_min,
            "bias50_max": bias60_max,

            "sig": int(sig),
            "signal": signal,
            "score": float(score),
            "signal_text": signal_text,
            "reason": reason,
            "position_zone": position_zone,
            "price_volume_state": price_volume_state,
            "trend_stage": trend_stage,
            "entry_note": entry_note,
            "custom_categories": custom_categories_text,
            "custom_category_list": custom_category_list,
            CUSTOM_CATEGORY_COLUMN: custom_categories_text,
        }
        return {k: to_py(v) for k, v in result.items()}

    except RuntimeError:
        raise
    except Exception as e:
        print(f"❌ process error {stock_id}: {e}")
        x = base.copy()
        x.update(_build_static_fields(static_row))
        x.update(_build_chip_fields(chip_row))
        x.update(_build_news_fields(news_row))
        x.update({
            "signal": "資料異常",
            "signal_text": "資料異常",
            "reason": f"process error: {e}",
        })
        return x


def _build_static_fields(static_row):
    period_start = static_row.get("period_start")
    if period_start is None or (np.isscalar(period_start) and pd.isna(period_start)):
        period_start = static_row.get("disposition_period_start")

    period_end = static_row.get("period_end")
    if period_end is None or (np.isscalar(period_end) and pd.isna(period_end)):
        period_end = static_row.get("disposition_period_end")

    return {
        "eps_Y": to_float_or_none(static_row.get("eps_Y")),
        "eps_Y_quarters": to_int_or_none(static_row.get("eps_Y_quarters")),
        "eps_ttm": to_float_or_none(static_row.get("eps_ttm")),
        "roe_last_year": to_float_or_none(static_row.get("roe_last_year")),
        "roe_ttm": to_float_or_none(static_row.get("roe_ttm")),
        "per_Y": to_float_or_none(static_row.get("per_Y")),
        "per_ttm": to_float_or_none(static_row.get("per_ttm")),
        "rev": to_float_or_none(static_row.get("rev")),
        "rev_mom": to_float_or_none(static_row.get("rev_mom")),
        "rev_qoq": to_float_or_none(static_row.get("rev_qoq")),
        "rev_yoy": to_float_or_none(static_row.get("rev_yoy")),

        "gross_margin": to_float_or_none(static_row.get("gross_margin")),
        "gross_margin_qoq": to_float_or_none(static_row.get("gross_margin_qoq")),
        "gross_margin_yoy_diff": to_float_or_none(static_row.get("gross_margin_yoy_diff")),

        "operating_margin": to_float_or_none(static_row.get("operating_margin")),
        "operating_margin_qoq": to_float_or_none(static_row.get("operating_margin_qoq")),
        "operating_margin_yoy_diff": to_float_or_none(static_row.get("operating_margin_yoy_diff")),

        "net_margin": to_float_or_none(static_row.get("net_margin")),
        "net_margin_qoq": to_float_or_none(static_row.get("net_margin_qoq")),
        "net_margin_yoy_diff": to_float_or_none(static_row.get("net_margin_yoy_diff")),

        "per_latest": to_float_or_none(static_row.get("per_latest")),
        "per_60d_high": to_float_or_none(static_row.get("per_60d_high")),
        "per_60d_low": to_float_or_none(static_row.get("per_60d_low")),
        "pbr_latest": to_float_or_none(static_row.get("pbr_latest")),
        "pbr_60d_high": to_float_or_none(static_row.get("pbr_60d_high")),
        "pbr_60d_low": to_float_or_none(static_row.get("pbr_60d_low")),
        "yield_value": to_float_or_none(static_row.get("yield_value")),

        "period_start": to_str_or_none(period_start),
        "period_end": to_str_or_none(period_end),
        "disposition_period_start": to_str_or_none(period_start),
        "disposition_period_end": to_str_or_none(period_end),
    }


def _build_chip_fields(chip_row):
    latest_date = to_str_or_none(chip_row.get("chip_latest_date"))
    latest_short_margin_ratio = to_float_or_none(
        chip_row.get("short_margin_ratio_pct"))
    latest_concentration = to_float_or_none(
        chip_row.get("chip_concentration_pct"))
    latest_main_force = to_int_or_none(chip_row.get("main_force_net"))
    latest_broker_diff = to_int_or_none(chip_row.get("broker_diff"))

    t0_short_margin_ratio = to_float_or_none(
        chip_row.get("short_margin_ratio_pct_t0"))
    t0_concentration = to_float_or_none(
        chip_row.get("chip_concentration_pct_t0"))
    t0_main_force = to_int_or_none(chip_row.get("main_force_net_t0"))
    t0_broker_diff = to_int_or_none(chip_row.get("broker_diff_t0"))

    return {
        "chip_trend_days": to_int_or_none(chip_row.get("chip_trend_days")),
        "chip_concentration_threshold": to_float_or_none(chip_row.get("chip_concentration_threshold")),
        "chip_latest_date": latest_date,
        "chip_available_days": to_int_or_none(chip_row.get("chip_available_days")),
        "short_margin_ratio_pct": latest_short_margin_ratio,
        "short_margin_ratio_score": to_float_or_none(chip_row.get("short_margin_ratio_score")),
        "chip_concentration_pct": latest_concentration,
        "chip_concentration_score": to_float_or_none(chip_row.get("chip_concentration_score")),
        "main_force_net": latest_main_force,
        "main_force_score": to_float_or_none(chip_row.get("main_force_score")),
        "broker_diff": latest_broker_diff,
        "broker_diff_score": to_float_or_none(chip_row.get("broker_diff_score")),
        "margin_cost_line": to_float_or_none(chip_row.get("margin_cost_line") or chip_row.get("margin_cost")),
        "margin_cost_line_t0": to_float_or_none(chip_row.get("margin_cost_line_t0") or chip_row.get("margin_cost_t0") or chip_row.get("margin_cost_line") or chip_row.get("margin_cost")),
        "margin_cost_line_t1": to_float_or_none(chip_row.get("margin_cost_line_t1") or chip_row.get("margin_cost_t1")),
        "margin_cost_line_t2": to_float_or_none(chip_row.get("margin_cost_line_t2") or chip_row.get("margin_cost_t2")),
        "foreign_investor_net": to_int_or_none(chip_row.get("foreign_investor_net")),
        "foreign_investor_net_score": to_float_or_none(chip_row.get("foreign_investor_net_score")),
        "investment_trust_net": to_int_or_none(chip_row.get("investment_trust_net")),
        "investment_trust_net_score": to_float_or_none(chip_row.get("investment_trust_net_score")),
        "dealer_net": to_int_or_none(chip_row.get("dealer_net")),
        "dealer_net_score": to_float_or_none(chip_row.get("dealer_net_score")),
        "institutional_total_net": to_int_or_none(chip_row.get("institutional_total_net")),
        "institutional_total_net_score": to_float_or_none(chip_row.get("institutional_total_net_score")),

        "main_force_net_5d": to_int_or_none(chip_row.get("main_force_net_5d")),
        "total_volume_5d": to_int_or_none(chip_row.get("total_volume_5d")),
        "main_force_buy_rate_5d_pct": to_float_or_none(chip_row.get("main_force_buy_rate_5d_pct")),
        "chip_concentration_avg_5d": to_float_or_none(chip_row.get("chip_concentration_avg_5d")),
        "chip_concentration_change_5d": to_float_or_none(chip_row.get("chip_concentration_change_5d")),
        "broker_diff_avg_5d": to_float_or_none(chip_row.get("broker_diff_avg_5d")),

        "chip_date_t0": to_str_or_none(chip_row.get("chip_date_t0")) or latest_date,
        "chip_date_t1": to_str_or_none(chip_row.get("chip_date_t1")),
        "chip_date_t2": to_str_or_none(chip_row.get("chip_date_t2")),
        "short_margin_ratio_pct_t0": t0_short_margin_ratio,
        "short_margin_ratio_pct_t1": to_float_or_none(chip_row.get("short_margin_ratio_pct_t1")),
        "short_margin_ratio_pct_t2": to_float_or_none(chip_row.get("short_margin_ratio_pct_t2")),
        "chip_concentration_pct_t0": t0_concentration,
        "chip_concentration_pct_t1": to_float_or_none(chip_row.get("chip_concentration_pct_t1")),
        "chip_concentration_pct_t2": to_float_or_none(chip_row.get("chip_concentration_pct_t2")),
        "main_force_net_t0": t0_main_force,
        "main_force_net_t1": to_int_or_none(chip_row.get("main_force_net_t1")),
        "main_force_net_t2": to_int_or_none(chip_row.get("main_force_net_t2")),
        "broker_diff_t0": t0_broker_diff,
        "broker_diff_t1": to_int_or_none(chip_row.get("broker_diff_t1")),
        "broker_diff_t2": to_int_or_none(chip_row.get("broker_diff_t2")),
        "foreign_investor_net_t0": to_int_or_none(chip_row.get("foreign_investor_net_t0")),
        "foreign_investor_net_t1": to_int_or_none(chip_row.get("foreign_investor_net_t1")),
        "foreign_investor_net_t2": to_int_or_none(chip_row.get("foreign_investor_net_t2")),
        "investment_trust_net_t0": to_int_or_none(chip_row.get("investment_trust_net_t0")),
        "investment_trust_net_t1": to_int_or_none(chip_row.get("investment_trust_net_t1")),
        "investment_trust_net_t2": to_int_or_none(chip_row.get("investment_trust_net_t2")),
        "dealer_net_t0": to_int_or_none(chip_row.get("dealer_net_t0")),
        "dealer_net_t1": to_int_or_none(chip_row.get("dealer_net_t1")),
        "dealer_net_t2": to_int_or_none(chip_row.get("dealer_net_t2")),
        "institutional_total_net_t0": to_int_or_none(chip_row.get("institutional_total_net_t0")),
        "institutional_total_net_t1": to_int_or_none(chip_row.get("institutional_total_net_t1")),
        "institutional_total_net_t2": to_int_or_none(chip_row.get("institutional_total_net_t2")),

        "chip_signal_state": to_str_or_none(chip_row.get("chip_signal_state")),
        "chip_signal_text": to_str_or_none(chip_row.get("chip_signal_text")),
        "chips_status": to_str_or_none(chip_row.get("chips_status")),
        "chips_reason": to_str_or_none(chip_row.get("chips_reason")),
        "chips_updated_at": to_str_or_none(chip_row.get("chips_updated_at")),
    }


def _build_news_fields(news_row):
    return {
        "industry_summary": to_str_or_none(
            news_row.get("產業")
            or news_row.get("industry_summary")
            or news_row.get("industry")
            or news_row.get("news_industry")
        ),
        "news_summary": to_str_or_none(
            news_row.get("新聞")
            or news_row.get("news_summary")
            or news_row.get("news")
            or news_row.get("news_keywords")
        ),
    }


def get_full_stock_analysis(stock_list, static_map=None, chips_map=None, news_map=None):
    results = []
    if static_map is None:
        static_map = load_static_map()
    if chips_map is None:
        chips_map = load_chips_static_map()
    if news_map is None:
        news_map = load_news_static_map()

    for i, s in enumerate(stock_list, 1):
        #   print(f"處理中 {i}/{len(stock_list)}: {s}")
        data = process_stock(s, static_map=static_map,
                             chips_map=chips_map, news_map=news_map)
        results.append(data)

        if data.get("signal") in ("無資料", "資料不足", "資料異常"):
            print(f"⚠️ 保留異常資料: {s} -> {data.get('reason')}")

    return results
