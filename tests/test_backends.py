"""Backend protocol — the FakeBackend satisfies it and round-trips correctly.

Also covers the GitHub remote-URL parser (pure) without hitting the network.
"""

from __future__ import annotations

import pytest

from tasklib.backends import AmbiguousBackendError, BackendError, EXIT_AMBIGUOUS
from tasklib.backends.github_issues import GitHubIssuesBackend, _api_root, _parse_remote
from tasklib.backends.http import AmbiguousHttpError
from tasklib.model import State, Ticket

from .conftest import assert_protocol


def _ticket() -> Ticket:
    return Ticket(
        title="thing",
        what="do the thing",
        why="reasons",
        user_impact="users",
        cost_of_inaction="pain",
        acceptance=["works"],
        labels=["session:s1", "agent"],
    )


def test_fake_satisfies_protocol(fake_backend):
    assert_protocol(fake_backend)


def test_create_assigns_id_and_url(fake_backend):
    created = fake_backend.create(_ticket())
    assert created.id == "#1"
    assert created.url.endswith("/1")


def test_get_round_trips_body(fake_backend):
    created = fake_backend.create(_ticket())
    fetched = fake_backend.get(created.id)
    assert fetched.what == "do the thing"
    assert [c.text for c in fetched.acceptance] == ["works"]


def test_update_changes_state(fake_backend):
    created = fake_backend.create(_ticket())
    created.state = State.IN_PROGRESS
    updated = fake_backend.update(created)
    assert updated.state == State.IN_PROGRESS


def test_transition(fake_backend):
    created = fake_backend.create(_ticket())
    done = fake_backend.transition(created.id, State.DONE)
    assert done.state == State.DONE


def test_session_tickets_filters_by_label(fake_backend):
    fake_backend.create(_ticket())
    other = _ticket()
    other.labels = ["session:s2"]
    fake_backend.create(other)
    assert len(fake_backend.session_tickets("session:s1")) == 1


def test_search_matches_title(fake_backend):
    fake_backend.create(_ticket())
    assert len(fake_backend.search("thing")) == 1
    assert len(fake_backend.search("nonexistent")) == 0


def test_comment_on_missing_ticket_raises(fake_backend):
    with pytest.raises(BackendError):
        fake_backend.comment("#999", "hi")


def test_attach_records(fake_backend):
    created = fake_backend.create(_ticket())
    ref = fake_backend.attach(created.id, "shot.png")
    assert ref == "shot.png"
    assert fake_backend.attachments == [(created.id, "shot.png")]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/alex-mextner/task-cli.git", ("alex-mextner", "task-cli")),
        ("https://github.com/alex-mextner/task-cli", ("alex-mextner", "task-cli")),
        ("git@github.com:alex-mextner/task-cli.git", ("alex-mextner", "task-cli")),
    ],
)
def test_parse_github_remote(url, expected):
    assert _parse_remote(url) == expected


def test_parse_github_remote_rejects_garbage():
    with pytest.raises(BackendError):
        _parse_remote("https://gitlab.com/x/y.git")


def test_api_root_defaults_to_public_github(monkeypatch):
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    assert _api_root() == "https://api.github.com"


def test_api_root_honors_loopback_http_mock(monkeypatch):
    # A hermetic mock server on loopback may be http; the trailing slash is normalized.
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:8771/")
    assert _api_root() == "http://127.0.0.1:8771"


def test_api_root_allows_https_enterprise_host(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    assert _api_root() == "https://ghe.example.com/api/v3"


def test_api_root_rejects_cleartext_nonloopback(monkeypatch):
    # Security: the bearer token rides every request, so a non-loopback http host is refused —
    # an ambient GITHUB_API_URL must not be able to exfiltrate the token over cleartext.
    monkeypatch.setenv("GITHUB_API_URL", "http://ghe.internal/api/v3")
    with pytest.raises(BackendError):
        _api_root()


def test_api_root_rejects_bracket_malformed_override_cleanly(monkeypatch):
    # review finding (PR #90 round 5, k3): a bracket-malformed GITHUB_API_URL ("https://[bad")
    # makes urlparse() itself raise a bare ValueError — the exact ambient-override scenario the
    # http.py InvalidURL hardening was motivated by, but this module's own direct urlparse() call
    # was unguarded and would crash raw instead of a clean BackendError.
    monkeypatch.setenv("GITHUB_API_URL", "https://[bad/api/v3")
    with pytest.raises(BackendError, match="not a valid url"):
        _api_root()


def test_issues_url_uses_api_root_override(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    assert be._issues_url() == "https://ghe.example.com/api/v3/repos/o/r/issues"
    assert be._issues_url("/1") == "https://ghe.example.com/api/v3/repos/o/r/issues/1"


def test_search_url_uses_api_root_override(monkeypatch):
    # The search() URL is built from _api_root() too; assert the override flows through it.
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    captured = {}

    def fake_call(url, **kw):
        captured["url"] = url
        return {"items": []}

    monkeypatch.setattr(be, "_call", fake_call)
    be.search("hello")
    assert captured["url"].startswith("https://ghe.example.com/api/v3/search/issues?")


def test_github_search_post_filters_by_state(monkeypatch):
    # the GitHub Search API only knows is:open/is:closed; finer states live in labels, so
    # search() must post-filter client-side. Stub _call so no network is touched.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    rows = {
        "items": [
            {
                "number": 1,
                "title": "a",
                "state": "open",
                "labels": [{"name": "status:in-progress"}],
            },
            {
                "number": 2,
                "title": "b",
                "state": "open",
                "labels": [{"name": "status:todo"}],
            },
        ]
    }
    monkeypatch.setattr(be, "_call", lambda url, **kw: rows)
    out = be.search("x", state=State.IN_PROGRESS)
    assert [t.id for t in out] == [
        "#1"
    ]  # only the in-progress one survives the post-filter


def test_github_attach_posts_reference_comment(monkeypatch):
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    calls = []
    monkeypatch.setattr(be, "_call", lambda url, **kw: calls.append((url, kw)) or {})
    ref = be.attach("#7", "shot.png")
    assert ref == "shot.png"
    assert any("/7/comments" in url for url, _ in calls)


def test_github_row_to_ticket_maps_updated_at(monkeypatch):
    # issue #59's global stale-ticket nudge reads Ticket.updated_at; GitHub's REST rows carry
    # this natively as `updated_at`, so _row_to_ticket must pass it through.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {
        "number": 1,
        "title": "a",
        "state": "open",
        "labels": [],
        "body": "",
        "html_url": "https://github.com/o/r/issues/1",
        "updated_at": "2026-07-01T12:00:00Z",
    }
    ticket = be._row_to_ticket(row)
    assert ticket.updated_at == "2026-07-01T12:00:00Z"


def test_github_row_to_ticket_defaults_updated_at_to_empty():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).updated_at == ""


def test_github_row_to_ticket_maps_reporter_from_issue_author():
    # issue #59 stale-nudge, review P2: the scan must scope to the current user's OWN tickets.
    # GitHub's row carries the issue author as `user.login` — task-cli's own `create()` never
    # sets a native assignee, so the AUTHOR (not assignee) is the field that actually identifies
    # "the user who filed this via task-cli".
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {
        "number": 1,
        "title": "a",
        "state": "open",
        "labels": [],
        "body": "",
        "user": {"login": "alex"},
    }
    assert be._row_to_ticket(row).reporter == "alex"


def test_github_row_to_ticket_reporter_defaults_to_empty_when_missing():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).reporter == ""


def test_github_row_to_ticket_marks_provider_closed_when_label_lags_native_state():
    # P2 (review): an issue closed OUTSIDE task-cli can keep a stale `status:todo` label —
    # `_derive_state` still reports `state=TODO` (label wins), so `provider_closed` must be the
    # separate, always-truthful signal the stale-ticket nudge checks instead.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {
        "number": 1,
        "title": "a",
        "state": "closed",
        "labels": [{"name": "status:todo"}],
        "body": "",
    }
    ticket = be._row_to_ticket(row)
    assert (
        ticket.state.value == "todo"
    )  # unchanged — label still wins for the derived state
    assert ticket.provider_closed is True


def test_github_row_to_ticket_provider_closed_false_when_open():
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    row = {"number": 1, "title": "a", "state": "open", "labels": [], "body": ""}
    assert be._row_to_ticket(row).provider_closed is False


def test_github_current_user_returns_login(monkeypatch):
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    seen = {}

    def fake_call(url, **kw):
        seen["url"] = url
        return {"login": "alex"}

    monkeypatch.setattr(be, "_call", fake_call)
    assert be.current_user() == "alex"
    assert seen["url"].endswith("/user")


def test_github_current_user_returns_none_on_backend_error(monkeypatch):
    from tasklib.backends import BackendError
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")

    def raise_err(url, **kw):
        raise BackendError("boom")

    monkeypatch.setattr(be, "_call", raise_err)
    assert be.current_user() is None


