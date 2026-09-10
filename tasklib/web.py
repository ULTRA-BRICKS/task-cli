"""Local HTTP server for the burn (and gantt) charts — stdlib ``http.server`` only.

WHAT this is: a tiny foreground/detached web UI for ``task burn`` and ``task gantt``.
It serves the same tickets those commands would chart as HTML/JSON. ``/`` shows both
SVG charts; ``/burn`` and ``/gantt`` are the individual pages.

HOW it is reached at runtime: ``task web start|stop|status|run``. ``run`` is the
foreground server; ``start`` detaches it. The CLI never imports this module until the
web command actually runs (lazy), so ``task --help`` stays stdlib-cheap.

INVARIANTS this assumes:
- No live port is required to render a page: :func:`handle` is a pure
  (path, tickets, today) → (status, content_type, body) function. Tests call it
  directly.
- The pid-file identity string includes ``web`` so ``daemon.pid_status`` cannot
  confuse this process with the reminder daemon. State lives under
  ``~/.local/state/task-cli/web/``, keyed by repo coordinate.
- Binding ``0.0.0.0`` is out of scope; we listen on 127.0.0.1 only.

PAST BUGS guarded here: none yet — this module is new.
"""

from __future__ import annotations

import html
import json
import os
import signal
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import burn as _burn
from . import daemon as _daemon
from . import gantt as _gantt

DEFAULT_PORT = 8765
_IDENTITY_MARK = "web"


def paths_for(coordinate: str, *, env: dict[str, str] | None = None) -> Path:
    """Pid-file path for this coordinate's web server (``…/task-cli/web/<key>.pid``)."""
    env = env or os.environ
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".local", "state"
    )
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in coordinate) or "default"
    directory = Path(base) / "task-cli" / "web"
    return directory / f"{safe}.pid"


def _identity() -> str:
    """Pid-file identity: live argv signature, forced to contain ``web``."""
    tokens = _daemon.process_cmdline(os.getpid()) or list(sys.argv)
    sig = _daemon.argv_signature(tokens)
    if _IDENTITY_MARK not in sig.split():
        sig = f"{_IDENTITY_MARK} {sig}".strip()
    return sig


def _nav(query: str = "") -> str:
    """Chart-page nav. ``query`` is the raw query string (no ``?``); preserved so ``?all=1`` survives."""
    q = f"?{query}" if query else ""
    return (
        '<nav class="charts">'
        f'<a href="/{q}">Both</a> · '
        f'<a href="/burn{q}">Burn</a> · '
        f'<a href="/gantt{q}">Gantt</a>'
        "</nav>\n"
    )

_PAGE_STYLE = """
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 1.5rem; color: #111; background: #fff; }
h1 { font-size: 1.25rem; }
h2 { font-size: 1rem; color: #555; }
nav.charts { margin-bottom: 1rem; }
nav.charts a { color: #111; }
svg { max-width: 100%; height: auto; }
.remaining { fill: none; stroke: #111; stroke-width: 2; }
.ideal { fill: none; stroke: #888; stroke-width: 1.5; stroke-dasharray: 4 3; }
.axis { stroke: #ccc; }
.today { stroke: #888; stroke-dasharray: 3 3; }
.gantt-label, .gantt-due { font-size: 12px; fill: #111; }
.gantt-empty { font-size: 12px; fill: #666; }
ul { padding-left: 1.2rem; }
section { margin-bottom: 2.5rem; }
"""


