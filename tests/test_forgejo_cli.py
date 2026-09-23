"""CLI seams for UB-25: explicit non-repository ``-C`` refusal, ``task comment``, and Forgejo
routing through the cwd / ``projects:`` registry. Hermetic — ``get_backend`` is replaced."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from tasklib.cli import _load, main
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


@pytest.mark.parametrize(
    "argv",
    [["read", "#4"], ["list"], ["find", "x"], ["status", "#4"], ["gantt"], ["audit"],
     ["classify", "is this a question?"]],
)
def test_explicit_non_repo_cwd_is_refused_for_reads_too(argv, tmp_path, monkeypatch, capsys):
    # UB-25 contract: EVERY explicit -C must be a git worktree, reads included — including the
    # classification-only `classify` (a review suggested exempting it; the user's requirement is
    # that an explicit target is always a repository).
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    rc = main([*argv, "-C", str(tmp_path)])
    assert rc == 2
    assert "not inside a git repository" in capsys.readouterr().out
    assert rec.configs == []


def test_registry_read_works_from_an_unscoped_cwd_without_c(tmp_path, monkeypatch):
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    monkeypatch.chdir(tmp_path)
    assert main(["read", "#4"]) == 0
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
    monkeypatch.chdir(tmp_path)
    rc = main(["read", "#4"])
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
    monkeypatch.chdir(tmp_path)
    assert main(["read", "acme/api#4"]) == 0
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


def test_github_default_auto_routes_a_known_forgejo_origin_via_fj_credentials(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://alex-mextner@git.hyperide.ai/ultrabricks/x.git",
        ],
        check=True,
    )

    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    assert main(["read", "#4", "-C", str(repo)]) == 0

    (cfg,) = rec.configs
    assert cfg.backend == "forgejo"
    assert cfg.section("forgejo")["url"] == "https://git.hyperide.ai"
    assert cfg.section("forgejo")["repo"] == "auto"
    assert any(layer == "autodetect:forgejo:https://git.hyperide.ai" for layer in cfg.layers)


def _write_fj_key(home, host):
    keys = home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(json.dumps({"hosts": {host: {"token": "test-token"}}}), encoding="utf-8")


def test_auto_route_preserves_nonstandard_forgejo_https_port(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai:3000")

    repo = tmp_path / "repo-port"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://git.hyperide.ai:3000/ultrabricks/x.git"],
        check=True,
    )
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)

    assert main(["read", "#4", "-C", str(repo)]) == 0
    assert rec.configs[0].backend == "forgejo"
    assert rec.configs[0].section("forgejo")["url"] == "https://git.hyperide.ai:3000"


def test_auto_route_supports_scp_style_ssh_origin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")

    repo = tmp_path / "repo-ssh"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "git@git.hyperide.ai:ultrabricks/x.git"],
        check=True,
    )
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)

    assert main(["read", "#4", "-C", str(repo)]) == 0
    assert rec.configs[0].backend == "forgejo"
    assert rec.configs[0].section("forgejo")["url"] == "https://git.hyperide.ai"


def test_auto_route_ignores_a_non_github_host_without_fj_credentials(tmp_path, monkeypatch):
    # Only a host fj already holds a key for is treated as Forgejo; an arbitrary git server
    # (GitLab, Bitbucket, a key for a different host) keeps the github-issues default.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    monkeypatch.delenv("FORGEJO_TOKEN", raising=False)
    monkeypatch.delenv("FORGEJO_URL", raising=False)
    _write_fj_key(home, "git.hyperide.ai")
    repo = tmp_path / "repo-gitlab"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://gitlab.example.com/team/x.git"],
        check=True,
    )
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    main(["read", "#4", "-C", str(repo)])
    assert rec.configs == [] or rec.configs[0].backend == "github-issues"
    assert all(cfg.section("forgejo").get("url", "") == "" for cfg in rec.configs)


@pytest.mark.parametrize(
    "remote",
    ["https://git.hyperide.ai:70000/ultrabricks/x.git", "https://git.hyperide.ai:abc/ultrabricks/x.git"],
)
def test_auto_route_survives_a_malformed_origin_port(remote, tmp_path, monkeypatch):
    # Auto-detection runs on every command; a typo'd origin port must degrade to "no
    # detection", never abort unrelated commands.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")
    repo = tmp_path / "repo-badport"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    cfg = _load(SimpleNamespace(cwd=str(repo), config=None, backend=None, repo=None))
    assert cfg.backend == "github-issues"


def test_auto_route_does_not_carry_a_plain_http_port_into_https(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")
    repo = tmp_path / "repo-http"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "http://git.hyperide.ai:80/ultrabricks/x.git"],
        check=True,
    )
    cfg = _load(SimpleNamespace(cwd=str(repo), config=None, backend=None, repo=None))
    assert cfg.backend == "forgejo"
    assert cfg.section("forgejo")["url"] == "https://git.hyperide.ai"


def test_auto_route_never_overrides_an_explicitly_configured_backend(tmp_path, monkeypatch):
    # An explicit `backend: github-issues` (e.g. a repo that deliberately files on its GitHub
    # mirror) wins over zero-config Forgejo detection, even when fj holds a token for the host.
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")
    repo = tmp_path / "repo-explicit"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://git.hyperide.ai/ultrabricks/x.git"],
        check=True,
    )
    (repo / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\ngithub: {repo: auto}\n", encoding="utf-8"
    )
    rec = _Recorder(_Commentable())
    monkeypatch.setattr("tasklib.backends.get_backend", rec)
    main(["read", "#4", "-C", str(repo)])
    assert rec.configs == [] or rec.configs[0].backend == "github-issues"
    assert all("autodetect" not in layer for cfg in rec.configs for layer in cfg.layers)


def test_auto_route_does_not_overwrite_explicit_dormant_forgejo_pin(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    _write_fj_key(home, "git.hyperide.ai")

    repo = tmp_path / "repo-pinned"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://git.hyperide.ai/ultrabricks/x.git"],
        check=True,
    )
    (repo / "task.yaml").write_text(
        "version: 1\n"
        "backend: github-issues\n"
        "forgejo:\n"
        "  url: https://other-forgejo.example\n"
        "  repo: pinned/team\n",
        encoding="utf-8",
    )
    cfg = _load(SimpleNamespace(cwd=str(repo), config=None, backend=None, repo=None))
    assert cfg.backend == "github-issues"
    assert cfg.section("forgejo")["url"] == "https://other-forgejo.example"
    assert cfg.section("forgejo")["repo"] == "pinned/team"


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


def test_classify_with_create_and_explicit_non_repo_cwd_is_refused(tmp_path, monkeypatch, capsys):
    _global_config(tmp_path, monkeypatch, _REGISTRY)
    monkeypatch.setattr("tasklib.backends.get_backend", _Recorder())
    assert main(["classify", "fix it", "--create", "-C", str(tmp_path)]) == 2
    assert "not inside a git repository" in capsys.readouterr().out