def test_github_list_state_filter_scans_full_page_not_just_limit(monkeypatch):
    # HYP-1347 P1 review finding: `list()` used to send `per_page=min(limit, 100)` and only
    # filter by `state` AFTER the fetch — so a repo with `limit` or more non-matching issues
    # ahead of the matching ones in fetch order returned a false-clean scan. `task audit`'s
    # whole point is catching a Done ticket that bypassed task-cli's close gates; this made it
    # silently see nothing in exactly the busy repos where that matters most. A state filter
    # must always scan the FULL page (100, the documented per-run cap) regardless of `limit`.
    # The stub below honors `per_page` (mirrors the real API truncating server-side) so this
    # test is red before the fix for the right reason, not just on a URL-substring assertion.
    import re

    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    # 50 open (non-Done) issues, THEN one closed/Done issue at position 51 — past a `limit=50`
    # fetch cap if the fetch were still capped by `limit` before the state filter runs.
    rows = [
        {"number": i, "title": f"open {i}", "state": "open", "labels": [], "body": ""}
        for i in range(1, 51)
    ]
    rows.append(
        {
            "number": 999,
            "title": "done ticket",
            "state": "closed",
            "labels": [{"name": "status:done"}],
            "body": "",
        }
    )
    seen = {}

    def fake_call(url, **kw):
        seen["url"] = url
        per_page = int(re.search(r"per_page=(\d+)", url).group(1))
        return rows[:per_page]

    monkeypatch.setattr(be, "_call", fake_call)
    out = be.list(state=State.DONE, limit=50)
    assert [t.id for t in out] == ["#999"]
    assert "per_page=100" in seen["url"]  # the full page, not min(limit, 100)


def test_github_list_sorts_by_updated_not_created(monkeypatch):
    # review finding (round 1, Codex): GitHub issues default to `sort=created` — without an
    # explicit `sort=updated`, "the 100 most recent issues" this function's own comment/README
    # claim would actually mean "the 100 oldest-created issues", so an issue created long ago
    # but closed TODAY could sit past a 100-issue page while 100 newer-created-but-stale issues
    # fill it — exactly the class of recently-bypassed Done ticket the audit exists to catch.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    seen = {}

    def fake_call(url, **kw):
        seen["url"] = url
        return []

    monkeypatch.setattr(be, "_call", fake_call)
    be.list(state=State.DONE, limit=50)
    assert "sort=updated" in seen["url"]
    assert "direction=desc" in seen["url"]


def test_github_list_state_filter_paginates_past_pull_request_pollution(monkeypatch):
    # review finding (round 2, Codex): GitHub's `/issues` endpoint mixes pull requests into
    # the same page as issues (`"pull_request" not in r` is the only way to tell them apart,
    # applied AFTER the fetch) — a single 100-row page dominated by PRs could starve the real
    # 100-issue scan budget the state filter depends on, silently missing a Done issue past
    # page 1 even though the fetch nominally covers "the full page". This is the same false-
    # clean-scan failure mode Finding 1 fixed, via a different mechanism (PR pollution instead
    # of premature truncation) — the fix must paginate past PR-heavy pages, not just widen
    # the fetch cap to 100 raw rows.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    # page 1: 100 rows, ALL pull requests (zero real issues) — the exact scenario that starved
    # the scan budget before pagination. page 2: one real, Done issue.
    pr_page = [
        {"number": i, "title": f"pr {i}", "state": "open", "labels": [], "body": "", "pull_request": {}}
        for i in range(1, 101)
    ]
    done_page = [
        {
            "number": 999,
            "title": "done ticket",
            "state": "closed",
            "labels": [{"name": "status:done"}],
            "body": "",
        }
    ]
    seen_pages: list[str] = []

    def fake_call(url, **kw):
        import re

        # `[?&]page=` (not bare `page=`) — the URL also carries `per_page=100`, whose tail
        # would otherwise false-match a bare `page=(\d+)` pattern.
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        seen_pages.append(url)
        return pr_page if page == 1 else done_page

    monkeypatch.setattr(be, "_call", fake_call)
    out = be.list(state=State.DONE, limit=50)
    assert [t.id for t in out] == ["#999"]
    assert len(seen_pages) == 2  # paginated past the all-PR page instead of giving up


def test_github_list_state_filter_stops_paginating_once_scan_target_reached(monkeypatch):
    # regression guard: the pagination loop must terminate — it is bounded by
    # `_STATE_SCAN_TARGET` (100 real issues) as well as `_STATE_SCAN_MAX_PAGES`, so a repo with
    # far more than 100 Done issues doesn't paginate forever.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")

    def fake_call(url, **kw):
        # every page returns 100 real, Done issues — an inexhaustible source
        return [
            {
                "number": i,
                "title": f"done {i}",
                "state": "closed",
                "labels": [{"name": "status:done"}],
                "body": "",
            }
            for i in range(1, 101)
        ]

    calls = []
    monkeypatch.setattr(
        be, "_call", lambda url, **kw: (calls.append(url), fake_call(url))[1]
    )
    be.list(state=State.DONE, limit=50)
    assert len(calls) == 1  # 100 real issues on page 1 already meets the scan target


def test_github_list_state_filter_gives_up_after_the_page_cap(monkeypatch):
    # the documented 10-page (1000 raw rows) cap: a repo whose /issues feed is nothing but pull
    # requests must not turn one state-filtered list() into an unbounded fetch.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    pr_page = [
        {"number": i, "title": f"pr {i}", "state": "open", "labels": [], "body": "", "pull_request": {}}
        for i in range(1, 101)
    ]
    calls: list[str] = []
    monkeypatch.setattr(be, "_call", lambda url, **kw: (calls.append(url), pr_page)[1])
    out = be.list(state=State.DONE, limit=50)
    assert out == []
    assert len(calls) == GitHubIssuesBackend._STATE_SCAN_MAX_PAGES == 10


def test_github_list_state_filter_stops_once_limit_matches_are_in_hand(monkeypatch):
    # review finding (perf): the caller truncates to `limit` anyway, so once `limit` matching
    # issues have been seen there is nothing left worth another request — 5 Done issues on
    # page 1 satisfy limit=5 even though the 100-issue scan budget is not exhausted.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    page = [
        {"number": i, "title": f"done {i}", "state": "closed", "labels": [{"name": "status:done"}], "body": ""}
        for i in range(1, 6)
    ] + [
        {"number": i, "title": f"open {i}", "state": "open", "labels": [], "body": ""}
        for i in range(6, 101)
    ]
    calls: list[str] = []
    monkeypatch.setattr(be, "_call", lambda url, **kw: (calls.append(url), page)[1])
    out = be.list(state=State.DONE, limit=5)
    assert len(out) == 5
    assert len(calls) == 1


def test_github_list_state_filter_dedups_an_issue_served_on_two_pages(monkeypatch):
    # review finding: `sort=updated` offset pagination is a live ordering — an issue touched
    # between the page-1 and page-2 fetches is served AGAIN at the head of page 2. It must not
    # appear twice in the result, count twice toward `limit`, or eat the scan budget twice.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    done = {"number": 7, "title": "done 7", "state": "closed", "labels": [{"name": "status:done"}], "body": ""}
    page1 = [done] + [
        {"number": i, "title": f"pr {i}", "state": "open", "labels": [], "body": "", "pull_request": {}}
        for i in range(100, 199)
    ]
    page2 = [done, {"number": 8, "title": "done 8", "state": "closed", "labels": [{"name": "status:done"}], "body": ""}]

    def fake_call(url, **kw):
        import re

        return page1 if int(re.search(r"[?&]page=(\d+)", url).group(1)) == 1 else page2

    monkeypatch.setattr(be, "_call", fake_call)
    out = be.list(state=State.DONE, limit=2)
    assert [t.id for t in out] == ["#7", "#8"]


def test_github_list_truncates_filtered_results_to_limit(monkeypatch):
    # review finding (round 1, Opus, Finding 1/2): the full-page fetch above must still be
    # truncated to `limit` AFTER filtering — otherwise a state-filtered `list(limit=5)` on a
    # busy repo returns up to 100 matches instead of 5, silently breaking any caller relying on
    # `limit` as a hard cap (display pagination, downstream slicing). The three pre-existing
    # state-filter tests only ever stub ONE matching row, so `len(out) == 1` regardless of
    # whether truncation actually happens — this uses 60 matching Done issues so the assertion
    # is a genuine tie-breaker, not vacuously true either way.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    rows = [
        {
            "number": i,
            "title": f"done {i}",
            "state": "closed",
            "labels": [{"name": "status:done"}],
            "body": "",
        }
        for i in range(1, 61)
    ]
    monkeypatch.setattr(be, "_call", lambda url, **kw: rows)
    out = be.list(state=State.DONE, limit=50)
    assert len(out) == 50


def test_github_list_without_state_filter_still_caps_fetch_to_limit(monkeypatch):
    # the fix above must not regress the common unfiltered hot path (e.g. interactive
    # `task list`) into always fetching a full page it doesn't need.
    from tasklib.backends.github_issues import GitHubIssuesBackend

    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    seen = {}

    def fake_call(url, **kw):
        seen["url"] = url
        return []

    monkeypatch.setattr(be, "_call", fake_call)
    be.list(limit=10)
    assert "per_page=10" in seen["url"]


def test_issue_number_removeprefix_not_lstrip():
    from tasklib.backends.github_issues import _issue_number

    assert _issue_number("#1") == "1"
    assert (
        _issue_number("##1") == "#1"
    )  # removeprefix drops exactly one '#', not all of them


