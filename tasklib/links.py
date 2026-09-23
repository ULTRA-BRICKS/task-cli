"""Typed relationships between tickets ("task link") + the post-create relatedness nudge.

Pure, stdlib-only — no network, no filesystem. Two independent pieces (task-cli#154):

1. **`task link <id> <relation> <other-id>`** writes a structured entry into `ticket.links`
   (the same dict `## Links` already renders — see `render.py`) for one of the relations in
   `RELATIONS` (`blocks` / `blocked-by` / `relates-to` / `duplicate-of` / `follow-up-of` /
   `followed-up-by` — see "Mirroring" below). `plan_link` decides WHAT to write (on which
   ticket, under which key, pointing at which URL); `cli.cmd_link` does the actual fetch/update.
2. **The post-`task new` nudge** does a cheap same-repo relatedness scan (title-keyword and
   label overlap — no embeddings, no fuzzy NLP) over currently-open tickets and suggests
   candidates; `cli.cmd_create` prints the result, never acts on it.

## Link-entry key shape

A relation to `other_id` is written as one `ticket.links` entry keyed
``"<Relation label> <other_id>"`` (e.g. ``"Blocks #149"``), never a bare ``"Blocks"`` key — a
ticket can legitimately block/relate-to more than one other ticket, and `ticket.links` is a flat
``dict[str, str]``, so a bare relation-name key would silently overwrite an earlier link to a
DIFFERENT ticket the next time the same relation type is used. Embedding the id in the key keeps
each (relation, other-ticket) pair its own entry and makes a repeat `task link` call for the SAME
pair naturally idempotent (same key, same value -> `apply_entry` reports no change, so
`cli.cmd_link` skips the redundant `backend.update()`).

## Mirroring

`blocks`/`blocked-by` are inverses (A blocks B implies B is blocked-by A), `follow-up-of`/
`followed-up-by` are inverses the same way (A is a follow-up of B implies B is followed up by
A — deploy-followup-links, tracing a regression found after a ticket shipped back to the
original), and `relates-to` is symmetric (A relates-to B implies B relates-to A) — `plan_link`
returns entries for BOTH tickets for all of those relations. Like `blocks`/`blocked-by`, and
unlike the symmetric `relates-to` (identical `"Relates to #<id>"` string on both sides),
`follow-up-of`/`followed-up-by` write GENUINELY DIFFERENT label strings on each side
(`"Follow-up of #<id>"` vs `"Followed up by #<id>"`). `duplicate-of` is deliberately NOT
mirrored: "A is a duplicate of B" names B as the canonical ticket, and auto-adding a reverse "is
duplicated by A" onto B has two costs neither `blocks` nor `relates-to` carries — it can
accumulate many entries on a popular canonical ticket every time something gets marked a dupe of
it (those two are typically 1:1 pairs), and a hasty `duplicate-of` call by the wrong party (A is
not really a dup of B) writes an assertion onto B — a ticket the caller may not even be looking
at — that nobody reviewing B would think to double-check. The one-sided form (only A's `## Links`
gets `Duplicate of` -> B) already gives the same audit trail without those costs.

Not validated: nothing stops a caller from writing a contradictory pair (`task link A blocks B`
then `task link B blocks A`), which leaves both tickets claiming to both block and be blocked by
each other. Accepted gap (review finding, task-cli#154) — `task link` records what it's told,
it doesn't reason about the relationship graph; a human/agent contradicting itself is a content
problem, not a tool bug.

## Already-linked prose (the links gate interaction)

`policy.links_violation` scans ticket PROSE for bare, un-linked references (`references.py`).
That scan has no idea `ticket.links` exists — it only masks markdown links/URLs already sitting
INSIDE the scanned prose. `linked_other_ids` extracts the set of other-ticket ids that already
have a structured `task link` entry (parsed back out of the key shape above), so
`policy.links_violation` can drop any bare reference matching one of those ids before deciding
whether the gate fires — a ticket id already linked via `task link` is not double-flagged just
because it's also mentioned in prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import State, Ticket

_LABELS: dict[str, str] = {
    "blocks": "Blocks",
    "blocked-by": "Blocked by",
    "relates-to": "Relates to",
    "duplicate-of": "Duplicate of",
    # Traceability past `done`: a ticket found to regress an already-shipped one links back
    # `follow-up-of` the original (task-cli deploy-followup-links). Structurally identical to
    # `blocks`/`blocked-by` — two entries, asymmetric label strings, both directly invocable —
    # even though the documented workflow only ever calls the `follow-up-of` direction by hand.
    "follow-up-of": "Follow-up of",
    "followed-up-by": "Followed up by",
}

# Derived from _LABELS (review finding, task-cli#154 round 3) so the relation set has exactly
# ONE source of truth — a hand-kept second tuple could drift out of sync with the labels dict.
RELATIONS: tuple[str, ...] = tuple(_LABELS)

# The relation to write on the OTHER ticket, or absent when a relation isn't mirrored
# (`duplicate-of` — see module docstring).
_INVERSE: dict[str, str] = {
    "blocks": "blocked-by",
    "blocked-by": "blocks",
    "relates-to": "relates-to",
    "follow-up-of": "followed-up-by",
    "followed-up-by": "follow-up-of",
}


@dataclass(frozen=True)
class LinkEntry:
    """One `ticket.links` key/value a `task link` call should ensure exists, and which ticket
    (by id) it belongs on."""

    ticket_id: str
    key: str
    url: str


def link_key(relation: str, other_id: str) -> str:
    """The `ticket.links` key for `relation` pointing at `other_id` (e.g. `"Blocks #149"`)."""
    if relation not in _LABELS:
        raise ValueError(f"unknown relation {relation!r} (one of {', '.join(RELATIONS)})")
    return f"{_LABELS[relation]} {other_id}"


def mirrors(relation: str) -> str | None:
    """The relation to write on the OTHER ticket, or `None` when `relation` isn't mirrored."""
    return _INVERSE.get(relation)


