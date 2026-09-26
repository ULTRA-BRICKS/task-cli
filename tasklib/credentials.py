"""Harvest provider credentials from existing CLI configs — no re-prompting (§1).

A user who already ran ``gh auth login`` / ``linear auth`` needs zero extra setup: task-cli
reads the same credentials those CLIs persist and calls the provider API directly. The CLI is
only needed for the one-time auth flow, never for task-cli's calls.

Resolution order:
  GitHub: ``$GITHUB_TOKEN`` / ``$GH_TOKEN`` → ``gh auth token`` (the reliable path; gh stores
          the real token in the OS keychain, not in hosts.yml) → ``~/.config/gh/hosts.yml``
          ``oauth_token`` (older gh, plaintext-token installs).
  Linear: ``$LINEAR_API_KEY`` → ``~/.config/linear/credentials.toml`` (the ``default``
          workspace's key, or a named workspace) → ``$XDG_CONFIG_HOME/linear/...``.

Tokens are returned, never logged or persisted by this module. The only subprocesses are the
one-shot ``gh auth token`` and, for an EXPIRED ``fj`` OAuth login, one ``fj -H <host> whoami``
(fj refreshes the token and rewrites its own store) — there is no per-API-call subprocess (that
is the whole point).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


class CredentialError(RuntimeError):
    """No usable credential could be harvested for a backend."""


@dataclass
class GitHubCreds:
    token: str
    source: str  # provenance, for diagnostics (never the token itself)


@dataclass
class LinearCreds:
    api_key: str
    workspace: str
    source: str


@dataclass
class TasksCreds:
    token: str
    source: str  # provenance, for diagnostics (never the token itself)


@dataclass
class ForgejoCreds:
    token: str
    source: str  # provenance, for diagnostics (never the token itself)


# ── GitHub ─────────────────────────────────────────────────────────────────────────


def github_token(env: dict[str, str] | None = None) -> GitHubCreds:
    """Resolve a GitHub token. Raises :class:`CredentialError` if none is available."""
    env = os.environ if env is None else env

    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = env.get(var)
        if val and val.strip():
            return GitHubCreds(token=val.strip(), source=f"env:{var}")

    tok = _gh_auth_token()
    if tok:
        return GitHubCreds(token=tok, source="gh auth token")

    tok = _gh_hosts_oauth_token(env)
    if tok:
        return GitHubCreds(token=tok, source="~/.config/gh/hosts.yml")

    raise CredentialError(
        "no GitHub token found. Run `gh auth login`, or set $GITHUB_TOKEN. "
        "(task-cli reads gh's stored credential; it never re-prompts.)"
    )


def _gh_auth_token() -> str | None:
    """Ask the ``gh`` CLI for the current token. Returns ``None`` if gh is absent/unauthed."""
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tok = out.stdout.strip()
    return tok or None


def _gh_hosts_oauth_token(env: dict[str, str]) -> str | None:
    """Read an ``oauth_token`` from ``hosts.yml`` (older gh installs that store it in plaintext).

    Modern gh keeps the token in the keychain and ``hosts.yml`` has no token, so this is a
    best-effort last-resort fallback (env and ``gh auth token`` are tried first). It is parsed
    with a tiny line scanner ON PURPOSE: the credential path must work even when pyyaml is
    absent (config parsing degrades to defaults without it, but credential harvest must not).
    """
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    path = Path(base) / "gh" / "hosts.yml"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("oauth_token:"):
                tok = stripped.split(":", 1)[1].strip().strip("'\"")
                if tok:
                    return tok
    except OSError:
        return None
    return None


# ── Linear ─────────────────────────────────────────────────────────────────────────


def linear_key(env: dict[str, str] | None = None, workspace: str | None = None) -> LinearCreds:
    """Resolve a Linear API key. Raises :class:`CredentialError` if none is available.

    ``workspace`` (if given) selects a specific named entry in ``credentials.toml``; otherwise
    the file's ``default`` pointer is followed.
    """
    env = os.environ if env is None else env

    val = env.get("LINEAR_API_KEY")
    if val and val.strip():
        return LinearCreds(api_key=val.strip(), workspace=workspace or "env", source="env:LINEAR_API_KEY")

    creds = _linear_credentials_toml(env, workspace)
    if creds:
        return creds

    raise CredentialError(
        "no Linear API key found. Run `linear auth` (or set $LINEAR_API_KEY). "
        "(task-cli reads linear's stored key; it never re-prompts.)"
    )


def _linear_credentials_toml(env: dict[str, str], workspace: str | None) -> LinearCreds | None:
    """Parse ``~/.config/linear/credentials.toml``.

    Format (observed): ``default = "<workspace>"`` plus ``<workspace> = "lin_api_..."`` lines.
    The ``default`` value names which workspace key to use unless ``workspace`` overrides it.
    """
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    path = Path(base) / "linear" / "credentials.toml"
    if not path.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    target = workspace or data.get("default")
    if isinstance(target, str) and target and target != "default":
        key = data.get(target)
        if isinstance(key, str) and key.strip():
            return LinearCreds(api_key=key.strip(), workspace=target, source="~/.config/linear/credentials.toml")
    # fall back: any value that looks like a Linear key
    for name, key in data.items():
        if name == "default":
            continue
        if isinstance(key, str) and key.startswith("lin_api_"):
            return LinearCreds(api_key=key.strip(), workspace=str(name), source="~/.config/linear/credentials.toml")
    return None


# ── Tasks ──────────────────────────────────────────────────────────────────────────


def tasks_token(url: str, env: dict[str, str] | None = None) -> TasksCreds:
    """Resolve a Tasks API token for the (already origin-validated) ``url``. Raises
    :class:`CredentialError` if none is available.

    Resolution order: ``$TASKS_TOKEN`` (with ``$TASKS_URL``) → ``$TICKETS_TOKEN`` (the native
    ``tasks-app`` CLI's own env var, with ``$TICKETS_URL``) → the ``tasks-app`` CLI's own
    persisted credential file. Every source is origin-bound: an env token is used only when its
    paired URL variable names the SAME origin as ``url``, and the file's token only when its
    stored ``url`` does. ``tasks.url`` comes from repository config, so a cloned repository must
    not be able to aim an ambient token at its own host (UB-25 review).
    """
    env = os.environ if env is None else env

    for var, url_var in (("TASKS_TOKEN", "TASKS_URL"), ("TICKETS_TOKEN", "TICKETS_URL")):
        val = env.get(var)
        if val and val.strip() and _same_origin_str(str(env.get(url_var, "")).strip(), url):
            return TasksCreds(token=val.strip(), source=f"env:{var}")

    creds = _tasks_app_cli_config(env, url)
    if creds:
        return creds

    raise CredentialError(
        f"no Tasks API token found for {url}. Run `tasks-app auth login --url {url}` "
        f"(or set $TASKS_TOKEN together with TASKS_URL={url}). (task-cli reads the tasks-app "
        "CLI's stored credential; it never re-prompts.)"
    )


def _tasks_app_cli_config(env: dict[str, str], url: str) -> TasksCreds | None:
    """Read the native ``tasks-app`` CLI's own credential file (``config.json``, mode 0600),
    reusing its token ONLY when the file's own stored ``url`` is the same origin as ``url``.

    The file holds a single ``{"url": ..., "token": ...}`` pair (whichever workspace the user
    last ran ``tasks-app auth login`` against) — NOT keyed per-origin itself, so the origin
    check here is what keeps a token minted for one tasks-app deployment from being replayed
    against a different ``url`` this backend happens to be configured for.
    """
    base = env.get("TICKETS_CONFIG_DIR") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".config", "tickets-app"
    )
    path = Path(base) / "config.json"
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stored_url = str(data.get("url", "")).strip()
    stored_token = str(data.get("token", "")).strip()
    if not stored_token or not _same_origin_str(stored_url, url):
        return None
    return TasksCreds(token=stored_token, source=f"{path}")


# ── Forgejo ────────────────────────────────────────────────────────────────────────


def forgejo_token(url: str, env: dict[str, str] | None = None) -> ForgejoCreds:
    """Resolve a Forgejo API token for the (already validated) origin ``url``. Raises
    :class:`CredentialError` if none is available.

    Order: ``$FORGEJO_TOKEN`` — used ONLY when ``$FORGEJO_URL`` names this same origin, because
    ``forgejo.url`` comes from repository config and a cloned repository must not be able to aim
    an ambient token at its own host — then the ``fj`` CLI's own store (``keys.json``), whose
    ``hosts`` map is keyed by host name (``host:port`` for a non-default port). Only the entry
    for THIS origin's host is ever used, and fj's ``aliases`` map is deliberately ignored:
    following an alias would send one host's token to a different host name.

    ``fj auth login`` stores an OAuth login whose access token expires after about an hour; fj
    refreshes it only when fj itself runs. An expired entry therefore triggers ONE
    ``fj -H <host> whoami`` (fj refreshes + rewrites ``keys.json``) and a re-read; if fj is
    absent or that does not yield a fresh token, the usual "no Forgejo token" error is raised.
    """
    env = os.environ if env is None else env
    val = env.get("FORGEJO_TOKEN")
    if val and val.strip() and _same_origin_str(str(env.get("FORGEJO_URL", "")).strip(), url):
        return ForgejoCreds(token=val.strip(), source="env:FORGEJO_TOKEN")

    key = _forgejo_host_key(url)
    expired = False
    found = _fj_store_entry(key, env) if key else None
    if found and _fj_entry_expired(found[1]):
        expired = True
        _fj_refresh(key, env)
        # Re-read even when fj failed: a concurrent task/fj run may have refreshed it already.
        found = _fj_store_entry(key, env)
        if found and _fj_entry_expired(found[1]):
            found = None
    if found:
        path, entry = found
        return ForgejoCreds(token=entry["token"].strip(), source=str(path))

    hint = (
        f" (the stored fj OAuth login for {key} has expired and `fj -H {key} whoami` could not "
        "refresh it)"
        if expired
        else ""
    )
    raise CredentialError(
        f"no Forgejo token found for {key or url}{hint}. Run `fj -H {key or 'HOST'} auth login` "
        f"(or set $FORGEJO_TOKEN together with FORGEJO_URL={url}). (task-cli reads fj's stored "
        "credential for that exact host; it never re-prompts.)"
    )


def forgejo_login_known(url: str, env: dict[str, str] | None = None) -> bool:
    """Whether the user has a Forgejo credential for exactly ``url``'s origin — the origin-bound
    env pair or an fj store entry for that host, EXPIRED OR NOT. Observational: no subprocess and
    no network (auto-detection runs during generic CLI setup), unlike :func:`forgejo_token`,
    which refreshes an expired fj OAuth login."""
    env = os.environ if env is None else env
    val = env.get("FORGEJO_TOKEN")
    if val and val.strip() and _same_origin_str(str(env.get("FORGEJO_URL", "")).strip(), url):
        return True
    key = _forgejo_host_key(url)
    return bool(key and _fj_store_entry(key, env))


def _forgejo_host_key(url: str) -> str:
    """fj's ``hosts`` key for ``url``: ``host`` on the default port, else ``host:port``."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    return f"{host}:{port}" if host and port and port != 443 else host


def _fj_key_files(env: dict[str, str]) -> list[Path]:
    """Candidate ``keys.json`` locations — fj's ``directories::ProjectDirs("", "forgejo-cli",
    "forgejo-cli").data_dir()``, the running platform's own location first: Windows
    ``%APPDATA%\\forgejo-cli\\forgejo-cli\\data``, macOS ``~/Library/Application Support/
    forgejo-cli.forgejo-cli`` (both verified against fj 0.6), Linux ``$XDG_DATA_HOME/forgejo-cli``."""
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    data_home = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    mac = home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    xdg = data_home / "forgejo-cli" / "keys.json"
    appdata = env.get("APPDATA")
    windows = [Path(appdata) / "forgejo-cli" / "forgejo-cli" / "data" / "keys.json"] if appdata else []
    if sys.platform == "win32":
        return [*windows, mac, xdg]
    if sys.platform == "darwin":
        return [mac, xdg, *windows]
    return [xdg, mac, *windows]


def _fj_store_entry(host_key: str, env: dict[str, str]) -> tuple[Path, dict] | None:
    """The first key-store ``(path, entry)`` holding a usable token for exactly ``host_key``."""
    for path in _fj_key_files(env):
        entry = _fj_host_entry(path, host_key)
        if entry is not None:
            return path, entry
    return None


def _fj_host_entry(path: Path, host_key: str) -> dict | None:
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    entry = hosts.get(host_key) if isinstance(hosts, dict) else None
    token = entry.get("token") if isinstance(entry, dict) else None
    return entry if isinstance(token, str) and token.strip() else None


def _fj_expires_at(value: object) -> datetime | None:
    """Parse fj's ``expires_at`` (a Rust ``time::OffsetDateTime``). fj serialises it with the
    ``time`` crate's default serde form, a 9-tuple ``[year, ordinal_day, hour, minute, second,
    nanosecond, offset_h, offset_m, offset_s]``; a human-readable/RFC 3339 string or a unix
    timestamp are accepted too. ``None`` when unrecognised."""
    try:
        if isinstance(value, list) and len(value) == 9 and all(type(v) is int for v in value):
            year, ordinal, hour, minute, second, nanos, off_h, off_m, off_s = value
            local = datetime(year, 1, 1) + timedelta(
                days=ordinal - 1, hours=hour, minutes=minute, seconds=second, microseconds=nanos // 1000
            )
            offset = timedelta(hours=off_h, minutes=off_m, seconds=off_s)
            return (local - offset).replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            parts = text.split(" ")
            if len(parts) == 3:  # time's human-readable form: "2026-09-25 20:05:01.5 +00:00:00"
                sign = -1 if parts[2].startswith("-") else 1
                h, m, sec = (int(x) for x in parts[2].lstrip("+-").split(":"))
                naive = datetime.fromisoformat(f"{parts[0]}T{parts[1][:15]}")
                return (naive - sign * timedelta(hours=h, minutes=m, seconds=sec)).replace(tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return None


def _fj_entry_expired(entry: dict) -> bool:
    """Whether an OAuth entry's access token is past its ``expires_at``. Application tokens (and an
    unrecognised timestamp) are treated as live — the server stays the authority on those."""
    if entry.get("type") != "OAuth":
        return False
    expires = _fj_expires_at(entry.get("expires_at"))
    return expires is not None and datetime.now(timezone.utc) >= expires


def find_fj(path_value: str) -> str | None:
    """The first ``fj`` in the ABSOLUTE directories of ``path_value`` — never the working directory.

    ``shutil.which`` on Windows (Python <= 3.13) searches the working directory first, and task
    runs inside repositories it does not control: a repo's own ``fj.bat`` must never run with the
    user's credentials. Windows looks for the PATHEXT names (rig's ``fj.cmd`` wrapper, ``fj.exe``);
    POSIX for ``fj``.
    """
    if sys.platform == "win32":
        names = ["fj" + ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    else:
        names = ["fj"]
    for directory in path_value.split(os.pathsep):
        if not directory or not os.path.isabs(directory):  # "" and "." mean the working directory
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def _fj_refresh(host_key: str, env: dict[str, str]) -> None:
    """Run ``fj -H <host> whoami`` once so fj refreshes its expired OAuth token and rewrites its
    own ``keys.json``. Output (which names the account) is discarded; a missing or failing fj is
    not an error here — the caller re-reads the store and decides. Only ``env``'s own PATH is
    searched: a scoped env without one finds no fj (never the ambient one)."""
    fj = find_fj(env.get("PATH", ""))
    if not fj:
        return
    run_env = None if env is os.environ else {**os.environ, **env}
    try:
        subprocess.run(
            [fj, "-H", host_key, "whoami"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=run_env,
            timeout=15,  # refresh failure is soft; a wedged fj must not stall every task command
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _same_origin_str(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` are the same origin (scheme + host + effective port).

    A small, self-contained comparison (not delegated to ``tasklib.backends.http``) so this
    module — imported before a backend is even chosen — never has to import the network layer
    just to compare two strings.
    """
    from urllib.parse import urlsplit

    default_ports = {"https": 443, "http": 80}
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        # `.port` validates lazily and raises on a malformed port: fail closed, never crash
        port_a = pa.port if pa.port is not None else default_ports.get(pa.scheme)
        port_b = pb.port if pb.port is not None else default_ports.get(pb.scheme)
    except ValueError:
        return False
    return (
        pa.scheme == pb.scheme
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and port_a == port_b
    )
