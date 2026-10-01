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
# "Deploy" (deploy-followup-links) sits between Screenshots and Links: it always renders (even
# "Deployed: no"), unlike the OPTIONAL Due/Skipped-gates trailers below — but parse() still
# tolerates a body with no "## Deploy" heading at all (every ticket rendered before this field
# existed), defaulting to not-deployed rather than crashing.
SECTIONS: tuple[str, ...] = (
    "What",
    "Why (motivation)",
    "User impact",
    "Cost of inaction",
    "Acceptance criteria",
    "Screenshots",
    "Deploy",
    "Links",
)

_DEPLOY_MARKER = "Deploy"
_SKIP_MARKER = "Skipped gates"  # an optional trailing section recording escape hatches
# An optional trailing section carrying the daemon-watched due date. Like Skipped gates it is
# NOT in SECTIONS — keeping it out of the fixed contract means every pre-existing ticket (no
# due date) still passes validate_format, and a ticket with a due date stays format-valid.
_DUE_MARKER = "Due"


def render(ticket: Ticket) -> str:
    """Serialize a ``Ticket`` to the canonical markdown body."""
    out: list[str] = []
    out.append(f"## What\n{_escape_embedded_headings(ticket.what.strip())}")
    out.append(f"## Why (motivation)\n{_escape_embedded_headings(ticket.why.strip())}")
    out.append(f"## User impact\n{_escape_embedded_headings(ticket.user_impact.strip())}")
    out.append(f"## Cost of inaction\n{_escape_embedded_headings(ticket.cost_of_inaction.strip())}")

    acc = "\n".join(_render_criterion(c) for c in ticket.acceptance) if ticket.acceptance else "- [ ] (none specified)"
    out.append(f"## Acceptance criteria\n{acc}")

    if ticket.screenshots:
        shots = "\n".join(_render_shot(s) for s in ticket.screenshots)
    else:
        shots = "(none)"
    out.append(f"## Screenshots\n{shots}")

    out.append(f"## {_DEPLOY_MARKER}\n{_render_deploy(ticket)}")

    links = _render_links(ticket)
    out.append(f"## Links\n{links}")

    if ticket.due.strip():
        out.append(f"## {_DUE_MARKER}\n{ticket.due.strip()}")

    if ticket.skips:
        lines = "\n".join(_render_skip(gate, reason) for gate, reason in sorted(ticket.skips.items()))
        out.append(f"## {_SKIP_MARKER}\n{lines}")

    return "\n\n".join(out) + "\n"


def round_trips_losslessly(raw: str) -> bool:
    """Whether ``raw`` is exactly the managed template ``render(parse(raw))`` would produce,
    tolerating ONLY the spacing an editor UI adds around it: blank lines directly after a managed
    ``## <section>`` heading, and trailing blank lines at the end. A body passing this holds
    nothing ``render`` would drop, so re-rendering it is lossless in content. Blank lines
    anywhere else (prose, code fences), unknown sections and any other difference still fail.
    """
    return _without_ui_spacing(raw) == _without_ui_spacing(render(parse(raw)))


_MANAGED_HEADING_LINES = frozenset(f"## {name}" for name in (*SECTIONS, _SKIP_MARKER, _DUE_MARKER))


def _without_ui_spacing(body: str) -> str:
    out: list[str] = []
    after_heading = False
    for line in body.rstrip().split("\n"):
        if after_heading and line.strip() == "":
            continue
        after_heading = line.rstrip() in _MANAGED_HEADING_LINES
        out.append(line)
    return "\n".join(out)


# The Ticket fields render()/parse() derive from the body that merge_body compares against
# parse(<stored body>) to tell a body edit from a metadata-only one: `due`/`state`/`labels`/
# `title` ride their own API fields, never the Markdown. The `## Deploy` fields (`deployed*`)
# are body-derived too but not compared, so a deploy-only edit writes no body (task-cli#237).
_BODY_FIELDS: tuple[str, ...] = (
    "what",
    "why",
    "user_impact",
    "cost_of_inaction",
    "acceptance",
    "screenshots",
    "links",
    "skips",
)

# What merge_body can write into a body carrying content outside the template (task-cli#240):
# exactly what closing a ticket needs (`task check`, `task done --skip-<gate>`). Any other field
# lives in prose the template would have to re-render.
_IN_PLACE_FIELDS = frozenset({"acceptance", "skips"})


