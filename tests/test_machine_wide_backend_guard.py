"""`task create` must not file a ticket on a non-GitHub backend that came only from a machine-wide
config layer (2026-10-20: a global `backend: tasks` / team HYP leaked personal-project tickets into
the startup tracker).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tasklib.cli import main
from tasklib.config import load

from .conftest import FakeBackend

_GLOBAL_TASKS = (
    "version: 1\nbackend: tasks\ntasks:\n  url: https://tasks.example.test\n  team: HYP\n"
)
_IMPACT = "People reading the tracker see tickets from the right project instead of a foreign one."


@pytest.fixture(autouse=True)
def _env(monkeypatch, isolated_state):
    monkeypatch.setenv("TASK_SESSION", "testsess")
    monkeypatch.setattr(
        "tasklib.backends.get_backend", lambda cfg, env=None: FakeBackend()
    )


def _write_global(tmp_path, text):
    gdir = tmp_path / "config" / "task-cli"
    gdir.mkdir(parents=True)
    (gdir / "config.yaml").write_text(text, encoding="utf-8")


def _create(*extra):
    return main(
        [
            "create", "--title", "Add a thing", "--what", "the change", "--why", "because",
            "--impact", _IMPACT, "--if-not-done", "pain",
            "--acceptance", "it works", "--acceptance", "it also handles the empty case",
            *extra,
        ]
    )


def test_global_backend_is_flagged_machine_wide(tmp_path):
    _write_global(tmp_path, _GLOBAL_TASKS)
    cfg = load(tmp_path / "repo")
    assert cfg.backend == "tasks"
    assert cfg.backend_machine_wide is True


def test_repo_task_yaml_overrides_machine_wide_flag(tmp_path):
    _write_global(tmp_path, _GLOBAL_TASKS)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\ngithub:\n  repo: o/r\n", encoding="utf-8"
    )
    cfg = load(repo)
    assert cfg.backend == "github-issues"
    assert cfg.backend_machine_wide is False


def test_create_refuses_when_non_github_backend_is_only_global(tmp_path, capsys):
    _write_global(tmp_path, _GLOBAL_TASKS)
    rc = _create()
    out = capsys.readouterr().out
    assert rc == 2
    assert "refusing to create a ticket" in out
    assert "task.yaml" in out and "--backend tasks" in out


def test_create_allowed_when_global_backend_is_github(tmp_path, capsys):
    _write_global(tmp_path, "version: 1\nbackend: github-issues\n")
    assert _create() == 0
    assert "created" in capsys.readouterr().out


def test_create_allowed_with_no_global_backend(capsys):
    assert _create() == 0
    assert "created" in capsys.readouterr().out


def test_create_allowed_when_repo_pins_the_tracker(tmp_path, capsys):
    _write_global(tmp_path, _GLOBAL_TASKS)
    # the isolated cwd repo gets its own task.yaml, exactly what the refusal message asks for
    (Path.cwd() / "task.yaml").write_text(_GLOBAL_TASKS, encoding="utf-8")
    assert _create() == 0
    assert "refusing" not in capsys.readouterr().out


def test_explicit_backend_flag_bypasses_the_guard(tmp_path, capsys):
    _write_global(tmp_path, _GLOBAL_TASKS)
    _create("--backend", "tasks")
    assert "refusing to create a ticket: this repo has no tracker" not in capsys.readouterr().out
