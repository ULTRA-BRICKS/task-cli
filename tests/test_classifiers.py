"""tasklib.classifiers — the pluggable ClassifierProvider architecture (task-cli#206/#217).

Covers `ReviewCliClassifier` (the pre-#206 `review just-ask` shell-out path, refactored in
place — behavior-preserving, so `tests/test_cli.py`'s existing `tasklib.classify.resolve_chain`
/ `cli._run_review_just_ask` monkeypatches need zero changes), `LayaClassifier` (the local
`laya`-package backend, mocked at its documented `laya.load(...).predict(state, questions) ->
dict` boundary), `JevClassifier` (the #217 direct-HTTP Vercel AI Gateway backend, mocked at
`tasklib.backends.http.request_json` — the shared stdlib-urllib helper every backend uses,
never a real network call), and `build_classifier_chain` (turns a `classify.fallbacks` list
into an ordered provider chain).
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

import tasklib.classifiers as classifiers
from tasklib.classify import Classification, DEFAULT_FALLBACKS, ResolvedModel, Verdict
from tasklib.classifiers import (
    _CLASSIFICATION_QUESTIONS,
    JevClassifier,
    LayaClassifier,
    ReviewCliClassifier,
    build_classifier_chain,
)


@pytest.fixture
def fake_laya_module(monkeypatch):
    """Install a fake `laya` module in `sys.modules` for the duration of one test.

    `monkeypatch.setitem` restores the previous `sys.modules["laya"]` entry (or removes it)
    automatically on teardown — replaces hand-rolled `try`/`finally` bookkeeping.
    """

    def _install(load_fn):
        class _FakeLayaModule:
            load = staticmethod(load_fn)

        monkeypatch.setitem(sys.modules, "laya", _FakeLayaModule())

    return _install


# The REAL raw dict observed from a live `agent.predict(message, questions)` call against
# `laya==0.3.7` / `convaiinnovations/laya` (task-cli#206 live-verification; captured verbatim,
# only the message/criteria differ from the shipped `_CLASSIFICATION_QUESTIONS` schema in
# shape, not in envelope). `answers[<question>]` for a `choice`-typed question carries
# `choice` (the winning label) plus `probabilities`/`confidence`; `noul`-typed questions carry
# a `noul` float instead.
_REAL_LAYA_RAW_RESULT = {
    "model": "laya-rl-agent",
    "answers": {
        "verdict": {
            "type": "choice",
            "choice": "justAsk",
            "probabilities": {"change": 0.1796, "actionable": 0.3644, "justAsk": 0.456},
            "confidence": 0.0586,
            "action": {"act_probability": 1.0},
        },
        "process_or_product": {
            "type": "choice",
            "choice": "product",
            "probabilities": {"process": 0.1589, "product": 0.8411},
            "confidence": 0.3684,
            "action": {"act_probability": 1.0},
        },
        "priority": {
            "type": "choice",
            "choice": "P1",
            "probabilities": {"P0": 0.0575, "P1": 0.355, "P2": 0.3378, "P3": 0.2497},
            "confidence": 0.102,
            "action": {"act_probability": 1.0},
        },
        "story_points": {
            "type": "choice",
            "choice": "3",
            "probabilities": {"1": 0.1, "2": 0.2, "3": 0.4, "5": 0.15, "8": 0.1, "13": 0.05},
            "confidence": 0.4,
            "action": {"act_probability": 1.0},
        },
    },
    "usage": {"input_tokens": 248, "output_tokens": 0},
}


# ── ReviewCliClassifier — behavior-preserving refactor of the pre-#206 shell-out path ──────


def test_review_cli_classifier_unavailable_when_resolve_chain_returns_none(monkeypatch):
    monkeypatch.setattr("tasklib.classify.resolve_chain", lambda *a, **k: None)
    provider = ReviewCliClassifier(fallbacks=[("anthropic", "claude-haiku-4-5")])
    assert provider.available() is False


def test_review_cli_classifier_available_when_resolve_chain_resolves(monkeypatch):
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda *a, **k: ResolvedModel("anthropic", "claude:claude-haiku-4-5"),
    )
    provider = ReviewCliClassifier(fallbacks=[("anthropic", "claude-haiku-4-5")])
    assert provider.available() is True


def test_review_cli_classifier_classify_honors_existing_cli_monkeypatch(monkeypatch):
    # The exact monkeypatch shape every pre-existing test_cli.py classify test already uses —
    # proves the refactor kept both seams (classify.resolve_chain, cli._run_review_just_ask)
    # late-bound so neither had to change.
    from tasklib import cli

    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda *a, **k: ResolvedModel("anthropic", "claude:claude-haiku-4-5"),
    )
    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    provider = ReviewCliClassifier(fallbacks=[("anthropic", "claude-haiku-4-5")])
    result = provider.classify("please add a logout button", Verdict.CHANGE)
    assert result is not None
    assert result.verdict is Verdict.CHANGE


def test_review_cli_classifier_classify_passes_model_arg_and_prompt_through(monkeypatch):
    from tasklib import cli

    seen = {}
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda *a, **k: ResolvedModel("anthropic", "claude:claude-haiku-4-5"),
    )

    def _fake_run(model_arg, prompt):
        seen["model_arg"] = model_arg
        seen["prompt"] = prompt
        return "justAsk"

    monkeypatch.setattr(cli, "_run_review_just_ask", _fake_run)
    provider = ReviewCliClassifier(fallbacks=[("anthropic", "claude-haiku-4-5")])
    result = provider.classify("what does this do?", Verdict.CHANGE)
    assert seen["model_arg"] == "claude:claude-haiku-4-5"
    assert "what does this do?" in seen["prompt"]
    assert result.verdict is Verdict.JUST_ASK


def test_review_cli_classifier_forwards_capability_and_env_to_resolve_chain(monkeypatch):
    seen = {}

    def _spy(fallbacks=None, env=None, *, capability=None):
        seen["fallbacks"] = fallbacks
        seen["env"] = env
        seen["capability"] = capability
        return None

    monkeypatch.setattr("tasklib.classify.resolve_chain", _spy)
    provider = ReviewCliClassifier(
        fallbacks=[("anthropic", "claude-haiku-4-5")], env={"X": "1"}, capability="reasoning"
    )
    assert provider.available() is False
    assert seen["capability"] == "reasoning"
    assert seen["env"] == {"X": "1"}
    assert seen["fallbacks"] == [("anthropic", "claude-haiku-4-5")]


def test_review_cli_classifier_caches_resolve_chain_resolution(monkeypatch):
    calls = []

    def _spy(*a, **k):
        calls.append(1)
        return ResolvedModel("anthropic", "claude:claude-haiku-4-5")

    monkeypatch.setattr("tasklib.classify.resolve_chain", _spy)
    from tasklib import cli

    monkeypatch.setattr(cli, "_run_review_just_ask", lambda model, prompt: "change")
    provider = ReviewCliClassifier(fallbacks=[("anthropic", "claude-haiku-4-5")])
    assert provider.available() is True
    provider.classify("a message", Verdict.CHANGE)
    assert len(calls) == 1  # resolved once, reused — not re-resolved per call


def test_review_cli_classifier_label_reflects_resolved_model_arg(monkeypatch):
    monkeypatch.setattr(
        "tasklib.classify.resolve_chain",
        lambda *a, **k: ResolvedModel("zai", "zai:glm-4.6-flash"),
    )
    provider = ReviewCliClassifier(fallbacks=[("zai", "glm-4.6-flash")])
    provider.available()
    assert provider.label == "zai:glm-4.6-flash"


def test_review_cli_classifier_label_is_none_when_unresolved(monkeypatch):
    monkeypatch.setattr("tasklib.classify.resolve_chain", lambda *a, **k: None)
    provider = ReviewCliClassifier(fallbacks=[])
    provider.available()
    assert provider.label == "none"


# ── build_classifier_chain — grouping laya entries vs review-cli-routed entries ────────────


def test_build_classifier_chain_default_fallbacks_is_a_single_review_cli_group():
    # task-cli#208 review finding: `build_classifier_chain`'s own `None` handling stays
    # pre-#208 (no laya prepend) -- the zero-config "laya first" policy lives entirely in
    # `config.py`'s `DEFAULTS["classify"]["fallbacks"]`, the one real caller (`cli.
    # _classify_one`) never passes bare `None`. See `test_config.py` for the real default.
    providers = build_classifier_chain(None)
    assert len(providers) == 1
    assert isinstance(providers[0], ReviewCliClassifier)
    assert providers[0].fallbacks == list(DEFAULT_FALLBACKS)


def test_build_classifier_chain_no_laya_stays_one_group():
    fallbacks = [("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 1
    assert isinstance(providers[0], ReviewCliClassifier)
    assert providers[0].fallbacks == fallbacks


def test_build_classifier_chain_laya_only():
    providers = build_classifier_chain([("laya", "local")])
    assert len(providers) == 1
    assert isinstance(providers[0], LayaClassifier)


def test_build_classifier_chain_laya_head_then_review_cli_group():
    fallbacks = [("laya", "local"), ("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 2
    assert isinstance(providers[0], LayaClassifier)
    assert isinstance(providers[1], ReviewCliClassifier)
    assert providers[1].fallbacks == [("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")]


def test_build_classifier_chain_interleaved_laya_splits_review_groups():
    fallbacks = [("anthropic", "claude-haiku-4-5"), ("laya", "local"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 3
    assert isinstance(providers[0], ReviewCliClassifier)
    assert providers[0].fallbacks == [("anthropic", "claude-haiku-4-5")]
    assert isinstance(providers[1], LayaClassifier)
    assert isinstance(providers[2], ReviewCliClassifier)
    assert providers[2].fallbacks == [("openai", "gpt-5-mini")]


def test_build_classifier_chain_capability_applied_only_to_first_review_group():
    fallbacks = [("anthropic", "claude-haiku-4-5"), ("laya", "local"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks, capability="reasoning")
    review_groups = [p for p in providers if isinstance(p, ReviewCliClassifier)]
    assert review_groups[0].capability == "reasoning"
    assert review_groups[1].capability is None


def test_build_classifier_chain_forwards_env_to_every_review_cli_group():
    fallbacks = [("laya", "local"), ("anthropic", "claude-haiku-4-5")]
    providers = build_classifier_chain(fallbacks, env={"X": "1"})
    review = next(p for p in providers if isinstance(p, ReviewCliClassifier))
    assert review.env == {"X": "1"}


def test_build_classifier_chain_jev_only():
    providers = build_classifier_chain([("jev", "cloud")])
    assert len(providers) == 1
    assert isinstance(providers[0], JevClassifier)


def test_build_classifier_chain_jev_head_then_review_cli_group():
    fallbacks = [("jev", "cloud"), ("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 2
    assert isinstance(providers[0], JevClassifier)
    assert isinstance(providers[1], ReviewCliClassifier)
    assert providers[1].fallbacks == [("anthropic", "claude-haiku-4-5"), ("openai", "gpt-5-mini")]


def test_build_classifier_chain_laya_and_jev_both_standalone():
    fallbacks = [("laya", "local"), ("jev", "cloud"), ("anthropic", "claude-haiku-4-5")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 3
    assert isinstance(providers[0], LayaClassifier)
    assert isinstance(providers[1], JevClassifier)
    assert isinstance(providers[2], ReviewCliClassifier)


def test_build_classifier_chain_interleaved_jev_splits_review_groups():
    fallbacks = [("anthropic", "claude-haiku-4-5"), ("jev", "cloud"), ("openai", "gpt-5-mini")]
    providers = build_classifier_chain(fallbacks)
    assert len(providers) == 3
    assert isinstance(providers[0], ReviewCliClassifier)
    assert providers[0].fallbacks == [("anthropic", "claude-haiku-4-5")]
    assert isinstance(providers[1], JevClassifier)
    assert isinstance(providers[2], ReviewCliClassifier)
    assert providers[2].fallbacks == [("openai", "gpt-5-mini")]


def test_build_classifier_chain_forwards_env_to_jev():
    providers = build_classifier_chain([("jev", "cloud")], env={"AI_GATEWAY_API_KEY": "x"})
    assert providers[0].env == {"AI_GATEWAY_API_KEY": "x"}


def test_build_classifier_chain_jev_never_honors_a_configured_value():
    # SECURITY (see module docstring): a `{jev: <value>}` entry's value must only decide
    # ROUTING, never which endpoint/model gets called -- both stay fixed regardless of what a
    # (potentially untrusted, repo-scoped) `classify.fallbacks` config names here.
    for configured_value in ("cloud", "default", "", "https://evil.example/v1/evaluate"):
        providers = build_classifier_chain([("jev", configured_value)])
        assert isinstance(providers[0], JevClassifier)
        assert providers[0].endpoint == "https://ai-gateway.vercel.sh/v1/evaluate"
        assert providers[0].model_id == "typesafe-ai/jev"


# ── LayaClassifier — the new task-cli#206 local backend ────────────────────────────────────


def test_build_classifier_chain_laya_never_honors_a_configured_model_id():
    # SECURITY: `classify.fallbacks` can come from a repo-scoped `task.yaml`/`rig.yaml` that
    # `task classify` treats as untrusted (it runs inside the target repo being ticketed) —
    # a `{laya: <value>}` entry's value must only decide ROUTING, never which Hub id loads.
    # A repo attempting to point laya at an attacker-controlled checkpoint must be a no-op.
    for configured_value in ("local", "default", "", "attacker/malicious-model", "convaiinnovations/laya-large"):
        providers = build_classifier_chain([("laya", configured_value)])
        assert len(providers) == 1
        assert providers[0].model_id == "convaiinnovations/laya"


def test_laya_priority_question_criteria_are_observable_impact_not_abstract_urgency():
    # Regression guard for task-cli#206's calibration fix: `criteria` must stay phrased as
    # concrete observable impact, not the old abstract urgency scale that showed zero
    # discrimination on real tickets (see the CALIBRATION comment above `_CLASSIFICATION_QUESTIONS`
    # in tasklib/classifiers.py for the full before/after evidence).
    priority = _CLASSIFICATION_QUESTIONS["priority"]
    assert set(priority["criteria"]) == {"P0", "P1", "P2", "P3"}  # mapping contract unchanged

    combined = " ".join(priority["criteria"].values()).lower()
    # the old abstract urgency-tier framing this replaced -- must be gone. Pins the STALE
    # framing's absence (a real regression guard), not the current wording verbatim (which
    # is free to keep being tuned without editing this test).
    for stale in ("drop everything", "should be picked up soon", "normal priority", "no rush", "low priority"):
        assert stale not in combined
    # `\burgent\w*\b` matches "urgent"/"urgently" (the old tier-label vocabulary) but NOT
    # "urgency" (no "t") -- P2's "no special urgency" is a negated description of the
    # residual/default bucket, not a resurrected P0-P3 label, so it's deliberately allowed.
    assert not re.search(r"\burgent\w*\b", combined)
    assert not re.search(r"\burgent\w*\b", priority["instructions"].lower())


def test_laya_classifier_available_true_when_package_importable(monkeypatch):
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "laya" else None
    )
    assert LayaClassifier().available() is True


def test_laya_classifier_available_false_when_package_missing_and_autoinstall_declines(monkeypatch):
    # `laya_autoinstall.ensure_installed` is mocked here (never a real subprocess/network call
    # in the hermetic suite) -- returning `False` (cooldown-skipped or a failed attempt) means
    # `available()` must still resolve to `False`, exactly like the pre-#208 behavior.
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr("tasklib.laya_autoinstall.ensure_installed", lambda **k: False)
    assert LayaClassifier().available() is False


def test_laya_classifier_available_attempts_autoinstall_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(
        "tasklib.laya_autoinstall.ensure_installed", lambda **k: calls.append(1) or False
    )
    LayaClassifier().available()
    assert calls == [1]


def test_laya_classifier_available_true_after_successful_autoinstall(monkeypatch):
    # `ensure_installed()` itself re-verifies importability (see its own docstring) before
    # returning `True` -- `available()` trusts that return value directly, with no second
    # `find_spec` probe of its own (same-invocation recovery, no re-exec, no double-check).
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr("tasklib.laya_autoinstall.ensure_installed", lambda **k: True)
    assert LayaClassifier().available() is True


def test_laya_classifier_available_only_attempts_autoinstall_once_per_instance(monkeypatch):
    # Two `available()` calls on the SAME instance (still missing both times) must only ever
    # trigger ONE auto-install attempt -- the "one time per invocation" requirement.
    attempts = []
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(
        "tasklib.laya_autoinstall.ensure_installed", lambda **k: attempts.append(1) or False
    )
    provider = LayaClassifier()
    assert provider.available() is False
    assert provider.available() is False
    assert attempts == [1]


def test_laya_classifier_available_broken_install_never_triggers_autoinstall(monkeypatch):
    # A `find_spec` raise (corrupted/partial install) must NOT trigger the auto-installer --
    # that's a different, weirder failure than "never installed" (see `available()`'s docstring).
    calls = []
    monkeypatch.setattr(
        "tasklib.laya_autoinstall.ensure_installed", lambda **k: calls.append(1) or True
    )

    def _boom(name):
        raise ImportError("simulated broken install metadata")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert LayaClassifier().available() is False
    assert calls == []


def test_laya_classifier_available_never_raises_even_if_find_spec_does(monkeypatch):
    # `find_spec` is documented to be able to raise on a broken/partial install rather than
    # just returning `None` -- `available()` must still keep its own "never raises" promise.
    def _boom(name):
        raise ImportError("simulated broken install metadata")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert LayaClassifier().available() is False


def test_laya_classifier_never_imports_laya_at_module_top():
    # Stdlib-only-at-import-time is a hard repo rule (AGENTS.md) — `laya` (a heavy ML dep) must
    # only ever be imported lazily, inside the function that needs it.
    import ast
    import inspect

    from tasklib import classifiers

    tree = ast.parse(inspect.getsource(classifiers))
    top_level_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = set()
    for node in top_level_imports:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        else:
            if node.module:
                names.add(node.module.split(".")[0])
    assert "laya" not in names


def test_laya_classifier_classify_maps_real_observed_result_shape():
    provider = LayaClassifier()
    provider._load_agent = lambda: _FakeAgent(_REAL_LAYA_RAW_RESULT)
    result = provider.classify("fix the login page crash on mobile Safari", Verdict.CHANGE)
    assert result == Classification(
        verdict=Verdict.JUST_ASK,
        process_or_product="product",
        priority="P1",
        story_points=3,
    )


def test_laya_classifier_classify_sends_the_real_question_schema_to_predict():
    # `classify()` must hand the real, live `_CLASSIFICATION_QUESTIONS` schema (the calibrated
    # wording) to `predict()` at the call site -- not a filtered/rebuilt substitute. Prior
    # tests only covered `_CLASSIFICATION_QUESTIONS`'s content and `_map_result`'s handling of a
    # canned raw reply; this pins the delivery step between them. Equality (not identity) is
    # asserted so a defensive copy at the call site still passes -- only content drift fails.
    seen = {}

    class _RecordingAgent:
        def predict(self, state, questions):
            seen["state"] = state
            seen["questions"] = questions
            return _REAL_LAYA_RAW_RESULT

    provider = LayaClassifier()
    provider._load_agent = lambda: _RecordingAgent()
    provider.classify("fix the login page crash on mobile Safari", Verdict.CHANGE)
    assert seen["questions"] == _CLASSIFICATION_QUESTIONS
    assert seen["state"] == "fix the login page crash on mobile Safari"


def test_laya_classifier_classify_defaults_axis_on_malformed_choice():
    raw = {
        "answers": {
            "verdict": {"type": "choice", "choice": "not-a-real-verdict"},
            "process_or_product": {"type": "choice", "choice": "nonsense"},
            "priority": {"type": "choice", "choice": "P9"},
            "story_points": {"type": "choice", "choice": "not-a-number"},
        }
    }
    provider = LayaClassifier()
    provider._load_agent = lambda: _FakeAgent(raw)
    result = provider.classify("hello", Verdict.JUST_ASK)
    # every axis independently falls back to its own safe default (mirrors
    # `classify.default_classification` / `parse_classification`'s per-axis philosophy)
    assert result == Classification(
        verdict=Verdict.JUST_ASK,  # bias, since "not-a-real-verdict" isn't a Verdict
        process_or_product="product",
        priority="P2",
        story_points=3,
    )


def test_laya_classifier_classify_defaults_entirely_on_missing_answers_key():
    provider = LayaClassifier()
    provider._load_agent = lambda: _FakeAgent({"model": "laya-rl-agent"})
    result = provider.classify("hello", Verdict.CHANGE)
    assert result is None


def test_laya_classifier_classify_returns_none_on_predict_exception():
    provider = LayaClassifier()

    class _Boom:
        def predict(self, *a, **k):
            raise RuntimeError("model runtime error")

    provider._load_agent = lambda: _Boom()
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_laya_classifier_classify_returns_none_when_model_load_fails(monkeypatch):
    provider = LayaClassifier()

    def _boom():
        raise OSError("no network egress to huggingface.co")

    provider._load_agent = _boom
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_laya_classifier_caches_loaded_agent_across_calls(fake_laya_module):
    # `laya.load()` downloads/loads real model weights -- it must only run ONCE per instance,
    # not once per `classify()` call. Stub the `laya` module itself (via `sys.modules`) so the
    # REAL, unpatched `_load_agent` caching path is what's under test.
    provider = LayaClassifier()
    real_loads = {"n": 0}

    def _load(model_id_or_path):
        real_loads["n"] += 1
        return _FakeAgent(_REAL_LAYA_RAW_RESULT)

    fake_laya_module(_load)
    provider.classify("one", Verdict.CHANGE)
    provider.classify("two", Verdict.CHANGE)
    assert real_loads["n"] == 1  # laya.load() only called once, agent cached on the instance


def test_laya_classifier_refuses_to_load_a_cwd_shadowing_local_path(
    tmp_path, monkeypatch, fake_laya_module
):
    # SECURITY: `laya.load()`'s `model_id_or_path` treats an id that already exists as a path
    # relative to cwd as a LOCAL directory, not a Hub repo. `task classify` runs inside the
    # target/foreign repo being operated on, so a planted `convaiinnovations/laya/` directory
    # there must never be silently loaded in place of the real Hub-hosted checkpoint.
    monkeypatch.chdir(tmp_path)
    shadow_dir = tmp_path / "convaiinnovations" / "laya"
    shadow_dir.mkdir(parents=True)

    def _must_not_be_called(*a, **k):
        raise AssertionError("laya.load() must never run against a cwd-shadowed path")

    fake_laya_module(_must_not_be_called)
    provider = LayaClassifier()  # default model_id == "convaiinnovations/laya"
    result = provider.classify("hello", Verdict.CHANGE)
    assert result is None  # refused, fell through gracefully — never crashed, never loaded


def test_laya_classifier_prints_diagnostic_to_stderr_on_runtime_failure(capsys):
    # A runtime failure must be OBSERVABLE, not silently swallowed — otherwise a permanently
    # broken local laya install degrades forever with no signal that laya is the thing failing.
    provider = LayaClassifier()

    class _Boom:
        def predict(self, *a, **k):
            raise RuntimeError("simulated model runtime error")

    provider._load_agent = lambda: _Boom()
    result = provider.classify("hello", Verdict.CHANGE)
    assert result is None
    err = capsys.readouterr().err
    assert "laya" in err
    assert "simulated model runtime error" in err


def test_laya_classifier_collapses_newlines_with_a_real_separator_on_failure(capsys):
    # `safe_display` drops `\n` outright (it isn't printable) -- the collapse to a single
    # space MUST happen BEFORE `safe_display`/`redact`, or a multi-line exception message
    # gets its lines silently fused with no separator.
    provider = LayaClassifier()

    class _Boom:
        def predict(self, *a, **k):
            raise RuntimeError("line one\nline two")

    provider._load_agent = lambda: _Boom()
    assert provider.classify("hello", Verdict.CHANGE) is None
    err = capsys.readouterr().err
    assert "line one line two" in err
    assert "line onel" not in err


def test_laya_classifier_diagnostic_redacts_secrets_and_strips_control_chars(capsys):
    # SECURITY: `message` is untrusted inbound text (tg-cli/agent). A tokenizer/parse error
    # commonly echoes its input verbatim -- a token-shaped secret or a terminal/bidi control
    # sequence embedded in the exception text must never reach stderr unredacted/raw.
    provider = LayaClassifier()

    class _Boom:
        def predict(self, *a, **k):
            # synthetic fixture value, never a real credential; shaped like a GitHub PAT
            # only so `redact()`'s token-shaped-secret rule actually fires on it.
            raise ValueError("bad input: ghp_1234567890abcdef1234567890abcdef1234\nnext\x1b[31m")  # gitleaks:allow

    provider._load_agent = lambda: _Boom()
    assert provider.classify("hello", Verdict.CHANGE) is None
    err = capsys.readouterr().err
    assert "ghp_1234567890abcdef1234567890abcdef1234" not in err  # gitleaks:allow
    assert "\x1b" not in err
    assert "\n" not in err.strip("\n")



def test_laya_classifier_classify_catches_map_result_exceptions_too():
    # `classify()`'s try/except must cover the ENTIRE pipeline (load + predict + map), not
    # just the `laya` calls -- a bug or adversarial return value in `_map_result` must degrade
    # to `None` (fall through) exactly like a `predict()`/load failure, never propagate.
    provider = LayaClassifier()
    provider._load_agent = lambda: _FakeAgent(_REAL_LAYA_RAW_RESULT)
    provider._map_result = lambda result, bias: (_ for _ in ()).throw(
        RuntimeError("simulated mapping bug")
    )
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_laya_classifier_label_includes_model_id():
    assert LayaClassifier().label == "laya:convaiinnovations/laya"


class _FakeAgent:
    def __init__(self, result):
        self._result = result

    def predict(self, state, questions):
        return self._result


# ── JevClassifier — the new task-cli#217 cloud backend (Vercel AI Gateway) ─────────────────


def test_read_api_key_from_env_file_parses_line(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AI_GATEWAY_API_KEY=plain-value\n", encoding="utf-8")
    assert classifiers._read_api_key_from_env_file(str(p)) == "plain-value"


def test_read_api_key_from_env_file_strips_double_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('AI_GATEWAY_API_KEY="double-quoted"\n', encoding="utf-8")
    assert classifiers._read_api_key_from_env_file(str(p)) == "double-quoted"


def test_read_api_key_from_env_file_strips_single_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text("AI_GATEWAY_API_KEY='single-quoted'\n", encoding="utf-8")
    assert classifiers._read_api_key_from_env_file(str(p)) == "single-quoted"


def test_read_api_key_from_env_file_ignores_other_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("SOME_OTHER_KEY=nope\nAI_GATEWAY_API_KEY=real\nTHIRD=x\n", encoding="utf-8")
    assert classifiers._read_api_key_from_env_file(str(p)) == "real"


def test_read_api_key_from_env_file_returns_none_when_file_missing(tmp_path):
    assert classifiers._read_api_key_from_env_file(str(tmp_path / "nope.env")) is None


def test_read_api_key_from_env_file_returns_none_when_line_absent(tmp_path):
    p = tmp_path / ".env"
    p.write_text("SOME_OTHER_KEY=nope\n", encoding="utf-8")
    assert classifiers._read_api_key_from_env_file(str(p)) is None


def test_jev_classifier_available_true_with_env_var_key():
    assert JevClassifier(env={"AI_GATEWAY_API_KEY": "real-key"}).available() is True


def test_jev_classifier_available_false_without_key_or_env_file(monkeypatch):
    monkeypatch.delenv("TASK_JEV_ENV_FILE", raising=False)
    assert JevClassifier(env={}).available() is False


def test_jev_classifier_explicit_env_never_reads_env_file_even_if_present(tmp_path, monkeypatch):
    # Review finding (hermeticity): an explicit `env=` dict must be authoritative -- it must
    # NEVER fall through to the machine-local `.env` file, even when that file exists and
    # holds a real key. Otherwise an `env={}` meant to simulate "no credentials" (every test
    # in this module, and any embedding caller) would silently become non-hermetic on this
    # one machine.
    env_file = tmp_path / "shared.env"
    env_file.write_text("AI_GATEWAY_API_KEY=from-file-key\n", encoding="utf-8")
    monkeypatch.setenv("TASK_JEV_ENV_FILE", str(env_file))
    provider = JevClassifier(env={})
    assert provider.available() is False
    assert provider._resolve_api_key() is None


def test_jev_classifier_falls_back_to_env_file_when_env_var_absent(tmp_path, monkeypatch):
    # `env=None` (the default) means "use the real process environment" -- ONLY then does the
    # `.env` file fallback apply, and only when `$TASK_JEV_ENV_FILE` itself is set (opt-in).
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    env_file = tmp_path / "shared.env"
    env_file.write_text("AI_GATEWAY_API_KEY=from-file-key\n", encoding="utf-8")
    monkeypatch.setenv("TASK_JEV_ENV_FILE", str(env_file))
    provider = JevClassifier()
    assert provider.available() is True
    assert provider._resolve_api_key() == "from-file-key"


def test_jev_classifier_env_var_takes_precedence_over_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "shared.env"
    env_file.write_text("AI_GATEWAY_API_KEY=from-file-key\n", encoding="utf-8")
    monkeypatch.setenv("TASK_JEV_ENV_FILE", str(env_file))
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "from-env-key")
    provider = JevClassifier()
    assert provider._resolve_api_key() == "from-env-key"


def test_jev_classifier_resolves_api_key_only_once(monkeypatch):
    # Mirrors `test_review_cli_classifier_caches_resolve_chain_resolution` -- resolution is
    # cached on the instance after the first `available()`/`classify()` call.
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("TASK_JEV_ENV_FILE", "/irrelevant/path.env")
    calls = {"n": 0}

    def _fake_read(path):
        calls["n"] += 1
        return "file-key"

    monkeypatch.setattr(classifiers, "_read_api_key_from_env_file", _fake_read)
    provider = JevClassifier()
    assert provider.available() is True
    assert provider.available() is True
    assert calls["n"] == 1


def test_jev_classifier_classify_builds_correct_request(monkeypatch):
    captured = {}

    def _fake_request_json(url, *, method, headers, payload, timeout):
        captured["url"] = url
        captured["method"] = method
        captured["headers"] = headers
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {"answers": {}}

    monkeypatch.setattr("tasklib.backends.http.request_json", _fake_request_json)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    provider.classify("add rate limiting to the API", Verdict.CHANGE)

    assert captured["url"] == "https://ai-gateway.vercel.sh/v1/evaluate"
    assert captured["method"] == "POST"
    assert captured["headers"] == {"Authorization": "Bearer placeholder-key-value"}
    assert captured["timeout"] == classifiers._JEV_TIMEOUT_SECONDS
    assert captured["payload"]["model"] == "typesafe-ai/jev"
    assert captured["payload"]["state"] == "add rate limiting to the API"
    assert captured["payload"]["questions"] == classifiers._CLASSIFICATION_QUESTIONS
    for question in captured["payload"]["questions"].values():
        assert question["type"] == "choice"


def test_jev_classifier_classify_maps_choice_response(monkeypatch):
    raw = {
        "model": "typesafe-ai/jev",
        "answers": {
            "verdict": {
                "type": "choice",
                "choice": "change",
                "probabilities": {"change": 0.9, "actionable": 0.05, "justAsk": 0.05},
            },
            "process_or_product": {
                "type": "choice",
                "choice": "product",
                "probabilities": {"process": 0.1, "product": 0.9},
            },
            "priority": {
                "type": "choice",
                "choice": "P1",
                "probabilities": {"P0": 0.1, "P1": 0.7, "P2": 0.15, "P3": 0.05},
            },
            "story_points": {
                "type": "choice",
                "choice": "5",
                "probabilities": {"1": 0, "2": 0, "3": 0.1, "5": 0.8, "8": 0.1, "13": 0},
            },
        },
        "usage": {"inputTokens": 42, "outputTokens": 7},
    }
    monkeypatch.setattr("tasklib.backends.http.request_json", lambda *a, **k: raw)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    result = provider.classify("add rate limiting", Verdict.CHANGE)
    assert result == Classification(
        verdict=Verdict.CHANGE, process_or_product="product", priority="P1", story_points=5
    )


def test_jev_classifier_classify_defaults_axis_on_malformed_choice(monkeypatch):
    raw = {
        "answers": {
            "verdict": {"type": "choice", "choice": "change"},
            "process_or_product": {"type": "choice", "choice": "not-a-real-value"},
            "priority": {"type": "choice", "choice": "P1"},
            "story_points": {"type": "choice", "choice": "not-a-number"},
        }
    }
    monkeypatch.setattr("tasklib.backends.http.request_json", lambda *a, **k: raw)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    result = provider.classify("hello", Verdict.CHANGE)
    assert result == Classification(
        verdict=Verdict.CHANGE, process_or_product="product", priority="P1", story_points=3
    )


def test_jev_classifier_classify_returns_none_on_missing_answers_key(monkeypatch):
    monkeypatch.setattr(
        "tasklib.backends.http.request_json", lambda *a, **k: {"model": "typesafe-ai/jev"}
    )
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_jev_classifier_classify_returns_none_on_non_dict_response(monkeypatch):
    monkeypatch.setattr("tasklib.backends.http.request_json", lambda *a, **k: "not a dict")
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_jev_classifier_classify_returns_none_without_a_key():
    provider = JevClassifier(env={})
    assert provider.available() is False
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_jev_classifier_classify_falls_through_on_billing_block(monkeypatch, capsys):
    # The REAL, live-verified Vercel AI Gateway billing-block body (task-cli#217
    # live-verification, captured verbatim against the real endpoint with a real, valid key on
    # this account -- which currently has no card on file): the error object's discriminator
    # field is `type`, NOT `code` as this ticket's own spec described up front. Must degrade
    # EXACTLY like every other failure -- never a crash, and the chain must fall through to
    # the next configured provider.
    from tasklib.backends.http import HttpError

    body = (
        '{"error":{"message":"AI Gateway requires a valid credit card on file to service '
        'requests. Please visit https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai%3Fmodal%3D'
        'add-credit-card to add a card and unlock your free credits.","type":'
        '"customer_verification_required"}}'
    )

    def _raise(*a, **k):
        raise HttpError(403, "HTTP 403 for POST https://ai-gateway.vercel.sh/v1/evaluate", body)

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None
    err = capsys.readouterr().err
    assert "jev classifier failed" in err
    assert "customer_verification_required" in err


def test_jev_classifier_classify_billing_block_code_field_fallback(monkeypatch, capsys):
    # `code` (this ticket's originally-described field name) is a tolerated fallback for
    # whenever a future/different Gateway response uses it instead of the real, live-verified
    # `type` field.
    from tasklib.backends.http import HttpError

    body = '{"error":{"code":"customer_verification_required","message":"no card on file"}}'

    def _raise(*a, **k):
        raise HttpError(403, "HTTP 403 for POST https://ai-gateway.vercel.sh/v1/evaluate", body)

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None
    assert "customer_verification_required" in capsys.readouterr().err


def test_jev_classifier_classify_tolerates_non_string_error_body(monkeypatch):
    # `getattr(exc, "body", "")` duck-types on ANY exception -- a hypothetical exception whose
    # `.body` is not a `str` (e.g. a dict) must never make `json.loads` raise a `TypeError`
    # that escapes `classify()`'s own "never raises" contract.
    class _WeirdError(Exception):
        body = {"already": "parsed"}

    def _raise(*a, **k):
        raise _WeirdError("weird failure")

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_jev_classifier_classify_returns_none_on_generic_http_error(monkeypatch, capsys):
    from tasklib.backends.http import HttpError

    def _raise(*a, **k):
        raise HttpError(500, "HTTP 500 for POST https://ai-gateway.vercel.sh/v1/evaluate", "")

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None
    assert "jev classifier failed" in capsys.readouterr().err


def test_jev_classifier_classify_returns_none_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "placeholder-key-value"})
    assert provider.classify("hello", Verdict.CHANGE) is None


def test_jev_classifier_never_logs_api_key_on_failure(monkeypatch, capsys):
    # SECURITY: belt-and-braces guard -- even an exception shape that (hypothetically) echoes
    # the key back must never leak it to stderr.
    secret = "YOUR_JEV_KEY_HERE_0123456789_never_should_leak"

    def _raise(*a, **k):
        raise RuntimeError(f"simulated failure embedding the key {secret} in its message")

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": secret})
    assert provider.classify("hello", Verdict.CHANGE) is None
    assert secret not in capsys.readouterr().err


def test_jev_classifier_collapses_newlines_with_a_real_separator_on_failure(monkeypatch, capsys):
    # `safe_display` drops `\n` outright (it isn't printable) -- the collapse to a single
    # space MUST happen BEFORE `safe_display`/`redact`, or a multi-line exception message
    # gets its lines silently fused with no separator (e.g. "line onecustomer_verification").
    def _raise(*a, **k):
        raise RuntimeError("line one\nline two")

    monkeypatch.setattr("tasklib.backends.http.request_json", _raise)
    provider = JevClassifier(env={"AI_GATEWAY_API_KEY": "k"})
    assert provider.classify("hello", Verdict.CHANGE) is None
    err = capsys.readouterr().err
    assert "line one line two" in err
    assert "line onel" not in err


def test_jev_classifier_label():
    assert JevClassifier().label == "jev:typesafe-ai/jev"
