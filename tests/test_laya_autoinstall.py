"""tasklib.laya_autoinstall — runtime auto-install of the optional `laya` package (task-cli#208,
task-cli#224).

Every `subprocess.run` call is mocked (`monkeypatch.setattr(laya_autoinstall.subprocess, "run",
...)`) -- this module must NEVER shell out to a real `pip`/`uv`/`venv`/network in the hermetic
test suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tasklib import laya_autoinstall

# Captured before the autouse fixture below patches it, for the one test of the real lookup.
_REAL_ACCOUNT_HOME = laya_autoinstall._account_home


class _FakeResult:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture(autouse=True)
def _no_real_laya_autoinstall(monkeypatch, tmp_path):
    """Override `conftest.py`'s global safety-net fixture of the same name: THIS module tests
    `ensure_installed` (and `install`) directly -- every dangerous boundary (`subprocess.run`,
    `install`, `_laya_importable`) is mocked per-test instead, so the real orchestration logic
    under test must not itself be replaced by the conftest default.

    Also points `$HOME` and `$XDG_STATE_HOME` at a scratch dir (review finding): `install()`'s
    `_neutral_subprocess_cwd` really `mkdir`s the state dir, so a test that calls `install()`
    without an `env=` override would otherwise create `~/.local/state/task-cli/...` in the
    developer's real home."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "home" / ".local" / "state"))
    monkeypatch.setattr(laya_autoinstall, "_account_home", lambda: tmp_path / "home")
    yield


# ── install() — case 1: already inside a virtualenv ─────────────────────────────────────────

_SPEC = laya_autoinstall._LAYA_PIP_SPEC  # pinned spec every install() attempt must use
_EXE = sys.executable  # install() always targets sys.executable -- see its own docstring


@pytest.fixture
def not_venv(monkeypatch):
    """Force `_in_virtualenv()` False -- the test PROCESS itself runs inside `.venv` (`uv run
    pytest`), so `install()`'s real venv check would otherwise always take the venv branch.
    Production always runs from the SAME process (`ensure_installed` -> `install()`), so the
    real check is correct there -- this fixture only compensates for the test runner's own
    venv-ness so the dedicated-venv branch is exercisable at all under `uv run pytest`."""
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: False)


@pytest.fixture
def in_venv_without_uv(monkeypatch):
    """Case 1 with no trusted `uv` -- the `python -m pip` fallback."""
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(laya_autoinstall, "_resolve_trusted_uv", lambda: None)


def test_install_direct_when_already_in_a_virtualenv(monkeypatch, in_venv_without_uv):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(0)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert detail == "installed"
    assert calls == [[_EXE, "-P", "-m", "pip", "install", _SPEC]]
    assert "--user" not in calls[0]
    assert "--break-system-packages" not in calls[0]


def test_install_direct_prefers_trusted_uv_in_a_pipless_venv(monkeypatch):
    # `uv tool install hyper-task` builds a tool venv without pip: case 1 must go through uv
    # (scrubbed env, UV_NO_CONFIG, neutral cwd) instead of a `-m pip` that can never succeed.
    for leaky_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
        monkeypatch.setenv(leaky_var, "attacker-controlled-value")
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: True)
    monkeypatch.setattr(laya_autoinstall, "_resolve_trusted_uv", lambda: "/opt/homebrew/bin/uv")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _FakeResult(0)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    assert laya_autoinstall.install() == (True, "installed")
    [(argv, kwargs)] = calls
    assert argv == ["/opt/homebrew/bin/uv", "pip", "install", "--python", _EXE, _SPEC]
    assert kwargs["env"]["UV_NO_CONFIG"] == "1"
    for scrubbed_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
        assert scrubbed_var not in kwargs["env"]
    assert kwargs["cwd"] == str(laya_autoinstall._state_dir())


def test_install_direct_reports_failure_without_raising(monkeypatch, in_venv_without_uv):
    monkeypatch.setattr(
        laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(1, stderr="disk full")
    )
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "disk full" in detail


def test_install_failure_detail_is_the_last_stderr_line(monkeypatch, in_venv_without_uv):
    # pip/uv print progress first and the actual error last; the detail must be the error.
    stderr = "Collecting laya\n  Downloading laya-0.3.21.whl\nERROR: No matching distribution found\n\n"
    monkeypatch.setattr(laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(1, stderr=stderr))
    assert laya_autoinstall.install() == (False, "ERROR: No matching distribution found")


def test_install_direct_handles_timeout_without_raising(monkeypatch, in_venv_without_uv):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install(timeout=1)
    assert ok is False
    assert "timeout" in detail.lower() or "complete" in detail.lower()


def test_install_direct_handles_missing_interpreter_oserror_without_raising(monkeypatch, in_venv_without_uv):
    def fake_run(argv, **kwargs):
        raise OSError("no such file or directory: python3")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert detail


def test_install_direct_truncates_a_very_long_stderr(monkeypatch, in_venv_without_uv):
    monkeypatch.setattr(
        laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(1, stderr="x" * 5000)
    )
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert len(detail) <= laya_autoinstall._MAX_DETAIL_CHARS + 1