class BodyMergeError(ValueError):
    """A body edit that cannot be merged into the stored Markdown without losing content."""


def merge_body(stored: str, ticket: Ticket) -> str | None:
    """The body to write so ``stored`` carries ``ticket``'s body fields, or ``None`` when it
    already does (a metadata-only edit must never rewrite the Markdown).

    A body that is exactly the managed template (:func:`round_trips_losslessly`) is re-rendered.
    Any other body — e.g. the 2026-09-10 grooming shape: a provenance preamble plus the retained
    original scope (task-cli#240) — is edited IN PLACE: only the acceptance-criterion lines
    whose checkbox/proof changed and the ``## Skipped gates`` lines are rewritten; every other
    byte is kept. Raises :class:`BodyMergeError` for a change that can't be written that way
    (prose, links or screenshots; criteria added, removed or reworded) or whose result would not
    parse back to ``ticket``.
    """
    original = parse(stored)
    changed = [f for f in _BODY_FIELDS if getattr(ticket, f) != getattr(original, f)]
    if not changed:
        return None
    if round_trips_losslessly(stored):
        return render(ticket)
    unsupported = [f for f in changed if f not in _IN_PLACE_FIELDS]
    if unsupported:
        raise BodyMergeError(f"the edit changes {', '.join(unsupported)}, which only a full re-render can write")
    body = stored
    if "acceptance" in changed:
        body = _edit_acceptance_in_place(body, original.acceptance, ticket.acceptance)
    if "skips" in changed:
        body = _edit_skips_in_place(body, original.skips, ticket.skips)
    reparsed = parse(body)
    drift = [f for f in _BODY_FIELDS if getattr(reparsed, f) != getattr(ticket, f)]
    if drift:
        raise BodyMergeError(f"the in-place edit would not read back as requested ({', '.join(drift)})")
    return body


def _last_section_span(body: str, name: str) -> tuple[int, int] | None:
    """The ``(start, end)`` span of the content under the LAST ``## <name>`` heading — the
    occurrence :func:`parse` reads, since :func:`split_sections` lets a repeated heading
    overwrite an earlier one — or ``None`` when there is no such heading. ``start`` is the end
    of the heading line (before its newline), so the span's first ``\\n``-split element is
    always that line's empty remainder and the next one is the first line under the heading.

    Refuses when that heading sits inside a fenced code block or an HTML comment: the parser
    reads it as a section, but in the rendered Markdown it is quoted content the edit must not
    rewrite."""
    matches = list(_HEADING_RE.finditer(body))
    for i in range(len(matches) - 1, -1, -1):
        if matches[i].group(1).strip() == name:
            if _inside_fence_or_comment(body, matches[i].start()):
                raise BodyMergeError(f"its '## {name}' heading is inside a code block or HTML comment")
            eol = body.find("\n", matches[i].start())
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            return (len(body) if eol == -1 else eol), end
    return None


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _inside_fence_or_comment(body: str, pos: int) -> bool:
    """Whether offset ``pos`` (a line start, or the end of ``body``) lies inside a fenced code
    block or an HTML comment that an earlier line opened."""
    prior = body[:pos].split("\n")
    if prior[-1] == "":
        prior.pop()  # `pos` starts a line: the split's last element is that (empty) line prefix
    fence = ""
    comment = False
    for line in prior:
        if fence:
            close = _FENCE_RE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = "" if not line[close.end() :].strip() else fence
            continue
        if comment or "<!--" in line:
            comment = line.rfind("<!--") > line.rfind("-->") if "-->" in line else True
            continue
        opening = _FENCE_RE.match(line)
        if opening:
            fence = opening.group(1)
    return bool(fence) or comment


def _eol_cr(body: str, pos: int) -> str:
    """``"\\r"`` when the line break at or before ``pos`` is CRLF (a browser textarea saves
    bodies that way), else ``""`` — so lines written next to it use the same line ending."""
    nl = body.rfind("\n", 0, pos + 1)
    return "\r" if nl > 0 and body[nl - 1] == "\r" else ""


