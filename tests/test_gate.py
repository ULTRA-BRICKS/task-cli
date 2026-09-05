"""`task gate` (the read-only ship-time acceptance verdict), multi-criterion `task check`,
`task accept` (the interactive walk), and the recorded post-merge-acceptance opt-out.

Exercised entirely against the in-memory FakeBackend (see conftest.py); no network, no TTY.
The one property every test here ultimately protects: **a ticket can be merged only when its
acceptance is proven — or when the ticket itself records, with a reason, that acceptance is
inherently post-merge** (task-cli#115: tickets were closing on merge behind task-cli's back).
"""

from __future__ import annotations

import json

import pytest

from tasklib.cli import build_parser, main
from tasklib.model import Criterion, State, Ticket
from tasklib.policy import POST_MERGE_ACCEPTANCE, acceptance_gate, EnforceConfig


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
        "acceptance": [
            Criterion(text="it works"),
            Criterion(text="handles the empty case"),
            Criterion(text="survives a restart"),
        ],
    }
    defaults.update(over)
    return fake_backend.create(Ticket(**defaults))


def _gate_json(capsys, ticket_id: str) -> tuple[int, dict]:
    rc = main(["gate", ticket_id, "--json"])
    return rc, json.loads(capsys.readouterr().out)


# ── policy: the pure evaluator reuses the on-done gate ──────────────────────────────


def test_policy_gate_uses_the_same_predicate_as_the_close_gate():
    t = Ticket(
        acceptance=[
            Criterion(text="a", checked=True, proof="a.png"),
            Criterion(text="b", checked=True),  # ticked in the web UI, no proof
            Criterion(text="c"),
        ]
    )
    report = acceptance_gate(t, EnforceConfig())
    assert report.ok is False
    assert [c.text for c in report.unchecked] == ["c"]
    assert [c.text for c in report.proofless] == ["b"]
    assert report.post_merge_reason == ""
    assert report.gate_enabled is True


def test_policy_gate_passes_when_every_criterion_is_proven():
    t = Ticket(
        acceptance=[
            Criterion(text="a", checked=True, proof="a.png"),
            Criterion(text="b", checked=True, force_reason="no UI to shoot"),
        ]
    )
    report = acceptance_gate(t, EnforceConfig())
    assert report.ok is True
    assert report.unchecked == [] and report.proofless == []


def test_policy_gate_honours_the_recorded_post_merge_opt_out():
    t = Ticket(acceptance=[Criterion(text="a")], skips={POST_MERGE_ACCEPTANCE: "release publish happens after merge"})
    report = acceptance_gate(t, EnforceConfig())
    assert report.ok is True
    assert report.post_merge_reason == "release publish happens after merge"
    assert [c.text for c in report.unchecked] == ["a"], "the gaps are still reported alongside the opt-out"


def test_policy_gate_disabled_in_config_passes_and_says_so():
    # >=2 criteria (the default acceptance_min) so the SEPARATE minimum-count gate — which
    # `acceptance_checked=False` does not touch — doesn't also fail this ticket for a
    # different reason than the one under test.
    t = Ticket(acceptance=[Criterion(text="a"), Criterion(text="b")])
    report = acceptance_gate(t, EnforceConfig(acceptance_checked=False))
    assert report.ok is True
    assert report.gate_enabled is False


def test_policy_gate_below_minimum_criteria_refuses_even_with_full_proof():
    """task-cli#115 review round 1 (Opus): a 1-criterion, FULLY-PROVEN ticket used to pass
    `task gate` (only zero criteria was checked) and then still refuse `task done` (the
    acceptance-criteria minimum, default 2, is a SEPARATE gate `unchecked_criteria_violation`
    never covers) — a PR could merge whose ticket could never legitimately close."""
    t = Ticket(acceptance=[Criterion(text="a", checked=True, proof="a.png")])
    report = acceptance_gate(t, EnforceConfig())
    assert report.ok is False and report.below_minimum is True
    assert report.unchecked == [] and report.proofless == []  # the one criterion IS proven


def test_policy_gate_below_minimum_honours_its_own_skip():
    """The minimum-count gate (`acceptance-criteria`) is independently skippable — unlike the
    hard `acceptance-checked` gate — so a ticket that recorded `--skip-acceptance "<reason>"`
    must not fail the merge gate for the very violation it explicitly waived."""
    from tasklib.policy import GATE_ACCEPTANCE

    t = Ticket(
        acceptance=[Criterion(text="a", checked=True, proof="a.png")],
        skips={GATE_ACCEPTANCE: "single-outcome fix, a second criterion would be padding"},
    )
    report = acceptance_gate(t, EnforceConfig())
    assert report.ok is True and report.below_minimum is False


