"""task attach — dedicated screenshot-proof verb (task-cli#167)."""

from __future__ import annotations

import pytest

from tasklib.backends import BackendError
from tasklib.cli import build_parser, main
from tasklib.model import State


_GOOD_IMPACT = (
    "Users on the dashboard can finally see their report load, so they no longer give up and "
    "leave thinking the page is broken"
)


@pytest.fixture
def fake(monkeypatch, fake_backend, isolated_state):
    monkeypatch.setattr(
        "tasklib.backends.get_backend", lambda cfg, env=None: fake_backend
    )
    monkeypatch.setenv("TASK_SESSION", "testsess")
    return fake_backend


def _create_argv():
    return [
        "create",
        "--title",
        "Add a thing",
        "--what",
        "the change",
        "--why",
        "because",
        "--impact",
        _GOOD_IMPACT,
        "--if-not-done",
        "pain",
        "--acceptance",
        "it works",
        "--acceptance",
        "it also handles the empty case",
    ]


def test_attach_adds_implementation_screenshot(tmp_path, capsys, fake):
    shot = tmp_path / "after.png"
    shot.write_bytes(b"png")
    main(_create_argv())
    capsys.readouterr()
    rc = main(["attach", "#1", str(shot)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "attached" in out
    ticket = fake.get("#1")
    assert any(s.ref == str(shot) and s.kind == "implementation" for s in ticket.screenshots)
    assert ("#1", str(shot)) in fake.attachments
    assert ticket.state is not State.DONE


def test_attach_multiple_paths(tmp_path, fake):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    main(_create_argv())
    rc = main(["attach", "#1", str(a), str(b)])
    assert rc == 0
    ticket = fake.get("#1")
    impl = [s for s in ticket.screenshots if s.kind == "implementation"]
    assert [s.ref for s in impl] == [str(a), str(b)]
    assert ("#1", str(a)) in fake.attachments
    assert ("#1", str(b)) in fake.attachments


def test_attach_unknown_ticket_exits_2(capsys, fake):
    rc = main(["attach", "#99", "shot.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out
    assert fake.attachments == []


def test_attach_missing_path_is_usage_error():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["attach", "#1"])


def test_attach_lets_ui_ticket_pass_on_done_screenshot_gate(tmp_path, capsys, fake):
    shot = tmp_path / "after.png"
    shot.write_bytes(b"png")
    main(_create_argv() + ["--label", "ui", "--screenshot", "creation.png"])
    ticket = fake.get("#1")
    for i in range(1, len(ticket.acceptance) + 1):
        main(["check", "#1", str(i), "--proof", "proof.png"])
    capsys.readouterr()
    rc = main(["attach", "#1", str(shot)])
    assert rc == 0
    rc = main(["done", "#1"])
    assert rc == 0


def test_attach_missing_local_file_exits_2_without_side_effects(capsys, fake):
    main(_create_argv())
    capsys.readouterr()
    rc = main(["attach", "#1", "missing.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out
    assert "not a file" in out
    assert fake.attachments == []
    assert fake.get("#1").screenshots == []


def test_attach_hosted_url_does_not_need_a_local_file(fake):
    main(_create_argv())
    rc = main(["attach", "#1", "https://example.com/shot.png"])
    assert rc == 0
    assert ("#1", "https://example.com/shot.png") in fake.attachments


def test_attach_backend_error_does_not_write_implementation_proof(capsys, fake):
    main(_create_argv())
    capsys.readouterr()

    def boom(ticket_id, file_path):
        raise BackendError("upload rejected")

    fake.attach = boom
    rc = main(["attach", "#1", "https://example.com/shot.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "error:" in out
    assert "upload rejected" in out
    assert fake.get("#1").screenshots == []


def test_attach_help_describes_screenshot(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["attach", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "screenshot" in out.lower()
    assert "PATH" in out


def test_skill_documents_task_attach():
    from tasklib.install import SKILL_MD

    assert "task attach" in SKILL_MD

def test_attach_redacts_token_shaped_path(tmp_path, capsys, fake):
    shot = tmp_path / "ghp_secret1234567890abcdef.png"
    shot.write_bytes(b"png")
    main(_create_argv())
    capsys.readouterr()
    rc = main(["attach", "#1", str(shot)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ghp_secret1234567890abcdef" not in out


def test_attach_ambiguous_returns_9(capsys, fake):
    from tasklib.backends import AmbiguousBackendError, EXIT_AMBIGUOUS

    main(_create_argv())
    capsys.readouterr()

    def boom(ticket_id, file_path):
        raise AmbiguousBackendError("connection dropped mid-write")

    fake.attach = boom
    rc = main(["attach", "#1", "https://example.com/shot.png"])
    out = capsys.readouterr().out
    assert rc == EXIT_AMBIGUOUS
    assert "ambiguous:" in out
    assert fake.get("#1").screenshots == []


def test_attach_persists_prefix_when_later_path_fails(tmp_path, capsys, fake):
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    main(_create_argv())
    capsys.readouterr()
    original = fake.attach
    n = {"i": 0}

    def sometimes(ticket_id, file_path):
        n["i"] += 1
        if n["i"] == 1:
            return original(ticket_id, file_path)
        raise BackendError("second upload rejected")

    fake.attach = sometimes
    rc = main(["attach", "#1", str(a), "https://example.com/b.png"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "second upload rejected" in out
    impl = [s.ref for s in fake.get("#1").screenshots if s.kind == "implementation"]
    assert impl == [str(a)]