def _edit_acceptance_in_place(body: str, before: list[Criterion], after: list[Criterion]) -> str:
    """Rewrite only the checkbox lines of the criteria whose checked state/proof changed; the
    bullet marker/indent and any trailing whitespace of each rewritten line are kept."""
    span = _last_section_span(body, "Acceptance criteria")
    if span is None or len(before) != len(after):
        raise BodyMergeError("the edit adds or removes acceptance criteria")
    start, end = span
    lines = body[start:end].split("\n")
    located = _criteria_blocks(lines)
    if [crit for crit, _ in located] != before:
        raise BodyMergeError("the acceptance criteria could not be located line by line")
    dropped: set[int] = set()
    for (old, block), new in zip(located, after):
        if new == old:
            continue
        if new.text != old.text:
            raise BodyMergeError("the edit rewords an acceptance criterion")
        lines[block[0]] = _rebullet(lines[block[0]], _criterion_item(new))
        dropped.update(block[1:])  # a Linear-style proof continuation; the proof is now inline
    kept = [line for i, line in enumerate(lines) if i not in dropped]
    return body[:start] + "\n".join(kept) + body[end:]


def _rebullet(raw: str, item: str) -> str:
    """``raw`` (a bullet line) with its item text replaced by ``item``, keeping the line's
    indent, bullet marker and trailing whitespace (a CRLF body's ``\\r`` included)."""
    bullet = _BULLET_ITEM_RE.match(raw)
    if bullet is None:  # unreachable: callers pass lines _bullet_blocks/_skip_gate matched
        raise BodyMergeError("an edited line is not a bullet")
    return raw[: bullet.start(1)] + item + raw[len(raw.rstrip()) :]


def _edit_skips_in_place(body: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Rewrite/remove the ``## Skipped gates`` lines of changed/dropped gates (a rewritten line
    keeps its bullet marker and indent) and insert new gates after the last existing one (right
    under the heading when there is none); with no such section, append one at the very end.
    New lines take the line ending of the heading / the body's last line break."""
    span = _last_section_span(body, _SKIP_MARKER)
    if span is None and _inside_fence_or_comment(body, len(body)):
        raise BodyMergeError("the body ends inside a code block or HTML comment")
    cr = _eol_cr(body, len(body) if span is None else span[0])
    added = [_render_skip(gate, after[gate]) + cr for gate in sorted(after) if gate not in before]
    if span is None:
        separator = cr + "\n" if body.endswith("\n") else (cr + "\n") * 2
        return f"{body}{separator}## {_SKIP_MARKER}{cr}\n" + "\n".join(added) + "\n"
    start, end = span
    out: list[str] = []
    rewritten: set[str] = set()
    insert_at = 1  # right under the heading when the section holds no gate line yet
    for line in body[start:end].split("\n"):
        gate = _skip_gate(line)
        if gate is not None and gate in before and before[gate] != after.get(gate):
            if gate in after and gate not in rewritten:
                out.append(_rebullet(line, _skip_item(gate, after[gate])))
                rewritten.add(gate)
                insert_at = len(out)
            continue
        out.append(line)
        if gate is not None:
            insert_at = len(out)
    out[insert_at:insert_at] = added
    return body[:start] + "\n".join(out) + body[end:]


def _skip_gate(line: str) -> str | None:
    """The gate a ``## Skipped gates`` line records (the key :func:`_parse_skips` reads), or
    ``None`` for a line that is not a ``- <gate>: <reason>`` bullet."""
    bullet = _BULLET_ITEM_RE.match(line)
    if bullet is None:
        return None
    item = _SKIP_ITEM_RE.match("- " + bullet.group(1).strip())
    return item.group(1).strip() if item else None


def _skip_item(gate: str, reason: str) -> str:
    return f"{gate}: {reason}"


def _render_skip(gate: str, reason: str) -> str:
    return f"- {_skip_item(gate, reason)}"


def _criterion_item(c: Criterion) -> str:
    """One acceptance criterion's checkbox item, without the bullet. A checked criterion carries
    its visual proof (``— proof: ![proof](ref)``) or, when forced, the recorded reason
    (``— forced: <reason>``)."""
    box = "x" if c.checked else " "
    item = f"[{box}] {c.text}"
    if c.checked:
        if c.proof:
            item += f" — proof: ![proof]({c.proof})"
        elif c.force_reason:
            item += f" — forced: {c.force_reason}"
    return item