def test_install_outer_exception_handler_never_raises(monkeypatch):
    # A genuinely unexpected exception anywhere in install()'s body (not just a subprocess
    # failure) must still degrade to (False, detail), never propagate -- install()'s own
    # documented "never raises" contract.
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: False)

    def _boom(**kw):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(laya_autoinstall, "_install_into_dedicated_venv", _boom)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "simulated unexpected failure" in detail


# ── install() — case 2: dedicated venv when NOT already in a virtualenv ────────────────────


def _classify(argv: list[str]) -> str:
    """A tiny router used by the fake `subprocess.run` below to tell apart the distinct
    subprocess calls `install()`'s dedicated-venv branch makes, by shape rather than by exact
    text (keeps the fakes readable and resilient to incidental arg-order changes)."""
    if len(argv) >= 2 and argv[-2:] == ["-c", "import sys"]:
        return "probe"
    if "sysconfig.get_path" in " ".join(argv):
        return "site_query"
    if "venv" in argv and "--python" in argv:
        return "venv_create"  # covers both `uv venv --seed --python ...` and disambiguates via argv[0]
    if "-m" in argv and "venv" in argv:
        return "stdlib_create"
    if "pip" in argv and "install" in argv:
        return "install"
    raise AssertionError(f"unclassified subprocess call: {argv}")


@pytest.fixture
def dedicated_venv_dir(monkeypatch, tmp_path):
    """Point the dedicated-venv location at a scratch dir and make the site-packages bolt-on
    a no-op recorder -- shared setup for the dedicated-venv-branch tests below."""
    monkeypatch.setattr(laya_autoinstall, "_resolve_trusted_uv", lambda: None)
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: tmp_path / "laya_venv")
    activated = []
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda path: activated.append(path))
    return tmp_path / "laya_venv", activated


def test_install_creates_and_uses_a_dedicated_venv_without_uv(monkeypatch, dedicated_venv_dir, not_venv):
    for _leaky_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
        monkeypatch.setenv(_leaky_var, "attacker-controlled-value")  # scrub must strip every one of these
    venv_dir, activated = dedicated_venv_dir
    resolved_site_packages = f"{venv_dir}/lib/python3.x/site-packages"
    calls = []
    kwargs_by_call = []
    probe_count = {"n": 0}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs_by_call.append(kwargs)
        kind = _classify(argv)
        if kind == "probe":
            probe_count["n"] += 1
            return _FakeResult(0 if probe_count["n"] > 1 else 1)
        if kind == "stdlib_create":
            return _FakeResult(0)
        if kind == "install":
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(0, stdout=resolved_site_packages + "\n")
        raise AssertionError(f"unexpected call kind: {kind}")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert detail == "installed (venv)"
    assert activated == [resolved_site_packages]
    kinds = [_classify(c) for c in calls]
    assert kinds == ["probe", "stdlib_create", "probe", "install", "site_query"]
    create_argv = calls[kinds.index("stdlib_create")]
    assert create_argv == [_EXE, "-P", "-m", "venv", "--clear", str(venv_dir)]
    for argv in calls:
        assert "--break-system-packages" not in argv
        assert "--user" not in argv
    # review finding: EVERY subprocess call gets a neutral cwd (never task classify's own
    # untrusted cwd) and a PYTHONPATH/package-index-URL-scrubbed environment.
    for kw in kwargs_by_call:
        assert kw.get("cwd") is not None
        child_env = kw.get("env") or {}
        for scrubbed_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
            assert scrubbed_var not in child_env
    # the resolved site-packages path is cached for a later, subprocess-free reuse
    marker = laya_autoinstall._venv_site_marker_path(venv_dir)
    assert marker.read_text(encoding="utf-8") == resolved_site_packages


def test_install_creates_and_uses_a_dedicated_venv_with_uv(monkeypatch, tmp_path, not_venv):
    for _leaky_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
        monkeypatch.setenv(_leaky_var, "attacker-controlled-value")  # scrub must strip every one of these
    venv_dir = tmp_path / "laya_venv"
    venv_python = laya_autoinstall._venv_python_path(venv_dir)
    resolved_site_packages = f"{venv_dir}/site-packages"
    monkeypatch.setattr(laya_autoinstall, "_resolve_trusted_uv", lambda: "/opt/homebrew/bin/uv")
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: venv_dir)
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda path: None)

    calls = []
    kwargs_by_call = []
    probe_count = {"n": 0}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kwargs_by_call.append(kwargs)
        kind = _classify(argv)
        if kind == "probe":
            probe_count["n"] += 1
            return _FakeResult(0 if probe_count["n"] > 1 else 1)
        if kind == "venv_create":
            return _FakeResult(0)
        if kind == "install":
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(0, stdout=resolved_site_packages)
        raise AssertionError(f"unexpected call kind: {kind}")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    assert detail == "installed (venv)"
    kinds = [_classify(c) for c in calls]
    assert kinds == ["probe", "venv_create", "probe", "install", "site_query"]
    create_argv = calls[kinds.index("venv_create")]
    # --seed is NOT optional: an unseeded `uv venv` has no pip, and the stdlib-pip fallback
    # would have nothing to fall back to if `uv` later disappears from $PATH. `--clear`
    # matches the stdlib path's own self-heal-by-rebuilding guarantee.
    assert create_argv == ["/opt/homebrew/bin/uv", "venv", "--seed", "--clear", "--python", _EXE, str(venv_dir)]
    install_argv = calls[kinds.index("install")]
    assert install_argv == ["/opt/homebrew/bin/uv", "pip", "install", "--python", str(venv_python), _SPEC]
    # review finding: EVERY child (uv and stdlib alike) gets a neutral cwd (never task
    # classify's own untrusted cwd) and a PYTHONPATH/package-index-URL-scrubbed environment.
    for kw in kwargs_by_call:
        assert kw.get("cwd") is not None
        child_env = kw.get("env") or {}
        for scrubbed_var in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS:
            assert scrubbed_var not in child_env
    uv_create_kwargs = kwargs_by_call[kinds.index("venv_create")]
    uv_install_kwargs = kwargs_by_call[kinds.index("install")]
    assert uv_create_kwargs["env"]["UV_NO_CONFIG"] == "1"
    assert uv_install_kwargs["env"]["UV_NO_CONFIG"] == "1"