def test_policy_gate_below_minimum_is_independent_of_the_checked_proof_gate():
    """Disabling ONLY `acceptance_checked` (the proof-checking gate) must NOT bypass the
    independent minimum-count gate — `task done` would still refuse on count alone."""
    t = Ticket(acceptance=[Criterion(text="a")])
    report = acceptance_gate(t, EnforceConfig(acceptance_checked=False))
    assert report.ok is False and report.below_minimum is True
    # disabling BOTH gates is what actually bypasses everything
    report2 = acceptance_gate(t, EnforceConfig(acceptance_checked=False, acceptance_criteria=False))
    assert report2.ok is True and report2.below_minimum is False


def test_policy_gate_refuses_a_ticket_with_zero_criteria():
    """Nothing to accept is not acceptance: a ticket authored in the tracker's web UI without
    criteria must not sail through the merge gate on vacuous truth."""
    report = acceptance_gate(Ticket(acceptance=[]), EnforceConfig())
    assert report.ok is False and report.no_criteria is True and report.below_minimum is True
    # disabling the proof gate alone still leaves the independent minimum-count gate active —
    # both must be off (or acceptance_min: 0) to genuinely bypass a criteria-less ticket
    disabled = acceptance_gate(
        Ticket(acceptance=[]), EnforceConfig(acceptance_checked=False, acceptance_criteria=False)
    )
    assert disabled.ok is True and disabled.no_criteria is False and disabled.below_minimum is False
    opted = Ticket(acceptance=[], skips={POST_MERGE_ACCEPTANCE: "publish after merge"})
    assert acceptance_gate(opted, EnforceConfig()).ok is True


def test_post_merge_opt_out_round_trips_through_the_body():
    """The real backends re-parse the body on every read: the opt-out must survive
    render -> parse, or the gate would stop seeing it the moment the ticket is re-fetched."""
    from tasklib.render import parse, render

    t = Ticket(
        title="t", what="w", why="y", user_impact="u", cost_of_inaction="c",
        acceptance=[Criterion(text="a")], skips={POST_MERGE_ACCEPTANCE: "release publish happens after merge"},
    )
    back = parse(render(t))
    assert back.skips[POST_MERGE_ACCEPTANCE] == "release publish happens after merge"
    assert acceptance_gate(back, EnforceConfig()).post_merge_reason == "release publish happens after merge"


def test_post_merge_opt_out_never_waives_the_close_gate(_inject_fake, capsys):
    """The opt-out is for the MERGE gate only: `task done` still refuses (acceptance-checked is
    non-skippable) — the ticket must be accepted for real, just after the merge."""
    created = _seed(_inject_fake, skips={POST_MERGE_ACCEPTANCE: "post-merge publish"})
    assert main(["gate", created.id]) == 0
    assert main(["done", created.id]) == 2
    assert "acceptance-checked" in capsys.readouterr().out


# ── `task gate` CLI ──────────────────────────────────────────────────────────────────


def test_gate_parses():
    ns = build_parser().parse_args(["gate", "#1", "--json"])
    assert ns.command == "gate" and ns.id == "#1" and ns.json is True


def test_gate_refuses_with_exit_1_and_lists_the_gaps(capsys, _inject_fake):
    created = _seed(_inject_fake)
    main(["check", created.id, "1", "--proof", "one.png"])
    capsys.readouterr()
    rc = main(["gate", created.id])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not accepted" in out
    assert "[2] handles the empty case" in out and "[3] survives a restart" in out
    assert "task check" in out and "task accept" in out


def test_gate_json_contract(capsys, _inject_fake):
    created = _seed(_inject_fake)
    main(["check", created.id, "1", "--proof", "one.png"])
    # a box ticked in the web UI round-trips as checked-without-proof
    stored = _inject_fake.get(created.id)
    stored.acceptance[1].checked = True
    _inject_fake.update(stored)
    capsys.readouterr()
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 1
    assert payload == {
        "id": created.id,
        "ok": False,
        "state": "todo",
        "gate_enabled": True,
        "post_merge_acceptance": None,
        "criteria": 3,
        "below_minimum": False,
        "unchecked": [{"index": 3, "text": "survives a restart"}],
        "proofless": [{"index": 2, "text": "handles the empty case"}],
    }


def test_gate_passes_with_exit_0_when_fully_accepted(capsys, _inject_fake):
    created = _seed(_inject_fake)
    for i in (1, 2, 3):
        main(["check", created.id, str(i), "--proof", f"{i}.png"])
    capsys.readouterr()
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 0
    assert payload["ok"] is True and payload["unchecked"] == [] and payload["proofless"] == []


