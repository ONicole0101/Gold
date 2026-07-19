import os
import time

import requests


USER_INFO_URL = "https://api.web.finmindtrade.com/v2/user_info"


def get_finmind_env_token() -> str:
    """Read FinMind token from environment at runtime."""
    value = os.getenv("FINMIND_TOKEN")
    return str(value).strip() if value and str(value).strip() else ""


def get_finmind_env_token_with_retry() -> str:
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
        token = get_finmind_env_token()
        if token:
            return token
        if attempt + 1 < retries and wait_ms > 0:
            time.sleep(wait_ms / 1000.0)

    return ""


def resolve_finmind_token() -> str:
    """Backward-compatible token resolver used by existing modules."""
    return get_finmind_env_token_with_retry()


def mask_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return token[:4] + "..." + token[-4:]


def require_finmind_token() -> str:
    token = resolve_finmind_token()
    if not token:
        raise RuntimeError("FINMIND_TOKEN is not set")
    return token


def get_finmind_auth_headers(token: str | None = None) -> dict:
    resolved = str(token or "").strip(
    ) if token is not None else resolve_finmind_token()
    return {"Authorization": f"Bearer {resolved}"} if resolved else {}


def get_finmind_request_kwargs(token: str | None = None) -> dict:
    resolved = str(token or "").strip(
    ) if token is not None else resolve_finmind_token()
    if not resolved:
        return {"headers": {}, "params": {}}
    return {
        "headers": get_finmind_auth_headers(resolved),
        "params": {"token": resolved},
    }


def login_by_runtime_token(token: str | None = None) -> dict:
    """Always read/resolve token at call time and try DataLoader login once."""
    resolved = str(token or "").strip(
    ) if token is not None else resolve_finmind_token()
    info = {
        "token_present": bool(resolved),
        "token_source": "FINMIND_TOKEN" if resolved else "",
        "token_masked": mask_token(resolved),
        "login_status": "missing_token",
        "login_message": "FINMIND_TOKEN is not set",
    }
    if not resolved:
        return info

    try:
        from FinMind.data import DataLoader

        api = DataLoader()
        api.login_by_token(api_token=resolved)
        info["login_status"] = "ok"
        info["login_message"] = "DataLoader.login_by_token succeeded"
        return info
    except Exception as exc:
        info["login_status"] = "error"
        info["login_message"] = str(exc)
        return info


def get_finmind_token_status() -> dict:
    """Runtime token/login snapshot. This does not reuse import-time login state."""
    return login_by_runtime_token()


def get_finmind_user_info(write_log: bool = False, source: str = "user_info") -> dict:
    """Return runtime token/login/quota snapshot by querying FinMind user_info."""
    _ = source  # reserved for callers that want to pass source context
    login_info = login_by_runtime_token()
    token = resolve_finmind_token()

    info = {
        "ok": False,
        "token_present": bool(token),
        "token_source": "FINMIND_TOKEN" if token else "",
        "token_masked": mask_token(token),
        "login_status": login_info.get("login_status"),
        "login_message": login_info.get("login_message"),
        "user_count": None,
        "api_request_limit": None,
        "remain": None,
        "status_code": None,
        "message": "FINMIND_TOKEN is not set",
    }

    if not token:
        return info

    try:
        res = requests.get(
            USER_INFO_URL, headers=get_finmind_auth_headers(token), timeout=300)
        try:
            payload = res.json()
        except Exception:
            payload = {}

        used = payload.get("user_count") if isinstance(payload, dict) else None
        limit = payload.get("api_request_limit") if isinstance(
            payload, dict) else None

        try:
            used_int = int(used or 0)
            limit_int = int(limit or 0)
            remain = max(limit_int - used_int, 0) if limit_int else 0
        except Exception:
            used_int = used
            limit_int = limit
            remain = None

        msg = ""
        if isinstance(payload, dict):
            msg = payload.get("msg") or payload.get(
                "message") or payload.get("status") or ""
        if not msg:
            msg = res.text[:200]

        ok = res.status_code == 200 and not (
            isinstance(payload, dict) and payload.get("error")
        )
        info.update(
            {
                "ok": ok,
                "login_status": "ok" if ok else "error",
                "user_count": used_int,
                "api_request_limit": limit_int,
                "remain": remain,
                "status_code": res.status_code,
                "message": msg,
            }
        )
    except Exception as exc:
        info.update(
            {
                "ok": False,
                "login_status": "error",
                "status_code": None,
                "message": str(exc),
            }
        )

    # write_log kept for API compatibility. Logging is handled by callers.
    _ = write_log
    return info