def test_install_reuses_an_already_working_dedicated_venv(monkeypatch, dedicated_venv_dir, not_venv):
    # The very first probe succeeds -- no venv (re)creation subprocess at all.
    venv_dir, _activated = dedicated_venv_dir
    resolved_site_packages = f"{venv_dir}/site-packages"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(0)
        if kind == "install":
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(0, stdout=resolved_site_packages)
        raise AssertionError(f"unexpected call kind: {kind} (venv creation must not run)")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is True
    kinds = [_classify(c) for c in calls]
    assert kinds == ["probe", "install", "site_query"]  # exactly one probe, no (re)creation


def test_install_venv_creation_failure_reports_and_does_not_raise(monkeypatch, dedicated_venv_dir, not_venv):
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(1)
        if kind == "stdlib_create":
            return _FakeResult(1, stderr="No space left on device")
        raise AssertionError(f"unexpected call kind: {kind} (install must never be attempted)")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "No space left on device" in detail


def test_install_venv_creation_succeeds_but_post_creation_probe_fails(monkeypatch, dedicated_venv_dir, not_venv):
    # A venv that "creates" (exit 0) but whose interpreter still fails the sanity probe (e.g.
    # a truncated/corrupt install) must fail cleanly, never fall through to install anyway.
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(1)  # every probe fails, including the post-creation one
        if kind == "stdlib_create":
            return _FakeResult(0)
        raise AssertionError(f"unexpected call kind: {kind} (install must never be attempted)")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "sanity check" in detail


def test_install_venv_install_failure_reports_and_does_not_raise(monkeypatch, dedicated_venv_dir, not_venv):
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(0)
        if kind == "install":
            return _FakeResult(1, stderr="Could not find a version that satisfies the requirement")
        raise AssertionError(f"unexpected call kind: {kind}")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "Could not find a version" in detail


def test_install_venv_site_packages_query_failure_reports_and_does_not_raise(monkeypatch, dedicated_venv_dir, not_venv):
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(0)
        if kind == "install":
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(1, stderr="boom")
        raise AssertionError(f"unexpected call kind: {kind}")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "site-packages" in detail


def test_install_refuses_a_site_packages_path_outside_the_dedicated_venv(monkeypatch, dedicated_venv_dir, not_venv):
    # A hijacked sysconfig answer must never be bolted onto sys.path, nor cached as the marker.
    venv_dir, activated = dedicated_venv_dir

    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind in ("probe", "install"):
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(0, stdout="/attacker/site-packages\n")
        raise AssertionError(f"unexpected call kind: {kind}")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert "outside the dedicated venv" in detail
    assert activated == []
    assert not laya_autoinstall._venv_site_marker_path(venv_dir).exists()


def test_install_venv_creation_timeout_does_not_raise(monkeypatch, dedicated_venv_dir, not_venv):
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(1)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install(timeout=1)
    assert ok is False
    assert "timeout" in detail.lower() or "complete" in detail.lower()


def test_install_venv_creation_oserror_does_not_raise(monkeypatch, dedicated_venv_dir, not_venv):
    def fake_run(argv, **kwargs):
        kind = _classify(argv)
        if kind == "probe":
            return _FakeResult(1)
        raise OSError("no such file or directory")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    ok, detail = laya_autoinstall.install()
    assert ok is False
    assert detail


def test_install_never_uses_break_system_packages_anywhere(monkeypatch, dedicated_venv_dir, not_venv):
    # Broad regression guard covering the whole dedicated-venv call sequence at once.
    calls = []
    probe_count = {"n": 0}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        kind = _classify(argv)
        if kind == "probe":
            probe_count["n"] += 1
            return _FakeResult(0 if probe_count["n"] > 1 else 1)
        if kind in ("stdlib_create", "install"):
            return _FakeResult(0)
        if kind == "site_query":
            return _FakeResult(0, stdout="/site-packages")
        raise AssertionError(kind)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    laya_autoinstall.install()
    flat = " ".join(" ".join(c) for c in calls)
    assert "--break-system-packages" not in flat
    assert "PIP_BREAK_SYSTEM_PACKAGES" not in flat


# ── env/PATH hardening: _sanitized_child_env / _resolve_trusted_uv ─────────────────────────


def test_sanitized_child_env_strips_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/attacker-controlled/path")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    result = laya_autoinstall._sanitized_child_env()
    assert "PYTHONPATH" not in result
    assert result["SOME_OTHER_VAR"] == "kept"


