"""Classification of an inbound message → ``change | actionable | justAsk``.

Pure parts only: building the prompt, parsing ``review``'s answer, and resolving the first
*available* model from a per-provider fallback chain. The effectful shell-out to
``review just-ask`` lives in the entrypoint (``bin/task``) — this module decides WHAT to
run and HOW to read the result, so it is fully unit-testable without spawning anything.

Bias is to ``change`` when ambiguous between change/justAsk — most questions to a dev agent
are latent change requests (spec §7). ``actionable`` (task-cli#80) is a THIRD, distinct
verdict, never a bias target: an instruction to directly EXECUTE an existing command/tool
right now (``"run a read-only code review on the diff"``, ``"run the tests"``) is neither a
pure question nor a request that should mint a ticket. Before this verdict existed, the
prompt's binary change/justAsk scheme plus its bias-to-change default silently routed every
review-request-style message into ``change``, which — with ``--create`` — auto-created a
placeholder GitHub issue full of ``(needs triage)`` boilerplate instead of the review ever
running (confirmed 7 times against real inbound messages; see task-cli#80's issue body).
Actually INVOKING an ``actionable`` message's command is out of scope for this repo (the
tg-cli inbound hook that owns real execution lives elsewhere — see AGENTS.md "Out of scope");
this module's job is only to stop misclassifying it as ``change``, so ``cmd_classify``
(cli.py) never reaches the ticket-auto-create path for it. That also makes the title-
similarity dedup's failure on repeated review-style requests (task-cli#80's other finding)
moot for THIS failure mode: an ``actionable`` message never reaches ``_classify_create`` /
``_best_dedup_match`` at all now, so there is nothing left to dedup.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeVar

CLASSIFY_PROMPT = (
    "Classify this message to a dev agent as EXACTLY ONE of:\n"
    "  `change` -- a genuine change/edit request: should result in a code/doc/config edit and "
    "needs a tracked ticket.\n"
    "  `actionable` -- an instruction to directly EXECUTE an existing command/tool RIGHT NOW "
    "(e.g. 'run a read-only code review on the diff', 'run the tests', 'ship PR #5') -- not a "
    "question, and not a request to edit anything; just do the named action, no ticket.\n"
    "  `justAsk` -- a pure question, no edit and nothing to execute.\n"
    "Bias to `change` ONLY when genuinely ambiguous between change and justAsk -- most "
    "questions to a dev agent are latent edit requests. Do NOT apply that bias to a message "
    "that names an existing action/tool/command to run directly -- that is `actionable`, "
    "never `change`, even if running it might incidentally touch files.\n"
    "\n"
    "ALSO classify three more axes of the underlying work (answer them regardless of verdict; "
    "they are simply ignored downstream for actionable/justAsk):\n"
    "  PROCESS_OR_PRODUCT -- `process` if this is a session-local/agent process step (e.g. "
    "reviewing an uncommitted diff, running a checklist before a commit) rather than durable "
    "product work; otherwise `product`.\n"
    "  PRIORITY -- one of P0 (drop everything), P1 (urgent), P2 (normal), P3 (low), for how "
    "urgently the work should be picked up.\n"
    "  SP -- a Fibonacci-ish story-point complexity estimate, one of 1, 2, 3, 5, 8, 13 "
    "(1 = trivial/typo-level, 2 = small single-function fix, 3 = moderate/single-file, "
    "5 = substantial/multi-file or new test infra, 8 = large/new subsystem or design-heavy, "
    "13 = epic/major feature from scratch).\n"
    "Respond with EXACTLY four lines and nothing else, each the literal label, a colon, then "
    "your answer: VERDICT (change/actionable/justAsk), PROCESS_OR_PRODUCT (process/product), "
    "PRIORITY (P0/P1/P2/P3), SP (1/2/3/5/8/13).\n\nMESSAGE:\n"
)

# The sentinel the model is asked to emit. Parsing keys off this first; it is unambiguous and
# cannot collide with the prompt body (which deliberately avoids the bare verdict words now).
_VERDICT_RE = re.compile(r"verdict\s*:\s*(change|actionable|justask|just-ask|just ask)", re.IGNORECASE)


class Verdict(str, Enum):
    CHANGE = "change"
    ACTIONABLE = "actionable"
    JUST_ASK = "justAsk"


# ── Story points / priority / process-vs-product axes ──────────────────────────────────────
#
# Three EXTRA classification axes riding the SAME `review just-ask` round-trip as the verdict
# (CLASSIFY_PROMPT above asks for all four; one shell-out, not two -- matches this module's
# existing cost/latency posture, and `review`'s own per-call cost). They are parsed the same
# way the verdict is: a `LABEL: value` sentinel line, read via `parse_classification` below.
# Only meaningful when the verdict is `change` (`actionable`/`justAsk` never mint a ticket, so
# their SP/priority/process-product would be moot) -- `cli.cmd_classify` is what skips
# surfacing them for those verdicts; parsing itself never branches on the verdict.
#
# Story-point scale (a plain Fibonacci-ish COMPLEXITY estimate, not a time estimate):
#   1  = trivial / typo-level (a one-line copy fix, a renamed constant)
#   2  = small, single-function fix (a bounded bug in one function, no new tests needed)
#   3  = moderate, single-file change (a small new feature in one module, a couple of tests)
#   5  = substantial: a multi-file change, or one that needs new test infrastructure
#   8  = large: a new subsystem, or design-heavy (several approaches to weigh, cross-module)
#   13 = epic: a major feature built from scratch, likely to itself need breaking into
#        smaller tickets before work starts
# review board finding (this ticket, two independent rounds): a negative lookahead that only
# excludes a following `/` or word character is not enough -- the PROMPT's own descriptive
# lines enumerate the allowed values with COMMAS and parentheses, not slashes ("PRIORITY --
# one of P0 (drop everything), P1 (urgent), P2 (normal), P3 (low)"; "SP -- ... one of 1, 2, 3,
# 5, 8, 13"), and a model that echoes/paraphrases that description onto a `LABEL: value` line
# (`PRIORITY: P0 (drop everything), P1 (urgent), ...`) would have the FIRST, unsafe-end-of-
# range token (`process`, `P0`, `1`) accepted, since a space/comma/`(` all satisfy "not `/` or
# `\w`" just as well as a genuine line ending does. Closed properly here: the captured token
# must be the ENTIRE remainder of its line (only trailing whitespace allowed after it, and the
# `LABEL:` prefix must start the line, only leading whitespace before it) -- `re.MULTILINE`
# anchors `^`/`$` to each line rather than the whole string. This matches the prompt's own
# instruction ("Respond with EXACTLY four lines... each the literal label, a colon, then your
# answer") — a genuine one-word answer is always alone on its line; anything else on that line
# (an enumerated list, a parenthetical, trailing prose) is exactly the "did not cleanly
# resolve" case this module already biases to a safe default for, module-wide.
_PROCESS_PRODUCT_RE = re.compile(
    r"^\s*process_or_product\s*:\s*(process|product)\s*$", re.IGNORECASE | re.MULTILINE
)
_PRIORITY_RE = re.compile(r"^\s*priority\s*:\s*(p[0-3])\s*$", re.IGNORECASE | re.MULTILINE)
_SP_RE = re.compile(r"^\s*sp\s*:\s*(13|1|2|3|5|8)\s*$", re.IGNORECASE | re.MULTILINE)

# Safe defaults when an axis doesn't cleanly resolve (mirrors `parse_verdict`'s `bias`
# parameter in spirit, but these three are never caller-configurable -- there is no genuine
# ambiguity here worth exposing a knob for, unlike the change/justAsk bias). `process_or_product`
# biases to `product`: a mis-routed PROCESS ticket (filed as a local harness task when it
# should have been a durable product ticket) is far cheaper to notice and fix than a silently
# DROPPED product ticket would be, so on ambiguity this stays on the durable, auditable
# product-tracker path rather than the local-only one -- never write to neither store.
_DEFAULT_PROCESS_OR_PRODUCT = "product"
_DEFAULT_PRIORITY = "P2"
_DEFAULT_STORY_POINTS = 3


@dataclass
class Classification:
    """A message's full classification: the change/actionable/justAsk verdict PLUS the three
    ticket-shaping axes above (story points, priority, process-vs-product). See
    :func:`parse_classification`."""

    verdict: Verdict
    process_or_product: Literal["process", "product"]
    priority: Literal["P0", "P1", "P2", "P3"]
    story_points: Literal[1, 2, 3, 5, 8, 13]


def default_classification(verdict: Verdict) -> Classification:
    """The safe-default :class:`Classification` used when there is no model output to parse at
    all (no classifier provider reachable) -- every axis takes the SAME fixed default
    ``parse_classification`` falls back to on a malformed/absent per-axis sentinel, exposed here
    as the single source of truth so a caller (``cli._classify_one``) never hand-copies the
    literal default values into a second file (review finding: a copy silently drifts from this
    module's own defaults if either one is ever changed alone).
    """
    return Classification(
        verdict=verdict,
        process_or_product=_DEFAULT_PROCESS_OR_PRODUCT,
        priority=_DEFAULT_PRIORITY,
        story_points=_DEFAULT_STORY_POINTS,
    )


# Default per-provider fallback chain (§2). One cheap/fast model PER PROVIDER; the runner
# uses the first AVAILABLE one. Default head is haiku. Each entry is (provider, model-id).
DEFAULT_FALLBACKS: tuple[tuple[str, str], ...] = (
    ("anthropic", "claude-haiku-4-5"),
    ("openai", "gpt-5-mini"),
    ("commandcode", "deepseek/deepseek-v4-flash"),
    ("zai", "glm-4.6-flash"),
    ("google", "gemini-2.5-flash"),
    ("ollama", "qwen2.5:3b"),
)

# How review-cli names each provider in a ``-m`` model string, and which env var(s) signal
# the provider is reachable. ollama is reachable when its local daemon binary exists.
#
# ``jev`` (task-cli's classification-axes ticket, 2026-09): CONFIG PLUMBING ONLY, added the
# same way every other provider here is added — a prefix-table entry and an availability rule
# — so a repo's ``classify.fallbacks``/``classify.capability`` CAN name it, exactly like
# anthropic/openai/ollama today. It is not wired to a real HTTP call anywhere in this repo
# (making that call, like every other review-cli-routed provider, is entirely ``review``'s
# job — its own provider table is out of scope here); no credentials exist in this
# environment, so this is unverified against a live endpoint. ``jev`` mirrors the API-key
# shape (``JEV_API_KEY``).
#
# ``laya`` (task-cli#206) is DELIBERATELY ABSENT from this table: unlike every other entry
# here, it is not an LLM ``review`` shells out to — it is a real, local, directly-invoked
# classifier (the ``laya`` PyPI package, Convai Innovations' free/local/Apache-2.0 "System 1"
# model) that never touches ``review``/``resolve_chain``/``to_model_arg`` at all. See
# ``tasklib/classifiers.py``'s ``LayaClassifier`` and ``build_classifier_chain``, which turns
# a ``classify.fallbacks`` entry named ``laya`` into a direct in-process ``predict()`` call
# instead of a ``-m laya:<model>`` review-cli invocation.
_PROVIDER_MODEL_PREFIX = {
    "anthropic": "claude",
    "openai": "commandcode",  # OpenAI models are routed through the commandcode gateway
    "commandcode": "commandcode",
    "zai": "zai",
    "google": "gemini",
    "ollama": "ollama",
    "jev": "jev",
}

# Local-daemon-on-PATH providers: reachable when their own binary exists on PATH, not via an
# env-var key (see ``provider_available``).
_LOCAL_DAEMON_PROVIDERS = frozenset({"ollama"})

_PROVIDER_KEY_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    # OpenAI models are routed through the commandcode gateway (the model arg is
    # `commandcode:<model>`), so reachability is the COMMANDCODE key — a bare OPENAI_API_KEY
    # would make us emit a commandcode arg that the gateway can't authenticate. Keep these in
    # lockstep with _PROVIDER_MODEL_PREFIX["openai"] == "commandcode".
    "openai": ("COMMANDCODE_API_KEY",),
    "commandcode": ("COMMANDCODE_API_KEY",),
    "zai": ("ZAI_API_KEY", "ZHIPU_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "ollama": (),  # availability is the daemon, not a key
    "jev": ("JEV_API_KEY",),
}

# The shared manifest (models.yaml) names Google's provider ``gemini`` and has no ``ollama``;
# this CLI's provider tables use ``google`` (and route ``review`` to a bare ``gemini`` arg).
# Map a manifest provider onto this CLI's provider key so a manifest-resolved entry slots
# into the SAME availability check + ``-m`` building as the hardcoded chain. Providers not
# listed pass through unchanged (anthropic/openai/commandcode/zai already line up).
#
# KNOWN LIMITATION (google only): ``to_model_arg("google", …)`` collapses to the bare ``gemini``
# token because review's gemini backend ignores a model id and uses its OWN env-configured model.
# So for a manifest entry whose provider is ``gemini`` the manifest's exact VERSION is not threaded
# into ``-m`` — the manifest only steers WHICH provider/role is preferred, and review picks the
# concrete gemini version. Every other provider DOES carry the manifest's exact model id.
_MANIFEST_PROVIDER_ALIASES = {"gemini": "google"}


@dataclass
class ResolvedModel:
    """A resolved fallback entry — the provider plus the ``-m`` string for ``review``."""

    provider: str
    model_arg: str


def to_model_arg(provider: str, model: str) -> str:
    """Build the ``review -m`` string for a (provider, model) pair.

    ``claude:claude-haiku-4-5``, ``commandcode:deepseek/...``, ``zai:glm-4.6-flash``,
    ``gemini`` (bare; review's gemini backend takes the env model), ``ollama:qwen2.5:3b``.
    Two providers are special-cased explicitly (rather than only via the prefix table) so the
    routing is obvious to a reader:
      - ``google`` → the bare ``gemini`` token (review's gemini backend ignores a model id);
      - ``openai`` → the ``commandcode:`` gateway prefix (review reaches OpenAI models through
        the commandcode gateway, so reachability is the COMMANDCODE key — see _PROVIDER_KEY_ENV).
    """
    if provider == "google":
        return "gemini"
    prefix = _PROVIDER_MODEL_PREFIX.get(provider, provider)  # openai -> "commandcode"
    return f"{prefix}:{model}"


def provider_available(provider: str, env: dict[str, str] | None = None) -> bool:
    """Is a provider reachable? Key present in env, or (ollama) its daemon binary exists.

    Pure-ish: reads ``env`` (defaults to ``os.environ``) and, for a local-daemon provider,
    checks for its binary on PATH. No network probe — availability here means "we have a
    credible way to reach it", matching review-cli's startup failover posture. NEVER called
    for ``laya`` (task-cli#206): that provider's own reachability check
    (``tasklib.classifiers.LayaClassifier.available``) is "the ``laya`` package is
    importable", entirely decoupled from this review-cli-routed provider table.
    """
    env = os.environ if env is None else env
    if provider in _LOCAL_DAEMON_PROVIDERS:
        return shutil.which(provider) is not None
    keys = _PROVIDER_KEY_ENV.get(provider, ())
    return any(env.get(k) for k in keys)


def capability_head(
    capability: str | None,
    env: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve ``capability`` to a manifest ``(provider, model)`` head, or ``None`` (rig#8).

    The consumer side of rig-cli#8: when a ``classify.capability`` is configured, ask the
    shared manifest (``models.yaml``) for the model it currently pins for that capability/role
    and return it as a ``(provider, model)`` pair to PREFER ahead of the hardcoded chain — so
    the classifier tracks the manifest the cron checker keeps fresh, instead of a literal pin.

    Fail-soft and purely additive: a falsy ``capability``, a missing manifest, or the shared
    resolver lib not being installed all yield ``None``, and the caller proceeds with the
    hardcoded chain exactly as before. The manifest's provider name is normalized onto this
    CLI's provider keys (``gemini`` -> ``google``) so the head slots into the same availability
    check + ``-m`` building as every other chain entry.
    """
    if not capability:
        return None
    from .manifest import resolve_capability

    resolved = resolve_capability(capability, env=env)
    if resolved is None:
        return None
    provider, model = resolved
    return _MANIFEST_PROVIDER_ALIASES.get(provider, provider), model


def resolve_chain(
    fallbacks: list[tuple[str, str]] | None = None,
    env: dict[str, str] | None = None,
    *,
    capability: str | None = None,
) -> ResolvedModel | None:
    """Return the first AVAILABLE (provider, model) in the chain, or ``None`` if none are.

    Same availability-failover as the review board, pool=1: walk the chain top-to-bottom and
    pick the first provider with a credential/daemon. Provider-agnostic — degrades to
    whatever's reachable so classification keeps working offline (ollama) or on any one key.

    When ``capability`` is given (the ``classify.capability`` config — rig#8), the model the
    shared manifest pins for it is PREPENDED as the preferred head, so a reachable manifest
    model wins over the hardcoded chain. The prepend is fail-soft (an unresolvable capability
    or a missing manifest adds nothing), and an UNREACHABLE manifest head still falls through
    to the rest of the chain — the head only changes the PREFERENCE, never removes a fallback.
    """
    chain = list(fallbacks) if fallbacks is not None else list(DEFAULT_FALLBACKS)
    head = capability_head(capability, env)
    if head is not None:
        # Promote the manifest model to the PREFERRED head. If it already appears in the chain
        # (anywhere — not just at position 0), drop that occurrence first so the promotion isn't
        # silently lost when the manifest picked an entry that sits LOWER than another reachable
        # provider; the manifest's choice must win the preference, never duplicate.
        chain = [entry for entry in chain if entry != head]
        chain.insert(0, head)
    for provider, model in chain:
        if provider_available(provider, env):
            return ResolvedModel(provider=provider, model_arg=to_model_arg(provider, model))
    return None


def build_prompt(message: str) -> str:
    """The fixed classification prompt with the message appended."""
    return CLASSIFY_PROMPT + message.strip()


# ── Multi-ask detection ──────────────────────────────────────────────────────────────────────
#
# An ecosystem-wide audit of task-cli intake (2026-09) found a large share of auto-created
# tickets were empty/vague in the body — one contributor: an inbound message that actually
# bundles SEVERAL distinct asks gets lumped into one `change` classification and one vague
# ticket, because nothing upstream of `cmd_classify` ever notices the message wasn't about one
# thing. `detect_multi_ask` is a deliberately STRUCTURAL heuristic (never prose-length or topic
# guessing) so an ordinary single-topic paragraph — even a long, detailed one — is never split:
#
#   1. A numbered/bulleted list (`1. ...`, `2) ...`, `- ...`, `* ...`, `• ...`) with >=2 items —
#      each item is one ask. The strongest, least ambiguous signal: the author already
#      structured the message as a list.
#   2. No list, but the message chains clauses with an EXPLICIT enumeration marker — English
#      "also"/"and also"/"additionally"/"plus", or the Russian equivalents of "and also"/
#      "also too"/"as well"/"besides that" (this project's inbound messages are frequently
#      Russian — see `_ENUMERATION_SEP_RE` below for the exact patterns matched; kept out of
#      this comment per the repo's English-only-docs rule) — or a semicolon.
#
# Deliberately NOT a signal: a bare English "and" (or its Russian equivalent) conjunction, OR a
# bare "also"/"additionally"/"plus" with nothing else marking it. "and" is the single most
# common conjunction in ordinary prose ("fix the login bug and improve the error message" could
# be one combined ask or two — genuinely ambiguous), and a lone "also" is just as often
# elaborating on the SAME ask ("...and this also happens on the second page" is still describing
# one bug, not introducing a second one — an early version of this heuristic split exactly that
# sentence and was caught by this module's own test suite). Only the STRONGER two-word/idiomatic
# markers in `_ENUMERATION_SEP_RE` below are kept ("and also," not bare "also"), and the Russian
# phrases there, which read as enumeration markers much more reliably than their literal
# word-for-word English gloss suggests.
# Anchored at ZERO indentation on purpose (review finding, P1): an earlier version allowed any
# leading whitespace, so an INDENTED sub-bullet under a parent item ("- do X\n  - a detail of
# X") was treated as its own independent top-level ask rather than part of X's item. Requiring
# zero indentation means only genuinely top-level bullets are split points; an indented line
# (nested bullet or a wrapped continuation line) is swept into the CONTAINING item's text.
_LIST_ITEM_RE = re.compile(r"^(?:[-*•]|\d{1,2}[.)])[ \t]+\S", re.MULTILINE)
_LIST_MARKER_STRIP_RE = re.compile(r"^(?:[-*•]|\d{1,2}[.)])[ \t]+")

_ENUMERATION_SEP_RE = re.compile(
    r"\s*;\s*"
    r"|\s+(?:"
    r"а\s+ещ[её]|и\s+ещ[её]|а\s+также|также|кроме\s+того"  # Russian
    r"|and\s+also"  # English (bare "also"/"plus"/"additionally" deliberately excluded — too
    # easily elaborates on the SAME ask rather than introducing a new one; see comment above)
    r")\s+",
    re.IGNORECASE,
)

# A split fragment must carry at least this many words to count as a distinct ask — a trailing
# "; thanks" or a one-word aside must not be treated as its own item.
_MIN_ASK_WORDS = 3
_ASK_WORD_RE = re.compile(r"\w+", re.UNICODE)

# A fragment that OPENS with a sequencing word ("then," or its Russian equivalents — see the
# regex below, kept out of this comment per the repo's English-only-docs rule) is a dependent
# NEXT STEP of the PRECEDING fragment, not an independent parallel ask — review finding: a
# semicolon alone doesn't distinguish "two separate requests" from "two ordered steps of one
# task" ("Migrate tenant IDs by adding the tenant_id column; then backfill existing rows." is
# ONE atomic migration, not two asks). A leading "and then"/"and" is included too, since "and
# then X" carries the same dependency signal as bare "then X".
_SEQUENCING_FRAGMENT_RE = re.compile(
    r"^(?:and\s+)?then\b|^затем\b|^потом\b", re.IGNORECASE
)

# KNOWN, ACCEPTED limitation, not fully closeable here (three separate adversarial review
# rounds each found a new phrasing this keyword list doesn't catch — e.g. "Upgrade the tenant
# migration by adding the tenant_id column; backfill existing rows after deployment" has no
# sequencing word at all, yet is still one ordered migration, not two asks). Whether a
# semicolon separates two INDEPENDENT asks or two ORDERED STEPS of one task is a discourse-level
# judgment no closed keyword list can fully capture — every fix here narrows the false-positive
# rate without ever reaching zero, the same class of tradeoff as the token/Jaccard duplicate
# gate's documented "Encrypt API token storage" vs "API token storage rotation" false positive
# (see policy.py). Accepted because the cost is bounded and reversible: a wrongly-split message
# creates two tickets instead of one, which a human closes/merges — never silent data loss, and
# no worse than the pre-existing failure mode (one vague ticket covering several real asks) this
# feature exists to fix. Further narrowing needs real inbound-message data to validate against,
# not another guessed keyword.


def _split_list_items(message: str) -> list[str]:
    """Split `message` at bullet/numbered list item boundaries. `[]` unless >=2 items result.

    Real bugs fixed here (review findings, P1/P2): a PREAMBLE before the first bullet ("Only
    enterprise plans:\n- add the flag\n- expose it in admin") was silently DISCARDED — the
    shared constraint never reached any item, not even the one whose text happened to include
    it — and a shared line/paragraph attached to ONE bullet's block (whether trailing after the
    LAST bullet: "...\n- expose it in admin\n\nBoth must preserve SSO behavior.", WITHOUT a blank
    line: "...\n- Update client configuration\nBoth must use the new key identifier.", or
    sandwiched between two bullets: "- update client configuration\nBoth changes must preserve
    SSO.\n- rotate signing keys") was misrepresented as specific to just that one ask when it
    applies to all of them. All these shapes mean the message carries context this heuristic
    cannot safely partition among items, so — same principle as `_split_enumeration`'s
    thin-fragment handling — DECLINE TO SPLIT AT ALL rather than lose or misattribute it; the
    caller keeps the whole message (preamble, list, and any extra content together) as one ask.
    The rule is simply "EVERY item's own line block must be exactly one line" (checked for ALL
    items, not only the last — an earlier version checked only the last item's block, missing
    the sandwiched-between-bullets shape entirely) — ANY extra line in ANY item's block
    (blank-separated or not) is ambiguous between "a wrapped continuation of that one bullet"
    and "a shared note for every bullet," and there is no reliable way to tell them apart from
    text shape alone — UNLESS the extra line is itself INDENTED, which is the same signal
    `_LIST_ITEM_RE`'s zero-indentation anchor already uses to recognize a nested sub-bullet or
    an explicitly subordinate continuation as belonging to the bullet directly above it (an
    indented line can never be a "shared note for every bullet" — that shape is always flush
    top-level prose). So: an extra line that starts with whitespace stays attached to its
    item; an extra line that does NOT (flush prose, or a blank line) declines the whole split.
    """
    lines = message.splitlines()
    starts = [i for i, ln in enumerate(lines) if _LIST_ITEM_RE.match(ln)]
    if len(starts) < 2:
        return []
    preamble = "\n".join(lines[: starts[0]]).strip()
    if preamble:
        return []
    items: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        block = lines[start:end]
        if any(not extra[:1].isspace() for extra in block[1:]):
            return []
        text = "\n".join(block).strip()
        text = _LIST_MARKER_STRIP_RE.sub("", text, count=1).strip()
        if text:
            items.append(text)
    return items if len(items) >= 2 else []


# A backtick/double-quote-delimited span ("`alpha; beta`", '"alpha; beta"') protects any
# enumeration marker inside it from being treated as a split point (review finding): a semicolon
# inside inline code/a literal string is punctuation belonging to THAT quoted content, not a
# separator between two asks ("Please preserve literal `alpha; beta gamma delta` in the
# generated output" is ONE request, not two). Deliberately simple (non-overlapping, first-match
# greedy pairing) — good enough for the common inline-code/quote shapes; a message with
# mismatched or nested delimiters just protects less than ideal, never more (a missed split
# opportunity is the safe direction, matching this module's overall "decline over guess" bias).
#
# SINGLE quotes are deliberately NOT included (review finding, a real bug in an earlier version
# that DID include them): English contractions/possessives ("Don't", "user's") use apostrophes
# far more often than genuine quoting, so pairing them up greedily protected everything BETWEEN
# two unrelated apostrophes — "Don't change the login flow; and also update the user's
# documentation" had its real `;` separator falsely hidden inside the "t ... user'" span. That
# violates this module's own "a missed protection is the safe direction" claim (over-protecting
# is the wrong direction, since it SUPPRESSES a real split rather than merely failing to add
# one). Backticks and double quotes don't have this ambiguity in ordinary English prose.
#
# KNOWN LIMITATION, not fixed here (review finding, Medium — flagged for follow-up rather than
# adding delimiter-balance detection to an already deep heuristic): an UNTERMINATED backtick or
# double-quote (`Please preserve "alpha; beta gamma delta`, cut off mid-message — truncated
# input, a copy-paste error) matches no protected span at all, so the semicolon inside it is
# treated as a real separator and the message can still be mis-split. Detecting and declining on
# an odd number of an unpaired delimiter would close this; the common well-formed case (which
# this module optimizes for) is unaffected either way.
_PROTECTED_SPAN_RE = re.compile(r"`[^`]*`|\"[^\"]*\"")


def _protected_spans(message: str) -> list[tuple[int, int]]:
    return [m.span() for m in _PROTECTED_SPAN_RE.finditer(message)]


def _in_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _split_enumeration(message: str) -> list[str]:
    """Split on an explicit enumeration marker/semicolon. `[]` unless every resulting part is
    substantial (`>= _MIN_ASK_WORDS` words) AND there are >=2 of them.

    Three real bugs fixed here (review findings): an EARLIER version dropped any part below the
    word floor and returned the rest — for `"fix login regression now; add tests; update API
    docs"` that silently threw away "add tests" (2 words), returning only the other two parts
    as if that were the complete set of asks: the request to add tests VANISHED, never
    classified or ticketed. The floor exists only to recognize a trailing aside ("...; thanks")
    as NOT a second ask; it must never cause a genuine part to be dropped. So now: if ANY part
    is too thin to trust as its own ask, DECLINE TO SPLIT AT ALL (return `[]`, meaning the
    caller keeps the whole message as one ask) rather than silently losing content. Separately,
    a semicolon alone is not sufficient evidence of two INDEPENDENT asks — a fragment beginning
    with a sequencing word (`_SEQUENCING_FRAGMENT_RE` — "then" or its Russian equivalents) is a
    dependent NEXT STEP of the previous fragment, not a separate request, and also declines the whole
    split (the two steps stay together as one ask, describing one atomic task). And a marker
    INSIDE a quoted/code span (see `_PROTECTED_SPAN_RE`) is never a real split point.
    """
    spans = _protected_spans(message)
    matches = [m for m in _ENUMERATION_SEP_RE.finditer(message) if not _in_any_span(m.start(), spans)]
    if not matches:
        return []
    raw_parts: list[str] = []
    prev_end = 0
    for m in matches:
        raw_parts.append(message[prev_end : m.start()])
        prev_end = m.end()
    raw_parts.append(message[prev_end:])
    parts = [p.strip() for p in raw_parts if p.strip()]
    if len(parts) < 2:
        return []
    if any(len(_ASK_WORD_RE.findall(p)) < _MIN_ASK_WORDS for p in parts):
        return []
    if any(_SEQUENCING_FRAGMENT_RE.match(p) for p in parts):
        return []
    return parts


# A hard cap on how many asks one message may split into (review finding, Medium — a real
# resource-exhaustion risk, not just a quality one): each returned part becomes its own model
# classification call PLUS a potential ticket creation (`cli._classify_multi`), so an unbounded
# split size means an unbounded number of both per inbound message. A message built from
# thousands of list-item lines ("- request number 1\n- request number 2\n...") would otherwise
# split into thousands of parts and fire thousands of classifier calls / ticket creations from
# ONE `classify --create` invocation. 20 is generous for a genuine bundled request (the audit
# this feature is based on never saw more than a handful of asks in one message) while making
# a message THIS long simply decline to split — same "decline over guess" principle as every
# other ambiguity in this module, just triggered by size instead of shape.
_MAX_MULTI_ASK_PARTS = 20


def detect_multi_ask(message: str) -> list[str]:
    """Split an inbound message into distinct asks when it structurally bundles more than one.

    Returns `[message]` (unsplit) when neither structural signal fires — the overwhelmingly
    common case, so an ordinary single-topic message is never touched — OR when a signal fires
    but produces MORE than `_MAX_MULTI_ASK_PARTS` items (review finding: an unbounded split size
    is a resource-exhaustion risk, each part costing its own classifier call and possible ticket
    creation; declining is the safe response, not silently capping and dropping the rest). A
    returned list of length >=2 means the caller should classify/ticket each item separately
    (see `cli.cmd_classify`). See the module comment above for why each signal was chosen and
    why a bare English "and" (or its Russian equivalent) conjunction deliberately is NOT one.
    """
    items = _split_list_items(message)
    if items and len(items) <= _MAX_MULTI_ASK_PARTS:
        return items
    items = _split_enumeration(message)
    if items and len(items) <= _MAX_MULTI_ASK_PARTS:
        return items
    return [message]


# Leading markdown blockquote decoration (`>`, `>>`, `> >`, mixed with spaces/tabs) that a
# model's echo commonly wraps each line in, tolerated per-line when matching an echoed template
# — a QUOTED echo of the prompt is recognized too, not just a byte-identical one. NOTE: this
# tolerates ONLY that one decoration; a fenced code block, a list marker, or a reflowed/
# re-wrapped line (a model that rejoins the prompt's long lines differently) is NOT recognized —
# matching still requires each ORIGINAL line boundary to be preserved. Closing every such
# variant is unbounded (unicode normalization, paraphrase, translation, ...); this stops the
# common byte-identical and quoted-echo cases without chasing every possible reflow.
_QUOTE_CHARS = " \t>"


def _normalize_lines(text: str) -> list[str]:
    """Split ``text`` into lines, normalizing CRLF/CR to LF first — a model's echo commonly
    normalizes the inbound message's own line endings, so the TEMPLATE (which embeds that raw
    message) must be normalized the same way before comparison, or a message containing literal
    ``\\r\\n`` silently defeats every line match (task-cli#80 review, Codex, round 6)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _build_echo_lines(template: str) -> list[str] | None:
    """Normalize ``template`` into its lines for :func:`_strip_echoed_prompt`, or ``None`` for a
    degenerate empty template (which can't be meaningfully "echoed")."""
    lines = _normalize_lines(template)
    return lines if any(lines) else None


# CLASSIFY_PROMPT never changes at runtime, so its lines are computed once at import time rather
# than rebuilt (split + normalize) on every parse_verdict call.
_CLASSIFY_PROMPT_ECHO_LINES = _build_echo_lines(CLASSIFY_PROMPT)


def _strip_echoed_prompt(output: str, template_lines: list[str] | None) -> str:
    """Remove EVERY echo of ``template_lines`` (built by :func:`_build_echo_lines`) found in
    ``output``, via a LINEAR line-by-line scan — deliberately NOT a regex. An earlier regex
    version (even a single flat ``[ \\t>]*`` character class, with no nested/ambiguous
    quantifier) still let the engine retry at every character offset inside a long run of
    quote/whitespace characters with no matching literal after it, which is genuinely quadratic
    in the length of that run — confirmed by hanging for over 20 seconds on a ~200KB adversarial
    input (task-cli#80 review, GLM, round 6). Plain string comparisons here (``==``,
    ``.startswith()``, ``.lstrip()``) have no backtracking, so this scan is O(output length)
    regardless of input shape.

    Requiring EVERY line of the template to match in order (all-but-last exactly, quote-prefix
    tolerant; the last as a quote-tolerant PREFIX, so trailing content glued onto that same line
    — e.g. ``build_prompt``'s message, appended with no separating newline — is left untouched)
    is what makes this safe: a genuine one-line answer can never accidentally satisfy a pattern
    this long, so a real ``"VERDICT: actionable"`` reply is never mistaken for an echo — which
    means removing ALL occurrences (not just the first) is strictly safer, never removing
    genuine content: a model that echoes the whole prompt-plus-message block more than once
    (some repeat the instructions before AND after answering) would otherwise leave an injected
    sentinel alive in a later copy. Matched blocks are blanked (not consumed into a following
    match), so overlapping re-scanning is skipped for speed, not correctness. Returns ``output``
    unchanged if ``template_lines`` is ``None`` or no echo is found.
    """
    if template_lines is None:
        return output
    output_lines = _normalize_lines(output)
    window = len(template_lines)
    total = len(output_lines)
    last_template_line = template_lines[-1]
    result = list(output_lines)
    changed = False
    i = 0
    while i <= total - window:
        last_idx = i + window - 1
        body_matches = all(
            _line_matches_exactly(output_lines[i + offset], template_lines[offset])
            for offset in range(window - 1)
        )
        # Only check the (allocating) last-line prefix match once the cheaper body check
        # already passed — most candidate positions fail on an early body line, so this avoids
        # paying for `_line_starts_with`'s `lstrip` on every position, every call.
        if body_matches:
            starts_with, remainder = _line_starts_with(output_lines[last_idx], last_template_line)
        else:
            starts_with, remainder = False, ""
        if body_matches and starts_with:
            for line_idx in range(i, last_idx):
                result[line_idx] = ""
            result[last_idx] = remainder
            changed = True
            i += window  # skip past the matched block, no overlapping re-scan
        else:
            i += 1
    return "\n".join(result) if changed else output


def _line_matches_exactly(output_line: str, template_line: str) -> bool:
    """True if ``output_line`` IS ``template_line``, optionally preceded by quote-decoration
    characters ONLY (space/tab/``>``). Anchored from the END (``endswith`` + checking only the
    leftover prefix), NOT a leading ``lstrip``, because ``template_line`` itself may genuinely
    start with spaces (e.g. CLASSIFY_PROMPT's own indented bullet lines) — blindly stripping the
    output's leading whitespace before comparing would then never match a byte-identical,
    undecorated echo at all (task-cli#80 review round 6: this exact bug was caught by a doubled-
    echo test failing on CLASSIFY_PROMPT's real indented lines)."""
    if not output_line.endswith(template_line):
        return False
    prefix = output_line[: len(output_line) - len(template_line)]
    return not prefix.strip(_QUOTE_CHARS)  # C-level check: prefix is quote-chars only


def _line_starts_with(output_line: str, template_line: str) -> tuple[bool, str]:
    """True + the genuine remainder if ``output_line``, after an OPTIONAL quote-decoration-only
    prefix, starts with ``template_line`` — used only for the LAST template line, where trailing
    content glued onto the same line (e.g. ``build_prompt``'s message) must be preserved as-is.

    Three attempts, cheapest/most-precise first (task-cli#80 review round 7, Codex/Opus,
    independently — the first two alone still missed the QUOTED-AND-INDENTED combination):
    1. Byte-identical: ``template_line`` can genuinely begin with spaces (e.g. a message whose
       last line is itself indented) — trying this FIRST, before any stripping, means an
       undecorated echo is never broken by stripping its genuine leading whitespace.
    2. Quote-decoration-only prefix on the OUTPUT side: handles a quoted echo of a template
       line that itself has no leading whitespace.
    3. Quote-decoration-only prefix on BOTH sides: handles a quoted echo of a template line
       that ALSO genuinely starts with whitespace — since a quoted model output can't
       distinguish "its own `> ` decoration" from "the real message's leading spaces" any more
       than this matcher can, comparing both sides with the SAME leading run stripped is the
       correct symmetric treatment. This intentionally loses precision on the computed
       remainder in this specific combined case (harmless: a real answer glued directly onto
       an already-indented, already-quoted echoed line with no separating newline does not
       occur in practice)."""
    if output_line.startswith(template_line):
        return True, output_line[len(template_line) :]
    stripped_output = output_line.lstrip(_QUOTE_CHARS)
    if stripped_output.startswith(template_line):
        return True, stripped_output[len(template_line) :]
    stripped_template = template_line.lstrip(_QUOTE_CHARS)
    if stripped_output.startswith(stripped_template):
        return True, stripped_output[len(stripped_template) :]
    return False, ""


def _word_to_verdict(word: str) -> Verdict:
    """Normalize a raw matched token (``"just-ask"``, ``"JUSTASK"``, ``"actionable"``, ...)
    into a :class:`Verdict`. Shared by both the sentinel match and the fallback token scan in
    :func:`parse_verdict` so the two paths can never disagree on how a word maps to a verdict.
    """
    normalized = word.lower().replace(" ", "").replace("-", "")
    if normalized == "change":
        return Verdict.CHANGE
    if normalized == "actionable":
        return Verdict.ACTIONABLE
    return Verdict.JUST_ASK


def _cleaned_output(output: str, message: str | None) -> str:
    """Apply the SEQUENTIAL two-step echo strip (combined ``build_prompt(message)`` pattern
    first, then the bare ``CLASSIFY_PROMPT`` pattern) exactly once. Shared by
    :func:`parse_verdict` and :func:`parse_classification` (review finding: an earlier version
    had each function run this strip independently off its OWN local ``cleaned`` variable —
    correct by construction since both built it the same way, but a future edit to the
    ordering/rationale below only had one chance in two of being applied everywhere). See
    :func:`parse_verdict`'s docstring for the full rationale (echo-injection guard, why the
    order is load-bearing, and the documented residual risk).
    """
    cleaned = output
    if message is not None:
        cleaned = _strip_echoed_prompt(cleaned, _build_echo_lines(build_prompt(message)))
    return _strip_echoed_prompt(cleaned, _CLASSIFY_PROMPT_ECHO_LINES)


def _verdict_from_cleaned(cleaned: str, bias: Verdict) -> Verdict:
    """The verdict-sentinel + fallback-token scan over an ALREADY echo-stripped ``cleaned``
    string. Split out of :func:`parse_verdict` so :func:`parse_classification` can read the
    verdict off the SAME cleaned text it reads the three new axes from, without re-running the
    strip (see :func:`_cleaned_output`)."""
    matches = _VERDICT_RE.findall(cleaned)
    if matches:
        return _word_to_verdict(matches[-1])

    # fallback: no sentinel. Take the last decisive standalone token — `change`/`justAsk`
    # only; see the module-level note above on why `actionable` is excluded here.
    tokens = re.findall(r"\b(change|justask|just-ask|just ask)\b", cleaned.lower())
    if tokens:
        return _word_to_verdict(tokens[-1])
    return bias


_T = TypeVar("_T")


def _last_match(
    pattern: re.Pattern[str], text: str, default: _T, transform: Callable[[str], _T]
) -> _T:
    """The transformed LAST match of ``pattern`` in ``text``, or ``default`` if none matched —
    the shared "read the last `LABEL: value` sentinel, else fall back" idiom every axis in
    :func:`parse_classification` uses (review finding: three copies of the same four-line
    findall/transform/default shape, differing only in regex/transform/default). Generic on
    ``_T`` (review finding) so each axis's declared type — ``Classification.priority``'s
    ``Literal["P0", ...]``, ``story_points``'s ``Literal[1, 2, ...]`` — is carried through
    ``transform``/``default`` rather than erased to ``Any``; a future regex broadened to admit
    a value outside that Literal (e.g. a `P4`) becomes a type-checker-visible mismatch at the
    call site instead of a silent runtime pass-through.
    """
    matches = pattern.findall(text)
    return transform(matches[-1]) if matches else default


def parse_verdict(output: str, *, bias: Verdict = Verdict.CHANGE, message: str | None = None) -> Verdict:
    """Parse ``review just-ask`` output into a verdict, biasing on ambiguity.

    Primary signal: the model is asked to emit a ``VERDICT: <word>`` line — we key off the
    LAST such sentinel (robust, can't collide with the prompt body, which no longer contains
    the bare verdict words). Fallback for a model that ignores the format: scan the output for
    a standalone ``change``/``justAsk`` token and take the last one. ``actionable`` is
    deliberately EXCLUDED from this fallback scan (task-cli#80 review finding): unlike
    ``justAsk``, "actionable" is ordinary English prose ("this change request is directly
    actionable"), so treating a bare occurrence as decisive would silently swallow a genuine
    ``change`` signal with no ticket AND no execution (execution is out of scope for this
    repo). ``actionable`` is reachable ONLY via the explicit ``VERDICT:`` sentinel. Failing
    both scans, ``bias`` (never ``actionable`` unless the caller explicitly configured it as
    the bias — the default bias exists for the change/justAsk ambiguity this feature does NOT
    touch, not for "no signal at all").

    ``message`` (optional, the ORIGINAL untrusted inbound text passed to :func:`build_prompt`)
    guards against echoed-message injection (task-cli#80 review, Codex/Opus, 2 rounds): if a
    model echoes the untrusted inbound message back in its answer, and that message happens to
    contain literal text like ``"VERDICT: actionable"``, the sentinel regex could match the
    echoed text instead of the model's real answer. An earlier attempted fix (stripping the raw
    message substring from ``output`` directly) was reverted: it can erase the model's OWN
    genuine sentinel whenever the message text happens to overlap with it, and a
    whitespace-only message triggers a pathological ``str.replace("", " ")`` that corrupts the
    entire output. Instead, when ``message`` is given, a RECOGNIZED ECHOED BLOCK is stripped via
    :func:`_strip_echoed_prompt` — a line-by-line, quote/whitespace-tolerant match of the full
    prompt-plus-message the model was asked to answer (``build_prompt(message)``), never a bare
    fragment of the message on its own. Requiring every line of the multi-line prompt to be
    present (each escaped literally, so no single short/common word can match on its own) is
    what keeps this safe against corrupting a real one-line answer that happens to reuse a word
    from the message — a genuine ``"VERDICT: actionable"`` answer can never itself satisfy a
    pattern built from a much longer, multi-line prompt template. RESIDUAL RISK, not closed by
    this (task-cli#80 review, Codex/Opus, several rounds): matching requires the echo to
    preserve the template's original LINE BOUNDARIES — a model that reflows/re-wraps the
    instructions, drops the surrounding template and echoes only the raw message, embeds the
    echo mid-line (e.g. ``"Here is the prompt: <the whole template>..."`` on one physical line —
    the OLD substring-based strip caught this, this line-anchored one does not, a real if minor
    narrowing), uses a decoration this module doesn't tolerate (a fenced code block, a list
    marker, a differently normalized whitespace run), or paraphrases, produces an echo this
    strip does not recognize, and any injected sentinel inside it survives. This is NOT a
    wholesale new exposure — the ORIGINAL ``CLASSIFY_PROMPT``-only strip this module already had
    for ``change``/``justAsk`` was ALSO byte-identical-only — this change adds the message
    portion, quoted echoes, and doubled echoes to what's caught, at the cost of the mid-line
    case above, without fully closing the class of risk either version had. Closing every
    reflow variant is an unbounded problem (there is always another paraphrase to handle); this
    stops the common, low-effort echo shapes without chasing all of them.

    The two strips (:func:`_cleaned_output`) are SEQUENTIAL, not either/or (task-cli#80 review
    round 3, Codex/Opus): a model very often echoes back the fixed INSTRUCTIONS without also
    echoing the user's message verbatim (reflowed, truncated, or simply not repeated) — if only
    the combined ``build_prompt(message)`` pattern were tried, that partial echo wouldn't match
    at all, and ``CLASSIFY_PROMPT``'s own instruction words (which literally include "justAsk")
    would leak into the sentinel/fallback scans below, misreading the echoed instructions as the
    verdict. Stripping the LONGER combined pattern first is still required: stripping
    ``CLASSIFY_PROMPT`` first would break apart a genuine full echo, leaving the message half's
    injected sentinel to survive on its own.
    """
    return _verdict_from_cleaned(_cleaned_output(output, message), bias)


def parse_classification(
    output: str, *, bias: Verdict = Verdict.CHANGE, message: str | None = None
) -> Classification:
    """Parse ``review just-ask``'s multi-line answer into a full :class:`Classification`.

    The VERDICT axis is read the SAME way :func:`parse_verdict` reads it (same echo-stripping,
    same injection guards, same ``bias``/``message`` handling — see its docstring), off the
    identical cleaned text the three NEW axes are read from (:func:`_cleaned_output` is applied
    exactly ONCE, shared, not duplicated per axis). Each axis is then read independently from
    the LAST ``LABEL: value`` sentinel match via its own regex (``_PROCESS_PRODUCT_RE`` /
    ``_PRIORITY_RE`` / ``_SP_RE``, mirroring ``_VERDICT_RE``'s shape, applied through the shared
    :func:`_last_match` idiom), so a doubled or partial echo is handled the same way the verdict
    sentinel already is.

    An axis whose sentinel is absent or doesn't cleanly resolve degrades to its own SAFE
    DEFAULT (module comment above, and :func:`default_classification`) rather than aborting the
    whole classification — a bad or truncated model reply must never crash ``task classify``,
    only under-inform it. Unlike ``parse_verdict``'s ``bias``, these three defaults are fixed
    (never caller-configurable — see the module comment for why). Each regex requires its
    captured token to be the ENTIRE remainder of its line — see the module comment directly
    above the three regex definitions for why a looser boundary check is not enough.
    """
    cleaned = _cleaned_output(output, message)
    verdict = _verdict_from_cleaned(cleaned, bias)
    process_or_product = _last_match(
        _PROCESS_PRODUCT_RE, cleaned, _DEFAULT_PROCESS_OR_PRODUCT, str.lower
    )
    priority = _last_match(_PRIORITY_RE, cleaned, _DEFAULT_PRIORITY, str.upper)
    story_points = _last_match(_SP_RE, cleaned, _DEFAULT_STORY_POINTS, int)

    return Classification(
        verdict=verdict,
        process_or_product=process_or_product,
        priority=priority,
        story_points=story_points,
    )