def test_linear_label_ids_creates_missing_labels(monkeypatch):
    # session:<id> and needs-triage won't pre-exist in a Linear team; _label_ids must CREATE
    # them (not silently drop) so the durable session label survives — else `task list` breaks.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._labels = {"ui": "lbl-ui"}  # ui exists; the session label does not
    created: list[str] = []

    def fake_gql(query, variables=None):
        if "issueLabelCreate" in query:
            name = variables["input"]["name"]
            created.append(name)
            return {
                "issueLabelCreate": {
                    "success": True,
                    "issueLabel": {"id": f"new-{name}", "name": name},
                }
            }
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    ids = be._label_ids(["ui", "session:abc", "needs-triage"])
    assert "lbl-ui" in ids
    assert "new-session:abc" in ids and "new-needs-triage" in ids
    assert created == [
        "session:abc",
        "needs-triage",
    ]  # only the missing ones were created


def test_linear_node_to_ticket_seeds_native_due_date(monkeypatch):
    # a ticket created/edited in the Linear UI carries dueDate natively (no body Due section);
    # _node_to_ticket must seed Ticket.due from it so the daemon still sees the date.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "native due",
        "url": "https://linear/HYP-7",
        "description": "",  # no body Due section
        "dueDate": "2026-09-01",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    ticket = be._node_to_ticket(node)
    assert ticket.due == "2026-09-01"


def test_linear_node_to_ticket_maps_updated_at():
    # issue #59's global stale-ticket nudge reads Ticket.updated_at; Linear's node carries this
    # natively as `updatedAt`, so _node_to_ticket must pass it through.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "2026-07-01T12:00:00.000Z",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).updated_at == "2026-07-01T12:00:00.000Z"


def test_linear_issue_fields_requests_attachments_beyond_default_page_size():
    # review finding: Linear's `attachments` connection defaults to a 50-item page; without an
    # explicit `first:`, a ticket with 51+ attachments would silently lose everything past the
    # first page. Pin the query shape so a regression (someone drops the `first:250`) fails here
    # rather than only manifesting as missing attachments in production.
    from tasklib.backends.linear import LinearBackend

    assert "attachments(first:250)" in LinearBackend._ISSUE_FIELDS


def test_linear_issue_fields_requests_project_id():
    # task-cli#83 2nd-round review finding: the single-ticket fetch must request the issue's
    # OWN project so `_node_to_ticket` can report it distinct from this backend's configured
    # query scope. Pin the query shape so a regression (someone drops `project{id}`) fails here.
    from tasklib.backends.linear import LinearBackend

    assert "project{id}" in LinearBackend._ISSUE_FIELDS


def test_linear_node_to_ticket_reports_actual_project_id():
    # task-cli#83 2nd-round review finding: a same-team, different-project ticket must be
    # distinguishable from the backend's own configured project after a fetch.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", project="proj-a")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
        "project": {
            "id": "proj-b"
        },  # NOT proj-a — genuinely a different project on the team
    }
    assert be._node_to_ticket(node).backend_project_id == "proj-b"


def test_linear_node_to_ticket_backend_project_id_empty_when_absent():
    # review finding: `list`/`search` DO request `project{id}` too (for their own team/project
    # filtering, see their query strings) — the only real path whose response never carries it
    # is `create()`'s. This test just pins the degrade-gracefully behavior for a raw node that
    # happens to omit the key (e.g. a malformed/partial response), not "the bulk paths never
    # send it" — `_node_to_ticket` itself is fetch-path-agnostic, it only reads what's given.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).backend_project_id == ""


def test_linear_node_to_ticket_reports_project_id_from_list_shaped_node():
    # review finding: confirm `backend_project_id` IS populated from a `list`/`search`-shaped
    # node too (they request `project{id}` alongside `team{key}`) — not just the single-ticket
    # `get`/`update` shape covered above.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", project="proj-a")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
        "team": {"key": "HYP"},
        "project": {"id": "proj-b"},
    }
    assert be._node_to_ticket(node).backend_project_id == "proj-b"


def test_linear_list_query_requests_updated_at(monkeypatch):
    # test_linear_node_to_ticket_maps_updated_at above injects `updatedAt` directly into a hand
    # built node, so it would keep passing even if `_ISSUE_FIELDS` stopped requesting the field
    # from Linear — silently losing every Linear stale-ticket warning in production. Drive the
    # real list() query so a dropped `updatedAt` in _ISSUE_FIELDS breaks a test, not just prod.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {
            "issues": {
                "nodes": [
                    {
                        "identifier": "HYP-7",
                        "title": "t",
                        "url": "https://linear/HYP-7",
                        "description": "",
                        "dueDate": None,
                        "updatedAt": "2026-07-01T12:00:00.000Z",
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    }
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    tickets = be.list()
    assert "updatedAt" in seen["query"]
    assert tickets[0].updated_at == "2026-07-01T12:00:00.000Z"


def test_linear_body_due_overrides_native_due_date():
    # when the body carries a ## Due section, it is the source of truth — it overrides the native
    # dueDate (a divergence shouldn't silently prefer the native field)
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket
    from tasklib.render import render

    be = LinearBackend(api_key="k", team_key="HYP")
    body = render(
        Ticket(title="t", what="w", due="2026-10-10")
    )  # body Due = 2026-10-10
    node = {
        "identifier": "HYP-8",
        "title": "t",
        "url": "u",
        "description": body,
        "dueDate": "2026-01-01",  # native says something else
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).due == "2026-10-10"


def test_linear_update_clears_native_due_when_empty(monkeypatch):
    # clearing the due date (empty) must send dueDate: null so the Linear UI stays in sync
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._states_by_type = {"unstarted": "st-1"}
    seen: dict = {}

    def fake_gql(query, variables=None):
        if "issue(id" in query:
            return {"issue": {"id": "node-1", "identifier": "HYP-3"}}
        if "issueUpdate" in query:
            seen["input"] = variables["input"]
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {
                        "identifier": "HYP-3",
                        "title": "t",
                        "url": "u",
                        "description": variables["input"]["description"],
                        "dueDate": None,
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    },
                }
            }
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    monkeypatch.setattr(be, "_label_ids", lambda names: [])
    be.update(Ticket(id="HYP-3", title="t", what="w", due=""))  # cleared
    assert seen["input"]["dueDate"] is None


def test_linear_create_mirrors_due_into_native_field(monkeypatch):
    # the body Due section is the portable source of truth; create also echoes it into the
    # native dueDate so it shows in the Linear UI.
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._states_by_type = {"unstarted": "st-1"}
    seen: dict = {}

    def fake_gql(query, variables=None):
        if "issueCreate" in query:
            seen["input"] = variables["input"]
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "identifier": "HYP-9",
                        "title": "t",
                        "url": "u",
                        "description": variables["input"]["description"],
                        "dueDate": variables["input"].get("dueDate"),
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                    },
                }
            }
        return {}

    monkeypatch.setattr(be, "_gql", fake_gql)
    monkeypatch.setattr(be, "_label_ids", lambda names: [])
    be.create(Ticket(title="t", what="w", due="2026-09-01"))
    assert seen["input"]["dueDate"] == "2026-09-01"


def test_linear_list_scopes_filter_to_team_and_project(monkeypatch):
    # the IssueFilter must carry the team, and the project too when pinned — otherwise two
    # registry entries on the same team but different projects would list identical issues.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", project="proj-9")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["filter"] = variables["filter"]
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    be.list()
    assert seen["filter"]["team"] == {"key": {"eq": "HYP"}}
    assert seen["filter"]["project"] == {"id": {"eq": "proj-9"}}


