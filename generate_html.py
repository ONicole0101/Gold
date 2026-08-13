import os
from datetime import datetime, timedelta

import pandas as pd
from jinja2 import Template

import config
from custom_categories import CUSTOM_CATEGORY_COLUMN, category_text, split_category_text
from data_sources import (
    get_finmind_user_info,
    get_latest_convertible_bond_overview,
)
from index_data import get_market_index_rows
from main import get_full_stock_analysis
from stock_service import load_chips_static_map, load_news_static_map, load_static_map


TECH_COLUMNS = [
    {"key": "position_zone", "label": "位階"},
    {"key": "price_volume_state", "label": "價量"},
    {"key": "trend_stage", "label": "趨勢階段"},
    {"key": "ma5", "label": "MA5"},
    {"key": "ma20", "label": "MA20"},
    {"key": "ma60", "label": "MA60"},
    {"key": "macd_hist", "label": "MACD柱"},
]


def enrich_html_fields(results):
    """補上 HTML 可直接顯示的技術摘要欄位。

    template.html 若要顯示新增欄位，可直接讀：
    tech_summary / position_zone / price_volume_state / trend_stage / ma5 / ma20 / ma60 / macd_hist。
    """
    out = []
    for item in results:
        if not item:
            continue
        x = dict(item)
        parts = []
        for key in ("position_zone", "price_volume_state", "trend_stage"):
            val = x.get(key)
            if val not in (None, ""):
                parts.append(str(val))
        if x.get("macd_hist") is not None:
            parts.append(f"MACD柱 {x.get('macd_hist')}")
        x["tech_summary"] = " / ".join(
            parts) if parts else x.get("signal_text", "")
        out.append(x)
    return out


def get_finmind_usage():
    info = get_finmind_user_info(write_log=False, source="generate_html")
    used = int(info.get("user_count") or 0)
    limit = int(info.get("api_request_limit") or 0)
    remain = info.get("remain")
    if remain is None and limit is not None and used is not None:
        remain = max(int(limit) - int(used), 0)
    else:
        remain = 0 if remain is None else int(remain)
    print(f"FinMind usage: {used}/{limit}, remain={remain}")
    return used, limit, remain


def normalize_stock_df(df):
    df = df.copy()
    df.columns = df.columns.str.strip()
    df = df.rename(columns={"Ticker": "stock_id", "Name": "name"})
    if "stock_id" not in df.columns:
        raise ValueError("CSV missing Ticker or stock_id column")
    if "name" not in df.columns:
        df["name"] = ""
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["name"] = df["name"].fillna("").astype(str).str.strip()
    if CUSTOM_CATEGORY_COLUMN not in df.columns:
        df[CUSTOM_CATEGORY_COLUMN] = ""
    else:
        df[CUSTOM_CATEGORY_COLUMN] = df[CUSTOM_CATEGORY_COLUMN].apply(
            category_text)
    return df[~df["stock_id"].str.lower().isin({"", "nan", "none", "null"})]


def collect_category_options(stocks):
    options = set()
    for stock in stocks or []:
        if not isinstance(stock, dict):
            continue
        values = stock.get("custom_category_list") or split_category_text(
            stock.get("custom_categories") or stock.get(CUSTOM_CATEGORY_COLUMN)
        )
        for value in values or []:
            text = category_text(value)
            if text:
                options.update(split_category_text(text))
    return sorted(options)


def _cb_num(value):
    try:
        if value in (None, "") or pd.isna(value):
            return None
        number = float(value)
        return number if pd.notna(number) else None
    except (TypeError, ValueError):
        return None


def _cb_date(value):
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    except Exception:
        return None


def _match_cb_stock_id(cb_id, stock_ids):
    """Match a CB issue code to an issuer already present in the report."""
    code = str(cb_id or "").strip()
    candidates = [sid for sid in stock_ids if code.startswith(sid)]
    if not candidates:
        return ""
    # Prefer the longest exact prefix so non-four-digit security identifiers
    # do not get accidentally matched to a shorter code.
    return max(candidates, key=len)


