"""Red-first safety coverage for ``TasksBackend.update`` (UB-23 review, two blocking findings):

1. Version safety — ``update`` must send the ``backend_version`` from the caller's ORIGINAL
   read, never a value re-derived from a fresh GET, and must reject a missing/invalid version
   before any network call.
2. Body safety — a metadata-only edit (status/labels/title/due, not the body) must never send
   ``description``, and a genuine body edit must either merge losslessly or refuse outright
   rather than silently clobber custom Markdown this backend's parser doesn't understand.

Plus three focused regressions from the follow-up integration review (see the "── regressions
(integration review) ──" section below): ``skips`` missing from the body-change comparison,
native ``due_date`` not authoritative over a stale "## Due" line, and label creation able to run
before a lossy body edit is refused.

Hermetic: ``request_bytes`` is monkeypatched, no real network/credentials.
"""
import json
from dataclasses import replace as dc_replace
from urllib.parse import urlsplit

import pytest

from tasklib.backends import BackendError
from tasklib.backends.tasks import TasksBackend
from tasklib.model import State, Ticket
from tasklib.render import parse, render

from .conftest import GROOMING_2026_09_10_BODY

ORIGIN = "https://tasks.example.test"
FAKE_TOKEN = "FAKE-TEST-TOKEN-NOT-REAL"  # placeholder credential for the hermetic mock below
TEAM = "HYP"
ROW_ID = "11111111-1111-4111-8111-111111111111"

CATALOG = {
    "teams": [{"id": "team-1", "key": TEAM}],
    "statuses": [
        {"id": "todo", "team_id": "team-1", "category": "unstarted", "name": "Todo"},
        {"id": "doing", "team_id": "team-1", "category": "started", "name": "In progress"},
        {"id": "done", "team_id": "team-1", "category": "completed", "name": "Done"},
    ],
    "labels": [],
}


@pytest.fixture
def api(monkeypatch):
    """Records every call; answers /catalog, GET /issues/<key>, and PATCH /issues/<key>.

    The GET-issue branch exists so a test can prove ``update`` never hits it (it would return a
    server row carrying a NEWER version than the caller's original read, i.e. the exact
    "someone else edited since I read it" race the version check exists to survive without a
    re-read).
    """
    calls: list[tuple[str, str, dict | None]] = []

    def request(url, *, method="GET", headers=None, data=None, **options):
        payload = json.loads(data) if data else None
        calls.append((method, url, payload))
        path = urlsplit(url).path
        if path.endswith("/catalog"):
            result = CATALOG
        elif method == "GET" and path.startswith("/api/v1/issues/"):
            # A newer row than any ticket.backend_version used below — update() must never see
            # this by re-reading; if it did and used THIS version, the tests below would fail.
            result = {
                "id": ROW_ID, "key": "HYP-1", "version": 99, "title": "Server-side newer edit",
                "description": "Server-side newer edit body.", "status_category": "unstarted",
                "status_name": "Todo", "labels": [],
            }
        elif method == "PATCH":
            result = {
                "id": ROW_ID, "key": "HYP-1",
                "version": (payload or {}).get("version", 0) + 1,
                "title": (payload or {}).get("title", ""),
                "description": (payload or {}).get("description", "unchanged description"),
                "status_category": "unstarted", "status_name": "Todo", "labels": [],
            }
        else:
            raise AssertionError(f"unexpected {method} {url}")
        return json.dumps(result).encode()

    monkeypatch.setattr("tasklib.backends.tasks.request_bytes", request)
    return calls


def backend() -> TasksBackend:
    return TasksBackend(url=ORIGIN, token=FAKE_TOKEN, team_key=TEAM)


def issue_get_calls(calls):
    return [c for c in calls if c[0] == "GET" and "/issues/" in urlsplit(c[1]).path]


def patch_calls(calls):
    return [c for c in calls if c[0] == "PATCH"]


def ticket_from_original_read(overrides: dict | None = None) -> Ticket:
    """A ticket shaped like what ``TasksBackend.get`` would have returned: canonical body,
    ``raw_body`` set to that SAME rendered body (nothing edited yet), ``backend_version=7``
    (the version that original read saw). ``overrides`` model the caller's local edit(s).
    """
    original = Ticket(
        title="Original title", what="Original what", why="Original why",
        user_impact="impact", cost_of_inaction="cost", acceptance=["done thing"],
        labels=[], state=State.TODO, id="HYP-1", backend_version=7,
    )
    original = dc_replace(original, raw_body=render(original))
    return dc_replace(original, **overrides) if overrides else original