def _render_criterion(c: Criterion) -> str:
    return f"- {_criterion_item(c)}"


def _render_shot(shot: Screenshot) -> str:
    caption = f" — {shot.caption}" if shot.caption else ""
    # markdown image when it looks like a URL/path, plain ref otherwise
    return f"- {shot.kind}: ![{shot.kind}]({shot.ref}){caption}"


def _render_links(ticket: Ticket) -> str:
    if not ticket.links:
        return "- (none)"
    return "\n".join(f"- {key}: {val}" for key, val in ticket.links.items())


def _render_deploy(ticket: Ticket) -> str:
    """The single-line ``## Deploy`` section body: ``"Deployed: no"``, or ``"Deployed: yes
    (<iso-timestamp>)"`` with an optional ``— <note>`` suffix (set by ``task change --deployed
    "<note>"``)."""
    if not ticket.deployed:
        return "Deployed: no"
    note = f" — {ticket.deployed_note}" if ticket.deployed_note else ""
    return f"Deployed: yes ({ticket.deployed_at}){note}"


# ── parsing (round-trip) ──────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

# task-cli#181: split_sections (below) treats ANY line matching `^##\s+` as a NEW section
# boundary — correct for the ticket's OWN canonical headings, but round-tripping raw
# user-authored free text through the same body meant a `--what`/`--why`/`--impact`/
# `--if-not-done` value that itself contained a line starting with `#`x1-6 + whitespace (e.g.
# a pasted spec excerpt) got silently truncated at that line on the next parse — everything
# after it was misread as belonging to a brand new section and dropped. Escaping a line-leading
# heading-shaped run before it's embedded (Markdown's own backslash-escape convention) makes it
# inert prose to split_sections; unescaping on the way back out restores the original text.
_EMBEDDED_HEADING_RE = re.compile(r"^(#{1,6}\s)", re.MULTILINE)
_ESCAPED_HEADING_RE = re.compile(r"^\\(#{1,6}\s)", re.MULTILINE)


def _escape_embedded_headings(text: str) -> str:
    """Escape a line-leading `#`x1-6 run in free-text ``text`` so it survives a round-trip
    through :func:`split_sections` as prose, not a new section boundary."""
    return _EMBEDDED_HEADING_RE.sub(r"\\\1", text)


def _unescape_embedded_headings(text: str) -> str:
    """Undo :func:`_escape_embedded_headings` after a field's content is pulled back out of
    a parsed body."""
    return _ESCAPED_HEADING_RE.sub(r"\1", text)


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
    # The Deploy section is REQUIRED going forward (in SECTIONS), but a body written before this
    # field existed has no "## Deploy" heading at all — fall back to the base ticket's deploy
    # fields (mirrors the Due fallback above) so an old ticket round-trips as not-deployed
    # instead of crashing.
    deploy_text = sec.get(_DEPLOY_MARKER, "").strip()
    if deploy_text:
        deployed, deployed_at, deployed_note = _parse_deploy(deploy_text)
    else:
        deployed, deployed_at, deployed_note = base.deployed, base.deployed_at, base.deployed_note

    return replace(
        base,
        what=_unescape_embedded_headings(sec.get("What", "").strip()),
        why=_unescape_embedded_headings(sec.get("Why (motivation)", "").strip()),
        user_impact=_unescape_embedded_headings(sec.get("User impact", "").strip()),
        cost_of_inaction=_unescape_embedded_headings(sec.get("Cost of inaction", "").strip()),
        acceptance=acceptance,
        screenshots=screenshots,
        links=links or base.links,
        skips=skips or base.skips,
        due=due,
        deployed=deployed,
        deployed_at=deployed_at,
        deployed_note=deployed_note,
        raw_body=body,
    )


_DEPLOY_LINE_RE = re.compile(
    r"^Deployed:\s*(yes|no)(?:\s*\(([^)]*)\))?(?:\s*—\s*(.*))?\s*$", re.IGNORECASE
)


