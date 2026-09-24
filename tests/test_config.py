"""config.py — the cascade loader, zero-config defaults, and validation."""

from __future__ import annotations

import pytest

from tasklib.config import ConfigError, load


def test_zero_config_defaults_to_github(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert cfg.layers == ["builtin-defaults"]
    assert cfg.enforce["acceptance_criteria"] == "required"


def test_repo_task_yaml_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: linear\nlinear: {team: HYP}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"


def test_global_layer_under_repo(tmp_path, monkeypatch):
    gdir = tmp_path / "cfg" / "task-cli"
    gdir.mkdir(parents=True)
    (gdir / "config.yaml").write_text("classify: {bias: justAsk}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path)
    # global sets bias, repo keeps backend; repo wins on conflicts but bias only in global
    assert cfg.classify_bias == "justAsk"
    assert cfg.backend == "github-issues"


def test_invalid_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: jira\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_classify_fallbacks_parsed_to_pairs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    chain = cfg.classify_fallbacks
    assert ("anthropic", "claude-haiku-4-5") in chain
    assert chain[0][0] == "laya"  # task-cli#208: laya is the new zero-config default head
    assert chain[1][0] == "anthropic"  # the pre-#208 default head, now second


def test_classify_fallbacks_accepts_a_laya_entry(tmp_path, monkeypatch):
    # task-cli#206: `classify.fallbacks` is provider-name-agnostic (config.py never allowlists
    # provider names) — a `laya` entry parses to a plain `("laya", <model>)` pair exactly like
    # every other provider, ready for `tasklib.classifiers.build_classifier_chain` to route.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nclassify:\n  fallbacks:\n    - { laya: local }\n"
        "    - { anthropic: claude-haiku-4-5 }\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.classify_fallbacks == [("laya", "local"), ("anthropic", "claude-haiku-4-5")]


def test_classify_capability_default_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.classify_capability == ""  # unset → no manifest resolution


def test_classify_capability_read_and_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        'version: 1\nbackend: github-issues\nclassify: {capability: "  fast  "}\n', encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.classify_capability == "fast"  # whitespace trimmed


def test_classify_route_process_to_harness_default_true(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.classify_route_process_to_harness is True


def test_classify_route_process_to_harness_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\nclassify: {route_process_to_harness: false}\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.classify_route_process_to_harness is False


def test_classify_route_process_to_harness_opt_out_via_quoted_yaml_string(tmp_path, monkeypatch):
    # Review finding: a bare `bool()` coercion treats the QUOTED string "false" as truthy
    # (Python's `bool("false")` is `True`), silently defeating the opt-out for any operator
    # who quotes the YAML value. Every falsy spelling in `_FALSY_STRINGS` must opt out too.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        'version: 1\nbackend: github-issues\nclassify: {route_process_to_harness: "false"}\n',
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.classify_route_process_to_harness is False


def test_classify_route_process_to_harness_blank_key_defaults_true(tmp_path, monkeypatch):
    # review finding, round 2: a BLANK key (`route_process_to_harness:` with nothing after the
    # colon, YAML `null`/Python `None`) and an explicit empty string previously disagreed with
    # each other (`bool(None)` is False, `"" not in _FALSY_STRINGS` is True) — both must mean
    # "unset" and fall back to the documented `True` default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\nclassify:\n  route_process_to_harness:\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.classify_route_process_to_harness is True


def test_classify_route_process_to_harness_empty_string_defaults_true(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        'version: 1\nbackend: github-issues\nclassify: {route_process_to_harness: ""}\n',
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.classify_route_process_to_harness is True


def test_session_detect_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.session_detect == ("env:TASK_SESSION", "tmux-pane", "git-branch")


def test_unsupported_version_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_load_does_not_mutate_module_defaults(tmp_path, monkeypatch):
    # mutating a loaded config's nested dict must not leak into the module-global DEFAULTS
    # (deepcopy guard) — otherwise --repo on one run would contaminate the next.
    from tasklib.config import DEFAULTS

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    cfg.data["github"]["repo"] = "owner/repo-override"
    assert DEFAULTS["github"]["repo"] == "auto"
    # a fresh load is unaffected
    assert load(tmp_path).data["github"]["repo"] == "auto"


def test_session_label_prefix_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    assert load(tmp_path).session_label_prefix == "session:"


# ── rig.yaml task: block — the CTO-decision tracker selector (#4136.4) ────────────────


def test_rig_yaml_missing_defaults_to_github(tmp_path, monkeypatch):
    # No rig.yaml and no task.yaml → the github default (the every-other-repo path).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_without_task_block_is_inert(tmp_path, monkeypatch):
    # A rig.yaml that says nothing about the tracker leaves the cascade on the github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("version: 1\nskills: {enabled: true}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_task_block_selects_linear(tmp_path, monkeypatch):
    # The hyperide case: a flat `task: {backend: linear, team: HYP}` block selects Linear/HYP.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "version: 1\ntask:\n  backend: linear\n  team: HYP\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert any(layer.endswith("rig.yaml") and layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_task_project_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task: {backend: linear, team: HYP, project: proj-123}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.section("linear")["project"] == "proj-123"


def test_rig_yaml_task_repo_alias_for_github(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: github-issues, repo: acme/web}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert cfg.section("github")["repo"] == "acme/web"


def test_task_yaml_wins_over_rig_yaml(tmp_path, monkeypatch):
    # A repo that keeps a native task.yaml is untouched: it overrides the rig.yaml block.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    (tmp_path / "task.yaml").write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"


def test_explicit_config_overrides_rig_yaml(tmp_path, monkeypatch):
    # --config replaces the task.yaml layer but still sits above the rig.yaml task: block.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("version: 1\nbackend: github-issues\n", encoding="utf-8")
    cfg = load(tmp_path, explicit_config=explicit)
    assert cfg.backend == "github-issues"


def test_rig_yaml_invalid_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: jira}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_not_a_mapping_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: linear\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_unknown_task_key_ignored_not_fatal(tmp_path, monkeypatch):
    # Forward-compat: an unknown sub-key (e.g. one a NEWER rig-cli writes) is warned-and-skipped,
    # not fatal — it must not crash every `task` command in a repo. The known keys still apply.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP, squad: blue}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.section("linear")["team"] == "HYP"
    assert "squad" not in cfg.data  # the unknown key did not leak into the config


def test_rig_yaml_non_mapping_document_is_inert(tmp_path, monkeypatch):
    # A rig.yaml whose root is not a mapping must not crash task; _load_yaml guards it, and
    # rig_task_overlay is defensively inert on a non-dict too.
    from tasklib.config import rig_task_overlay

    assert rig_task_overlay([]) == {}  # type: ignore[arg-type]
    assert rig_task_overlay("nonsense") == {}  # type: ignore[arg-type]


def test_rig_yaml_non_mapping_root_via_load_falls_through(tmp_path, monkeypatch):
    # End-to-end: a rig.yaml with a list/scalar root (a foreign file rig-cli owns) must NOT
    # crash every `task` command — it falls through cleanly to the github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)


def test_rig_yaml_broken_yaml_via_load_falls_through(tmp_path, monkeypatch):
    # Likewise, unparseable YAML in rig.yaml is tolerated (logged, ignored), not fatal.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, : broken\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "github-issues"


def test_rig_yaml_present_but_pyyaml_missing_falls_through(tmp_path, monkeypatch):
    # The installer can continue without PyYAML (built-in defaults promised). A repo that merely
    # CONTAINS a rig.yaml must NOT traceback with ModuleNotFoundError when `import yaml` fails in
    # _load_yaml — it falls through to the github default, same as an unreadable rig.yaml, and
    # logs a WARN (the contract: ignore-and-warn, not silently drop).
    import sys

    import tasklib.logging as task_logging

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")

    warns: list[tuple[str, str]] = []
    monkeypatch.setattr(
        task_logging,
        "log_event",
        lambda event, level="INFO", **f: warns.append((level, event)),
    )
    # Stub yaml out of the import system. On CPython a None entry makes `import yaml` raise a
    # real ModuleNotFoundError with name="yaml" (verified: "import of yaml halted; None in
    # sys.modules") — a true integration check that drives the actual `import yaml` in _load_yaml,
    # unlike the sibling tests that mock _load_yaml. Do NOT "fix" this to a message-match shim.
    monkeypatch.setitem(sys.modules, "yaml", None)

    cfg = load(tmp_path)  # must not raise
    assert cfg.backend == "github-issues"
    assert not any(layer.startswith("rig:") for layer in cfg.layers)
    assert ("WARN", "rig.yaml not loaded; ignoring") in warns


@pytest.mark.parametrize("missing", ["something_else", "yaml_processor", "my_yaml_utils"])
def test_rig_yaml_unrelated_modulenotfound_propagates(tmp_path, monkeypatch, missing):
    # The PyYAML-missing tolerance is narrow and keyed on exc.name only: a ModuleNotFoundError for
    # anything OTHER than the yaml package is a genuinely broken environment and must surface, not
    # masquerade as "no rig.yaml". Includes substring-collision names (yaml_processor) that a
    # message-based match would have wrongly swallowed.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear}\n", encoding="utf-8")

    import tasklib.config as config_mod

    def _boom(_path):
        raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)

    monkeypatch.setattr(config_mod, "_load_yaml", _boom)

    with pytest.raises(ModuleNotFoundError):
        load(tmp_path)


@pytest.mark.parametrize("yaml_name", ["yaml", "_yaml", "yaml.cyaml"])
def test_rig_yaml_pyyaml_submodule_missing_falls_through(tmp_path, monkeypatch, yaml_name):
    # A partially-broken PyYAML whose C-extension or a submodule is missing (exc.name in the yaml
    # package) is treated the same as PyYAML wholly absent: ignore-and-warn, github default.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear}\n", encoding="utf-8")

    import tasklib.config as config_mod

    def _boom(_path):
        raise ModuleNotFoundError(f"No module named '{yaml_name}'", name=yaml_name)

    monkeypatch.setattr(config_mod, "_load_yaml", _boom)

    cfg = load(tmp_path)  # must not raise
    assert cfg.backend == "github-issues"


def test_rig_yaml_task_version_subkey_ignored_not_fatal(tmp_path, monkeypatch):
    # `version` under task: is NOT task-cli's schema version (rig.yaml has its own top-level
    # version); a stray one is warn-and-ignored, not fail-closed — forward-compat with rig-cli.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP, version: 99}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.data["version"] == 1  # task-cli's own version is untouched by the stray key


def test_rig_yaml_task_flat_alias_merges_with_nested_section(tmp_path, monkeypatch):
    # README invites mixing a flat shorthand (team:) with a nested section (linear:) in the
    # same task: block — they must deep-merge, not clobber, regardless of key order.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  team: HYP\n  linear: {project: proj-123}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    lin = cfg.section("linear")
    assert lin["team"] == "HYP"  # from the flat alias
    assert lin["project"] == "proj-123"  # from the nested section


def test_rig_yaml_task_flat_alias_merges_nested_section_reverse_order(tmp_path, monkeypatch):
    # Same as above but the nested section is declared BEFORE the alias — order-independent.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  linear: {project: proj-123}\n  team: HYP\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    lin = cfg.section("linear")
    assert lin["team"] == "HYP"
    assert lin["project"] == "proj-123"


def test_rig_yaml_task_alias_vs_nested_leaf_conflict_rejected(tmp_path, monkeypatch):
    # team: HYP and linear.team: OTHER both set linear.team to different values → hard error.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  team: HYP\n  linear: {team: OTHER}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_repo_alias_merges_with_github_section(tmp_path, monkeypatch):
    # Symmetric to the linear case: the repo: shorthand and a nested github: section deep-merge.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: github-issues\n  repo: acme/web\n  github: {default_labels: [bug]}\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    gh = cfg.section("github")
    assert gh["repo"] == "acme/web"  # from the flat alias
    assert gh["default_labels"] == ["bug"]  # from the nested section


def test_rig_yaml_task_repo_alias_vs_github_leaf_conflict_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  repo: acme/web\n  github: {repo: other/repo}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_malformed_backend_value_clean_error(tmp_path, monkeypatch):
    # A non-string backend (dict/list) from the foreign rig.yaml must raise a clean ConfigError,
    # not an unhandled TypeError on the `in` membership test against the VALID_BACKENDS set.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text("task:\n  backend: {nested: bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(tmp_path)


def test_rig_yaml_task_passthrough_section_applies(tmp_path, monkeypatch):
    # A nested enforce: section in the rig.yaml task: block actually lands in the config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: github-issues\n  enforce: {motivation: optional}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.enforce["motivation"] == "optional"
    # other enforce defaults survive the deep merge
    assert cfg.enforce["acceptance_criteria"] == "required"


def test_rig_yaml_task_global_layer_carries(tmp_path, monkeypatch):
    # rig.yaml task: sits above global, so a global classify.bias still carries through.
    gdir = tmp_path / "cfg" / "task-cli"
    gdir.mkdir(parents=True)
    (gdir / "config.yaml").write_text("classify: {bias: justAsk}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "rig.yaml").write_text("task: {backend: linear, team: HYP}\n", encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.backend == "linear"
    assert cfg.classify_bias == "justAsk"


# ── linear.attachment_mode (tg#11652: link vs native Linear attach) ────────────────────────


def test_attachment_mode_default_is_native(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "native"
    # tg#16794/review-cli#484: the knob is now backend-agnostic — GitHub gets the same default.
    assert cfg.github_attachment_mode == "native"


def test_task_yaml_sets_attachment_mode_link(tmp_path, monkeypatch):
    # hyperide's own shape (HYP-1248 default): task.yaml pins attachment_mode: link.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nbackend: linear\nlinear:\n  team: HYP\n  attachment_mode: link\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "link"


def test_invalid_attachment_mode_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nlinear:\n  attachment_mode: carrier-pigeon\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="attachment_mode"):
        load(tmp_path)


def test_invalid_github_attachment_mode_rejected(tmp_path, monkeypatch):
    # symmetric with the linear.attachment_mode case above — tg#16794/review-cli#484 extended
    # the same validation to github.attachment_mode.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ngithub:\n  attachment_mode: carrier-pigeon\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="github.attachment_mode"):
        load(tmp_path)


def test_malformed_attachment_mode_value_clean_error(tmp_path, monkeypatch):
    # a dict/list value (malformed YAML) must fail-closed with ConfigError, not a raw TypeError
    # from the `in` membership check — mirrors test_rig_yaml_malformed_backend_value_clean_error.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nlinear:\n  attachment_mode: {nested: true}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="attachment_mode"):
        load(tmp_path)


def test_rig_yaml_task_attachment_mode_flat_alias(tmp_path, monkeypatch):
    # the rig.yaml task: block's flat shorthand, mirroring team/project/repo.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task: {backend: linear, team: HYP, attachment_mode: link}\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "link"
    # tg#16794/review-cli#484: the flat shorthand fans out onto BOTH backends' sections, since
    # whichever backend is actually active reads its own — proves the fan-out, not just Linear.
    assert cfg.github_attachment_mode == "link"


def test_rig_yaml_flat_attachment_mode_defers_to_nested_backend_override(tmp_path, monkeypatch):
    # review finding (PR #184): the flat alias fans `attachment_mode` onto BOTH backend
    # sections, so it must NOT collide with an explicit NESTED override for one specific
    # backend — "native by default, link specifically for github" is a deliberate per-backend
    # narrowing, not a contradiction, and must load exactly as it did before the fan-out ever
    # touched github (when this same block only ever set linear.attachment_mode).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n"
        "  backend: linear\n"
        "  team: HYP\n"
        "  attachment_mode: native\n"
        "  github: { attachment_mode: link }\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)  # must not raise ConfigError
    assert cfg.linear_attachment_mode == "native"  # unaffected — no nested linear override
    assert cfg.github_attachment_mode == "link"  # the explicit nested override wins


def test_rig_yaml_task_attachment_mode_nested_passthrough(tmp_path, monkeypatch):
    # the nested shape (linear: {attachment_mode: ...}) also works, since `linear` is already
    # a verbatim-passthrough key.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  linear: {team: HYP, attachment_mode: native}\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "native"


def test_trusted_attachment_hosts_default_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    cfg = load(tmp_path)
    assert cfg.trusted_attachment_hosts == frozenset()


def test_trusted_attachment_hosts_from_task_yaml(tmp_path, monkeypatch):
    # hyperide.github.io is a first-party host (a repo's own GitHub Pages), not a genuinely
    # external one — a repo-configured allowlist lets it get auto-fetched without weakening the
    # guard globally.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: [hyperide.github.io]\n", encoding="utf-8"
    )
    cfg = load(tmp_path)
    assert cfg.trusted_attachment_hosts == frozenset({"hyperide.github.io"})


def test_trusted_attachment_hosts_rig_yaml_task_passthrough(tmp_path, monkeypatch):
    # review finding: a rig-provisioned repo (the CANONICAL path, per AGENTS.md — rig writes
    # rig.yaml, a repo need not keep its own task.yaml at all) setting this under rig.yaml's
    # task: block must not be silently warned-and-ignored by rig_task_overlay — mirrors
    # test_rig_yaml_task_attachment_mode_nested_passthrough for the sibling key.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "rig.yaml").write_text(
        "task:\n  backend: linear\n  trusted_attachment_hosts: [hyperide.github.io]\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.trusted_attachment_hosts == frozenset({"hyperide.github.io"})


def test_trusted_attachment_hosts_normalized_lowercase_and_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: [' Hyperide.GitHub.io ']\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.trusted_attachment_hosts == frozenset({"hyperide.github.io"})


def test_trusted_attachment_hosts_non_list_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: hyperide.github.io\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


def test_trusted_attachment_hosts_non_string_entry_rejected(tmp_path, monkeypatch):
    # review finding: a non-string entry (e.g. an int from a YAML typo) must fail-closed via
    # the isinstance guard, not raise a raw TypeError from a later membership/string check.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: [123]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


def test_trusted_attachment_hosts_blank_entry_rejected(tmp_path, monkeypatch):
    # review finding: a blank/whitespace-only entry used to be silently dropped by the
    # normalizing property — now it fails closed at validation, the same as any other malformed
    # entry, instead of quietly doing nothing.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: ['   ']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


@pytest.mark.parametrize(
    "bad_host",
    [
        "https://hyperide.github.io",  # scheme prefix — the most likely copy-paste mistake
        "hyperide.github.io/assets",  # path included
        "hyperide.github.io:443",  # port included
        "user@hyperide.github.io",  # userinfo included
        "hyperide.github.io?copied-query",  # review finding, round 2: the char-blacklist this
        # allowlist replaced let this specific shape through (no /@:\\), and it can never equal
        # urlparse(url).hostname either.
        "hyperide github.io",  # internal whitespace
        "-hyperide.github.io",  # label starting with a hyphen (invalid per RFC 1123)
        "",  # blank via the parametrized (non-list-context) path too
    ],
)
def test_trusted_attachment_hosts_non_bare_hostname_rejected(tmp_path, monkeypatch, bad_host):
    # review finding: these all parse fine as a YAML string but can never equal
    # urlparse(url).hostname, so every attachment on that host would silently just keep
    # printing "skipped" with no clue why — fail loud instead, at config-load time.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        f"version: 1\ntrusted_attachment_hosts: [{bad_host!r}]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


@pytest.mark.parametrize(
    "ip_literal",
    [
        "169.254.169.254",  # review finding, round 3 (P1): the AWS/GCP/Azure cloud-metadata
        # endpoint — canonical dotted-decimal, caught directly by ipaddress.ip_address().
        "127.0.0.1",  # loopback, canonical form
        "::1",  # IPv6 loopback, canonical form
        "2130706433",  # review finding, round 4 (P1): bare decimal integer == 127.0.0.1 —
        # a well-documented SSRF filter-bypass encoding ipaddress.ip_address() does NOT
        # recognize as a valid literal (it only accepts canonical notation), but OS resolvers
        # (getaddrinfo) have historically still accepted.
        "0x7f000001",  # hex literal == 127.0.0.1, same bypass class
        "0",  # bare "0" resolves to 0.0.0.0 on many resolvers
        "127.000.000.001",  # zero-padded octets — non-canonical but resolver-accepted
        "127.1",  # shorthand dotted form (fewer than 4 octets) == 127.0.0.1 on many resolvers
        "127.0x1",  # review finding, round 5 (P1): MIXED per-label notation (plain-decimal
        # label + hex label) == 127.0.0.1 on getaddrinfo-legacy resolvers (verified live:
        # macOS). Neither a whole-string bare-decimal nor a whole-string-hex check (round 4)
        # catches this — it needed a genuine per-LABEL check.
        "0x7f.1",  # same mixed-notation bypass, hex label first
        "0x7f.0x0.0x0.0x1",  # all-hex, 4 labels == 127.0.0.1
        "0177.0x0.0.1",  # decimal-looking-but-legacy-octal label mixed with hex and plain —
        # the per-label check doesn't need to know it's "octal", just that every label is
        # purely digit/hex-numeric.
    ],
)
def test_trusted_attachment_hosts_ip_literal_rejected(tmp_path, monkeypatch, ip_literal):
    # a bare IP literal, in ANY notation a resolver might still accept, is never a legitimate
    # value for this knob — it exists to name a first-party DOMAIN.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        f"version: 1\ntrusted_attachment_hosts: ['{ip_literal}']\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


@pytest.mark.parametrize(
    "alias",
    [
        "localhost",  # review finding, round 4
        "attacker.localhost",  # review finding, round 6 (P1): RFC 6761 §6.3 formally
        # reserves the ENTIRE .localhost TLD as always resolving to loopback — verified live
        # that the bare "localhost" check alone let this through.
        "sub.domain.localhost",
        "localhost.localdomain",  # review finding, round 6: long-standing glibc/Linux
        # default-hostname convention, still common in /etc/hosts.
        "attacker.localhost.localdomain",  # review finding, round 8: a SUBDOMAIN of the
        # localdomain alias — the round-6 check rejected the bare alias but not a subdomain of
        # it, even though the same resolver setups that resolve the bare alias resolve this too.
        "anything.localdomain",
    ],
)
def test_trusted_attachment_hosts_localhost_alias_rejected(tmp_path, monkeypatch, alias):
    # "localhost" and its recognized aliases resolve to loopback and are not IP literals in
    # any sense the checks above catch, but are just as unambiguously not a legitimate
    # first-party domain for this knob.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        f"version: 1\ntrusted_attachment_hosts: [{alias}]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="trusted_attachment_hosts"):
        load(tmp_path)


def test_trusted_attachment_hosts_numeric_looking_but_not_pure_digits_survives(
    tmp_path, monkeypatch
):
    # guard against the numeric-literal check over-firing: a real hostname with a numeric
    # LABEL mixed with letters (not purely digits end-to-end) must still be accepted.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ntrusted_attachment_hosts: [s3.us-east-1.amazonaws.com]\n",
        encoding="utf-8",
    )
    cfg = load(tmp_path)
    assert cfg.trusted_attachment_hosts == frozenset({"s3.us-east-1.amazonaws.com"})


def test_env_attachment_mode_override_beats_repo_task_yaml(tmp_path, monkeypatch):
    # tg#11652: a repo can pin attachment_mode: link (hyperide's HYP-1248 default) for
    # everyone, but ONE machine can override it locally without touching the committed file —
    # the highest-precedence layer.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nlinear:\n  attachment_mode: link\n", encoding="utf-8"
    )
    monkeypatch.setenv("TASK_CLI_ATTACHMENT_MODE", "native")
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "native"
    assert any("TASK_CLI_ATTACHMENT_MODE" in layer for layer in cfg.layers)


