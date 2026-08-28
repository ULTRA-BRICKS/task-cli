"""`task mark-shipped` — the `gh ship` post-merge hook entry point.

Exercised entirely against the in-memory FakeBackend (see conftest.py); no network. The
one property every test here ultimately protects: **a merge is recorded, but it never
closes the ticket** — only `task check` + `task done`, with a real proof, may do that.
"""

from __future__ import annotations

import pytest

from tasklib.cli import build_parser, main
from tasklib.model import Criterion, State, Ticket


@pytest.fixture(autouse=True)
def _inject_fake(monkeypatch, fake_backend, isolated_state):
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: fake_backend)
    monkeypatch.setenv("TASK_SESSION", "testsess")
    return fake_backend


def _seed(fake_backend, **over) -> Ticket:
    defaults = {
        "title": "Fix the thing",
        "what": "the change",
        "why": "because",
        "user_impact": "users no longer hit the bug",
        "cost_of_inaction": "pain",
        "acceptance": [Criterion(text="it works"), Criterion(text="handles the empty case")],
    }
    defaults.update(over)
    return fake_backend.create(Ticket(**defaults))


_PR_URL = "https://github.com/acme/widgets/pull/42"


def test_mark_shipped_parses():
    parser = build_parser()
    ns = parser.parse_args(["mark-shipped", "#1", "--pr", _PR_URL])
    assert ns.command == "mark-shipped"
    assert ns.id == "#1"
    assert ns.pr == _PR_URL


def test_mark_shipped_requires_pr_flag():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["mark-shipped", "#1"])


def test_mark_shipped_empty_pr_is_user_error(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["mark-shipped", created.id, "--pr", "  "])
    assert rc == 2
    assert "--pr" in capsys.readouterr().out


def test_mark_shipped_moves_todo_to_in_review_and_records_pr(capsys, _inject_fake):
    created = _seed(_inject_fake)
    assert created.state is State.TODO

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0

    stored = _inject_fake.get(created.id)
    assert stored.state is State.IN_REVIEW
    assert stored.links["PR"] == _PR_URL
    out = capsys.readouterr().out
    assert "shipped" in out
    assert "state in-review" in out


def test_mark_shipped_moves_in_progress_to_in_review(_inject_fake):
    created = _seed(_inject_fake, state=State.IN_PROGRESS)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert _inject_fake.get(created.id).state is State.IN_REVIEW


@pytest.mark.parametrize("state", [State.IN_REVIEW, State.DONE])
def test_mark_shipped_never_moves_a_ticket_past_in_review(_inject_fake, state):
    """A ticket already IN_REVIEW/DONE is left in its own STATE — mark-shipped must never
    force-march it forward. The PR link is still recorded (durable bookkeeping)."""
    created = _seed(_inject_fake, state=state)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.state is state
    assert stored.links["PR"] == _PR_URL


def test_mark_shipped_skips_a_cancelled_ticket_entirely(capsys, _inject_fake):
    """CANCELLED is a deliberate dead end (mirrors transitions.py) — mark-shipped must not
    record ANYTHING against it: no link, no comment, no state touch, no TG notify."""
    created = _seed(_inject_fake, state=State.CANCELLED)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.state is State.CANCELLED
    assert "PR" not in stored.links
    assert _inject_fake.comments == []
    assert "cancelled" in capsys.readouterr().out


def test_mark_shipped_cancelled_ticket_honors_json_flag(capsys, _inject_fake):
    """The CANCELLED short-circuit must still honor --json (review finding: the human-only
    early return broke a scripted caller piping the output through a JSON parser)."""
    import json

    created = _seed(_inject_fake, state=State.CANCELLED)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "id": created.id,
        "url": created.url,
        "state": "cancelled",
        "recorded": False,
        "reason": "cancelled",
    }


def test_mark_shipped_on_a_done_ticket_gets_a_reference_comment_not_acceptance_framing(_inject_fake):
    """A ticket that's legitimately Done — every criterion checked — gets the calm reference
    note, not a warning. (Unchecked-while-Done is a distinct scenario, see the
    HYP-1295 tests below.)"""
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[Criterion(text="it works", checked=True, proof="shot.png")],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert len(_inject_fake.comments) == 1
    _, body = _inject_fake.comments[0]
    assert "already Done" in body
    assert "does NOT close" not in body
    assert "WARNING" not in body


def test_mark_shipped_on_a_done_ticket_prints_a_reference_note_not_acceptance_nudge(capsys, _inject_fake):
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[Criterion(text="it works", checked=True, proof="shot.png")],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "already Done" in out
    assert "acceptance needed" not in out


