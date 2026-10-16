"""``tasklib.backends.http.request_json`` — the ambiguous-read failure class (task-cli bug).

A connection that drops WHILE reading the response body — after the server already returned a
2xx status — is genuinely ambiguous: the request may have completed server-side (e.g. GitHub
created the issue) even though this process never saw the body. Only ``urllib.error.HTTPError``/
``URLError`` were ever caught here; ``http.client.IncompleteRead``, ``ConnectionResetError``, and
a read-time ``TimeoutError`` (``socket.timeout`` is the same class on 3.10+) are none of those,
so they used to propagate as a bare, unhandled traceback even though the server-side call had
already succeeded. These tests pin the fix: such a failure is caught and re-raised as the new,
distinct :class:`AmbiguousHttpError` — never silently swallowed, never confused with a clean
:class:`HttpError` (a real non-2xx or a connect-time failure).
"""

from __future__ import annotations

import http.client
import ssl

import pytest

from tasklib.backends.http import AmbiguousHttpError, HttpError, request_json


class _FakeResponse:
    """Mimics the context-managed object ``urlopen`` returns: a 2xx status already landed,
    but ``.read()`` blows up mid-body — the exact ambiguous window this module must catch."""

    def __init__(self, status: int, read_exc: Exception) -> None:
        self.status = status
        self._read_exc = read_exc

    def read(self):
        raise self._read_exc

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


@pytest.mark.parametrize(
    "read_exc",
    [
        http.client.IncompleteRead(partial=b"", expected=10),
        ConnectionResetError("connection reset by peer"),
        TimeoutError("timed out"),
        # review finding: both providers are HTTPS-only, and a connection dropped mid-TLS-read
        # typically surfaces as an SSL error, NOT a bare ConnectionResetError — the enumerated
        # allowlist this replaced missed exactly this, the most common real-world trigger.
        ssl.SSLError("decryption failed or bad record mac"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
        ConnectionAbortedError("connection aborted"),
        BrokenPipeError("broken pipe"),
    ],
    ids=[
        "incomplete-read",
        "connection-reset",
        "read-timeout",
        "ssl-error",
        "ssl-eof",
        "connection-aborted",
        "broken-pipe",
    ],
)
def test_request_json_raises_ambiguous_when_read_fails_after_2xx(monkeypatch, read_exc):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(201, read_exc)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError) as exc:
        request_json("https://api.github.com/repos/o/r/issues", method="POST")
    # the message must be informative enough to act on: the method, the URL, and that a status
    # was already received (so the reader knows this isn't "nothing happened").
    assert "POST" in str(exc.value)
    assert "api.github.com" in str(exc.value)
    assert "201" in str(exc.value)


