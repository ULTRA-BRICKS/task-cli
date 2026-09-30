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
  (credential harvest), a one-shot `fj -H <host> whoami` when fj's stored OAuth token has
  expired (fj refreshes its own store), the classify shell-out to `review`, and the one detached
  warm laya classify server (`tasklib/classify_server.py`, at most one surviving per idle
  period — see Classification). Never spawn a CLI per API call.
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
- **Deploy + follow-up traceability** (`deploy-followup-links`): `task change <id> --deployed
  "<note>"` records that ACCEPTED work actually reached production — sets `Ticket.deployed`
  (bool) / `deployed_at` (UTC ISO8601) / `deployed_note`, rendered as the §5 template's
  `## Deploy` section (a FIXED slot, right after `## Screenshots` and before `## Links` —
  `render()` always emits it, but `parse()` tolerates a pre-existing body with no `## Deploy`
  heading at all, defaulting to not-deployed rather than crashing). Refused (clean `error:`,
  non-zero exit, no backend write) unless the ticket's `state` is already `done` — this is a
  plain guard in `cmd_change`, NOT a new `transitions.py` `State`: `deployed` is deliberately
  orthogonal to `state` (a `done` ticket reopened to `in-progress` for a regression fix keeps
  `deployed=True`, since a deploy DID happen historically). `task link <followup-id>
  follow-up-of <original-id>` traces a later-discovered bug/regression back to the ticket that
  shipped it — `tasklib/links.py`'s `follow-up-of`/`followed-up-by` relation pair, structurally
  identical to `blocks`/`blocked-by` (two entries, asymmetric label strings, both directly
  invocable) even though the documented workflow only ever calls `follow-up-of` by hand. Both
  the `## Deploy` fields and the raw `links` dict are in `--json` output (`read`/`change`/
  `list`) so a caller can walk the full request → ship → deploy → follow-up chain without a
  second IPC mechanism — `task read <id> --json` IS the integration point (e.g. harness-cli's
  `harness stats` shells out to it; it never imports `tasklib`).
