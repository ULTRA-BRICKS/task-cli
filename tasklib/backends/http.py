"""Tiny stdlib ``urllib`` JSON-HTTP helper shared by the backends.

No ``requests``, no per-call subprocess — just ``urllib.request`` with JSON bodies and a
bearer/header auth scheme the caller supplies. Kept minimal: a single ``request_json`` that
returns parsed JSON or raises :class:`HttpError` with the status + a (token-free) snippet.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import ParseResult, urlparse


class HttpError(RuntimeError):
    """An HTTP request failed without an uncertain mutation outcome."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def safe_urlparse(url: str) -> ParseResult | None:
    """``urlparse(url)`` — validated enough to actually be usable, or ``None`` on ANY
    malformation. review finding: a caller of this is typically parsing tracker/attachment-
    controlled data (not a URL this process constructed), and there are TWO distinct places a
    "successfully parsed" url can still be garbage:

    - ``urlparse()`` itself raises ``ValueError`` at PARSE time for some malformed inputs (e.g.
      invalid IPv6-bracket syntax like ``"https://[bad"``).
    - ``.port`` is a LAZY property that validates at ACCESS time, not parse time — a
      syntactically well-formed url with a non-numeric/out-of-range port
      (``"https://host:bad/x"``) parses fine but raises ``ValueError`` the moment ``.port`` is
      actually touched. (``.hostname`` does NOT have this hazard — verified against
      ``urllib.parse``'s implementation, it never raises on access; only ``.port`` does the
      int-cast/range check that can fail. Nothing else here needs eager-touching.)

    Touching ``.port`` eagerly means every caller — a backend's own native-asset-host check no
    less than a CLI-layer trusted-host check — gets the SAME single "None means unusable, for
    any reason" contract, instead of each reimplementing its own try/except (review finding,
    round 2: two independent copies of exactly this guard were found drifting apart during a
    review — ``backends.linear``'s former ``_safe_urlparse`` and
    ``cli._is_trusted_attachment_host`` both now delegate here).
    """
    try:
        parsed = urlparse(url)
        _ = (
            parsed.port
        )  # property access validates port syntax (raises ValueError if malformed)
    except ValueError:
        return None
    return parsed


def https_host_matches(url: str, hosts: frozenset[str]) -> bool:
    """Whether ``url`` is a well-formed, default-port ``https://`` URL whose host is exactly
    (not a suffix/prefix/subdomain of) one of ``hosts``. The one shared predicate behind BOTH
    of this codebase's host-allowlist checks — a backend's own native-asset-host check
    (:data:`~tasklib.backends.linear._LINEAR_ASSET_HOSTS`) and the CLI-layer
    ``trusted_attachment_hosts`` widening (``cli._is_trusted_attachment_host``).

    review finding (round 4): these two calls used to be near-identical, independently
    maintained copies (``safe_urlparse`` → scheme check → frozenset membership) — and they had
    ALREADY drifted: the CLI-layer check rejected a non-default port (``:8443``), the native
    check did not, so a hostile ticket's attachment URL could aim a REAL Linear API key at an
    arbitrary port on ``uploads.linear.app`` (attachment urls are tracker-controlled data, same
    threat model as everywhere else in this module). This is exactly the drift class
    :func:`safe_urlparse`'s own docstring already exists to prevent for URL *parsing* — the
    same discipline now applies to host *matching* too, in one place both callers share, so a
    future asymmetry can't quietly reappear between them.

    ``https`` and default-port (``None``/``443``) only, deliberately not parameterized: neither
    current caller has a legitimate reason to trust a non-default port, and a caller that
    someday does needs to make that a conscious, separately-reviewed decision, not inherit a
    permissive default silently.
    """
    parsed = safe_urlparse(url)
    if parsed is None or parsed.scheme != "https":
        return False
    if parsed.port not in (None, 443):
        return False
    return parsed.hostname in hosts


