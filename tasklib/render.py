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
        lines = "\n".join(f"- {gate}: {reason}" for gate, reason in sorted(ticket.skips.items()))
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
    blocks: list[list[str]] = []
    # "open_kind"/"open_proof" (awaiting an image) -> "after_kind_image" (awaiting a caption,
    # Screenshots only — the confirmed Criterion proof sample never grows a second
    # continuation line, so a joined proof image goes straight to "closed") -> "closed"
    state = "closed"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue  # a blank line never breaks a continuation chain (Linear's own shape)
        m = _BULLET_ITEM_RE.match(raw_line)
        if m:
            content = m.group(1).strip()
            blocks.append([content])
            kind = _open_bullet_kind(content)
            state = f"open_{kind}" if kind else "closed"
            continue
        is_continuation_shaped = raw_line.startswith((" ", "\t")) and (
            line.startswith("![") or line.startswith("—")
        )
        if state in ("open_kind", "open_proof") and is_continuation_shaped and line.startswith("!["):
            blocks[-1].append(line)
            state = "after_kind_image" if state == "open_kind" else "closed"
            continue
        if state == "after_kind_image" and is_continuation_shaped and line.startswith("—"):
            blocks[-1].append(line)
            state = "closed"  # at most one caption/proof line after the image
            continue
        # a column-0 line, a non-image/caption-shaped indented line, a second image, or a
        # continuation-shaped line that no longer immediately follows its OPEN bullet —
        # breaks the chain
        state = "closed"
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

    Proof/forced suffixes are only parsed off a CHECKED criterion: ``_render_criterion`` emits
    one only when ``checked`` is true, so on an unchecked line the same-looking text (e.g. a
    PLANNED proof method named up front per strict-ticket-discipline, "— proof: an e2e spec")
    is part of the criterion itself and must be kept verbatim, or render() silently drops it
    on the very next round-trip.
    """
    items: list[Criterion] = []
    for line in _join_bullet_blocks(text):
        m = _CRITERION_RE.match(line)
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        rest = m.group(2).strip()
        if checked:
            crit_text, proof, force_reason = _split_criterion_rest(rest)
        else:
            crit_text, proof, force_reason = rest, "", ""
        if crit_text and crit_text.lower() != "(none specified)":
            items.append(Criterion(text=crit_text, checked=checked, proof=proof, force_reason=force_reason))
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