def test_linear_list_state_done_filters_server_side(monkeypatch):
    # HYP-1347 P1 review finding, Linear side: `list(state=State.DONE)` used to send
    # `first: min(limit, 100)` with NO state filter in the GraphQL query, then filter client-
    # side after the fetch — a repo with `limit`+ non-Done issues sorted ahead of the Done ones
    # returned a false-clean scan, defeating the audit's whole point. Linear's normalized state
    # is derived purely from the native workflow-state `type` (no label-vs-native divergence to
    # protect, unlike GitHub), and DONE has a unique reverse type ("completed") — so the fix can
    # filter server-side with zero risk of a new blind spot: `state: {type: {eq: "completed"}}`.
    # The stub below honors `n` (mirrors the real API truncating server-side) so this is red
    # before the fix for the right reason, not just an assertion on the query variables.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    nodes = [
        {
            "identifier": f"HYP-{i}",
            "title": f"open {i}",
            "state": {"type": "started"},
            "labels": {"nodes": []},
        }
        for i in range(1, 51)
    ]
    nodes.append(
        {
            "identifier": "HYP-999",
            "title": "done ticket",
            "state": {"type": "completed"},
            "labels": {"nodes": []},
        }
    )
    seen = {}

    def fake_gql(query, variables=None):
        seen["filter"] = variables["filter"]
        n = seen["n"] = variables["n"]
        flt = variables["filter"]
        matching = nodes
        if "state" in flt:
            wanted = flt["state"]["type"]["eq"]
            matching = [node for node in nodes if node["state"]["type"] == wanted]
        return {"issues": {"nodes": matching[:n]}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    out = be.list(state=State.DONE, limit=50)
    assert [t.id for t in out] == ["HYP-999"]
    assert seen["filter"]["state"] == {"type": {"eq": "completed"}}
    # review finding (round 1, Opus, minor): the server already filters exactly to DONE here,
    # so `n` must stay `min(limit, 100)` — bumping it to a full-page fetch on this path would be
    # a pointless regression (the server-side filter makes the full-page fallback unnecessary).
    assert seen["n"] == 50


def test_linear_list_truncates_non_done_filtered_results_to_limit(monkeypatch):
    # review finding (round 1, Opus, Finding 1/2): the client-side-filter fallback path (used
    # for TODO/IN_PROGRESS/IN_REVIEW) must still truncate to `limit` after filtering — otherwise
    # `list(state=State.TODO, limit=5)` on a repo with 60 TODO issues in the fetched page returns
    # up to 60 instead of 5. Uses 60 matching rows so the assertion is a genuine tie-breaker.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    nodes = [
        {
            "identifier": f"HYP-{i}",
            "title": f"todo {i}",
            "state": {"type": "unstarted"},
            "labels": {"nodes": []},
        }
        for i in range(1, 61)
    ]

    def fake_gql(query, variables=None):
        return {"issues": {"nodes": nodes[: variables["n"]]}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    out = be.list(state=State.TODO, limit=50)
    assert len(out) == 50


def test_linear_list_non_done_state_filter_scans_full_page_not_just_limit(monkeypatch):
    # DONE/CANCELLED are the only states with a unique reverse native type — TODO can come from
    # THREE distinct native types (backlog/unstarted/triage — see `_TYPE_TO_STATE`), so it can't
    # be narrowed to a single `type: {eq: ...}` without under-fetching. Those states must still
    # fall back to fetching the full page (100) before filtering, exactly like GitHub.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    nodes = [
        {
            "identifier": f"HYP-{i}",
            "title": f"in progress {i}",
            "state": {"type": "started"},
            "labels": {"nodes": []},
        }
        for i in range(1, 51)
    ]
    nodes.append(
        {
            "identifier": "HYP-999",
            "title": "todo ticket via backlog type",
            "state": {"type": "backlog"},
            "labels": {"nodes": []},
        }
    )
    seen = {}

    def fake_gql(query, variables=None):
        seen["n"] = variables["n"]
        return {"issues": {"nodes": nodes[: variables["n"]]}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    out = be.list(state=State.TODO, limit=50)
    assert [t.id for t in out] == ["HYP-999"]
    assert seen["n"] == 100


def test_linear_list_without_state_filter_still_caps_fetch_to_limit(monkeypatch):
    # regression guard: the fix must not make the common unfiltered path (e.g. cross-project
    # aggregation) always fetch a full page it doesn't need.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    seen = {}

    def fake_gql(query, variables=None):
        seen["n"] = variables["n"]
        return {"issues": {"nodes": []}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    be.list(limit=10)
    assert seen["n"] == 10


def test_linear_search_filters_results_to_team(monkeypatch):
    # searchIssues is workspace-wide; the backend must drop hits from other teams so a
    # cross-project `find` doesn't attribute the whole workspace to this Linear group.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    def fake_gql(query, variables=None):
        return {
            "searchIssues": {
                "nodes": [
                    {
                        "identifier": "HYP-1",
                        "title": "mine",
                        "team": {"key": "HYP"},
                        "project": None,
                        "state": {"type": "started"},
                        "labels": {"nodes": []},
                    },
                    {
                        "identifier": "OTH-9",
                        "title": "theirs",
                        "team": {"key": "OTH"},
                        "project": None,
                        "state": {"type": "started"},
                        "labels": {"nodes": []},
                    },
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    hits = be.search("anything")
    ids = {t.id for t in hits}
    assert ids == {"HYP-1"}  # the OTH team's hit is scoped out


def test_linear_node_to_ticket_maps_reporter_from_creator():
    # issue #59 stale-nudge, review P2: Linear's node carries the issue creator as `creator.id` —
    # task-cli's own `create()` never sets a native assignee, so the creator identifies "the user
    # who filed this via task-cli".
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
        "creator": {"id": "user-123"},
    }
    assert be._node_to_ticket(node).reporter == "user-123"


def test_linear_node_to_ticket_reporter_defaults_to_empty_when_missing():
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "unstarted"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).reporter == ""


def test_linear_list_query_requests_creator(monkeypatch):
    # mirrors test_linear_list_query_requests_updated_at: prove the real list() query asks for
    # `creator`, so a future edit dropping it from _ISSUE_FIELDS breaks a test, not just prod.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    seen: dict = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {
            "issues": {
                "nodes": [
                    {
                        "identifier": "HYP-7",
                        "title": "t",
                        "url": "https://linear/HYP-7",
                        "description": "",
                        "dueDate": None,
                        "updatedAt": "",
                        "state": {"type": "unstarted"},
                        "labels": {"nodes": []},
                        "creator": {"id": "user-123"},
                    }
                ]
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    tickets = be.list()
    assert "creator" in seen["query"]
    assert tickets[0].reporter == "user-123"


def test_linear_node_to_ticket_marks_provider_closed_from_completed_state():
    # Linear's normalized state is derived straight from its native workflow state — no separate
    # label-lag risk — so `provider_closed` simply mirrors "state.type is a closed type".
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "completed"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).provider_closed is True


def test_linear_node_to_ticket_provider_closed_false_for_active_state():
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "identifier": "HYP-7",
        "title": "t",
        "url": "https://linear/HYP-7",
        "description": "",
        "dueDate": None,
        "updatedAt": "",
        "state": {"type": "started"},
        "labels": {"nodes": []},
    }
    assert be._node_to_ticket(node).provider_closed is False


def test_linear_current_user_returns_viewer_id(monkeypatch):
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    seen = {}

    def fake_gql(query, variables=None):
        seen["query"] = query
        return {"viewer": {"id": "user-123"}}

    monkeypatch.setattr(be, "_gql", fake_gql)
    assert be.current_user() == "user-123"
    assert "viewer" in seen["query"]


def test_linear_current_user_returns_none_on_backend_error(monkeypatch):
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    def raise_err(query, variables=None):
        raise BackendError("boom")

    monkeypatch.setattr(be, "_gql", raise_err)
    assert be.current_user() is None


@pytest.mark.parametrize("bad_response", [None, [], "viewer", 42])
def test_linear_current_user_returns_none_on_non_dict_response(
    monkeypatch, bad_response
):
    """`_gql` is typed to return a dict, but a malformed response or a third-party override
    could hand back something else — mirror the `isinstance(row, dict)` guard the GitHub
    backend already has for its own `/user` call (review finding: `current_user` must never
    raise, per its own docstring, rather than relying on a caller's broad except). Parametrized
    across a few non-dict shapes (not just `None`) so the guard's actual contract — ANY
    non-dict, not one specific falsy value — is locked in (review finding, round 2)."""
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    monkeypatch.setattr(be, "_gql", lambda query, variables=None: bad_response)
    assert be.current_user() is None


@pytest.mark.parametrize("bad_viewer", ["not-a-dict", 42, [], True])
def test_linear_current_user_returns_none_on_non_dict_viewer(monkeypatch, bad_viewer):
    """A dict root response with a truthy non-dict ``viewer`` value (e.g. a string) would crash
    `.get("id")` if only the root were guarded — `"invalid" or {}` evaluates to `"invalid"`,
    not `{}`, since the `or` fallback only fires on a FALSY viewer (review finding, round 3)."""
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")

    monkeypatch.setattr(
        be, "_gql", lambda query, variables=None: {"viewer": bad_viewer}
    )
    assert be.current_user() is None


# ── ambiguous read failure (task-cli bug 1: a crash after a successful create) ──────────────


def test_github_call_wraps_ambiguous_http_error(monkeypatch):
    # request_json raising AmbiguousHttpError (the connection dropped mid-read after a 2xx —
    # see test_http.py) must surface through _call as the distinctly-typed AmbiguousBackendError,
    # not the generic BackendError every other failure funnels into.
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    monkeypatch.setattr(
        "tasklib.backends.github_issues.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(
            AmbiguousHttpError("status 201 was already returned")
        ),
    )
    with pytest.raises(AmbiguousBackendError) as exc:
        be._call("https://api.github.com/repos/o/r/issues", method="POST")
    assert "201" in str(exc.value)


def test_github_call_ambiguous_error_is_not_caught_as_backend_error(monkeypatch):
    # the whole point of the separate type: a bare `except BackendError` (used by nearly every
    # other call site in cli.py) must NOT swallow this — that bucket means "nothing happened".
    be = GitHubIssuesBackend(owner="o", repo="r", token="t")
    monkeypatch.setattr(
        "tasklib.backends.github_issues.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(AmbiguousHttpError("boom")),
    )
    try:
        be._call("url", method="POST")
    except BackendError:
        pytest.fail("AmbiguousBackendError must not be catchable as BackendError")
    except AmbiguousBackendError:
        pass


def test_linear_gql_wraps_ambiguous_http_error(monkeypatch):
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    monkeypatch.setattr(
        "tasklib.backends.linear.request_json",
        lambda *a, **kw: (_ for _ in ()).throw(
            AmbiguousHttpError("status 200 was already returned")
        ),
    )
    with pytest.raises(AmbiguousBackendError):
        be._gql("mutation { issueCreate { success } }")


def test_exit_ambiguous_does_not_collide_with_agenttools_errors_contract():
    # unlike EXIT_ILLEGAL_TRANSITION (which deliberately pins to the shared EXIT_USAGE), this
    # code is deliberately NOT aliased to anything in agenttools_errors — review finding: the
    # closest-sounding shared class, EXIT_NETWORK (7), documents itself as the safe-to-retry
    # class ("DNS, timeout, 5xx"), and this exception means the opposite (verify before
    # retrying). Assert no accidental collision with ANY shared code, not just EXIT_NETWORK, so a
    # future contract addition can't silently start meaning "safe to retry" for this value too.
    try:
        from agenttools_errors import EXIT_CODES
    except Exception:  # noqa: BLE001 - shared lib optional in this env
        pytest.skip("agenttools_errors not installed")
    assert EXIT_AMBIGUOUS not in EXIT_CODES


# ── linear.attach — attachment_mode (link / native) and the fileUpload/attachmentCreate flow ──


def _linear_gql_stub(monkeypatch, be, responses: dict[str, object]):
    """Route ``be._gql`` calls to canned responses keyed by a substring of the query — mirrors
    the file's existing ``fake_gql`` pattern but supports several distinct mutations in one test
    (issueByIdentifier / attachmentCreate / fileUpload all fire inside a single ``attach()``)."""
    calls = []

    def fake_gql(query, variables=None):
        calls.append((query, variables))
        for needle, resp in responses.items():
            if needle in query:
                return resp
        raise AssertionError(f"unexpected gql call: {query}")

    monkeypatch.setattr(be, "_gql", fake_gql)
    return calls


def test_linear_attach_hosted_url_registers_regardless_of_mode(monkeypatch):
    # A ref that's already a URL (e.g. a GitHub PR asset) is always just registered as-is — no
    # upload attempt, no mode branching — for BOTH attachment_mode values.
    from tasklib.backends.linear import LinearBackend

    for mode in ("link", "native"):
        be = LinearBackend(api_key="k", team_key="HYP", attachment_mode=mode)
        calls = _linear_gql_stub(
            monkeypatch,
            be,
            {
                "issue(id": {"issue": {"id": "internal-1"}},
                "attachmentCreate": {"attachmentCreate": {"success": True}},
            },
        )
        result = be.attach("HYP-1", "https://github.com/user-attachments/assets/abc")
        assert result == "https://github.com/user-attachments/assets/abc"
        create_calls = [v for q, v in calls if "attachmentCreate" in q]
        assert create_calls == [
            {
                "input": {
                    "issueId": "internal-1",
                    "title": "abc",
                    "url": "https://github.com/user-attachments/assets/abc",
                }
            }
        ]


def test_linear_attach_link_mode_local_path_raises_backend_error(monkeypatch):
    # link mode has nothing to link a bare local path to — raise BackendError (the caller,
    # cli._attach_screenshots, already swallows this as best-effort).
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="link")
    _linear_gql_stub(monkeypatch, be, {"issue(id": {"issue": {"id": "internal-1"}}})
    with pytest.raises(BackendError, match="attachment_mode=link"):
        be.attach("HYP-1", "/tmp/shot.png")


