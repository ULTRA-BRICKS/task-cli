# AGENTS.md — task-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`task` is the **enforced interface to the ticket system**: a standalone Python CLI (peer to
`tg-cli` / `review-cli` / `rig-cli`) that turns every request into a durable, well-formed
ticket the moment it arrives, and enforces ticket *quality* (acceptance criteria, motivation,
user-impact, cost-of-inaction, screenshots, formatting) in the tool itself, not by convention.
Backends: **GitHub Issues (default)** and **Linear** (per-repo). It consumes `review-cli` for
cheap message classification and is wired into a `tg-cli` inbound hook.

## Hard rules

- **Stdlib-only at import time.** Every `tasklib/*` module imports only the standard library
  when loaded. Heavy/optional deps — `yaml` (config), the backends (`urllib`) — are imported
  lazily inside the function that needs them. `task --help` and every `<subcommand> --help`
  must run with zero third-party imports. Do not add a top-level `import yaml`.
- **Backends call the provider API DIRECTLY** (stdlib `urllib`, no per-call subprocess):
  GitHub REST, Linear GraphQL. The only subprocesses are the one-shot `gh auth token`
  (credential harvest) and the classify shell-out to `review`. Never spawn a CLI per API call.
- **Credentials are harvested, never re-prompted, never logged.** GitHub from
  `$GITHUB_TOKEN` → `gh auth token` → `hosts.yml`; Linear from `$LINEAR_API_KEY` →
  `~/.config/linear/credentials.toml`. All logging goes through `tasklib.logging` which
  **redacts** token-shaped values and masks secret-named keys. A token must never reach a log
  line or stdout.
- **Pure core stays pure.** `model.py` / `render.py` / `policy.py` / `classify.py` (parse +
  chain resolution) / `session.py` (detection) / `config.py` do **no network and no provider
  I/O**. The only I/O in the "pure" layer is the session sidecar (isolated, fail-soft) and
  config file reads. Effects (backend calls, the classify shell-out, the filesystem writes)
  live in `bin/task` → `tasklib/cli.py`.
