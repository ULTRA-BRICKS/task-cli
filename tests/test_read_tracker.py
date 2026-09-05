"""read_tracker.py — the read-before-edit gate's marker store (task-cli#119)."""

from __future__ import annotations

import json
import time

from tasklib.read_tracker import DEFAULT_TTL_S, _log_path, is_fresh, record, state_dir


def test_is_fresh_false_when_never_read(isolated_state):
    assert is_fresh("sess1", "#10") is False


def test_is_fresh_true_right_after_record(isolated_state):
    record("sess1", "#10")
    assert is_fresh("sess1", "#10") is True


def test_is_fresh_scoped_to_ticket_id(isolated_state):
    record("sess1", "#10")
    assert is_fresh("sess1", "#11") is False


def test_is_fresh_scoped_to_session_id(isolated_state):
    # a DIFFERENT session's read must not satisfy this session's gate — see the module
    # docstring on why this is per-session, not a machine-wide "did anyone look" marker.
    record("other-session", "#10")
    assert is_fresh("sess1", "#10") is False


def test_is_fresh_scoped_to_repo_coordinate(isolated_state):
    # two different repos/projects can both have a "#10" (task-cli#83-class collision) — a read
    # of repo A's #10 must not satisfy the gate for repo B's unrelated #10 under the same
    # session id (e.g. the same git branch name reused across two checkouts).
    record("sess1", "#10", repo="github-issues:acme/a")
    assert is_fresh("sess1", "#10", repo="github-issues:acme/b") is False
    assert is_fresh("sess1", "#10", repo="github-issues:acme/a") is True


def test_is_fresh_respects_ttl(isolated_state, monkeypatch):
    # write a marker with a timestamp already outside the TTL window, then confirm it reads as
    # stale — avoids a real sleep() in the test suite.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = 1_000_000.0
    path.write_text(
        json.dumps({"id": "#10", "repo": "", "ts": stale_ts}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: stale_ts + DEFAULT_TTL_S + 1)
    assert is_fresh("sess1", "#10") is False


def test_is_fresh_env_overridable_ttl(isolated_state, monkeypatch):
    env = {"TASK_READ_TTL_S": "5"}
    path = state_dir(env) / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": "#10", "repo": "", "ts": 1_000_000.0}) + "\n", encoding="utf-8")
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 1_000_000.0 + 4)
    assert is_fresh("sess1", "#10", env=env) is True
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 1_000_000.0 + 6)
    assert is_fresh("sess1", "#10", env=env) is False


def test_latest_entry_wins_on_repeated_reads(isolated_state, monkeypatch):
    # an append-only log for the same (session, repo, id) may carry more than one line — the
    # LATEST timestamp must govern freshness, not the first.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": 1_000_000.0}) + "\n")
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": 2_000_000.0}) + "\n")
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 2_000_000.0 + 1)
    assert is_fresh("sess1", "#10") is True


def test_is_fresh_uses_max_timestamp_not_last_line(isolated_state, monkeypatch):
    # review finding, task-cli#119: an out-of-order append (e.g. a backward clock step/NTP
    # correction) can put a STALER entry AFTER a genuinely fresh one — is_fresh must take the
    # MAX valid timestamp seen, not just whichever line happens to be last in the file, or a
    # legitimately fresh read gets masked and the edit is wrongly refused.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": 2_000_000.0}) + "\n")  # fresh
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": 1_000_000.0}) + "\n")  # stale, LAST
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 2_000_000.0 + 1)
    assert is_fresh("sess1", "#10") is True  # the fresh (first) entry must still count


def test_is_fresh_rejects_a_boolean_timestamp(isolated_state, monkeypatch):
    # review finding, task-cli#119: `bool` is an `int` subclass in Python, so a corrupt/
    # adversarial line carrying `"ts": true` would pass `isinstance(ts, (int, float))` and
    # compare as `1` (a 1970 timestamp) — silently downgrading freshness if it landed after a
    # genuine entry. Must be treated as malformed (skipped), not a valid (very stale) timestamp.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": 2_000_000.0}) + "\n")  # fresh
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": True}) + "\n")  # adversarial
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 2_000_000.0 + 1)
    assert is_fresh("sess1", "#10") is True  # the genuine fresh entry must still count


