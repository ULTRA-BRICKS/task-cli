"""task CLI — argparse + subcommand dispatch (the effectful entrypoint).

The thin entry point (``[project.scripts] task = "tasklib.cli:main"`` and the target of the
``bin/task`` shim). It owns argument parsing, the backend/classify shell-outs, and the
filesystem (sidecar). All pure logic lives in the sibling modules (``model``/``render``/
``policy``/``classify``/``session``/``config``). Heavy/optional imports (yaml via config,
the backends) are lazy so ``task --help`` stays fast and dependency-light.

Subcommands: create/new · list · gantt (read-only due-date timeline) · burn (read-only remaining-over-time chart) · web (local HTML burn chart: start/stop/status/run) · read/view · find · change
· attach (implementation screenshot proof) · status · done · check · link (record a typed
blocks/blocked-by/relates-to/duplicate-of/follow-up-of/followed-up-by
relationship between two tickets) · audit (reconciliation backstop — flags/reverts a Done ticket
that never passed the close gates, e.g. one closed by Linear's/GitHub's own PR-merge auto-close)
· mark-shipped (the `gh ship` post-merge hook — records a merge, never closes) · classify ·
module (list/add/assign the repo's product-module taxonomy) · session · daemon (the
due-date reminder watcher: start/stop/status/run).
Global flags: --backend, --repo, --config, --json, --yes, and per-gate --skip-<gate>.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import __version__
from .links import RELATIONS
from .model import Criterion, Screenshot, State, Ticket
from .stdio import ensure_utf8_stdio
from .transitions import TransitionError, validate_transition

if TYPE_CHECKING:
    from .classify import Classification, Verdict
    from .links import LinkEntry
    from .policy import Violation

# ── tiny output helpers (no color dep; honor NO_COLOR) ──────────────────────────────
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _ok(s: str) -> str:
    return _c("32", s)


def _warn(s: str) -> str:
    return _c("33", s)


def _err(s: str) -> str:
    return _c("31", s)


def _dim(s: str) -> str:
    return _c("2", s)


def _bold(s: str) -> str:
    return _c("1", s)


# Per-gate escape-hatch flags: --skip-<gate> "<reason>".
_SKIP_FLAGS = (
    ("--skip-acceptance", "acceptance-criteria"),
    ("--skip-motivation", "motivation"),
    ("--skip-user-impact", "user-impact"),
    ("--skip-user-impact-quality", "user-impact-quality"),
    ("--skip-cost-of-inaction", "cost-of-inaction"),
    ("--skip-screenshots", "screenshots"),
    ("--skip-formatting", "formatting"),
    ("--skip-links", "links"),
    ("--skip-links-url", "links-url"),
    ("--skip-msgref-quote", "msgref-quote"),
    ("--skip-read-before-edit", "read-before-edit"),
    ("--skip-duplicate", "duplicate"),
    ("--skip-todo-shaped", "todo-shaped"),
)


def _add_skip_flags(p: argparse.ArgumentParser) -> None:
    for flag, gate in _SKIP_FLAGS:
        dest = "skip_" + gate.replace("-", "_")
        p.add_argument(
            flag,
            dest=dest,
            metavar="REASON",
            help=f"skip the {gate} gate with a recorded justification",
        )


def _add_create_args(p: argparse.ArgumentParser) -> None:
    """The ticket-creation argument set, shared by `create` and its `new` alias."""
    p.add_argument("--title", help="ticket title")
    p.add_argument(
        "--from-message",
        dest="from_message",
        metavar="TEXT",
        help="raw user text → derive title/body",
    )
    p.add_argument("--what", help="the change (one paragraph)")
    p.add_argument(
        "--acceptance",
        action="append",
        default=[],
        metavar="CRIT",
        help="acceptance criterion (repeatable)",
    )
    p.add_argument("--why", help="motivation")
    p.add_argument("--impact", help="user impact")
    p.add_argument("--if-not-done", dest="if_not_done", help="cost of inaction")
    p.add_argument(
        "--screenshot",
        action="append",
        default=[],
        metavar="PATH",
        help="screenshot (repeatable)",
    )
    p.add_argument("--label", action="append", default=[], help="label (repeatable)")
    p.add_argument(
        "--due", metavar="YYYY-MM-DD", help="due date (the daemon reminds before/at it)"
    )
    p.add_argument("--yes", action="store_true", help="non-interactive; do not prompt")
    p.add_argument(
        "--force",
        dest="force_reason",
        metavar="REASON",
        help="override the links / user-impact-quality gates, or a detected close-duplicate "
        "ticket, with a recorded reason (a false-positive link match, a genuinely-N/A impact, "
        "or an intentional duplicate)",
    )
    _add_skip_flags(p)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="task",
        description="task — the enforced ticket interface. Every request becomes a "
        "well-formed ticket (GitHub Issues / Linear); ticket quality is enforced by the tool.",
    )
    p.add_argument("--version", action="version", version=f"task {__version__}")
    # global flags live on the top parser AND each subparser via parents
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-C",
        "--cwd",
        default=None,
        help="repository to operate on (default: cwd). An explicit path must be inside a git "
        "repository",
    )
    common.add_argument(
        "--backend",
        help="override the configured backend (github-issues|linear|tasks|forgejo)",
    )
    common.add_argument(
        "--repo", help="override the repository (owner/name) for github-issues or forgejo"
    )
    common.add_argument(
        "--config", help="explicit config file (default: ./task.yaml + global)"
    )
    common.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )

    sub = p.add_subparsers(dest="command", metavar="<command>")

    # create (+ `new` alias — same arguments, same handler)
    for verb, blurb in (
        ("create", "create a ticket (enforces the policy gates)"),
        ("new", "alias of create (enforces the policy gates)"),
    ):
        cp = sub.add_parser(verb, parents=[common], help=blurb)
        _add_create_args(cp)

    # list
    lp = sub.add_parser(
        "list", parents=[common], help="list THIS session's tickets (default)"
    )
    lp.add_argument(
        "--all", action="store_true", help="all tickets, not just this session"
    )
    lp.add_argument("--mine", action="store_true", help="only tickets assigned to me")
    lp.add_argument(
        "--state", help="filter by state (todo|in-progress|in-review|done|cancelled)"
    )
    lp.add_argument(
        "--label", action="append", default=[], help="filter by label (repeatable)"
    )
    # default=None so the limit is chosen by interactivity: 100 in a TTY (the pager scrolls),
    # 30 when piped/scripted. An explicit -n always wins (see _effective_limit).
    lp.add_argument(
        "-n",
        type=int,
        default=None,
        dest="limit",
        help="max results (default 100 interactive, 30 piped)",
    )
    lp.add_argument(
        "--no-pager",
        action="store_true",
        dest="no_pager",
        help="never page output (also honors NO_PAGER, $PAGER='')",
    )

    # gantt — read-only due-date timeline (charts `list`'s tickets, same scoping)
    gp = sub.add_parser(
        "gantt",
        parents=[common],
        help="render tickets on a due-date timeline (read-only)",
    )
    gp.add_argument(
        "--all", action="store_true", help="all tickets, not just this session"
    )
    gp.add_argument(
        "--state", help="filter by state (todo|in-progress|in-review|done|cancelled)"
    )
    gp.add_argument(
        "--label", action="append", default=[], help="filter by label (repeatable)"
    )
    gp.add_argument(
        "-n",
        type=int,
        default=None,
        dest="limit",
        help="max tickets (default 100 interactive, 30 piped)",
    )
    gp.add_argument(
        "--width",
        type=int,
        default=None,
        help="bar-area width in columns (default: auto from terminal)",
    )
    gp.add_argument(
        "--no-pager",
        action="store_true",
        dest="no_pager",
        help="never page output (also honors NO_PAGER, $PAGER='')",
    )

    # burn — read-only remaining-open-over-time chart (same scoping as gantt/list)
    bp = sub.add_parser(
        "burn",
        parents=[common],
        help="render remaining tickets over time (read-only burn chart)",
    )
    bp.add_argument(
        "--all", action="store_true", help="all tickets, not just this session"
    )
    bp.add_argument(
        "--state", help="filter by state (todo|in-progress|in-review|done|cancelled)"
    )
    bp.add_argument(
        "--label", action="append", default=[], help="filter by label (repeatable)"
    )
    bp.add_argument(
        "-n",
        type=int,
        default=None,
        dest="limit",
        help="max tickets (default 100 interactive, 30 piped)",
    )
    bp.add_argument(
        "--width",
        type=int,
        default=None,
        help="chart width in columns (default: auto from terminal)",
    )
    bp.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="window start date (default: earliest created date or today)",
    )
    bp.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="window end date (default: today or last closed date)",
    )
    bp.add_argument(
        "--html",
        action="store_true",
        help="print a self-contained HTML burn chart to stdout (not paged)",
    )
    bp.add_argument(
        "--no-pager",
        action="store_true",
        dest="no_pager",
        help="never page output (also honors NO_PAGER, $PAGER='')",
    )

    # read / view
    rp = sub.add_parser("read", parents=[common], help="show a full ticket")
    rp.add_argument("id", help="ticket id (#123 or HYP-456)")
    rp.add_argument(
        "--save-attachments",
        metavar="DIR",
        help="download each native attachment's bytes into DIR (Linear only; "
        "authenticates the fetch so a uploads.linear.app asset doesn't 401 — see "
        "backends/linear.py:fetch_attachment_bytes)",
    )
    vp = sub.add_parser("view", parents=[common], help="alias of read")
    vp.add_argument("id", help="ticket id")
    vp.add_argument("--save-attachments", metavar="DIR", help="see `task read --help`")

    # find
    fp = sub.add_parser("find", parents=[common], help="search tickets (title+body)")
    fp.add_argument("query", help="search query")
    fp.add_argument("--state", help="filter by state")
    fp.add_argument(
        "--all",
        action="store_true",
        help="(reserved) include all; search is global by default",
    )
    fp.add_argument(
        "-n",
        type=int,
        default=None,
        dest="limit",
        help="max results (default 100 interactive, 30 piped)",
    )
    fp.add_argument(
        "--no-pager",
        action="store_true",
        dest="no_pager",
        help="never page output (also honors NO_PAGER, $PAGER='')",
    )

    # change
    chp = sub.add_parser(
        "change",
        parents=[common],
        help="update a ticket (enforces on-done gates when closing)",
    )
    chp.add_argument("id", help="ticket id")
    chp.add_argument("--title", help="new title")
    chp.add_argument("--what", help="replace the What section")
    chp.add_argument(
        "--acceptance",
        action="append",
        default=[],
        metavar="CRIT",
        help="add acceptance criterion",
    )
    chp.add_argument("--why", help="set motivation")
    chp.add_argument("--impact", help="set user impact")
    chp.add_argument("--if-not-done", dest="if_not_done", help="set cost of inaction")
    chp.add_argument(
        "--screenshot",
        action="append",
        default=[],
        metavar="PATH",
        help="add implementation screenshot",
    )
    chp.add_argument("--label", action="append", default=[], help="add label")
    chp.add_argument(
        "--due",
        metavar="YYYY-MM-DD",
        help="set/replace the due date (empty string clears it)",
    )
    chp.add_argument(
        "--post-merge-acceptance",
        dest="post_merge_acceptance",
        metavar="REASON",
        help="record that acceptance is inherently post-merge (e.g. a release publish): "
        "`task gate` passes and reports the reason; `task done` still needs real proofs",
    )
    chp.add_argument(
        "--done", action="store_true", help="close the ticket (runs the on-done gates)"
    )
    chp.add_argument(
        "--deployed",
        metavar="NOTE",
        help="record that this ticket's work has been deployed to production (requires "
        "state=done; e.g. --deployed \"shipped in v1.4.0\")",
    )
    chp.add_argument(
        "--force",
        action="store_true",
        help="override the legal-transition check (e.g. re-close a cancelled ticket)",
    )
    _add_skip_flags(chp)

    # attach — dedicated screenshot-proof verb (task-cli#167). Same durable path as
    # `change --screenshot`, without the field-edit flags agents were missing.
    atp = sub.add_parser(
        "attach",
        parents=[common],
        help="attach screenshot(s) to a ticket as implementation proof",
    )
    atp.add_argument("id", help="ticket id (#123 or HYP-456)")
    atp.add_argument(
        "path",
        nargs="+",
        metavar="PATH",
        help="screenshot path or URL (one or more)",
    )

    # comment — the supported way to add a note to any backend's ticket (UB-25: the normal path
    # that replaces raw forge-CLI/API comment writes)
    cmp = sub.add_parser(
        "comment", parents=[common], help="add a comment to a ticket (Markdown kept as-is)"
    )
    cmp.add_argument("id", help="ticket id (#123, owner/repo#123 or HYP-456)")
    cmp.add_argument("text", nargs="*", help="comment text (words are joined with spaces)")
    cmp.add_argument(
        "--body-file", help="read the comment body from a file ('-' reads stdin)"
    )

    # status
    stp = sub.add_parser(
        "status", parents=[common], help="read or transition a ticket's state"
    )
    stp.add_argument("id", help="ticket id")
    stp.add_argument(
        "new_state",
        nargs="?",
        help="new state (todo|in-progress|in-review|done|cancelled)",
    )
    stp.add_argument(
        "--force",
        action="store_true",
        help="override the legal-transition check (e.g. reopen a cancelled ticket)",
    )
    _add_skip_flags(stp)

    # done — close a ticket by id (the on-done gates run; the close-verb the CTO reaches for)
    dnp = sub.add_parser(
        "done", parents=[common], help="close a ticket (runs the on-done gates)"
    )
    dnp.add_argument("id", help="ticket id (#123 or HYP-456)")
    dnp.add_argument(
        "--screenshot",
        action="append",
        default=[],
        metavar="PATH",
        help="add implementation screenshot",
    )
    dnp.add_argument(
        "--force",
        action="store_true",
        help="override the legal-transition check (re-close a cancelled/done ticket)",
    )
    _add_skip_flags(dnp)

    # check — tick an acceptance criterion off, with the visual proof the box demands
    ckp = sub.add_parser(
        "check",
        parents=[common],
        help="check an acceptance criterion (requires a visual proof)",
    )
    ckp.add_argument("id", help="ticket id (#123 or HYP-456)")
    ckp.add_argument(
        "selector",
        nargs="*",
        help="which criteria: 1-based indexes, or a text substring (one or several)",
    )
    ckp.add_argument(
        "--all",
        action="store_true",
        dest="all_criteria",
        help="select every criterion (one --proof per criterion in order, or one shared)",
    )
    ckp.add_argument(
        "--proof",
        action="append",
        default=[],
        metavar="PATH",
        help="visual proof (screenshot/image); one per selected criterion in order, "
        "or a single shared one (repeatable)",
    )
    ckp.add_argument(
        "--screenshot",
        action="append",
        default=[],
        dest="proof",
        help="alias of --proof",
    )
    ckp.add_argument(
        "--force",
        dest="force_reason",
        metavar="REASON",
        help="check WITHOUT a visual proof, recording why one is impossible/impractical",
    )

    # accept — walk the remaining criteria interactively, asking for a proof each
    acp = sub.add_parser(
        "accept",
        parents=[common],
        help="walk the unaccepted criteria one by one, asking for a proof path (TTY only)",
    )
    acp.add_argument("id", help="ticket id (#123 or HYP-456)")

    # link — a typed relationship between two tickets (task-cli#154, deploy-followup-links)
    lkp = sub.add_parser(
        "link",
        parents=[common],
        # `'|'.join(RELATIONS)` (not a hand-kept string): RELATIONS is derived from _LABELS
        # (links.py), so a future relation addition can't leave this help text stale again.
        help=f"record a typed relationship between two tickets ({'|'.join(RELATIONS)})",
    )
    lkp.add_argument("id", help="ticket id (#123 or HYP-456)")
    lkp.add_argument("relation", choices=list(RELATIONS), help="relation type")
    lkp.add_argument("other_id", metavar="other-id", help="the other ticket id")

    # module — the repo's product-module taxonomy: list/add/assign (task-cli#218)
    mdp = sub.add_parser(
        "module",
        parents=[common],
        help="manage the repo's product-module taxonomy (list|add|assign)",
    )
    mdp.add_argument("action", choices=["list", "add", "assign"], help="list | add | assign")
    mdp.add_argument(
        "arg1",
        nargs="?",
        metavar="NAME-OR-ID",
        help="add: module name | assign: ticket id (#123 or HYP-456)",
    )
    mdp.add_argument(
        "arg2", nargs="?", metavar="MODULE-NAME", help="assign: module name"
    )
    mdp.add_argument(
        "--description", metavar="TEXT", help="module description (add only)"
    )

    # gate — the read-only ship-time verdict (agent-tools ci/ship/ship.sh calls this pre-merge)
    gtp = sub.add_parser(
        "gate",
        parents=[common],
        help="exit 0 only when every criterion is checked WITH a proof (the pre-merge gate)",
    )
    gtp.add_argument("id", help="ticket id (#123 or HYP-456)")

    # mark-shipped — the `gh ship` post-merge hook entry (agent-tools ci/ship/ship.sh)
    msp = sub.add_parser(
        "mark-shipped",
        parents=[common],
        help="record a merged PR against a ticket; NEVER closes it (prints acceptance instructions)",
    )
    msp.add_argument("id", help="ticket id (#123 or HYP-456)")
    msp.add_argument("--pr", required=True, metavar="URL", help="the merged PR's URL")
    msp.add_argument(
        "--commit",
        metavar="SHA",
        help="merge commit SHA (used to derive a commit link when the PR URL is GitHub's)",
    )

    # audit — reconciliation backstop: catches a Done ticket that never passed the close gates
    # (e.g. closed by Linear's/GitHub's own PR-merge auto-close, entirely outside task-cli)
    adp = sub.add_parser(
        "audit",
        parents=[common],
        help="find Done tickets that never passed the close gates (e.g. closed by an external PR-merge auto-close)",
    )
    adp.add_argument(
        "id", nargs="?", help="audit only this ticket id (default: recent Done tickets)"
    )
    adp.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="how many Done tickets to scan when no id is given (default: 50)",
    )
    adp.add_argument(
        "--revert",
        action="store_true",
        help="move a noncompliant ticket back to in-review with an explanatory comment",
    )

    # classify
    clp = sub.add_parser(
        "classify",
        parents=[common],
        help="classify a message change|actionable|justAsk (the tg hook entry)",
    )
    clp.add_argument("text", nargs="?", help="the message text (omit with --batch)")
    clp.add_argument(
        "--create",
        action="store_true",
        help="on a `change` verdict, create/dedup a ticket",
    )
    clp.add_argument(
        "--update", metavar="ID", help="on a `change` verdict, append to this ticket"
    )
    clp.add_argument(
        "--batch",
        action="store_true",
        help=(
            'classify many messages: stdin JSONL {"text": ..., "id"?: ...} -> stdout JSONL '
            "(one verdict object per line, id echoed); classification only"
        ),
    )
    clp.add_argument(
        "--verbose",
        action="store_true",
        help="show the local model's load progress and warnings on stderr",
    )

    # session
    sep = sub.add_parser(
        "session",
        parents=[common],
        help="show/bind the current session and its tickets",
    )
    sep.add_argument(
        "action",
        nargs="?",
        choices=["show", "bind"],
        default="show",
        help="show (default) | bind",
    )
    sep.add_argument(
        "bind_id", nargs="?", metavar="ID", help="ticket id to bind (with `bind`)"
    )

    sub.add_parser("install-skill", help="register the task agent skill with harnesses")

    # daemon — the due-date reminder watcher (start/stop/status/run lifecycle)
    dp = sub.add_parser(
        "daemon",
        parents=[common],
        help="due-date reminder daemon (start|stop|status|run)",
    )
    dp.add_argument(
        "action",
        choices=["start", "stop", "status", "run"],
        help="start (spawn detached) | stop | status | run (foreground loop)",
    )

    # web — local HTML burn chart (start/stop/status/run lifecycle)
    wp = sub.add_parser(
        "web",
        parents=[common],
        help="local HTML burn chart (start|stop|status|run)",
    )
    wp.add_argument(
        "action",
        choices=["start", "stop", "status", "run"],
        help="start (spawn detached) | stop | status | run (foreground HTTP server)",
    )
    wp.add_argument(
        "--port",
        type=int,
        default=8765,
        help="listen port (default 8765)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "create": cmd_create,
        "new": cmd_create,  # `new` is an alias of `create` (CTO-requested verb)
        "list": cmd_list,
        "gantt": cmd_gantt,
        "burn": cmd_burn,
        "web": cmd_web,
        "read": cmd_read,
        "view": cmd_read,
        "find": cmd_find,
        "change": cmd_change,
        "attach": cmd_attach,
        "comment": cmd_comment,
        "status": cmd_status,
        "done": cmd_done,
        "check": cmd_check,
        "accept": cmd_accept,
        "link": cmd_link,
        "module": cmd_module,
        "gate": cmd_gate,
        "audit": cmd_audit,
        "mark-shipped": cmd_mark_shipped,
        "classify": cmd_classify,
        "session": cmd_session,
        "install-skill": cmd_install_skill,
        "daemon": cmd_daemon,
    }
    try:
        _refuse_explicit_non_repo_cwd(args)
        return handlers[args.command](args)
    except TransitionError as exc:
        # an illegal state transition (issue #10): clean error, no traceback. It returns the
        # structured exit code the TransitionError pins — the agenttools_errors USAGE class (2),
        # the same "the request is invalid" class _UserError already uses, NOT a transition-only
        # code (a script sees "usage error", not "illegal transition specifically").
        print(_err(f"error: {exc}"))
        return exc.exit_code
    except _UserError as exc:
        print(_err(f"error: {exc}"))
        return 2


class _UserError(Exception):
    """A user-facing error → printed as ``error: ...`` and exit 2 (no traceback)."""


class _ClassifyBatchAmbiguous(Exception):
    """Internal-only signal (never escapes `_classify_multi`): a multi-ask batch item hit an
    AMBIGUOUS backend write (review finding, "High" — the connection may have dropped AFTER the
    backend already accepted the request) — either a fresh CREATE (`_create_ticket_or_ambiguous`
    returns `(None, exit_code)`, no confirmed ticket id) or a dedup-fold-in COMMENT (the restated
    text may have already posted to the matched ticket). Either way there is nothing safe to
    record into `batch_resolved_ids`, which means a LATER batch item's dedup check would see the
    possibly-affected ticket as an ORDINARY (non-excluded) candidate and could wrongly fold a
    genuinely distinct ask into it. The only safe response is to stop the whole batch here —
    raised instead of returned so `_classify_multi` can tell "this item finished with some exit
    code" apart from "stop processing the remaining items now."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


# ── shared plumbing ─────────────────────────────────────────────────────────────────


# `owner/repo#N` — a repository-qualified GitHub/Forgejo issue id (numbers are per repository).
_QUALIFIED_ID_RE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[0-9]+")
_FORGE_BACKENDS = ("github-issues", "forgejo")


def _refuse_explicit_non_repo_cwd(args: argparse.Namespace) -> None:
    """UB-25 contract: EVERY explicit ``-C``/``--cwd`` — for reads and writes alike — must name
    a path inside a git worktree, checked before any config, credential or provider I/O.

    An explicit target says "operate on THIS repository"; a non-repository path would silently
    fall back to a global/registry route that may be a different project. The unscoped
    registry/aggregation views (``list``/``find``/``read`` outside a repo) stay available, but
    only from a non-repository shell cwd with NO ``-C``.
    """
    explicit = getattr(args, "cwd", None)
    if explicit is None:
        return
    target = Path(explicit).expanduser()
    if target.is_dir() and _in_git_repo(target.resolve()):
        return
    raise _UserError(
        f"-C {explicit} is not inside a git repository.\n"
        "  why: an explicit -C/--cwd selects the repository this command operates on; a "
        "non-repository path would fall back to a global or registry route that may be a "
        "different project.\n"
        "  fix: pass -C with a path inside the target repository, or run the command from "
        "inside it (no flag needed). For the cross-project registry view, run it from outside "
        "any repository without -C."
    )


def _load(args: argparse.Namespace):
    """Load config for the -C repo, applying --backend/--repo/--config overrides."""
    from .config import ConfigError, load

    repo_root = Path(args.cwd or ".").resolve()
    explicit = None
    if getattr(args, "config", None):
        cp = Path(args.config)
        explicit = cp if cp.is_absolute() else repo_root / cp
    try:
        cfg = load(repo_root, explicit_config=explicit)
    except ConfigError as exc:
        raise _UserError(str(exc)) from exc

    # Zero-config Forgejo routing: the built-in default is github-issues, but a
    # repository whose origin lives on a host already trusted by fj is
    # unambiguously Forgejo for repo:auto. Any explicitly configured backend (a
    # `backend:` in any config layer, even `github-issues`), explicit CLI
    # routing/pins AND an explicitly preconfigured dormant forgejo url/repo win.
    forgejo_cfg = cfg.section("forgejo")
    if (
        not getattr(args, "backend", None)
        and not getattr(args, "repo", None)
        and not cfg.explicit_backend
        and cfg.backend == "github-issues"
        and str(cfg.section("github").get("repo", "auto")).strip() == "auto"
        and not str(forgejo_cfg.get("url", "")).strip()
        and str(forgejo_cfg.get("repo", "auto")).strip() == "auto"
    ):
        from .backends.forgejo import autodetect_forgejo_origin

        origin = autodetect_forgejo_origin(repo_root)
        if origin:
            cfg.data["backend"] = "forgejo"
            cfg.data.setdefault("forgejo", {})["url"] = origin
            cfg.data["forgejo"]["repo"] = "auto"
            cfg.layers.append(f"autodetect:forgejo:{origin}")
            try:
                from .config import validate

                validate(cfg.data)
            except ConfigError as exc:
                raise _UserError(str(exc)) from exc

    if getattr(args, "backend", None):
        cfg.data["backend"] = args.backend
        try:
            from .config import validate

            validate(cfg.data)
        except ConfigError as exc:
            raise _UserError(str(exc)) from exc
    if getattr(args, "repo", None):
        section = "forgejo" if cfg.backend == "forgejo" else "github"
        cfg.data.setdefault(section, {})["repo"] = args.repo
    return cfg


def _backend(cfg):
    from .backends import BackendError, get_backend
    from .credentials import CredentialError

    try:
        return get_backend(cfg)
    except (BackendError, CredentialError) as exc:
        raise _UserError(str(exc)) from exc


# ── repo presence + project resolution (the outside-a-repo / cross-repo machinery) ──