def _cb_pressure_label(item):
    """Transparent screening label, not a forecast of actual selling."""
    premium = item.get("cb_premium_pct")
    distance = item.get("cb_distance_to_break_even_pct")
    moneyness = item.get("cb_moneyness_pct")
    pressure_days = item.get("cb_pressure_days")
    early_redemption = item.get("cb_early_redemption_active")

    conversion_economic = (
        early_redemption
        or (premium is not None and premium <= 3)
        or (distance is not None and distance >= 0)
    )
    large_supply = pressure_days is not None and pressure_days >= 5
    if conversion_economic and (large_supply or early_redemption):
        return "高關注", 3
    if (
        early_redemption
        or (moneyness is not None and moneyness >= 0)
        or (distance is not None and distance >= -10)
    ):
        return "中關注", 2
    return "低關注", 1


def _cb_metric_snapshot(raw, fallback_price, avg_volume, volume_basis, as_of_value):
    """Calculate one issue's comparable metrics for one snapshot date."""
    conversion_price = _cb_num(raw.get("ConversionPrice"))
    cb_price = _cb_num(raw.get("ReferencePrice"))
    underlying_price = (
        _cb_num(raw.get("PriceOfUnderlyingStock")) or fallback_price
    )
    issuance = _cb_num(raw.get("IssuanceAmount"))
    outstanding = _cb_num(raw.get("OutstandingAmount"))
    if not conversion_price or conversion_price <= 0:
        return None

    parity = (
        100 * underlying_price / conversion_price
        if underlying_price is not None else None
    )
    premium = (
        (cb_price / parity - 1) * 100
        if cb_price is not None and parity not in (None, 0) else None
    )
    break_even = (
        conversion_price * cb_price / 100
        if cb_price is not None else None
    )
    distance = (
        (underlying_price / break_even - 1) * 100
        if underlying_price is not None and break_even not in (None, 0)
        else None
    )
    moneyness = (
        (underlying_price / conversion_price - 1) * 100
        if underlying_price is not None else None
    )
    remaining_pct = (
        outstanding / issuance * 100
        if outstanding is not None and issuance not in (None, 0) else None
    )
    potential_shares = (
        outstanding / conversion_price if outstanding is not None else None
    )
    potential_lots = (
        potential_shares / 1000 if potential_shares is not None else None
    )
    pressure_days = (
        potential_lots / avg_volume
        if potential_lots is not None and avg_volume not in (None, 0) else None
    )
    as_of = _cb_date(as_of_value or raw.get("date"))
    early_start = _cb_date(raw.get("InitialDateOfEarlyRedemption"))
    early_end = _cb_date(raw.get("DueDateOfEarlyRedemption"))
    early_active = bool(
        as_of and early_start and early_end and early_start <= as_of <= early_end
    )
    metric = {
        "date": str(raw.get("date") or as_of_value or ""),
        "cb_price": cb_price,
        "cb_underlying_price": underlying_price,
        "cb_conversion_price": conversion_price,
        "cb_moneyness_pct": round(moneyness, 2) if moneyness is not None else None,
        "cb_parity": round(parity, 2) if parity is not None else None,
        "cb_premium_pct": round(premium, 2) if premium is not None else None,
        "cb_break_even_price": round(break_even, 2) if break_even is not None else None,
        "cb_distance_to_break_even_pct": round(distance, 2) if distance is not None else None,
        "cb_outstanding_amount": outstanding,
        "cb_remaining_pct": round(remaining_pct, 2) if remaining_pct is not None else None,
        "cb_potential_lots": round(potential_lots) if potential_lots is not None else None,
        "cb_pressure_days": round(pressure_days, 2) if pressure_days is not None else None,
        "cb_volume_basis": volume_basis,
        "cb_early_redemption_active": early_active,
    }
    label, rank = _cb_pressure_label(metric)
    metric["cb_pressure_label"] = label
    metric["cb_pressure_rank"] = rank
    return metric


