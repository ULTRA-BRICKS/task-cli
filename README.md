# task-cli

**The enforced interface to the ticket system.** Every request becomes a durable,
well-formed ticket the moment it arrives — `task` enforces ticket *quality* (acceptance
criteria, motivation, user-impact, cost-of-inaction, screenshots, formatting) in the tool
itself, not by convention. A ticket and its PR speak one shape.

A standalone Python CLI, peer to [`review-cli`](https://github.com/alex-mextner/rig-cli),
[`rig-cli`](https://github.com/alex-mextner/rig-cli), and `tg-cli`. Backends: **GitHub Issues
(default)** and **Linear** (per-repo). Stdlib-first; the backends call the provider API
directly (no `requests`, no per-call subprocess), and credentials are harvested from the CLIs
you already authed — zero extra setup.

> Why this exists: agents drop requests, lose the thread, and produce work nobody can trace
> back to an ask. `task` makes "promise = durable action" mechanical — every request becomes a
> well-formed ticket, and `task list` always answers "what am I doing for you right now".

## Install

```bash
git clone https://github.com/alex-mextner/task-cli && cd task-cli && ./install.sh
# or, one-liner:
curl -fsSL https://raw.githubusercontent.com/alex-mextner/task-cli/main/install.sh | bash
```

The installer symlinks `bin/task` into `~/.local/bin` and registers the agent skill. The only
runtime dep is `pyyaml` (for `task.yaml`); without it the tool falls back to built-in defaults.

**Credentials** are read from the CLIs you already use — run these once and you're done:

```bash
gh auth login      # GitHub Issues (the default backend)
linear auth        # Linear (per-repo, via the rig.yaml task: block)
```

`task` reads `gh auth token` / `~/.config/linear/credentials.toml` (or `$GITHUB_TOKEN` /
`$LINEAR_API_KEY`) and calls the API directly. Tokens are never logged or persisted.

## Commands

```
task new --title "..." --why "..." --impact "..." --if-not-done "..." --acceptance "..." --acceptance "..." [--due YYYY-MM-DD]
task create                        # alias of `new` (same arguments, same gates)  [--force "<reason>"]
task list                          # THIS session's tickets (falls back to all when empty)
task list --all                    # every known project's tickets, grouped by project
task gantt        [--all] [--json] # read-only due-date timeline (Gantt) — see below
task read <id>     (alias: view)   # the full ticket — every section (works outside a repo)
task find "<query>"                # search title+body (cross-project when outside a repo)
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
task done <id>    [--screenshot p] # close a ticket — runs the on-done gates (all criteria checked)
task change <id>  [--due ...] [--done]  # update; --due sets/clears the due date; --done closes (gates)
task status <id> [<new-state>]     # read or transition state (works outside a repo)
#   close/transition verbs validate legality first: a cancelled ticket is a dead-end and a
#   re-close of an already-done ticket is rejected (no silent re-write). `--force` overrides.
task classify "<text>" [--create]  # change|actionable|justAsk via review; --create makes/dedups a ticket
task session [show|bind <id>]      # show/bind the current session and its tickets
task daemon start|stop|status|run  # the due-date reminder watcher (see Daemon below)
task audit [<id>] [--revert]       # reconciliation backstop — flags a Done ticket that never
#   passed the close gates (see Audit below)
```

Global flags: `-C/--cwd`, `--backend`, `--repo`, `--config`, `--json`, and the per-gate escape
hatch `--skip-<gate> "<reason>"`.

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
none of its gates ever run. This is exactly how ticket HYP-1347 was closed in August 2026, with
all 4 acceptance criteria still unchecked and no screenshot attached — nobody ran `task done`,
Linear's own PR-merge automation closed it.

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
   overrides a genuine false positive.
5. **Screenshots** — required at *creation* and at *done* for UI/visual tickets (label-gated,
   configurable). The on-done gate demands the **implementation** proof specifically — a
   creation mock does not let you close a UI ticket. That is why the gate runs twice.
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
| junk-title (create, and any `--title` edit) | don't phrase a title as "Review/Audit/Adversarial [review of] ... diff" — that shape can never lead to a merge on its own; post review findings as a PR comment or run `review diff`/`review quorum` directly instead | **none** (hard; disable the check itself only via `enforce.junk_title: false`) |
| acceptance-checked (close) | `task check <id> <n> --proof <path>` for each | **none** (hard; disable in config only) |
| read-before-edit (`change --what` only) | `task read <id>` (or `task view <id>`) THIS session, recently, before overwriting `--what` | `--skip-read-before-edit "<reason>"` — a *persisted* skip from an earlier command never silently waives a later, still-unread one |

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
## Links
```

This is the same section set as the agent-tools `pull_request_template.md`, so a ticket and
its PR speak one shape. `render.py` is the single source of truth — fields are authoritative,
the body is derived.

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
  registered project (a Linear `HYP-3` by its team; a `#123` when exactly one GitHub project
  is registered); an ambiguous id fails with a clear, actionable error.
- **Only `task new`/`create` is repo-bound** — it writes a ticket into one specific project, so
  it needs a repo (or `--repo owner/name`). Outside one it fails with a 3-part WHAT/WHY/HOW error.
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
```

The repo you're currently inside is always one of the groups, even if it isn't (yet) listed.

## Classification

`task classify "<text>"` decides `change` (→ a ticket), `actionable` (an instruction to
directly execute an existing command/tool right now — e.g. "run a review on the diff"; no
ticket, and actually invoking it is out of scope for this repo, see "Out of scope" below), or
`justAsk` (a pure question) by shelling out to `review just-ask -m <model> --pool 1`. The
model is the **first available** in a per-provider fallback chain (default head
`claude-haiku-4-5`; degrades through OpenAI → commandcode → z.ai → Google → local ollama), so
it works with whatever you have — offline via ollama, or on any one key. **Bias is to
`change` on ambiguity between change/justAsk** — most questions to a dev agent are latent
change requests; that bias never applies to a genuine `actionable` signal, which is only
recognized via the model's explicit `VERDICT: actionable` sentinel. `task classify "<text>"
--create` is the entry point the `tg-cli` inbound hook calls.

## Config — `rig.yaml` `task:` block (per-repo) + `task.yaml` + global

The per-repo tracker backend is selected from the repo's committed **`rig.yaml`** — the single
source of truth for the whole agent toolchain (rig provisions it). Drop a `task:` block in:

```yaml
# rig.yaml (repo root) — selects the tracker backend for this repo
task:
  backend: linear      # or github-issues (the default for every other repo)
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
backend: github-issues            # or: linear
github:   { repo: auto, default_labels: [agent] }   # repo: auto = origin owner/name
linear:   { team: HYP, project: "", attachment_mode: native }  # attachment_mode: native|link
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
  bias: change
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
tool defaults to `github-issues` with every gate on (and the daemon's built-in defaults above).

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

**Per-machine override — `$TASK_CLI_ATTACHMENT_MODE`** (tg#11652): a repo can pin
`linear.attachment_mode` for everyone (e.g. hyperide pins `link`, HYP-1248 — raw pasted
`uploads.linear.app` URLs 401 for a non-browser viewer). Set `TASK_CLI_ATTACHMENT_MODE=native`
(or `link`) in your own shell/machine to override that pin locally without touching the
committed `task.yaml`/`rig.yaml` — it is the single highest-precedence layer in the cascade,
above even an explicit `--config P` (deliberate: it's a per-machine convenience override, meant
to win over anything file-based), and is still validated (an unrecognized value fails closed,
same as the file-based setting). If a `task` invocation's `attachment_mode` looks wrong and
`--config` doesn't explain it, check `echo $TASK_CLI_ATTACHMENT_MODE`.

## Architecture

- `bin/task` — thin shim → `tasklib.cli:main`.
- `tasklib/cli.py` — argparse + dispatch + the effects (backend calls, the classify shell-out,
  sidecar writes). Kept thin.
- Pure core (no provider I/O): `model.py` (the `Ticket`), `render.py` (template ↔ Ticket),
  `policy.py` (the gates), `classify.py` (chain resolution + verdict parse), `session.py`
  (detection + sidecar), `config.py` (cascade loader).
- `tasklib/backends/` — the `TicketBackend` protocol + `github_issues.py` (REST) and
  `linear.py` (GraphQL), each calling the API directly via the tiny `http.py` urllib helper.
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

- **Dependency system + Gantt rendering** — [#1](https://github.com/alex-mextner/task-cli/issues/1).
- **Daemon service + webhooks** (adapter-based trackers, survives restarts) —
  [#2](https://github.com/alex-mextner/task-cli/issues/2).
- **Completion + due-date notifications** (tmux-inject into the agent pane) —
  [#3](https://github.com/alex-mextner/task-cli/issues/3).
- **Integrations** — tg classify-on-inbound hook, the agent-tools `require-ticket-before-commit`
  guard, and rig cross-repo provisioning — [#4](https://github.com/alex-mextner/task-cli/issues/4).

## License

MIT.

<!-- rig:ecosystem-block -->
## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[review-cli](https://github.com/alex-mextner/review-cli)** — multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.
- **[agent-tools](https://github.com/alex-mextner/agent-tools)** — the shared catalog `rig` applies: portable agent skills, agent-hooks, the global git-hook dispatcher, CI gates, and MCP servers
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[dev-cli](https://github.com/alex-mextner/dev-cli)** — project-scoped dev/e2e process runner (start/list/stop dev servers and e2e jobs); rig validates the `scripts:` / `dev:` config shape and provisions `dev:*` / `Bash(dev:*)` as the harness permission surface, without granting raw process/git/package-manager tools
- **[research-cli](https://github.com/alex-mextner/research-cli)** — multi-provider research / panel CLI: puts a question to a panel of models, each through a research lens, then synthesizes one attributed, fact-checked note
- **[pm-cli](https://github.com/alex-mextner/pm-cli)** — autonomous project-manager coordinator over the task/tg/rig ecosystem: keeps a work queue as a deterministic projection of an append-only event log, reconciling on unforgeable evidence rather than dispatching or editing code itself
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — sets up a repo (and a dev machine) from a committed `rig.yaml`: skills, agent-hooks, git-hook dispatcher, CI gates, MCP, and the personal CLI ecosystem itself
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.
<!-- /rig:ecosystem-block -->