def _in_git_repo(repo_root) -> bool:
    """``True`` if ``repo_root`` is inside a git work tree (cheap, no network)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _current_project_overlay(cfg) -> tuple[str, dict[str, Any]] | None:
    """The single project to scope the cwd to (name + config overlay), or ``None``.

    This is the "do I have ONE concrete target?" predicate that routes every command between
    the single-project path and the cross-project/outside-a-repo path. It returns a target when
    the coordinate is pinned — an explicit ``github.repo``/``linear.team`` in config (works even
    without git) — or when ``github.repo: auto`` resolves a git ``origin`` (inside a repo).
    ``None`` means no single target (genuinely outside a git work tree), which sends
    ``list``/``find`` to the grouped registry view and makes ``create`` emit its 3-part error.
    The overlay is what :func:`tasklib.backends.get_backend` resolves unchanged.

    Crucially, ``None`` is returned ONLY for the real outside-a-repo case (no git work tree). A
    repo with a broken/unsupported ``origin`` is NOT silently demoted to "outside a repo" — the
    backend's resolution error surfaces normally, rather than being masked as "no projects".
    """
    backend = cfg.backend
    if backend == "linear":
        team = str(cfg.section("linear").get("team", "")).strip()
        if not team:
            # A teamless `backend: linear` INSIDE a repo is a real, actionable misconfig — surface
            # the backend's "requires a team key" error (mirrors the github `repo: auto` branch
            # below) instead of masking it as "outside a repo". Only genuinely outside a work tree
            # do we return None to route to the grouped/registry view.
            if not _in_git_repo(cfg.repo_root):
                return None
            _backend(
                cfg
            )  # NoReturn here: raises _UserError("linear backend requires a team key")
            raise AssertionError(
                "unreachable: _backend raises on a teamless linear config"
            )
        project = str(cfg.section("linear").get("project", "")).strip()
        name = f"{team}/{project}" if project else team
        return name, {"backend": "linear", "linear": {"team": team, "project": project}}

    if backend == "forgejo":
        return _current_forgejo_overlay(cfg)

    if backend == "tasks":
        sec = cfg.section("tasks")
        url = str(sec.get("url", "")).strip()
        team = str(sec.get("team", "")).strip()
        if not url or not team:
            # Same reasoning as the teamless-linear branch above: a misconfigured `backend:
            # tasks` INSIDE a repo is a real, actionable misconfig, not "outside a repo".
            if not _in_git_repo(cfg.repo_root):
                return None
            _backend(
                cfg
            )  # NoReturn here: raises _UserError("tasks backend requires a url"/"...team key")
            raise AssertionError(
                "unreachable: _backend raises on a misconfigured tasks config"
            )
        name = f"{team}@{url}"
        return name, {"backend": "tasks", "tasks": {"url": url, "team": team}}

    # github-issues
    gh = cfg.section("github")
    repo_spec = str(gh.get("repo", "auto")).strip()
    if repo_spec and repo_spec != "auto" and "/" in repo_spec:
        return repo_spec, {"backend": "github-issues", "github": {"repo": repo_spec}}
    # repo: auto → no git work tree is the genuine "outside a repo" signal. INSIDE a work tree
    # we resolve the origin and let any resolution error surface (a broken remote is not the
    # same as being outside a repo — don't mask it as "no projects").
    if not _in_git_repo(cfg.repo_root):
        return None
    from .backends import BackendError
    from .backends.github_issues import _resolve_repo

    try:
        owner, repo = _resolve_repo("auto", cfg.repo_root)
    except BackendError as exc:
        # inside a repo, but the origin is broken/unsupported → a real, user-facing error
        # (not "outside a repo"). Surface it as a clean _UserError, never a traceback.
        raise _UserError(_with_forgejo_hint(str(exc))) from exc
    full = f"{owner}/{repo}"
    return full, {"backend": "github-issues", "github": {"repo": full}}


def _with_forgejo_hint(message: str) -> str:
    """UB-25: a repo still routed to the github default (or a stale `backend: github-issues`
    override) whose origin is a non-GitHub forge gets a pointer at the Forgejo backend instead
    of a bare "unrecognized github remote URL"."""
    if not message.startswith("unrecognized github remote URL"):
        return message
    return (
        f"{message}\n"
        "  why: this repository is routed to the github-issues backend, but its origin is not "
        "on github.com.\n"
        "  fix: if it lives on a Forgejo forge, route it there in the repository's rig.yaml: "
        "`task: {backend: forgejo, forgejo: {url: https://<forge-host>, repo: owner/name}}`."
    )


def _current_forgejo_overlay(cfg) -> tuple[str, dict[str, Any]] | None:
    """The forgejo branch of :func:`_current_project_overlay`: an explicit ``forgejo.repo``
    pins the target; ``auto`` resolves the git ``origin`` (only on ``forgejo.url``'s host), and
    returns ``None`` only when genuinely outside a git work tree."""
    from .backends import BackendError
    from .backends.forgejo import forgejo_origin, resolve_repo

    sec = cfg.section("forgejo")
    spec = str(sec.get("repo", "auto") or "auto").strip()
    if spec == "auto" and not _in_git_repo(cfg.repo_root):
        return None
    try:
        origin = forgejo_origin(str(sec.get("url", "") or ""))
        owner, repo = resolve_repo(spec, cfg.repo_root, origin)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    full = f"{owner}/{repo}"
    return full, {"backend": "forgejo", "forgejo": {"url": origin, "repo": full}}


def _config_for_overlay(cfg, overlay: dict):
    """Clone ``cfg`` with ``overlay`` deep-merged over its data (so a project resolves).

    Returns a fresh ``LoadedConfig`` — the base ``cfg`` is never mutated, so aggregating
    across projects can't leak one project's coordinates into the next.
    """
    return cfg.with_overlay(overlay)


def _known_projects(cfg) -> tuple[list, str | None]:
    """Return ``(projects, current_coordinate)`` — the groups + which one is the cwd's repo.

    The registry projects, plus (when inside a repo) the synthetic current-repo project if its
    coordinate isn't already registered. ``current_coordinate`` is the cwd repo's coordinate (or
    ``None`` outside a repo) — currentness is tracked by COORDINATE, kept separate from registry
    explicitness, so the cwd repo is flagged ``(current)`` even when it IS in the registry.
    """
    from .projects import current_repo_project, projects_from_config

    projects = projects_from_config(cfg.data)
    current = _current_project_overlay(cfg)
    current_coordinate: str | None = None
    if current is not None:
        name, overlay = current
        cur_backend = str(overlay.get("backend", cfg.backend))
        cur = current_repo_project(name, cur_backend, overlay)
        current_coordinate = cur.coordinate
        # match by COORDINATE (repo/team), not display name: a registry entry for the same repo
        # under a different label is still the same project — append the synthetic one only when
        # the coordinate isn't already registered (the registry entry keeps its own label).
        if not any(p.coordinate == cur.coordinate for p in projects):
            projects.append(cur)
    return projects, current_coordinate


def _config_for_id(cfg, ticket_id: str):
    """Resolve the config to use for ``ticket_id`` — the cwd's when inside a repo (or with a
    pinned coordinate / ``--repo``), else the config the registered project's overlay resolves
    to: a Linear id (``HYP-456``) by its team prefix; a GitHub id (``#123``) when exactly one
    GitHub project is registered. An ambiguous or unroutable id fails with a 3-part error
    rather than a cryptic backend failure.

    LIMITATION: only ``backend``/``github``/``linear`` are ever re-pointed by a registry
    entry's overlay — ``enforce:`` always resolves to the CALLER's own config, whether pinned
    or registry-routed, never the ticket's owning project's committed `task.yaml` (cross-project
    `enforce:` override tracked in task-cli#117, an open design question, not an obvious bug)."""
    if _current_project_overlay(cfg) is not None:
        return cfg

    projects, _ = _known_projects(cfg)
    candidates = [p for p in projects if p.explicit]
    chosen = _route_id_to_project(ticket_id, candidates)
    if chosen is None:
        raise _UserError(_unroutable_id_error(ticket_id, candidates))
    return _config_for_overlay(cfg, chosen.overlay)


def _backend_for_id(cfg, ticket_id: str):
    """The backend that owns ``ticket_id`` — see :func:`_config_for_id`."""
    return _backend(_config_for_id(cfg, ticket_id))


def _routing_for_id(cfg, ticket_id: str):
    """Resolve BOTH the backend and the owning project's config for ``ticket_id`` in ONE
    routing pass — shared by every command that needs both (`cmd_gate`, `cmd_accept`,
    `cmd_mark_shipped`), so a cross-project ticket's policy is always read from the SAME
    config the backend was built from, and routing is never derived twice per invocation
    (review finding, task-cli PR#118 rounds 3-4: each of these three commands had
    independently reinvented this pairing, one of them re-resolving routing a second time)."""
    pcfg = _config_for_id(cfg, ticket_id)
    return _backend(pcfg), pcfg


def _route_id_to_project(ticket_id: str, projects: list):
    """Pick the registered project that owns ``ticket_id``, or ``None`` if ambiguous/none."""
    tid = ticket_id.strip()
    qualified = _QUALIFIED_ID_RE.fullmatch(tid)
    if qualified is not None:
        # owner/repo#N names its repository: route to the registered forge project with that
        # repo (never to a same-numbered issue elsewhere).
        named = qualified.group(1).lower()
        hits = [p for p in projects if p.backend in _FORGE_BACKENDS and _repo_of(p) == named]
        return hits[0] if len(hits) == 1 else None
    if tid.startswith("#") or tid.lstrip("#").isdigit():
        # A bare number is only meaningful inside ONE repository: with more than one registered
        # GitHub/Forgejo repository it is ambiguous, whichever backends they use.
        forge = [p for p in projects if p.backend in _FORGE_BACKENDS]
        return forge[0] if len(forge) == 1 else None
    # TEAM-123-shaped id: Linear AND Tasks both use this identifier shape and are both
    # team-scoped, so route by team prefix across BOTH backends. Within ONE tracker, get/update
    # resolve by the identifier alone (ignoring the project), so two same-team projects of that
    # tracker resolve the same issue — pick the first deterministically. Across trackers (Linear
    # vs Tasks, or two Tasks deployments) the same `HYP-3` names DIFFERENT tickets, so that is
    # ambiguous and refused, never guessed (UB-25 review).
    prefix = tid.partition("-")[0].upper() if "-" in tid else ""
    if prefix:
        matches = [
            p
            for p in projects
            if (p.backend == "linear" and _linear_team_of(p) == prefix)
            or (p.backend == "tasks" and _tasks_team_of(p) == prefix)
        ]
        trackers = {_tracker_of(p) for p in matches}
        if len(trackers) == 1:
            return matches[0]
    return None


def _tracker_of(project) -> str:
    """The ticket store a team-scoped project reads from: one Linear workspace, or one Tasks
    deployment (by url)."""
    if project.backend == "tasks":
        return "tasks:" + str(project.overlay.get("tasks", {}).get("url", "")).strip().rstrip("/").lower()
    return project.backend


def _repo_of(project) -> str:
    section = "forgejo" if project.backend == "forgejo" else "github"
    return str(project.overlay.get(section, {}).get("repo", "")).strip().lower()


def _linear_team_of(project) -> str:
    return str(project.overlay.get("linear", {}).get("team", "")).strip().upper()


def _tasks_team_of(project) -> str:
    return str(project.overlay.get("tasks", {}).get("team", "")).strip().upper()


def _unroutable_id_error(ticket_id: str, projects: list) -> str:
    names = ", ".join(p.name for p in projects) or "(none registered)"
    return (
        f"cannot resolve which project ticket {ticket_id!r} belongs to (you are outside a git repo).\n"
        f"  why: the id maps to no single known project. registered projects: {names}.\n"
        "  fix: run inside the repo, or pass --repo owner/name (GitHub), "
        "or register the project under `projects:` in ~/.config/task-cli/config.yaml."
    )


def _create_without_repo_error(cfg) -> str:
    """The honest 3-part (WHAT/WHY/HOW) error for `task create` run outside a repo.

    The HOW must not lie: it points at the real escape hatches that exist — ``--repo`` (which
    pins the GitHub coordinate), a Linear team in config, or simply running inside the repo.
    """
    backend = cfg.backend
    if backend == "linear":
        how = (
            "  fix: run inside the repo whose `task.yaml` sets `linear.team`, "
            "or set `linear: {team: KEY}` in ~/.config/task-cli/config.yaml."
        )
    elif backend == "tasks":
        how = (
            "  fix: run inside the repo whose `task.yaml` sets `tasks.url`/`tasks.team`, "
            "or set `tasks: {url: URL, team: KEY}` in ~/.config/task-cli/config.yaml."
        )
    elif backend == "forgejo":
        how = (
            "  fix: run inside the target Forgejo repository's checkout (its origin must be on "
            "`forgejo.url`'s host), or pin `forgejo.repo: owner/name`."
        )
    else:
        how = (
            "  fix: run inside the target git repo, or pass `--repo owner/name` "
            "to pin the GitHub project explicitly."
        )
    return (
        "cannot create a ticket: no project context (you are outside a git repo).\n"
        "  why: `create` is repo-bound — it writes the ticket into ONE specific project, "
        "so it must know which backend/repo to target; `github.repo: auto` needs a git origin.\n"
        f"{how}"
    )


def _enforce_config(cfg):
    from .policy import EnforceConfig

    return EnforceConfig.from_dict(cfg.enforce)


def _detect_session(cfg):
    from .session import detect

    # root git-branch detection at the same repo the backend resolves (cfg.repo_root), not the
    # shell's cwd, so `task -C /other/repo` is consistent between session scope and backend.
    return detect(
        detect_order=cfg.session_detect,
        cwd=str(cfg.repo_root),
        label_prefix=cfg.session_label_prefix,
    )


def _parse_state(value: str):
    """Parse a state string, turning the ValueError into a clean ``_UserError`` (exit 2)."""
    try:
        return State.parse(value)
    except ValueError as exc:
        raise _UserError(str(exc)) from exc


def _normalize_due(value: str | None) -> str | None:
    """Validate a ``--due`` value to a canonical ``YYYY-MM-DD`` string (``None`` = not passed).

    An empty string is a deliberate "clear the due date" signal (returns ``""``). Any other
    value must be an ISO date; a malformed one is a clean ``_UserError`` (exit 2), never stored
    as-is — a daemon that watches due dates must not be fed un-parseable junk at the front door.
    """
    if value is None:
        return None
    text = value.strip()
    if text == "":
        return ""
    from datetime import date

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise _UserError(
            f"--due must be an ISO date YYYY-MM-DD (got {value!r})"
        ) from exc


def _collect_skips(args: argparse.Namespace) -> dict[str, str]:
    """Gather the recorded escape-hatch justifications from --skip-<gate> flags."""
    skips: dict[str, str] = {}
    for _flag, gate in _SKIP_FLAGS:
        dest = "skip_" + gate.replace("-", "_")
        reason = getattr(args, dest, None)
        if reason:
            skips[gate] = reason
    return skips


def _ticket_line(t: Ticket) -> str:
    first_para = (t.what or t.raw_body or "").strip().split("\n", 1)[0][:80]
    state = _dim(f"[{t.state.value}]")
    sep = f" — {first_para}" if first_para else ""
    return f"{_bold(t.id or '(new)')} {state} {t.title}{sep}"


def _format_tickets(tickets: list[Ticket]) -> str:
    """The flat (non-grouped) human view as a single string — fed to the pager by the caller."""
    if not tickets:
        return _dim("(no tickets)")
    return "\n".join(_ticket_line(t) for t in tickets)


def _print_tickets_json(tickets: list[Ticket]) -> None:
    """The flat machine-readable view — straight to stdout, never paged (must stay parseable)."""
    import json

    print(json.dumps([_ticket_dict(t) for t in tickets], ensure_ascii=False, indent=2))


def _ticket_dict(t: Ticket) -> dict:
    d = {
        "id": t.id,
        "title": t.title,
        "state": t.state.value,
        "url": t.url,
        "labels": t.labels,
        "what": t.what,
        "due": t.due,
        # `links` (raw key -> URL dict, unchanged shape) and the deploy fields are the JSON
        # surface harness-cli's `stats` command reads via `task read <id> --json`
        # (deploy-followup-links) — grep the keys for the `"Follow-up of #<id>"` /
        # `"Followed up by #<id>"` prefixes `linked_other_ids()` (tasklib/links.py) already
        # parses internally, rather than adding a second, parsed "followups" shape here.
        "links": dict(t.links),
        "deployed": t.deployed,
        "deployed_at": t.deployed_at,
        "deployed_note": t.deployed_note,
    }
    # Only emitted when the backend actually returned any (Linear; GitHub never populates this)
    # — keeps the JSON shape unchanged for every existing consumer that doesn't care about it.
    if t.attachments:
        d["attachments"] = [
            {"id": a.id, "title": a.title, "url": a.url, "subtitle": a.subtitle}
            for a in t.attachments
        ]
        # review finding: human output already warns about the 250-item cap — a JSON consumer
        # (`--json`) must get the SAME signal, not silently treat a capped list as complete.
        if t.attachments_truncated:
            d["attachments_truncated"] = True
    return d


# ── policy enforcement helper (shared by create + change/close) ─────────────────────


def _msgref_resolvable_ids(ticket, cfg_e) -> set[int] | None:
    """The ``tg#<id>`` ids resolvable in local tg-cli history — loaded ONCE, and ONLY when it can
    actually matter: the ``msgref-quote`` gate is enabled AND the ticket's prose carries an
    UNQUOTED reference. A gate disabled via ``enforce.msgref_quote: false``, or a fully-expanded /
    reference-free ticket, never touches the (potentially large, private) history log (review
    finding). Feeds the gate's deny-vs-warn split (:func:`tasklib.policy.check`); returns ``None``
    when there is nothing to assess (the gate then no-ops), else the resolvable-id set. Uses the
    gate's own :func:`tasklib.policy.unquoted_body_ids` so the "should I load history?" question
    can never diverge from "what does the gate look at?". ``load_history`` is contractually
    best-effort (it swallows a missing dir / absent tg-cli / corrupt line and returns ``{}``), so
    this advisory path never breaks a create/close even where tg-cli was never installed."""
    from .policy import GATE_MSGREF_QUOTE, unquoted_body_ids

    # Skip the (potentially large, private) history read entirely when it can't change the
    # outcome: the gate is disabled, already waived on this ticket, or there is nothing unquoted.
    if not cfg_e.msgref_quote or GATE_MSGREF_QUOTE in ticket.skips:
        return None
    if not unquoted_body_ids(ticket):
        return None
    from .msgrefs import load_history

    return set(load_history())


def _read_before_edit_violation(
    ticket_id: str, session_id: str, repo: str, cfg_e
) -> Violation | None:
    """Build the read-before-edit ``Violation`` for ``cmd_change --what``, or ``None`` when the
    edit may proceed (the gate is disabled, or a fresh read is on record).

    The ONLY I/O this gate needs (``read_tracker.is_fresh``) lives here, in the impure `cli.py`
    layer — `_enforce_edit_or_die` stays I/O-free and just renders whatever `Violation` this
    returns (see its docstring for the waiver-scoping rationale). Distinguishes
    ``is_fresh``'s three outcomes (see that function's docstring): ``True`` → no violation;
    ``False`` → the ordinary "go read it first" refusal; ``None`` → the marker STORE itself is
    unreadable, a different and more actionable message (retrying `task read` won't fix a
    broken store) — both ``False`` and ``None`` refuse the edit (fail closed either way), only
    the message differs.
    """
    if not cfg_e.read_before_edit:
        return None
    from .policy import GATE_READ_BEFORE_EDIT, Violation
    from .read_tracker import is_fresh

    fresh = is_fresh(session_id, ticket_id, repo=repo)
    if fresh is True:
        return None
    if fresh is None:
        return Violation(
            GATE_READ_BEFORE_EDIT,
            f"cannot confirm {ticket_id} was read recently — the read-marker store could not "
            "be read",
            # `_report_and_die` already auto-appends the "(or --skip-{gate} ...)" suggestion for
            # every non-hard gate (review finding, task-cli#119: an earlier version repeated it
            # here too, printing the skip flag twice in the rendered refusal) — this hint
            # describes only the FIX action, matching every other gate's hint convention.
            hint="check the marker store's permissions, or verify the current content manually",
        )
    return Violation(
        GATE_READ_BEFORE_EDIT,
        f"--what would overwrite {ticket_id}'s description, but this session has not read the "
        "current ticket recently",
        hint=f"run `task read {ticket_id}` first",
    )


def _enforce_or_die(ticket: Ticket, cfg, phase) -> None:
    from .policy import Phase, check

    cfg_e = _enforce_config(cfg)
    result = check(
        ticket, cfg_e, phase, resolvable_ids=_msgref_resolvable_ids(ticket, cfg_e)
    )
    _report_and_die(result, "create" if phase is Phase.CREATE else "close")


def _report_and_die(result, label: str) -> None:
    """Print the skipped/violation summary for a PolicyResult and refuse if any gate is unmet.

    Stream contract: on the SUCCESS path (no violation) the advisory warnings (``result.warnings``)
    and the "skipped gates (justified)" summary are diagnostics, not data — they go to STDERR so a
    machine-readable ``--json`` payload the caller then prints to stdout is never corrupted (review
    finding). The FATAL refusal report below prints to stdout and then raises ``_UserError`` — that
    is pre-existing behavior shared with every other command's refusal (and asserted by existing
    tests); a failing command exits non-zero without printing ``--json``, so it does not corrupt a
    success-path parse. Routing the refusal report to stderr too is a worthwhile follow-up but a
    cross-cutting change beyond this gate's scope."""
    import sys

    for w in result.warnings:
        print(_warn(f"  ⚠ {w.gate}: {w.message}"), file=sys.stderr)
    if result.skipped:
        print(
            _warn(f"  skipped gates (justified): {', '.join(result.skipped)}"),
            file=sys.stderr,
        )
    if not result.ok:
        from .policy import NON_SKIPPABLE_GATES

        print(_err(f"refusing to {label}: {len(result.violations)} gate(s) unmet"))
        for v in result.violations:
            print(_err(f"  ✗ {v.gate}: {v.message}"))
            if v.hint:
                # A NON_SKIPPABLE gate (e.g. acceptance-checked, msgref-title) has no
                # --skip-<gate> flag at all -- suggesting one is actively misleading (task-cli#45
                # review finding: the new msgref-title gate inherited this pre-existing wart from
                # acceptance-checked). Only append the skip hint for a gate that actually has one.
                skip_hint = (
                    ""
                    if v.gate in NON_SKIPPABLE_GATES
                    else f'  (or --skip-{v.gate} "<reason>")'
                )
                print(_dim(f"      → {v.hint}{skip_hint}"))
        raise _UserError("policy gates not satisfied")


def _apply_force(
    ticket: Ticket, cfg, phase, reason: str | None, forceable: set[str]
) -> None:
    """Record ``reason`` as the skip justification for whichever ``forceable`` gates actually fire.

    This is how ``--force "<reason>"`` overrides the new content gates (links / user-impact-
    quality) — and, when ``GATE_TODO_SHAPED`` is in ``forceable``, the non-blocking todo-shaped
    hint too: the reason is written onto the ticket's ``skips``, so it lands — audited — in the
    body's ``Skipped gates`` section, exactly like ``--skip-<gate>``. Only gates that genuinely
    fire are recorded, so a force never pollutes the audit trail with gates that passed.

    Scans BOTH ``result.violations`` and ``result.warnings``: todo-shaped never appears in
    ``violations`` (it never blocks — see ``check()``'s own comment), so a force-set that
    includes it would otherwise silently never fire (review finding while wiring the new gate).
    """
    if not reason:
        return
    from .policy import check

    result = check(ticket, _enforce_config(cfg), phase)
    for v in (*result.violations, *result.warnings):
        if v.gate in forceable:
            ticket.skips.setdefault(v.gate, reason)


def _session_dedup_candidates(backend, session) -> list[Ticket]:
    """This session's tickets for a dedup scan, degrading to `[]` on ANY backend hiccup.

    Shared by `_refuse_if_duplicate`, `_enforce_similar_duplicate_or_die`, and `_classify_create`
    so every dedup mechanism fails open the same way — AND shares the same bounded coverage
    (review finding, P2, explicitly called out as part of the contract rather than left silent):
    `limit=30` (the `TicketBackend` protocol's own default) means only the 30 most recent
    session tickets are ever considered. A duplicate of a ticket OUTSIDE that window is never
    caught by any duplicate-detection mechanism — no warning, no recorded waiver, just a
    silently-created second ticket. Raising the limit or paginating would cost an extra
    API round-trip on every `create`/`classify --create`; for a session-scoped dedup scan (most
    sessions stay well under 30 tickets) that trade wasn't judged worth it here, but a caller
    relying on duplicate detection for a long-lived, high-volume session should know its actual
    bound. Catches BOTH `BackendError` and `AmbiguousBackendError`: this is a read-only GET,
    so an ambiguous outcome here is harmless (nothing was mutated either way) — unlike
    `AmbiguousBackendError` from a WRITE, which must never be silently swallowed. Missing
    `AmbiguousBackendError` here was itself a review-caught bug: `session_tickets()` funnels
    through the same `_call`/`_gql` wrappers as every other request, so a mid-read drop during
    THIS lookup used to crash `create` with a raw traceback before the create was even attempted
    — defeating the very guard meant to protect a retry after a flaky connection.
    """
    from .backends import AmbiguousBackendError, BackendError

    try:
        return backend.session_tickets(session.label, limit=30)
    except (BackendError, AmbiguousBackendError):
        return []


def _refuse_if_duplicate(
    candidates: list[Ticket], force_reason: str | None, ticket: Ticket
) -> None:
    """Block `create` when a close-duplicate ticket already exists in this session.

    Reuses `_best_dedup_match` — the SAME conservative title-similarity check `_classify_create`
    already runs before creating from an inbound message — so `task new`/`create` gets the
    identical guard. Without this, a caller retrying after ANY ambiguous result (e.g.
    `AmbiguousBackendError`) could silently create a genuine duplicate ticket (task-cli review
    finding).

    Takes the already-fetched session-scoped `candidates` (the caller, `cmd_create`, fetches
    them once via `_session_dedup_candidates` and reuses them for the broader token/Jaccard
    gate too — `_enforce_similar_duplicate_or_die` — rather than each gate re-fetching).

    `force_reason` (`--force "<reason>"`) overrides the block; the reason is recorded under
    `policy.GATE_DUPLICATE` in the ticket's `skips` (renders in the `Skipped gates` section) ONLY
    when it actually overrode a detected match — a force that found nothing to override never
    pollutes the audit trail, mirroring `_apply_force`'s same rule for the policy gates.

    `--skip-duplicate "<reason>"` ALSO overrides this hard block, not only the broader
    token/Jaccard gate (`_enforce_similar_duplicate_or_die`) below it — both mechanisms share
    `policy.GATE_DUPLICATE`, so one waiver name must waive both, or the README's documented
    escape hatch would silently not work for the near-exact-title case (review finding).
    """
    from .policy import GATE_DUPLICATE, gate_is_skipped

    match = _best_dedup_match(ticket.title, candidates)
    if match is None:
        return
    if gate_is_skipped(ticket, GATE_DUPLICATE):
        return  # already recorded via --skip-duplicate; nothing further to record here
    if force_reason:
        ticket.skips.setdefault(GATE_DUPLICATE, force_reason)
        return
    # Quote the id (`'#1'`, not bare `#1`): pasted bare into a shell, `#1` is swallowed as a
    # comment (`#` starts a comment wherever a new word may begin, e.g. `bash -c 'echo hi #1'`
    # prints only "hi") — quoting keeps these suggestions actually copy-paste-safe while
    # task-cli still receives the identical plain `#1` argument once the shell strips the quotes.
    raise _UserError(
        f"a close-duplicate ticket already exists: {match.id}  {match.title}\n"
        f"  url: {match.url}\n"
        f"  next steps:\n"
        f"    task read '{match.id}'                    view it\n"
        f"    task change '{match.id}' ...               edit it instead of creating a new one\n"
        f'    task new ... --force "<reason>"          create anyway (records the override)'
    )


def _enforce_similar_duplicate_or_die(
    ticket: Ticket, candidates: list[Ticket], session, force_reason: str | None
) -> None:
    """The token/Jaccard-similarity duplicate gate (`policy.duplicate_violation`) — broader than
    `_refuse_if_duplicate`'s near-exact-title check above, catching a REWORDED duplicate of the
    same underlying request (task-cli ecosystem audit, 2026-09: 13 confirmed duplicates from
    `task classify`'s auto-create path used exactly this shape — same ask, different wording,
    so the title-only SequenceMatcher check never fired).

    Reported through the SAME `PolicyResult`/`_report_and_die` pipeline every other soft gate
    uses, so it prints and refuses like the others, and `--skip-duplicate "<reason>"` (or
    `--force "<reason>"`, mirroring `_apply_force`'s "only record what actually fired" rule)
    silences it.
    """
    from datetime import datetime, timezone

    from .policy import PolicyResult, duplicate_violation, gate_is_skipped

    v = duplicate_violation(
        ticket.title,
        ticket.what,
        candidates,
        new_session=session.label,
        now=datetime.now(timezone.utc),
    )
    if v is None:
        return
    if force_reason:
        ticket.skips.setdefault(v.gate, force_reason)
    result = PolicyResult()
    if gate_is_skipped(ticket, v.gate):
        result.skipped.append(v.gate)
    else:
        result.violations.append(v)
    _report_and_die(result, "create")


def _derive_title_from_message(text: str) -> str:
    """Derive a ticket title from raw inbound text (the ``--from-message`` / ``classify
    --create`` hook path): first line, truncated to 72 chars — but with a leading tg-cli inbound-
    inject wrap (``[TG from <name> tg#<id>]``) stripped FIRST, so the title never inherits the
    wrap's own ``tg#<id>`` and trips the non-skippable ``msgref-title`` gate (task-cli#45 review
    finding: this is the documented hook path tasklib.msgrefs's module docstring names as the
    whole feature's motivating case — it must not be the one path that breaks). Shared by
    :func:`cmd_create` and :func:`_classify_create` so both derive a title the same way."""
    from .msgrefs import strip_inbound_wrap

    unwrapped = strip_inbound_wrap(text.strip())
    first = unwrapped.split("\n", 1)[0]
    return first[:72]


def _expand_msgrefs(text: str, history=None):
    """Expand every ``tg#<id>`` mention in ``text`` into itself plus a quote pulled from
    tg-cli's local message history (task 6109 / HYP-897) — see :mod:`tasklib.msgrefs`. A no-op
    when ``text`` carries no reference (or ``text`` is falsy), so calling this on every prose
    field is always safe. ``history`` lets a caller load the (potentially large, append-only)
    history log ONCE per command via :func:`_load_msgref_history` and reuse it across fields,
    instead of re-reading it from disk for every field that happens to carry a reference."""
    if not text:
        return text
    from .msgrefs import render_msgref_quotes

    return render_msgref_quotes(text, history=history)


def _msgref_expand_enabled(skips, cfg) -> bool:
    """Whether tg#<id> auto-expansion should run for a ticket carrying these skips under ``cfg``.
    OFF when either (a) ``enforce.msgref_quote: false`` — that flag is the single master switch for
    the whole tg#<id> feature, so disabling the gate also stops the quote machinery and its
    private-history read (review finding); or (b) a ``--skip-msgref-quote`` is recorded — the
    author declared the reference a false positive, so it must not be quoted at all. The skip
    governs expansion, not merely the enforcement gate."""
    from .policy import GATE_MSGREF_QUOTE

    if not _enforce_config(cfg).msgref_quote:
        return False
    return GATE_MSGREF_QUOTE not in skips


def _load_msgref_history_if_needed(*texts: str):
    """Load tg-cli's local message history ONCE per command — but only when at least one of
    ``texts`` actually names a ``tg#<id>`` reference. A metadata-only edit, or a ticket that
    never mentions one, never touches disk for this (the log is append-only and can grow large;
    task-cli#45 review finding)."""
    from .msgrefs import detect_msgrefs, load_history

    if not any(detect_msgrefs(t) for t in texts if t):
        return None
    return load_history()


def _enforce_edit_or_die(
    ticket: Ticket,
    cfg,
    *,
    check_links: bool,
    check_impact: bool,
    check_title: bool = False,
    read_violation: Violation | None = None,
    read_waived: bool = False,
) -> None:
    """The edit-time gates (rule 1 links, rule 5 impact-quality when impact was touched, and
    the read-before-edit gate when ``--what`` was touched).

    A plain ``change`` (not a close) doesn't re-run the full create gates — an edit may be a
    partial step. The links rule runs only when the edit TOUCHED a scanned text field, so a
    metadata-only edit (``--due``/``--label``) on a ticket whose body (e.g. one created in the
    GitHub/Linear web UI) already carries a bare reference is never blocked. The impact-quality
    rule runs only when the edit sets/changes the impact. ``check_title`` runs the (non-skippable)
    ``msgref-title`` gate only when this edit is SETTING the title (``--title``) — an edit that
    leaves the title alone can't be blocked by a title it didn't touch.

    Note the links scan is a FULL re-scan of the ticket text, not only the changed field: a
    clean ``--title`` edit on a ticket that already carries a bare reference in another section
    (e.g. ``why``) is blocked on that pre-existing reference — deliberately, as a nudge to fix
    it. On this edit path the waiver is ``--skip-links``/``--skip-user-impact-quality`` (the
    ``--force "<reason>"`` shortcut is a create/new-only flag — on ``change`` ``--force`` is the
    boolean transition-legality override, not a reason). Only a metadata-only edit, which
    touches no scanned field, is exempt.

    ``read_violation``/``read_waived`` carry the read-before-edit gate's outcome, computed by
    the CALLER (``cmd_change``, via ``_read_before_edit_violation``) — that check needs I/O (a
    marker-file lookup, ``read_tracker.is_fresh``), and this function stays I/O-free like every
    other gate it runs. Critically, ``read_waived`` must come from THIS command's OWN
    ``--skip-read-before-edit`` flag (the ``new_skips`` dict `cmd_change` collects), NOT from
    ``ticket.skips`` the way every other gate here is waived: ``apply_skips`` treats a gate name
    in ``ticket.skips`` as a PERMANENT waiver (it round-trips through the rendered body forever),
    which is correct for ``links``/``user-impact-quality`` (a false-positive match is a
    false positive forever) but wrong here — a `--skip-read-before-edit` a past command recorded
    would then silently auto-waive every FUTURE unread edit too, defeating the entire point of a
    gate that means "did the agent making THIS edit actually look first" (task-cli#119, review
    finding). So this gate is applied to the result AFTER ``apply_skips`` runs on the others,
    keyed on the caller-supplied ``read_waived`` instead.
    """
    from .policy import (
        apply_skips,
        impact_quality_violation,
        junk_title_violation,
        links_url_violation,
        links_violation,
        msgref_quote_reports,
        msgref_title_violation,
    )

    cfg_e = _enforce_config(cfg)
    raw = []
    warnings = []
    if check_links:
        # `check_links` means this edit touched a scanned content field. Re-run the same content
        # gates the create phase would, over the WHOLE (conservatively re-scanned) body: the prose
        # bare-reference gate, the Links-field URL gate, and the body tg#<id> quote gate. A pure
        # metadata edit (--due/--label) leaves check_links False and is exempt from all three, so
        # a legacy junk link / unquoted reference never blocks an unrelated metadata edit — it is
        # still caught at create and at close (where these gates run unconditionally via check()).
        v = links_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
        v = links_url_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
        # Auto-expansion runs on this same command before enforcement, so a freshly set --what
        # carrying a reference is already quoted; this catches a pre-existing unquoted reference
        # in an untouched field — the backstop for a web-UI-authored ticket.
        deny, warn = msgref_quote_reports(
            ticket, cfg_e, _msgref_resolvable_ids(ticket, cfg_e)
        )
        if deny is not None:
            raw.append(deny)
        if warn is not None:
            warnings.append(warn)
    if check_impact:
        v = impact_quality_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
    if check_title:
        v = msgref_title_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
        # A title-only edit (`task change --title`) re-checks the junk-title shape too — the
        # gate must hold on every path that can leave a ticket with that title persisted, not
        # only create/done (review finding: `task change --title "Review uncommitted X diff"`
        # on an already-clean ticket bypassed it entirely).
        v = junk_title_violation(ticket, cfg_e)
        if v is not None:
            raw.append(v)
    result = apply_skips(raw, ticket, warnings)
    if read_violation is not None:
        if read_waived:
            if read_violation.gate not in result.skipped:
                result.skipped.append(read_violation.gate)
        else:
            result.violations.append(read_violation)
    if result.violations or result.skipped or result.warnings:
        _report_and_die(result, "update")


def _notify_mutation(cfg, ticket, action: str) -> None:
    """Send a TG notification after a ticket mutation (create/update/done/transition).

    Uses the daemon's notifier config (default: tg --tag report) and the daemon's
    notifications.on_mutation enable flag. Best-effort: a notifier failure never fails the
    ticket op. Read-only commands (list/find/read/session) never call this.
    """
    # Config gate: notifications.on_mutation (default True). Set to false to silence.
    notif_section = cfg.section("notifications")
    if not _as_bool_cfg(notif_section.get("on_mutation"), default=True):
        return
    from . import daemon as _daemon

    dcfg = _daemon.DaemonConfig.from_config(cfg)
    notifier, html_mode = _resolve_notification_notifier(dcfg.notifier)
    state_str = ticket.state.value if ticket.state else "unknown"
    msg = _mutation_message(ticket, action, state_str, html_mode=html_mode)
    _daemon.notify(msg, notifier)


def _mutation_message(
    ticket: Ticket, action: str, state_str: str, *, html_mode: bool
) -> str:
    """Render the one-line mutation notice in plain text or Telegram HTML."""
    ticket_id = ticket.id or "(new)"
    if not html_mode:
        url_part = f"\n{ticket.url}" if ticket.url else ""
        return f"[task {action}] {ticket_id}: {ticket.title} [{state_str}]{url_part}"

    safe_action = html.escape(action, quote=False)
    safe_title = html.escape(ticket.title or "", quote=False)
    safe_state = html.escape(state_str, quote=False)
    display_id = html.escape(ticket_id, quote=False)
    if ticket.url:
        display_id = f'<a href="{html.escape(ticket.url, quote=True)}">{display_id}</a>'
    return f"[task {safe_action}] {display_id}: {safe_title} [{safe_state}]"


def _resolve_notification_notifier(
    notifier: tuple[str, ...],
) -> tuple[tuple[str, ...], bool]:
    if not _is_tg_notifier(notifier):
        return notifier, False
    notifier, fmt = _normalize_format_arg(notifier)
    if fmt is None:
        return (*notifier, "--format", "html"), True
    return notifier, fmt.strip().lower() == "html"


def _is_tg_notifier(notifier: tuple[str, ...]) -> bool:
    return bool(notifier) and Path(notifier[0]).name == "tg"


def _normalize_format_arg(
    notifier: tuple[str, ...],
) -> tuple[tuple[str, ...], str | None]:
    normalized: list[str] = []
    fmt: str | None = None
    idx = 0
    while idx < len(notifier):
        arg = notifier[idx]
        if arg == "--format":
            normalized.append(arg)
            if idx + 1 < len(notifier):
                fmt = notifier[idx + 1]
                normalized.append(fmt)
                idx += 2
            else:
                fmt = ""
                idx += 1
            continue
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
            normalized.extend(["--format", fmt])
            idx += 1
            continue
        normalized.append(arg)
        idx += 1
    return tuple(normalized), fmt


def _as_bool_cfg(value, *, default: bool) -> bool:
    """Coerce a config value to bool (mirrors daemon._as_bool without importing daemon)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "0", "off", "")
    return bool(value)


def _attach_screenshots(backend, ticket_id: str, screenshots) -> None:
    """Push each screenshot to the backend's attachment endpoint (durable proof), and swap each
    screenshot's ``.ref`` to the DURABLE ref ``backend.attach()`` returns (tg#16794/
    review-cli#484 — a raw local path left in ``.ref`` renders as a permanently broken markdown
    image in ``render.py``'s Screenshots section to every consumer but the machine that captured
    it; GitHub's ``attach()`` used to echo the local path back unchanged, so this discard-the-
    return-value bug was invisible until that backend actually started returning something
    durable). Mutating in place means any caller that extended ``ticket.screenshots`` with these
    SAME objects BEFORE its own persisting ``backend.update()``/``create()`` call gets the swap
    reflected in the persisted body for free — see ``cmd_change``, which now attaches before its
    ``update()`` for exactly this reason.

    Best-effort per screenshot, as before: an attach failure leaves ``.ref`` untouched (the raw
    local ref still lives in the body, just not durably attached).

    Catches ``AmbiguousBackendError`` too, not just ``BackendError`` (review finding): the
    ticket/comment this attaches to already exists by the time this runs, so an ambiguous attach
    outcome carries none of the "did this create a duplicate?" risk the type exists to flag — it
    is exactly the harmless-to-swallow case this function's own docstring already describes.
    Missing this was itself a bug: a caller wrapping ``backend.create()`` + this in one ``try``
    (``cmd_create``) would misreport a definitely-created ticket as merely "may have been
    created" and never print its id, because this raised past its own best-effort contract.
    """
    from .backends import AmbiguousBackendError, BackendError

    for shot in screenshots:
        try:
            shot.ref = backend.attach(ticket_id, shot.ref)
        except (BackendError, AmbiguousBackendError):
            continue


def _create_ticket_or_ambiguous(
    backend, ticket: Ticket
) -> tuple[Ticket | None, int | None]:
    """Create ``ticket`` via ``backend``. Returns ``(created, None)`` on success, or
    ``(None, exit_code)`` when the outcome is ambiguous — the caller returns ``exit_code``
    immediately without further output (the message is already printed here).

    Shared by ``cmd_create`` and ``_classify_create`` — the two paths that create a NEW ticket
    and so share the exact same duplicate-on-retry hazard (review finding: only ``cmd_create``
    handled ``AmbiguousBackendError``, leaving the inbound-message hook path — which is ALSO a
    creation path — to crash with a raw traceback on the identical scenario).
    """
    from .backends import AmbiguousBackendError, BackendError

    try:
        return backend.create(ticket), None
    except AmbiguousBackendError as exc:
        # the connection dropped AFTER the backend already accepted the request (e.g. GitHub
        # returned 201 and created the issue, then the read itself failed) — this is NOT the
        # same as a clean, nothing-happened refusal (that's the BackendError/_UserError/exit-2
        # path below), so it gets its own message and its own exit code rather than either a
        # bare traceback or a misleadingly-clean "error:" line.
        print(_err(f"ambiguous: {exc}"))
        print(
            _warn(
                "  the ticket may have already been created — run `task list` (or check the "
                "backend) before retrying, to avoid creating a duplicate."
            )
        )
        return None, exc.exit_code
    except BackendError as exc:
        raise _UserError(str(exc)) from exc


# ── commands ────────────────────────────────────────────────────────────────────────


def cmd_create(args: argparse.Namespace) -> int:
    cfg = _load(args)
    # create is the one repo-BOUND op: it writes a ticket into a specific project, so it needs
    # to know which one. Outside a repo (and with no pinned coordinate) fail with an honest
    # 3-part error — never the cryptic "no 'origin' remote" the backend would otherwise throw.
    if _current_project_overlay(cfg) is None:
        raise _UserError(_create_without_repo_error(cfg))
    session = _detect_session(cfg)

    title = args.title
    what = args.what or ""
    if args.from_message and not title:
        # derive a title from the first line of the raw message (the hook path) — see
        # _derive_title_from_message for why this strips a leading tg-cli inbound wrap first.
        title = _derive_title_from_message(args.from_message)
        what = what or args.from_message.strip()
    if not title:
        raise _UserError(
            "a title is required (--title, or --from-message to derive one)"
        )

    # Expand tg#<id> mentions in each prose field, once history is loaded (a single read of
    # tg-cli's local history log, reused across all four fields — see _load_msgref_history).
    # Safe to do BEFORE the gates run: `links`/`user-impact-quality` ignore any `>` blockquote
    # line (policy._strip_quoted_blocks), which is exactly the shape the appended quote takes, so
    # the quoted Telegram message — arbitrary text the ticket's author didn't write — can never
    # itself cause a refusal, no matter which command later re-scans the stored field
    # (task-cli#45 review finding: an earlier "expand only after THIS command's gates" version
    # protected only the expanding command itself — a later edit or close re-scanned the already-
    # expanded stored text and reintroduced the same failure).
    #
    # Acceptance criteria are deliberately EXCLUDED: render.py serializes each criterion as ONE
    # markdown checkbox line (`- [ ] <text>`), and _parse_criteria reads it back line-by-line —
    # a criterion text containing the quote's embedded newlines renders as loose markdown BELOW
    # the checkbox and is silently dropped on the next parse (verified empirically: render→parse
    # loses everything after the first line). A tg#<id> in a criterion stays a bare, un-expanded
    # mention — harmless, since the links gate already excludes tg#<id> from its bare-reference
    # scan.
    # A recorded --skip-msgref-quote means the author declares the tg#<id> a false positive (a
    # version tag, not a message ref), so NEITHER expand it (attaching an unrelated, possibly
    # private Telegram message) NOR read history at all — the skip must govern expansion, not only
    # the gate, or the waiver would still leak the very quote it waives (review finding).
    skips = _collect_skips(args)
    expand = _msgref_expand_enabled(skips, cfg)
    history = (
        _load_msgref_history_if_needed(
            what, args.why or "", args.impact or "", args.if_not_done or ""
        )
        if expand
        else None
    )
    labels = list(dict.fromkeys([*args.label, session.label]))
    screenshots = [Screenshot(ref=p, kind="creation") for p in args.screenshot]
    due = _normalize_due(getattr(args, "due", None)) or ""
    ticket = Ticket(
        title=title,
        what=_expand_msgrefs(what, history) if expand else what,
        why=_expand_msgrefs(args.why or "", history) if expand else (args.why or ""),
        user_impact=_expand_msgrefs(args.impact or "", history)
        if expand
        else (args.impact or ""),
        cost_of_inaction=_expand_msgrefs(args.if_not_done or "", history)
        if expand
        else (args.if_not_done or ""),
        acceptance=list(args.acceptance),
        screenshots=screenshots,
        labels=labels,
        # NOTE: the session id already lives in `labels` (the value `session_tickets()` queries
        # by) — it must NOT also go into `links`. The Links section renders as `- key: value`
        # with no URL requirement, so a "Session: session:2" entry silently escaped the links
        # gate (which only scans prose fields, see policy._scanned_text) and showed up in the
        # ticket as a fake, non-URL "link". Reported by the CTO as junk (2026-07-17).
        skips=skips,
        due=due,
    )

    from .policy import GATE_LINKS, GATE_TODO_SHAPED, GATE_USER_IMPACT_QUALITY, Phase

    force_reason = getattr(args, "force_reason", None)
    _apply_force(
        ticket,
        cfg,
        Phase.CREATE,
        force_reason,
        {GATE_LINKS, GATE_USER_IMPACT_QUALITY, GATE_TODO_SHAPED},
    )
    _enforce_or_die(ticket, cfg, Phase.CREATE)

    backend = _backend(cfg)
    if _enforce_config(cfg).duplicate:
        candidates = _session_dedup_candidates(backend, session)
        _refuse_if_duplicate(candidates, force_reason, ticket)
        _enforce_similar_duplicate_or_die(ticket, candidates, session, force_reason)

    created, ambiguous_exit = _create_ticket_or_ambiguous(backend, ticket)
    if ambiguous_exit is not None:
        return ambiguous_exit
    # `created` is set (not None) whenever ambiguous_exit is None — attach is best-effort and
    # runs AFTER, in its own scope, so an attach hiccup (even an ambiguous one — see
    # _attach_screenshots) can never suppress the fact that the ticket itself was created.
    # Unlike cmd_change/cmd_attach (which now attach BEFORE persisting, so the durable ref lands
    # in the same write — see _attach_screenshots's docstring), this MUST run after: there is no
    # ticket id to attach to before creation. Structural, not an oversight — the body still
    # embeds the raw local ref from render() until whatever NEXT edits/reads this ticket.
    _attach_screenshots(backend, created.id, screenshots)

    from .session import record

    record(
        session.id, created.id, created.title, repo=_safe_backend_coordinate(backend)
    )
    # task-cli#119: creating a ticket counts as reading it — see _mark_ticket_read's docstring.
    # `session=session` reuses the detection this function already did above (review finding,
    # task-cli#119) rather than paying a second `_detect_session` call.
    _mark_ticket_read(cfg, backend, created, session=session)
    from .logging import log_event

    log_event(
        "ticket.created", ticket_id=created.id, backend=cfg.backend, session=session.id
    )
    _notify_mutation(cfg, created, "created")

    if args.json:
        import json

        print(json.dumps(_ticket_dict(created), ensure_ascii=False, indent=2))
    else:
        print(_ok(f"created {created.id}  {created.url}"))
        _print_recent_tickets_after_create(session, backend)
        _print_related_tickets_nudge(backend, created)
        _print_session_attention_after_mutation(
            args, cfg, backend, current_id=created.id
        )
    return 0


# Limit defaults: a small one when piped/scripted (machine-readable, bounded), a larger one
# in an interactive TTY where the pager scrolls and a 30-row cap would just hide tickets.
_LIMIT_PIPED = 30
_LIMIT_INTERACTIVE = 100
_STALE_WARNING_SECONDS = 4 * 60 * 60
_RECENT_WARNING_SECONDS = 30 * 60
_ATTENTION_STATES = {State.TODO, State.IN_PROGRESS, State.IN_REVIEW}
_RECENT_PRIORITY_STATES = {State.IN_PROGRESS, State.IN_REVIEW}
_RECENT_TICKETS_LIMIT = 5
_RECENT_TICKETS_WINDOW_SECONDS = 60 * 60


def _effective_limit(args: argparse.Namespace) -> int:
    """The result cap: an explicit ``-n`` wins; otherwise pick by interactivity.

    Without ``-n``, a TTY gets the higher cap (the pager handles scrolling) and a pipe gets the
    small one (scriptable, bounded). Decided off the real stdout so a piped run is deterministic.
    """
    if getattr(args, "limit", None) is not None:
        return args.limit
    return _LIMIT_INTERACTIVE if sys.stdout.isatty() else _LIMIT_PIPED


def _emit_list(args: argparse.Namespace, blocks: list[str]) -> None:
    """Join the human-readable list output and route it through the pager (interactive only).

    ``blocks`` are the already-rendered sections (e.g. the "showing all project tasks" notice
    then the ticket/group body). Empty blocks are dropped so we don't emit blank separators.
    """
    from .pager import page

    text = "\n".join(b for b in blocks if b)
    page(text, no_pager_flag=getattr(args, "no_pager", False))


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _age_text(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _attention_line(ticket: Ticket, age: int | None) -> str:
    suffix = f" (touched {_age_text(age)})" if age is not None else ""
    return f"{ticket.id} [{ticket.state.value}] {ticket.title}{suffix}"


def _stale_line(ticket: Ticket, age: int) -> str:
    return (
        f"{ticket.id} [{ticket.state.value}] {ticket.title} (updated {_age_text(age)})"
    )


def _format_duration_hours(seconds: int) -> str:
    """A duration in hours for display, without a naive ``// 3600`` flooring any realistic
    sub-hour value straight to a misleading "0h".

    ``TASK_GLOBAL_STALE_SECONDS`` accepts any positive value, not just whole hours — integer
    division would print "0h+" for a configured 30-minute threshold and silently round 90
    minutes down to "1h+". One decimal place is enough precision for a nudge message; a
    threshold under ~3 minutes still rounds to "0h+" (a config value that extreme is not a
    realistic staleness window for this feature, so this stays a display nicety, not a
    correctness guarantee for arbitrarily tiny thresholds).
    """
    hours = round(seconds / 3600, 1)
    return f"{hours:g}h"


def _session_attention_notice(
    session, tickets: list[Ticket], backend, cfg, *, priority_change: bool = False
) -> str:
    """Human-only warning for active session tasks that are easy to forget.

    Backend timestamps are not portable across GitHub/Linear, but the session sidecar records
    when task-cli last touched a ticket in this session. Use that as a conservative signal:
    old active work suggests parallel follow-up; recently touched in-progress work suggests the
    agent should ask whether to continue it before switching priority.

    Each ticket's OWN verified coordinate scopes its sidecar lookup (task-cli#83, review
    finding, 2nd round): `tickets` is already correctly repo-scoped (it comes from
    `backend.session_tickets`, a native per-backend query), but without a per-entry filter,
    keying `read_entries()` by bare ticket id would let ANOTHER repo's same-numbered entry (e.g.
    both repos have a `#1`) silently supply the wrong touch-timestamp for one of THESE tickets —
    producing a false or suppressed staleness/recency warning, not a wrong ticket shown outright.
    A single coordinate shared across every ticket (the earlier, simpler version of this fix)
    regressed the exact same-team-Linear misroute scenario this whole feature targets: the
    WRITE side (`_record_session_touch`) records a misrouted ticket under its ACTUAL project
    (`verified_ticket_coordinate`), but a single naive `backend`-derived coordinate here would
    filter by the project the id merely happened to be ROUTED THROUGH — the two would never
    match, silently dropping the just-touched ticket from its own session's attention notice.
    Computing each ticket's coordinate individually (same helper the write side uses) keeps
    both sides in agreement.

    Resolving a coordinate is best-effort (`_safe_verified_coordinate`, which itself degrades to
    `_safe_backend_coordinate` on failure): some callers (test doubles, a minimal/partial backend
    implementation) may not carry the attributes it reads. This is a "best-effort human warning"
    (see above), not a security boundary like the sidecar write path — an unresolvable ticket
    degrades to the same `""` coordinate a pre-task-cli#83 legacy line carries (so it can still
    match a legacy entry, but never a real repo's), rather than raising into a command that
    otherwise has nothing to do with this feature.

    KNOWN LIMITATION (review finding, 4th round), out of scope for task-cli#83: `tickets` only
    ever contains what `backend.session_tickets()` actually returned, and Linear's own
    `list()`/`session_tickets()` filters SERVER-SIDE to `self.project` whenever the routed-to
    registry entry pins one — so a same-team ticket living in a genuinely different, ALSO
    project-pinned entry is never fetched in the first place, regardless of anything this
    function does with whatever coordinate it computes. The per-ticket correction here only has
    something to act on when the routed-to entry is TEAM-ONLY (no `project:` pinned), so Linear
    returns every team ticket unfiltered and different tickets in that set can genuinely report
    different `backend_project_id`s. Fully closing the project-pinned case would mean querying
    every same-team registered project's own backend for its session tickets too (the same
    cross-project aggregation `_list_grouped` already does elsewhere) — a materially bigger
    change than task-cli#83's actual scope (the session-sidecar's cross-REPO id-leak), left as a
    separate concern.
    """
    if session.source == "none":
        return ""
    active = [t for t in tickets if t.state in _ATTENTION_STATES]
    if not active:
        return ""

    from .session import read_entries

    # 3rd-round review finding (codex + Opus, independently): collapsing `read_entries()` to a
    # bare-id dict BEFORE filtering by coordinate reintroduces the exact cross-repo same-id bug
    # this feature exists to prevent — a DIFFERENT repo's more-recently-appended same-numbered
    # entry would shadow THIS repo's own valid, coordinate-matching one, since dict construction
    # keeps only the last-appended entry per bare id regardless of which repo it belongs to.
    # Fix: compute each active ticket's expected coordinate FIRST, then walk entries in their
    # natural (oldest-appended-first) order, keeping only entries that match BOTH the id and
    # that ticket's own coordinate — "last write wins" now operates within the matching set,
    # not across repos. `_verified_coord_cache` avoids re-parsing the project registry once per
    # DISTINCT `backend_project_id` value in `active` (review finding: most session tickets share
    # one actual project, so this collapses what would otherwise be an O(active × registry) scan
    # back to O(distinct_projects × registry) — `verified_ticket_coordinate`'s result depends
    # only on `backend`/`cfg` (fixed for this call) and `ticket.backend_project_id`).
    _verified_coord_cache: dict[str, str] = {}

    def _ticket_coordinate(ticket: Ticket) -> str:
        key = str(getattr(ticket, "backend_project_id", "") or "")
        if key not in _verified_coord_cache:
            _verified_coord_cache[key] = _safe_verified_coordinate(cfg, backend, ticket)
        return _verified_coord_cache[key]

    coordinates = {ticket.id: _ticket_coordinate(ticket) for ticket in active}
    entries = {}
    for entry in read_entries(session.id):
        if entry.id in coordinates and entry.repo == coordinates[entry.id]:
            entries[entry.id] = entry
    now = int(time.time())
    stale_after = _env_seconds("TASK_STALE_WARNING_SECONDS", _STALE_WARNING_SECONDS)
    recent_before = _env_seconds("TASK_RECENT_WARNING_SECONDS", _RECENT_WARNING_SECONDS)
    stale: list[tuple[Ticket, int | None]] = []
    recent: list[tuple[Ticket, int | None]] = []
    for ticket in active:
        entry = entries.get(ticket.id)
        age = (now - entry.ts) if (entry and entry.ts > 0) else None
        if age is not None and stale_after and age >= stale_after:
            stale.append((ticket, age))
            continue
        if (
            (len(active) > 1 or priority_change)
            and ticket.state in _RECENT_PRIORITY_STATES
            and age is not None
            and age >= 0
            and recent_before
            and age <= recent_before
        ):
            recent.append((ticket, age))

    if not stale and not recent:
        return ""

    lines = [_warn("warning: active tasks in this session may need attention:")]
    if stale:
        lines.append(
            _dim(
                "  forgotten/old: consider continuing in parallel or explicitly parking:"
            )
        )
        lines.extend(
            _dim(f"    - {_attention_line(ticket, age)}") for ticket, age in stale[:3]
        )
    if recent:
        lines.append(
            _dim(
                "  recently touched active task: ask whether to continue it or park it before switching priorities:"
            )
        )
        lines.extend(
            _dim(f"    - {_attention_line(ticket, age)}") for ticket, age in recent[:3]
        )
    return "\n".join(lines)


def _safe_backend_coordinate(backend) -> str:
    """`backend_coordinate(backend)`, degrading to `""` on any failure (task-cli#83 review
    finding: `backend_coordinate` reads `backend.name` via direct attribute access, so a
    partial/test-double backend missing it raises). The sidecar `repo` field is a best-effort
    scoping aid, not a security boundary — every call site here runs AFTER the real mutation
    (ticket create/change/etc.) already succeeded, so a coordinate failure must degrade to an
    unscoped write, matching a pre-task-cli#83 legacy line, rather than turn an already-
    successful command into a traceback."""
    from .backends import backend_coordinate

    try:
        return backend_coordinate(backend)
    except Exception:
        return ""


def _local_project_coordinate(cfg) -> str:
    """This repo's coordinate resolved from config/git alone — no backend construction, no
    credential harvest (task-cli#83 review finding: gating `task session`'s leak-fix behind
    `_backend(cfg)`, which is documented "Effectful (harvests creds)", makes the fix silently
    inert in exactly the credential-free/outside-CI-creds environments a purely-local sidecar
    command previously worked in unconditionally — it would fall back to unfiltered and the
    leak would persist right where a `show`/`bind` user most needs it fixed).

    ``_current_project_overlay`` resolves the SAME pinned-config-or-git-origin coordinate
    `get_backend` would, but stops at local git metadata (``git rev-parse``/``git remote``,
    no credentials) — :class:`tasklib.projects.Project` then computes the identical string
    shape :func:`tasklib.backends.backend_coordinate` does from a LIVE instance (see
    ``test_backend_coordinate_*_matches_project_coordinate_shape``). Degrades to `""` ONLY for
    the genuine outside-a-repo case (``_current_project_overlay`` returning ``None``), since
    there's no coordinate to filter by there (and `task session` is documented to work outside a
    repo). A genuine resolution ERROR (a broken/unsupported git origin, a teamless `linear`
    config) is a DIFFERENT thing — `_current_project_overlay` already raises `_UserError` for
    that case rather than returning `None` — and is deliberately let PROPAGATE here (2nd-round
    review finding: an earlier blanket `except Exception: return ""` swallowed that `_UserError`
    into the same empty coordinate as "outside a repo", which made `cmd_session`'s callers take
    the unfiltered fallback branch on a transient error — silently reintroducing the exact
    cross-repo leak this coordinate exists to prevent, and losing a `session bind` write into an
    unscoped entry it can no longer find once the error clears). Callers reach this through
    `main()`'s existing top-level `except _UserError` handler, which already turns it into a
    clean ``error: ...`` message + exit 2 — the same handling every other resolution failure in
    this module gets, not a new code path.

    A truly UNEXPECTED exception (not `_UserError`, i.e. not one of this module's own recognized
    resolution-error shapes) still degrades to `""` — preserving `bind`'s original design intent
    (it "had NO backend dependency before task-cli#83") of never hard-crashing a purely-local
    sidecar command on a failure mode nobody anticipated. Only a genuine, RECOGNIZED resolution
    error gets the clean-surface treatment; a mystery exception still fails soft. Construction of
    the (pure, no-I/O) `Project` value from an already-resolved overlay is likewise degraded to
    `""` on any failure."""
    try:
        current = _current_project_overlay(cfg)
    except _UserError:
        raise
    except Exception:
        return ""
    if current is None:
        return ""
    name, overlay = current
    from .projects import Project

    try:
        return Project(
            name=name, backend=str(overlay.get("backend", cfg.backend)), overlay=overlay
        ).coordinate
    except Exception:
        return ""


def _recent_tickets_block(session, backend) -> str:
    """The 'Recent tickets (last hour)' block printed after a successful `task new`/`create`
    (Alex, tg#10993/#10995): up to 5 tickets created in this session within the last hour,
    newest first, the just-created one included.

    Reuses the session sidecar (:mod:`tasklib.session`) — the same local, offline record
    `_refuse_if_duplicate` and `_session_attention_notice` already read — rather than a fresh
    backend query, so this costs no extra network round-trip. Note the same caveat
    `_session_attention_notice` already documents for this sidecar: ``SessionEntry.ts`` is when
    task-cli last TOUCHED the ticket in this session, not a pure creation timestamp. In practice
    an entry is written once, at `record()`-after-create time, and only moves if a later
    `change`/`done`/`check` in the SAME session touches that SAME ticket again — an accepted
    approximation, not a new one invented for this feature.

    ``session.source == "none"`` (no `TASK_SESSION`/tmux pane/git branch resolved) short-
    circuits to ``""``, mirroring `_session_attention_notice`'s own guard: an unresolved
    session always collapses to the SAME ``id="default"`` sidecar file, shared by every repo
    where detection fails — without this guard, "recent tickets" could leak a completely
    unrelated project's tickets into this one's `create` output.

    Also filters every candidate entry to THIS repo's own coordinate (task-cli#83): a session id
    (git branch / tmux pane) is not repo-unique, so two different repos on the same branch name
    (or sharing a `TASK_SESSION`/tmux pane) share one sidecar file. Without this filter, an entry
    recorded while working in a DIFFERENT repo under the same session id would print here too —
    a ticket id/title from an unrelated project, confusable with this repo's own ids. A legacy
    entry with no `repo` recorded (written before this field existed) never matches and is simply
    not shown, rather than shown on the unverifiable assumption that it belongs here.

    Bounded by BOTH the count (5) AND a genuinely fixed 1-hour window — deliberately NOT
    configurable (an earlier `TASK_RECENT_TICKETS_WINDOW_SECONDS` override made the hardcoded
    "(last hour)" header lie under a different value; Alex's ask was a fixed hour, so the
    header just stays honest instead). Returns ``""`` when nothing falls in the window (never
    happens right after a successful create: the ticket just created is always inside its own
    window).
    """
    if session.source == "none":
        return ""
    from .session import read_entries

    coordinate = _safe_backend_coordinate(backend)
    entries = read_entries(session.id)
    now = int(time.time())
    in_window = [
        e
        for e in entries
        if e.ts > 0
        and e.repo == coordinate
        and 0 <= now - e.ts <= _RECENT_TICKETS_WINDOW_SECONDS
    ]
    # `entries` is oldest-appended-first (session.py: "newest-last"). At 1-second `ts`
    # resolution, two touches in the same second are a real case (a fast create batch, or the
    # just-created ticket landing in the same second as a seeded one) — a plain
    # `sorted(..., reverse=True)` is stable and would keep ties in oldest-appended-first order,
    # silently violating "newest first". Break ties by original (append) position instead.
    by_recency = sorted(
        enumerate(in_window), key=lambda pair: (pair[1].ts, pair[0]), reverse=True
    )
    recent = [entry for _, entry in by_recency][:_RECENT_TICKETS_LIMIT]
    if not recent:
        return ""
    lines = [_dim("Recent tickets (last hour):")]
    lines.extend(_dim(f"  {e.id}  {e.title}") for e in recent)
    return "\n".join(lines)


def _print_recent_tickets_after_create(session, backend) -> None:
    """Best-effort wrapper around `_recent_tickets_block`: a sidecar hiccup must never turn a
    successful `create` into a failure — mirrors `_print_session_attention_after_mutation`'s
    same fail-soft contract for the sibling session-sidecar-backed notice."""
    try:
        block = _recent_tickets_block(session, backend)
    except Exception as exc:
        from .logging import log_event

        log_event("recent_tickets.skipped", error=type(exc).__name__)
        return
    if block:
        print(block)


def _print_related_tickets_nudge(backend, created: Ticket) -> None:
    """Best-effort post-`task new` nudge (task-cli#154): a cheap same-repo relatedness scan
    (title/label keyword overlap, `tasklib.links.find_related_candidates`) suggesting tickets
    that MIGHT be worth linking. Never auto-links — only prints the exact `task link` command
    for a human/agent to run. Mirrors `_print_recent_tickets_after_create`'s fail-soft contract:
    a backend hiccup here must never turn an already-successful `create` into a failure.
    """
    try:
        block = _related_tickets_block(backend, created)
    except Exception as exc:
        from .logging import log_event

        log_event("related_tickets.skipped", error=type(exc).__name__)
        return
    if block:
        print(block)


def _related_tickets_block(backend, created: Ticket) -> str:
    """The nudge text, or `""` when nothing plausibly related was found. Scans up to 100 of the
    repo's tickets (the same "cheap read" budget `task audit`'s default scan uses) — cheap
    keyword overlap, not a fuzzy/NLP search (see `links.find_related_candidates`).

    A candidate's TITLE is untrusted (review finding, task-cli#154 round 3, Grok): anyone who
    can file a ticket in the same repo controls it, and this text lands in agent-facing,
    copy-pasteable stdout right next to a real `task link` command. Run through the same
    control/format-char stripper `policy.py` uses for untrusted Links values
    (`policy.safe_display`) before printing — in particular this removes embedded newlines,
    closing the injection this finding described: a title carrying a fake `\\n    task link
    #victim blocks #attacker` line that would otherwise print as if it were the tool's own
    suggestion.
    """
    from .links import find_related_candidates
    from .policy import safe_display

    candidates = find_related_candidates(created, backend.list(limit=100), limit=5)
    if not candidates:
        return ""
    lines = [_dim("related open tickets found (verify, then link if relevant):")]
    for c in candidates:
        title = safe_display(c.ticket.title)
        lines.append(_dim(f"  {c.ticket.id}  {title}"))
        lines.append(_dim(f"    task link {created.id} relates-to {c.ticket.id}"))
    return "\n".join(lines)


def _safe_verified_coordinate(cfg, backend, ticket: Ticket) -> str:
    """``verified_ticket_coordinate(cfg, backend, ticket)``, degrading to the NAIVE
    ``_safe_backend_coordinate(backend)`` — not `""` — on any failure (review finding: the
    correction machinery this wraps does extra work, `projects_from_config`/`Project`
    construction from the config registry, that the naive coordinate never needed; if THAT
    fails, e.g. a malformed `projects:` entry, the caller already has a perfectly good naive
    coordinate in hand and discarding it in favor of an empty one would make the sidecar entry
    invisible to every reader — `_recent_tickets_block`/`_session_attention_notice`/
    `cmd_session` are all explicitly tested to treat `repo=""` as "outside a repo, don't
    filter/show," which is a much worse outcome than "recorded under the possibly-still-wrong
    pre-correction coordinate," the exact behavior before this fix existed). This sidecar field
    is a best-effort scoping aid, not a security boundary, and every call site here runs AFTER
    the real mutation already succeeded."""
    from .backends import verified_ticket_coordinate

    try:
        return verified_ticket_coordinate(cfg, backend, ticket)
    except Exception:
        return _safe_backend_coordinate(backend)


def _record_session_touch(cfg, backend, ticket: Ticket) -> None:
    """Refresh this session's sidecar timestamp for a ticket mutation.

    Scopes the sidecar entry to this ticket's ACTUAL repo coordinate (task-cli#83), not just
    whichever backend config happened to resolve/fetch it — outside a repo, a Linear id can be
    routed to a same-team project's config that isn't the ticket's real project (review finding);
    `_safe_verified_coordinate` corrects for that using the ticket's own reported project when
    available, falling back to the naive backend coordinate otherwise.
    """
    if not ticket.id:
        return
    session = _detect_session(cfg)
    if session.source == "none":
        return
    if session.label not in ticket.labels:
        return
    from .session import record

    record(
        session.id,
        ticket.id,
        ticket.title,
        repo=_safe_verified_coordinate(cfg, backend, ticket),
    )


def _mark_ticket_read(cfg, backend, ticket: Ticket, *, session=None) -> None:
    """Record ``ticket`` as read by the current session — the write half of the
    read-before-edit gate (task-cli#119). Shared by `cmd_read`/`cmd_view` (an explicit read),
    `cmd_create`/`_classify_create` (creating a ticket counts as having read it), and
    `cmd_change` (a successful ``--what`` edit also counts, since the session just supplied the
    new content). See `tasklib.read_tracker`'s module docstring for the full design rationale —
    scope, TTL, fail-closed tri-state, what the marker does and doesn't prove, and the accepted
    Linear create/get/update coordinate-shape limitation.

    ``session``, when given, is used instead of a fresh `_detect_session(cfg)` call — the
    session cannot change mid-process, so a caller that already detected it this same command
    (`cmd_change`'s own gate check) should pass it rather than pay a second detection (which can
    spawn a `git rev-parse` subprocess on the git-branch fallback; review finding, task-cli#119).
    Every OTHER call site (`cmd_read`, `cmd_create`, `_classify_create`) omits it and resolves
    internally, unchanged.

    Unlike `_record_session_touch`, this does NOT skip an unresolved session
    (`session.source == "none"`) or require the ticket to carry this session's label — those
    guards exist for the SESSION-TICKET-LIST sidecar's user-visible listing concern, which this
    gate doesn't have.

    Wrapped in a broad ``except Exception`` (not just the ``OSError`` ``read_tracker.record``
    already swallows): a bookkeeping side effect of a command whose PRIMARY job (show the
    ticket / create it / apply the edit) already succeeded by the time this runs must degrade to
    "not recorded" (fail-closed: the next edit is refused as unread) rather than crash an
    already-successful command, mirroring `_print_recent_tickets_after_create`'s identical
    contract for its own best-effort sidecar read.
    """
    if not ticket.id:
        return
    try:
        from .read_tracker import record as record_read

        if session is None:
            session = _detect_session(cfg)
        record_read(
            session.id, ticket.id, repo=_safe_verified_coordinate(cfg, backend, ticket)
        )
    except Exception as exc:
        from .logging import log_event

        log_event("read_tracker.mark_read_failed", error=type(exc).__name__)


def _print_session_attention_after_mutation(
    args: argparse.Namespace, cfg, backend, *, current_id: str | None = None
) -> None:
    """Best-effort human warning after commands that can switch task context."""
    if args.json:
        return
    try:
        session = _detect_session(cfg)
        if session.source == "none":
            return
        tickets = backend.session_tickets(session.label, limit=_LIMIT_INTERACTIVE)
        if current_id is not None:
            tickets = [ticket for ticket in tickets if ticket.id != current_id]
        notice = _session_attention_notice(
            session, tickets, backend, cfg, priority_change=True
        )
    except Exception as exc:
        from .logging import log_event

        log_event("session.attention.skipped", error=type(exc).__name__)
        return
    if notice:
        print(notice)


def _scope_to_current_user(backend, tickets: list) -> list:
    """Keep only tickets the current user actually reported/filed (issue #59, review P2): an
    unqualified scan would nag about a COWORKER's stale ticket in a shared GitHub/Linear
    project, contradicting this feature's "the user's own open tickets" framing. Reporter
    (issue author / Linear creator), not assignee: `create()` never sets a native assignee on
    either backend, so an assignee-based filter would exclude virtually every ticket task-cli
    itself creates — the reporter is who actually ran task-cli.

    Fails OPEN: if the backend can't determine "who am I" (`current_user()` -> None — a
    transient hiccup, or simply unsupported), keep the unfiltered set rather than dropping every
    ticket — parity with the pre-fix behavior, never worse.

    Also fails OPEN if `current_user()` *raises* — a network hiccup a backend forgot to wrap in
    `BackendError`, or `AttributeError` from a third-party backend that never implements it (the
    protocol is structural, not ABC-enforced) — rather than a "normal" `None` return (review P1,
    found independently by two reviewers). Left uncaught, that exception would propagate into
    `_global_stale_notice_block`'s single broad `except Exception`, which swallows the ENTIRE
    nudge (and skips `mark_checked`) — silently hiding a real stale ticket that has nothing to do
    with the identity lookup. Catching it HERE, at the scope-lookup's own boundary, keeps that
    failure mode contained to "don't scope" instead of "don't nudge at all".
    """
    try:
        me = backend.current_user()
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.identity_lookup_failed", error=type(exc).__name__)
        return tickets
    if not me:
        return tickets
    return [t for t in tickets if t.reporter and t.reporter == me]


def _global_stale_notice_block(
    args: argparse.Namespace, backend, coordinate: str
) -> str:
    """Rate-limited nudge: ANY of the user's own active tickets stale >48h, not just this
    session's (issue #59 — `_session_attention_notice` above only ever sees session-touched
    tickets). Fail-soft end to end: a backend hiccup or a malformed cache file here must never
    break `task list`'s primary output, it's a bonus notice, not the command's job.

    ALWAYS fetches its OWN unfiltered (by `--state`/`--label`) ticket set — never a caller's
    already-filtered `tickets`. A caller in the all-tasks fallback branch already has a ticket
    list in hand, but reusing it silently narrows "every open ticket" to whatever the user
    filtered for (e.g. `task list --state done` would only ever see done tickets and could
    never flag a stale active one). The extra backend round-trip this costs is bounded by the
    rate limit — at most once per window, not once per invocation. It IS narrowed by
    :func:`_scope_to_current_user` right after the fetch, to the current user's own tickets
    (issue #59, review P2) — a shared GitHub/Linear project must not nag one user about a
    coworker's stale ticket.

    Like `daemon.py`'s own polling loop, this reads at most `_LIMIT_INTERACTIVE` (100) tickets
    per backend page — a project with more than that many open tickets won't have the tail
    examined. Matches the daemon's existing `query_limit` precedent rather than adding
    pagination for what's meant to stay a lightweight nudge.
    """
    if args.json:
        return ""
    from . import stale_notice

    try:
        if not stale_notice.should_check(coordinate):
            return ""
        tickets = backend.list(limit=_LIMIT_INTERACTIVE)
        tickets = _scope_to_current_user(backend, tickets)
        stale = stale_notice.stale_tickets(tickets)
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.skipped", error=type(exc).__name__)
        return ""

    # Mark AFTER a completed check (never inside the try above — a transient backend failure
    # must not consume the rate-limit window, see should_check's docstring) and in its OWN
    # try/except: an unwritable cache must not discard an already-computed finding — worst case
    # we merely check again sooner than the window intends, never worse than that.
    try:
        stale_notice.mark_checked(coordinate)
    except Exception as exc:
        from .logging import log_event

        log_event("global_stale.mark_failed", error=type(exc).__name__)

    if not stale:
        return ""
    threshold = _format_duration_hours(stale_notice.stale_after_seconds())
    lines = [
        _warn(
            f"warning: {len(stale)} open ticket(s) look stale (no backend update in {threshold}+):"
        )
    ]
    lines.extend(_dim(f"    - {_stale_line(ticket, age)}") for ticket, age in stale[:3])
    return "\n".join(lines)


def cmd_list(args: argparse.Namespace) -> int:
    """List tickets. Three shapes, chosen by where you are and what you ask for:

    - **Session scope** (default, inside a repo, in an agent session with tickets) — this
      session's tickets in the current repo.
    - **All-tasks fallback** — same as session scope BUT when there's no agent session, or the
      session has no tickets: fall back to ALL tickets in the current repo, and SAY SO.
    - **Cross-project grouped** — ``--all``, or run OUTSIDE any repo: every known project's
      tickets, grouped under a heading per project. A project whose backend errors shows a
      degraded group; it never aborts the whole aggregation.

    Human output (non-``--json``) is paged through ``less`` when stdout is an interactive TTY
    and the user hasn't opted out (``--no-pager`` / ``NO_PAGER`` / ``$PAGER=''``); a piped run
    prints plain text so it stays scriptable.
    """
    cfg = _load(args)
    state = _parse_state(args.state) if args.state else None
    current = _current_project_overlay(cfg)

    # Cross-project grouped view: an explicit --all, or there's no current repo to scope to.
    if args.all or current is None:
        return _list_grouped(args, cfg, state, outside_repo=current is None)

    # Inside a repo, no --all → session scope, with the all-tasks fallback.
    return _list_session_scoped(args, cfg, state, current)


def _list_session_scoped(args, cfg, state, current) -> int:
    """The default in-repo view: this session's tickets, falling back to all repo tasks."""
    from .backends import BackendError

    backend = _backend(cfg)
    session = _detect_session(cfg)
    no_session = session.source == "none"
    want_labels = set(args.label or [])
    limit = _effective_limit(args)
    try:
        # Whether to fall back is decided on the UNFILTERED session result: a session that HAS
        # tickets but none match --state/--label is a legitimately-empty FILTERED view, NOT a
        # reason to spill every other session's tickets. Only a truly empty session falls back.
        session_tickets = (
            [] if no_session else backend.session_tickets(session.label, limit=limit)
        )
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    if no_session or not session_tickets:
        # Fallback: no agent session, or the session has NO tickets at all → show ALL repo tasks
        # and SAY SO, so the user understands why they're seeing everything (not just theirs).
        try:
            tickets = backend.list(labels=args.label or None, state=state, limit=limit)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        if args.json:
            _print_tickets_json(tickets)
            return 0
        notice = _dim(
            "showing all project tasks (`task list` defaults to tasks created in the agent session)"
        )
        # deliberately NOT reusing `tickets` here — it may be --state/--label filtered, and the
        # global check needs its OWN unfiltered view (see _global_stale_notice_block's docstring).
        stale_notice = _global_stale_notice_block(args, backend, current[0])
        _emit_list(args, [notice, stale_notice, _format_tickets(tickets)])
        return 0

    # session HAS tickets → scope to it, then apply the --state/--label filters within that view.
    tickets = session_tickets
    if state is not None:
        tickets = [t for t in tickets if t.state == state]
    if want_labels:
        tickets = [t for t in tickets if want_labels <= set(t.labels)]
    if args.json:
        _print_tickets_json(tickets)
        return 0
    # session_tickets is scoped to THIS session, so the global check needs its own (rate-limited,
    # best-effort) fetch of every project ticket — see _global_stale_notice_block.
    stale_notice = _global_stale_notice_block(args, backend, current[0])
    _emit_list(
        args,
        [
            _session_attention_notice(session, session_tickets, backend, cfg),
            stale_notice,
            _dim(f"session {session.id} ({session.source}):"),
            _format_tickets(tickets),
        ],
    )
    return 0


def _list_grouped(args, cfg, state, *, outside_repo: bool) -> int:
    """Cross-project view: query every known project and group tickets under its heading."""
    projects, current_coordinate = _known_projects(cfg)
    if not projects:
        # No registry and no current repo: there is nothing to aggregate. Guide the user to
        # register a project rather than printing a bare empty list (informative > silent).
        raise _UserError(
            "no projects to list. You are outside a git repo and no projects are registered.\n"
            "  why: `task list` aggregates across known projects; none are configured.\n"
            "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml "
            "(e.g. `projects: [{repo: owner/name}]`), or run inside a repo."
        )

    groups = _aggregate_projects(
        cfg,
        projects,
        labels=args.label or None,
        state=state,
        limit=_effective_limit(args),
        current_coordinate=current_coordinate,
    )

    if args.json:
        _print_groups_json(groups)
        return 0

    # The session-vs-all line: an implicit aggregate (outside a repo) must explain itself; an
    # explicit `--all` was asked for, so no apology is needed there.
    notice = (
        _dim(
            "showing all project tasks (`task list` defaults to tasks created in the agent session)"
        )
        if outside_repo
        else ""
    )
    _emit_list(args, [notice, _format_groups(groups)])
    return 0


@dataclass
class _ProjectGroup:
    """One project's slice of the aggregated list: its tickets, or a degraded error."""

    name: str
    backend: str
    tickets: list[Ticket]
    error: str | None = None
    current: bool = False  # True for the repo the user is currently inside


def _aggregate_projects(
    base_cfg, projects, *, labels, state, limit, current_coordinate=None
) -> list:
    """Query each project's backend; a failing one becomes a degraded group, not a hard stop.

    Each project's config is the base config with the project's overlay deep-merged in, so the
    existing :func:`tasklib.backends.get_backend` resolves it unchanged. The aggregation is
    best-effort by design: one unreachable/unauthed/empty project must not sink the rest of the
    cross-repo view (the "never aborts the whole aggregation" rule).
    """
    return _query_projects(
        base_cfg,
        projects,
        lambda b: b.list(labels=labels, state=state, limit=limit),
        current_coordinate,
    )


def _query_projects(base_cfg, projects, call, current_coordinate=None) -> list:
    """Run ``call(backend)`` for each project's backend, capturing a failure as a degraded group.

    The single best-effort fan-out used by both the grouped ``list`` and ``find`` — ``call`` is
    the per-backend query (``.list`` or ``.search``). One project's error never aborts the rest.
    ``current_coordinate`` flags the cwd's repo (by coordinate, so it works whether or not that
    repo is also in the registry).
    """
    from .backends import BackendError, get_backend
    from .credentials import CredentialError

    groups: list[_ProjectGroup] = []
    for proj in projects:
        cur = current_coordinate is not None and proj.coordinate == current_coordinate
        try:
            backend = get_backend(_config_for_overlay(base_cfg, proj.overlay))
            tickets = call(backend)
            groups.append(
                _ProjectGroup(
                    name=proj.name, backend=proj.backend, tickets=tickets, current=cur
                )
            )
        except (BackendError, CredentialError) as exc:
            groups.append(
                _ProjectGroup(
                    name=proj.name,
                    backend=proj.backend,
                    tickets=[],
                    error=str(exc),
                    current=cur,
                )
            )
    return groups


def _format_groups(groups) -> str:
    """Render the grouped, cross-project list as a string — a heading per project, tickets beneath.

    Heading shape: ``<name> · <backend> · <N> (current)`` — informative at a glance (which
    backend, how many, is this the repo I'm in). A degraded project shows its one-line error.
    Returned as one string so the caller can route it through the pager (interactive only).
    """
    total = sum(len(g.tickets) for g in groups)
    if total == 0 and all(g.error is None for g in groups):
        return _dim("(no tickets)")
    sections: list[str] = []
    for g in groups:
        lines = []
        marker = _dim(" (current)") if g.current else ""
        if g.error is not None:
            head = f"{g.backend} · " + _err("degraded")
        else:
            head = f"{g.backend} · {len(g.tickets)}"
        lines.append(_bold(g.name) + _dim(" · ") + head + marker)
        if g.error is not None:
            lines.append(_dim(f"  ! {g.error.splitlines()[0]}"))
        elif not g.tickets:
            lines.append(_dim("  (none)"))
        else:
            lines.extend("  " + _ticket_line(t) for t in g.tickets)
        sections.append("\n".join(lines))
    return "\n\n".join(
        sections
    )  # blank line BETWEEN project groups (was the per-iter print())


def _print_groups_json(groups) -> None:
    """The machine-readable grouped shape: ``[{project, backend, current, error, tickets}]``."""
    import json

    payload = [
        {
            "project": g.name,
            "backend": g.backend,
            "current": g.current,
            "error": g.error,
            "tickets": [_ticket_dict(t) for t in g.tickets],
        }
        for g in groups
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ── gantt (read-only due-date timeline) ─────────────────────────────────────────────


def _gantt_tickets(args, cfg, state) -> list[Ticket]:
    """Collect the tickets to chart, with the SAME scoping `list` uses (session / all / grouped).

    Returns a flat ticket list — the chart is one timeline, so a cross-project run flattens every
    project's tickets into a single axis (the project shows up via the ticket id prefix). Reuses
    the existing backend queries so `gantt` and `list` never drift on what "this session" means.
    """
    from .backends import BackendError

    current = _current_project_overlay(cfg)
    limit = _effective_limit(args)

    # Cross-project: an explicit --all, or outside any repo → flatten every known project's tickets.
    if args.all or current is None:
        projects, current_coordinate = _known_projects(cfg)
        if not projects:
            raise _UserError(
                "no projects to chart. You are outside a git repo and no projects are registered.\n"
                "  why: `task gantt` charts known projects; none are configured.\n"
                "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml, or run inside a repo."
            )
        groups = _aggregate_projects(
            cfg,
            projects,
            labels=args.label or None,
            state=state,
            limit=limit,
            current_coordinate=current_coordinate,
        )
        return [t for g in groups for t in g.tickets]

    # In-repo: session scope with the all-tasks fallback, mirroring `list`.
    backend = _backend(cfg)
    session = _detect_session(cfg)
    no_session = session.source == "none"
    want_labels = set(args.label or [])
    try:
        session_tickets = (
            [] if no_session else backend.session_tickets(session.label, limit=limit)
        )
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    if no_session or not session_tickets:
        try:
            return backend.list(labels=args.label or None, state=state, limit=limit)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc

    tickets = session_tickets
    if state is not None:
        tickets = [t for t in tickets if t.state == state]
    if want_labels:
        tickets = [t for t in tickets if want_labels <= set(t.labels)]
    return tickets


def _gantt_width(args) -> int:
    """Resolve the bar-area width: an explicit --width wins; else fit the terminal, with a floor."""
    from . import gantt as _g

    if getattr(args, "width", None) is not None:
        return max(1, args.width)
    import shutil

    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    return _g.fit_width(cols)


def cmd_gantt(args: argparse.Namespace) -> int:
    """Render open tickets on a due-date timeline (read-only). Scoping mirrors `task list`.

    Human output is a box-drawing chart (one row per dated ticket, bars on a date axis, a status
    marker, and a clearly-labeled undated section), paged like `list`. `--json` emits the same
    timeline as a deterministic structure. No mutation — this is purely a view.
    """
    from datetime import date

    from . import gantt as _g

    cfg = _load(args)
    state = _parse_state(args.state) if args.state else None
    tickets = _gantt_tickets(args, cfg, state)
    today = date.today()
    chart = _g.layout(tickets, today, width=_gantt_width(args))

    if args.json:
        import json

        print(json.dumps(_g.to_json(chart, today), ensure_ascii=False, indent=2))
        return 0

    text = _g.render(chart, color=_c, today=today)
    _emit_list(args, [text])
    return 0


# ── burn (read-only remaining-over-time chart) ──────────────────────────────────


def _parse_iso_day(value: str | None, flag: str):
    """Parse ``YYYY-MM-DD`` or raise ``_UserError``. ``None``/empty stays ``None``."""
    from datetime import date as _date

    if not value:
        return None
    try:
        return _date.fromisoformat(value)
    except ValueError as exc:
        raise _UserError(
            f"invalid {flag} {value!r} (want YYYY-MM-DD).\n"
            f"  why: the burn window is a calendar date range.\n"
            f"  fix: pass {flag} 2026-06-01"
        ) from exc


def cmd_burn(args: argparse.Namespace) -> int:
    """Render remaining-open tickets over calendar days (read-only). Scoping mirrors gantt/list.

    Default state is None so OPEN+CLOSED both come back (burn needs closures). ``--json``
    emits the remaining/ideal series; ``--html`` prints a self-contained page (not paged).
    Empty charts exit 0.
    """
    from datetime import date

    from . import burn as _b

    cfg = _load(args)
    state = _parse_state(args.state) if args.state else None
    tickets = _gantt_tickets(args, cfg, state)
    today = date.today()
    since = _parse_iso_day(getattr(args, "since", None), "--since")
    until = _parse_iso_day(getattr(args, "until", None), "--until")
    if getattr(args, "width", None) is not None:
        width = max(1, args.width)
    else:
        import shutil

        width = _b.fit_width(shutil.get_terminal_size(fallback=(80, 24)).columns)
    chart = _b.layout(tickets, today, since=since, until=until, width=width)

    if args.json:
        import json

        print(json.dumps(_b.to_json(chart), ensure_ascii=False, indent=2))
        return 0
    if getattr(args, "html", False):
        print(_b.to_html(chart), end="")
        return 0

    text = _b.render(chart, color=_c)
    _emit_list(args, [text])
    return 0


# ── web (local HTML burn chart) ─────────────────────────────────────────────────


def cmd_web(args: argparse.Namespace) -> int:
    """Dispatch the local web-server lifecycle (start | stop | status | run)."""
    from . import web as _web

    cfg = _load(args)
    coordinate = _daemon_coordinate(cfg)
    port = getattr(args, "port", None) or _web.DEFAULT_PORT

    def load_tickets(*, include_all: bool = False):
        prev = bool(getattr(args, "all", False))
        args.all = prev or include_all
        # web parser has no --label/--limit; _gantt_tickets still reads them like `list`.
        if getattr(args, "label", None) is None:
            setattr(args, "label", [])
        if not hasattr(args, "limit"):
            setattr(args, "limit", None)
        try:
            return _gantt_tickets(args, cfg, None)
        finally:
            args.all = prev

    if args.action == "run":
        print(f"task web: serving {_web.url_for(port)}  (Ctrl-C to stop)")
        return _web.run(port, load_tickets, coordinate=coordinate)
    if args.action == "start":
        outcome, pid = _web.start(
            coordinate,
            port=port,
            cwd=str(cfg.repo_root),
            child_flags=_daemon_child_flags(args),
        )
        if outcome == "already-running":
            print(_dim(f"web already running (pid {pid}) for {coordinate}  {_web.url_for(port)}"))
        else:
            print(_ok(f"web started (pid {pid}) for {coordinate}  {_web.url_for(port)}"))
        return 0
    if args.action == "stop":
        outcome, pid = _web.stop(coordinate)
        if outcome == "stopped":
            print(_ok(f"web stopped (was pid {pid})"))
        elif outcome == "timeout":
            print(_warn(f"web (pid {pid}) did not exit in time; pid-file cleared"))
        elif outcome == "not-ours":
            print(_warn(f"pid {pid} is alive but is not task web; not signalled, pid-file cleared"))
        else:
            print(_dim("no running web server"))
        return 0

    # status
    pid_path = _web.paths_for(coordinate)
    status, pid = _web.pid_status_for(coordinate)
    url = _web.url_for(port)
    if getattr(args, "json", False):
        import json

        print(
            json.dumps(
                {
                    "status": status,
                    "pid": pid,
                    "coordinate": coordinate,
                    "url": url,
                    "pidfile": str(pid_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    line = {
        "running": _ok(f"running (pid {pid})  {url}"),
        "not-ours": _warn(f"recycled pid-file (pid {pid} is alive but is a foreign process)"),
        "stale": _warn(f"stale pid-file (pid {pid} is gone)"),
        "stopped": _dim("stopped"),
    }.get(status, _warn(f"{status} (pid {pid})"))
    print(f"web: {line}  for {coordinate}")
    print(_dim(f"  pidfile={pid_path}"))
    return 0


_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(10)}
    | {f"LPT{i}" for i in range(10)}
)


def _sanitize_attachment_basename(title: str) -> str:
    """Strip ``title`` (tracker-owned, untrusted data) down to a safe, bare filename component
    — no path traversal possible, on ANY platform, into ``--save-attachments DIR``.

    review finding: a naive ``title.replace(os.sep, "_")`` only strips the CURRENT platform's
    separator — on POSIX that's ``/`` (safe), but on Windows ``os.sep`` is ``\\`` while ``/`` is
    ALSO a valid separator there (``os.altsep``), so a title like ``../outside.txt`` would
    survive untouched and ``os.path.join(dest_dir, "../outside.txt")`` would escape ``dest_dir``.
    Fixed by hardcoding BOTH slash characters (not deriving from the running platform's
    ``os.sep``/``os.altsep`` — the title could theoretically be crafted on one platform and read
    back on another, e.g. a synced dotfiles-style config) and by rejecting the bare
    ``.``/``..``/empty results a slash-strip alone can still produce (a title of exactly ``".."``
    has no separator to strip, but would still resolve to ``dest_dir``'s PARENT as a path).

    Also strips NUL and other C0 control characters (review finding, 2nd round): a title like
    ``"\\0"`` isn't a traversal attempt, but NUL is illegal in a POSIX filename and ``os.link``
    raises ``ValueError`` (not ``OSError``) for it — a value ``_write_attachment_exclusive``'s
    caller specifically wasn't catching, so it would crash the command instead of being reported
    as one failed attachment.

    Also strips ``:`` (review finding, 3rd round — Windows drive-prefix traversal): a title with
    no slash at all, like ``"C:payload.py"``, survives the slash-stripping above untouched. On
    Windows that's a DRIVE-RELATIVE path — ``ntpath.join("downloads", "C:payload.py")`` sees the
    second component carries its own drive letter, decides the base ("downloads") doesn't apply,
    and collapses to ``"C:payload.py"`` alone (relative to whatever the CURRENT directory on
    drive C: happens to be), silently discarding the requested ``dest_dir``. Same hardcode-not-
    platform-derived reasoning as the slash fix above: a colon is stripped unconditionally, not
    only when ``sys.platform`` says Windows, since the malicious title could be read back on a
    different platform than it was written on. This also incidentally neutralizes NTFS
    Alternate-Data-Stream syntax (``"file.txt:hidden"``), a related colon-in-filename hazard.

    Also strips TRAILING dots/spaces, and rejects Windows-reserved device basenames (review
    finding, 4th round — same bug class, different corner): Windows silently drops trailing
    ``.``/`` `` characters from each path component when a file is actually created, so
    ``"shot.png"`` and ``"shot.png."`` are distinct strings to this sanitizer (and to
    ``_unique_attachment_filenames``'s case-fold dedup) but the SAME file on that filesystem —
    exactly the write-time ``FileExistsError`` the case-fold fix above was meant to head off,
    just via a different normalization Windows applies. Trimmed here so both attachments land on
    one deduped name instead of colliding at ``os.link()`` time. Separately, ``CON``, ``PRN``,
    ``NUL``, ``COM1``-``COM9``, ``LPT1``-``LPT9`` etc. refer to a DEVICE on Windows regardless of
    extension (``"CON.png"`` still means the CON device) — a title matching one is rejected the
    same way ``"."``/``".."`` are, so the caller falls back to the attachment id instead of a
    name that would fail to ever create a real file.
    """
    stripped = title.replace("/", "_").replace("\\", "_").replace(":", "_").strip()
    stripped = "".join(c for c in stripped if ord(c) >= 0x20 and c != "\x7f")
    # review finding (on the fix itself): `stripped.rstrip(". ")` below means a title consisting
    # ENTIRELY of dots/spaces (".", "..", "...", " . ", …) always collapses all the way to "" —
    # so an explicit "." / ".." check here would be dead code; a bare emptiness check covers
    # every one of those cases already.
    stripped = stripped.rstrip(". ")
    if stripped == "":
        return ""
    # review finding: `.split(".", 1)[0]` alone can still carry an INTERIOR trailing space —
    # "CON .png" splits to "CON " (the space is inside the first component, not at the very end
    # of `stripped`, so the `.rstrip(". ")` above never touches it) — but Windows strips
    # trailing space/dot from EACH path component before the device-name comparison, so "CON
    # .png" still resolves to the CON device on that filesystem. Re-strip the stem itself.
    if stripped.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_BASENAMES:
        return ""
    return stripped


def _unique_attachment_filenames(
    attachments, existing: frozenset = frozenset()
) -> dict:
    """``{id(attachment): filename}`` with a ``-2``/``-3``/… suffix on any REPEATED title
    (review finding: two attachments can share a title — e.g. two screenshots both named
    ``screenshot.png`` — and writing both to the same bare filename silently drops the first).

    Guards against a SECOND review finding on top of that: a naive "count occurrences of this
    title" scheme can itself collide when a later attachment's OWN title happens to equal an
    earlier DEDUPED name (titles in order ``screenshot.png``, ``screenshot-2.png``,
    ``screenshot.png`` would naively produce ``screenshot.png``, ``screenshot-2.png``,
    ``screenshot-2.png`` — the 2nd and 3rd collide). Fixed by tracking the set of names ACTUALLY
    assigned so far and bumping the suffix until the candidate is free, not just counting how
    many times the original title has been seen.

    ``existing`` (review finding, 3rd round): filenames ALREADY present in the destination
    directory before this run — a tracker-controlled attachment title matching a pre-existing
    file in the user's chosen directory must not silently overwrite it. Those names are seeded
    into the same "already taken" set, so a colliding title gets a ``-N`` suffix exactly like an
    in-batch collision, rather than reusing the exact target name.

    Collision tracking is CASE-FOLDED, not exact-string (review finding, 4th round): two titles
    like ``shot.png`` and ``SHOT.PNG`` are distinct Python strings, so an exact-string ``assigned``
    set lets both through with their original names — fine on a case-sensitive filesystem, but on
    a case-insensitive one (default macOS/Windows) the destination filesystem itself treats them
    as the SAME file, and the second attachment's ``os.link()`` at write time fails with
    ``FileExistsError`` even though this function reported it as a clean, unique name. Folding
    every membership check through ``.casefold()`` (Unicode-correct case folding, not just
    ``.lower()``) makes the in-memory reservation agree with what the filesystem will USUALLY
    enforce, catching the common case here as a normal ``-N`` suffix bump instead of a write-time
    failure. This is deliberately conservative even on a genuinely case-sensitive filesystem
    (Linux ext4/btrfs): worst case, two titles differing only by case get suffixed apart when
    they technically didn't have to.

    NOT a complete model of every filesystem's equivalence relation, though (review finding on
    the fix itself — this docstring previously overclaimed "never wrong"): ``.casefold()`` alone
    doesn't cover, e.g., Unicode-normalization-equivalent names on macOS APFS/HFS+ (NFC vs NFD
    forms of the same accented filename are the same file there, but distinct casefolded Python
    strings) or German ``ß``/``ss`` folding differences from the filesystem's own table. The
    write-time ``FileExistsError → reported failure`` path in ``_save_attachments`` (not this
    function) is still the LAST-resort backstop for whatever slips past this best-effort
    reservation — do not remove it as "made redundant" by this fix.

    Keyed by ``id()`` rather than the ``Attachment`` object itself: it's a plain (unfrozen)
    dataclass, so it's unhashable — this is only ever looked up against the SAME list within one
    ``_save_attachments`` call, so identity is a safe, cheap key.
    """
    import os

    assigned: set[str] = {name.casefold() for name in existing}
    names: dict = {}
    for a in attachments:
        # review finding: `a.id` is ALSO tracker-controlled data (the same trust boundary as
        # `a.title`) — falling back to it raw would bypass the sanitizer entirely and reopen the
        # exact traversal hole it exists to close.
        base = (
            _sanitize_attachment_basename(a.title)
            or _sanitize_attachment_basename(a.id)
            or "attachment"
        )
        stem, ext = os.path.splitext(base)
        candidate = base
        n = 1
        while candidate.casefold() in assigned:
            n += 1
            candidate = f"{stem}-{n}{ext}"
        assigned.add(candidate.casefold())
        names[id(a)] = candidate
    return names


def _write_attachment_exclusive(dest_dir: str, dest_path: str, data: bytes) -> None:
    """Publish ``data`` at ``dest_path`` atomically: write it COMPLETE to a temp file in
    ``dest_dir`` first, then hard-link the temp file to ``dest_path`` (fails with
    ``FileExistsError`` if anything — a file, a symlink, a race since the caller's
    pre-existing-name scan — already sits at that name; never overwrites, never follows a
    symlink), and always remove the temp file afterward.

    review finding: writing straight to ``dest_path`` with ``open(..., "xb")`` means a write
    that fails PARTWAY (e.g. disk-full mid-write) leaves a truncated, corrupt file already
    claiming ``dest_path``'s name — a retry would then treat that name as "already taken" and
    save the real bytes under a spurious ``-2`` name instead of the intended one. Publishing via
    a hard link only after the FULL write succeeded means a failed write leaves no trace at
    ``dest_path`` at all.
    """
    import os
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".attach-")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
        os.link(tmp_path, dest_path)
    finally:
        os.unlink(tmp_path)


def _is_trusted_attachment_host(url: str, trusted_hosts: frozenset[str]) -> bool:
    """Whether ``url``'s host is in the repo-configured ``trusted_attachment_hosts`` allowlist
    (``task.yaml`` — see ``config.LoadedConfig.trusted_attachment_hosts``). Used ONLY by
    ``_save_attachments`` to widen which URLs get auto-fetched beyond a backend's own native
    asset domain — a repo's own GitHub Pages host, say, isn't genuinely external, just
    unrecognized by the narrower native-asset check.

    Deliberately separate from ``is_native_attachment_url``: a trusted-but-non-native host is
    fetched PLAIN (no auth header) via the backend's own ``fetch_attachment_bytes``, which
    independently re-derives ``is_native_attachment_url`` before ever attaching credentials —
    widening THIS check must never widen which URLs get the tracker's auth header, or a
    repo-configured "trusted for auto-download" host would also start receiving the API key.
    ``fetch_attachment_bytes`` also always passes ``same_host_redirects_only=True`` regardless
    of that decision, so a trusted host's response redirecting off-host is refused rather than
    silently followed (review finding — an earlier version of this feature tied the redirect
    guard to the auth decision, which left every non-native fetch, trusted or not, free to
    redirect anywhere).

    Delegates to the shared :func:`~tasklib.backends.http.https_host_matches` (review finding,
    round 4: this check and ``is_native_attachment_url``'s were near-identical independently
    maintained copies that had ALREADY drifted apart — this one rejected a non-default port,
    the native one didn't, until both were folded onto the one shared predicate) — https-only,
    default-port-only (``None``/``443``), exact hostname match (an attachment url is
    tracker-controlled data, not something this process constructed, so a non-default port like
    ``hyperide.github.io:8443`` must not match either — a config-validated trusted-hosts entry
    can never itself carry a port, see ``config.validate``, but the ATTACHMENT url can add one).

    KNOWN LIMITATION (review finding, round 7 — stated plainly so this docstring never implies
    a stronger guarantee than it delivers): ``config.validate`` rejects IP literals (every
    getaddrinfo-legacy encoding) and known loopback aliases, but that is STRING validation, not
    a network-level control — it cannot and does not defend against a syntactically ordinary,
    genuinely resolvable DNS name that a wildcard-DNS-to-IP service (e.g. ``nip.io``,
    ``sslip.io``) maps to an internal address (``127.0.0.1.nip.io`` resolves to ``127.0.0.1``
    and is indistinguishable, as a STRING, from any other legitimate hostname), nor against DNS
    rebinding (a name resolving to a public address at config-validation time, then to an
    internal one at actual fetch time). Closing that class requires resolving the hostname and
    validating the ACTUAL IP address at connect time (and pinning it against a TOCTOU re-resolve
    mid-request) — a materially different, larger piece of work than a config-string validator,
    deliberately out of scope here and tracked as a separate follow-up rather than attempted as
    an ever-growing chain of string-pattern special cases.
    """
    if not trusted_hosts:
        return False
    from .backends.http import https_host_matches

    return https_host_matches(url, trusted_hosts)


def _save_attachments(
    backend, ticket, dest_dir: str, *, trusted_hosts: frozenset[str] = frozenset()
) -> list[str]:
    """Download each of ``ticket.attachments`` into ``dest_dir``, via the backend's own
    authenticated fetch (Linear's ``fetch_attachment_bytes`` — the actual "read" half of
    ``attachment_mode: native": a plain unauthenticated GET against a ``uploads.linear.app``
    asset 401s, see ``backends/linear.py``'s module docstring). Returns one status line per
    attachment (success or failure) for the caller to print; best-effort per-file, like
    ``_attach_screenshots`` on the write side — one bad attachment (a fetch failure OR a local
    filesystem error: permission denied, full disk) must not abort the rest.

    Backends that don't implement the method (GitHub — no native-attachment dichotomy there)
    are skipped with an explicit line rather than an ``AttributeError``.

    Fetches an attachment the backend confirms as its OWN native asset
    (``is_native_attachment_url``) OR whose host is in the caller-supplied ``trusted_hosts``
    allowlist (``config.LoadedConfig.trusted_attachment_hosts`` — see
    ``_is_trusted_attachment_host``): an attachment's ``url`` is tracker-owned data, not
    something this process constructed, so blindly fetching every attachment url would let a
    malicious/compromised ticket make this CLI probe arbitrary hosts (or, without the scheme
    check in `fetch_attachment_bytes`, read a local file). Anything else (an external,
    unrecognized ``link``-mode URL) is reported, not fetched — the URL is already right there in
    the printed line for the user to open directly. review finding (2nd round): a backend that
    implements ``fetch_attachment_bytes`` but NOT ``is_native_attachment_url`` must NOT default
    to "trust everything" (that inverts the whole point of the capability check) — it fails
    CLOSED (nothing beyond a configured trusted host gets auto-fetched) until that backend
    explicitly opts in.
    """
    fetch = getattr(backend, "fetch_attachment_bytes", None)
    if fetch is None:
        return [
            f"  (backend {backend.name!r} has no native attachment fetch — nothing to save)"
        ]
    is_native = getattr(backend, "is_native_attachment_url", None)

    import os

    from .backends import AmbiguousBackendError, BackendError

    try:
        os.makedirs(dest_dir, exist_ok=True)
        # review finding: reserve names already on disk too, so a tracker-controlled attachment
        # title matching a pre-existing file in the user's chosen directory doesn't overwrite it.
        existing = frozenset(os.listdir(dest_dir))
    except OSError as exc:
        return [f"  ✗ cannot create/list {dest_dir}: {exc}"]

    lines = []
    to_fetch = []
    for a in ticket.attachments:
        if (is_native is not None and is_native(a.url)) or _is_trusted_attachment_host(
            a.url, trusted_hosts
        ):
            to_fetch.append(a)
        else:
            lines.append(
                f"  (skipped, external/unconfirmed link — open directly) {a.title}: {a.url}"
            )
    # review finding: naming ALL attachments (including skipped ones) let a skipped external
    # attachment's title consume a name slot, giving a later NATIVE attachment a spurious `-2`
    # suffix for no reason the user could see. Only the ones actually being fetched compete for
    # names.
    filenames = _unique_attachment_filenames(to_fetch, existing)
    for a in to_fetch:
        dest_path = os.path.join(dest_dir, filenames[id(a)])
        try:
            data = fetch(a.url)
            _write_attachment_exclusive(dest_dir, dest_path, data)
        # ValueError alongside OSError: review finding — os.link/open can raise ValueError (not
        # OSError) for some illegal-filename byte sequences (a NUL that survived sanitization on
        # a platform-specific edge case, say); belt-and-suspenders on top of
        # `_sanitize_attachment_basename`'s own stripping, so a filesystem-level rejection is
        # still reported as one failed attachment rather than crashing the whole command.
        except (BackendError, AmbiguousBackendError, OSError, ValueError) as exc:
            lines.append(f"  ✗ {a.title}: {exc}")
            continue
        lines.append(f"  ✓ {a.title} -> {dest_path} ({len(data)} bytes)")
    # Deliberately NOT appending a truncation note here (review finding: it duplicated the one
    # `cmd_read` already prints in the attachments listing) — `ticket.attachments_truncated` is
    # the single source of truth, surfaced once in human output (cmd_read) and once in --json
    # (`_ticket_dict`); a caller of this function directly can check the same field itself.
    return lines


def cmd_read(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError
    from .render import render

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    save_dir = getattr(args, "save_attachments", None)
    saved_lines = (
        _save_attachments(
            backend, ticket, save_dir, trusted_hosts=cfg.trusted_attachment_hosts
        )
        if save_dir
        else []
    )

    if args.json:
        import json

        d = _ticket_dict(ticket)
        d["body"] = render(ticket)
        if save_dir:
            d["saved_attachments"] = saved_lines
        print(json.dumps(d, ensure_ascii=False, indent=2))
        _mark_ticket_read(cfg, backend, ticket)
        return 0
    print(_bold(f"{ticket.id}  {ticket.title}"))
    print(_dim(f"  state: {ticket.state.value}   {ticket.url}"))
    if ticket.labels:
        print(_dim(f"  labels: {', '.join(ticket.labels)}"))
    if ticket.attachments:
        # Real backend-native attachments (Linear "native" mode) — distinct from the body's
        # `## Screenshots` section, which is only the local ref that was REQUESTED at attach
        # time. `native` mode's own asset URL needs the same Authorization header any GraphQL
        # call uses to fetch (see backends/linear.py's module docstring); a raw browser open of
        # it will 401 like any other uploads.linear.app URL — that's expected, not a bug. Use
        # `--save-attachments DIR` to fetch the real bytes through the authenticated path.
        count_note = "+" if ticket.attachments_truncated else ""
        print(_dim(f"  attachments ({len(ticket.attachments)}{count_note}):"))
        for a in ticket.attachments:
            print(_dim(f"    - {a.title}: {a.url}"))
        if ticket.attachments_truncated:
            print(
                _dim(
                    "    (more attachments exist beyond the 250-item cap — not all are listed)"
                )
            )
    if saved_lines:
        print(_dim("  saved attachments:"))
        for line in saved_lines:
            print(_dim(line))
    print()
    print(render(ticket))
    # Record this session as having read the CURRENT ticket content (task-cli#119) — the read
    # half of the read-before-edit gate `cmd_change --what` enforces. Placed LAST, after
    # `_save_attachments` AND every print above have already succeeded (review finding, round
    # 3: NOT immediately after the fetch) — if `--save-attachments` raises (an unwritable dir)
    # or a print raises (e.g. a closed pipe), the command exits having shown the caller nothing
    # (or an incomplete view), and must not still mint a "read" credential for content that was
    # never actually, fully displayed. Best-effort (see read_tracker.record); a marker-write
    # failure here just means the next edit is refused as unread, the correct fail-closed
    # outcome, not a reason to fail this already-successful read.
    _mark_ticket_read(cfg, backend, ticket)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .backends import BackendError

    state = _parse_state(args.state) if args.state else None

    limit = _effective_limit(args)
    # Inside a repo: search the cwd's backend (current behavior). Outside one: search across
    # every known project and group the hits, so `find` is a true global op anywhere.
    if _current_project_overlay(cfg) is None:
        projects, current_coordinate = _known_projects(cfg)
        if not projects:
            raise _UserError(
                "no projects to search. You are outside a git repo and no projects are registered.\n"
                "  why: `task find` searches known projects; none are configured.\n"
                "  fix: add a `projects:` entry to ~/.config/task-cli/config.yaml, or run inside a repo."
            )
        groups = _search_projects(
            cfg,
            projects,
            args.query,
            state=state,
            limit=limit,
            current_coordinate=current_coordinate,
        )
        if args.json:
            _print_groups_json(groups)
            return 0
        _emit_list(args, [_format_groups(groups)])
        return 0

    backend = _backend(cfg)
    try:
        tickets = backend.search(args.query, state=state, limit=limit)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    if args.json:
        _print_tickets_json(tickets)
        return 0
    _emit_list(args, [_format_tickets(tickets)])
    return 0


def _search_projects(
    base_cfg, projects, query, *, state, limit, current_coordinate=None
) -> list:
    """Cross-project search — the search analogue of :func:`_aggregate_projects`."""
    return _query_projects(
        base_cfg,
        projects,
        lambda b: b.search(query, state=state, limit=limit),
        current_coordinate,
    )


def _record_post_merge_acceptance(ticket: Ticket, reason: str | None) -> None:
    """Record the merge-gate opt-out (`--post-merge-acceptance "<reason>"`) on the ticket —
    under `## Skipped gates`, the ticket's one audited escape-hatch record. `None` = flag not
    passed (leave as-is); a blank reason is refused (an opt-out without a reason is exactly the
    silent bypass task-cli#115 exists to stop)."""
    if reason is None:
        return
    from .policy import POST_MERGE_ACCEPTANCE

    text = reason.strip()
    if not text:
        raise _UserError(
            "--post-merge-acceptance needs a reason (why is acceptance only possible after the merge?)"
        )
    ticket.skips[POST_MERGE_ACCEPTANCE] = text


def cmd_change(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    # legality FIRST, before any edit mutates the fetched ticket: an illegal `change --done` on a
    # cancelled/already-done ticket must refuse without touching title/labels/screenshots/skips —
    # so a backend that hands back a live/cached Ticket object isn't left dirtied (#10).
    if args.done:
        validate_transition(ticket.state, State.DONE, force=args.force)

    # `--deployed` is a plain guard, NOT a `transitions.py` state: `deployed` is orthogonal to
    # `state` (a ticket can be `done` then reopened to `in-progress` for a regression fix while
    # `deployed` stays True, recording that a deploy DID happen historically — see
    # `Ticket.deployed`'s docstring). Checked against the state AS FETCHED, before any mutation
    # below (including this same command's own `--done`) — a combined `--done --deployed` call
    # on a not-yet-done ticket refuses too, same as every other pre-mutation gate here.
    if args.deployed is not None and ticket.state is not State.DONE:
        raise _UserError(
            f"cannot mark {ticket.id} deployed: state is `{ticket.state.value}`, not `done`\n"
            "  why: `deployed` records a PAST production deploy of ACCEPTED work — marking "
            "undone/unaccepted work deployed would misrepresent what actually shipped\n"
            f"  fix: close the ticket first (`task done {ticket.id}`), then re-run "
            f"`task change {ticket.id} --deployed \"...\"`"
        )

    # A --skip-msgref-quote passed on THIS edit turns expansion off for this command — the skip
    # governs the quote machinery, not only the gate (review finding), so a waived false positive
    # isn't expanded into an unrelated Telegram quote. Gate on THIS command's skips only, NOT a
    # skip persisted from a past create: a historical waiver must not permanently disable
    # expansion of a genuinely NEW reference added later (review finding). The enforcement gate
    # below still honours the persisted skip (it lives in ticket.skips) — only expansion is scoped
    # to the current command's intent.
    new_skips = _collect_skips(args)
    expand = _msgref_expand_enabled(new_skips, cfg)
    # Load tg-cli's local history ONCE for this command, and only if a touched field actually
    # names a reference (see _load_msgref_history_if_needed) — a metadata-only edit (--due/
    # --label), an edit naming no tg#<id>, or a msgref-quote-waived edit never touches the
    # (potentially large) history log.
    history = (
        _load_msgref_history_if_needed(
            args.what or "", args.why or "", args.impact or "", args.if_not_done or ""
        )
        if expand
        else None
    )
    if args.title:
        ticket.title = args.title
    if args.what:
        ticket.what = _expand_msgrefs(args.what, history) if expand else args.what
    if args.why:
        ticket.why = _expand_msgrefs(args.why, history) if expand else args.why
    if args.impact:
        ticket.user_impact = (
            _expand_msgrefs(args.impact, history) if expand else args.impact
        )
    if args.if_not_done:
        ticket.cost_of_inaction = (
            _expand_msgrefs(args.if_not_done, history) if expand else args.if_not_done
        )
    # Acceptance criteria are NEVER msgref-expanded (see the cmd_create comment on why: the
    # single-line checkbox format can't carry a multi-line quote), so this dedup stays plain
    # equality against the stored (always-raw) text — unchanged from before this feature.
    existing_texts = {c.text for c in ticket.acceptance}
    for crit in args.acceptance:
        if crit not in existing_texts:
            ticket.acceptance.append(Criterion(text=crit))
            existing_texts.add(crit)
    if getattr(args, "due", None) is not None:
        # --due passed (incl. --due "" to clear): validate then set. Not passed → leave as-is.
        ticket.due = _normalize_due(args.due)
    new_shots = [
        Screenshot(ref=path, kind="implementation") for path in args.screenshot
    ]
    ticket.screenshots.extend(new_shots)
    for label in args.label:
        if label not in ticket.labels:
            ticket.labels.append(label)
    ticket.skips.update(new_skips)
    _record_post_merge_acceptance(ticket, args.post_merge_acceptance)
    if args.deployed is not None:
        # Legality was already checked above (ticket.state is State.DONE) before any mutation —
        # this only ever runs on a refusal-free path.
        from datetime import datetime, timezone

        # `## Deploy` is rendered/parsed as a SINGLE line (render.py's `_render_deploy`/
        # `_DEPLOY_LINE_RE`, deliberately — see their docstrings), unlike every other free-text
        # field, which round-trips multi-line content through `_escape_embedded_headings`. A
        # note carrying a literal newline would silently break that single-line parse on the
        # NEXT fetch→parse cycle (review finding: `_parse_deploy` returns `(False, "", "")`,
        # permanently erasing `deployed`/`deployed_at` with no error anywhere) and a note
        # carrying a line starting `## ` would forge a phantom section boundary and corrupt an
        # unrelated field on the next render (the same class of bug task-cli#181 already fixed
        # for What/Why/Impact/Cost — Deploy has no multi-line escaping to fall back on, so the
        # fix here is to never let the note contain a newline in the first place). Collapsing
        # internal newlines to spaces keeps the note itself intact and human-readable while
        # making the single-line format's own invariant impossible to violate.
        ticket.deployed = True
        ticket.deployed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ticket.deployed_note = " ".join(args.deployed.splitlines())

    # An edit that TOUCHES a text field is re-scanned for bare references (rule 1) and re-graded
    # on impact quality (rule 5) — whether or not this same command ALSO closes the ticket, so
    # `change --what "…HYP-789…" --done` cannot smuggle a bare reference (or a thinned impact)
    # past the edit gates on its way to DONE. The scan is conservative (it re-validates the whole
    # body, not only the new bytes); a pure metadata edit (--due/--label) touches no scanned
    # field and is exempt entirely (see _enforce_edit_or_die).
    touched_text = any(
        [
            args.title,
            args.what,
            args.why,
            args.impact,
            args.if_not_done,
            args.acceptance,
        ]
    )
    # The read-before-edit gate (task-cli#119) fires ONLY when `--what` itself is being set — the
    # ticket's core description, which is what Alex asked to stop losing to a blind overwrite.
    # `--why`/`--impact`/`--if-not-done` are NOT gated: unlike `--what`, they aren't the field
    # whose loss prompted this (see task-cli#119's ticket body for the scoping call).
    read_violation = None
    read_cfg_e = _enforce_config(cfg)
    # `read_session` is threaded down to the post-edit `_mark_ticket_read` call below too (review
    # finding, task-cli#119) — the session cannot change mid-process, so detecting it twice in one
    # `change --what` (once here, once there) is pure waste; `None` here (gate disabled, or no
    # `--what`) just means that later call falls back to detecting it itself, unchanged.
    read_session = None
    # Check `.read_before_edit` BEFORE resolving the session/coordinate: both can be real work —
    # `_detect_session` falls back to a `git rev-parse` subprocess (5s timeout) when neither
    # `$TASK_SESSION` nor tmux is set — and `_read_before_edit_violation` already no-ops on a
    # disabled gate internally, so a repo with `enforce.read_before_edit: false` must not still
    # pay that cost on every `--what` edit just to discover the gate is off.
    if args.what and read_cfg_e.read_before_edit:
        read_session = _detect_session(cfg)
        read_violation = _read_before_edit_violation(
            ticket.id,
            read_session.id,
            _safe_verified_coordinate(cfg, backend, ticket),
            read_cfg_e,
        )
    from .policy import GATE_READ_BEFORE_EDIT

    _enforce_edit_or_die(
        ticket,
        cfg,
        check_links=touched_text,
        check_impact=bool(args.impact),
        check_title=bool(args.title),
        read_violation=read_violation,
        # `GATE_READ_BEFORE_EDIT`, not a bare "read-before-edit" literal (review finding,
        # task-cli#119): the canonical gate name has one home, `policy.py`, matching every
        # other gate-name comparison in this file.
        read_waived=GATE_READ_BEFORE_EDIT in new_skips,
    )

    if args.done:
        from .policy import Phase

        # enforce the close gates BEFORE the state flip so a refusal doesn't dirty the fetched
        # ticket (#10).
        _enforce_or_die(ticket, cfg, Phase.DONE)
        ticket.state = State.DONE

    # tg#16794/review-cli#484: attach (and swap `.ref` to the durable url) BEFORE persisting,
    # so render() embeds a working permanent link instead of the raw local path — mirrors
    # cmd_attach and cmd_check's _ProofAttacher. Uses `ticket.id` (an edit's id is stable).
    _attach_screenshots(backend, ticket.id, new_shots)
    try:
        updated = backend.update(ticket)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    _record_session_touch(cfg, backend, updated)
    # task-cli#119 review finding: a successful `--what` edit that PASSED (or was waived on)
    # the read-before-edit gate must itself count as reading the ticket going forward —
    # otherwise `read #1` at T0, `change --what` at T0+10m (passes), `change --what` again at
    # T0+40m refuses as "blind" even though this session just WROTE the description it would be
    # overwriting. GATED ON `args.what` (review finding, second round): marking read on EVERY
    # successful change — including a metadata-only `--due`/`--label` edit that never touched
    # or displayed the description — would hand out a read credential the session never earned,
    # opening a two-command bypass (metadata edit first to mint a fresh marker, unrelated blind
    # `--what` edit next, still within the TTL). Only a command that actually set `--what` (and
    # therefore supplied/knows the new current text) may refresh the marker.
    if args.what:
        _mark_ticket_read(cfg, backend, updated, session=read_session)
    log_event(
        "ticket.changed", ticket_id=updated.id, backend=cfg.backend, closed=args.done
    )
    _notify_mutation(cfg, updated, "done" if args.done else "changed")
    if args.json:
        import json

        print(json.dumps(_ticket_dict(updated), ensure_ascii=False, indent=2))
    else:
        print(_ok(f"updated {updated.id}  {updated.url}"))
    return 0



def cmd_comment(args: argparse.Namespace) -> int:
    """Append a comment to a ticket through its configured backend (UB-25).

    The supported, backend-agnostic way to leave a note on a GitHub/Forgejo/Linear/Tasks ticket
    — agents use this instead of a raw forge CLI or API write. The text is posted verbatim (it
    is Markdown, never re-rendered), and never touches the ticket body.
    """
    body = _comment_body(args)
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import AmbiguousBackendError, BackendError

    try:
        backend.comment(args.id, body)
    except AmbiguousBackendError as exc:
        print(_err(f"ambiguous: {exc}"))
        print(_warn(f"  the comment may already be posted — check {args.id} before retrying."))
        return exc.exit_code
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    if args.json:
        import json

        print(json.dumps({"id": args.id, "commented": True}, ensure_ascii=False))
    else:
        print(f"commented on {args.id}")
    return 0


def _comment_body(args: argparse.Namespace) -> str:
    """The comment text from ``--body-file`` (``-`` = stdin) or the positional words; an empty
    or whitespace-only comment is refused before any backend is built."""
    if args.body_file and args.text:
        raise _UserError("pass the comment either as text or with --body-file, not both")
    if args.body_file:
        try:
            if args.body_file == "-":
                body = sys.stdin.read()
            else:
                body = Path(args.body_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _UserError(f"cannot read --body-file {args.body_file}: {exc}") from exc
    else:
        body = " ".join(args.text)
    if not body.strip():
        raise _UserError(
            "the comment is empty\n"
            "  fix: pass the text (`task comment <id> \"note\"`) or --body-file PATH"
        )
    return body


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach screenshot path(s) as implementation proof.

    The dedicated verb agents reach for (task-cli#167 / ask 017): `task attach <id>
    shot.png` instead of having to know `task change <id> --screenshot shot.png`.
    Same durable path as change: body Screenshots section + backend.attach. Does not
    close the ticket and does not run close gates. Capture itself is out of scope —
    pass a path already taken via Playwright/agent-browser, never screencapture.
    """
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .backends import AmbiguousBackendError, EXIT_AMBIGUOUS
    from .logging import log_event, redact
    from .policy import is_http_url

    for path in args.path:
        if is_http_url(path):
            continue
        if not Path(path).is_file():
            raise _UserError(
                f"cannot attach {redact(path)} — not a file and not an http(s) URL\n"
                "  why: task attach needs a Playwright/agent-browser capture "
                "(or a hosted URL); a missing path is not proof\n"
                "  fix: pass an existing image path, or a URL already hosting "
                "the screenshot"
            )

    # review finding (PR #184): a dict keyed by the raw path collapses two IDENTICAL args.path
    # entries (e.g. `task attach #1 shot.png shot.png`) onto whichever upload finished last —
    # each call to backend.attach() still genuinely uploads/returns its own distinct durable url
    # in native mode, so a dict silently drops the first upload's url (an orphaned asset) and
    # persists the SAME (second) url for both Screenshot entries. Pair each attach with its own
    # durable ref POSITIONALLY instead, so duplicate paths keep their own distinct results.
    attached: list[tuple[str, str]] = []
    fail: BackendError | None = None
    ambiguous: AmbiguousBackendError | None = None
    for path in args.path:
        try:
            attached.append((path, backend.attach(ticket.id, path)))
        except AmbiguousBackendError as exc:
            ambiguous = exc
            break
        except BackendError as exc:
            fail = exc
            break

    # tg#16794/review-cli#484: use the DURABLE ref backend.attach() returned, not the raw path
    # — otherwise the ticket body's Screenshots section embeds a local path no other viewer can
    # ever resolve (mirrors _attach_screenshots' identical fix for change/done/create).
    new_shots = [
        Screenshot(ref=durable_ref, kind="implementation") for _path, durable_ref in attached
    ]
    updated = ticket
    if new_shots:
        ticket.screenshots.extend(new_shots)
        try:
            updated = backend.update(ticket)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        _record_session_touch(cfg, backend, updated)
        log_event(
            "ticket.attached",
            ticket_id=updated.id,
            backend=cfg.backend,
            count=len(new_shots),
        )
        _notify_mutation(cfg, updated, "attached")

    if ambiguous is not None:
        print(_err(f"ambiguous: {ambiguous}"))
        print(
            _warn(
                "  an attach may already have landed — run `task read "
                f"{ticket.id}` before retrying, to avoid a duplicate."
            )
        )
        return EXIT_AMBIGUOUS
    if fail is not None:
        raise _UserError(str(fail))

    if args.json:
        import json

        print(json.dumps(_ticket_dict(updated), ensure_ascii=False, indent=2))
    else:
        names = ", ".join(redact(s.ref) for s in new_shots)
        print(_ok(f"attached {names} to {updated.id}  {updated.url}"))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError

    if not args.new_state:
        try:
            ticket = backend.get(args.id)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        if args.json:
            import json

            print(json.dumps(_ticket_dict(ticket), ensure_ascii=False, indent=2))
            return 0
        print(f"{ticket.id} {_dim('[' + ticket.state.value + ']')} {ticket.title}")
        return 0

    new_state = _parse_state(args.new_state)
    skips = _collect_skips(args)
    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    # legality first: status guards EVERY transition (not only close-to-done) so a cancelled
    # ticket can't be silently revived and a same-state re-write is rejected (#10).
    validate_transition(ticket.state, new_state, force=args.force)
    ticket.skips.update(skips)
    try:
        if new_state is State.DONE:
            from .policy import Phase

            # enforce BEFORE the state flip so a refusal doesn't dirty the fetched ticket (#10).
            _enforce_or_die(ticket, cfg, Phase.DONE)
            ticket.state = new_state
            # persist the MUTATED ticket (carries the recorded skip justifications) — using
            # transition() here would re-fetch and drop ticket.skips, silently losing the
            # audit section a gate was waived under. update() writes the body with the skips.
            updated = backend.update(ticket)
        elif skips:
            # a skip recorded on a non-done transition is still an auditable decision → persist.
            ticket.state = new_state
            updated = backend.update(ticket)
        else:
            updated = backend.transition(args.id, new_state)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    from .logging import log_event

    _record_session_touch(cfg, backend, updated)
    log_event("ticket.transition", ticket_id=updated.id, state=new_state.value)
    _notify_mutation(cfg, updated, "done" if new_state is State.DONE else "changed")
    if args.json:
        # --json promises machine-readable stdout: the updated ticket, nothing else
        import json

        print(json.dumps(_ticket_dict(updated), ensure_ascii=False, indent=2))
        return 0
    print(_ok(f"{updated.id} → {new_state.value}"))
    if new_state in _RECENT_PRIORITY_STATES:
        _print_session_attention_after_mutation(
            args,
            cfg,
            backend,
            current_id=updated.id,
        )
    return 0


def cmd_done(args: argparse.Namespace) -> int:
    """Close a ticket by id — the dedicated close verb. Runs the on-done gates (and the
    `--skip-<gate>` hatches), and accepts `--screenshot` for the implementation proof a
    UI ticket needs to close. The same close path `change --done` takes, minus the edits.
    """
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import BackendError
    from .policy import Phase

    try:
        ticket = backend.get(args.id)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    # legality first: refuse re-closing a cancelled/already-done ticket before any mutation (#10).
    validate_transition(ticket.state, State.DONE, force=args.force)
    new_shots = [
        Screenshot(ref=path, kind="implementation") for path in args.screenshot
    ]
    ticket.screenshots.extend(new_shots)
    ticket.skips.update(_collect_skips(args))
    # enforce BEFORE mutating state (the DONE phase is passed explicitly, not read off the
    # ticket), so a gate refusal leaves the fetched ticket undirtied — same rule as #10.
    _enforce_or_die(ticket, cfg, Phase.DONE)
    ticket.state = State.DONE

    # tg#16794/review-cli#484 (review finding, PR #184): attach BEFORE persisting, same as
    # cmd_change — `task done --screenshot` is a primary close-with-proof path, and attaching
    # AFTER update() (the old order) means the `.ref` swap to the durable url never reaches the
    # persisted body, leaving the closing ticket's own implementation proof broken for exactly
    # the reason this fix exists. Uses `ticket.id` (an existing ticket's id is stable).
    _attach_screenshots(backend, ticket.id, new_shots)
    try:
        updated = backend.update(ticket)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    from .logging import log_event

    _record_session_touch(cfg, backend, updated)
    log_event("ticket.transition", ticket_id=updated.id, state=State.DONE.value)
    _notify_mutation(cfg, updated, "done")
    print(_ok(f"{updated.id} → {State.DONE.value}"))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Check acceptance criteria off — but only WITH a visual proof (rule 3).

    A checkbox cannot be ticked without a screenshot/image backing it (``--proof <path>``).
    When a visual proof is genuinely impossible, ``--force "<reason>"`` records the reason on
    the criterion (audited). The checked state + proof live in the body, so the on-done gate
    (rule 2) — and the pre-merge `task gate` — can refuse while any criterion is still open.

    Several criteria at once (task-cli#115): ``task check <id> 1 2 3 --proof a --proof b
    --proof c`` (one proof per criterion, in order) or ``task check <id> --all --proof shot.png``
    (one shared proof). The single-selector form keeps its original contract: the first
    ``--proof`` backs the box, any further ones are attached as implementation screenshots.
    """
    cfg = _load(args)
    backend = _backend_for_id(cfg, args.id)
    from .backends import AmbiguousBackendError, BackendError

    try:
        ticket = backend.get(args.id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc

    crits = _select_criteria(ticket, args.selector, args.all_criteria)
    proofs = list(args.proof or [])
    # A duplicate selector (`1 1 2`) collapsing to fewer criteria than selectors given, combined
    # with several --proof values, is positionally ambiguous: which proof was meant for the
    # REPEATED selector? (review finding, task-cli PR#118 round 5, Opus — `1 1 2 --proof a
    # --proof b` silently paired `b` onto criterion 2 instead of the second `1`, durably
    # persisting the wrong proof on the wrong box with no error.) Refuse rather than guess.
    if len(proofs) > 1 and not args.all_criteria and len(crits) < len(args.selector):
        raise _UserError(
            f"{len(args.selector)} selectors given but only {len(crits)} distinct criteria — "
            "a duplicate selector combined with several --proof values is ambiguous (which "
            "proof was meant for the repeat?). Give one --proof per DISTINCT criterion, in "
            "order, with no repeated selectors."
        )
    force_reason = getattr(args, "force_reason", None)
    if not proofs and not force_reason:
        what = repr(crits[0].text) if len(crits) == 1 else f"{len(crits)} criteria"
        raise _UserError(
            f"refusing to check {what}: a visual proof is required.\n"
            "  why: a criterion is only 'done' when there's a screenshot/image proving it.\n"
            '  fix: task check <id> <selector> --proof <path>   (or --force "<reason>" if a '
            "proof is genuinely impossible)."
        )
    per_criterion = _plan_proofs(crits, proofs)
    # Positions are captured BEFORE the write: `backend.update` returns a fresh Ticket, so
    # the Criterion objects in `crits` are not the ones in the updated acceptance list.
    indexes = [_criterion_index(ticket, c) for c in crits]

    attach = _ProofAttacher(backend, ticket.id)
    extra_shots: list[Screenshot] = []
    for crit, crit_proofs in zip(crits, per_criterion):
        _apply_check(attach, crit, crit_proofs, force_reason)
        extra_shots.extend(
            Screenshot(ref=p, kind="implementation") for p in crit_proofs[1:]
        )
    try:
        updated = backend.update(ticket)
        _attach_screenshots(backend, updated.id, extra_shots)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    _record_session_touch(cfg, backend, updated)
    _log_checked(updated, indexes, forced=bool(force_reason and not proofs))
    print(_ok(_checked_summary(updated, indexes, force_reason if not proofs else None)))
    return 0


def _select_criteria(ticket: Ticket, selectors: list[str], all_criteria: bool) -> list[Criterion]:
    """Resolve the ``check`` selection: ``--all`` (every criterion), or one-or-more selectors
    (each a 1-based index or a unique text substring, via :func:`_select_criterion`). Exactly
    one of the two forms; duplicates collapse (``1 1`` is one criterion)."""
    if all_criteria and selectors:
        raise _UserError("pass either --all or explicit selectors, not both")
    if all_criteria:
        if not ticket.acceptance:
            raise _UserError(f"ticket {ticket.id or '(unknown)'} has no acceptance criteria to check")
        return list(ticket.acceptance)
    if not selectors:
        raise _UserError(
            "which criteria? give one or more selectors (a 1-based index or a text "
            "substring), or --all for every criterion"
        )
    chosen: list[Criterion] = []
    for sel in selectors:
        crit = _select_criterion(ticket, sel)
        if not any(crit is c for c in chosen):
            chosen.append(crit)
    return chosen


def _plan_proofs(crits: list[Criterion], proofs: list[str]) -> list[list[str]]:
    """Which proofs back which criterion. One criterion: ALL proofs (legacy contract — the
    first backs the box, the rest are attached as screenshots). Several: exactly one per
    criterion in order, or a single shared proof, or none (``--force``) — anything else is
    ambiguous and refused before anything is persisted."""
    if len(crits) == 1:
        return [proofs]
    if not proofs:
        return [[] for _ in crits]
    if len(proofs) == 1:
        return [[proofs[0]] for _ in crits]
    if len(proofs) == len(crits):
        return [[p] for p in proofs]
    raise _UserError(
        f"{len(crits)} criteria selected but {len(proofs)} proofs given — pass one --proof "
        "per criterion (in selector order) or a single shared --proof"
    )


class _ProofAttacher:
    """Turn a raw proof ref into the DURABLE ref the ticket body should carry — once per
    distinct ref, so a proof shared by several criteria is uploaded a single time.

    HYP-1355: a raw local path (or an unauthenticated-on-fetch backend URL) embedded directly in
    the criterion text is never actually viewable by anyone reading the ticket — ``attach()`` is
    the backend's contract for turning a ref into something durable (a real hosted URL, or a
    registered Linear attachment echoing its own asset URL back). Best-effort: if the durable
    attach itself fails, fall back to the raw ref rather than losing the proof entirely
    (matches ``_attach_screenshots``' contract) — logged, because a silent fallback means the
    printed proof note and the actually-embedded ref can quietly disagree.
    """

    def __init__(self, backend, ticket_id: str) -> None:
        self._backend = backend
        self._ticket_id = ticket_id
        self._durable: dict[str, str] = {}

    def __call__(self, ref: str) -> str:
        if ref in self._durable:
            return self._durable[ref]
        from .backends import AmbiguousBackendError, BackendError
        from .logging import log_event

        try:
            durable = self._backend.attach(self._ticket_id, ref)
        except (BackendError, AmbiguousBackendError) as exc:
            durable = ref
            log_event(
                "criterion.proof_attach_failed", ticket_id=self._ticket_id, error=str(exc)
            )
        self._durable[ref] = durable
        return durable


def _apply_check(attach: _ProofAttacher, crit: Criterion, proofs: list[str], force_reason: str | None) -> None:
    """Tick one criterion in memory: with its (durably attached) first proof, or forced."""
    crit.checked = True
    if proofs:
        crit.proof = attach(proofs[0])
        crit.force_reason = ""
    else:
        crit.proof = ""
        crit.force_reason = force_reason or ""


def _log_checked(ticket: Ticket, indexes: list[int], *, forced: bool) -> None:
    from .logging import log_event

    for _ in indexes:
        log_event("criterion.checked", ticket_id=ticket.id, forced=forced)


def _checked_summary(ticket: Ticket, indexes: list[int], force_reason: str | None) -> str:
    """The one-line result, read back from the UPDATED ticket by 1-based position (see
    `cmd_check`: the pre-write Criterion objects are stale after `backend.update`). `crit.proof`,
    not the raw path the caller typed: when the durable attach succeeded the embedded ref may
    differ, and the note must match the ticket body."""
    remaining = len(ticket.unmet_criteria())
    crits = [ticket.acceptance[i - 1] for i in indexes]
    if len(crits) == 1:
        crit = crits[0]
        proof_note = f"forced ({force_reason})" if force_reason else f"proof {crit.proof}"
        return f"checked {crit.text!r} on {ticket.id}  ({proof_note}; {remaining} left)"
    notes = ", ".join(
        f"[{i}] " + (f"forced ({force_reason})" if force_reason else f"proof {c.proof}")
        for i, c in zip(indexes, crits)
    )
    return f"checked {len(crits)} criteria on {ticket.id}  ({notes}; {remaining} left)"


def _criterion_index(ticket: Ticket, crit: Criterion) -> int:
    """The 1-based position of ``crit`` in the ticket's FULL acceptance list — the number
    `task check <id> <n>` expects. By identity, not equality: two criteria can share text."""
    return next(i for i, c in enumerate(ticket.acceptance, start=1) if c is crit)


def _select_criterion(ticket: Ticket, selector: str) -> Criterion:
    """Resolve a criterion by 1-based index or by a unique text substring; raise on miss/ambiguity."""
    crits = ticket.acceptance
    if not crits:
        raise _UserError(
            f"ticket {ticket.id or '(unknown)'} has no acceptance criteria to check"
        )
    sel = selector.strip()
    if sel.isdigit():
        idx = int(sel)
        if not 1 <= idx <= len(crits):
            raise _UserError(f"criterion index {idx} out of range (1..{len(crits)})")
        return crits[idx - 1]
    matches = [c for c in crits if sel.lower() in c.text.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _UserError(
            f"no acceptance criterion matches {selector!r}; use an index (1..{len(crits)})"
        )
    raise _UserError(
        f"{len(matches)} criteria match {selector!r}; use an index (1..{len(crits)}) to disambiguate"
    )


# ── audit (the reconciliation backstop) ─────────────────────────────────────────────
# A Done ticket that never passed the close gates (HYP-1347: closed via Linear's own PR-merge
# automation, bypassing task-cli entirely). See tasklib/audit.py's module docstring for the pure
# logic + the "a finding is not proof of bypass" caveat, and README's "Audit" section for the
# full incident, the recommended report-only cron wiring, and known limitations.


def cmd_audit(args: argparse.Namespace) -> int:
    """Find Done tickets that do not currently satisfy the close gates, and optionally revert
    them. See the module comment above and tasklib/audit.py for what a finding does and does
    NOT prove."""
    if args.limit <= 0:
        raise _UserError(f"--limit must be a positive integer (got {args.limit})")
    cfg = _load(args)
    tickets, backend = _audit_fetch_targets(cfg, args)
    if args.id and tickets[0].state is not State.DONE:
        _print_audit_not_done(tickets[0], as_json=args.json)
        return 0

    cfg_e = _enforce_config(cfg)
    resolvable_ids = _audit_resolvable_ids(cfg_e, tickets)

    from .audit import audit_done_tickets

    findings = audit_done_tickets(tickets, cfg_e, resolvable_ids)
    reverted, revert_errors = (
        _audit_revert(cfg, backend, findings, cfg_e, resolvable_ids)
        if findings and args.revert
        else ([], [])
    )
    _print_audit_report(findings, reverted, revert_errors, scanned=len(tickets), as_json=args.json)
    return 1 if findings else 0


def _print_audit_not_done(ticket: Ticket, *, as_json: bool) -> None:
    """The by-id early return when the fetched ticket isn't Done — kept in the SAME
    ``--json``-aware shape :func:`_print_audit_report` uses (``scanned``/``findings``/
    ``reverted``/``revert_errors``), so a machine consumer never has to special-case this path
    (review finding: the earlier version printed plain text here even under ``--json``)."""
    if as_json:
        import json

        payload = {
            "scanned": 0,
            "findings": [],
            "reverted": [],
            "revert_errors": [],
            "note": f"{ticket.id} is not Done (state: {ticket.state.value}) — nothing to audit",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(_ok(f"{ticket.id} is not Done (state: {ticket.state.value}) — nothing to audit"))


def _audit_fetch_targets(cfg, args: argparse.Namespace) -> tuple[list[Ticket], Any]:
    """The tickets to audit + the backend they came from: one ticket (``args.id``) or the
    most-recent Done tickets up to ``args.limit``.

    Catches ``AmbiguousBackendError`` alongside ``BackendError``, not just the latter: both
    calls here are READ-ONLY, so an ambiguous outcome (the response was lost after a 2xx) is
    harmless — nothing was mutated either way, the same collapse ``_session_dedup_candidates``
    already makes for its own read-only lookup. Without this, a dropped read crashed `task
    audit --json` with a raw traceback instead of the documented JSON shape (review finding) —
    especially bad for the recommended report-only cron usage.
    """
    from .backends import AmbiguousBackendError, BackendError

    if args.id:
        backend = _backend_for_id(cfg, args.id)
        try:
            return [backend.get(args.id)], backend
        except (BackendError, AmbiguousBackendError) as exc:
            raise _UserError(str(exc)) from exc
    backend = _backend(cfg)
    try:
        return backend.list(state=State.DONE, limit=args.limit), backend
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc


def _audit_resolvable_ids(cfg_e, tickets: list[Ticket]) -> set[int] | None:
    """The ``tg#<id>`` ids resolvable in local tg-cli history, for the WHOLE audited batch.

    Mirrors ``_msgref_resolvable_ids`` (used by the single-ticket close paths) but computed
    ONCE across every ticket in the batch rather than per-ticket — ``load_history()`` reads
    the same local log regardless of which ticket asks, so there is nothing to gain from
    re-loading it per ticket. Without this, `audit` diverged from what `task done` actually
    checks: a Done ticket with an unquoted-but-locally-resolvable ``tg#<id>`` would pass the
    audit (the gate degrades to a warning when it can't prove resolvability) even though `task
    done` would have refused it (review finding). Returns ``None`` when nothing in the batch
    could possibly need it — the gate is disabled, or no ticket carries an unquoted reference —
    so `check_done` treats it exactly like every other caller that skips loading history.
    """
    from .policy import unquoted_body_ids

    if not cfg_e.msgref_quote:
        return None
    if not any(unquoted_body_ids(t) for t in tickets):
        return None
    from .msgrefs import load_history

    return set(load_history())


def _audit_revert(
    cfg, backend, findings: list, cfg_e, resolvable_ids: set[int] | None
) -> tuple[list[str], list[tuple[str, str]]]:
    """Move every STILL-noncompliant ticket DONE -> IN_REVIEW, with a best-effort explanation.

    Re-fetches and re-checks each ticket immediately before acting — NOT the snapshot the scan
    saw. Between the scan and the revert, someone may have fixed the ticket (checked the
    remaining criteria) or moved it off Done entirely; acting on the stale snapshot would
    silently reopen work that no longer needs it (review finding — a real TOCTOU gap in the
    original version of this function). Transitions BEFORE commenting: a comment claiming
    "reverted" must never be posted on a ticket the transition call actually failed to move.
    Best-effort per ticket: one backend failure must not abort the rest of the batch — the
    audit is a periodic sweep, and losing the whole run to one bad ticket would hide every
    other finding until the next run.

    RESIDUAL RACE (accepted, not fixed here): re-check-then-write is not atomic — neither
    backend's protocol supports a conditional/versioned update (no ETag/CAS), so a fix landing
    in the gap between the re-check above and the ``transition()`` call below could still be
    overwritten. The window shrank from "the whole scan-to-revert duration" to one HTTP
    round-trip; closing it fully needs backend-level optimistic concurrency, a separate, larger
    change to the ``TicketBackend`` protocol.

    ``AmbiguousBackendError`` (the response was lost AFTER the provider already accepted the
    call — see :class:`tasklib.backends.AmbiguousBackendError`) is handled per call, not folded
    into the same bucket as a clean failure: the re-fetch is read-only (harmless either way,
    same collapse as ``_audit_fetch_targets``), but ``transition()`` is a WRITE whose outcome is
    now genuinely unknown — it may have already landed. Recording it with the same text as an
    ordinary failure would tell an operator "nothing happened, safe to just re-run" when a
    blind re-run could double-fire the revert; it gets distinct wording instead (review finding).
    """
    from .audit import audit_done_tickets
    from .backends import AmbiguousBackendError, BackendError

    reverted: list[str] = []
    errors: list[tuple[str, str]] = []
    for finding in findings:
        ticket_id = finding.ticket.id
        try:
            fresh = backend.get(ticket_id)
        except (BackendError, AmbiguousBackendError) as exc:
            errors.append((ticket_id, str(exc)))
            continue
        still_bad = audit_done_tickets([fresh], cfg_e, resolvable_ids)
        if not still_bad:
            continue  # fixed, or moved off Done, since the scan — nothing to revert
        try:
            validate_transition(fresh.state, State.IN_REVIEW)
            updated = backend.transition(ticket_id, State.IN_REVIEW)
        except TransitionError as exc:
            errors.append((ticket_id, str(exc)))
            continue
        except AmbiguousBackendError as exc:
            errors.append(
                (ticket_id, f"outcome unknown — verify {ticket_id} before re-running: {exc}")
            )
            continue
        except BackendError as exc:
            errors.append((ticket_id, str(exc)))
            continue
        reverted.append(ticket_id)
        # review finding (P2): every other successful mutation path (change/status/done/check/
        # mark-shipped) refreshes the session sidecar immediately after the write lands — a
        # revert is exactly such a mutation (DONE -> IN_REVIEW) and was the one path that
        # skipped it, leaving a reactivated ticket's sidecar timestamp stale so a later `task
        # session` view could misclassify it. Mirrors the placement `cmd_status` uses: right
        # after the transition is confirmed, before the best-effort comment/notify below.
        # Current-session only, like every other mutation path: `_record_session_touch` is a
        # no-op when there is no ambient session or the ticket belongs to another session, so
        # a cron/operator-shell `task audit --revert` refreshes nothing (by design, not a gap).
        _record_revert_session_touch(cfg, backend, updated)
        _post_revert_comment(backend, ticket_id, still_bad[0].result)
        _post_revert_notify(cfg, updated)
    return reverted, errors


def _record_revert_session_touch(cfg, backend, ticket: Ticket) -> None:
    """Best-effort, like every other step after the transition in `_audit_revert`'s loop
    (review finding): the ticket is already reverted, so a sidecar failure — `session.record`
    swallows `OSError` but not e.g. a `UnicodeEncodeError` from a title with a lone surrogate —
    must neither abort the rest of the batch nor be mis-reported as a revert failure."""
    import sys

    try:
        _record_session_touch(cfg, backend, ticket)
    except Exception as exc:  # noqa: BLE001 — bookkeeping only, the revert itself already landed
        print(_warn(f"  reverted {ticket.id} but failed to refresh the session sidecar: {exc}"), file=sys.stderr)


def _post_revert_comment(backend, ticket_id: str, result) -> None:
    """Best-effort: the ticket is already reverted by the time this runs, so a failed
    explanatory comment must never undo that fact or get mis-reported as a revert failure."""
    import sys

    from .backends import AmbiguousBackendError, BackendError

    try:
        backend.comment(ticket_id, _audit_revert_comment(result))
    except (BackendError, AmbiguousBackendError) as exc:
        print(_warn(f"  reverted {ticket_id} but failed to post explanation: {exc}"), file=sys.stderr)


def _post_revert_notify(cfg, ticket: Ticket) -> None:
    """Best-effort, mirroring :func:`_post_revert_comment`: the ticket is already reverted by
    the time this runs, so a config/notifier failure must never abort the batch or get
    mis-reported as a revert failure (review finding — `_notify_mutation` itself is designed to
    swallow a down notifier, but a malformed `notifications:`/`daemon:` config block can still
    raise BEFORE it reaches that guarded call; catch broadly here since notification is purely
    advisory and must never be allowed to undo an already-committed state change)."""
    import sys

    try:
        _notify_mutation(cfg, ticket, "reverted (audit)")
    except Exception as exc:  # noqa: BLE001 - advisory only, see docstring
        print(_warn(f"  reverted {ticket.id} but failed to notify: {exc}"), file=sys.stderr)


def _audit_revert_comment(result) -> str:
    lines = [
        "task audit: reverted to in-review — this ticket does not currently satisfy the close "
        "gates `task done` enforces (it may have been closed outside task-cli entirely — e.g. "
        "an external PR-merge auto-close — or a gate may have changed since it was closed):",
    ]
    lines.extend(f"- {v.gate}: {v.message}" for v in result.violations)
    lines.append("Re-close with `task done <id>` once the gates above are satisfied.")
    return "\n".join(lines)


def _print_audit_report(
    findings: list,
    reverted: list[str],
    revert_errors: list[tuple[str, str]],
    *,
    scanned: int,
    as_json: bool,
) -> None:
    if as_json:
        _print_audit_report_json(findings, reverted, revert_errors, scanned)
        return
    if not findings:
        print(_ok(f"audit clean: {scanned} Done ticket(s) checked, 0 noncompliant"))
        return
    print(
        _err(f"audit: {len(findings)}/{scanned} Done ticket(s) do not currently satisfy the close gates")
    )
    for f in findings:
        print(_err(f"  {f.ticket.id}  {f.ticket.title}"))
        for v in f.result.violations:
            print(_err(f"    ✗ {v.gate}: {v.message}"))
    if reverted:
        print(_warn(f"  reverted to in-review: {', '.join(reverted)}"))
    for tid, err in revert_errors:
        print(_err(f"  failed to revert {tid}: {err}"))


def _print_audit_report_json(
    findings: list, reverted: list[str], revert_errors: list[tuple[str, str]], scanned: int
) -> None:
    import json

    payload = {
        "scanned": scanned,
        "findings": [
            {
                "id": f.ticket.id,
                "title": f.ticket.title,
                "violations": [{"gate": v.gate, "message": v.message} for v in f.result.violations],
            }
            for f in findings
        ],
        "reverted": reverted,
        "revert_errors": [{"id": tid, "error": err} for tid, err in revert_errors],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ── mark-shipped (the `gh ship` post-merge hook) ────────────────────────────────────

# A GitHub PR URL's PATH ("/owner/repo/pull/123", optionally followed by more path segments —
# a sub-page like "/files" or "/commits") — the single source of truth for "is this a GitHub
# PR, and which one" used by BOTH `_normalize_pr_url` (to canonicalize away a query string,
# fragment, sub-page, or trailing slash — review finding: those variants used to survive
# normalization untouched, comparing as a DIFFERENT PR from the clean form and dropping a
# still-valid "Merge commit" link) and `_commit_url_from_pr` (to derive a commit link for the
# same repo). Matched against the PATH ONLY (via `urlsplit`), never the raw URL string, so a
# query/fragment can never smuggle extra characters past the pattern.
_GITHUB_PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$")

# A git commit SHA (short or full hex). Guards `--commit` before it's ever interpolated into
# a Links URL — an unvalidated value (whitespace, a pasted message) would build a malformed
# "Merge commit" link that then fails the links-url gate later, at close time (review finding:
# the command would poison a gate it later enforces). Case-folded to lowercase by the caller
# (`_parse_mark_shipped_flags`) so `ABC1234`/`abc1234` compare equal for idempotency (review
# finding: git SHAs are case-insensitive hex, but an un-folded mixed-case retry derived a
# different "Merge commit" URL and was treated as new work — a duplicate comment + TG ping).
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _commit_url_from_pr(pr_url: str, commit_sha: str) -> str | None:
    """Derive a GitHub commit URL from a GitHub PR URL + SHA, or ``None`` when ``pr_url``
    isn't a recognizable GitHub PR URL (a different forge, or no SHA) — the caller then
    records the bare SHA in the comment text only, never as a Links entry (which requires
    an http(s) URL, see ``policy.links_url_violation``). ``pr_url`` is assumed already
    normalized (:func:`_normalize_pr_url`) and ``commit_sha`` already shape-validated and
    lowercased by the caller (:data:`_COMMIT_SHA_RE`)."""
    if not commit_sha:
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(pr_url)
    if parts.netloc.lower() != "github.com":
        return None
    m = _GITHUB_PR_PATH_RE.match(parts.path)
    if not m:
        return None
    owner, repo, _number = m.groups()
    return f"https://github.com/{owner}/{repo}/commit/{commit_sha}"


def _done_with_unmet_warning(ticket: Ticket, unmet: list[Criterion]) -> str:
    """The single message body for the "Done but not really" invariant violation (HYP-1295,
    2026-08-19) — shared by the durable tracker comment (`_shipped_comment`) and the CLI
    output (`_acceptance_instructions`) so the two never drift into different prose or
    different criteria lists (review finding: an earlier version duplicated near-identical
    text and passed only a *count* to the comment, so the actionable detail — which criteria
    — reached CLI stdout but not the tracker, the audience that actually needed it).

    Numbers each listed criterion by its REAL 1-based position in ``ticket.acceptance``
    (review finding: an earlier version enumerated the already-filtered ``unmet`` list
    instead, so e.g. `[checked, unchecked]` printed the unchecked one as `[1]` — the wrong
    number to feed `task check <id> <n>`, which indexes the full list). Filters by object
    identity (``id(c)``), not equality, since two criteria can legitimately share text.

    Names the LIKELY cause (an external integration — e.g. Linear's GitHub auto-close firing
    on a "Closes"/"Fixes"/"Resolves" keyword in a PR body — transitioning the ticket to Done
    the instant a referencing PR merges, entirely outside task-cli, so
    `strict-ticket-discipline` rule 2 never got a chance to fire) as a hedged possibility, not
    an assertion of fact (review finding: a human closing the ticket by hand without checkbox
    hygiene produces the identical state, and this durable comment must not misattribute
    that)."""
    unmet_ids = {id(c) for c in unmet}
    lines = [
        f"{ticket.id} is Done but {len(unmet)} acceptance criterion/criteria "
        "are not fully verified (unchecked, or checked without a proof/force reason):"
    ]
    lines.extend(
        f"  [{i}] {c.text}"
        for i, c in enumerate(ticket.acceptance, start=1)
        if id(c) in unmet_ids
    )
    lines.append(
        "This can happen when an external integration (e.g. Linear's GitHub auto-close on a "
        "'Closes'/'Fixes'/'Resolves' PR-body keyword) closes the ticket on merge before "
        "task-cli's own close gate runs — or from a manual close that skipped the checklist. "
        f"Verify these for real; run `task read {ticket.id}`, then either check each with "
        "proof or reopen the ticket."
    )
    return "\n".join(lines)


def _shipped_comment(
    ticket: Ticket,
    pr_url: str,
    commit_sha: str,
    *,
    already_closed: bool,
    unmet_despite_closed: list[Criterion] | None = None,
) -> str:
    """The durable backend comment recorded on a shipped ticket — the point of it is to make
    the merge visible on the tracker itself (not just in task-cli's own state). For a ticket
    that's already Done, the "this does NOT close" framing would be actively misleading (it's
    already closed), so that ticket gets a plain reference note instead — UNLESS it's Done
    with unmet criteria (see `_done_with_unmet_warning`), which gets a loud warning instead."""
    lines = [f"Shipped: merged via {pr_url}."]
    if commit_sha:
        lines.append(f"Merge commit: {commit_sha}")
    if already_closed:
        if unmet_despite_closed:
            lines.append("WARNING: " + _done_with_unmet_warning(ticket, unmet_despite_closed))
        else:
            lines.append("The ticket is already Done — recorded here for reference.")
    else:
        lines.append(
            "This does NOT close the ticket — acceptance still needs proof. "
            f"Run `task read {ticket.id}` for the outstanding criteria, then `task check`/`task done`."
        )
    return "\n".join(lines)


def _acceptance_instructions(ticket: Ticket, *, acceptance_checked_gate: bool = True) -> list[str]:
    """The human-readable checklist printed after `mark-shipped`: what still stands between
    this merge and a genuine `task done` (strict-ticket-discipline rules 2/3 — a close needs
    every criterion CHECKED with a visual proof; a merge alone proves nothing about that).

    A ticket that's already Done gets a one-line reference note instead — printing "acceptance
    needed" advice about a ticket that is, by definition, already accepted would be nonsense.
    UNLESS it's Done with unmet criteria still sitting on it (see `_done_with_unmet_warning`,
    HYP-1295) — that combination is itself an invariant violation, so it gets the same warning
    treatment as the tracker comment, not the calm note.

    ``acceptance_checked_gate`` (review finding) mirrors `enforce.acceptance_checked` from
    this project's `task.yaml`: a repo that has deliberately DISABLED the on-done
    unchecked-criteria gate (`policy.unchecked_criteria_violation` returns ``None`` there) has
    a configured contract where an externally-closed ticket with unchecked boxes is NOT a
    violation — `mark-shipped` must not contradict that contract with a false warning.
    """
    if ticket.state is State.DONE:
        unmet_while_done = ticket.unmet_criteria() if acceptance_checked_gate else []
        if unmet_while_done:
            return [_warn(_done_with_unmet_warning(ticket, unmet_while_done))]
        return [
            _dim(
                f"{ticket.id} is already Done — this merge was recorded for reference only."
            )
        ]
    unchecked = ticket.unchecked_criteria()
    if not unchecked:
        return [
            _dim(
                f"all acceptance criteria are already checked — if the other close gates pass, run `task done {ticket.id}`."
            )
        ]
    if not acceptance_checked_gate:
        # Same contract as the Done branch above (review finding): a repo that disabled the
        # gate will not demand checked criteria on `task done`, so "acceptance needed before
        # this can be marked Done" would be false advice there.
        return [
            _dim(
                f"{len(unchecked)} criteria unchecked (checked criteria are not enforced on close here) — run `task done {ticket.id}` when the other close gates pass."
            )
        ]
    lines = [
        _warn(
            f"acceptance needed before this can be marked Done ({len(unchecked)} unchecked):"
        )
    ]
    lines.extend(
        _dim(f"  [{i}] {c.text}")
        for i, c in enumerate(ticket.acceptance, start=1)
        if not c.checked
    )
    if ticket.is_ui:
        lines.append(
            _dim(
                "  this ticket is UI-labeled — each criterion needs a visual proof (screenshot/recording), not just a passing test."
            )
        )
    lines.append(
        _dim(
            f'  next: task check {ticket.id} <N> --proof <path>   (or --force "<reason>" if a proof is genuinely impossible)'
        )
    )
    lines.append(_dim(f"  then: task done {ticket.id}"))
    return lines


def _record_shipped_pr(
    backend,
    ticket: Ticket,
    pr_url: str,
    commit_sha: str,
    commit_url: str | None,
    existing_pr: str | None,
    *,
    acceptance_checked_gate: bool = True,
) -> tuple[Ticket, bool]:
    """Post the shipped comment, persist the PR link (+ derived commit link), and move
    TODO/IN_PROGRESS to IN_REVIEW. Returns ``(updated_ticket, moved_to_review)``.

    Every OTHER state but DONE (IN_REVIEW/CANCELLED — CANCELLED is filtered out by the caller
    before this runs) is left untouched for the transition itself — a merge is exactly the
    kind of event a reviewer should look at, but it must never force-march a ticket already
    past that point.

    The comment is posted BEFORE the ticket is persisted (not after — a review finding on an
    earlier version): if `update()` fails after a successful `comment()`, a retry cleanly
    redoes both (one duplicate comment, which is recoverable) rather than the ordering used
    originally, where a comment failing AFTER a successful update left `already_recorded` true
    on retry and the comment was silently lost forever — worse, because it is the durable,
    tracker-visible half of what this command promises. `validate_transition` runs BEFORE even
    that comment (review finding): it's pure and free, so any future state it could reject
    should fail closed before an irreversible side effect, not after one.

    Builds a NEW ticket via `dataclasses.replace` instead of mutating ``ticket`` in place
    (review finding): some backends' `get()` can hand back a live, shared object (the
    in-memory FakeBackend does, to round-trip through render/parse like a real one) — mutating
    it BEFORE `update()` actually persists would let a failed `update()` still leave the
    in-memory object looking "already recorded", masking the very failure the comment-first
    ordering above exists to survive.

    ``existing_pr`` is the ticket's CURRENT normalized PR link (or ``None``), passed in by the
    caller — which already computed it for the idempotency check — rather than recomputed here
    (review finding: two call sites deriving the same fact from the same ticket is a needless
    second source of truth).

    ``acceptance_checked_gate`` — see `_acceptance_instructions`'s docstring; a repo with
    `enforce.acceptance_checked: false` never treats unchecked/unproven criteria as a
    violation, so the Done-with-unmet warning must not fire there either.

    The Done-with-unmet warning (HYP-1295) fires on THIS call's own read of the ticket only —
    a close landing after this command returns is invisible to it. `gh ship` invokes
    `mark-shipped` once per merge, so there is no retry to catch that case anyway.
    """
    from .backends import BackendError

    moved_to_review = ticket.state in (State.TODO, State.IN_PROGRESS)
    if moved_to_review:
        validate_transition(ticket.state, State.IN_REVIEW)

    already_closed = ticket.state is State.DONE
    unmet_despite_closed = (
        ticket.unmet_criteria() if already_closed and acceptance_checked_gate else []
    )
    try:
        backend.comment(
            ticket.id,
            _shipped_comment(
                ticket,
                pr_url,
                commit_sha,
                already_closed=already_closed,
                unmet_despite_closed=unmet_despite_closed,
            ),
        )
    except BackendError as exc:
        raise _UserError(str(exc)) from exc

    new_links = dict(ticket.links)
    pr_changed = existing_pr != pr_url
    new_links["PR"] = pr_url
    if commit_url:
        new_links["Merge commit"] = commit_url
    elif pr_changed:
        # The PR is being REPLACED (a different PR than whatever was recorded before) and this
        # call carries no fresh commit link — drop the stale one rather than leaving a "Merge
        # commit" link that now points at the OLD PR's commit while "PR" points at the new one
        # (review finding: a silently mismatched pair is worse than no link at all).
        new_links.pop("Merge commit", None)
    to_persist = replace(
        ticket,
        links=new_links,
        state=State.IN_REVIEW if moved_to_review else ticket.state,
    )
    try:
        updated = backend.update(to_persist)
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    return updated, moved_to_review


def _normalize_pr_url(pr_url: str) -> str:
    """Canonicalize a PR URL for storage/comparison (review finding). Without this,
    `https://github.com/a/b/pull/1`, `https://GitHub.com/a/b/pull/1/`,
    `.../pull/1?diff=split`, and `.../pull/1/files` all compare as DIFFERENT PRs — defeating
    idempotency, and worse, making `_record_shipped_pr` think the PR was REPLACED and drop a
    still-valid "Merge commit" link (see its `pr_changed` handling).

    For a GitHub PR URL (matched via `_GITHUB_PR_PATH_RE` against the PATH only), collapses
    ENTIRELY to the exact canonical form `https://github.com/<owner>/<repo>/pull/<n>` —
    dropping any query string, fragment, sub-page path, or trailing slash, and lowercasing the
    host (the owner/repo PATH segments are left exactly as given; those ARE meaningfully
    cased). For any other URL, only the scheme+host are lowercased and a trailing slash is
    stripped — a lighter touch, since this command doesn't know that forge's URL grammar.

    Applied to BOTH the freshly-validated `--pr` value (in `_parse_mark_shipped_flags`) AND
    whatever is already stored in `ticket.links["PR"]` (via `_stored_pr_url` below) — a review
    finding on an earlier version normalized only the former, so a value written out-of-band,
    by an older version, or by a hand-edit (a documented, tested scenario — see
    `test_mark_shipped_transitions_even_when_the_pr_link_was_set_out_of_band`) still compared
    raw and could trip the same false-replace hazard this function exists to close."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(pr_url)
    scheme, netloc = parts.scheme.lower(), parts.netloc.lower()
    if netloc == "github.com":
        m = _GITHUB_PR_PATH_RE.match(parts.path)
        if m:
            owner, repo, number = m.groups()
            return f"{scheme}://github.com/{owner}/{repo}/pull/{number}"
    normalized = parts._replace(
        scheme=scheme, netloc=netloc, path=parts.path.rstrip("/") or "/"
    )
    return urlunsplit(normalized)


def _stored_pr_url(ticket: Ticket) -> str | None:
    """The ticket's currently-recorded PR link, normalized — or ``None`` if it has none. The
    single read point both `cmd_mark_shipped`'s idempotency/replace check and
    `_record_shipped_pr`'s stale-commit-link check use, so they can never compare a raw stored
    value against a normalized incoming one (see `_normalize_pr_url`'s docstring)."""
    raw = ticket.links.get("PR")
    return _normalize_pr_url(raw) if raw else None


def _parse_mark_shipped_flags(args: argparse.Namespace) -> tuple[str, str]:
    """Validate ``--pr``/``--commit`` up front, before any backend call.

    Validating AFTER ``backend.get()`` reports the wrong error ("unknown ticket" instead of
    "bad --pr") on a typo'd id + a bad flag together (review finding). An unvalidated value
    would also poison the Links section with a non-URL entry that only fails later, at close
    time, via ``policy.links_url_violation`` — this command must not create a violation it
    later enforces.
    """
    pr_url = (args.pr or "").strip()
    if not pr_url:
        raise _UserError("--pr is required and must not be empty")
    from .policy import is_http_url

    if not is_http_url(pr_url):
        raise _UserError(f"--pr must be a full http(s) URL (got {args.pr!r})")
    pr_url = _normalize_pr_url(pr_url)
    commit_sha = (args.commit or "").strip()
    if commit_sha and not _COMMIT_SHA_RE.match(commit_sha):
        raise _UserError(
            f"--commit must be a 7-40 character hex commit SHA (got {args.commit!r})"
        )
    return pr_url, commit_sha.lower()


def _print_mark_shipped_cancelled(ticket: Ticket, as_json: bool) -> None:
    """Output for the CANCELLED short-circuit — kept `--json`-aware (review finding: the
    original version printed free human text and exited 0 regardless of `--json`, breaking a
    scripted caller piping the output through a JSON parser)."""
    if as_json:
        import json

        print(
            json.dumps(
                {
                    "id": ticket.id,
                    "url": ticket.url,
                    "state": ticket.state.value,
                    "recorded": False,
                    "reason": "cancelled",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(
        _warn(
            f"{ticket.id} is cancelled — not recording (a cancelled ticket is a deliberate dead end)."
        )
    )


def _print_mark_shipped_result(
    updated: Ticket,
    pr_url: str,
    *,
    already_recorded: bool,
    moved_to_review: bool,
    replaced_pr: str | None,
    as_json: bool,
    instructions: list[str],
    acceptance_checked_gate: bool = True,
) -> None:
    """The human/`--json` output for `mark-shipped` — split out of the command purely to keep
    the command itself under the file's function-length target."""
    if as_json:
        import json

        print(
            json.dumps(
                {
                    "id": updated.id,
                    "url": updated.url,
                    "state": updated.state.value,
                    "pr": pr_url,
                    "already_recorded": already_recorded,
                    "moved_to_review": moved_to_review,
                    "replaced_pr": replaced_pr,
                    "commit_url": updated.links.get("Merge commit"),
                    "unchecked_criteria": [
                        c.text for c in updated.unchecked_criteria()
                    ],
                    # Non-empty only when state is DONE with criteria that wouldn't pass the
                    # on-done close gate (HYP-1295) — the structured signal a scripted caller
                    # (gh ship / task-cli's own hooks) can act on without parsing prose; the
                    # human-facing warning lives in the tracker comment + `instructions` above.
                    # Empty when `enforce.acceptance_checked` is disabled for this project
                    # (review finding) — that gate is what defines "wouldn't pass", so a repo
                    # that turned it off has no such notion.
                    "done_with_unmet_criteria": (
                        [c.text for c in updated.unmet_criteria()]
                        if updated.state is State.DONE and acceptance_checked_gate
                        else []
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if replaced_pr:
        print(
            _warn(
                f"{updated.id} already had a different PR recorded ({replaced_pr}) — replaced with "
                f"{pr_url} (a ticket shipped via more than one PR, e.g. a revert-and-reland, keeps "
                "only the latest in Links; earlier ones remain visible in the comment history)."
            )
        )
    if already_recorded:
        note = " (already recorded)"
    elif moved_to_review:
        note = f" → state {updated.state.value}"
    else:
        # Recorded (link/comment), but the ticket was already past TODO/IN_PROGRESS, so no
        # transition actually happened — the arrow would otherwise imply a move that didn't
        # occur (review finding).
        note = f" (state stays {updated.state.value})"
    print(_ok(f"{updated.id} shipped — {pr_url}{note}"))
    for line in instructions:
        print(line)


def cmd_mark_shipped(args: argparse.Namespace) -> int:
    """Record a merged PR against a ticket — the `gh ship` post-merge hook entry point
    (agent-tools ci/ship/ship.sh calls this so a ticket tracks its merge instead of drifting
    out of sync, and so the merge surfaces acceptance instructions instead of the ticket
    silently going quiet).

    Deliberately NEVER closes the ticket: only PROPER acceptance (every criterion checked
    with a visual proof — see `strict-ticket-discipline` rules 2/3) may set it Done. This
    command only records the merge (a Links entry + a durable backend comment), nudges
    TODO/IN_PROGRESS to IN_REVIEW, and prints what's still needed to close it for real. A
    CANCELLED ticket is a deliberate dead end (mirrors `transitions.py`) and is skipped
    entirely — no link, no comment, no notify.
    """
    pr_url, commit_sha = _parse_mark_shipped_flags(args)
    cfg = _load(args)

    # Resolve routing once — `pcfg` feeds both the backend and the acceptance-gate read below
    # (same shared helper `cmd_gate`/`cmd_accept` use — task-cli PR#118 round 4).
    backend, pcfg = _routing_for_id(cfg, args.id)
    from .backends import AmbiguousBackendError, BackendError

    try:
        ticket = backend.get(args.id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc

    if ticket.state is State.CANCELLED:
        _print_mark_shipped_cancelled(ticket, args.json)
        return 0

    commit_url = _commit_url_from_pr(pr_url, commit_sha) if commit_sha else None
    # Idempotency keys on the PR URL, when a commit was given whether its derived link is
    # already recorded, AND the ticket already being past TODO/IN_PROGRESS:
    #   - the commit clause: a review finding on an earlier version keyed on the PR URL alone,
    #     so a RETRY that arrives with a commit SHA the first call didn't have would be silently
    #     treated as already-recorded and never backfill the commit link. This is a
    #     manual-recovery path (a human re-running the command after seeing a ship.sh WARNING)
    #     — not something ship.sh's own single call triggers, since it resolves the PR url and
    #     merge SHA together in one pass. NOTE: a backfill re-posts the FULL "Shipped" comment
    #     and re-notifies (it re-runs `_record_shipped_pr` in full) rather than posting a
    #     targeted "Merge commit: …" addendum — an accepted trade-off for a rare, human-triggered
    #     path; not worth the extra state needed to special-case a comment-only patch.
    #   - the state clause (review finding): without it, a ticket whose Links["PR"] was set
    #     OUT-OF-BAND (not by this command — e.g. hand-edited, or reverted with `task status
    #     ... todo --force` after a prior mark-shipped) would never actually transition to
    #     IN_REVIEW even though it's still sitting in TODO/IN_PROGRESS — "already recorded"
    #     would be true for the link, but the command's other promise (nudge toward review)
    #     would silently never happen.
    # Known residual gap (tracked, not fixed here — see #93): this key is still a proxy for
    # "did mark-shipped actually run", not a dedicated marker — a crash in the narrow window
    # AFTER a successful update() but BEFORE the TG notify below would leave `already_recorded`
    # true on any retry and the notification permanently skipped (the durable link + comment
    # survive either way, so the impact is a missed best-effort ping, not a lost record).
    #
    # `acceptance_checked_gate` mirrors `enforce.acceptance_checked` from this project's
    # `task.yaml` — a repo that disabled that gate never treats unchecked/unproven criteria as
    # a violation, so the Done-with-unmet warning (see `_acceptance_instructions`/
    # `_record_shipped_pr`) must not fire there either. It does not add an `already_recorded`
    # clause — the warning fires on this call's own read only (see `_record_shipped_pr`'s
    # docstring).
    acceptance_checked_gate = _enforce_config(pcfg).acceptance_checked
    existing_pr = _stored_pr_url(ticket)
    already_recorded = (
        existing_pr == pr_url
        and (commit_url is None or ticket.links.get("Merge commit") == commit_url)
        and ticket.state not in (State.TODO, State.IN_PROGRESS)
    )
    if already_recorded:
        updated, moved_to_review = ticket, False
    else:
        updated, moved_to_review = _record_shipped_pr(
            backend,
            ticket,
            pr_url,
            commit_sha,
            commit_url,
            existing_pr,
            acceptance_checked_gate=acceptance_checked_gate,
        )
        _record_session_touch(cfg, backend, updated)
        from .logging import log_event

        log_event(
            "ticket.shipped",
            ticket_id=updated.id,
            pr=pr_url,
            commit=commit_sha,
            moved_to_review=moved_to_review,
        )
        _notify_mutation(cfg, updated, "shipped")

    replaced_pr = existing_pr if (existing_pr and existing_pr != pr_url) else None
    _print_mark_shipped_result(
        updated,
        pr_url,
        already_recorded=already_recorded,
        moved_to_review=moved_to_review,
        replaced_pr=replaced_pr,
        as_json=args.json,
        instructions=_acceptance_instructions(updated, acceptance_checked_gate=acceptance_checked_gate),
        acceptance_checked_gate=acceptance_checked_gate,
    )
    return 0


# ── gate + accept (the pre-merge acceptance gate, task-cli#115) ─────────────────────
# Tickets were closing on merge behind task-cli's back: Linear's GitHub integration and
# GitHub's "Closes #N" flip a ticket to Done the instant the linked PR merges, so the on-done
# gates (which only run inside `task done`) never fire. `task gate` is the read-only verdict
# `gh ship` (agent-tools ci/ship/ship.sh) reads BEFORE merging; `task accept` is the
# convenience that makes passing it cheap.


def cmd_gate(args: argparse.Namespace) -> int:
    """The read-only ship-time verdict. Exit 0 = accepted (every criterion checked WITH a
    proof, or a recorded post-merge opt-out, or the gate disabled in config, or a cancelled
    ticket); exit 1 = NOT accepted (the gaps are listed); exit 2 = could not evaluate (unknown
    id, backend error). ``--json`` prints the same shape on every 0/1 path — see
    `_gate_payload` for the contract ship.sh consumes."""
    cfg = _load(args)
    backend, pcfg = _routing_for_id(cfg, args.id)
    from .backends import AmbiguousBackendError, BackendError
    from .policy import acceptance_gate

    try:
        ticket = backend.get(args.id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc

    # The gate is the OWNING project's policy (`pcfg`), not necessarily the cwd's: `gh ship`
    # runs this in the code repo while the ticket may live in another project.
    report = acceptance_gate(ticket, _enforce_config(pcfg))
    cancelled = ticket.state is State.CANCELLED
    ok = report.ok or cancelled
    if args.json:
        import json

        print(json.dumps(_gate_payload(ticket, report, ok), ensure_ascii=False, indent=2))
    else:
        for line in _gate_lines(ticket, report, ok, cancelled):
            print(line)
    return 0 if ok else 1


def _gate_payload(ticket: Ticket, report, ok: bool) -> dict:
    """The `task gate --json` contract (consumed by agent-tools' ship.sh — keep the field
    names stable; documented in README "The pre-merge gate"): ``ok`` (the verdict),
    ``unchecked`` / ``proofless`` (each ``{index, text}``, 1-based index into the full criteria
    list = the number `task check` takes), ``post_merge_acceptance`` (the recorded opt-out
    reason, or null), ``gate_enabled`` (``enforce.acceptance_checked``), ``state``, ``criteria``
    (the total count — 0 means "nothing to accept", which is itself a refusal), ``below_minimum``
    (true when `criteria` is fewer than this repo's configured `acceptance_min`, the SAME
    minimum-count gate `task done` enforces, and it wasn't skipped — a ticket can refuse here
    with EMPTY `unchecked`/`proofless`, e.g. one fully-proven criterion below a minimum of two;
    a consumer must check this field too, not just the two gap arrays, to explain a refusal),
    ``id``."""
    return {
        "id": ticket.id,
        "ok": ok,
        "state": ticket.state.value,
        "gate_enabled": report.gate_enabled,
        "post_merge_acceptance": report.post_merge_reason or None,
        "criteria": len(ticket.acceptance),
        "below_minimum": report.below_minimum,
        "unchecked": [{"index": _criterion_index(ticket, c), "text": c.text} for c in report.unchecked],
        "proofless": [{"index": _criterion_index(ticket, c), "text": c.text} for c in report.proofless],
    }


def _gate_lines(ticket: Ticket, report, ok: bool, cancelled: bool) -> list[str]:
    """The human report: verdict first, then the gaps (always, so a passing opt-out still
    shows what is owed after the merge), then the fix commands.

    Precedence matters (review finding, task-cli#115 round 2): a disabled `acceptance_checked`
    gate does NOT imply a pass any more — the independent minimum-count gate
    (`report.below_minimum`) can still refuse a criteria-checked-gate-disabled ticket, so `ok`
    is checked before assuming "gate disabled" means "gate passes".
    """
    total = len(ticket.acceptance)
    gaps = len(report.unchecked) + len(report.proofless)
    lines: list[str] = []
    if cancelled:
        lines.append(_ok(f"{ticket.id} is cancelled — nothing to accept, gate passes."))
    elif ok and report.post_merge_reason:
        lines.append(_ok(f"{ticket.id}: post-merge acceptance recorded — gate passes ({gaps} of {total} still owed after the merge)."))
        lines.append(_dim(f"  reason: {report.post_merge_reason}"))
    elif ok and not report.gate_enabled:
        lines.append(_ok(f"{ticket.id}: the acceptance-checked gate is disabled in this repo's config — gate passes."))
    elif ok and report.no_criteria:
        # `ok` here means the MINIMUM-count gate itself allows zero (acceptance_criteria:
        # false, acceptance_min: 0, or a recorded --skip-acceptance) — "0 of 0 criteria checked
        # with a proof" would assert proofs that don't exist (review finding, task-cli PR#118
        # round 4, GLM). There is genuinely nothing to accept, and this repo's config says
        # that's fine.
        lines.append(_ok(f"{ticket.id}: gate passes — this repo does not require acceptance criteria (there are none to accept)."))
    elif ok:
        lines.append(_ok(f"{ticket.id}: accepted — {total} of {total} criteria checked with a proof."))
    elif report.no_criteria:
        lines.append(_err(f"{ticket.id}: not accepted — the ticket has no acceptance criteria, so nothing can be proven."))
        lines.append(_dim(f'  fix: task change {ticket.id} --acceptance "<criterion>" (repeatable), then accept them'))
    elif report.below_minimum:
        noun = "criterion" if total == 1 else "criteria"
        lines.append(_err(f"{ticket.id}: not accepted — only {total} acceptance {noun} (the minimum is enforced on close too)."))
        lines.append(_dim(f'  fix: task change {ticket.id} --acceptance "<criterion>" (repeatable), or --skip-acceptance "<reason>" if genuinely N/A'))
    else:
        lines.append(_err(f"{ticket.id}: not accepted — {gaps} of {total} criteria still open."))
    for c in report.unchecked:
        lines.append(_warn(f"  [{_criterion_index(ticket, c)}] {c.text}  (unchecked)"))
    for c in report.proofless:
        lines.append(_warn(f"  [{_criterion_index(ticket, c)}] {c.text}  (checked without a proof)"))
    if gaps:
        lines.append(_dim(f"  fix: task accept {ticket.id}   (walks each one, asking for a proof)"))
        lines.append(_dim(f"       task check {ticket.id} <n> [<n> ...] --proof <path> [--proof <path> ...]"))
        if not ok:
            lines.append(_dim(f'       or, if acceptance is inherently post-merge: task change {ticket.id} --post-merge-acceptance "<reason>"'))
    return lines


def cmd_accept(args: argparse.Namespace) -> int:
    """Walk the criteria that still block the gate, one by one, asking for a proof path each
    (interactive — a TTY is required; without one it prints the headless equivalent). Every
    answer is persisted immediately, so an interrupted walk keeps its progress. Exit 0 when
    the ticket ends up accepted, 1 when something was skipped."""
    if not sys.stdin.isatty():
        print(_accept_usage(args.id))
        return 2
    cfg = _load(args)
    # Resolve routing once — pcfg feeds both the backend and every acceptance-gate read below,
    # matching `cmd_gate` exactly (review finding, task-cli PR#118 rounds 3-4): `gh ship` runs
    # `task gate` in the code repo while the ticket may live in another project, so `cmd_accept`'s
    # own completion check must read the SAME owning-project config `task gate` would.
    backend, pcfg = _routing_for_id(cfg, args.id)
    from .backends import AmbiguousBackendError, BackendError

    try:
        ticket = backend.get(args.id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc

    pending = ticket.unmet_criteria()
    if not pending:
        ok, msg = _accept_completion_message(ticket, pcfg)
        print(msg)
        return 0 if ok else 1
    print(f"{ticket.id}: {len(pending)} criteria to accept. Enter a proof path/URL, "
          '"force: <reason>" when a proof is impossible, or Enter to skip.')
    attach = _ProofAttacher(backend, ticket.id)
    # Walk by 1-based position, not by Criterion object: every `backend.update` hands back a
    # fresh Ticket, so the objects in `pending` are stale after the first persisted answer.
    for idx in [_criterion_index(ticket, c) for c in pending]:
        crit = ticket.acceptance[idx - 1]
        answer = _prompt_proof(idx, crit)
        if answer is None:
            continue
        proof, force_reason = answer
        _apply_check(attach, crit, [proof] if proof else [], force_reason)
        try:
            ticket = backend.update(ticket)
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        crit = ticket.acceptance[idx - 1]
        _log_checked(ticket, [idx], forced=bool(force_reason))
        print(_ok(f"  checked ({'forced: ' + force_reason if force_reason else 'proof ' + crit.proof})"))
    _record_session_touch(cfg, backend, ticket)
    ok, msg = _accept_completion_message(ticket, pcfg)
    print(msg)
    return 0 if ok else 1


def _accept_completion_message(ticket: Ticket, cfg) -> tuple[bool, str]:
    """Whether `task accept` should report success, and the lines to print — reusing
    `_gate_lines` (`task gate`'s OWN message builder), not a second, bespoke message (review
    finding, task-cli PR#118 round 3, Opus): a hand-written "every criterion is checked with a
    proof" string collapsed the opt-out/disabled-gate/below-minimum cases into one wording,
    which could assert a falsehood (e.g. print "checked with a proof" for a ticket accepted
    only via the post-merge opt-out, with criteria still unchecked). Reusing `_gate_lines`
    means this can never drift from what `task gate {id}` itself would print.

    ``cfg`` must already be the OWNING project's config (`_config_for_id`, resolved ONCE by the
    caller — `cmd_accept` — and reused here), matching what `cmd_gate` reads, so the two can
    never disagree about a cross-project ticket's `acceptance_min`."""
    from .policy import acceptance_gate

    report = acceptance_gate(ticket, _enforce_config(cfg))
    cancelled = ticket.state is State.CANCELLED
    ok = report.ok or cancelled
    return ok, "\n".join(_gate_lines(ticket, report, ok, cancelled))


def _prompt_proof(idx: int, crit: Criterion) -> tuple[str, str] | None:
    """Ask for one criterion's proof (``idx`` is its 1-based position, the number `task check`
    takes). Returns ``(proof, "")``, ``("", force_reason)``, or ``None`` to skip. A local path
    that does not exist is re-prompted (a typo must not be recorded as a proof); a URL is
    taken as-is. Ctrl-D (EOFError) or Ctrl-C (KeyboardInterrupt) degrades to a skip, like an
    empty answer, instead of an unhandled traceback (review finding, task-cli PR#118 rounds
    1/4/5) — every already-answered criterion this session stays persisted either way."""
    print(f"[{idx}] {crit.text}")
    while True:
        try:
            raw = input("  proof: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print(_dim("  skipped (input closed)"))
            return None
        if not raw:
            print(_dim("  skipped"))
            return None
        if raw.lower().startswith("force:"):
            reason = raw[len("force:"):].strip()
            if reason:
                return "", reason
            print(_warn("  a force needs a reason: force: <why a proof is impossible>"))
            continue
        if _looks_like_url(raw) or Path(raw).expanduser().exists():
            return raw, ""
        print(_warn(f"  {raw}: not found — give an existing file (e.g. from `dev shot`) or a URL"))


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _accept_usage(ticket_id: str) -> str:
    return (
        f"task accept {ticket_id} is interactive and needs a terminal (no TTY on stdin).\n"
        "  headless equivalents:\n"
        f"    task check {ticket_id} <n> [<n> ...] --proof <path> [--proof <path> ...]   # one proof per criterion, in order\n"
        f"    task check {ticket_id} --all --proof <path>                                # one shared proof\n"
        f'    task check {ticket_id} <n> --force "<reason>"                              # when a proof is impossible\n'
        f"    task gate {ticket_id} [--json]                                            # the verdict"
    )


_LinkStatus = Literal["linked", "already-linked", "failed", "ambiguous"]


@dataclass(frozen=True)
class _LinkOutcome:
    """The result of writing (or skipping, or failing to write) one `LinkEntry` — what
    `cmd_link` reports per entry so a partial multi-backend failure is never silently
    swallowed (review finding, task-cli#154: two entries can target different backends, and
    one `backend.update()` succeeding while the other raises must not look identical to either
    full success or full failure). `"ambiguous"` is distinct from `"failed"`: it means the
    connection dropped AFTER the backend may have already accepted the write (see
    `AmbiguousBackendError`) — re-running is still safe (this write is idempotent), but the
    caller should know the outcome is genuinely unknown, not a clean no-op refusal. `changed`
    is recorded directly (not re-derived from `status` later — review finding: a string
    comparison like `status == "linked"` silently breaks the moment a new status value is
    added, since nothing ties it back to what `apply_entry` actually reported)."""

    ticket: Ticket
    entry: LinkEntry
    status: _LinkStatus
    error: str = ""
    changed: bool = False


def cmd_link(args: argparse.Namespace) -> int:
    """Record a typed relationship (see `tasklib.links.RELATIONS`: `blocks`/`blocked-by`/
    `relates-to`/`duplicate-of`/`follow-up-of`/`followed-up-by`) between two tickets, writing a
    structured entry into their `## Links` sections (task-cli#154, deploy-followup-links).

    Mirrors `blocks`/`blocked-by`/`relates-to`/`follow-up-of`/`followed-up-by` onto the OTHER
    ticket too (A blocks B implies B is blocked-by A; A is a follow-up of B implies B was
    followed up by A); `duplicate-of` is deliberately one-sided — see `tasklib/links.py`'s
    module docstring for why. Idempotent: re-running the identical call writes nothing (the
    target key already maps to the same URL), so it never accumulates a duplicate Links entry.

    Each entry is written independently and its own outcome recorded (`_LinkOutcome`) — a
    `BackendError`/`AmbiguousBackendError` on ONE entry (a network blip, an auth hiccup, a
    different backend's rate limit) does not roll back an entry that already succeeded, and is
    never hidden behind a generic `_UserError` that would erase the fact that the other entry
    landed (review finding: the original version raised on the first failure, discarding any
    already-written result). Exit code: 9 (`EXIT_AMBIGUOUS`) if ANY entry's outcome is unknown
    (checked first — an unknown outcome takes precedence over a co-occurring clean failure,
    since "might have landed" is the more cautious read for a caller deciding whether to
    retry), else 2 if any entry failed cleanly, else 0. Re-running the identical call afterward
    is always safe either way — idempotency means it only retries the entry(ies) that didn't
    land.

    The reopen-hazard check (see `_reopen_hazard_reason`) runs only on a ticket this call will
    ACTUALLY write to (review finding: an earlier version checked both `source`/`other`
    unconditionally, which wrongly refused a valid `duplicate-of` — that relation never writes
    to the other ticket — whenever the other ticket happened to carry the hazard).

    CAVEAT (shared with every other command that calls `backend.update`, e.g. `check`/
    `mark-shipped`): the update re-renders the WHOLE body from the `Ticket` fields
    (`render.render`), so linking B from A also rewrites B's stored body — including whatever B
    doesn't round-trip (AGENTS.md's HYP-1355/HYP-1356 note). A caller may run `task link A
    blocks B` without ever having read B; unlike `change --what`, this command has no
    `read-before-edit` gate, because it never touches B's prose fields, only `## Links`. Also
    shared with every other `fetch → replace(...) → update` command in this file: two concurrent
    writers mirroring onto the SAME third ticket can each build their `links` dict from a
    now-stale snapshot, and whichever `update()` lands second silently drops the other's entry
    (review finding) — there is no optimistic-concurrency layer anywhere in this codebase's
    backend protocol, so this is an accepted, pre-existing risk class, not something this
    command can fix alone.
    """
    cfg = _load(args)
    from .backends import AmbiguousBackendError, BackendError
    from .links import apply_entry, plan_link
    from .policy import is_http_url

    source_backend, source_ticket = _resolve_ticket_for_link(cfg, args.id)
    other_backend, other_ticket = _resolve_ticket_for_link(cfg, args.other_id)
    if source_ticket.id == other_ticket.id:
        raise _UserError(f"cannot link {source_ticket.id} to itself")

    entries = plan_link(
        source_ticket.id, source_ticket.url, args.relation, other_ticket.id, other_ticket.url
    )
    # Validated per-ENTRY, not per-ticket (review finding, task-cli#154 round 4, Opus): for
    # `duplicate-of`, only ONE entry exists and `source_ticket.url` is never embedded in it —
    # requiring it to be a valid URL anyway was needlessly strict (the same "only check what
    # this call actually writes" fix already applied to the reopen-hazard guard). The raw value
    # is deliberately NOT echoed (round 3, Grok): `is_http_url` also rejects a URL carrying
    # embedded userinfo or a secret-shaped query value — exactly the cases that must never hit
    # stdout/a log/tmux scrollback. A `!r`-interpolated rejected value would leak precisely that.
    for entry in entries:
        if not is_http_url(entry.url):
            raise _UserError(
                f"cannot write {entry.key!r} on {entry.ticket_id} — the linked ticket has no "
                "usable URL (the backend must report a real http(s) URL, with no embedded "
                "credentials or secret-shaped value, before it can be linked)"
            )

    tickets = {source_ticket.id: source_ticket, other_ticket.id: other_ticket}
    backends = {source_ticket.id: source_backend, other_ticket.id: other_backend}
    outcomes: list[_LinkOutcome] = []
    for entry in entries:
        ticket = tickets[entry.ticket_id]
        new_links, changed = apply_entry(ticket.links, entry)
        if not changed:
            outcomes.append(_LinkOutcome(ticket, entry, "already-linked", changed=False))
            continue
        hazard = _reopen_hazard_reason(ticket)
        if hazard:
            outcomes.append(_LinkOutcome(ticket, entry, "failed", hazard, changed=False))
            continue
        try:
            updated = backends[entry.ticket_id].update(replace(ticket, links=new_links))
        except AmbiguousBackendError as exc:
            outcomes.append(_LinkOutcome(ticket, entry, "ambiguous", str(exc), changed=False))
            continue
        except BackendError as exc:
            outcomes.append(_LinkOutcome(ticket, entry, "failed", str(exc), changed=False))
            continue
        tickets[entry.ticket_id] = updated
        outcomes.append(_LinkOutcome(updated, entry, "linked", changed=True))

    _print_link_result(args, outcomes)
    if any(o.status == "ambiguous" for o in outcomes):
        from .backends import EXIT_AMBIGUOUS

        return EXIT_AMBIGUOUS
    return 2 if any(o.status == "failed" for o in outcomes) else 0


def cmd_module(args: argparse.Namespace) -> int:
    """`task module list/add/assign` — the repo's product-module taxonomy (task-cli#218).

    A module is a NAMED, durable subsystem/area (`tasklib.modules.ProductModule`) a repo
    defines once in `task.yaml`'s `modules:` list; `list` shows it (or a clear "none
    configured" message — a taxonomy is never required), `add` prints the exact YAML snippet
    to append by hand (`task.yaml` has no config-WRITING precedent anywhere in this repo — see
    `tasklib.modules`'s module docstring — it is a committed, human-edited file), and `assign`
    stamps a `module:<name>` label onto an existing ticket via `backend.update()`, the exact
    same label mechanism `sp:<n>`/`priority:<Pn>` already use. `assign` FAILS CLOSED (a clean
    `error:`, no backend write) on a module name outside the configured taxonomy — it never
    invents a new label on the fly.
    """
    cfg = _load(args)
    if args.action == "list":
        return _cmd_module_list(cfg, args)
    if args.action == "add":
        return _cmd_module_add(cfg, args)
    return _cmd_module_assign(cfg, args)


def _cmd_module_list(cfg, args: argparse.Namespace) -> int:
    from .modules import load_taxonomy

    taxonomy = load_taxonomy(cfg)
    if args.json:
        import json

        print(
            json.dumps(
                [{"name": m.name, "description": m.description} for m in taxonomy.modules],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not taxonomy.configured:
        print(
            "no module taxonomy configured for this repo — add one with "
            '`task module add <name> --description "..."`'
        )
        return 0
    for m in taxonomy.modules:
        print(f"{m.name}  —  {m.description}" if m.description else m.name)
    return 0


def _cmd_module_add(cfg, args: argparse.Namespace) -> int:
    from .modules import NAME_RE, load_taxonomy

    name = (args.arg1 or "").strip()
    if not name:
        raise _UserError('usage: task module add <name> --description "TEXT"')
    if not NAME_RE.match(name):
        raise _UserError(
            f"invalid module name {name!r} — lowercase letters, digits, hyphens only, "
            "starting with a letter or digit"
        )
    taxonomy = load_taxonomy(cfg)
    if name in taxonomy.names():
        raise _UserError(f"module {name!r} is already configured")
    description = (args.description or "").strip()
    # No config-WRITING precedent exists anywhere in this repo (tasklib.modules's module
    # docstring) — task.yaml is committed and human-edited, so this prints the snippet to add
    # by hand rather than silently rewriting a file a repo owner reviews and commits. Target
    # path mirrors `_load`'s own `--config` resolution (review finding) — an explicit
    # `--config` file replaces the `task.yaml` layer, so the printed path must too, or the
    # guidance points at a file the loader never reads.
    explicit = getattr(args, "config", None)
    target = (
        (cfg.repo_root / explicit if not Path(explicit).is_absolute() else Path(explicit))
        if explicit
        else cfg.repo_root / "task.yaml"
    )
    import json

    print(
        f"{target} is human-edited (task-cli never writes it) — add this entry to its "
        "`modules:` list:\n"
    )
    if not taxonomy.configured:
        # Only emit the `modules:` header when the taxonomy is empty/absent — printing it
        # unconditionally would create a SECOND top-level `modules:` key once pasted under an
        # already-populated one, and PyYAML silently keeps only the last one, dropping every
        # existing entry (review finding).
        print("modules:")
    # `json.dumps` on both the name and the description produces YAML-safe double-quoted
    # scalars (valid under both JSON and YAML flow-scalar syntax) — a raw, unquoted name like
    # `on`/`null`/`42` parses back as a bool/null/int under YAML 1.1's implicit typing, and an
    # unquoted description breaks on an embedded colon or `#` (review finding: either one,
    # pasted verbatim, corrupts task.yaml for every subsequent `task` command in the repo).
    print(f"  - name: {json.dumps(name, ensure_ascii=False)}")
    if description:
        print(f"    description: {json.dumps(description, ensure_ascii=False)}")
    return 0


def _cmd_module_assign(cfg, args: argparse.Namespace) -> int:
    from .backends import AmbiguousBackendError, BackendError
    from .modules import assigned_module_names, label_for, load_taxonomy

    ticket_id = (args.arg1 or "").strip()
    name = (args.arg2 or "").strip()
    if not ticket_id or not name:
        raise _UserError("usage: task module assign <ticket-id> <module-name>")
    taxonomy = load_taxonomy(cfg)
    if not taxonomy.configured:
        raise _UserError(
            "no module taxonomy configured for this repo — add one with "
            '`task module add <name> --description "..."` first'
        )
    if taxonomy.get(name) is None:
        raise _UserError(
            f"{name!r} is not a configured module — known modules: "
            f"{', '.join(taxonomy.names())}"
        )
    backend = _backend_for_id(cfg, ticket_id)
    try:
        # A plain read with no side effects — an ambiguous outcome is as harmless as a clean
        # one here, matching `_resolve_ticket_for_link`'s identical convention for its own
        # `backend.get()` (review finding: an earlier version caught only `BackendError`,
        # letting `AmbiguousBackendError` escape as a raw traceback).
        ticket = backend.get(ticket_id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc
    label = label_for(name)
    if label in ticket.labels:
        # A `--json` caller (e.g. a script re-running an idempotent assign) must get the same
        # JSON shape on every branch, not a human-readable ANSI line on the no-op path
        # (review finding).
        if args.json:
            import json

            print(json.dumps(_ticket_dict(ticket), ensure_ascii=False, indent=2))
        else:
            print(_ok(f"{ticket.id} is already assigned to module {name!r}"))
        return 0
    # Same reopen/re-close hazard `cmd_link` guards against before every `backend.update()`
    # (review finding: a stale managed-state label vs. the backend's real open/closed flag
    # would otherwise be silently flipped by this metadata-only write, since `update()`
    # unconditionally PATCHes state from `ticket.state`) — see `_reopen_hazard_reason`'s
    # docstring for the full rationale.
    hazard = _reopen_hazard_reason(ticket)
    if hazard:
        raise _UserError(hazard)
    existing_modules = assigned_module_names(ticket.labels)
    ticket.labels.append(label)
    try:
        updated = backend.update(ticket)
    except AmbiguousBackendError as exc:
        # The write may have already landed server-side before the connection dropped — same
        # ambiguity `cmd_link`/`_classify_create` handle explicitly for their own
        # `backend.update()` calls (review finding: this path previously let it propagate as
        # an unhandled traceback instead of a clean, actionable exit).
        print(_err(f"ambiguous: {exc}"))
        print(
            _warn(
                f"  the module:{name} label on {ticket.id} may have already been written — "
                "check the backend before retrying, to avoid a duplicate/partial write."
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    if args.json:
        import json

        print(json.dumps(_ticket_dict(updated), ensure_ascii=False, indent=2))
    elif existing_modules:
        print(
            _ok(
                f"assigned {updated.id} to module {name!r} "
                f"(modules: {', '.join(sorted(existing_modules | {name}))})"
            )
        )
    else:
        print(_ok(f"assigned {updated.id} to module {name!r}"))
    return 0


def _reopen_hazard_reason(ticket: Ticket) -> str:
    """`""` when `ticket` is safe to `backend.update()`; otherwise the refusal reason.

    Catches BOTH stale-mismatch directions (Opus review finding, task-cli#154 round 2): the
    original guard only caught `provider_closed=True` with an active-looking `state` (would
    silently REOPEN on update — `GitHubIssuesBackend.update` always PATCHes `state` from
    `ticket.state`, never from `provider_closed`, the same mismatch `stale_notice.py` already
    treats as authoritative). The mirror case — `state` in `DONE`/`CANCELLED` but
    `provider_closed=False` (e.g. natively reopened in the GitHub UI with a stale `status:done`
    label) — would just as silently RE-CLOSE a ticket on update, via the identical mechanism in
    the opposite direction. Both are refused: `ticket.provider_closed` must agree with whether
    `state` is a closed state.

    This is a narrow, task-cli#154-scoped guard — it does not touch `backend.update()`'s general
    PATCH behavior (a pre-existing, wider issue: task-cli#158), it only stops THIS command's
    write path from walking into it, and only for a ticket the command is ABOUT to write (see
    `cmd_link`'s docstring for why it's not a blanket pre-check on every ticket named on the
    command line)."""
    state_says_closed = ticket.state in (State.DONE, State.CANCELLED)
    if ticket.provider_closed == state_says_closed:
        return ""
    verb = "reopen" if ticket.provider_closed else "re-close"
    return (
        f"{ticket.id}'s tracked state ({ticket.state.value!r}) disagrees with the backend's "
        f"own open/closed flag (a stale managed label) — writing to it would silently {verb} "
        "it, since the backend re-derives the native open/closed flag from that same stale "
        f"state on every update. Reconcile the label first (e.g. `task status {ticket.id} "
        "done`/`in-progress` to match reality), then link it."
    )


def _resolve_ticket_for_link(cfg, ticket_id: str) -> tuple[Any, Ticket]:
    """Fetch one side of a `task link` call, returning the SAME backend instance the fetch used
    (so `cmd_link` never re-resolves `_backend_for_id` for the same ticket a second time — the
    `_routing_for_id` docstring flags exactly that as a prior review finding). Converts a
    backend failure into `_UserError` — including `AmbiguousBackendError` (review finding,
    task-cli#154 round 2, Grok): this is a plain read with no side effects, so an ambiguous
    outcome here is exactly as harmless as a clean one, matching `cmd_check`'s identical
    `except (BackendError, AmbiguousBackendError)` convention for its own `backend.get()`."""
    backend = _backend_for_id(cfg, ticket_id)
    from .backends import AmbiguousBackendError, BackendError

    try:
        return backend, backend.get(ticket_id)
    except (BackendError, AmbiguousBackendError) as exc:
        raise _UserError(str(exc)) from exc


def _print_link_result(args: argparse.Namespace, outcomes: list[_LinkOutcome]) -> None:
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "ok": all(o.status not in ("failed", "ambiguous") for o in outcomes),
                    "links": [
                        {
                            "ticket": o.ticket.id,
                            "key": o.entry.key,
                            "url": o.entry.url,
                            "changed": o.changed,
                            "status": o.status,
                            "error": o.error or None,
                        }
                        for o in outcomes
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    unresolved = [o for o in outcomes if o.status in ("failed", "ambiguous")]
    for o in outcomes:
        if o.status == "failed":
            print(_err(f"failed {o.ticket.id}: {o.entry.key} -> {o.entry.url}  ({o.error})"))
        elif o.status == "ambiguous":
            print(
                _warn(
                    f"ambiguous {o.ticket.id}: {o.entry.key} -> {o.entry.url}  "
                    f"(connection dropped after the request — outcome unknown: {o.error})"
                )
            )
        else:
            verb = "linked" if o.status == "linked" else "already linked"
            print(_ok(f"{verb} {o.ticket.id}: {o.entry.key} -> {o.entry.url}"))
    if unresolved:
        print(
            _warn(
                "not fully linked — re-run the same `task link` command to retry; "
                "already-written entries are skipped (idempotent, safe even after an "
                "ambiguous outcome)."
            )
        )


def cmd_classify(args: argparse.Namespace) -> int:
    """Entry point for `task classify`: split a multi-ask message, then classify each part.

    Splitting happens BEFORE classification (`classify.detect_multi_ask` is a pure structural
    heuristic — see its module docstring for why) and ONLY for the auto-CREATE path
    (`args.create` and not `args.update`): a plain `task classify "<text>"` with neither flag is
    classification-only — it prints exactly one verdict and has no side effect either way — and
    a caller relying on that single-verdict-per-call contract must not silently get two verdicts
    for a multi-ask message (review finding: an earlier version gated on `not args.update` alone,
    so a classification-only call with a numbered-list message ran the classifier TWICE with no
    `--create` to justify it). `--update` means the caller already picked ONE target ticket for
    the whole raw message, so splitting it there would be a surprising behavior change to a
    narrower, explicit operation. Each part is classified/created independently through
    :func:`_classify_one`, which holds the ENTIRE former body of this function unchanged.

    The split DECISION runs on the tg-cli-inbound-wrap-STRIPPED text (review finding, P1): the
    real hook payload shape is `[TG from Alex tg#42] 1. fix login\n2. update email` — the wrap
    sits on the SAME line as item 1, so `detect_multi_ask`'s list-marker regex (anchored at
    column 0) never matched line 1 against the raw wrapped text, and the message silently never
    split at all. `msgrefs.strip_inbound_wrap` is the SAME stripper `_derive_title_from_message`
    already uses. The single-item (non-split) fallthrough below still passes the ORIGINAL
    `args.text` (wrap included) to `_classify_one`, unchanged from before this fix, since that
    path's own downstream title-derivation already strips the wrap itself.

    The wrap is RE-ATTACHED to every resulting part (review finding, P1 — an earlier version of
    this fix stripped it for the split decision and then simply discarded it): the wrap's own
    `tg#<id>` is what `_classify_create`'s `_expand_msgrefs` call finds and quotes into `what` —
    the single-message path gets this because the wrap stays embedded in `args.text` there. A
    split part built from the UNWRAPPED text alone would carry no `tg#<id>` at all, silently
    losing the message-reference/quote every part would otherwise get. Each part is prefixed
    with the SAME wrap the original message had (or left alone if there was none), so every
    resulting ticket still traces back to the one inbound message they all came from — the wrap
    is stripped again per-part by `_derive_title_from_message`, exactly as the single-item path
    already does, so it never leaks into a derived title.
    """
    if args.batch:
        if args.text is not None or args.create or args.update:
            raise _UserError("--batch reads JSONL from stdin: it takes no TEXT and no --create/--update")
        return _classify_batch(args, _load(args))
    if args.text is None:
        raise _UserError("classify needs the message TEXT (or --batch with JSONL on stdin)")
    cfg = _load(args)
    if args.create and not args.update:
        from .classify import detect_multi_ask
        from .msgrefs import strip_inbound_wrap

        stripped = args.text.strip()
        unwrapped = strip_inbound_wrap(stripped)
        wrap = stripped[: len(stripped) - len(unwrapped)]
        parts = detect_multi_ask(unwrapped)
        if len(parts) > 1:
            return _classify_multi([wrap + p for p in parts], args, cfg)
    return _classify_one(args, cfg)


def _classify_multi(parts: list[str], args: argparse.Namespace, cfg) -> int:
    """Classify/create each distinct ask found by `detect_multi_ask` separately.

    Prints one "N distinct asks detected" banner, then reuses `_classify_one` per part via a
    shallow `Namespace` copy with `.text` swapped — every downstream behavior (dedup, auto-skip,
    session recording) stays byte-for-byte identical to the single-message path. Returns the
    WORST (highest) exit code across parts, so a caller checking `rc == 0` still sees a failure
    if any single part failed, even though the others may have succeeded.

    Passes a SHARED, growing `batch_resolved_ids` set to every part (review finding, P1): without
    it, a genuinely two-part split like "- Fix iOS login error\n- Fix Android login error" would
    create ticket #1 for "Fix iOS login error", then — because `_classify_create` re-fetches the
    session's candidates AFRESH for part 2 and #1 is now IN that list — part 2's own dedup check
    would score #1 as a near/token duplicate of itself and fold "Fix Android login error" into a
    COMMENT on #1 instead of creating a second ticket. That defeats the entire point of having
    just split them: `detect_multi_ask` already decided these are separate asks, so dedup within
    the SAME batch is never correct — only dedup against tickets that existed BEFORE this
    `classify` call is meaningful. `_classify_create` excludes every id in this set from its
    candidates and adds its OWN part's resolved id to it before returning — whether that id came
    from a fresh CREATE or an EARLIER part's dedup-comment match (review finding, P1: a part
    that matched a pre-existing ticket via dedup must ALSO be excluded from a later part's
    candidates, not only a part that created a brand new one — the near-exact matcher's 0.7
    ratio threshold is lenient enough on templated titles, e.g. "Fix iOS login authentication
    error" vs "Fix Android login authentication error" at 0.889, that a later part can otherwise
    collapse into whatever an earlier part already resolved to) — so ticket 2 never "sees"
    ticket 1's resolution (or ticket 3 tickets 1 and 2's, etc.).

    The banner/progress lines go to STDERR, not stdout: `_classify_create`'s own comment notes
    "classify --create is the tg-cli inbound hook path... it must not append a session-attention
    notice because the hook consumes stdout programmatically" — stdout here is machine-consumed
    (one verdict/`created <id>` line per part), so a human-facing progress banner must not land
    there either.

    **KNOWN INTEGRATION RISK, not fixed here (review finding, "High"):** a multi-ask message
    now emits N stdout units (one per part) instead of one — a caller that assumed exactly one
    verdict/outcome per `classify` call (e.g. the `tg-cli` inbound hook — AGENTS.md's "Out of
    scope" section explicitly says that hook is a separate repo, not built or testable here) may
    only process the FIRST unit and silently drop the rest. Coordinate a hook-side update (or a
    kill switch / opt-in on the hook side) BEFORE this reaches any environment using that hook;
    do not treat this docstring note as the fix.

    Two failure-isolation rules, both review findings ("High"), keep one bad part from silently
    losing the others:
      - A `_UserError` from one part (e.g. a non-skippable gate refusing that part's derived
        title) is caught HERE, reported the same way `main()`'s top-level handler would
        (`error: ...`, contributing exit 2), and the loop CONTINUES to the next part — an
        earlier version let this exception unwind the whole loop, silently never classifying or
        creating anything for the remaining parts.
      - An AMBIGUOUS create (`_ClassifyBatchAmbiguous`, raised by `_classify_create` — see its
        docstring) STOPS the batch entirely rather than continuing: with no confirmed id for the
        possibly-created ticket, a later part's dedup check can't safely exclude it, so
        continuing risks silently folding a genuinely distinct ask into a comment on a ticket
        that may or may not actually exist.
    """
    import copy

    print(
        _warn(f"  {len(parts)} distinct asks detected in this message — classifying each separately"),
        file=sys.stderr,
    )
    worst = 0
    batch_resolved_ids: set[str] = set()
    for i, part in enumerate(parts, 1):
        # Deliberately no raw-text preview here: `part` is untrusted inbound content and could
        # itself contain something that LOOKS like tool status (e.g. a crafted "VERDICT:
        # actionable" — see test_classify_echoed_injected_verdict_does_not_bypass_ticket_creation,
        # the same injection class `classify.parse_verdict` already guards against for the
        # model's own echo). An index-only progress line can't be confused with a real verdict.
        print(_dim(f"  [{i}/{len(parts)}] classifying..."), file=sys.stderr)
        item_args = copy.copy(args)
        item_args.text = part
        try:
            worst = max(worst, _classify_one(item_args, cfg, batch_resolved_ids=batch_resolved_ids))
        except _ClassifyBatchAmbiguous as exc:
            remaining = len(parts) - i
            print(
                _err(
                    f"error: ambiguous create for ask {i}/{len(parts)} — stopping the batch; "
                    f"{remaining} remaining ask(s) were not processed"
                )
            )
            return max(worst, exc.exit_code)
        except _UserError as exc:
            print(_err(f"error: {exc}"))
            worst = max(worst, 2)
    return worst


def _classify_one(
    args: argparse.Namespace, cfg, batch_resolved_ids: set[str] | None = None
) -> int:
    """Classify ONE message (`args.text`) and act on the verdict — the pre-multi-ask body of
    `cmd_classify`, extracted so `_classify_multi` can call it once per split part.

    `batch_resolved_ids` (only ever non-`None` when called FROM `_classify_multi`) is forwarded
    to `_classify_create`'s dedup check — see that function's docstring for why a multi-ask
    batch must not dedup its own parts against each other.

    On a `change` verdict, also resolves the three ticket-shaping axes (`classify.
    parse_classification` — story points, priority, process-vs-product) from the SAME model
    round-trip, and surfaces them alongside the verdict in both human and `--json` output.
    `actionable`/`justAsk` never mint a ticket, so their axes are never surfaced (moot) even
    though the model answers them regardless (parsing never branches on the verdict). A
    `process`-classified `change` routes `--create` to a local `harness tasks new` ticket
    instead of the configured product backend (`_classify_route_to_harness`), unless
    `classify.route_process_to_harness` is disabled, in which case it falls through to the
    normal product-ticket path unchanged.
    """
    from .classify import Verdict

    classification, used_label = _classify_text(
        args.text, _classifier_chain(args, cfg), _classify_bias(cfg)
    )
    verdict = classification.verdict
    # `actionable`/`justAsk` never mint a ticket, so their axes are moot — computed once here
    # rather than repeating `verdict is Verdict.CHANGE` at every output site.
    axes = classification if verdict is Verdict.CHANGE else None
    _log_classification(classification, used_label)

    if args.json:
        import json

        print(json.dumps(_classification_payload(classification), ensure_ascii=False))
    else:
        print(f"{verdict.value}")
        if axes is not None:
            print(f"  {axes.process_or_product} / {axes.priority} / {axes.story_points}sp")

    if verdict is Verdict.JUST_ASK:
        return 0
    if verdict is Verdict.ACTIONABLE:
        # task-cli#80: an instruction to execute an existing command/tool directly must never
        # silently spawn a placeholder ticket, even with --create/--update. Actually invoking
        # the action is out of scope for this repo (AGENTS.md "Out of scope" — the tg-cli
        # inbound hook owns real execution); this only needs to not file a ticket for it.
        return 0
    # verdict == change
    if args.update:
        return _classify_append(args, cfg)
    if args.create:
        # verdict is CHANGE here (JUST_ASK/ACTIONABLE returned above), so axes is never None.
        if axes.process_or_product == "process" and cfg.classify_route_process_to_harness:
            # `_which_harness()` is checked ONCE here, never re-probed inside
            # `_classify_route_to_harness` — see that function's docstring for the full TOCTOU
            # race this closes: the axis correctly resolved to `process`, so this is NOT the
            # "ambiguous axis" case `default_classification`'s product-bias already guards —
            # it's a DIFFERENT failure mode (a confidently-process-classified item whose target
            # store, harness-cli, isn't installed) that must fall back to the SAME safe default
            # rather than erroring out and dropping the message with no ticket filed anywhere. A
            # harness binary that IS present but fails AT RUNTIME (non-zero exit, timeout) is a
            # different, genuinely actionable error and still surfaces as one —
            # `_classify_route_to_harness` handles that case unchanged.
            harness = _which_harness()
            if harness is not None:
                return _classify_route_to_harness(args, harness)
            print(
                _warn(
                    "  `harness` binary not found on PATH; filing as a product ticket instead "
                    "of a local process ticket (install harness-cli, or this is expected "
                    "behavior — nothing is ever silently dropped)"
                ),
                file=sys.stderr,  # a warning, not the payload stdout callers parse
            )
        return _classify_create(args, cfg, classification, batch_resolved_ids=batch_resolved_ids)
    return 0


def _classify_bias(cfg) -> "Verdict":
    """The verdict to fall back to when no provider classifies (`classify_bias`).

    `actionable` (task-cli#80) is deliberately EXCLUDED from the configurable-bias set (review
    finding): it's a genuine model-signaled verdict, never a "when confused, assume this"
    default. Allowing it as a bias would mean every no-provider/ambiguous message under
    `classify_bias: actionable` silently drops (no ticket, no execution) instead of degrading
    to a safe extra ticket (`change`) or a safe no-op question (`justAsk`).
    """
    from .classify import Verdict

    return Verdict(cfg.classify_bias) if cfg.classify_bias in ("change", "justAsk") else Verdict.CHANGE


def _classifier_chain(args: argparse.Namespace, cfg) -> list:
    """The ordered provider chain for this invocation (task-cli#206, `tasklib/classifiers.py`).

    `cfg.classify_fallbacks` is passed through AS-IS (task-cli#208 review finding) -- never
    coalesced to `None` on an empty list. `config.py`'s cascade always supplies SOME fallbacks
    list (`DEFAULTS` at minimum), so `[]` here means either a repo explicitly wrote
    `classify: {fallbacks: []}` (deliberately disabling every classifier, falling straight to
    the bias default), OR every configured entry was malformed and silently dropped
    (`config.LoadedConfig.classify_fallbacks` only keeps single-key `{provider: model}` maps).
    Coalescing `[]` to `None` would silently reinterpret EITHER case as "unconfigured" and hand
    it the FULL zero-config default chain instead, breaking the "explicit config is never
    altered" invariant. A malformed-entries-only config silently degrading to the bias default
    is a pre-existing gap this does not newly fix.
    """
    from .classifiers import build_classifier_chain

    return build_classifier_chain(
        cfg.classify_fallbacks,
        capability=cfg.classify_capability or None,
        verbose=bool(getattr(args, "verbose", False)),
    )


def _classify_text(text: str, providers: list, bias: "Verdict") -> tuple["Classification", str]:
    """Classify one message: guard rules around the first provider that answers.

    Walks `providers` and uses the first one that is BOTH available AND actually produces a
    classification -- a provider that looks reachable but fails at runtime (e.g. laya's model
    download/predict) falls through to the next one. No answer at all -> `classify_bias` with
    classify.py's fixed safe axes (`default_classification`), plus a stderr warning.

    task-cli#245 guard rules (`classify.is_small_talk`/`looks_like_bug_report`, applied to the
    tg-cli-inbound-wrap-stripped body): a greeting or tiny text is `justAsk` WITHOUT running any
    provider, and a `justAsk` verdict on plain defect prose is corrected to `change`
    (keeping the provider's axes). Returns `(classification, used_label)`; a rule's involvement
    shows in the label (`rule:small-talk`, `<provider>+rule:bug-report`).
    """
    import dataclasses

    from .classify import Verdict, default_classification, is_small_talk, looks_like_bug_report
    from .msgrefs import strip_inbound_wrap

    body = strip_inbound_wrap(text.strip())
    if is_small_talk(body):
        return default_classification(Verdict.JUST_ASK), "rule:small-talk"
    classification = None
    used_label = "none"
    for provider in providers:
        if not provider.available():
            continue
        classification = provider.classify(text, bias)
        if classification is not None:
            used_label = provider.label
            break
    if classification is None:
        classification = default_classification(bias)
        print(
            _warn(f"  no classifier provider available; biasing to {classification.verdict.value}"),
            file=sys.stderr,  # a warning, not the verdict/axes/--json unit stdout callers parse
        )
    # Only justAsk: an `actionable` verdict must never become a ticket (task-cli#80).
    if classification.verdict is Verdict.JUST_ASK and looks_like_bug_report(body):
        classification = dataclasses.replace(classification, verdict=Verdict.CHANGE)
        used_label = f"{used_label}+rule:bug-report"
    return classification, used_label


def _classification_payload(classification: "Classification") -> dict:
    """The `--json` unit: the verdict, plus the ticket-shaping axes only on `change`."""
    from .classify import Verdict

    payload: dict = {"verdict": classification.verdict.value}
    if classification.verdict is Verdict.CHANGE:
        payload["process_or_product"] = classification.process_or_product
        payload["priority"] = classification.priority
        payload["story_points"] = classification.story_points
    return payload


def _log_classification(classification: "Classification", used_label: str) -> None:
    from .logging import log_event

    payload = _classification_payload(classification)
    log_event(
        "classify",
        verdict=payload["verdict"],
        model=used_label,
        process_or_product=payload.get("process_or_product"),
        priority=payload.get("priority"),
        story_points=payload.get("story_points"),
    )


def _classify_batch(args: argparse.Namespace, cfg) -> int:
    """`task classify --batch`: stdin JSONL in, stdout JSONL out, one provider chain for all.

    Each input line is `{"text": "...", "id": <anything>?}`; each output line is that message's
    `--json` unit with `id` echoed when given. Building the chain ONCE is the point
    (task-cli#245): a grooming pass over N tickets pays the provider's startup (the local
    model's load, when the warm server is off) once instead of N times. Classification only --
    no ticket side effects. A bad line yields `{"line": n, "error": ...}` (plus its `id` when it
    had one) and the batch continues; the exit code is then 2.
    """
    import json

    providers = _classifier_chain(args, cfg)
    bias = _classify_bias(cfg)
    rc = 0
    for lineno, raw in enumerate(sys.stdin, 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            item = None
        if not isinstance(item, dict):
            print(
                json.dumps({"line": lineno, "error": "not a JSON object"}, ensure_ascii=False),
                flush=True,
            )
            rc = 2
            continue
        head = {"id": item["id"]} if "id" in item else {}
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            print(
                json.dumps(
                    {**head, "line": lineno, "error": 'missing a non-empty "text" string'},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            rc = 2
            continue
        classification, used_label = _classify_text(text, providers, bias)
        _log_classification(classification, used_label)
        print(
            json.dumps({**head, **_classification_payload(classification)}, ensure_ascii=False),
            flush=True,
        )
    return rc


def _inferred_module_labels(cfg, text: str) -> list[str]:
    """`classify.infer_module` opt-in (`cfg.classify_infer_module`, default False): a cheap
    keyword-overlap heuristic (`tasklib.modules.infer_module`) against the repo's configured
    `modules:` taxonomy, applied as a single `module:<name>` label on a freshly-CREATED ticket
    only — never on a dedup-fold-in comment, mirroring `sp:<n>`/`priority:<Pn>`'s same scoping.
    Returns `[]` (no-op) for a repo with no taxonomy configured or the opt-in left at its
    default False — this must never change existing behavior for a repo that hasn't opted in.
    """
    if not cfg.classify_infer_module:
        return []
    from .modules import infer_module, label_for, load_taxonomy

    taxonomy = load_taxonomy(cfg)
    name = infer_module(taxonomy, text)
    return [label_for(name)] if name else []


def _classify_create(
    args: argparse.Namespace,
    cfg,
    classification: "Classification",
    batch_resolved_ids: set[str] | None = None,
) -> int:
    """`change` + --create: dedup against the session, else create from the message.

    `batch_resolved_ids` (see `_classify_multi`'s docstring for the full rationale): when this
    call is one part of a multi-ask batch, every id already RESOLVED by an EARLIER part in the
    SAME batch — whether created fresh or matched via dedup — is excluded from the dedup
    candidate set. The splitter already decided these are distinct asks, so one part matching
    another part's resolution is never a real duplicate, only same-batch noise. This function
    adds its own part's resolved id to the set before returning (on EITHER the create path or
    the dedup-comment path), so a LATER part in the batch also excludes it.

    `classification` (the `classify.Classification` `_classify_one` already resolved for this
    part) supplies the `sp:<n>`/`priority:<Pn>` labels stamped on a freshly-CREATED ticket —
    never on a dedup-fold-in comment, since that path touches an EXISTING ticket whose own
    labels (possibly from an earlier, different classification) must not be silently relabeled.
    """
    backend = _backend(cfg)
    session = _detect_session(cfg)
    from .backends import BackendError

    match = None
    advisory_match = None
    candidates: list[Ticket] = []
    if _enforce_config(cfg).duplicate:
        # dedup: same session + high title similarity (near-exact wording) AUTO-FOLDS into a
        # comment, exactly as before this ticket's changes — that matcher is the original,
        # already-tested, high-confidence check. The broader token/Jaccard net
        # (policy.best_duplicate_match) is ADVISORY ONLY here (review finding, P1 — "High"): it
        # catches a REWORDED duplicate the near-exact check misses (the shape the ecosystem
        # audit found `_best_dedup_match` alone was missing, 13 confirmed duplicates from this
        # exact auto-create path; task-cli intake gates, 2026-09), but it is also lower-
        # confidence and has a CONFIRMED false-positive class (two DIFFERENT tickets sharing
        # dense domain vocabulary — see policy.py's "Encrypt API token storage" vs "API token
        # storage rotation" pinned example). On `task new`, that false positive is a soft,
        # SKIPPABLE gate (`--skip-duplicate`/`--force`). `classify` has no such escape — its
        # parser exposes only `--create`/`--update`, and it is typically driven autonomously by
        # the tg-cli hook with no human in the loop to add a flag — so silently auto-folding a
        # genuinely NEW request into an unrelated ticket's comment thread would be an
        # unrecoverable, invisible action with zero override. Print a warning naming the
        # candidate instead and CREATE the ticket normally; both mechanisms are still gated
        # together behind `enforce.duplicate` — an earlier version left `_best_dedup_match`
        # unconditional, so `enforce.duplicate: false` silently failed to disable the near-exact
        # half of dedup on this path.
        candidates = _session_dedup_candidates(backend, session)
        if batch_resolved_ids:
            candidates = [c for c in candidates if c.id not in batch_resolved_ids]
        match = _best_dedup_match(args.text, candidates)
        if match is None:
            from datetime import datetime, timezone

            from .policy import best_duplicate_match

            advisory_match = best_duplicate_match(
                _derive_title_from_message(args.text),
                args.text,
                candidates,
                new_session=session.label,
                now=datetime.now(timezone.utc),
            )
    if advisory_match is not None:
        # `.title` is UNTRUSTED (a candidate ticket read back from the backend) — sanitized with
        # the SAME `policy.safe_display` the duplicate-detection Violation message already uses,
        # for the same reason (review finding, P2: a crafted title containing a terminal escape
        # sequence, printed verbatim, could clear/spoof the console).
        from .policy import safe_display

        print(
            _warn(
                f"  possible duplicate of {advisory_match.id}  {safe_display(advisory_match.title)} "
                "(token-similarity match, not exact — creating a new ticket anyway; "
                f"task read '{advisory_match.id}' to check)"
            ),
            file=sys.stderr,
        )
    if match is not None:
        from .backends import AmbiguousBackendError

        try:
            backend.comment(match.id, f"(restated) {args.text.strip()}")
        except AmbiguousBackendError as exc:
            # review finding: the comment call may have already landed server-side before the
            # connection dropped — same ambiguity `_create_ticket_or_ambiguous` handles for a
            # fresh create, but this is the DEDUP-FOLD-IN path, which previously let this
            # exception propagate uncaught (a raw traceback). That was tolerable as an isolated
            # single-command crash; inside a multi-ask BATCH it would also silently abandon
            # every remaining part with no controlled exit code — so this path now needs the
            # same explicit handling `cmd_create`'s create path already has.
            print(_err(f"ambiguous: {exc}"))
            print(
                _warn(
                    f"  the comment on {match.id} may have already been posted — check the "
                    "backend before retrying, to avoid a duplicate comment."
                ),
                file=sys.stderr,  # advisory, not the machine-consumed unit — mirrors the
                # possible-duplicate warning above.
            )
            if batch_resolved_ids is not None:
                raise _ClassifyBatchAmbiguous(exc.exit_code) from exc
            return exc.exit_code
        except BackendError as exc:
            raise _UserError(str(exc)) from exc
        if batch_resolved_ids is not None:
            # review finding (P1): a LATER batch part must exclude a ticket an EARLIER part
            # matched via dedup, not only a ticket an earlier part CREATED — otherwise the
            # near-exact matcher (0.7 ratio) can fold a genuinely distinct part into the SAME
            # pre-existing ticket a sibling part just matched, exactly the same "detect_multi_ask
            # already decided these are separate" violation `batch_resolved_ids` exists to
            # prevent for freshly-created tickets. Symmetric with the `.add(created.id)` below.
            batch_resolved_ids.add(match.id)
        _record_session_touch(cfg, backend, match)
        print(_ok(f"appended to {match.id} (dedup)"))
        return 0

    # create from message: an inbound message can't carry full criteria, so the draft is
    # filled with triage placeholders. Rather than silently bypassing policy, we RUN the gates
    # and record every still-failing SKIPPABLE gate as an explicit, auditable auto-skip — so the
    # ticket is always policy-clean by construction and the bypass is visible in the body. The
    # title is derived the SAME way cmd_create's --from-message path does (strip a leading tg-cli
    # inbound wrap first) — this is, after all, the exact hook this classify path exists for
    # (task-cli#45 review finding: the two paths must not derive a title differently).
    what_raw = args.text.strip()
    title = _derive_title_from_message(args.text)
    if not title:
        # A wrap-only inbound message (`[TG from Alex tg#1234]`, no content after it — a
        # malformed or truncated hook call) derives an EMPTY title. cmd_create's --from-message
        # path already refuses this (`if not title: raise _UserError(...)`); this path must
        # refuse the same way rather than silently create a titleless ticket (task-cli#45
        # review finding: the two paths must not diverge on this either).
        raise _UserError(
            "cannot auto-create from this message — no title could be derived from it"
        )
    ticket = Ticket(
        title=title,
        what=what_raw,
        why="(auto-created from an inbound message; needs triage)",
        user_impact="(needs triage)",
        cost_of_inaction="(needs triage)",
        acceptance=[Criterion(text="triage this request and fill in the criteria")],
        labels=list(
            dict.fromkeys(
                [
                    *cfg.section("github").get("default_labels", []),
                    session.label,
                    "needs-triage",
                    f"sp:{classification.story_points}",
                    f"priority:{classification.priority}",
                    *_inferred_module_labels(cfg, args.text),
                ]
            )
        ),
        # See the matching NOTE in cmd_create: the session id belongs in `labels` only, never
        # duplicated into `links` (which is for real URL references like a PR link, not session
        # metadata — the Links renderer does not enforce URLs, so junk there slips through).
    )
    blocking = _auto_skip_failing_gates(ticket, cfg)
    if blocking:
        # A NON-skippable gate still fails after auto-skipping everything that CAN be skipped —
        # e.g. msgref-title, if the title-derivation fix above somehow still let a tg#<id>
        # through. Refuse loudly rather than silently persist a ticket that violates a rule with
        # no legitimate exception (task-cli#45 review finding: this is exactly the scenario the
        # non-skippable gate exists to prevent, and it must never be bypassed here either).
        names = "; ".join(
            f"{v.gate}: {v.message}" + (f" ({v.hint})" if v.hint else "")
            for v in blocking
        )
        raise _UserError(
            f"cannot auto-create from this message — non-skippable gate(s) failed: {names}"
        )
    # Expand tg#<id> mentions in `what` the SAME way cmd_create's --from-message path does — this
    # classify path is ALSO a tg-cli hook entry point (it IS the `task classify "<msg>" --create`
    # call an agent makes from an inbound message), so it must not be the one path that silently
    # drops the quote a reader would otherwise get everywhere else (task-cli#45 review finding).
    # Honours the same master switch as the other paths: no expansion (nor history read) when
    # `enforce.msgref_quote: false` or the reference is waived (review finding).
    if _msgref_expand_enabled(ticket.skips, cfg):
        ticket.what = _expand_msgrefs(
            ticket.what, _load_msgref_history_if_needed(what_raw)
        )

    created, ambiguous_exit = _create_ticket_or_ambiguous(backend, ticket)
    if ambiguous_exit is not None:
        if batch_resolved_ids is not None:
            # We're inside a multi-ask batch and have no confirmed id for this possibly-created
            # ticket to exclude from a LATER part's dedup candidates — see
            # `_ClassifyBatchAmbiguous`'s docstring for why the only safe response is to stop.
            raise _ClassifyBatchAmbiguous(ambiguous_exit)
        return ambiguous_exit
    if batch_resolved_ids is not None:
        batch_resolved_ids.add(created.id)  # a LATER batch part must not dedup against this one
    from .session import record

    record(
        session.id, created.id, created.title, repo=_safe_backend_coordinate(backend)
    )
    # task-cli#119: creating a ticket counts as reading it — see _mark_ticket_read's docstring.
    # `session=session` reuses the detection this function already did above (review finding,
    # task-cli#119) rather than paying a second `_detect_session` call.
    _mark_ticket_read(cfg, backend, created, session=session)
    # classify --create is the tg-cli inbound hook path; unlike cmd_create, it must not append
    # a session-attention notice because the hook consumes stdout programmatically.
    print(_ok(f"created {created.id} from message  {created.url}"))
    return 0


def _auto_skip_failing_gates(ticket: Ticket, cfg) -> list:
    """Run the create gates and record any failing SKIPPABLE gate as an auditable auto-skip.

    Used by the inbound-classify path, where a message can't satisfy every gate up front. A
    skippable gate's waiver is recorded, visible in the ``Skipped gates`` section — never a
    silent bypass. Returns any violations that are NOT skippable (see
    ``policy.NON_SKIPPABLE_GATES``): those are NEVER auto-skipped here — a gate with no
    legitimate exception (e.g. ``msgref-title``) must not be silently waived just because this
    path can't fill in every field up front (task-cli#45 review finding). The caller must refuse
    the create when this returns anything.

    Also prints any advisory ``result.warnings`` to stderr (review finding): this is the ONLY
    call site on the classify auto-create path that ever runs ``check()``, so it is also the
    only place the todo-shaped hint (``PolicyResult.warnings``, never ``.violations`` — it must
    never block) can surface. Without this, `task classify "Adversarial review of uncommitted
    safe-cleanup diff" --create` created a ticket with NO hint ever printed — the exact
    inbound-hook path the audit that motivated this hint was itself about.
    """
    from .policy import NON_SKIPPABLE_GATES, Phase, check

    result = check(ticket, _enforce_config(cfg), Phase.CREATE)
    for w in result.warnings:
        print(_warn(f"  ⚠ {w.gate}: {w.message}"), file=sys.stderr)
    blocking = []
    for v in result.violations:
        if v.gate in NON_SKIPPABLE_GATES:
            blocking.append(v)
        else:
            ticket.skips.setdefault(
                v.gate, "auto-created from inbound message; pending triage"
            )
    return blocking


def _classify_append(args: argparse.Namespace, cfg) -> int:
    backend = _backend(cfg)
    from .backends import BackendError

    try:
        backend.comment(args.update, f"(restated) {args.text.strip()}")
    except BackendError as exc:
        raise _UserError(str(exc)) from exc
    try:
        ticket = backend.get(args.update)
    except BackendError:
        ticket = None
    if ticket is not None:
        _record_session_touch(cfg, backend, ticket)
    print(_ok(f"appended to {args.update}"))
    return 0


def _classify_route_to_harness(args: argparse.Namespace, harness: str) -> int:
    """`change` + `--create`, classified `process` (`classify.py`'s process/product axis):
    file this as a LOCAL harness-cli process ticket instead of a product-tracker ticket.

    harness-cli is a local peer tool, not a `TicketBackend` — this is reached ONLY by shelling
    out to its already-installed `harness` binary on PATH (mirroring this module's existing
    `review just-ask` shell-out; AGENTS.md's "Backends call the provider API directly" rule
    governs GitHub/Linear, not this local-process sibling). A non-zero exit or a spawn failure
    is reported as a clean `error:` line, never a raw traceback — the caller (often the
    unattended tg-cli inbound hook) needs an observable failure, not a crash.

    `harness` is the ALREADY-RESOLVED `shutil.which("harness")` path, passed in by the sole
    caller (`_classify_one`) rather than re-probed here (review finding, TOCTOU — a second,
    independent `_which_harness()` call here could disagree with the caller's check, e.g. the
    binary is removed from PATH between the two calls, silently defeating the caller's
    "harness missing -> fall back to the product ticket path, never drop the message"
    guarantee: THIS function has no such fallback, it only ever errors on failure, so a
    caller-vs-callee split-brain here would re-introduce the exact silent-drop bug the
    fallback was added to close). One resolved path, one decision, no race.

    Every printed fragment below is `redact`ed then `safe_display`d before it reaches stdout
    (review finding, security): `args.text` is UNTRUSTED inbound content that can contain a
    token-shaped secret or a terminal-escape/stdout-injection payload, and this function's
    stdout is the SAME stream the tg-cli inbound hook parses. A raw `subprocess.TimeoutExpired`
    stringifies to its full argv (which embeds the untrusted `--body`), so that exception is
    NEVER interpolated directly — mirrors `_run_review_just_ask`'s existing fail-quiet posture
    for its own subprocess call, which already never echoes the prompt back on failure.

    `--title=<value>`/`--body=<value>` (glued, not a separate token) is deliberate (review
    finding): the derived title or the raw message can itself start with a `-` (an inbound
    "- fix the login page" bullet, or a short dash-prefixed message like "-x"), and argparse
    (harness-cli's own, like this repo's) reads a SEPARATE `--title -x` as "expected one
    argument" whenever the value token alone looks option-like — the glued form has no such
    ambiguity for any value, regardless of what it starts with.
    """
    title = _derive_title_from_message(args.text)
    if not title:
        raise _UserError(
            "cannot route to harness — no title could be derived from this message"
        )
    from .logging import redact
    from .policy import safe_display

    try:
        result = subprocess.run(
            [harness, "tasks", "new", f"--title={title}", f"--body={args.text.strip()}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Never interpolate the exception itself: `TimeoutExpired.__str__`/`CalledProcessError`
        # both embed the full argv, which carries the untrusted message via `--body`.
        print(_err("error: `harness tasks new` failed to run (see harness-cli's own logs)"))
        return 1
    if result.returncode != 0:
        detail = safe_display(str(redact((result.stderr or result.stdout or "").strip())))
        detail = detail.replace("\n", " ")
        print(_err("error: `harness tasks new` failed" + (f": {detail}" if detail else "")))
        return 1
    filed_title = safe_display(str(redact((result.stdout or "").strip() or title))).replace("\n", " ")
    # Human text ONLY, never a second JSON document (review finding): `_classify_one` already
    # printed the verdict/axes payload as the ONE JSON document a `--json` caller gets for this
    # invocation — exactly mirroring `_classify_create`, which never checks `args.json` either
    # and always prints its "created #N"/dedup status as plain text. A caller doing a single
    # `json.loads(stdout)` on `--create --json` output must see exactly one JSON object
    # regardless of which routing branch (harness vs. the product backend) actually fired.
    print(_ok(f"filed local process task via harness: {filed_title}"))
    return 0


def _which_harness() -> str | None:
    import shutil

    return shutil.which("harness")


def _best_dedup_match(text: str, candidates: list[Ticket]) -> Ticket | None:
    """Conservative dedup: pick the candidate whose title is highly similar to the message.

    ``text`` is matched via the SAME title derivation :func:`_derive_title_from_message` uses
    (strip a leading tg-cli inbound wrap, first line, truncate) — not the raw first line — so a
    wrapped inbound message (``[TG from Alex tg#1234] <message>``) is compared against a stored
    candidate's title on equal terms. A stored title is ALREADY wrap-stripped (every ticket this
    path creates goes through the same derivation); comparing it against the raw wrapped first
    line would score a genuine repeat-message low on similarity and create a duplicate ticket
    instead of dedup-commenting on the existing one (task-cli#45 review finding).
    """
    import difflib

    target = _derive_title_from_message(text).lower()
    best: tuple[float, Ticket] | None = None
    for t in candidates:
        if t.state in (State.DONE, State.CANCELLED):
            continue
        ratio = difflib.SequenceMatcher(None, target, t.title.lower()).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, t)
    if best and best[0] >= 0.7:  # high-similarity threshold (conservative; §12b)
        return best[1]
    return None


def _run_review_just_ask(model_arg: str, prompt: str) -> str:
    """Shell out to ``review just-ask "<prompt>" -m <model> --pool 1``. Returns stdout.

    The single classification shell-out. Modes in review-cli are SUBCOMMANDS (``review
    just-ask …``), not flags. ``review`` resolves/calls the model; ``--pool 1`` is one
    fast/cheap model, no panel. A failure returns empty output (the caller biases).
    """
    review = _which_review()
    if not review:
        return ""
    try:
        out = subprocess.run(
            [review, "just-ask", prompt, "-m", model_arg, "--pool", "1"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def _which_review() -> str | None:
    import shutil

    return shutil.which("review")


def cmd_session(args: argparse.Namespace) -> int:
    cfg = _load(args)
    session = _detect_session(cfg)
    if args.action == "bind":
        if not args.bind_id:
            raise _UserError("`task session bind` needs a ticket id")
        from .session import record

        # `bind` had NO backend dependency before task-cli#83 — it's a purely local sidecar
        # write. Resolve the coordinate from config/git alone (`_local_project_coordinate`),
        # NOT by constructing a live backend — the latter is documented "Effectful (harvests
        # creds)" and would add a new credential-harvest side effect to a command that
        # previously required none (review finding), on top of degrading to unscoped in
        # exactly the creds-free environments this command is meant to keep working in.
        record(session.id, args.bind_id, "", repo=_local_project_coordinate(cfg))
        print(_ok(f"bound {args.bind_id} to session {session.id}"))
        return 0

    # show
    from .session import read_entries

    # Filter to THIS repo's coordinate when one resolves (task-cli#83 review finding:
    # `read_ids` itself has no repo scoping, so an unfiltered read leaks another repo's ticket
    # ids into `task session`/`--json` for a session id shared across repos). Resolved from
    # config/git alone (`_local_project_coordinate`) rather than a live backend, so the fix
    # isn't gated behind credentials `task session` never needed before (review finding: a
    # backend-construction failure would otherwise silently fall back to unfiltered in
    # exactly the creds-free environments this leak most needs fixing in). Falls back to ""
    # (unfiltered, matching pre-#83 behavior) only for the genuine outside-a-repo case.
    coordinate = _local_project_coordinate(cfg)
    entries = read_entries(session.id)
    if coordinate:
        entries = [e for e in entries if e.repo == coordinate]
    else:
        # No coordinate to scope by (genuinely outside a repo) — `read_entries` dedups by
        # `(repo, id)`, so two different repos' same-numbered ticket both survive here as
        # separate entries (review finding, task-cli#83): shown unfiltered, that would print
        # an ambiguous "#1, #1" with no way to tell them apart, a REGRESSION from the pre-#83
        # behavior (dedup by bare id only). Collapse to bare-id dedup for this display,
        # keeping the latest occurrence and its position (mirrors `read_entries`'s own
        # pop-then-reinsert "moves to the end" trick) — the same ambiguity the old code had,
        # not a new, worse one.
        deduped: dict[str, object] = {}
        for e in entries:
            deduped.pop(e.id, None)
            deduped[e.id] = e
        entries = list(deduped.values())
    ids = [e.id for e in entries]
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "id": session.id,
                    "source": session.source,
                    "label": session.label,
                    "tickets": ids,
                }
            )
        )
        return 0
    print(_bold(f"session: {session.id}") + _dim(f"  (source: {session.source})"))
    print(_dim(f"  label: {session.label}"))
    if ids:
        print(_dim(f"  tickets ({len(ids)}): {', '.join(ids)}"))
    else:
        print(_dim("  no tickets recorded yet"))
    return 0


def _daemon_coordinate(cfg) -> str:
    """The repo/team coordinate that keys this daemon's state files (one daemon per project).

    Reuses the same single-target resolution every command uses. A daemon is repo-bound — it
    watches ONE project's tickets — so running it outside a repo (no pinned coordinate) is a
    clean 3-part error, never a cryptic backend failure.
    """
    current = _current_project_overlay(cfg)
    if current is None:
        raise _UserError(
            "cannot run the daemon: no project context (you are outside a git repo).\n"
            "  why: the daemon watches ONE project's due dates, so it must know which backend/repo.\n"
            "  fix: run inside the target git repo, or pass `--repo owner/name` / set `linear.team`."
        )
    name, _overlay = current
    return name


def cmd_daemon(args: argparse.Namespace) -> int:
    """Dispatch the daemon lifecycle action (start | stop | status | run)."""
    cfg = _load(args)
    coordinate = _daemon_coordinate(cfg)
    from . import daemon

    if args.action == "run":
        paths = daemon.paths_for(coordinate)
        return daemon.run_loop(cfg, paths)
    if args.action == "start":
        return _daemon_start(daemon, coordinate, cfg, args)
    if args.action == "stop":
        return _daemon_stop(daemon, coordinate)
    return _daemon_status(daemon, coordinate, cfg, args)


def _daemon_child_flags(args: argparse.Namespace) -> list[str]:
    """The backend-selecting global flags to forward to the spawned ``daemon run`` child.

    These are exactly the flags that change coordinate/backend resolution — so the child resolves
    the SAME coordinate the launcher checked (otherwise start isn't idempotent and stop/status
    miss the daemon). ``-C`` is added separately by the spawn.
    """
    flags: list[str] = []
    if getattr(args, "backend", None):
        flags += ["--backend", args.backend]
    if getattr(args, "repo", None):
        flags += ["--repo", args.repo]
    if getattr(args, "config", None):
        flags += ["--config", args.config]
    return flags


def _daemon_start(daemon, coordinate: str, cfg, args: argparse.Namespace) -> int:
    dcfg = daemon.DaemonConfig.from_config(cfg)
    if not dcfg.enabled:
        print(
            _warn("daemon is disabled in config (daemon.enabled: false) — not starting")
        )
        return 0
    outcome, pid = daemon.start(
        coordinate, cwd=str(cfg.repo_root), child_flags=_daemon_child_flags(args)
    )
    if outcome == "already-running":
        print(_dim(f"daemon already running (pid {pid}) for {coordinate}"))
        return 0
    if outcome == "unsupported-interpreter":
        # Refused to spawn at all -- pid is None, no child, no log written for this attempt, so
        # there is nothing on disk to point at (task-cli#61/#63 review finding: an earlier
        # version pointed at a log path here that, for THIS outcome, was never written). Uses
        # daemon's own public helpers (not private-constant/sys.version_info reconstruction) so
        # this can never drift out of sync with the check start() itself made (review finding).
        required = daemon._min_python_for_safepath_str()
        found = daemon._interpreter_version_str()
        print(_warn(f"daemon not started: Python {required}+ required (found {found}) for {coordinate}"))
        return 1
    if outcome == "failed":
        # Bootstrap failure (e.g. an unsupported interpreter flag) — reported as an error instead
        # of the started-then-immediately-stopped flap this readiness check exists to catch
        # (task-cli#61 review finding). The daemon's own log has the real stderr.
        paths = daemon.paths_for(coordinate)
        print(_warn(f"daemon failed to start (pid {pid} exited before bootstrapping) for {coordinate}"))
        print(_dim(f"  see log: {paths.log}"))
        return 1
    if outcome == "no-op":
        # The spawned child exited cleanly without ever becoming the live daemon, and no OTHER
        # daemon is running either -- not a crash, but nothing is running either: a nonzero exit
        # (Fable review finding) so a `task daemon start && ...` script doesn't proceed on the
        # false premise that a daemon is now up.
        paths = daemon.paths_for(coordinate)
        print(_warn(f"daemon did not start (exited without becoming active) for {coordinate}"))
        print(_dim(f"  see log: {paths.log}"))
        return 1
    if outcome == "started":
        print(
            _ok(
                f"daemon started (pid {pid}) for {coordinate}  interval={dcfg.interval_s}s"
            )
        )
        return 0
    # Defensive: an outcome daemon.start() didn't document above must never be reported as quiet
    # success (Fable review finding: an earlier version's fallthrough branch treated ANY unknown
    # outcome as "started").
    print(_warn(f"daemon start returned an unrecognized outcome {outcome!r} for {coordinate}"))
    return 1


def _daemon_stop(daemon, coordinate: str) -> int:
    outcome, pid = daemon.stop(coordinate)
    if outcome == "stopped":
        print(_ok(f"daemon stopped (was pid {pid})"))
    elif outcome == "timeout":
        print(_warn(f"daemon (pid {pid}) did not exit in time; pid-file cleared"))
    elif outcome == "not-ours":
        # the recorded pid is alive but is NOT our daemon (a crash + OS pid-reuse) — we refused to
        # signal it; the stale pid-file is cleared. Surface this so it isn't mistaken for a clean stop.
        print(
            _warn(
                f"pid {pid} is alive but is not the task daemon (recycled pid); not signalled, pid-file cleared"
            )
        )
    else:
        print(_dim("no running daemon"))
    return 0


def _daemon_status(daemon, coordinate: str, cfg, args: argparse.Namespace) -> int:
    paths = daemon.paths_for(coordinate)
    status, pid = daemon.pid_status(paths.pid)
    dcfg = daemon.DaemonConfig.from_config(cfg)
    if getattr(args, "json", False):
        import json

        print(
            json.dumps(
                {
                    "status": status,
                    "pid": pid,
                    "coordinate": coordinate,
                    "interval_s": dcfg.interval_s,
                    "due_soon_days": dcfg.due_soon_days,
                    "notifier": list(dcfg.notifier),
                    "pidfile": str(paths.pid),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    line = {
        "running": _ok(f"running (pid {pid})"),
        "not-ours": _warn(
            f"recycled pid-file (pid {pid} is alive but is a foreign, reused process)"
        ),
        "stale": _warn(f"stale pid-file (pid {pid} is gone)"),
        "stopped": _dim("stopped"),
    }.get(status, _warn(f"{status} (pid {pid})"))
    print(f"daemon: {line}  for {coordinate}")
    print(
        _dim(
            f"  interval={dcfg.interval_s}s  due_soon={dcfg.due_soon_days}d  notifier={' '.join(dcfg.notifier)}"
        )
    )
    print(_dim(f"  pidfile={paths.pid}"))
    return 0


def cmd_install_skill(args: argparse.Namespace) -> int:
    from .install import install_skill

    return install_skill()


if __name__ == "__main__":
    raise SystemExit(main())
