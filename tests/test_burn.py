"""burn.py — remaining-open tickets over calendar days. Tests assert series math, not ANSI.

The renderer takes a ``color(code, s)`` callback; an identity callback (returns ``s``) gives a
plain-text render that's deterministic to assert against. Remaining counts, the ideal line, the
JSON shape, and HTML containing those remaining values are the contract.
"""

from __future__ import annotations

from datetime import date

from tasklib.burn import fit_width, layout, render, to_html, to_json
from tasklib.model import State, Ticket

TODAY = date(2026, 6, 10)


def _plain(_code, text):
    return text


def _t(
    tid,
    title="t",
    *,
    created="",
    closed="",
    state=State.TODO,
    updated="",
):
    return Ticket(
        id=tid,
        title=title,
        state=state,
        created_at=created,
        closed_at=closed,
        updated_at=updated,
    )


def test_remaining_gone_on_close_day():
    # closed_date <= D means NOT remaining: closed on D is gone on D (recommended rule).
    tickets = [
        _t("#1", created="2026-06-01T00:00:00Z", closed="2026-06-05T15:00:00Z", state=State.DONE)
    ]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 10))
    days = {d.isoformat(): n for d, n in zip(chart.days, chart.remaining)}
    assert days["2026-06-04"] == 1
    assert days["2026-06-05"] == 0
    assert days["2026-06-06"] == 0


def test_created_mid_window_does_not_count_before_created_day():
    tickets = [_t("#1", created="2026-06-05T08:00:00Z")]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 10))
    days = {d.isoformat(): n for d, n in zip(chart.days, chart.remaining)}
    assert days["2026-06-04"] == 0
    assert days["2026-06-05"] == 1
    assert days["2026-06-10"] == 1


def test_cancelled_counts_as_closed():
    tickets = [
        _t(
            "#1",
            created="2026-06-01T00:00:00Z",
            closed="2026-06-03T12:00:00Z",
            state=State.CANCELLED,
        )
    ]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 10))
    days = {d.isoformat(): n for d, n in zip(chart.days, chart.remaining)}
    assert days["2026-06-02"] == 1
    assert days["2026-06-03"] == 0


def test_done_without_closed_at_uses_updated_at():
    tickets = [
        _t(
            "#1",
            created="2026-06-01T00:00:00Z",
            state=State.DONE,
            updated="2026-06-04T09:00:00Z",
        )
    ]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 10))
    days = {d.isoformat(): n for d, n in zip(chart.days, chart.remaining)}
    assert days["2026-06-03"] == 1
    assert days["2026-06-04"] == 0


def test_garbage_timestamps_never_crash():
    tickets = [_t("#1", created="not-a-date", closed="", state=State.TODO)]
    chart = layout(tickets, TODAY)
    assert chart.window.span_days >= 1
    assert len(chart.remaining) == len(chart.days)


def test_layout_empty_is_safe():
    chart = layout([], TODAY)
    assert chart.window.span_days >= 1
    assert all(n == 0 for n in chart.remaining)
    out = render(chart, color=_plain)
    assert "no tickets" in out.lower()


def test_layout_zero_width_does_not_crash():
    tickets = [_t("#1", created="2026-06-01T00:00:00Z")]
    chart = layout(tickets, TODAY, width=0)
    assert chart.width >= 1
    render(chart, color=_plain)  # no exception


def test_ideal_is_linear_start_to_zero():
    tickets = [
        _t("#1", created="2026-06-01T00:00:00Z"),
        _t("#2", created="2026-06-01T00:00:00Z"),
        _t("#3", created="2026-06-01T00:00:00Z"),
        _t("#4", created="2026-06-01T00:00:00Z"),
    ]
    chart = layout(tickets, date(2026, 6, 5), since=date(2026, 6, 1), until=date(2026, 6, 5))
    assert chart.remaining[0] == 4
    assert chart.ideal[0] == 4
    assert chart.ideal[-1] == 0
    # 5 days (indices 0..4): midpoint is 2
    assert chart.ideal[2] == 2


def test_ideal_all_zeros_when_nothing_remaining_at_start():
    tickets = [
        _t(
            "#1",
            created="2026-06-01T00:00:00Z",
            closed="2026-06-01T00:00:00Z",
            state=State.DONE,
        )
    ]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 3))
    assert chart.remaining[0] == 0
    assert all(v == 0 for v in chart.ideal)


def test_to_json_shape_is_deterministic():
    tickets = [
        _t("#1", created="2026-06-01T00:00:00Z"),
        _t("#2", created="2026-06-01T00:00:00Z", closed="2026-06-05T00:00:00Z", state=State.DONE),
    ]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 10), width=20)
    payload = to_json(chart)
    assert set(payload) >= {
        "window",
        "days",
        "remaining",
        "ideal",
        "total",
        "remaining_today",
        "open",
        "closed",
    }
    assert payload["window"]["start"] == "2026-06-01"
    assert payload["window"]["end"] == "2026-06-10"
    assert payload["window"]["span_days"] == 10
    assert payload["window"]["width"] == 20
    assert payload["days"][0] == "2026-06-01"
    assert payload["days"][-1] == "2026-06-10"
    assert payload["total"] == 2
    assert payload["remaining"][0] == 2
    assert payload["remaining"][-1] == 1
    assert payload["remaining_today"] == 1
    assert payload["open"] == 1
    assert payload["closed"] == 1
    assert len(payload["ideal"]) == len(payload["remaining"]) == len(payload["days"])


def test_to_html_contains_remaining_polyline():
    tickets = [_t("#1", created="2026-06-01T00:00:00Z"), _t("#2", created="2026-06-01T00:00:00Z")]
    chart = layout(tickets, TODAY, since=date(2026, 6, 1), until=date(2026, 6, 3))
    html = to_html(chart)
    assert "Burn chart" in html
    assert "<svg" in html.lower()
    assert "polyline" in html.lower()
    joined = ",".join(str(n) for n in chart.remaining)
    assert joined in html or all(str(n) in html for n in chart.remaining)


def test_to_html_escapes_ticket_titles():
    tickets = [_t("#1", title="<script>x</script>", created="2026-06-01T00:00:00Z")]
    html = to_html(layout(tickets, TODAY, since=date(2026, 6, 1), until=TODAY))
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html or "&#" in html


def test_fit_width_clamps():
    assert fit_width(200) >= 20
    assert fit_width(10) >= 1