def test_linear_attach_hosted_url_registration_rejected_raises_backend_error(
    monkeypatch,
):
    # review finding (missing test): attachmentCreate returning success=false for the
    # ALREADY-HOSTED-URL path must raise, not silently report the ref as attached.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="link")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "attachmentCreate": {"attachmentCreate": {"success": False}},
        },
    )
    with pytest.raises(BackendError, match="attachmentCreate"):
        be.attach("HYP-1", "https://github.com/user-attachments/assets/abc")


def test_linear_from_config_default_attachment_mode_is_native():
    # review finding (missing test): the main behavioral change this feature introduces for
    # EXISTING users — a repo with no `attachment_mode` set at all must get the real upload
    # path (not the old no-op file:// registration) by default.
    from pathlib import Path

    from tasklib.backends.linear import LinearBackend
    from tasklib.config import DEFAULTS, LoadedConfig

    data = {**DEFAULTS, "linear": {**DEFAULTS["linear"], "team": "HYP"}}
    cfg = LoadedConfig(data=data, repo_root=Path("."))
    be = LinearBackend.from_config(cfg, env={"LINEAR_API_KEY": "k"})
    assert be.attachment_mode == "native"


def test_linear_attach_native_mode_uploads_and_registers(monkeypatch, tmp_path):
    # The real native path: fileUpload -> PUT the bytes to the signed URL (with Linear's exact
    # headers) -> attachmentCreate with the returned assetUrl.
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n fake bytes")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    calls = _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "assetUrl": "https://uploads.linear.app/asset-1",
                        "uploadUrl": "https://storage.googleapis.com/signed-put-url",
                        "headers": [
                            {"key": "x-goog-content-length-range", "value": "20,20"}
                        ],
                    },
                }
            },
            "attachmentCreate": {"attachmentCreate": {"success": True}},
        },
    )
    put_calls = []

    def fake_request_bytes(
        url,
        *,
        method="GET",
        headers=None,
        data=None,
        timeout=60,
        same_host_redirects_only=False,
    ):
        put_calls.append(
            (url, method, dict(headers or {}), data, same_host_redirects_only)
        )
        return b""

    monkeypatch.setattr("tasklib.backends.linear.request_bytes", fake_request_bytes)

    result = be.attach("HYP-1", str(png))

    assert result == "https://uploads.linear.app/asset-1"
    assert len(put_calls) == 1
    put_url, put_method, put_headers, put_body, put_redirects_guarded = put_calls[0]
    assert put_url == "https://storage.googleapis.com/signed-put-url"
    assert put_method == "PUT"
    assert put_headers["x-goog-content-length-range"] == "20,20"
    assert put_headers["Content-Type"] == "image/png"
    assert put_body == png.read_bytes()
    # review finding: consistency with fetch_attachment_bytes — the upload PUT also refuses a
    # cross-host/downgrade redirect, even though this request carries no Authorization header.
    assert put_redirects_guarded is True
    create_calls = [v for q, v in calls if "attachmentCreate" in q]
    assert create_calls == [
        {
            "input": {
                "issueId": "internal-1",
                "title": "shot.png",
                "url": "https://uploads.linear.app/asset-1",
            }
        }
    ]


def test_linear_attach_native_mode_upload_put_failure_raises_backend_error(
    monkeypatch, tmp_path
):
    from tasklib.backends import BackendError
    from tasklib.backends.http import HttpError
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"x")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "assetUrl": "https://uploads.linear.app/asset-1",
                        "uploadUrl": "https://signed",
                        "headers": [],
                    },
                }
            },
        },
    )
    monkeypatch.setattr(
        "tasklib.backends.linear.request_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(HttpError(403, "HTTP 403", "expired")),
    )
    with pytest.raises(BackendError, match="upload PUT"):
        be.attach("HYP-1", str(png))


def test_linear_attach_native_mode_malformed_upload_url_raises_backend_error_not_raw_crash(
    monkeypatch, tmp_path
):
    # review finding (PR #90 round 5, Fable/k3): the signed uploadUrl in fileUpload's response is
    # LINEAR'S OWN data, not something this process fully controls either — a bracket-malformed
    # value ("https://[bad") used to crash _upload_and_attach with a raw ValueError from
    # urllib.request.Request()'s own IPv6-bracket parsing, escaping past the HttpError-only
    # except clause below (InvalidURL alone doesn't cover a construction-time ValueError).
    # request_bytes now catches that ValueError at construction time and wraps it as a clean
    # HttpError, so this surfaces as the same "upload PUT ... failed" BackendError as any other
    # upload failure. Exercised through the REAL request_bytes (no monkeypatch) since the fix
    # lives there, not in this backend.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"x")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "assetUrl": "https://uploads.linear.app/asset-1",
                        "uploadUrl": "https://[bad",
                        "headers": [],
                    },
                }
            },
        },
    )
    with pytest.raises(BackendError, match="upload PUT"):
        be.attach("HYP-1", str(png))


def test_linear_attach_native_mode_upload_put_ambiguous_failure_raises_ambiguous_backend_error(
    monkeypatch, tmp_path
):
    # the connection-dropped-mid-read case (status already landed, bytes may have uploaded
    # server-side with nothing pointing at them since attachmentCreate never ran) must surface
    # as AmbiguousBackendError, not the generic BackendError every other failure funnels into —
    # the same distinction _gql's own ambiguous-read handling makes.
    from tasklib.backends import AmbiguousBackendError
    from tasklib.backends.http import AmbiguousHttpError
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"x")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "assetUrl": "https://uploads.linear.app/asset-1",
                        "uploadUrl": "https://signed",
                        "headers": [],
                    },
                }
            },
        },
    )
    monkeypatch.setattr(
        "tasklib.backends.linear.request_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(
            AmbiguousHttpError("status 200 was already returned")
        ),
    )
    with pytest.raises(AmbiguousBackendError, match="upload PUT"):
        be.attach("HYP-1", str(png))


