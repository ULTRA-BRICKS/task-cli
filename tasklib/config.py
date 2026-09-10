"""Config cascade loader for the per-repo ``rig.yaml`` / ``task.yaml`` + global config.

Layers, cascaded by **location** (no scope flag), exactly like rig-cli — later layers win:

1. **Built-in defaults** — GitHub Issues, every gate on (the zero-config path).
2. **Global** — ``~/.config/task-cli/config.yaml`` (or ``$XDG_CONFIG_HOME/...``). Machine-wide
   defaults a developer carries across repos.
3. **Per-repo ``rig.yaml`` ``task:`` block** — the repo's committed ``rig.yaml`` is the single
   source of truth for the whole agent toolchain (rig provisions it). Its ``task:`` block
   selects the tracker backend per repo (CTO decision #4136.4: hyperide → Linear/HYP, every
   other repo → GitHub Issues by default). See :func:`rig_task_overlay` for the shape.
4. **Per-repo ``task.yaml``** — the native task-cli config, if a repo keeps one. It still wins
   over ``rig.yaml`` so an existing repo with a hand-tuned ``task.yaml`` is untouched.
5. **``--config P``** — an explicit file replaces the ``task.yaml`` layer.

The merge is a deep dict merge: later keys win, dicts merge recursively, scalars and lists
replace wholesale. ``yaml`` is imported lazily so ``task --help`` works without PyYAML; the
loader degrades to built-in defaults if no config file is present (so the tool works with
**zero config** on any GitHub repo — and a repo with only a ``rig.yaml`` task: block needs no
``task.yaml`` at all).

Validation is fail-closed on the few enum-ish keys (backend, version), lenient on the rest —
this is a personal-tooling config, not a hostile-input parser.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "task.yaml"
RIG_CONFIG_FILENAME = "rig.yaml"

VALID_BACKENDS = {"github-issues", "linear"}

# How a screenshot/file gets attached to a ticket (tg#11652, CTO decision — originally Linear-
# only; extended to GitHub too by tg#16794/review-cli#484, see backends/github_issues.py's module
# docstring for why GitHub needed the identical knob):
#   "native" — upload the bytes through the backend's OWN authenticated API (Linear:
#              fileUpload + attachmentCreate, backends/linear.py; GitHub:
#              uploads.github.com/user-attachments/assets, backends/github_issues.py) so the
#              ticket carries a real, durably-hosted asset with an inline preview. This is the
#              DEFAULT for both backends: it is the properly-fixed path (no 401 for Linear — see
#              the module docstring on backends/linear.py for why the earlier "any non-browser
#              consumer 401s" finding does NOT apply here; GitHub's upload endpoint needs no such
#              caveat, it's a plain bearer-token POST).
#   "link"   — never touch the backend's upload API for a local file. If the ref is ALREADY a
#              hosted URL (e.g. a GitHub PR asset, or a PR's `#screenshots` anchor) it is
#              registered as an external-link attachment as-is. A bare local path in this mode
#              has nothing to link to and is skipped (best-effort, see `_attach_screenshots`'s
#              existing swallow) or raises `BackendError` (a direct `backend.attach()` call, e.g.
#              `task attach`/`task check --proof`). hyperide pins this mode for Linear (HYP-1248):
#              raw pasted-image `uploads.linear.app` URLs are session-scoped and 401 for a
#              non-browser viewer, so hyperide's own mitigation is to link out to the PR's
#              `#screenshots` section instead of embedding/uploading an image.
VALID_ATTACHMENT_MODES = {"link", "native"}

# Highest-precedence override for BOTH `linear.attachment_mode` and `github.attachment_mode` —
# set on ONE machine to beat even a repo-committed pin (hyperide's `task.yaml` pins "link" for
# everyone, HYP-1248; tg#11652 asked for a personal override without touching that shared file).
# Mirrors this ecosystem's other per-machine env overrides (RIG_TMUX_DRY_RUN, RIG_TG_CTL_DRY_RUN).
ATTACHMENT_MODE_ENV_VAR = "TASK_CLI_ATTACHMENT_MODE"

# A single DNS label: alnum, optionally hyphenated, never starting/ending in a hyphen (RFC 1123).
_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# A label that's purely numeric (any getaddrinfo-legacy base): plain decimal digits, or a
# `0x`-prefixed hex literal. See the trusted_attachment_hosts IP-literal-bypass check in
# validate() for why this needs to be checked per-LABEL, not on the whole string.
_NUMERIC_OR_HEX_LABEL_RE = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before any backend call)."""


