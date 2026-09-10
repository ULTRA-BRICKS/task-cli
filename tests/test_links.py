"""links.py — typed ticket relationships (`task link`) + the post-create relatedness nudge."""

from __future__ import annotations

import pytest

from tasklib.links import (
    LinkEntry,
    apply_entry,
    find_related_candidates,
    link_key,
    linked_other_ids,
    mirrors,
    plan_link,
)
from tasklib.model import State, Ticket


# ── link_key / mirrors ───────────────────────────────────────────────────────────────


def test_link_key_shapes():
    assert link_key("blocks", "#149") == "Blocks #149"
    assert link_key("blocked-by", "#149") == "Blocked by #149"
    assert link_key("relates-to", "HYP-42") == "Relates to HYP-42"
    assert link_key("duplicate-of", "#7") == "Duplicate of #7"


def test_link_key_rejects_unknown_relation():
    with pytest.raises(ValueError):
        link_key("no-such-relation", "#1")


def test_mirrors():
    assert mirrors("blocks") == "blocked-by"
    assert mirrors("blocked-by") == "blocks"
    assert mirrors("relates-to") == "relates-to"
    # duplicate-of is deliberately NOT mirrored (see links.py's module docstring)
    assert mirrors("duplicate-of") is None


# ── plan_link ─────────────────────────────────────────────────────────────────────────


def test_plan_link_blocks_writes_both_sides():
    entries = plan_link("#1", "https://x/1", "blocks", "#2", "https://x/2")
    assert entries == [
        LinkEntry("#1", "Blocks #2", "https://x/2"),
        LinkEntry("#2", "Blocked by #1", "https://x/1"),
    ]


def test_plan_link_relates_to_is_symmetric():
    entries = plan_link("#1", "https://x/1", "relates-to", "#2", "https://x/2")
    assert entries == [
        LinkEntry("#1", "Relates to #2", "https://x/2"),
        LinkEntry("#2", "Relates to #1", "https://x/1"),
    ]


def test_plan_link_duplicate_of_is_one_sided():
    entries = plan_link("#1", "https://x/1", "duplicate-of", "#2", "https://x/2")
    assert entries == [LinkEntry("#1", "Duplicate of #2", "https://x/2")]


# ── apply_entry (idempotency) ───────────────────────────────────────────────────────


def test_apply_entry_adds_a_new_key():
    links, changed = apply_entry({}, LinkEntry("#1", "Blocks #2", "https://x/2"))
    assert changed is True
    assert links == {"Blocks #2": "https://x/2"}


def test_apply_entry_is_a_noop_when_already_present():
    existing = {"Blocks #2": "https://x/2"}
    links, changed = apply_entry(existing, LinkEntry("#1", "Blocks #2", "https://x/2"))
    assert changed is False
    assert links == existing
    assert links is not existing  # always a fresh dict, even on the no-op branch


def test_apply_entry_does_not_mutate_the_input_dict_when_it_does_change():
    existing = {"PR": "https://x/pr"}
    links, changed = apply_entry(existing, LinkEntry("#1", "Blocks #2", "https://x/2"))
    assert changed is True
    assert existing == {"PR": "https://x/pr"}  # original untouched
    assert links == {"PR": "https://x/pr", "Blocks #2": "https://x/2"}


# ── linked_other_ids ─────────────────────────────────────────────────────────────────


def test_linked_other_ids_extracts_ids_from_structured_keys():
    links = {"Blocks #2": "https://x/2", "Blocked by HYP-9": "https://x/9", "PR": "https://x/pr"}
    assert linked_other_ids(links) == {"#2", "HYP-9"}


def test_linked_other_ids_empty_when_no_structured_entries():
    assert linked_other_ids({"PR": "https://x/pr", "Merge commit": "https://x/c"}) == set()


# ── find_related_candidates (the post-`task new` nudge) ────────────────────────────


def _ticket(id_, title, labels=None, state=State.TODO, provider_closed=False):
    return Ticket(
        id=id_, title=title, labels=labels or [], state=state, provider_closed=provider_closed
    )


def test_related_titles_trigger_a_candidate():
    new = _ticket("#3", "daemon activity not tracked in rig stats")
    others = [_ticket("#1", "rig stats should track daemon activity")]
    found = find_related_candidates(new, others)
    assert [c.ticket.id for c in found] == ["#1"]
    assert {"daemon", "activity", "tracked", "rig", "stats"} & found[0].shared_keywords


def test_unrelated_titles_do_not_trigger():
    new = _ticket("#3", "daemon activity not tracked in rig stats")
    others = [_ticket("#1", "fix flaky pagination test in the gantt view")]
    assert find_related_candidates(new, others) == []


def test_shared_label_plus_one_keyword_triggers():
    new = _ticket("#3", "review threads P2 in dependency script", labels=["review"])
    others = [_ticket("#1", "another P2 in the leftover-grep script", labels=["review"])]
    found = find_related_candidates(new, others)
    assert [c.ticket.id for c in found] == ["#1"]


def test_single_shared_generic_label_alone_does_not_trigger():
    new = _ticket("#3", "totally different topic entirely", labels=["agent", "session:main"])
    others = [_ticket("#1", "nothing in common here at all", labels=["agent", "session:main"])]
    assert find_related_candidates(new, others) == []


def test_a_single_shared_keyword_alone_does_not_trigger():
    new = _ticket("#3", "improve the gantt export flow")
    others = [_ticket("#1", "unrelated gantt bug report")]
    # shares "gantt" only (one keyword) — too weak on its own
    assert find_related_candidates(new, others) == []


def test_excludes_done_and_cancelled_tickets():
    new = _ticket("#3", "daemon activity not tracked in rig stats")
    others = [
        _ticket("#1", "rig stats should track daemon activity", state=State.DONE),
        _ticket("#2", "rig stats should track daemon activity", state=State.CANCELLED),
    ]
    assert find_related_candidates(new, others) == []


def test_excludes_the_new_ticket_itself():
    new = _ticket("#3", "daemon activity not tracked in rig stats")
    assert find_related_candidates(new, [new]) == []


def test_excludes_provider_closed_even_when_state_reads_active():
    # task-cli#154 review finding (Grok): a GitHub issue closed natively (e.g. a PR's `Closes
    # #N`) can keep a stale `status:in-progress` label — `state` alone would call it open, and
    # the nudge would suggest linking to (and, via `task link`, silently reopening) it.
    new = _ticket("#3", "daemon activity not tracked in rig stats")
    others = [
        _ticket(
            "#1",
            "rig stats should track daemon activity",
            state=State.IN_PROGRESS,
            provider_closed=True,
        )
    ]
    assert find_related_candidates(new, others) == []


def test_results_capped_at_limit_and_ranked_by_overlap():
    new = _ticket("#9", "daemon activity not tracked in rig stats reporting")
    others = [
        _ticket("#1", "rig stats overlap only"),  # 2 shared keywords
        _ticket("#2", "daemon activity not tracked in rig stats"),  # 5 shared keywords
        _ticket("#3", "stats reporting daemon here"),  # 3 shared keywords
    ]
    found = find_related_candidates(new, others, limit=2)
    assert [c.ticket.id for c in found] == ["#2", "#3"]
