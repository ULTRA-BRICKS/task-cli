"""Session id detection + the local sidecar index.

A "session" is a unit of work the agent is doing for the user; ``task list`` defaults to the
current session's tickets. The id is detected by a precedence chain (§6):

    1. ``$TASK_SESSION`` — explicit, harness-set (most reliable).
    2. tmux pane — the ``$TMUX_PANE`` / pane id, stable for the life of a pane.
    3. git branch — the current branch name (the steady-state default in a dev repo).

Every ticket touched in a session is labelled ``session:<id>`` (portable across machines)
AND recorded in a local sidecar ``~/.local/state/task-cli/sessions/<id>.jsonl`` (fast,
offline lookup). The label is the durable source of truth; the sidecar is a cache that lets
``task list`` answer instantly without a backend round-trip.

Detection is pure-ish (reads env + an optional injected git-branch resolver). The sidecar
read/write is the only I/O, isolated to the bottom of the file and keyed off ``$XDG_STATE_HOME``
so tests can redirect it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DETECT_ORDER = ("env:TASK_SESSION", "tmux-pane", "git-branch")
LABEL_PREFIX = "session:"


@dataclass
class Session:
    """A detected session: its stable id, the source, and the configured label prefix."""

    id: str
    source: str  # one of the detect-order tokens, or "none"
    prefix: str = LABEL_PREFIX

    @property
    def label(self) -> str:
        return f"{self.prefix}{self.id}"


@dataclass(frozen=True)
class SessionEntry:
    """One sidecar touch record after de-duplication.

    ``repo`` is the ticket's OWN backend-scoped coordinate at record() time
    (:func:`tasklib.backends.backend_coordinate`, e.g. ``"github-issues:owner/repo"``) — not
    whatever repo the current reader happens to be in. A session id (git branch / tmux pane) is
    not repo-unique, so without this a reader in one repo could see another, unrelated repo's
    ticket under the same session id (task-cli#83). Empty for legacy lines written before this
    field existed; a caller that needs the leak fix must treat an empty/mismatched ``repo`` as
    "don't show", never "show anyway".
    """

    id: str
    title: str = ""
    ts: int = 0
    repo: str = ""


def _slug(value: str) -> str:
    """Make a label-safe, filesystem-safe id from a raw source string.

    Lowercased, non-alphanumerics → ``-``, collapsed, trimmed. A long/odd value is hashed to
    a short suffix so the label stays bounded and stable.
    """
    base = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    if not base:
        base = "anon"
    if len(base) > 40:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]
        base = f"{base[:31]}-{digest}"
    return base


def detect(
    env: dict[str, str] | None = None,
    detect_order: tuple[str, ...] = DEFAULT_DETECT_ORDER,
    git_branch: Callable[[], str | None] | None = None,
    *,
    cwd: str | None = None,
    label_prefix: str = LABEL_PREFIX,
) -> Session:
    """Detect the current session by the precedence chain. Always returns a ``Session``.

    ``git_branch`` is injected for testability; it defaults to a real ``git`` call rooted at
    ``cwd`` (so ``task -C /other/repo`` detects the branch of THAT repo, matching where the
    backend resolves the origin — not the shell's cwd). If nothing resolves, returns
    ``Session("default", "none")`` so the tool always has a scope. ``label_prefix`` comes from
    config (``session.label_prefix``) so the ``session:`` prefix is configurable end to end.
    """
    env = os.environ if env is None else env
    branch_fn = git_branch if git_branch is not None else (lambda: _git_branch(cwd))

    for token in detect_order:
        if token == "env:TASK_SESSION":
            raw = env.get("TASK_SESSION")
            if raw and raw.strip():
                return Session(_slug(raw), "env:TASK_SESSION", label_prefix)
        elif token == "tmux-pane":
            raw = env.get("TMUX_PANE") or env.get("TMUX")
            if raw and raw.strip():
                return Session(_slug(raw), "tmux-pane", label_prefix)
        elif token == "git-branch":
            raw = branch_fn()
            if raw and raw.strip():
                return Session(_slug(raw), "git-branch", label_prefix)
    return Session("default", "none", label_prefix)


def _git_branch(cwd: str | None = None) -> str | None:
    cmd = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
    if cwd:
        cmd = ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    branch = out.stdout.strip()
    return branch if branch and branch != "HEAD" else None


# ── sidecar index ──────────────────────────────────────────────────────────────────


def state_dir(env: dict[str, str] | None = None) -> Path:
    """The sidecar root. ``$XDG_STATE_HOME/task-cli/sessions`` or ``~/.local/state/...``."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".local", "state")
    return Path(base) / "task-cli" / "sessions"


def sidecar_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    return state_dir(env) / f"{session_id}.jsonl"


def record(
    session_id: str, ticket_id: str, title: str, env: dict[str, str] | None = None, *, repo: str = ""
) -> None:
    """Append a ticket reference to the session's sidecar (idempotent on ticket id).

    ``env`` stays the 4th POSITIONAL parameter (unchanged from before task-cli#83) — an
    existing caller doing ``record(session_id, id, title, env_dict)`` must keep landing in
    ``env``, not silently serialize the env dict into ``repo``. ``repo`` is therefore
    keyword-only, appended after ``env``.

    ``repo`` is the ticket's own backend coordinate (see :class:`SessionEntry`) — pass
    :func:`tasklib.backends.backend_coordinate` of the backend the ticket was created/touched
    through, so a later repo-scoped read (task-cli#83) can tell this entry apart from another
    repo's ticket sharing the same session id. The sidecar is a cache; duplicate
    ``(repo, ticket_id)`` pairs are de-duplicated on read, so a re-append is fine. Never raises
    into the caller — a sidecar write failure must not break a ticket operation.
    """
    path = sidecar_path(session_id, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"id": ticket_id, "title": title, "ts": int(time.time()), "repo": repo}, ensure_ascii=False
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return


def read_ids(session_id: str, env: dict[str, str] | None = None) -> list[str]:
    """Return the recorded ticket ids, newest-last. Already de-duplicated by
    :func:`read_entries` keying on ``(repo, id)`` — two different repos sharing a ticket
    NUMBER (both have a "#1") are two different tickets, so the bare id can legitimately
    appear more than once here; the same ``(repo, id)`` pair never does."""
    return [entry.id for entry in read_entries(session_id, env)]


def read_entries(session_id: str, env: dict[str, str] | None = None) -> list[SessionEntry]:
    """Return de-duplicated sidecar entries, newest-last.

    The sidecar is append-only. Re-appending a ``(repo, id)`` pair is a touch, so the latest
    line wins and moves that ticket to the end. Malformed legacy lines are ignored; missing
    timestamps degrade to ``0`` so old sidecars still produce ids without pretending to know
    recency.

    Deduplicates by ``(repo, ticket_id)``, NOT by ``ticket_id`` alone (task-cli#83 review
    finding): two different repos commonly both have a ticket ``#1``. Keying only on the bare
    id would let repo B's ``#1`` silently evict repo A's ``#1`` entry from the sidecar read —
    an outright data-loss bug, not just a display leak — before any repo-scoped filter
    (:func:`tasklib.cli._recent_tickets_block`) even runs. A legacy line with no ``repo``
    recorded keys as ``("", ticket_id)``, so legacy entries still dedup among themselves the
    same way they always did.
    """
    path = sidecar_path(session_id, env)
    if not path.is_file():
        return []
    seen: dict[tuple[str, str], SessionEntry] = {}
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            tid = obj.get("id")
            if isinstance(tid, str) and tid:
                repo = str(obj.get("repo") or "")
                key = (repo, tid)
                seen.pop(key, None)
                try:
                    ts = int(obj.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                seen[key] = SessionEntry(id=tid, title=str(obj.get("title") or ""), ts=ts, repo=repo)
    except OSError:
        return []
    return list(seen.values())