@pytest.mark.parametrize(
    "file_upload_response",
    [
        {"fileUpload": {"success": True, "uploadFile": None}},
        {"fileUpload": {"success": True}},  # uploadFile key missing entirely
        {
            "fileUpload": {
                "success": True,
                "uploadFile": {"assetUrl": "https://uploads.linear.app/x"},
            }
        },  # no uploadUrl
        {
            "fileUpload": {
                "success": True,
                "uploadFile": {"uploadUrl": "https://signed"},
            }
        },  # no assetUrl
        {"fileUpload": {"success": True, "uploadFile": "not-a-dict"}},
    ],
    ids=[
        "null-uploadfile",
        "missing-uploadfile-key",
        "missing-uploadurl",
        "missing-asseturl",
        "non-dict-uploadfile",
    ],
)
def test_linear_attach_native_mode_malformed_fileupload_response_raises_cleanly(
    monkeypatch, tmp_path, file_upload_response
):
    # review finding: `success: true` alone doesn't guarantee a well-formed `uploadFile` — a
    # bare KeyError/TypeError from blindly indexing it would escape _attach_screenshots's
    # BackendError-only best-effort catch and crash an already-successful create/update.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"x")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": file_upload_response,
        },
    )
    with pytest.raises(BackendError, match="malformed"):
        be.attach("HYP-1", str(png))


@pytest.mark.parametrize(
    "bad_headers",
    [
        "not-a-list",  # a string is iterable char-by-char in Python — must still be rejected
        [{"key": "x"}],  # missing "value"
        [{"value": "y"}],  # missing "key"
        [{"key": 1, "value": "y"}],  # non-string key
        ["not-a-dict"],
    ],
    ids=[
        "non-list",
        "missing-value",
        "missing-key",
        "non-string-key",
        "non-dict-entry",
    ],
)
def test_linear_attach_native_mode_malformed_upload_headers_raises_cleanly(
    monkeypatch, tmp_path, bad_headers
):
    # `headers: null` is deliberately NOT in this list — _parse_upload_headers treats an absent/
    # null headers value as "no extra headers" (valid, see test_parse_upload_headers_empty_and_
    # none_are_valid), the same as an empty list; it is these genuinely malformed SHAPES that
    # must raise. review finding: the previous `{h["key"]: h["value"] for h in upload_file.get("headers", [])}`
    # blindly indexed each entry — a malformed `headers` value must raise BackendError, not a
    # raw TypeError/KeyError that escapes _attach_screenshots's best-effort catch.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    png = tmp_path / "shot.png"
    png.write_bytes(b"x")

    be = LinearBackend(api_key="k", team_key="HYP", attachment_mode="native")
    _linear_gql_stub(
        monkeypatch,
        be,
        {
            "issue(id": {"issue": {"id": "internal-1"}},
            "fileUpload": {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "assetUrl": "https://uploads.linear.app/asset-1",
                        "uploadUrl": "https://signed",
                        "headers": bad_headers,
                    },
                }
            },
        },
    )
    with pytest.raises(BackendError, match="upload headers"):
        be.attach("HYP-1", str(png))


def test_parse_upload_headers_empty_and_none_are_valid():
    from tasklib.backends.linear import _parse_upload_headers

    assert _parse_upload_headers(None) == {}
    assert _parse_upload_headers([]) == {}
    assert _parse_upload_headers(
        [{"key": "Content-Disposition", "value": 'attachment; filename="x"'}]
    ) == {"Content-Disposition": 'attachment; filename="x"'}


def test_linear_is_native_attachment_url():
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    assert be.is_native_attachment_url("https://uploads.linear.app/abc") is True
    assert (
        be.is_native_attachment_url("http://uploads.linear.app/abc") is False
    )  # plaintext
    assert (
        be.is_native_attachment_url("https://github.com/user-attachments/assets/abc")
        is False
    )
    assert be.is_native_attachment_url("file:///etc/passwd") is False


def test_linear_is_native_attachment_url_handles_unparseable_url():
    # review finding: urlparse() itself raises ValueError on some malformed urls (e.g. invalid
    # IPv6-bracket syntax) — an attachment's url is tracker-controlled data, so this must
    # degrade to "not native" cleanly, not propagate a raw ValueError past a boolean predicate.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    assert be.is_native_attachment_url("https://[bad") is False


def test_linear_is_native_attachment_url_normalizes_port_and_case():
    # review finding: `.netloc` includes the port and preserves case — a genuine asset url with
    # an explicit default port or different host casing must still classify as native
    # (`.hostname` is lowercased and port-stripped; `.netloc` alone would wrongly say "no").
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    assert be.is_native_attachment_url("https://uploads.linear.app:443/asset-1") is True
    assert be.is_native_attachment_url("https://UPLOADS.LINEAR.APP/asset-1") is True
    # a genuinely DIFFERENT host is still rejected — this fix normalizes port/case, it does not
    # loosen the host comparison itself.
    assert (
        be.is_native_attachment_url("https://not-uploads.linear.app/asset-1") is False
    )


def test_linear_fetch_attachment_bytes_handles_unparseable_url():
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    with pytest.raises(BackendError, match="unparseable"):
        be.fetch_attachment_bytes("https://[bad")


def test_linear_is_native_attachment_url_rejects_invalid_port():
    # review finding (PR #90, round 2): ".hostname" alone doesn't validate the port, so
    # "https://uploads.linear.app:bad/asset" used to classify as a genuine native asset even
    # though nothing could ever fetch it. Validating ".port" too (it raises ValueError on a
    # non-numeric port, same as ".hostname" can) means a malformed authority is classified as an
    # unconfirmed external link instead — reported, not attempted as a trusted native fetch.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    assert be.is_native_attachment_url("https://uploads.linear.app:bad/asset") is False
    assert (
        be.is_native_attachment_url("https://uploads.linear.app:443/asset") is True
    )  # a VALID port still passes


def test_linear_is_native_attachment_url_rejects_non_default_port():
    # review finding (round 4): this predicate used to accept ANY numeric port on
    # uploads.linear.app (only :bad/malformed ports were rejected, above) — meaning a hostile
    # ticket's attachment url (tracker-controlled data) could aim a REAL Linear API key
    # (fetch_attachment_bytes attaches Authorization whenever this returns True) at an
    # arbitrary port on the asset host. Now delegates to the same default-port-only shared
    # predicate the CLI-layer trusted-host check already used, closing the asymmetry.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    assert be.is_native_attachment_url("https://uploads.linear.app:8443/asset") is False


def test_linear_fetch_attachment_bytes_reports_invalid_port_as_backend_error():
    # review finding (PR #90): a malformed-port attachment url must fail CLEANLY as a
    # BackendError, not crash `task read --save-attachments` with a raw traceback. With
    # `safe_urlparse` now validating `.port` eagerly (see test_safe_urlparse_returns_none_on_
    # invalid_port above), this is caught at the very first validation step — before any network
    # call is even attempted — same clean "unparseable url" refusal as any other malformed url.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    with pytest.raises(BackendError, match="unparseable"):
        be.fetch_attachment_bytes("https://uploads.linear.app:bad/asset")


def test_save_attachments_reports_malformed_port_url_as_skipped_not_crashed(tmp_path):
    # full stack (PR #90): `task read --save-attachments` against a ticket whose attachment url
    # has a malformed port must never crash the whole command — with the classification fix
    # above, is_native_attachment_url now correctly says "not native", so cli._save_attachments
    # reports it as a skipped/external link and moves on, rather than attempting (and failing) a
    # fetch. Exercised through the REAL LinearBackend, not a fake, so a future change to either
    # side of this boundary can't drift silently.
    from tasklib.backends.linear import LinearBackend
    from tasklib.cli import _save_attachments
    from tasklib.model import Attachment, Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    ticket = Ticket(
        attachments=[
            Attachment(
                id="1", title="shot.png", url="https://uploads.linear.app:bad/asset"
            )
        ]
    )
    lines = _save_attachments(be, ticket, str(tmp_path))
    assert any("skipped" in ln for ln in lines)
    assert not any(
        "✗" in ln for ln in lines
    )  # not treated as a failed fetch attempt either
    assert list(tmp_path.iterdir()) == []  # nothing was written


def test_safe_urlparse_returns_none_on_malformed_url():
    # review finding, round 3: this guard used to live as a private `_safe_urlparse` in
    # `backends.linear` (duplicated again by a new caller before being caught) — it's now the
    # one shared `backends.http.safe_urlparse` every caller imports.
    from tasklib.backends.http import safe_urlparse

    assert safe_urlparse("https://[bad") is None
    assert safe_urlparse("https://uploads.linear.app/x") is not None


def test_safe_urlparse_returns_none_on_invalid_port():
    # review finding (PR #90, round 2): urlparse() itself only raises at PARSE time (the case
    # above) — ".hostname"/".port" are LAZY properties that raise ValueError at ACCESS time, a
    # syntactically well-formed url with a non-numeric port parses fine but is still unusable.
    # Every caller must get "None" for this too, not just for the parse-time failure.
    from tasklib.backends.http import safe_urlparse

    assert safe_urlparse("https://uploads.linear.app:bad/asset") is None
    assert (
        safe_urlparse("https://uploads.linear.app:99999999/asset") is None
    )  # out of range
    assert (
        safe_urlparse("https://uploads.linear.app:443/asset") is not None
    )  # a VALID port is fine


