from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from finmind_auth import (
    get_finmind_request_kwargs,
    get_finmind_user_info,
    mask_token,
    resolve_finmind_token,
)

API_URL = "https://api.finmindtrade.com/api/v4/data"
USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"
TAIWAN_STOCK_TRADING_DAILY_REPORT_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
_raw_output_dir = Path(
    os.getenv("R_DATASET_OUTPUT_DIR", str(Path(__file__).resolve().parent))
).expanduser()
if _raw_output_dir.name.lower() == "dataset":
    OUTPUT_DIR = _raw_output_dir
else:
    OUTPUT_DIR = _raw_output_dir / "Dataset"
HISTORY_DIR = OUTPUT_DIR / "His"
START_DATE = "2020-04-01"
FINMIND_USAGE_LOG_FILE = os.getenv(
    "FINMIND_USAGE_LOG_FILE", "finmind_token_usage_log.csv"
)
REMOTE_DATASET_SUBDIR = str(
    os.getenv("R_DATASET_REMOTE_SUBDIR", "Dataset") or "Dataset"
).strip()

DATASETS_RANGE = [
    "TaiwanStockPrice",
    "TaiwanStockWeekPrice",
    "TaiwanStockMonthPrice",
    "TaiwanStockPER",
    "TaiwanStockMarginPurchaseShortSale",
    "TaiwanStockFinancialStatements",
    "TaiwanStockBalanceSheet",
    "TaiwanStockIndustryChain",
    "TaiwanStockMonthRevenue",
    "TaiwanStockDividend",
    "TaiwanStockDispositionSecuritiesPeriod",
    "TaiwanStockTotalReturnIndex",
    "TaiwanStockMarginMaintenance",
]

DATASETS_ONE_DAY = []

TRADING_DAILY_REPORT_LOOKBACK_DAYS = 20

# Keep every FinMind request small enough for the API to finish reliably.
# These values can be overridden in GitHub Actions/environment variables.
FINMIND_CONNECT_TIMEOUT_SECONDS = max(
    int(os.getenv("FINMIND_CONNECT_TIMEOUT_SECONDS", "30")), 1
)
FINMIND_READ_TIMEOUT_SECONDS = max(
    int(os.getenv("FINMIND_READ_TIMEOUT_SECONDS", "600")), 60
)
FINMIND_MAX_RETRIES = max(int(os.getenv("FINMIND_MAX_RETRIES", "3")), 1)
FINMIND_RETRY_BACKOFF_SECONDS = max(
    float(os.getenv("FINMIND_RETRY_BACKOFF_SECONDS", "2")), 0.0
)
FINMIND_CHUNK_DAYS = max(int(os.getenv("FINMIND_CHUNK_DAYS", "366")), 1)

DATASETS_NO_END_DATE = {
}

DATASETS_FORCE_ONE_DAY = {
}

# Datasets that require specific fixed data_id values (not per-stock, not 所有).
# Each entry maps dataset_name → list of data_ids to fetch and combine.
INDEX_SPECIFIC_DATA_IDS: dict[str, list[str]] = {
    "TaiwanStockTotalReturnIndex": ["TAIEX", "TPEx"],
}

# These datasets often returned only a single-day snapshot in earlier runs.
# We still force an explicit all-market path for clarity, but no longer rely on
# stocks.csv-targeted per-stock fetch mode.
PER_STOCK_ONLY_DATASETS = {
    "TaiwanStockPrice",
    "TaiwanStockMonthPrice",
    "TaiwanStockMarginPurchaseShortSale",
}

NO_EMPTY_OUTPUT_DATASETS = {
    "TaiwanStockPrice",
    "TaiwanStockMonthPrice",
    "TaiwanStockMarginPurchaseShortSale",
}

ANNUAL_ARCHIVE_DATASETS = set()

ONE_TIME_BACKFILL_DATASETS = {
    "TaiwanStockPrice",
}

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def build_finmind_date_chunks(start_date: str, end_date: str | None) -> list[tuple[str, str | None]]:
    """Split a FinMind date range at year boundaries and by a hard day limit."""
    if not end_date:
        return [(start_date, None)]

    start_ts = pd.to_datetime(start_date, errors="coerce")
    end_ts = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
        return [(start_date, end_date)]

    chunks: list[tuple[str, str | None]] = []
    cursor = start_ts.normalize()
    final_day = end_ts.normalize()
    while cursor <= final_day:
        year_end = pd.Timestamp(year=cursor.year, month=12, day=31)
        size_end = cursor + timedelta(days=FINMIND_CHUNK_DAYS - 1)
        chunk_end = min(year_end, size_end, final_day)
        chunks.append((cursor.strftime("%Y-%m-%d"),
                      chunk_end.strftime("%Y-%m-%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def finmind_get(url: str, *, params: dict | None = None, headers: dict | None = None, label: str) -> requests.Response:
    """GET with a long read timeout and bounded retries for transient failures."""
    timeout = (FINMIND_CONNECT_TIMEOUT_SECONDS, FINMIND_READ_TIMEOUT_SECONDS)
    retry_statuses = {408, 425, 429, 500, 502, 503, 504}

    for attempt in range(1, FINMIND_MAX_RETRIES + 1):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout)
            if response.status_code not in retry_statuses:
                return response
            reason = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            response = None
            reason = f"{type(exc).__name__}: {exc}"

        if attempt >= FINMIND_MAX_RETRIES:
            raise RuntimeError(
                f"FinMind request failed after {FINMIND_MAX_RETRIES} attempts "
                f"({label}): {reason}"
            )

        retry_after = 0.0
        if response is not None:
            try:
                retry_after = float(
                    response.headers.get("Retry-After", 0) or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
        wait_seconds = max(
            retry_after,
            FINMIND_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)),
        )
        print(
            f"FinMind transient error ({label}), attempt={attempt}/{FINMIND_MAX_RETRIES}, "
            f"reason={reason}, retry_in={wait_seconds:.1f}s",
            flush=True,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    raise RuntimeError(f"FinMind request failed unexpectedly ({label})")


def _google_drive_service():
    client_id = str(os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    client_secret = str(os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    refresh_token = str(os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN") or "").strip()
    missing = [name for name, value in (
        ("GOOGLE_OAUTH_CLIENT_ID", client_id),
        ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
        ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
    ) if not value]
    if missing:
        raise RuntimeError(
            f"Missing Google OAuth secret(s): {', '.join(missing)}")
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_DRIVE_SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _google_sleep() -> None:
    try:
        seconds = max(float(os.getenv("GOOGLE_SLEEP_SECONDS", "0") or "0"), 0)
    except ValueError:
        seconds = 0
    if seconds:
        time.sleep(seconds)


def _verify_drive_folder(service, folder_id: str) -> dict:
    try:
        metadata = service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
        ).execute(num_retries=5)
    except Exception as exc:
        raise RuntimeError(
            "Cannot access GOOGLE_DEST_FOLDER_ID with the authorized Google account. "
            "Confirm the folder ID and OAuth account."
        ) from exc
    if metadata.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError("GOOGLE_DEST_FOLDER_ID must identify a folder")
    print(
        f"Google Drive destination verified: folder={metadata.get('name')}",
        flush=True,
    )
    return metadata


def _drive_children(service, folder_id: str) -> list[dict]:
    results, page_token = [], None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken,files(id,name,mimeType)", pageToken=page_token,
            pageSize=1000, spaces="drive",
        ).execute(num_retries=5)
        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return results


def _ensure_remote_dataset_folder(service, parent_folder_id: str) -> str:
    name = REMOTE_DATASET_SUBDIR or "Dataset"
    for item in _drive_children(service, parent_folder_id):
        if item["name"] == name and item["mimeType"] == "application/vnd.google-apps.folder":
            return item["id"]
    result = service.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_folder_id],
        },
        fields="id",
    ).execute(num_retries=5)
    _google_sleep()
    return result["id"]


