#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日產製 AllStatic_news.csv。

修正版重點：
1. 修正「產業」欄一直沿用「待AI摘要補齊」fallback 的問題。
2. Google News RSS 先做相關性過濾，降低 ETF、股市討論文、同名商店/地點等雜訊。
3. Prompt 改為 120~180 字短段落，要求「整理」而非照抄新聞標題。
4. 新聞標題相同時可沿用既有摘要，節省 OpenAI 額度。
5. 遇到 401 / 429 quota 類錯誤時，只停用後續 OpenAI 呼叫，不讓整批流程失敗。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

try:
    import config
except Exception:
    config = None

TAIWAN_TZ = dt.timezone(dt.timedelta(hours=8))
OUTPUT_COLUMNS = ["stock_id", "name", "產業", "新聞"]

_OPENAI_DISABLED_REASON = ""


@dataclass
class Stock:
    stock_id: str
    name: str


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    default_text = "1" if default else "0"
    return _env(name, default_text).strip().lower() in {"1", "true", "yes", "y", "on"}


def _detect_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="ignore")[:4096]
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    if "\t" in first_line:
        return "\t"
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except Exception:
        return ","


def read_stocks(path: str | Path) -> List[Stock]:
    path = Path(path)
    delimiter = _detect_delimiter(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError(f"{path} 沒有表頭")

        rows: List[Stock] = []
        for row in reader:
            stock_id = (
                row.get("stock_id")
                or row.get("Ticker")
                or row.get("代碼")
                or row.get("股票代碼")
                or row.get("證券代號")
                or ""
            ).strip()
            name = (
                row.get("name")
                or row.get("Name")
                or row.get("名稱")
                or row.get("股票名稱")
                or row.get("證券名稱")
                or ""
            ).strip()
            if stock_id and name:
                rows.append(Stock(stock_id=stock_id, name=name))
    return rows


def load_existing_news(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            out: dict[str, dict] = {}
            for row in reader:
                sid = str(row.get("stock_id") or "").strip()
                if sid:
                    out[sid] = row
            return out
    except Exception as exc:
        print(f"⚠️ 讀取既有 AllStatic_news.csv 失敗，將重新產製: {exc}", flush=True)
        return {}


def load_cache(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(path: str | Path, cache: dict) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False,
                   indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _compact_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.replace(" ，", "，").replace(" 。", "。")
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def is_placeholder_industry(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True

    bad_keywords = [
        "待AI摘要補齊",
        "待 AI 摘要補齊",
        "尚待AI補充",
        "尚待 AI 補充",
        "產業定位尚待",
        "產業與產品定位待",
        "請以年報",
        "請搭配年報",
        "主要產品、市占與供應鏈角色確認",
        "護城河。",
        "可能涉及",
        "需進一步查證",
        "主要產品與市場定位需再參考年報或法說確認。",
        "主要產品與市場定位需再參考年報或法說確認",
    ]

    if any(k in t for k in bad_keywords):
        return True

    if len(t) < 40:
        return True

    return False


def _fallback_industry(stock: Stock, existing_industry: str = "", max_chars: int = 150) -> str:
    if existing_industry and not is_placeholder_industry(existing_industry):
        return _compact_text(existing_industry, max_chars)
    return _compact_text(
        f"{stock.name}主要產品、供應鏈角色與市場定位資料不足；後續需以年報、法說或公司公告補充確認。",
        max_chars,
    )


def _fallback_news(news_items: List[dict], max_chars: int = 150) -> str:
    titles = [str(x.get("title", "")).strip()
              for x in news_items[:3] if x.get("title")]
    if not titles:
        return "近十天可用且明確相關的公司新聞不足，暫無可彙總重點。"
    text = "近十天新聞來源有限，主要標題：" + "；".join(titles)
    return _compact_text(text, max_chars)


def build_news_query(stock: Stock, days: int) -> str:
    # 用股票代號 + 公司名提高精準度；避免只用「統一」「勤美」這類泛詞。
    return f'("{stock.stock_id}" OR "{stock.name}") ("營收" OR "獲利" OR "股東會" OR "法說" OR "訂單" OR "出貨" OR "產能" OR "AI" OR "台股" OR "股票") -ETF -權證 -紀念品 -CMoney when:{days}d'


def fetch_google_news_rss(stock: Stock, days: int = 10, max_items: int = 12) -> List[dict]:
    params = urllib.parse.urlencode({
        "q": build_news_query(stock, days),
        "hl": "zh-TW",
        "gl": "TW",
        "ceid": "TW:zh-Hant",
    })
    url = f"https://news.google.com/rss/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    items: List[dict] = []
    for item in root.findall("./channel/item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        if title:
            items.append({"title": title, "source": source,
                         "link": link, "pubDate": pub_date})

    return filter_relevant_news(stock, items, max_items=max_items)


def filter_relevant_news(stock: Stock, items: List[dict], max_items: int = 8) -> List[dict]:
    """先過濾新聞雜訊，避免 prompt 被 ETF、泛市場、同名商店/地名拖偏。"""
    positive_terms = [
        stock.stock_id,
        stock.name,
        "營收",
        "法說",
        "股東會",
        "訂單",
        "出貨",
        "擴產",
        "獲利",
        "EPS",
        "AI",
        "伺服器",
        "散熱",
        "半導體",
        "金融股",
        "水泥",
        "車用",
    ]
    negative_terms = [
        "ETF",
        "成分股",
        "統一投信",
        "股市爆料同學會",
        "CMoney",
        "紀念品",
        "領取時間",
        "端午變盤",
        "台股下殺原因",
        "加權指數",
        "TWA00",
        "等待再次挑戰",
        "請問今天股東會",
        "股東會紀念品",
        "紀念品",
        "熱搜榜",
        "股價表現",
        "目標價",
        "權證",
        "投信",
        "外資買賣超",
        "停車",
        "繳費機",
        "私設停車場",
        "交通局",
        "籃協",
        "理事長",
        "無利益包袱",
        "外資加碼",
        "外資看好",
        "股價創高",
        "創歷史新高",
        "市值",
        "目標價",
        "評等",
        "熱搜",
        "討論度",
        "德國Emma",
        "漢堡排嘉",
        "618購物節",
    ]

    filtered: List[dict] = []
    seen = set()
    for item in items:
        title = str(item.get("title") or "")
        source = str(item.get("source") or "")
        text = f"{title} {source}"

        if title in seen:
            continue
        seen.add(title)

        has_identity = (stock.stock_id in text) or (stock.name in text)
        negative_score = sum(1 for k in negative_terms if k and k in text)
        positive_score = sum(1 for k in positive_terms if k and k in text)

        # 股票代號或公司名至少要出現；若有明顯雜訊詞，除非同時有營運關鍵字才保留。
        if not has_identity:
            continue
        operational_terms = ["營收", "獲利", "訂單", "出貨", "產能", "法說", "股東會", "財務長", "新廠", "投產", "撤照", "合作", "投資"]
        has_operational_title = any(term in text for term in operational_terms)
        if negative_score and not has_operational_title:
            continue
        if negative_score and positive_score < 2 and not has_operational_title:
            continue

        filtered.append(item)
        if len(filtered) >= max_items:
            break

    return filtered


def news_titles_hash(news_items: List[dict]) -> str:
    titles = [str(x.get("title", "")).strip()
              for x in news_items if x.get("title")]
    payload = "\n".join(titles)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_json_loads(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
        return {}


def clean_investment_tone(text: str) -> str:
    """降低摘要中的投資評論、研究報告語氣與推測語氣，改成中性事件描述。"""
    replacements = {
        "表現亮眼": "營運數據受市場關注",
        "業績表現佳": "業績較前期改善",
        "營運表現良好": "營運數據有所改善",
        "穩定的營運表現": "營運數據維持",
        "穩定表現": "營運數據維持",
        "強勁的營運表現": "營收年增幅度較高",
        "強勁的市場需求": "市場需求增加",
        "強勁的成長動能": "營收較前期增加",
        "強勁": "增加",
        "成長動能": "營運變化",
        "成長潛力": "後續營運仍待觀察",
        "應用潛力": "應用情境",
        "展望樂觀": "公司或市場關注後續需求",
        "抱有期待": "仍待後續營運數據驗證",
        "市場信心": "市場討論度提高",
        "利多": "相關事件",
        "看好": "關注",
        "正向": "改善",
        "具有良好的市場前景": "後續需求仍待觀察",
        "良好的市場定位": "市場定位明確",
        "競爭優勢": "市場定位",
        "競爭力": "市場位置",
        "顯示出其在市場中的": "反映其",
        "顯示出公司在市場上的": "反映公司",
        "顯示出公司在市場中的": "反映公司",
        "顯示出": "反映",
        "可能有助於": "新聞提及",
        "預期將": "新聞提及",
        "預計將": "新聞提及",
        "未來將持續增長": "後續仍需觀察",
        "持續增長": "增加",
        "持續成長": "增加",
        "持續擴大": "擴大",
        "持續深化": "持續布局",
        "進一步增強了其市場地位": "增加市場討論度",
        "重返成長軌道": "後續營運仍待觀察",
        "成為成長領頭羊": "市場討論度提高",
        "商機的發酵": "相關需求受到關注",
    }
    out = str(text or "")
    for old, new in replacements.items():
        out = out.replace(old, new)

    out = out.replace("顯示市場對其未來發展的關注", "市場討論度提高")
    out = out.replace("顯示市場對其未來表現的關注", "市場討論度提高")
    out = out.replace("這被視為未來訂單的潛力", "相關資本支出與訂單動向受到關注")
    out = out.replace("提供創新解決方案以滿足市場需求", "產品定位仍需依公司公告確認")
    return out


def has_operational_event(text: str) -> bool:
    """判斷新聞摘要是否已含明確營運事件。"""
    t = str(text or "")
    event_terms = [
        "營收", "月營收", "年增", "月增", "獲利", "EPS", "每股盈餘",
        "訂單", "接單", "出貨", "產能", "新廠", "投產", "擴產",
        "股東會", "法說", "董事長", "財務長", "股利", "現金股利",
        "合作", "收購", "併購", "產品", "技術", "AI", "ASIC", "GPU",
        "零碳礦山", "無人駕駛", "漁電共生", "撤照", "營運", "投資",
        "新店", "開幕", "招商", "租賃", "融資", "保證機制",
    ]
    return any(term in t for term in event_terms)


def clean_contradictory_news(text: str) -> str:
    """若已有營運事件，移除『營運事件有限』這類矛盾尾句。"""
    out = str(text or "").strip()
    if has_operational_event(out):
        patterns = [
            "近十天多為股價、評等或市場討論，缺少明確營運事件。",
            "近十天多為市場交易與股價討論，營運事件有限。",
            "近期新聞多為市場討論，營運事件有限。",
            "缺少其他明確的營運事件。",
            "缺乏具體的營運事件。",
        ]
        for p in patterns:
            out = out.replace(p, "")
        out = re.sub(r"[，,；;。 ]+$", "。", out)
    return out.strip()


def clean_non_operating_noise(text: str) -> str:
    """移除明顯非公司營運主軸的雜訊片段。"""
    out = str(text or "")
    noise_patterns = [
        r"此外，?台泥的第三代辜公怡當選籃協理事長[^。]*。",
        r"辜公怡當選籃協理事長[^。]*。",
        r"外資近期加碼[^。]*。",
        r"今年以來股價[^。]*。",
        r"股價[^。]*創下歷史新高[^。]*。",
        r"金融股助攻台股[^。]*。",
        r"公司在台股大漲中[^。]*。",
        r"市值達到[^。]*。",
    ]
    for pat in noise_patterns:
        out = re.sub(pat, "", out)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"[，,；;。 ]+$", "。", out)
    return out


def normalize_summary_text(text: str) -> str:
    """統一摘要後處理。"""
    out = clean_investment_tone(text)
    out = clean_non_operating_noise(out)
    out = clean_contradictory_news(out)
    return out


def should_refresh_industry(existing_industry: str) -> bool:
    if _env_bool("REFRESH_INDUSTRY", False):
        return True
    if is_placeholder_industry(existing_industry):
        return True

    weekday_text = _env("REFRESH_INDUSTRY_WEEKDAY", "").strip()
    if weekday_text == "":
        return False
    try:
        weekday = int(weekday_text)
    except Exception:
        return False
    return dt.datetime.now(TAIWAN_TZ).weekday() == weekday


def _openai_error_should_disable(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "invalid_api_key" in lowered or "incorrect api key" in lowered or "401" in lowered:
        return "OpenAI API key 無效，改用 fallback/既有摘要。"
    if "insufficient_quota" in lowered or "exceeded your current quota" in lowered or "billing" in lowered or "429" in lowered:
        return "OpenAI API 額度不足或 billing 未啟用，改用 fallback/既有摘要。"
    return ""


def summarize_with_openai(
    stock: Stock,
    news_items: List[dict],
    existing_industry: str = "",
    existing_news: str = "",
) -> Tuple[str, str, str]:
    """回傳 industry, news, status。"""
    global _OPENAI_DISABLED_REASON

    industry_chars = _env_int("INDUSTRY_SUMMARY_CHARS", 150)
    news_chars = _env_int("NEWS_SUMMARY_CHARS", 150)
    use_openai = _env_bool("USE_OPENAI_SUMMARY", True)

    if not use_openai:
        return (
            _fallback_industry(stock, existing_industry, industry_chars),
            _fallback_news(news_items, news_chars),
            "fallback/openai_off",
        )

    if _OPENAI_DISABLED_REASON:
        return (
            _fallback_industry(stock, existing_industry, industry_chars),
            existing_news or _fallback_news(news_items, news_chars),
            "fallback/openai_disabled",
        )

    api_key = _env("OPENAI_API_KEY", "")
    if not api_key:
        return (
            _fallback_industry(stock, existing_industry, industry_chars),
            _fallback_news(news_items, news_chars),
            "fallback/no_key",
        )

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return (
            _fallback_industry(stock, existing_industry, industry_chars),
            _fallback_news(news_items, news_chars),
            "fallback/no_openai_pkg",
        )

    refresh_industry = should_refresh_industry(existing_industry)
    keep_industry = bool(existing_industry and not is_placeholder_industry(
        existing_industry) and not refresh_industry)

    titles = [
        f"- {x.get('title', '')}｜{x.get('source', '')}｜{x.get('pubDate', '')}"
        for x in news_items
    ]
    news_text = "\n".join(titles) if titles else "無可用且明確相關的公司新聞。"

    industry_task = (
        f"industry 請沿用既有內容，不要改寫：{existing_industry}"
        if keep_industry else
        f"industry 請用 {industry_chars} 字以內，依序說明：主要產品/服務、所屬產業趨勢、較明確的產品定位或供應鏈角色。避免空泛形容詞；不確定市占、客戶與產品線不得編寫。"
    )

    prompt = f"""
你是台股公司產業與新聞摘要助理。請只輸出 JSON，不要 markdown。

公司：{stock.stock_id} {stock.name}

近十天新聞標題如下，可能含有市場討論、股價評論、ETF、權證、紀念品、目標價或同名雜訊：
{news_text}

請先自行過濾不相關標題，只整理「與該公司直接相關」的資訊。

請輸出兩欄：

1. industry：
- {industry_task}
- 僅可使用公司常識與新聞標題可合理支持的資訊。
- 不得編造市占率、客戶、訂單或產品線。
- 若產品線不確定，請寫「主要產品與市場定位需以年報或法說確認」，不要猜測。
- 不要使用「尚待AI補充」「請搭配年報」這類 placeholder。
- 語氣要像資料庫欄位，不要像研究報告或投資評論。

2. news：
- {news_chars} 字以內。
- 只能根據上方新聞標題整理，不得補不存在的事件。
- 優先順序：營收/月營收/獲利 > 訂單/出貨/產能 > 產品/技術 > 股東會/法說/人事 > 產業題材 > 股價/外資/目標價/市場討論。
- 若有營收、訂單、出貨、產能、產品、法說、人事或重大爭議資訊，請只摘要這些事件，不得再寫「營運事件有限」。
- 非公司營運資訊，例如籃協、個人職務、紀念品、外資評等、目標價、股價創高，除非與公司營運直接相關，否則不要寫入摘要。
- 只有在完全沒有營收、訂單、產品、法說、人事或重大事件時，才可寫「近十天多為股價、評等或市場討論，缺少明確營運事件。」
- 不要給買賣建議。
- 不要使用投資語氣或推測語句，例如：表現亮眼、看好、樂觀、信心、潛力、利多、強勁、正向、競爭優勢、可能有助、預期將、未來將持續成長。
- 不要照抄標題，不要用分號串接標題。
- 若新聞同時包含營運事件與股價討論，請優先寫營運事件，忽略股價討論。

JSON 格式：
{{"industry":"...","news":"..."}}
""".strip()

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=_env("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.15,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "你只輸出有效 JSON。"},
                {"role": "user", "content": prompt},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = _safe_json_loads(content)
        industry = existing_industry if keep_industry else str(
            data.get("industry") or "")
        news = str(data.get("news") or "")
        industry = normalize_summary_text(industry)
        news = normalize_summary_text(news)

        industry = _compact_text(industry, industry_chars)
        news = _compact_text(news, news_chars)

        if not industry or is_placeholder_industry(industry):
            industry = _fallback_industry(
                stock, existing_industry, industry_chars)
        if not news:
            news = _fallback_news(news_items, news_chars)

        status = "ai/news_refresh"
        if refresh_industry and not keep_industry:
            status = "ai/industry_news_refresh"
        elif keep_industry:
            status = "ai/news_refresh_keep_industry"
        return industry, news, status

    except Exception as exc:
        reason = _openai_error_should_disable(exc)
        if reason:
            _OPENAI_DISABLED_REASON = reason
            print(f"⚠️ OpenAI 後續停用 {stock.stock_id}: {reason}", flush=True)
            status = "fallback/openai_disabled_now"
        else:
            print(f"⚠️ AI 摘要失敗 {stock.stock_id}: {exc}", flush=True)
            status = "fallback/ai_error"

        return (
            _fallback_industry(stock, existing_industry, industry_chars),
            existing_news or _fallback_news(news_items, news_chars),
            status,
        )


def atomic_write_csv(rows: list[dict], out_path: str | Path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    tmp_path.replace(out_path)


def write_allstatic_news(
    stocks: Iterable[Stock],
    out_path: str | Path,
    days: int = 10,
    sleep_sec: float = 0.6,
    max_news_items: int = 8,
    cache_path: str | Path = ".allstatic_news_cache.json",
) -> None:
    industry_chars = _env_int("INDUSTRY_SUMMARY_CHARS", 150)
    news_chars = _env_int("NEWS_SUMMARY_CHARS", 150)
    skip_same_titles = _env_bool("NEWS_SKIP_IF_SAME_TITLES", True)

    existing = load_existing_news(out_path)
    cache = load_cache(cache_path)

    rows = []
    stock_list = list(stocks)
    for i, stock in enumerate(stock_list, start=1):
        existing_row = existing.get(stock.stock_id, {})
        existing_industry = str(existing_row.get(
            "產業") or existing_row.get("industry_summary") or "").strip()
        existing_news = str(existing_row.get(
            "新聞") or existing_row.get("news_summary") or "").strip()

        try:
            news_items = fetch_google_news_rss(
                stock, days=days, max_items=max_news_items)
        except Exception as exc:
            print(f"⚠️ RSS 擷取失敗 {stock.stock_id}: {exc}", flush=True)
            news_items = []

        title_hash = news_titles_hash(news_items)
        cache_row = cache.get(stock.stock_id, {})
        same_titles = bool(title_hash and cache_row.get(
            "news_titles_hash") == title_hash)
        need_industry = should_refresh_industry(existing_industry)

        if skip_same_titles and same_titles and existing_news and not need_industry:
            industry = _fallback_industry(
                stock, existing_industry, industry_chars)
            industry = normalize_summary_text(industry)

            news = normalize_summary_text(existing_news)
            news = _compact_text(news, news_chars)
            status = "cache/unchanged_titles"
        else:
            industry, news, status = summarize_with_openai(
                stock,
                news_items,
                existing_industry=existing_industry,
                existing_news=existing_news,
            )

        rows.append({
            "stock_id": stock.stock_id,
            "name": stock.name,
            "產業": industry,
            "新聞": news,
        })

        cache[stock.stock_id] = {
            "name": stock.name,
            "news_titles_hash": title_hash,
            "updated_at": dt.datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
        }

        print(
            f"[{i}/{len(stock_list)}] {stock.stock_id} {stock.name} {status}", flush=True)
        if sleep_sec and sleep_sec > 0:
            time.sleep(sleep_sec)

    atomic_write_csv(rows, out_path)
    save_cache(cache_path, cache)


def get_default_stocks_csv() -> str:
    return _env("STOCKS_CSV", getattr(config, "CSV_FILE", "stocks.csv") if config else "stocks.csv")


def get_default_output_csv() -> str:
    config_value = getattr(config, "ALLSTATIC_NEWS_OUTPUT_FILE",
                           "AllStatic_news.csv") if config else "AllStatic_news.csv"
    return _env("ALLSTATIC_NEWS_OUTPUT_FILE", _env("ALLSTATIC_NEWS_CSV", _env("ALLSTATIC_NEWS_FILE", config_value)))


def main() -> None:
    stocks_csv = get_default_stocks_csv()
    out_csv = get_default_output_csv()
    days = _env_int("NEWS_DAYS", 10)
    max_stocks = _env_int("MAX_STOCKS", 0)
    max_news_items = _env_int("NEWS_MAX_ITEMS", 8)
    sleep_sec = _env_float("NEWS_SLEEP_SEC", 0.6)
    cache_path = _env("NEWS_CACHE_FILE", ".allstatic_news_cache.json")

    stocks = read_stocks(stocks_csv)
    if max_stocks > 0:
        stocks = stocks[:max_stocks]

    print(
        f"Start AllStatic_news at {dt.datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"stocks={len(stocks)}, days={days}, max_news_items={max_news_items}, out={out_csv}, cache={cache_path}", flush=True)
    print(
        "settings="
        f"refresh_industry={_env('REFRESH_INDUSTRY', '0')}, "
        f"refresh_industry_weekday={_env('REFRESH_INDUSTRY_WEEKDAY', '')}, "
        f"skip_same_titles={_env('NEWS_SKIP_IF_SAME_TITLES', '1')}, "
        f"industry_chars={_env('INDUSTRY_SUMMARY_CHARS', '150')}, "
        f"news_chars={_env('NEWS_SUMMARY_CHARS', '150')}, "
        f"openai_key_present={bool(_env('OPENAI_API_KEY', ''))}",
        flush=True,
    )
    write_allstatic_news(
        stocks,
        out_csv,
        days=days,
        sleep_sec=sleep_sec,
        max_news_items=max_news_items,
        cache_path=cache_path,
    )
    print("Done", flush=True)


if __name__ == "__main__":
    main()
