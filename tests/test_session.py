"""session.py — detection precedence + the sidecar index."""

from __future__ import annotations

from tasklib.session import detect, read_entries, read_ids, record, sidecar_path


def test_env_task_session_wins():
    s = detect(env={"TASK_SESSION": "my-feature", "TMUX_PANE": "%3"}, git_branch=lambda: "main")
    assert s.id == "my-feature"
    assert s.source == "env:TASK_SESSION"
    assert s.label == "session:my-feature"


def test_tmux_pane_used_when_no_env():
    s = detect(env={"TMUX_PANE": "%7"}, git_branch=lambda: "main")
    assert s.source == "tmux-pane"
    assert s.id == "7"  # slugged ('%' stripped)


def test_git_branch_fallback():
    s = detect(env={}, git_branch=lambda: "HYP-123-add-toggle")
    assert s.source == "git-branch"
    assert s.id == "hyp-123-add-toggle"


def test_default_when_nothing_resolves():
    s = detect(env={}, git_branch=lambda: None)
    assert s.id == "default"
    assert s.source == "none"


def test_long_value_is_hashed_to_bounded_id():
    huge = "x" * 200
    s = detect(env={"TASK_SESSION": huge})
    assert len(s.id) <= 40


def test_detect_order_can_be_reordered():
    # put git-branch first → it wins over env
    s = detect(
        env={"TASK_SESSION": "envid"},
        detect_order=("git-branch", "env:TASK_SESSION"),
        git_branch=lambda: "branchid",
    )
    assert s.id == "branchid"


def test_sidecar_record_and_read(isolated_state):
    record("sess1", "#10", "first")
    record("sess1", "#11", "second")
    assert read_ids("sess1") == ["#10", "#11"]


def test_sidecar_dedups_on_id(isolated_state):
    record("sess1", "#10", "first")
    record("sess1", "#10", "first again")
    assert read_ids("sess1") == ["#10"]


def test_sidecar_path_under_state_home(isolated_state):
    p = sidecar_path("sess1")
    assert "task-cli/sessions/sess1.jsonl" in str(p)


def test_read_ids_empty_for_unknown_session(isolated_state):
    assert read_ids("never-seen") == []


def test_label_prefix_is_configurable():
    s = detect(env={"TASK_SESSION": "feat"}, label_prefix="sess/")
    assert s.label == "sess/feat"


def test_record_defaults_repo_to_empty_string(isolated_state):
    # legacy call shape (no repo arg) — task-cli#83's repo field must not become mandatory.
    record("sess1", "#10", "first")
    entries = read_entries("sess1")
    assert entries[0].repo == ""


def test_record_persists_repo_coordinate(isolated_state):
    record("sess1", "#10", "first", repo="github-issues:acme/widgets")
    entries = read_entries("sess1")
    assert entries[0].repo == "github-issues:acme/widgets"


def test_read_entries_defaults_repo_for_legacy_lines_missing_the_field(isolated_state):
    # a sidecar line written before task-cli#83's fix has no "repo" key at all.
    import json

    path = sidecar_path("sess1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "#10", "title": "legacy", "ts": 1}) + "\n", encoding="utf-8")
    entries = read_entries("sess1")
    assert entries[0].repo == ""


def test_record_keeps_env_as_the_fourth_positional_argument(tmp_path):
    # review finding: `repo` must NOT shift `env` out of its existing positional slot — an
    # existing caller doing record(session_id, id, title, env_dict) must still land in `env`,
    # not silently serialize the dict into `repo` and write to the real/default state dir.
    env = {"XDG_STATE_HOME": str(tmp_path)}
    record("sess1", "#10", "first", env)
    entries = read_entries("sess1", env)
    assert len(entries) == 1
    assert entries[0].repo == ""  # repo was never passed — must default, not receive `env`
    assert sidecar_path("sess1", env).is_file()


def test_read_entries_keeps_same_id_from_different_repos(isolated_state):
    # task-cli#83 review finding: two repos commonly both have a ticket "#1". Deduping only by
    # id would let repo B's entry silently evict repo A's from the sidecar read entirely —
    # actual data loss, not just a display leak.
    record("sess1", "#1", "repo A's first ticket", repo="github-issues:acme/a")
    record("sess1", "#1", "repo B's first ticket", repo="github-issues:acme/b")
    entries = read_entries("sess1")
    assert len(entries) == 2
    titles = {e.repo: e.title for e in entries}
    assert titles["github-issues:acme/a"] == "repo A's first ticket"
    assert titles["github-issues:acme/b"] == "repo B's first ticket"


def test_read_entries_still_dedups_same_repo_same_id_touch(isolated_state):
    # re-touching the SAME (repo, id) pair (e.g. change/done in the same session) must still
    # collapse to one entry, moved to the end — the pre-task-cli#83 behavior, unchanged.
    record("sess1", "#10", "first", repo="github-issues:acme/a")
    record("sess1", "#10", "first again", repo="github-issues:acme/a")
    entries = read_entries("sess1")
    assert len(entries) == 1
    assert entries[0].title == "first again"


def test_read_ids_shows_an_id_once_per_distinct_repo(isolated_state):
    record("sess1", "#1", "repo A's first ticket", repo="github-issues:acme/a")
    record("sess1", "#1", "repo B's first ticket", repo="github-issues:acme/b")
    assert read_ids("sess1") == ["#1", "#1"]  # two distinct tickets, both surfaced


def test_read_ids_still_collapses_the_same_repo_same_id_touch(isolated_state):
    record("sess1", "#10", "first")
    record("sess1", "#10", "first again")
    assert read_ids("sess1") == ["#10"]


def test_git_branch_detect_uses_cwd(monkeypatch):
    # the injected branch resolver wins, but verify a real call would target the given cwd:
    # detect() builds a closure over cwd; assert it is threaded through (no crash, right id).
    captured = {}

    def fake_branch():
        captured["called"] = True
        return "scoped-branch"

    s = detect(env={}, git_branch=fake_branch, cwd="/some/repo")
    assert s.id == "scoped-branch"
    assert captured["called"]