def download_google_dataset() -> None:
    folder_id = str(os.getenv("GOOGLE_DEST_FOLDER_ID") or "").strip()
    if not folder_id:
        raise RuntimeError("GOOGLE_DEST_FOLDER_ID is not set")
    service = _google_drive_service()
    _verify_drive_folder(service, folder_id)
    dataset_folder_id = _ensure_remote_dataset_folder(service, folder_id)

    def download_folder(remote_id: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        for item in _drive_children(service, remote_id):
            target = local_dir / item["name"]
            if item["mimeType"] == "application/vnd.google-apps.folder":
                download_folder(item["id"], target)
            elif not item["mimeType"].startswith("application/vnd.google-apps"):
                request = service.files().get_media(
                    fileId=item["id"]
                )
                with target.open("wb") as output:
                    downloader = MediaIoBaseDownload(
                        output, request, chunksize=10 * 1024 * 1024)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk(num_retries=5)
                _google_sleep()

    download_folder(dataset_folder_id, OUTPUT_DIR)
    print(
        f"Downloaded existing Google Drive dataset to {OUTPUT_DIR} (subdir={REMOTE_DATASET_SUBDIR})",
        flush=True,
    )


def upload_google_dataset() -> None:
    folder_id = str(os.getenv("GOOGLE_DEST_FOLDER_ID") or "").strip()
    if not folder_id:
        raise RuntimeError("GOOGLE_DEST_FOLDER_ID is not set")
    service = _google_drive_service()
    _verify_drive_folder(service, folder_id)
    dataset_folder_id = _ensure_remote_dataset_folder(service, folder_id)

    def ensure_folder(parent_id: str, name: str) -> str:
        for item in _drive_children(service, parent_id):
            if item["name"] == name and item["mimeType"] == "application/vnd.google-apps.folder":
                return item["id"]
        result = service.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder",
                  "parents": [parent_id]},
            fields="id",
        ).execute(num_retries=5)
        _google_sleep()
        return result["id"]

    def upload_folder(local_dir: Path, remote_id: str) -> None:
        remote_files = {x["name"]: x for x in _drive_children(service, remote_id)
                        if x["mimeType"] != "application/vnd.google-apps.folder"}
        for path in sorted(local_dir.iterdir()):
            if path.is_dir():
                upload_folder(path, ensure_folder(remote_id, path.name))
                continue
            media = MediaFileUpload(
                str(path), chunksize=10 * 1024 * 1024, resumable=True
            )
            existing = remote_files.get(path.name)
            if existing:
                service.files().update(fileId=existing["id"], media_body=media
                                       ).execute(num_retries=5)
            else:
                service.files().create(body={"name": path.name, "parents": [remote_id]},
                                       media_body=media, fields="id"
                                       ).execute(num_retries=5)
            _google_sleep()

    upload_folder(OUTPUT_DIR, dataset_folder_id)
    print(
        f"Uploaded dataset output to Google Drive (subdir={REMOTE_DATASET_SUBDIR})",
        flush=True,
    )


def _append_finmind_usage_event(
    event: str,
    source: str,
    token: str,
    status: str,
    status_code: int | None,
    user_count: int | None,
    api_request_limit: int | None,
    remain: int | None,
    message: str,
) -> None:
    row = {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "source": source,
        "token_present": bool(token),
        "token_source": "FINMIND_TOKEN" if token else "",
        "token_masked": mask_token(token),
        "login_status": status,
        "status_code": status_code,
        "user_count": user_count,
        "api_request_limit": api_request_limit,
        "remain": remain,
        "message": str(message or "")[:300],
    }

    try:
        exists = os.path.exists(FINMIND_USAGE_LOG_FILE)
        pd.DataFrame([row]).to_csv(
            FINMIND_USAGE_LOG_FILE,
            mode="a",
            header=not exists,
            index=False,
            encoding="utf-8-sig",
        )
    except Exception as exc:
        print(f"warning: cannot write FinMind usage log: {exc}", flush=True)


