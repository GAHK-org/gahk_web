"""HMAC request signing for /api/* (the contract in intern's design: both sides must match exactly).

    X-Gahk-Timestamp: <unix seconds>
    X-Gahk-Signature: hex(hmac_sha256(KEY, "{ts}\\n{METHOD}\\n{path}\\n{sha256_hex(body)}"))

`path` is the request path without the query string. A request more than five minutes off, or with a
signature that does not match (compared in constant time), is refused.
"""

import hashlib
import hmac
import re
import time

MAX_SKEW_SECONDS = 300


def sign(key: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    message = f"{timestamp}\n{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def signed_headers(key: str, method: str, path: str, body: bytes, now: float | None = None) -> dict[str, str]:
    """Headers for a signed request (what intern sends; used by tests and tools)."""
    timestamp = str(int(time.time() if now is None else now))
    return {
        "X-Gahk-Timestamp": timestamp,
        "X-Gahk-Signature": sign(key, timestamp, method, path, body),
    }


def verify(
    key: str,
    timestamp: str | None,
    signature: str | None,
    method: str,
    path: str,
    body: bytes,
    now: float | None = None,
) -> str | None:
    """None when the request is authentic, else the reason it is not."""
    if not key:
        return "signing is not configured"
    if not timestamp or not signature:
        return "missing signature"
    if not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return "bad timestamp"
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > MAX_SKEW_SECONDS:
        return "timestamp outside the allowed window"
    given = signature.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", given):
        return "bad signature"
    expected = sign(key, timestamp, method, path, body)
    if not hmac.compare_digest(expected, given):
        return "bad signature"
    return None
