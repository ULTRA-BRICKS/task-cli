# Tasks backend continuation — 17 September 2026

User requests native Tasks support for tasks-ultrabricks.vercel.app in this task-cli, alongside completion of the Linear migration into Tasks.

Current branch: feat/tasks-app-backend-20260917, based on main c663520. Existing task-cli is installed at ~/.files/bin/task. Its behavior remains unchanged; Tasks is not yet a registered backend.

Added tests/test_tasks_backend.py as a hermetic red-first specification. Initial pytest failed during collection because tasklib.backends.tasks does not exist; this is expected missing-implementation evidence, not a passing test. Log: /private/tmp/task-cli-tasks-red.log.

The attempted adapter creation and syntax-check command was blocked before execution by the tool safety check (could not determine safety status). The adapter file was NOT written. Do not reroute the rejected write through another tool or represent it as completed. No commit, push, installation or credential change occurred.

Implementation requirements: native Tasks REST, full TicketBackend protocol, stdlib-only imports, versioned writes using the version actually read, no automatic ambiguous-write retries, cursor pagination, native workflow/label/session mapping, private attachment upload and byte readback, origin-bound reuse of the native CLI credential, no use of Linear credentials, and no silent rewriting of unmanaged Markdown.

Integrate the adapter through get_backend, validated tasks configuration, provider/origin-specific coordinates, project registry and generic team-scoped routing. Preserve all existing quality and read-before-edit gates. Add tests for transport errors, cross-origin redirects, credential-origin mismatch, stale versions, pagination, aliases, body preservation, attachments and cross-provider routing ambiguity.

Native tasks-app CLI already exists and its --help was executed successfully from /Users/ultra/xp/tasks-linear-import-20260916/packages/cli/tickets.mjs. The tasks-app command itself is not on PATH. Do not confuse working --help with authentication or live Tasks CRUD.

Production was read through Neon for ultra@glide.vc: owner of ultrabricks workspace, schema4, zero issues and projects. No production migration/import in this continuation. Tasks-app's separate checkpoint records known review findings and prepared schema5–8 migration.

## Production import is now complete

The import-first continuation completed the live Tasks migration on17September2026. Canonical site tasks-ultrabricks.vercel.app now has1717issues,1341comments,12projects,7milestones,4documents,1initiative,826relations and5393history events in ultra@glide.vc's ultrabricks workspace.184private source files were verified through production Blob;27upstream404references preserved. No accounts/passwords/memberships changed. Production source is the reviewed main3da8c2a with schema7, not the unfinished navigation branch.
Installed native `tasks-app` command at ~/.local/bin/tasks-app using a standalone Bun-target bundle from reviewed source under ~/.local/share/tasks-app-cli/3da8c2a. Help runs; no API token configured. This is separate from the still-unimplemented tasklib.backends.tasks adapter and does not make the adapter tests pass. No task-cli backend implementation or gate bypass was applied.

## Adapter implemented and live — 18 September 2026

`tasklib/backends/tasks.py` (`TasksBackend`) is now written and wired through `get_backend`,
`backend_coordinate`, `tasklib/config.py` (backend validation + `tasks:` section + rig.yaml
passthrough/attachment-mode fan-out), and `tasklib/projects.py`/`tasklib/cli.py` (the
`projects:` registry and `_current_project_overlay`/`_route_id_to_project` in-repo and
outside-repo routing, mirroring the Linear team-scoped path). Full protocol implemented:
create/get/update/list/search/comment/attach/transition/session_tickets/current_user, cursor
pagination, catalog-derived workflow-status/label mapping, single-read optimistic-concurrency
transitions (never re-reads before writing, never includes `description` in a transition PATCH),
a no-silent-fallback refusal when a team has no review-capable status, same-origin attachment
upload+fetch (tasks-app serves attachments from its own origin, not a separate asset domain), and
origin-bound reuse of the native `tasks-app` CLI's own persisted credential
(`~/.config/tickets-app/config.json`) alongside `$TASKS_TOKEN`/`$TICKETS_TOKEN`.

`tests/test_tasks_backend.py` (the hermetic red-first spec above) is green: 11/11. Additional
coverage added in `tests/test_backends.py` (transport errors, ambiguous-read typing, redirect
wiring, stale-version conflicts, id aliasing, body preservation, attachment upload, credential
origin binding), `tests/test_projects.py`, and `tests/test_cli_non_repo.py` (registry parsing +
in-repo/outside-repo routing). Full suite: 1297 passed, 360 pre-existing failures (unrelated —
this dev machine's task-cli checkout has its own git origin repointed at a Forgejo host, which
breaks the unrelated `github-issues` git-auto-remote resolution every `cli.py`/`mark_shipped`/
`gate` test depends on; verified identical failure count/names with these changes stashed).

Live-verified against production: `~/.config/tickets-app/config.json` already held a valid
token (from a prior `tasks-app auth login`), so no new token request was needed. Confirmed via
`/api/v1/catalog` that workspace `ultrabricks` has teams GL (Gliders), HYP (Hype Riders), TKT
(Engineering), UB (Ultrabricks). Created `/Users/ultra/work/glide-infra/task.yaml`
(`backend: tasks`, `tasks: {url: https://tasks-ultrabricks.vercel.app, team: GL}`, left
uncommitted) and filed a real ticket through it: GL-264 "Activate Hypergithub skin on
git.hyperide.ai Forgejo" — https://tasks-ultrabricks.vercel.app/issues/e5b0b670-aedc-401e-9e60-45f4d67ee954,
verified round-tripping cleanly via `task read GL-264`.

`~/.local/bin/task` is a live symlink into this checkout's `bin/task` (no separate install
step needed/run). All changes above are uncommitted in this repo — task-cli's own global
pre-commit hooks run secret-scanning only, no require-ticket-before-commit gate, so there was
no forcing function to commit against and none was invented.
