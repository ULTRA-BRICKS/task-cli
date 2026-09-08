"""Burn chart layout — remaining OPEN tickets over calendar days (no I/O, no color).

WHAT this is: the pure, testable core of ``task burn``. It turns ticket created/closed
timestamps into a remaining-over-time series plus an ideal straight line to zero, then
renders that as ASCII, JSON, or self-contained HTML+SVG.

HOW it is reached at runtime: ``tasklib.cli``'s ``burn`` subcommand (and ``web``'s HTML
handler) call :func:`layout` then :func:`render` / :func:`to_json` / :func:`to_html`.
Nothing here talks to a backend, the filesystem, or the network.

INVARIANTS this assumes:
- A ticket is remaining on day D iff created_date <= D AND (closed_date is None OR
  closed_date > D). Closed on D is gone on D.
- Garbage/empty timestamps parse to None and never raise.
- A missing created_date is treated as the window start (still counted, never dropped).
- DONE or CANCELLED with no closed_at uses updated_at's date if parseable, else the
  window start, so they leave remaining rather than inflating it forever.
- The window is inclusive; a degenerate single-day range still has span_days >= 1.

PAST BUGS guarded here: none yet — this module is new.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Ticket

DEFAULT_WIDTH = 40
_MIN_SPAN_DAYS = 1
_MIN_WIDTH = 1
_CHART_HEIGHT = 10


@dataclass(frozen=True)
class DateWindow:
    """The inclusive ``[start, end]`` date range the axis spans."""

    start: date
    end: date

    @property
    def span_days(self) -> int:
        return max(_MIN_SPAN_DAYS, (self.end - self.start).days + 1)

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.span_days)]


@dataclass
class BurnChart:
    """Laid-out remaining-over-time series plus the ideal line to zero."""

    window: DateWindow
    width: int
    days: list[date]
    remaining: list[int]
    ideal: list[float]
    opened_on: list[int] = field(default_factory=list)
    closed_on: list[int] = field(default_factory=list)
    total: int = 0
    remaining_today: int = 0
    open: int = 0
    closed: int = 0
    tickets: list = field(default_factory=list)
    today: date | None = None


def parse_iso_date(value: object) -> date | None:
    """UTC calendar date of an ISO8601 timestamp. Garbage/empty → None, never raises."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Take the date prefix; GitHub/Linear both emit YYYY-MM-DDTHH:MM:SS[.mmm][Z]
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _closed_date_for(ticket: "Ticket", window_start: date) -> date | None:
    """The day the ticket leaves remaining, or None if it is still open."""
    from .model import State

    closed = parse_iso_date(ticket.closed_at)
    if closed is not None:
        return closed
    if ticket.state in (State.DONE, State.CANCELLED):
        return parse_iso_date(ticket.updated_at) or window_start
    return None


def _created_date_for(ticket: "Ticket", window_start: date) -> date:
    return parse_iso_date(ticket.created_at) or window_start


def compute_window(
    tickets: list["Ticket"],
    today: date,
    *,
    since: date | None = None,
    until: date | None = None,
) -> DateWindow:
    """Inclusive [start, end]. Default start = min(created dates, today), end = max(today, last closed)."""
    created = [parse_iso_date(t.created_at) for t in tickets]
    created_ok = [d for d in created if d is not None]
    closed = [parse_iso_date(t.closed_at) for t in tickets]
    closed_ok = [d for d in closed if d is not None]

    start = since if since is not None else (min(created_ok + [today]) if created_ok else today)
    end = until if until is not None else (max([today] + closed_ok))
    if start > end:
        start, end = end, start
    return DateWindow(start=start, end=end)


def _ideal_line(remaining0: int, n: int) -> list[float]:
    """Linear interpolation from remaining[0] down to 0 at the last day."""
    if n <= 0:
        return []
    if remaining0 == 0:
        return [0.0] * n
    if n == 1:
        return [0.0]
    return [remaining0 * (1 - i / (n - 1)) for i in range(n)]


def layout(
    tickets: list["Ticket"],
    today: date,
    *,
    since: date | None = None,
    until: date | None = None,
    width: int = DEFAULT_WIDTH,
) -> BurnChart:
    """Lay remaining-open counts onto each calendar day of the window."""
    window = compute_window(tickets, today, since=since, until=until)
    days = window.days()
    n = len(days)
    remaining = [0] * n
    opened_on = [0] * n
    closed_on = [0] * n
    day_index = {d: i for i, d in enumerate(days)}

    for ticket in tickets:
        created = _created_date_for(ticket, window.start)
        closed = _closed_date_for(ticket, window.start)
        if created in day_index:
            opened_on[day_index[created]] += 1
        if closed is not None and closed in day_index:
            closed_on[day_index[closed]] += 1
        for i, day in enumerate(days):
            if created <= day and (closed is None or closed > day):
                remaining[i] += 1

    today_idx = day_index.get(today, n - 1)
    remaining_today = remaining[today_idx] if remaining else 0
    open_count = remaining_today
    total = len(tickets)
    chart = BurnChart(
        window=window,
        width=max(_MIN_WIDTH, width),
        days=days,
        remaining=remaining,
        ideal=_ideal_line(remaining[0] if remaining else 0, n),
        opened_on=opened_on,
        closed_on=closed_on,
        total=total,
        remaining_today=remaining_today,
        open=open_count,
        closed=total - open_count,
        tickets=list(tickets),
        today=today,
    )
    return chart


