"""Forgejo Issues backend — the Forgejo REST API (``<origin>/api/v1``), called directly.

What: a native :class:`~tasklib.backends.TicketBackend` for a self-hosted Forgejo forge (UB-25,
git.hyperide.ai). Standalone on purpose — NOT a ``GitHubIssuesBackend`` subclass: GitHub's
module-level API root (``GITHUB_API_URL``), ``/search/issues`` endpoint, uploads host and
labels-in-PATCH contract are all wrong for Forgejo, and inheriting them silently sent a
per-host credential to an unrelated origin in an earlier candidate.

Accessed via: ``get_backend`` for ``backend: forgejo`` with a ``forgejo: {url, repo,
attachment_mode}`` config section.

Invariants (verified against the live Forgejo 16.0.3 Swagger, not memory):
- Every request goes to ``self.url + "/api/v1"`` — one validated https origin, no path/userinfo/
  query — with ``Authorization: token <t>`` and ``same_host_redirects_only=True``, so a redirect
  can never carry the token to another host or downgrade it to http. Error text is scrubbed of
  the token and never echoes a rejected URL (it may carry credentials).
- Issue numbers are per-repository. ``Ticket.id`` stays ``#N`` (the shared id shape), while the
  coordinate (``forgejo:<origin>/<owner>/<repo>``) and ``Ticket.url`` carry the repository, so two
  repos' ``#1`` never collide. ``owner/repo#N`` and a full issue URL are accepted only for THIS
  repository on THIS origin.
- ``GET /repos/{o}/{r}/issues`` mixes pull requests in; plain issues carry ``"pull_request":
  null``. Requests pass ``type=issues`` and rows with a truthy ``pull_request`` are skipped too.
- ``CreateIssueOption.labels`` takes label IDS; ``EditIssueOption`` has NO labels field. Label
  changes go through ``PUT /repos/{o}/{r}/issues/{index}/labels``.
- Due dates are native (``Issue.due_date``; ``unset_due_date`` clears). When the row carries the
  key, the native value wins over a ``## Due`` body line; the Markdown itself is never rewritten
  for a due-only change.
- :meth:`update` sends only what changed relative to the issue as this backend last read it, and
  refuses before any write when the issue changed on the server since that read. Forgejo has no
  ETag/If-Match/version (``EditIssueOption.updated_at`` is a timestamp override, not a
  concurrency token), so this is a best-effort stale-read check, not an atomic compare-and-set:
  an edit landing between the check and the write is still overwritten (last write wins).
- The body is written only when a body-derived field changed, and only if the stored Markdown
  round-trips losslessly through the managed template; otherwise the edit is refused before
  any write.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote, urlencode

from ..model import State, Ticket
from ..render import parse, render, round_trips_losslessly
from ..taskref import TaskRefError, forgejo_issue_number
from . import AmbiguousBackendError, BackendError
from .github_issues import _STATE_LABEL, _derive_state
from .http import (
    AmbiguousHttpError,
    HttpError,
    detect_content_type,
    request_bytes,
    safe_urlparse,
)

# Body-derived Ticket fields (render.py's template). `due` is NOT one: it is a native Forgejo
# field, so a due-only edit never touches the Markdown.
_BODY_FIELDS = ("what", "why", "user_impact", "cost_of_inaction", "acceptance", "screenshots", "links", "skips")

# One owner or repository path segment. Forgejo names are alnum plus `-_.`; `..`/`.` are
# rejected separately so a configured coordinate can never walk the API path.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_PAGE_SIZE = 50  # Forgejo's default MAX_RESPONSE_ITEMS; the server may cap lower, silently
_MAX_PAGES = 200  # safety cap on one listing (≈10k rows at the default page size); reported
# A date-only due date is sent at 12:00 UTC so the calendar date survives any server time zone
# within ±11h when Forgejo echoes it back in its own zone.
_DUE_TIME = "T12:00:00Z"


def _where(url: str) -> str:
    """A safe description of a URL for error text: its host only, never userinfo/query/path."""
    parsed = safe_urlparse(str(url).strip())
    return repr(parsed.hostname) if parsed is not None and parsed.hostname else "(unparseable url)"


def forgejo_origin(url: str) -> str:
    """``url`` normalized to a bare ``https://host[:port]`` origin, or :class:`BackendError`.

    The token rides every request, so the origin must carry nothing else: https only, no
    userinfo, no query/fragment, no path (the backend appends ``/api/v1`` itself). A rejected
    value is never echoed in full — it may itself hold a credential.
    """
    parsed = safe_urlparse(str(url).strip())
    if parsed is None or parsed.scheme != "https" or not parsed.hostname:
        raise BackendError(f"forgejo: url must be an https origin like https://git.example.com (got host {_where(url)})")
    if parsed.username or parsed.password:
        raise BackendError(f"forgejo: url for host {_where(url)} must not carry userinfo")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise BackendError(
            f"forgejo: url for host {_where(url)} must be a bare origin (no path such as /api/v1, no query)"
        )
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{parsed.hostname}{port}"


def _check_segment(value: str, what: str) -> str:
    text = str(value).strip()
    if not _SEGMENT_RE.fullmatch(text) or text in (".", ".."):
        raise BackendError(f"forgejo: invalid repository {what} {value!r}")
    return text


@dataclass(frozen=True)
class _Snapshot:
    """The issue fields this backend last read, used to write only real changes and to detect
    that the issue changed on the server in between."""

    title: str
    body: str
    state: str
    labels: frozenset
    due: str

    @classmethod
    def of(cls, row: dict) -> "_Snapshot":
        return cls(
            title=str(row.get("title") or ""),
            body=str(row.get("body") or ""),
            state=str(row.get("state") or ""),
            labels=frozenset(_label_names(row.get("labels"))),
            due=_native_due(row.get("due_date")),
        )

    def differs(self, other: "_Snapshot") -> list[str]:
        return [f for f in ("title", "body", "state", "labels", "due") if getattr(self, f) != getattr(other, f)]


@dataclass
class ForgejoBackend:
    """REST adapter for one Forgejo repository (``owner``/``repo`` on origin ``url``).

    ``attachment_mode``: ``"native"`` uploads a local file as an issue asset; ``"link"`` accepts
    only already-hosted URLs (see :data:`tasklib.config.VALID_ATTACHMENT_MODES`).
    """

    url: str
    owner: str
    repo: str
    token: str = field(repr=False)
    default_labels: tuple[str, ...] = ()
    attachment_mode: str = "native"
    name: str = "forgejo"
    _label_cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    _snapshots: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.url = forgejo_origin(self.url)
        self.owner = _check_segment(self.owner, "owner")
        self.repo = _check_segment(self.repo, "name")
        if self.attachment_mode not in ("native", "link"):
            raise BackendError(f"forgejo: attachment_mode must be native or link, got {self.attachment_mode!r}")

    @classmethod
    def from_config(cls, config, *, env: dict | None = None) -> "ForgejoBackend":
        from ..credentials import forgejo_token

        sec = config.section("forgejo")
        url = str(sec.get("url", "") or "").strip()
        if not url:
            raise BackendError("forgejo backend requires a url (set forgejo.url, e.g. https://git.hyperide.ai)")
        origin = forgejo_origin(url)
        owner, repo = resolve_repo(str(sec.get("repo", "auto") or "auto"), config.repo_root, origin)
        creds = forgejo_token(origin, env)
        return cls(
            url=origin,
            owner=owner,
            repo=repo,
            token=creds.token,
            default_labels=tuple(str(x) for x in (sec.get("default_labels") or [])),
            attachment_mode=str(sec.get("attachment_mode", "native")),
        )

    # ── HTTP plumbing ────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}", "Accept": "application/json"}

    def _repo_path(self, suffix: str = "") -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}{suffix}"

    def _call(self, path: str, *, method: str = "GET", payload=None, query: dict | None = None):
        url = f"{self.url}/api/v1{path}"
        if query:
            url += "?" + urlencode(query)
        headers = self._headers()
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        raw = self._send(url, method=method, headers=headers, data=data)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(f"forgejo: malformed response for {method} {path}: {exc}") from exc

    def _send(self, url: str, *, method: str, headers: dict, data: bytes | None) -> bytes:
        try:
            return request_bytes(
                url, method=method, headers=headers, data=data, same_host_redirects_only=True
            )
        except AmbiguousHttpError as exc:
            raise AmbiguousBackendError(self._scrub(f"forgejo: {exc}")) from None
        except HttpError as exc:
            raise BackendError(self._scrub(f"forgejo: {exc} {exc.body}".strip())) from None

    def _scrub(self, text: str) -> str:
        """Error text with this backend's token (and any other token-shaped value) removed — a
        misbehaving server may reflect request headers into its error body."""
        from ..logging import redact

        if self.token:
            text = text.replace(self.token, "<redacted>")
        return str(redact(text))

    # ── ids + rows ───────────────────────────────────────────────────────────────
    def _number(self, ticket_id: str) -> str:
        text = str(ticket_id).strip()
        if "://" in text:
            return self._number_from_url(text)
        try:
            return forgejo_issue_number(text, self.owner, self.repo)
        except TaskRefError as exc:
            raise BackendError(str(exc)) from exc

    def _number_from_url(self, text: str) -> str:
        """A full issue URL is accepted only for this origin AND this repository."""
        parsed = safe_urlparse(text)
        parts = parsed.path.strip("/").split("/") if parsed is not None else []
        if (
            parsed is None
            or not self.is_native_attachment_url(text)
            or len(parts) != 4
            or parts[2] != "issues"
            or f"{parts[0]}/{parts[1]}".lower() != f"{self.owner}/{self.repo}".lower()
            or not parts[3].isdigit()
        ):
            raise BackendError(
                f"forgejo: {_where(text)} URL is not an issue of {self.owner}/{self.repo} on {self.url}"
            )
        return parts[3]

    def _issue_row(self, ticket_id: str) -> dict:
        number = self._number(ticket_id)
        row = self._call(self._repo_path(f"/issues/{number}"))
        if not isinstance(row, dict) or "number" not in row:
            raise BackendError(f"forgejo: malformed issue response for {ticket_id!r}")
        if row.get("pull_request"):
            raise BackendError(
                f"forgejo: {self.owner}/{self.repo}#{number} is a pull request, not an issue"
            )
        return row

    def _row_to_ticket(self, row: dict) -> Ticket:
        labels = _label_names(row.get("labels"))
        self._snapshots[str(row.get("number"))] = _Snapshot.of(row)
        base = Ticket(
            title=str(row.get("title") or ""),
            labels=[lbl for lbl in labels if not lbl.startswith("status:")],
            state=_derive_state(row, labels),
            id=f"#{row.get('number')}",
            url=str(row.get("html_url") or ""),
            updated_at=str(row.get("updated_at") or ""),
            created_at=str(row.get("created_at") or ""),
            closed_at=str(row.get("closed_at") or ""),
            reporter=str((row.get("user") or {}).get("login") or ""),
            provider_closed=row.get("state") == "closed",
        )
        ticket = parse(str(row.get("body") or ""), base)
        if "due_date" in row:
            # The native column wins over a (possibly stale) "## Due" body line; the Markdown
            # itself (raw_body) is left exactly as read.
            ticket = replace(ticket, due=_native_due(row.get("due_date")))
        return ticket

    # ── labels ───────────────────────────────────────────────────────────────────
    def _label_ids(self, names) -> list[int]:
        """Repository label ids for ``names``, creating any that don't exist yet."""
        wanted = list(dict.fromkeys(n for n in names if n))
        if any(n not in self._label_cache for n in wanted):
            self._load_labels()
        for name in wanted:
            if name not in self._label_cache:
                self._label_cache[name] = self._create_label(name)
        return [self._label_cache[n] for n in wanted]

    def _load_labels(self) -> None:
        for page in range(1, _MAX_PAGES + 1):
            rows = self._call(self._repo_path("/labels"), query={"page": page, "limit": _PAGE_SIZE})
            if not isinstance(rows, list) or not rows:
                return
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), int) and isinstance(row.get("name"), str):
                    self._label_cache[row["name"]] = row["id"]

    def _create_label(self, name: str) -> int:
        row = self._call(
            self._repo_path("/labels"),
            method="POST",
            payload={"name": name, "color": "#4a90e2", "description": "managed by task-cli"},
        )
        label_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(label_id, int):
            raise BackendError(f"forgejo: creating label {name!r} returned no numeric id")
        return label_id

    def _put_labels(self, number: str, names: list[str]) -> list[str]:
        rows = self._call(
            self._repo_path(f"/issues/{number}/labels"), method="PUT", payload={"labels": self._label_ids(names)}
        )
        return _label_names(rows) if isinstance(rows, list) else names

    # ── protocol: writes ─────────────────────────────────────────────────────────
    def create(self, ticket: Ticket) -> Ticket:
        labels = [*self.default_labels, *ticket.labels, _STATE_LABEL[ticket.state]]
        payload: dict = {
            "title": ticket.title,
            "body": render(ticket),
            "labels": self._label_ids(labels),
        }
        if ticket.due.strip():
            payload["due_date"] = ticket.due.strip() + _DUE_TIME
        if _native_state(ticket.state) == "closed":
            payload["closed"] = True
        row = self._call(self._repo_path("/issues"), method="POST", payload=payload)
        if not isinstance(row, dict) or "number" not in row:
            raise BackendError(f"forgejo: issue create returned a malformed response: {row!r}")
        return self._row_to_ticket(row)

    def update(self, ticket: Ticket) -> Ticket:
        """Write back only what changed since this backend read the issue.

        Order: (1) re-read the issue and refuse if it changed since the snapshot; (2) decide the
        body (may refuse a lossy rewrite); (3) resolve/create label ids; only then (4) PATCH the
        changed fields and (5) PUT labels if the label set changed. Any refusal happens before
        the first write. Not atomic — see the module docstring.
        """
        number = self._number(ticket.id)
        base = self._fresh_base(number, ticket)
        patch = self._patch_for(ticket, base)
        wanted = frozenset([*ticket.labels, _STATE_LABEL[ticket.state]])
        labels_changed = wanted != base.labels
        if labels_changed:
            self._label_ids(sorted(wanted))
        row = None
        if patch:
            row = self._call(self._repo_path(f"/issues/{number}"), method="PATCH", payload=patch)
            if not isinstance(row, dict) or "number" not in row:
                raise BackendError(f"forgejo: issue update returned a malformed response: {row!r}")
        if row is None:
            row = self._call(self._repo_path(f"/issues/{number}"))
        if labels_changed:
            row = {**row, "labels": [{"name": n} for n in self._put_labels(number, sorted(wanted))]}
        return self._row_to_ticket(row)

    def _fresh_base(self, number: str, ticket: Ticket) -> _Snapshot:
        """The issue as last read by this backend, after confirming the server still has it
        that way. Refuses (no writes made) when it changed in between."""
        snapshot = self._snapshots.get(number)
        row = self._issue_row(number)
        current = _Snapshot.of(row)
        if snapshot is not None:
            changed = snapshot.differs(current)
        elif ticket.updated_at:
            changed = [] if str(row.get("updated_at") or "") == ticket.updated_at else ["updated_at"]
        else:
            changed = ["(no read snapshot)"]
        if changed:
            raise BackendError(
                f"forgejo: refusing to update {self.owner}/{self.repo}#{number}: it changed on the "
                f"server since it was read ({', '.join(changed)}). Read it again (`task read`) and "
                "reapply the change."
            )
        self._snapshots[number] = current
        return current

    def _patch_for(self, ticket: Ticket, base: _Snapshot) -> dict:
        patch: dict = {}
        if ticket.title != base.title:
            patch["title"] = ticket.title
        state = _native_state(ticket.state)
        # Only patch native open/closed state when the state LABEL is actually changing —
        # a stale `status:cancelled` on an open issue must not silently re-close it on a
        # label-only edit (task-cli#238).  When the label already matches (the issue was
        # fetched with that label), the native state was already open and the label is just
        # stale from an out-of-band reopen; let the native state stay as-is.
        if state != base.state and _STATE_LABEL.get(ticket.state) not in base.labels:
            patch["state"] = state
        due = ticket.due.strip()
        if due != base.due:
            if due:
                patch["due_date"] = due + _DUE_TIME
            else:
                patch["unset_due_date"] = True
        body = self._new_body(ticket, base.body)
        if body is not None:
            patch["body"] = body
        return patch

    def _new_body(self, ticket: Ticket, stored: str) -> str | None:
        """``None`` = leave the body untouched; otherwise the new rendered body. Refuses a body
        edit whose stored Markdown holds content outside the managed template."""
        original = parse(stored)
        if all(getattr(ticket, f) == getattr(original, f) for f in _BODY_FIELDS):
            return None
        # Tolerates only the heading spacing an editor UI adds (see round_trips_losslessly).
        if not round_trips_losslessly(stored):
            raise BackendError(
                f"forgejo: update refuses to overwrite {self.owner}/{self.repo}{ticket.id}'s body — "
                "its Markdown holds content outside task-cli's managed template, so the requested "
                "body change cannot be merged losslessly. Edit the body in Forgejo directly, or "
                "use `task comment` to add information."
            )
        return render(ticket)

    def transition(self, ticket_id: str, state: State) -> Ticket:
        """Swap the managed ``status:*`` label and open/close the issue from ONE fresh read.
        Never sends the body, title or due date."""
        row = self._issue_row(ticket_id)
        number = str(row["number"])
        current = _label_names(row.get("labels"))
        wanted = [n for n in current if not n.startswith("status:")] + [_STATE_LABEL[state]]
        if set(wanted) != set(current):
            row["labels"] = [{"name": n} for n in self._put_labels(number, wanted)]
        native = _native_state(state)
        if row.get("state") != native:
            patched = self._call(self._repo_path(f"/issues/{number}"), method="PATCH", payload={"state": native})
            if not isinstance(patched, dict) or "number" not in patched:
                raise BackendError(
                    f"forgejo: state change of {self.owner}/{self.repo}#{number} returned a malformed "
                    "response — the change may have been applied; `task read` it before retrying"
                )
            row = {**patched, "labels": row["labels"]}
        return self._row_to_ticket(row)

    def comment(self, ticket_id: str, body: str) -> None:
        number = self._number(ticket_id)
        self._call(self._repo_path(f"/issues/{number}/comments"), method="POST", payload={"body": body})

    # ── protocol: reads ──────────────────────────────────────────────────────────
    def get(self, ticket_id: str) -> Ticket:
        return self._row_to_ticket(self._issue_row(ticket_id))

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        query: dict = {"state": "all", "type": "issues"}
        if labels:
            query["labels"] = ",".join(labels)  # server-side "any of"; narrowed to "all of" below
        if state is not None:
            query["sort"] = "recentupdate"
        wanted = set(labels or [])

        def keep(t: Ticket) -> bool:
            return wanted <= set(t.labels) | {_STATE_LABEL[t.state]} and (state is None or t.state == state)

        return self._scan(query, keep, limit)

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        params = {"state": "all", "type": "issues", "q": query}
        if state is not None:
            params["state"] = "closed" if state in (State.DONE, State.CANCELLED) else "open"
        return self._scan(params, lambda t: state is None or t.state == state, limit)

    def _scan(self, query: dict, keep, limit: int) -> list[Ticket]:
        """Page through the issue list until ``limit`` matches or an EMPTY page (the server caps
        ``limit`` silently, so a short page is not proof of the end). Hitting ``_MAX_PAGES`` is
        reported on stderr as a truncated result, never passed off as complete."""
        matched: list[Ticket] = []
        seen: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            rows = self._call(self._repo_path("/issues"), query={**query, "page": page, "limit": _PAGE_SIZE})
            if not isinstance(rows, list) or not rows:
                return matched[:limit]
            for row in rows:
                if not isinstance(row, dict) or row.get("pull_request"):
                    continue
                number = str(row.get("number"))
                if number in seen:
                    # live ordering can serve a row twice across pages; dedup BEFORE parsing so
                    # the stored snapshot always matches the Ticket actually returned
                    continue
                seen.add(number)
                ticket = self._row_to_ticket(row)
                if keep(ticket):
                    matched.append(ticket)
            if len(matched) >= limit:
                return matched[:limit]
        sys.stderr.write(
            f"warning: forgejo: stopped after {_MAX_PAGES} pages ({len(seen)} issues) of "
            f"{self.owner}/{self.repo}; the result is truncated ({len(matched)} of {limit} wanted).\n"
        )
        return matched[:limit]

    def session_tickets(self, session_label: str, *, limit: int = 30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)

    def current_user(self) -> str | None:
        try:
            row = self._call("/user")
        except BackendError:
            return None
        login = row.get("login") if isinstance(row, dict) else None
        return login if isinstance(login, str) and login else None

    # ── attachments ──────────────────────────────────────────────────────────────
    def attach(self, ticket_id: str, file_path: str) -> str:
        """Attach a local file (``native``: uploaded as an issue asset and read back) or register
        an already-hosted URL, then post a comment embedding it. Returns the durable URL."""
        if "://" in file_path:
            self.comment(ticket_id, f"screenshot: {_markdown_image(os.path.basename(file_path), file_path)}")
            return file_path
        if self.attachment_mode == "link":
            raise BackendError(
                f"forgejo: attachment_mode=link cannot attach local path {file_path!r} "
                "(nothing hosted to link to) — pre-host the file and pass a URL, or switch to "
                "attachment_mode: native"
            )
        return self._upload_and_attach(ticket_id, file_path)

    def _upload_and_attach(self, ticket_id: str, file_path: str) -> str:
        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError as exc:
            raise BackendError(f"forgejo: cannot read {file_path} for upload: {exc}") from exc
        number = self._number(ticket_id)
        filename = os.path.basename(file_path) or "attachment"
        asset_url = self._upload(number, filename, detect_content_type(file_path), content)
        self._verify_upload(asset_url, content)
        try:
            self.comment(ticket_id, f"screenshot: {_markdown_image(filename, asset_url)}")
        except (BackendError, AmbiguousBackendError) as exc:
            # The asset is already attached to the issue: report the partial success and do NOT
            # let a retry re-upload it (that would create a duplicate attachment).
            sys.stderr.write(
                f"warning: forgejo: uploaded {filename} to {self.owner}/{self.repo}#{number} as "
                f"{asset_url}, but the comment linking it failed ({exc}); not re-uploading.\n"
            )
        return asset_url

    def _upload(self, number: str, filename: str, content_type: str, content: bytes) -> str:
        body, multipart_type = _multipart("attachment", filename, content_type, content)
        url = f"{self.url}/api/v1{self._repo_path(f'/issues/{number}/assets')}?{urlencode({'name': filename})}"
        raw = self._send(url, method="POST", headers={**self._headers(), "Content-Type": multipart_type}, data=body)
        asset_url = _asset_url(raw)
        if not self.is_native_attachment_url(asset_url):
            raise BackendError(f"forgejo: upload returned an attachment url outside {self.url} (host {_where(asset_url)})")
        return asset_url

    def _verify_upload(self, asset_url: str, content: bytes) -> None:
        """Read the stored bytes back (privately, same origin) before calling the upload durable."""
        stored = self.fetch_attachment_bytes(asset_url)
        if stored != content:
            raise BackendError(
                f"forgejo: attachment {asset_url} was stored but reads back {len(stored)} bytes, "
                f"not the {len(content)} uploaded — not reporting it as proof"
            )

    def is_native_attachment_url(self, url: str) -> bool:
        parsed = safe_urlparse(url)
        if parsed is None or parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        return f"https://{parsed.hostname}{port}" == self.url

    def fetch_attachment_bytes(self, url: str) -> bytes:
        """Read an attachment back. The token is sent only to this backend's own origin; any
        other origin (or a URL carrying userinfo) is refused with zero requests."""
        if not self.is_native_attachment_url(url):
            raise BackendError(
                f"forgejo: refusing to fetch an attachment from host {_where(url)} — it is not the "
                f"configured origin {self.url} (would leak the token)"
            )
        return self._send(url, method="GET", headers=self._headers(), data=None)


