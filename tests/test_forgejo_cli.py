"""CLI seams for UB-25: explicit non-repository ``-C`` refusal, ``task comment``, and Forgejo
routing through the cwd / ``projects:`` registry. Hermetic — ``get_backend`` is replaced."""

from __future__ import annotations

import subprocess

import pytest

from tasklib.cli import main
from tasklib.model import State, Ticket


class _Recorder:
    """A stand-in for ``tasklib.backends.get_backend`` that records every construction."""

    def __init__(self, backend=None):
        self.configs = []
        self.backend = backend

    def __call__(self, cfg, env=None):
        self.configs.append(cfg)
        if self.backend is None:
            raise AssertionError("get_backend must not be reached")
        return self.backend


class _Commentable:
    name = "forgejo"

    def __init__(self):
        self.comments = []

    def get(self, tid):
        return Ticket(title="t", state=State.TODO, id=tid, url="u")

    def comment(self, tid, body):
        self.comments.append((tid, body))


def _global_config(tmp_path, monkeypatch, body):
    cfg_home = tmp_path / "xdg"
    (cfg_home / "task-cli").mkdir(parents=True)
    (cfg_home / "task-cli" / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


_REGISTRY = (
    "version: 1\nbackend: github-issues\nprojects:\n"
    "  - {name: web, backend: forgejo, forgejo: {url: 'https://forge.example.test', repo: acme/web}}\n"
)


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "#1", "hi"],
        ["status", "#1", "done"],
        ["change", "#1", "--title", "x"],
        ["done", "#1"],
        ["attach", "#1", "/tmp/x.png"],
        ["create", "--title", "t", "--why", "w", "--impact", "i", "--if-not-done", "c",
         "--acceptance", "a"],
    ],
)
def test_explicit_non_repo_cwd_is_refused_before_any_backend(argv, tmp_path, monkeypatch, capsys):
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    rec = _Recorder()
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    rc = main([*argv, "-C", str(not_a_repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "not inside a git repository" in out
    assert rec.configs == []


def test_explicit_missing_cwd_is_refused(tmp_path, monkeypatch, capsys):
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder())
    rc = main(["comment", "#1", "hi", "-C", str(tmp_path / "missing")])
    assert rc == 2
    assert "not inside a git repository" in capsys.readouterr().out


def test_read_only_registry_commands_keep_working_with_explicit_non_repo_cwd(
    tmp_path, monkeypatch, capsys
):
    # `read` outside a repo is an intentional registry lookup — the refusal is for writes only.
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    rc = main(["read", "#4", "-C", str(tmp_path)])
    assert rc == 0
    (cfg,) = rec.configs
    assert cfg.backend == "forgejo" and cfg.section("forgejo")["repo"] == "acme/web"


def test_comment_from_implicit_cwd_outside_repo_routes_via_registry(tmp_path, monkeypatch, capsys):
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    fake = _Commentable()
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder(fake))
    monkeypatch.chdir(tmp_path)
    rc = main(["comment", "#4", "shipped in", "abc123"])
    assert rc == 0
    assert fake.comments == [("#4", "shipped in abc123")]


def _forgejo_repo(tmp_path, remote="https://forge.example.test/acme/web.git"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    (repo / "task.yaml").write_text(
        "version: 1\nbackend: forgejo\nforgejo: {url: 'https://forge.example.test'}\n",
        encoding="utf-8",
    )
    return repo


def test_comment_in_repo_cwd_needs_no_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = _forgejo_repo(tmp_path)
    fake = _Commentable()
    rec = _Recorder(fake)
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    monkeypatch.chdir(repo)
    rc = main(["comment", "3", "looks good"])
    assert rc == 0
    assert fake.comments == [("3", "looks good")]
    assert rec.configs[0].backend == "forgejo"


def test_comment_with_empty_body_is_refused_before_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = _forgejo_repo(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    rc = main(["comment", "3", "   ", "-C", str(repo)])
    assert rc == 2
    assert "empty" in capsys.readouterr().out
    assert rec.configs == []


def test_comment_body_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = _forgejo_repo(tmp_path)
    fake = _Commentable()
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder(fake))
    note = tmp_path / "note.md"
    note.write_text("## Result\n\n- ok\n", encoding="utf-8")
    rc = main(["comment", "#3", "--body-file", str(note), "-C", str(repo)])
    assert rc == 0
    assert fake.comments == [("#3", "## Result\n\n- ok\n")]


def test_bare_number_outside_repo_with_github_and_forgejo_projects_is_ambiguous(
    tmp_path, monkeypatch, capsys
):
    _global_config(
        tmp_path,
        monkeypatch,
        _REGISTRY + "  - {repo: acme/gh}\n",
    )
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder())
    rc = main(["read", "#4", "-C", str(tmp_path)])
    assert rc == 2
    assert "cannot resolve which project" in capsys.readouterr().out


def test_repo_qualified_id_outside_repo_selects_the_matching_forgejo_project(
    tmp_path, monkeypatch
):
    _global_config(
        tmp_path,
        monkeypatch,
        _REGISTRY
        + "  - {name: api, backend: forgejo, forgejo: {url: 'https://forge.example.test', repo: acme/api}}\n",
    )
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    assert main(["read", "acme/api#4", "-C", str(tmp_path)]) == 0
    assert rec.configs[0].section("forgejo")["repo"] == "acme/api"


def test_github_default_with_forgejo_origin_suggests_the_forgejo_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://git.hyperide.ai/ultrabricks/x.git"], check=True)
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder())
    rc = main(["list", "-C", str(repo)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "backend: forgejo" in out


def test_comment_body_from_stdin(tmp_path, monkeypatch):
    import io

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = _forgejo_repo(tmp_path)
    fake = _Commentable()
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder(fake))
    monkeypatch.setattr("sys.stdin", io.StringIO("from *stdin*\n"))
    assert main(["comment", "#3", "--body-file", "-", "-C", str(repo)]) == 0
    assert fake.comments == [("#3", "from *stdin*\n")]


def test_comment_ambiguous_outcome_exits_9_and_warns(tmp_path, monkeypatch, capsys):
    from tasklib.backends import EXIT_AMBIGUOUS, AmbiguousBackendError

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    repo = _forgejo_repo(tmp_path)

    class _Dropped(_Commentable):
        def comment(self, tid, body):
            raise AmbiguousBackendError("connection dropped after 201")

    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder(_Dropped()))
    rc = main(["comment", "#3", "note", "-C", str(repo)])
    assert rc == EXIT_AMBIGUOUS
    assert "may already be posted" in capsys.readouterr().out


