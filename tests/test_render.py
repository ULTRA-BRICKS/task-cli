"""render.py — template render/parse round-trip + the formatting gate."""

from __future__ import annotations

from tasklib.model import Criterion, Screenshot, Ticket
from tasklib.render import SECTIONS, parse, render, split_sections, validate_format


def _full_ticket() -> Ticket:
    return Ticket(
        title="Add dark mode toggle",
        what="A toggle in the header that switches the theme.",
        why="Users asked for it repeatedly.",
        user_impact="Everyone on the web app.",
        cost_of_inaction="Churn from users who hate the bright theme.",
        acceptance=["toggle persists across reloads", "respects prefers-color-scheme"],
        screenshots=[Screenshot(ref="before.png", kind="creation", caption="current header")],
        labels=["ui"],
        links={"PR": "https://github.com/x/y/pull/1", "Issue": "https://linear.app/x/issue/HYP-1"},
    )


def test_render_has_all_sections_in_order():
    body = render(_full_ticket())
    positions = [body.index(f"## {s}") for s in SECTIONS]
    assert positions == sorted(positions)


def test_render_acceptance_as_checkboxes():
    body = render(_full_ticket())
    assert "- [ ] toggle persists across reloads" in body
    assert "- [ ] respects prefers-color-scheme" in body


def test_render_checked_criterion_carries_proof():
    t = _full_ticket()
    t.acceptance = [
        Criterion("toggle persists across reloads", checked=True, proof="after.png"),
        Criterion("respects prefers-color-scheme", checked=False),
    ]
    body = render(t)
    assert "- [x] toggle persists across reloads — proof: ![proof](after.png)" in body
    assert "- [ ] respects prefers-color-scheme" in body


def test_round_trip_preserves_checked_state_and_proof():
    t = _full_ticket()
    t.acceptance = [
        Criterion("a", checked=True, proof="p.png"),
        Criterion("b", checked=True, force_reason="proof impossible for a CLI flag"),
        Criterion("c", checked=False),
    ]
    parsed = parse(render(t))
    assert [(c.text, c.checked, c.proof, c.force_reason) for c in parsed.acceptance] == [
        ("a", True, "p.png", ""),
        ("b", True, "", "proof impossible for a CLI flag"),
        ("c", False, "", ""),
    ]


def test_round_trip_preserves_proof_path_containing_parens():
    # a proof path/URL with literal parens must survive render→parse intact (greedy capture),
    # not truncate at the first ')' and re-wrap into ![proof](![proof](...)) on the next render.
    t = _full_ticket()
    t.acceptance = [Criterion("a", checked=True, proof="https://s3/img(1).png")]
    once = render(t)
    parsed = parse(once)
    assert parsed.acceptance[0].proof == "https://s3/img(1).png"
    # idempotent: a second render→parse round produces the same proof (no progressive wrapping)
    assert parse(render(parsed)).acceptance[0].proof == "https://s3/img(1).png"
    assert "![proof](![proof]" not in render(parsed)


def test_round_trip_preserves_fields():
    original = _full_ticket()
    body = render(original)
    parsed = parse(body)
    assert parsed.what == original.what
    assert parsed.why == original.why
    assert parsed.user_impact == original.user_impact
    assert parsed.cost_of_inaction == original.cost_of_inaction
    assert parsed.acceptance == original.acceptance
    assert parsed.links["PR"] == original.links["PR"]
    assert [s.ref for s in parsed.screenshots] == ["before.png"]


def test_parse_keeps_non_body_fields_from_base():
    base = Ticket(id="#42", title="kept", labels=["ui"])
    parsed = parse(render(_full_ticket()), base)
    assert parsed.id == "#42"
    assert parsed.title == "kept"
    assert parsed.labels == ["ui"]


def test_validate_format_passes_for_rendered_body():
    assert validate_format(render(_full_ticket())) == []


def test_validate_format_flags_missing_section():
    body = render(_full_ticket()).replace("## Cost of inaction", "## Something else")
    problems = validate_format(body)
    assert any("Cost of inaction" in p for p in problems)


def test_validate_format_flags_out_of_order():
    body = (
        "## Why (motivation)\nx\n\n## What\ny\n\n## User impact\nz\n\n## Cost of inaction\nc\n\n"
        "## Acceptance criteria\n- [ ] a\n\n## Screenshots\n(none)\n\n## Links\n- (none)\n"
    )
    problems = validate_format(body)
    assert any("out of order" in p for p in problems)


def test_skipped_gates_section_survives_round_trip():
    t = _full_ticket()
    t.skips = {"screenshots": "no UI proof yet"}
    body = render(t)
    assert "## Skipped gates" in body
    assert parse(body).skips["screenshots"] == "no UI proof yet"


def test_split_sections_handles_extra_whitespace():
    body = "##   What  \n  hello  \n\n## Links\n- (none)\n"
    sec = split_sections(body)
    assert sec["What"] == "hello"


# ── due date (the daemon-watched optional section) ──────────────────────────────────