def test_mark_shipped_on_a_done_ticket_with_unchecked_criteria_warns_in_the_comment(_inject_fake):
    """HYP-1295 (2026-08-19): a PR body used a GitHub/Linear magic-close keyword ("Closes
    HYP-1295") against a ticket whose acceptance included a post-merge publish step. Linear's
    own GitHub integration auto-transitioned the issue to Done the moment the PR merged —
    entirely outside task-cli, so rule 2 (no close with unchecked boxes) never ran. By the
    time `mark-shipped` runs, the ticket already reads DONE with unchecked criteria. That
    combination must be surfaced loudly on the tracker, not filed under the calm reference
    note used for a legitimately-Done ticket."""
    created = _seed(_inject_fake, state=State.DONE)  # default acceptance is unchecked
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert len(_inject_fake.comments) == 1
    _, body = _inject_fake.comments[0]
    assert "WARNING" in body
    assert "unchecked" in body
    assert "already Done — recorded here for reference." not in body


def test_mark_shipped_on_a_done_ticket_with_unchecked_criteria_warns_in_the_printed_output(
    capsys, _inject_fake
):
    created = _seed(_inject_fake, state=State.DONE)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "unchecked" in out
    assert "reopen" in out


def test_mark_shipped_on_a_done_ticket_lists_only_the_still_unmet_criterion(capsys, _inject_fake):
    """Partial case (review finding): 2 criteria, 1 checked-with-proof, 1 unchecked. The
    warning must name only the outstanding one, at its REAL position in the full acceptance
    list — not the position of the always-filtered `unmet` list — since that number is meant
    to be fed straight to `task check <id> <n>`, which indexes the full list. Criterion 1 is
    the checked/proven one, criterion 2 is the unmet one, so the warning must say `[2]`, not
    the `[1]` an earlier (buggy) version of this test wrongly pinned."""
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[
            Criterion(text="it works", checked=True, proof="shot.png"),
            Criterion(text="handles the empty case"),
        ],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    _, body = _inject_fake.comments[0]
    assert "[2] handles the empty case" in body
    assert "it works" not in body.split("WARNING", 1)[1]


def test_mark_shipped_on_a_done_ticket_with_a_checked_but_unproven_criterion_warns_too(_inject_fake):
    """A criterion CHECKED without a proof or a force_reason fails the exact same on-done
    close gate (`policy.py`'s `unchecked_criteria_violation`) as an unchecked one — a ticket
    externally closed with that shape must not be waved through as legitimately Done."""
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[Criterion(text="publish release", checked=True)],  # no proof, no force_reason
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    _, body = _inject_fake.comments[0]
    assert "WARNING" in body
    assert "publish release" in body


def test_mark_shipped_json_output_reports_done_with_unmet_criteria(capsys, _inject_fake):
    import json

    created = _seed(_inject_fake, state=State.DONE)  # default acceptance is unchecked
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["done_with_unmet_criteria"] == ["it works", "handles the empty case"]


def test_mark_shipped_on_a_done_ticket_with_a_forced_checked_criterion_does_not_warn(_inject_fake):
    """Boundary the earlier version of this change left untested (review finding): a checked
    criterion with NO proof but a recorded ``force_reason`` is a legitimate close per
    `policy.py` (the `--force "<reason>"` escape) — it must NOT be treated as unmet."""
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[
            Criterion(text="it works", checked=True, force_reason="no visual surface, pure logic")
        ],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    _, body = _inject_fake.comments[0]
    assert "WARNING" not in body
    assert "already Done" in body