def test_gate_reports_the_post_merge_opt_out_and_passes(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["change", created.id, "--post-merge-acceptance", "the release is published after merge"])
    assert rc == 0
    capsys.readouterr()
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["post_merge_acceptance"] == "the release is published after merge"
    assert len(payload["unchecked"]) == 3, "the gaps are still listed so the shipper knows what is owed"
    rc = main(["gate", created.id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "post-merge" in out and "the release is published after merge" in out


def test_change_records_the_opt_out_under_skipped_gates(_inject_fake):
    created = _seed(_inject_fake)
    main(["change", created.id, "--post-merge-acceptance", "publish step"])
    stored = _inject_fake.get(created.id)
    assert stored.skips[POST_MERGE_ACCEPTANCE] == "publish step"
    assert "post-merge-acceptance: publish step" in stored.raw_body


def test_change_rejects_an_empty_opt_out_reason(capsys, _inject_fake):
    created = _seed(_inject_fake)
    assert main(["change", created.id, "--post-merge-acceptance", "   "]) == 2
    assert "reason" in capsys.readouterr().out


def test_gate_json_reports_below_minimum_with_empty_gap_arrays(capsys, _inject_fake):
    """A 1-criterion ticket, fully proven, has NOTHING in unchecked/proofless — a consumer
    (agent-tools' ship.sh) must read `below_minimum` to explain the refusal, not just the
    two gap arrays, which are both empty here."""
    created = _seed(_inject_fake, acceptance=[Criterion(text="only one")])
    main(["check", created.id, "1", "--proof", "one.png"])
    capsys.readouterr()
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 1
    assert payload["below_minimum"] is True
    assert payload["unchecked"] == [] and payload["proofless"] == []


def test_gate_prints_a_dedicated_message_for_below_minimum(capsys, _inject_fake):
    created = _seed(_inject_fake, acceptance=[Criterion(text="only one")])
    main(["check", created.id, "1", "--proof", "one.png"])
    capsys.readouterr()
    rc = main(["gate", created.id])
    out = capsys.readouterr().out
    assert rc == 1
    assert "only 1 acceptance criteria" in out
    assert "--skip-acceptance" in out


def test_gate_on_cancelled_ticket_passes_with_a_note(capsys, _inject_fake):
    created = _seed(_inject_fake, state=State.CANCELLED)
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 0
    assert payload["ok"] is True and payload["state"] == "cancelled"


def test_gate_on_done_ticket_with_unmet_criteria_still_refuses(capsys, _inject_fake):
    """The HYP-1440 shape: an integration flipped the ticket to Done with nothing accepted."""
    created = _seed(_inject_fake, state=State.DONE)
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 1 and payload["state"] == "done" and len(payload["unchecked"]) == 3


def test_gate_refuses_zero_criteria_with_a_fix_hint(capsys, _inject_fake):
    created = _seed(_inject_fake, acceptance=[])
    rc, payload = _gate_json(capsys, created.id)
    assert rc == 1 and payload["ok"] is False and payload["criteria"] == 0
    rc = main(["gate", created.id])
    out = capsys.readouterr().out
    assert rc == 1 and "no acceptance criteria" in out and "--acceptance" in out


def test_gate_unknown_id_is_a_clean_error_exit_2(capsys, _inject_fake):
    rc = main(["gate", "#999", "--json"])
    assert rc == 2
    assert "error" in capsys.readouterr().out


# ── multi-criterion `task check` ──────────────────────────────────────────────────


def test_check_many_with_one_proof_each_in_order(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "1", "3", "--proof", "a.png", "--proof", "c.png"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.acceptance[0].checked and stored.acceptance[0].proof == "a.png"
    assert not stored.acceptance[1].checked
    assert stored.acceptance[2].checked and stored.acceptance[2].proof == "c.png"
    out = capsys.readouterr().out
    assert "checked 2 criteria" in out and "1 left" in out


def test_check_all_with_a_shared_proof(_inject_fake):
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "--all", "--proof", "shot.png"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert all(c.checked and c.proof == "shot.png" for c in stored.acceptance)


def test_check_all_with_one_proof_per_criterion(_inject_fake):
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "--all", "--proof", "1.png", "--proof", "2.png", "--proof", "3.png"])
    assert rc == 0
    assert [c.proof for c in _inject_fake.get(created.id).acceptance] == ["1.png", "2.png", "3.png"]


def test_check_many_with_mismatched_proof_count_is_clean_error(capsys, _inject_fake):
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "1", "2", "3", "--proof", "a.png", "--proof", "b.png"])
    assert rc == 2
    assert "3 criteria" in capsys.readouterr().out
    assert not any(c.checked for c in _inject_fake.get(created.id).acceptance), "nothing persisted on refusal"