def ticket_with_custom_markdown(overrides: dict | None = None) -> Ticket:
    """A ticket whose ``raw_body`` is hand-written Markdown outside the managed §5 template —
    ``parse()`` recognizes none of it, so its body fields all come back at their defaults. This
    is the "unmanaged content" shape the body-safety refusal exists for.
    """
    custom_body = "# Custom heading\nHand-written notes outside the managed template.\n"
    parsed = parse(custom_body)  # body fields all default; raw_body == custom_body
    base = dc_replace(parsed, id="HYP-1", backend_version=7, title="Old title")
    return dc_replace(base, **overrides) if overrides else base


# ── version safety ──────────────────────────────────────────────────────────────


def test_update_sends_original_version_and_never_re_reads(api):
    ticket = ticket_from_original_read({"state": State.DONE})  # metadata-only edit
    backend().update(ticket)
    patches = patch_calls(api)
    assert len(patches) == 1
    assert patches[0][2]["version"] == 7  # the ORIGINAL read's version, not the server's 99
    assert issue_get_calls(api) == []  # never re-read the issue row before writing


def test_missing_backend_version_raises(api):
    ticket = ticket_from_original_read({"backend_version": 0, "state": State.DONE})
    with pytest.raises(BackendError, match="backend_version"):
        backend().update(ticket)
    assert api == []  # rejected before any network call


@pytest.mark.parametrize("bad_version", ["7", 7.5, -1, True, None])
def test_invalid_backend_version_raises(api, bad_version):
    ticket = ticket_from_original_read({"backend_version": bad_version, "state": State.DONE})
    with pytest.raises(BackendError, match="backend_version"):
        backend().update(ticket)
    assert api == []  # rejected before any network call


# ── body safety ──────────────────────────────────────────────────────────────────


def test_metadata_only_edit_does_not_touch_description(api):
    ticket = ticket_from_original_read({"state": State.DONE, "title": "New title"})
    backend().update(ticket)
    patch = patch_calls(api)[0]
    assert "description" not in patch[2]


def test_metadata_only_edit_of_custom_markdown_does_not_rewrite_it(api):
    ticket = ticket_with_custom_markdown({"state": State.DONE})  # body fields untouched
    backend().update(ticket)
    patch = patch_calls(api)[0]
    assert "description" not in patch[2]


def test_managed_body_edit_merges_and_succeeds(api):
    ticket = ticket_from_original_read({"what": "Updated what content"})
    result = backend().update(ticket)
    patch = patch_calls(api)[0]
    assert patch[2]["description"] == render(ticket)
    assert isinstance(result, Ticket)


def test_unmanaged_body_mutation_is_refused_not_silently_dropped(api):
    ticket = ticket_with_custom_markdown({"what": "Content the caller wants to save"})
    with pytest.raises(BackendError, match="body"):
        backend().update(ticket)
    assert patch_calls(api) == []  # refused before ever writing


# ── regressions (integration review) ──────────────────────────────────────────────


def test_skips_only_change_is_persisted_as_a_body_change(api):
    """render() writes ``ticket.skips`` into the "## Skipped gates" section — a skips-only
    edit (no other body field touched) is a real body change and must be sent, not silently
    dropped by treating ``skips`` as metadata."""
    ticket = ticket_from_original_read({"skips": {"formatting": "legacy import, grandfathered"}})
    backend().update(ticket)
    patch = patch_calls(api)[0]
    assert "description" in patch[2]
    assert patch[2]["description"] == render(ticket)


def test_row_to_ticket_native_due_date_wins_over_stale_body_due_section():
    """The native ``due_date`` column is authoritative over whatever a leftover "## Due" line
    in the Markdown says — a due-date change must read back correctly even if the body hasn't
    been rewritten to match yet."""
    row = {
        "id": ROW_ID, "key": "HYP-1", "version": 3, "title": "T",
        "description": "## Due\n2025-12-25\n",
        "due_date": "2026-01-01", "status_category": "unstarted", "status_name": "Todo",
        "labels": [],
    }
    ticket = backend()._row_to_ticket(row)
    assert ticket.due == "2026-01-01"  # native wins, not the stale "## Due" text
    assert ticket.raw_body == row["description"]  # verbatim Markdown untouched