def global_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    return Path(base) / "task-cli" / "config.yaml"


def repo_config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_FILENAME


def rig_config_path(repo_root: Path) -> Path:
    return repo_root / RIG_CONFIG_FILENAME


# Aliases in the rig.yaml ``task:`` block → the dotted path(s) they map onto in task-cli's own
# config shape. The block is intentionally flat (a CTO drops `task: {backend: linear, team:
# HYP}` into rig.yaml without learning task-cli's nesting), so we translate the shorthands.
# ``team``/``project`` are Linear coordinates; ``repo`` is the GitHub coordinate;
# ``attachment_mode`` is the screenshot-attach strategy (see ``VALID_ATTACHMENT_MODES``) — it
# fans out onto BOTH backends' sections (tg#16794/review-cli#484: extended from Linear-only to
# backend-agnostic), since whichever backend is actually active reads its own section and
# setting the value on the inactive one is harmless. Every other alias here maps to exactly one
# path, so the value type is a tuple of paths (each itself a tuple of path segments) rather than
# a single path, to accommodate the one multi-target alias without a second shape.
_RIG_TASK_ALIASES: dict[str, tuple[tuple[str, ...], ...]] = {
    "team": (("linear", "team"),),
    "project": (("linear", "project"),),
    "repo": (("github", "repo"),),
    "attachment_mode": (("linear", "attachment_mode"), ("github", "attachment_mode")),
}
# Keys passed through verbatim (same name in both shapes): the backend selector + the nested
# sections a power user might want to override straight from rig.yaml. ``version`` is
# intentionally NOT here — that is task-cli's own config-schema version (rig.yaml carries its
# OWN top-level version), and passing it through would make it fail-closed, so a future rig
# bumping a stray task.version would crash an older task-cli — the opposite of forward-compat.
_RIG_TASK_PASSTHROUGH = frozenset(
    {
        "backend",
        "github",
        "linear",
        "projects",
        "enforce",
        "classify",
        "session",
        # review finding: omitting this contradicted the README's own claim that the "full
        # native shape... nested under rig.yaml task:" includes trusted_attachment_hosts — a
        # rig-provisioned repo (the canonical path — rig writes this block, per AGENTS.md)
        # setting it there was silently warned-and-ignored by rig_task_overlay, exactly the
        # "silently never-match" failure this whole feature exists to eliminate.
        "trusted_attachment_hosts",
    }
)


def _nested_override_set(block: dict[str, Any], path: tuple[str, ...]) -> bool:
    """Whether ``block`` sets ``path`` directly through NESTED (passthrough) dicts — e.g.
    ``block = {"github": {"attachment_mode": "link"}}``, ``path = ("github",
    "attachment_mode")`` → ``True``. Used by a fan-out alias (see :func:`rig_task_overlay`) to
    tell "the author wrote an explicit override for this exact target" apart from "nothing else
    touches this leaf" — only the former should suppress the alias's own write to that path.
    """
    cursor: Any = block
    for part in path[:-1]:
        if not isinstance(cursor, dict):
            return False
        cursor = cursor.get(part)
    return isinstance(cursor, dict) and path[-1] in cursor


