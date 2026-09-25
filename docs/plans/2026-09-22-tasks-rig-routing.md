# Tasks/rig folder-level config routing (UB-23)

Refs UB-23: https://tasks-ultrabricks.vercel.app/issues/a871d6a3-b6c8-430f-9a8e-9677c7ad2f5c

## Problem

task-cli's config cascade (`tasklib/config.py`) already resolves, in order: built-in
defaults → task-cli's own global config (`~/.config/task-cli/config.yaml`) → the repo's own
`rig.yaml` `task:` block → the repo's native `task.yaml`/`--config` → the
`TASK_CLI_ATTACHMENT_MODE` env override. That covers "one repo, one tracker." It does not cover
a workspace convention: a folder like `~/xp` or `~/work` holding many repos that should all
default to the same tracker (the tasks-app workspace `UB` or `HYP`) without every repo needing
its own `rig.yaml` `task:` block or `task.yaml`. UB-23 asks for that folder-level default,
plus rig-cli's own machine-wide config as a layer above it.

## Two real facts resolved during investigation (not assumptions)

1. **rig-cli's own global config path**: `~/.config/rig/config.yaml` (or
   `$XDG_CONFIG_HOME/rig/config.yaml`) — confirmed by reading `riglib/config.py`
   (`global_config_path()`), NOT `~/rig.yaml`. This is a DIFFERENT filename and directory from
   an ancestor-walk `rig.yaml` (see below), so there is no double-read risk from including
   `$HOME` itself in the ancestor walk.
