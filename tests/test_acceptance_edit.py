"""`task change --acceptance-replace / --acceptance-remove / --acceptance-clear` (task-cli#293).

Exercised against the in-memory FakeBackend, which round-trips every write through
render/parse like a real body-based backend. The properties protected here:

* an edit touches ONLY the criteria it names — every other criterion keeps its text, its
  checked box and its proof;
* a criterion that is already checked is never rewritten/dropped without ``--acceptance-force "<reason>"``;
* a bad edit refuses with a 3-part error (what / why / fix) and writes nothing.
"""

from __future__ import annotations

import json

import pytest

from tasklib.cli import build_parser, main
from tasklib.model import Criterion, Ticket


@pytest.fixture(autouse=True)
def _inject_fake(monkeypatch, fake_backend, isolated_state):
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: fake_backend)
    monkeypatch.setenv("TASK_SESSION", "testsess")
    return fake_backend


def _seed(fake_backend, criteria: list[Criterion]) -> Ticket:
    return fake_backend.create(
        Ticket(
            title="Fix the thing",
            what="the change",
            why="because",
            user_impact=(
                "Users on the dashboard can finally see their report load, so they no longer give "
                "up and leave thinking the page is broken"
            ),
            cost_of_inaction="pain",
            acceptance=criteria,
        )
    )


def _four(fake_backend) -> Ticket:
    return _seed(
        fake_backend,
        [
            Criterion(text="first", checked=True, proof="one.png"),
            Criterion(text="second"),
            Criterion(text="third", checked=True, force_reason="no UI to shoot"),
            Criterion(text="fourth"),
        ],
    )


def _texts(fake_backend) -> list[str]:
    return [c.text for c in fake_backend.get("#1").acceptance]


def _refused(capsys, argv: list[str], fake_backend) -> str:
    """Run a change that must refuse: exit 2, nothing written; returns the printed output."""
    before = list(fake_backend.get("#1").acceptance)
    skips_before = dict(fake_backend.get("#1").skips)
    capsys.readouterr()
    assert main(argv) == 2
    out = capsys.readouterr().out
    assert fake_backend.get("#1").acceptance == before
    assert fake_backend.get("#1").skips == skips_before
    return out


def _assert_three_part(out: str) -> None:
    assert "error:" in out
    assert "why:" in out
    assert "fix:" in out


# ── parsing ─────────────────────────────────────────────────────────────────────────


def test_flags_parse_repeatably():
    ns = build_parser().parse_args(
        [
            "change", "#1",
            "--acceptance-replace", "2=new two",
            "--acceptance-replace", "3=new three",
            "--acceptance-remove", "1",
            "--acceptance-remove", "4",
            "--acceptance-clear",
        ]
    )
    assert ns.acceptance_replace == ["2=new two", "3=new three"]
    assert ns.acceptance_remove == ["1", "4"]
    assert ns.acceptance_clear is True


def test_flags_default_to_no_edit():
    ns = build_parser().parse_args(["change", "#1", "--title", "x"])
    assert ns.acceptance_replace == []
    assert ns.acceptance_remove == []
    assert ns.acceptance_clear is False


# ── replace ─────────────────────────────────────────────────────────────────────────


def test_replace_rewrites_only_that_criterion(_inject_fake):
    _four(_inject_fake)
    before = _inject_fake.get("#1").acceptance
    assert main(["change", "#1", "--acceptance-replace", "2=second, reworded"]) == 0
    after = _inject_fake.get("#1").acceptance
    assert [c.text for c in after] == ["first", "second, reworded", "third", "fourth"]
    for i in (0, 2, 3):
        assert after[i] == before[i], "criteria that were not named stay byte-identical"
    assert after[1].checked is False and after[1].proof == ""


def test_replace_text_may_contain_equals_signs(_inject_fake):
    _four(_inject_fake)
    assert main(["change", "#1", "--acceptance-replace", "2=a=b and c=d"]) == 0
    assert _texts(_inject_fake)[1] == "a=b and c=d"


