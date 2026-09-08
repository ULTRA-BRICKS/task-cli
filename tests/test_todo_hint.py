"""todo_hint.py — the non-blocking todo-vs-ticket heuristic.

Real examples in the fixtures below are drawn from an ecosystem-wide audit of misfiled
tickets (task-cli intake, 2026-09): titles/bodies that were genuinely session-local process
steps filed as durable tickets.
"""

from __future__ import annotations

from tasklib.todo_hint import assess_todo_shaped


def test_real_misfiled_title_is_flagged():
    # dev-cli#21, the ticket that triggered this whole investigation.
    problems = assess_todo_shaped("Adversarial review of uncommitted safe-cleanup diff", "")
    assert problems
    assert "process step" in problems[0]


def test_review_my_diff_phrasing_is_flagged():
    problems = assess_todo_shaped(
        "Fix the login bug",
        "Please review my uncommitted diff before I commit it, it touches the auth flow.",
    )
    assert problems
    assert "process step" in problems[0]


def test_before_committing_phrasing_is_flagged():
    problems = assess_todo_shaped(
        "Check the rules.yaml change",
        "Verify this is safe before committing.",
    )
    assert problems


def test_empty_what_with_review_diff_title_is_flagged():
    # dev-cli#7/#11/#13/#14: boilerplate title, empty What section.
    problems = assess_todo_shaped("Review uncommitted cleanup command diff", "")
    assert problems
    assert "What section is empty" in problems[0]


def test_review_diff_title_with_real_content_is_not_flagged():
    # A "review"-shaped title is fine once the What section actually says something concrete —
    # not every ticket with the word "review" in it is a process step.
    problems = assess_todo_shaped(
        "Review pagination logic for off-by-one errors",
        "The list view's pagination component drops the last row on page boundaries divisible "
        "by the page size; this is a durable bug affecting every paginated list in the app.",
    )
    assert problems == []


def test_ordinary_feature_ticket_is_not_flagged():
    problems = assess_todo_shaped(
        "Add dark mode toggle to settings",
        "Users on OLED screens want a dark theme option; currently there is no way to switch "
        "away from the bright default theme, which is uncomfortable at night.",
    )
    assert problems == []


def test_short_what_without_review_title_is_not_flagged():
    # a thin What section alone is the EXISTING motivation/user-impact gates' job, not this one —
    # this gate only fires the empty-what path when the TITLE also smells like a review step.
    problems = assess_todo_shaped("Add dark mode toggle", "short")
    assert problems == []
