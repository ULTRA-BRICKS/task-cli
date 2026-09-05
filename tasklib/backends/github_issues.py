"""GitHub Issues backend — calls the GitHub REST API directly (stdlib ``urllib``).

The default backend (§1). State is mapped onto GitHub's open/closed + a small set of managed
``status:<state>`` labels, since GitHub issues have only two native states. The body is the
§5 section template (``render.py``), so a GitHub issue and its PR speak one shape.

``repo`` defaults to ``auto`` = the ``origin`` owner/name resolved from git. Credentials come
from :mod:`tasklib.credentials` (never re-prompted, never logged).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from ..model import State, Ticket
from ..render import parse, render
from . import AmbiguousBackendError, BackendError
from .http import AmbiguousHttpError, HttpError, request_json

# Default to public GitHub; honor GITHUB_API_URL the same way gh/octokit do, so a GitHub
# Enterprise host (https://ghe.example.com/api/v3) — or a hermetic mock server in a test
# harness — is reachable without code changes. Read per-call (not a frozen module constant)
# so an env set after import still takes effect.
_DEFAULT_API_ROOT = "https://api.github.com"


def _api_root() -> str:
    """Resolve the GitHub API base. Honors ``GITHUB_API_URL`` (GHE / a test mock).

    Security invariant: the bearer token is attached to every request, so a non-loopback host
    MUST be https — otherwise an ambient ``GITHUB_API_URL`` could exfiltrate the token over
    cleartext or to a foreign server. A loopback host (a local mock/test harness) may be http.

    review finding (PR #90 round 5): a bracket-malformed override (``GITHUB_API_URL=https://[bad``)
    makes ``urlparse()`` itself raise ``ValueError`` — this is the exact ambient-override scenario
    the ``http.request_json``/``request_bytes`` ``InvalidURL`` hardening was motivated by, but
    those fixes only cover the request functions; this module's OWN direct ``urlparse()`` call was
    still unguarded and would crash raw instead of a clean :class:`BackendError`.
    """
    root = os.environ.get("GITHUB_API_URL", _DEFAULT_API_ROOT).rstrip("/")
    if root == _DEFAULT_API_ROOT:
        return root
    from urllib.parse import urlparse

    try:
        parsed = urlparse(root)
    except ValueError as exc:
        raise BackendError(f"GITHUB_API_URL is not a valid url ({root!r}): {exc}") from exc
    host = (parsed.hostname or "").lower()
    is_loopback = host in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not is_loopback:
        raise BackendError(
            f"GITHUB_API_URL must be https for a non-loopback host (got {root!r}); refusing to "
            "send the auth token over cleartext / to an untrusted scheme."
        )
    return root

# GitHub issues are open/closed; we carry the finer lifecycle as managed labels.
_STATE_LABEL = {
    State.TODO: "status:todo",
    State.IN_PROGRESS: "status:in-progress",
    State.IN_REVIEW: "status:in-review",
    State.DONE: "status:done",
    State.CANCELLED: "status:cancelled",
}
_LABEL_STATE = {v: k for k, v in _STATE_LABEL.items()}


@dataclass
class GitHubIssuesBackend:
    """REST adapter. ``owner``/``repo`` target the issues; ``token`` authenticates."""

    owner: str
    repo: str
    token: str
    default_labels: tuple[str, ...] = ()
    name: str = "github-issues"

    # ── construction ──────────────────────────────────────────────────────────────
    @classmethod
    def from_config(cls, config, *, env: dict | None = None) -> "GitHubIssuesBackend":
        from ..credentials import github_token

        gh = config.section("github")
        repo_spec = str(gh.get("repo", "auto"))
        owner, repo = _resolve_repo(repo_spec, config.repo_root)
        creds = github_token(env)
        labels = tuple(str(x) for x in (gh.get("default_labels") or []))
        return cls(owner=owner, repo=repo, token=creds.token, default_labels=labels)

    # ── helpers ───────────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _issues_url(self, suffix: str = "") -> str:
        return f"{_api_root()}/repos/{self.owner}/{self.repo}/issues{suffix}"

    def _call(self, url: str, *, method: str = "GET", payload=None):
        try:
            return request_json(url, method=method, headers=self._headers(), payload=payload)
        except AmbiguousHttpError as exc:
            # caught BEFORE HttpError on purpose: AmbiguousHttpError is not an HttpError
            # subclass (see its docstring), so this branch only exists to give it its own,
            # distinctly-typed wrapper rather than falling through to the generic one below.
            raise AmbiguousBackendError(f"github: {exc}") from exc
        except HttpError as exc:
            raise BackendError(f"github: {exc} {exc.body}".strip()) from exc

    def _all_labels(self, ticket: Ticket) -> list[str]:
        labels = list(dict.fromkeys([*self.default_labels, *ticket.labels]))
        labels.append(_STATE_LABEL[ticket.state])
        return list(dict.fromkeys(labels))

    def _row_to_ticket(self, row: dict) -> Ticket:
        labels = [lbl["name"] for lbl in row.get("labels", []) if isinstance(lbl, dict)]
        state = _derive_state(row, labels)
        reporter = (row.get("user") or {}).get("login") or ""
        base = Ticket(
            title=row.get("title", ""),
            labels=[lbl for lbl in labels if not lbl.startswith("status:")],
            state=state,
            id=f"#{row.get('number')}",
            url=row.get("html_url", ""),
            updated_at=row.get("updated_at", "") or "",
            reporter=reporter,
            # the issue's own open/closed truth, independent of the (possibly stale)
            # status:<state> label `_derive_state` prioritizes — see Ticket.provider_closed.
            provider_closed=row.get("state") == "closed",
        )
        body = row.get("body") or ""
        return parse(body, base)

    # ── protocol ──────────────────────────────────────────────────────────────────
    def create(self, ticket: Ticket) -> Ticket:
        payload = {"title": ticket.title, "body": render(ticket), "labels": self._all_labels(ticket)}
        row = self._call(self._issues_url(), method="POST", payload=payload)
        return self._row_to_ticket(row)

    def get(self, ticket_id: str) -> Ticket:
        num = _issue_number(ticket_id)
        row = self._call(self._issues_url(f"/{num}"))
        return self._row_to_ticket(row)

    def update(self, ticket: Ticket) -> Ticket:
        num = _issue_number(ticket.id)
        payload = {
            "title": ticket.title,
            "body": render(ticket),
            "labels": self._all_labels(ticket),
            "state": "closed" if ticket.state in (State.DONE, State.CANCELLED) else "open",
        }
        row = self._call(self._issues_url(f"/{num}"), method="PATCH", payload=payload)
        return self._row_to_ticket(row)

    # The scan cap for a state-filtered list() — the documented "100 real issues" per-run
    # budget (see README's Audit section), counted in ACTUAL issues (PRs excluded), not raw
    # rows. _STATE_SCAN_MAX_PAGES bounds the worst case where a repo's /issues page is mostly
    # pull requests (see _list_state_filtered's docstring) — 10 pages = at most 1000 raw rows
    # fetched before giving up, never an unbounded scan.
    _STATE_SCAN_TARGET = 100
    _STATE_SCAN_MAX_PAGES = 10

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        # HYP-1347 P1 review finding: `state` is applied client-side AFTER the fetch — GitHub
        # issues are only natively open/closed, our finer states live in a managed label — so
        # capping the fetch to `min(limit, 100)` BEFORE that filter runs can silently scan zero
        # matching issues when `limit` or more non-matching ones sort ahead of them (e.g. `task
        # audit`'s default `--limit 50` scanning the 50 most recent issues of ANY state instead
        # of the 50 most recent Done ones). Whenever a state filter narrows the result, always
        # scan the FULL 100-issue budget before filtering, then truncate to `limit` — see
        # `_list_state_filtered`. Deliberately NOT narrowed further with `state=closed`/
        # `labels=status:done`: `_derive_state` is label-first, so a native `Closes #123`
        # auto-close that leaves the managed label untouched would still need the full scan to
        # be found (a documented, separate blind spot — see README). An unfiltered list (the
        # common case, e.g. interactive `task list`) keeps fetching only what it needs, in the
        # SAME order as before this fix (no `sort=` override) — only the state-filtered path's
        # ordering/pagination changed.
        if state is not None:
            tickets = self._list_state_filtered(labels, state, limit)
        else:
            params = [f"per_page={min(limit, 100)}", "state=all"]
            if labels:
                params.append("labels=" + ",".join(labels))
            rows = self._call(self._issues_url("?" + "&".join(params))) or []
            tickets = [self._row_to_ticket(r) for r in rows if "pull_request" not in r]
        return tickets[:limit]

    def _list_state_filtered(self, labels, state: State, limit: int) -> list[Ticket]:
        """Paginate until `_STATE_SCAN_TARGET` real issues (PRs excluded) have been scanned
        for `state`, `limit` matches are already in hand (review finding: the caller truncates
        to `limit` anyway, so further pages could only fetch rows it would discard), or the
        search space is exhausted.

        review finding (round 2, Codex): GitHub's `/issues` endpoint returns pull requests
        mixed into the same page as issues (`"pull_request" not in r` is the only way to tell
        them apart, applied AFTER the fetch) — a single 100-row page on a repo where recent
        activity is PR-heavy could be almost entirely PRs, silently starving the REAL 100-issue
        scan budget the state filter is supposed to get. Paginating with `sort=updated&
        direction=desc` (the "most recently touched first" ordering the audit's whole
        guarantee depends on — GitHub's own default is `sort=created`, unrelated to what
        changed most recently) keeps accumulating non-PR rows across pages until 100 real
        issues have been seen or `_STATE_SCAN_MAX_PAGES` is exhausted.
        """
        tickets: list[Ticket] = []
        seen: set[str] = set()
        page = 1
        while (
            len(tickets) < self._STATE_SCAN_TARGET
            and page <= self._STATE_SCAN_MAX_PAGES
            and sum(1 for t in tickets if t.state == state) < limit
        ):
            params = ["per_page=100", "state=all", "sort=updated", "direction=desc", f"page={page}"]
            if labels:
                params.append("labels=" + ",".join(labels))
            rows = self._call(self._issues_url("?" + "&".join(params))) or []
            if not rows:
                break
            # Dedup by id (review finding): `sort=updated` offset pagination is a LIVE ordering,
            # not a snapshot — an issue touched between two page fetches jumps to the head and
            # is served again on the next page. A duplicate must neither eat the scan budget
            # nor count twice toward `limit` (displacing a real match).
            for row in rows:
                if "pull_request" in row:
                    continue
                ticket = self._row_to_ticket(row)
                if ticket.id not in seen:
                    seen.add(ticket.id)
                    tickets.append(ticket)
            if len(rows) < 100:
                break  # last page — nothing left to paginate into
            page += 1
        return [t for t in tickets if t.state == state]

    def search(self, query: str, *, state=None, limit=30) -> list[Ticket]:
        import urllib.parse

        q = f"repo:{self.owner}/{self.repo} is:issue {query}"
        if state in (State.DONE, State.CANCELLED):
            q += " is:closed"
        elif state is not None:
            q += " is:open"
        url = f"{_api_root()}/search/issues?q={urllib.parse.quote(q)}&per_page={min(limit, 100)}"
        result = self._call(url) or {}
        rows = result.get("items", []) if isinstance(result, dict) else []
        tickets = [self._row_to_ticket(r) for r in rows]
        # GitHub search only knows is:open/is:closed; our finer states (in-progress/in-review)
        # live in labels, so post-filter client-side — otherwise `find --state in-progress`
        # would return every open issue. Known and accepted: unlike list() (which scans a full
        # 100-issue budget before filtering — see _list_state_filtered), this fetch is capped
        # at `limit` rows BEFORE the filter, so a state-filtered search can under-report when
        # non-matching results sort ahead of the matches. `task audit` never uses search();
        # `task find --state` is an interactive convenience where a wider fetch is not worth
        # the latency.
        if state is not None:
            tickets = [t for t in tickets if t.state == state]
        return tickets[:limit]

    def comment(self, ticket_id: str, body: str) -> None:
        num = _issue_number(ticket_id)
        self._call(self._issues_url(f"/{num}/comments"), method="POST", payload={"body": body})

    def attach(self, ticket_id: str, file_path: str) -> str:
        # GitHub has no issue-attachment REST endpoint; the upload path is the web UI only.
        # We record the local path as a reference comment so the proof is auditable. The
        # body's Screenshots section embeds the same ref via render().
        self.comment(ticket_id, f"screenshot: {file_path}")
        return file_path

    def transition(self, ticket_id: str, state: State) -> Ticket:
        ticket = self.get(ticket_id)
        ticket.state = state
        return self.update(ticket)

    def session_tickets(self, session_label: str, *, limit=30) -> list[Ticket]:
        return self.list(labels=[session_label], limit=limit)

    def current_user(self) -> str | None:
        """Best-effort login of this token's owner (issue #59's stale-nudge personal-scope
        filter). ``None`` on any failure — never an authorization boundary, just a filter hint;
        the caller (``cli.py``) falls back to an unfiltered scan when it can't be determined."""
        try:
            row = self._call(f"{_api_root()}/user")
        except BackendError:
            return None
        login = row.get("login") if isinstance(row, dict) else None
        return login if isinstance(login, str) and login else None


