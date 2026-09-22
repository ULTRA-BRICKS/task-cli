"""Tasks backend — calls the tasks-app REST API directly (stdlib ``urllib``).

tasks-app (the user's in-house Linear replacement, production at
tasks-ultrabricks.vercel.app) exposes a plain JSON REST API under ``/api/v1`` — snake_case
fields, UUID identifiers, ``Authorization: Bearer <token>`` auth, and OPTIMISTIC CONCURRENCY:
every write to an issue carries the ``version`` integer the caller last READ, and the server
409s a stale one instead of silently clobbering a concurrent edit. See
``docs/TASKS-BACKEND-CHECKPOINT.md`` in this repo and tasks-app's own ``docs/openapi.json`` for
the full API shape this module targets.

## Why every call goes through ``request_bytes``, not ``request_json``

Every request here carries the bearer token, so — same rule as Linear's/GitHub's native-upload
calls — a redirect must never be followed off the requested host (credential exfiltration; see
``backends/http.py``'s ``_SameHostRedirectHandler``). ``request_json`` has no
``same_host_redirects_only`` knob; ``request_bytes`` does, so this module calls ``request_bytes``
for EVERY API call (JSON-encoding the payload itself, decoding the JSON response itself) rather
than mixing the two helpers.

## Optimistic concurrency: read-once, write-once

:meth:`transition` resolves the target ``status_id`` from the cached catalog FIRST — this can
raise (e.g. no review-capable status — see below) before any network call touches the issue
itself — then reads the issue exactly ONCE and PATCHes with the version from that SAME read.
It never re-reads before writing: a second read would only widen the race window between
"read the version" and "write it back", the opposite of what optimistic concurrency buys. The
PATCH body carries ONLY ``version`` + ``status_id`` — never ``description`` — so a transition
can never clobber ticket content, and never silently rewrites Markdown this backend doesn't own.

## No native "in-review" category

tasks-app's ``Status.category`` enum is ``triage | backlog | unstarted | started | completed |
canceled`` — there is no ``review`` category. A team's own "In Review" status (if it has one) is
just a ``started``-category status with a review-suggestive name. Unlike Linear (which silently
folds ``IN_REVIEW`` onto its ``started`` type — see ``backends/linear.py``'s ``_STATE_TO_TYPE``),
this backend does NOT guess: :meth:`transition` to ``State.IN_REVIEW`` scans the team's
``started`` statuses for one whose name mentions "review" and raises :class:`BackendError`
(matching ``"review"``) if none exists, making ZERO write calls — silently landing on an
arbitrary ``started`` status would be a worse failure than a clear refusal.

## Attachments are same-origin, not a separate asset domain

Unlike Linear (``uploads.linear.app``) or GitHub (``uploads.github.com``), tasks-app serves
attachment bytes from its OWN origin (``GET/PUT /api/v1/attachments/{id}/content`` — private
object storage behind the same API, per tasks-app's ``docs/openapi.json``). So the ONE guard
:meth:`fetch_attachment_bytes` needs is "is this url the SAME origin task-cli is already
authenticated against" — there is no separate native-asset-host allowlist to maintain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from urllib.parse import quote, urlencode

from ..model import State, Ticket
from ..render import parse, render
from ..taskref import TaskRefError, tasks_identifier
from . import AmbiguousBackendError, BackendError
from .http import (
    AmbiguousHttpError,
    HttpError,
    detect_content_type,
    request_bytes,
    safe_urlparse,
)

_DEFAULT_PORTS = {"https": 443, "http": 80}

# The Ticket fields render()/parse() derive from the body (§5 template — see render.py's
# SECTIONS/`_SKIP_MARKER`). `update()`'s metadata-only-edit check (`_update_description`)
# compares exactly these fields against what raw_body parses back to. `skips` IS a body field
# (render() writes it into the "## Skipped gates" section — integration review finding: a
# skips-only edit was silently dropped when this tuple omitted it) but `due`/`state`/`labels`/
# `title` are deliberately excluded — those ride their OWN PATCH fields (`due_date`/`status_id`/
# `label_ids`), never `description`, so a due-date-only or status-only edit must not be treated
# as a body change just because `due`/`state`/`labels` differ.
_BODY_FIELDS = (
    "what",
    "why",
    "user_impact",
    "cost_of_inaction",
    "acceptance",
    "screenshots",
    "links",
    "skips",
)

# A team's workflow status carries a `category` (see the module docstring — there is no native
# "review" category), never a free-form State. Preference order per normalized State: the
# FIRST category in the tuple that the team's catalog actually has wins. IN_REVIEW is handled
# separately in `_status_id_for` (name-based, not category-based — see the module docstring).
_CATEGORY_FOR_STATE: dict[State, tuple[str, ...]] = {
    State.TODO: ("unstarted", "backlog", "triage"),
    State.IN_PROGRESS: ("started",),
    State.DONE: ("completed",),
    State.CANCELLED: ("canceled",),
}

# The reverse direction: a status `category` -> the normalized State a ticket carrying that
# status reads as. `started` is refined to IN_REVIEW by name at read time (`_derive_state`),
# mirroring the write-side heuristic in `_status_id_for` exactly, so a ticket's state reads
# the same way it would be written.
_CATEGORY_TO_STATE: dict[str, State] = {
    "triage": State.TODO,
    "backlog": State.TODO,
    "unstarted": State.TODO,
    "started": State.IN_PROGRESS,
    "completed": State.DONE,
    "canceled": State.CANCELLED,
}


def _host_of(url: str) -> str:
    """The host of ``url`` for error text — never the full value, which may carry userinfo or a
    credential-bearing query string."""
    parsed = safe_urlparse(str(url).strip())
    return repr(parsed.hostname) if parsed is not None and parsed.hostname else "unparseable"


def _validate_origin(url: str) -> str:
    """Return ``url`` normalized to a bare ``https://host[:port]`` origin, or raise
    :class:`BackendError`.

    Every request this backend makes carries the bearer token, so the configured origin must
    be unambiguous and carry NOTHING else a caller could smuggle a credential-exfiltration
    vector through: no userinfo (``user:pass@host`` — the token would ride alongside stolen
    basic-auth credentials to whatever ``host`` names), no query/fragment (a
    ``?token=...``-shaped url reads as if the token belongs there, and a query string has no
    business on a bare API origin), no path suffix (this backend interpolates
    ``self.url + "/api/v1/..."`` itself — a pre-existing path would double up or silently
    redirect requests into an unintended sub-path), and ``https`` only (plaintext ``http://``
    would send the bearer token over the wire in the clear).
    """
    parsed = safe_urlparse(str(url).strip())
    if parsed is None:
        raise BackendError(f"tasks: url must be a valid https origin (host {_host_of(url)})")
    if parsed.scheme != "https":
        raise BackendError(f"tasks: url must use https (host {_host_of(url)})")
    if parsed.username or parsed.password:
        raise BackendError(f"tasks: url must not carry userinfo (user:pass@host) (host {_host_of(url)})")
    if parsed.query or parsed.fragment:
        raise BackendError(
            f"tasks: url must be a bare origin with no query/fragment (host {_host_of(url)})"
        )
    if parsed.path not in ("", "/"):
        raise BackendError(f"tasks: url must be a bare origin with no path (host {_host_of(url)})")
    if not parsed.hostname:
        raise BackendError(f"tasks: url must name a host (host {_host_of(url)})")
    port = f":{parsed.port}" if parsed.port and parsed.port != _DEFAULT_PORTS["https"] else ""
    return f"https://{parsed.hostname}{port}"


@dataclass
class TasksBackend:
    """REST adapter for tasks-app. ``team_key`` (e.g. ``GL``) scopes creation/listing.

    ``attachment_mode`` selects how :meth:`attach` handles a LOCAL file ref (already-hosted
    URLs are always registered as-is regardless of mode — see :meth:`attach`'s docstring):
    ``"native"`` (the default) uploads the bytes through tasks-app's own reserve+PUT flow (see
    :meth:`_upload_and_attach`); ``"link"`` refuses a bare local path (nothing to link to) and
    no-ops. Mirrors :class:`~tasklib.backends.linear.LinearBackend`'s identical contract — see
    :data:`tasklib.config.VALID_ATTACHMENT_MODES`.
    """

    url: str
    token: str = field(repr=False)  # never leak the bearer token through repr()/logging
    team_key: str
    attachment_mode: str = "native"
    name: str = "tasks"
    _team_id: str = field(default="", init=False, repr=False, compare=False)
    _statuses: list = field(default_factory=list, init=False, repr=False, compare=False)
    _label_by_name: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.url = _validate_origin(self.url)

    @classmethod
    def from_config(cls, config, *, env: dict | None = None) -> "TasksBackend":
        from ..credentials import tasks_token

        sec = config.section("tasks")
        url = str(sec.get("url", "")).strip()
        if not url:
            raise BackendError("tasks backend requires a url (set tasks.url in task.yaml)")
        team = str(sec.get("team", "")).strip()
        if not team:
            raise BackendError("tasks backend requires a team key (set tasks.team in task.yaml)")
        url = _validate_origin(url)  # before credential harvest: a malformed url fails clean
        creds = tasks_token(url, env)
        return cls(
            url=url,
            token=creds.token,
            team_key=team,
            attachment_mode=str(sec.get("attachment_mode", "native")),
        )

    # ── HTTP plumbing ────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def _call(self, path: str, *, method: str = "GET", payload: dict | None = None):
        url = self.url + path
        headers = self._headers()
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            raw = request_bytes(
                url, method=method, headers=headers, data=data, same_host_redirects_only=True
            )
        except AmbiguousHttpError as exc:
            # caught before HttpError on purpose: AmbiguousHttpError is not an HttpError
            # subclass (see its docstring) — this gives it its own distinctly-typed wrapper
            # instead of falling through to the generic BackendError below.
            raise AmbiguousBackendError(self._scrub(f"tasks: {exc}")) from None
        except HttpError as exc:
            raise BackendError(self._scrub(f"tasks: {exc} {exc.body}".strip())) from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackendError(f"tasks: malformed response for {method} {url}: {exc}") from exc

    def _scrub(self, text: str) -> str:
        """Error text with this backend's token (and any other token-shaped value) removed — a
        server or proxy may reflect request headers into its error body (UB-25 review)."""
        from ..logging import redact

        if self.token:
            text = text.replace(self.token, "<redacted>")
        return str(redact(text))

    # ── ticket-id aliasing ───────────────────────────────────────────────────────
    def _resolve_id(self, ticket_id: str) -> str:
        """Normalize a caller-given code to this team's canonical ``TEAM-N`` key when it looks
        like one (case-insensitive team-prefix aliasing, e.g. ``gl-12`` -> ``GL-12`` — mirrors
        :func:`~tasklib.taskref.linear_identifier`'s contract exactly). A code that does not
        match that shape (a UUID, or an id from a different team) is passed through unchanged —
        this backend's ``/api/v1/issues/{id}`` accepts either a UUID or a human key, so a
        non-matching string is still a legitimate lookup, not necessarily a typo.
        """
        try:
            return tasks_identifier(ticket_id, self.team_key)
        except TaskRefError:
            return str(ticket_id).strip()

    # ── catalog (team id, workflow statuses, labels) ────────────────────────────
    def _ensure_catalog(self) -> None:
        if self._team_id:
            return
        data = self._call("/api/v1/catalog") or {}
        teams = data.get("teams") or []
        match = next(
            (
                t
                for t in teams
                if isinstance(t, dict) and (t.get("key") == self.team_key or t.get("id") == self.team_key)
            ),
            None,
        )
        if match is None:
            raise BackendError(f"tasks: no team with key {self.team_key!r}")
        self._team_id = match["id"]
        self._statuses = [
            s
            for s in (data.get("statuses") or [])
            if isinstance(s, dict) and s.get("team_id") == self._team_id
        ]
        for lbl in data.get("labels") or []:
            if isinstance(lbl, dict) and isinstance(lbl.get("name"), str) and isinstance(lbl.get("id"), str):
                self._label_by_name[lbl["name"].lower()] = lbl["id"]

    def _status_id_for(self, state: State) -> str:
        """Resolve the team's status id for ``state``. Pure catalog lookup — no issue read, no
        write — so a caller (:meth:`transition`) can resolve this FIRST and fail before ever
        touching the issue or making a write, per the module docstring.
        """
        self._ensure_catalog()
        if state == State.IN_REVIEW:
            for st in self._statuses:
                if st.get("category") == "started" and "review" in str(st.get("name", "")).lower():
                    return st["id"]
            raise BackendError(
                f"tasks: team {self.team_key} has no review-capable status (a 'started' "
                "workflow status whose name mentions review) — cannot transition to in-review"
            )
        if state == State.IN_PROGRESS:
            # Prefer a `started` status that does NOT read as review (see _derive_state) so
            # in-progress and in-review never collide on the same status id.
            non_review = [
                st
                for st in self._statuses
                if st.get("category") == "started" and "review" not in str(st.get("name", "")).lower()
            ]
            if non_review:
                return non_review[0]["id"]
            for st in self._statuses:
                if st.get("category") == "started":
                    return st["id"]
            raise BackendError(f"tasks: team {self.team_key} has no in-progress workflow status")
        for category in _CATEGORY_FOR_STATE[state]:
            for st in self._statuses:
                if st.get("category") == category:
                    return st["id"]
        raise BackendError(f"tasks: team {self.team_key} has no {state.value} workflow status")

    def _derive_state(self, row: dict) -> State:
        category = str(row.get("status_category") or "unstarted")
        name = str(row.get("status_name") or "")
        if category == "started" and "review" in name.lower():
            return State.IN_REVIEW
        return _CATEGORY_TO_STATE.get(category, State.TODO)

    def _label_names(self, raw) -> list[str]:
        names: list[str] = []
        for item in raw or []:
            if isinstance(item, dict):
                nm = item.get("name")
                if isinstance(nm, str) and nm:
                    names.append(nm)
            elif isinstance(item, str) and item:
                names.append(item)
        return names

    def _label_ids(self, names: list[str]) -> list[str]:
        """Resolve label names -> ids, CREATING any that don't exist yet (mirrors
        :meth:`~tasklib.backends.linear.LinearBackend._label_ids`'s identical reasoning —
        tasks-app, like Linear, only accepts existing label ids on an issue write, and a
        session label like ``session:<id>`` won't pre-exist).
        """
        self._ensure_catalog()
        ids: list[str] = []
        for nm in names:
            lid = self._label_by_name.get(nm.lower())
            if lid is None:
                lid = self._create_label(nm)
            if lid:
                ids.append(lid)
        return ids

    def _create_label(self, name: str) -> str | None:
        result = self._call("/api/v1/labels", method="POST", payload={"name": name, "color": "#5b6472"})
        if isinstance(result, dict):
            lid = result.get("id")
            if isinstance(lid, str) and lid:
                self._label_by_name[name.lower()] = lid
                return lid
        return None

    # ── row <-> Ticket ───────────────────────────────────────────────────────────
    def _row_to_ticket(self, row: dict) -> Ticket:
        # PRESENCE-check, not truthy-check: `"due_date" in row` is the only way to tell "the
        # server explicitly cleared this" (key present, value null/absent-of-value → must win
        # and clear the ticket's due date) apart from "this response simply doesn't carry the
        # column at all" (key absent — must fall back to whatever parse() derives from the body,
        # the pre-existing backward-compatible behavior). A truthy/`or ""` check on `.get()`
        # alone cannot distinguish these two cases — both collapse to the empty string — and a
        # response shape that omits `due_date` entirely would otherwise silently wipe a real
        # body-parsed due date on every read (found during Phase-3 integration review).
        has_native_due = "due_date" in row
        native_due = str(row.get("due_date") or "")
        base = Ticket(
            title=str(row.get("title", "")),
            id=str(row.get("key") or row.get("id") or ""),
            url=f"{self.url}/issues/{row.get('id', '')}",
            state=self._derive_state(row),
            labels=self._label_names(row.get("labels")),
            updated_at=str(row.get("updated_at") or ""),
            created_at=str(row.get("created_at") or ""),
            due=native_due,
            backend_version=int(row.get("version") or 0),
        )
        ticket = parse(row.get("description") or "", base)
        if not has_native_due:
            # No native column in this response at all — let parse()'s own precedence stand (a
            # nonempty "## Due" body line wins, else `base.due`, which is "" here since we have
            # no native value to offer). raw_body/description is never touched either way.
            return ticket
        # parse() prefers a nonempty "## Due" line in the Markdown over base.due (render.py:
        # `due = sec.get(_DUE_MARKER, "").strip() or base.due`) — correct for a backend with no
        # native due-date column, but tasks-app HAS one, and it must win: a stale "## Due" line
        # left over from before a native due-date change/clear must not resurrect an old date
        # (integration review finding). Overriding back to `native_due` here only corrects the
        # derived `due` field on the in-memory Ticket — `ticket.raw_body`/`description` (the
        # verbatim Markdown) is untouched, so a metadata-only update() still sees the SAME
        # raw_body it would have without this override.
        return replace(ticket, due=native_due)

    def _issue_row(self, ticket_id: str) -> dict:
        code = self._resolve_id(ticket_id)
        row = self._call(f"/api/v1/issues/{quote(code, safe='')}")
        if not isinstance(row, dict) or not row.get("id"):
            raise BackendError(f"tasks: no issue {ticket_id}")
        return row

    # ── protocol ─────────────────────────────────────────────────────────────────
    def create(self, ticket: Ticket) -> Ticket:
        self._ensure_catalog()
        payload: dict = {
            "title": ticket.title,
            "description": render(ticket),
            "team_id": self._team_id,
            "status_id": self._status_id_for(ticket.state),
            "label_ids": self._label_ids(ticket.labels),
        }
        if ticket.due.strip():
            payload["due_date"] = ticket.due.strip()
        row = self._call("/api/v1/issues", method="POST", payload=payload)
        if not isinstance(row, dict) or not row.get("id"):
            raise BackendError(f"tasks: issue create returned a malformed response: {row!r}")
        return self._row_to_ticket(row)

    def get(self, ticket_id: str) -> Ticket:
        return self._row_to_ticket(self._issue_row(ticket_id))

    def update(self, ticket: Ticket) -> Ticket:
        """PATCH ``ticket`` back to tasks-app.

        Two safety rules (UB-23 review, both load-bearing — see :meth:`_update_version` and
        :meth:`_update_description`):

        1. **Version is read-once, never re-read.** The PATCH carries ``ticket.backend_version``
           — the version the CALLER's original :meth:`get` saw — never a value fetched fresh
           here. tasks-app's own PATCH route (``packages/server/modules/tickets/service.mjs``)
           accepts the SAME ``key`` path segment GET does (UUID or team key, interchangeably —
           confirmed in the route dispatch), so this never re-reads the issue just to learn its
           UUID either: a second read would only widen the race window between "read the
           version" and "write it back", the opposite of what optimistic concurrency buys (same
           reasoning as :meth:`transition`'s docstring). A stale version 409s server-side
           (``UPDATE ... WHERE version=?`` — zero rows changed -> conflict) instead of silently
           clobbering a concurrent edit.
        2. **A metadata-only edit never touches ``description``.** ``description`` is omitted
           from the PATCH payload entirely unless the caller's body-derived fields actually
           differ from what :attr:`Ticket.raw_body` parses back to — tasks-app's PATCH only
           writes fields present in the JSON body (``Object.hasOwn(input, field)``), so omitting
           the key is a true no-op on that column, not just "send the same value back".

        Order matters (integration review finding): :meth:`_update_description` runs BEFORE
        :meth:`_status_id_for`/:meth:`_label_ids` — both of those can make their OWN write calls
        (``_label_ids`` CREATES a not-yet-existing label). A refused lossy body edit must make
        ZERO API calls, including no side-effecting label creation, not just no PATCH — so the
        body-safety check that can raise has to run before anything else that talks to the
        network.
        """
        version = self._update_version(ticket)
        description = self._update_description(ticket)
        code = self._resolve_id(ticket.id)
        payload: dict = {
            "version": version,
            "title": ticket.title,
            "status_id": self._status_id_for(ticket.state),
            "label_ids": self._label_ids(ticket.labels),
            "due_date": ticket.due.strip() or None,
        }
        if description is not None:
            payload["description"] = description
        updated = self._call(f"/api/v1/issues/{quote(code, safe='')}", method="PATCH", payload=payload)
        if not isinstance(updated, dict) or not updated.get("id"):
            raise BackendError(f"tasks: issue update returned a malformed response: {updated!r}")
        return self._row_to_ticket(updated)

    def _update_version(self, ticket: Ticket) -> int:
        """Validate and return the optimistic-concurrency token ``update`` must send — always
        ``ticket.backend_version`` from the caller's ORIGINAL read (see :meth:`update`'s
        docstring), never a value this method re-derives. Rejects a missing/invalid version
        before any network call: a 0/absent version means the caller never read a real,
        versioned row (see :class:`Ticket`'s ``backend_version`` docstring — 0 is "unknown", not
        a real server version, which starts at 1), and sending it anyway would either 409
        against every future edit or (worse, if tasks-app ever tolerated a 0/None version) skip
        the concurrency check altogether.
        """
        version = ticket.backend_version
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise BackendError(
                f"tasks: update refuses to write ticket {ticket.id!r} without a valid "
                f"backend_version from the original read (got {version!r}) — read the ticket "
                "fresh (e.g. `task read`) before updating it"
            )
        return version

    def _update_description(self, ticket: Ticket) -> str | None:
        """Whether/what to send as ``description`` — ``None`` omits the key entirely (see
        :meth:`update`'s docstring, rule 2). Compares ``ticket``'s body-derived fields against
        :func:`~tasklib.render.parse` of its own :attr:`Ticket.raw_body` (the verbatim body this
        backend last read) to tell a metadata-only edit (status/labels/due/title — the body
        untouched) from a genuine body edit, without ever assuming the caller re-serialized an
        unchanged body identically.
        """
        original = parse(ticket.raw_body)
        if all(getattr(ticket, f) == getattr(original, f) for f in _BODY_FIELDS):
            return None
        # A body change WAS requested. Only safe to send render(ticket) if raw_body round-trips
        # losslessly through parse()/render() — i.e. it holds nothing outside the managed
        # section template. If it doesn't, raw_body carries custom Markdown (extra headings,
        # imported prose, ...) this backend doesn't understand and render(ticket) would silently
        # drop on the way back out — refuse instead of clobbering it (UB-23 review, option b).
        if render(original).strip() != ticket.raw_body.strip():
            raise BackendError(
                f"tasks: update refuses to overwrite ticket {ticket.id!r}'s body — its current "
                "Markdown contains content this backend's parser doesn't fully understand "
                "(round-tripping it through the managed template would lose it), so the "
                "requested body change cannot be merged losslessly. Edit the body directly in "
                "tasks-app instead."
            )
        return render(ticket)

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        self._ensure_catalog()
        params: dict[str, str] = {"team_id": self._team_id}
        if labels:
            first_id = self._label_by_name.get(labels[0].lower())
            if first_id:
                params["label_id"] = first_id
        # A label/state filter narrows client-side after the fetch (tasks-app filters only one
        # label id server-side, and `state` has no direct server filter param at all — see
        # `category` vs our finer IN_REVIEW split) — scan a full page so a narrow filter doesn't
        # silently under-report (same reasoning as GitHubIssuesBackend.list()'s state handling).
        fetch_limit = 100 if (labels or state is not None) else limit
        tickets = self._paginate_issues(params=params, limit=fetch_limit)
        if labels:
            wanted = {lbl.lower() for lbl in labels}
            tickets = [t for t in tickets if wanted <= {x.lower() for x in t.labels}]
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        self._ensure_catalog()
        params: dict[str, str] = {"team_id": self._team_id, "q": query}
        fetch_limit = 100 if state is not None else limit
        tickets = self._paginate_issues(params=params, limit=fetch_limit)
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def _paginate_issues(self, *, params: dict, limit: int) -> list[Ticket]:
        """Signed keyset pagination via ``cursor`` (tasks-app's own contract — see
        ``docs/openapi.json``'s ``/v1/issues`` ``cursor``/``next_cursor`` pair). Fetches pages
        until ``limit`` tickets are in hand or the backend reports no further page.
        """
        tickets: list[Ticket] = []
        cursor: str | None = None
        page_size = max(1, min(limit, 100))
        while len(tickets) < limit:
            query = dict(params)
            query["limit"] = str(page_size)
            if cursor:
                query["cursor"] = cursor
            result = self._call(f"/api/v1/issues?{urlencode(query)}") or {}
            rows = result.get("data") or []
            for row in rows:
                tickets.append(self._row_to_ticket(row))
                if len(tickets) >= limit:
                    break
            cursor = result.get("next_cursor")
            if not rows or not cursor:
                break
        return tickets

    def comment(self, ticket_id: str, body: str) -> None:
        row = self._issue_row(ticket_id)
        self._call(f"/api/v1/issues/{row['id']}/comments", method="POST", payload={"body": body})

    def attach(self, ticket_id: str, file_path: str) -> str:
        """Attach ``file_path`` (a local path OR an already-hosted URL) to ``ticket_id``.
        Mirrors :meth:`LinearBackend.attach`/:meth:`GitHubIssuesBackend.attach`'s identical
        contract: an already-hosted URL is accepted as a reference as-is (nothing to upload —
        tasks-app's private-object attachment flow has no "register this external URL"
        operation, so an already-hosted link is simply returned unchanged rather than
        rejected); only a bare LOCAL path branches on ``attachment_mode``.
        """
        if "://" in file_path:
            return file_path
        if self.attachment_mode == "link":
            raise BackendError(
                f"tasks: attachment_mode=link cannot attach local path {file_path!r} "
                "(nothing hosted to link to) — pre-host the file and pass a URL, or switch to "
                "attachment_mode: native"
            )
        return self._upload_and_attach(ticket_id, file_path)

    def _upload_and_attach(self, ticket_id: str, file_path: str) -> str:
        """The real native-upload path: reserve an attachment slot (``POST
        /api/v1/issues/{id}/attachments``), PUT the bytes to the reservation's own content
        endpoint (``PUT /api/v1/attachments/{id}/content`` — SAME origin, unlike Linear/GitHub's
        separate signed-URL/asset-domain flows, see the module docstring), and return the
        content endpoint as the durable reference.
        """
        try:
            content_type = detect_content_type(file_path)
            with open(file_path, "rb") as f:
                body = f.read()
        except OSError as exc:
            raise BackendError(f"tasks: cannot read {file_path} for upload: {exc}") from exc

        row = self._issue_row(ticket_id)
        filename = os.path.basename(file_path) or "attachment"
        reservation = self._call(
            f"/api/v1/issues/{row['id']}/attachments",
            method="POST",
            payload={"filename": filename, "mime": content_type, "bytes": len(body)},
        )
        if not isinstance(reservation, dict) or not reservation.get("id"):
            raise BackendError(
                f"tasks: attachment reservation returned a malformed response: {reservation!r}"
            )
        attachment_id = reservation["id"]
        content_url = f"{self.url}/api/v1/attachments/{attachment_id}/content"
        try:
            request_bytes(
                content_url,
                method="PUT",
                headers={**self._headers(), "Content-Type": content_type},
                data=body,
                same_host_redirects_only=True,
            )
        except AmbiguousHttpError as exc:
            raise AmbiguousBackendError(self._scrub(f"tasks: upload PUT for {file_path}: {exc}")) from None
        except HttpError as exc:
            raise BackendError(
                self._scrub(f"tasks: upload PUT for {file_path} failed: {exc} {exc.body}".strip())
            ) from None
        return content_url

    def is_native_attachment_url(self, url: str) -> bool:
        """Whether ``url`` is one of THIS backend's own attachment objects — i.e. same origin
        as the configured ``self.url`` (see the module docstring: tasks-app serves attachments
        from its own origin, not a separate asset domain like Linear/GitHub)."""
        parsed = safe_urlparse(url)
        return parsed is not None and self._is_own_origin(parsed)

    def _is_own_origin(self, parsed) -> bool:
        own = safe_urlparse(self.url)
        if own is None:
            return False
        own_port = own.port or _DEFAULT_PORTS.get(own.scheme)
        other_port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme)
        return (
            parsed.scheme == own.scheme
            and parsed.hostname == own.hostname
            and other_port == own_port
        )

    def fetch_attachment_bytes(self, url: str) -> bytes:
        """Read back an attachment's raw bytes. The bearer token is attached ONLY when ``url``
        is the SAME origin as this backend's configured ``self.url`` — an attachment ``url`` is
        tracker-controlled data (see ``backends/linear.py``'s identical reasoning), and this
        backend has no separate "trusted but not native" case to widen for (tasks-app never
        hands back a foreign asset-domain URL) — a cross-origin url is refused outright, with
        ZERO requests made, rather than fetched unauthenticated.
        """
        parsed = safe_urlparse(url)
        if parsed is None or parsed.scheme not in ("http", "https"):
            raise BackendError(f"tasks: refusing to fetch attachment with an unusable url {url!r}")
        if not self._is_own_origin(parsed):
            raise BackendError(
                f"tasks: refusing to fetch attachment {url!r} — its origin differs from the "
                f"configured tasks origin {self.url!r} (would leak the API token to an "
                "untrusted host)"
            )
        try:
            return request_bytes(
                url, method="GET", headers=self._headers(), same_host_redirects_only=True
            )
        except AmbiguousHttpError as exc:
            raise AmbiguousBackendError(self._scrub(f"tasks: fetch attachment {url}: {exc}")) from None
        except HttpError as exc:
            raise BackendError(
                self._scrub(f"tasks: fetch attachment {url} failed: {exc} {exc.body}".strip())
            ) from None

    def transition(self, ticket_id: str, state: State) -> Ticket:
        # Resolve the target status FIRST (pure catalog lookup — no issue read, no write): an
        # unmapped state (IN_REVIEW with no review-capable status) must fail here, before the
        # issue is ever read or written — see the module docstring and
        # test_no_silent_in_review_fallback.
        status_id = self._status_id_for(state)
        row = self._issue_row(ticket_id)  # the ONE read whose version the write below reuses
        payload = {"version": row.get("version"), "status_id": status_id}
        updated = self._call(f"/api/v1/issues/{row['id']}", method="PATCH", payload=payload)
        if not isinstance(updated, dict) or not updated.get("id"):
            raise BackendError(f"tasks: issue transition returned a malformed response: {updated!r}")
        return self._row_to_ticket(updated)

    def session_tickets(self, session_label: str, *, limit: int = 30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)

    def current_user(self) -> str | None:
        """Best-effort id of the token's owner (issue #59's stale-nudge personal-scope filter).
        ``None`` on any failure — never an authorization boundary, just a filter hint."""
        try:
            result = self._call("/api/v1/me")
        except BackendError:
            return None
        if not isinstance(result, dict):
            return None
        uid = result.get("id")
        return uid if isinstance(uid, str) and uid else None
