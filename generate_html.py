import os
from datetime import datetime, timedelta

import pandas as pd
from jinja2 import Template

import config
from custom_categories import CUSTOM_CATEGORY_COLUMN, category_text, split_category_text
from data_sources import get_finmind_user_info
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

        data = format_output(results)
        text_data = build_strings(data)

        now_dt = datetime.utcnow() + timedelta(hours=8)
        now_str = now_dt.strftime("%m%d%H%M")
        filename = f"{output_file}_{now_str}.html"

        try:
            with open("template.html", "r", encoding="utf-8") as f:
                template = Template(f.read())

            html_content = template.render(
                stocks=data["stocks"],
                top_stocks=text_data["top_str"],
                weak_stocks=text_data["weak_str"],
                rebound_list=text_data["rebound_str"],
                selloff_list=text_data["selloff_str"],
                report_title=report_title,
                report_type=report_type,
                generated_time=now_dt.strftime("%Y-%m-%d %H:%M"),
                tech_columns=TECH_COLUMNS,
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
