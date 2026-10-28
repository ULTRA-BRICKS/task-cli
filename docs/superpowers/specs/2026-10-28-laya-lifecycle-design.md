# Warm Laya server lifecycle — design (task-cli#298)

Ticket: [task-cli#298](https://git.hyperide.ai/ultrabricks/task-cli/issues/298). Owner decision
(2026-10-28): the warm classify server must be able to stay resident; a fixed 15-minute idle
exit with no restart is not acceptable for hosts that need Laya available without a cold load
or a silent downgrade.

## Problem

`tasklib/classify_server.py` exits after `IDLE_SECONDS = 900` without a request and nothing
restarts it. `serve(idle_s=...)` already takes the value but `main()` never passes it. Also:

- `serve` uses `srv.settimeout(idle_s)`; `idle_s=0` would make the socket non-blocking and the
  accept loop would busy-spin on `BlockingIOError`.
- SIGTERM/SIGINT kill the process outright: no socket removal, an in-flight request is cut off.
- The foreground module entrypoint is usable as a service, but nothing documents or tests that.

## Decisions

1. **Setting.** `TASK_CLASSIFY_SERVER_IDLE_SECONDS`: a finite number >= 0. Unset keeps the
   default 900. `0` means never exit on idle. Empty, negative, NaN, infinite or non-numeric is
   an error — never a silent fallback to 900. One parser, `idle_seconds(env)`, raising
   `IdleSettingError`; `serve` re-checks its own `idle_s` with the same predicate.
2. **Who reads it.** The server process (`main`) parses it and passes it explicitly to `serve`.
   `task classify-server start` parses it first (clear `error:` line, exit 1, nothing spawned);
   the spawned server inherits the environment, so the setting reaches it unchanged.
   `predict()` (the `task classify` accelerator) prints one stderr diagnostic and falls back to
   in-process classification on an invalid setting instead of raising.
   A server that is already running is never restarted or reconfigured by `start`.
3. **`idle_s == 0`** is a blocking wait (`select` with no timeout), not a zero timeout.
4. **Signals.** The foreground entrypoint turns SIGTERM and SIGINT into a wake-up byte on a
   pipe the accept loop selects on. The in-flight request finishes, then the loop returns 0 and
   the existing `finally` blocks remove the socket and release the flock. Model load is not
   interruptible; a signal received during load takes effect right after it.
5. **Service mode.** No new transport. The module foreground entrypoint
   (`python -P -m tasklib.classify_server`) is the service: a systemd *user* unit sets
   `TASK_CLASSIFY_SERVER_IDLE_SECONDS=0` and `Restart=always`. The flock stays the singleton
   guard: a second instance (e.g. a client-spawned server of the same version) exits 0
   without loading. Replacing a same-version server that was started with the 900 s default is
   done through its own `shutdown` request, never by deleting its lock or socket.
   The foreground `main` also makes the socket's directory its cwd (as the client spawn does),
   so a supervisor's cwd cannot shadow the model id; only the default state directory is
   forced to 0700, an operator-named socket directory is left untouched.

## Invariants kept

Exclusive flock before model load; socket 0600 in a 0700 user-owned directory; fixed model id
(nothing in a request chooses what loads); `TASK_CLASSIFY_SERVER=0` still disables the server.

## Out of scope

tg-cli callers and config; the multilingual model route and hung-load watchdog
([#294](https://git.hyperide.ai/ultrabricks/task-cli/issues/294),
[#295](https://git.hyperide.ai/ultrabricks/task-cli/issues/295)); cross-version socket reuse
([#282](https://git.hyperide.ai/ultrabricks/task-cli/issues/282)).