def test_row_to_ticket_native_due_date_cleared_beats_stale_body_due_section():
    """An explicit native clear (``due_date: null``) must also win — a stale nonempty "## Due"
    line must not resurrect an old date the caller already cleared natively."""
    row = {
        "id": ROW_ID, "key": "HYP-1", "version": 3, "title": "T",
        "description": "## Due\n2025-12-25\n",
        "due_date": None, "status_category": "unstarted", "status_name": "Todo", "labels": [],
    }
    ticket = backend()._row_to_ticket(row)
    assert ticket.due == ""  # native clear wins, not the stale "## Due" text
    assert ticket.raw_body == row["description"]  # verbatim Markdown untouched


def test_row_to_ticket_absent_due_date_key_preserves_body_parsed_value():
    """A response that omits ``due_date`` ENTIRELY (key absent, not present-and-null) is a
    different case from an explicit native clear above: there is no native value to be
    authoritative here at all, so the pre-existing backward-compatible behavior — fall back to
    whatever ``parse()`` derives from the body's own "## Due" section — must be preserved, not
    silently wiped to empty (Phase-3 integration review finding: a truthy/`or ""` check on
    ``row.get("due_date")`` cannot tell "explicitly cleared" apart from "column not in this
    response at all", and previously collapsed both to the same wipe)."""
    row = {
        "id": ROW_ID, "key": "HYP-1", "version": 3, "title": "T",
        "description": "## Due\n2025-12-25\n",
        # due_date key deliberately ABSENT — not set to None/null.
        "status_category": "unstarted", "status_name": "Todo", "labels": [],
    }
    ticket = backend()._row_to_ticket(row)
    assert ticket.due == "2025-12-25"  # body-parsed value preserved, not cleared
    assert ticket.raw_body == row["description"]  # verbatim Markdown untouched


def test_unmanaged_body_mutation_with_new_label_makes_zero_api_calls(api):
    """A refused lossy body edit must be atomic: ``_label_ids`` can CREATE a not-yet-existing
    label as a side effect, so the body-safety check must run BEFORE it — a request carrying
    both an unmanageable body edit and a brand-new label must make no API call whatsoever, not
    just skip the final PATCH."""
    ticket = ticket_with_custom_markdown(
        {"what": "Content the caller wants to save", "labels": ["brand-new-label-not-in-catalog"]}
    )
    with pytest.raises(BackendError, match="body"):
        backend().update(ticket)
    assert api == []  # zero API calls at all — no catalog read, no label creation, no PATCH


# ── UI-normalized template spacing (UB-25 live regression) ─────────────────────────────


def _ui_spaced(body: str) -> str:
    """What tasks-app's editor did to a task-cli body: one blank line after every managed
    heading, plus one at the end. Content is otherwise byte-identical."""
    lines = []
    for line in body.splitlines():
        lines.append(line)
        if line.startswith("## "):
            lines.append("")
    return "\n".join(lines) + "\n\n"


def test_skips_change_on_ui_spaced_managed_body_is_accepted(api):
    original = ticket_from_original_read()
    spaced = _ui_spaced(original.raw_body)
    read = dc_replace(parse(spaced, original), raw_body=spaced)
    read.skips["post-merge-acceptance"] = "verified after deploy"
    backend().update(read)
    (patch,) = patch_calls(api)
    assert "## Skipped gates" in patch[2]["description"]
    assert "Original what" in patch[2]["description"]


def test_metadata_only_edit_on_ui_spaced_body_still_omits_description(api):
    original = ticket_from_original_read()
    spaced = _ui_spaced(original.raw_body)
    read = dc_replace(parse(spaced, original), raw_body=spaced, state=State.DONE)
    backend().update(read)
    (patch,) = patch_calls(api)
    assert "description" not in patch[2]


def test_skips_change_on_a_body_with_an_unknown_section_is_merged_in_place(api):
    original = ticket_from_original_read()
    spaced = _ui_spaced(original.raw_body) + "## Custom notes\n\nhand-written\n"
    read = dc_replace(parse(spaced, original), raw_body=spaced)
    read.skips["post-merge-acceptance"] = "x"
    backend().update(read)
    (patch,) = patch_calls(api)
    assert patch[2]["description"] == spaced + "\n## Skipped gates\n- post-merge-acceptance: x\n"


def test_ticking_a_criterion_on_a_grooming_body_sends_only_that_line_changed(api):
    """task-cli#240 on the tasks-app backend: `task check` on a body with a provenance
    preamble sends the stored description with just the ticked checkbox line rewritten."""
    read = dc_replace(parse(GROOMING_2026_09_10_BODY), id="HYP-1", backend_version=7)
    first = read.acceptance[0]
    read.acceptance[0] = dc_replace(first, checked=True, force_reason="verified on main")
    backend().update(read)
    (patch,) = patch_calls(api)
    assert patch[2]["description"] == GROOMING_2026_09_10_BODY.replace(
        f"- [ ] {first.text}", f"- [x] {first.text} — forced: verified on main"
    )