def test_sanitized_child_env_strips_package_index_env_vars(monkeypatch):
    # review finding: a malicious checkout's auto-loaded `.env` (via `uv run`) or ambient
    # shell env must never be able to redirect the laya install to an attacker-controlled
    # package index -- the same cwd-independent-channel hole PYTHONPATH already closed, just
    # for the package SOURCE instead of the import path.
    index_vars = [v for v in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS if v != "PYTHONPATH"]
    for name in index_vars:
        monkeypatch.setenv(name, "https://attacker-controlled.example/simple")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    result = laya_autoinstall._sanitized_child_env()
    for name in index_vars:
        assert name not in result
    assert result["SOME_OTHER_VAR"] == "kept"


def test_scrubbed_vars_include_find_links_and_cache_dir(monkeypatch):
    # UV_FIND_LINKS is the uv equivalent of PIP_FIND_LINKS (already scrubbed): an attacker
    # can redirect package resolution to a directory of pre-built malicious wheels.
    # UV_CACHE_DIR / PIP_CACHE_DIR: a poisoned cache directory serves cached .whl files
    # without re-fetching from the index, so the cached content is trusted implicitly.
    # UV_CONFIG_FILE: uv's per-project/user config can set all of the above.
    # UV_INSECURE_HOST: allows plain HTTP (MITM) to a package-index host.
    new_attack_vectors = [
        ("UV_FIND_LINKS", "/attacker/fake-packages/"),
        ("UV_CACHE_DIR", "/attacker/poisoned-cache/"),
        ("PIP_CACHE_DIR", "/attacker/poisoned-cache/"),
        ("UV_CONFIG_FILE", "/attacker/uv.toml"),
        ("UV_INSECURE_HOST", "attacker-controlled.example"),
    ]
    for name, value in new_attack_vectors:
        assert name in laya_autoinstall._SCRUBBED_CHILD_ENV_VARS, (
            f"{name} must be in _SCRUBBED_CHILD_ENV_VARS"
        )
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin")  # must survive the scrub
    result = laya_autoinstall._sanitized_child_env()
    for name, _ in new_attack_vectors:
        assert name not in result, f"{name} leaked through to child env"
    assert result["PATH"] == "/usr/bin"