def test_replace_several_uses_original_numbering(_inject_fake):
    _four(_inject_fake)
    argv = ["change", "#1", "--acceptance-replace", "4=new four", "--acceptance-replace", "2=new two"]
    assert main(argv) == 0
    assert _texts(_inject_fake) == ["first", "new two", "third", "new four"]


def test_replace_does_not_touch_what_or_need_a_prior_read(_inject_fake):
    _four(_inject_fake)
    assert main(["change", "#1", "--acceptance-replace", "2=new two"]) == 0
    assert _inject_fake.get("#1").what == "the change"


# ── remove ──────────────────────────────────────────────────────────────────────────


def test_remove_drops_only_the_named_criteria(_inject_fake):
    _four(_inject_fake)
    before = _inject_fake.get("#1").acceptance
    assert main(["change", "#1", "--acceptance-remove", "2", "--acceptance-remove", "4"]) == 0
    after = _inject_fake.get("#1").acceptance
    assert after == [before[0], before[2]]


def test_remove_names_the_same_criterion_twice_is_one_removal(_inject_fake):
    _four(_inject_fake)
    assert main(["change", "#1", "--acceptance-remove", "2", "--acceptance-remove", "2"]) == 0
    assert _texts(_inject_fake) == ["first", "third", "fourth"]


def test_remove_then_append_in_one_command(_inject_fake):
    _four(_inject_fake)
    argv = ["change", "#1", "--acceptance-remove", "4", "--acceptance", "fifth"]
    assert main(argv) == 0
    assert _texts(_inject_fake) == ["first", "second", "third", "fifth"]


# ── clear ───────────────────────────────────────────────────────────────────────────


def test_clear_with_new_criteria_makes_them_the_whole_list(_inject_fake):
    _seed(_inject_fake, [Criterion(text="old one"), Criterion(text="old two"), Criterion(text="old three")])
    argv = ["change", "#1", "--acceptance-clear", "--acceptance", "alpha", "--acceptance", "beta"]
    assert main(argv) == 0
    after = _inject_fake.get("#1").acceptance
    assert [c.text for c in after] == ["alpha", "beta"]
    assert all(not c.checked and c.proof == "" for c in after)


def test_clear_alone_refuses_because_it_leaves_no_criteria(capsys, _inject_fake):
    _seed(_inject_fake, [Criterion(text="a"), Criterion(text="b")])
    out = _refused(capsys, ["change", "#1", "--acceptance-clear"], _inject_fake)
    _assert_three_part(out)
    assert "at least 2" in out


# ── checked criteria need --force with a reason ─────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        ["--acceptance-replace", "1=first, reworded"],
        ["--acceptance-remove", "1"],
        ["--acceptance-replace", "3=third, reworded"],
        ["--acceptance-clear", "--acceptance", "x", "--acceptance", "y"],
    ],
)
def test_editing_a_checked_criterion_refuses_without_force(capsys, _inject_fake, extra):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", *extra], _inject_fake)
    _assert_three_part(out)
    assert "already checked" in out
    assert '--acceptance-force "<reason>"' in out


