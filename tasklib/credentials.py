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

Tokens are returned, never logged or persisted by this module. The only subprocess is the
one-shot ``gh auth token`` — there is no per-API-call subprocess (that is the whole point).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
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

    Resolution order mirrors GitHub/Linear above: ``$TASKS_TOKEN`` (task-cli's own env var,
    the name ``get_backend`` documents) → ``$TICKETS_TOKEN`` (the native ``tasks-app`` CLI's
    own env var — reusing it means a shell that already exports it for that CLI needs no
    second variable) → the ``tasks-app`` CLI's own persisted credential file, reused ONLY when
    its stored ``url`` is the SAME origin as ``url`` (origin-bound: a token minted for a
    different tasks-app deployment must never be sent to this one).
    """
    env = os.environ if env is None else env

    for var in ("TASKS_TOKEN", "TICKETS_TOKEN"):
        val = env.get(var)
        if val and val.strip():
            return TasksCreds(token=val.strip(), source=f"env:{var}")

    creds = _tasks_app_cli_config(env, url)
    if creds:
        return creds

    raise CredentialError(
        f"no Tasks API token found for {url}. Run `tasks-app auth login --url {url}` "
        "(or set $TASKS_TOKEN). (task-cli reads the tasks-app CLI's stored credential; "
        "it never re-prompts.)"
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


def _same_origin_str(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` are the same origin (scheme + host + effective port).

    A small, self-contained comparison (not delegated to ``tasklib.backends.http``) so this
    module — imported before a backend is even chosen — never has to import the network layer
    just to compare two strings.
    """
    from urllib.parse import urlsplit

    try:
        pa, pb = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    default_ports = {"https": 443, "http": 80}
    port_a = pa.port if pa.port is not None else default_ports.get(pa.scheme)
    port_b = pb.port if pb.port is not None else default_ports.get(pb.scheme)
    return (
        pa.scheme == pb.scheme
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and port_a == port_b
    )
