"""Folder-level / global rig-cli config routing (UB-23).

Two NEW layers in the cascade, both above the repo's own ``rig.yaml`` (see
``docs/plans/2026-09-22-tasks-rig-routing.md`` for the full precedence chain and the
fail-closed contract):

- ``rig-global:`` — rig-cli's OWN global config's ``task:`` block
  (``~/.config/rig/config.yaml``, distinct from task-cli's own
  ``~/.config/task-cli/config.yaml``).
- ``rig-ancestor:`` — every directory strictly above the repo root (bounded by ``$HOME``, or by
  the filesystem root if the repo is outside ``$HOME``) that has its own ``rig.yaml``.

Every test here builds its own isolated ``HOME``/``XDG_CONFIG_HOME`` inside ``tmp_path`` and
passes them to ``load()`` via the explicit ``env`` dict param — never ``monkeypatch.setenv`` on
real process env, never touching the real filesystem outside ``tmp_path``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tasklib.config import ConfigError, _ancestor_rig_paths, load


def _env(tmp_path: Path, home: Path, *, xdg: Path | None = None) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg if xdg is not None else tmp_path / "no-global"),
    }


# ── 1. Outer→inner ancestor precedence ──────────────────────────────────────────────


def test_ancestor_outer_to_inner_precedence(tmp_path):
    home = tmp_path / "home"
    outer = home / "xp"
    inner = outer / "workspace"
    repo = inner / "myrepo"
    outer.mkdir(parents=True)
    inner.mkdir()
    repo.mkdir()
    (outer / "rig.yaml").write_text("task: {backend: linear, team: OUTER}\n", encoding="utf-8")
    (inner / "rig.yaml").write_text("task: {backend: linear, team: INNER}\n", encoding="utf-8")

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.section("linear")["team"] == "INNER"  # nearer ancestor wins
    layers = cfg.layers
    assert any(layer == f"rig-ancestor:{outer / 'rig.yaml'}" for layer in layers)
    assert any(layer == f"rig-ancestor:{inner / 'rig.yaml'}" for layer in layers)
    # outer must be folded BEFORE inner (so inner can win) — assert relative order too.
    outer_idx = layers.index(f"rig-ancestor:{outer / 'rig.yaml'}")
    inner_idx = layers.index(f"rig-ancestor:{inner / 'rig.yaml'}")
    assert outer_idx < inner_idx


# ── 2. Repo's own rig.yaml beats any ancestor ───────────────────────────────────────


def test_repo_rig_yaml_beats_ancestor(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text("task: {backend: linear, team: OUTER}\n", encoding="utf-8")
    (repo / "rig.yaml").write_text("task: {backend: linear, team: REPO}\n", encoding="utf-8")

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.section("linear")["team"] == "REPO"


# ── 3. Repo's native task.yaml beats an ancestor rig.yaml ───────────────────────────


def test_repo_task_yaml_beats_ancestor_rig_yaml(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text("task: {backend: linear, team: OUTER}\n", encoding="utf-8")
    (repo / "task.yaml").write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.backend == "github-issues"


# ── 4. --config beats everything ────────────────────────────────────────────────────


def test_explicit_config_beats_ancestor_rig_yaml(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text("task: {backend: linear, team: OUTER}\n", encoding="utf-8")
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")

    cfg = load(repo, env=_env(tmp_path, home), explicit_config=explicit)

    assert cfg.backend == "github-issues"


# ── 5. Sibling isolation — the exact bug the contract warns about ──────────────────


def test_sibling_prefix_directory_does_not_leak(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    xp_other = home / "xp-other"  # deliberately prefix-matches "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    xp_other.mkdir(parents=True)
    repo.mkdir()
    (xp_other / "rig.yaml").write_text(
        "task: {backend: linear, team: SHOULD_NOT_LEAK}\n", encoding="utf-8"
    )

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.backend == "github-issues"  # sibling's config never applied
    assert not any("xp-other" in layer for layer in cfg.layers)


# ── 6. A worktree path still resolves ancestors correctly ──────────────────────────


def test_worktree_path_resolves_ancestors(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    proj = xp / "task-cli"
    worktree = proj / ".worktrees" / "feature-x"
    xp.mkdir(parents=True)
    proj.mkdir()
    worktree.mkdir(parents=True)
    (xp / "rig.yaml").write_text("task: {backend: linear, team: XPWIDE}\n", encoding="utf-8")

    cfg = load(worktree, env=_env(tmp_path, home))

    assert cfg.section("linear")["team"] == "XPWIDE"
    assert any(layer == f"rig-ancestor:{xp / 'rig.yaml'}" for layer in cfg.layers)


# ── 7. HOME boundary — nothing above $HOME is ever read ────────────────────────────


def test_nothing_above_home_is_read(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    home.mkdir()
    repo.mkdir()
    # A rig.yaml ABOVE home (in home's own parent) with a poison value that must never apply.
    (tmp_path / "rig.yaml").write_text(
        "task: {backend: linear, team: POISON_ABOVE_HOME}\n", encoding="utf-8"
    )

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.backend == "github-issues"
    assert not any("POISON_ABOVE_HOME" in layer for layer in cfg.layers)
    # the poisoned file lives at tmp_path/rig.yaml — above home — so it must never surface here.
    assert f"rig-ancestor:{tmp_path / 'rig.yaml'}" not in cfg.layers


# ── 8. Outside-HOME repo — walk excludes filesystem root itself ────────────────────


def test_outside_home_walk_excludes_filesystem_root():
    # Unit-tested directly on the helper: no real filesystem writes needed/possible at "/".
    repo_root = Path("/private/some/nested/outside/home/repo")
    env = {"HOME": "/Users/testuser"}

    paths = _ancestor_rig_paths(repo_root, env)

    fs_root_rig_yaml = Path("/rig.yaml")
    assert fs_root_rig_yaml not in paths
    assert all(p.parent != Path("/") for p in paths)


# ── 9. rig-global sits below native global, above ancestors ────────────────────────


def test_three_way_precedence_native_global_rig_global_ancestor(tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / "task-cli").mkdir(parents=True)
    (xdg / "task-cli" / "config.yaml").write_text(
        "classify: {bias: fromNativeGlobal}\n", encoding="utf-8"
    )
    (xdg / "rig").mkdir(parents=True)
    (xdg / "rig" / "config.yaml").write_text(
        "task: {backend: linear, team: FROM_RIG_GLOBAL}\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text(
        "task: {backend: linear, team: FROM_ANCESTOR}\n", encoding="utf-8"
    )

    cfg = load(repo, env=_env(tmp_path, home, xdg=xdg))

    assert cfg.classify_bias == "fromNativeGlobal"  # native global still applies
    assert cfg.section("linear")["team"] == "FROM_ANCESTOR"  # ancestor beats rig-global
    layers = cfg.layers
    assert any(layer.startswith("global:") for layer in layers)
    assert any(layer.startswith("rig-global:") for layer in layers)
    assert any(layer.startswith("rig-ancestor:") for layer in layers)
    # order: rig-global (a default) folds FIRST, then the user's own explicit native global
    # config (which must be able to override it), then rig-ancestor (later wins).
    gi = next(i for i, layer in enumerate(layers) if layer.startswith("global:"))
    rgi = next(i for i, layer in enumerate(layers) if layer.startswith("rig-global:"))
    rai = next(i for i, layer in enumerate(layers) if layer.startswith("rig-ancestor:"))
    assert rgi < gi < rai


def test_global_native_backend_wins_over_global_rig_task_block(tmp_path):
    # Review correction: a user's explicit global native choice (their own
    # ~/.config/task-cli/config.yaml) must win over a conflicting rig-global DEFAULT, not get
    # silently replaced by it — the same relationship task.yaml already has with rig.yaml.
    xdg = tmp_path / "xdg"
    (xdg / "task-cli").mkdir(parents=True)
    (xdg / "task-cli" / "config.yaml").write_text("backend: github-issues\n", encoding="utf-8")
    (xdg / "rig").mkdir(parents=True)
    (xdg / "rig" / "config.yaml").write_text(
        "task: {backend: linear, team: RIG_GLOBAL_DEFAULT}\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    repo = home / "repo"
    home.mkdir()
    repo.mkdir()

    cfg = load(repo, env=_env(tmp_path, home, xdg=xdg))

    assert cfg.backend == "github-issues"  # explicit global native choice wins


# ── 10. include_global=False reads neither global file ─────────────────────────────


def test_include_global_false_skips_both_global_layers(tmp_path, monkeypatch):
    import tasklib.config as config_mod

    xdg = tmp_path / "xdg"
    (xdg / "task-cli").mkdir(parents=True)
    (xdg / "task-cli" / "config.yaml").write_text(
        "classify: {bias: shouldNotApply}\n", encoding="utf-8"
    )
    (xdg / "rig").mkdir(parents=True)
    (xdg / "rig" / "config.yaml").write_text(
        "task: {backend: linear, team: shouldNotApply}\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    repo = home / "repo"
    home.mkdir()
    repo.mkdir()

    forbidden = {xdg / "task-cli" / "config.yaml", xdg / "rig" / "config.yaml"}
    orig_load_yaml = config_mod._load_yaml

    def _guarded(path):
        assert path not in forbidden, f"global layer file opened despite include_global=False: {path}"
        return orig_load_yaml(path)

    monkeypatch.setattr(config_mod, "_load_yaml", _guarded)

    cfg = load(repo, env=_env(tmp_path, home, xdg=xdg), include_global=False)

    assert cfg.classify_bias == "change"  # default, not the forbidden global value
    assert cfg.backend == "github-issues"  # default, not the forbidden rig-global value
    assert not any(layer.startswith("global:") or layer.startswith("rig-global:") for layer in cfg.layers)


# ── 11-13. Fail-closed ancestor layer ───────────────────────────────────────────────


def test_malformed_ancestor_yaml_names_the_file(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text("task: {backend: linear, : broken\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


def test_non_mapping_task_block_in_ancestor_names_the_file(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text("task: linear\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


def test_invalid_backend_in_ancestor_names_the_file_not_silent_fallback(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text("task: {backend: jira}\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


# ── 14. code_prefix is stripped from an ancestor's task: block ─────────────────────


def test_code_prefix_stripped_from_ancestor_not_propagated(tmp_path, monkeypatch):
    import tasklib.logging as task_logging

    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text(
        "task: {backend: linear, team: HYP, code_prefix: XP}\n", encoding="utf-8"
    )

    warns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        task_logging,
        "log_event",
        lambda event, level="INFO", **f: warns.append((level, event)),
    )

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert "code_prefix" not in cfg.data
    # silent strip — no WARN about an unknown/ignored key for code_prefix specifically
    assert not any("code_prefix" in event for _level, event in warns)


# ── 15. Explicit repo task.yaml wins over an ancestor rig.yaml (tasks backend) ─────


def test_repo_task_yaml_wins_over_ancestor_tasks_backend(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text(
        "task: {backend: tasks, tasks: {url: 'https://tasks-ultrabricks.vercel.app', team: UB}}\n",
        encoding="utf-8",
    )
    (repo / "task.yaml").write_text(
        "version: 1\nbackend: linear\nlinear: {team: HYP}\n", encoding="utf-8"
    )

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.backend == "linear"


# ── 16. Zero-config case unchanged ──────────────────────────────────────────────────


def test_zero_config_layers_unchanged(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    home.mkdir()
    repo.mkdir()

    cfg = load(repo, env=_env(tmp_path, home))

    assert cfg.layers == ["builtin-defaults"]


# ── 17. task --help still works with PyYAML unavailable ────────────────────────────


def test_help_works_without_pyyaml(monkeypatch):
    import sys

    from tasklib.cli import main

    monkeypatch.setitem(sys.modules, "yaml", None)

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


# ── 18. include_global=False does NOT skip the ancestor walk ───────────────────────


def test_include_global_false_still_applies_ancestor_layer(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    (xp / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")

    cfg = load(repo, env=_env(tmp_path, home), include_global=False)

    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert any(layer.startswith("rig-ancestor:") for layer in cfg.layers)


# ── 20-21. tasks: section field validation for ancestor/rig-global layers ──────────


def test_ancestor_tasks_section_bad_field_type_names_the_file(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text(
        "task: {backend: tasks, tasks: {url: 123, team: UB}}\n", encoding="utf-8"
    )

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


def test_ancestor_tasks_section_invalid_attachment_mode_names_the_file(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text(
        "task:\n"
        "  backend: tasks\n"
        "  tasks: {url: 'https://tasks-ultrabricks.vercel.app', team: UB, "
        "attachment_mode: carrier-pigeon}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


def test_ancestor_tasks_section_unknown_key_names_the_file(tmp_path):
    home = tmp_path / "home"
    xp = home / "xp"
    repo = xp / "repo"
    xp.mkdir(parents=True)
    repo.mkdir()
    bad = xp / "rig.yaml"
    bad.write_text(
        "task:\n"
        "  backend: tasks\n"
        "  tasks: {url: 'https://tasks-ultrabricks.vercel.app', team: UB, workspace: bogus}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=re.escape(str(bad))):
        load(repo, env=_env(tmp_path, home))


def test_rig_global_tasks_section_bad_field_type_names_the_file(tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / "rig").mkdir(parents=True)
    rig_global = xdg / "rig" / "config.yaml"
    rig_global.write_text(
        "task: {backend: tasks, tasks: {url: 'https://x', team: 42}}\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    repo = home / "repo"
    home.mkdir()
    repo.mkdir()

    with pytest.raises(ConfigError, match=re.escape(str(rig_global))):
        load(repo, env=_env(tmp_path, home, xdg=xdg))
