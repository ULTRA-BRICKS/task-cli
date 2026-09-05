"""audit.py — the reconciliation backstop for a Done ticket that never passed the close gates.

Root-cause context (HYP-1347, Aug 2026): task-cli's own `done`/`change --done`/`status … done`
commands already fully enforce `policy.check_done` before writing `State.DONE`. The gap is a
ticket that reaches Done WITHOUT ever calling task-cli at all — Linear's (and GitHub's) own
PR-merge auto-close writes the closed state directly to the backend. `audit_done_tickets` is the
after-the-fact check: it re-runs the same close gates against tickets already sitting in Done.
"""

from __future__ import annotations

from tasklib.audit import audit_done_tickets
from tasklib.model import Criterion, State, Ticket
from tasklib.policy import EnforceConfig


def _good_ticket(**over) -> Ticket:
    base = dict(
        title="t",
        what="the change",
        why="because",
        user_impact="Users can read the page without the menu covering the text, so they stop getting lost",
        cost_of_inaction="bad things",
        acceptance=["it works", "it also handles the empty case"],
        state=State.DONE,
    )
    base.update(over)
    return Ticket(**base)


def _checked_criteria() -> list[Criterion]:
    return [
        Criterion("it works", checked=True, proof="proof.png"),
        Criterion("it also handles the empty case", checked=True, proof="proof.png"),
    ]


def test_done_ticket_with_unchecked_criteria_is_flagged():
    # the HYP-1347 shape: Done, but every criterion still unchecked and no screenshot.
    ticket = _good_ticket()
    findings = audit_done_tickets([ticket], EnforceConfig())
    assert len(findings) == 1
    assert findings[0].ticket is ticket
    gates = {v.gate for v in findings[0].result.violations}
    assert "acceptance-checked" in gates


def test_done_ticket_with_all_criteria_checked_and_proven_is_clean():
    ticket = _good_ticket(acceptance=_checked_criteria())
    assert audit_done_tickets([ticket], EnforceConfig()) == []


def test_checked_without_proof_is_still_flagged():
    # a criterion ticked `- [x]` (e.g. hand-edited in the web UI) with no proof and no
    # force_reason is exactly what the on-done gate would have refused — the audit must
    # catch it the same way.
    ticket = _good_ticket(
        acceptance=[Criterion("it works", checked=True), Criterion("it also handles the empty case", checked=True)]
    )
    findings = audit_done_tickets([ticket], EnforceConfig())
    assert len(findings) == 1
    assert any(v.gate == "acceptance-checked" for v in findings[0].result.violations)


def test_non_done_tickets_are_never_audited():
    # a TODO/in-progress ticket makes no "this is finished" claim — nothing to audit.
    ticket = _good_ticket(state=State.TODO, acceptance=[])
    assert audit_done_tickets([ticket], EnforceConfig()) == []


def test_ui_ticket_without_implementation_screenshot_is_flagged():
    ticket = _good_ticket(acceptance=_checked_criteria(), labels=["ui"])
    findings = audit_done_tickets([ticket], EnforceConfig())
    assert len(findings) == 1
    assert any(v.gate == "screenshots" for v in findings[0].result.violations)


def test_a_ticket_closed_through_a_recorded_skip_is_not_flagged():
    # an escape hatch used AT close time is audited/justified — apply_skips already drops it
    # from `violations`, so the audit (which reuses check_done unmodified) must agree: a
    # legitimately-waived gate is not a "bypassed task-cli" finding.
    ticket = _good_ticket(
        acceptance=[
            Criterion("it works", checked=True, force_reason="no UI, backend invariant"),
            Criterion("it also handles the empty case", checked=True, force_reason="ditto"),
        ]
    )
    assert audit_done_tickets([ticket], EnforceConfig()) == []


def test_multiple_tickets_only_noncompliant_ones_are_returned():
    clean = _good_ticket(acceptance=_checked_criteria())
    dirty = _good_ticket(title="dirty")
    findings = audit_done_tickets([clean, dirty], EnforceConfig())
    assert [f.ticket for f in findings] == [dirty]