def _cb_overall_trend(history):
    """Summarize pressure direction using non-duplicative core indicators."""
    if len(history or []) < 2:
        return "資料不足", 0
    current, previous = history[0], history[1]
    votes = []

    def vote(key, pressure_when_up=True, tolerance=0.01):
        now = _cb_num(current.get(key))
        before = _cb_num(previous.get(key))
        if now is None or before is None or abs(now - before) <= tolerance:
            return
        direction = 1 if now > before else -1
        votes.append(direction if pressure_when_up else -direction)

    # Premium falling, distance-to-break-even rising and supply-days rising
    # each contribute one vote.  Moneyness/parity are intentionally excluded
    # because they duplicate the distance signal.
    vote("cb_premium_pct", pressure_when_up=False)
    vote("cb_distance_to_break_even_pct", pressure_when_up=True)
    vote("cb_pressure_days", pressure_when_up=True)
    rank_now = _cb_num(current.get("cb_pressure_rank"))
    rank_before = _cb_num(previous.get("cb_pressure_rank"))
    if rank_now is not None and rank_before is not None and rank_now != rank_before:
        votes.append(1 if rank_now > rank_before else -1)

    score = sum(votes)
    if score > 0:
        return "壓力增加", 1
    if score < 0:
        return "壓力下降", -1
    return "大致持平", 0