def test_sanitized_child_env_strips_pythonpath_even_when_absent(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = laya_autoinstall._sanitized_child_env()
    assert "PYTHONPATH" not in result  # never raises on a missing key either


def test_sanitized_child_env_applies_extra_on_top_of_the_scrub(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/attacker-controlled/path")
    result = laya_autoinstall._sanitized_child_env(extra={"UV_NO_CONFIG": "1"})
    assert "PYTHONPATH" not in result
    assert result["UV_NO_CONFIG"] == "1"


def test_uv_proc_env_is_sanitized_and_carries_uv_no_config(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/attacker-controlled/path")
    result = laya_autoinstall._uv_proc_env()
    assert "PYTHONPATH" not in result
    assert result["UV_NO_CONFIG"] == "1"


def test_resolve_trusted_uv_finds_it_on_an_absolute_path_entry(monkeypatch, tmp_path):
    real_bin = tmp_path / "realbin"
    real_bin.mkdir()
    uv_path = real_bin / "uv"
    uv_path.write_text("#!/bin/sh\n", encoding="utf-8")
    uv_path.chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("PATH", str(real_bin))
    monkeypatch.chdir(elsewhere)
    assert laya_autoinstall._resolve_trusted_uv() == str(uv_path.resolve())


def test_resolve_trusted_uv_drops_empty_and_relative_path_entries(monkeypatch, tmp_path):
    # An empty $PATH entry means "the current directory" per POSIX shell convention -- a
    # planted `uv` in the untrusted target repo's cwd must never be found this way.
    decoy_dir = tmp_path / "decoy_cwd"
    decoy_dir.mkdir()
    decoy_uv = decoy_dir / "uv"
    decoy_uv.write_text("#!/bin/sh\n", encoding="utf-8")
    decoy_uv.chmod(0o755)
    monkeypatch.setenv("PATH", f":relative/bin:{decoy_dir}")
    monkeypatch.chdir(decoy_dir)
    # even though `decoy_dir` IS on $PATH (as an absolute entry), the cwd-containment check
    # below must still refuse it since it resolves inside the current working directory.
    assert laya_autoinstall._resolve_trusted_uv() is None


def test_resolve_trusted_uv_refuses_a_binary_inside_the_current_directory(monkeypatch, tmp_path):
    # Belt-and-suspenders: even a `uv` resolved from a seemingly legitimate absolute $PATH
    # entry is refused if it turns out to live inside cwd (the untrusted target repo).
    repo_bin = tmp_path / "repo" / "bin"
    repo_bin.mkdir(parents=True)
    planted_uv = repo_bin / "uv"
    planted_uv.write_text("#!/bin/sh\n", encoding="utf-8")
    planted_uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(repo_bin))
    monkeypatch.chdir(tmp_path / "repo")
    assert laya_autoinstall._resolve_trusted_uv() is None


def test_resolve_trusted_uv_returns_none_when_uv_not_found(monkeypatch, tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_dir))
    assert laya_autoinstall._resolve_trusted_uv() is None


def test_resolve_trusted_uv_returns_none_with_no_path_at_all(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert laya_autoinstall._resolve_trusted_uv() is None


def test_laya_pip_spec_matches_pyprojects_laya_extra():
    # `_LAYA_PIP_SPEC` (this module) and `pyproject.toml`'s `laya` extra are the only two
    # copies left (task-cli#224 removed `scripts/lib/install_laya.sh`'s own duplicate by
    # having it delegate straight into this module) -- this test turns a drift between them
    # into a caught test failure instead of a silent one.
    import tomllib

    repo_root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    laya_extra = data["project"]["optional-dependencies"]["laya"]
    assert laya_extra == [laya_autoinstall._LAYA_PIP_SPEC]


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


# ── dedicated-venv helpers ───────────────────────────────────────────────────────────────


def test_dedicated_venv_dir_is_version_keyed_and_a_sibling_of_the_cache_file(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    venv_dir = laya_autoinstall._dedicated_venv_dir(env)
    assert venv_dir.parent == laya_autoinstall.cache_path(env).parent
    tag = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert venv_dir.name == f"laya_venv-py{tag}"


# ── _state_dir cwd-containment refusal ──────────────────────────────────────────────────────


def test_state_dir_refuses_an_override_nested_inside_the_current_directory(monkeypatch, tmp_path):
    # A poisoned/relative $XDG_STATE_HOME (ambient env, or a malicious checkout's auto-loaded
    # .env via `uv run`) must never place the dedicated venv/cache inside cwd -- the untrusted
    # target repo `task classify` runs in -- same class of refusal as `_resolve_trusted_uv`'s
    # cwd check on the `uv` binary.
    repo_cwd = tmp_path / "untrusted_repo"
    repo_cwd.mkdir()
    monkeypatch.chdir(repo_cwd)
    env = {"XDG_STATE_HOME": str(repo_cwd / ".fake_state")}
    result = laya_autoinstall._state_dir(env)
    assert result == laya_autoinstall._trusted_default_state_dir()


def test_state_dir_refuses_an_override_equal_to_the_current_directory(monkeypatch, tmp_path):
    state_target = tmp_path / "cache_root" / "task-cli" / "laya_autoinstall"
    state_target.mkdir(parents=True)
    monkeypatch.chdir(state_target)
    env = {"XDG_STATE_HOME": str(tmp_path / "cache_root")}
    result = laya_autoinstall._state_dir(env)
    assert result == laya_autoinstall._trusted_default_state_dir()


def test_state_dir_accepts_an_override_outside_the_current_directory(monkeypatch, tmp_path):
    # Sanity counterpart: a legitimate override that does NOT resolve inside cwd must still
    # be honored, not always overridden to the HOME default.
    other_cwd = tmp_path / "some_repo"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    state_home = tmp_path / "trusted_state"
    env = {"XDG_STATE_HOME": str(state_home)}
    result = laya_autoinstall._state_dir(env)
    assert result == state_home / "task-cli" / "laya_autoinstall"


@pytest.mark.parametrize("relative", [".state", "./.state", "../outside_state"])
def test_state_dir_refuses_a_relative_override(monkeypatch, tmp_path, relative):
    # A relative $XDG_STATE_HOME is anchored at cwd (the untrusted target repo) -- even one
    # that climbs out via `..` -- and the XDG spec says to ignore relative values anyway.
    repo_cwd = tmp_path / "untrusted_repo"
    repo_cwd.mkdir()
    monkeypatch.chdir(repo_cwd)
    result = laya_autoinstall._state_dir({"XDG_STATE_HOME": relative})
    assert result == laya_autoinstall._trusted_default_state_dir()


def test_state_dir_containment_applies_to_the_process_environment(monkeypatch, tmp_path):
    # Production passes env=None, i.e. the real os.environ -- the refusal must hold there too.
    repo_cwd = tmp_path / "untrusted_repo"
    repo_cwd.mkdir()
    monkeypatch.chdir(repo_cwd)
    monkeypatch.setenv("XDG_STATE_HOME", str(repo_cwd / ".state"))
    assert laya_autoinstall._state_dir() == laya_autoinstall._trusted_default_state_dir()
    assert laya_autoinstall._dedicated_venv_dir().parent == laya_autoinstall._state_dir()


def test_state_dir_refuses_a_poisoned_home_without_falling_back_to_it(monkeypatch, tmp_path):
    # XDG_STATE_HOME unset and $HOME pointed into the repo: the fallback must come from the
    # account database, not from the same poisoned $HOME it just refused.
    repo = tmp_path / "untrusted_repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    result = laya_autoinstall._state_dir({"HOME": str(repo / ".fakehome")})
    assert result == tmp_path / "home" / ".local" / "state" / "task-cli" / "laya_autoinstall"


def test_state_dir_refuses_an_override_elsewhere_in_the_enclosing_repo(monkeypatch, tmp_path):
    # cwd is a SUBDIRECTORY of the untrusted repo; an override at the repo root is not under
    # cwd but is still inside the repo, and must be refused.
    repo = tmp_path / "untrusted_repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    result = laya_autoinstall._state_dir({"XDG_STATE_HOME": str(repo / ".state")})
    assert result == laya_autoinstall._trusted_default_state_dir()


def test_state_dir_refuses_a_symlink_into_the_repo(monkeypatch, tmp_path):
    repo = tmp_path / "untrusted_repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".state").mkdir()
    (tmp_path / "innocent_state").symlink_to(repo / ".state")
    monkeypatch.chdir(repo)
    result = laya_autoinstall._state_dir({"XDG_STATE_HOME": str(tmp_path / "innocent_state")})
    assert result == laya_autoinstall._trusted_default_state_dir()


def test_account_home_ignores_the_home_env_var(monkeypatch, tmp_path):
    pwd = pytest.importorskip("pwd")
    monkeypatch.setenv("HOME", str(tmp_path / "poisoned"))
    assert _REAL_ACCOUNT_HOME() == Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_state_dir_without_an_account_entry_never_trusts_home(monkeypatch, tmp_path):
    # pwd present but the uid unmapped (arbitrary container uid) and $HOME poisoned into the
    # repo: the fallback must be a fresh private dir, never the refused $HOME.
    pwd = pytest.importorskip("pwd")

    def _no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", _no_entry)
    monkeypatch.setattr(laya_autoinstall, "_account_home", _REAL_ACCOUNT_HOME)
    monkeypatch.setattr(laya_autoinstall, "_EPHEMERAL_STATE_DIR", None)
    monkeypatch.setattr(laya_autoinstall.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    repo = tmp_path / "untrusted_repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    first = laya_autoinstall._state_dir({"HOME": str(repo / ".fakehome")})
    assert first.parent == tmp_path / "tmp"
    assert list(first.iterdir()) == []
    assert laya_autoinstall._state_dir({"HOME": str(repo / ".fakehome")}) == first  # stable per process


def test_state_dir_accepts_an_override_that_is_an_ancestor_of_the_repo(monkeypatch, tmp_path):
    # The usual production shape: $XDG_STATE_HOME under $HOME, the repo somewhere below $HOME.
    # An ANCESTOR of the repo is not inside it and must be honored.
    repo = tmp_path / "home" / "src" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    state_home = tmp_path / "home" / ".local" / "state"
    result = laya_autoinstall._state_dir({"XDG_STATE_HOME": str(state_home)})
    assert result == state_home / "task-cli" / "laya_autoinstall"


def test_neutral_cwd_fallback_never_lands_in_the_repo(monkeypatch, tmp_path):
    # State dir uncreatable AND $TMPDIR pointed into the untrusted repo: fall back to "/".
    repo = tmp_path / "untrusted_repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(laya_autoinstall.tempfile, "gettempdir", lambda: str(repo / "tmp"))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("", encoding="utf-8")  # mkdir under a regular file fails with OSError
    cwd = laya_autoinstall._neutral_subprocess_cwd({"XDG_STATE_HOME": str(blocker)})
    assert cwd == os.path.abspath(os.sep)


def test_dedicated_venv_install_waits_for_a_concurrent_holder_of_the_lock(monkeypatch, tmp_path, not_venv):
    # A second process must not `--clear` the venv while another is creating/installing it.
    fcntl = pytest.importorskip("fcntl")
    import threading

    venv_dir = tmp_path / "laya_venv"
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: venv_dir)
    monkeypatch.setattr(laya_autoinstall, "_resolve_trusted_uv", lambda: None)
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda path: None)
    started = threading.Event()

    def fake_run(argv, **kwargs):
        started.set()
        if argv[-2:] == ["-c", "import sys"]:
            return _FakeResult(0)
        return _FakeResult(0, stdout=f"{venv_dir}/lib/site-packages\n")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    holder = os.open(venv_dir.with_name(venv_dir.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    results = []
    worker = threading.Thread(target=lambda: results.append(laya_autoinstall.install()))
    worker.start()
    try:
        assert not started.wait(0.5)  # blocked on the lock: no subprocess ran
    finally:
        os.close(holder)
    worker.join(10)
    assert results == [(True, "installed (venv)")]


def test_venv_python_path_matches_platform_layout(monkeypatch):
    monkeypatch.setattr(laya_autoinstall.sys, "platform", "darwin")
    assert laya_autoinstall._venv_python_path(Path("/x")) == Path("/x/bin/python")
    monkeypatch.setattr(laya_autoinstall.sys, "platform", "win32")
    assert laya_autoinstall._venv_python_path(Path("/x")) == Path("/x/Scripts/python.exe")


def test_venv_site_marker_path_lives_inside_the_venv_dir():
    # INSIDE the venv dir (not a sibling): a `--clear` rebuild of a broken venv must drop the
    # stale marker along with everything else, automatically.
    marker = laya_autoinstall._venv_site_marker_path(Path("/x/laya_venv-py3.12"))
    assert marker.parent == Path("/x/laya_venv-py3.12")


def test_activate_venv_site_adds_the_resolved_site_packages_dir(monkeypatch):
    import site as real_site

    calls = []
    monkeypatch.setattr(real_site, "addsitedir", lambda path: calls.append(path))
    laya_autoinstall._activate_venv_site("/some/venv/site-packages")
    assert calls == ["/some/venv/site-packages"]


def test_activate_venv_site_never_raises_on_a_broken_site_module(monkeypatch):
    import site as real_site

    def _boom(path):
        raise RuntimeError("simulated site.addsitedir failure")

    monkeypatch.setattr(real_site, "addsitedir", _boom)
    laya_autoinstall._activate_venv_site("/whatever")  # must not raise


def test_venv_site_packages_resolves_via_sysconfig_subprocess(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(0, stdout="/venv/lib/python3.x/site-packages\n")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    result = laya_autoinstall._venv_site_packages(Path("/venv/bin/python"), timeout=5, cwd="/tmp")
    assert result == "/venv/lib/python3.x/site-packages"
    assert calls == [["/venv/bin/python", "-P", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]]


def test_venv_site_packages_takes_the_last_line_of_noisy_stdout(monkeypatch):
    monkeypatch.setattr(
        laya_autoinstall.subprocess,
        "run",
        lambda argv, **kw: _FakeResult(0, stdout="hello from a .pth\n/venv/lib/site-packages\n\n"),
    )
    assert laya_autoinstall._venv_site_packages(Path("/venv/bin/python"), timeout=5, cwd="/") == "/venv/lib/site-packages"


def test_probe_and_site_query_use_the_short_probe_timeout(monkeypatch):
    # A wedged venv interpreter must cost the short probe cap, not the full install budget.
    timeouts = []

    def fake_run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return _FakeResult(0, stdout="/x/site-packages\n")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", fake_run)
    laya_autoinstall._probe_python(Path("/x/bin/python"), timeout=180, cwd="/")
    laya_autoinstall._venv_site_packages(Path("/x/bin/python"), timeout=180, cwd="/")
    assert timeouts == [laya_autoinstall._PROBE_TIMEOUT_S] * 2
    assert laya_autoinstall._PROBE_TIMEOUT_S < 180


def test_venv_site_packages_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(1, stderr="broken")
    )
    assert laya_autoinstall._venv_site_packages(Path("/venv/bin/python"), timeout=5, cwd="/tmp") is None


def test_run_and_check_reports_success_failure_and_uncompleted(monkeypatch):
    monkeypatch.setattr(laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(0))
    assert laya_autoinstall._run_and_check(["x"], timeout=5) == (True, "")

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", lambda argv, **kw: _FakeResult(1, stderr="nope"))
    ok, detail = laya_autoinstall._run_and_check(["x"], timeout=5)
    assert ok is False and "nope" in detail

    def _timeout(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(laya_autoinstall.subprocess, "run", _timeout)
    ok, detail = laya_autoinstall._run_and_check(["x"], timeout=5)
    assert ok is False
    assert "complete" in detail


# ── _try_activate_cached_dedicated_venv() — the zero-subprocess reuse fast path ────────────


def test_try_activate_cached_dedicated_venv_succeeds_with_a_valid_marker(monkeypatch, tmp_path):
    venv_dir = tmp_path / "laya_venv"
    site_packages = venv_dir / "lib" / "site-packages"  # must resolve INSIDE the venv dir
    site_packages.mkdir(parents=True)
    marker = laya_autoinstall._venv_site_marker_path(venv_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(site_packages), encoding="utf-8")
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: venv_dir)
    activated = []
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda p: activated.append(p))
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: True)
    assert laya_autoinstall._try_activate_cached_dedicated_venv() is True
    assert activated == [str(site_packages)]


def test_try_activate_cached_dedicated_venv_returns_false_with_no_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: tmp_path / "missing")
    assert laya_autoinstall._try_activate_cached_dedicated_venv() is False


def test_try_activate_cached_dedicated_venv_returns_false_when_marker_points_nowhere(tmp_path, monkeypatch):
    venv_dir = tmp_path / "laya_venv"
    marker = laya_autoinstall._venv_site_marker_path(venv_dir)
    marker.parent.mkdir(parents=True)
    marker.write_text(str(tmp_path / "gone"), encoding="utf-8")
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: venv_dir)
    assert laya_autoinstall._try_activate_cached_dedicated_venv() is False