def fit_width(terminal_columns: int) -> int:
    """Bar-area width that fits ``terminal_columns``, clamped to a sensible band."""
    avail = max(1, terminal_columns - 8)
    if avail < 20:
        return DEFAULT_WIDTH
    return max(20, min(DEFAULT_WIDTH, avail))


def render(chart: BurnChart, color) -> str:
    """ASCII chart: Y = remaining, X = days. ``#`` actual, ``.`` ideal."""
    if chart.total == 0:
        return color("2", "(no tickets to chart)")

    remaining = chart.remaining
    ideal = chart.ideal
    width = chart.width
    n = len(remaining)
    # Sample series onto ``width`` columns when the window is longer than the chart.
    cols = width
    xs = [int(i * (n - 1) / (cols - 1)) if cols > 1 else 0 for i in range(cols)]
    actual_s = [remaining[j] for j in xs]
    ideal_s = [ideal[j] for j in xs]
    peak = max([*actual_s, *[int(v) for v in ideal_s], 1])
    height = min(_CHART_HEIGHT, max(peak, 1))

    lines: list[str] = []
    start = chart.window.start.isoformat()
    end = chart.window.end.isoformat()
    lines.append(
        color("1", f"Burn chart  {start} → {end}   remaining today: {chart.remaining_today}")
    )
    for row in range(height, -1, -1):
        # row value is remaining count at this height
        threshold = peak * row / height if height else 0
        gutter = f"{int(round(threshold)):>3} │"
        cells: list[str] = []
        for a, ide in zip(actual_s, ideal_s):
            glyph = " "
            if a >= threshold and row > 0:
                glyph = "#"
            elif ide >= threshold and row > 0:
                glyph = "."
            elif row == 0:
                glyph = "─"
            cells.append(glyph)
        if row == 0:
            lines.append("  0 └" + "".join(cells))
        else:
            lines.append(gutter + "".join(cells))
    lines.append("    " + start + " " * max(1, cols - len(start) - len(end)) + end)
    lines.append(color("2", "    # remaining   . ideal"))
    return "\n".join(lines)


def to_json(chart: BurnChart) -> dict:
    """Deterministic remaining-over-time payload for ``task burn --json``."""
    return {
        "window": {
            "start": chart.window.start.isoformat(),
            "end": chart.window.end.isoformat(),
            "span_days": chart.window.span_days,
            "width": chart.width,
        },
        "days": [d.isoformat() for d in chart.days],
        "remaining": list(chart.remaining),
        "ideal": list(chart.ideal),
        "opened_on": list(chart.opened_on),
        "closed_on": list(chart.closed_on),
        "total": chart.total,
        "remaining_today": chart.remaining_today,
        "open": chart.open,
        "closed": chart.closed,
    }


def to_html(chart: BurnChart) -> str:
    """Self-contained HTML+SVG burn chart (no CDN, no JS required)."""
    title = "Burn chart"
    remaining = chart.remaining
    ideal = chart.ideal
    n = max(len(remaining), 1)
    w, h, pad = 720, 280, 40
    peak = max([*remaining, 1])
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad

    def pt(i: int, value: float) -> str:
        x = pad + (i / max(n - 1, 1)) * inner_w
        y = pad + inner_h - (value / peak) * inner_h
        return f"{x:.1f},{y:.1f}"

    rem_pts = " ".join(pt(i, v) for i, v in enumerate(remaining)) if remaining else f"{pad},{pad + inner_h}"
    ide_pts = " ".join(pt(i, v) for i, v in enumerate(ideal)) if ideal else rem_pts
    rem_csv = ",".join(str(v) for v in remaining)

    items = []
    for t in chart.tickets:
        tid = html.escape(str(t.id or ""))
        ttl = html.escape(str(t.title or ""))
        items.append(f"<li>{tid} {ttl}</li>")
    listing = "\n".join(items)

    start = chart.window.start.isoformat()
    end = chart.window.end.isoformat()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 1.5rem; color: #111; background: #fff; }}
h1 {{ font-size: 1.25rem; }}
svg {{ max-width: 100%; height: auto; }}
.remaining {{ fill: none; stroke: #111; stroke-width: 2; }}
.ideal {{ fill: none; stroke: #888; stroke-width: 1.5; stroke-dasharray: 4 3; }}
.axis {{ stroke: #ccc; }}
ul {{ padding-left: 1.2rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>{html.escape(start)} → {html.escape(end)} · remaining today: {chart.remaining_today} / {chart.total}</p>
<svg viewBox="0 0 {w} {h}" role="img" aria-label="{title}" data-remaining="{html.escape(rem_csv)}">
  <line class="axis" x1="{pad}" y1="{pad}" x2="{pad}" y2="{pad + inner_h}" />
  <line class="axis" x1="{pad}" y1="{pad + inner_h}" x2="{pad + inner_w}" y2="{pad + inner_h}" />
  <polyline class="ideal" points="{ide_pts}" />
  <polyline class="remaining" points="{rem_pts}" />
</svg>
<ul>
{listing}
</ul>
</body>
</html>
"""