def test_is_fresh_tolerates_malformed_lines(isolated_state):
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write("42\n")  # valid JSON, not a dict
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": None}) + "\n")  # bad ts type
    record("sess1", "#10")  # a genuine, valid entry appended after the junk
    assert is_fresh("sess1", "#10") is True


def test_is_fresh_none_when_store_unreadable(isolated_state, monkeypatch, tmp_path):
    # simulate an unreadable store (e.g. a permissions problem): Path.read_text raises OSError.
    # is_fresh must return None (fail-closed, distinct message), never crash or silently pass.
    record("sess1", "#10")

    from pathlib import Path

    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if self.name == "sess1.jsonl":
            raise OSError("permission denied")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert is_fresh("sess1", "#10") is None


def test_record_is_silent_on_write_failure(isolated_state, monkeypatch):
    # record() must never raise, even if the marker directory can't be created.
    from pathlib import Path

    def _boom(self, *a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "mkdir", _boom)
    record("sess1", "#10")  # must not raise
    assert is_fresh("sess1", "#10") is False  # nothing was recorded → correctly "not read"


def test_state_dir_under_xdg_state_home(isolated_state):
    p = state_dir()
    assert str(p).endswith("task-cli/reads") or str(p).endswith("task-cli\\reads")


# ── session-id filename safety (review finding, task-cli#119) ───────────────────────


def test_session_id_path_traversal_stays_inside_state_dir(isolated_state):
    # a `$TASK_SESSION`-shaped session id trying to escape state_dir() via ../.. must not be
    # able to — the marker file always lands DIRECTLY inside state_dir(), never outside it, and
    # never in a subdirectory (no "/" survives sanitization to create one).
    evil = "../../../../../../tmp/evil-marker"
    record(evil, "#10")
    path = _log_path(evil)
    assert path.parent == state_dir()  # contained: a direct child of state_dir(), not escaped
    assert path.is_file()
    assert is_fresh(evil, "#10") is True  # the (sanitized) marker round-trips for the SAME id


def test_session_id_dot_dot_alone_does_not_become_parent_dir(isolated_state):
    # a session id of exactly ".." must not sanitize to a bare "..jsonl"/"...jsonl" that could
    # still be interpreted as a parent-directory-shaped component by some tooling — it falls
    # back to `tasklib.session._slug`'s own empty-input fallback ("anon", reused as-is — see
    # `_safe_session_id`'s docstring on why this module doesn't maintain a second sanitizer).
    path = _log_path("..")
    assert path.parent == state_dir()
    assert path.name == "anon.jsonl"


def test_session_id_sanitization_can_collide_but_never_escapes(isolated_state):
    # Reusing `tasklib.session._slug` (round-7 review finding) means a raw, unslugged id like
    # "../sess1" CAN sanitize to the same bucket as a legitimate "sess1" (`_slug` strips the
    # leading "../" run entirely, leaving "sess1") — a namespace collision, not a directory
    # escape. This mirrors an ALREADY-ACCEPTED property of `tasklib.session` itself (two raw
    # inputs slugging to the same id already share ITS sidecar bucket) and only matters for a
    # caller that hands read_tracker a raw, never-detected id directly — every real command
    # path routes through `session.detect()` first, which already normalizes `$TASK_SESSION`
    # before read_tracker ever sees it, so this is a theoretical/direct-call concern, not a
    # production one. What actually matters — containment — still holds unconditionally.
    record("sess1", "#10")
    assert is_fresh("../sess1", "#10") is True  # collides with "sess1" (a namespace weakness)
    assert _log_path("../sess1").parent == state_dir()  # but never escapes state_dir() itself