- **Product modules** (`tasklib/modules.py`, task-cli#218): a **module** is a named, durable
  subsystem/area of the product (e.g. for task-cli itself: `classify`/`backends`/`links`) — a
  small, per-repo TAXONOMY a team defines ONCE in `task.yaml`'s `modules:` list
  (`[{name, description}, ...]`), not a free-text label. `ModuleTaxonomy` is loaded via the
  SAME config-cascade pattern as every other `task.yaml` key (`config.py`); absent → an empty,
  fail-SOFT taxonomy (never required), present-but-malformed → `ConfigError`, fail-CLOSED
  (`validate_modules_config` is the single source of truth for the shape, called from both
  `config.validate()` and `load_taxonomy()`). A ticket is tagged into a module via a plain
  `module:<name>` label — the EXACT same mechanism `sp:<n>`/`priority:<Pn>` (#203) already
  use, so it needed zero backend-adapter changes. `task module list` shows the taxonomy (or
  "none configured"); `task module add <name> --description "..."` never writes `task.yaml`
  itself (no config-WRITING precedent exists anywhere in this repo — it is committed and
  human-edited) and instead prints the exact YAML snippet to add by hand; `task module assign
  <id> <name>` stamps the label via `backend.update()` and FAILS CLOSED — a clean `error:`, no
  backend write — on a name outside the configured taxonomy, never inventing a new one.
  `classify.infer_module` (default `false`, opt-in) lets `_classify_create` additionally try a
  cheap keyword-overlap heuristic (`tasklib.modules.infer_module`) against the taxonomy on a
  freshly-CREATED ticket only — never a guarantee, an ambiguous/tied score or an empty
  taxonomy both yield no label, and a repo that hasn't opted in sees zero behavior change.
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

`task classify` resolution is **pluggable** (`tasklib/classifiers.py`, task-cli#206/#217): a
small `ClassifierProvider` protocol (`available() -> bool`, `classify(message, bias) ->
Classification | None`, `label`) with three implementations. `build_classifier_chain` turns
`classify.fallbacks` (a list of `{provider: model}` pairs) into an ordered list of providers —
a `laya`/`jev` entry becomes its own `LayaClassifier`/`JevClassifier`; consecutive entries
naming neither fold into one `ReviewCliClassifier` (so `classify.capability`'s manifest-head
preference and `resolve_chain`'s own availability-walk apply to that run exactly as before).
`_classify_one` walks the chain and uses the first provider that is BOTH available AND
actually returns a `Classification` — a provider that looks reachable but fails at runtime
returns `None` and the chain falls through to the next one, same as the pre-existing "no
reachable provider" path. Neither `laya` nor `jev` is ever the zero-config default — both are
opt-in only.

**task-cli#208: `laya` is the zero-config DEFAULT, auto-installed on demand.** The default
lives entirely in `config.DEFAULTS["classify"]["fallbacks"]`, which now lists `{laya: local}`
FIRST -- `cli._classify_one` passes `cfg.classify_fallbacks` straight through (never coalesced
to `None`), and the cascade merge replaces a `fallbacks:` list WHOLESALE, so any repo's own
committed `classify.fallbacks` (even one omitting `laya`) fully overrides this default and is
never silently altered (verified live against this repo's own laya-free `task.yaml` pin).
`build_classifier_chain` itself is UNCHANGED from pre-#208 (`None` -> `classify.
DEFAULT_FALLBACKS`, no laya) -- it only groups whatever chain it's handed; see its own
docstring, and `test_build_classifier_chain_default_fallbacks_is_a_single_review_cli_group`.

`LayaClassifier.available()` auto-installs the package (once per instance, i.e. once per
`task classify` invocation) the first time it's found genuinely not importable: see
`tasklib/laya_autoinstall.py`'s module docstring for the full dedicated-venv/24h-cooldown-cache
design (task-cli#224, live-verified end to end against a real PEP-668-managed Homebrew Python:
a dedicated venv created under `~/.local/state/task-cli/laya_autoinstall/laya_venv-py<major.
minor>`, a real install into it, a `site.addsitedir` bolt-on making it importable in the
running process, and a real loaded `laya` model producing a live classification, all in one
`task classify` invocation — never touching system/Homebrew-managed site-packages, and no
escalating pip flag anywhere). `available()` deliberately routes ONLY the `find_spec() is
None` branch into the auto-installer -- a `find_spec` exception (broken/partial install
metadata) is a different, weirder failure and is left alone, matching the pre-#208
"treat as not installed, don't reinstall over it" posture.
`install.sh` also attempts this same best-effort, non-fatal install
(`scripts/lib/install_laya.sh`, a thin delegation into `tasklib.laya_autoinstall.install()`
itself since task-cli#224) against `command -v python3` (the interpreter `bin/task`'s shebang
resolves to) when `task` itself is installed, so on most machines the runtime auto-install
path is never even needed.

- **`ReviewCliClassifier`** — the original path, refactored in place, unchanged behavior:
  resolves the **first available** model in the per-provider fallback chain
  (`classify.resolve_chain`, default head haiku) and shells out to `review just-ask
  "<prompt>" -m <model> --pool 1`. Availability = a provider key in env, or (ollama) the
  daemon on PATH. The shell-out is the only effect; prompt-building and verdict-parsing are
  pure and tested without spawning `review`. `resolve_chain`/`_run_review_just_ask` are
  looked up on their owning module at call time (never cached), so this stays a thin,
  behavior-preserving wrapper.
- **`LayaClassifier`** — task-cli#206's local backend: Convai Innovations' free, local,
  Apache-2.0 "laya" PyPI package (an open-weights alternative to the paid cloud "Jev"
  model). Runs entirely in-process — no subprocess, no API key, no `review` involvement —
  via `laya.load(model_id).predict(message, questions) -> dict`. Optional dependency
  (`pyproject.toml`'s `laya` extra, `uv sync --extra laya` — or auto-installed at runtime,
  see the task-cli#208 note above), lazily imported inside `_load_agent`/`classify` (never at
  this module's top — stdlib-only-at-import-time still holds for every `tasklib/*` module).
  `available()` starts with a cheap `importlib.util.find_spec("laya") is not None` check --
  when that's `False`, it also drives the task-cli#208 auto-install (`ensure_installed`
  above) before returning, so it is NO LONGER guaranteed cheap/import-free on that path.
  `classify()` never raises — package missing, a first-run HuggingFace download failure, or a
  `predict()`/result-mapping runtime error all yield `None` (fall through, with a stderr
  diagnostic so a broken local install is observable) instead of a crash or traceback.
- **`JevClassifier`** — task-cli#217's cloud backend: TypeSafe AI's paid "Jev" evaluation
  model, called directly over Vercel AI Gateway's `POST https://ai-gateway.vercel.sh/v1/
  evaluate` HTTP endpoint (model id `typesafe-ai/jev`) via stdlib `urllib`
  (`tasklib.backends.http.request_json` — no `review`, no subprocess, no new dependency).
  `available()` is cheap — just "is an API key resolvable" (`$AI_GATEWAY_API_KEY`, falling
  back to a `.env`-shaped file read ONLY when `$TASK_JEV_ENV_FILE` is also set — opt-in per
  machine, never a hardcoded path) — no network call. `classify()` never raises: a missing
  key, a network/timeout error, or ANY
  non-2xx HTTP status (including Vercel's documented `customer_verification_required`
  billing-block shape — a disclosed, live-verified environment limitation on this account,
  not a code bug) all yield `None` and print one stderr diagnostic (API key and any other
  token-shaped secret redacted first); a malformed-but-2xx response body also yields `None`
  but degrades SILENTLY (no diagnostic), matching `LayaClassifier`'s own precedent. Either
  way the chain falls through instead of a crash or traceback.

`laya`/`jev` are DELIBERATELY ABSENT from `classify.py`'s
`_PROVIDER_MODEL_PREFIX`/`_LOCAL_DAEMON_PROVIDERS`/`_PROVIDER_KEY_ENV` tables — neither
touches `resolve_chain`/`review` at all (`jev` used to live there as config-plumbing-only
vaporware before task-cli#217 gave it a real implementation, the same way `laya` itself did
before task-cli#206). SECURITY (two layers each, `tasklib/classifiers.py`): (1)
`build_classifier_chain` NEVER threads a `{laya: <value>}`/`{jev: <value>}` fallback entry's
value into which Hub id/endpoint gets used — that value only decides ROUTING (does this
entry become a `LayaClassifier`/`JevClassifier`?). `classify.fallbacks` can come from a
repo-scoped `task.yaml`/`rig.yaml`, and `task classify` runs inside the target/foreign repo
being ticketed, so honoring an arbitrary configured value would let a malicious checkout make
`task classify` download and run attacker-controlled model code in-process (`laya`) or
exfiltrate `$AI_GATEWAY_API_KEY` to an attacker-controlled host (`jev`). `model_id`/`endpoint`
always resolve to the fixed `_LAYA_DEFAULT_MODEL_ID`/`_JEV_ENDPOINT`+`_JEV_MODEL_ID`. (2)
`LayaClassifier._load_agent` ALSO refuses to load whenever its fixed id already exists as a
path relative to the current working directory — `laya.load()`'s underlying
`model_id_or_path` resolution treats an existing local path as authoritative over the Hub, so
an unguarded lookup would let a planted `convaiinnovations/laya/` directory in the target
repo shadow the real checkpoint (mirrors `bin/task`'s existing cwd-import-shadowing posture).

**Warm laya server, guard rules, batch (task-cli#245).** `LayaClassifier.classify` first asks
`classify_server.predict` — a detached, machine-wide `python -P -m tasklib.classify_server`
that holds the loaded model and answers over a 0600 unix socket keyed by task-cli version;
the first call spawns it (reusing `daemon`'s child-env/`-P` bootstrap) and waits for the
socket, and every failure returns `None` so the provider loads in-process as before. The
server takes a flock BEFORE loading (racing cold clients load once), carries no copy of the
question schema (the client sends it), and exits after 15 idle minutes;
`TASK_CLASSIFY_SERVER=0` disables it (`tests/conftest.py` sets that for every test, and also
stubs `_cached_laya_snapshot`, the load's local-HF-cache-first lookup). `cli._classify_text`
wraps EVERY provider in two pure rules from `classify.py`: `is_small_talk` → `justAsk` with
no provider call, `looks_like_bug_report` → a `justAsk` verdict becomes `change`, axes kept
(`actionable` stays: task-cli#80). `task classify --batch` (stdin JSONL → stdout JSONL,
classification only) builds the
chain once.

**SP / priority / process-vs-product** (`classify.parse_classification` → `Classification`):
the SAME `review` round-trip that yields the verdict also yields three axes on a `change`
verdict (never surfaced for `actionable`/`justAsk`, which mint no ticket): `story_points`
(`1,2,3,5,8,13` — a Fibonacci-ish COMPLEXITY scale, not time: `1` trivial/typo, `2` small
single-function fix, `3` moderate/single-file, `5` substantial/multi-file or new test infra,
`8` large/new subsystem or design-heavy, `13` epic/major feature from scratch), `priority`
(`P0`..`P3`), and `process_or_product` (`process` = a session-local/agent process step vs
`product` = durable work). `ReviewCliClassifier` parses each axis independently via its own
`LABEL: value` sentinel regex (mirrors `_VERDICT_RE`); `LayaClassifier` reads each axis's
`choice` field off `laya`'s structured `predict()` reply instead. Either way, an axis that
doesn't cleanly resolve biases to a FIXED safe default (`product`/`P2`/`3` — never
caller-configurable, unlike the change/justAsk bias). `process_or_product` biases to
`product` specifically because a mis-routed PROCESS ticket is far cheaper to fix than a
silently DROPPED product ticket — never write to neither store.

On `--create`, a `process`-classified item shells out to `harness tasks new --title=<derived>
--body=<message>` (glued `=`-form, not a separate value token — the derived title/message can
itself start with `-`, which a separate token would read as an option flag; the local
harness-cli peer tool, reached ONLY via its installed binary on PATH — never a Python import of
harness-cli, mirroring the `review` shell-out) instead of the configured product ticket
backend. A MISSING `harness` binary never drops the item: it warns (stderr) and falls back to
the normal product-ticket path — the exact same "never write to neither store" posture the
process/product axis's own default already has. A harness binary that IS present but fails AT
RUNTIME (non-zero exit, timeout) is a different, genuinely actionable error and still surfaces
as a clean `error:` line, never a traceback — nothing falls back in that case.
`classify.route_process_to_harness: false` disables routing entirely — every item then follows
the normal product-ticket path regardless of the axis. A `product`-classified create
additionally labels the new ticket `sp:<n>` and `priority:<Pn>`, threaded through the SAME
`labels=[...]` list `_classify_create` already builds (no backend-adapter changes). The harness
route has NO dedup and no session recording (unlike `_classify_create`'s near-exact + advisory
token/Jaccard net) — a repeated inbound message mints a fresh local harness task each time. In
a multi-ask batch (`_classify_multi`), every split part is classified fully and independently,
so one message bundling a product bug and a process step correctly mints one product ticket AND
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