def attach_cb_analysis(results, snapshot):
    """Attach issue-level CB conversion and supply-pressure metrics."""
    output = [dict(item) for item in results if item]
    stock_ids = {
        str(item.get("code") or item.get("stock_id") or "").strip()
        for item in output
    }
    stock_ids.discard("")
    raw_by_stock = {sid: [] for sid in stock_ids}
    daily_by_cb = {}
    monthly_by_cb = {}

    daily_snapshots = snapshot.get("daily_snapshots") or [
        {"date": snapshot.get("as_of"), "rows": snapshot.get("rows") or []}
    ]
    for snap in daily_snapshots:
        for raw in snap.get("rows") or []:
            cb_id = str(raw.get("cb_id") or "")
            if cb_id:
                daily_by_cb.setdefault(cb_id, []).append(raw)

    for snap in snapshot.get("monthly_snapshots") or []:
        for raw in snap.get("rows") or []:
            cb_id = str(raw.get("cb_id") or "")
            if cb_id:
                monthly_by_cb.setdefault(cb_id, []).append(raw)

    for raw in snapshot.get("rows") or []:
        sid = _match_cb_stock_id(raw.get("cb_id"), stock_ids)
        if sid:
            raw_by_stock.setdefault(sid, []).append(raw)

    as_of = _cb_date(snapshot.get("as_of"))
    for stock in output:
        sid = str(stock.get("code") or stock.get("stock_id") or "").strip()
        items = []
        total_volume_20d = _cb_num(stock.get("total_volume_20d"))
        available_days = _cb_num(stock.get("chip_available_days"))
        avg_volume_20d = None
        volume_basis = ""
        if total_volume_20d is not None and total_volume_20d > 0:
            denominator = min(max(int(available_days or 20), 1), 20)
            avg_volume_20d = total_volume_20d / denominator
            volume_basis = f"近{denominator}日均量"
        else:
            daily_volume = _cb_num(stock.get("volume"))
            if daily_volume is not None and daily_volume > 0:
                avg_volume_20d = daily_volume
                volume_basis = "當日量替代"

        for raw in raw_by_stock.get(sid, []):
            conversion_price = _cb_num(raw.get("ConversionPrice"))
            cb_price = _cb_num(raw.get("ReferencePrice"))
            underlying_price = (
                _cb_num(raw.get("PriceOfUnderlyingStock"))
                or _cb_num(stock.get("price"))
            )
            issuance = _cb_num(raw.get("IssuanceAmount"))
            outstanding = _cb_num(raw.get("OutstandingAmount"))

            if not conversion_price or conversion_price <= 0:
                continue
            if outstanding is not None and outstanding <= 0:
                continue

            parity = (
                100 * underlying_price / conversion_price
                if underlying_price is not None else None
            )
            premium = (
                (cb_price / parity - 1) * 100
                if cb_price is not None and parity not in (None, 0) else None
            )
            break_even = (
                conversion_price * cb_price / 100
                if cb_price is not None else None
            )
            distance_to_break_even = (
                (underlying_price / break_even - 1) * 100
                if underlying_price is not None and break_even not in (None, 0)
                else None
            )
            moneyness = (
                (underlying_price / conversion_price - 1) * 100
                if underlying_price is not None else None
            )
            remaining_pct = (
                outstanding / issuance * 100
                if outstanding is not None and issuance not in (None, 0)
                else None
            )
            potential_shares = (
                outstanding / conversion_price
                if outstanding is not None else None
            )
            potential_lots = (
                potential_shares / 1000 if potential_shares is not None else None
            )
            pressure_days = (
                potential_lots / avg_volume_20d
                if potential_lots is not None and avg_volume_20d not in (None, 0)
                else None
            )

            early_start = _cb_date(raw.get("InitialDateOfEarlyRedemption"))
            early_end = _cb_date(raw.get("DueDateOfEarlyRedemption"))
            early_active = bool(
                as_of and early_start and early_end and
                early_start <= as_of <= early_end
            )
            due_date = _cb_date(raw.get("DueDateOfConversion"))
            days_to_due = (
                (due_date - as_of).days if due_date and as_of else None
            )

            item = {
                "cb_id": str(raw.get("cb_id") or ""),
                "cb_name": str(raw.get("cb_name") or ""),
                "cb_date": str(raw.get("date") or snapshot.get("as_of") or ""),
                "cb_price": cb_price,
                "cb_conversion_price": conversion_price,
                "cb_underlying_price": underlying_price,
                "cb_parity": round(parity, 2) if parity is not None else None,
                "cb_premium_pct": round(premium, 2) if premium is not None else None,
                "cb_break_even_price": round(break_even, 2) if break_even is not None else None,
                "cb_distance_to_break_even_pct": round(distance_to_break_even, 2) if distance_to_break_even is not None else None,
                "cb_moneyness_pct": round(moneyness, 2) if moneyness is not None else None,
                "cb_issuance_amount": issuance,
                "cb_outstanding_amount": outstanding,
                "cb_remaining_pct": round(remaining_pct, 2) if remaining_pct is not None else None,
                "cb_potential_shares": round(potential_shares) if potential_shares is not None else None,
                "cb_potential_lots": round(potential_lots) if potential_lots is not None else None,
                "cb_avg_volume_20d": round(avg_volume_20d, 2) if avg_volume_20d is not None else None,
                "cb_pressure_days": round(pressure_days, 2) if pressure_days is not None else None,
                "cb_volume_basis": volume_basis,
                "cb_conversion_start": str(raw.get("InitialDateOfConversion") or ""),
                "cb_conversion_end": str(raw.get("DueDateOfConversion") or ""),
                "cb_days_to_due": days_to_due,
                "cb_early_redemption_active": early_active,
            }
            label, rank = _cb_pressure_label(item)
            item["cb_pressure_label"] = label
            item["cb_pressure_rank"] = rank

            history = []
            for hist_raw in daily_by_cb.get(item["cb_id"], [])[:3]:
                metric = _cb_metric_snapshot(
                    hist_raw,
                    _cb_num(stock.get("price")),
                    avg_volume_20d,
                    volume_basis,
                    hist_raw.get("date"),
                )
                if metric:
                    history.append(metric)
            item["cb_history"] = history
            trend_label, trend_score = _cb_overall_trend(history)
            item["cb_trend_label"] = trend_label
            item["cb_trend_score"] = trend_score

            monthly_history = []
            for month_raw in monthly_by_cb.get(item["cb_id"], [])[:3]:
                metric = _cb_metric_snapshot(
                    month_raw,
                    _cb_num(stock.get("price")),
                    avg_volume_20d,
                    volume_basis,
                    month_raw.get("date"),
                )
                if metric:
                    monthly_history.append(metric)
            item["cb_monthly_history"] = monthly_history
            items.append(item)

        items.sort(
            key=lambda item: (
                item.get("cb_pressure_rank") or 0,
                -(abs(item.get("cb_distance_to_break_even_pct"))
                  if item.get("cb_distance_to_break_even_pct") is not None
                  else 999999),
                item.get("cb_outstanding_amount") or 0,
            ),
            reverse=True,
        )
        stock["cb_items"] = items
        stock["cb_count"] = len(items)
        if items:
            primary = items[0]
            for key, value in primary.items():
                stock[key] = value

    return output