def test_same_team_id_across_linear_and_tasks_is_ambiguous_outside_a_repo(tmp_path, monkeypatch, capsys):
    _global_config(
        tmp_path,
        monkeypatch,
        "version: 1\nbackend: github-issues\nprojects:\n"
        "  - {name: HYP-linear, backend: linear, team: HYP}\n"
        "  - {name: HYP-tasks, backend: tasks, tasks: {url: 'https://tasks.example.test', team: HYP}}\n",
    )
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder())
    monkeypatch.chdir(tmp_path)
    assert main(["read", "HYP-3"]) == 2
    assert "cannot resolve which project" in capsys.readouterr().out


def test_classify_without_create_accepts_an_explicit_non_repo_cwd(tmp_path, monkeypatch):
    from tasklib import cli

    _global_config(tmp_path, monkeypatch, _REGISTRY)
    seen = []
    monkeypatch.setattr(cli, "cmd_classify", lambda args: seen.append(args.cwd) or 0)
    monkeypatch.setitem(cli.main.__globals__, "cmd_classify", cli.cmd_classify)
    assert main(["classify", "is this a question?", "-C", str(tmp_path)]) == 0
    assert seen == [str(tmp_path)]
    assert main(["classify", "fix it", "--create", "-C", str(tmp_path)]) == 2
