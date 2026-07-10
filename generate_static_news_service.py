#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日產製 AllStatic_news.csv。

修正版重點：
1. 不使用 OpenAI。
2. 不使用 Google News RSS 或其他外部 URL 搜尋。
3. 只使用 FinMind TaiwanStockNews。
4. 由今天開始逐日查詢；每個 loop 區間為 7 天，最多往前 4 個 loop。
5. 預設輸出最多 5 筆新聞，格式 MMDD：新聞。

"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

API_URL = "https://api.finmindtrade.com/api/v4/data"
USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"


def _get_finmind_env_token() -> str:
    """Read FinMind token directly from the standardized environment variable.

    Standardize on FINMIND_TOKEN only.
    The token is read from the current process environment at runtime,
    so changes to the environment are reflected on each run.
    """
    value = os.getenv("FINMIND_TOKEN")
    return str(value).strip() if value and str(value).strip() else ""


def _get_finmind_env_token_with_retry() -> str:
    """Read FINMIND_TOKEN with short retries for CI timing windows."""
    retries_text = os.getenv("FINMIND_TOKEN_READ_RETRIES", "3")
    wait_ms_text = os.getenv("FINMIND_TOKEN_READ_WAIT_MS", "300")

    try:
        retries = max(int(str(retries_text).strip() or "3"), 1)
    except Exception:
        retries = 3

    try:
        wait_ms = max(int(str(wait_ms_text).strip() or "300"), 0)
    except Exception:
        wait_ms = 300

    for attempt in range(retries):
        token = _get_finmind_env_token()
        if token:
            return token
        if attempt + 1 < retries and wait_ms > 0:
            time.sleep(wait_ms / 1000.0)

    return ""


def _get_finmind_auth_headers(token: str | None = None) -> dict:
    token = str(token or "").strip(
    ) if token is not None else _get_finmind_env_token_with_retry()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _mask_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "..." + token[-4:]


