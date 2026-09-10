"""web.py handle() + start/stop/status — no live port required for pages."""

from __future__ import annotations

import json
import os
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
            due="2026-07-01",
        ),
        Ticket(
            id="#2",
            title="closed one",
            created_at="2026-06-01T00:00:00Z",
            closed_at="2026-06-05T00:00:00Z",
            state=State.DONE,
        ),
    ]


def test_handle_root_shows_both_charts():
    status, ctype, body = handle("/", _tickets(), TODAY)
    assert status == 200
    assert "html" in ctype
    assert "Burn chart" in body
    assert "polyline" in body.lower()
    assert "Gantt" in body
    assert "gantt-bar" in body
    assert "open one" in body
    assert 'href="/burn"' in body
    assert 'href="/gantt"' in body
    # remaining series: 2 until close day, then 1
    assert "2" in body and "1" in body


def test_handle_burn_page_is_still_the_svg_chart():
    status, ctype, body = handle("/burn", _tickets(), TODAY)
    assert status == 200
    assert "Burn chart" in body
    assert "polyline" in body.lower()
    assert 'href="/gantt"' in body


def test_handle_burn_json():
    status, ctype, body = handle("/burn.json", _tickets(), TODAY)
    assert status == 200
    assert "json" in ctype
    payload = json.loads(body)
    assert payload["remaining"][0] == 2
    assert payload["remaining"][-1] == 1
    assert "ideal" in payload


def test_handle_gantt_is_svg_chart_not_pre():
    tickets = [
        Ticket(id="#1", title="dated", due="2026-07-01", created_at="2026-06-01T00:00:00Z")
    ]
    status, ctype, body = handle("/gantt", tickets, TODAY)
    assert status == 200
    assert "html" in ctype
    assert "<svg" in body.lower()
    assert "gantt-bar" in body
    assert "dated" in body
    assert "<pre>" not in body
    assert 'href="/burn"' in body


def test_handle_gantt_json():
    tickets = [
        Ticket(id="#1", title="dated", due="2026-07-01", created_at="2026-06-01T00:00:00Z"),
        Ticket(id="#2", title="undated", created_at="2026-06-01T00:00:00Z"),
    ]
    status, ctype, body = handle("/gantt.json", tickets, TODAY)
    assert status == 200
    assert "json" in ctype
    payload = json.loads(body)
    assert payload["rows"][0]["id"] == "#1"
    assert payload["undated"][0]["id"] == "#2"


def test_handle_unknown_is_404():
    status, ctype, body = handle("/nope", [], TODAY)
    assert status == 404
    assert "plain" in ctype


def test_start_is_idempotent_when_already_running(tmp_path, monkeypatch):
    from tasklib import daemon as _d
    from tasklib import web as _web

    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    pid_path = _web.paths_for("owner/repo", env=env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    recorded = "-m tasklib web run"
    pid_path.write_text(f"{os.getpid()}\n{recorded}\n", encoding="utf-8")
    monkeypatch.setattr(
        _d, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "web", "run"]
    )
    spawned: list = []

    class _Fake:
        pid = 999

    monkeypatch.setattr(
        "subprocess.Popen", lambda *a, **k: spawned.append(a[0]) or _Fake()
    )
    outcome, pid = _web.start("owner/repo", env=env)
    assert outcome == "already-running"
    assert pid == os.getpid()
    assert spawned == []


def test_start_spawns_web_run_argv(tmp_path, monkeypatch):
    from tasklib import web as _web

    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    seen: dict = {}

    class _Fake:
        pid = 4242

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _Fake()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    outcome, pid = _web.start(
        "owner/repo",
        port=9000,
        cwd=str(tmp_path),
        child_flags=["--backend", "linear"],
        env=env,
    )
    assert outcome == "started"
    assert pid == 4242
    argv = seen["argv"]
    assert argv[1:6] == ["-P", "-m", "tasklib", "web", "run"]
    assert "--port" in argv and "9000" in argv
    assert argv[-2:] == ["--backend", "linear"]


def test_stop_without_pidfile_is_not_running(tmp_path):
    from tasklib import web as _web

    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    outcome, pid = _web.stop("owner/repo", env=env)
    assert outcome == "not-running"
    assert pid is None


def test_pid_status_stopped_without_pidfile(tmp_path):
    from tasklib import web as _web

    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    status, pid = _web.pid_status_for("owner/repo", env=env)
    assert status == "stopped"
    assert pid is None

def test_handle_preserves_all_query_on_nav():
    status, _ctype, body = handle("/?all=1", _tickets(), TODAY)
    assert status == 200
    assert 'href="/burn?all=1"' in body
    assert 'href="/gantt?all=1"' in body