def test_due_section_renders_and_round_trips():
    t = _full_ticket()
    t.due = "2026-07-01"
    body = render(t)
    assert "## Due\n2026-07-01" in body
    assert parse(body).due == "2026-07-01"


def test_due_section_is_omitted_when_unset():
    body = render(_full_ticket())  # no due
    assert "## Due" not in body
    assert parse(body).due == ""


def test_due_does_not_break_the_formatting_gate():
    # the Due section is OPTIONAL (not in SECTIONS) — a body with it (or without) stays valid
    t = _full_ticket()
    t.due = "2026-07-01"
    assert validate_format(render(t)) == []
    assert validate_format(render(_full_ticket())) == []


def test_parse_falls_back_to_base_due_when_body_omits_it():
    # a backend that supplied the date natively (base.due set) isn't clobbered by a due-less body
    base = Ticket(id="#1", due="2026-08-15")
    parsed = parse(render(_full_ticket()), base)  # rendered body has no Due section
    assert parsed.due == "2026-08-15"


# ── Linear's server-side reformatting round-trip (HYP-1355/1356/1398) ───────────────
#
# Confirmed live (scratch ticket HYP-1437, glide-vc/HYP): every `issueUpdate` mutation echoes
# back a description Linear's OWN editor has reformatted, not a byte-identical copy of what was
# sent. Three concrete, captured mutations: the bullet marker becomes "*" instead of "-"; a
# bulleted item whose content includes an inline image gets that image (and any trailing
# "— caption"/"— proof: ..." text after it) pushed onto separate, blank-line-separated
# CONTINUATION lines instead of staying on the bullet's own line; and a bare URL value gets
# auto-linkified into "[url](<url>)". A parser that only reads one line per bullet item — which
# is what every parser here did before this fix — sees an empty/truncated item on the very next
# parse, which is the confirmed mechanism behind real Screenshots/Links/proof content vanishing
# after nothing more than a single Linear round-trip.


def _body_with_section(name: str, content: str) -> str:
    """A minimal-but-valid full body with every SECTIONS heading present (required by
    split_sections/parse to find anything), only `name`'s content swapped for `content` —
    everything else stays a plain placeholder."""
    parts = []
    for s in SECTIONS:
        parts.append(f"## {s}\n{content if s == name else 'x'}")
    return "\n\n".join(parts) + "\n"


def test_parse_screenshots_survives_linear_reformatting():
    # captured verbatim from a real `issueUpdate` response echo
    reformatted = (
        "\n* implementation:\n\n"
        "  ![implementation](https://uploads.linear.app/b16c196e/2451bf90/09c3f199)\n\n"
        "  — test image markdown\n"
    )
    body = _body_with_section("Screenshots", reformatted)
    shots = parse(body).screenshots
    assert len(shots) == 1
    assert shots[0].kind == "implementation"
    assert shots[0].ref == "https://uploads.linear.app/b16c196e/2451bf90/09c3f199"
    assert shots[0].caption == "test image markdown"


def test_parse_criterion_proof_survives_linear_reformatting():
    # captured verbatim: the checkbox + text stay on one line, but Linear pushes the image
    # markdown for a checked criterion's proof onto its own continuation line.
    reformatted = (
        "\n- [X] a criterion with a proof — proof:\n\n"
        "  ![proof](https://uploads.linear.app/b16c196e/2451bf90/09c3f199)\n"
    )
    body = _body_with_section("Acceptance criteria", reformatted)
    crits = parse(body).acceptance
    assert len(crits) == 1
    assert crits[0].checked is True
    assert crits[0].text == "a criterion with a proof"
    assert crits[0].proof == "https://uploads.linear.app/b16c196e/2451bf90/09c3f199"


def test_parse_links_survives_linear_reformatting():
    # captured verbatim: bullet becomes "*" and the bare URL value gets auto-linkified.
    reformatted = (
        "\n* PR: [https://github.com/alex-mextner/task-cli/pull/1]"
        "(<https://github.com/alex-mextner/task-cli/pull/1>)"
    )
    body = _body_with_section("Links", reformatted)
    links = parse(body).links
    assert links == {"PR": "https://github.com/alex-mextner/task-cli/pull/1"}


def test_parse_criteria_survives_linear_star_marker_reformatting():
    # review finding (task-cli#F3): Linear rewrites the bullet marker "-" -> "*" on every
    # save (same mechanism as the Links/Screenshots tests above) — a regression to
    # `_BULLET_ITEM_RE`'s "*" support would silently break checked-state parsing of any
    # Linear-round-tripped criterion, not just the image/link cases already covered.
    body = _body_with_section("Acceptance criteria", "\n* [X] a criterion, star-marker form\n")
    crits = parse(body).acceptance
    assert len(crits) == 1
    assert crits[0].checked is True
    assert crits[0].text == "a criterion, star-marker form"