2. **`code_prefix` location**: nested inside `task:` (`task.code_prefix`), confirmed via
   `riglib/schema.py` (`_opt("task.code_prefix", ...)`) and `riglib/config.py`'s
   `_validate_task` docstring ("the unrelated `code_prefix` leaf folded into the same top-level
   block"). It is legitimately present in a repo's own `rig.yaml` `task:` block (rig-cli
   materializes it into `.ship-config`'s `SHIP_TASK_CODE_PREFIX`), but it is NOT something an
   ancestor/folder-level `rig.yaml` should hand down to every nested repo — each repo has its
   own ticket-code prefix.

## Precedence chain (later layer wins; unchanged tail)

1. Built-in defaults (existing)
2. **rig-cli's global config's `task:` block** — `~/.config/rig/config.yaml` (NEW) — a
   workspace-wide DEFAULT (rig-cli's own machine config)
3. task-cli's own global config — `~/.config/task-cli/config.yaml` (existing) — the user's own
   EXPLICIT machine-wide choice, which must win over rig-global's default on any shared key
   (corrected during review: an explicit choice must never be silently replaced by a default —
   the same relationship `task.yaml` already has with `rig.yaml` below)
4. **Ancestor `rig.yaml` `task:` blocks, OUTER → INNER** (NEW) — every directory strictly above
   the repo root that has a `rig.yaml`, bounded as described below
5. The repo's own `rig.yaml` `task:` block (existing)
6. The repo's native `task.yaml` / `--config` (existing)
7. `TASK_CLI_ATTACHMENT_MODE` env override (existing, stays last)

`include_global=False` (used today by `tasklib/taskref.py` for shape-only validation) skips
BOTH global layers (2 and 3) — it does NOT skip the ancestor walk (layer 4), which is
folder/workspace-scoped, not machine-global, and behaves like an extension of the repo's own
`rig.yaml` layer (5) rather than like the two truly-global layers above it.

## Ancestor-walk algorithm

Given `repo_root` (resolved) and `env` (a dict, never `Path.home()`/real `os.environ` directly
— callers must be able to fake `HOME` for tests):

- `home = Path(env.get("HOME") or os.path.expanduser("~")).resolve()`
- If `repo_root.is_relative_to(home)`: walk `repo_root.parents`, keep only those still
  `is_relative_to(home)` — this naturally stops at (and includes) `home` itself, and never goes
  above it.
- Else (repo checked out outside `$HOME`, e.g. under `/opt` or `/tmp`): walk `repo_root.parents`,
  drop only the filesystem root itself (`Path(repo_root.anchor)`).
- Reverse the result to OUTER → INNER order (farthest ancestor folds first, so a nearer
  ancestor's explicit value overrides a farther one — same "later wins" rule as every other
  layer).
- For each selected directory, the candidate file is `<dir>/rig.yaml` (same filename as the
  repo's own).

`Path.is_relative_to` is used throughout, never `str.startswith` — a sibling directory whose
name happens to prefix-match (`~/xp-other` vs `~/xp`) must never be treated as an ancestor of a
repo under `~/xp`. A worktree path (`<repo>/.worktrees/<name>`) needs no special-casing: its
real filesystem ancestry already walks past `.worktrees` up through the owning repo's own
ancestors, exactly like any other nested directory.

## Fail-closed contract for the two NEW layers (ancestor + rig-global)

Unlike the repo's own `rig.yaml` (which tolerates unreadable/malformed content as a
foreign-file forward-compat measure — `_ignore_rig`), a malformed **ancestor or rig-global**
layer fails closed:

- Unparseable YAML, or a non-mapping document root → `ConfigError` naming the file's path
  (already true today via `_load_yaml`'s own message, which embeds the path).
- A non-mapping `task:` block → `ConfigError` naming the file's path.
- A leaf conflict (e.g. a flat `team:` shorthand disagreeing with a nested `linear.team`) →
  `ConfigError` naming the file's path.
- An invalid `backend:` value → `ConfigError` naming the file's path, checked eagerly at LIFT
  time (before the final post-merge `validate()` call, which can no longer say which of several
  folded layers set the bad value).
- **The one tolerated case**: PyYAML not installed (`ModuleNotFoundError` on `import yaml`,
  scoped to the yaml package by `exc.name`) — an environmental condition, not a content
  problem, tolerated the same way for all three `rig.yaml`-shaped layers (repo/ancestor/global).
- `code_prefix` inside an ancestor's `task:` block is stripped silently (no WARN — it is a
  legitimate key for the repo that owns it, just not something a folder-level default should
  hand down) before the block is folded. The repo's own `rig.yaml` layer's existing behavior
  (an unknown key there logs a WARN and is skipped) is untouched — `code_prefix` under the
  repo's OWN `task:` block still goes through the pre-existing unknown-key path, unchanged.

## Provenance strings (`cfg.layers`)

- `rig-global:<path>` — rig-cli's own global config's `task:` block
- `rig-ancestor:<path>` — one ancestor `rig.yaml`'s `task:` block (one entry per ancestor that
  actually has a `task:` block)
- (unchanged) `rig:<path>` — the repo's own `rig.yaml`'s `task:` block

## `--backend` help text

`tasklib/cli.py`'s `--backend` flag help said `(github-issues|linear)`, missing the `tasks`
backend that already exists in `VALID_BACKENDS`. Fixed to `(github-issues|linear|tasks)`. No
`choices=` list restricts the flag's actual values (verified by grep), so this is a
documentation-only fix — the flag already accepted `tasks` at runtime.

## Test list (red-first, `tests/test_config_ancestors.py`)

1. Outer→inner ancestor precedence (three-level tree, inner wins)
2. Repo's own `rig.yaml` beats any ancestor
3. Repo's native `task.yaml` beats an ancestor `rig.yaml`
4. `--config` beats everything
5. Sibling isolation (`~/xp-other` must not leak into `~/xp/repo`)
6. A worktree path (`<repo>/.worktrees/x`) still resolves ancestors correctly
7. HOME boundary — nothing above `$HOME` is ever read
8. Outside-HOME repo — walk excludes `/` itself (unit-tested directly on `_ancestor_rig_paths`)
9. rig-global sits below native global, above ancestors (three-way precedence)
10. `include_global=False` reads neither global file (asserted via a patched `_load_yaml` that
    fails the test if either global path is ever opened)
11. Malformed ancestor YAML → `ConfigError` naming that file
12. Non-mapping `task:` in an ancestor → `ConfigError` naming that file
13. Invalid `backend:` in an ancestor → `ConfigError` naming that file, not a silent fallback
14. `code_prefix` in an ancestor's `task:` block is stripped, not propagated
15. Explicit repo `task.yaml` wins over an ancestor `rig.yaml`
16. Zero-config case: `cfg.layers == ["builtin-defaults"]`, unchanged
17. `task --help` still works with PyYAML unavailable
18. (added) Ancestor layer still applies under `include_global=False` — only the two truly
    global layers (native global, rig-global) are skipped by that flag, not the ancestor walk
19. (added, review correction) The user's own explicit global native config (`~/.config/task-cli/config.yaml`)
    wins over a conflicting `backend:` set by rig-global — a default must not silently replace
    an explicit choice
20. (added) A malformed `tasks:` section field (non-string `url`/`team`, invalid
    `attachment_mode`) folded from an ancestor `rig.yaml` fails closed, naming that file
21. (added) An unknown key inside an ancestor's `tasks:` section fails closed, naming that file
22. (added) The three-way precedence test's folding-order assertion is now `rig-global < global <
    rig-ancestor` (was `global < rig-global < rig-ancestor` before the correction)

## Tasks-section field validation (NEW, ancestor/rig-global layers only)

`_validate_layer_tasks_section` runs alongside `_validate_layer_backend` for both new layers
(never for the repo's own tolerant `rig.yaml`/`task.yaml` layers, which the general post-merge
`validate()` still covers as before). It rejects, fail-closed and naming the source file: a
non-mapping `tasks:` value, an unknown key inside `tasks:` (only `url`/`team`/`attachment_mode`
are recognized — mirrors `DEFAULTS["tasks"]`), a non-string `url`/`team`, or an invalid
`attachment_mode` enum. This runs at LIFT time, before the value is merged into the final
config and long before any backend I/O.

## Non-goals for this change

- No writes to `~/xp/rig.yaml` / `~/work/rig.yaml` — those are provisioned separately (rig-cli's
  own job), this change only teaches task-cli to READ them if/when they exist.
- No change to the repo's own `rig.yaml` layer's existing tolerant behavior.
- No new backend, no new network calls, no credential handling in either new layer — both only
  ever carry the `task:` routing block (via the existing `rig_task_overlay` translation),
  exactly like the repo's own `rig.yaml` layer does today.