# The read/parse-time failures that can happen AFTER ``urlopen`` already returned a 2xx status.
# Broad ON PURPOSE (review finding: an earlier version enumerated ConnectionResetError/
# TimeoutError/IncompleteRead individually and missed the MOST common real-world case — both
# providers are HTTPS-only, and a connection dropped mid-TLS-read typically surfaces as
# ``ssl.SSLError``/``ssl.SSLEOFError``, not a bare ``ConnectionResetError``). Any exception in
# this window means the same thing — "the status already landed, something after that failed" —
# so the net is cast at the CATEGORY level instead of an enumerated allowlist that a new failure
# mode can silently fall outside of:
#   - OSError: every OS/TLS-level read failure (ConnectionResetError, TimeoutError/
#     socket.timeout — the same class since Python 3.10 — ssl.SSLError/SSLEOFError,
#     ConnectionAbortedError, BrokenPipeError, ...).
#   - http.client.HTTPException: httplib-native read failures (IncompleteRead and friends).
#   - UnicodeDecodeError / json.JSONDecodeError: the body arrived but is truncated/malformed —
#     a connection that closes early without tripping IncompleteRead can still hand back a body
#     that doesn't decode/parse; that's the identical ambiguous window, just caught one step
#     later, so it must raise the SAME error, not a bare traceback.
_AMBIGUOUS_READ_EXCEPTIONS = (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError)


class AmbiguousHttpError(RuntimeError):
    """The request may have completed, but no usable response was received.

    Header/open failures can happen after sending a write. Body failures can happen
    after a successful status. Neither is evidence of a safe-to-replay operation.
    Deliberately separate from HttpError so callers must handle uncertainty explicitly.
    """


def _raise_redirect_error(req: urllib.request.Request, status: int = 0) -> None:
    """A redirect receipt cannot prove that the original mutation did not commit."""
    # urllib shares redirect_dict with the original request before following.
    # Refused/malformed redirects can fail before that assignment, with a 3xx.
    if req.get_method().upper() not in {"GET", "HEAD"} and (
        getattr(req, "redirect_dict", None) or status in {301, 302, 303, 307, 308}
    ):
        raise AmbiguousHttpError(
            "redirect response unusable — the request may have completed server-side; "
            "check the backend before retrying"
        ) from None


