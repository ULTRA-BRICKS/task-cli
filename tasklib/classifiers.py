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
    NEXT configured provider — never a crash, never a traceback.
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


# The shared, provider-neutral classification question schema (matches `CLASSIFY_PROMPT`'s
# wording/tone). All four axes are modeled as `choice` questions with a `criteria` dict of
# option -> description -- this is BOTH `laya`'s documented `predict(state, questions)` input
# shape AND, verbatim, Vercel AI Gateway's documented `/v1/evaluate` `questions.<name>` input
# shape (task-cli#217: `{"type": "choice", "instructions": ..., "criteria": {...}}`), so the
# identical dict is sent to either provider with no per-provider adapter. Both providers' real
# responses also share the same `choice`-question answer envelope (`{"choice": <winning
# label>, "probabilities": {...}}`), read back via `_extract_choice`/`_map_choice_answers`
# below -- `LayaClassifier`/`JevClassifier` differ only in HOW they call out, never in how the
# request is shaped or the response is mapped.
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

    def available(self) -> bool:
        try:
            return importlib.util.find_spec("laya") is not None
        except Exception:
            # `find_spec` is documented to be able to raise on a broken/partial install
            # (corrupted `.pth` entries, malformed namespace-package metadata) rather than
            # just returning `None` -- treat that the same as "not installed" so `available()`
            # keeps its own "never raises" promise, matching `classify()`.
            return False

    def classify(self, message: str, bias: "Verdict") -> "Classification | None":
        try:
            agent = self._load_agent()
            result = agent.predict(message, _CLASSIFICATION_QUESTIONS)
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

# Narrow, machine-local fallback (task-cli#217, live-verified): this workspace's shared `.env`
# at the `xp` monorepo root, a single `AI_GATEWAY_API_KEY=<value>` line -- read ONLY when
# `self.env is None` (i.e. the caller is using the real process environment, not an explicit
# dict) AND that real environment itself lacks the key. An explicit `env=` (every test in this
# module, and any future embedding caller that wants a hermetic "no credentials" simulation)
# NEVER consults this file, regardless of what's on disk — review finding: consulting it
# unconditionally would make an explicit `env={}` silently non-hermetic on this one machine.
# Never any other file/location; never a directory walk. A repo-scoped `classify.fallbacks`
# entry cannot influence this path in any way (it is a fixed module constant, not derived from
# config), so this is not a new attack surface for the untrusted-config threat model the module
# docstring's SECURITY note describes.
_JEV_ENV_FILE_FALLBACK = "/Users/ultra/xp/.env"
_JEV_ENV_LINE_RE = re.compile(r"^AI_GATEWAY_API_KEY=(.*)$", re.MULTILINE)
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
    env: dict[str, str] | None = None
    _api_key: str | None = field(default=None, init=False, repr=False, compare=False)
    _key_checked: bool = field(default=False, init=False, repr=False, compare=False)

    def _resolve_api_key(self) -> str | None:
        if not self._key_checked:
            try:
                if self.env is not None:
                    # Explicit env dict: authoritative and hermetic, see class docstring —
                    # never falls through to the `.env` file even if one exists on disk.
                    key = self.env.get(_JEV_API_KEY_ENV)
                else:
                    key = os.environ.get(_JEV_API_KEY_ENV) or _read_api_key_from_env_file(
                        _JEV_ENV_FILE_FALLBACK
                    )
            except Exception:
                key = None
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
            detail = safe_display(str(redact(detail))).replace("\n", " ")
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
) -> list[ClassifierProvider]:
    """Turn a `classify.fallbacks` chain into an ordered list of `ClassifierProvider`s.

    See the module docstring for the laya/jev-vs-review-cli grouping rule and the SECURITY
    note on why a `{laya: <value>}`/`{jev: <value>}` entry's value only decides routing, never
    which Hub id/endpoint gets used. A `None` `fallbacks` (no `classify.fallbacks` configured)
    uses `classify.DEFAULT_FALLBACKS`, exactly matching `resolve_chain`'s own default -- the
    pre-#206 default config (no `laya`/`jev` entry) still resolves to a single
    `ReviewCliClassifier`, byte-for-byte the old behavior.
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
        elif provider_name == "jev":
            _flush()
            providers.append(JevClassifier(env=env))
        else:
            group.append((provider_name, model))
    _flush()
    return providers