- **Enforcement is the point.** The gates (`policy.py`) run on `create` and again on
  `change`→`done`. Each gate is (a) disable-able per repo via `enforce:` config and (b)
  skippable with a **recorded, auditable** justification (`--skip-<gate> "<reason>"`, or
  `--force "<reason>"` on `create`/`new` for the links + user-impact-quality gates → both
  written into the ticket's `Skipped gates` section). The content gates also enforce: **≥2
  acceptance criteria** (`acceptance_min`), a **plain-language, user-framed** user-impact
  (`quality.py`, the `user-impact-quality` gate), and **links not bare tokens** — any tracker
  id / `#ref` / commit SHA / repo-path named in the text must be a markdown link or full URL
  (`references.py`, the `links` gate). The on-done screenshot gate demands the *implementation*
  proof specifically — a creation shot does not let you close a UI ticket. **Two on-done gates
  are special:** a ticket cannot close while any acceptance criterion is **unchecked**
  (`acceptance-checked`) and that gate is a **hard refuse — non-skippable** (see
  `NON_SKIPPABLE_GATES`); and a checkbox is ticked only via `task check <id> <selector>
  --proof <path>`, which requires a **visual proof** (or `--force "<reason>"` when a proof is
  genuinely impossible, recorded on the `Criterion`). Acceptance criteria are `Criterion`
  objects (text + `checked` + `proof`/`force_reason`), round-tripped by `render.py`. Never
  weaken a gate silently; if a gate is wrong, change `policy.py` + add a test. **Two more
  create-only gates** (task-cli intake hardening, 2026-09): **duplicate**
  (`duplicate_violation`/`best_duplicate_match`) is a token/Jaccard-similarity net catching a
  REWORDED duplicate of an already-open session ticket — broader than the pre-existing
  near-exact-title hard block (`cli._refuse_if_duplicate`), which it does NOT replace; both
  share `GATE_DUPLICATE` and `--skip-duplicate "<reason>"`. **todo-shaped**
  (`tasklib/todo_hint.py`) is deliberately NON-BLOCKING: it never refuses a create, only prints
  a hint (via `PolicyResult.warnings`, never `.violations`) when the title/what reads as a
  session-local process step ("review my uncommitted diff before I commit it") rather than
  durable product work — pointing at the harness's own TodoList tool instead.
- **`read-before-edit` gates `change --what` on a recent `task read` (task-cli#119).** It refuses
  to overwrite a ticket's `what` (its core description) unless THIS session ran `task read`/
  `view` on it (or just created it, or itself just wrote `--what` on a prior edit) within a
  bounded TTL — see `tasklib/read_tracker.py`'s module docstring for the full design: why the
  marker is scoped per (session id, backend coordinate, ticket id) rather than a machine-wide
  "did anyone look" flag, the 30-minute TTL's rationale, what the marker does and doesn't prove,
  and why a failed marker-store read must refuse the edit (fail closed) rather than allow it. A
  metadata-only edit (`--due`/`--label`/…, no `--what`) is exempt from the gate AND deliberately
  never mints a fresh marker either (`cmd_change` gates the post-edit `_mark_ticket_read` call on
  `args.what` specifically) — recording one unconditionally would let a metadata edit followed by
  an unrelated blind `--what` edit, both inside the TTL, bypass the gate entirely (review
  finding, task-cli#119). Its waiver (`--skip-read-before-edit "<reason>"`) never lets a
  PERSISTED `ticket.skips` entry from a past command silently waive a LATER, still-unread edit —
  unlike every other `--skip-<gate>` here (`_enforce_edit_or_die` in `cli.py`), which treats
  `ticket.skips` as a permanent waiver. It is not, however, quite "this command only" in an
  absolute sense: a WAIVED edit that successfully sets `--what` still refreshes the marker (the
  session now knows the content it just wrote), so a further `--what` edit from the SAME session
  within the TTL needs no flag of its own — see the module docstring's "one direct consequence"
  note for the precise distinction from the persisted-skip case this gate actually guards
  against.
- **`task gate` is the same predicate as the close gate, exposed pre-merge.**
  `policy.acceptance_gate` wraps `unchecked_criteria_violation` (the call `check_done` makes)
  and `acceptance_min_violation` (the minimum-count gate `check` also enforces at close — a
  1-criterion, fully-proven ticket must refuse `task gate` too, or a PR could merge whose
  ticket can never legitimately reach Done), and adds only the recorded `post-merge-acceptance`
  opt-out (`POST_MERGE_ACCEPTANCE`, stored in `Ticket.skips` but never matched by `apply_skips`,
  so it cannot waive `task done`). Never fork either criterion predicate — `Ticket.unmet_criteria()`
  and `acceptance_min_violation` are their single homes. The `--json` field names (`ok`,
  `unchecked`, `proofless`, `below_minimum`, `post_merge_acceptance`, `gate_enabled`, `criteria`,
  `state`, `id`) are a contract consumed by agent-tools' `ci/ship/ship.sh`; changing them is a
  two-repo change. PR bodies in this ecosystem say `Refs #N`, never `Closes`/`Fixes` — the
  magic-close keywords close tickets behind these gates.
- **The close gates only run when task-cli's own commands run them — an external integration
  (Linear's GitHub-PR-link automation, GitHub's native "Closes #123" auto-close) can still write
  Done straight to the backend without ever invoking task-cli, so none of `policy.check_done`'s
  gates run.** `task audit` (`tasklib/audit.py` + `cli.cmd_audit`) is the after-the-fact
  backstop — see README's "Audit" section for the full incident (HYP-1347), the policy-drift
  caveat on what a finding does and doesn't prove, the recommended report-only cron wiring, and
  known limitations (the 100-ticket page cap, the GitHub label-vs-native blind spot). Two
  separately-tracked tool bugs (HYP-1355: a `task check --proof` upload can 404; HYP-1356:
  `task check`/`task done` regenerate the whole description via `render(ticket)`, dropping
  manually-authored content that doesn't round-trip) touch the same proof machinery this
  backstop assumes is real — not fixed here, flagged so the connection isn't missed.
- **`render.py` is the single source of truth for the ticket body.** The §5 section template
  (fixed order) is the contract; fields are authoritative, the body is derived. Do not
  hand-format a body anywhere else. `validate_format()` is what the formatting gate calls.
- **`task.yaml` is committed by default** (per-repo source of truth) and **overrides** the
  global `~/.config/task-cli/config.yaml`. Scope is by location, never a flag. The tool works
  with **zero config** on any GitHub repo (built-in defaults = github-issues, all gates on).
- **`task list` defaults to THIS session.** Session id = `env:TASK_SESSION` → tmux pane → git
  branch. Tickets are labelled `session:<id>` (durable) AND recorded in a local sidecar
  (`~/.local/state/task-cli/sessions/<id>.jsonl`, a cache). The label is the source of truth.
  It **falls back** to all of the repo's tickets — and says so — when there's no agent session
  or the session has none.
- **`list`/`find` page like git.** Human (non-`--json`) output goes through `less` ONLY on an
  interactive TTY; piped/scripted output is plain text (scriptable). The result cap is 100
  interactive vs 30 piped unless `-n` is given. Opt out with `--no-pager`/`NO_PAGER`/empty
  `$PAGER` or `$TASK_PAGER`. `--json` is never paged. The decision + piping live in
  `tasklib/pager.py` (pure decision, defensive routing); `cli._emit_list` is the single call site.
- **Read/global ops work OUTSIDE a git repo; only `create` is repo-bound.** `list` outside a
  repo (or `--all` anywhere) aggregates the `projects:` registry and groups by project;
  `read`/`status`/`find` route an id to a registered project; a degraded project never aborts
  the aggregation. `create` writes into ONE project, so outside a repo it fails with a 3-part
  WHAT/WHY/HOW error. The registry parsing/overlay shaping is pure (`tasklib/projects.py`); the
  multi-backend orchestration is an effect and lives in `cli.py`. Add a project backend
  coordinate, not a special case.

## Backend seam

`tasklib/backends/__init__.py` defines the `TicketBackend` protocol; `get_backend()` is the
only place that picks an adapter. `github_issues.py` and `linear.py` are the only modules that
know a provider's API shape. Tests use a structural `FakeBackend` (`tests/conftest.py`) — they
never hit live GitHub/Linear. If you add a backend, implement the whole protocol and add a
fake-backed test; do not special-case a backend anywhere in `cli.py`.

## Classification

`task classify` resolves the **first available** model in the per-provider fallback chain
(`classify.resolve_chain`, default head haiku) and shells out to
`review just-ask "<prompt>" -m <model> --pool 1`. Availability = a provider key in env, or
(ollama/laya) the daemon on PATH. Bias to `change` on ambiguity. The shell-out is the only
effect; prompt-building and verdict-parsing are pure and tested without spawning `review`.
`laya`/`jev` are additional provider-table entries (config plumbing only, unverified — no
credentials/HTTP call exist for either in this repo; wiring the real call is `review`'s job).

**SP / priority / process-vs-product** (`classify.parse_classification` → `Classification`):
the SAME `review` round-trip that yields the verdict also yields three axes on a `change`
verdict (never surfaced for `actionable`/`justAsk`, which mint no ticket): `story_points`
(`1,2,3,5,8,13` — a Fibonacci-ish COMPLEXITY scale, not time: `1` trivial/typo, `2` small
single-function fix, `3` moderate/single-file, `5` substantial/multi-file or new test infra,
`8` large/new subsystem or design-heavy, `13` epic/major feature from scratch), `priority`
(`P0`..`P3`), and `process_or_product` (`process` = a session-local/agent process step vs
`product` = durable work). Each axis is parsed independently via its own `LABEL: value`
sentinel regex (mirrors `_VERDICT_RE`); an axis that doesn't cleanly resolve biases to a FIXED
safe default (`product`/`P2`/`3` — never caller-configurable, unlike the change/justAsk bias).
`process_or_product` biases to `product` specifically because a mis-routed PROCESS ticket is
far cheaper to fix than a silently DROPPED product ticket — never write to neither store.

On `--create`, a `process`-classified item shells out to `harness tasks new --title <derived>
--body <message>` (the local harness-cli peer tool, reached ONLY via its installed binary on
PATH — never a Python import of harness-cli, mirroring the `review` shell-out) instead of the
configured product ticket backend; a missing binary or non-zero exit prints a clean `error:`
line, never a traceback. `classify.route_process_to_harness: false` disables this — every item
then follows the normal product-ticket path regardless of the axis. A `product`-classified
create additionally labels the new ticket `sp:<n>` and `priority:<Pn>`, threaded through the
SAME `labels=[...]` list `_classify_create` already builds (no backend-adapter changes). In a
multi-ask batch (`_classify_multi`), every split part is classified fully and independently, so
one message bundling a product bug and a process step correctly mints one product ticket AND
one local harness task.

## Tests

- `PYTHON=.venv/bin/python python3 -m pytest -q` — the unit suite. Fast, hermetic; uses the
  `FakeBackend` and `tmp_path`/isolated `$HOME`/`$XDG_*`. Tests never touch the real HOME,
  real credentials, or a live provider.
- `bash tests/smoke.sh` — end-to-end: `--help`, every subcommand `--help`, the lazy-import
  invariant, zero-config load, and pytest. No token used.
- Add a test with every behavior change. TDD red-first is the house style.

## Out of scope for THIS repo (separate follow-ups)

The ecosystem integrations are deliberately NOT built here: the `tg-cli` inbound classify
hook, the agent-tools `strict-ticket-discipline` skill + `require-ticket-before-commit` guard
+ `ci/ticket-required` backstop, and the `rig` `tickets:` provisioning. This repo is the TOOL.
Do not edit `tg-cli` / `agent-tools` / `rig` from here.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params, no `as-unknown-as` escape hatches.
- Keep `cli.py` thin (argparse + dispatch + effects); pure logic lives in the sibling modules.