def rig_task_overlay(rig_data: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``rig.yaml`` ``task:`` block into a task-cli config overlay.

    The block is the per-repo tracker selection the CTO drops into a repo's ``rig.yaml``::

        task:
          backend: linear      # or github-issues (the default)
          team: HYP            # → linear.team
          project: ""          # → linear.project
          # repo: owner/name   # → github.repo (for the github-issues backend)

    Returns ``{}`` when there is no ``task:`` block, so a ``rig.yaml`` that says nothing about
    the tracker leaves the cascade untouched (clean fall-through to the github default). The
    same for a ``rig.yaml`` whose document root is not a mapping (defensive — ``_load_yaml``
    already guards this, but this keeps the function safe on any caller). A non-mapping
    ``task:`` is a config error (fail-closed before any backend call).

    Unknown sub-keys inside ``task:`` are **warned-and-ignored**, not fatal: ``rig.yaml`` is
    owned and evolved by rig-cli, so a sub-key a newer rig writes must not crash every ``task``
    command in a repo just because this task-cli predates it (forward-compat across two tools
    that ship independently). This mirrors the lenient handling of the native ``task.yaml``.
    """
    if not isinstance(rig_data, dict):
        return {}
    block = rig_data.get("task")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError(f"'task' block in {RIG_CONFIG_FILENAME} must be a mapping, got {type(block).__name__}")

    # Each key contributes a fragment; we deep-merge them so a flat shorthand (team: HYP →
    # {linear: {team}}) and a same-named nested section (linear: {project}) in the SAME block
    # combine instead of clobbering. Order-independent: deep-merge is commutative on disjoint
    # leaves, and a true leaf collision (same dotted path set twice) is caught explicitly.
    import copy

    overlay: dict[str, Any] = {}
    for key, value in block.items():
        if key in _RIG_TASK_ALIASES:
            # Fan out onto every target path the alias maps to (only `attachment_mode` maps to
            # more than one — see `_RIG_TASK_ALIASES`'s own comment); each path builds its own
            # nested cursor into the SAME fragment dict, so disjoint paths (e.g. `linear.*` and
            # `github.*`) combine into one fragment instead of clobbering each other.
            paths = _RIG_TASK_ALIASES[key]
            fragment = {}
            for path in paths:
                # review finding (PR #184): a fan-out alias (more than one target path) must
                # defer to an explicit NESTED override at that SAME path, not collide with it —
                # `attachment_mode: native` alongside `github: {attachment_mode: link}` means
                # "native by default, link specifically for github", the same thing it meant
                # before this alias fanned out to github at all. Skipping this path here means
                # the passthrough branch below writes it alone, so `_assert_no_leaf_conflict`
                # never even sees two writers for it. A single-path alias (team/project/repo)
                # never takes this branch — a genuine collision there still hard-errors, as
                # before this fix.
                if len(paths) > 1 and _nested_override_set(block, path):
                    continue
                *parents, leaf = path
                cursor = fragment
                for part in parents:
                    cursor = cursor.setdefault(part, {})
                cursor[leaf] = value
        elif key in _RIG_TASK_PASSTHROUGH:
            fragment = {key: copy.deepcopy(value)} if isinstance(value, dict) else {key: value}
        else:
            # Forward-compat: a sub-key a newer rig-cli writes that this task-cli doesn't know.
            # Skip it (don't crash); log so a genuine typo is still discoverable (TASK_LOG=json).
            from .logging import log_event

            log_event("rig.task: ignoring unknown key", level="WARN", key=str(key))
            continue
        _assert_no_leaf_conflict(overlay, fragment, prefix=f"{RIG_CONFIG_FILENAME} task")
        overlay = _deep_merge(overlay, fragment)
    return overlay


def _assert_no_leaf_conflict(base: dict[str, Any], over: dict[str, Any], *, prefix: str) -> None:
    """Raise ConfigError if ``over`` would overwrite a leaf that ``base`` already set.

    Deep-merge silently lets a later scalar replace an earlier one; for the ``task:`` block we
    want a true collision (e.g. ``team: HYP`` AND ``linear: {team: X}`` both setting
    ``linear.team``) to be a hard error, not a last-writer-wins surprise.
    """
    for k, v in over.items():
        if k not in base:
            continue
        bv = base[k]
        if isinstance(bv, dict) and isinstance(v, dict):
            _assert_no_leaf_conflict(bv, v, prefix=f"{prefix}.{k}")
        elif bv != v:
            raise ConfigError(f"conflicting value for '{prefix}.{k}' in {RIG_CONFIG_FILENAME}")


# Built-in defaults — what the tool uses with zero config (GitHub Issues, everything enforced).
DEFAULTS: dict[str, Any] = {
    "version": 1,
    "backend": "github-issues",
    "github": {"repo": "auto", "default_labels": ["agent"], "attachment_mode": "native"},
    "linear": {"team": "", "project": "", "attachment_mode": "native"},
    # Extra hostnames (repo-configured) `_save_attachments` treats as safe to auto-fetch bytes
    # from even though they aren't the tracker's own native asset domain — e.g. a repo's own
    # GitHub Pages host. Empty by default (secure-by-default: unchanged behavior). Fetched
    # WITHOUT the tracker's auth header — see LoadedConfig.trusted_attachment_hosts.
    "trusted_attachment_hosts": [],
    "enforce": {
        "acceptance_criteria": "required",
        "motivation": "required",
        "user_impact": "required",
        "cost_of_inaction": "required",
        "formatting": "strict",
        "screenshots": {
            "on_create": {"required_if_label": ["ui", "visual"]},
            "on_done": {"required_if_label": ["ui", "visual"]},
        },
        "escape_hatch": "explain",
    },
    "classify": {
        "fallbacks": [
            {"anthropic": "claude-haiku-4-5"},
            {"openai": "gpt-5-mini"},
            {"commandcode": "deepseek/deepseek-v4-flash"},
            {"zai": "glm-4.6-flash"},
            {"google": "gemini-2.5-flash"},
            {"ollama": "qwen2.5:3b"},
        ],
        "bias": "change",
    },
    "session": {
        "detect": ["env:TASK_SESSION", "tmux-pane", "git-branch"],
        "label_prefix": "session:",
    },
}


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # lazy: keeps `task --help` dependency-free

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def _ignore_rig(exc: Exception) -> dict[str, Any]:
    """Log a foreign-rig.yaml problem at WARN and yield an empty overlay (clean fall-through).

    Shared by the rig.yaml load's tolerated failures — unreadable/invalid content
    (``ConfigError``) and PyYAML being absent (``ModuleNotFoundError`` on the yaml import) — so
    both warn-and-ignore identically. The message says "not loaded" rather than "unreadable": a
    missing-PyYAML file is perfectly readable, only unparsed.

    The ``log_event`` import is deliberately call-time (not hoisted to module top): tests patch
    ``tasklib.logging.log_event`` to assert the WARN, which only works while the lookup is
    deferred to call time. Keep it here.
    """
    from .logging import log_event

    log_event("rig.yaml not loaded; ignoring", level="WARN", error=str(exc))
    return {}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class LoadedConfig:
    """A cascaded, validated config plus provenance of where each layer came from."""

    data: dict[str, Any]
    repo_root: Path
    layers: list[str] = field(default_factory=list)

    @property
    def backend(self) -> str:
        return str(self.data.get("backend", "github-issues"))

    def section(self, name: str) -> dict[str, Any]:
        sec = self.data.get(name)
        return sec if isinstance(sec, dict) else {}

    def with_overlay(self, overlay: dict[str, Any]) -> "LoadedConfig":
        """A fresh config with ``overlay`` deep-merged over this one's data (self untouched).

        Used to resolve a registered project's backend coordinates against the same base
        config (so per-repo ``enforce``/``classify`` defaults carry, but the project's
        ``backend``/``github``/``linear`` win). The clone is deep so aggregating across
        projects can't leak one project's coordinates into the next.
        """
        import copy

        merged = _deep_merge(copy.deepcopy(self.data), overlay)
        return LoadedConfig(data=merged, repo_root=self.repo_root, layers=list(self.layers))

    @property
    def enforce(self) -> dict[str, Any]:
        return self.section("enforce")

    @property
    def classify_fallbacks(self) -> list[tuple[str, str]]:
        """The fallback chain as ``[(provider, model), ...]``, in declared order."""
        raw = self.section("classify").get("fallbacks") or []
        chain: list[tuple[str, str]] = []
        for entry in raw:
            if isinstance(entry, dict) and len(entry) == 1:
                ((provider, model),) = entry.items()
                chain.append((str(provider), str(model)))
        return chain

    @property
    def classify_capability(self) -> str:
        """The capability/role to resolve the classifier's preferred model from ``models.yaml``.

        Empty (the default) means "no manifest resolution" — the classifier uses the
        hardcoded/config fallback chain alone, exactly as before rig#8. A value (``fast`` /
        ``reasoning`` / ``code`` — a role or capability tag the shared manifest defines)
        makes the classifier prefer the model the manifest currently pins for it, kept fresh
        by the cron checker. (One caveat: for the ``gemini`` provider the manifest steers only
        the provider/role, not the exact version — ``review``'s gemini backend uses its own
        env-configured model; every other provider carries the manifest's exact id.) The
        resolution is fail-soft: an unresolvable value, a missing manifest, or the resolver
        lib being absent all fall through to the fallback chain.
        """
        return str(self.section("classify").get("capability", "")).strip()

    @property
    def classify_bias(self) -> str:
        return str(self.section("classify").get("bias", "change"))

    @property
    def linear_attachment_mode(self) -> str:
        """``"native"`` (real Linear attachment upload) or ``"link"`` (never re-host a local
        file; only register an already-hosted URL). See :data:`VALID_ATTACHMENT_MODES`."""
        return str(self.section("linear").get("attachment_mode", "native"))

    @property
    def github_attachment_mode(self) -> str:
        """``"native"`` (real GitHub attachment upload) or ``"link"`` (never re-host a local
        file; only register an already-hosted URL). See :data:`VALID_ATTACHMENT_MODES`."""
        return str(self.section("github").get("attachment_mode", "native"))

    @property
    def trusted_attachment_hosts(self) -> frozenset[str]:
        """Extra hostnames (repo-configured, top-level ``trusted_attachment_hosts:``) that
        ``_save_attachments`` treats as safe to auto-fetch bytes from, even though they are not
        a backend's own native asset domain — e.g. a repo's own GitHub Pages host, which a
        narrower native-asset check has no way to recognize as first-party.

        Fetched WITHOUT the backend's auth header — this list only widens the FETCH decision in
        ``_save_attachments``, never ``is_native_attachment_url`` itself (which stays the sole
        gate for "does this URL get the tracker's credentials attached" — see
        ``backends/linear.py``'s ``fetch_attachment_bytes`` docstring). Every entry here is
        already validated (``validate()``): a blank/whitespace-only entry, or one carrying a
        scheme/path/port/userinfo, is rejected at config-load time rather than silently never
        matching — this property only lowercases/strips whitespace off an already-valid list.
        Empty by default (secure-by-default, unchanged old behavior).
        """
        raw = self.data.get("trusted_attachment_hosts") or []
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(str(h).strip().lower() for h in raw if str(h).strip())

    @property
    def session_detect(self) -> tuple[str, ...]:
        raw = self.section("session").get("detect")
        if isinstance(raw, list) and raw:
            return tuple(str(x) for x in raw)
        return ("env:TASK_SESSION", "tmux-pane", "git-branch")

    @property
    def session_label_prefix(self) -> str:
        return str(self.section("session").get("label_prefix", "session:"))


def load(
    repo_root: Path,
    *,
    explicit_config: Path | None = None,
    include_global: bool = True,
    env: dict[str, str] | None = None,
) -> LoadedConfig:
    """Cascade-load config for ``repo_root``, layered over the built-in :data:`DEFAULTS`.

    Layer order (later wins): defaults → global → ``rig.yaml`` ``task:`` block → ``task.yaml``
    → ``--config P`` (the last replaces the ``task.yaml`` layer) → the
    ``TASK_CLI_ATTACHMENT_MODE`` env var (highest precedence — a personal, per-machine override
    that beats even a repo-committed pin; see :data:`ATTACHMENT_MODE_ENV_VAR`). The ``rig.yaml``
    block is the canonical per-repo tracker selector (rig provisions it); a native ``task.yaml``
    still wins so a repo that keeps one is unaffected. The result is validated (fail-closed on
    backend/version) before return. With no config file at all the built-in defaults are
    returned (the zero-config GitHub-Issues path).
    """
    import copy

    repo_root = repo_root.resolve()
    env = os.environ if env is None else env
    # deepcopy: DEFAULTS has nested dicts; a shallow dict(DEFAULTS) would share them, so a
    # later mutation (e.g. cli `--repo` writing cfg.data["github"]["repo"]) would leak into
    # the module-global DEFAULTS for the whole process (and contaminate across main() calls).
    merged: dict[str, Any] = copy.deepcopy(DEFAULTS)
    layers: list[str] = ["builtin-defaults"]

    if include_global:
        gpath = global_config_path(env)
        if gpath.is_file():
            merged = _deep_merge(merged, _load_yaml(gpath))
            layers.append(f"global:{gpath}")

    # rig.yaml task: block — the per-repo tracker selector, below the native task.yaml so a
    # repo keeping a hand-tuned task.yaml is untouched. Read whether or not --config is given:
    # --config replaces the task.yaml layer, not the toolchain's rig.yaml selection.
    # rig.yaml is a FOREIGN file (owned/evolved by rig-cli): a parse error or unexpected root
    # shape must NOT crash every `task` command in the repo — it falls through to the github
    # default (forward-compat). This tolerance is rig.yaml-only; task-cli's own task.yaml/global
    # stay fail-closed below.
    rig_path = rig_config_path(repo_root)
    if rig_path.is_file():
        try:
            rig_data = _load_yaml(rig_path)
        except ConfigError as exc:
            rig_data = _ignore_rig(exc)
        except ModuleNotFoundError as exc:
            # PyYAML absent — the installer may continue without it, promising built-in defaults.
            # A repo merely CONTAINING a rig.yaml must NOT traceback then; it falls through to the
            # github default, same as any other foreign-file problem above. ModuleNotFoundError
            # (not the broader ImportError: a circular/broken-init ImportError is a real bug that
            # must surface) scoped to the yaml package by exc.name keeps this to "PyYAML not
            # installed" — any other missing module is a broken environment and is re-raised.
            # Matched on exc.name only (CPython always sets it), never the message, so a missing
            # module that merely has "yaml" in its name (e.g. yaml_processor) still surfaces.
            name = exc.name or ""
            if not (name == "yaml" or name == "_yaml" or name.startswith("yaml.")):
                raise
            rig_data = _ignore_rig(exc)
        overlay = rig_task_overlay(rig_data)
        if overlay:
            merged = _deep_merge(merged, overlay)
            layers.append(f"rig:{rig_path}")

    if explicit_config is not None:
        rpath = explicit_config.resolve()
        if not rpath.is_file():
            raise ConfigError(f"--config file not found: {rpath}")
        merged = _deep_merge(merged, _load_yaml(rpath))
        layers.append(f"config:{rpath}")
    else:
        rpath = repo_config_path(repo_root)
        if rpath.is_file():
            merged = _deep_merge(merged, _load_yaml(rpath))
            layers.append(f"repo:{rpath}")

    # Highest-precedence layer: a personal, per-machine override for the attachment strategy
    # (tg#11652; extended from Linear-only to BOTH backends by tg#16794/review-cli#484) — set
    # TASK_CLI_ATTACHMENT_MODE=native on one machine to beat even a repo-committed pin (e.g.
    # hyperide's task.yaml pins "link" for everyone, HYP-1248) without touching that shared file
    # or affecting any other machine/user. Sets BOTH `linear.attachment_mode` and
    # `github.attachment_mode` so the one env var works regardless of which backend is active.
    # Still goes through the SAME validate() call below as every other layer, so a value ALSO
    # invalid for any other reason (e.g. a future stricter check on attachment_mode) still fails
    # closed there too — but review finding: relying SOLELY on that generic post-merge error
    # means a bad env value produces "linear.attachment_mode must be one of [...], got
    # 'carrier-pigeon'" with no mention of TASK_CLI_ATTACHMENT_MODE, and cfg.layers (which WOULD
    # show the env source) is never built because validate() raises before LoadedConfig() exists
    # — a user auditing task.yaml/rig.yaml for the typo finds nothing wrong there and has no
    # pointer to check their shell env. Validate the override eagerly, right here, so the error
    # names its actual source.
    override = str(env.get(ATTACHMENT_MODE_ENV_VAR, "")).strip()
    if override:
        if override not in VALID_ATTACHMENT_MODES:
            raise ConfigError(
                f"{ATTACHMENT_MODE_ENV_VAR}={override!r} must be one of "
                f"{sorted(VALID_ATTACHMENT_MODES)}"
            )
        merged = _deep_merge(
            merged,
            {"linear": {"attachment_mode": override}, "github": {"attachment_mode": override}},
        )
        layers.append(f"env:{ATTACHMENT_MODE_ENV_VAR}={override}")

    validate(merged)
    return LoadedConfig(data=merged, repo_root=repo_root, layers=layers)


def validate(data: dict[str, Any]) -> None:
    """Fail-closed validation of the enum-ish keys."""
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    version = data.get("version", 1)
    if not isinstance(version, int):
        raise ConfigError(f"version must be an int, got {version!r}")
    if version != 1:
        raise ConfigError(f"unsupported config version {version} (this task supports v1)")

    backend = data.get("backend", "github-issues")
    # isinstance guard BEFORE the membership test: VALID_BACKENDS is a set, so a non-hashable
    # value (a dict/list from a malformed `backend:` in the foreign rig.yaml) would raise an
    # unhandled TypeError on `in`. Convert that into a clean fail-closed ConfigError.
    if not isinstance(backend, str) or backend not in VALID_BACKENDS:
        raise ConfigError(f"backend must be one of {sorted(VALID_BACKENDS)}, got {backend!r}")

    for key in ("github", "linear", "enforce", "classify", "session"):
        if key in data and not isinstance(data[key], dict):
            raise ConfigError(f"'{key}' must be a mapping")

    linear = data.get("linear")
    if isinstance(linear, dict) and "attachment_mode" in linear:
        mode = linear["attachment_mode"]
        # Same isinstance-before-membership guard as `backend` above: a malformed
        # `linear.attachment_mode` (dict/list) must fail-closed with a clean ConfigError, not a
        # raw TypeError from the `in` check against the (unhashable-safe) set.
        if not isinstance(mode, str) or mode not in VALID_ATTACHMENT_MODES:
            raise ConfigError(f"linear.attachment_mode must be one of {sorted(VALID_ATTACHMENT_MODES)}, got {mode!r}")

    github = data.get("github")
    if isinstance(github, dict) and "attachment_mode" in github:
        mode = github["attachment_mode"]
        # Symmetric with `linear.attachment_mode` above (tg#16794/review-cli#484: the knob is
        # now backend-agnostic) — same isinstance-before-membership guard.
        if not isinstance(mode, str) or mode not in VALID_ATTACHMENT_MODES:
            raise ConfigError(f"github.attachment_mode must be one of {sorted(VALID_ATTACHMENT_MODES)}, got {mode!r}")

    if "trusted_attachment_hosts" in data:
        hosts = data["trusted_attachment_hosts"]
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise ConfigError(
                f"trusted_attachment_hosts must be a list of strings, got {hosts!r}"
            )
        # review finding: this is a security-relevant knob (it decides which hosts
        # `_save_attachments` auto-fetches from) — a copy-paste mistake here must fail LOUD, the
        # same way `linear.attachment_mode` does above, not silently never-match at fetch time.
        # review finding, round 2: a character BLACKLIST here (reject only "/@:\\") still let
        # plenty of non-hostname shapes through unrejected (a "?query", a "#fragment", an
        # internal space, bracket/quote characters) — all of which parse fine as a config
        # string but can never equal `urlparse(url).hostname`, so every attachment on that host
        # would just keep printing "skipped" with no clue why. An ALLOWLIST (RFC 1123 hostname
        # label shape) closes that instead of chasing individual bad characters one review round
        # at a time. Also rejects a blank/whitespace-only entry here — `LoadedConfig.
        # trusted_attachment_hosts` is the ONE place that still strips/lowercases a (now
        # guaranteed valid-shape) entry, not where blanks get filtered.
        for h in hosts:
            stripped = h.strip().lower()
            # review finding, round 3 (P1): a bare IP LITERAL — e.g. `169.254.169.254`, the
            # AWS/GCP/Azure cloud-metadata endpoint — passes the RFC-1123 label regex below
            # unchanged (every segment of a dotted-quad is a valid alnum label), so a
            # config-level typo or a deliberately hostile `rig.yaml`/`task.yaml` edit could
            # allowlist auto-fetching from an internal address with zero further checks. This
            # knob exists to name a specific first-party DOMAIN (a repo's GitHub Pages host);
            # it never has a legitimate reason to hold a bare IP, public or private — reject
            # every IP-literal shape outright rather than trying to enumerate "bad" ranges
            # (RFC1918/loopback/link-local today, someone's internal /8 tomorrow).
            try:
                ipaddress.ip_address(stripped)
            except ValueError:
                pass
            else:
                raise ConfigError(
                    "trusted_attachment_hosts entries must be DNS hostnames, not IP "
                    f"literals (SSRF risk — e.g. a cloud metadata address), got {h!r}"
                )
            # review finding, rounds 4-5 (P1, fixed twice more narrowly before this): `ipaddress.
            # ip_address()` is STRICT — it only recognizes canonical decimal-dotted-quad/
            # colon-hex notation, and rejects (raises ValueError on, i.e. does NOT catch)
            # non-canonical numeric encodings that OS resolvers (`getaddrinfo`, and by extension
            # `urllib`) have historically still accepted as real addresses. A round-4 fix caught
            # only a WHOLE-STRING bare-decimal or WHOLE-STRING `0x...` form, missing a MIXED,
            # PER-LABEL case: `127.0x1` (label "127" is plain decimal, label "0x1" is hex — a
            # real getaddrinfo-accepted encoding of 127.0.0.1) has no single whole-string
            # classification that either round-3 or round-4's checks catch, but every one of its
            # labels is individually numeric/hex-numeric. Check PER LABEL instead of the whole
            # string: if every dot-separated label is purely decimal digits or a `0x`-prefixed
            # hex literal, the entry cannot be a legitimate DNS hostname (a real one always has
            # at least one label containing an actual letter outside that shape) regardless of
            # which base/notation/mix any one label uses — this one check subsumes the
            # whole-string bare-decimal and whole-string-hex cases it replaces (a single-label
            # host IS "every label", so `2130706433`/`0x7f000001` still match here too).
            ip_check_labels = stripped.split(".") if stripped else []
            if ip_check_labels and all(
                _NUMERIC_OR_HEX_LABEL_RE.match(lbl) for lbl in ip_check_labels
            ):
                raise ConfigError(
                    "trusted_attachment_hosts entries must be DNS hostnames, not IP "
                    f"literals (SSRF risk — e.g. a cloud metadata address), got {h!r}"
                )
            # review finding, round 4: "localhost" resolves to loopback and is not an IP
            # literal in any sense `ipaddress`/the check above would catch, but is just as
            # unambiguously not a legitimate first-party domain for this knob.
            # review finding, round 6: the bare-string check alone missed two real aliases —
            # `attacker.localhost` (RFC 6761 §6.3 formally reserves the ENTIRE `.localhost` TLD
            # as always resolving to loopback — not a guess, a standard) and
            # `localhost.localdomain` (a long-standing glibc/Linux default-hostname convention,
            # still common in `/etc/hosts`). Both verified as real bypasses before this fix.
            # review finding, round 8: the round-6 check was itself asymmetric with its own
            # rationale — it rejected `attacker.localhost` and the exact string
            # `localhost.localdomain`, but not a SUBDOMAIN of that second alias
            # (`attacker.localhost.localdomain`), even though the same `/etc/hosts`+
            # search-domain setups that resolve the bare alias resolve that too.
            if stripped == "localhost" or stripped.endswith(
                (".localhost", ".localhost.localdomain", ".localdomain")
            ):
                raise ConfigError(
                    "trusted_attachment_hosts entries must be a first-party DNS domain, not "
                    f"a loopback alias like 'localhost' (SSRF risk), got {h!r}"
                )
            labels = stripped.split(".") if stripped else []
            if not labels or not all(_HOSTNAME_LABEL_RE.match(lbl) for lbl in labels):
                raise ConfigError(
                    "trusted_attachment_hosts entries must be bare hostnames (letters, "
                    f"digits, hyphens, dots only — no scheme/path/port/userinfo), got {h!r}"
                )