def format_output(results):
    results = enrich_html_fields([r for r in results if r])

    def safe_num(v, default=-999999):
        return v if isinstance(v, (int, float)) and v is not None else default

    sorted_by_score = sorted(
        results,
        key=lambda x: safe_num(x.get("score")),
        reverse=True,
    )

    sorted_by_chg = sorted(
        results,
        key=lambda x: safe_num(x.get("chgPct")),
        reverse=True,
    )

    return {
        "stocks": sorted_by_chg,
        "top_stocks": sorted_by_score[:5],
        "hot_stocks": sorted_by_chg[:5],
        "weak_stocks": sorted_by_chg[-5:] if sorted_by_chg else [],
        "rebound_list": [s for s in results if "反彈" in s.get("strategy", "")],
        "selloff_list": [s for s in results if "出貨" in s.get("strategy", "")],
        "buy_signal_list": [s for s in results if s.get("sig") == 1],
        "volume_up_list": [s for s in results if s.get("volume_ok")],
        "bottom_pick_list": [s for s in results if s.get("entry_note") == "抄底"],
    }


def build_strings(data):
    def safe_join(lst):
        return ", ".join([s["name"] for s in lst if s])

    return {
        "top_str": safe_join(data.get("top_stocks", [])),
        "weak_str": safe_join(data.get("weak_stocks", [])),
        "rebound_str": safe_join(data.get("rebound_list", [])[:5]),
        "selloff_str": safe_join(data.get("selloff_list", [])[:5]),
    }


