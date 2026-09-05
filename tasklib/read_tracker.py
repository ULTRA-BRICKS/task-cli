"""Local, session-scoped record of "this ticket was just read" — the read-before-edit gate.

``cmd_change --what`` can silently overwrite a ticket's core description with no proof the
caller ever looked at what is CURRENTLY there. This module is the tracking half of the fix:
``task read``/``task view`` (and ``task new``/``create`` for the ticket it just wrote) call
:func:`record`; ``cmd_change`` calls :func:`is_fresh` before letting ``--what`` through. The
gate refusal itself, the config on/off switch (``enforce.read_before_edit``), and the
per-command ``--skip-read-before-edit`` waiver live in ``policy.py``/``cli.py`` — this module
only answers "was <ticket> read recently by <session>", doing the one bit of I/O that requires.

Design decisions (see task-cli#119):

**Scope: per (session, backend coordinate, ticket id), not a machine-wide marker per ticket
id.** The problem statement is "did the AGENT ABOUT TO MAKE THIS EDIT actually see the current
content" — not "has anyone, anywhere, looked at this ticket recently". A machine-wide marker
would let an agent that never read the ticket sail through the gate because some UNRELATED
concurrent agent happened to `task read` it for its own purposes moments earlier — that
satisfies the letter of "somebody looked" while defeating the actual goal (this agent's edit is
still blind). Session id reuses the SAME precedence chain ``tasklib.session`` already uses to
scope ``task list`` (``$TASK_SESSION`` → tmux pane → git branch), so the two concepts of
"session" in this tool never diverge. The backend coordinate (``tasklib.backends.
backend_coordinate`` / ``verified_ticket_coordinate``) closes the same cross-repo id-collision
hole ``tasklib.session`` was fixed for in task-cli#83: two different repos/projects both have a
ticket "#1", and a git-branch-derived session id is not repo-unique, so without the coordinate a
read of repo A's #1 could satisfy the gate for repo B's unrelated #1.

This scoping is NOT airtight against every concurrent-agent shape, and the honest boundary is
the session id's OWN precedence chain, not this module: two agents that share a git checkout
with no `$TASK_SESSION` set and no tmux pane (the ``git-branch`` fallback) resolve the SAME
session id, so one agent's `task read` on a ticket DOES satisfy the OTHER's gate on that same
ticket — this module has no way to tell them apart once `tasklib.session.detect()` hands back
one shared id (review finding, task-cli#119). This is accepted, not fixed here: a stronger id
would need `tasklib.session`'s own precedence chain to distinguish them (e.g. requiring
`$TASK_SESSION` per agent), which is out of this gate's scope — it inherits whatever
concurrency guarantee `task list`'s session scoping already has, no better and no worse.

**TTL: 30 minutes (``DEFAULT_TTL_S`` / ``$TASK_READ_TTL_S``).** Two existing fixed windows in
this codebase set the boundaries to reason from: the agent-tools ``stop-completion-selfcheck``
hook's cooldown marker defaults to 1800s (30 min) for "is this still the same task", and this
repo's own ``_recent_tickets_block`` uses a fixed 1-hour window for "still worth showing as
just-created". A read-before-edit gate sits between those: long enough that a realistic
read → think → draft → `change` turn is never punished for taking a few minutes, short enough
that "I read it a while ago and don't actually remember what it said" (Alex's explicit "3 days
ago should probably not count" example) can't satisfy it. 30 minutes is the agent-tools
precedent value and comfortably covers one continuous editing turn without covering "earlier
today, in a different train of thought" — so it is reused rather than invented fresh.
Overridable via ``$TASK_READ_TTL_S`` for ops/tests, mirroring ``SELFCHECK_TTL_S``.

**Failure mode: fail CLOSED, and say WHICH failure.** :func:`is_fresh` returns a tri-state:
``True`` (fresh read on file), ``False`` (the store is readable but has no fresh record for
this ticket — the ordinary "go read it" case), or ``None`` (the store itself could not be read
— an OSError, e.g. a permissions problem). The caller (``cli._read_before_edit_violation``)
turns ``False`` into "run `task read <id>` first" and ``None`` into a DIFFERENT message that
says the marker store is unreadable, so a broken store doesn't look like a merely-unread ticket
and send the caller into a `task read` → still-blocked loop with no visible reason why. Either
way the edit is refused — "can't determine" must never mean "allow" (Alex's explicit fail-closed
requirement) — only the message differs. :func:`record` stays best-effort/silent on a write
failure (mirrors ``tasklib.session.record``): a failed write simply means the next
:func:`is_fresh` finds nothing and the edit is refused, which IS the fail-closed behavior, not a
separate failure path.

**Known limitation: a WRITE-side failure degrades to the wrong message, not the wrong outcome.**
If the marker directory is genuinely unwritable (e.g. a read-only `~/.local/state`), every
:func:`record` call silently fails (as designed, above) — but the NEXT :func:`is_fresh` then
sees a plain missing file (`FileNotFoundError`) and returns ``False``, the ordinary "go read it"
case, not ``None`` ("store unreadable"). The caller re-runs `task read`, which ALSO silently
fails to record, and gets the same message again — an undiagnosable loop, the exact failure
shape the ``None`` branch exists to prevent, just entered from the write side instead of the
read side (review finding, task-cli#119). Not fixed here: distinguishing "genuinely
never read" from "this session's reads keep failing to persist" from inside `is_fresh` alone
would need probing directory writability on every miss (racy, and adds a syscall to the hot
path for a rare failure mode) or persisting write-failure state somewhere that itself needs to
survive the very failure it's recording. The edit is still correctly REFUSED either way — this
is a message-wording gap, not a fail-open — and a genuinely broken state directory is also
likely to break plenty else in this tool (the session sidecar, the daemon) well before this
gate's message wording becomes the diagnostic bottleneck.

**"task new"/"create" counts as reading the ticket it just wrote — and so does a successful
"change --what".** An agent that just authored a ticket's title/what/why/impact/acceptance has,
by construction, seen the current content — there is nothing yet that an immediately-following
``task change <id> --what "..."`` could silently clobber that this same command didn't just
write. ``cli.py`` calls :func:`record` for the newly created ticket right after a successful
``create``/``new`` (and the ``task classify --create`` inbound-hook path, which also creates a
ticket), so the ordinary create-then-immediately-refine workflow is never blocked. The SAME
reasoning applies to ``cmd_change`` itself: it re-marks the ticket read after any ``--what`` edit
that SUCCEEDED (whether it passed the gate outright or was waived with
``--skip-read-before-edit``) — the session now knows the current text because it just supplied
it. This is deliberately gated on ``args.what`` specifically (review finding, task-cli#119): a
metadata-only edit (``--due``/``--label``/…) never touches or reveals the description, so it
must NOT mint a fresh marker — doing so unconditionally would let a two-command sequence
(metadata edit, then an unrelated blind ``--what`` edit, both inside the TTL) bypass the gate
entirely, which is exactly the ordinary "mark in-progress, then rewrite later" workflow, not an
edge case.

One direct consequence worth being honest about: because a WAIVED ``--what`` edit still refreshes
the marker, ``--skip-read-before-edit "<reason>"`` is not quite "this command only" in an
absolute sense — it opens a TTL-length window afterward in which a FURTHER ``--what`` edit from
the SAME session passes with no flag, because that session now legitimately knows the
just-written content. What the skip does NOT do (and what the dedicated regression test proves)
is let a *different*, still-genuinely-unread edit ride on a skip *persisted in ``ticket.skips``
from a past command* — that persisted record is audit-only and is never consulted by the live
check.

**Known limitation: the marker proves "this session's `read`/`what`-edit was recent", not
"nobody else changed the ticket since".** A read at T0, an unrelated edit by someone/something
else at T0+5m, then this session's own ``change --what`` at T0+10m still passes — the marker has
no way to know the content it once saw is now stale. Detecting that would need comparing a
content fingerprint (a hash of `what`, or the backend's `updated_at`) at check time, not just a
timestamp — out of scope for this gate (task-cli#119 asked for "did an agent look recently", not
"is the agent's mental model still current"), and a legitimate follow-up if it ever matters in
practice.

**Known limitation: an unresolved session (``Session.source == "none"`` — no
``$TASK_SESSION``/tmux pane/git branch) shares one ``"default"`` bucket**, the same fallback
``tasklib.session`` uses. Two concurrent no-session processes touching the same (repo, ticket
id) are indistinguishable in that bucket — a read by one satisfies the gate for the other. This
mirrors an accepted, pre-existing limitation of the session sidecar itself (see
``cli._recent_tickets_block``'s own "none" guard) rather than a new gap introduced here; it only
matters when a process resolves NO session id at all, which is rare in this tool's normal usage
(most invocations run under tmux or in a git checkout).

**Known limitation: the repo coordinate can (rarely) differ between where a ticket is RECORDED
read and where it's CHECKED, on Linear.** ``cli._mark_ticket_read`` computes the coordinate
(via ``verified_ticket_coordinate``, documented pure/offline/deterministic) from whichever
Ticket OBJECT the caller passes it — but ``cmd_create``/``_classify_create`` pass the object
``backend.create()`` just returned, while ``cmd_change``'s check uses a SEPARATELY fetched
object (``backend.get()``), and its own post-edit re-mark uses yet a third
(``backend.update()``'s return value). These are the same underlying ticket, but not
necessarily the same field values, if the backend's response shapes differ per call. On Linear
specifically: ``create()``'s GraphQL response omits ``project{id}`` (a deliberate field-set
trim — see ``linear.py``'s ``_ISSUE_FIELDS_BASE`` comment), so a create-response Ticket always
reports ``backend_project_id == ""``, while ``get()`` and ``update()`` (both request the fuller
``_ISSUE_FIELDS``, including ``project{id}``) report the real value. This is a no-op for the
common cases — GitHub's coordinate never reads the ticket at all, and a Linear backend with
``project:`` PINNED in config resolves to the same naive coordinate from either response shape
(proven in ``tests/test_backends.py::
test_verified_ticket_coordinate_matches_between_linear_create_and_get_responses``) — but it CAN
diverge for a project-LESS Linear config (``project: ""``) whose team also registers multiple
same-team projects in ``projects:``, one of which happens to be the default project Linear
auto-assigns new issues to: ``get()``/``update()`` can then report a real ``backend_project_id``
that resolves to a corrected, non-naive coordinate the create-time marker never used. Effect is
narrow and safe (fail-closed: at worst, one extra ``task read`` is needed in that specific
config shape) — not fixed here, since fixing it would mean either widening
``LinearBackend.create()``'s GraphQL response (touches a well-tested, shared module) or an extra
round-trip on every create; flagged as a tracked, deliberate trade-off rather than a
silently-assumed guarantee.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# After this many seconds a recorded read is considered stale — see the TTL rationale above.
DEFAULT_TTL_S = 1800


def _safe_session_id(session_id: str) -> str:
    """Make ``session_id`` safe to use as a filename component.

    ``session_id`` normally already comes pre-slugged from ``tasklib.session.detect()`` (which
    runs every id — including a raw, attacker/caller-controlled ``$TASK_SESSION`` — through its
    own ``_slug()`` before this module ever sees it). This module must not TRUST that upstream
    contract, though: it takes ``session_id`` as a plain string parameter with no static
    guarantee it was ever slugged, and blindly formatting an unsanitized value into a filename
    (``state_dir() / f"{session_id}.jsonl"``) is a path-traversal arbitrary-file-write primitive
    — a ``session_id`` of ``"../../../../etc/cron.d/x"`` would escape ``state_dir()`` entirely
    (review finding, task-cli#119).

    Reuses ``tasklib.session._slug`` rather than a second, independent sanitizer (review
    finding: an earlier version of this function shipped its own regex-based cleaner —
    exactly the "second copy that can silently drift" anti-pattern :func:`state_dir`'s own
    docstring already argues against, one function over). Concretely, the earlier copy had no
    length cap, unlike ``_slug`` (which caps at 40 chars + a hash suffix): a session id over the
    filesystem's filename limit would make :func:`record` fail (``OSError``, silently swallowed)
    and :func:`is_fresh` then return the WRONG tri-state (``None``, "store unreadable") instead
    of the correct "not read yet" — the exact misdiagnosis the ``False``/``None`` split exists
    to prevent, entered via the sanitization path itself. ``_slug`` is idempotent on an
    already-slugged id (``detect()``'s own output, e.g. ``"hyp-123-add-toggle"``, maps to
    itself), so this is a no-op for the common case and closes the gap for every other one.
    """
    from .session import _slug

    return _slug(session_id)


def state_dir(env: dict[str, str] | None = None) -> Path:
    """The marker root: ``task-cli/reads``, a SIBLING of ``tasklib.session``'s
    ``task-cli/sessions`` under the same ``$XDG_STATE_HOME``/``~/.local/state`` base.

    Derives the base from :func:`tasklib.session.state_dir` (``.parent``) rather than
    re-implementing the ``$XDG_STATE_HOME``/``~/.local/state`` resolution logic here (review
    finding, task-cli#119): a second, independent copy of that fallback chain risks silently
    drifting from the original if it's ever tweaked (a new env var, a platform fix) — landing
    markers under a different root than the sessions sidecar with no visible error, since a
    wrong-but-consistent path just makes every read look "never happened" (fail-closed, but
    undiagnosable). One shared implementation keeps the sibling relationship structural.
    """
    from .session import state_dir as _sessions_dir

    return _sessions_dir(env).parent / "reads"


def _log_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    return state_dir(env) / f"{_safe_session_id(session_id)}.jsonl"


def _ttl_s(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("TASK_READ_TTL_S")
    if not raw:
        return DEFAULT_TTL_S
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TTL_S


def record(
    session_id: str, ticket_id: str, *, repo: str = "", env: dict[str, str] | None = None
) -> None:
    """Record that ``session_id`` just read ``ticket_id`` (scoped to ``repo``, see module
    docstring). Append-only, like ``tasklib.session.record``; never raises into the caller — a
    write failure here just means the next :func:`is_fresh` finds no fresh entry and the next
    edit is refused, which is the intended fail-closed behavior, not a separate error path."""
    path = _log_path(session_id, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"id": ticket_id, "repo": repo, "ts": time.time()}, ensure_ascii=False
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return


def is_fresh(
    session_id: str, ticket_id: str, *, repo: str = "", env: dict[str, str] | None = None
) -> bool | None:
    """Was ``ticket_id`` (scoped to ``repo``) read by ``session_id`` within the TTL?

    Returns ``True`` (fresh read found), ``False`` (the store is readable but has no fresh
    match — the ordinary "not read yet" case), or ``None`` (the store could not be read at all
    — an OSError; see the module docstring's fail-closed section for why the caller must treat
    this differently from ``False`` in its message, while refusing the edit either way). A
    malformed individual line (bad JSON, wrong shape, or a timestamp in the future — see below)
    is skipped, not fatal — the file is append-only and a genuine entry for this ticket may
    still be intact elsewhere in it; only a failure to read the FILE itself degrades to
    ``None``.

    Deliberately does NOT use ``Path.is_file()``/``Path.exists()`` to decide "missing vs.
    unreadable": both of those swallow ``OSError`` internally and return ``False`` for ANY
    stat failure, including a permissions problem on the file or an inaccessible parent
    directory (review finding, task-cli#119) — exactly the case this function's ``None``
    branch exists to distinguish from "genuinely never read". ``open()``/``read_text()`` does
    NOT swallow that: a missing file raises the specific ``FileNotFoundError`` (→ ``False``,
    the ordinary case), while a permissions/other I/O failure raises a different ``OSError``
    (→ ``None``). A file that exists but isn't valid UTF-8 (corrupt/binary garbage — this
    module owns the file, but nothing stops it being clobbered by something else) raises
    ``UnicodeDecodeError`` from ``read_text`` — NOT an ``OSError`` subclass — and is caught
    explicitly for the same ``None`` outcome (review finding, task-cli#119): "the store exists
    but can't be trusted" must never silently read as "never read".
    """
    path = _log_path(session_id, env)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError):
        return None

    ttl = _ttl_s(env)
    now = time.time()
    latest_ts: float | None = None
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            # a single corrupt/adversarial line is skipped like any other malformed line, not
            # fatal to the whole read (review finding, task-cli#119). `ValueError` (not the
            # narrower `json.JSONDecodeError`, a `ValueError` subclass) catches malformed JSON
            # AND CPython's integer-string-conversion limit: a JSON line carrying a `ts` (or any
            # number) written as an integer literal over ~4300 digits raises a bare `ValueError`
            # from `json.loads` itself, not `JSONDecodeError` — a corrupt/adversarial line could
            # otherwise crash `is_fresh` uncaught (review finding, task-cli#119). `RecursionError` is
            # a RuntimeError, not a ValueError, so pathologically deep nesting still needs its
            # own arm here.
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("id") != ticket_id or str(obj.get("repo") or "") != repo:
            continue
        ts = obj.get("ts")
        # A timestamp in the future (clock skew, or a hand-edited/corrupt line) must not grant
        # an indefinite fresh read — reject it rather than let `now - ts` go negative and
        # satisfy the TTL forever (review finding, task-cli#119). `not isinstance(ts, bool)`
        # matters because `bool` is an `int` subclass in Python — a corrupt/adversarial line
        # carrying `"ts": true` would otherwise pass `isinstance(ts, (int, float))` and compare
        # as `1` (a 1970 timestamp), silently downgrading freshness (review finding, task-cli#119).
        # Take the MAX valid timestamp seen, not just "the last one in the file": an
        # out-of-order append (e.g. after a backward clock step/NTP correction) would otherwise
        # let a genuinely fresh earlier line be masked by a later, staler one — always erring
        # toward FAIL-CLOSED (over-refuse), never the reverse, but still worth getting right.
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts <= now:
            latest_ts = ts if latest_ts is None else max(latest_ts, ts)
    if latest_ts is None:
        return False
    return (now - latest_ts) <= ttl
