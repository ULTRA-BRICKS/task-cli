"""tasklib.classifiers — the pluggable ClassifierProvider architecture (task-cli#206).

Covers `ReviewCliClassifier` (the pre-#206 `review just-ask` shell-out path, refactored in
place — behavior-preserving, so `tests/test_cli.py`'s existing `tasklib.classify.resolve_chain`
/ `cli._run_review_just_ask` monkeypatches need zero changes), `LayaClassifier` (the new local
`laya`-package backend, mocked at its documented `laya.load(...).predict(state, questions) ->
dict` boundary — see this module's docstrings for the REAL raw shape observed live), and
`build_classifier_chain` (turns a `classify.fallbacks` list into an ordered provider chain).
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from tasklib.classify import Classification, ResolvedModel, Verdict
from tasklib.classifiers import (
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
# only the message/criteria differ from the shipped `_LAYA_QUESTIONS` schema in shape, not in
# envelope). `answers[<question>]` for a `choice`-typed question carries `choice` (the winning
# label) plus `probabilities`/`confidence`; `noul`-typed questions carry a `noul` float instead.
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
    providers = build_classifier_chain(None)
    assert len(providers) == 1
    assert isinstance(providers[0], ReviewCliClassifier)


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


def test_laya_classifier_available_true_when_package_importable(monkeypatch):
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: object() if name == "laya" else None
    )
    assert LayaClassifier().available() is True


def test_laya_classifier_available_false_when_package_missing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert LayaClassifier().available() is False


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