# ── module helpers ──────────────────────────────────────────────────────────────────


def _issue_number(ticket_id: str) -> str:
    # removeprefix, NOT lstrip("#") — lstrip strips ALL leading '#' (e.g. "##1" -> "1"),
    # mangling an id; removeprefix drops exactly one leading marker.
    return ticket_id.removeprefix("#").strip()


def _derive_state(row: dict, labels: list[str]) -> State:
    for lbl in labels:
        if lbl in _LABEL_STATE:
            return _LABEL_STATE[lbl]
    return State.DONE if row.get("state") == "closed" else State.TODO


def _resolve_repo(repo_spec: str, repo_root) -> tuple[str, str]:
    """Resolve ``owner/repo``. ``auto`` reads the git ``origin`` remote of ``repo_root``."""
    if repo_spec and repo_spec != "auto":
        if "/" not in repo_spec:
            raise BackendError(f"github.repo must be 'owner/name' or 'auto', got {repo_spec!r}")
        owner, repo = repo_spec.split("/", 1)
        return owner, repo
    return _git_origin_owner_repo(repo_root)


def _git_origin_owner_repo(repo_root) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackendError("cannot resolve github repo: git not available") from exc
    if out.returncode != 0:
        raise BackendError("cannot resolve github repo: no 'origin' remote (set github.repo in task.yaml)")
    url = out.stdout.strip()
    return _parse_remote(url)


def _parse_remote(url: str) -> tuple[str, str]:
    """Parse ``owner/repo`` from an https or ssh GitHub remote URL."""
    cleaned = url.removesuffix(".git")
    if cleaned.startswith("git@") and ":" in cleaned:
        path = cleaned.split(":", 1)[1]
    elif "github.com/" in cleaned:
        path = cleaned.split("github.com/", 1)[1]
    else:
        raise BackendError(f"unrecognized github remote URL: {url}")
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise BackendError(f"cannot parse owner/repo from remote: {url}")
    return parts[0], parts[1]
