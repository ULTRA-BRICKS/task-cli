"""credentials.py — harvest GitHub/Linear creds from env + existing CLI configs.

The ``gh auth token`` subprocess is monkeypatched; no real CLI is invoked.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import pytest

from tasklib import credentials
from tasklib.credentials import (
    CredentialError,
    forgejo_login_known,
    forgejo_token,
    github_token,
    linear_key,
)


def test_github_token_from_env():
    creds = github_token({"GITHUB_TOKEN": "ghp_envtoken"})
    assert creds.token == "ghp_envtoken"
    assert creds.source == "env:GITHUB_TOKEN"


def test_github_token_from_gh_cli(monkeypatch):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: "gho_fromcli")
    creds = github_token({})
    assert creds.token == "gho_fromcli"
    assert creds.source == "gh auth token"


def test_github_token_from_hosts_yml(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: None)
    gh = tmp_path / "gh"
    gh.mkdir()
    (gh / "hosts.yml").write_text(
        "github.com:\n    oauth_token: ghp_fromhosts\n    user: x\n", encoding="utf-8"
    )
    creds = github_token({"XDG_CONFIG_HOME": str(tmp_path)})
    assert creds.token == "ghp_fromhosts"


def test_github_token_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "_gh_auth_token", lambda: None)
    with pytest.raises(CredentialError):
        github_token({"XDG_CONFIG_HOME": str(tmp_path)})


def test_linear_key_from_env():
    creds = linear_key({"LINEAR_API_KEY": "lin_api_env"})
    assert creds.api_key == "lin_api_env"


def test_linear_key_follows_default_pointer(tmp_path):
    lin = tmp_path / "linear"
    lin.mkdir()
    (lin / "credentials.toml").write_text(
        'default = "glide-vc"\nglide-vc = "lin_api_realkey123456789"\n', encoding="utf-8"
    )
    creds = linear_key({"XDG_CONFIG_HOME": str(tmp_path)})
    assert creds.api_key == "lin_api_realkey123456789"
    assert creds.workspace == "glide-vc"


def test_linear_key_named_workspace_override(tmp_path):
    lin = tmp_path / "linear"
    lin.mkdir()
    (lin / "credentials.toml").write_text(
        'default = "a"\na = "lin_api_aaa1111111111111"\nb = "lin_api_bbb2222222222222"\n', encoding="utf-8"
    )
    creds = linear_key({"XDG_CONFIG_HOME": str(tmp_path)}, workspace="b")
    assert creds.api_key == "lin_api_bbb2222222222222"


def test_linear_key_missing_raises(tmp_path):
    with pytest.raises(CredentialError):
        linear_key({"XDG_CONFIG_HOME": str(tmp_path)})


# ── Forgejo: fj key store location + expired OAuth refresh ────────────────────────────

FORGE = "https://forge.example.test"
PAST = [2020, 1, 0, 0, 0, 0, 0, 0, 0]  # fj's time-crate serde tuple: 2020-01-01T00:00:00Z
FUTURE = [2999, 1, 0, 0, 0, 0, 0, 0, 0]

# A stand-in `fj`: logs its argv; in `refresh` mode rewrites the store the way the real fj
# does after refreshing an OAuth token, `noop` succeeds without refreshing, `fail` exits 1.
_FAKE_FJ = """
import json, os, sys
with open(os.environ["FAKE_FJ_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + chr(10))
mode = os.environ.get("FAKE_FJ_MODE", "refresh")
if mode == "fail":
    sys.exit(1)
if mode == "refresh":
    path = os.environ["FAKE_FJ_KEYS"]
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    data["hosts"][sys.argv[2]].update(token="fresh-value", expires_at=[2999, 1, 0, 0, 0, 0, 0, 0, 0])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
"""


def _windows_store(tmp_path, entry, host="forge.example.test"):
    keys = tmp_path / "appdata" / "forgejo-cli" / "forgejo-cli" / "data" / "keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({"hosts": {host: entry}, "aliases": {}}), encoding="utf-8")
    return keys


def _fake_fj(tmp_path, keys, mode="refresh"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if os.name == "nt":
        script = tmp_path / "fake_fj.py"
        script.write_text(_FAKE_FJ, encoding="utf-8")
        (bindir / "fj.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / "fj"
        exe.write_text(f"#!{sys.executable}\n" + _FAKE_FJ, encoding="utf-8")
        exe.chmod(0o755)
    log = tmp_path / "fj.log"
    env = {
        "HOME": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "appdata"),
        "PATH": str(bindir),
        "FAKE_FJ_LOG": str(log),
        "FAKE_FJ_KEYS": str(keys),
        "FAKE_FJ_MODE": mode,
    }
    return env, log


def _fj_calls(log):
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]


def _oauth(token, expires_at):
    return {"type": "OAuth", "token": token, "refresh_token": "r", "expires_at": expires_at}


def test_forgejo_token_reads_the_windows_fj_store(tmp_path):
    _windows_store(tmp_path, {"type": "Application", "token": "app-value"})
    env = {"HOME": str(tmp_path / "home"), "APPDATA": str(tmp_path / "appdata")}
    creds = forgejo_token(FORGE, env)
    assert creds.token == "app-value"
    assert creds.source.endswith("keys.json") and "app-value" not in creds.source


@pytest.mark.parametrize(
    ("platform", "first"),
    [("win32", "appdata"), ("darwin", "Application Support"), ("linux", "share")],
)
def test_fj_key_files_put_the_platform_store_first(monkeypatch, tmp_path, platform, first):
    monkeypatch.setattr(credentials.sys, "platform", platform)
    env = {
        "HOME": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "appdata"),
        "XDG_DATA_HOME": str(tmp_path / "share"),
    }
    files = credentials._fj_key_files(env)
    assert first in str(files[0])
    assert len(files) == 3


def test_expired_oauth_login_is_refreshed_via_fj_whoami(tmp_path):
    keys = _windows_store(tmp_path, _oauth("stale-value", PAST))
    env, log = _fake_fj(tmp_path, keys)
    creds = forgejo_token(FORGE, env)
    assert creds.token == "fresh-value"
    assert _fj_calls(log) == [["-H", "forge.example.test", "whoami"]]


def test_live_oauth_login_does_not_run_fj(tmp_path):
    keys = _windows_store(tmp_path, _oauth("live-value", FUTURE))
    env, log = _fake_fj(tmp_path, keys)
    assert forgejo_token(FORGE, env).token == "live-value"
    assert _fj_calls(log) == []


@pytest.mark.parametrize("mode", ["fail", "noop"])
def test_expired_oauth_login_fj_cannot_refresh_raises_no_token_error(tmp_path, mode):
    keys = _windows_store(tmp_path, _oauth("stale-value", PAST))
    env, log = _fake_fj(tmp_path, keys, mode=mode)
    with pytest.raises(CredentialError, match=r"no Forgejo token found for forge\.example\.test.*expired") as exc:
        forgejo_token(FORGE, env)
    assert "fj -H forge.example.test auth login" in str(exc.value)
    assert "stale-value" not in str(exc.value)
    assert len(_fj_calls(log)) == 1  # exactly one refresh attempt


def test_expired_oauth_login_without_fj_raises_no_token_error(tmp_path):
    _windows_store(tmp_path, _oauth("stale-value", PAST))
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    env = {"HOME": str(tmp_path / "home"), "APPDATA": str(tmp_path / "appdata"), "PATH": str(empty)}
    with pytest.raises(CredentialError, match="no Forgejo token found"):
        forgejo_token(FORGE, env)


def test_login_known_counts_an_expired_login_without_running_fj(tmp_path):
    keys = _windows_store(tmp_path, _oauth("stale-value", PAST))
    env, log = _fake_fj(tmp_path, keys)
    assert forgejo_login_known(FORGE, env) is True
    assert forgejo_login_known("https://other.example.test", env) is False
    assert _fj_calls(log) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            [2026, 268, 20, 5, 1, 500_000_000, 0, 0, 0],
            datetime(2026, 9, 25, 20, 5, 1, 500_000, tzinfo=timezone.utc),
        ),
        ([2026, 268, 23, 5, 1, 0, 3, 0, 0], datetime(2026, 9, 25, 20, 5, 1, tzinfo=timezone.utc)),
        (
            "2026-09-25 20:05:01.5 +00:00:00",
            datetime(2026, 9, 25, 20, 5, 1, 500_000, tzinfo=timezone.utc),
        ),
        ("2026-09-25T20:05:01Z", datetime(2026, 9, 25, 20, 5, 1, tzinfo=timezone.utc)),
        (1790366701, datetime(2026, 9, 25, 20, 5, 1, tzinfo=timezone.utc)),
        ("garbage", None),
        ([1, 2, 3], None),
        (None, None),
    ],
)
def test_fj_expires_at_parsing(value, expected):
    assert credentials._fj_expires_at(value) == expected


def test_find_fj_never_uses_the_working_directory(tmp_path, monkeypatch):
    """task runs inside repos it doesn't control: a repo's own fj (fj.bat on Windows, where
    shutil.which searches the working directory first) must never be the one refreshed with."""
    names = ["fj.bat", "fj.cmd", "fj.exe"] if sys.platform == "win32" else ["fj"]
    repo = tmp_path / "some-repo"
    repo.mkdir()
    for name in names:
        (repo / name).write_text("#!/bin/sh\necho pwned\n")
        (repo / name).chmod(0o755)
    monkeypatch.chdir(repo)
    assert credentials.find_fj(os.pathsep.join(["", ".", "relative/bin"])) is None
    real = tmp_path / "bin"
    real.mkdir()
    (real / names[-1]).write_text("#!/bin/sh\n")
    (real / names[-1]).chmod(0o755)
    assert credentials.find_fj(os.pathsep.join([".", str(real)])) == str(real / names[-1])