def _parse_deploy(text: str) -> tuple[bool, str, str]:
    """Parse the ``## Deploy`` section's single line back into ``(deployed, deployed_at,
    deployed_note)``. An unrecognized/malformed line (a hand-edited body) is tolerated the same
    way a malformed ``due`` date is — treated as not-deployed rather than raising."""
    m = _DEPLOY_LINE_RE.match(text.strip())
    if not m:
        return False, "", ""
    deployed = m.group(1).lower() == "yes"
    at = (m.group(2) or "").strip()
    note = (m.group(3) or "").strip()
    return deployed, at, note


_CRITERION_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")
# `\s*` (zero-or-more), not `\s+`, after the marker (review finding, task-cli#F4): every
# per-section parser this feeds already tolerated a zero-space marker (`-[x]`, `-PR: ...`)
# before this function existed — requiring at least one space here silently dropped those
# lines as unrecognized prose instead. Safe to relax: each downstream regex still gates on
# its own shape (a checkbox `[`, a `key:` colon, ...), so this alone doesn't un-anchor
# bullet detection.
_BULLET_ITEM_RE = re.compile(r"^\s*[-*]\s*(.*)$")

_OPEN_KIND_BULLET_RE = re.compile(r"^\w+:$")
# Requires a CHECKED checkbox prefix (review finding, GitHub PR #107 Codex pass): Linear's
# confirmed proof-continuation sample is only ever produced for a checked criterion (a proof
# is only ever set together with checking it off, via `cmd_check`) — an unchecked criterion
# whose own hand-typed text happens to literally end in "— proof:" must NOT be treated as
# awaiting a continuation, or an unrelated indented image immediately below it would be
# silently absorbed as a phantom proof and then dropped entirely on render (unchecked
# criteria never render a proof line).
_OPEN_PROOF_BULLET_RE = re.compile(r"^\[[xX]\].*—\s*proof:\s*$")


def _open_bullet_kind(content: str) -> str | None:
    """Which of Linear's two CONFIRMED "awaiting a continuation" shapes ``content`` (a
    bullet's own text, before any continuation-joining) matches — ``"kind"`` for a bare
    Screenshot kind marker (``implementation:``, nothing else), ``"proof"`` for a CHECKED
    Criterion's dangling ``— proof:`` marker, or ``None`` for an already-complete bullet.

    Deliberately narrower than "ends with any colon" (review finding, task-cli#107 review
    thread) — a criterion or caption whose own text happens to end in a colon for an
    unrelated reason (``run the checklist:``) must NOT be treated as awaiting a continuation,
    or an unrelated indented image/caption line immediately below it would still glue on.

    The two shapes are distinguished (not just a single "is open" bool) because their
    confirmed continuation grammar differs: a Screenshot's image may be followed by one
    caption line, but a Criterion's proof image never has one — the two confirmed samples
    never show a criterion growing a second continuation line after its proof image.
    """
    if _OPEN_KIND_BULLET_RE.search(content):
        return "kind"
    if _OPEN_PROOF_BULLET_RE.search(content):
        return "proof"
    return None


def _join_bullet_blocks(text: str) -> list[str]:
    """Group a bulleted section's raw lines into one reconstructed ``"- <content>"`` line per
    bullet item, so every line-anchored parser regex below keeps working unchanged. The
    grouping itself (which raw lines form one item) is :func:`_bullet_blocks`."""
    lines = text.splitlines()
    return [_joined_block(lines, block) for block in _bullet_blocks(lines)]


def _joined_block(lines: list[str], block: list[int]) -> str:
    """One bullet item's reconstructed ``"- <content>"`` line: the bullet's own content plus
    each joined continuation line, space-separated."""
    head = _BULLET_ITEM_RE.match(lines[block[0]])
    assert head is not None  # _bullet_blocks only ever opens a block on a bullet line
    parts = [head.group(1).strip(), *(lines[i].strip() for i in block[1:])]
    return "- " + " ".join(parts)


