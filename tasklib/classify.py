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
from dataclasses import dataclass
from enum import Enum

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
    "Respond with EXACTLY one line and nothing else: the literal word VERDICT, a colon, "
    "then your one-word answer (change/actionable/justAsk).\n\nMESSAGE:\n"
)

# The sentinel the model is asked to emit. Parsing keys off this first; it is unambiguous and
# cannot collide with the prompt body (which deliberately avoids the bare verdict words now).
_VERDICT_RE = re.compile(r"verdict\s*:\s*(change|actionable|justask|just-ask|just ask)", re.IGNORECASE)


class Verdict(str, Enum):
    CHANGE = "change"
    ACTIONABLE = "actionable"
    JUST_ASK = "justAsk"


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
# the provider is reachable. ollama is reachable when the local daemon binary exists.
_PROVIDER_MODEL_PREFIX = {
    "anthropic": "claude",
    "openai": "commandcode",  # OpenAI models are routed through the commandcode gateway
    "commandcode": "commandcode",
    "zai": "zai",
    "google": "gemini",
    "ollama": "ollama",
}

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
    """Is a provider reachable? Key present in env, or (ollama) the daemon binary exists.

    Pure-ish: reads ``env`` (defaults to ``os.environ``) and, for ollama only, checks for the
    binary on PATH. No network probe — availability here means "we have a credible way to
    reach it", matching review-cli's startup failover posture.
    """
    env = os.environ if env is None else env
    if provider == "ollama":
        return shutil.which("ollama") is not None
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

    The two strips below are SEQUENTIAL, not either/or (task-cli#80 review round 3, Codex/Opus):
    a model very often echoes back the fixed INSTRUCTIONS without also echoing the user's
    message verbatim (reflowed, truncated, or simply not repeated) — if only the combined
    ``build_prompt(message)`` pattern were tried, that partial echo wouldn't match at all, and
    ``CLASSIFY_PROMPT``'s own instruction words (which literally include "justAsk") would leak
    into the sentinel/fallback scans below, misreading the echoed instructions as the verdict.
    Stripping the LONGER combined pattern first is still required: stripping ``CLASSIFY_PROMPT``
    first would break apart a genuine full echo, leaving the message half's injected sentinel to
    survive on its own.
    """
    cleaned = output
    if message is not None:
        cleaned = _strip_echoed_prompt(cleaned, _build_echo_lines(build_prompt(message)))
    cleaned = _strip_echoed_prompt(cleaned, _CLASSIFY_PROMPT_ECHO_LINES)

    matches = _VERDICT_RE.findall(cleaned)
    if matches:
        return _word_to_verdict(matches[-1])

    # fallback: no sentinel. Take the last decisive standalone token — `change`/`justAsk`
    # only; see the module-level note above on why `actionable` is excluded here.
    tokens = re.findall(r"\b(change|justask|just-ask|just ask)\b", cleaned.lower())
    if tokens:
        return _word_to_verdict(tokens[-1])
    return bias