def _page(title: str, body: str, *, query: str = "") -> str:
    """Wrap inner chart markup in the shared document chrome (nav + styles)."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_PAGE_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{_nav(query)}"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def handle(path: str, tickets: list, today: date) -> tuple[int, str, str]:
    """Route ``path`` to HTML/JSON without binding a port.

    Returns ``(status, content_type, body)``. Unknown paths are 404.
    """
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    query = parsed.query
    if route == "/":
        burn_inner = _burn.chart_markup(_burn.layout(tickets, today))
        gantt_inner = _gantt.chart_markup(_gantt.layout(tickets, today), today)
        body = f"<section>{burn_inner}</section>\n<section>{gantt_inner}</section>"
        return 200, "text/html; charset=utf-8", _page("Task charts", body, query=query)
    if route == "/burn":
        inner = _burn.chart_markup(_burn.layout(tickets, today))
        return 200, "text/html; charset=utf-8", _page("Burn chart", inner, query=query)
    if route == "/gantt":
        inner = _gantt.chart_markup(_gantt.layout(tickets, today), today)
        return 200, "text/html; charset=utf-8", _page("Gantt", inner, query=query)
    if route == "/burn.json":
        chart = _burn.layout(tickets, today)
        body = json.dumps(_burn.to_json(chart), ensure_ascii=False, indent=2)
        return 200, "application/json; charset=utf-8", body
    if route == "/gantt.json":
        chart = _gantt.layout(tickets, today)
        body = json.dumps(_gantt.to_json(chart, today), ensure_ascii=False, indent=2)
        return 200, "application/json; charset=utf-8", body
    return 404, "text/plain; charset=utf-8", "not found\n"


def run(
    port: int,
    load_tickets: Callable[..., list],
    *,
    coordinate: str = "",
    env: dict[str, str] | None = None,
    today: date | None = None,
) -> int:
    """Foreground HTTP server on 127.0.0.1. Writes a pid-file when ``coordinate`` is set."""
    env = env or os.environ
    pid_path = paths_for(coordinate, env=env) if coordinate else None
    if pid_path is not None:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{os.getpid()}\n{_identity()}\n", encoding="utf-8")

    today_fn = (lambda: today) if today is not None else date.today

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            want_all = qs.get("all", ["0"])[0] in ("1", "true", "yes")
            tickets = load_tickets(include_all=want_all)
            status, ctype, body = handle(self.path, tickets, today_fn())
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def _stop(_signum=None, _frame=None) -> None:
        # serve_forever() blocks this thread; stdlib shutdown() must run on another.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if pid_path is not None:
            try:
                if pid_path.read_text(encoding="utf-8").splitlines()[0].strip() == str(os.getpid()):
                    pid_path.unlink()
            except (OSError, IndexError):
                pass
    return 0


def start(
    coordinate: str,
    *,
    port: int = DEFAULT_PORT,
    cwd: str | None = None,
    child_flags: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, int | None]:
    """Idempotently spawn a detached ``task web run``. Returns ``(outcome, pid)``."""
    env = env or os.environ
    pid_path = paths_for(coordinate, env=env)
    status, pid = _daemon.pid_status(pid_path)
    if status == "running":
        return "already-running", pid
    if status in ("stale", "not-ours"):
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass

    repo_root = Path(__file__).resolve().parent.parent
    child_env = dict(env)
    existing = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = str(repo_root) + ((os.pathsep + existing) if existing else "")
    log_path = pid_path.with_suffix(".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cwd = cwd or os.getcwd()
    argv = [
        sys.executable,
        "-P",
        "-m",
        "tasklib",
        "web",
        "run",
        "--port",
        str(port),
        "-C",
        cwd,
        *(child_flags or []),
    ]
    logf = open(log_path, "a", encoding="utf-8")
    try:
        import subprocess

        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
            cwd=cwd,
            env=child_env,
        )
    finally:
        logf.close()
    return "started", proc.pid


def stop(coordinate: str, *, env: dict[str, str] | None = None, timeout_s: float = 5.0) -> tuple[str, int | None]:
    """SIGTERM the web process for ``coordinate``, then clear the pid-file."""
    import time

    env = env or os.environ
    pid_path = paths_for(coordinate, env=env)
    status, pid = _daemon.pid_status(pid_path)
    if pid is None or status in ("stopped", "stale"):
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        return "not-running", pid
    if status == "not-ours":
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        return "not-ours", pid
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        return "stopped", pid
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    try:
        os.kill(pid, 0)
        gone = False
    except OSError:
        gone = True
    try:
        if pid_path.read_text(encoding="utf-8").splitlines()[0].strip() == str(pid):
            pid_path.unlink()
    except (OSError, IndexError):
        pass
    return ("stopped", pid) if gone else ("timeout", pid)


def url_for(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def pid_status_for(coordinate: str, *, env: dict[str, str] | None = None) -> tuple[str, int | None]:
    """``daemon.pid_status`` for this coordinate's web pid-file."""
    return _daemon.pid_status(paths_for(coordinate, env=env))