def test_session_id_over_filesystem_length_limit_does_not_break_freshness(isolated_state):
    # review finding, task-cli#119: the EARLIER version of `_safe_session_id` had no length
    # cap (unlike `_slug`, which caps at 40 chars + a hash suffix) — an id over the filesystem's
    # filename limit made `record()` silently fail (OSError, ENAMETOOLONG) and the SUBSEQUENT
    # `is_fresh()` misdiagnose the result as `None` ("store unreadable") instead of correctly
    # recording and finding the read. Reusing `_slug` closes this: the id is capped long before
    # it ever reaches a filesystem call.
    huge_id = "x" * 5000
    record(huge_id, "#10")
    assert is_fresh(huge_id, "#10") is True


# ── unreadable store vs. genuinely-never-read (review finding, task-cli#119) ────────


def test_is_fresh_none_when_parent_directory_is_inaccessible(isolated_state, monkeypatch):
    # Path.is_file()/exists() swallow OSError and return False for ANY stat failure — including
    # a permissions problem on the parent directory — which would misreport "unreadable store"
    # as the ordinary "never read" case. is_fresh must not rely on is_file()/exists() for this
    # distinction; it must surface the OSError via read_text()/open() instead.
    from pathlib import Path

    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise PermissionError("parent directory not accessible")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert is_fresh("sess1", "#10") is None


# ── future timestamps (review finding, task-cli#119) ─────────────────────────────────


def test_is_fresh_rejects_a_future_timestamp(isolated_state, monkeypatch):
    # a `ts` in the future (clock skew, or a hand-edited/corrupt line) must not grant an
    # indefinite fresh read — reject it rather than let `now - ts` go negative.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    far_future = 4_102_444_800.0  # year 2100
    path.write_text(
        json.dumps({"id": "#10", "repo": "", "ts": far_future}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 1_000_000.0)
    assert is_fresh("sess1", "#10") is False


def test_is_fresh_accepts_a_timestamp_exactly_at_now(isolated_state, monkeypatch):
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"id": "#10", "repo": "", "ts": 1_000_000.0}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr("tasklib.read_tracker.time.time", lambda: 1_000_000.0)
    assert is_fresh("sess1", "#10") is True


# ── corrupt/adversarial marker content (review finding, task-cli#119) ───────────────


def test_is_fresh_none_on_invalid_utf8_in_the_store(isolated_state):
    # a marker file containing non-UTF-8 bytes (corruption, or something else clobbering the
    # file) must degrade to the fail-closed None outcome, not crash `read_text` uncaught.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"id": "#10", "repo": "", "ts": 1000000}\n\xff\xfe\x00\x01')
    assert is_fresh("sess1", "#10") is None


def test_is_fresh_tolerates_a_deeply_nested_json_line(isolated_state):
    # a single pathologically-nested line can blow json.loads's recursion limit
    # (RecursionError, a RuntimeError — NOT a ValueError/JSONDecodeError) — it must be skipped
    # like any other malformed line, not propagate and crash the whole read.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    poison = "[" * 100_000 + "]" * 100_000
    with path.open("w", encoding="utf-8") as fh:
        fh.write(poison + "\n")
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": time.time()}) + "\n")
    assert is_fresh("sess1", "#10") is True  # the genuine entry after the poison still counts


def test_is_fresh_tolerates_a_json_line_with_an_oversized_integer(isolated_state):
    # review finding, task-cli#119: CPython's integer-string-conversion limit (~4300 digits,
    # sys.set_int_max_str_digits) makes json.loads raise a bare ValueError — NOT
    # json.JSONDecodeError — for a syntactically valid JSON line carrying an integer literal
    # over that length. A narrower `except json.JSONDecodeError` would let this crash the whole
    # read uncaught; it must be skipped like any other malformed line.
    path = state_dir() / "sess1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    poison = '{"id": "#10", "repo": "", "ts": ' + ("9" * 5000) + "}"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(poison + "\n")
        fh.write(json.dumps({"id": "#10", "repo": "", "ts": time.time()}) + "\n")
    assert is_fresh("sess1", "#10") is True  # the genuine entry after the poison still counts