# ── module helpers ──────────────────────────────────────────────────────────────────────


def _label_names(rows) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [str(r["name"]) for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]


def _native_state(state: State) -> str:
    return "closed" if state in (State.DONE, State.CANCELLED) else "open"


def _native_due(value) -> str:
    """``YYYY-MM-DD`` of a Forgejo ``due_date`` date-time (``""`` when unset)."""
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def _markdown_image(name: str, url: str) -> str:
    """``![alt](url)`` with the alt text stripped of Markdown-structural characters and the URL
    percent-encoding the characters that would end or break the link target."""
    alt = re.sub(r"[\[\]\(\)`*_<>!\\\r\n]+", " ", name).strip() or "attachment"
    target = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
    return f"![{alt}]({target})"


def _multipart(field_name: str, filename: str, content_type: str, content: bytes) -> tuple[bytes, str]:
    """A single-file ``multipart/form-data`` body (stdlib has no encoder for requests)."""
    boundary = f"task-cli-{uuid.uuid4().hex}"
    safe_name = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def _asset_url(raw: bytes) -> str:
    try:
        row = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackendError(f"forgejo: upload returned a malformed response: {exc}") from exc
    url = row.get("browser_download_url") if isinstance(row, dict) else None
    if not isinstance(url, str) or not url:
        raise BackendError("forgejo: upload response carried no browser_download_url")
    return url