def test_mark_shipped_retry_on_a_legitimately_done_ticket_stays_idempotent(_inject_fake):
    """A ticket that's genuinely Done (every criterion checked+proven) posts exactly one
    comment across two identical mark-shipped calls — the new Done-with-unmet clause must
    not disturb the existing already-recorded idempotency for the normal, non-broken case."""
    created = _seed(
        _inject_fake,
        state=State.DONE,
        acceptance=[Criterion(text="it works", checked=True, proof="shot.png")],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert len(_inject_fake.comments) == 1
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    assert len(_inject_fake.comments) == 1  # no second comment


def test_mark_shipped_respects_a_disabled_acceptance_checked_gate(tmp_path, _inject_fake):
    """A repo with `enforce.acceptance_checked: false` never treats unchecked/unproven
    criteria as a close-gate violation (`policy.unchecked_criteria_violation` returns
    ``None`` there) — mark-shipped must not contradict that configured contract with a false
    Done-with-unmet warning."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/widgets"],
        check=True,
    )
    (repo / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\nenforce:\n  acceptance_checked: false\n",
        encoding="utf-8",
    )
    created = _seed(_inject_fake, state=State.DONE)  # default acceptance is unchecked
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--cwd", str(repo)])
    assert rc == 0
    _, body = _inject_fake.comments[0]
    assert "WARNING" not in body
    assert "already Done" in body


def test_mark_shipped_rejects_a_non_url_pr(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["mark-shipped", created.id, "--pr", "not a url"])
    assert rc == 2
    assert "--pr" in capsys.readouterr().out
    assert "PR" not in _inject_fake.get(created.id).links


def test_mark_shipped_rejects_a_malformed_commit_sha(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "not a sha!!"])
    assert rc == 2
    assert "--commit" in capsys.readouterr().out
    assert "PR" not in _inject_fake.get(created.id).links


def test_mark_shipped_backfills_commit_link_when_a_later_call_supplies_one(capsys, _inject_fake):
    """A retry that ARRIVES with a commit SHA the first call didn't have (ship.sh may notify
    before the merge SHA is queryable) must still be treated as new work, not skipped as
    already-recorded — otherwise the commit link never gets backfilled."""
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"
    assert "already recorded" not in capsys.readouterr().out


def test_mark_shipped_records_a_backend_comment(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    assert len(_inject_fake.comments) == 1
    ticket_id, body = _inject_fake.comments[0]
    assert ticket_id == created.id
    assert _PR_URL in body
    assert "abc1234" in body
    assert "does NOT close" in body


def test_mark_shipped_comment_survives_a_failed_persist_and_retry_completes_it(_inject_fake, monkeypatch):
    """If backend.update() fails AFTER the comment was already posted, the comment must not be
    lost, AND a subsequent retry (once the backend recovers) must still finish the job.

    Verifies the comment-before-update ordering (a review finding on an earlier version that
    commented AFTER update: a failed update left `already_recorded` false on retry, so a retry
    would redo the work — but the ORIGINAL ordering, comment-after-update, meant a comment
    failing after a successful update was lost forever once the retry short-circuited on
    already_recorded=True). Also verifies `_record_shipped_pr` never mutates the ticket object
    in place before `update()` actually persists (a review finding: `FakeBackend.get()` hands
    back the live stored object, so an in-place mutation would make the failed attempt LOOK
    already-recorded on retry even though update() never ran) — the first (failing) call must
    leave the stored ticket completely untouched, and the retry must re-post the comment and
    then successfully persist.
    """
    from tasklib.backends import BackendError

    created = _seed(_inject_fake)

    class _FlakyOnUpdate:
        def __init__(self, real):
            self._real = real
            self.should_fail = True

        def __getattr__(self, name):
            return getattr(self._real, name)

        def update(self, ticket):
            if self.should_fail:
                raise BackendError("simulated transient failure")
            return self._real.update(ticket)

    flaky = _FlakyOnUpdate(_inject_fake)
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: flaky)

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 2
    assert len(_inject_fake.comments) == 1  # comment survived the failed update
    assert "PR" not in _inject_fake.get(created.id).links  # nothing persisted yet

    flaky.should_fail = False
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    assert len(_inject_fake.comments) == 2  # retried: a second (duplicate, recoverable) comment
    stored = _inject_fake.get(created.id)
    assert stored.links["PR"] == _PR_URL
    assert stored.state is State.IN_REVIEW


def test_mark_shipped_derives_commit_link_for_github_pr(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"


def test_mark_shipped_no_commit_link_for_non_github_pr(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", "https://gitlab.com/acme/widgets/-/merge_requests/9", "--commit", "abc1234"])
    stored = _inject_fake.get(created.id)
    assert "Merge commit" not in stored.links


def test_mark_shipped_no_commit_link_without_a_sha(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    stored = _inject_fake.get(created.id)
    assert "Merge commit" not in stored.links


def test_mark_shipped_is_idempotent_on_repeat_for_the_same_pr(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc1 = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc1 == 0
    capsys.readouterr()

    rc2 = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc2 == 0
    # only ONE comment posted across both invocations — a retried ship must not spam the tracker.
    assert len(_inject_fake.comments) == 1
    assert "already recorded" in capsys.readouterr().out


def test_mark_shipped_prints_unchecked_acceptance_instructions(capsys, _inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "acceptance needed" in out
    assert "it works" in out
    assert "handles the empty case" in out
    assert "task check" in out
    assert "task done" in out


def test_mark_shipped_flags_ui_ticket_for_visual_proof(capsys, _inject_fake):
    created = _seed(_inject_fake, labels=["ui"])
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "visual proof" in out


def test_mark_shipped_reports_all_criteria_already_checked(capsys, _inject_fake):
    created = _seed(
        _inject_fake,
        acceptance=[
            Criterion(text="it works", checked=True, proof="before.png"),
            Criterion(text="handles the empty case", checked=True, proof="after.png"),
        ],
    )
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "already checked" in out
    assert "task done" in out


def test_mark_shipped_replace_without_a_new_commit_drops_the_stale_commit_link(_inject_fake):
    """A second, DIFFERENT PR with no --commit must not leave a "Merge commit" link that still
    points at the FIRST PR's commit while "PR" now points at the second — a silently
    mismatched pair is worse than no commit link at all."""
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", "https://github.com/acme/widgets/pull/7", "--commit", "aaaaaaa"])
    other = _inject_fake.get(created.id)
    assert other.links["Merge commit"] == "https://github.com/acme/widgets/commit/aaaaaaa"

    main(["mark-shipped", created.id, "--pr", _PR_URL])
    stored = _inject_fake.get(created.id)
    assert stored.links["PR"] == _PR_URL
    assert "Merge commit" not in stored.links


def test_mark_shipped_replace_with_a_new_commit_overwrites_the_old_link(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", "https://github.com/acme/widgets/pull/7", "--commit", "aaaaaaa"])
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "bbbbbbb"])
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/bbbbbbb"


def test_mark_shipped_transitions_even_when_the_pr_link_was_set_out_of_band(_inject_fake):
    """A TODO/IN_PROGRESS ticket whose Links["PR"] already matches (set some other way, or
    reverted back to TODO after a prior mark-shipped) must still be nudged to IN_REVIEW — the
    idempotency guard must never suppress the ONE thing this command promises for an
    active-not-reviewed ticket."""
    created = _seed(_inject_fake)
    created.links["PR"] = _PR_URL
    _inject_fake.update(created)
    assert _inject_fake.get(created.id).state is State.TODO

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    assert _inject_fake.get(created.id).state is State.IN_REVIEW


def test_mark_shipped_normalizes_an_out_of_band_raw_stored_pr_url_too(capsys, _inject_fake):
    """The stored Links["PR"] value is normalized before comparison too, not just the
    freshly-validated --pr input (review finding: normalizing only one side still compared a
    raw, un-normalized stored value — e.g. hand-edited with a trailing slash or different case
    — against a normalized retry as if they were different PRs, spuriously warning "replaced"
    and dropping a still-valid Merge commit link)."""
    created = _seed(_inject_fake)
    created.links["PR"] = "https://GitHub.com/acme/widgets/pull/42/"  # raw, un-normalized
    created.links["Merge commit"] = "https://github.com/acme/widgets/commit/abc1234"
    _inject_fake.update(created)

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"
    out = capsys.readouterr().out
    assert "replaced" not in out


def test_mark_shipped_github_pr_url_is_case_insensitive_on_the_host(_inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", "https://GitHub.com/acme/widgets/pull/42", "--commit", "abc1234"])
    stored = _inject_fake.get(created.id)
    assert stored.links["PR"] == _PR_URL
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"


def test_mark_shipped_normalizes_a_mixed_case_host_for_idempotency(capsys, _inject_fake):
    """A retry with a differently-cased host (GitHub's host is case-insensitive) must be
    recognized as the SAME PR — not treated as a "different PR" that drops the recorded
    commit link (review finding: without normalization this silently destroyed a still-valid
    Merge commit link on a trivially-different retry)."""
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", "https://GitHub.COM/acme/widgets/pull/42"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"
    out = capsys.readouterr().out
    assert "already recorded" in out
    assert "replaced" not in out


def test_mark_shipped_normalizes_a_trailing_slash_for_idempotency(capsys, _inject_fake):
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL + "/"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"
    assert "already recorded" in capsys.readouterr().out


@pytest.mark.parametrize(
    "variant",
    [
        _PR_URL + "?diff=split",
        _PR_URL + "#discussion_r1",
        _PR_URL + "/files",
        _PR_URL + "/commits",
    ],
)
def test_mark_shipped_normalizes_query_fragment_and_subpage_pr_urls(capsys, _inject_fake, variant):
    """A GitHub PR URL carrying a query string, fragment, or sub-page path (a copy-pasted
    tracking param, a deep link) must canonicalize to the SAME PR as the clean form — a review
    finding: an earlier normalization only lowercased scheme/host and stripped a trailing
    slash, so these variants still compared as a DIFFERENT PR and dropped a still-valid Merge
    commit link on retry."""
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", variant])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["PR"] == _PR_URL
    assert stored.links["Merge commit"] == "https://github.com/acme/widgets/commit/abc1234"
    out = capsys.readouterr().out
    assert "already recorded" in out
    assert "replaced" not in out


def test_mark_shipped_lowercases_a_mixed_case_commit_sha_for_idempotency(capsys, _inject_fake):
    """Git SHAs are case-folded hex — a retry with the SAME sha in a different case must not
    be treated as new work (review finding: an earlier version embedded the SHA verbatim, so
    ABC1234 vs abc1234 derived different Merge-commit links and duplicated the comment/notify)."""
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "ABC1234"])
    assert rc == 0
    assert len(_inject_fake.comments) == 1
    assert "already recorded" in capsys.readouterr().out


def test_mark_shipped_zero_criteria_ticket_gets_the_hedged_already_checked_message(capsys, _inject_fake):
    """A legacy/edge-case ticket with no acceptance criteria hits the same "all checked"
    branch as a ticket with checked criteria — the message must hedge ("if the other close
    gates pass") rather than promise `task done` will succeed outright, since the acceptance
    gate itself requires >=2 criteria."""
    created = _seed(_inject_fake, acceptance=[])
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    out = capsys.readouterr().out
    assert "if the other close gates pass" in out


def test_mark_shipped_json_output(capsys, _inject_fake):
    import json

    created = _seed(_inject_fake)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "id": created.id,
        "url": created.url,
        "state": "in-review",
        "pr": _PR_URL,
        "already_recorded": False,
        "moved_to_review": True,
        "replaced_pr": None,
        "commit_url": None,
        "unchecked_criteria": ["it works", "handles the empty case"],
        "done_with_unmet_criteria": [],
    }


def test_mark_shipped_json_output_includes_commit_url(capsys, _inject_fake):
    import json

    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["commit_url"] == "https://github.com/acme/widgets/commit/abc1234"


def test_mark_shipped_a_second_different_pr_replaces_the_first_with_a_warning(capsys, _inject_fake):
    """A ticket shipped via TWO PRs (a follow-up fix, a revert-and-reland) must not silently
    lose the first PR's link with no trace — a warning is printed and the JSON payload
    reports the replaced URL, even though only the latest link is kept in Links (the comment
    history preserves both)."""
    other_pr = "https://github.com/acme/widgets/pull/7"
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", other_pr])
    capsys.readouterr()

    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.links["PR"] == _PR_URL
    out = capsys.readouterr().out
    assert other_pr in out and "replaced" in out
    assert len(_inject_fake.comments) == 2


def test_mark_shipped_recorded_links_pass_the_close_time_links_gate(_inject_fake):
    """The whole point of validating --pr/--commit up front is that the Links entries this
    command writes must never trip the close-time links-url gate — pin that end to end rather
    than just trusting the input validation, in case a future key/value change (e.g. renaming
    "Merge commit" to something with a colon) silently breaks it."""
    from tasklib.policy import EnforceConfig, links_url_violation

    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL, "--commit", "abc1234"])
    stored = _inject_fake.get(created.id)
    assert links_url_violation(stored, EnforceConfig.from_dict({})) is None


def test_mark_shipped_unknown_ticket_is_clean_error(capsys, _inject_fake):
    rc = main(["mark-shipped", "#999", "--pr", _PR_URL])
    assert rc == 2
    assert "error:" in capsys.readouterr().out


def _patch_notify(monkeypatch):
    """Capture daemon.notify calls; return the list of (message, notifier) tuples."""
    from tasklib import daemon as _d

    calls: list[tuple[str, tuple]] = []

    def _fake_notify(msg, notifier):
        calls.append((msg, notifier))
        return True

    monkeypatch.setattr(_d, "notify", _fake_notify)
    return calls


def test_mark_shipped_sends_tg_notification(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    created = _seed(_inject_fake)
    rc = main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert rc == 0
    assert len(calls) == 1
    msg, _ = calls[0]
    assert "shipped" in msg and created.id in msg


def test_mark_shipped_does_not_renotify_on_repeat(monkeypatch, _inject_fake):
    calls = _patch_notify(monkeypatch)
    created = _seed(_inject_fake)
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    calls.clear()
    main(["mark-shipped", created.id, "--pr", _PR_URL])
    assert calls == []