def test_parse_criteria_and_links_tolerate_a_zero_space_bullet_marker():
    # review finding (task-cli#F4): every per-section parser tolerated a marker with no
    # space before its content ("-[x] ...", "-PR: ...") prior to _join_bullet_blocks/
    # _BULLET_ITEM_RE existing — a regression to `\s+` silently drops those lines as
    # unrecognized prose (and, worse, shifts every later criterion's 1-based index).
    body = _body_with_section("Acceptance criteria", "\n-[x] deploy verified\n")
    crits = parse(body).acceptance
    assert len(crits) == 1
    assert crits[0].checked is True
    assert crits[0].text == "deploy verified"

    body = _body_with_section("Links", "\n-PR: https://github.com/o/r/pull/1\n")
    assert parse(body).links == {"PR": "https://github.com/o/r/pull/1"}


def test_parse_screenshots_and_links_still_work_on_never_reformatted_bodies():
    # a fresh render()→parse() round trip (no Linear in the loop, e.g. GitHub backend, or a
    # brand-new Linear ticket before its first save) must keep working unchanged.
    t = _full_ticket()
    t.acceptance = [Criterion("a", checked=True, proof="after.png")]
    body = render(t)
    parsed = parse(body)
    assert parsed.screenshots == t.screenshots
    assert parsed.links == t.links
    assert parsed.acceptance[0].proof == "after.png"


def test_join_bullet_blocks_does_not_absorb_hand_added_prose():
    # review finding (task-cli#F1): a human/bot note paragraph added under a list in the
    # tracker UI is a normal event, not a Linear reformatting artifact — joining ANY
    # non-blank continuation line onto the previous bullet corrupted Links (whitespace
    # fails is_http_url), Screenshots (double-wraps the image markdown), and Criterion
    # text. Only a Linear-shaped continuation (an image line, or an em-dash caption/proof
    # line) may be joined.
    body = _body_with_section(
        "Links", "\n- PR: https://github.com/o/r/pull/1\n\nsee thread for context\n"
    )
    links = parse(body).links
    assert links == {"PR": "https://github.com/o/r/pull/1"}


def test_unwrap_link_value_preserves_user_authored_labels():
    # review finding (task-cli#F2): unwrapping must only fire on Linear's own auto-linkify
    # echo (label == target), never on a genuinely user-authored labeled link.
    body = _body_with_section("Links", "\n- design: [design review](https://figma.com/x)\n")
    links = parse(body).links
    assert links == {"design": "[design review](https://figma.com/x)"}


def test_join_bullet_blocks_does_not_absorb_column_zero_pasted_image():
    # review finding: a hand-pasted GitHub screenshot commonly lands at column 0
    # (`![Screenshot...](...)`, no leading whitespace) directly under the Links section
    # (the last required section). Every confirmed Linear reformatting sample is indented,
    # so requiring indentation distinguishes the two without reopening task-cli#F1.
    body = _body_with_section(
        "Links",
        "\n- PR: https://github.com/o/r/pull/1\n\n"
        "![Screenshot from 2026-08-30](https://user-images.githubusercontent.com/x.png)\n",
    )
    links = parse(body).links
    assert links == {"PR": "https://github.com/o/r/pull/1"}


def test_join_bullet_blocks_does_not_glue_distant_indented_image_after_prose():
    # review finding: an indented image/caption line joins its bullet only when it
    # IMMEDIATELY follows (across blank lines only) — a column-0 prose line in between must
    # permanently break the chain for that bullet, even if a later line happens to match the
    # indented image/caption shape. Without this, an image landing anywhere later in a Links
    # section (e.g. GitHub's list-continuation paste, which keeps the list indent) glues
    # onto a bullet paragraphs away.
    body = _body_with_section(
        "Links",
        "\n- PR: https://github.com/o/r/pull/1\n\n"
        "see thread for context\n\n"
        "  ![Screenshot from 2026-08-30](https://user-images.githubusercontent.com/x.png)\n",
    )
    links = parse(body).links
    assert links == {"PR": "https://github.com/o/r/pull/1"}


def test_links_do_not_absorb_an_indented_note_immediately_under_the_bullet():
    # review finding (task-cli#F1-v2): Links/Skips never get a continuation line from Linear
    # (only inline auto-linkify) — routing them through the image/caption-continuation
    # joiner let a plausible human edit (an indented note added under a Links bullet in the
    # tracker UI) glue onto and corrupt the link, since that shape is exactly what the
    # Screenshots/Criteria join guards are designed to allow. Links/Skips must use the
    # non-joining bullet splitter instead.
    body = _body_with_section(
        "Links",
        "\n* PR: [https://github.com/o/r/pull/1](<https://github.com/o/r/pull/1>)\n"
        "  — see thread for context\n",
    )
    links = parse(body).links
    assert links == {"PR": "https://github.com/o/r/pull/1"}


def test_unwrap_link_value_survives_url_containing_parens():
    # review finding (task-cli#F2 follow-on, Codex): a URL that itself contains a
    # parenthesis (e.g. a Wikipedia article title) must still round-trip — the old
    # first-`)`-stopping regex left the whole markdown wrapper in place instead.
    url = "https://en.wikipedia.org/wiki/Function_(mathematics)"
    body = _body_with_section("Links", f"\n* Docs: [{url}](<{url}>)\n")
    links = parse(body).links
    assert links == {"Docs": url}
