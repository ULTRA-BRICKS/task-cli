"""tasklib.laya_autoinstall — runtime auto-install of the optional `laya` package (task-cli#208).

Every `subprocess.run` call is mocked (`monkeypatch.setattr(laya_autoinstall.subprocess, "run",
...)`) -- this module must NEVER shell out to a real `pip`/network in the hermetic test suite.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tasklib import laya_autoinstall


class _FakeResult:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture(autouse=True)
def _no_real_laya_autoinstall():
    """Override `conftest.py`'s global safety-net fixture of the same name: THIS module tests
    `ensure_installed` (and `install`) directly -- every dangerous boundary (`subprocess.run`,
    `install`, `_laya_importable`) is mocked per-test instead, so the real orchestration logic
    under test must not itself be replaced by the conftest default."""
    yield


# ── install() — the two-step pip fallback ───────────────────────────────────────────────────

_SPEC = laya_autoinstall._LAYA_PIP_SPEC  # pinned spec every install() attempt must use
_EXE = sys.executable  # install() always targets sys.executable -- see its own docstring


@pytest.fixture
def not_venv(monkeypatch):
    """Force `_in_virtualenv()` False -- the test PROCESS itself runs inside `.venv` (`uv run
    pytest`), so `install()`'s real venv check would otherwise always take the venv branch.
    Production always runs from the SAME process (`ensure_installed` -> `install()`), so the
    real check is correct there -- this fixture only compensates for the test runner's own
    venv-ness so the "not a venv" branch is exercisable at all under `uv run pytest`."""
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: False)


def test_install_succeeds_on_plain_attempt(monkeypatch, not_venv):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(0)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert detail == "installed"
    assert len(calls) == 1
    assert calls[0] == [_EXE, "-m", "pip", "install", "--user", _SPEC]
    assert "--break-system-packages" not in calls[0]


def test_install_retries_with_break_system_packages_on_externally_managed_environment(monkeypatch, not_venv):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--break-system-packages" in argv:
            return _FakeResult(0)
        return _FakeResult(1, stderr="error: externally-managed-environment\n...")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert detail == "installed (--break-system-packages)"
    assert len(calls) == 2
    assert calls[1] == [_EXE, "-m", "pip", "install", "--user", "--break-system-packages", _SPEC]


def test_install_reports_failure_when_break_system_packages_retry_also_fails(monkeypatch, not_venv):
    def fake_run(argv, **kwargs):
        if "--break-system-packages" in argv:
            return _FakeResult(1, stderr="disk full")
        return _FakeResult(1, stderr="error: externally-managed-environment\n...")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "disk full" in detail


def test_install_does_not_retry_on_an_unrelated_failure(monkeypatch, not_venv):
    # An unrelated failure (no network) must NOT gain a noisier --break-system-packages
    # retry -- that flag is scoped to the one PEP 668 error it's a documented fix for.
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(1, stderr="Could not find a version that satisfies the requirement")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert len(calls) == 1  # no retry attempted
    assert "Could not find a version" in detail


def test_install_drops_user_flag_inside_a_virtualenv(monkeypatch):
    # `pip install --user` is unconditionally refused inside an activated virtualenv
    # (live-verified: "User site-packages are not visible in this virtualenv") -- --user must
    # never even be attempted there, and a venv is never "externally managed" either.
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(0)

    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert calls == [[_EXE, "-m", "pip", "install", _SPEC]]
    assert "--user" not in calls[0]


def test_install_handles_timeout_without_raising(monkeypatch, not_venv):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install(timeout=1)
    assert ok is False
    assert "timeout" in detail.lower() or "complete" in detail.lower()


def test_install_handles_missing_pip_oserror_without_raising(monkeypatch, not_venv):
    def fake_run(argv, **kwargs):
        raise OSError("no such file or directory: python3")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert detail


def test_install_truncates_a_very_long_stderr(monkeypatch, not_venv):
    def fake_run(argv, **kwargs):
        return _FakeResult(1, stderr="x" * 5000)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert len(detail) <= laya_autoinstall._MAX_DETAIL_CHARS + 1


def test_laya_pip_spec_matches_pyprojects_laya_extra():
    # `_LAYA_PIP_SPEC` (this module), `pyproject.toml`'s `laya` extra, and `scripts/lib/
    # install_laya.sh`'s `_LAYA_SPEC` are three separate copies (no single cross-language
    # constant is practical across TOML/Python/bash) -- this test turns a drift between ANY
    # pair of them into a caught test failure instead of a silent one.
    import re
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    laya_extra = data["project"]["optional-dependencies"]["laya"]
    assert laya_extra == [laya_autoinstall._LAYA_PIP_SPEC]

    sh_text = (repo_root / "scripts" / "lib" / "install_laya.sh").read_text(encoding="utf-8")
    match = re.search(r'_LAYA_SPEC="([^"]+)"', sh_text)
    assert match is not None, "scripts/lib/install_laya.sh must define _LAYA_SPEC=\"...\""
    assert match.group(1) == laya_autoinstall._LAYA_PIP_SPEC


# ── should_attempt / mark_failed_attempt (cooldown cache) ──────────────────────────────────


def test_should_attempt_true_before_any_failure(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert laya_autoinstall.should_attempt(env=env) is True


def test_should_attempt_false_immediately_after_a_failed_attempt(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    laya_autoinstall.mark_failed_attempt(now=1_000_000.0, env=env)
    assert laya_autoinstall.should_attempt(now=1_000_000.0 + 60, env=env) is False


def test_should_attempt_true_again_after_cooldown_elapses(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    laya_autoinstall.mark_failed_attempt(now=1_000_000.0, env=env)
    later = 1_000_000.0 + laya_autoinstall.cooldown_seconds() + 1
    assert laya_autoinstall.should_attempt(now=later, env=env) is True


def test_should_attempt_true_when_cache_is_corrupted(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    path = laya_autoinstall.cache_path(env)
    path.parent.mkdir(parents=True)
    path.write_text("not json at all {{{", encoding="utf-8")
    assert laya_autoinstall.should_attempt(env=env) is True


def test_should_attempt_true_when_cache_is_valid_json_but_not_an_object(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    path = laya_autoinstall.cache_path(env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert laya_autoinstall.should_attempt(env=env) is True


def test_should_attempt_true_when_cache_records_a_future_timestamp(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    laya_autoinstall.mark_failed_attempt(now=2_000_000.0, env=env)
    assert laya_autoinstall.should_attempt(now=1_000_000.0, env=env) is True


def test_mark_failed_attempt_survives_unwritable_directory(monkeypatch, tmp_path):
    # A read-only/unwritable state dir must never crash the caller -- best-effort, matching
    # read_tracker.record's own silent-failure contract.
    env = {"XDG_STATE_HOME": str(tmp_path)}

    def _boom(*a, **k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("pathlib.Path.write_text", _boom)
    laya_autoinstall.mark_failed_attempt(env=env)  # must not raise


def test_cooldown_seconds_reflects_env_override(monkeypatch):
    monkeypatch.setenv("TASK_LAYA_AUTOINSTALL_COOLDOWN_S", "3600")
    assert laya_autoinstall.cooldown_seconds() == 3600


def test_cooldown_seconds_rejects_non_positive_override(monkeypatch):
    monkeypatch.setenv("TASK_LAYA_AUTOINSTALL_COOLDOWN_S", "0")
    assert laya_autoinstall.cooldown_seconds() == laya_autoinstall._COOLDOWN_S_DEFAULT


def test_cooldown_seconds_rejects_garbage_override(monkeypatch):
    monkeypatch.setenv("TASK_LAYA_AUTOINSTALL_COOLDOWN_S", "not-a-number")
    assert laya_autoinstall.cooldown_seconds() == laya_autoinstall._COOLDOWN_S_DEFAULT


# ── _refresh_user_site_path() — make a first-ever install visible in THIS process ──────────


def test_refresh_user_site_path_adds_the_user_site_dir(monkeypatch, not_venv):
    import site as real_site

    calls = []
    monkeypatch.setattr(real_site, "addsitedir", lambda path: calls.append(path))
    laya_autoinstall._refresh_user_site_path()
    assert calls == [real_site.getusersitepackages()]


def test_refresh_user_site_path_never_raises_on_a_broken_site_module(monkeypatch, not_venv):
    import site as real_site

    def _boom(path):
        raise RuntimeError("simulated site.addsitedir failure")

    monkeypatch.setattr(real_site, "addsitedir", _boom)
    laya_autoinstall._refresh_user_site_path()  # must not raise


def test_refresh_user_site_path_is_a_noop_inside_a_virtualenv(monkeypatch):
    # A venv install never uses `--user` (see `install()`'s own docstring) -- appending the
    # HOST user-site dir would only pollute the venv's isolated `sys.path`.
    import site as real_site

    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: True)
    calls = []
    monkeypatch.setattr(real_site, "addsitedir", lambda path: calls.append(path))
    laya_autoinstall._refresh_user_site_path()
    assert calls == []


# ── ensure_installed() — the orchestrating entry point ──────────────────────────────────────


def test_ensure_installed_skips_silently_within_cooldown(tmp_path, monkeypatch, capsys):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    laya_autoinstall.mark_failed_attempt(now=1_000_000.0, env=env)

    def _never_called(*a, **k):
        raise AssertionError("install() must not run while the cooldown is active")

    monkeypatch.setattr(laya_autoinstall, "install", _never_called)
    result = laya_autoinstall.ensure_installed(env=env, now=1_000_000.0 + 60)
    assert result is False
    captured = capsys.readouterr()
    assert captured.err == ""


def test_ensure_installed_attempts_prints_and_succeeds(tmp_path, monkeypatch, capsys):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "install", lambda: (True, "installed"))
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: True)
    refreshed = []
    monkeypatch.setattr(
        laya_autoinstall, "_refresh_user_site_path", lambda: refreshed.append(True)
    )
    invalidated = []
    monkeypatch.setattr(
        "importlib.invalidate_caches", lambda: invalidated.append(True)
    )
    result = laya_autoinstall.ensure_installed(env=env)
    assert result is True
    assert refreshed == [True]  # first-ever install visible in THIS process (live-verified fix)
    assert invalidated == [True]
    err = capsys.readouterr().err
    assert "laya not installed; installing now" in err
    assert "installed" in err
    # a successful, genuinely-importable install never writes a cooldown marker
    assert not laya_autoinstall.cache_path(env).exists()


def test_ensure_installed_marks_failure_when_pip_succeeds_but_package_stays_unimportable(
    tmp_path, monkeypatch, capsys
):
    # PEP 668/venv review finding: pip exiting 0 does not guarantee `import laya` works (e.g.
    # PYTHONNOUSERSITE disabling the very directory pip just installed into) -- this must be
    # treated as a FAILURE (cooldown marker written, `False` returned), not a silent success.
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "install", lambda: (True, "installed"))
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: False)
    monkeypatch.setattr("importlib.invalidate_caches", lambda: None)
    result = laya_autoinstall.ensure_installed(env=env, now=5_000_000.0)
    assert result is False
    err = capsys.readouterr().err
    assert "still not importable" in err
    assert "falling back" in err
    assert laya_autoinstall.cache_path(env).exists()
    assert laya_autoinstall.should_attempt(env=env, now=5_000_000.0 + 60) is False


def test_ensure_installed_attempts_prints_and_fails_then_caches(tmp_path, monkeypatch, capsys):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "install", lambda: (False, "no network"))
    result = laya_autoinstall.ensure_installed(env=env, now=5_000_000.0)
    assert result is False
    err = capsys.readouterr().err
    assert "laya not installed; installing now" in err
    assert "no network" in err
    assert "falling back" in err
    assert laya_autoinstall.cache_path(env).exists()
    assert laya_autoinstall.should_attempt(env=env, now=5_000_000.0 + 60) is False


def test_ensure_installed_second_call_after_failure_is_silent_within_cooldown(tmp_path, monkeypatch, capsys):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "install", lambda: (False, "offline"))
    laya_autoinstall.ensure_installed(env=env, now=1_000_000.0)
    capsys.readouterr()  # discard first call's output

    def _never_called(*a, **k):
        raise AssertionError("install() must not run again within the cooldown window")

    monkeypatch.setattr(laya_autoinstall, "install", _never_called)
    result = laya_autoinstall.ensure_installed(env=env, now=1_000_000.0 + 60)
    assert result is False
    assert capsys.readouterr().err == ""