def _get_finmind_user_info_snapshot() -> dict:
    """Runtime token/login/quota snapshot for log annotation."""
    token = _get_finmind_env_token_with_retry()

    info = {
        "token_present": bool(token),
        "token_source": "FINMIND_TOKEN" if token else "",
        "token_masked": _mask_token(token),
        "login_status": "missing_token",
        "user_count": 0,
        "api_request_limit": 0,
        "remain": 0,
    }
    if not token:
        return info

    req_headers = _get_finmind_auth_headers(token)
    req_headers.setdefault("User-Agent", "Mozilla/5.0")
    req_headers.setdefault("Accept", "application/json")

    try:
        req = urllib.request.Request(USER_INFO_URL, headers=req_headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read().decode("utf-8", errors="ignore")
        payload = _safe_json_dict(raw)

        used = payload.get("user_count")
        limit = payload.get("api_request_limit")
        try:
            used_int = int(used or 0)
            limit_int = int(limit or 0)
            remain_int = max(limit_int - used_int, 0) if limit_int else 0
        except Exception:
            used_int = 0
            limit_int = 0
            remain_int = 0

        info["login_status"] = "ok" if status_code == 200 and not payload.get(
            "error") else "error"
        info["user_count"] = used_int
        info["api_request_limit"] = limit_int
        info["remain"] = remain_int
        return info
    except Exception:
        info["login_status"] = "error"
        return info


try:
    import config
except Exception:
    config = None

TAIWAN_TZ = dt.timezone(dt.timedelta(hours=8))
OUTPUT_COLUMNS = ["stock_id", "name", "新聞"]


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


def normalize_stock_id_from_left6(value: str) -> str:
    """Read stock code from stocks.csv by fixed left 6-character width.

    Rule requested:
    - Stock code comes from stocks.csv.
    - Use the left 6 characters as the stock code field.
    - Remove spaces/full-width spaces inside the 6-character code.
    - Keep only valid Taiwan code shape after cleanup: 4~6 digits plus optional final letter.

    Examples:
    - "00631L元大台灣50正2" -> "00631L"
    - "00679B元大美債20年" -> "00679B"
    - "00878 國泰永續高股息" -> "00878"
    - "2330  台積電" -> "2330"
    """
    raw = str(value or "").strip().upper()
    left6 = raw[:6]
    left6 = re.sub(r"[\s\u3000]+", "", left6)
    left6 = left6.replace(".TW", "").replace(".TWO", "").replace(".T", "")

    match = re.match(r"^(\d{4,6}[A-Z]?)", left6)
    return match.group(1) if match else left6


def normalize_stock_name_from_left6(value: str) -> str:
    """If name is embedded in the same stocks.csv field, take text after left 6 chars."""
    raw = str(value or "").strip()
    if len(raw) > 6:
        name = raw[6:].strip()
        if name:
            return name
    return ""


def read_stocks(path: str | Path) -> List[Stock]:
    path = Path(path)
    delimiter = _detect_delimiter(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError(f"{path} 沒有表頭")

        rows: List[Stock] = []
        for row in reader:
            raw_stock_id = (
                row.get("stock_id")
                or row.get("Ticker")
                or row.get("代碼")
                or row.get("股票代碼")
                or row.get("證券代號")
                or ""
            )
            raw_name = (
                row.get("name")
                or row.get("Name")
                or row.get("名稱")
                or row.get("股票名稱")
                or row.get("證券名稱")
                or ""
            )

            # Stock code rule: take left 6 characters from stocks.csv code field.
            # Do not infer or append suffix from the name field.
            stock_id = normalize_stock_id_from_left6(raw_stock_id)
            name = str(raw_name or "").strip(
            ) or normalize_stock_name_from_left6(raw_stock_id)

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


def _to_datetime_safe(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
    except Exception:
        return None


def _extract_finmind_title(item: dict) -> str:
    for key in ("title", "news_title", "headline", "summary"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_finmind_source(item: dict) -> str:
    for key in ("source", "provider", "news_source"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _extract_finmind_date_text(item: dict) -> str:
    for key in ("date", "publish_date", "published_at", "created_at"):
        value = str(item.get(key) or "").strip()
        if value:
            return value[:10]
    return ""


def finmind_request_headers(token: str | None = None) -> dict:
    # Build headers from the current process environment at request time.
    req_headers = _get_finmind_auth_headers(token)
    req_headers.setdefault("User-Agent", "Mozilla/5.0")
    req_headers.setdefault("Accept", "application/json")
    return req_headers


def _safe_json_dict(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _mask_sensitive_request_url(url: str) -> str:
    """Log the request URL while masking secrets by default.

    Set FINMIND_LOG_FULL_URL_WITH_TOKEN=1 only when you intentionally want the
    exact token printed in your local logs.
    """
    if _env_bool("FINMIND_LOG_FULL_URL_WITH_TOKEN", False):
        return url

    try:
        parsed = urllib.parse.urlsplit(url)
        query_pairs = urllib.parse.parse_qsl(
            parsed.query, keep_blank_values=True)
        sensitive_keys = {
            "token",
            "api_token",
            "api_key",
            "apikey",
            "authorization",
            "access_token",
        }
        masked_pairs = [
            (key, "***" if key.lower() in sensitive_keys else value)
            for key, value in query_pairs
        ]
        masked_query = urllib.parse.urlencode(
            masked_pairs, doseq=True).replace("%2A%2A%2A", "***")
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, masked_query, parsed.fragment)
        )
    except Exception:
        return url


def _finmind_status_code(payload: dict) -> str:
    for key in ("status_code", "status", "code"):
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def _finmind_msg(payload: dict) -> str:
    for key in ("msg", "message", "detail", "error"):
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return ""


def _is_finmind_error_payload(payload: dict) -> bool:
    status_text = _finmind_status_code(payload).lower()
    msg_text = _finmind_msg(payload).lower()

    if status_text and status_text not in {"200", "success", "ok", "true"}:
        return True

    error_words = (
        "error",
        "forbidden",
        "payment",
        "required",
        "unauthorized",
        "permission",
        "limit",
        "invalid",
        "failed",
    )
    if msg_text and msg_text not in {"success", "ok"}:
        return any(word in msg_text for word in error_words)

    return False


def _print_finmind_api_error(
    context: str,
    stock: Stock,
    query_date: dt.date,
    request_url: str,
    raw_response: str = "",
    payload: dict | None = None,
    http_status: int | str | None = None,
    http_reason: str = "",
    exception: BaseException | None = None,
) -> None:
    if payload is None:
        payload = _safe_json_dict(raw_response)

    lines = [
        f"⚠️ {context}",
        f"  stock_id={stock.stock_id}",
        f"  stock_name={stock.name}",
        f"  query_date={query_date}",
        f"  request_url={_mask_sensitive_request_url(request_url)}",
        f"  http_status={http_status if http_status is not None else '<unknown>'}",
        f"  http_reason={http_reason or '<none>'}",
        f"  finmind_status_code={_finmind_status_code(payload) or '<missing>'}",
        f"  finmind_msg={_finmind_msg(payload) or '<missing>'}",
        f"  raw_response={raw_response if raw_response else '<empty>'}",
    ]
    if exception is not None:
        lines.append(f"  exception={type(exception).__name__}: {exception}")

    print("\n".join(lines), flush=True)


def fetch_finmind_news(stock: Stock, loop_days: int = 7, max_loops: int = 4, target_count: int = 5) -> List[dict]:
    """由今天起逐日查詢 FinMind TaiwanStockNews，並以 7 天為一個 loop 往前查。"""
    today = dt.datetime.now(TAIWAN_TZ).date()

    def _fetch_one_day(query_date: dt.date) -> List[dict]:
        token = _get_finmind_env_token_with_retry()
        req_headers = finmind_request_headers(token=token)
        params_dict = {
            "dataset": "TaiwanStockNews",
            "data_id": stock.stock_id,
            "start_date": query_date.strftime("%Y-%m-%d"),
        }
        if token:
            params_dict["token"] = token

        params = urllib.parse.urlencode(params_dict)
        request_url = f"{API_URL}?{params}"
        req = urllib.request.Request(request_url, headers=req_headers)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                http_status = getattr(resp, "status", None) or resp.getcode()
                http_reason = getattr(resp, "reason", "") or ""
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                raw = ""
            _print_finmind_api_error(
                "FinMind HTTP error while querying TaiwanStockNews",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response=raw,
                http_status=getattr(exc, "code", None),
                http_reason=str(getattr(exc, "reason", "") or ""),
                exception=exc,
            )
            return []
        except urllib.error.URLError as exc:
            _print_finmind_api_error(
                "FinMind URL/network error while querying TaiwanStockNews",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response="",
                http_reason=str(getattr(exc, "reason", "") or ""),
                exception=exc,
            )
            return []
        except Exception as exc:
            _print_finmind_api_error(
                "FinMind request failed before a usable response was available",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response="",
                exception=exc,
            )
            return []

        try:
            payload = json.loads(raw)
        except Exception as exc:
            _print_finmind_api_error(
                "FinMind returned non-JSON response",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response=raw,
                http_status=http_status,
                http_reason=http_reason,
                exception=exc,
            )
            return []

        if not isinstance(payload, dict):
            _print_finmind_api_error(
                "FinMind returned unexpected JSON type",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response=raw,
                http_status=http_status,
                http_reason=http_reason,
            )
            return []

        if _is_finmind_error_payload(payload):
            _print_finmind_api_error(
                "FinMind returned error payload",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response=raw,
                payload=payload,
                http_status=http_status,
                http_reason=http_reason,
            )
            return []

        rows = payload.get("data") or []
        if not isinstance(rows, list):
            _print_finmind_api_error(
                "FinMind response data is not a list",
                stock=stock,
                query_date=query_date,
                request_url=request_url,
                raw_response=raw,
                payload=payload,
                http_status=http_status,
                http_reason=http_reason,
            )
            return []
        normalized = []
        for row in rows:
            title = _extract_finmind_title(row)
            if not title:
                continue
            normalized.append({
                "title": title,
                "source": _extract_finmind_source(row),
                "date": _extract_finmind_date_text(row) or query_date.strftime("%Y-%m-%d"),
            })
        return normalized

    normalized: List[dict] = []
    seen = set()
    loop_days = max(int(loop_days), 1)
    max_loops = max(int(max_loops), 1)

    for loop_idx in range(max_loops):
        start_offset = loop_idx * loop_days
        end_offset = start_offset + loop_days

        for offset in range(start_offset, end_offset):
            query_date = today - dt.timedelta(days=offset)
            for row in _fetch_one_day(query_date):
                title = str(row.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                normalized.append(row)

            if len(normalized) >= target_count:
                break

        if len(normalized) >= target_count:
            break

    normalized.sort(
        key=lambda x: _to_datetime_safe(
            x.get("date") or "") or dt.datetime.min,
        reverse=True,
    )
    return normalized[:target_count]


def build_finmind_news_summary(news_items: List[dict], max_chars: int = 150, max_items: int = 5) -> str:
    if not news_items:
        return "近期待間無可用新聞。"

    lines = []
    for item in news_items[:max_items]:
        date_text = str(item.get("date") or "").strip()
        mmdd = date_text[5:10].replace(
            "-", "") if len(date_text) >= 10 else "----"
        title = str(item.get("title") or "").strip()
        if title:
            lines.append(f"{mmdd}：{title}")

    if not lines:
        return "近期待間無可用新聞。"
    return _compact_text("；".join(lines), max_chars)


def news_titles_hash(news_items: List[dict]) -> str:
    titles = [str(x.get("title", "")).strip()
              for x in news_items if x.get("title")]
    payload = "\n".join(titles)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_summary_text(text: str) -> str:
    out = str(text or "").strip()
    out = re.sub(r"\s+", " ", out)
    return out


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
    sleep_sec: float = 0.6,
    cache_path: str | Path = ".allstatic_news_cache.json",
) -> None:
    news_chars = _env_int("NEWS_SUMMARY_CHARS", 150)
    skip_same_titles = _env_bool("NEWS_SKIP_IF_SAME_TITLES", True)

    existing = load_existing_news(out_path)
    cache = load_cache(cache_path)

    rows = []
    stock_list = list(stocks)
    for i, stock in enumerate(stock_list, start=1):
        existing_row = existing.get(stock.stock_id, {})
        existing_news = str(existing_row.get(
            "新聞") or existing_row.get("news_summary") or "").strip()

        try:
            news_items = fetch_finmind_news(
                stock, loop_days=7, max_loops=4, target_count=5)
        except Exception as exc:
            print(
                f"⚠️ FinMind TaiwanStockNews 擷取失敗 {stock.stock_id}: {exc}", flush=True)
            news_items = []

        title_hash = news_titles_hash(news_items)
        cache_row = cache.get(stock.stock_id, {})
        same_titles = bool(title_hash and cache_row.get(
            "news_titles_hash") == title_hash)

        if skip_same_titles and same_titles and existing_news:
            news = normalize_summary_text(existing_news)
            news = _compact_text(news, news_chars)
            status = "cache/unchanged_titles"
        else:
            news = build_finmind_news_summary(
                news_items, news_chars, max_items=5)
            status = "finmind/taiwan_stock_news"

        rows.append({
            "stock_id": stock.stock_id,
            "name": stock.name,
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

    max_stocks = _env_int("MAX_STOCKS", 0)
    sleep_sec = _env_float("NEWS_SLEEP_SEC", 0.6)
    cache_path = _env("NEWS_CACHE_FILE", ".allstatic_news_cache.json")

    stocks = read_stocks(stocks_csv)
    if max_stocks > 0:
        stocks = stocks[:max_stocks]

    if stocks:
        sample_codes = ", ".join(f"{s.stock_id} {s.name}" for s in stocks[:5])
        print(
            f"[INFO] Parsed stock samples by left-6 rule: {sample_codes}", flush=True)

    print(
        f"Start AllStatic_news at {dt.datetime.now(TAIWAN_TZ).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"stocks={len(stocks)}, loop_days=7, max_loops=4, target_news_items=5, out={out_csv}, cache={cache_path}", flush=True)

    finmind_info = _get_finmind_user_info_snapshot()
    token_msg = (
        f"token_present={finmind_info.get('token_present')}, "
        f"source={finmind_info.get('token_source')}, "
        f"token={finmind_info.get('token_masked')}, "
        f"login={finmind_info.get('login_status')}"
    )
    print(f"FinMind token: {token_msg}", flush=True)
    print(
        f"FinMind usage: {int(finmind_info.get('user_count') or 0)}/{int(finmind_info.get('api_request_limit') or 0)}, "
        f"remain={int(finmind_info.get('remain') or 0)}",
        flush=True,
    )

    print(
        "settings="
        f"finmind_token_present={bool(_get_finmind_env_token())}, "
        f"skip_same_titles={_env('NEWS_SKIP_IF_SAME_TITLES', '1')}, "
        f"news_chars={_env('NEWS_SUMMARY_CHARS', '150')}, "
        f"finmind_log_full_url_with_token={_env('FINMIND_LOG_FULL_URL_WITH_TOKEN', '0')}, "
        "openai_removed=True, google_news_removed=True",
        flush=True,
    )

    write_allstatic_news(
        stocks,
        out_csv,
        sleep_sec=sleep_sec,
        cache_path=cache_path,
    )
    print("Done", flush=True)


if __name__ == "__main__":
    main()
