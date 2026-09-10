"""Detect a ticket that reads as an agent's own in-session process step, not durable product
work — the "review my diff before I commit it" category error.

Pure, stdlib-only, mirrors :mod:`tasklib.quality`'s shape: one assess function returning a
list of problems (empty = fine), wrapped into a `Violation` by ``policy.py``.

Why this exists: an audit of the ecosystem's issue trackers found dozens of tickets like
"Review uncommitted X diff" / "Adversarial review of uncommitted Y diff", almost always with
an EMPTY "What" section and a boilerplate "Why" ("the requested review needs a durable
record"). These are a session's own TodoList step (a review-before-commit checklist) filed as
a durable GitHub issue / Linear ticket instead — durable because `require-ticket-before-commit`
(an agent-tools hook) demands SOME ticket reference before a commit, and a plain process step
has no real product ticket to point at, so one gets manufactured. The harness's own ephemeral
TodoList tool (TodoWrite/TaskCreate) is what this session-scoped bookkeeping is *for*.

This gate is deliberately NON-BLOCKING (unlike the content gates in policy.py): a `create` is
never refused over it. It only prints a hint, via `PolicyResult.warnings` — see policy.py's
`check()`. It is still recorded via `--skip-todo-shaped "<reason>"` / `--force "<reason>"` so a
deliberate false-positive is silenced without repeating the hint on every future command that
re-scans the stored ticket.
"""

from __future__ import annotations

import re

# First-person, session-scoped phrasing that names the CURRENT diff/commit/session rather than
# a reusable product concern. Deliberately narrow (English-only, matching what the audit's real
# examples used) — a false negative here just means the hint doesn't fire, which is harmless;
# a false positive nags at a legitimate ticket, which is the more expensive mistake to avoid.
_PROCESS_STEP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "review my/this/the (...) diff", "review my uncommitted changes"
    re.compile(
        r"\breview\s+(?:my|this|the)\s+(?:own\s+)?(?:current\s+|latest\s+|uncommitted\s+|"
        r"staged\s+)*(?:diff|changes|code)\b",
        re.IGNORECASE,
    ),
    # "adversarial review of ... diff" / "adversarial review of my/this/the ..."
    re.compile(r"\badversarial\s+review\s+of\b", re.IGNORECASE),
    # "before committing" / "before I commit" / "before shipping" / "before merging"
    re.compile(
        r"\bbefore\s+(?:i\s+)?(?:committing|commit|shipping|ship|merging|merge)\b",
        re.IGNORECASE,
    ),
    # "my uncommitted diff/changes/session", "this session's diff"
    re.compile(
        r"\b(?:my|this\s+session'?s?)\s+(?:current\s+|uncommitted\s+)?"
        r"(?:diff|changes|session)\b",
        re.IGNORECASE,
    ),
)

# A ticket whose title names a review/diff-check step (even without a first-person pronoun —
# "Review uncommitted omp hook bridge diff" has no "my"/"this") is only suspicious when it is
# ALSO carrying no real content: an empty "What" section is exactly the shape a review checklist
# item has when auto-filed to satisfy a commit-time ticket gate rather than authored as a ticket.
_REVIEW_TITLE_RE = re.compile(
    r"\breview(?:ed|ing)?\b.{0,40}\bdiff\b|\breview\s+(?:uncommitted|staged)\b",
    re.IGNORECASE,
)

_MIN_WHAT_WORDS = 8
_WORD_RE = re.compile(r"[A-Za-z']+")


def assess_todo_shaped(title: str, what: str) -> list[str]:
    """Return the todo-vs-ticket hints for (title, what) — empty means "looks like a real
    ticket". At most one hint is returned (there is nothing to gain from stacking phrasings)."""
    combined = f"{title}\n{what}"
    for pattern in _PROCESS_STEP_PATTERNS:
        m = pattern.search(combined)
        if m:
            return [
                f"the title/what reads as a process step for the current session "
                f"({m.group(0)!r}), not durable product work"
            ]
    what_words = len(_WORD_RE.findall(what))
    if _REVIEW_TITLE_RE.search(title) and what_words < _MIN_WHAT_WORDS:
        return [
            "the title names a review/diff-check step but the What section is empty or "
            "near-empty — a durable ticket says what is concretely wrong, not just 'review X'"
        ]
    return []
