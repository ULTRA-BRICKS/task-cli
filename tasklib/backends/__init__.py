"""Backend abstraction — the ``TicketBackend`` protocol + the adapter selector.

Adapters call the provider API **directly** (stdlib ``urllib``, no per-call subprocess):
``github_issues`` → GitHub REST, ``linear`` → Linear GraphQL. The rest of the tool never
touches a provider; it speaks only this protocol, so a fake backend (tests) is a drop-in.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model import State, Ticket


@runtime_checkable
class TicketBackend(Protocol):
    """The contract every backend implements. Methods raise ``BackendError`` on failure."""

    name: str

    def create(self, ticket: Ticket) -> Ticket:
        """Create a ticket from a fully-populated, policy-passed ``Ticket``. Returns it with
        ``id``/``url`` filled in."""
        ...

    def get(self, ticket_id: str) -> Ticket:
        """Fetch a single ticket by id, body parsed back into structured fields."""
        ...

    def update(self, ticket: Ticket) -> Ticket:
        """Update an existing ticket (``ticket.id`` set). Returns the updated ``Ticket``."""
        ...

    def list(
        self,
        *,
        labels: list[str] | None = None,
        state: State | None = None,
        limit: int = 30,
    ) -> list[Ticket]:
        """List tickets, optionally filtered by labels/state."""
        ...

    def search(
        self, query: str, *, state: State | None = None, limit: int = 30
    ) -> list[Ticket]:
        """Full-text search over title+body."""
        ...

    def comment(self, ticket_id: str, body: str) -> None:
        """Append a comment to a ticket."""
        ...

    def attach(self, ticket_id: str, file_path: str) -> str:
        """Attach a file (e.g. a screenshot). Returns a reference (URL or marker)."""
        ...

    def transition(self, ticket_id: str, state: State) -> Ticket:
        """Move a ticket to a new normalized state."""
        ...

    def session_tickets(self, session_label: str, *, limit: int = 30) -> list[Ticket]:
        """List tickets carrying a ``session:<id>`` label (the backend-side session view)."""
        ...

    def current_user(self) -> str | None:
        """Best-effort identity (login/id) of the authenticated caller, or ``None`` if it
        can't be determined. A filter hint only (issue #59's personal-scope stale-ticket nudge)
        — never an authorization boundary, and never raises; failures degrade to ``None``."""
        ...


class BackendError(RuntimeError):
    """A backend call failed (HTTP error, auth error, not-found, malformed response)."""


# The structured exit code for an ambiguous backend failure (task-cli bug: a create's response
# read failed AFTER the server already accepted it, so the crash looked identical to "nothing
# happened" and a retry could double-create a ticket). Hard-coded (not imported) so this package
# stays stdlib-only at import time — mirrors ``tasklib.transitions.EXIT_ILLEGAL_TRANSITION``,
# which pins its literal the same way.
#
# Deliberately NOT aliased to any ``agenttools_errors`` class, unlike ``EXIT_ILLEGAL_TRANSITION``
# (review finding): the shared contract's closest-sounding code, ``EXIT_NETWORK`` (7), documents
# itself as "a network/remote operation failed... DNS, timeout, 5xx" — the class a caller is
# TOLD is safe to retry. This exception means the exact opposite (the request may have already
# succeeded — do NOT blindly retry), so reusing that code would tell any exit-code-aware script
# to do precisely the double-create this exception exists to prevent. None of the shared
# contract's other classes (0-8, 127) fit "the outcome is unknown, verify before retrying"
# either, so this is a task-cli-local code with no upstream pin — the next free integer after the
# contract's highest assigned value (8). ``tests/test_backends.py`` asserts it collides with
# nothing in ``agenttools_errors.EXIT_CODES`` when that (optional, not installed here) library is
# present.
EXIT_AMBIGUOUS = 9


class AmbiguousBackendError(RuntimeError):
    """The backend call's outcome is UNKNOWN — the connection dropped while reading the
    response, after the provider already accepted the request (see
    :class:`tasklib.backends.http.AmbiguousHttpError`, which this wraps). The ticket may already
    exist server-side even though this process never saw it.

    Deliberately NOT a :class:`BackendError` subclass. Every ``except BackendError`` call site in
    ``cli.py`` treats that class as "a clean, known failure — nothing happened server-side" (a
    4xx, an auth error, a malformed config) and converts it uniformly into the same exit-2
    ``_UserError``. Subclassing ``BackendError`` would make those call sites silently swallow
    this into that same clean-refusal bucket — indistinguishable from "nothing happened" — which
    is exactly the ambiguity that let a retry double-create a ticket. A caller that must react to
    this specifically (``cmd_create``) catches it by name; everywhere else it propagates
    unhandled, same as before this class existed (no regression — those paths never handled the
    underlying read failure either).
    """

    exit_code: int = EXIT_AMBIGUOUS


def backend_coordinate(backend) -> str:
    """The backend-scoped repo identity (``github-issues:owner/repo`` or ``linear:TEAM/project``)
    for an already-constructed backend instance.

    Same shape as :attr:`tasklib.projects.Project.coordinate`, which computes the identical
    string from a config overlay — this is the live-instance counterpart, for callers (like the
    session sidecar, task-cli#83) that only have a resolved ``backend``, not a ``Project``. Pure:
    reads attributes the backend already resolved at construction time, no I/O.
    """
    # `getattr(..., default)` only substitutes the default when the attribute is ABSENT — a
    # backend whose attribute is explicitly `None` (e.g. `LinearBackend(project=None)`) would
    # otherwise stringify to the literal `"None"`, a truthy value that corrupts the coordinate
    # (review finding, task-cli#83). `or ""` collapses `None` to empty before stripping.
    if backend.name == "linear":
        team = str(getattr(backend, "team_key", "") or "").strip().upper()
        project = str(getattr(backend, "project", "") or "").strip()
        return f"linear:{team}/{project}" if project else f"linear:{team}"
    if backend.name == "tasks":
        url = str(getattr(backend, "url", "") or "").strip()
        team = str(getattr(backend, "team_key", "") or "").strip().upper()
        return f"tasks:{url}/{team}" if team else f"tasks:{url}"
    owner = str(getattr(backend, "owner", "") or "").strip().lower()
    repo = str(getattr(backend, "repo", "") or "").strip().lower()
    return f"github-issues:{owner}/{repo}"


def verified_ticket_coordinate(cfg, backend, ticket) -> str:
    """``ticket``'s REAL coordinate, correcting :func:`backend_coordinate` when the two disagree
    (task-cli#83 review finding).

    Outside a repo, a Linear ticket id is routed to whichever registered same-team project's
    config happened to be picked FIRST (``cli._route_id_to_project``) to authenticate/query
    with — Linear resolves ``get``/``update`` by identifier alone, so that pick can still
    successfully fetch a ticket that actually lives in a DIFFERENT project on the same team.
    ``ticket.backend_project_id`` (populated from Linear's own response, when the single-ticket
    fetch requested it) is the ground truth; if it names a project THIS config also registers
    under the same team, prefer that project's coordinate over the one initially guessed — so
    the session-sidecar entry this feeds (task-cli#83) is attributed to the ticket's actual
    project, not the config that merely happened to be able to fetch it.

    Degrades to the plain :func:`backend_coordinate` whenever there's nothing to correct
    (GitHub, no reported project id, the report matches what was already configured, or no
    OTHER registered project claims that id) — pure, never raises.
    """
    naive = backend_coordinate(backend)
    actual_project = str(getattr(ticket, "backend_project_id", "") or "").strip()
    if backend.name != "linear" or not actual_project:
        return naive
    configured_project = str(getattr(backend, "project", "") or "").strip()
    if actual_project == configured_project:
        return naive
    from ..projects import projects_from_config

    team = str(getattr(backend, "team_key", "") or "").strip().upper()
    for project in projects_from_config(getattr(cfg, "data", None) or {}):
        if project.backend != "linear":
            continue
        lin = project.overlay.get("linear", {})
        if str(lin.get("team", "")).strip().upper() != team:
            continue
        if str(lin.get("project", "")).strip() == actual_project:
            return project.coordinate
    return naive


def get_backend(config, *, env: dict | None = None):
    """Construct the configured backend from a ``LoadedConfig``. Effectful (harvests creds).

    Imported lazily by the entrypoint; importing this package must stay dependency-light.
    """
    backend = config.backend
    if backend == "github-issues":
        from .github_issues import GitHubIssuesBackend

        return GitHubIssuesBackend.from_config(config, env=env)
    if backend == "linear":
        from .linear import LinearBackend

        return LinearBackend.from_config(config, env=env)
    if backend == "tasks":
        from .tasks import TasksBackend

        return TasksBackend.from_config(config, env=env)
    raise BackendError(f"unknown backend {backend!r}")
