# Warm Laya server lifecycle — plan (task-cli#298)

Spec: `docs/superpowers/specs/2026-10-28-laya-lifecycle-design.md`. TDD: each step's test is
written and seen failing for the right reason before its code.

1. **Setting parser** — tests: unset -> 900.0; `"0"` -> 0.0; `"2.5"`; rejects `""`, `"-1"`,
   `"nan"`, `"inf"`, `"abc"`. Code: `IDLE_ENV`, `IdleSettingError`, `idle_seconds(env)`.
2. **Blocking idle 0** — tests: `serve(idle_s=0)` survives a 0.4 s quiet interval with near-zero
   CPU and answers a second request; `idle_s=0.3` still exits and removes the socket; `serve`
   rejects negative/NaN/inf `idle_s`. Code: `select`-based accept loop.
3. **Signals** — tests (subprocess running the real foreground path with a fake predictor):
   SIGTERM removes the socket, frees the flock, exits 0; SIGTERM during a slow request still
   delivers that reply. Code: `stop_fd` in `serve`, `_run_foreground` installing the handlers
   and passing the parsed setting; `main` delegates to it.
4. **Wiring** — tests: `main` passes 900 when unset and the parsed value otherwise; invalid
   setting makes the server process exit 2 before taking the lock; `start()` refuses an invalid
   setting without spawning (`classify-server start` exits 1 with one `error:` line); the
   spawned child's environment carries the setting; `predict()` falls back with a diagnostic;
   four concurrent `start()` calls leave exactly one live server.
5. **Docs** — README (setting, systemd user unit, replacing a running server), CHANGELOG.
6. **Verify** — focused tests locally, full suite via `server-run`, one `review diff --task`
   pass, commit, push, `gh ship` after CI is green.
7. **Deliver** — install the reviewed main on Ubuntu and clvm through the supported installer
   when its dependency set is safe; per host: confirm ownership of the running same-version
   server, ask it to `shutdown`, start one `systemd --user` unit with the setting `0`, kill
   test for restart, measure cold versus warm latency with the real model.
