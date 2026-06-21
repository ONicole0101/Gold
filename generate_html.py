import os
from datetime import datetime, timedelta

import pandas as pd
import requests
from jinja2 import Template

import config
from main import get_full_stock_analysis


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
        x["tech_summary"] = " / ".join(parts) if parts else x.get("signal_text", "")
        out.append(x)
    return out


def get_finmind_usage():
    token = os.getenv("FINMIND_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    url = "https://api.web.finmindtrade.com/v2/user_info"
    resp = requests.get(url, headers=headers, timeout=30)
    data = resp.json()
    used = data.get("user_count", 0)
    limit = data.get("api_request_limit", 0)
    remain = limit - used
    print(f"FinMind usage: {used}/{limit}, remain={remain}")
    return used, limit, remain


def get_static_csv_path():
    config_path = getattr(config, "STATIC_OUTPUT_FILE", None)
    env_path = os.getenv("STATIC_CSV_FILE")
    return env_path or config_path or "AllStatic.csv"


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
        static_csv_file = get_static_csv_path()

        df = pd.read_csv(csv_file, sep="\t", encoding="utf-8-sig", dtype=str)
        df.columns = df.columns.str.strip()
        stock_list = df.rename(
            columns={"Ticker": "stock_id", "Name": "name"}
        ).to_dict(orient="records")

    except Exception as e:
        print(f"❌ 讀取 config.yml 或 CSV 失敗: {e}")
        return

    if not os.path.exists(static_csv_file):
        print(f"❌ 找不到靜態資料檔：{static_csv_file}")
        print("請先執行 generate_static_csv.py 產生 AllStatic.csv")
        return

    # 讓 stock_service.py 能讀到同一路徑
    os.environ["STATIC_CSV_FILE"] = static_csv_file
    print(f"📄 使用靜態資料檔：{static_csv_file}")

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
            results = get_full_stock_analysis(stock_list)
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

        if report_type == "Holding":
            report_subtitle = "持股追蹤與風險檢視"
        elif report_type == "Gold":
            report_subtitle = "潛力黃金股觀察名單"
        else:
            report_subtitle = "台股技術分析"

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
                report_subtitle=report_subtitle,
                report_type=report_type,
                generated_time=now_dt.strftime("%Y-%m-%d %H:%M"),
                tech_columns=TECH_COLUMNS,
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