def test_blank_lines_inside_prose_survive_a_body_update(api):
    original = ticket_from_original_read({"what": "line one\n\n\n\nline two"})
    spaced = _ui_spaced(render(original))
    read = dc_replace(parse(spaced, original), raw_body=spaced)
    read.skips["post-merge-acceptance"] = "x"
    backend().update(read)
    (patch,) = patch_calls(api)
    assert "line one\n\n\n\nline two" in patch[2]["description"]


# ── native status preservation (UB-25 blocker) ─────────────────────────────────────────

BLOCKED_CATALOG = {
    "teams": [{"id": "team-1", "key": TEAM}],
    "statuses": [
        {"id": "todo", "team_id": "team-1", "category": "unstarted", "name": "Todo"},
        {"id": "doing", "team_id": "team-1", "category": "started", "name": "In progress"},
        {"id": "blocked", "team_id": "team-1", "category": "started", "name": "Blocked"},
        {"id": "review", "team_id": "team-1", "category": "started", "name": "In review"},
        {"id": "done", "team_id": "team-1", "category": "completed", "name": "Done"},
    ],
    "labels": [{"id": "lbl-bug", "name": "bug"}],
}


@pytest.fixture
def blocked_api(monkeypatch):
    """A tasks-app whose issue HYP-1 sits in the native "Blocked" status (a second `started`
    status after "In progress"); PATCH applies only the keys it is sent."""
    state = {"row": {
        "id": ROW_ID, "key": "HYP-1", "version": 7, "title": "Blocked one",
        "description": render(Ticket(title="Blocked one", what="w", why="y", user_impact="u",
                                     cost_of_inaction="c", acceptance=["a", "b"])),
        "status_id": "blocked", "status_category": "started", "status_name": "Blocked",
        "labels": [], "due_date": None,
    }}
    calls = []

    def request(url, *, method="GET", headers=None, data=None, **options):
        payload = json.loads(data) if data else None
        path = urlsplit(url).path
        calls.append((method, path, payload))
        if path.endswith("/catalog"):
            return json.dumps(BLOCKED_CATALOG).encode()
        if method == "POST" and path == "/api/v1/issues":
            state["row"].update({"title": payload["title"], "description": payload["description"]})
            return json.dumps(state["row"]).encode()
        if method == "PATCH":
            row = {**state["row"], **{k: v for k, v in payload.items() if k != "version"}}
            row["version"] = state["row"]["version"] + 1
            status = next(s for s in BLOCKED_CATALOG["statuses"] if s["id"] == row["status_id"])
            row.update(status_category=status["category"], status_name=status["name"])
            state["row"] = row
        return json.dumps(state["row"]).encode()

    monkeypatch.setattr("tasklib.backends.tasks.request_bytes", request)
    return state, calls


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: setattr(t, "title", "renamed"),
        lambda t: t.labels.append("bug"),
        lambda t: setattr(t, "due", "2026-12-01"),
        lambda t: setattr(t, "what", "a managed body change"),
    ],
    ids=["title", "label", "due", "body"],
)
def test_non_state_edit_keeps_native_blocked_status(blocked_api, edit):
    state, calls = blocked_api
    b = backend()
    t = b.get("HYP-1")
    edit(t)
    b.update(t)
    (patch,) = [c for c in calls if c[0] == "PATCH"]
    assert "status_id" not in patch[2]
    assert state["row"]["status_id"] == "blocked"


def test_real_state_change_still_sends_the_status(blocked_api):
    state, calls = blocked_api
    b = backend()
    t = b.get("HYP-1")
    t.state = State.DONE
    b.update(t)
    (patch,) = [c for c in calls if c[0] == "PATCH"]
    assert patch[2]["status_id"] == "done"
    assert b.transition("HYP-1", State.IN_REVIEW).state == State.IN_REVIEW
    assert state["row"]["status_id"] == "review"


def test_create_then_update_keeps_the_native_status(blocked_api):
    state, calls = blocked_api
    b = backend()
    created = b.create(Ticket(title="New", what="w", why="y", user_impact="u",
                              cost_of_inaction="c", acceptance=["a", "b"], state=State.IN_PROGRESS))
    created.title = "New, renamed"
    b.update(created)
    patch = [c for c in calls if c[0] == "PATCH"][-1]
    assert "status_id" not in patch[2]
    assert state["row"]["status_id"] == "blocked"