def print_finmind_usage_snapshot() -> dict:
    info = get_finmind_user_info(
        write_log=False, source="R_DataSet.py")
    _append_finmind_usage_event(
        event="token_check",
        source="R_DataSet.py",
        token=resolve_finmind_token(),
        status=info.get("login_status") or "error",
        status_code=info.get("status_code"),
        user_count=info.get("user_count"),
        api_request_limit=info.get("api_request_limit"),
        remain=info.get("remain"),
        message=info.get("message") or "",
    )
    print(
        "FinMind token: "
        f"token_present={info.get('token_present')}, "
        f"source={info.get('token_source')}, "
        f"token={info.get('token_masked')}, "
        f"login={info.get('login_status')}",
        flush=True,
    )
    print(
        "FinMind usage: "
        f"{int(info.get('user_count') or 0)}/{int(info.get('api_request_limit') or 0)}, "
        f"remain={int(info.get('remain') or 0)}",
        flush=True,
    )
    if not info.get("ok"):
        print(
            f"warning: FinMind token/user_info check failed: {info.get('message')}",
            flush=True,
        )
    return info


def should_keep_history_files() -> bool:
    value = (os.getenv("R_DATASET_KEEP_HISTORY_FILES") or "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def should_run_one_time_backfill_missing() -> bool:
    value = (os.getenv("R_DATASET_ONE_TIME_BACKFILL_MISSING")
             or "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def should_run_backfill_missing_history() -> bool:
    value = (os.getenv("R_DATASET_BACKFILL_MISSING_HISTORY")
             or "1").strip().lower()
    return value in {"1", "true", "yes", "on"}


def load_target_stock_ids_from_stocks_csv() -> list[str]:
    path = Path(__file__).resolve().parent / "stocks.csv"
    if not path.exists():
        return []

    try:
        df = pd.read_csv(path, sep="\t", dtype=str, encoding="utf-8-sig")
        if len(df.columns) == 1:
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except Exception:
        return []

    if df.empty:
        return []

    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={"Ticker": "stock_id", "ticker": "stock_id"})

    stock_col = None
    for candidate in ["stock_id", "Ticker", "ticker", "id"]:
        if candidate in df.columns:
            stock_col = candidate
            break
    if stock_col is None:
        stock_col = df.columns[0]

    ids: list[str] = []
    for raw in df[stock_col].astype(str).tolist():
        sid = str(raw or "").strip()
        if sid and sid.lower() not in {"nan", "none", "null"}:
            ids.append(sid)
    return sorted(set(ids))


def load_all_dataset_outputs(dataset_name: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    candidates = []
    candidates.extend(list_dataset_files(dataset_name))
    if HISTORY_DIR.exists():
        candidates.extend(sorted(HISTORY_DIR.glob(f"{dataset_name}_*.csv")))

    # Annual archive files are also part of cumulative history.
    candidates.extend(list_annual_archive_files(dataset_name))

    seen = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        try:
            frames.append(pd.read_csv(path, dtype=str, encoding="utf-8-sig"))
        except Exception:
            continue

    if not frames:
        return pd.DataFrame()

    merged = pd.concat(frames, ignore_index=True, sort=False)
    return merge_dedup_with_existing(pd.DataFrame(), merged.to_dict("records"))


def detect_missing_history_stock_ids(existing_df: pd.DataFrame, target_ids: list[str], required_start: str) -> list[str]:
    if not target_ids:
        return []

    required_ts = pd.to_datetime(required_start, errors="coerce")
    if pd.isna(required_ts):
        return []

    if existing_df is None or existing_df.empty or "stock_id" not in existing_df.columns or "date" not in existing_df.columns:
        return list(target_ids)

    work = existing_df[["stock_id", "date"]].copy()
    work["stock_id"] = work["stock_id"].astype(str).str.strip()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["stock_id", "date"])
    if work.empty:
        return list(target_ids)

    min_dates = work.groupby("stock_id")["date"].min().to_dict()
    missing: list[str] = []
    for sid in target_ids:
        min_dt = min_dates.get(sid)
        if min_dt is None or pd.isna(min_dt) or min_dt > required_ts:
            missing.append(sid)
    return missing


def detect_missing_stock_ids_on_date(existing_df: pd.DataFrame, target_ids: list[str], date_str: str) -> list[str]:
    if not target_ids:
        return []

    if existing_df is None or existing_df.empty:
        return list(target_ids)

    if "stock_id" not in existing_df.columns or "date" not in existing_df.columns:
        return list(target_ids)

    target_set = {str(x).strip() for x in target_ids if str(x).strip()}
    if not target_set:
        return []

    work = existing_df[["stock_id", "date"]].copy()
    work["stock_id"] = work["stock_id"].astype(str).str.strip()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    target_ts = pd.to_datetime(date_str, errors="coerce")
    if pd.isna(target_ts):
        return list(target_set)

    rows = work.loc[work["date"] == target_ts]
    present = set(rows["stock_id"].dropna().astype(str).str.strip().tolist())
    return sorted(target_set - present)


def build_stock_coverage_snapshot(existing_df: pd.DataFrame, target_ids: list[str], date_str: str) -> dict:
    target_set = {str(x).strip() for x in (target_ids or []) if str(x).strip()}
    expected = len(target_set)

    result = {
        "check_date": date_str,
        "target_stock_count": expected,
        "actual_stock_count": 0,
        "missing_stock_count": expected,
        "has_required_columns": False,
    }

    if expected == 0:
        result["missing_stock_count"] = 0
        result["has_required_columns"] = True
        return result

    if existing_df is None or existing_df.empty:
        return result

    if "stock_id" not in existing_df.columns or "date" not in existing_df.columns:
        return result

    result["has_required_columns"] = True
    work = existing_df[["stock_id", "date"]].copy()
    work["stock_id"] = work["stock_id"].astype(str).str.strip()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    target_ts = pd.to_datetime(date_str, errors="coerce")
    if pd.isna(target_ts):
        return result

    day_rows = work.loc[work["date"] == target_ts]
    present_targets = set(
        day_rows["stock_id"].dropna().astype(str).str.strip().tolist())
    present_targets = present_targets & target_set

    result["actual_stock_count"] = len(present_targets)
    result["missing_stock_count"] = max(expected - len(present_targets), 0)
    return result


def fetch_rows_backfill_missing_stocks(dataset_name: str, stock_ids: list[str], start_date: str, end_date: str | None) -> list[dict]:
    if not stock_ids:
        return []

    rows: list[dict] = []
    errors: list[str] = []
    total = len(stock_ids)
    print(
        f"{dataset_name}: one-time backfill start, stocks={total}, start={start_date}, end={end_date}",
        flush=True,
    )

    for idx, sid in enumerate(stock_ids, start=1):
        try:
            item_rows = fetch_rows_for_data_id(
                dataset_name,
                data_id=sid,
                start_date=start_date,
                end_date=end_date,
            )
            if item_rows:
                rows.extend(item_rows)
        except Exception as exc:
            errors.append(str(exc))
            if len(errors) <= 5:
                print(
                    f"{dataset_name}: one-time backfill skip {sid} ({idx}/{total}), reason={exc}",
                    flush=True,
                )

    print(
        f"{dataset_name}: one-time backfill done, fetched_rows={len(rows)}, failed_stocks={len(errors)}",
        flush=True,
    )
    return rows


def is_finmind_permission_error(message: str) -> bool:
    text = str(message or "").lower()
    return any(k in text for k in [
        "your level is free",
        "please update your user level",
        "access denied",
        "permission",
        "forbidden",
        "sponsor",
    ])


def resolve_end_date() -> str:
    now = datetime.utcnow()
    if now.weekday() == 5:
        now = now - timedelta(days=1)
    elif now.weekday() == 6:
        now = now - timedelta(days=2)
    return now.strftime("%Y-%m-%d")


def list_dataset_files(dataset_name: str) -> list[Path]:
    return sorted(OUTPUT_DIR.glob(f"{dataset_name}_*.csv"))


def list_annual_archive_files(dataset_name: str) -> list[Path]:
    pattern = re.compile(rf"^{re.escape(dataset_name)}_Y(\d{{4}})\.csv$")
    return sorted([p for p in list_dataset_files(dataset_name) if pattern.match(p.name)])


def list_timestamped_dataset_files(dataset_name: str) -> list[Path]:
    files = []
    for path in list_dataset_files(dataset_name):
        if extract_dataset_exec_date(dataset_name, path):
            files.append(path)
    return files


def load_latest_dataset_output(dataset_name: str) -> pd.DataFrame:
    # Always build cumulative base from all known outputs (dataset + dataset/His).
    return load_all_dataset_outputs(dataset_name)


def find_latest_non_empty_dataset_file(dataset_name: str) -> tuple[Path | None, int]:
    files = list_dataset_files(dataset_name)
    for path in reversed(files):
        try:
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
            rows = len(df)
            if rows > 0:
                return path, rows
        except Exception:
            continue
    return None, 0


def remove_older_dataset_files(dataset_name: str, keep_file: Path) -> None:
    for path in list_dataset_files(dataset_name):
        if path != keep_file:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def extract_dataset_exec_date(dataset_name: str, path: Path) -> str | None:
    pattern = rf"^{re.escape(dataset_name)}_(\d{{8}})(?:\d{{0,6}})?\.csv$"
    m = re.match(pattern, path.name)
    if not m:
        return None
    return m.group(1)


def should_archive_previous_files(existing_files: list[Path], new_file_name: str) -> bool:
    if not existing_files:
        return False
    return any(path.name != new_file_name for path in existing_files)


def archive_dataset_files_to_history(files: list[Path]) -> list[str]:
    if not files:
        return []

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for path in files:
        target = HISTORY_DIR / path.name
        try:
            if target.exists():
                target.unlink()
            shutil.move(str(path), str(target))
            archived.append(path.name)
        except Exception as exc:
            print(
                f"archive warning: {path.name} -> His failed, reason={exc}", flush=True)
    return archived


def merge_dedup_with_existing(existing_df: pd.DataFrame, rows: list[dict]) -> pd.DataFrame:
    new_df = pd.DataFrame(rows or [])
    if existing_df is None or existing_df.empty:
        merged = new_df
    elif new_df.empty:
        merged = existing_df
    else:
        merged = pd.concat([existing_df, new_df],
                           ignore_index=True, sort=False)

    if merged.empty:
        return merged

    normalized = merged.astype(str).fillna("")
    dedup_mask = ~normalized.duplicated(keep="last")
    return merged.loc[dedup_mask].reset_index(drop=True)


def infer_incremental_start_date(existing_df: pd.DataFrame, fallback_start: str) -> str:
    if existing_df is None or existing_df.empty or "date" not in existing_df.columns:
        return fallback_start
    try:
        s = pd.to_datetime(existing_df["date"], errors="coerce").dropna()
    except Exception:
        return fallback_start
    if s.empty:
        return fallback_start
    # Incremental sync should start from next day to avoid repeatedly re-pulling
    # the same max date forever when remote endpoint returns a fixed snapshot.
    return (s.max() + timedelta(days=1)).strftime("%Y-%m-%d")


def infer_incremental_start_date_trading_daily_report(existing_df: pd.DataFrame, fallback_start: str) -> str:
    start_date = infer_incremental_start_date(existing_df, fallback_start)
    archive_files = list_annual_archive_files("TaiwanStockTradingDailyReport")
    archive_years: list[int] = []
    for path in archive_files:
        m = re.match(
            r"^TaiwanStockTradingDailyReport_Y(\d{4})\.csv$", path.name)
        if not m:
            continue
        try:
            archive_years.append(int(m.group(1)))
        except Exception:
            continue

    if not archive_years:
        return start_date

    lower_bound = f"{max(archive_years) + 1:04d}-01-01"
    try:
        if pd.to_datetime(start_date, errors="coerce") < pd.to_datetime(lower_bound, errors="coerce"):
            return lower_bound
    except Exception:
        return lower_bound
    return start_date


def split_trading_daily_report_by_year(df: pd.DataFrame, cutoff_year: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame(), (df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame())

    work = df.copy()
    dates = pd.to_datetime(work["date"], errors="coerce")
    years = pd.Series(pd.NA, index=work.index, dtype="Int64")
    years.loc[dates.notna()] = dates.loc[dates.notna()].dt.year.astype("Int64")
    older_mask = years.notna() & (years <= int(cutoff_year))
    older_df = work.loc[older_mask].reset_index(drop=True)
    current_df = work.loc[~older_mask].reset_index(drop=True)
    return older_df, current_df


def cleanup_trading_daily_report_files(current_file: Path, annual_file: Path | None) -> None:
    keep_names = {current_file.name}
    if annual_file is not None:
        keep_names.add(annual_file.name)

    for path in list_dataset_files("TaiwanStockTradingDailyReport"):
        if path.name in keep_names:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def write_trading_daily_report_outputs(final_df: pd.DataFrame, exec_ts: str, keep_history: bool, mode: str) -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    exec_year = int(exec_ts[:4])
    cutoff_year = exec_year - 1
    current_path = OUTPUT_DIR / f"TaiwanStockTradingDailyReport_{exec_ts}.csv"
    annual_path = OUTPUT_DIR / \
        f"TaiwanStockTradingDailyReport_Y{cutoff_year}.csv"

    older_df, current_df = split_trading_daily_report_by_year(
        final_df, cutoff_year)

    if current_df.empty and not final_df.empty:
        current_df = final_df.copy()

    if current_df.empty:
        pd.DataFrame().to_csv(current_path, index=False, encoding="utf-8-sig")
    else:
        current_df.to_csv(current_path, index=False, encoding="utf-8-sig")

    annual_file_written = False
    annual_rows = 0
    if not older_df.empty:
        older_df.to_csv(annual_path, index=False, encoding="utf-8-sig")
        annual_file_written = True
        annual_rows = len(older_df)
    elif not annual_path.exists():
        annual_path = None

    # TradingDailyReport 不再累積多個日檔，僅保留「當年度活檔 + 截止去年封存檔」。
    # 其餘 dataset 的歷史檔保留規則不受影響。
    cleanup_trading_daily_report_files(current_path, annual_path)

    return {
        "dataset": "TaiwanStockTradingDailyReport",
        "mode": mode,
        "output_file": current_path.name,
        "output_path": str(current_path),
        "output_exists": current_path.exists(),
        "output_size": current_path.stat().st_size if current_path.exists() else 0,
        "rows_written": len(current_df),
        "history_files_kept": keep_history,
        "annual_archive_file": annual_path.name if annual_path is not None else None,
        "annual_archive_written": annual_file_written,
        "annual_archive_rows": annual_rows,
        "archived_to_his": [],
    }


def fetch_rows_per_stock(dataset_name: str, target_ids: list[str], start_date: str, end_date: str | None) -> list[dict]:
    if not target_ids:
        return []

    rows: list[dict] = []
    errors: list[str] = []
    total = len(target_ids)
    print(
        f"{dataset_name}: per-stock incremental query, stocks={total}, start={start_date}, end={end_date}",
        flush=True,
    )

    for idx, sid in enumerate(target_ids, start=1):
        try:
            item_rows = fetch_rows_for_data_id(
                dataset_name=dataset_name,
                data_id=sid,
                start_date=start_date,
                end_date=end_date,
            )
            if item_rows:
                rows.extend(item_rows)
        except Exception as exc:
            errors.append(str(exc))
            if len(errors) <= 3:
                print(
                    f"{dataset_name}: skip {sid} ({idx}/{total}), reason={exc}",
                    flush=True,
                )

    print(
        f"{dataset_name}: per-stock incremental done, rows={len(rows)}, failed_stocks={len(errors)}",
        flush=True,
    )
    return rows


def fetch_rows_for_data_id(dataset_name: str, data_id: str, start_date: str, end_date: str | None) -> list[dict]:
    req = get_finmind_request_kwargs()
    req_params = req.get("params", {})
    req_headers = req.get("headers", {})
    all_rows: list[dict] = []
    chunks = build_finmind_date_chunks(start_date, end_date)
    for chunk_start, chunk_end in chunks:
        params = {
            "dataset": dataset_name,
            "data_id": data_id,
            "start_date": chunk_start,
        }
        if chunk_end and dataset_name not in DATASETS_NO_END_DATE:
            params["end_date"] = chunk_end
        if req_params:
            params.update(req_params)

        label = f"{dataset_name}/{data_id}/{chunk_start}..{chunk_end or 'open'}"
        response = finmind_get(API_URL, params=params,
                               headers=req_headers, label=label)
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"FinMind response is not JSON ({label}): {exc}") from exc

        if response.status_code != 200:
            msg = payload.get("msg") or payload.get(
                "message") or payload.get("status") or response.text[:300]
            raise RuntimeError(
                f"FinMind API error ({label}): status_code={response.status_code}, msg={msg}")

        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("FinMind API returned an unexpected data shape")
        all_rows.extend(rows)

    return all_rows


def build_recent_business_dates(end_date: str, lookback_days: int) -> list[str]:
    end_ts = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(end_ts):
        return []

    dates: list[str] = []
    current = end_ts
    while len(dates) < max(int(lookback_days), 1):
        if current.weekday() < 5:
            dates.append(current.strftime("%Y-%m-%d"))
        current = current - timedelta(days=1)
    return dates


def fetch_rows_trading_daily_report(
    target_ids: list[str],
    start_date: str,
    end_date: str | None,
    apply_start_date_filter: bool,
) -> list[dict]:
    if not end_date:
        raise RuntimeError("TaiwanStockTradingDailyReport requires end_date")

    if not target_ids:
        print(
            "TaiwanStockTradingDailyReport: stocks.csv has no stock ids, skip per-stock incremental fetch",
            flush=True,
        )
        return []

    candidate_dates = build_recent_business_dates(
        end_date, TRADING_DAILY_REPORT_LOOKBACK_DAYS)
    if apply_start_date_filter:
        start_ts = pd.to_datetime(start_date, errors="coerce")
        if pd.notna(start_ts):
            candidate_dates = [
                d for d in candidate_dates if pd.to_datetime(d) >= start_ts]

    if not candidate_dates:
        print(
            "TaiwanStockTradingDailyReport: no target date to fetch after incremental filter",
            flush=True,
        )
        return []

    all_rows: list[dict] = []
    errors: list[str] = []
    success_days = 0
    print(
        "TaiwanStockTradingDailyReport: one-day requests, "
        f"dates={len(candidate_dates)}, scope=stocks.csv, stock_count={len(target_ids)}",
        flush=True,
    )

    for date_str in candidate_dates:
        day_rows = 0
        day_errors: list[str] = []
        day_failed_stocks = 0

        for sid in target_ids:
            try:
                rows = fetch_rows_trading_daily_report_one_day_for_stock(
                    stock_id=sid,
                    date_str=date_str,
                )
            except Exception as exc:
                day_failed_stocks += 1
                if len(day_errors) < 3:
                    day_errors.append(str(exc))
                continue

            if rows:
                all_rows.extend(rows)
                day_rows += len(rows)

        if day_failed_stocks < len(target_ids):
            success_days += 1
        elif day_errors:
            errors.append(day_errors[0])

        if day_failed_stocks > 0:
            sample = day_errors[0] if day_errors else "unknown"
            print(
                "TaiwanStockTradingDailyReport day summary "
                f"{date_str}: rows={day_rows}, failed_stocks={day_failed_stocks}/{len(target_ids)}, sample_error={sample[:180]}",
                flush=True,
            )

    if not all_rows and success_days == 0 and errors:
        if any(is_finmind_permission_error(msg) for msg in errors):
            raise RuntimeError(
                "TaiwanStockTradingDailyReport: blocked by FinMind permission level (free tier). "
                "No rows returned for all one-day requests."
            )
        raise RuntimeError(
            "TaiwanStockTradingDailyReport: all one-day per-stock requests failed. "
            f"sample_error={errors[0][:220]}"
        )

    if not all_rows and success_days == 0:
        raise RuntimeError(
            "TaiwanStockTradingDailyReport: all one-day per-stock requests failed with unknown errors"
        )

    return all_rows


def fetch_rows_trading_daily_report_one_day_for_stock(stock_id: str, date_str: str) -> list[dict]:
    params = {
        "data_id": str(stock_id),
        "date": date_str,
    }

    req = get_finmind_request_kwargs()
    req_params = req.get("params", {})
    req_headers = req.get("headers", {})
    if req_params:
        params.update(req_params)

    response = finmind_get(
        TAIWAN_STOCK_TRADING_DAILY_REPORT_URL,
        params=params,
        headers=req_headers,
        label=f"TaiwanStockTradingDailyReport/{stock_id}/{date_str}",
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"FinMind response is not JSON ({stock_id}/{date_str}): {exc}"
        ) from exc

    if response.status_code != 200:
        msg = payload.get("msg") or payload.get(
            "message") or payload.get("status") or response.text[:300]
        raise RuntimeError(
            f"FinMind API error (TaiwanStockTradingDailyReport/{stock_id}/{date_str}): "
            f"status_code={response.status_code}, msg={msg}"
        )

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("FinMind API returned an unexpected data shape")
    return rows


def fetch_rows_all_market(dataset_name: str, include_data_id_all: bool, start_date: str, end_date: str | None) -> list[dict]:
    req = get_finmind_request_kwargs()
    req_params = req.get("params", {})
    req_headers = req.get("headers", {})
    all_rows: list[dict] = []
    chunks = build_finmind_date_chunks(start_date, end_date)
    for chunk_start, chunk_end in chunks:
        params = {
            "dataset": dataset_name,
            "start_date": chunk_start,
        }
        if chunk_end and dataset_name not in DATASETS_NO_END_DATE:
            params["end_date"] = chunk_end
        if include_data_id_all:
            params["data_id"] = "所有"
        if req_params:
            params.update(req_params)

        label = (
            f"{dataset_name}/all-market/data_id_all={include_data_id_all}/"
            f"{chunk_start}..{chunk_end or 'open'}"
        )
        response = finmind_get(API_URL, params=params,
                               headers=req_headers, label=label)
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"FinMind response is not JSON ({label}): {exc}") from exc

        if response.status_code != 200:
            msg = payload.get("msg") or payload.get(
                "message") or payload.get("status") or response.text[:300]
            raise RuntimeError(
                f"FinMind API error ({label}): status_code={response.status_code}, msg={msg}"
            )

        rows = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("FinMind API returned an unexpected data shape")
        all_rows.extend(rows)

    return all_rows