def test_try_activate_cached_dedicated_venv_returns_false_when_marker_points_outside_the_venv_dir(
    tmp_path, monkeypatch
):
    # Defense in depth (review finding): even a valid-looking, directory-pointing marker must
    # be refused if it resolves OUTSIDE the dedicated venv this module owns -- a tampered
    # marker or a hijacked sysconfig query must never get bolted onto sys.path via
    # site.addsitedir (which executes .pth-file import lines).
    venv_dir = tmp_path / "laya_venv"
    outside = tmp_path / "attacker_controlled"
    outside.mkdir()
    marker = laya_autoinstall._venv_site_marker_path(venv_dir)
    marker.parent.mkdir(parents=True)
    marker.write_text(str(outside), encoding="utf-8")
    monkeypatch.setattr(laya_autoinstall, "_dedicated_venv_dir", lambda env=None: venv_dir)
    activated = []
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda p: activated.append(p))
    assert laya_autoinstall._try_activate_cached_dedicated_venv() is False
    assert activated == []


def test_is_within_dedicated_venv():
    venv_dir = Path("/x/laya_venv-py3.12")
    assert laya_autoinstall._is_within_dedicated_venv(str(venv_dir / "lib/site-packages"), venv_dir) is True
    assert laya_autoinstall._is_within_dedicated_venv(str(venv_dir), venv_dir) is True
    assert laya_autoinstall._is_within_dedicated_venv("/x/attacker_controlled", venv_dir) is False
    assert laya_autoinstall._is_within_dedicated_venv("/x/laya_venv-py3.12-evil", venv_dir) is False


