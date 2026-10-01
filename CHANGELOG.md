# Changelog

## 0.22.0 — 2026-10-01

- New `task change <id> --unlabel <label>` (repeatable) removes a label the ticket carries and
  keeps every other one (task-cli#244) — e.g. a duplicate `priority:<Pn>`. A label the ticket
  does not carry, a blank name, or one the same command also adds with `--label` is refused
  before anything is written. `task create`/`task change --label` refuse a blank name too.
- GitHub backend: `default_labels` are stamped only when a ticket is created; an update sends
  exactly the ticket's labels, so removing a default label sticks instead of being re-added.
- GitHub and Forgejo backends strip only the managed `status:<state>` labels from a ticket's
  labels, so any other `status:*` label (e.g. a `status:security-review` default) survives an
  update and a state transition.

## 0.21.2 — 2026-10-01

- Verified tickets whose body carries content outside the managed template (e.g. the
  2026-09-10 grooming provenance preamble) can be closed (task-cli#240): on Forgejo and
  tasks-app, `task check` / `task done --skip-<gate>` now edit such a body in place — only
  the changed checkbox lines and the `## Skipped gates` lines are rewritten, every other byte
  is kept (CRLF line endings included). Prose, links, screenshots and added/removed/reworded
  criteria on such a body are still refused before any write, as is a body whose `## Acceptance
  criteria` / `## Skipped gates` heading sits inside a fenced code block or HTML comment (or
  that ends inside an unclosed one).

## 0.21.1 — 2026-10-01

- The test suite no longer spawns the real `tg --tag report` notifier (task-cli#239): an autouse
  fixture records any `tg` notifier call instead, so a full run posts nothing to Telegram.

## 0.21.0 — 2026-09-30

- `task classify` is fast on repeat calls (task-cli#245): the `laya` provider keeps the model
  loaded in a warm, machine-wide server (`tasklib/classify_server.py`, spawned on first use,
  unix socket under `~/.local/state/task-cli/classify/`, exits after 15 idle minutes;
  `TASK_CLASSIFY_SERVER=0` opts out). Warm calls drop from 4-8 s to ~0.2 s.
- A cached laya checkpoint loads from the local HuggingFace cache instead of re-validating
  against the Hub on every load.
- The model load is quiet: no HF "Fetching N files" bar and no laya "invalid temperatures"
  RuntimeWarning on stderr unless `task classify --verbose` (which skips the warm server and
  loads in-process so the output is visible).
- New `task classify --batch`: JSONL on stdin (`{"text", "id"?}`) -> one verdict object per
  line on stdout, one provider chain for the whole batch.
- Guard rules around every provider: greetings/tiny texts are `justAsk` without running a
  model; plain defect reports the model called `justAsk` are `change` (`actionable` is kept).