def resolve_mode_for_dataset(dataset_name: str, global_mode: str) -> str:
    if global_mode in {"full", "incremental"}:
        return global_mode
    if not os.isatty(0):
        return "incremental"
    answer = input(f"{dataset_name}: full download? [y/N]: ").strip().lower()
    return "full" if answer in {"y", "yes", "1"} else "incremental"


def fetch_dataset_rows(
    dataset_name: str,
    start_date: str,
    end_date: str | None,
    target_ids: list[str],
    mode: str,
) -> list[dict]:
    if dataset_name == "TaiwanStockTradingDailyReport":
        # Incremental mode always fetches the latest rolling window for this
        # one-day endpoint; full mode can still respect start_date.
        return fetch_rows_trading_daily_report(
            target_ids,
            start_date,
            end_date,
            apply_start_date_filter=True,
        )

    if dataset_name in PER_STOCK_ONLY_DATASETS:
        if target_ids:
            rows = fetch_rows_per_stock(
                dataset_name,
                target_ids,
                start_date,
                end_date,
            )
            if rows:
                return rows
            print(
                f"{dataset_name}: per-stock query returned no rows, fallback to all-market",
                flush=True,
            )

        rows = fetch_rows_all_market(
            dataset_name,
            include_data_id_all=False,
            start_date=start_date,
            end_date=end_date,
        )
        if rows:
            return rows
        print(
            f"{dataset_name}: all-market fallback returned no rows",
            flush=True,
        )
        return []

    if dataset_name in INDEX_SPECIFIC_DATA_IDS:
        rows: list[dict] = []
        for data_id in INDEX_SPECIFIC_DATA_IDS[dataset_name]:
            try:
                item_rows = fetch_rows_for_data_id(
                    dataset_name=dataset_name,
                    data_id=data_id,
                    start_date=start_date,
                    end_date=end_date,
                )
                rows.extend(item_rows)
                print(
                    f"{dataset_name}: data_id={data_id} returned {len(item_rows)} rows",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"{dataset_name}: data_id={data_id} failed, reason={exc}",
                    flush=True,
                )
        return rows

    errors: list[str] = []
    try:
        rows = fetch_rows_all_market(
            dataset_name, include_data_id_all=True, start_date=start_date, end_date=end_date)
        if rows:
            print(
                f"{dataset_name}: all-market query with data_id=所有 returned rows", flush=True)
            return rows
        print(
            f"{dataset_name}: all-market query with data_id=所有 returned no rows", flush=True)
    except Exception as exc:
        errors.append(str(exc))
        print(f"{dataset_name}: all-market query with data_id=所有 failed, continue fallback. reason={exc}", flush=True)

    try:
        rows = fetch_rows_all_market(
            dataset_name, include_data_id_all=False, start_date=start_date, end_date=end_date)
        if rows:
            print(
                f"{dataset_name}: all-market query without data_id returned rows", flush=True)
            return rows
        print(
            f"{dataset_name}: all-market query without data_id returned no rows", flush=True)
    except Exception as exc:
        errors.append(str(exc))
        print(f"{dataset_name}: all-market query without data_id failed, continue fallback. reason={exc}", flush=True)

    if errors and len(errors) >= 2:
        if any(is_finmind_permission_error(msg) for msg in errors):
            raise RuntimeError(
                f"{dataset_name}: blocked by FinMind permission level (free tier)."
            )
        raise RuntimeError(
            f"{dataset_name}: all-market queries failed. sample_error={errors[0][:220]}"
        )

    # API responded successfully but returned no new rows in this date window.
    return []


