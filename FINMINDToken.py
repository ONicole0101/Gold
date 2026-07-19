import os
import base64
import sys
from finmind_auth import resolve_finmind_token

token = resolve_finmind_token()
if not token:
    print("[ERROR] FINMIND_TOKEN environment variable not set. Set it and re-run.")
    sys.exit(1)

print("[DEBUG] token exists:", True)
print("[DEBUG] token length:", len(token))
Preview = token[:8]+"..." if len(token) > 8 else token
lastview = "..." + token[-8:] if len(token) > 8 else token
print("[DEBUG] token view:", Preview + lastview)