def test_env_attachment_mode_override_affects_github_too(tmp_path, monkeypatch):
    # tg#16794/review-cli#484: the one env var must override whichever backend is actually
    # active — proves the override also sets github.attachment_mode, not just linear's.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\ngithub:\n  attachment_mode: link\n", encoding="utf-8"
    )
    monkeypatch.setenv("TASK_CLI_ATTACHMENT_MODE", "native")
    cfg = load(tmp_path)
    assert cfg.github_attachment_mode == "native"
    assert any("TASK_CLI_ATTACHMENT_MODE" in layer for layer in cfg.layers)


def test_env_attachment_mode_override_beats_explicit_config_flag(tmp_path, monkeypatch):
    # review finding: the README documents the env override as winning even over an explicit
    # --config P (deliberate — it's a per-machine convenience meant to beat anything
    # file-based) — pin that with a real test, not just the docstring/README claim, so a
    # future refactor that reorders the two layers can't pass the suite while inverting it.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("version: 1\nlinear:\n  attachment_mode: link\n", encoding="utf-8")
    monkeypatch.setenv("TASK_CLI_ATTACHMENT_MODE", "native")
    cfg = load(tmp_path, explicit_config=explicit)
    assert cfg.linear_attachment_mode == "native"


def test_env_attachment_mode_override_unset_leaves_repo_value_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nlinear:\n  attachment_mode: link\n", encoding="utf-8"
    )
    monkeypatch.delenv("TASK_CLI_ATTACHMENT_MODE", raising=False)
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "link"
    assert not any("TASK_CLI_ATTACHMENT_MODE" in layer for layer in cfg.layers)


def test_env_attachment_mode_override_invalid_value_rejected(tmp_path, monkeypatch):
    # review finding: the error must name TASK_CLI_ATTACHMENT_MODE itself, not just the
    # generic "linear.attachment_mode must be one of [...]" a user would get from a bad
    # task.yaml value — that message alone gives no clue to check the shell environment
    # instead of auditing config files that are actually fine.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    monkeypatch.setenv("TASK_CLI_ATTACHMENT_MODE", "carrier-pigeon")
    with pytest.raises(ConfigError, match="TASK_CLI_ATTACHMENT_MODE"):
        load(tmp_path)


def test_env_attachment_mode_override_blank_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-global"))
    (tmp_path / "task.yaml").write_text(
        "version: 1\nlinear:\n  attachment_mode: link\n", encoding="utf-8"
    )
    monkeypatch.setenv("TASK_CLI_ATTACHMENT_MODE", "   ")
    cfg = load(tmp_path)
    assert cfg.linear_attachment_mode == "link"