# ── ensure_installed() — the orchestrating entry point ──────────────────────────────────────


def test_ensure_installed_reuses_a_cached_dedicated_venv_with_zero_subprocesses(tmp_path, monkeypatch, capsys):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: False)
    monkeypatch.setattr(laya_autoinstall, "_try_activate_cached_dedicated_venv", lambda **kw: True)

    def _never_called(*a, **k):
        raise AssertionError("install() must not run when the cached venv reuse path succeeds")

    monkeypatch.setattr(laya_autoinstall, "install", _never_called)
    result = laya_autoinstall.ensure_installed(env=env)
    assert result is True
    assert capsys.readouterr().err == ""  # zero output on the fast reuse path


def test_ensure_installed_cached_venv_wins_over_an_active_cooldown(tmp_path, monkeypatch, capsys):
    # A working dedicated venv must stay usable even while a cooldown marker from an earlier
    # failed attempt (e.g. a lost concurrent rebuild race) is still fresh: the marker fast
    # path must run BEFORE the cooldown check.
    env = {"XDG_STATE_HOME": str(tmp_path)}
    laya_autoinstall.mark_failed_attempt(now=1_000_000.0, env=env)
    venv_dir = laya_autoinstall._dedicated_venv_dir(env)
    site_packages = venv_dir / "lib" / "site-packages"
    site_packages.mkdir(parents=True)
    laya_autoinstall._venv_site_marker_path(venv_dir).write_text(str(site_packages), encoding="utf-8")
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: False)
    activated = []
    monkeypatch.setattr(laya_autoinstall, "_activate_venv_site", lambda p: activated.append(p))
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: True)

    def _never_called(*a, **k):
        raise AssertionError("install() must not run when the cached venv reuse path succeeds")

    monkeypatch.setattr(laya_autoinstall, "install", _never_called)
    assert laya_autoinstall.ensure_installed(env=env, now=1_000_000.0 + 60) is True
    assert activated == [str(site_packages)]
    assert capsys.readouterr().err == ""