def plan_link(
    source_id: str, source_url: str, relation: str, other_id: str, other_url: str
) -> list[LinkEntry]:
    """The `LinkEntry` values a `task link <source_id> <relation> <other_id>` call should ensure
    exist. Always includes the entry on `source_id`; adds the mirrored entry on `other_id` too
    when `relation` has one (see module docstring)."""
    entries = [LinkEntry(source_id, link_key(relation, other_id), other_url)]
    inverse = mirrors(relation)
    if inverse:
        entries.append(LinkEntry(other_id, link_key(inverse, source_id), source_url))
    return entries


def apply_entry(links: dict[str, str], entry: LinkEntry) -> tuple[dict[str, str], bool]:
    """Merge `entry` into a ticket's `links` dict. Returns `(new_links, changed)` — `changed` is
    `False` when the key already maps to the same value (idempotent no-op), so a caller can skip
    a redundant `backend.update()`. `new_links` is ALWAYS a fresh dict, on both branches (review
    finding: an earlier version returned the input `links` object itself, unmodified, on the
    no-op branch — harmless for the current caller, which only reads `changed`, but a trap for
    a future one that mutates `new_links` expecting a copy and would silently corrupt the
    ticket's live `links` dict instead)."""
    if links.get(entry.key) == entry.url:
        return dict(links), False
    merged = dict(links)
    merged[entry.key] = entry.url
    return merged, True


def linked_other_ids(links: dict[str, str]) -> set[str]:
    """The other-ticket ids already carrying a structured relation entry in `links` (keys
    `task link` writes, `"<Label> <id>"`) — see module docstring's "already-linked prose"
    section. Used by `policy.links_violation` to exclude a bare prose reference that is already
    structurally linked."""
    ids: set[str] = set()
    for key in links:
        for label in _LABELS.values():
            prefix = label + " "
            if key.startswith(prefix) and key[len(prefix):]:
                ids.add(key[len(prefix):])
    return ids


# ── the post-`task new` relatedness nudge ────────────────────────────────────────────

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "to", "for", "and", "or", "of", "in", "on", "with", "should",
        "is", "are", "this", "that", "not", "when", "it", "its", "be", "by", "as", "at",
        "so", "but", "we", "task", "add", "fix", "make", "into", "from", "than", "then",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")
# Labels every ticket this tool creates tends to carry (a `session:<id>` label, and the
# blanket `agent` label) — too common to be a relatedness SIGNAL on their own.
_GENERIC_LABEL_PREFIXES = ("session:",)
_GENERIC_LABELS = frozenset({"agent"})


def _title_keywords(title: str) -> set[str]:
    return {w for w in _WORD_RE.findall(title.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _signal_labels(labels: list[str]) -> set[str]:
    return {
        lo
        for label in labels
        if (lo := label.strip().lower())
        and lo not in _GENERIC_LABELS
        and not lo.startswith(_GENERIC_LABEL_PREFIXES)
    }


@dataclass(frozen=True)
class RelatedCandidate:
    """One same-repo ticket the post-create nudge flags as plausibly related, with the overlap
    that earned it a mention (so the caller can explain *why* it was suggested)."""

    ticket: Ticket
    shared_keywords: frozenset[str]
    shared_labels: frozenset[str]


def find_related_candidates(
    new_ticket: Ticket, others: list[Ticket], *, limit: int = 5
) -> list[RelatedCandidate]:
    """Cheap same-repo relatedness scan for the post-`task new` nudge: title-keyword and label
    overlap only — no embeddings, no fuzzy NLP (task-cli#154).

    A candidate qualifies when it shares 2+ non-trivial title keywords with `new_ticket`, OR
    shares at least one non-generic label AND at least 1 title keyword. Either signal alone (a
    single common word; only the `agent`/`session:*` label every ticket this tool creates
    already carries) is too weak to be worth flagging. Open tickets only — excludes `DONE`/
    `CANCELLED` *and* any ticket with `provider_closed=True` (review finding, task-cli#154: a
    GitHub issue closed outside task-cli, e.g. via a PR's native `Closes #N`, can keep a stale
    `status:in-progress` label — `state` alone would call it open. Same guard `stale_notice.py`
    uses for the identical reason: the provider's own closed signal always wins over a
    possibly-stale managed label). The ticket never suggests itself. Ranked by (keyword
    overlap, label overlap) descending, capped at `limit`.
    """
    new_keywords = _title_keywords(new_ticket.title)
    new_labels = _signal_labels(new_ticket.labels)
    candidates: list[RelatedCandidate] = []
    for other in others:
        if other.id == new_ticket.id:
            continue
        if other.state in (State.DONE, State.CANCELLED) or other.provider_closed:
            continue
        shared_kw = new_keywords & _title_keywords(other.title)
        shared_lbl = new_labels & _signal_labels(other.labels)
        if len(shared_kw) >= 2 or (shared_lbl and shared_kw):
            candidates.append(RelatedCandidate(other, frozenset(shared_kw), frozenset(shared_lbl)))
    # `RelatedCandidate` already carries the overlap it was scored on — sort directly off those
    # fields instead of keeping a parallel score tuple in sync by hand (review finding).
    candidates.sort(key=lambda c: (len(c.shared_keywords), len(c.shared_labels)), reverse=True)
    return candidates[:limit]
