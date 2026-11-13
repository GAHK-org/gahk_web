import time

from conftest import SYNC_KEY, Client

from gahk_web import signing


def test_good_signature_is_accepted() -> None:
    headers = signing.signed_headers(SYNC_KEY, "POST", "/api/publish", b"{}")
    reason = signing.verify(
        SYNC_KEY, headers["X-Gahk-Timestamp"], headers["X-Gahk-Signature"], "POST", "/api/publish", b"{}"
    )
    assert reason is None


def test_signature_covers_method_path_and_body() -> None:
    headers = signing.signed_headers(SYNC_KEY, "POST", "/api/publish", b"{}")
    ts, sig = headers["X-Gahk-Timestamp"], headers["X-Gahk-Signature"]
    assert signing.verify(SYNC_KEY, ts, sig, "GET", "/api/publish", b"{}") == "bad signature"
    assert signing.verify(SYNC_KEY, ts, sig, "POST", "/api/rebuild", b"{}") == "bad signature"
    assert signing.verify(SYNC_KEY, ts, sig, "POST", "/api/publish", b"{ }") == "bad signature"
    assert signing.verify("x" * 40, ts, sig, "POST", "/api/publish", b"{}") == "bad signature"


def test_expired_and_future_timestamps_are_refused() -> None:
    now = time.time()
    old = signing.signed_headers(SYNC_KEY, "GET", "/api/status", b"", now=now - 301)
    assert (
        signing.verify(
            SYNC_KEY, old["X-Gahk-Timestamp"], old["X-Gahk-Signature"], "GET", "/api/status", b"", now=now
        )
        == "timestamp outside the allowed window"
    )
    future = signing.signed_headers(SYNC_KEY, "GET", "/api/status", b"", now=now + 301)
    assert signing.verify(
        SYNC_KEY, future["X-Gahk-Timestamp"], future["X-Gahk-Signature"], "GET", "/api/status", b"", now=now
    )
    within = signing.signed_headers(SYNC_KEY, "GET", "/api/status", b"", now=now - 299)
    assert (
        signing.verify(
            SYNC_KEY,
            within["X-Gahk-Timestamp"],
            within["X-Gahk-Signature"],
            "GET",
            "/api/status",
            b"",
            now=now,
        )
        is None
    )


def test_matches_the_documented_scheme() -> None:
    # hex(hmac_sha256(KEY, "{ts}\n{METHOD}\n{path}\n{sha256_hex(body)}")), computed independently.
    import hashlib
    import hmac

    message = "1700000000\nPOST\n/api/publish\n" + hashlib.sha256(b"{}").hexdigest()
    expected = hmac.new(SYNC_KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
    assert signing.sign(SYNC_KEY, "1700000000", "post", "/api/publish", b"{}") == expected


def test_missing_or_malformed_headers() -> None:
    assert signing.verify(SYNC_KEY, None, None, "GET", "/api/status", b"") == "missing signature"
    assert signing.verify(SYNC_KEY, "abc", "0" * 64, "GET", "/api/status", b"") == "bad timestamp"
    assert signing.verify("", "1", "0" * 64, "GET", "/api/status", b"") == "signing is not configured"


def test_api_refuses_unsigned_and_wrongly_signed_calls(client: Client) -> None:
    assert client.get("/api/status").status == 401
    wrong = Client(client.app, key="w" * 40)
    result = wrong.api("GET", "/api/status")
    assert result.status == 401
    assert result.json() == {"error": "bad signature"}
    assert client.api("GET", "/api/status").status == 200


def test_health_needs_no_signature(client: Client) -> None:
    result = client.get("/api/health")
    assert result.status == 200
    assert result.json() == {"ok": True, "version": "dev", "revision": None}