# ── repository resolution ───────────────────────────────────────────────────────────────


def autodetect_forgejo_origin(repo_root, env: dict[str, str] | None = None) -> str | None:
    """Return the repo's Forgejo HTTPS origin when the git origin host is already trusted by
    the user's fj credentials; otherwise return None.

    This is credential-backed rather than treating every non-github.com remote as Forgejo:
    GitLab, Bitbucket and arbitrary git servers must not be reclassified. No network call is
    made; forgejo_login_known only checks the origin-bound env pair / fj key store (an expired
    fj OAuth login still counts — refreshing it is forgejo_token's job, not auto-detection's).
    """
    from ..credentials import forgejo_login_known

    remote = _git_origin_url_from_config(repo_root)
    if not remote:
        return None
    cleaned = remote.strip().removesuffix("/").removesuffix(".git")
    host = ""
    api_port = ""
    if "://" in cleaned:
        parsed = safe_urlparse(cleaned)
        if parsed is None or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        # Preserve an HTTPS remote's non-default Forgejo web/API port. An ssh:// port is
        # the SSH transport port and a plain http:// port is not an HTTPS port, so
        # neither is reused.
        if parsed.scheme.lower() == "https" and parsed.port and parsed.port != 443:
            api_port = f":{parsed.port}"
    else:
        host, _path = _split_remote(cleaned)
    if not host or host == "github.com":
        return None
    origin = f"https://{host}{api_port}"
    return origin if forgejo_login_known(origin, env) else None


