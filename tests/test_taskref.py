"""Repo-aware task-code validation — invented/typo codes fail closed (task-cli#173)."""

from __future__ import annotations

import pytest

from tasklib.backends import BackendError
from tasklib.backends.github_issues import GitHubIssuesBackend
from tasklib.backends.linear import LinearBackend
from tasklib.taskref import (
    TaskRefError,
    github_issue_number,
    linear_identifier,
    validate,
)


INVENTED = (
    "TEST-1",
    "CODE",
    "RIG-CLI-298-VERSION-BUMP",
    "ambiguous-create-dedup",
    "HYP-123",
)


@pytest.fixture(autouse=True)
def _isolate_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))


@pytest.mark.parametrize("code", INVENTED)
def test_github_rejects_invented_placeholders(code):
    result = validate(code)
    assert not result.ok
    assert not result
    reason = result.reason.lower()
    assert "github" in reason or "#n" in reason or "owner/repo" in reason
    assert "task find" in reason
    assert "task new" in reason


def test_github_bare_and_hash_number_ok():
    hashed = validate("#12")
    assert hashed.ok
    assert hashed
    assert hashed.canonical == "12"
    bare = validate("12")
    assert bare.ok
    assert bare.canonical == "12"


def test_github_cross_repo_form_library_ok():
    result = validate("owner/repo#12")
    assert result.ok
    assert result.canonical == "owner/repo#12"


def test_linear_accepts_configured_team_prefix():
    result = validate("HYP-9", backend="linear", linear_team="HYP")
    assert result.ok
    assert result.canonical == "HYP-9"
    assert result.backend == "linear"


def test_linear_rejects_mismatched_prefix_naming_team():
    result = validate("TEST-1", backend="linear", linear_team="HYP")
    assert not result.ok
    assert "HYP" in result.reason
    assert "task find" in result.reason.lower()
    assert "task new" in result.reason.lower()


def test_linear_rejects_github_hash_number():
    result = validate("#12", backend="linear", linear_team="HYP")
    assert not result.ok


def test_linear_empty_team_not_ok():
    result = validate("HYP-9", backend="linear", linear_team="")
    assert not result.ok


def test_validate_reads_task_yaml_linear(tmp_path):
    (tmp_path / "task.yaml").write_text(
        "version: 1\nbackend: linear\nlinear: {team: HYP}\n",
        encoding="utf-8",
    )
    ok = validate("HYP-9", tmp_path)
    assert ok.ok
    assert ok.backend == "linear"
    bad = validate("TEST-1", tmp_path)
    assert not bad.ok
    assert "HYP" in bad.reason


def test_validate_zero_config_github_default(tmp_path):
    result = validate("TEST-1", tmp_path)
    assert not result.ok
    assert result.backend == "github-issues"


def test_validate_reads_rig_yaml_task_block(tmp_path):
    (tmp_path / "rig.yaml").write_text(
        "version: 1\ntask:\n  backend: linear\n  team: HYP\n",
        encoding="utf-8",
    )
    result = validate("HYP-9", tmp_path)
    assert result.ok
    assert result.backend == "linear"


def test_github_issue_number_and_linear_identifier_raise_on_junk():
    with pytest.raises(TaskRefError):
        github_issue_number("TEST-1")
    with pytest.raises(TaskRefError):
        github_issue_number("owner/repo#12")
    with pytest.raises(TaskRefError):
        github_issue_number("##1")
    assert github_issue_number("#12") == "12"
    assert github_issue_number("12") == "12"

    with pytest.raises(TaskRefError):
        linear_identifier("TEST-1", "HYP")
    with pytest.raises(TaskRefError):
        linear_identifier("#12", "HYP")
    with pytest.raises(TaskRefError):
        linear_identifier("HYP-9", "")
    assert linear_identifier("hyp-9", "HYP") == "HYP-9"


def test_github_get_rejects_invented_code_without_http():
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")

    def boom(*_a, **_k):
        raise AssertionError("must not call _call")

    be._call = boom
    with pytest.raises(BackendError):
        be.get("TEST-1")


def test_github_spaced_hash_does_not_collapse_onto_issue_one():
    # Codex P1: "# 1" / "#\t1" used to strip to "1" and PATCH the real issue.
    for code in ("# 1", "#\t1"):
        with pytest.raises(TaskRefError):
            github_issue_number(code)
        assert not validate(code, backend="github-issues")

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")

    def boom(*_a, **_k):
        raise AssertionError("must not call _call")

    be._call = boom
    for code in ("# 1", "#\t1"):
        with pytest.raises(BackendError):
            be.get(code)


def test_linear_get_rejects_invented_code_without_http():
    be = LinearBackend(api_key="k", team_key="HYP")

    def boom(*_a, **_k):
        raise AssertionError("must not call _gql")

    be._gql = boom
    with pytest.raises(BackendError):
        be.get("TEST-1")
