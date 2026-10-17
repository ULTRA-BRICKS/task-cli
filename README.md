# task-cli

**The enforced interface to the ticket system.** Every request becomes a durable,
well-formed ticket the moment it arrives — `task` enforces ticket *quality* (acceptance
criteria, motivation, user-impact, cost-of-inaction, screenshots, formatting) in the tool
itself, not by convention. A ticket and its PR speak one shape.

A standalone Python CLI, peer to [`review-cli`](https://git.hyperide.ai/ultrabricks/review-cli),
[`rig-cli`](https://git.hyperide.ai/ultrabricks/rig-cli), and [`tg-cli`](https://git.hyperide.ai/ultrabricks/tg-cli). Backends: **GitHub Issues
(default)** and **Linear** (per-repo). Stdlib-first; the backends call the provider API
directly (no `requests`, no per-call subprocess), and credentials are harvested from the CLIs
you already authed — zero extra setup.

> Why this exists: agents drop requests, lose the thread, and produce work nobody can trace
> back to an ask. `task` makes "promise = durable action" mechanical — every request becomes a
> well-formed ticket, and `task list` always answers "what am I doing for you right now".

## Install

Pick one — each ends with `task` on your PATH:

```bash
# 1. uv — recommended; macOS, Linux and Windows (incl. Cygwin / Git Bash)
uv tool install hyper-task

# 2. pipx
pipx install hyper-task

# 3. one-liner — clones to ~/.local/share/task-cli, links task into ~/.local/bin, registers the agent skill
curl -fsSL https://git.hyperide.ai/ultrabricks/task-cli/raw/branch/main/install.sh | bash

# 4. from a clone — to hack on it; `git pull` updates the installed tool
git clone https://git.hyperide.ai/ultrabricks/task-cli && cd task-cli && ./install.sh
```

- After **1** or **2**, run `task install-skill` once so coding agents discover `task` (3 and 4 do it for you).
- **Unreleased `main`:** `uv tool install git+https://git.hyperide.ai/ultrabricks/task-cli` (or the same with `pipx install`).
- **Update:** `uv tool upgrade hyper-task` · `pipx upgrade hyper-task` · re-run the one-liner · `git pull`.
- **Installed before the rename** (as `task-cli`, from git)? Remove that first — `uv tool uninstall task-cli` or `pipx uninstall task-cli` — or two installs both claim `task`.
- **Via rig:** clone into `~/xp/task-cli` and list `task` under `tools.items` in `~/.config/rig/config.yaml` — `rig apply commit` runs its `install.sh` and keeps the clone fresh.
- **Windows:** use uv (option 1) — `./install.sh` and the one-liner do exactly that under Cygwin / Git Bash. If a command dies with `UnicodeEncodeError` in mintty, run `setx PYTHONUTF8 1` once. More: [rig-cli → Windows](https://git.hyperide.ai/ultrabricks/rig-cli#windows-cygwin-git-bash-powershell).

> **On PyPI as `hyper-task`** (the command is still `task`). The canonical repo is
> **git.hyperide.ai/ultrabricks/task-cli**; `github.com/alex-mextner/task-cli` is a frozen, archived mirror.

The only runtime dep is `pyyaml` (for `task.yaml`); without it the tool falls back to built-in defaults.

**Credentials** are read from the CLIs you already use — run these once and you're done:

```bash
gh auth login      # GitHub Issues (the default backend)
linear auth        # Linear (per-repo, via the rig.yaml task: block)
```

`task` reads `gh auth token` / `~/.config/linear/credentials.toml` / the `fj` CLI's stored
Forgejo token (or `$GITHUB_TOKEN` / `$LINEAR_API_KEY` / `$FORGEJO_TOKEN`) and calls the API
directly. Tokens are never logged or persisted.

## Commands

```
task new --title "..." --why "..." --impact "..." --if-not-done "..." --acceptance "..." --acceptance "..." [--due YYYY-MM-DD]
task create                        # alias of `new` (same arguments, same gates)  [--force "<reason>"]
task list                          # THIS session's tickets (falls back to all when empty)
task list --all                    # every known project's tickets, grouped by project
task gantt        [--all] [--json] # read-only due-date timeline (Gantt) — see below
task burn         [--all] [--json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]  # remaining-over-time burn chart — see below
task web start|stop|status|run [--port N]  # local HTML burn chart (default http://127.0.0.1:8765/burn)
task read <id>     (alias: view)   # the full ticket — every section (works outside a repo)
task find "<query>"                # search title+body (cross-project when outside a repo)
task link <id> <relation> <other-id>  # blocks|blocked-by|relates-to|duplicate-of|follow-up-of|
#   followed-up-by — writes a structured ## Links entry, mirrored on the other ticket (see
#   "Linking tickets" below)
task check <id> <n|text> --proof p # tick an acceptance criterion (needs a visual proof)
task check <id> 1 2 3 --proof a --proof b --proof c  # several at once, one proof each (in order)
task check <id> --all --proof p    # every criterion, one shared proof
task accept <id>                   # walk the unaccepted criteria interactively, asking for a proof each
task gate <id> [--json]            # read-only PRE-MERGE verdict (what `gh ship` runs) — exit 0 only when
#   every criterion is checked WITH a proof; 1 lists what is still owed; 2 = could not evaluate
task change <id> --post-merge-acceptance "<reason>"  # opt-out: acceptance is inherently post-merge
task mark-shipped <id> --pr <url> [--commit <sha>]  # record a merged PR; NEVER closes the
#   ticket (the `gh ship` post-merge hook — see agent-tools' ci/ship/ship.sh) — moves
#   TODO/IN_PROGRESS to in-review, posts a durable comment, and prints what's still needed
#   before a genuine `task done`.
task attach <id> <path> [path...]  # add implementation screenshot(s) (Playwright/agent-browser capture, not screencapture)
task comment <id> "text"           # add a comment (Markdown posted as-is; never touches the body)
task comment <id> --body-file F    # the same, body read from a file (`-` = stdin)
#   `task comment` is the supported way to comment on any backend's ticket — use it instead of
#   a raw forge CLI or API call
task done <id>    [--screenshot p] # close a ticket — runs the on-done gates (all criteria checked)
task change <id>  [--due ...] [--done] [--deployed "<note>"]  # update; --due sets/clears the
#   due date; --done closes (gates); --deployed records a production deploy (state must
#   already be `done` — see "Deploy + follow-up traceability" below); --label adds a label,
#   --unlabel removes one the ticket carries (repeatable; e.g. a duplicate priority:<Pn>).
#   `default_labels` are stamped at create only, so an unlabeled default stays removed
task status <id> [<new-state>]     # read or transition state (works outside a repo)
#   close/transition verbs validate legality first: a cancelled ticket is a dead-end and a
#   re-close of an already-done ticket is rejected (no silent re-write). `--force` overrides.
task classify "<text>" [--create]  # change|actionable|justAsk (+ SP/priority/process-vs-product
                                    # on `change`) via review; --create makes/dedups a product
                                    # ticket, or files a local `harness task` for a `process` item
task classify --batch < in.jsonl   # {"text","id"?} per line -> one verdict JSON per line
task classify-server start         # start the warm laya server if needed; prints its socket path
task classify-server serve         # run that server in the foreground (a service unit's ExecStart)
task session [show|bind <id>]      # show/bind the current session and its tickets
task module list [--json]         # show the repo's configured product-module taxonomy
task module add <name> --description "..."  # print the task.yaml snippet to add by hand
task module assign <id> <name>    # tag an existing ticket module:<name> (see below)
task daemon start|stop|status|run  # the due-date reminder watcher (see Daemon below)
task audit [<id>] [--revert]       # reconciliation backstop — flags a Done ticket that never
#   passed the close gates (see Audit below)
```

Global flags: `-C/--cwd`, `--backend`, `--repo`, `--config`, `--json`, and the per-gate escape
hatch `--skip-<gate> "<reason>"`.

Normal use needs no `-C`: run `task` inside the repository. An **explicit** `-C`/`--cwd` must
point inside a git repository — for every command, reads included. A non-repository path is
refused before any config, credential or network access, because it would otherwise fall back
to a global/registry route that may be a different project. The cross-project registry views
(`list`, `find`, `read` outside a repo) work from a non-repository shell cwd **without** `-C`.

`task read <id>` / `view` displays the exact backend document, including custom headings,
Unicode, code fences, and whitespace. In JSON, `body` is that original document; `what` and
other parsed fields are normalized views, not substitutes for the document. A fetched empty
document stays empty. A synthetic ticket with no fetched document falls back to the canonical
template (a directly supplied nonempty `raw_body` also takes precedence). The read marker is
recorded only after the full output has been written and stdout successfully flushed.

Full reads also include discussion: JSON has `comments` (the original provider rows, preserving
IDs, authors, source attribution, timestamps, bodies, and optional metadata) and `comments_status`
(`fetched`, `not-fetched`, or `truncated`). `comments: []` with `fetched` means an actually loaded
empty discussion; `comments: null` with `not-fetched` means the backend did not load it. Human
output gives the same status, comment metadata, and complete bodies in provider order. The Tasks
backend uses comments already returned by the issue detail endpoint; it makes no separate comment
GET. Malformed comment rows (including missing or non-text bodies) fail the read cleanly;
Optional metadata is preserved without requiring it. Other backends that do not load comments
report `not-fetched`. List/find summaries retain their existing output.

Tasks creates carry a fresh `Idempotency-Key` per create operation. On an uncertain transport
outcome (including a timeout waiting for headers or reading the body), task-cli makes at most
one recovery POST with the identical encoded payload and key, using tasks-app's stored receipt.
Proven URL validation/connect refusals and explicit HTTP failures are not retried. Other
mutations are never automatically replayed. If recovery also fails, exit 9 remains explicitly
ambiguous and includes the operation key: inspect the backend before starting another create.
The key survives only within that running operation; restarting the process or invoking create
again generates a new key, so this does not promise exactly-once creation across process restarts.
The supported tasks-app API persists `Idempotency-Key` receipts for POST issues and rejects
reuse with a changed payload. Recovery relies on that contract: a non-conforming server or
a proxy that strips the key is not covered. Recovery uses that contract without a capability
probe. GET/HEAD response failures are read errors, with no possible-create warning.
A recovery HTTP rejection
retains its scrubbed status and body, but cannot rule out a commit from the first attempt.

### Due-date reminders (the daemon)

A ticket can carry a `--due YYYY-MM-DD` date (set on `new`/`create`, changed or cleared on
`change`). It is stored backend-portably — a `## Due` section in the ticket body that round-trips
through both backends (Linear also mirrors it into the native `dueDate`).

The **daemon** is a background watcher that polls the backend on an interval, selects open
tickets that are overdue or due within a window, and pushes a reminder to the CTO's channel (the
`tg` CLI by default):

```
task daemon start    # spawn the detached daemon (idempotent — never double-starts)
task daemon stop     # stop it (SIGTERM, then SIGKILL on timeout); clears the pid-file
task daemon status   # running / not-ours / stale / stopped + pid + config (--json for machine output)
task daemon run      # the foreground loop (what `start` spawns)
```

`status` is pid-identity-aware (consistent with `stop`/`start`): a pid that was recycled by the OS
for an unrelated process after a crash reports as `not-ours` (in `--json` too), not `running`. To
verify that, a live daemon's status reads the process argv (`ps -ww` / `/proc`), slightly more than a
bare liveness probe.

The loop is fail-soft: a backend error, a malformed ticket, or a down notifier in one tick is
caught and logged — the daemon keeps running. De-dupe is per `(ticket, due-date)`, so a ticket
is reminded once; a changed due date re-fires. One daemon per repo coordinate (the state files
are keyed by it). Tunables live in a `daemon:` config block (see Configuration).

### Audit — catching a Done ticket that bypassed task-cli

`task done` / `task change --done` / `task status <id> done` all run the full close-gate set
(every acceptance criterion checked WITH a proof, screenshots for UI tickets, etc.) before ever
writing the ticket to Done. But a ticket can *reach* Done a different way entirely: Linear's own
GitHub-PR-link automation, and GitHub's native "Closes #123" issue auto-close, write the closed
state straight to the backend the instant a linked PR merges — task-cli is never invoked, so
none of its gates ever run. 

`task audit` is the backstop: it re-runs the same close gates against tickets that are already
sitting in Done, after the fact.

```
task audit                 # scan the most recent Done tickets (default: 50) for gate violations
task audit --limit 200     # scan more — capped at 100 per backend page, see note below
task audit <id>            # audit one specific ticket
task audit --revert        # additionally move each STILL-noncompliant ticket back to
#   in-review (re-checked immediately before acting, not the scan's snapshot), with a comment
#   explaining exactly which gate(s) it fails
task audit --json          # machine-readable findings (for a cron job / dashboard)
```

**The scan budget is 100 real tickets per run — how precisely that applies differs by backend.**
On **Linear**, `list()` filters Done tickets server-side (its normalized state is derived
purely from the native workflow state, so this is exact — no risk of missing a match), so
`--limit 100` fetches the 100 most-recently-updated **Done** tickets directly in one request:
`--limit 50` genuinely means "the 50 most recent Done tickets." On **GitHub Issues**, state
lives in a managed label rather than a native field, so `list()` instead scans (most-recently-
updated first) until it has seen 100 real issues **of any state** — excluding pull requests,
which GitHub's `/issues` endpoint mixes into the same page, so a page dominated by recent PR
activity is paginated past rather than counted toward the 100 — and filters client-side:
`--limit 50` scans those 100 issues for Done ones and returns up to 50 matches. Either backend:
any Done ticket past the scan budget (`--limit` Done tickets on Linear, 100 scanned issues on
GitHub — a repo closing more than that since the last audit) is invisible to that run until it
ages into the window, and the GitHub scan itself is capped at 10 pages
(1000 raw GitHub rows) so a pathologically PR-heavy repo can't turn one `task audit` run into
an unbounded fetch. Run audit often enough that your close rate never outpaces the scan budget
(a larger `--limit` cannot widen it — 100 is the hard cap on both backends).

**GitHub Issues backend: a known blind spot.** `task audit` is fully effective against Linear
(HYP-1347's own backend — Linear's normalized state IS the native truth, so there is nothing
to diverge). On the GitHub Issues backend, state is derived LABEL-FIRST (`_derive_state`): if a
GitHub-native `Closes #123` auto-close flips an issue to native-closed while its managed
`status:<state>` label is still active (e.g. `status:in-review`), the ticket keeps reading as
that active state and `task audit` does not currently see it as Done at all. Deciding how to
fix this (trust `Ticket.provider_closed` instead of the label, or re-prioritize
`_derive_state` itself) has blast radius across `list`/`find`/the stale-ticket nudge, not just
audit, so it is tracked as a separate follow-up rather than folded into this feature.

**A finding is not proof the ticket bypassed task-cli — read it before automating on it.** It
only means the ticket does not satisfy today's gates. The likely cause is an out-of-band close
(the reason this command exists), but a second real cause is policy drift: if a gate in
`policy.py` is tightened later (a stricter `acceptance_min`, a newly-required quality check, …),
a ticket that legitimately passed `task done` under the OLD rules can retroactively fail here.
For that reason, **the recommended cron setup is report-only**: schedule plain `task audit
--json` (no `--revert`) and alert a human on a non-empty `findings` array; use `--revert`
yourself, by hand, after you've read the findings — not as something a cron job runs
unattended.

Exit code is `0` when every scanned Done ticket passes, `1` when at least one does not — same
exit-code discipline `list`/`find` use, so a cron job can alert on it directly. `--json` always
emits the same shape (`scanned`, `findings`, `reverted`, `revert_errors`) whether or not any
findings exist, so a consumer never has to special-case the clean run. There is currently no
automatic scheduling built in (task-cli has no server component to run it from) — wire it into
a periodic job yourself, e.g. a daily cron / `CronCreate` entry. A gate that was legitimately
waived with a recorded `--skip-<gate>` at close time is never re-flagged here — the audit
reuses the exact same gate logic (`policy.check_done`) the close commands already run, so an
audited waiver is honored, not re-litigated (as long as the gate is still enabled in config —
see the caveat above about a gate's own rules changing over time).

### Timeline view (`task gantt`)

`task gantt` is a **read-only** Gantt: it charts the same tickets `task list` would show
(same session / `--all` / outside-a-repo scoping) on a date axis by their `--due` date.

```
task gantt                 # this session's tickets on a due-date timeline
task gantt --all           # every known project's tickets, flattened onto one axis
task gantt --state todo    # filter by state / --label, same flags as `task list`
task gantt --json          # machine-readable timeline (window + per-row bar geometry)
task gantt --width 60      # override the bar-area width (default: auto-fit the terminal)
```

Each dated ticket is a row with a status marker on the axis: `○` todo, `◐` in-progress,
`◑` in-review, `●` done, `!` (red) **overdue** (an open ticket past its due date), `✗`
cancelled. The `│`/`▼` gridline marks today. The date window auto-fits the tickets' range and
always includes today; a degenerate (single-date) range still renders. Tickets with **no due
date** are listed in a clearly-marked `undated` section — never hidden. It is purely a view:
no ticket is mutated. Output is paged like `list` in an interactive terminal (`--no-pager` /
`NO_PAGER` opt out); `--json` is never paged.

### Burn chart (`task burn`)

`task burn` is a **read-only** remaining-over-time chart: it plots how many tickets are
still open on each calendar day, using each ticket's created/closed timestamps from
GitHub (`created_at` / `closed_at`) or Linear (`createdAt` / `completedAt` or
`canceledAt`). A ticket closed on day D is gone on D. The ideal line is a straight
drop from the first day's remaining count to zero on the last day.

```
task burn                      # this session's tickets (OPEN+CLOSED) as a burn chart
task burn --all                # every known project's tickets
task burn --json               # remaining / ideal series (window + per-day counts)
task burn --html               # self-contained HTML+SVG on stdout (not paged)
task burn --since 2026-06-01 --until 2026-06-30
task burn --width 60           # override chart width (default: auto-fit the terminal)
```

Empty input prints a "no tickets" message and exits 0. Nothing is mutated.

### Web (`task web start|stop|status|run`)

`task web` serves the burn and gantt charts as local pages (stdlib `http.server`,
127.0.0.1 only). Lifecycle matches the reminder daemon, but the pid-file identity
includes `web` so the two cannot be confused.

```
task web run                   # foreground server on http://127.0.0.1:8765/
task web start                 # detach (idempotent — already-running is a no-op)
task web status                # running/stopped + pid + url
task web stop                  # SIGTERM, then clear the pid-file
task web run --port 9000       # override the listen port
```

Pages: `/` (both SVG charts), `/burn` and `/gantt` (HTML+SVG), `/burn.json` and
`/gantt.json`. Append `?all=1` to chart every ticket, not just this session.
Each HTML page has a nav between Both / Burn / Gantt. No CDN, no JavaScript.

### Example

```bash
task new \
  --title "Add a logout button to the header" \
  --what "A button in the top-right that ends the session and redirects to /login." \
  --why "Users have no way to sign out on shared machines." \
  --impact "Every authenticated user on the web app." \
  --if-not-done "Security complaint risk; sessions linger on shared devices." \
  --acceptance "button visible when logged in" \
  --acceptance "click clears the session cookie and redirects to /login" \
  --label ui --screenshot mock.png
```

`task` refuses to create the ticket if a required gate is unmet, with a precise message and
the exact flag (or escape hatch) to satisfy it.

## Enforcement — the point of the tool

On `create`, and again on `change`→`done`:

1. **Acceptance criteria** — **≥2**, rendered as a checkbox list. A real ticket has more than
   one provable outcome.
2. **Motivation / User impact / Cost of inaction** — three required, non-empty sections.
3. **Plain-language user impact** — the User-impact section must be written for someone only
   weakly familiar with the product, in their world's terms (their tasks/goals/what they see),
   not in implementation/jargon terms. A one-word or jargon-only impact is rejected with
   guidance.
4. **Related entities are links** — anything that names another entity (a tracker id like
   `HYP-789`, an issue/PR `#123`, a commit SHA, a repo/path slug) must be a proper **link**
   (markdown link / full URL), not a bare token. The scan is broad; `--force "<reason>"`
   overrides a genuine false positive. An id already carrying a structured `task link` entry
   (see "Linking tickets" below) is exempt — it won't ALSO be flagged when mentioned in prose.
5. **Screenshots** — required at *creation* and at *done* for UI/visual tickets (label-gated,
   configurable). Also required at *creation* when any prose field mentions screenshot in English or its
   Russian equivalent (any case) — attach with `--screenshot <path>`; do not dodge by deleting the
   word. The on-done gate (UI/visual only) demands the **implementation** proof specifically — a
   creation mock does not let you close a UI ticket. That is why the label-gated gate runs twice.
6. **Formatting** — the body must match the fixed section template (`render.py` validates).

On `change`→`done` (close) two more gates apply:

7. **Every criterion checked** — a ticket cannot close while any acceptance-criterion checkbox
   is unchecked. This gate is a **hard** refuse — no escape hatch waives it (only disabling it
   in config does).
8. **Each check carries a visual proof** — a checkbox is ticked with `task check <id>
   <selector> --proof <path>` and that requires a screenshot/image. When a proof is genuinely
   impossible, `task check … --force "<reason>"` records the reason on the criterion (audited).

Every gate except *every-criterion-checked*, *msgref-title*, and *junk-title* has an **escape
hatch**:
`--skip-<gate> "<reason>"` (or `--force "<reason>"` on `create`/`new` for the links /
user-impact-quality gates) writes the justification into the ticket's `Skipped gates` section
(auditable, recorded forever). Gates are also disable-able per repo via `enforce:` in config.

| gate | flag to satisfy | escape hatch |
| --- | --- | --- |
| acceptance-criteria (≥2) | `--acceptance "..."` (≥2, repeatable) | `--skip-acceptance "<reason>"` |
| motivation | `--why "..."` | `--skip-motivation "<reason>"` |
| user-impact | `--impact "..."` | `--skip-user-impact "<reason>"` |
| user-impact-quality | `--impact "<plain-language, user-framed>"` | `--skip-user-impact-quality "<reason>"` (or `--force "<reason>"` on create/new) |
| cost-of-inaction | `--if-not-done "..."` | `--skip-cost-of-inaction "<reason>"` |
| links | make each reference a link / full URL | `--skip-links "<reason>"` (or `--force "<reason>"` on create/new) |
| links-url | put only `http(s)://…` URLs in the `## Links` section (a `tg#<id>` reference goes in a prose field, not Links) | `--skip-links-url "<reason>"` |
| msgref-quote | let a body `tg#<id>` keep its auto-attached quote (re-run `create`/`change` to attach it) | `--skip-msgref-quote "<reason>"` |
| screenshots | `--screenshot <path>` | `--skip-screenshots "<reason>"` |
| formatting | (automatic — body is rendered) | `--skip-formatting "<reason>"` |
| msgref-title | keep `--title` free of a `tg#<id>` reference (put it in `--what`/`--why`/etc. instead) | **none** (hard; disable in config only via `enforce.msgref_title: false`) |
| junk-title (create, and any `--title` edit) | don't phrase a title as "Review uncommitted …", "Review/Audit/Adversarial [review of] ... diff", or "... diff for performance" — that shape is process work for `harness task`, never the product GitHub/Linear tracker; run `review diff`/`review quorum` or `harness task` instead | **none** (hard; disable the check itself only via `enforce.junk_title: false`) |
| acceptance-checked (close) | `task check <id> <n> --proof <path>` for each | **none** (hard; disable in config only) |
| read-before-edit (`change --what` only) | `task read <id>` (or `task view <id>`) THIS session, recently, before overwriting `--what` | `--skip-read-before-edit "<reason>"` — a *persisted* skip from an earlier command never silently waives a later, still-unread one |
| duplicate (`create`/`new`, token/Jaccard) | give the title/what real, distinguishing content | `--skip-duplicate "<reason>"` (or `--force "<reason>"`) |
| todo-shaped (`create`/`new`, non-blocking) | rewrite the title/what as a durable, reusable concern | `--skip-todo-shaped "<reason>"` — never blocks even unskipped, see below |

The **read-before-edit** gate (task-cli#119) refuses `task change <id> --what "..."` unless this
same working session has run `task read <id>` (or just created that ticket with `task new`/
`create`, or itself just wrote `--what` on a prior edit) within the last 30 minutes — so an agent
that never actually looked at a ticket's current What/Why/Impact/acceptance criteria can't
silently overwrite them. It only gates `--what` (the ticket's core description); a metadata-only
edit (`--why`/`--impact`/`--if-not-done`/`--due`/`--label`, with no `--what`) is never blocked by
it, and deliberately never mints a fresh "read" credential either — only an edit that actually
sets `--what` does that, whether or not it needed `--skip-read-before-edit` to pass. What the
waiver does NOT do: a `--skip-read-before-edit` recorded on a past command is written into
`ticket.skips` for audit only — a LATER, genuinely unread `--what` edit that supplies no skip
flag of its own is still refused, never silently waived by that historical record (see
`tasklib/read_tracker.py`'s module docstring for the full design rationale: the marker's
session/TTL/repo scoping, what the marker does and doesn't prove, and why a failed marker-store
read refuses the edit rather than allowing it).

The **links-url** gate keeps the `## Links` section strictly URLs — a bare, non-URL value
(`Session: session:2`) rendered as a fake link and slid past the prose-only `links` gate. The
**msgref-quote** gate ensures a `tg#<id>` mentioned in a prose field carries the quoted message
content. On `create`/`change` the reference is auto-expanded from local `tg-cli` history: a
resolvable id gets its quote inlined, an unresolvable one gets an inline "message not found" note
(so the reference is never left bare) — either way nothing else is needed. The gate itself is the
backstop for a ticket authored in the web UI or before the feature existed, whose reference never
went through that expansion: it only *blocks* when the message resolves in local history yet the
quote is missing, and merely *warns* (never blocks) when the reference is unresolvable — too old
for local retention, or `tg-cli` isn't installed here. Passing `--skip-msgref-quote "<reason>"`
waives the gate AND suppresses expansion for that command (so a false-positive `tg#N` isn't quoted
at all). Both gates are disable-able per repo via `enforce.links_url: false` /
`enforce.msgref_quote: false`.

The **duplicate** gate (`create`/`new` only, `tasklib/policy.py`'s `duplicate_violation`/
`best_duplicate_match`) is a second, broader net alongside the pre-existing near-exact-title
block: it compares the new title/what against this session's already-open tickets by TOKEN
overlap (after stripping this codebase's own boilerplate vocabulary — "review"/"diff"/
"uncommitted"/…) and flags a match at ≥2 shared meaningful tokens AND Jaccard ≥0.5 — thresholds
validated against a real ecosystem audit's 13 confirmed duplicates. It catches a REWORDED
duplicate of the same request the exact-title check misses ("Review OMP model-detection
ordering diff" vs "Review OMP lineage-first model detection diff"). `task classify`'s
auto-create path (the hook a repeated inbound message actually goes through) also runs this
matcher as a fallback when its own near-exact check finds nothing — but only as a WARNING
("possible duplicate of #N ... creating a new ticket anyway"), never to fold the message into a
comment: `classify` has no `--skip-duplicate` escape (its parser exposes only `--create`/
`--update`) and is typically driven autonomously with no human in the loop, so a known false
positive (see below) auto-folding into an unrelated ticket would be an invisible, unrecoverable
mistake there. `task new`/`create`'s near-exact-title check still auto-folds on `classify` —
only the broader token/Jaccard net is advisory-only on that path.

The **todo-shaped** gate (`create`/`new` only, `tasklib/todo_hint.py`) is deliberately
NON-BLOCKING: it never refuses a create, even unskipped — it prints a hint when the title/what
reads like an agent's own in-session process step ("review my uncommitted diff before I commit
it", "adversarial review of...") or names a review/diff-check step with an empty What section,
suggesting the harness's own ephemeral TodoList tool (TodoWrite/TaskCreate) instead. Silence a
false positive with `--skip-todo-shaped "<reason>"` (or `--force "<reason>"`) so it isn't
re-printed on a later command that re-scans the ticket.

### Linking tickets — `task link` + the post-create nudge

```
task link <id> blocks <other-id>         # <id> blocks <other-id>
task link <id> blocked-by <other-id>     # <id> is blocked by <other-id>
task link <id> relates-to <other-id>     # symmetric — no canonical direction
task link <id> duplicate-of <other-id>   # <id> is a duplicate of <other-id> (the canonical one)
task link <id> follow-up-of <other-id>   # <id> is a follow-up (e.g. a regression) of <other-id>
task link <id> followed-up-by <other-id> # <id> was followed up by <other-id> (the inverse)
```

Writes a structured entry into `<id>`'s `## Links` section (e.g. `Blocks #149: <url>`).
`blocks`/`blocked-by`/`relates-to`/`follow-up-of`/`followed-up-by` also write the mirrored
entry on the OTHER ticket (`A blocks B` implies `B is blocked-by A`; `A follow-up-of B` implies
`B followed-up-by A`); `duplicate-of` is deliberately one-sided (marking something a duplicate
of a popular ticket shouldn't spam that ticket's own Links section with every dupe that ever
pointed at it). Re-running the same `task link` call is a no-op — it never adds a second entry
for the same relation + ticket pair.

After a successful `task new`/`create`, a cheap same-repo scan (title/label keyword overlap —
no embeddings, no fuzzy matching) checks currently-open tickets for plausible relatives and
prints a nudge with the exact `task link` command for each candidate. It only ever *suggests* —
nothing is auto-linked, and a scan hiccup never fails the create.

### Product modules — `task module`

A **module** is a named, durable subsystem/area of the product a ticket belongs to (e.g. for
task-cli itself: `classify`, `backends`, `links`, `render`) — a small, per-repo TAXONOMY a
team defines once, not a free-text label. A repo opts in by adding a `modules:` list to its
`task.yaml`:

```yaml
modules:
  - name: classify
    description: message classification, SP/priority axes, the classifier-provider chain
  - name: backends
    description: GitHub Issues / Linear adapters
```

```
task module list [--json]                 # show the configured taxonomy, or a clear "none
                                            #   configured" message when the repo has none
task module add <name> --description "..."  # task.yaml is committed/human-edited (task-cli
                                            #   never writes it) — prints the exact YAML
                                            #   snippet to add by hand
task module assign <id> <name>             # tags <id> with a `module:<name>` label (the same
                                            #   mechanism `sp:<n>`/`priority:<Pn>` use) —
                                            #   REFUSES cleanly (no backend write) for a name
                                            #   outside the configured taxonomy
```

With `classify.infer_module: true` in `task.yaml` (default `false` — opt-in), `task classify
--create` also tries to infer a module from the taxonomy via a cheap keyword-overlap heuristic
against each module's name/description — a best-effort nudge, not a guarantee, and it never
applies a label when the signal is empty or ambiguous (a tie).

### Deploy + follow-up traceability — `task change --deployed` + `follow-up-of`

A ticket's lifecycle can be traced past `done` (accepted) through to an actual production
deploy, and a bug/regression found afterward back to the original ticket that shipped it:

```
todo → in-progress → in-review → done —[optionally]→ deployed
                                          │
                                          └─ regression found → new ticket, linked
                                             `follow-up-of` the original
```

```
task change <id> --deployed "<note>"     # record a production deploy (e.g. "shipped in v1.4.0")
task link <followup-id> follow-up-of <original-id>  # trace a later regression back to it
```

`--deployed` sets `deployed=true` and `deployed_at` (UTC ISO8601, captured at call time) and
appends `<note>` into the ticket's `## Deploy` section — refused with a clean `error:` (no
backend write) unless the ticket's state is already `done`: `deployed` is a historical record
of a PAST deploy of ACCEPTED work, not a way to close a ticket early. It is deliberately
orthogonal to `state`, not a new lifecycle state — a `done` ticket that gets reopened to
`in-progress` for a regression fix keeps `deployed=true`, since a deploy DID happen
historically even though the ticket is active again. `--json` (on `read`/`change`/`list`)
exposes `"deployed"`/`"deployed_at"`, plus the raw `"links"` dict so a caller can walk a full
request → ship → deploy → follow-up chain: `task read <id> --json` on the original ticket shows
`"Followed up by #<id>"` in `links` and `deployed: true` in `## Deploy`; `task read
<followup-id> --json` shows `"Follow-up of #<original-id>"`.

### Checking criteria off — `task check`

```
task check <id> <selector> --proof <path>     # tick a criterion WITH its visual proof
task check <id> <selector> --force "<reason>"  # tick it when a proof is genuinely impossible
```

`<selector>` is a 1-based index or a text substring of the criterion. The checked state and
the proof live in the body (`- [x] … — proof: ![proof](path)`), so the close gate can see what
is and isn't done.

Several criteria in one call: `task check <id> 1 3 --proof a.png --proof c.png` takes one
proof per selector, in order; `task check <id> --all --proof shot.png` ticks every criterion
with one shared proof (or `--all` with exactly one `--proof` per criterion). `--force
"<reason>"` applies to every selected criterion. The single-selector form keeps its original
contract: the first `--proof` backs the box, further ones are attached as implementation
screenshots. `task accept <id>` is the interactive equivalent: it walks the criteria that
still block the gate, asks for a proof path/URL (or `force: <reason>`) for each, and persists
every answer immediately; without a TTY it prints the headless commands instead.

### The pre-merge gate — `task gate` (and why PR bodies say `Refs`, never `Closes`)

task-cli's close gates only run inside `task done`. GitHub's magic-close keywords (`Closes
#N`, `Fixes #N`, `Resolves #N`) close an issue the instant the linked PR merges — behind
task-cli's back, so a Done ticket ends up with empty checkboxes (Linear's "PR merged"
automation used to do the same; for the HYP team it now moves the ticket to **In Review**,
which is the normal "merged, awaiting acceptance" state — `task mark-shipped` leaves an
In Review ticket where it is). Done is reached only through acceptance: `task done` after
every criterion is checked with a proof. `task gate` is the read-only verdict `gh ship`
(agent-tools `ci/ship/ship.sh`) runs BEFORE merging, so a PR cannot merge while its ticket is
unaccepted:

```
task gate <id>            # exit 0 = accepted; 1 = not accepted (lists the gaps); 2 = could not evaluate
task gate <id> --json     # the machine contract below
```

The verdict is exactly TWO on-done gates: `acceptance-checked` (`policy.acceptance_gate` calls
the same `unchecked_criteria_violation` `task done` does — never a re-derivation) and the
`acceptance-criteria` MINIMUM count (`acceptance_min`, default 2 — the same predicate every
`task new`/`task done` already enforces), plus one merge-only concession: a ticket whose
acceptance is inherently post-merge (a release publish, a deploy the merge triggers) records
`task change <id> --post-merge-acceptance "<reason>"` (stored under `## Skipped gates` as
`post-merge-acceptance: <reason>`); the gate then passes and reports the reason. That opt-out
never waives `task done` — the ticket still needs real proofs, just after the merge. A
cancelled ticket passes; a ticket with zero criteria fails (nothing to accept is not
acceptance); a ticket below the minimum fails too — even with every criterion it DOES have
fully proven, since `task done` would still refuse it on count alone (`--skip-acceptance
"<reason>"` waives this one specifically, same as at create); `enforce.acceptance_checked:
false` alone does NOT bypass the minimum-count gate — that needs `acceptance_criteria: false`
too (or `acceptance_min: 0`).

`--json` (exit 0 and 1 print the same shape; exit 2 prints `error: …`, not JSON):

```json
{
  "id": "HYP-1440", "ok": false, "state": "done", "gate_enabled": true,
  "post_merge_acceptance": null,          // or the recorded reason (string)
  "criteria": 3,                          // total count; 0 => refused
  "below_minimum": false,                 // true when criteria < acceptance_min and not skipped
  "unchecked": [{"index": 3, "text": "survives a restart"}],
  "proofless": [{"index": 2, "text": "handles the empty case"}]   // checked without a proof/force reason
}
```

`index` is the 1-based position in the full criteria list — the number `task check <id> <n>`
takes. `unchecked`/`proofless` are always populated, even when an opt-out makes `ok` true, so
the shipper sees what is still owed — but a `below_minimum` refusal can have BOTH arrays
empty (e.g. one fully-proven criterion below a minimum of two): a consumer must check
`below_minimum` too, not just the two gap arrays, to explain every refusal. Consumers key off
`ok` for the verdict; the other fields are for the message. Field names are stable
(agent-tools' ship.sh depends on them; the same contract is documented in its
`ci/ship/README.md`).

**PR bodies must say `Refs #N` / `Refs HYP-N` — never `Closes`/`Fixes`/`Resolves`** (or their
-s/-d forms) before an issue or ticket reference. `Refs` links the PR to the ticket without
closing it; the ticket closes only through `task done` after acceptance. `gh ship` refuses a
PR whose title or body carries a magic-close keyword.

### Upgrading to 0.6.0 — the close gates are stricter

0.6.0 adds the **every-criterion-checked-with-proof** close gate (`acceptance-checked`, a
*hard* refuse) and the **≥2 criteria** rule, and both run on `done`. The **links** and
**plain-language user-impact** gates also run on close now (not only create), so a ticket that
never passed the new create gates — one opened before 0.6.0, or edited directly in the
GitHub/Linear web UI — is caught at the close boundary too. A pre-0.6.0 ticket therefore cannot
be closed as-is when it has unchecked / proof-less `- [ ]` criteria, fewer than two criteria, a
bare reference (`HYP-789`), or a thin impact: run `task check <id> <selector> --proof <path>` for
each criterion (use `--force "<reason>"` when a proof is genuinely impossible), add a second
criterion if it has only one, and link the references / rewrite the impact — or waive a genuine
legacy exception on the close command itself with `--skip-links` / `--skip-user-impact-quality`
`"<reason>"`. If you need to close a batch of legacy tickets without that migration, disable the
gates per repo in `enforce:` (`acceptance_checked: false`, `acceptance_min: 1`, `links: false`,
`links_url: false`, `msgref_quote: false`, `user_impact_quality: false`) rather than fighting it
ticket-by-ticket.

## The ticket body template

```
## What
## Why (motivation)
## User impact
## Cost of inaction
## Acceptance criteria
- [ ] …
## Screenshots
## Deploy
## Links
```

This is the same section set as the agent-tools `pull_request_template.md`, PLUS `## Deploy`
(this repo's own deploy/follow-up traceability addition — see "Deploy + follow-up
traceability" above), so a ticket and its PR speak one shape. `render.py` is the single source
of truth — fields are authoritative, the body is derived. `## Deploy` always renders (even
`Deployed: no`), but `parse()` tolerates a body with no `## Deploy` heading at all — every
ticket rendered before this field existed round-trips as not-deployed instead of crashing.

## Session-scoped `task list`

A "session" is the unit of work `task` is doing for you. The id is detected by precedence:

1. `$TASK_SESSION` — explicit, harness-set.
2. tmux pane (`$TMUX_PANE`).
3. git branch.

Every ticket created/touched in a session is labelled `session:<id>` (portable) **and**
recorded in a local sidecar (`~/.local/state/task-cli/sessions/<id>.jsonl`, fast/offline).
`task list` defaults to the current session's tickets.

## Working outside a repo / across projects

A tool's *read* and *global* operations should not demand you stand inside a git repo. So:

- **`task list` outside any repo** → shows **all** tickets across the projects you've
  registered, **grouped by project** (a heading per project, tickets beneath). The output
  says `showing all project tasks` so it's clear why you see everything.
- **`task list` inside a repo** → scopes to that repo's current session. With **no agent
  session**, or a session with **no tickets**, it falls back to *all* of that repo's tickets
  and says so. **`task list --all`** gives the cross-project grouped view from anywhere.
- **`task read` / `task status` / `task find`** work outside a repo too. An id is routed to a
  registered project (a Linear/Tasks `HYP-3` by its team; `owner/repo#123` to the registered
  GitHub/Forgejo project with that repo; a bare `#123` only when exactly ONE GitHub/Forgejo
  project is registered — issue numbers are per repository); an ambiguous id fails with a
  clear, actionable error.
- **Only `task new`/`create` is repo-bound** — it writes a ticket into one specific project, so
  it needs a repo (or `--repo owner/name`). Outside one it fails with a 3-part WHAT/WHY/HOW error.
  Id-routed reads and writes work from a non-repository shell cwd, never through an explicit
  non-repository `-C` (see Global flags).
- A project whose backend errors (auth, offline, unknown team) is shown as a **degraded
  group** — it never aborts the whole cross-project listing.

`--json` follows the view: the session/single-repo list is a flat `[ticket]`, while the
grouped cross-project view (outside a repo, or `--all`) is `[{project, backend, current, error,
tickets}]` — one object per project group, so a degraded project is visible to scripts too. The
in-repo fallback (session empty → all of *this* repo's tickets) stays the flat `[ticket]` shape,
scoped to the current repo, even though the text output prints the `showing all project tasks`
line. Only the cross-project view is grouped.

### Pagination (`list` / `find`)

Like `git log`, the human (non-`--json`) output is paged through `less` **only when stdout is an
interactive terminal**. Piped or scripted (`task list | …`, CI), it prints plain text so it stays
parseable — no pager, no surprises. Short output that fits one screen prints directly (`less -F`).

- The result cap follows the same split: **100** in a terminal (the pager scrolls), **30** when
  piped. An explicit **`-n N`** always wins.
- Opt out of the pager with **`--no-pager`**, **`NO_PAGER=1`** (any non-empty value), or an empty
  **`$PAGER`/`$TASK_PAGER`** (git's "cat, don't page"). Choose the pager via `$TASK_PAGER` →
  `$PAGER` → `less` → `more`. `$LESS` defaults to `FRX` (quit-if-one-screen, raw colors, no screen
  clear) unless you set it.

The cross-project view reads a **`projects:`** registry from the config cascade — usually the
**global** `~/.config/task-cli/config.yaml`, since it spans repos:

```yaml
projects:
  - { repo: acme/frontend }                       # GitHub shorthand → group "acme/frontend"
  - { name: Backend, github: { repo: acme/api } }  # explicit block + display name
  - { name: HYP, backend: linear, team: HYP }      # a Linear team/project
  - { name: rig, forgejo: { url: https://git.hyperide.ai, repo: ultrabricks/rig-cli } }  # Forgejo (nested only)
```

The repo you're currently inside is always one of the groups, even if it isn't (yet) listed.

## Classification

`task classify "<text>"` decides `change` (→ a ticket), `actionable` (an instruction to
directly execute an existing command/tool right now — e.g. "run a review on the diff"; no
ticket, and actually invoking it is out of scope for this repo, see "Out of scope" below), or
`justAsk` (a pure question). Resolution is **pluggable** (`tasklib/classifiers.py`,
task-cli#206/#217): `classify.fallbacks` is walked in order, and each configured provider is
tried in turn until one is both AVAILABLE and actually produces a classification. Three
provider kinds:

- **`review-cli`** (every entry except `laya`/`jev`) — shells out to `review just-ask -m
  <model> --pool 1`. The model is the first available provider in that RUN of the chain
  (default head `claude-haiku-4-5`; degrades through OpenAI → commandcode → z.ai → Google →
  local ollama), so it works with whatever you have — offline via ollama, or on any one key.
  A subprocess error degrades to the bias default for that run (it never raises out of the
  shell-out itself), so this provider does not itself fall through to a later one in
  `fallbacks`.
- **`laya`** — a REAL, free, fully local classifier: Convai Innovations' Apache-2.0 "laya"
  PyPI package (an open-weights alternative to the paid cloud "Jev" model). Runs entirely
  in-process (no subprocess, no API key). **The zero-config DEFAULT (task-cli#208):** a repo
  with no committed `classify.fallbacks` tries `laya` FIRST, ahead of the hardcoded
  `review-cli` chain above. The real mechanism is `config.py`'s built-in `DEFAULTS` (the
  layer-0 config every repo starts from) listing `{laya: local}` first — the cascade merge
  replaces a `fallbacks:` list WHOLESALE, never item-by-item, so a repo's own committed
  `classify.fallbacks` (even one that never mentions `laya`) fully replaces this default and
  is never silently altered. `tasklib/classifiers.py`'s `build_classifier_chain` itself is
  UNCHANGED from before task-cli#208 (a `None` input still uses the plain, laya-free
  `classify.DEFAULT_FALLBACKS`) — it only groups whatever chain it's handed.

  **Auto-install.** `laya` is never a hard dependency and never imported at module top —
  `task --help` and every other command stay dependency-free whether or not it's installed.
  The first time `task classify` actually reaches the `laya` step and finds the package not
  importable, it auto-installs it once (`tasklib/laya_autoinstall.py`, task-cli#224) before
  falling through to the next provider, routed to avoid ever touching a PEP-668-managed
  system/Homebrew/pyenv Python's own site-packages rather than bypassing that protection:
  1. Already inside a virtualenv (`sys.prefix != sys.base_prefix` — e.g. `uv tool install
     hyper-task`, `pipx install hyper-task`, or a dev `.venv`): installs `laya>=0.3.7,<0.4`
     straight into it via `uv pip install --python <sys.executable>` when a trusted `uv` is on
     `$PATH` (a uv tool venv has no pip), else `<sys.executable> -m pip install` — always the
     exact interpreter `task classify` is already running under; the pin matches
     `pyproject.toml`'s `laya` extra exactly (an unpinned install could pull a breaking
     release `LayaClassifier`'s hand-parsed result shape can no longer read). A venv's own
     site-packages is already isolated and user-writable, so nothing further is needed.
  2. Not in a virtualenv (a system/Homebrew/distro Python, possibly PEP-668-managed):
     installs into a SEPARATE, dedicated venv this module owns and manages, under
     `~/.local/state/task-cli/laya_autoinstall/laya_venv-py<major.minor>` — created once
     (`uv venv --seed` when `uv` is on `$PATH`, else the stdlib `venv` module) and reused
     thereafter, with `laya` going
     into THAT venv's own interpreter. PEP 668's `externally-managed-environment` guard is a
     property of the SYSTEM interpreter's own site-packages; it never fires against pip
     running inside a venv, so this sidesteps the whole problem by construction — no retry
     flag, no touching Homebrew/system-owned files, ever. A successful install is made
     importable in the CURRENT process via `site.addsitedir()` on the venv's resolved
     site-packages path.

  On success, the SAME `task classify` invocation retries and proceeds with `laya`
  immediately — no re-exec, no second run needed. On failure, it falls through to the next
  provider exactly like an already-uninstalled `laya` always has, and caches the failure
  under `~/.local/state/task-cli/laya_autoinstall/` for 24h
  (`$TASK_LAYA_AUTOINSTALL_COOLDOWN_S` to override) so a subsequent call on an offline machine
  skips the network attempt entirely instead of paying a pip-timeout tax on every invocation.
  A corrupted/unreadable cache file fails OPEN (treated as "never attempted") rather than
  wedging auto-install off permanently. `install.sh` also attempts this same install
  (`scripts/lib/install_laya.sh`, a thin bash delegation straight into
  `tasklib.laya_autoinstall.install()` — no separate/duplicated implementation), best-effort,
  when you first install `task` — so on most machines `laya` is already present before you
  ever run `task classify`.

  Not installed (and auto-install also failed/was skipped), its first-run HuggingFace model
  download fails (no network/disk/rate-limit), or the model call itself errors (logged to
  stderr) → it reports itself unreachable and the chain falls through to the NEXT configured
  provider — the only provider kind that does (besides `jev`, below). Security: the
  `{ laya: <value> }` value only decides whether this entry becomes a laya provider at all —
  it is NEVER used to pick which model loads (always the built-in
  `convaiinnovations/laya` checkpoint), since `classify.fallbacks` can come from a
  repo-scoped `task.yaml`/`rig.yaml` that `task classify` — running inside the target repo
  it's ticketing — must not trust with an arbitrary Hub id to download and execute in-process.
  To opt back INTO an explicit chain (e.g. to reorder it, or to drop `laya` entirely),
  configure `classify.fallbacks` yourself — see the config block below. Its `priority`
  criteria are calibrated around concrete observables (production down / data loss / active
  security breach / blocks other engineers or most customers) rather than `CLASSIFY_PROMPT`'s
  abstract "drop everything .. low" framing (see the `priority` axis bullet below): that
  abstract framing showed zero discrimination on this local model against real,
  professionally-worded tickets, and the observable-based wording is a real, measured fix for
  it — see the CALIBRATION comment in `tasklib/classifiers.py` for the evidence.
- **`jev`** (task-cli#217) — a REAL, paid, cloud classifier: TypeSafe AI's "Jev" evaluation
  model, called directly over Vercel AI Gateway's `POST https://ai-gateway.vercel.sh/v1/
  evaluate` HTTP endpoint (model id `typesafe-ai/jev`) via stdlib `urllib` — no `review`, no
  subprocess, no extra dependency. Needs `AI_GATEWAY_API_KEY` in the environment; set
  `TASK_JEV_ENV_FILE=/path/to/.env` to ALSO read that one `AI_GATEWAY_API_KEY=<value>` line
  from a `.env`-shaped file when the env var itself is absent — an explicit, per-machine
  opt-in (unset by default, never a hardcoded path). Never a zero-config default — opt-in only, since it costs
  money and needs network. Missing key, a network/timeout error, ANY non-2xx HTTP status
  (including Vercel's documented `customer_verification_required` "no card on file" billing
  block), or a malformed response body all report unreachable/fail → the chain falls through
  to the NEXT configured provider, same graceful-degradation contract as `laya`. Security:
  same posture as `laya` — the `{ jev: <value> }` value only decides routing, never which
  endpoint/model gets called (both fixed), so a repo-scoped config can't redirect the call
  (and your API key) to an attacker-controlled host.

**Speed: the warm `laya` server (task-cli#245).** Loading the laya checkpoint costs ~3 s and
every `task classify` is a fresh process, so the `laya` provider does not load the model
itself: the first call spawns a detached, machine-wide server
(`python -P -m tasklib.classify_server`, `tasklib/classify_server.py`) that loads the model
once and answers over a unix socket under `~/.local/state/task-cli/classify/` (dir 0700,
socket 0600, named after the task-cli version, log alongside it). A server already running
under ANY version's socket is reused, newest first, so an upgrade never starts a second model
next to the previous version's server (task-cli#282). Later calls are one socket
round-trip; the server exits after 15 minutes without a request. Measured on the M-series dev
host: cold ~4 s (spawn + load), warm ~0.3 s, versus 4-8 s for EVERY call before. If the server
cannot start or answer, the provider loads the model in-process exactly as before.
`TASK_CLASSIFY_SERVER=0` keeps classification in-process. The model load is quiet — no
HuggingFace "Fetching N files" bar and no laya "invalid temperatures" RuntimeWarning — unless
`task classify --verbose` (which skips the warm server and loads in-process, so that output
reaches your terminal).

**Starting it on demand: `task classify-server start` (task-cli#279).** For a caller that needs
the warm server rather than a fallback (text-lint, where laya is mandatory): if the server
already accepts connections (under any version's socket) it is left alone and that socket is
printed, otherwise the command starts it (see below) and waits up to 60 s for a socket to
accept. On success it prints only the socket path on stdout and exits 0. Otherwise it exits 1
with one `error:` line on stderr naming the reason: disabled by `TASK_CLASSIFY_SERVER=0`, a
socket directory that is not yours with mode 0700, a unit or spawn that fails (the unit's
message names the unit), or the server exiting or not listening within 60 s (those last two
say where its log is). Concurrent starts are safe: the server's singleton lock loads the model
once and every caller waits for that server.

**Who runs the server: a service-manager unit when installed (0.24.0).** When nothing answers,
`task classify` and `task classify-server start` start the user unit `ai.hyperide.laya.service`
(override the name with `TASK_CLASSIFY_UNIT`), so the server is not a child of whichever agent
or hook asked for it:

- Linux: the unit counts as installed when `systemctl --user cat <unit>` succeeds; it is started
  with `systemctl --user start <unit>`, and a failed unit is reported with
  `journalctl --user -u <unit>`.
- macOS: when `~/Library/LaunchAgents/ai.hyperide.laya.plist` exists (the label is the unit
  name without `.service`), it is started with `launchctl kickstart gui/<uid>/ai.hyperide.laya`.
  If that fails because the agent is not loaded yet, it is loaded with `launchctl bootstrap
  gui/<uid> <plist>` and kicked once more.
- No unit installed: the server is spawned detached, as before. The same happens when the
  unit's server would bind a different state dir than the caller's (the unit gets the service
  manager's environment, e.g. a caller-only `XDG_STATE_HOME` override), since it could never
  answer on the caller's socket.

The unit runs the foreground `task classify-server serve`, which binds the installed version's
socket, takes the same singleton lock and exits 0 after 15 idle minutes. A minimal systemd unit
(`~/.config/systemd/user/ai.hyperide.laya.service`; no `[Install]` section, it is started on
demand):

```ini
[Unit]
Description=Laya classify server (task-cli)

[Service]
ExecStart=%h/.local/bin/task classify-server serve
```

**Batch mode.** `task classify --batch` reads JSONL on stdin — one `{"text": "...", "id": ...}`
object per line, `id` optional and echoed back — and writes one `--json`-shaped verdict object
per line to stdout, building the provider chain once for the whole batch (for grooming
passes). Classification only: `--batch` refuses `--create`/`--update` and a positional text.
A malformed line yields `{"line": n, "error": ...}` and the batch continues, exiting 2.

**Guard rules** (`classify.is_small_talk` / `classify.looks_like_bug_report`, applied around
every provider): a greeting/acknowledgement or a text of at most two letters (`hi`, `thanks!`,
`ok`) is `justAsk` without running any provider, and plain defect prose that is not phrased as
a question (a crash/leak/"fails"/"does not remove"/HTTP 5xx report of five words or more) is
`change` when the provider said `justAsk` (an `actionable` verdict is kept: an execute-this
instruction never becomes a ticket, task-cli#80) — the local model's verdict probabilities are
close to uniform on exactly these shapes (live: "hi" → `actionable`, the tg-cli#486 leak
report → `justAsk`). The `classify` log event's `model` field records a rule's involvement
(`rule:small-talk`, `laya:…+rule:bug-report`).

**Bias is to `change` on ambiguity between change/justAsk** — most questions to a dev agent
are latent change requests; that bias never applies to a genuine `actionable` signal, which is
only recognized via an explicit `actionable` verdict. `task classify "<text>" --create` is the
entry point the `tg-cli` inbound hook calls.

**Multi-ask detection** (`tasklib/classify.py`'s `detect_multi_ask`) splits a message that
structurally bundles several distinct asks — a numbered/bulleted list, or clauses joined by an
explicit enumeration marker (English "and also", or the Russian equivalents of "and also
also"/"also"/"in addition" — see `_ENUMERATION_SEP_RE` in `classify.py` for the exact patterns
this repo's English-only-docs rule keeps out of here — or a semicolon) — into separate items
BEFORE classification, printing "N distinct asks detected" and running the rest of `classify`
once per item (so `--create` files one ticket per ask instead of one vague ticket for all of
them). An ordinary single-topic message, however long, is never split — a bare English "and"
(or its Russian equivalent) is deliberately NOT a signal (see the module docstring for why).
Only applies to `--create`; `--update <ID>` names one explicit target for the whole raw message
and is left unsplit. Each split item is classified (verdict + the three axes below)
INDEPENDENTLY, so a message that bundles a product bug and a process step in one batch routes
each correctly — one becomes a GitHub/Linear ticket, the other a local `harness` task.

**SP / priority / process-vs-product axes.** On a `change` verdict the SAME model round-trip
(one shell-out, not two) also resolves three ticket-shaping axes, printed under the verdict
(`--json` gains `process_or_product`/`priority`/`story_points`; `actionable`/`justAsk` never
surface them — they mint no ticket, so the axes are moot):

- **`story_points`** — one of `1, 2, 3, 5, 8, 13`, a Fibonacci-ish COMPLEXITY estimate, not a
  time estimate: `1` trivial/typo-level, `2` small single-function fix, `3` moderate/
  single-file, `5` substantial/multi-file or new test infra, `8` large/new subsystem or
  design-heavy, `13` epic/major feature from scratch.
- **`priority`** — one of `P0` (drop everything) .. `P3` (low).
- **`process_or_product`** — `process` (a session-local/agent process step — reviewing an
  uncommitted diff, a pre-commit checklist) vs `product` (durable product work). On `--create`,
  a `process` item routes to a LOCAL `harness tasks new --title=<derived> --body=<message>`
  ticket instead of the configured GitHub/Linear backend (glued `=`-form; no dedup, no session
  recording — a repeated message mints a fresh local task each time, unlike the product path).
  A MISSING `harness` binary never drops the item — it warns and falls back to the normal
  product-ticket path; a harness binary that's present but fails AT RUNTIME still errors
  cleanly (`error:` line, never a traceback) with no fallback. A `product` item creates the
  ticket exactly as before, additionally labeled `sp:<n>` and `priority:<Pn>`. Set
  `classify.route_process_to_harness: false` to keep every item on the product-ticket path
  regardless of this axis. An axis the model doesn't cleanly answer biases to a SAFE default
  (`product`/`P2`/`3`) — never silently to neither store.

## Config — `rig.yaml` `task:` block (per-repo) + `task.yaml` + global

The per-repo tracker backend is selected from the repo's committed **`rig.yaml`** — the single
source of truth for the whole agent toolchain (rig provisions it). Drop a `task:` block in:

```yaml
# rig.yaml (repo root) — selects the tracker backend for this repo
task:
  backend: linear      # or github-issues (the default), tasks, forgejo
  team: HYP            # → linear.team (Linear coordinate)
  # project: ""        # → linear.project
  # repo: owner/name   # → github.repo (for the github-issues backend)
```

The block is intentionally flat: `team`/`project`/`repo` are shorthands translated onto
task-cli's own config shape, and the full sections (`github:`/`linear:`/`enforce:`/`classify:`/
`session:`/`projects:`) may be nested in verbatim for fine control. Unknown
sub-keys under `task:` are warned-and-ignored, never fatal — `rig.yaml` is owned by rig-cli and
a newer key must not crash an older task-cli. **DEFAULT = GitHub Issues**: a repo with no
`task:` block (or no `rig.yaml`) falls through cleanly to `github-issues`. A repo that keeps a
native `task.yaml` still has it win (the cascade is defaults → global → `rig.yaml` task: →
`task.yaml` → `--config`).

The full native shape (also accepted as `task.yaml`, or nested under `rig.yaml` `task:`):

```yaml
version: 1
backend: github-issues            # or: linear | tasks | forgejo
github:   { repo: auto, default_labels: [agent], attachment_mode: native }  # repo: auto = origin owner/name
linear:   { team: HYP, project: "", attachment_mode: native }  # attachment_mode: native|link — same knob, every backend
forgejo:  { url: https://git.hyperide.ai, repo: owner/name, attachment_mode: native }  # see "Forgejo backend"
trusted_attachment_hosts: []      # extra hosts `task read --save-attachments` may auto-fetch
                                   # from beyond the tracker's own asset domain (e.g. your own
                                   # GitHub Pages host) — fetched WITHOUT the tracker's auth
                                   # header. Empty by default (secure-by-default).
projects:                          # cross-project registry (mostly in the GLOBAL config)
  - { repo: acme/frontend }        # `task list` outside a repo / `--all` aggregates these
  - { name: HYP, backend: linear, team: HYP }
enforce:
  acceptance_criteria: required
  motivation: required
  user_impact: required
  cost_of_inaction: required
  formatting: strict
  links_url: required                # the ## Links section holds http(s):// URLs only
  msgref_quote: required             # a body tg#<id> must carry its auto-attached quote
  read_before_edit: true             # `change --what` needs a recent `read` this session first
  todo_shaped_hint: true             # non-blocking "this looks like a process step, not a
                                      # durable ticket" hint on create (--skip-todo-shaped to
                                      # silence a false positive)
  duplicate: true                    # governs BOTH duplicate-detection mechanisms on create
                                      # (near-exact-title hard block + the token/Jaccard net)
  screenshots:
    on_create: { required_if_label: [ui, visual] }
    on_done:   { required_if_label: [ui, visual] }
  escape_hatch: explain
classify:
  capability: ""                     # optional (rig#8): a role/capability tag (e.g. `fast` /
                                     # `reasoning` / `code`) resolved from the shared model
                                     # manifest (agent-tools `lib/contracts/models.yaml`) and
                                     # PREFERRED ahead of `fallbacks`. Empty → manifest unused.
                                     # The manifest's exact model id is used for every provider
                                     # EXCEPT gemini (there it steers the provider/role only —
                                     # review's gemini backend picks the version). Fail-soft: a
                                     # missing manifest / resolver falls through to `fallbacks`.
                                     # Point at a manifest with $TASK_MODELS_MANIFEST.
  fallbacks:
    - { anthropic:   claude-haiku-4-5 }
    - { openai:      gpt-5-mini }
    - { commandcode: deepseek/deepseek-v4-flash }
    - { zai:         glm-4.6-flash }
    - { google:      gemini-2.5-flash }
    - { ollama:      qwen2.5:3b }
    # - { laya: local }               # explicit opt-in equivalent to the IMPLICIT zero-
    #                                 # config default (task-cli#208 -- an unconfigured
    #                                 # `fallbacks:` already tries `laya` first automatically;
    #                                 # only list it here to pin its position when the rest of
    #                                 # this chain is also explicitly configured, e.g. to try
    #                                 # it AFTER a paid provider instead of first). See
    #                                 # "Classification" above for auto-install + the extra.
    # - { jev: cloud }                # opt-in: a REAL, paid, cloud classifier -- TypeSafe
    #                                 # AI's "Jev" model over Vercel AI Gateway's
    #                                 # /v1/evaluate endpoint. Needs $AI_GATEWAY_API_KEY; the
    #                                 # value here (`cloud`) is a free-form label only -- see
    #                                 # "Classification" below, `jev` never lets a configured
    #                                 # value pick the endpoint/model.
  bias: change
  route_process_to_harness: true     # `process`-classified `--create` items file a local
                                     # `harness tasks new` ticket instead of the product
                                     # backend; false keeps everything on the product-ticket
                                     # path regardless of the process/product axis
session:
  detect: [env:TASK_SESSION, tmux-pane, git-branch]
  label_prefix: "session:"
daemon:                              # the due-date reminder watcher (all keys optional)
  enabled: true                      # false → both `daemon start` AND `daemon run` are no-ops
  interval_s: 3600                   # poll interval (seconds); a 0/negative value falls back
  due_soon_days: 3                   # remind when due within N days (or already overdue)
  query_limit: 100                   # tickets fetched per tick; raise it for a big/old project
  notifier: [tg, --tag, report]      # the reminder command; the message is appended as the last arg
```

Config is committed by default and scoped by location, never a flag. With no config at all the
tool defaults to `github-issues` with every gate on, `classify.fallbacks` trying `laya` first
(task-cli#208, auto-installed on demand — see "Classification" above), and the daemon's
built-in defaults above.

**`trusted_attachment_hosts` — what it does and does not protect against.** Entries are
validated as bare hostnames: no scheme/path/port/userinfo, no IP literal (canonical or any
getaddrinfo-legacy encoding — decimal, hex, zero-padded, per-label-mixed), no `localhost`/
`*.localhost`/`localhost.localdomain`. That closes config typos and known loopback aliases. It
does **not** and cannot close a genuinely resolvable DNS name that a wildcard-DNS-to-IP service
(`nip.io`, `sslip.io`, and similar) maps to an internal address, or DNS rebinding — those need
resolving the hostname and validating the actual IP at connect time, out of scope for this
config-string check. Only list a domain here that you actually control.

When `classify.capability` is set, the model manifest is located by probing a few conventional
`agent-tools` checkout paths; set `$TASK_MODELS_MANIFEST` to an explicit `models.yaml` to make the
choice deterministic on a machine with more than one checkout (it always wins over the heuristics).

**Per-machine override — `$TASK_CLI_ATTACHMENT_MODE`** (tg#11652; extended from Linear-only to
BOTH backends by tg#16794/review-cli#484): a repo can pin `linear.attachment_mode` and/or
`github.attachment_mode` for everyone (e.g. hyperide pins Linear's to `link`, HYP-1248 — raw
pasted `uploads.linear.app` URLs 401 for a non-browser viewer). Set
`TASK_CLI_ATTACHMENT_MODE=native` (or `link`) in your own shell/machine to override BOTH pins
locally without touching the committed `task.yaml`/`rig.yaml` — it is the single
highest-precedence layer in the cascade, above even an explicit `--config P` (deliberate: it's a
per-machine convenience override, meant to win over anything file-based), and is still validated
(an unrecognized value fails closed, same as the file-based setting). If a `task` invocation's
`attachment_mode` looks wrong and `--config` doesn't explain it, check
`echo $TASK_CLI_ATTACHMENT_MODE`. GitHub's `native` mode uploads through an undocumented
endpoint (see `backends/github_issues.py`'s module docstring); set `attachment_mode: link` for
that backend if you'd rather not depend on it.

### Forgejo backend

`backend: forgejo` files tickets as native issues of a Forgejo repository (Forgejo REST API,
`<url>/api/v1`) — not a GitHub alias and not a shared tracker for many repositories: every
repository keeps its own issues. Put the route in the repository's own `rig.yaml`:

```yaml
task:
  backend: forgejo
  forgejo:
    url: https://git.hyperide.ai   # the forge origin; https, no path (/api/v1 is appended)
    repo: owner/name               # explicit in committed config
    attachment_mode: native        # native = upload as an issue attachment; link = URLs only
```

- **`repo`** must be explicit in committed/published config. At runtime `repo: auto` (or an
  absent `repo`) resolves the git `origin` remote, and only when that remote is on `url`'s host —
  a GitHub (or other-host) origin is an error, never a silent fallback. A `projects:` registry
  entry always needs an explicit `repo`. The flat `repo:` shorthand in `rig.yaml` `task:` stays
  GitHub's; Forgejo uses the nested `forgejo:` block.
- **Precedence** is the normal cascade (defaults → rig-global → task-cli global → ancestor
  `rig.yaml` `task:` blocks, outer to inner → the repo's `rig.yaml` → `task.yaml` → `--config`):
  a nearer layer wins key by key. An ancestor `rig.yaml` that is not owned by you, or that
  (or whose directory) is writable by other users — for example one planted in `/tmp` — is
  skipped with a warning. **Inheritance** (a cross-repo contract shared with rig-cli, so
  `rig` and `task` read an inherited block the same way): a folder-level `rig.yaml` (for
  example `~/work/rig.yaml`) passes every `task:` key down to the repositories below it,
  `forgejo.repo` included — only `code_prefix` is never inherited. A folder-wide default should
  therefore say `forgejo.repo: auto` (each repository resolves its own origin), while an
  explicit `owner/name` in a repository's own `rig.yaml` also carries into its nested
  worktrees.
- **Stale overrides:** a repository hosted on a Forgejo forge but still routed to
  `github-issues` fails with `unrecognized github remote URL` plus a pointer at this block.
  A leftover `task.yaml` `backend:`/`github:`/`linear:` override beats `rig.yaml`, so remove it
  when moving a repository to Forgejo.
- **Credentials:** `$FORGEJO_TOKEN` (honored only when `$FORGEJO_URL` names the same origin,
  so a repository's config cannot aim an ambient token at another host), else the `fj` CLI's
  own store (`keys.json`, macOS:
  `~/Library/Application Support/forgejo-cli.forgejo-cli/`; Windows:
  `%APPDATA%\forgejo-cli\forgejo-cli\data\`; Linux, best effort:
  `$XDG_DATA_HOME/forgejo-cli/`) — only the entry for exactly `url`'s host. `fj` host aliases
  are never followed. Log in once with `fj -H <host> auth login` (or `auth add-token`). An
  `auth login` (OAuth) token expires after about an hour and fj refreshes it only when fj
  runs, so when the stored one has expired `task` runs `fj -H <host> whoami` once (fj
  refreshes and rewrites its store) and re-reads it; without `fj` on `PATH` it fails with the
  usual "no Forgejo token" error.
- **Transport:** every request goes to `url` + `/api/v1` with the token, and a redirect to
  another host (or from https to http) is refused, so the token cannot leave that origin.
  Attachment downloads send the token only to the same origin.
- **Ids:** `#N`, `N`, `owner/repo#N`, or the issue's full URL (the qualified forms only for the
  current repository on the configured origin).
  Issue numbers are per repository; the project coordinate is `forgejo:<url>/<owner>/<repo>`.
  Pull requests share the number space and are never treated as issues.
- **Due dates** are Forgejo's native issue due date. When the issue has one, it wins over a
  `## Due` line in the body; setting or clearing a due date never rewrites the body.
- **Minimal writes:** an update sends only the fields that changed since `task` read the issue,
  and replaces labels only when the label set changed. It first re-reads the issue and refuses,
  before writing anything, if the issue changed on the server since that read.
- **Attachments** (`native`): the file is uploaded as an issue attachment and its bytes are
  downloaded back (same origin, with the token) and compared before it counts as proof. If the
  follow-up comment linking it fails, the attachment still exists: `task` reports the partial
  success and does not upload it again.
- **Body safety:** label/state/due changes and comments never rewrite the issue body. When the
  stored Markdown holds content outside task-cli's section template (a provenance preamble,
  hand-written sections, HTML comments), the body is never re-rendered: `task check` rewrites
  only the ticked criteria's checkbox lines and `--skip-<gate>` reasons go into the last
  `## Skipped gates` section (appended at the end when there is none), every other byte kept —
  so such a ticket can still be checked off and closed with `task done`. Any other body edit
  on it (prose, links, screenshots, adding/rewording criteria) is refused — edit such an issue
  in Forgejo or use `task comment`. That includes `task attach` on such an issue: the upload
  and its comment succeed, the body's Screenshots section update is refused.

**API limitations (Forgejo 16):** issues have no version, ETag or `If-Match` support
(`EditIssueOption.updated_at` only overrides the timestamp; it is not a concurrency check). The
stale-read refusal above narrows the window but is not an atomic compare-and-set: an edit that
lands between `task`'s re-read and its write is still overwritten (last write wins). An update
can also take two requests (the issue `PATCH`, then the labels `PUT`, because the issue edit
endpoint has no labels field), so a failure between them can leave the new title/state with
the old labels. The issue-list `limit` is capped server-side (50 by default), so `task` pages
until it has enough matches or the list ends; a listing that hits the 200-page safety cap
prints a `truncated` warning instead of passing a short result off as complete.

## Architecture

- `bin/task` — thin shim → `tasklib.cli:main`.
- `tasklib/cli.py` — argparse + dispatch + the effects (backend calls, the classify shell-out,
  sidecar writes). Kept thin.
- Pure core (no provider I/O): `model.py` (the `Ticket`), `render.py` (template ↔ Ticket),
  `policy.py` (the gates), `classify.py` (chain resolution + verdict parse), `session.py`
  (detection + sidecar), `config.py` (cascade loader).
- `tasklib/backends/` — the `TicketBackend` protocol + `github_issues.py` (REST),
  `linear.py` (GraphQL), `tasks.py` (tasks-app REST) and `forgejo.py` (Forgejo REST), each
  calling the API directly via the tiny `http.py` urllib helper.
- `tasklib/credentials.py` — harvest tokens from existing CLI configs.
- `tasklib/logging.py` — structured JSONL in the `agenttools_log` shape, with secret redaction.

## Tests

```bash
python3 -m pytest -q     # the unit suite (FakeBackend; never hits live GitHub/Linear)
bash tests/smoke.sh      # --help, every subcommand --help, lazy-import invariant, pytest
```

## Roadmap (what v1 does NOT do yet)

v1 is the usable core: `new`/`create`, `list`, `read`, `find`, `change`, `done`, `status`,
`classify`, `session` against GitHub Issues (default) and Linear, with the enforcement gates.
Deferred to follow-up issues:

- **Dependency system + Gantt rendering** — [#1](https://git.hyperide.ai/ultrabricks/task-cli/issues/1).
- **Daemon service + webhooks** (adapter-based trackers, survives restarts) —
  [#2](https://git.hyperide.ai/ultrabricks/task-cli/issues/2).
- **Completion + due-date notifications** (tmux-inject into the agent pane) —
  [#3](https://git.hyperide.ai/ultrabricks/task-cli/issues/3).
- **Integrations** — tg classify-on-inbound hook, the agent-tools `require-ticket-before-commit`
  guard, and rig cross-repo provisioning — [#4](https://git.hyperide.ai/ultrabricks/task-cli/issues/4).

## License

MIT.

<!-- rig:ecosystem-block -->
## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://git.hyperide.ai/ultrabricks/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[review-cli](https://git.hyperide.ai/ultrabricks/review-cli)** — multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.
- **[agent-tools](https://git.hyperide.ai/ultrabricks/agent-tools)** — the shared catalog `rig` applies: portable agent skills, agent-hooks, the global git-hook dispatcher, CI gates, and MCP servers
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[dev-cli](https://git.hyperide.ai/ultrabricks/dev-cli)** — project-scoped dev/e2e process runner (start/list/stop dev servers and e2e jobs); rig validates the `scripts:` / `dev:` config shape and provisions `dev:*` / `Bash(dev:*)` as the harness permission surface, without granting raw process/git/package-manager tools
- **[research-cli](https://git.hyperide.ai/ultrabricks/research-cli)** — multi-provider research / panel CLI: puts a question to a panel of models, each through a research lens, then synthesizes one attributed, fact-checked note
- **[pm-cli](https://git.hyperide.ai/ultrabricks/pm-cli)** — autonomous project-manager coordinator over the task/tg/rig ecosystem: keeps a work queue as a deterministic projection of an append-only event log, reconciling on unforgeable evidence rather than dispatching or editing code itself
- **[rig-cli](https://git.hyperide.ai/ultrabricks/rig-cli)** — sets up a repo (and a dev machine) from a committed `rig.yaml`: skills, agent-hooks, git-hook dispatcher, CI gates, MCP, and the personal CLI ecosystem itself
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.
<!-- /rig:ecosystem-block -->