def is_empty_fetch_window(start_date: str, end_date: str | None) -> bool:
    if not end_date:
        return False
    start_ts = pd.to_datetime(start_date, errors="coerce")
    end_ts = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start_ts) or pd.isna(end_ts):
        return False
    return bool(start_ts > end_ts)


def sync_one_dataset(dataset_name: str, mode: str, target_ids: list[str], exec_ts: str) -> dict:
    is_one_day = dataset_name in DATASETS_ONE_DAY or dataset_name in DATASETS_FORCE_ONE_DAY
    end_date = resolve_end_date()

    existing_df = load_latest_dataset_output(dataset_name)
    if mode == "full":
        # Keep existing history even in full mode; full means backfill request range,
        # not wipe local output.
        base_df = existing_df
        start_date = end_date if is_one_day else START_DATE
    else:
        base_df = existing_df
        if is_one_day:
            start_date = end_date
        elif dataset_name == "TaiwanStockTradingDailyReport":
            start_date = infer_incremental_start_date_trading_daily_report(
                existing_df, START_DATE
            )
        else:
            start_date = infer_incremental_start_date(existing_df, START_DATE)

    request_end_date: str | None = end_date
    if dataset_name in DATASETS_NO_END_DATE:
        request_end_date = None

    coverage_snapshot = None
    missing_latest_ids: list[str] = []
    if request_end_date and target_ids and (
        dataset_name in PER_STOCK_ONLY_DATASETS
        or dataset_name == "TaiwanStockTradingDailyReport"
    ):
        coverage_snapshot = build_stock_coverage_snapshot(
            existing_df=base_df,
            target_ids=target_ids,
            date_str=request_end_date,
        )
        missing_latest_ids = detect_missing_stock_ids_on_date(
            existing_df=base_df,
            target_ids=target_ids,
            date_str=request_end_date,
        )
        print(
            f"{dataset_name}: latest CSV stock check ({request_end_date}) "
            f"expected={coverage_snapshot.get('target_stock_count')}, "
            f"actual={coverage_snapshot.get('actual_stock_count')}, "
            f"missing={coverage_snapshot.get('missing_stock_count')}",
            flush=True,
        )

    effective_start_date = start_date
    if is_empty_fetch_window(start_date, request_end_date):
        print(
            f"{dataset_name}: no target date to fetch after incremental filter (start_date={start_date}, end_date={request_end_date})",
            flush=True,
        )
        rows = []
        if request_end_date:
            effective_start_date = request_end_date

        if request_end_date and missing_latest_ids:
            print(
                f"{dataset_name}: empty incremental window but missing stocks on {request_end_date}, "
                f"targeted refill stocks={len(missing_latest_ids)}",
                flush=True,
            )
            if dataset_name == "TaiwanStockTradingDailyReport":
                refill_rows: list[dict] = []
                for sid in missing_latest_ids:
                    try:
                        refill_rows.extend(
                            fetch_rows_trading_daily_report_one_day_for_stock(
                                stock_id=sid,
                                date_str=request_end_date,
                            )
                        )
                    except Exception as exc:
                        print(
                            f"{dataset_name}: targeted refill skip {sid}, reason={exc}",
                            flush=True,
                        )
                rows.extend(refill_rows)
            else:
                refill_rows = fetch_rows_backfill_missing_stocks(
                    dataset_name=dataset_name,
                    stock_ids=missing_latest_ids,
                    start_date=request_end_date,
                    end_date=request_end_date,
                )
                rows.extend(refill_rows)
    else:
        rows = fetch_dataset_rows(
            dataset_name, effective_start_date, request_end_date, target_ids, mode)

    if (
        should_run_backfill_missing_history()
        and dataset_name in PER_STOCK_ONLY_DATASETS
        and target_ids
    ):
        missing_history_ids = detect_missing_history_stock_ids(
            existing_df=base_df,
            target_ids=target_ids,
            required_start=START_DATE,
        )
        if missing_history_ids:
            print(
                f"{dataset_name}: history refill missing stocks={len(missing_history_ids)}/{len(target_ids)}",
                flush=True,
            )
            history_rows = fetch_rows_backfill_missing_stocks(
                dataset_name=dataset_name,
                stock_ids=missing_history_ids,
                start_date=START_DATE,
                end_date=request_end_date,
            )
            if history_rows:
                rows.extend(history_rows)

    if should_run_one_time_backfill_missing() and dataset_name in ONE_TIME_BACKFILL_DATASETS:
        stock_ids = load_target_stock_ids_from_stocks_csv()
        missing_ids = detect_missing_history_stock_ids(
            existing_df=base_df,
            target_ids=stock_ids,
            required_start=START_DATE,
        )
        if missing_ids:
            print(
                f"{dataset_name}: one-time backfill missing stocks={len(missing_ids)}/{len(stock_ids)}",
                flush=True,
            )
            backfill_rows = fetch_rows_backfill_missing_stocks(
                dataset_name=dataset_name,
                stock_ids=missing_ids,
                start_date=START_DATE,
                end_date=request_end_date,
            )
            if backfill_rows:
                rows.extend(backfill_rows)
        else:
            print(
                f"{dataset_name}: one-time backfill no missing stocks in stocks.csv",
                flush=True,
            )
    final_df = merge_dedup_with_existing(base_df, rows)

    keep_history = should_keep_history_files()

    if dataset_name == "TaiwanStockTradingDailyReport":
        write_info = write_trading_daily_report_outputs(
            final_df=final_df,
            exec_ts=exec_ts,
            keep_history=keep_history,
            mode=mode,
        )
        write_info.update({
            "rows_received": len(rows),
            "start_date": effective_start_date,
            "end_date": request_end_date,
            "stock_coverage": coverage_snapshot,
        })
        return write_info

    if final_df.empty and dataset_name in NO_EMPTY_OUTPUT_DATASETS:
        fallback_path, fallback_rows = find_latest_non_empty_dataset_file(
            dataset_name)
        if fallback_path is not None:
            print(
                f"{dataset_name}: empty result, keep previous non-empty output {fallback_path.name}",
                flush=True,
            )
            return {
                "dataset": dataset_name,
                "mode": mode,
                "output_file": fallback_path.name,
                "output_path": str(fallback_path),
                "output_exists": True,
                "output_size": fallback_path.stat().st_size,
                "rows_received": len(rows),
                "rows_written": fallback_rows,
                "start_date": effective_start_date,
                "end_date": request_end_date,
                "reused_previous_output": True,
            }
        raise RuntimeError(
            f"{dataset_name}: empty result and no previous non-empty output available"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    existing_files_before_write = list_dataset_files(dataset_name)
    output_path = OUTPUT_DIR / f"{dataset_name}_{exec_ts}.csv"
    if final_df.empty:
        pd.DataFrame().to_csv(output_path, index=False, encoding="utf-8-sig")
        rows_written = 0
    else:
        final_df.to_csv(output_path, index=False, encoding="utf-8-sig")
        rows_written = len(final_df)

    archived_files: list[str] = []
    files_to_archive = [
        path for path in existing_files_before_write if path.name != output_path.name
    ]
    if keep_history and should_archive_previous_files(existing_files_before_write, output_path.name):
        archived_files = archive_dataset_files_to_history(files_to_archive)

    if not keep_history:
        remove_older_dataset_files(dataset_name, output_path)

    return {
        "dataset": dataset_name,
        "mode": mode,
        "output_file": output_path.name,
        "output_path": str(output_path),
        "output_exists": output_path.exists(),
        "output_size": output_path.stat().st_size if output_path.exists() else 0,
        "rows_received": len(rows),
        "rows_written": rows_written,
        "start_date": effective_start_date,
        "end_date": request_end_date,
        "stock_coverage": coverage_snapshot,
        "history_files_kept": keep_history,
        "archived_to_his": archived_files,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync multiple FinMind datasets into date-stamped CSV files.")
    parser.add_argument(
        "--mode",
        choices=["full", "incremental", "ask"],
        default=(os.getenv("R_DATASET_MODE") or "incremental").strip().lower(),
        help="full: re-download from START_DATE; incremental: append from latest local date; ask: prompt per dataset",
    )
    parser.add_argument(
        "--datasets",
        default=(os.getenv("R_DATASETS") or "all").strip(),
        help=(
            "all: run all datasets; or provide comma-separated dataset names, "
            "e.g. TaiwanStockPrice,TaiwanStockPER"
        ),
    )
    parser.add_argument(
        "--list-datasets",
        action="store_true",
        help="Print available dataset names and exit",
    )
    return parser.parse_args()


def resolve_target_datasets(datasets_arg: str) -> list[str]:
    all_datasets = list(dict.fromkeys(DATASETS_RANGE + DATASETS_ONE_DAY))
    raw = str(datasets_arg or "").strip()
    if raw == "" or raw.lower() in {"all", "*"}:
        return all_datasets

    selected = [x.strip() for x in raw.split(",") if x and x.strip()]
    if not selected:
        return all_datasets

    known = set(all_datasets)
    invalid = [name for name in selected if name not in known]
    if invalid:
        raise RuntimeError(
            "Unknown dataset(s): "
            + ", ".join(invalid)
            + ". Available: "
            + ", ".join(all_datasets)
        )

    # Keep user order while deduplicating.
    return list(dict.fromkeys(selected))


def main() -> None:
    args = parse_args()

    if args.list_datasets:
        datasets = list(dict.fromkeys(DATASETS_RANGE + DATASETS_ONE_DAY))
        print(json.dumps({"datasets": datasets}, ensure_ascii=False, indent=2))
        return

    # Authenticate with Google OAuth and download existing My Drive files first.
    # This preserves the original incremental and history behavior.
    download_google_dataset()

    print_finmind_usage_snapshot()
    exec_ts = datetime.utcnow().strftime("%Y%m%d")
    target_ids = load_target_stock_ids_from_stocks_csv()
    print(
        f"dataset sync mode=incremental-cumulative, stocks.csv targets={len(target_ids)}",
        flush=True,
    )

    datasets = resolve_target_datasets(args.datasets)
    print(
        f"target datasets ({len(datasets)}): {', '.join(datasets)}",
        flush=True,
    )
    summaries = []
    failures = []
    for dataset_name in datasets:
        mode = resolve_mode_for_dataset(dataset_name, args.mode)
        try:
            summaries.append(sync_one_dataset(
                dataset_name, mode, target_ids, exec_ts))
        except Exception as exc:
            msg = str(exc)
            print(f"{dataset_name}: failed, skipped. reason={msg}", flush=True)
            failures.append(
                {"dataset": dataset_name, "mode": mode, "error": msg})

    summary = {
        "synced_at_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_count": len(summaries),
        "failure_count": len(failures),
        "datasets": summaries,
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    upload_google_dataset()


if __name__ == "__main__":
    main()