def _git_origin_url_from_config(repo_root) -> str | None:
    """Read the origin URL without spawning git.

    Auto-detection runs during generic CLI setup (including process-only `task classify`),
    so it must be observational: no extra subprocesses and no dependency on git being on PATH.
    A linked worktree's .git file points at its per-worktree gitdir; `commondir` then points
    back to the shared git directory that owns config.
    """
    root = Path(repo_root)
    dotgit = root / ".git"
    git_dir: Path
    if dotgit.is_dir():
        git_dir = dotgit
    elif dotgit.is_file():
        try:
            marker = dotgit.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        prefix = "gitdir:"
        if not marker.lower().startswith(prefix):
            return None
        raw = marker[len(prefix):].strip()
        git_dir = Path(raw)
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    else:
        return None

    common_dir = git_dir
    commondir = git_dir / "commondir"
    if commondir.is_file():
        try:
            raw_common = commondir.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if raw_common:
            candidate = Path(raw_common)
            common_dir = candidate if candidate.is_absolute() else (git_dir / candidate).resolve()

    config_path = common_dir / "config"
    try:
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            parser = configparser.RawConfigParser(strict=False)
            parser.read_file(fh)
        value = parser.get('remote "origin"', "url", fallback="").strip()
    except (OSError, configparser.Error):
        return None
    return value or None