def test_check_many_with_force_reason_applies_to_each(_inject_fake):
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "2", "3", "--force", "no UI to shoot"])
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.acceptance[1].force_reason == "no UI to shoot"
    assert stored.acceptance[2].force_reason == "no UI to shoot"


def test_check_requires_a_selector_or_all(capsys, _inject_fake):
    created = _seed(_inject_fake)
    assert main(["check", created.id, "--proof", "a.png"]) == 2
    assert "--all" in capsys.readouterr().out


def test_check_all_and_selectors_together_is_clean_error(capsys, _inject_fake):
    created = _seed(_inject_fake)
    assert main(["check", created.id, "1", "--all", "--proof", "a.png"]) == 2


def test_check_single_selector_keeps_extra_proofs_as_screenshots(_inject_fake):
    """Pre-existing single-selector contract: the first proof backs the box, the rest are
    attached as implementation screenshots — untouched by the multi-selector feature."""
    created = _seed(_inject_fake)
    rc = main(["check", created.id, "1", "--proof", "a.png", "--proof", "b.png"])
    assert rc == 0
    assert _inject_fake.get(created.id).acceptance[0].proof == "a.png"
    assert (created.id, "b.png") in _inject_fake.attachments


# ── `task accept` — the interactive walk ─────────────────────────────────────────────


def test_accept_without_a_tty_prints_usage(capsys, monkeypatch, _inject_fake):
    created = _seed(_inject_fake)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = main(["accept", created.id])
    out = capsys.readouterr().out
    assert rc == 2
    assert "task check" in out and "--proof" in out
    assert not any(c.checked for c in _inject_fake.get(created.id).acceptance)


def test_accept_walks_the_remaining_criteria(capsys, monkeypatch, tmp_path, _inject_fake):
    created = _seed(_inject_fake)
    main(["check", created.id, "1", "--proof", "already.png"])
    shot = tmp_path / "two.png"
    shot.write_bytes(b"png")
    answers = iter([str(shot), "force: restart cannot be screenshotted"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    capsys.readouterr()
    rc = main(["accept", created.id])
    out = capsys.readouterr().out
    assert rc == 0
    stored = _inject_fake.get(created.id)
    assert stored.acceptance[1].checked and stored.acceptance[1].proof == str(shot)
    assert stored.acceptance[2].checked and stored.acceptance[2].force_reason == "restart cannot be screenshotted"
    assert "[2] handles the empty case" in out and "[3] survives a restart" in out
    assert "accepted" in out


def test_accept_skips_on_empty_answer_and_persists_each_step(capsys, monkeypatch, tmp_path, _inject_fake):
    created = _seed(_inject_fake)
    shot = tmp_path / "one.png"
    shot.write_bytes(b"png")
    answers = iter([str(shot), "", ""])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc = main(["accept", created.id])
    out = capsys.readouterr().out
    assert rc == 1, "skipped criteria leave the ticket unaccepted"
    stored = _inject_fake.get(created.id)
    assert stored.acceptance[0].checked and not stored.acceptance[1].checked
    assert "2 left" in out and "task gate" in out


def test_accept_reprompts_on_a_missing_local_file(capsys, monkeypatch, tmp_path, _inject_fake):
    created = _seed(_inject_fake, acceptance=[Criterion(text="only one"), Criterion(text="two")])
    shot = tmp_path / "real.png"
    shot.write_bytes(b"png")
    answers = iter([str(tmp_path / "typo.png"), str(shot), "https://example.com/proof.png"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc = main(["accept", created.id])
    out = capsys.readouterr().out
    assert rc == 0
    assert "typo.png" in out and "not found" in out
    stored = _inject_fake.get(created.id)
    assert stored.acceptance[0].proof == str(shot)
    assert stored.acceptance[1].proof == "https://example.com/proof.png"


def test_accept_on_fully_accepted_ticket_is_a_noop(capsys, monkeypatch, _inject_fake):
    created = _seed(_inject_fake)
    main(["check", created.id, "--all", "--proof", "shot.png"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    capsys.readouterr()
    rc = main(["accept", created.id])
    assert rc == 0
    assert "nothing left" in capsys.readouterr().out


# ── the skill text documents the pre-merge contract ─────────────────────────────────


def test_skill_text_documents_gate_accept_and_the_refs_rule():
    from tasklib.install import SKILL_MD

    for needle in ("task gate", "task accept", "task check <id> 1 2", "--post-merge-acceptance", "Refs", "Closes"):
        assert needle in SKILL_MD, needle
    # the WHY, not just the rule: the reader must learn what closes tickets behind the gates,
    # and that a merge parks the ticket in In Review — only acceptance reaches Done
    assert "magic-close" in SKILL_MD and "Linear" in SKILL_MD
    assert "In Review" in SKILL_MD and "only acceptance closes it" in SKILL_MD