@pytest.mark.parametrize(
    "bad_scheme_url",
    ["file:///etc/passwd", "ftp://uploads.linear.app/x", "data:text/plain,hi"],
)
def test_linear_fetch_attachment_bytes_rejects_non_http_schemes(bad_scheme_url):
    # review finding (SSRF/local-file-read): an attachment url is tracker-owned data; urlopen
    # also understands file:/ftp:/data: schemes, so a malicious/malformed attachment url must be
    # refused BEFORE any network/file call, not merely stripped of its auth header.
    from tasklib.backends import BackendError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    with pytest.raises(BackendError, match="scheme"):
        be.fetch_attachment_bytes(bad_scheme_url)


def test_linear_node_to_ticket_parses_attachments():
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Attachment

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "id": "i1",
        "identifier": "HYP-1",
        "url": "https://linear.app/x/issue/HYP-1",
        "title": "t",
        "description": "",
        "attachments": {
            "nodes": [
                {
                    "id": "a1",
                    "title": "shot.png",
                    "url": "https://uploads.linear.app/asset-1",
                    "subtitle": None,
                },
            ]
        },
    }
    ticket = be._node_to_ticket(node)
    assert ticket.attachments == [
        Attachment(
            id="a1",
            title="shot.png",
            url="https://uploads.linear.app/asset-1",
            subtitle="",
        )
    ]
    assert ticket.attachments_truncated is False


def test_linear_node_to_ticket_skips_null_entries_in_attachments_nodes():
    # review finding: a null ENTRY inside `nodes` (the same "unusual but possible GraphQL
    # response shape" class the container-level `or {}` guards defend against) must be skipped
    # cleanly, not raise AttributeError on `.get(...)`.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "id": "i1",
        "identifier": "HYP-1",
        "url": "https://linear.app/x/issue/HYP-1",
        "title": "t",
        "description": "",
        "attachments": {
            "nodes": [
                {
                    "id": "a1",
                    "title": "shot.png",
                    "url": "https://uploads.linear.app/1",
                    "subtitle": None,
                },
                None,
            ]
        },
    }
    ticket = be._node_to_ticket(node)
    assert len(ticket.attachments) == 1
    assert ticket.attachments[0].id == "a1"


def test_linear_node_to_ticket_surfaces_attachment_truncation():
    # review finding: `first:250` is a cap, not exhaustive pagination — when Linear's own
    # pageInfo says there's more, the ticket must say so too, not silently present a partial
    # list as "all of the attachments".
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    node = {
        "id": "i1",
        "identifier": "HYP-1",
        "url": "https://linear.app/x/issue/HYP-1",
        "title": "t",
        "description": "",
        "attachments": {
            "nodes": [
                {
                    "id": "a1",
                    "title": "shot.png",
                    "url": "https://uploads.linear.app/1",
                    "subtitle": None,
                }
            ],
            "pageInfo": {"hasNextPage": True},
        },
    }
    ticket = be._node_to_ticket(node)
    assert ticket.attachments_truncated is True


def test_linear_issue_fields_requests_page_info_for_attachments():
    from tasklib.backends.linear import LinearBackend

    assert "pageInfo" in LinearBackend._ISSUE_FIELDS
    assert "hasNextPage" in LinearBackend._ISSUE_FIELDS


def test_linear_issue_fields_base_excludes_attachments():
    # review finding: attachments only belong in the SINGLE-ticket field set — pulling up to 250
    # attachment sub-objects per ticket into the bulk list/search path would be a real
    # payload/latency regression almost no list consumer needs.
    from tasklib.backends.linear import LinearBackend

    assert "attachments" not in LinearBackend._ISSUE_FIELDS_BASE
    assert "attachments" in LinearBackend._ISSUE_FIELDS


def test_linear_list_and_search_queries_use_base_fields_without_attachments(
    monkeypatch,
):
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    queries = []

    def fake_gql(query, variables=None):
        queries.append(query)
        return (
            {"issues": {"nodes": []}}
            if "issues(" in query
            else {"searchIssues": {"nodes": []}}
        )

    monkeypatch.setattr(be, "_gql", fake_gql)
    be.list()
    be.search("q")

    for q in queries:
        assert "attachments(" not in q


def test_linear_create_uses_base_fields_without_attachments(monkeypatch):
    # a just-created issue has no attachments yet — nothing to fetch.
    from tasklib.backends.linear import LinearBackend
    from tasklib.model import Ticket

    be = LinearBackend(api_key="k", team_key="HYP")
    be._team_id = "team-1"
    be._states_by_type = {"unstarted": "state-1"}
    queries = []

    def fake_gql(query, variables=None):
        queries.append(query)
        return {
            "issueCreate": {
                "success": True,
                "issue": {
                    "id": "i1",
                    "identifier": "HYP-1",
                    "url": "https://x",
                    "title": "t",
                    "description": "",
                },
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    monkeypatch.setattr(be, "_label_ids", lambda names: [])
    be.create(Ticket(title="t"))

    assert "attachments(" not in queries[0]


def test_linear_get_and_update_still_use_full_fields_with_attachments(monkeypatch):
    # unlike list/search, a single-ticket fetch (get/update, via _issue_by_identifier) SHOULD
    # still pull attachments — that's the whole point of this feature's read side.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    queries = []

    def fake_gql(query, variables=None):
        queries.append(query)
        return {
            "issue": {
                "id": "i1",
                "identifier": "HYP-1",
                "url": "https://x",
                "title": "t",
                "description": "",
            }
        }

    monkeypatch.setattr(be, "_gql", fake_gql)
    be.get("HYP-1")

    assert "attachments(" in queries[0]


def test_linear_fetch_attachment_bytes_sends_authorization_header(monkeypatch):
    # This IS the "no 401" fix for the read side: the SAME Authorization header every GraphQL
    # call sends, applied to a plain GET against the attachment's asset URL.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="lin_api_secret", team_key="HYP")
    seen = {}

    def fake_request_bytes(
        url,
        *,
        method="GET",
        headers=None,
        data=None,
        timeout=60,
        same_host_redirects_only=False,
    ):
        seen["url"] = url
        seen["method"] = method
        seen["headers"] = headers
        seen["same_host_redirects_only"] = same_host_redirects_only
        return b"\x89PNG..."

    monkeypatch.setattr("tasklib.backends.linear.request_bytes", fake_request_bytes)
    result = be.fetch_attachment_bytes("https://uploads.linear.app/asset-1")

    assert result == b"\x89PNG..."
    assert seen["url"] == "https://uploads.linear.app/asset-1"
    assert seen["method"] == "GET"
    assert seen["headers"]["Authorization"] == "lin_api_secret"
    # the redirect guard MUST be on whenever the auth header is attached (review finding).
    assert seen["same_host_redirects_only"] is True


def test_linear_fetch_attachment_bytes_omits_auth_for_non_linear_host(monkeypatch):
    # review finding: never send the Linear API key to an arbitrary attachment host (e.g. a
    # `link`-mode external URL) — only uploads.linear.app gets the Authorization header.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="lin_api_secret", team_key="HYP")
    seen = {}

    def fake_request_bytes(
        url,
        *,
        method="GET",
        headers=None,
        data=None,
        timeout=60,
        same_host_redirects_only=False,
    ):
        seen["headers"] = headers
        seen["same_host_redirects_only"] = same_host_redirects_only
        return b"data"

    monkeypatch.setattr("tasklib.backends.linear.request_bytes", fake_request_bytes)
    be.fetch_attachment_bytes("https://github.com/user-attachments/assets/abc")

    assert "Authorization" not in seen["headers"]
    # review finding (round 2): the redirect guard is now UNCONDITIONAL — a non-native fetch
    # carries no credentials, but it's still a network fetch this process makes on the caller's
    # behalf against tracker-controlled data, and must not silently follow a redirect off the
    # requested host (SSRF widening — see the CLI-layer trusted_attachment_hosts feature this
    # guards).
    assert seen["same_host_redirects_only"] is True


def test_linear_fetch_attachment_bytes_omits_auth_for_plaintext_http_linear_host(
    monkeypatch,
):
    # review finding: the hostname check alone isn't enough — a plain http://uploads.linear.app
    # URL (spoofed/malformed, Linear itself never hands one out) must also NOT get the API key.
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="lin_api_secret", team_key="HYP")
    seen = {}

    def fake_request_bytes(
        url,
        *,
        method="GET",
        headers=None,
        data=None,
        timeout=60,
        same_host_redirects_only=False,
    ):
        seen["headers"] = headers
        seen["same_host_redirects_only"] = same_host_redirects_only
        return b"data"

    monkeypatch.setattr("tasklib.backends.linear.request_bytes", fake_request_bytes)
    be.fetch_attachment_bytes("http://uploads.linear.app/asset-1")

    assert "Authorization" not in seen["headers"]
    # the redirect guard is unconditional now (see the sibling non-linear-host test) — no auth
    # header here, but still no free redirect.
    assert seen["same_host_redirects_only"] is True


def test_linear_fetch_attachment_bytes_wraps_http_error(monkeypatch):
    from tasklib.backends import BackendError
    from tasklib.backends.http import HttpError
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="HYP")
    monkeypatch.setattr(
        "tasklib.backends.linear.request_bytes",
        lambda *a, **kw: (_ for _ in ()).throw(
            HttpError(401, "HTTP 401", "unauthorized")
        ),
    )
    with pytest.raises(BackendError, match="401"):
        be.fetch_attachment_bytes("https://uploads.linear.app/asset-1")