def test_ensure_installed_never_consults_the_venv_marker_when_already_in_a_virtualenv(
    tmp_path, monkeypatch
):
    # The dedicated-venv marker fast path is case-2-only (module docstring's DEDICATED VENV
    # note) -- review finding: nothing else guarantees this except the `not _in_virtualenv()`
    # guard itself, so pin it directly. A present, valid-looking marker must NOT short-circuit
    # install() while inside a virtualenv (case 1 installs straight into the ambient venv and
    # never touches the dedicated-venv machinery at all).
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "_in_virtualenv", lambda: True)

    def _marker_must_not_be_consulted(**kw):
        raise AssertionError("the dedicated-venv marker must never be consulted inside a virtualenv")

    monkeypatch.setattr(laya_autoinstall, "_try_activate_cached_dedicated_venv", _marker_must_not_be_consulted)
    monkeypatch.setattr(laya_autoinstall, "install", lambda **kw: (True, "installed"))
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: True)
    monkeypatch.setattr("importlib.invalidate_caches", lambda: None)
    result = laya_autoinstall.ensure_installed(env=env)
    assert result is True


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
    install_calls = []

    def fake_install(**kw):
        install_calls.append(kw)
        return True, "installed (venv)"

    monkeypatch.setattr(laya_autoinstall, "install", fake_install)
    monkeypatch.setattr(laya_autoinstall, "_laya_importable", lambda: True)
    invalidated = []
    monkeypatch.setattr("importlib.invalidate_caches", lambda: invalidated.append(True))
    result = laya_autoinstall.ensure_installed(env=env)
    assert result is True
    assert install_calls == [{"env": env}]  # env threaded through to install()
    assert invalidated == [True]
    err = capsys.readouterr().err
    assert "laya not installed; installing now" in err
    assert "installed (venv)" in err
    # a successful, genuinely-importable install never writes a cooldown marker
    assert not laya_autoinstall.cache_path(env).exists()


def test_ensure_installed_marks_failure_when_install_succeeds_but_package_stays_unimportable(
    tmp_path, monkeypatch, capsys
):
    # Review finding (carried over from the --user era): a successful install() does not
    # guarantee `import laya` works in THIS process (e.g. the site-packages bolt-on silently
    # failed) -- this must be treated as a FAILURE (cooldown marker written, `False` returned).
    env = {"XDG_STATE_HOME": str(tmp_path)}
    monkeypatch.setattr(laya_autoinstall, "install", lambda **kw: (True, "installed (venv)"))
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
    monkeypatch.setattr(laya_autoinstall, "install", lambda **kw: (False, "no network"))
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
    monkeypatch.setattr(laya_autoinstall, "install", lambda **kw: (False, "offline"))
    laya_autoinstall.ensure_installed(env=env, now=1_000_000.0)
    capsys.readouterr()  # discard first call's output

    def _never_called(*a, **k):
        raise AssertionError("install() must not run again within the cooldown window")

    monkeypatch.setattr(laya_autoinstall, "install", _never_called)
    result = laya_autoinstall.ensure_installed(env=env, now=1_000_000.0 + 60)
    assert result is False
    assert capsys.readouterr().err == ""


def test_ensure_installed_never_raises_on_an_unexpected_exception(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path)}

    def _boom(**kw):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(laya_autoinstall, "install", _boom)
    result = laya_autoinstall.ensure_installed(env=env, now=9_000_000.0)
    assert result is False
    assert laya_autoinstall.cache_path(env).exists()  # cooldown marked even on a weird failure