def main():
    try:
        report_type = config.REPORT_TYPE
        csv_file = config.CSV_FILE
        report_title = config.REPORT_TITLE
        output_file = config.OUTPUT_FILE

        df = pd.read_csv(csv_file, sep="\t", encoding="utf-8-sig", dtype=str)
        if len(df.columns) == 1:
            df = pd.read_csv(csv_file, encoding="utf-8-sig", dtype=str)
        df = normalize_stock_df(df)
        stock_list = df.to_dict(orient="records")

    except Exception as e:
        print(f"❌ 讀取 config.yml 或 CSV 失敗: {e}")
        return

    start_used = start_limit = start_remain = None

    try:
        print("📊 執行前查詢 FinMind 使用量...")
        start_used, start_limit, start_remain = get_finmind_usage()

        estimated_calls = len(stock_list) * 2
        if start_remain < estimated_calls:
            print(
                f"⚠️ FinMind 剩餘額度可能不足，remain={start_remain}, estimated={estimated_calls}，仍繼續執行"
            )

        print(f"🚀 開始分析股票... [{report_type}]")
        try:
            static_csv_path = getattr(
                config, "STATIC_OUTPUT_FILE", "AllStatic.csv")
            static_chip_csv_path = getattr(
                config, "STATIC_CHIP_OUTPUT_FILE", "AllStatic_Chips.csv")
            static_news_csv_path = getattr(
                config, "ALLSTATIC_NEWS_OUTPUT_FILE", "AllStatic_news.csv")

            print(
                "📄 HTML render uses preloaded maps: "
                f"static={static_csv_path}, chips={static_chip_csv_path}, news={static_news_csv_path}"
            )
            static_map = load_static_map(static_csv_path=static_csv_path)
            chips_map = load_chips_static_map(
                static_chips_csv_path=static_chip_csv_path)
            news_map = load_news_static_map(
                static_news_csv_path=static_news_csv_path)

            results = get_full_stock_analysis(
                stock_list,
                static_map=static_map,
                chips_map=chips_map,
                news_map=news_map,
            )
        except RuntimeError as e:
            print(f"❌ {e}")
            return

        if not results:
            print("⚠️ 無分析結果")
            return

        print("📄 讀取最新可轉債總覽...")
        cb_snapshot = get_latest_convertible_bond_overview()
        results = attach_cb_analysis(results, cb_snapshot)
        print(
            "CB snapshot: "
            f"status={cb_snapshot.get('status')}, "
            f"as_of={cb_snapshot.get('as_of') or '-'}, "
            f"issues={len(cb_snapshot.get('rows') or [])}, "
            f"daily_points={len(cb_snapshot.get('daily_snapshots') or [])}, "
            f"monthly_points={len(cb_snapshot.get('monthly_snapshots') or [])}"
        )

        data = format_output(results)
        text_data = build_strings(data)

        print("[index] loading market index rows (TAIEX / TPEX)...")
        try:
            index_rows = get_market_index_rows()
        except Exception as e:
            print(f"⚠️ 指數資料載入失敗: {e}")
            index_rows = []

        now_dt = datetime.utcnow() + timedelta(hours=8)
        now_str = now_dt.strftime("%m%d%H%M")
        filename = f"{output_file}_{now_str}.html"

        try:
            with open("template.html", "r", encoding="utf-8") as f:
                template = Template(f.read())

            html_content = template.render(
                stocks=data["stocks"],
                index_rows=index_rows,
                top_stocks=text_data["top_str"],
                weak_stocks=text_data["weak_str"],
                rebound_list=text_data["rebound_str"],
                selloff_list=text_data["selloff_str"],
                report_title=report_title,
                report_type=report_type,
                generated_time=now_dt.strftime("%Y-%m-%d %H:%M"),
                tech_columns=TECH_COLUMNS,
                cb_meta={
                    "status": cb_snapshot.get("status") or "no_data",
                    "as_of": cb_snapshot.get("as_of") or "",
                    "reason": cb_snapshot.get("reason") or "",
                    "issue_count": len(cb_snapshot.get("rows") or []),
                    "daily_dates": [
                        point.get("date") for point in
                        cb_snapshot.get("daily_snapshots") or []
                    ],
                    "monthly_dates": [
                        point.get("date") for point in
                        cb_snapshot.get("monthly_snapshots") or []
                    ],
                },
                custom_category_options=collect_category_options(
                    data["stocks"]),
            )

            for f_name in [filename, "index.html"]:
                with open(f_name, "w", encoding="utf-8") as f:
                    f.write(html_content)

            print(f"✅ HTML 已生成：{filename}")

        except Exception as e:
            print(f"❌ HTML 生成失敗: {e}")
            return

    finally:
        try:
            print("📊 執行後查詢 FinMind 使用量...")
            end_used, end_limit, end_remain = get_finmind_usage()
            if start_used is not None and end_used is not None:
                print(
                    f"📉 本次約使用 {end_used - start_used} 次 API，剩餘 {end_remain}/{end_limit}"
                )
        except Exception as e:
            print(f"⚠️ 無法查詢執行後 FinMind 使用量: {e}")


if __name__ == "__main__":
    main()