def _raise_open_error(exc: BaseException, method: str) -> None:
    """Reads are clean failures; only proven pre-send write failures are clean."""
    if method.upper() in {"GET", "HEAD"}:
        raise HttpError(0, f"connection interrupted while awaiting the response for {method}") from None
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)):
        raise HttpError(0, f"connection failed before sending {method}") from None
    raise AmbiguousHttpError(
        f"connection interrupted while awaiting the response for {method} — "
        "the request may have completed server-side; check the backend before retrying"
    ) from None


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: int = 30,
) -> Any:
    """Return parsed JSON, or None for an empty response.

    Direct HTTP rejections and proven pre-send URL validation/connect refusal raise HttpError.
    Failures after a write redirects remain ambiguous, including later HTTP rejections.
    GET/HEAD failures are clean read errors. For writes, header/open failures without
    proof of non-delivery and unusable response bodies raise AmbiguousHttpError.
    No automatic replay.
    """
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    hdrs.setdefault("Accept", "application/json")
    hdrs.setdefault("User-Agent", "task-cli")

    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    except ValueError as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted provider hosts
            try:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
            except _AMBIGUOUS_READ_EXCEPTIONS as exc:
                if method.upper() in {"GET", "HEAD"}:
                    raise HttpError(resp.status, f"unusable response for {method} (status {resp.status})") from None
                # the status line already arrived (2xx — urlopen raises HTTPError itself for a
                # non-2xx, so we never reach here otherwise) but reading/decoding/parsing the
                # body then failed: the request may have already completed server-side. See
                # AmbiguousHttpError.
                raise AmbiguousHttpError(
                    f"connection interrupted while reading the response for {method} {url} "
                    f"(status {resp.status} was already returned) — the request may have "
                    "completed server-side even though this process never saw a usable body"
                ) from exc
    except urllib.error.HTTPError as exc:
        _raise_redirect_error(req, exc.code)
        body = ""
        try:
            body = exc.read().decode("utf-8")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(exc.code, f"HTTP {exc.code} for {method} {url}", body) from exc
    except http.client.InvalidURL as exc:
        _raise_redirect_error(req)
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        # ValueError can come from parsing a malformed redirect before urllib
        # records its history; it is not proof that the write was never sent.
        _raise_redirect_error(req)
        _raise_open_error(exc, method)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _same_origin(a: ParseResult, b: ParseResult) -> bool:
    """Whether ``a`` and ``b`` are the same origin (host + effective port), tolerant of an
    explicit default port and hostname case (review finding, GitHub PR #109 Codex pass):
    comparing raw ``.netloc`` strings treats ``https://host:443/x`` and ``https://host/x`` —
    or a hostname differing only in case — as different hosts, wrongly refusing a genuinely
    same-origin redirect. ``.hostname`` is already lowercased by ``urllib.parse``.

    Port comparison is skipped entirely when NEITHER side names an explicit port — this
    preserves the pre-existing http-to-https same-host UPGRADE allowance (both sides have no
    explicit port; their scheme DEFAULT ports genuinely differ, 80 vs 443, but that's not the
    signal this function is checking — the caller's separate scheme-downgrade check is what
    actually gates https/http transitions). Once EITHER side names an explicit port, both
    ports are resolved to their effective value (explicit, or that URL's OWN scheme default)
    and compared — this is what makes ``https://host:443`` match ``https://host``.
    """
    if a.hostname != b.hostname:
        return False
    if a.port is None and b.port is None:
        return True
    a_port = a.port if a.port is not None else _DEFAULT_PORTS.get(a.scheme)
    b_port = b.port if b.port is not None else _DEFAULT_PORTS.get(b.scheme)
    return a_port == b_port


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect to a different host, OR a scheme downgrade (https -> http),
    than the request that was actually made — used whenever the request carries a
    caller-supplied auth header (review finding: urllib follows redirects by default, so a
    malicious/compromised server could either 302 the request to an attacker-controlled host, or
    302 it to a PLAINTEXT `http://` URL on the SAME host, and urllib would happily replay every
    header, including ``Authorization``, onto it either way — a same-host-only check misses the
    downgrade case)."""

    def http_error_302(self, req, fp, code, msg, headers):  # noqa: N802 - stdlib override name
        # review finding (round 4): the stdlib's OWN `HTTPRedirectHandler.http_error_302` reads
        # the raw `Location`/`URI` header and calls `urlparse(newurl)` on it ITSELF, before ever
        # calling `redirect_request` — verified against the actual installed stdlib source. A
        # round-3 fix that only overrode `redirect_request` (validating there) was therefore
        # DEAD CODE for a bracket-malformed Location: stdlib's own pre-`redirect_request`
        # `urlparse()` call raises the uncaught `ValueError` first, before this class's guard
        # ever runs — a genuinely broken fix that passing tests hid, because both tests drove
        # `redirect_request` (or a stand-in exception) directly rather than the real
        # `http_error_302` entry point. Pre-validate the raw header HERE, before delegating to
        # `super()`, which is the actual first place this process sees server-controlled
        # redirect data.
        # review finding (round 5): stdlib selects the redirect target by HEADER PRESENCE
        # (`if "location" in headers: ... elif "uri" in headers: ...`), not truthiness — a
        # `headers.get("location") or headers.get("uri")` diverges from that for a PRESENT-BUT-
        # EMPTY Location header (stdlib uses the empty location; the truthiness version would
        # have fallen through to a URI header stdlib never actually acts on). Mirror stdlib's
        # own selection exactly so this pre-validation always checks the SAME value `super()`
        # is about to use.
        if "location" in headers:
            newurl = headers["location"]
        elif "uri" in headers:
            newurl = headers["uri"]
        else:
            newurl = None
        # review finding (round 8): stdlib's OWN http_error_302 strips the header value
        # (`newurl.strip()`, right after the selection above) before ever parsing it — mirror
        # that too, not just the presence-based selection, so this pre-validation checks the
        # EXACT value `super()` will actually urlparse (a leading-whitespace `Location` would
        # otherwise parse here as a harmless relative path — scheme detection breaks on the
        # leading space — while stdlib's stripped version could still be the malformed
        # authority this guard exists to catch). Not reachable through a real HTTP response in
        # practice (RFC 7230 header-value parsing already strips leading OWS before this code
        # ever sees it — verified: Python's own `http.client`/`email` header parser does this),
        # but a direct handler invocation (as this file's own tests do) has no such guarantee,
        # and matching stdlib exactly is cheap and removes the discrepancy either way.
        if newurl is not None:
            newurl = newurl.strip()
        if newurl is not None and safe_urlparse(newurl) is None:
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"refused unparseable redirect Location from {req.full_url}: {newurl!r}",
                headers,
                fp,
            )
        return super().http_error_302(req, fp, code, msg, headers)

    # review finding (round 4): the stdlib maps EVERY redirect status (301/302/303/307/308) onto
    # the SAME `http_error_302` method via class-level aliasing (`http_error_301 =
    # http_error_303 = ... = http_error_302`) — verified live. Overriding only `http_error_302`
    # above would NOT cover a 301/303/307/308 response: Python attribute lookup for
    # `self.http_error_301` on an instance of THIS subclass would still resolve to the PARENT
    # class's original (unguarded) function object, since the parent's aliases were bound at
    # the parent's own class-definition time, not re-derived from whatever a subclass later
    # overrides. Re-alias them here, mirroring the stdlib's own pattern, or 3 of the 5 redirect
    # codes would silently bypass every guard in this class.
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 - stdlib override name
        # By the time we get here, `http_error_302` above has already confirmed `newurl` parses
        # cleanly (it's server-controlled data, re-validated defensively rather than trusted
        # across the two methods).
        new = safe_urlparse(newurl)
        if new is None:
            raise urllib.error.HTTPError(
                newurl, code, f"refused unparseable redirect Location from {req.full_url}: {newurl!r}", headers, fp
            )
        # `_same_origin` touches `.port` on BOTH sides (review finding, GitHub PR #109 Codex
        # pass) — `new`'s port was already validated above via `safe_urlparse`, but `old` was
        # not: a plain `urlparse()` never touches `.port`, so this is the FIRST access on
        # `old` this class ever makes (the pre-existing `.netloc` string compare it replaced
        # never needed to). `req.full_url` is this process's own already-in-flight request,
        # not attacker data, but validate it the same defensive way regardless — fail closed
        # (refuse the redirect) rather than let a malformed port raise ValueError uncaught.
        old = safe_urlparse(req.full_url)
        if old is None:
            raise urllib.error.HTTPError(
                newurl, code, f"refused redirect from an unparseable original URL {req.full_url!r}", headers, fp
            )
        if not _same_origin(old, new) or (old.scheme == "https" and new.scheme != "https"):
            raise urllib.error.HTTPError(
                newurl, code, f"refused unsafe redirect from {req.full_url} to {newurl}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAME_HOST_OPENER = urllib.request.build_opener(_SameHostRedirectHandler)


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 60,
    same_host_redirects_only: bool = False,
) -> bytes:
    """Return raw response bytes with the same uncertainty contract as request_json.

    Set same_host_redirects_only for credential-bearing requests to prevent a redirect
    from exfiltrating authentication headers to another origin. No automatic replay.
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", "task-cli")

    try:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    except ValueError as exc:
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
    opener = _SAME_HOST_OPENER.open if same_host_redirects_only else urllib.request.urlopen
    try:
        with opener(req, timeout=timeout) as resp:  # noqa: S310 - trusted provider hosts
            try:
                return resp.read()
            except _AMBIGUOUS_READ_EXCEPTIONS as exc:
                if method.upper() in {"GET", "HEAD"}:
                    raise HttpError(resp.status, f"unusable response for {method} (status {resp.status})") from None
                raise AmbiguousHttpError(
                    f"connection interrupted while reading the response for {method} {url} "
                    f"(status {resp.status} was already returned) — the request may have "
                    "completed server-side even though this process never saw a usable body"
                ) from exc
    except urllib.error.HTTPError as exc:
        _raise_redirect_error(req, exc.code)
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise HttpError(exc.code, f"HTTP {exc.code} for {method} {url}", body) from exc
    except http.client.InvalidURL as exc:
        _raise_redirect_error(req)
        raise HttpError(0, f"invalid URL for {method} {url}: {exc}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        _raise_redirect_error(req)
        _raise_open_error(exc, method)


# MIME lookup shared by every backend's native-upload path (Linear's `fileUpload`, GitHub's
# `user-attachments/assets`) — both require the content type up front, before the bytes are
# sent. Kept here (not per-backend) so the two don't drift, review finding (this fix):
# `linear.py` used to carry its own private copy as `_CONTENT_TYPES`/`_detect_content_type`.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
}


def detect_content_type(file_path: str) -> str:
    """The MIME type for a local file, by extension (falls back to
    ``application/octet-stream``) — both native-upload APIs (Linear's ``fileUpload``, GitHub's
    ``user-attachments/assets``) require it up front.

    Deliberately does NOT also return a size via a separate ``os.path.getsize`` call (review
    finding, TOCTOU — originally found on Linear's private copy of this function, applies
    identically here): the caller reads the file's bytes anyway, and ``len(body)`` from that
    SAME read is the authoritative size.
    """
    ext = os.path.splitext(file_path)[1].lower()
    return CONTENT_TYPES.get(ext, "application/octet-stream")