# ── backend_coordinate (task-cli#83) ────────────────────────────────────────────────


def test_backend_coordinate_github_matches_project_coordinate_shape():
    from tasklib.backends import backend_coordinate
    from tasklib.projects import Project

    be = GitHubIssuesBackend(owner="Acme", repo="Widgets", token="t")
    project = Project(
        name="x", backend="github-issues", overlay={"github": {"repo": "Acme/Widgets"}}
    )
    assert backend_coordinate(be) == project.coordinate == "github-issues:acme/widgets"


def test_backend_coordinate_linear_with_project_matches_project_coordinate_shape():
    from tasklib.backends import backend_coordinate
    from tasklib.backends.linear import LinearBackend
    from tasklib.projects import Project

    be = LinearBackend(api_key="k", team_key="hyp", project="proj-1")
    project = Project(
        name="x",
        backend="linear",
        overlay={"linear": {"team": "hyp", "project": "proj-1"}},
    )
    assert backend_coordinate(be) == project.coordinate == "linear:HYP/proj-1"


def test_backend_coordinate_linear_without_project():
    from tasklib.backends import backend_coordinate
    from tasklib.backends.linear import LinearBackend

    be = LinearBackend(api_key="k", team_key="hyp")
    assert backend_coordinate(be) == "linear:HYP"


def test_backend_coordinate_distinguishes_different_repos():
    from tasklib.backends import backend_coordinate

    a = GitHubIssuesBackend(owner="acme", repo="one", token="t")
    b = GitHubIssuesBackend(owner="acme", repo="two", token="t")
    assert backend_coordinate(a) != backend_coordinate(b)


# ── verified_ticket_coordinate (task-cli#83, 2nd-round review finding) ──────────────


class _FakeCfg:
    """Minimal `cfg` stand-in — `verified_ticket_coordinate` only reads `.data` (the raw config
    mapping `projects_from_config` parses), never anything backend-specific."""

    def __init__(self, data: dict) -> None:
        self.data = data


def test_verified_ticket_coordinate_corrects_same_team_project_misattribution():
    # task-cli#83 review finding: outside a repo, a Linear id is routed to whichever registered
    # same-team project's config was picked FIRST to authenticate/query with — but Linear
    # resolves get/update by identifier alone, so the ticket can genuinely live in a DIFFERENT
    # project on that team. The backend the id was routed through ("proj-a") is NOT where the
    # fetched ticket actually lives ("proj-b") — the coordinate must reflect the ticket's real
    # project, found by matching `backend_project_id` against the OTHER registered project.
    from tasklib.backends import verified_ticket_coordinate
    from tasklib.backends.linear import LinearBackend

    cfg = _FakeCfg(
        {
            "projects": [
                {
                    "backend": "linear",
                    "team": "hyp",
                    "project": "proj-a",
                    "name": "Proj A",
                },
                {
                    "backend": "linear",
                    "team": "hyp",
                    "project": "proj-b",
                    "name": "Proj B",
                },
            ]
        }
    )
    backend = LinearBackend(
        api_key="k", team_key="hyp", project="proj-a"
    )  # routed here first
    ticket = Ticket(
        id="HYP-1", title="t", backend_project_id="proj-b"
    )  # actually lives here

    assert verified_ticket_coordinate(cfg, backend, ticket) == "linear:HYP/proj-b"


def test_verified_ticket_coordinate_matches_when_routing_already_correct():
    from tasklib.backends import backend_coordinate, verified_ticket_coordinate
    from tasklib.backends.linear import LinearBackend

    cfg = _FakeCfg(
        {"projects": [{"backend": "linear", "team": "hyp", "project": "proj-a"}]}
    )
    backend = LinearBackend(api_key="k", team_key="hyp", project="proj-a")
    ticket = Ticket(id="HYP-1", title="t", backend_project_id="proj-a")

    assert (
        verified_ticket_coordinate(cfg, backend, ticket)
        == backend_coordinate(backend)
        == "linear:HYP/proj-a"
    )


def test_verified_ticket_coordinate_degrades_when_actual_project_not_registered():
    # The ticket's real project differs from the one routed to, but no OTHER registered project
    # claims it — nothing to correct to, so fall back to the naive (still possibly wrong, but not
    # worse than before) coordinate rather than inventing one.
    from tasklib.backends import backend_coordinate, verified_ticket_coordinate
    from tasklib.backends.linear import LinearBackend

    cfg = _FakeCfg(
        {"projects": [{"backend": "linear", "team": "hyp", "project": "proj-a"}]}
    )
    backend = LinearBackend(api_key="k", team_key="hyp", project="proj-a")
    ticket = Ticket(id="HYP-1", title="t", backend_project_id="proj-unregistered")

    assert verified_ticket_coordinate(cfg, backend, ticket) == backend_coordinate(
        backend
    )


def test_verified_ticket_coordinate_ignores_github_backend():
    from tasklib.backends import backend_coordinate, verified_ticket_coordinate

    cfg = _FakeCfg({})
    backend = GitHubIssuesBackend(owner="acme", repo="widgets", token="t")
    ticket = Ticket(id="#1", title="t", backend_project_id="proj-b")

    assert verified_ticket_coordinate(cfg, backend, ticket) == backend_coordinate(
        backend
    )


def test_verified_ticket_coordinate_no_op_when_ticket_reports_no_project():
    # A ticket fetched via a path that didn't request `project{id}` (or a non-Linear backend)
    # has an empty `backend_project_id` — nothing to correct against, degrade to naive.
    from tasklib.backends import backend_coordinate, verified_ticket_coordinate
    from tasklib.backends.linear import LinearBackend

    cfg = _FakeCfg(
        {"projects": [{"backend": "linear", "team": "hyp", "project": "proj-b"}]}
    )
    backend = LinearBackend(api_key="k", team_key="hyp", project="proj-a")
    ticket = Ticket(id="HYP-1", title="t")  # backend_project_id defaults to ""

    assert verified_ticket_coordinate(cfg, backend, ticket) == backend_coordinate(
        backend
    )


def test_verified_ticket_coordinate_matches_between_linear_create_and_get_responses():
    # task-cli#119 review finding: the read-before-edit gate's marker is recorded at
    # `cmd_create` time using the ticket `LinearBackend.create()` returns, and checked later at
    # `cmd_change` time using the ticket `.get()` returns — if those two ever compute a
    # DIFFERENT `verified_ticket_coordinate` for the SAME just-created ticket, the "create then
    # immediately `change --what`" workflow (task-cli#119's own acceptance criterion) would be
    # refused on a real Linear backend even though the fake-backend test can't see it (a
    # FakeBackend returns the identical stored object from both calls).
    #
    # `create()`'s GraphQL query uses `_ISSUE_FIELDS_BASE`, which — deliberately, per that
    # constant's own comment — omits `project{id}`, so a create-response Ticket ALWAYS reports
    # `backend_project_id == ""` (proven directly by
    # `test_linear_node_to_ticket_backend_project_id_empty_when_absent`). `get()`'s response
    # (via `_ISSUE_FIELDS`) DOES include it. This test proves the two nonetheless resolve to the
    # SAME coordinate for a just-created ticket WITH A PROJECT PINNED in config (`project:
    # "proj-a"`, the common/documented case) — `create()`'s own mutation sends `projectId:
    # self.project` in that case, so the ticket's real project can never differ from the
    # configured one, and `verified_ticket_coordinate`'s correction path (which only fires when
    # `actual_project != configured_project`) never triggers for either response shape here;
    # both degrade to the same naive `backend_coordinate`.
    #
    # Deliberately NOT covering the project-LESS case (`project: ""`): there, `create()` sends
    # no `projectId` (Linear assigns its own default project), so the two response shapes CAN
    # diverge if that team also registers another same-team project in `projects:` — see
    # `tasklib.read_tracker`'s module docstring, "Known limitation: the repo coordinate can
    # (rarely) differ..." paragraph, for the accepted, narrow, fail-closed trade-off this leaves.
    from tasklib.backends import backend_coordinate, verified_ticket_coordinate
    from tasklib.backends.linear import LinearBackend

    cfg = _FakeCfg(
        {"projects": [{"backend": "linear", "team": "hyp", "project": "proj-a"}]}
    )
    backend = LinearBackend(api_key="k", team_key="hyp", project="proj-a")

    create_response_ticket = Ticket(id="HYP-1", title="t")  # backend_project_id == ""
    get_response_ticket = Ticket(
        id="HYP-1", title="t", backend_project_id="proj-a"  # the real (== configured) project
    )

    create_coord = verified_ticket_coordinate(cfg, backend, create_response_ticket)
    get_coord = verified_ticket_coordinate(cfg, backend, get_response_ticket)
    assert create_coord == get_coord == backend_coordinate(backend)