class _FakeResponseWithBody:
    """Mimics ``urlopen``'s returned object when the read itself succeeds but hands back a
    truncated/undecodable body — a connection that closes early WITHOUT tripping
    ``IncompleteRead`` can still leave the caller holding unusable bytes."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


def test_request_json_raises_ambiguous_on_malformed_json_after_2xx(monkeypatch):
    # the read succeeded but the body is truncated mid-JSON — the SAME ambiguous window as an
    # IncompleteRead, just caught one step later (json.loads, not .read()); it must raise the
    # same AmbiguousHttpError, not an uncaught json.JSONDecodeError.
    def fake_urlopen(req, timeout=None):
        return _FakeResponseWithBody(201, b'{"id": 42, "title": "trunc')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


def test_request_json_raises_ambiguous_on_undecodable_body_after_2xx(monkeypatch):
    # a body truncated mid-multibyte-character fails at .decode("utf-8"), before json.loads even
    # runs — same ambiguous window again.
    def fake_urlopen(req, timeout=None):
        return _FakeResponseWithBody(201, "café".encode("utf-8")[:-1])

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


def test_ambiguous_http_error_is_not_an_http_error():
    # a caller's `except HttpError` (the "clean 4xx / connect failure" bucket) must NOT also
    # catch this — that would make a genuine server-side success indistinguishable from a
    # clean, nothing-happened refusal.
    assert not issubclass(AmbiguousHttpError, HttpError)


def test_request_json_still_raises_http_error_for_a_normal_4xx(monkeypatch):
    # unrelated regression guard: the pre-existing clean-failure path must be untouched.
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as exc:
        request_json("https://api.github.com/repos/o/r/issues", method="POST")
    assert exc.value.status == 422


def test_request_json_wraps_invalid_url_port_as_http_error(monkeypatch):
    # review finding: request_bytes's InvalidURL fix (below) applies verbatim to request_json —
    # a malformed authority (e.g. an ambient GITHUB_API_URL override with a bad port) raises the
    # identical bare http.client.InvalidURL here too, escaping the HTTPError/URLError-only
    # except clauses and crashing the caller instead of a clean BackendError. InvalidURL always
    # fires BEFORE anything is sent (constructing/validating the request), so it's unambiguously
    # safe to classify as a clean, retryable HttpError — unlike a failure raised from reading
    # back a response, which is a materially harder classification problem intentionally left
    # out of scope (see the docstring and the "left deliberately unhandled" test below).
    def fake_urlopen(req, timeout=None):
        raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError):
        request_json("https://ghe.corp:bad/api/v3/repos/o/r/issues", method="POST")


def test_request_json_wraps_construction_time_valueerror_as_http_error():
    # review finding (round 5): a bracket-malformed authority ("https://[bad") makes
    # urllib.request.Request() itself raise a bare ValueError at CONSTRUCTION time — a step
    # earlier than the connect-time InvalidURL the test above covers. Left unguarded, this
    # escapes raw before urlopen is even reached (no monkeypatch needed here: construction fails
    # before any network call would happen, so this is a real, hermetic exercise of the actual
    # bug, not a simulation of it).
    with pytest.raises(HttpError):
        request_json("https://[bad/api/v3/repos/o/r/issues", method="POST")


def test_request_json_bad_status_line_is_conservatively_ambiguous(monkeypatch):
    # A malformed status gives no evidence that the write did not commit.
    def fake_urlopen(req, timeout=None):
        raise http.client.BadStatusLine("garbage status line")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(AmbiguousHttpError):
        request_json("https://api.github.com/repos/o/r/issues", method="POST")


# ── request_bytes + the same-host redirect guard (credential-exfiltration hazard) ──────────


def test_request_bytes_wraps_invalid_url_port_as_http_error(monkeypatch):
    # review finding (PR #90, task-cli): a tracker-controlled attachment url like
    # "https://uploads.linear.app:bad/asset" parses fine at urlparse()-time — `.hostname` doesn't
    # validate the port — but http.client raises a BARE http.client.InvalidURL the moment it
    # tries to actually open the connection. That's neither urllib.error.HTTPError nor URLError,
    # so left uncaught it propagates past every caller's HttpError-only `except` clause (e.g.
    # LinearBackend.fetch_attachment_bytes) and crashes the whole `task read --save-attachments`
    # command instead of failing just the one malformed attachment. Must be wrapped in HttpError.
    from tasklib.backends.http import request_bytes

    def fake_urlopen(req, timeout=None):
        raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(HttpError) as exc:
        request_bytes("https://uploads.linear.app:bad/asset")
    assert "invalid url" in str(exc.value).lower()


def test_request_bytes_wraps_invalid_url_port_through_guarded_opener(monkeypatch):
    # same fix, through the OTHER opener path (same_host_redirects_only=True) — this is the one
    # LinearBackend.fetch_attachment_bytes actually uses for a native asset url (it always passes
    # same_host_redirects_only=is_linear_asset), so the plain-opener test above alone wouldn't
    # catch a fix that only patched one of the two call paths.
    from tasklib.backends import http as http_mod

    class _FakeOpener:
        def open(self, req, timeout=None):
            raise http.client.InvalidURL("nonnumeric port: 'bad'")

    monkeypatch.setattr(http_mod, "_SAME_HOST_OPENER", _FakeOpener())
    with pytest.raises(HttpError):
        http_mod.request_bytes("https://uploads.linear.app:bad/asset", same_host_redirects_only=True)


def test_request_bytes_wraps_construction_time_valueerror_as_http_error():
    # review finding (round 5): same construction-time ValueError as request_json's counterpart
    # test above, through request_bytes — this is the function LinearBackend._upload_and_attach
    # calls with Linear's OWN (not fully trusted) signed uploadUrl, so this gap was live in
    # exactly the case the InvalidURL docstring already called out as "the live case today".
    # Hermetic: construction fails before urlopen is reached, no monkeypatch needed.
    from tasklib.backends.http import request_bytes

    with pytest.raises(HttpError):
        request_bytes("https://[bad/asset")


def test_request_bytes_returns_raw_body(monkeypatch):
    from tasklib.backends.http import request_bytes

    class _FakeResp:
        status = 200

        def read(self):
            return b"\x89PNG raw bytes"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
    assert request_bytes("https://uploads.linear.app/asset-1") == b"\x89PNG raw bytes"


def test_same_host_redirect_handler_allows_same_host_redirect():
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    # same host + path change → delegates to the stdlib handler (returns a new Request, doesn't raise)
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/b")
    assert result is not None
    assert result.full_url == "https://uploads.linear.app/b"


def test_same_host_redirect_handler_refuses_cross_host_redirect():
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://attacker.example.com/steal")


def test_same_host_redirect_handler_refuses_different_explicit_port_same_host():
    # review finding: the port-matching test coverage only exercised the ALLOW direction
    # (default-port normalization) — nothing pinned that two genuinely DIFFERENT explicit
    # ports on the same host are still refused. Without this, a regression collapsing
    # _same_origin's port check to "always True after the hostname matches" would pass every
    # other test in this file.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app:8443/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app:444/b")
    req2 = urllib.request.Request("https://uploads.linear.app:8443/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req2, None, 302, "Found", {}, "https://uploads.linear.app/b")


def test_same_host_redirect_handler_allows_explicit_default_port_redirect():
    # review finding (GitHub PR #109 Codex pass): comparing raw netloc strings treats
    # "host:443" and "host" as different hosts even though they're the same origin — a
    # genuinely same-origin redirect that happens to add/drop the explicit default port
    # must still be allowed.
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app:443/a")
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/b")
    assert result is not None
    assert result.full_url == "https://uploads.linear.app/b"

    req2 = urllib.request.Request("https://uploads.linear.app/a")
    result2 = handler.redirect_request(req2, None, 302, "Found", {}, "https://uploads.linear.app:443/b")
    assert result2 is not None


def test_same_host_redirect_handler_allows_hostname_case_change():
    # review finding (GitHub PR #109 Codex pass): a hostname differing only in case is the
    # same origin — urllib.parse already lowercases .hostname, but the fix must actually use
    # that normalized field, not the raw (case-preserving) netloc.
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://Uploads.Linear.App/a")
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/b")
    assert result is not None


def test_same_host_redirect_handler_refuses_out_of_range_port_in_redirect_target():
    # review finding (GitHub PR #109 Codex pass): a malformed port in the redirect Location
    # (out of range, or non-numeric) must be refused cleanly (the existing "unparseable
    # Location" HTTPError, via safe_urlparse returning None), never let ParseResult.port's
    # ValueError escape uncaught.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app:99999/b")


def test_same_host_redirect_handler_refuses_malformed_original_url_port():
    # review finding (GitHub PR #109 Codex pass): _same_origin now touches .port on the
    # ORIGINAL request URL too (old), not just the redirect target — a malformed port there
    # must also be refused cleanly (fail closed), not raise ValueError uncaught.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app:99999/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/b")


def test_same_host_redirect_handler_refuses_https_to_http_downgrade_same_host():
    # review finding: a same-HOST check alone misses a same-host SCHEME downgrade — a
    # compromised uploads.linear.app could 302 https -> http on the identical hostname and a
    # host-only guard would wave the Authorization header straight through onto plaintext.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://uploads.linear.app/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://uploads.linear.app/a")


def test_same_host_redirect_handler_refuses_malformed_redirect_location_via_real_entry_point():
    # review finding (round 4): the round-3 version of this test drove redirect_request()
    # directly — but the REAL stdlib entry point is http_error_302, which calls its OWN
    # urlparse(newurl) on the raw header BEFORE ever calling redirect_request. Verified against
    # the actual installed stdlib source: a bracket-malformed Location raises ValueError right
    # there, never reaching redirect_request at all — the round-3 "fix" was dead code for this
    # exact scenario, and the round-3 test's bypass hid that. Drive http_error_302 itself (the
    # real first place this process sees server-controlled redirect data) to prove the guard
    # actually engages.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://hyperide.github.io/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.http_error_302(
            req, None, 302, "Found", {"location": "https://[bad"}
        )


def test_same_host_redirect_handler_refuses_malformed_location_with_leading_whitespace():
    # review finding (round 8): stdlib's OWN http_error_302 strips the header value
    # (newurl.strip()) before ever parsing it — this guard must mirror that, not just the
    # presence-based selection, or a leading-whitespace malformed Location would parse HERE as
    # a harmless relative path (scheme detection breaks on the leading space) while stdlib's
    # own stripped version could still be the malformed authority this guard exists to catch.
    # Not reachable through a real HTTP response (header-value parsing already strips leading
    # OWS before this code sees it) but this guard's whole contract is "check the exact value
    # super() will use" — pin it regardless.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://hyperide.github.io/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.http_error_302(req, None, 302, "Found", {"location": " https://[bad"})


def test_same_host_redirect_handler_refuses_malformed_uri_header_variant():
    # http_error_302 also accepts a "uri" header as a Location fallback (stdlib behavior,
    # `elif "uri" in headers`) — must get the same pre-validation.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://hyperide.github.io/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.http_error_302(req, None, 302, "Found", {"uri": "https://[bad"})


def test_same_host_redirect_handler_ignores_uri_header_when_location_present_even_if_empty():
    # review finding (round 5): stdlib selects the redirect target by HEADER PRESENCE
    # (`if "location" in headers: ... elif "uri" in ...`), not truthiness. A malformed "uri"
    # header must NOT be consulted (let alone cause a refusal) when a "location" header is
    # already present — even an empty one — because stdlib itself would never look at "uri" in
    # that case. This pins the fix against a truthiness-based `... or ...` selection, which
    # would have wrongly fallen through to (and refused on) the malformed "uri" here.
    import io
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    opened = []

    class _FakeParent:
        def open(self, request, timeout=None):
            opened.append(request)
            return "SENTINEL_RESPONSE"

    handler = _SameHostRedirectHandler()
    handler.parent = _FakeParent()
    req = urllib.request.Request("https://hyperide.github.io/a")
    req.timeout = 60  # normally set by OpenerDirector.open() before dispatch.
    fp = io.BytesIO(b"")
    fp.status = 302
    result = handler.http_error_302(
        req, fp, 302, "Found", {"location": "", "uri": "https://[bad"}
    )
    # an empty Location joins onto the request's own URL (urljoin semantics) — same host,
    # no refusal, no ValueError from the malformed "uri" that was correctly never consulted.
    assert result == "SENTINEL_RESPONSE"
    assert len(opened) == 1


def test_same_host_redirect_handler_all_redirect_codes_share_the_guarded_method():
    # review finding (round 4): the stdlib maps 301/302/303/307/308 onto the SAME
    # http_error_302 method via class-level aliasing done AT THE PARENT's definition time —
    # overriding http_error_302 alone in a subclass does NOT retroactively redirect those other
    # four names to the override (verified live: attribute lookup for e.g. http_error_301 on
    # the subclass still resolves to the PARENT's original, unguarded function object unless
    # explicitly re-aliased). Pin the re-aliasing structurally so a future refactor that drops
    # it (e.g. "simplifying" back to overriding just http_error_302) fails loudly here instead
    # of silently leaving 4 of 5 redirect codes unguarded.
    from tasklib.backends.http import _SameHostRedirectHandler

    guarded = _SameHostRedirectHandler.http_error_302
    for name in ("http_error_301", "http_error_303", "http_error_307", "http_error_308"):
        assert getattr(_SameHostRedirectHandler, name) is guarded, (
            f"{name} is not aliased to the guarded http_error_302 — that redirect code would "
            "bypass every guard in this class"
        )


def test_same_host_redirect_handler_301_redirect_via_real_entry_point_also_guarded():
    # concrete instance of the aliasing pin above: an actual 301 (not just 302) with a
    # malformed Location must be refused too, driven through the real entry point.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("https://hyperide.github.io/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.http_error_301(
            req, None, 301, "Moved Permanently", {"location": "https://[bad"}
        )


def test_same_host_redirect_handler_valid_redirect_still_delegates_through_http_error_302():
    # the happy path: a well-formed, same-host Location must still reach the real stdlib
    # machinery and produce a genuine redirected Request — the new pre-validation must not
    # accidentally swallow or short-circuit a legitimate redirect. Past redirect_request(),
    # stdlib's http_error_302 calls self.parent.open(new, ...) — a directly-instantiated
    # handler (not built via build_opener()) has no real .parent, so fake one narrowly enough
    # to observe what request it was asked to open, without needing a real network stack.
    import io
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    opened = []

    class _FakeParent:
        def open(self, request, timeout=None):
            opened.append(request)
            return "SENTINEL_RESPONSE"

    handler = _SameHostRedirectHandler()
    handler.parent = _FakeParent()
    req = urllib.request.Request("https://hyperide.github.io/a")
    req.timeout = 60  # normally set by OpenerDirector.open() before dispatch; set by hand
    # since this test drives the handler directly, bypassing that outer layer.
    fp = io.BytesIO(b"")
    fp.status = 302
    result = handler.http_error_302(
        req, fp, 302, "Found", {"location": "https://hyperide.github.io/b"}
    )
    assert result == "SENTINEL_RESPONSE"
    assert len(opened) == 1
    assert opened[0].full_url == "https://hyperide.github.io/b"


def test_request_bytes_wraps_malformed_redirect_location_as_http_error(monkeypatch):
    # full stack: request_bytes (through the guarded opener) must convert the malformed-
    # redirect refusal into a clean HttpError (status 302, from request_bytes's own
    # urllib.error.HTTPError except clause — its message is built from status+url only, the
    # refusal reason lives in the exception this simulates, already pinned at the handler
    # level above), not let a raw ValueError escape uncaught.
    import urllib.error

    from tasklib.backends.http import HttpError, request_bytes

    def fake_open(req, timeout=60):
        # simulate what the guarded opener's redirect handling raises when it hits a
        # malformed Location — exercised end-to-end via the handler test above; here we
        # confirm request_bytes's own except clauses actually catch and wrap it, rather than
        # letting it (or a raw ValueError) escape uncaught.
        raise urllib.error.HTTPError(
            "https://hyperide.github.io/a",
            302,
            "refused unparseable redirect Location from https://hyperide.github.io/a: 'https://[bad'",
            {},
            None,
        )

    monkeypatch.setattr("tasklib.backends.http._SAME_HOST_OPENER.open", fake_open)
    with pytest.raises(HttpError, match="HTTP 302"):
        request_bytes(
            "https://hyperide.github.io/a", method="GET", same_host_redirects_only=True
        )


def test_same_host_redirect_handler_allows_http_to_https_upgrade_same_host():
    # the inverse (http -> https) is a safe upgrade, not a downgrade — must still be allowed.
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    handler = _SameHostRedirectHandler()
    req = urllib.request.Request("http://uploads.linear.app/a")
    result = handler.redirect_request(req, None, 302, "Found", {}, "https://uploads.linear.app/a")
    assert result is not None


def test_request_bytes_same_host_redirects_only_uses_guarded_opener(monkeypatch):
    # request_bytes must route through the guarded opener (not plain urlopen) when the caller
    # says the request carries a credential — this is the load-bearing wiring the review finding
    # was about; a unit test on the handler alone can't catch a caller forgetting to opt in.
    from tasklib.backends import http as http_mod

    seen = {}

    class _FakeOpener:
        def open(self, req, timeout=None):
            seen["used_guarded_opener"] = True

            class _Resp:
                status = 200

                def read(self):
                    return b"ok"

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            return _Resp()

    monkeypatch.setattr(http_mod, "_SAME_HOST_OPENER", _FakeOpener())
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not use the plain opener")),
    )
    result = http_mod.request_bytes("https://uploads.linear.app/asset-1", same_host_redirects_only=True)
    assert result == b"ok"
    assert seen.get("used_guarded_opener") is True
