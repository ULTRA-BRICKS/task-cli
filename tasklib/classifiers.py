"""Pluggable classifier-provider architecture (task-cli#206, task-cli#217).

`_classify_one` (cli.py) used to hardcode ONE resolution path: `classify.resolve_chain`
(walk a per-provider fallback chain, pick the first available LLM provider by env-key or
daemon-on-PATH) then shell out to `review just-ask` and parse its four-line answer. This
module generalizes that into a small `ClassifierProvider` protocol with three providers of the
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
  - `JevClassifier` (task-cli#217) — TypeSafe AI's paid cloud "Jev" evaluation model, called
    directly over Vercel AI Gateway's `POST /v1/evaluate` HTTP endpoint (stdlib `urllib`, via
    `tasklib.backends.http.request_json` — no `review`, no subprocess). `classify.py`'s
    `_PROVIDER_MODEL_PREFIX`/`_PROVIDER_KEY_ENV` USED to carry a `jev` entry too, but that was
    always config-plumbing-only vaporware (no real HTTP call existed anywhere in this repo —
    the same pre-#206 history `laya` itself once had, see `tests/test_classify.py`'s own
    comment on that); this ticket replaces it with a REAL implementation and removes the
    stale table entries the same way #206 removed `laya`'s. Never raises out of
    `available()`/`classify()`: a missing `AI_GATEWAY_API_KEY`, a network/timeout error, any
    non-2xx HTTP status (including Vercel's documented `customer_verification_required`
    billing block — this account currently lacks a card on file, a live-verified, disclosed
    environment limitation, not a code bug), or a malformed response body all make this
    provider report itself unavailable / return `None`, identical to `LayaClassifier`'s
    contract.

task-cli#208: `laya` is now the zero-config DEFAULT `classify.fallbacks` head. That policy
lives entirely in `config.py`'s `DEFAULTS["classify"]["fallbacks"]` (which now lists `laya`
first) -- NOT in `build_classifier_chain` itself, which keeps its pre-#208 grouping-only
contract unchanged (see its own docstring). An explicit, repo-configured `classify.fallbacks`
(even one that omits `laya`) is never altered by either mechanism.

`build_classifier_chain` turns a `classify.fallbacks` config list (e.g.
`[{"laya": "local"}, {"jev": "cloud"}, {"anthropic": "claude-haiku-4-5"}, ...]`) into an
ordered list of `ClassifierProvider` instances: each `laya`/`jev` entry becomes its own
`LayaClassifier`/`JevClassifier`; consecutive entries naming neither are grouped into ONE
`ReviewCliClassifier` so the manifest `capability` head and `resolve_chain`'s own
availability-walk apply to that group exactly as they did before task-cli#206. `capability`
is applied to only the FIRST such group. Neither `laya` nor `jev` is ever a zero-config
default (`classify.DEFAULT_FALLBACKS`) — both are opt-in: `laya` needs its optional extra
installed, `jev` is a paid, network-dependent provider.

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
`JevClassifier` mirrors point 1 for the same reason: a `{jev: <value>}` entry's value ALSO
only decides routing, never which endpoint/model gets called — both stay fixed
(`_JEV_ENDPOINT`/`_JEV_MODEL_ID`). A repo-scoped value picking an arbitrary URL would let a
malicious checkout exfiltrate `$AI_GATEWAY_API_KEY` to an attacker-controlled host.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
    env: dict[str, str] | None = field(default=None, repr=False)
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


# The shared, provider-neutral classification question schema (matches `CLASSIFY_PROMPT`'s
# wording/tone, with one deliberate exception documented in CALIBRATION below). All four axes
# are modeled as `choice` questions with a `criteria` dict of option -> description -- this is
# BOTH `laya`'s documented `predict(state, questions)` input shape AND, verbatim, Vercel AI
# Gateway's documented `/v1/evaluate` `questions.<name>` input shape (task-cli#217:
# `{"type": "choice", "instructions": ..., "criteria": {...}}`), so the identical dict is sent
# to either provider with no per-provider adapter. Both providers' real responses also share
# the same `choice`-question answer envelope (`{"choice": <winning label>, "probabilities":
# {...}}`), read back via `_extract_choice`/`_map_choice_answers` below -- `LayaClassifier`/
# `JevClassifier` differ only in HOW they call out, never in how the request is shaped or the
# response is mapped.
#
# CALIBRATION (task-cli#206 follow-up): the ORIGINAL `priority` wording -- `instructions:
# "How urgently should this work be picked up?"`, criteria phrased as an abstract urgency
# scale ("drop everything", "urgent", "normal priority", "low priority, no rush") -- showed
# ZERO discrimination on real, professionally-worded engineering tickets (What/Why/
# Acceptance-criteria prose that states impact/urgency implicitly rather than with
# alarm-style language).
# Measured live against a 20-ticket sample pulled from a real Forgejo `tasks-app` tracker (5
# tickets each at human-assigned Urgent/High/Medium/Low, mapped 1:1 to P0..P3): `choice`
# landed on P2 for 19/20 tickets (the 20th got P1) at an average `confidence` of 0.11 (range
# 0.026-0.232) -- the model was clearly reading SOME signal (the per-ticket confidence
# varied) but that signal never moved the winning `choice`. A control message written in
# alarm-style language ("P0: production is down, users cannot log in at all, revenue impact
# right now") DID correctly resolve to `choice: P0` on this same ORIGINAL wording, at
# P(P0)=0.87 (the model's own `confidence` field, a separate entropy-based calibration score,
# reads 0.64 for that same answer) -- proving the model CAN discriminate, the wording just
# gave it nothing to key on in calmer, formally-worded prose.
#
# FIX: reworded `criteria` around concrete, checkable OBSERVABLES instead of an abstract
# urgency label -- the same signals a human uses to infer priority from ticket prose (is
# production down / is data being lost / is there an active security breach -> P0; does this
# block other engineers or most customers -> P1; a normal-shaped bug/feature -> P2; text that
# explicitly reads as exploratory/deferred/nice-to-have -> P3). Re-run against the identical
# 20-ticket sample: `choice` now spans all four labels (P0:1, P1:6, P2:8, P3:5 -- no longer
# collapsed onto one value) at an average confidence of 0.11 (range 0.022-0.198), the control
# message still resolves correctly (`choice: P0` at P(P0)=0.70), and exact-label agreement
# with the human-assigned tier rose from 5/20 (trivially -- only because the flat-P2 baseline
# happened to match the 5 truly-P2 tickets) to 7/20, with every remaining miss landing on the
# ADJACENT tier (e.g. a true P0 landing on P1), never a 2+ tier miss.
#
# Considered and rejected: (1) longer, keyword-dense criteria ("production/outage/down,
# active data loss, an exploitable security hole... means P0") -- overshot into a NEW flat
# collapse (15/20 P0) at LOWER confidence, so more verbose criteria hurt more than it helped;
# (2) `laya`'s `score` question type (an ordinal 0-3 scale instead of `choice`) -- gave a
# much higher, better-calibrated `answer_confidence` (avg 0.51 vs 0.11), but that is a
# by-construction property of `score`'s probability mass concentrating around its weighted
# mean, not evidence of better discrimination, and adopting it would need a second,
# incompatible result shape (`score`/`probabilities` keyed by level index, no `choice` field)
# for this one axis alone -- not adopted, to keep a uniform result shape across all four axes;
# (3) a companion `noul` (yes/no) "severe impact" question in the SAME `predict()` call to
# condition `priority` on -- `laya.Agent.predict`'s own docstring states every question in one
# call runs an INDEPENDENT parallel forward pass over the same input (no cross-question
# conditioning inside the model), and empirically the noul signal only weakly separated
# true-P0/P1 tickets (mean p(severe impact)=0.73) from true-P2/P3 tickets (mean
# p(severe impact)=0.64) -- not a strong enough signal to justify combining two questions'
# outputs in `_map_result` for a marginal gain. This wording is shared verbatim by
# `JevClassifier`; its evidence base is `laya`-specific (the two models are different
# checkpoints), but the underlying fix -- observable-based criteria over abstract urgency
# labels -- is a reasonable prior for any `choice`-question classifier of this shape.
_CLASSIFICATION_QUESTIONS: dict[str, dict] = {
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
        "instructions": (
            "What priority should this ticket be given, based on its concrete impact, not "
            "its tone?"
        ),
        "criteria": {
            "P0": (
                "production is down, data is being lost, or there is an active security "
                "breach right now"
            ),
            "P1": "this blocks other engineers from shipping, or it affects most customers",
            "P2": "a normal bug or feature, no special urgency",
            "P3": "exploratory, research, deferred, or a nice-to-have cleanup",
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


def _extract_choice(answers: dict, key: str) -> str | None:
    """Read the winning `choice` label for one `choice`-typed question's answer entry.

    Shared by `LayaClassifier` and `JevClassifier` -- both real, live-verified response
    envelopes carry `answers[<question>] == {"choice": <label>, "probabilities": {...}, ...}`
    for a `choice`-typed question.
    """
    entry = answers.get(key)
    if not isinstance(entry, dict):
        return None
    choice = entry.get("choice")
    return choice if isinstance(choice, str) and choice else None


def _map_choice_answers(answers: object, bias: "Verdict") -> "Classification | None":
    """Map a `{question: {choice: ..., ...}}` answers dict onto a `Classification`.

    Shared mapping for `LayaClassifier._map_result` and `JevClassifier.classify`'s own inline
    result-mapping -- both providers answer all four axes as `choice` questions over the same
    `_CLASSIFICATION_QUESTIONS` schema, so the same per-axis "trust the answer only if it's
    one of the schema's own `criteria` keys, else fall back to the bias default" logic applies
    unchanged. Valid-value sets are derived from `_CLASSIFICATION_QUESTIONS`'s own `criteria`
    keys rather than a second hardcoded literal, so the schema actually sent to the model and
    the values honored here can never drift apart. Returns `None` (never raises) when
    `answers` isn't a dict at all -- callers treat that as "provider failed", exactly like
    every other malformed-response case.
    """
    from .classify import Classification, Verdict, default_classification

    if not isinstance(answers, dict):
        return None
    fallback = default_classification(bias)

    verdict_raw = _extract_choice(answers, "verdict")
    try:
        verdict = Verdict(verdict_raw) if verdict_raw else fallback.verdict
    except ValueError:
        verdict = fallback.verdict

    process_or_product = _extract_choice(answers, "process_or_product")
    if process_or_product not in _CLASSIFICATION_QUESTIONS["process_or_product"]["criteria"]:
        process_or_product = fallback.process_or_product

    priority = _extract_choice(answers, "priority")
    if priority not in _CLASSIFICATION_QUESTIONS["priority"]["criteria"]:
        priority = fallback.priority

    sp_raw = _extract_choice(answers, "story_points")
    if sp_raw in _CLASSIFICATION_QUESTIONS["story_points"]["criteria"]:
        story_points = int(sp_raw)
    else:
        story_points = fallback.story_points

    return Classification(
        verdict=verdict,
        process_or_product=process_or_product,
        priority=priority,
        story_points=story_points,
    )


# The ONLY model id `LayaClassifier` ever loads (see the module docstring's SECURITY note —
# a `classify.fallbacks` value is not honored as a Hub id override).
_LAYA_DEFAULT_MODEL_ID = "convaiinnovations/laya"


# The one warning laya's shipped checkpoint emits on EVERY load (laya 0.3.x): it clamps an
# out-of-range per-choice temperature and says so. It is a property of the published weights,
# not of anything task-cli controls, so repeating it on every `task classify` is pure noise
# (task-cli#245); `--verbose` keeps it.
_LAYA_TEMPERATURE_WARNING_RE = r"laya: this checkpoint ships invalid temperatures"


_LAYA_SNAPSHOT_PATTERNS = ["rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*"]


def _cached_laya_snapshot(model_id: str) -> str | None:
    """The local HuggingFace cache dir already holding `model_id`, or `None`.

    `laya.load(<hub id>)` always re-validates the snapshot against the Hub first — normally
    ~0.25 s, but measured at 30 s on a bad network moment (task-cli#245), on the critical path
    of a cold classify. A checkpoint that is already cached is loaded from its local dir
    instead; only a missing one goes to the network. The cost: a newer upstream revision is
    picked up only once the cache is cleared — a pinned checkpoint is the safer default for a
    classifier whose result shape `_map_result` hand-parses anyway.
    """
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(
            model_id, local_files_only=True, allow_patterns=_LAYA_SNAPSHOT_PATTERNS
        )
    except Exception:
        return None


def _load_laya(laya_module, model_id: str, *, verbose: bool):
    """`laya.load(model_id)`, from the local cache when possible, quiet unless `verbose`.

    Quiet = no HF snapshot progress bar and no uncalibrated-temperature RuntimeWarning. A
    cached dir that fails to load (e.g. a half-finished earlier download) falls back to the
    Hub id, which re-fetches what is missing.
    """
    import warnings

    source = _cached_laya_snapshot(model_id) or model_id
    with warnings.catch_warnings():
        if not verbose:
            try:
                from huggingface_hub.utils import disable_progress_bars

                disable_progress_bars()
            except Exception:
                pass  # a hub without this helper just keeps its bar
            warnings.filterwarnings(
                "ignore", message=_LAYA_TEMPERATURE_WARNING_RE, category=RuntimeWarning
            )
        try:
            return laya_module.load(model_id_or_path=source)
        except Exception:
            if source == model_id:
                raise
            return laya_module.load(model_id_or_path=model_id)


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

    task-cli#245: loading costs ~3 s and every `task classify` is a fresh process, so
    `classify()` first asks the warm machine-wide server (`tasklib/classify_server.py`, spawned
    on first use) and only loads in-process when that returns nothing. `use_server=False` is how
    the server itself loads its model. The load is quiet (no HF download progress bar, no
    laya "uncalibrated temperatures" RuntimeWarning) unless `verbose`; `verbose` skips the
    server and loads in-process, since a server's load output goes to its log, not this stderr.
    """

    model_id: str = _LAYA_DEFAULT_MODEL_ID
    use_server: bool = True
    verbose: bool = False
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
            result = None
            if self.use_server and not self.verbose:
                from . import classify_server

                result = classify_server.predict(message, _CLASSIFICATION_QUESTIONS)
            if result is None:
                result = self._load_agent().predict(message, _CLASSIFICATION_QUESTIONS)
            return self._map_result(result, bias)
        except Exception as exc:
            # SECURITY: `exc` can embed `message` (an untrusted inbound tg-cli/agent string —
            # a tokenizer/parse error commonly echoes its input), so it is never interpolated
            # raw -- same `redact` (token-shaped secrets) + `safe_display` (control/bidi
            # chars) + collapsed-newlines treatment `cli._classify_route_to_harness` already
            # applies to its own untrusted subprocess output before printing it.
            from .logging import redact
            from .policy import safe_display

            detail = safe_display(redact(str(exc)).replace("\n", " "))
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

            self._agent = _load_laya(laya, self.model_id, verbose=self.verbose)
        return self._agent

    def _map_result(self, result: object, bias: "Verdict") -> "Classification | None":
        if not isinstance(result, dict):
            return None
        return _map_choice_answers(result.get("answers"), bias)

    @property
    def label(self) -> str:
        return f"laya:{self.model_id}"


# Vercel AI Gateway's real, documented evaluation endpoint + TypeSafe AI's real model id
# (https://vercel.com/docs/ai-gateway/modalities/evaluation, task-cli#217). Both are FIXED —
# see the module docstring's SECURITY note on why a `classify.fallbacks` value never picks
# either.
_JEV_ENDPOINT = "https://ai-gateway.vercel.sh/v1/evaluate"
_JEV_MODEL_ID = "typesafe-ai/jev"
_JEV_API_KEY_ENV = "AI_GATEWAY_API_KEY"

# Narrow, OPT-IN `.env` fallback (task-cli#217): when `self.env is None` (the caller is using
# the real process environment, not an explicit dict) AND that real environment lacks
# `AI_GATEWAY_API_KEY`, ALSO check `$TASK_JEV_ENV_FILE` (a path to one `.env`-shaped file) if
# that variable itself is set — review finding: a hardcoded machine-specific path baked into
# shipped library code is dead weight everywhere else and a silent "this one laptop can spend
# money with no visible env var set" surprise on the machine it targets. Requiring the path to
# come from its OWN env var keeps the behavior opt-in and observable (the operator who wants
# it sets `TASK_JEV_ENV_FILE=/path/to/.env` once, e.g. in their shell profile) without
# encoding anyone's home directory into the repo. Unset -> no fallback file is ever read. An
# explicit `env=` (every test in this module, and any future embedding caller that wants a
# hermetic "no credentials" simulation) NEVER consults this file, regardless of what's on
# disk or what `$TASK_JEV_ENV_FILE` holds — review finding: consulting it unconditionally
# would make an explicit `env={}` silently non-hermetic. A repo-scoped `classify.fallbacks`
# entry cannot influence this path in any way (it reads a separate env var, never config), so
# this is not a new attack surface for the untrusted-config threat model the module
# docstring's SECURITY note describes.
_JEV_ENV_FILE_PATH_ENV = "TASK_JEV_ENV_FILE"
_JEV_ENV_LINE_RE = re.compile(
    rf"^(?:export[ \t]+)?{re.escape(_JEV_API_KEY_ENV)}=(.*)$", re.MULTILINE
)
_JEV_TIMEOUT_SECONDS = 20


def _read_api_key_from_env_file(path: str) -> str | None:
    """Read `AI_GATEWAY_API_KEY=<value>` off one line of `path`, or `None` on any failure.

    Deliberately narrow: a single regex match against the whole-file text, no general `.env`
    parser, no other variable ever read from this file. Quotes around `<value>` (`"..."` or
    `'...'`) are stripped, matching how a shell-sourceable `.env` line is usually quoted.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    match = _JEV_ENV_LINE_RE.search(text)
    if match is None:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value or None


@dataclass
class JevClassifier:
    """TypeSafe AI's paid cloud "Jev" evaluation model, called directly over Vercel AI
    Gateway's `POST /v1/evaluate` HTTP endpoint (task-cli#217) -- no `review`/subprocess
    involved, the same "direct in-process backend" shape as `LayaClassifier`.

    The API key is resolved lazily and cached on the instance. When `env` is `None` (the
    default -- "use the real process environment"), `$AI_GATEWAY_API_KEY` is tried first,
    falling back to a narrow, machine-local `.env` read (`_read_api_key_from_env_file`) only
    when the env var is absent. An EXPLICIT `env=` dict is authoritative and hermetic: only
    that dict is consulted, the `.env` file is never read, even if it exists and holds a key
    (review finding -- an explicit `env={}` must mean "no credentials", full stop).
    `available()` is cheap (no network call, just "is a key resolvable") -- mirrors
    `LayaClassifier.available()`'s own "no real model load just to answer the question"
    posture.

    `classify()` never raises: a missing key, any network/timeout error, any non-2xx HTTP
    status (`tasklib.backends.http.request_json` raises `HttpError`/`AmbiguousHttpError` for
    all of those, including Vercel's documented `customer_verification_required` billing
    block this account currently hits — a live-verified, disclosed environment limitation),
    or a malformed response body all make `classify()` return `None`, so
    `build_classifier_chain`'s caller falls through to the NEXT configured provider exactly
    like a `LayaClassifier` runtime failure. A malformed-but-2xx response degrades SILENTLY
    (no diagnostic), matching `LayaClassifier._map_result`'s own silent-on-malformed-body
    precedent; an actual exception (network/timeout/non-2xx) DOES print one diagnostic line
    to stderr (with the API key — and any other token-shaped secret — redacted, same
    `redact`/`safe_display` treatment `LayaClassifier` already applies to its own
    untrusted-input diagnostic) so a persistently broken/billing-blocked jev config is
    observable.
    """

    endpoint: str = _JEV_ENDPOINT
    model_id: str = _JEV_MODEL_ID
    env: dict[str, str] | None = field(default=None, repr=False)
    _api_key: str | None = field(default=None, init=False, repr=False, compare=False)
    _key_checked: bool = field(default=False, init=False, repr=False, compare=False)

    def _resolve_api_key(self) -> str | None:
        if not self._key_checked:
            if self.env is not None:
                # Explicit env dict: authoritative and hermetic, see class docstring —
                # never falls through to the `.env` file even if one exists on disk.
                key = self.env.get(_JEV_API_KEY_ENV)
            else:
                key = os.environ.get(_JEV_API_KEY_ENV)
                if not key:
                    env_file = os.environ.get(_JEV_ENV_FILE_PATH_ENV)
                    key = _read_api_key_from_env_file(env_file) if env_file else None
            self._api_key = key or None
            self._key_checked = True
        return self._api_key

    def available(self) -> bool:
        return self._resolve_api_key() is not None

    def classify(self, message: str, bias: "Verdict") -> "Classification | None":
        api_key = self._resolve_api_key()
        if not api_key:
            return None
        try:
            from .backends.http import request_json

            payload = {
                "model": self.model_id,
                "state": message,
                "questions": _CLASSIFICATION_QUESTIONS,
            }
            result = request_json(
                self.endpoint,
                method="POST",
                headers={"Authorization": f"Bearer {api_key}"},
                payload=payload,
                timeout=_JEV_TIMEOUT_SECONDS,
            )
            if not isinstance(result, dict):
                return None
            return _map_choice_answers(result.get("answers"), bias)
        except Exception as exc:
            # SECURITY: never let the API key (or any other token-shaped secret) reach
            # stderr. `request_json`'s own `HttpError` message/body never embeds the
            # `Authorization` header, but the exact-match `.replace(api_key, ...)` below is a
            # belt-and-braces guard against any future exception shape that might -- applied
            # LAST (after the body-derived code/message is appended, review finding) so it
            # also scrubs a key that somehow got echoed back in a response body, not just one
            # embedded in `str(exc)` itself. `redact` (token-shaped secrets in general) +
            # `safe_display` (control/bidi chars) + collapsed-newlines is the same treatment
            # `LayaClassifier` already applies to its own untrusted-input diagnostic.
            from .logging import redact
            from .policy import safe_display

            detail = str(exc)
            body = getattr(exc, "body", "")
            if isinstance(body, str) and body:
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                    # Vercel's REAL, live-verified `/v1/evaluate` error body names this field
                    # `type` ({"error": {"type": "customer_verification_required", ...}}), not
                    # the `code` this ticket's own spec described up front -- `code` is kept
                    # as a tolerant fallback only.
                    code = parsed["error"].get("type") or parsed["error"].get("code") or ""
                    err_msg = parsed["error"].get("message") or ""
                    detail = f"{detail}: {code} {err_msg}".strip()
            detail = detail.replace(api_key, "<redacted>")
            detail = safe_display(redact(detail).replace("\n", " "))
            print(f"  jev classifier failed ({detail}); trying next provider", file=sys.stderr)
            return None

    @property
    def label(self) -> str:
        return f"jev:{self.model_id}"


def build_classifier_chain(
    fallbacks: list[tuple[str, str]] | None,
    env: dict[str, str] | None = None,
    *,
    capability: str | None = None,
    verbose: bool = False,
) -> list[ClassifierProvider]:
    """Turn a `classify.fallbacks` chain into an ordered list of `ClassifierProvider`s.

    See the module docstring for the laya/jev-vs-review-cli grouping rule and the SECURITY
    note on why a `{laya: <value>}`/`{jev: <value>}` entry's value only decides routing, never
    which Hub id/endpoint gets used.

    A `None` `fallbacks` uses `classify.DEFAULT_FALLBACKS` (pre-#206 behavior, unchanged) --
    task-cli#208's zero-config "`laya` first" default is implemented entirely in `config.py`'s
    `DEFAULTS["classify"]["fallbacks"]` (which now lists `laya` first), NOT here: `cli.
    _classify_one`'s one real caller always passes `cfg.classify_fallbacks` (never bare
    `None`), so this function only ever groups whatever chain it's handed -- see `config.py`'s
    `DEFAULTS` comment for the actual default-construction mechanism and why keeping that
    policy in exactly one place (rather than also duplicating it here) was a deliberate,
    review-driven simplification. `verbose` only reaches `LayaClassifier` (whether its model
    load shows progress/warnings on stderr).
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
            providers.append(LayaClassifier(verbose=verbose))
        elif provider_name == "jev":
            _flush()
            providers.append(JevClassifier(env=env))
        else:
            group.append((provider_name, model))
    _flush()
    return providers
