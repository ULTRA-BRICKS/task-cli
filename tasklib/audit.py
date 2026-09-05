"""Reconciliation audit — catches a Done ticket that never passed task-cli's close gates.

WHAT THIS IS
    task-cli's own close paths (``done`` / ``change --done`` / ``status <id> done``) run the
    full ``policy.check_done`` gate set — acceptance count, every criterion checked WITH a
    proof, screenshots for UI tickets, etc. — before ever writing ``State.DONE``. But a ticket
    can also *reach* Done without ever calling task-cli: Linear's own GitHub-PR-link automation
    (and GitHub's native "Closes #123" issue auto-close) writes the closed state straight to the
    backend the moment a linked PR merges. task-cli is never invoked in that path, so none of
    its gates ever run. HYP-1347 (Aug 2026) is the concrete incident this module answers: closed
    via Linear's own PR-merge automation with all 4 acceptance criteria still unchecked and no
    screenshot attached, because `task done` was simply never called — the ticket went straight
    from "PR merged" to "Done" inside Linear's servers.

    :func:`audit_done_tickets` is the backstop: it re-runs the SAME ``policy.check_done`` gates
    against tickets already sitting in Done, after the fact. A ticket closed THROUGH task-cli
    never fails this re-run: it either passed the gates outright, or recorded an explicit,
    audited ``--skip-<gate>`` that ``apply_skips`` honors, so a legitimately-waived gate never
    reappears here as a violation.

    IMPORTANT CAVEAT — a finding here is NOT proof of how the ticket reached Done. It only
    means the ticket does not satisfy TODAY's gates. The most likely cause is an out-of-band
    close (the whole reason this module exists), but a second, real cause is policy drift: this
    repo's own convention is "if a gate is wrong, change ``policy.py`` + add a test" (AGENTS.md),
    and ``EnforceConfig`` can change over time (a stricter ``acceptance_min``, a newly-required
    ``user_impact_quality`` rule, …). Re-running TODAY's config against a ticket closed under
    YESTERDAY's config can retroactively flag a ticket that legitimately passed `task done` at
    the time. Callers (`cli.cmd_audit`) must not treat a finding as certain proof of bypass —
    report it, but a human should look before ``--revert`` reopens it unattended (see README's
    Audit section).

HOW IT'S REACHED
    ``task audit`` (``cli.cmd_audit``). Meant to run periodically (cron / CronCreate), not only
    on demand — a Done ticket can go noncompliant at any time an external integration closes it,
    not only right after a `task` invocation, so there is no single "hook" this can attach to
    the way the create/close gates attach to a command. Pure module: no I/O, no backend calls —
    the caller (``cli.py``) fetches the tickets and threads through the same
    ``resolvable_ids`` hint ``_enforce_or_die`` uses, keeping "policy is pure" intact.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import State, Ticket
from .policy import EnforceConfig, PolicyResult, check_done


@dataclass
class AuditFinding:
    """One Done ticket that fails the close gates TODAY — see the module docstring's caveat
    above: this is not proof it never legitimately passed them (policy can drift)."""

    ticket: Ticket
    result: PolicyResult


def audit_done_tickets(
    tickets: list[Ticket],
    cfg: EnforceConfig,
    resolvable_ids: set[int] | None = None,
) -> list[AuditFinding]:
    """Re-run the close gates against every DONE ticket in ``tickets``; return the failures.

    A non-DONE ticket is skipped outright — the close gates describe what closing REQUIRES, and
    a ticket not yet claiming to be finished makes no such claim to audit. Order is preserved
    from the input (callers that fetch "most recently updated first" get findings in that order).
    """
    findings: list[AuditFinding] = []
    for ticket in tickets:
        if ticket.state is not State.DONE:
            continue
        result = check_done(ticket, cfg, resolvable_ids=resolvable_ids)
        if not result.ok:
            findings.append(AuditFinding(ticket=ticket, result=result))
    return findings
