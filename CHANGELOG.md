# Changelog

## 0.24.0 — 2026-10-28

- New `task change <id> --acceptance-replace N=TEXT`, `--acceptance-remove N` (both repeatable) and
  `--acceptance-clear` correct a ticket's acceptance criteria instead of only appending
  (task-cli#293). N is the 1-based position `task read` shows, counted before the edit; criteria
  the edit does not name keep their text, checked box and proof. The minimum number of criteria
  must still hold afterwards. Rewriting or dropping an already-checked criterion warns and is
  refused without `--acceptance-force "<reason>"` (`--force` stays the transition
  override and never unlocks an edit); an edit and `--done` never share a command; the reason is recorded under
  `## Skipped gates`. Bad numbers, contradicting flags and a result below the minimum are refused
  with a what / why / fix error before anything is written. `task change --json` now lists the
  resulting `acceptance` (index, text, checked, proof).
- Linear backend: `task done` (and `gh ship`'s auto-close) no longer refuses a ticket linked
  with `task link` on the `links-url` gate (task-cli#278). Linear stores each link as
  `[GL-346](<url>)`; the parser now unwraps that identifier-labelled form back to the bare URL,
  while bare values, embedded credentials and other labelled links are still refused.
- Warm laya server: `TASK_CLASSIFY_SERVER_IDLE_SECONDS` sets the idle lifetime (task-cli#298).
  Unset keeps 900 s; `0` never exits on idle; an empty, negative, NaN, infinite or non-numeric
  value is refused (`task classify-server start` exits 1, the server process exits 2,
  `task classify` warns and classifies in-process) instead of silently using the default. The
  spawned server inherits the setting. The foreground module entrypoint now stops cleanly on
  SIGTERM/SIGINT (finishes the request in flight, removes its socket, releases its lock, exits 0)
  and runs from its own state directory, so a supervisor such as a `systemd --user` unit with
  `Restart=always` can keep Laya resident (see the README).

## 0.23.0 — 2026-10-17

- New `task classify-server start` starts the warm laya classify server when it is not already
  accepting connections and waits up to 60 s for it (task-cli#279), so a tool that needs laya
  (text-lint) can bring it up on demand. It prints only the socket path and exits 0, also when a
  server was already running (nothing is spawned then). It exits 1 with one `error:` line naming
  the reason when the server is disabled (`TASK_CLASSIFY_SERVER=0`), cannot be spawned, exits,
  or does not listen in time.

## 0.22.1 — 2026-10-16

- Linear backend: a team with more than 50 labels no longer fails `task new`, `task check`,
  `task link`, `task mark-shipped` or any other update with `duplicate label name`
  (task-cli#241, #265, #268). The label cache now reads every page of the team's labels
  (Linear's `team.labels` returns workspace labels too), and a label another session creates
  between that read and our create is looked up by name instead of failing the command.

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