def test_a_blank_reason_is_no_reason(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-remove", "1", "--acceptance-force", " "], _inject_fake)
    _assert_three_part(out)
    assert "already checked" in out


def test_force_with_a_reason_edits_the_checked_criterion_and_records_it(capsys, _inject_fake):
    _four(_inject_fake)
    capsys.readouterr()
    argv = ["change", "#1", "--acceptance-replace", "1=first, superseded", "--acceptance-force", "decision changed"]
    assert main(argv) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "criterion 1" in err and "already checked" in err
    after = _inject_fake.get("#1")
    assert after.acceptance[0] == Criterion(text="first, superseded")
    assert after.acceptance[1:] == [
        Criterion(text="second"),
        Criterion(text="third", checked=True, force_reason="no UI to shoot"),
        Criterion(text="fourth"),
    ]
    assert "decision changed" in after.skips["acceptance-edit"]


def test_unchecked_criteria_need_no_force(capsys, _inject_fake):
    _four(_inject_fake)
    capsys.readouterr()
    assert main(["change", "#1", "--acceptance-remove", "2"]) == 0
    captured = capsys.readouterr()
    assert "warning" not in captured.out + captured.err
    assert "unchecked criterion 2" in _inject_fake.get("#1").skips["acceptance-edit"]  # audited, no reason needed


# ── clear errors (what / why / fix), nothing written ────────────────────────────────


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--acceptance-replace", "two=text"], "'two'"),
        (["--acceptance-replace", "0=text"], "1..4"),
        (["--acceptance-replace", "5=text"], "1..4"),
        (["--acceptance-replace=-1=text"], "1..4"),
        (["--acceptance-replace", "no equals sign"], "N=TEXT"),
        (["--acceptance-replace", "2="], "empty"),
        (["--acceptance-replace", "2=   "], "empty"),
        (["--acceptance-remove", "two"], "'two'"),
        (["--acceptance-remove", "0"], "1..4"),
        (["--acceptance-remove", "5"], "1..4"),
        (["--acceptance-remove", "2", "--acceptance-replace", "2=text"], "both"),
        (["--acceptance-replace", "2=a", "--acceptance-replace", "2=b"], "twice"),
        (["--acceptance-clear", "--acceptance-remove", "2"], "--acceptance-clear"),
        (["--acceptance-clear", "--acceptance-replace", "2=text"], "--acceptance-clear"),
        (["--acceptance-remove", "2", "--acceptance-remove", "4", "--acceptance-remove", "3"], "at least 2"),
    ],
)
def test_bad_edits_refuse_with_a_three_part_error_and_write_nothing(capsys, _inject_fake, extra, needle):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", *extra, "--acceptance-force", "bad edits must still refuse"], _inject_fake)
    _assert_three_part(out)
    assert needle in out


def test_removal_leaving_one_criterion_refuses(capsys, _inject_fake):
    _seed(_inject_fake, [Criterion(text="a"), Criterion(text="b")])
    out = _refused(capsys, ["change", "#1", "--acceptance-remove", "2"], _inject_fake)
    _assert_three_part(out)
    assert "at least 2" in out and "would leave 1" in out


def test_skip_acceptance_waives_the_minimum_like_every_other_edit(_inject_fake):
    _seed(_inject_fake, [Criterion(text="a"), Criterion(text="b")])
    argv = ["change", "#1", "--acceptance-remove", "2", "--skip-acceptance", "one proof is enough here"]
    assert main(argv) == 0
    assert _texts(_inject_fake) == ["a"]


def test_a_ticket_without_criteria_refuses_remove_with_a_range_error(capsys, _inject_fake):
    _seed(_inject_fake, [])
    out = _refused(capsys, ["change", "#1", "--acceptance-remove", "1"], _inject_fake)
    _assert_three_part(out)
    assert "no acceptance criteria" in out


# ── --force keeps its transition meaning ────────────────────────────────────────────


