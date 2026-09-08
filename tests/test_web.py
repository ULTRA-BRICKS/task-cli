"""web.py handle() — HTML/JSON burn pages without binding a live port."""

from __future__ import annotations

import json
from datetime import date

from tasklib.model import State, Ticket
from tasklib.web import handle

TODAY = date(2026, 6, 10)


def _tickets():
    return [
        Ticket(
            id="#1",
            title="open one",
            created_at="2026-06-01T00:00:00Z",
            state=State.TODO,
        ),
        Ticket(
            id="#2",
            title="closed one",
            created_at="2026-06-01T00:00:00Z",
            closed_at="2026-06-05T00:00:00Z",
            state=State.DONE,
        ),
    ]


def test_handle_root_is_html_burn_with_remaining():
    status, ctype, body = handle("/", _tickets(), TODAY)
    assert status == 200
    assert "html" in ctype
    assert "Burn chart" in body
    assert "polyline" in body.lower()
    # remaining series: 2 until close day, then 1
    assert "2" in body and "1" in body


def test_handle_burn_json():
    status, ctype, body = handle("/burn.json", _tickets(), TODAY)
    assert status == 200
    assert "json" in ctype
    payload = json.loads(body)
    assert payload["remaining"][0] == 2
    assert payload["remaining"][-1] == 1
    assert "ideal" in payload


def test_handle_gantt_pre():
    tickets = [
        Ticket(id="#1", title="dated", due="2026-07-01", created_at="2026-06-01T00:00:00Z")
    ]
    status, ctype, body = handle("/gantt", tickets, TODAY)
    assert status == 200
    assert "html" in ctype
    assert "<pre>" in body
    assert "dated" in body


def test_handle_unknown_is_404():
    status, ctype, body = handle("/nope", [], TODAY)
    assert status == 404
    assert "plain" in ctype