def resolve_repo(repo_spec: str, repo_root, origin: str) -> tuple[str, str]:
    """``owner, repo`` from an explicit ``owner/name`` or, for ``auto``, from the git
    ``origin`` remote — accepted only when that remote lives on ``origin``'s host."""
    spec = repo_spec.strip()
    if spec and spec != "auto":
        parts = spec.split("/")
        if len(parts) != 2:
            raise BackendError(f"forgejo.repo must be 'owner/name' or 'auto', got {repo_spec!r}")
        return _check_segment(parts[0], "owner"), _check_segment(parts[1], "name")
    return parse_forgejo_remote(_git_origin_url(repo_root), origin)


def _git_origin_url(repo_root) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError("cannot resolve forgejo repo: git not available") from exc
    if out.returncode != 0:
        raise BackendError("cannot resolve forgejo repo: no 'origin' remote (set forgejo.repo: owner/name)")
    return out.stdout.strip()


def parse_forgejo_remote(remote: str, origin: str) -> tuple[str, str]:
    """Parse ``owner/repo`` from an https or ssh remote, refusing any host but ``origin``'s.
    The error text names hosts only (a remote URL can hold a credential)."""
    own = safe_urlparse(origin)
    host = (own.hostname or "").lower() if own is not None else ""
    remote_host, path = _split_remote(remote.strip().removesuffix("/").removesuffix(".git"))
    if not host or remote_host != host:
        raise BackendError(
            f"cannot resolve forgejo repo: origin remote host {remote_host or '?'!r} is not the "
            f"configured forgejo host {host!r} (set forgejo.repo: owner/name, or fix forgejo.url)"
        )
    parts = path.strip("/").split("/")
    if len(parts) != 2:
        raise BackendError(f"cannot parse owner/repo from the origin remote on {host}")
    return _check_segment(parts[0], "owner"), _check_segment(parts[1], "name")


def _split_remote(cleaned: str) -> tuple[str, str]:
    """``(host, path)`` of an ``https://``/``ssh://`` URL or scp-like ``user@host:path``."""
    if "://" in cleaned:
        parsed = safe_urlparse(cleaned)
        if parsed is None:
            return "", ""
        return (parsed.hostname or "").lower(), parsed.path
    if ":" in cleaned:
        lhs, path = cleaned.split(":", 1)
        return lhs.rsplit("@", 1)[-1].lower(), path
    return "", ""
