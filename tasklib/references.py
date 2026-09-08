"""Detect un-linked references in ticket text — the "related entities must be LINKS" rule.

Pure, stdlib-only. A ticket that *names* another entity — a tracker id (``HYP-789``), an
issue/PR (``#123``), a commit SHA, a repo/path slug — must reference it as a proper LINK (a
markdown link ``[text](url)`` or a full URL), not a bare token a reader can't click through.
:func:`find_unlinked_references` returns the bare tokens that are NOT already linked, so the
``links`` gate (``policy.py``) can refuse creation/edit naming each match and how to link it.

The matcher is deliberately BROAD (it over-matches rather than miss a real reference); the
escape hatch (``--force "<reason>"`` / ``--skip-links``) is the precise tool for a genuine
false positive. Already-linked content (markdown links and bare URLs) is masked out first, so a
reference that IS a proper link never trips the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class UnlinkedRef:
    """One bare reference that should be a link. ``how`` tells the user how to link it."""

    text: str
    kind: str
    how: str


# A tracker id: an uppercase project key + number (HYP-789, ABC-12). High precision.
_TICKET_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
# Common acronym-dash-number tokens that LOOK like a tracker id but aren't (encodings, hashes,
# standards). Excluded so the gate doesn't fire on every "UTF-8"/"SHA-256"/"ISO-8601". This is a
# deliberate false-NEGATIVE: a real tracker key that happens to collide with one of these would
# go unflagged — accepted because these acronyms are far more common in ticket prose.
_TICKET_ID_DENY_PREFIXES = frozenset(
    {
        "UTF", "SHA", "SHA1", "SHA256", "SHA512", "MD5", "AES", "RSA", "ISO", "RFC", "ASCII",
        "COVID", "SARS", "IPV4", "IPV6", "UTC", "GMT", "BASE64", "EC2", "S3", "H264", "H265",
    }
)
# A bare issue/PR ref (#123) not part of a path/word (so an anchor like `a#b` is ignored).
_HASH_REF_RE = re.compile(r"(?<![\w/#])#(\d+)\b")
# A commit SHA: 7–40 hex chars requiring BOTH a hex letter and a digit — so a pure decimal number
# ("1000000 requests") and an all-letter hex word ("defaced") are excluded, while "6103ec8" matches.
_SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
# A repo/path slug: word(/word)+. The single-slash case is filtered in _find_repo_slugs so common
# English/jargon pairs ("and/or", "input/output", "TS/TSX") and numeric dates/ratios aren't flagged.
_SLUG_RE = re.compile(r"(?<![\w./@-])([A-Za-z0-9][\w.-]*(?:/[A-Za-z0-9][\w.-]*)+)")
_SLUG_SIGNAL_RE = re.compile(r"[.\-_0-9]")
_HAS_ALPHA_RE = re.compile(r"[A-Za-z]")
# Sentence punctuation the slug regex swallows at the end of a phrase (`reads/writes.`): stripped
# before a segment is judged, so a trailing full stop is never the "signal" that flags an
# ordinary English pair (task-cli#126).
_SLUG_TRAILING_PUNCT = ".,;:!?"
# A segment that is a CSS length / plain measurement (`48px`, `1.5em`, `100%`): a pair of those
# (`48px/48px`) is a size ratio in prose, not a repo/path (task-cli#125).
_MEASUREMENT_RE = re.compile(r"^\d+(?:\.\d+)?(?:px|em|rem|pt|vh|vw|%|ms|s|x)?$", re.IGNORECASE)
# A last segment carrying a file extension (`docs/index.mdx`, `src/app.ts`): a FILENAME named in
# prose, which has no link target of its own (it needs a repo + ref to become one). Treated as
# prose, not a reference (task-cli#126) — a canonical file link carries a host and is a URL.
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
# Context that marks an all-decimal `#RRGGBB` as a CSS colour rather than an issue number: a
# colour word within the surrounding window. `#767676` and `#292929` are structurally identical
# to six-digit issue refs; only the sentence around them tells (task-cli#126).
_COLOUR_CONTEXT_RE = re.compile(
    r"\b(?:colou?rs?|contrast|background|foreground|hex|rgba?|palette|swatch|theme|shade|tint)\b",
    re.IGNORECASE,
)
_COLOUR_WINDOW = 80
# A sha "standing alone as a reference" (`regressed in 6103ec8`, `see 6103ec8`, a bare sha on its
# own line) is flagged; a sha QUOTED inside a prose sentence — surrounded by more than this many
# other words on its line, or set in backticks — is evidence, not a reference (task-cli#126).
_SHA_STANDALONE_MAX_OTHER_WORDS = 3
_WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")


def find_unlinked_references(text: str) -> list[UnlinkedRef]:
    """Return the bare (un-linked) references in ``text``, de-duplicated and grouped by kind
    (tracker ids, then ``#refs``, then SHAs, then repo/path slugs)."""
    masked = _mask_links(text)
    refs: list[UnlinkedRef] = []
    seen: set[str] = set()

    def add(token: str, kind: str, how: str) -> None:
        if token not in seen:
            seen.add(token)
            refs.append(UnlinkedRef(text=token, kind=kind, how=how))

    for m in _TICKET_ID_RE.finditer(masked):
        tok = m.group(1)
        if tok.split("-", 1)[0] in _TICKET_ID_DENY_PREFIXES:
            continue  # an encoding/hash/standard token, not a tracker id
        add(tok, "tracker id", f"link it: [{tok}](<tracker-url>)")
    for m in _HASH_REF_RE.finditer(masked):
        if _is_css_colour(masked, m):
            continue
        tok = "#" + m.group(1)
        add(tok, "issue/PR ref", f"link it: [{tok}](<issue-or-pr-url>)")
    for m in _SHA_RE.finditer(masked):
        if not _sha_stands_alone(masked, m):
            continue
        tok = m.group(0)
        add(tok, "commit sha", f"link it: [{tok}](<commit-url>)")
    for tok in _find_repo_slugs(masked):
        add(tok, "repo/path", f"link it: [{tok}](<url>) or paste the full URL")
    return refs


def _mask_links(text: str) -> str:
    """Blank out already-linked content (markdown links + bare/auto URLs) before scanning.

    Replaces each with same-length spaces so a reference that is ALREADY a proper link is never
    re-flagged as bare (the whole point of the gate is to require links, not forbid references).
    """
    def blank(m: re.Match[str]) -> str:
        return " " * len(m.group(0))

    text = re.sub(r"\[[^\]]*\]\([^)]*\)", blank, text)  # [text](url)
    text = re.sub(r"<https?://[^>]+>", blank, text)  # <http://...>
    text = re.sub(r"https?://\S+", blank, text)  # bare http(s) URL
    return text


def _find_repo_slugs(text: str) -> list[str]:
    """Bare ``owner/repo`` or path slugs, filtered to avoid common English/jargon pairs.

    A 3+ segment slug (``a/b/c``, a real path) or a single-slash slug with a "signal" char (a
    dot, dash, underscore, or digit) is a candidate — so ``and/or`` and ``input/output`` pass.
    A candidate is only flagged when at least one segment contains a LETTER, so numeric
    dates/ratios (``06/28``, ``24/7``, ``9/11``) are not mistaken for a repo/path, while
    ``owner/task-cli`` and ``tasklib/cli.py`` are flagged.

    A trailing sentence stop is stripped first (``reads/writes.`` is the English pair), a last
    segment with a file extension is a FILENAME named in prose (``docs/index.mdx``), and a pair
    of measurements (``48px/48px``) is a ratio — none of those is flagged (task-cli#126/#125).

    DELIBERATE false-negative: a two-segment, all-LETTER single-slash slug (``facebook/react``,
    ``django/django``) is NOT flagged — it is indistinguishable in prose from an ordinary
    English/jargon pair (``read/write``, ``input/output``, ``client/server``) without a
    word-list, and over-flagging those would make the gate fire on normal sentences. The common
    canonical GitHub reference carries a host (``github.com/owner/repo``) and is caught as a URL;
    a genuinely bare ``owner/repo`` that should be a link stays the author's call (and any of the
    other signal forms — a ``.``/``-``/digit, or a 3-segment path — is still flagged).
    """
    out: list[str] = []
    for m in _SLUG_RE.finditer(text):
        slug = m.group(1).rstrip(_SLUG_TRAILING_PUNCT)
        segs = slug.split("/")
        if len(segs) < 2 or _FILE_EXT_RE.search(segs[-1]):
            continue  # a bare word once the sentence stop is gone, or a filename named in prose
        if all(_MEASUREMENT_RE.match(seg) for seg in segs):
            continue  # `48px/48px`, `24/7`: a ratio or size pair
        candidate = len(segs) >= 3 or any(_SLUG_SIGNAL_RE.search(seg) for seg in segs)
        if candidate and any(_HAS_ALPHA_RE.search(seg) for seg in segs):
            out.append(slug)
    return out


def _is_css_colour(text: str, m: re.Match[str]) -> bool:
    """An all-decimal `#NNNNNN` is a CSS colour when a colour word sits in the window around it."""
    if len(m.group(1)) != 6:
        return False
    window = text[max(0, m.start() - _COLOUR_WINDOW): m.end() + _COLOUR_WINDOW]
    return bool(_COLOUR_CONTEXT_RE.search(window))


def _sha_stands_alone(text: str, m: re.Match[str]) -> bool:
    """True when the sha is a REFERENCE (few other words on its line, not in backticks) rather
    than a value quoted inside a prose sentence."""
    if m.start() > 0 and text[m.start() - 1] == "`" and text[m.end(): m.end() + 1] == "`":
        return False
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start: len(text) if line_end < 0 else line_end]
    others = [w for w in _WORD_RE.findall(line) if w != m.group(0)]
    return len(others) <= _SHA_STANDALONE_MAX_OTHER_WORDS
