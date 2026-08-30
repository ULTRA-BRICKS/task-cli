"""Ticket ↔ structured markdown body. The single source of truth for the §5 template.

The body is **derived** from a ``Ticket``'s fields, never hand-written, so a ticket and its
PR speak one shape (the same section set as agent-tools' ``pull_request_template.md``).
``render()`` serializes; ``parse()`` reads a body back into a ``Ticket`` (round-trip);
``validate_format()`` is what the formatting gate calls — it checks an arbitrary body
conforms to the fixed section order and headings.

Pure string work — no I/O, no provider knowledge. Section order is FIXED and is the contract.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .model import Criterion, Screenshot, Ticket

# Fixed section order — the contract. Changing this is a breaking change to the template.
SECTIONS: tuple[str, ...] = (
    "What",
    "Why (motivation)",
    "User impact",
    "Cost of inaction",
    "Acceptance criteria",
    "Screenshots",
    "Links",
)

_SKIP_MARKER = "Skipped gates"  # an optional trailing section recording escape hatches
# An optional trailing section carrying the daemon-watched due date. Like Skipped gates it is
# NOT in SECTIONS — keeping it out of the fixed contract means every pre-existing ticket (no
# due date) still passes validate_format, and a ticket with a due date stays format-valid.
_DUE_MARKER = "Due"


def render(ticket: Ticket) -> str:
    """Serialize a ``Ticket`` to the canonical markdown body."""
    out: list[str] = []
    out.append(f"## What\n{ticket.what.strip()}")
    out.append(f"## Why (motivation)\n{ticket.why.strip()}")
    out.append(f"## User impact\n{ticket.user_impact.strip()}")
    out.append(f"## Cost of inaction\n{ticket.cost_of_inaction.strip()}")

    acc = "\n".join(_render_criterion(c) for c in ticket.acceptance) if ticket.acceptance else "- [ ] (none specified)"
    out.append(f"## Acceptance criteria\n{acc}")

    if ticket.screenshots:
        shots = "\n".join(_render_shot(s) for s in ticket.screenshots)
    else:
        shots = "(none)"
    out.append(f"## Screenshots\n{shots}")

    links = _render_links(ticket)
    out.append(f"## Links\n{links}")

    if ticket.due.strip():
        out.append(f"## {_DUE_MARKER}\n{ticket.due.strip()}")

    if ticket.skips:
        lines = "\n".join(f"- {gate}: {reason}" for gate, reason in sorted(ticket.skips.items()))
        out.append(f"## {_SKIP_MARKER}\n{lines}")

    return "\n\n".join(out) + "\n"


def _render_criterion(c: Criterion) -> str:
    """One acceptance-criterion checkbox line. A checked criterion carries its visual proof
    (``— proof: ![proof](ref)``) or, when forced, the recorded reason (``— forced: <reason>``)."""
    box = "x" if c.checked else " "
    line = f"- [{box}] {c.text}"
    if c.checked:
        if c.proof:
            line += f" — proof: ![proof]({c.proof})"
        elif c.force_reason:
            line += f" — forced: {c.force_reason}"
    return line


def _render_shot(shot: Screenshot) -> str:
    caption = f" — {shot.caption}" if shot.caption else ""
    # markdown image when it looks like a URL/path, plain ref otherwise
    return f"- {shot.kind}: ![{shot.kind}]({shot.ref}){caption}"


def _render_links(ticket: Ticket) -> str:
    if not ticket.links:
        return "- (none)"
    return "\n".join(f"- {key}: {val}" for key, val in ticket.links.items())


# ── parsing (round-trip) ──────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split_sections(body: str) -> dict[str, str]:
    """Split a markdown body into ``{heading: content}``. Tolerant of extra whitespace.

    Headings are matched on ``## <name>`` lines; content is everything until the next
    heading. Unknown headings are kept (so ``Skipped gates`` and stray sections survive a
    round-trip); the caller decides what is required.
    """
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def parse(body: str, ticket: Ticket | None = None) -> Ticket:
    """Parse a canonical body back into a ``Ticket`` (round-trip with :func:`render`).

    If ``ticket`` is given, its non-body fields (id, url, state, title, labels) are kept and
    only the body-derived fields are overwritten. ``raw_body`` always records the input.
    """
    base = ticket or Ticket()
    sec = split_sections(body)

    acceptance = _parse_criteria(sec.get("Acceptance criteria", ""))
    screenshots = _parse_screenshots(sec.get("Screenshots", ""))
    links = _parse_links(sec.get("Links", ""))
    skips = _parse_skips(sec.get(_SKIP_MARKER, ""))
    # The Due section is a single ISO date on its own line; fall back to the base ticket's due
    # so a backend that supplied the date natively (Linear dueDate) isn't clobbered by a body
    # that happens to omit the section.
    due = sec.get(_DUE_MARKER, "").strip() or base.due

    return replace(
        base,
        what=sec.get("What", "").strip(),
        why=sec.get("Why (motivation)", "").strip(),
        user_impact=sec.get("User impact", "").strip(),
        cost_of_inaction=sec.get("Cost of inaction", "").strip(),
        acceptance=acceptance,
        screenshots=screenshots,
        links=links or base.links,
        skips=skips or base.skips,
        due=due,
        raw_body=body,
    )


_CRITERION_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")
# `\s*` (zero-or-more), not `\s+`, after the marker (review finding, task-cli#F4): every
# per-section parser this feeds already tolerated a zero-space marker (`-[x]`, `-PR: ...`)
# before this function existed — requiring at least one space here silently dropped those
# lines as unrecognized prose instead. Safe to relax: each downstream regex still gates on
# its own shape (a checkbox `[`, a `key:` colon, ...), so this alone doesn't un-anchor
# bullet detection.
_BULLET_ITEM_RE = re.compile(r"^\s*[-*]\s*(.*)$")


def _join_bullet_blocks(text: str) -> list[str]:
    """Group a bulleted section's raw lines into one reconstructed ``"- <content>"`` line per
    bullet item, so every line-anchored parser regex below keeps working unchanged.

    This exists because Linear's server-side markdown reformatting (confirmed live,
    HYP-1355/1356/1398) rewrites a saved description on every ``issueUpdate``: the bullet
    marker becomes ``*`` instead of ``-``, and — critically — a bullet item whose content
    includes an inline image gets that image (and any trailing ``— caption``/``— proof``
    text after it) pushed onto separate, blank-line-separated, INDENTED continuation lines
    instead of staying on the bullet's own line. A line-by-line parser that only ever looks
    at one line per item silently sees an empty/truncated item on the very next parse — this
    is the confirmed mechanism behind task-cli losing Screenshots, Links, and
    checked-criterion proof references after nothing more than a single Linear round-trip.

    A continuation line joins its owning bullet only when it BOTH (a) is indented (has
    leading whitespace — every confirmed Linear reformatting sample is), (b) starts with
    ``![`` or ``—`` (an image line, or a trailing caption/proof line), AND (c) immediately
    follows the bullet or an already-joined continuation, with nothing but blank lines in
    between — tracked via ``continuable`` below. Any column-0, non-blank, non-bullet line
    (hand-added prose under a list, or the SAME shape appearing later in the section) breaks
    that chain permanently for the current bullet, even across further blank lines. Anything
    that doesn't join is left alone, never joined. All three guards are load-bearing (review
    findings, task-cli#F1): joining unconditionally corrupted Links (embedded whitespace
    fails ``is_http_url``), double-wrapped Screenshot image markdown, and polluted Criterion
    text; joining an un-indented ``![``/``—`` line reopened the same corruption for a
    column-0 hand-pasted GitHub screenshot; and — without the "immediately follows" guard —
    an indented ``![``/``—`` line appearing ANYWHERE later in the section (even past
    intervening prose paragraphs) still silently glued onto a distant, unrelated bullet.
    """
    blocks: list[list[str]] = []
    continuable = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue  # a blank line never breaks a continuation chain (Linear's own shape)
        m = _BULLET_ITEM_RE.match(raw_line)
        if m:
            blocks.append([m.group(1).strip()])
            continuable = True
            continue
        if (
            continuable
            and blocks
            and raw_line.startswith((" ", "\t"))
            and (line.startswith("![") or line.startswith("—"))
        ):
            blocks[-1].append(line)
            continuable = True  # a second continuation line (image, then a caption line) is fine
            continue
        # a column-0 line, a non-image/caption-shaped indented line, or a continuation-shaped
        # line that no longer immediately follows its bullet — breaks the chain
        continuable = False
    return ["- " + " ".join(parts) for parts in blocks]


def _bullet_lines(text: str) -> list[str]:
    """Return one reconstructed ``"- <content>"`` line per bullet item, with NO continuation
    joining (review finding, task-cli#F1-v2) — for sections whose confirmed Linear
    reformatting never produces a continuation line in the first place.

    Links and Skipped-gates values are only ever auto-linkified INLINE (``url`` ->
    ``[url](<url>)``, handled entirely by ``_unwrap_link_value`` on a single line) — Linear's
    confirmed continuation-line behavior (HYP-1355/1356/1398) is specific to bullets whose
    content includes an inline IMAGE (Screenshots, checked-criterion proof), which Links/Skips
    never contain. Routing Links/Skips through the joining ``_join_bullet_blocks`` instead of
    this function let a plausible, unrelated human edit — an indented note added directly
    under a Links bullet in the tracker UI — glue onto and corrupt that bullet's value, since
    an indented ``—``-line legitimately joining a *Screenshots* bullet is exactly what the
    join guards are designed to allow.
    """
    lines: list[str] = []
    for raw_line in text.splitlines():
        m = _BULLET_ITEM_RE.match(raw_line)
        if m:
            lines.append("- " + m.group(1).strip())
    return lines


_LINK_VALUE_RE = re.compile(r"^\[([^\]]*)\]\((.+)\)$")


def _unwrap_link_value(val: str) -> str:
    """Undo Linear's auto-linkify wrapping of a bare URL value (``url`` -> ``[url](<url>)``
    on save, confirmed live) back to the plain URL/text task-cli originally wrote.

    Only unwraps when the visible label equals the target URL — Linear's own auto-linkify
    always echoes the bare URL as both, so this never touches a genuinely user-authored
    labeled link like ``[design review](https://figma.com/x)`` (review finding,
    task-cli#F2). The target capture is greedy-to-the-last-``)`` (anchored at end of
    string) rather than stopping at the first ``)``, so a URL that itself contains a
    parenthesis (e.g. a Wikipedia article title) still round-trips.
    """
    m = _LINK_VALUE_RE.match(val)
    if not m:
        return val
    label, wrapped = m.group(1).strip(), m.group(2).strip()
    target = wrapped[1:-1].strip() if wrapped.startswith("<") and wrapped.endswith(">") else wrapped
    return target if label == target else val


def _parse_criteria(text: str) -> list[Criterion]:
    """Parse the Acceptance-criteria checkbox lines back into Criterion objects (round-trip).

    Reads the checkbox state (``[x]``/``[ ]``) and pulls the trailing proof/forced marker off
    the criterion text so a checked-with-proof criterion survives a render→parse round-trip.
    """
    items: list[Criterion] = []
    for line in _join_bullet_blocks(text):
        m = _CRITERION_RE.match(line)
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        crit_text, proof, force_reason = _split_criterion_rest(m.group(2).strip())
        if crit_text and crit_text.lower() != "(none specified)":
            items.append(Criterion(text=crit_text, checked=checked, proof=proof, force_reason=force_reason))
    return items


def _split_criterion_rest(rest: str) -> tuple[str, str, str]:
    """Split a criterion's text from its trailing ``— proof: …`` / ``— forced: …`` marker.

    Returns ``(text, proof_ref, force_reason)``. Anchored on the specific ``— proof:`` /
    ``— forced:`` markers (unlikely in a normal criterion), so ordinary em-dashes in the text
    are left alone. The proof capture is GREEDY to the trailing ``)`` so a path/URL that itself
    contains parens (e.g. ``img(1).png``) round-trips intact instead of truncating at the first
    ``)`` and falling through to the bare branch (which would re-capture the whole ``![…](…)``
    wrapper and progressively double-wrap on the next render).
    """
    m = re.search(r"\s+—\s+proof:\s*!\[[^\]]*\]\((.+)\)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), m.group(1).strip(), ""
    m = re.search(r"\s+—\s+proof:\s*(\S.*?)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), m.group(1).strip(), ""
    m = re.search(r"\s+—\s+forced:\s*(\S.*?)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), "", m.group(1).strip()
    return rest, "", ""


def _parse_screenshots(text: str) -> list[Screenshot]:
    shots: list[Screenshot] = []
    for line in _join_bullet_blocks(text):
        # "- <kind>: ![alt](ref) — caption"
        m = re.match(r"^-\s*(\w+)\s*:\s*!\[[^\]]*\]\(([^)]+)\)(?:\s*—\s*(.*))?$", line)
        if m:
            shots.append(Screenshot(ref=m.group(2).strip(), kind=m.group(1).strip(), caption=(m.group(3) or "").strip()))
            continue
        # bare "- <kind>: <ref>"
        m = re.match(r"^-\s*(\w+)\s*:\s*(\S+)", line)
        if m and m.group(2).lower() != "(none)":
            shots.append(Screenshot(ref=m.group(2).strip(), kind=m.group(1).strip()))
    return shots


def _parse_links(text: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for line in _bullet_lines(text):
        m = re.match(r"^\s*-\s*([^:]+):\s*(.+?)\s*$", line)
        if m:
            key, val = m.group(1).strip(), _unwrap_link_value(m.group(2).strip())
            if val and val != "(none)":
                links[key] = val
    return links


def _parse_skips(text: str) -> dict[str, str]:
    skips: dict[str, str] = {}
    for line in _bullet_lines(text):
        m = re.match(r"^\s*-\s*([^:]+):\s*(.+?)\s*$", line)
        if m:
            skips[m.group(1).strip()] = _unwrap_link_value(m.group(2).strip())
    return skips


# ── formatting gate ────────────────────────────────────────────────────────────────


def validate_format(body: str) -> list[str]:
    """Return a list of formatting violations (empty = the body matches the template).

    Required: every section in :data:`SECTIONS` is present, in that order. Extra trailing
    sections (e.g. ``Skipped gates``) are allowed. This is exactly what the ``formatting``
    gate enforces.
    """
    problems: list[str] = []
    headings = [m.group(1).strip() for m in _HEADING_RE.finditer(body)]
    present = set(headings)

    for required in SECTIONS:
        if required not in present:
            problems.append(f"missing required section: ## {required}")

    if problems:
        return problems  # order check is meaningless if sections are missing

    # order: the required sections must appear in the canonical order (ignoring extras)
    canonical_positions = [headings.index(s) for s in SECTIONS]
    if canonical_positions != sorted(canonical_positions):
        problems.append("sections are out of order; expected: " + " → ".join(SECTIONS))

    return problems