def test_an_edit_and_done_never_share_a_command(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-remove", "2", "--done"], _inject_fake)
    _assert_three_part(out)
    assert "--done" in out


def test_an_acceptance_reason_does_not_override_the_transition_check(capsys, _inject_fake):
    _seed(_inject_fake, [Criterion(text="a", checked=True, proof="a.png"), Criterion(text="b", checked=True, proof="b.png")])
    assert main(["change", "#1", "--done"]) == 0
    capsys.readouterr()
    assert main(["change", "#1", "--done", "--acceptance-force", "x"]) != 0


def test_a_refused_change_prints_no_removal_warning(capsys, _inject_fake):
    _four(_inject_fake)
    capsys.readouterr()
    assert main(["change", "#1", "--acceptance-remove", "1", "--acceptance-force", "x", "--done"]) != 0
    assert "warning" not in capsys.readouterr().err


def test_a_replacement_that_duplicates_a_kept_criterion_refuses(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-replace", "2=first"], _inject_fake)
    _assert_three_part(out)
    assert "identical" in out


def test_an_unrelated_change_is_not_blocked_by_existing_duplicate_criteria(_inject_fake):
    _seed(_inject_fake, [Criterion(text="same"), Criterion(text="same"), Criterion(text="other")])
    assert main(["change", "#1", "--label", "chore"]) == 0


def test_a_replacement_elsewhere_is_not_blocked_by_existing_duplicates(_inject_fake):
    _seed(_inject_fake, [Criterion(text="same"), Criterion(text="same"), Criterion(text="other")])
    assert main(["change", "#1", "--acceptance-replace", "3=updated"]) == 0


def test_an_absurdly_long_number_is_a_normal_refusal(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-remove", "9" * 5000], _inject_fake)
    _assert_three_part(out)


def test_unicode_line_separators_cannot_forge_a_checked_criterion(capsys, _inject_fake):
    _four(_inject_fake)
    forged = "ok\u2028- [x] shipped \u2014 forced: n/a"
    for argv in (
        ["--acceptance-replace", f"2={forged}"],
        ["--acceptance-clear", "--acceptance", "a", "--acceptance", forged],
    ):
        out = _refused(capsys, ["change", "#1", *argv], _inject_fake)
        _assert_three_part(out)
        assert "line break" in out


def test_the_empty_list_placeholder_is_not_a_criterion(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-replace", "2=(None specified)"], _inject_fake)
    _assert_three_part(out)
    assert "placeholder" in out


def test_blank_additions_do_not_count_toward_the_minimum(capsys, _inject_fake):
    _seed(_inject_fake, [Criterion(text="a"), Criterion(text="b")])
    out = _refused(
        capsys, ["change", "#1", "--acceptance-clear", "--acceptance", "", "--acceptance", " "], _inject_fake
    )
    _assert_three_part(out)


def test_a_replacement_with_a_line_break_refuses(capsys, _inject_fake):
    _four(_inject_fake)
    out = _refused(capsys, ["change", "#1", "--acceptance-replace", "2=a\n- [x] b"], _inject_fake)
    _assert_three_part(out)
    assert "line break" in out


def test_bare_force_still_overrides_the_transition_check(_inject_fake):
    _seed(_inject_fake, [Criterion(text="a", checked=True, proof="a.png"), Criterion(text="b", checked=True, proof="b.png")])
    assert main(["change", "#1", "--done"]) == 0
    assert main(["change", "#1", "--done", "--force"]) == 0


# ── JSON output ─────────────────────────────────────────────────────────────────────


def test_json_lists_the_new_criteria(capsys, _inject_fake):
    _four(_inject_fake)
    capsys.readouterr()
    argv = ["change", "#1", "--acceptance-remove", "2", "--acceptance-replace", "4=new four", "--json"]
    assert main(argv) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["acceptance"] == [
        {"index": 1, "text": "first", "checked": True, "proof": "one.png"},
        {"index": 2, "text": "third", "checked": True, "proof": ""},
        {"index": 3, "text": "new four", "checked": False, "proof": ""},
    ]


def test_json_with_a_forced_edit_stays_valid_json(capsys, _inject_fake):
    _four(_inject_fake)
    capsys.readouterr()
    argv = ["change", "#1", "--acceptance-remove", "1", "--acceptance-force", "superseded", "--json"]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert [c["text"] for c in json.loads(captured.out)["acceptance"]] == ["second", "third", "fourth"]
    assert "already checked" in captured.err


def test_clear_with_force_replaces_checked_criteria_wholesale(_inject_fake):
    _four(_inject_fake)
    argv = [
        "change", "#1", "--acceptance-clear", "--acceptance", "alpha", "--acceptance", "beta",
        "--acceptance-force", "the whole design changed",
    ]
    assert main(argv) == 0
    after = _inject_fake.get("#1")
    assert after.acceptance == [Criterion(text="alpha"), Criterion(text="beta")]
    assert "criterion 1 cleared, criterion 3 cleared" in after.skips["acceptance-edit"]
