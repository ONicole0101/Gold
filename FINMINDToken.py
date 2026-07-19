import sys
from finmind_auth import get_finmind_user_info, resolve_finmind_token


def print_finmind_runtime_status() -> int:
    token = resolve_finmind_token()
    if not token:
        print("[ERROR] FINMIND_TOKEN environment variable not set. Set it and re-run.")
        return 1

    print("[DEBUG] token exists:", True)
    print("[DEBUG] token length:", len(token))
    preview = token[:8] + "..." if len(token) > 8 else token
    lastview = "..." + token[-8:] if len(token) > 8 else token
    print("[DEBUG] token view:", preview + lastview)

    info = get_finmind_user_info(write_log=False, source="FINMINDToken.py")
    print("[DEBUG] login status:", info.get("login_status"))
    print("[DEBUG] login message:", info.get("login_message"))
    print("[DEBUG] usage:",
          f"{int(info.get('user_count') or 0)}/{int(info.get('api_request_limit') or 0)}")
    print("[DEBUG] remain:", int(info.get("remain") or 0))
    return 0


if __name__ == "__main__":
    sys.exit(print_finmind_runtime_status())
