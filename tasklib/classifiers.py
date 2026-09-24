"""Pluggable classifier-provider architecture (task-cli#206).

`_classify_one` (cli.py) used to hardcode ONE resolution path: `classify.resolve_chain`
(walk a per-provider fallback chain, pick the first available LLM provider by env-key or
daemon-on-PATH) then shell out to `review just-ask` and parse its four-line answer. This
module generalizes that into a small `ClassifierProvider` protocol with two providers of the
identical shape:

  - `ReviewCliClassifier` — the ORIGINAL path above, refactored in place, unchanged behavior.
    `resolve_chain` (classify.py) and `_run_review_just_ask` (cli.py) are looked up on their
    OWNING module at call time, never imported at this module's top or cached at class-
    definition time — so every pre-existing `monkeypatch.setattr(tasklib.classify,
    "resolve_chain", ...)` / `monkeypatch.setattr(cli, "_run_review_just_ask", ...)` in
    `tests/test_cli.py` keeps working completely unchanged.
  - `LayaClassifier` — Convai Innovations' free, local, Apache-2.0 "System 1" decision model
    (PyPI `laya`), an open-weights alternative to the paid cloud "Jev" model. Lazily imports
    `laya` (never at this module's top, and never a hard runtime dependency — see
    `pyproject.toml`'s `laya` extra) and never raises out of `available()`/`classify()`: the
    package not being installed, the first-run HuggingFace model download failing, or the
    `predict()` call itself raising all make this provider report itself unavailable / return
    `None`, so `build_classifier_chain`'s caller (`cli._classify_one`) falls through to the
    NEXT configured provider — never a crash, never a traceback. task-cli#208: `available()`
    auto-installs the `laya` package the first time it's found genuinely not importable (once
    per instance, cooldown-cached on failure — see `tasklib.laya_autoinstall`'s module
    docstring) and, on success, re-checks importability in the SAME call so a fresh checkout
    with no `laya` extra installed still classifies with it on the very first `task classify`.

task-cli#208: `laya` is now the zero-config DEFAULT `classify.fallbacks` head. That policy
lives entirely in `config.py`'s `DEFAULTS["classify"]["fallbacks"]` (which now lists `laya`
first) -- NOT in `build_classifier_chain` itself, which keeps its pre-#208 grouping-only
contract unchanged (see its own docstring). An explicit, repo-configured `classify.fallbacks`
(even one that omits `laya`) is never altered by either mechanism.

`build_classifier_chain` turns a `classify.fallbacks` config list (e.g.
`[{"laya": "local"}, {"anthropic": "claude-haiku-4-5"}, ...]`) into an ordered list of
`ClassifierProvider` instances: each `laya` entry becomes its own `LayaClassifier`;
consecutive non-laya entries are grouped into ONE `ReviewCliClassifier` so the manifest
`capability` head and `resolve_chain`'s own availability-walk apply to that group exactly as
they did before this ticket. `capability` is applied to only the FIRST such group.

SECURITY (the single canonical explanation — every other reference in this module, README,
and AGENTS.md points back here): `classify.fallbacks` can originate from a REPO-scoped
`task.yaml`/`rig.yaml`, and `task classify` runs inside the target/foreign repo it is
ticketing — that config is not necessarily trusted the way a machine-global config is.
`LayaClassifier` closes two attack surfaces on the model it loads:
  1. A `{laya: <value>}` entry's value only decides ROUTING (does this entry become a
     `LayaClassifier`?), never WHICH Hub id gets loaded — `model_id` always resolves to the
     fixed `_LAYA_DEFAULT_MODEL_ID`. Letting a repo-scoped value pick an arbitrary Hub id
     would let a malicious checkout make `task classify` download and run attacker-controlled
     model code in the same process that holds `$GITHUB_TOKEN`/`$LINEAR_API_KEY`.
  2. `laya.load()`'s underlying `model_id_or_path` resolution treats an id that already
     exists as a path relative to the CURRENT WORKING DIRECTORY as a local model directory,
     not a Hub repo — `_load_agent` refuses to load whenever that happens, so a planted
     `convaiinnovations/laya/` directory in the target repo cannot shadow the real,
     Hub-hosted checkpoint (the same cwd-shadowing class of attack `bin/task` already guards
     against for Python imports, by never inserting cwd onto `sys.path`).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .classify import Classification, ResolvedModel, Verdict


class ClassifierProvider(Protocol):
    """One classifier backend: a reachability check plus the actual classify call.

    `classify()` is only ever invoked after `available()` returned `True` for the SAME
    provider instance (see `build_classifier_chain`'s callers) — but it still returns `None`
    (never raises) rather than a `Classification` when the provider turns out to fail at
    RUNTIME despite looking reachable (a `laya` `predict()` error, a model download that
    completed at import-check time but fails on first use). Callers fall through to the next
    provider in the chain on `None`, exactly the "graceful degradation" contract task-cli#206
    asks for. `ReviewCliClassifier` never returns `None` once available — its underlying
    `parse_classification` already defends a malformed/empty model reply with its own
    per-axis safe defaults, so that pre-existing single-shot behavior is preserved unchanged.

    `available()` is USUALLY a cheap, side-effect-free reachability probe (an env-key check,
    `shutil.which`, `find_spec`) — the one exception is `LayaClassifier.available()`
    (task-cli#208), which may run a real `pip install` (bounded, at most once per instance)
    the first time it finds the package genuinely not installed. Never raises either way.
    """

    def available(self) -> bool: ...

    def classify(self, message: str, bias: "Verdict") -> "Classification | None": ...

    @property
    def label(self) -> str: ...


@dataclass
class ReviewCliClassifier:
    """The pre-task-cli#206 `review just-ask` shell-out path, unchanged, as one provider.

    Wraps `classify.resolve_chain` (pick the first available LLM provider in `fallbacks`,
    optionally preferring a manifest `capability` head) + `cli._run_review_just_ask` (the
    actual subprocess shell-out) + `classify.parse_classification` (read the four-axis
    answer, defaulting per-axis on a malformed/empty reply). The resolution is cached after
    the first `available()`/`classify()` call on this instance (mirrors the original code,
    which called `resolve_chain` exactly once per `_classify_one` invocation) — a fresh
    `ReviewCliClassifier` is expected per `task classify` invocation, not reused across many.

    Note: unlike `LayaClassifier`, a `_run_review_just_ask` failure is NOT caught here — it
    already returns `""` (never raises) for its own subprocess errors, which
    `parse_classification` degrades to the bias default, matching the pre-#206 single-shot
    behavior exactly. This provider therefore never returns `None` once `available()` is
    `True`, so it never itself triggers a fall-through to a later provider in the chain.
    """

    fallbacks: list[tuple[str, str]] | None
    env: dict[str, str] | None = None
    capability: str | None = None
    _resolved: "ResolvedModel | None" = field(default=None, init=False, repr=False, compare=False)
    _checked: bool = field(default=False, init=False, repr=False, compare=False)

    def _resolve(self) -> "ResolvedModel | None":
        if not self._checked:
            from .classify import resolve_chain

            self._resolved = resolve_chain(self.fallbacks, self.env, capability=self.capability)
            self._checked = True
        return self._resolved

    def available(self) -> bool:
        return self._resolve() is not None

    def classify(self, message: str, bias: "Verdict") -> "Classification | None":
        resolved = self._resolve()
        if resolved is None:
            return None
        from . import cli
        from .classify import build_prompt, parse_classification

        output = cli._run_review_just_ask(resolved.model_arg, build_prompt(message))
        return parse_classification(output, bias=bias, message=message)

    @property
    def label(self) -> str:
        resolved = self._resolved
        return resolved.model_arg if resolved is not None else "none"


# The `laya` question schema (matches `CLASSIFY_PROMPT`'s wording/tone). All four axes are
# modeled as `choice` questions -- `laya`'s real, live-verified `predict()` return shape
# gives each `choice` question a `choice: <winning label>` field, which this module reads
# back off `criteria`'s own string keys (see `LayaClassifier._map_result`).
_LAYA_QUESTIONS: dict[str, dict] = {
    "verdict": {
        "type": "choice",
        "instructions": "Classify this message to a dev agent as exactly one category.",
        "criteria": {
            "change": (
                "a genuine change/edit request -- should result in a code/doc/config edit "
                "and needs a tracked ticket"
            ),
            "actionable": (
                "an instruction to directly execute an existing command/tool right now "
                "(e.g. run a code review, run the tests, ship a PR) -- not a question, not "
                "a request to edit anything"
            ),
            "justAsk": "a pure question, no edit and nothing to execute",
        },
    },
    "process_or_product": {
        "type": "choice",
        "instructions": (
            "Is the underlying work a session-local/agent process step, or durable product "
            "work?"
        ),
        "criteria": {
            "process": (
                "a session-local/agent process step (e.g. reviewing an uncommitted diff, "
                "running a pre-commit checklist)"
            ),
            "product": (
                "durable product work (a feature, bug fix, or other change that should be "
                "tracked long-term)"
            ),
        },
    },
    "priority": {
        "type": "choice",
        "instructions": "How urgently should this work be picked up?",
        "criteria": {
            "P0": "drop everything -- critical, blocking",
            "P1": "urgent -- should be picked up soon",
            "P2": "normal priority",
            "P3": "low priority, no rush",
        },
    },
    "story_points": {
        "type": "choice",
        "instructions": (
            "A Fibonacci-ish complexity estimate for the work (a complexity estimate, not a "
            "time estimate)."
        ),
        "criteria": {
            "1": "trivial / typo-level (a one-line copy fix, a renamed constant)",
            "2": (
                "small, single-function fix (a bounded bug in one function, no new tests "
                "needed)"
            ),
            "3": (
                "moderate, single-file change (a small new feature in one module, a couple "
                "of tests)"
            ),
            "5": "substantial: a multi-file change, or one that needs new test infrastructure",
            "8": (
                "large: a new subsystem, or design-heavy (several approaches to weigh, "
                "cross-module)"
            ),
            "13": (
                "epic: a major feature built from scratch, likely to itself need breaking "
                "into smaller tickets before work starts"
            ),
        },
    },
}

# The ONLY model id `LayaClassifier` ever loads (see the module docstring's SECURITY note —
# a `classify.fallbacks` value is not honored as a Hub id override).
_LAYA_DEFAULT_MODEL_ID = "convaiinnovations/laya"


@dataclass
class LayaClassifier:
    """Convai Innovations' free, local, Apache-2.0 laya "System 1" decision model (PyPI
    `laya`) -- an open-weights alternative to the paid cloud "Jev" model.

    Lazily imports `laya` (never at this module's top, and never a hard runtime dependency —
    see `pyproject.toml`'s `laya` extra) and never raises out of `available()`/`classify()`:
    the package not being installed, the first-run HuggingFace model download failing (no
    network, disk full, rate-limited), or the `predict()`/result-mapping call itself raising
    all make this provider report itself unavailable / return `None` from `classify()`, so
    `build_classifier_chain`'s caller falls through to the NEXT configured provider — never a
    crash, never a traceback. A runtime failure also prints one diagnostic line to stderr so a
    persistently broken local install is observable rather than silently masked by whichever
    provider ends up producing the eventual classification.

    The loaded `Agent` is cached on the instance (`laya.load()` downloads/loads real model
    weights -- on the order of ~400M params for the English checkpoint -- so it must only
    happen once per instance, not once per `classify()` call). `model_id` always resolves to
    `_LAYA_DEFAULT_MODEL_ID` — see the module docstring's SECURITY note.
    """

    model_id: str = _LAYA_DEFAULT_MODEL_ID
    _agent: object | None = field(default=None, init=False, repr=False, compare=False)
    # task-cli#208: at most one auto-install attempt per INSTANCE (`build_classifier_chain`
    # mints a fresh `LayaClassifier` per `task classify` invocation), matching the "one time
    # per invocation" requirement without needing a separate call counter.
    _autoinstall_attempted: bool = field(default=False, init=False, repr=False, compare=False)

    def available(self) -> bool:
        try:
            found = importlib.util.find_spec("laya") is not None
        except Exception:
            # `find_spec` is documented to be able to raise on a broken/partial install
            # (corrupted `.pth` entries, malformed namespace-package metadata) rather than
            # just returning `None` -- treat that the same as "not installed" so `available()`
            # keeps its own "never raises" promise, matching `classify()`. Deliberately NOT
            # routed into the auto-installer below: a broken/partial install is a different,
            # weirder failure than "never installed", and blindly reinstalling over it risks
            # masking the real problem rather than fixing it.
            return False
        if found:
            return True
        if self._autoinstall_attempted:
            return False
        self._autoinstall_attempted = True
        # task-cli#208: laya is now the zero-config DEFAULT provider (see `build_classifier_
        # chain`), so a fresh checkout with the `laya` extra never installed must still get to
        # use it on the very first `task classify` -- attempt exactly once here, specifically
        # on the "package genuinely not importable" path (a model-load/predict failure is a
        # SEPARATE runtime path, handled entirely inside `classify()`, and never reaches here).
        # `ensure_installed` already re-verifies importability (and marks the cooldown on
        # either a pip failure OR a "succeeded but still unimportable" outcome) before
        # returning -- its return value IS the ground truth, no second find_spec probe needed.
        # `ensure_installed` itself never raises (its own try/except), but the import AND the
        # call are ALSO wrapped here so `available()` keeps its documented "never raises"
        # promise even against a hypothetical import-time failure in that module.
        try:
            from . import laya_autoinstall

            return laya_autoinstall.ensure_installed()
        except Exception:
            return False

    def classify(self, message: str, bias: "Verdict") -> "Classification | None":
        try:
            agent = self._load_agent()
            result = agent.predict(message, _LAYA_QUESTIONS)
            return self._map_result(result, bias)
        except Exception as exc:
            # SECURITY: `exc` can embed `message` (an untrusted inbound tg-cli/agent string —
            # a tokenizer/parse error commonly echoes its input), so it is never interpolated
            # raw -- same `redact` (token-shaped secrets) + `safe_display` (control/bidi
            # chars) + collapsed-newlines treatment `cli._classify_route_to_harness` already
            # applies to its own untrusted subprocess output before printing it.
            from .logging import redact
            from .policy import safe_display

            detail = safe_display(str(redact(str(exc)))).replace("\n", " ")
            print(f"  laya classifier failed ({detail}); trying next provider", file=sys.stderr)
            return None

    def _load_agent(self):
        if self._agent is None:
            if os.path.exists(self.model_id):
                # SECURITY (see module docstring): refuse a cwd-relative shadow of `model_id`
                # rather than let `laya.load()` silently resolve it as a local directory.
                raise RuntimeError(
                    f"refusing to load laya model {self.model_id!r}: a local path with that "
                    "name exists in the current working directory (possible cwd-shadowing)"
                )
            import laya

            self._agent = laya.load(model_id_or_path=self.model_id)
        return self._agent

    @staticmethod
    def _choice(answers: dict, key: str) -> str | None:
        entry = answers.get(key)
        if not isinstance(entry, dict):
            return None
        choice = entry.get("choice")
        return choice if isinstance(choice, str) and choice else None

    def _map_result(self, result: object, bias: "Verdict") -> "Classification | None":
        from .classify import Classification, Verdict, default_classification

        if not isinstance(result, dict):
            return None
        answers = result.get("answers")
        if not isinstance(answers, dict):
            return None
        fallback = default_classification(bias)

        verdict_raw = self._choice(answers, "verdict")
        try:
            verdict = Verdict(verdict_raw) if verdict_raw else fallback.verdict
        except ValueError:
            verdict = fallback.verdict

        # Valid-value sets are derived from `_LAYA_QUESTIONS`'s own `criteria` keys rather
        # than a second hardcoded literal, so the schema actually sent to the model and the
        # values honored here can never drift apart.
        process_or_product = self._choice(answers, "process_or_product")
        if process_or_product not in _LAYA_QUESTIONS["process_or_product"]["criteria"]:
            process_or_product = fallback.process_or_product

        priority = self._choice(answers, "priority")
        if priority not in _LAYA_QUESTIONS["priority"]["criteria"]:
            priority = fallback.priority

        sp_raw = self._choice(answers, "story_points")
        if sp_raw in _LAYA_QUESTIONS["story_points"]["criteria"]:
            story_points = int(sp_raw)
        else:
            story_points = fallback.story_points

        return Classification(
            verdict=verdict,
            process_or_product=process_or_product,
            priority=priority,
            story_points=story_points,
        )

    @property
    def label(self) -> str:
        return f"laya:{self.model_id}"


def build_classifier_chain(
    fallbacks: list[tuple[str, str]] | None,
    env: dict[str, str] | None = None,
    *,
    capability: str | None = None,
) -> list[ClassifierProvider]:
    """Turn a `classify.fallbacks` chain into an ordered list of `ClassifierProvider`s.

    See the module docstring for the laya-vs-review-cli grouping rule and the SECURITY note
    on why a `{laya: <value>}` entry's value only decides routing, never which Hub id loads.

    A `None` `fallbacks` uses `classify.DEFAULT_FALLBACKS` (pre-#206 behavior, unchanged) --
    task-cli#208's zero-config "`laya` first" default is implemented entirely in `config.py`'s
    `DEFAULTS["classify"]["fallbacks"]` (which now lists `laya` first), NOT here: `cli.
    _classify_one`'s one real caller always passes `cfg.classify_fallbacks` (never bare
    `None`), so this function only ever groups whatever chain it's handed -- see `config.py`'s
    `DEFAULTS` comment for the actual default-construction mechanism and why keeping that
    policy in exactly one place (rather than also duplicating it here) was a deliberate,
    review-driven simplification.
    """
    from .classify import DEFAULT_FALLBACKS

    chain = list(fallbacks) if fallbacks is not None else list(DEFAULT_FALLBACKS)
    providers: list[ClassifierProvider] = []
    group: list[tuple[str, str]] = []
    capability_used = False

    def _flush() -> None:
        nonlocal group, capability_used
        if group:
            providers.append(
                ReviewCliClassifier(
                    list(group),
                    env=env,
                    capability=None if capability_used else capability,
                )
            )
            capability_used = True
            group = []

    for provider_name, model in chain:
        if provider_name == "laya":
            _flush()
            providers.append(LayaClassifier())
        else:
            group.append((provider_name, model))
    _flush()
    return providers
