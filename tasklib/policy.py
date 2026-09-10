"""Enforcement — the point of the tool. Pure rules over a ``Ticket`` + an ``EnforceConfig``.

No I/O. ``check()`` runs the gates and returns structured violations; the entrypoint turns a
non-empty result into a refusal with a precise message. Each gate is individually skippable
via the escape hatch (``--skip-<gate> "<reason>"`` → recorded in ``Ticket.skips``), and each
gate is disable-able in config (``enforce:``). The escape hatch is auditable: the
justification lives on the ticket forever.

Two entry points, one engine:
- :func:`check_create` — the gates at ticket *creation*.
- :func:`check_done`   — the gates at ``change``→``done`` (close).

They differ only in which screenshot kind is demanded; everything else is shared.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Sequence

from .model import Criterion, State, Ticket
from .msgrefs import strip_quoted_blocks as _strip_quoted_blocks
from .msgrefs import unquoted_msgrefs as _unquoted_msgrefs
from .render import validate_format

if TYPE_CHECKING:
    from datetime import datetime

# _strip_quoted_blocks (tasklib.msgrefs.strip_quoted_blocks) removes an appended tg#<id> quote
# block before the `links`/`user-impact-quality` gates scan any text — that quote is machine-
# appended text from someone ELSE's Telegram message, not authored prose, so its content must
# never itself cause a refusal. Without this, "expand only after THIS command's own gates"
# (cmd_create/cmd_change in cli.py) protects only the command that did the expanding: the quote
# is then STORED on the ticket, and any LATER command (an edit to an unrelated field, or `change
# --done`) re-scans the stored, already-expanded text and can refuse on content the author never
# wrote and cannot fix (task-cli#45 review finding — verified: the first "expand after gates" fix
# does not survive a second command). Stripping at the SCAN site is order-independent and covers
# every phase/command uniformly. The stripper is anchored on the EXACT quote-block shape
# tasklib.msgrefs generates (not any `>`-prefixed line), so a hand-authored blockquote elsewhere
# in ticket prose — a different shape — is not swept up and stays subject to the links gate.

# The canonical gate names. These are also the ``--skip-<gate>`` suffixes (hyphenated).
GATE_ACCEPTANCE = "acceptance-criteria"
GATE_MOTIVATION = "motivation"
GATE_USER_IMPACT = "user-impact"
GATE_COST_OF_INACTION = "cost-of-inaction"
GATE_SCREENSHOTS = "screenshots"
GATE_FORMATTING = "formatting"
# New gates (the enforcement-doctrine rules):
GATE_LINKS = "links"  # related entities must be proper links, not bare tokens
GATE_USER_IMPACT_QUALITY = "user-impact-quality"  # impact must be plain-language + user-framed
# DONE-phase only, and deliberately NOT skippable: a ticket cannot close with an unchecked
# acceptance criterion. It is absent from `normalize_skip_gate` so no `--skip-…` can waive it.
GATE_ACCEPTANCE_CHECKED = "acceptance-checked"
# Deliberately NOT skippable (task 6109): a `tg#<id>` reference in the title has no legitimate
# reason to stay there — the fix is always "move it into a prose field", never a judgment call
# the way a false-positive `links` match can be. Absent from `normalize_skip_gate` for the same
# reason GATE_ACCEPTANCE_CHECKED is: no `--skip-…` can waive it.
GATE_MSGREF_TITLE = "msgref-title"
# A tg#<id> reference sits in a BODY prose field but its QUOTED content was never attached.
# Skippable (unlike msgref-title): unlike a title reference — which is always wrong and always
# fixable by moving it out — an unquoted body reference has a legitimate waiver (a false-positive
# `tg#<n>` in ordinary prose, or a reference the author knowingly can't quote). See
# :func:`msgref_quote_reports` for the deny-vs-warn split (deny only when the id IS resolvable in
# local tg-cli history yet unquoted; a reference to a message too old for local retention only
# warns).
GATE_MSGREF_QUOTE = "msgref-quote"
# NOT a blocking content gate: `todo_shaped_warning` — the hint that a ticket reads as an
# agent's own in-session process step ("review my uncommitted diff before I commit it") rather
# than durable product work (see `tasklib.todo_hint`'s module docstring for the full rationale).
# Deliberately advisory-only (routes through `PolicyResult.warnings`, never `violations`) — the
# heuristic is necessarily coarse and a false positive must never block a legitimate ticket.
# Still carries a real `--skip-todo-shaped "<reason>"` flag so a deliberate false positive is
# silenced (and recorded) rather than nagging on every later command that re-scans the ticket.
GATE_TODO_SHAPED = "todo-shaped"

# The Links section must contain only real URLs. A bare, non-URL value (`Session: session:2`)
# rendered as `- key: value` escaped the `links` gate entirely — that gate scans PROSE for bare
# references, never the Links field itself — so junk masqueraded as a link (CTO report, tg#9179).
# Skippable: a genuinely legacy ticket read back from the backend may carry a non-URL value the
# author can't retroactively fix in one step.
GATE_LINKS_URL = "links-url"

# A title matching the auto-generated "review this diff" shape. An unchecked review pass or
# inbound-message classifier files this EXACT template when it has no other actionable output:
# the ticket can never lead to a merge (the review that files it runs read-only, often in a
# worktree disconnected from the diff it names), so it just sits at status:todo forever and gets
# refiled on the next pass over the same diff. A real, durable process step is never phrased this
# way — this gate is a hard, non-skippable refuse ("'Review uncommitted' should guarantee no
# ticket gets created"), not a warning, because there is no legitimate ticket the FULL template
# below should ever match.
#
# Deliberately narrower than "starts with review/audit/adversarial and contains diff anywhere":
# that shape over-blocks real product tickets whose title happens to mention a "diff" feature —
# "Audit diff-view authorization regression", "Review diff parser data-loss fix" — because in
# those, "diff" is an early NOUN/ADJECTIVE the title is ABOUT, not the trailing OBJECT of a
# generated "review the diff" sentence. Every real generated example found fits one of four
# specific shapes instead, so only those are matched:
#   1. "uncommitted ... diff"                      — "Review uncommitted X diff"
#   2. "review of ... diff"                        — "Adversarial cross-file review of X diff"
#   3. "diff for cross-file consistency"           — "Audit X diff for cross-file consistency"
#   4. starts review/audit/adversarial AND ends in "diff" (the bare form, nothing after it) —
#      "Review OMP lineage-first model detection diff"
# Plus two uncommitted shapes that do NOT require a trailing "diff" (agents dropped it after
# shape 1 started blocking, and still filed GitHub issues — rig-cli#409/#436/#454):
#   5. title starts "Review (the )uncommitted"     — "Review uncommitted 0.36.0 version bump"
#   6. "review of (the )current uncommitted"       — "Read-only adversarial ... review of the
#      current uncommitted rig-cli diff"
# Shape 5/6 do not fire on "Audit uncommitted transaction handling" or a product title like
# "Add a review of uncommitted files to the status bar" (no leading Review-uncommitted, no
# "review of current uncommitted").
#   7. "diff for performance"                      — "Review documentation diff for performance
#      regressions" (conloca/landing#263). Kept as this phrase, not a bare "diff for", so a
#      product title like "Review diff for user-visible changes in the compare view" still
#      passes (junk-title is non-skippable).
# None of the false-positive examples above end in a bare "diff", contain "uncommitted", "review
# of", "diff for cross-file consistency", or "diff for performance" — so none of the seven
# shapes fire on them.
#
# Scope boundary: this catches the auto-generated TEMPLATE shape at the create/edit boundary — the
# threat model is an agent's classifier reflexively filing "Review uncommitted X diff", not an
# adversary deliberately evading a security control. Accidental-encoding bypasses (trailing
# punctuation, Unicode terminal characters, a stray zero-width space from a copy-paste) are in
# scope and normalized away, because those change nothing a human would notice. Deliberate
# evasion — homoglyphs ("Rеview" with a Cyrillic е), NFKC/fullwidth digit tricks, and similar — is
# explicitly OUT OF SCOPE: an agent that wants to file junk under a different spelling always can,
# and chasing every such variant turns a template-catcher into an unwinnable WAF. If a genuinely
# adversarial actor becomes the real threat model here, that is a different gate.
#
# The title is whitespace-normalized (every run of whitespace, including a literal newline,
# collapsed to one space) before matching: `--title` accepts an embedded newline from the CLI,
# and `.` does not cross `\n` without `re.DOTALL` — a raw `"Review\nuncommitted policy diff"`
# would otherwise slip past shape 1 unmatched.
#
# Trailing punctuation/markdown is then stripped too: shape 4's terminal matcher requires "diff"
# to be the literal last word, so ordinary sentence punctuation or markdown wrapping — "Review the
# payments diff.", "...diff!", "...`diff`" — would put a non-word character after "diff" and slip
# past unmatched, even though it's still the same generated review-title shape. Match on `\w`
# (Unicode word characters under Python 3's default str semantics), not an enumerated ASCII
# punctuation class, so a Unicode terminal character — an ellipsis "…", a CJK full stop "。", a
# typographic quote — is stripped the same way. Stripping is safe here because it only widens
# which titles reach the phrase check below, never the phrase check itself — an unrelated title
# stays unmatched regardless of trailing characters.
_TRAILING_PUNCT_RE = re.compile(r"[^\w]+$")


def _normalize_title_for_junk_check(title: str) -> str:
    # Strip Unicode format characters (category "Cf": zero-width space, zero-width joiner, RTL/LTR
    # marks, ...) BEFORE anything else: a zero-width space inserted between two letters of a
    # required keyword ("di<ZWSP>ff") renders indistinguishably from the plain word but breaks
    # `\bdiff\b`'s contiguous match, defeating the whole gate. These characters carry no visible
    # width, so removing them can never change what a human reads.
    no_format_chars = "".join(ch for ch in (title or "") if unicodedata.category(ch) != "Cf")
    collapsed = re.sub(r"\s+", " ", no_format_chars).strip()
    return _TRAILING_PUNCT_RE.sub("", collapsed)


# Non-capturing: the lead is never back-referenced, only used to anchor each alternative below.
_JUNK_TITLE_LEAD = r"^(?:review|audit|adversarial)\b"
# Shape 5/6: "Review uncommitted" with no trailing "diff" required. Compiled separately so
# they can search (shape 6 is often prefixed: "Read-only adversarial cross-file review of
# the current uncommitted …") without loosening the lead-anchored shapes 1–4. Shape 6
# requires the process phrase "current uncommitted", not a bare "review of … uncommitted",
# so a product title about reviewing uncommitted files still creates.
_JUNK_REVIEW_UNCOMMITTED = re.compile(
    r"(?:^review\s*[:\-–—,.]?\s*(?:the\s+)?uncommitted\b|"
    r"^(?:(?:read-only|adversarial|cross-file)\s+)*review of (?:the\s+)?current\s+uncommitted\b)",
    re.IGNORECASE,
)
_JUNK_REVIEW_TITLE = re.compile(
    _JUNK_TITLE_LEAD + r".*(?:"
    r"\buncommitted\b.*\bdiff\b"  # 1
    r"|\breview of\b.*\bdiff\b"  # 2
    r"|\bdiff for cross-file consistency\b"  # 3
    r"|\bdiff for performance\b"  # 7 — conloca/landing#263
    r"|\bdiff\s*$"  # 4 — bare form, "diff" is the last word
    r")",
    re.IGNORECASE,
)
GATE_JUNK_TITLE = "junk-title"

# NOT a content gate `check()`/`Phase` ever evaluates — like GATE_TODO_SHAPED, duplicate
# detection needs I/O (candidate tickets fetched from the backend), which `check()` never does
# by design. TWO mechanisms share this one gate name, both driven from `cli.py`:
#   - `cli._refuse_if_duplicate` — a bespoke, session-scoped near-EXACT title-similarity hard
#     block (unchanged by task-cli's duplicate-detection gate below; only `--force` overrides it).
#   - `duplicate_violation`/`best_duplicate_match` below — the broader token/Jaccard-similarity
#     net, reported through the standard `--skip-duplicate "<reason>"` escape.
# Named here anyway (rather than a bare string literal in cli.py) so the `ticket.skips` key
# either mechanism records lives in the SAME namespace as every other gate name — a review
# finding: a magic string outside this module reads as "one more of these gates" without actually
# being one.
GATE_DUPLICATE = "duplicate"

# NOT a content gate `check()`/`Phase` ever evaluates, for the same structural reason as
# `GATE_DUPLICATE`: deciding it needs I/O (was this ticket read recently? — a local marker-file
# lookup, `read_tracker.is_fresh`), and this module does no I/O by design (module docstring). The
# gate itself is evaluated in `cli.py` (`_read_before_edit_violation`), which builds a `Violation`
# this module's `apply_skips`/`_report_and_die` still render through. UNLIKE `GATE_DUPLICATE`
# (which has no real `--skip-` flag — nobody types `--skip-duplicate`), this gate DOES have one
# (`--skip-read-before-edit`, wired via `cli._SKIP_FLAGS`) — so it IS listed in `ALL_GATES` and
# `normalize_skip_gate`'s aliases below (review finding: omitting a gate with a real skip flag
# from the "valid gates" list is a latent trap for a future `normalize_skip_gate` caller, even
# though nothing routes through it today). Only `cmd_change`'s `--what` edit is gated (task-
# cli#119's scope is the ticket's core description, not every prose field); see
# `EnforceConfig.read_before_edit` for the config on/off switch.
GATE_READ_BEFORE_EDIT = "read-before-edit"

# The minimum number of acceptance criteria a ticket must declare (rule: a real ticket has
# more than one provable outcome). Overridable via `enforce.acceptance_min`.
DEFAULT_ACCEPTANCE_MIN = 2

# Gates that NO recorded justification can waive — a hard refuse. A ticket must not be closed
# with an unchecked criterion under any escape hatch; only disabling the gate in config removes it.
NON_SKIPPABLE_GATES: frozenset[str] = frozenset(
    {GATE_ACCEPTANCE_CHECKED, GATE_MSGREF_TITLE, GATE_JUNK_TITLE}
)

# NOT a gate: the recorded post-merge-acceptance opt-out (task-cli#115). A ticket whose
# acceptance is inherently AFTER the merge (a release publish, a deploy the merge itself
# triggers) records `post-merge-acceptance: <reason>` under `## Skipped gates` (via `task change
# <id> --post-merge-acceptance "<reason>"`), and the ship-time gate (`task gate`,
# :func:`acceptance_gate`) passes while reporting the reason. It lives in `Ticket.skips` because
# that is the ticket's one audited, body-persisted escape-hatch record — but `apply_skips` never
# matches it (no violation carries this name), so it can NOT waive `acceptance-checked` on close:
# the ticket still needs real acceptance to reach Done, just after the merge.
POST_MERGE_ACCEPTANCE = "post-merge-acceptance"

ALL_GATES: tuple[str, ...] = (
    GATE_ACCEPTANCE,
    GATE_MOTIVATION,
    GATE_USER_IMPACT,
    GATE_USER_IMPACT_QUALITY,
    GATE_COST_OF_INACTION,
    GATE_SCREENSHOTS,
    GATE_FORMATTING,
    GATE_LINKS,
    GATE_LINKS_URL,
    GATE_ACCEPTANCE_CHECKED,
    GATE_MSGREF_TITLE,
    GATE_MSGREF_QUOTE,
    GATE_READ_BEFORE_EDIT,
    GATE_JUNK_TITLE,
    # Listed here (mirroring GATE_READ_BEFORE_EDIT above) even though neither is evaluated
    # inside `check()`'s own gate list the way the content gates are — both DO have a real
    # `--skip-<gate>` flag wired in cli.py, so omitting either from "valid gates" would be the
    # same latent trap the read-before-edit comment already warns about.
    GATE_DUPLICATE,
    GATE_TODO_SHAPED,
)


class Phase(str, Enum):
    """The two enforcement edges `check()` runs at — and the ONLY values it is ever passed.

    There is deliberately no third "check"/"edit" phase: the `task check` command toggles a
    criterion in place (`cmd_check`) and never calls `check()`, and a non-closing `task change`
    runs its own narrow edit gates (`_enforce_edit_or_die`), also not `check()`. So a gate run
    unconditionally inside `check()` runs at exactly create AND close — the two edges — and
    nowhere else.
    """

    CREATE = "create"
    DONE = "done"


@dataclass
class Violation:
    """One failed gate. ``gate`` is the canonical name; ``hint`` tells the user the fix."""

    gate: str
    message: str
    hint: str = ""


@dataclass
class EnforceConfig:
    """Which gates are active. Mirrors the ``enforce:`` config block (§2).

    A gate set to ``False`` is disabled entirely (no check, no skip needed). Screenshot
    gating is label-driven (``screenshot_labels``): screenshots are required only when the
    ticket carries one of those labels, separately for create/done.
    """

    acceptance_criteria: bool = True
    motivation: bool = True
    user_impact: bool = True
    cost_of_inaction: bool = True
    formatting: bool = True
    screenshots_on_create: bool = True
    screenshots_on_done: bool = True
    screenshot_labels: frozenset[str] = frozenset({"ui", "visual"})
    # enforcement-doctrine gates (default-on)
    links: bool = True
    user_impact_quality: bool = True
    acceptance_checked: bool = True  # block close while any criterion is unchecked
    acceptance_min: int = DEFAULT_ACCEPTANCE_MIN  # minimum acceptance criteria a ticket must declare
    msgref_title: bool = True  # refuse a tg#<id> reference in the title (task 6109)
    msgref_quote: bool = True  # refuse a body tg#<id> whose quote is missing yet resolvable (tg#9161)
    links_url: bool = True  # refuse a non-URL value in the Links section (tg#9179)
    read_before_edit: bool = True  # refuse `change --what` without a recent `read` (task-cli#119)
    # Refuse the auto-generated "review this diff" title shape (task-cli#135). On by default and
    # NOT per-ticket skippable (see GATE_JUNK_TITLE in NON_SKIPPABLE_GATES) — but, like every
    # other gate here, a repo can turn the CHECK ITSELF off via `enforce.junk_title: false` if it
    # genuinely has a legitimate ticket shaped this way; that is a repo-level policy call, not a
    # per-ticket escape hatch a single caller can grant itself.
    junk_title: bool = True
    todo_shaped_hint: bool = True  # non-blocking "this looks like a process step" hint on create
    # Governs BOTH duplicate-detection mechanisms in cli.py (the near-exact-title hard block
    # `_refuse_if_duplicate` AND the broader token/Jaccard net `_enforce_similar_duplicate_or_die`
    # / the classify-path fallback) — they share GATE_DUPLICATE, so one config key disables both
    # (review finding: an earlier version invoked both unconditionally, with no way to turn
    # duplicate detection off per repo, contradicting this file's own "every gate is disable-able
    # via enforce:" contract). NOT consulted by `check()` itself — like GATE_DUPLICATE generally,
    # duplicate detection needs I/O (candidate tickets) `check()` never does; cli.py reads this
    # flag directly before calling either mechanism.
    duplicate: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> "EnforceConfig":
        """Build from the parsed ``enforce:`` config block. Absent keys keep defaults.

        Accepts the spec's shape: scalar ``required``/``optional``/``strict`` for the text
        gates, and a nested ``screenshots: {on_create, on_done}`` map whose
        ``required_if_label`` list drives ``screenshot_labels``.
        """
        if not data:
            return cls()

        def _on(key: str, default: bool = True) -> bool:
            v = data.get(key)
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"required", "strict", "true", "on", "yes"}

        shots = data.get("screenshots") or {}
        labels: set[str] = set()
        soc = True
        sod = True
        if isinstance(shots, dict):
            soc = _label_gate_enabled(shots.get("on_create"), labels)
            sod = _label_gate_enabled(shots.get("on_done"), labels)

        try:
            acc_min = int(data.get("acceptance_min", DEFAULT_ACCEPTANCE_MIN))
        except (TypeError, ValueError):
            acc_min = DEFAULT_ACCEPTANCE_MIN

        return cls(
            acceptance_criteria=_on("acceptance_criteria"),
            motivation=_on("motivation"),
            user_impact=_on("user_impact"),
            cost_of_inaction=_on("cost_of_inaction"),
            formatting=_on("formatting"),
            screenshots_on_create=soc,
            screenshots_on_done=sod,
            screenshot_labels=frozenset(labels) if labels else cls.screenshot_labels,
            links=_on("links"),
            user_impact_quality=_on("user_impact_quality"),
            acceptance_checked=_on("acceptance_checked"),
            acceptance_min=acc_min,
            msgref_title=_on("msgref_title"),
            msgref_quote=_on("msgref_quote"),
            links_url=_on("links_url"),
            read_before_edit=_on("read_before_edit"),
            junk_title=_on("junk_title"),
            todo_shaped_hint=_on("todo_shaped_hint"),
            duplicate=_on("duplicate"),
        )


def _label_gate_enabled(spec: object, labels_out: set[str]) -> bool:
    """Interpret a screenshots ``on_create``/``on_done`` spec, collecting its labels.

    Forms accepted:
      - ``required`` / ``optional`` / bool — enable/disable, no label restriction change.
      - ``{required_if_label: [ui, visual]}`` — enable, gated on those labels.
    """
    if spec is None:
        return True
    if isinstance(spec, bool):
        return spec
    if isinstance(spec, str):
        return spec.strip().lower() in {"required", "strict", "true", "on", "yes"}
    if isinstance(spec, dict):
        lbls = spec.get("required_if_label")
        if isinstance(lbls, list):
            labels_out.update(str(x).strip().lower() for x in lbls)
        return True
    return True


@dataclass
class PolicyResult:
    """The outcome of running the gates. ``ok`` iff no un-skipped violations remain.

    ``warnings`` is a NON-blocking channel, distinct from ``violations``: it never affects
    ``ok`` and never refuses a command. It carries advisory notes the caller should surface but
    not enforce — currently only an unresolvable ``msgref-quote`` (a body ``tg#<id>`` whose
    message is too old for local retention, so no quote could be attached; see
    :func:`msgref_quote_reports`). A gate that can genuinely block belongs in ``violations``.
    """

    violations: list[Violation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # gates a justification bypassed
    warnings: list[Violation] = field(default_factory=list)  # advisory, never blocks (see docstring)

    @property
    def ok(self) -> bool:
        return not self.violations


def check(
    ticket: Ticket,
    cfg: EnforceConfig,
    phase: Phase,
    resolvable_ids: set[int] | None = None,
) -> PolicyResult:
    """Run all active gates for ``phase``, honoring per-gate escape hatches on the ticket.

    ``resolvable_ids`` is the set of ``tg#<id>`` ids that resolve in local tg-cli history (the
    caller loads it — this module does no I/O). It powers the ``msgref-quote`` gate's deny-vs-warn
    split: a body reference that is unquoted AND in this set is a blocking violation (the quote
    could and should have been attached); one that is unquoted but NOT in this set only warns (the
    message is older than local retention / tg-cli isn't installed here, so no quote is possible).
    ``None`` (the default, and what every caller that doesn't load history passes) means "cannot
    prove resolvability", so no reference is ever DENIED on that basis — every unquoted reference
    degrades to a warning. This keeps ``check`` pure and every legacy caller unchanged.
    """
    raw: list[Violation] = []
    warnings: list[Violation] = []
    if phase is Phase.CREATE:
        # CREATE only (review finding, task-cli#135 round 2): running this in the generic
        # DONE/close path too would make an already-existing junk ticket — created before this
        # gate existed, exactly the backlog this gate's own audit found and closed by hand —
        # permanently uncloseable. The documented contract ("checked on create and any --title
        # edit") is enforced structurally here, not just in prose.
        v = junk_title_violation(ticket, cfg)
        if v is not None:
            raw.append(v)
    raw.extend(_text_gates(ticket, cfg))
    # links + user-impact-quality are content rules enforced at BOTH create and close: a ticket
    # must not be CREATED carrying a bare reference / thin impact, and must not be CLOSED carrying
    # one either. Running them on the close transition catches a ticket that never passed create —
    # one made before this version, or edited directly in the GitHub/Linear web UI — so it can't be
    # closed with an un-linked HYP-789 or a "users"-thin impact. A genuine legacy exception is
    # waived on the close command with --skip-links / --skip-user-impact-quality (both wired there).
    v = links_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    v = msgref_title_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    v = links_url_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    deny, warn = msgref_quote_reports(ticket, cfg, resolvable_ids)
    if deny is not None:
        raw.append(deny)
    if warn is not None:
        warnings.append(warn)
    raw.extend(_screenshot_gate(ticket, cfg, phase))
    if phase is Phase.DONE:
        v = unchecked_criteria_violation(ticket, cfg)
        if v is not None:
            raw.append(v)
    raw.extend(_formatting_gate(ticket, cfg))
    result = apply_skips(raw, ticket, warnings)
    # todo-shaped: advisory-only, CREATE phase only (a ticket that's already been through create
    # doesn't need re-nagging on every later `done`/`change` pass), and outside `apply_skips`'s
    # normal violations/skipped split on purpose — this gate never contributes to `result.ok`,
    # skipped or not; see GATE_TODO_SHAPED's module-level comment.
    if phase is Phase.CREATE:
        tv = todo_shaped_violation(ticket, cfg)
        if tv is not None:
            if gate_is_skipped(ticket, tv.gate):
                if tv.gate not in result.skipped:
                    result.skipped.append(tv.gate)
            else:
                result.warnings.append(tv)
    return result


def gate_is_skipped(ticket: Ticket, gate: str) -> bool:
    """Whether a recorded skip waives ``gate`` — the ONE predicate every consumer of
    ``ticket.skips`` must use (:func:`apply_skips`, and :func:`acceptance_gate` for the
    independently-skippable minimum-count gate), so a skip can never be honored by one and not
    the other (review finding, task-cli PR#118 round 4: an earlier version of `acceptance_gate`
    re-implemented this as a bare ``gate not in ticket.skips`` inline, which would silently
    diverge from `apply_skips` the moment either copy grew a new rule — e.g. requiring a
    non-empty reason)."""
    return gate in ticket.skips and gate not in NON_SKIPPABLE_GATES


def apply_skips(raw: list[Violation], ticket: Ticket, warnings: list[Violation] | None = None) -> PolicyResult:
    """Partition raw violations into still-failing vs. (auditably) skipped ones.

    A gate whose canonical name is recorded in ``ticket.skips`` is bypassed and reported as
    skipped; everything else is a live violation. Shared by :func:`check` and the edit-time
    enforcement so the escape-hatch semantics are defined in exactly one place. ``warnings`` (the
    advisory, non-blocking channel) is carried straight onto the result so callers never have to
    remember to assign it afterwards — the invariant lives here, not at each call site.
    """
    result = PolicyResult(warnings=list(warnings) if warnings else [])
    for v in raw:
        if gate_is_skipped(ticket, v.gate):
            if v.gate not in result.skipped:
                result.skipped.append(v.gate)
        else:
            result.violations.append(v)
    return result


def acceptance_min_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The acceptance-criteria MINIMUM-COUNT gate (``cfg.acceptance_min``, default 2) —
    extracted from :func:`_text_gates` (task-cli#115 review finding) so :func:`acceptance_gate`
    can check the SAME predicate `task done` enforces at close, not just the every-criterion-
    checked-with-proof gate. Unlike that hard gate, this one IS skippable (``--skip-acceptance
    "<reason>"``) — `apply_skips` honours a recorded skip for it, `acceptance_gate` must too."""
    if cfg.acceptance_criteria and len(ticket.acceptance) < cfg.acceptance_min:
        return Violation(
            GATE_ACCEPTANCE,
            f"at least {cfg.acceptance_min} acceptance criteria are required "
            f"(have {len(ticket.acceptance)})",
            hint='add --acceptance "<criterion>" (repeatable)',
        )
    return None


def _text_gates(ticket: Ticket, cfg: EnforceConfig) -> list[Violation]:
    """The structured-content gates: acceptance count, motivation, user-impact, cost-of-inaction.

    All run in every phase — a ticket can't be created OR closed without them. The user-impact
    *quality* check (rule 5) fires whenever the impact is non-empty, on create and on close alike,
    so a ticket can't be closed carrying a thin/jargon-only impact it never had to justify at
    create (e.g. one authored in the GitHub/Linear web UI). These gates are phase-agnostic, so the
    helper takes no ``phase``.
    """
    out: list[Violation] = []
    v = acceptance_min_violation(ticket, cfg)
    if v is not None:
        out.append(v)
    if cfg.motivation and not ticket.why.strip():
        out.append(Violation(GATE_MOTIVATION, "the Why (motivation) section is required", hint='add --why "..."'))
    if cfg.user_impact and not ticket.user_impact.strip():
        out.append(Violation(GATE_USER_IMPACT, "the User impact section is required", hint='add --impact "..."'))
    elif ticket.user_impact.strip():
        # the impact is present → grade its quality, on every phase (create and close), so a
        # thin/jargon-only impact can't slip through at either edge. Gated on a non-empty impact
        # so disabling the emptiness gate (user_impact=False) with an empty impact is not then
        # re-blocked here as "low quality" — an empty impact is empty, not thin.
        v = impact_quality_violation(ticket, cfg)
        if v is not None:
            out.append(v)
    if cfg.cost_of_inaction and not ticket.cost_of_inaction.strip():
        out.append(
            Violation(
                GATE_COST_OF_INACTION,
                "the Cost of inaction section is required",
                hint='add --if-not-done "..."',
            )
        )
    return out


def _screenshot_gate(ticket: Ticket, cfg: EnforceConfig, phase: Phase) -> list[Violation]:
    """Screenshot proof: label-gated (ui/visual) on create AND done, plus a create-time
    mention gate — prose that mentions a screenshot in English or its Russian equivalent
    with no image attached is refused (task-cli#166, same hole tg-cli#337 closed for
    outbound Telegram)."""
    if phase is Phase.CREATE and cfg.screenshots_on_create:
        if ticket.screenshots:
            return []
        if _ticket_needs_screenshots(ticket, cfg):
            return [
                Violation(
                    GATE_SCREENSHOTS,
                    "a creation screenshot is required for UI/visual tickets",
                    hint="add --screenshot <path>",
                )
            ]
        if mentions_screenshot(_scanned_text(ticket)):
            return [
                Violation(
                    GATE_SCREENSHOTS,
                    "ticket mentions a screenshot but none is attached",
                    hint="add --screenshot <path>. Do not dodge by deleting the word — attach the screenshot.",
                )
            ]
        return []
    if phase is Phase.DONE and cfg.screenshots_on_done and _ticket_needs_screenshots(ticket, cfg):
        # the on-done gate demands the IMPLEMENTATION proof specifically — a creation
        # shot alone does not let you close a UI ticket. This is why the gate runs again.
        # Mention-based attach is CREATE-only so a pre-existing ticket that talks about
        # a screenshot remains closeable (same posture as junk-title).
        if any(s.kind == "implementation" for s in ticket.screenshots):
            return []
        return [
            Violation(
                GATE_SCREENSHOTS,
                "an implementation screenshot is required for UI/visual tickets",
                hint="add --screenshot <path>",
            )
        ]
    return []


def _formatting_gate(ticket: Ticket, cfg: EnforceConfig) -> list[Violation]:
    """The body must match the fixed section template (render is the source of truth)."""
    if not cfg.formatting:
        return []
    from .render import render

    return [Violation(GATE_FORMATTING, problem, hint="fix the section template") for problem in validate_format(render(ticket))]


def _scanned_text(ticket: Ticket) -> str:
    """The ticket text the links gate scans: title + every prose field + criterion texts, minus
    any tg#<id> quote block (see :func:`_strip_quoted_blocks`)."""
    parts = [ticket.title, ticket.what, ticket.why, ticket.user_impact, ticket.cost_of_inaction]
    parts += [c.text for c in ticket.acceptance]
    return _strip_quoted_blocks("\n".join(p for p in parts if p))


def junk_title_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Refuse a title matching the auto-generated "review this diff" shape (see
    :data:`_JUNK_REVIEW_TITLE` for why), unless a repo has explicitly disabled the CHECK itself
    via ``enforce.junk_title: false``. No PER-TICKET escape hatch — no ``--skip-junk-title``, no
    ``--force`` — that stays true regardless of ``cfg``; this is not a quality judgment a single
    caller can waive, it is a structural fact about the title. A real review request belongs in
    ``harness task`` (or a PR comment / review-cli run), never a product GitHub/Linear
    ticket phrased this way."""
    if not cfg.junk_title:
        return None
    normalized = _normalize_title_for_junk_check(ticket.title)
    if _JUNK_REVIEW_TITLE.match(normalized) or _JUNK_REVIEW_UNCOMMITTED.search(normalized):
        # Redact before it reaches the diagnostic (review finding): a title carrying a
        # credential-shaped value ("Review ghp_... diff") must not have that value echoed
        # verbatim into a message `_report_and_die` prints to stdout — the same posture
        # `tasklib.logging.redact` already enforces for structured log fields.
        from .logging import redact

        return Violation(
            GATE_JUNK_TITLE,
            f"title matches the auto-generated 'review this diff' shape: {redact(ticket.title)!r}",
            hint="process tickets belong in `harness task`, not the product GitHub/Linear tracker — "
            "run `review diff`/`review quorum` or file the finding with `harness task` instead",
        )
    return None


def links_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rule 1: every related entity named in the ticket must be a proper LINK, not a bare token.

    Returns a single ``links`` violation listing each un-linked reference, or ``None`` when the
    text is clean or the gate is disabled. Shared by :func:`check` (create/close) and the
    edit-time enforcement, so the scan + message live in one place.
    """
    if not cfg.links:
        return None
    from .links import linked_other_ids
    from .references import find_unlinked_references

    # A bare reference that already has a structured `task link` entry pointing at it (its id
    # sits in `ticket.links`, see `links.linked_other_ids`) is not a violation — the relation is
    # already recorded in the `## Links` section, prose mentioning it again is not the "un-linked
    # reference" this gate exists to catch (task-cli#154).
    already_linked = linked_other_ids(ticket.links)
    refs = [
        r for r in find_unlinked_references(_scanned_text(ticket)) if r.text not in already_linked
    ]
    if not refs:
        return None
    listed = "; ".join(f"{r.text} [{r.kind}]" for r in refs)
    return Violation(
        GATE_LINKS,
        f"related entities must be links, not bare references: {listed}",
        hint="write the link INSIDE the text field that names it (--what/--why/--impact/"
        "--if-not-done/--acceptance): either a markdown link [text](https://…) or the bare "
        "https://… URL — there is no --links flag; waive a false positive "
        'with --skip-links "<reason>" (works on new and on close; --force "<reason>" is a '
        "create/new-only shorthand)",
    )


def msgref_title_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The title must never carry a ``tg#<id>`` Telegram-message reference (task 6109): a title
    is one line with no room to quote the message, and it is never linkified downstream — so the
    reference is functionally useless there and must move into a prose field. Non-skippable (see
    :data:`GATE_MSGREF_TITLE` in :data:`NON_SKIPPABLE_GATES`) — there is no legitimate reason to
    keep it in the title, only a config-level opt-out (``enforce.msgref_title: false``).

    KNOWN LIMITATION (task-cli#45 review finding): the ``tg#[1-9][0-9]*`` pattern is broad by
    design (mirrors tg-cli's own detector) and could in principle false-positive on an ordinary
    title that happens to contain that exact shape (e.g. "migrate tg#2 protocol"). Because this
    gate also runs at CLOSE (not just create — see :func:`check`) and carries no per-ticket
    ``--skip-…`` waiver, such a title would need to be rewritten (or the gate disabled globally
    via ``enforce.msgref_title: false``) before the ticket could close. Accepted: a real
    `tg#<id>` string colliding with unrelated prose is rare, and the fix (rename the title) is
    always available — unlike the quoted-message content the ``links``/``user-impact-quality``
    gates must never block on.
    """
    if not cfg.msgref_title:
        return None
    from .msgrefs import title_msgrefs

    refs = title_msgrefs(ticket.title)
    if not refs:
        return None
    listed = ", ".join(f"tg#{r}" for r in refs)
    return Violation(
        GATE_MSGREF_TITLE,
        f"the title contains a Telegram message reference ({listed}) — move it into the body",
        hint='rewrite --title without the tg#<id>, and add "per tg#<id>…" to --what/--why/etc. instead',
    )


def msgref_quote_scan_fields(ticket: Ticket) -> tuple[str, ...]:
    """The prose fields the ``msgref-quote`` gate scans — the four the auto-expander appends a
    quote INTO (what / why / user_impact / cost_of_inaction). Deliberately NOT the title (a title
    reference is the ``msgref-title`` gate's job; a title has no room for a quote) and NOT the
    acceptance criteria (each serialized as a single ``- [ ] <text>`` line — msgrefs' module
    docstring forbids a multi-line quote there, it does not survive the render/parse round-trip).

    Returned as separate fields, NEVER joined: the expander appends a quote into the SAME field
    the reference sits in, so scanning a concatenation would let a quote for ``tg#42`` in ``why``
    mask a still-unquoted ``tg#42`` in ``what`` (and a stray ``> …`` line in one field followed by
    a marker in the next could even form a spurious cross-field quote block). :func:`unquoted_body_ids`
    scans each field independently for exactly this reason."""
    return (ticket.what, ticket.why, ticket.user_impact, ticket.cost_of_inaction)


def unquoted_body_ids(ticket: Ticket) -> list[int]:
    """Every ``tg#<id>`` referenced in a scanned prose field that lacks its GENERATED quote block,
    scanned PER FIELD (see :func:`msgref_quote_scan_fields`) and de-duplicated across fields in
    first-appearance order. Pure — the resolvability question (deny vs warn) is the caller's."""
    seen: set[int] = set()
    ordered: list[int] = []
    for field_text in msgref_quote_scan_fields(ticket):
        if not field_text:
            continue
        for i in _unquoted_msgrefs(field_text):
            if i not in seen:
                seen.add(i)
                ordered.append(i)
    return ordered


def _msgref_quote_deny(ids: list[int]) -> Violation:
    listed = ", ".join(f"tg#{i}" for i in ids)
    return Violation(
        GATE_MSGREF_QUOTE,
        f"the body references {listed} but the quoted message content is not attached "
        "(the message IS in local tg-cli history, so the quote can and must be inlined)",
        hint="re-run the create/change so the quote is auto-attached (e.g. `task change <id> "
        '--what "$(current what)"`), or waive a false positive with --skip-msgref-quote "<reason>"',
    )


def _msgref_quote_warn(ids: list[int]) -> Violation:
    listed = ", ".join(f"tg#{i}" for i in ids)
    return Violation(
        GATE_MSGREF_QUOTE,
        f"the body references {listed} but no quote could be attached — not in local tg-cli "
        "history (older than local retention, or tg-cli is not installed here)",
        hint="none required; the reference is kept as-is (the message predates local history)",
    )


def msgref_quote_reports(
    ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None
) -> tuple[Violation | None, Violation | None]:
    """Split the body's UNQUOTED ``tg#<id>`` references into a blocking violation + an advisory
    warning. Returns ``(deny, warn)`` — each a single ``msgref-quote`` :class:`Violation` or
    ``None``:

    - ``deny`` lists ids that are unquoted YET resolvable in ``resolvable_ids`` — the quote COULD
      have been attached (re-run create/change so the expander appends it) and wasn't, so the
      ticket is refused until it is.
    - ``warn`` lists ids that are unquoted AND unresolvable (older than local retention, or
      ``resolvable_ids`` is ``None`` because the caller loaded no history) — nothing can be quoted,
      so it is surfaced but never blocks.

    A ticket created/changed through task-cli never trips this: the auto-expander
    (``cli._expand_msgrefs``) always appends a block — real, or a "not found" note that still
    carries the provenance marker — BEFORE the gate runs, so :func:`_unquoted_msgrefs` finds
    nothing. The gate is the backstop for a ticket authored elsewhere (the GitHub/Linear web UI)
    or before this feature existed, caught at its next create/close/edit through task-cli.
    """
    if not cfg.msgref_quote:
        return None, None
    unquoted = unquoted_body_ids(ticket)
    if not unquoted:
        return None, None
    # A recorded --skip-msgref-quote waives the gate. Emit the deny UNCONDITIONALLY (resolvability
    # is irrelevant once waived, and the CLI skips loading history in this case) so it flows
    # through apply_skips into ``PolicyResult.skipped`` — otherwise this gate would be the one
    # skippable gate that never shows in the "skipped gates (justified)" audit line (review
    # finding). The advisory warning is suppressed in the same breath; the deny becomes the
    # audit record instead.
    if GATE_MSGREF_QUOTE in ticket.skips:
        return _msgref_quote_deny(unquoted), None
    resolvable = resolvable_ids or set()
    deny_ids = [i for i in unquoted if i in resolvable]
    warn_ids = [i for i in unquoted if i not in resolvable]
    deny = _msgref_quote_deny(deny_ids) if deny_ids else None
    warn = _msgref_quote_warn(warn_ids) if warn_ids else None
    return deny, warn


def safe_display(s: str) -> str:
    """Drop control/format characters so an untrusted value (a link key/value, a candidate
    ticket's title) can't inject a terminal escape (ANSI colour, bidi override) into a
    diagnostic printed to the console or forwarded to a Telegram notification.
    ``str.isprintable()`` is False for exactly those (Cc/Cf/Zl/Zp). Public (no leading
    underscore) because `cli.py` also sanitizes an untrusted candidate title with it — see
    `duplicate_violation`'s, `cli._classify_create`'s, and the post-`task new` relatedness
    nudge's call sites."""
    return "".join(ch for ch in s if ch.isprintable())


def _redact_link_display(s: str) -> str:
    """Sanitize a link key/value for a diagnostic echoed to the console / a notification: run the
    SHARED token redactor (:func:`tasklib.logging.redact`, the same one that scrubs log lines) so a
    token-shaped substring (`ghp_…`, `lin_api_…`, a long opaque secret) becomes ``<redacted>``,
    then strip control chars (terminal-injection). Applied to BOTH key and value, honouring the
    repo's no-token-output posture rather than re-inventing redaction here (review finding)."""
    from .logging import redact

    return safe_display(redact(s))


def _redact_link_value_display(key: str, value: str) -> str:
    """Like :func:`_redact_link_display` for a link VALUE, but also KEY-aware: mirrors
    :func:`tasklib.logging._redact_fields`'s secret-KEY masking (``api_key``, ``token``,
    ``secret``, …), not just the value-shape regex. A value-shape-only check misses a
    non-gh/lin_api/sk-/Bearer-shaped secret (e.g. a Slack ``xoxb-…`` token) sitting under an
    obviously-sensitive key name — the key name alone is enough signal to redact wholesale
    rather than echo it back in a violation message (review finding)."""
    from .logging import _REDACTED, _SECRET_KEY_RE

    if _SECRET_KEY_RE.search(key):
        return _REDACTED
    return _redact_link_display(value)


def _is_valid_link_key(key: str) -> bool:
    """A Links KEY must round-trip through render→parse and be safe to echo. ``render._render_links``
    writes ``- {key}: {value}`` and ``render._parse_links`` splits on the FIRST ``:``, so a key
    containing ``:`` reparses as ``{first: 'rest: value'}`` — the ticket passes the gate before
    persistence and FAILS after a backend round-trip (review finding). A key must therefore be
    non-empty, printable (no control/bidi injection), and free of ``:``."""
    return bool(key.strip()) and safe_display(key) == key and key == key.strip() and ":" not in key


def is_http_url(value: str) -> bool:
    """A Links value is valid iff it is an absolute http/https URL with a real host and no
    embedded credentials. Uses :func:`urllib.parse.urlparse` (not a regex) so an odd-but-real
    URL — query strings, ports — is accepted while a bare token is not: ``session:2`` parses to
    scheme ``session`` (rejected — only http/https pass), a Linear key ``HYP-1001`` has an empty
    scheme, and a schemeless ``foo`` has an empty netloc. Additionally rejects malformed shapes
    that still carry an http scheme (review finding): any internal whitespace (a real URL has
    none, so ``https://Session: session:2`` is out) and a netloc with no alphanumeric host
    character (``https://``, ``https://.``, ``https://:`` are out)."""
    stripped = value.strip()
    # A real URL is printable with no whitespace: reject internal whitespace AND any non-printable
    # control/format character (NUL, ESC, bidi overrides), which would otherwise pass the scheme
    # check and then render verbatim into the ticket body / a violation message / a TG notification
    # (review finding). ``str.isprintable()`` is False for any Cc/Cf/Zl/Zp char (space excluded,
    # but we already rejected whitespace above).
    if not stripped or not stripped.isprintable() or any(ch.isspace() for ch in stripped):
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(stripped)
        _ = parsed.port  # property access validates port syntax (raises ValueError if malformed)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    # Reject embedded userinfo (`https://user:ghp_xxx@host/...`) outright: this gate persists the
    # value verbatim into a GitHub/Linear issue body, so an accepted credential-shaped userinfo
    # segment leaks it into a shared, often-public tracker (review finding). There is no
    # legitimate reason for a ticket Link to carry embedded auth.
    if parsed.username is not None or parsed.password is not None:
        return False
    # A credential can also ride in the path/query/fragment, not just userinfo
    # (`https://example.com/download?token=ghp_...`, `.../ghp_.../artifact`) — this gate
    # persists the value verbatim, so that leaks it into a shared, often-public tracker the
    # same as embedded userinfo does (review finding). Reuse the shared token-shaped-value
    # detector (gh/Linear/OpenAI-style opaque tokens, `Bearer ...`) rather than duplicating
    # its pattern here.
    from .logging import _SECRET_VALUE_RE

    if _SECRET_VALUE_RE.search(stripped):
        return False
    # Require a real host: `https://` (empty), `https://user@` / `https://user@:443` (userinfo
    # only, empty host), and `https://.` (no alphanumeric host character) are all rejected (review
    # finding). `parsed.hostname` strips any userinfo/port, leaving the host alone to inspect.
    host = parsed.hostname or ""
    return any(ch.isalnum() for ch in host)


def links_url_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The Links section must hold only real URLs — never a bare, non-URL value like
    ``Session: session:2`` (CTO report tg#9179). That junk renders as ``- key: value`` and slides
    past the ``links`` gate entirely (which scans PROSE for bare references, never the structured
    Links field), so it surfaces in the ticket as a fake link. Returns one ``links-url`` violation
    listing every offending ``key: value`` pair, or ``None`` when every value is a URL or the gate
    is disabled. Skippable (``--skip-links-url``) for a legacy ticket read back from the backend
    whose Links can't be fixed in one step."""
    if not cfg.links_url:
        return None
    # An entry is bad if its value isn't a URL OR its key is not a valid link key — the key is
    # rendered verbatim by render._render_links (`- key: value`) and split back on the first `:`
    # by render._parse_links, so a key with a control/bidi escape (terminal injection into `task
    # read`) or an embedded `:` (breaks the round-trip) is rejected even when its value is a valid
    # URL (review finding). See :func:`_is_valid_link_key`.
    bad = [(k, v) for k, v in ticket.links.items() if not is_http_url(v) or not _is_valid_link_key(k)]
    if not bad:
        return None
    # Strip control/format chars from BOTH the key and the value before echoing them: the value
    # is validated but a REJECTED one still reaches this message, and the KEY is never validated
    # at all — either could carry an ANSI/bidi escape that would inject into the console output or
    # a TG notification (review finding).
    listed = "; ".join(f"{_redact_link_display(k)}: {_redact_link_value_display(k, v)}" for k, v in bad)
    return Violation(
        GATE_LINKS_URL,
        f"the Links section must contain only URLs (http(s)://…), not bare values or an "
        f"embedded credential: {listed}",
        hint="use a full https:// URL with no embedded token/credential for each link (a tg#<id> "
        'reference belongs in a body prose field, not Links); waive a legacy value with '
        '--skip-links-url "<reason>"',
    )


def impact_quality_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rule 5: the user-impact must be plain-language and user-framed (only when non-empty)."""
    if not cfg.user_impact_quality or not ticket.user_impact.strip():
        return None
    from .quality import assess_user_impact

    problems = assess_user_impact(_strip_quoted_blocks(ticket.user_impact))
    if not problems:
        return None
    return Violation(
        GATE_USER_IMPACT_QUALITY,
        "user impact is not plain-language/user-framed: " + problems[0],
        hint="rewrite the impact in the user's terms (--impact on new, `task change --impact` on "
        'an existing ticket); waive with --skip-user-impact-quality "<reason>" (works on new and '
        'on close; --force "<reason>" is a create/new-only shorthand) if truly N/A',
    )


def todo_shaped_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The non-blocking todo-vs-ticket hint (see GATE_TODO_SHAPED / `tasklib.todo_hint`).

    Always returns a `Violation` shape (reused for both the warning and the "skipped, show it
    in the audit" path in `check()`) — never itself decides blocking vs advisory; that split
    lives in `check()`, the one place that knows a warning must never touch `PolicyResult.ok`.
    """
    if not cfg.todo_shaped_hint:
        return None
    from .todo_hint import assess_todo_shaped

    problems = assess_todo_shaped(ticket.title, ticket.what)
    if not problems:
        return None
    # The actionable guidance lives in `message`, not `hint`: `cli._report_and_die` prints
    # `warnings` as `⚠ {gate}: {message}` only — `hint` is a `violations`-only field, never
    # rendered for a warning (review finding). This IS the coordinator's required wording, so it
    # must survive to the terminal.
    return Violation(
        GATE_TODO_SHAPED,
        problems[0] + ". If this is genuinely a session-local process step, use your harness's "
        "own TodoList tool (TodoWrite/TaskCreate) instead of task-cli.",
        hint='silence this hint with --skip-todo-shaped "<reason>" if it is a false positive',
    )


# ── Duplicate detection (GATE_DUPLICATE) ────────────────────────────────────────────────────
#
# A companion ecosystem-wide audit found 13 confirmed duplicate tickets, all minted by `task
# classify`'s auto-create path (`_classify_create` in cli.py). That path already dedups via
# `_best_dedup_match` — a `difflib.SequenceMatcher` ratio on the derived TITLE ALONE — but that
# catches only near-identical wording. Real duplicates are often worded differently for the
# same underlying request ("Review OMP model-detection ordering diff" vs "Review OMP
# lineage-first model detection diff"): same meaningful content, low SequenceMatcher ratio.
#
# `duplicate_violation`/`best_duplicate_match` add a second, complementary signal: TOKEN overlap
# after stripping this codebase's own boilerplate vocabulary (every "review my diff" ticket
# shares words like "review"/"diff"/"uncommitted", which would otherwise make nearly any two
# tickets look similar — a false-positive cluster, not a real duplicate signal). The threshold
# below (>=2 shared meaningful tokens AND Jaccard >=0.5) is the one specified by that audit's
# findings, relayed as this ticket's brief — NOT independently re-run against the raw 13 here
# (this implementer worked from a small sample of the resulting GitHub issue titles, not the
# audit's own dataset). **Reviewer: please re-check this threshold, and the min-tokens-per-side
# floor just below it (this implementer's own addition, added AFTER hitting a real false
# positive in this repo's own test suite — see its comment), against the actual 13 before
# merging** — a real duplicate with a thin title on both sides would silently fall through it.
#
# CONFIRMED, ACCEPTED false-positive class (adversarial review, not merely theoretical): two
# genuinely DIFFERENT pieces of work that share dense domain vocabulary can clear this
# threshold purely on shared nouns — "Encrypt API token storage" vs "API token storage
# rotation" share {api, token, storage} (3 tokens, Jaccard 0.6) despite being distinct security
# work (encryption-at-rest vs. key rotation); their only real differentiator ("encrypt" vs.
# "rotation") is a minority of the tokens. Verified this is NOT fixable by retuning
# DUPLICATE_MIN_SHARED_TOKENS/DUPLICATE_JACCARD_THRESHOLD alone: this false positive (Jaccard
# 0.6) scores HIGHER than the real reworded-duplicate case this module's own tests pin ("Review
# OMP model-detection ordering diff" vs "...lineage-first model detection diff", Jaccard 0.5) —
# any threshold strict enough to exclude the former would also exclude the latter. Closing this
# properly needs term-specificity weighting (TF-IDF-like: "storage"/"api"/"token" are generic
# across many tickets, "encrypt"/"rotation" are the real signal) — out of scope for a token-set
# Jaccard heuristic. Accepted as a bounded cost because the gate is SOFT and cheaply escaped
# (`--skip-duplicate "<reason>"` / `--force`, one flag) — see test_policy.py's pinning test for
# this exact pair. If the false-positive rate turns out to matter more than that in practice,
# retuning needs the real 13-ticket audit dataset to validate against, not a guess from here.
#
# Deliberately kept SESSION-scoped (candidates come from the same `_session_dedup_candidates`
# `_refuse_if_duplicate`/`_classify_create` already use) rather than widened to every open
# ticket in the repo — `test_create_dedup_scoped_to_session_not_global` pins that scoping as an
# intentional design choice (a same-title ticket from a DIFFERENT session must not block this
# one), and the audit's report (as relayed) described the 13 as same-session clusters.

# Words generic across nearly every review/process-step ticket this codebase's own tooling
# generates — stripped before comparison so two UNRELATED tickets that both happen to be
# "review ... diff" tickets don't score as similar just because they share that boilerplate.
# Deliberately narrow and codebase-specific, not a general English stopword list.
_DUPLICATE_BOILERPLATE_WORDS = frozenset(
    {
        "review", "reviewed", "reviewing", "uncommitted", "diff", "diffs", "cross", "file",
        "consistency", "the", "a", "an", "for", "of", "and", "or", "to", "in", "on", "with",
        "this", "that", "before", "committing", "commit", "shipping", "ship", "changes",
        "change", "code", "adversarial",
    }
)

# Split on ANY non-word boundary, including hyphens: "model-detection" -> "model", "detection",
# so it intersects with a candidate that happens to write the same concept as two separate
# words. Also why the boilerplate set above lists "cross"/"file" separately rather than
# "cross-file" as one entry. `[^\W_]+` (NOT `[a-z0-9]+`, review finding P1) is Unicode-aware —
# letters/digits in ANY script, underscore excluded — because task-cli's own classify intake is
# explicitly documented to handle Russian messages (see classify.py's multi-ask module comment);
# an ASCII-only pattern silently tokenized a Cyrillic title/body to ZERO tokens, so every
# Russian reworded-duplicate check silently no-opped (`_DUPLICATE_MIN_TOKENS_PER_SIDE` would
# never be met) with no error, warning, or test ever exercising that path.
_DUPLICATE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# From the audit's brief (see module comment above — not independently re-verified here).
DUPLICATE_MIN_SHARED_TOKENS = 2
DUPLICATE_JACCARD_THRESHOLD = 0.5

# A THIN title (after boilerplate stripping) makes Jaccard unreliable regardless of the
# threshold above: a 2-token title sharing both tokens with an unrelated 2-token title scores a
# perfect 0.5-1.0 Jaccard on pure coincidence ("Add a thing" vs "Add a second, unrelated
# thing" — a real false positive this floor was added to fix, caught by this repo's own test
# suite: test_list_label_filters_session_view and friends in tests/test_cli.py). This floor is
# THIS implementer's own addition, not part of the audit's specified threshold, and has NOT been
# checked against the real 13 duplicates — it is plausible a real duplicate with thin titles on
# both sides (e.g. a 2-token title) would now fall through it. Flagging for reviewer follow-up
# (see the module comment above) rather than silently shipping an unverified floor.
_DUPLICATE_MIN_TOKENS_PER_SIDE = 3

# The "extra cheap signal" (same session, minted within this window) is deliberately NOT a
# separate, lower-bar trigger — an EARLIER version of this file relaxed the match threshold to
# 1 shared token + Jaccard >=0.3 for a same-session-recent candidate, and an adversarial review
# broke it immediately: two GENUINELY DIFFERENT requests in the same session five minutes apart
# — "Postgres retry failures require driver upgrade" and "Upgrade driver for Postgres replica
# failover" — share only common domain vocabulary ("postgres"/"driver"/"upgrade") at Jaccard
# 0.375, comfortably below the PRIMARY threshold (0.5) yet enough to trip that relaxed one. Real
# tickets on the same topic routinely share 2-3 domain words without being duplicates, so ANY
# threshold low enough to catch the audit's 13 near-identical same-session tickets is also low
# enough to catch unrelated same-session tickets that happen to share domain vocabulary — the
# two failure modes are not separable by token overlap alone at this scale. So: same-session-
# recency is now ONLY ever used to ANNOTATE a match that ALREADY cleared the primary threshold
# (see `_same_session_recent` / `duplicate_violation`'s message) — it never lowers the bar.
DUPLICATE_SESSION_WINDOW_MINUTES = 60


def _duplicate_tokens(text: str) -> set[str]:
    """Lowercase word-ish tokens, boilerplate stripped, single-character tokens dropped.

    Generated ``tg#<id>`` quote blocks are stripped first: they are the same machine-appended
    excerpt on every ticket that referenced that message, so leaving them in would let one
    shared quote dominate the Jaccard sets of otherwise unrelated tickets.
    """
    text = _strip_quoted_blocks(text)
    tokens = {t for t in _DUPLICATE_TOKEN_RE.findall(text.lower()) if len(t) > 1}
    return tokens - _DUPLICATE_BOILERPLATE_WORDS


def _parse_backend_timestamp(value: str) -> "datetime | None":
    """Parse a backend `updated_at` (GitHub/Linear ISO8601, typically `...Z`) leniently.

    Returns `None` on anything unparseable — the time-proximity signal is then simply unusable
    for that candidate, never an error (this predicate must stay pure and never raise on real
    backend data, which occasionally omits or malforms this field). Guards against a non-string
    value too (review finding): a backend that ever passed a raw int/float through would crash
    `.replace()` below, breaking this function's own "never raise" promise."""
    from datetime import datetime

    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_session_recent(candidate: Ticket, new_session: str | None, now: "datetime | None") -> bool:
    """Whether `candidate` was filed in `new_session`, within `DUPLICATE_SESSION_WINDOW_MINUTES`
    of `now`. Purely an ANNOTATION signal (see the module comment on `DUPLICATE_SESSION_WINDOW_
    MINUTES` for why this is never allowed to lower the match threshold) — used only to add
    corroborating context to an ALREADY-matched violation's message. `None` `new_session`/`now`
    (the caller declined to supply session/time context) always returns `False`."""
    if not new_session or now is None:
        return False
    if candidate.session_label() != new_session:
        return False
    ts = _parse_backend_timestamp(candidate.updated_at)
    if ts is None:
        return False
    # A naive `ts` (no offset in the source string) can't be subtracted from an AWARE `now`
    # (`datetime.now(timezone.utc)`, what every real caller passes) without TypeError.
    # GitHub/Linear both emit an offset, so this is a defensive normalize for a malformed/legacy
    # value, not the expected path — assume UTC rather than raise (must never crash `task new`).
    if ts.tzinfo is None:
        from datetime import timezone as _timezone

        ts = ts.replace(tzinfo=_timezone.utc)
    try:
        delta = (now - ts).total_seconds()
    except TypeError:
        return False
    return 0 <= delta <= DUPLICATE_SESSION_WINDOW_MINUTES * 60


def best_duplicate_match(
    new_title: str,
    new_body: str,
    candidates: Sequence[Ticket],
    new_session: str | None = None,
    now: "datetime | None" = None,
) -> Ticket | None:
    """The best token/Jaccard-similarity duplicate among `candidates`, or `None`.

    `new_body` is typically `what` (the ticket's core description) — the same field the
    candidates' own `what` is compared against. `new_session`/`now` are used ONLY to pick the
    same-session-recent candidate as the tiebreak among several equally-scored matches (see
    module comment: they never lower the match threshold itself). Closed/done candidates are
    excluded (a duplicate of a CLOSED ticket isn't a duplicate — it's exactly the shape a
    re-filed real bug should take)."""
    new_tokens = _duplicate_tokens(f"{new_title}\n{new_body}")
    if len(new_tokens) < _DUPLICATE_MIN_TOKENS_PER_SIDE:
        return None  # too thin to judge similarity reliably (see the floor's module comment)
    best: tuple[float, int, bool, Ticket] | None = None
    for candidate in candidates:
        if candidate.state in (State.DONE, State.CANCELLED):
            continue
        cand_tokens = _duplicate_tokens(f"{candidate.title}\n{candidate.what}")
        if len(cand_tokens) < _DUPLICATE_MIN_TOKENS_PER_SIDE:
            continue
        shared = new_tokens & cand_tokens
        union = new_tokens | cand_tokens
        jaccard = len(shared) / len(union) if union else 0.0
        matched = (
            len(shared) >= DUPLICATE_MIN_SHARED_TOKENS
            and jaccard >= DUPLICATE_JACCARD_THRESHOLD
        )
        if not matched:
            continue
        # Same-session-recency only breaks a TIE among candidates that already cleared the
        # match threshold on their own (never a reason to match on its own — see module comment).
        recent = _same_session_recent(candidate, new_session, now)
        score = (jaccard, len(shared), recent)
        if best is None or score > best[:3]:
            best = (jaccard, len(shared), recent, candidate)
    return best[3] if best else None


def duplicate_violation(
    new_title: str,
    new_body: str,
    candidates: Sequence[Ticket],
    new_session: str | None = None,
    now: "datetime | None" = None,
) -> Violation | None:
    """`best_duplicate_match`, turned into a `Violation` for the standard skip/report pipeline.

    Distinct from the pre-existing near-exact-title hard block (`cli._refuse_if_duplicate`,
    kept as-is): this is the broader, token-based net, reported as a normal skippable gate
    rather than a bespoke early refusal.
    """
    match = best_duplicate_match(new_title, new_body, candidates, new_session=new_session, now=now)
    if match is None:
        return None
    # Same-session-recency is corroborating CONTEXT in the message only — it never decided the
    # match itself (see best_duplicate_match's module comment).
    context = (
        " (same session, filed recently)"
        if _same_session_recent(match, new_session, now)
        else ""
    )
    # No "--skip-duplicate"/"--force" in the hint here — `cli._report_and_die` already
    # auto-appends `(or --skip-{gate} "<reason>")` to every non-hard violation's hint (see its
    # own comment at the print site); repeating it here would double-print (task-cli#119 review
    # finding, the same class this gate's own docstring precedent warns against).
    #
    # `match.title` is UNTRUSTED — it's a candidate ticket's title read back from the backend,
    # which could carry a hand-crafted ANSI/bidi control sequence (review finding, P2: a title
    # containing a terminal escape, printed verbatim into this diagnostic, could clear/spoof the
    # console) or a token-shaped credential. Run the logging redactor (same as
    # junk_title_violation / _redact_link_display) then safe_display so neither class of payload
    # reaches stdout.
    from .logging import redact

    shown = safe_display(redact(match.title))
    return Violation(
        GATE_DUPLICATE,
        f"a similar ticket may already exist{context}: {match.id}  {shown}",
        hint=f"task read '{match.id}' to check — if this is genuinely separate work, create it anyway",
    )


def unchecked_criteria_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rules 2 + 3 at close: a ticket can close only when EVERY criterion is checked AND each
    checked one carries a visual proof (or a recorded ``force_reason``). Not skippable.

    The proof half matters because a ``- [x]`` ticked in the GitHub/Linear web UI round-trips as
    ``checked`` with an empty ``proof`` — the close gate is the backstop that still demands the
    proof the ``task check`` command would have required.
    """
    if not cfg.acceptance_checked:
        return None
    # `Ticket.unmet_criteria()` (model.py) is the single source of truth for "would this
    # criterion fail the close gate" — unchecked, OR checked without a proof/force_reason
    # (review finding: an earlier version of `mark-shipped`'s HYP-1295 warning reimplemented
    # this predicate by hand in `cli.py`, which could silently drift from this gate if either
    # copy changed alone). Partitioned back into unchecked/unproven here only for the
    # human-readable message below; by construction every "checked" member of `unmet` has no
    # proof and no force_reason (that's the only way a checked criterion lands in `unmet`).
    unmet = ticket.unmet_criteria()
    if not unmet:
        return None
    unchecked = [c for c in unmet if not c.checked]
    unproven = [c for c in unmet if c.checked]
    parts: list[str] = []
    if unchecked:
        parts.append(f"{len(unchecked)} unchecked: " + "; ".join(c.text for c in unchecked))
    if unproven:
        parts.append(f"{len(unproven)} checked without a visual proof: " + "; ".join(c.text for c in unproven))
    return Violation(
        GATE_ACCEPTANCE_CHECKED,
        "a ticket closes only when every criterion is checked WITH a proof — " + " | ".join(parts),
        hint="check each with a visual proof: task check <id> <n> --proof <path>",
    )


@dataclass
class GateReport:
    """The ship-time acceptance verdict (`task gate`, task-cli#115) — what `gh ship` reads
    BEFORE merging, so a ticket can no longer be closed on merge with nothing accepted.

    ``ok`` is the verdict; ``unchecked``/``proofless`` are the gaps (always populated, even
    when an opt-out or a disabled gate makes the verdict pass — the shipper still needs to
    know what is owed after the merge). ``post_merge_reason`` is the recorded opt-out (see
    :data:`POST_MERGE_ACCEPTANCE`) or ``""``; ``gate_enabled`` mirrors
    ``enforce.acceptance_checked``. ``no_criteria`` is true specifically for ZERO criteria (the
    ``criteria: 0`` case the JSON contract documents); ``below_minimum`` is the broader "fewer
    than ``acceptance_min`` (and not skipped)" case task-cli#115 review round 1 added — every
    ``no_criteria`` ticket is also ``below_minimum`` whenever the minimum is >=1, but a
    1-of-2-criteria ticket is ``below_minimum`` without being ``no_criteria``.
    """

    ok: bool
    unchecked: list[Criterion] = field(default_factory=list)
    proofless: list[Criterion] = field(default_factory=list)
    post_merge_reason: str = ""
    gate_enabled: bool = True
    no_criteria: bool = False
    below_minimum: bool = False


def acceptance_gate(ticket: Ticket, cfg: EnforceConfig) -> GateReport:
    """Evaluate the merge-time acceptance gate. The VERDICT is exactly the two on-done gates
    that decide whether a ticket's acceptance is real: ``acceptance-checked``
    (:func:`unchecked_criteria_violation`, the same call :func:`check_done` makes — every
    criterion checked WITH a proof) and the ``acceptance-criteria`` MINIMUM count
    (:func:`acceptance_min_violation`, the same predicate `_text_gates` enforces at both create
    and close) — never a re-derivation of either, so `task gate` and `task done` can't disagree
    about whether a ticket is accepted (task-cli#115 review round 1: a 1-criterion, fully-proven
    ticket used to pass `task gate` and then still refuse `task done`, merging a PR whose ticket
    could never close). The only thing this ADDS beyond those two gates is the recorded
    post-merge opt-out, a MERGE-gate-only concession (the ticket's acceptance is inherently
    after the merge — a release publish) that never waives either close gate.
    """
    unmet = ticket.unmet_criteria()
    reason = ticket.skips.get(POST_MERGE_ACCEPTANCE, "").strip()
    violation = unchecked_criteria_violation(ticket, cfg)
    # The minimum-count gate — `acceptance_min_violation`, the SAME predicate `_text_gates`/
    # `check_done` enforce — is the ONLY thing that may fail a zero-criteria ticket. It already
    # respects every legitimate override `task done` does: disabled via `acceptance_criteria:
    # false`, a lowered `acceptance_min: 0`, or a recorded `--skip-acceptance "<reason>"`
    # (review finding, task-cli PR#118 round 2 — Codex: an earlier version had a SEPARATE,
    # unconditional `no_criteria = cfg.acceptance_checked and not ticket.acceptance` term that
    # ignored all three overrides, so `task gate` could refuse a ticket `task done` would
    # legitimately let close). `no_criteria` below is now purely INFORMATIONAL — which refusal
    # message to show — never a second, independent way to fail the gate.
    min_violation = acceptance_min_violation(ticket, cfg)
    below_minimum = min_violation is not None and not gate_is_skipped(ticket, GATE_ACCEPTANCE)
    no_criteria = not ticket.acceptance
    return GateReport(
        ok=(violation is None and not below_minimum) or bool(reason),
        unchecked=[c for c in unmet if not c.checked],
        proofless=[c for c in unmet if c.checked],
        post_merge_reason=reason,
        gate_enabled=cfg.acceptance_checked,
        no_criteria=no_criteria,
        below_minimum=below_minimum,
    )


def check_create(ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None = None) -> PolicyResult:
    return check(ticket, cfg, Phase.CREATE, resolvable_ids=resolvable_ids)


def check_done(ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None = None) -> PolicyResult:
    return check(ticket, cfg, Phase.DONE, resolvable_ids=resolvable_ids)


def _ticket_needs_screenshots(ticket: Ticket, cfg: EnforceConfig) -> bool:
    """Screenshots are label-gated: required only when the ticket carries a gating label."""
    ticket_labels = {label.strip().lower() for label in ticket.labels}
    return bool(ticket_labels & cfg.screenshot_labels)


_SCREENSHOT_MENTION_RE = re.compile(r"screenshot|\u0441\u043a\u0440\u0438\u043d\u0448\u043e\u0442", re.IGNORECASE)


def mentions_screenshot(text: str) -> bool:
    """True when `text` mentions screenshot in English or its Russian equivalent (matched via Unicode code points), any case (task-cli#166)."""
    return bool(_SCREENSHOT_MENTION_RE.search(text or ""))


def normalize_skip_gate(name: str) -> str:
    """Normalize a ``--skip-<gate>`` suffix to a canonical gate name. Raises on unknown."""
    norm = name.strip().lower().replace("_", "-")
    aliases = {
        "acceptance": GATE_ACCEPTANCE,
        "acceptance-criteria": GATE_ACCEPTANCE,
        "motivation": GATE_MOTIVATION,
        "why": GATE_MOTIVATION,
        "user-impact": GATE_USER_IMPACT,
        "impact": GATE_USER_IMPACT,
        "cost-of-inaction": GATE_COST_OF_INACTION,
        "if-not-done": GATE_COST_OF_INACTION,
        "screenshots": GATE_SCREENSHOTS,
        "screenshot": GATE_SCREENSHOTS,
        "formatting": GATE_FORMATTING,
        "format": GATE_FORMATTING,
        "links": GATE_LINKS,
        "link": GATE_LINKS,
        "user-impact-quality": GATE_USER_IMPACT_QUALITY,
        "impact-quality": GATE_USER_IMPACT_QUALITY,
        # NB: no bare "msgref" alias — it would silently map to msgref-QUOTE, but a user typing
        # --skip-msgref most likely means the (non-skippable) msgref-TITLE gate; a silent no-op
        # against the wrong gate is worse than an "unknown gate" error (review finding).
        "msgref-quote": GATE_MSGREF_QUOTE,
        "links-url": GATE_LINKS_URL,
        "link-url": GATE_LINKS_URL,
        "read-before-edit": GATE_READ_BEFORE_EDIT,
        "duplicate": GATE_DUPLICATE,
        "todo-shaped": GATE_TODO_SHAPED,
        "todo": GATE_TODO_SHAPED,
    }
    if norm in aliases:
        return aliases[norm]
    raise ValueError(f"unknown gate {name!r} (valid: {', '.join(ALL_GATES)})")