def _bullet_blocks(lines: list[str]) -> list[list[int]]:
    """Group a bulleted section's raw ``lines`` into bullet items — each item the indices of
    its bullet line plus any continuation lines joined onto it. Indices (not text) so the
    in-place body editor (:func:`merge_body`) can rewrite exactly the lines an item occupies.

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
    between — tracked via ``state`` below. Any column-0, non-blank, non-bullet line
    (hand-added prose under a list, or the SAME shape appearing later in the section) breaks
    that chain permanently for the current bullet, even across further blank lines. And a
    bullet only ever accepts a continuation in the first place if its OWN content — before
    any joining — matches one of Linear's two CONFIRMED "awaiting a continuation" shapes (see
    :func:`_open_bullet_kind`): an ALREADY-COMPLETE bullet (a checked criterion with no
    dangling proof marker, a screenshot bullet whose image is already inline) never does,
    even when an unrelated indented image/caption line happens to sit immediately below it
    (GitHub PR
    review finding on task-cli#107: a second, unrelated image below an already-inline
    screenshot bullet was getting absorbed and double-wrapping the first image's markdown on
    the next render). Anything that doesn't join is left alone, never joined. All guards are
    load-bearing (review findings, task-cli#F1, and a GitHub PR review on task-cli#107):
    joining unconditionally corrupted Links (embedded whitespace fails ``is_http_url``),
    double-wrapped Screenshot image markdown, and polluted Criterion text; joining an
    un-indented ``![``/``—`` line reopened the same corruption for a column-0 hand-pasted
    GitHub screenshot; without the "immediately follows" guard, an indented ``![``/``—``
    line appearing ANYWHERE later in the section still silently glued onto a distant,
    unrelated bullet; without the "bullet must be open" guard (:func:`_open_bullet_kind`), an
    indented image/caption line immediately below an already-COMPLETE bullet still silently
    glued onto it; and without bounding the chain to AT MOST one image then one caption, a
    SECOND unrelated image immediately below a genuinely open bullet still glued on and
    double-wrapped the first image on the next render (task-cli#107 review thread).

    Deliberate, narrower trade-off (same F1-v2 philosophy — only ever join a CONFIRMED Linear
    reformatting shape, nothing reconstructed by pattern-matching): once a bullet is COMPLETE
    (already has its inline image, or was never "open" to begin with), a hand-added indented
    ``— caption`` line below it is dropped, not joined, even though such a line was
    reconstructive under the pre-#107-fix code. This never happens in the wild — a human
    editing a ticket types a caption inline on the bullet's own line, never manufactures
    Linear's own continuation-line artifact by hand — so preferring "never absorb anything
    ambiguous" over guessing at a hand-edit's intent is the safer default.
    """
    blocks: list[list[int]] = []
    # "open_kind"/"open_proof" (awaiting an image) -> "after_kind_image" (awaiting a caption,
    # Screenshots only — the confirmed Criterion proof sample never grows a second
    # continuation line, so a joined proof image goes straight to "closed") -> "closed"
    state = "closed"
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue  # a blank line never breaks a continuation chain (Linear's own shape)
        m = _BULLET_ITEM_RE.match(raw_line)
        if m:
            blocks.append([index])
            kind = _open_bullet_kind(m.group(1).strip())
            state = f"open_{kind}" if kind else "closed"
            continue
        is_continuation_shaped = raw_line.startswith((" ", "\t")) and (
            line.startswith("![") or line.startswith("—")
        )
        if state in ("open_kind", "open_proof") and is_continuation_shaped and line.startswith("!["):
            blocks[-1].append(index)
            state = "after_kind_image" if state == "open_kind" else "closed"
            continue
        if state == "after_kind_image" and is_continuation_shaped and line.startswith("—"):
            blocks[-1].append(index)
            state = "closed"  # at most one caption/proof line after the image
            continue
        # a column-0 line, a non-image/caption-shaped indented line, a second image, or a
        # continuation-shaped line that no longer immediately follows its OPEN bullet —
        # breaks the chain
        state = "closed"
    return blocks


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

    Proof/forced suffixes are only parsed off a CHECKED criterion: ``_render_criterion`` emits
    one only when ``checked`` is true, so on an unchecked line the same-looking text (e.g. a
    PLANNED proof method named up front per strict-ticket-discipline, "— proof: an e2e spec")
    is part of the criterion itself and must be kept verbatim, or render() silently drops it
    on the very next round-trip.
    """
    return [crit for crit, _ in _criteria_blocks(text.splitlines())]


def _criteria_blocks(lines: list[str]) -> list[tuple[Criterion, list[int]]]:
    """Each acceptance criterion in the section's raw ``lines`` paired with the line indices
    its bullet item occupies (see :func:`_bullet_blocks`) — the one parse both
    :func:`_parse_criteria` and the in-place editor (:func:`merge_body`) read."""
    items: list[tuple[Criterion, list[int]]] = []
    for block in _bullet_blocks(lines):
        m = _CRITERION_RE.match(_joined_block(lines, block))
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        rest = m.group(2).strip()
        if checked:
            crit_text, proof, force_reason = _split_criterion_rest(rest)
        else:
            crit_text, proof, force_reason = rest, "", ""
        if crit_text and crit_text.lower() != "(none specified)":
            items.append((Criterion(text=crit_text, checked=checked, proof=proof, force_reason=force_reason), block))
    return items


_PROOF_IMAGE_START_RE = re.compile(r"\s+—\s+proof:\s*!\[[^\]]*\]\(")
_MARKER_DELIM_RE = re.compile(r"\s+—\s+(proof|forced):\s*(?=\S)")


def _matching_close_paren(s: str, start: int) -> int:
    """Index of the ``)`` balancing the ``(`` immediately before ``start``, or -1.

    A proof path/URL can itself contain parens (e.g. ``img(1).png``), so the boundary of an
    image proof's value is wherever its OWN parens balance out — not just any later ``)`` in
    the line, which could belong to an unrelated parenthetical in a trailing ``— forced: …``
    reason (``"blocked (waiting on infra)"``) and would otherwise look like a closer.
    """
    depth = 1
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _image_proof_value(rest: str, marker: re.Match[str], last_paren: int) -> str | None:
    """The proof value for an image-marker occurrence starting at ``marker``, or ``None``.

    Tried balanced first (bounds a value that itself contains marker-shaped text, e.g. an
    embedded planned-proof note); falls back to the line's very last ``)`` for a real but
    genuinely unbalanced-parens ref, accepted only when nothing marker-shaped sits in that
    span — so it can't silently swallow a real, later ``— forced:``/``— proof:`` marker.
    """
    close = _matching_close_paren(rest, marker.end())
    if close != -1 and not rest[close + 1 :].strip():
        return rest[marker.end() : close].strip()
    if last_paren > marker.end() and not rest[last_paren + 1 :].strip():
        span = rest[marker.end() : last_paren]
        if not _MARKER_DELIM_RE.search(span):
            return span.strip()
    return None


def _split_criterion_rest(rest: str) -> tuple[str, str, str]:
    """Split a checked criterion's text from its trailing ``— proof: …`` / ``— forced: …`` marker.

    Returns ``(text, proof_ref, force_reason)``. The rightmost marker in the line wins, not
    the first one found — a criterion's own text can independently contain a marker-shaped
    substring (a planned proof method named while still unchecked, per ``_parse_criteria``'s
    docstring), and ``_render_criterion`` always appends the real marker last. An image proof
    is tried first (see ``_image_proof_value``); otherwise the marker is a bare line whose
    value has no boundary of its own and always extends to the end of ``rest``.

    Two shapes are not, and cannot be, disambiguated from an embedded marker by any regex:
    a proof/force VALUE that itself contains a marker-shaped substring, and a criterion
    hand-checked (``[x]``) without ever going through ``task check`` at all, whose own text
    happens to end in marker-shaped prose. Both produce a string identical to a real embedded
    marker for the opposite intent; this only stabilizes (round-trips on the SECOND pass),
    it does not preserve the original.
    """
    last_paren = rest.rfind(")")
    for img_start in reversed(list(_PROOF_IMAGE_START_RE.finditer(rest))):
        value = _image_proof_value(rest, img_start, last_paren)
        if value is not None:
            return rest[: img_start.start()].strip(), value, ""
    matches = list(_MARKER_DELIM_RE.finditer(rest))
    if not matches:
        return rest, "", ""
    marker = matches[-1]
    text = rest[: marker.start()].strip()
    value = rest[marker.end() :].strip()
    return (text, "", value) if marker.group(1) == "forced" else (text, value, "")


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


_SKIP_ITEM_RE = re.compile(r"^\s*-\s*([^:]+):\s*(.+?)\s*$")


def _parse_skips(text: str) -> dict[str, str]:
    skips: dict[str, str] = {}
    for line in _bullet_lines(text):
        m = _SKIP_ITEM_RE.match(line)
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
