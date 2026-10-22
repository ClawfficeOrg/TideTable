# TideTable Memory

Project state and decisions for agents. Read before starting work. Rules for
working on this repo live in `AGENTS.md`.

## What This Is

Structured JSON task management for autonomous agents. Alternative to markdown
todo files. Stable IDs, dependency DAG, parallel execution groups, semver
version mapping. Built for [ralph](https://github.com/ClawfficeOrg/Zoid), works
with any autonomous agent. See `docs/philosophy.md` for rationale.

## Layout

```
tidetable/cli.py                      CLI logic (the only implementation)
tidetable/agents.py                   Tier -> provider/model config layering
tidetable/jsonio.py                   Atomic writes, the file lock, the event log
tidetable/sidebar.py                  Budgeted sidebar payload for harness panels
tidetable/integrations.py             `integrations list|install|uninstall` adapters
tidetable/tkrun.py                     TKRun: unattended runner (`tkrun`, `overnight` alias)
tidetable/runs.py                      Live run state (pid/STOP/KILL/log) read by the sidebar
tidetable/serve.py                    Single-writer HTTP API (`serve`)
tidetable/projector.py                Projection + presence for the hub
tidetable/sessions.py                 Session/token handling for the hub
tidetable/tokens.py                   Machine token hashing/validation
tidetable/__main__.py                 python -m tidetable
tidetable/schema/todo-v1.schema.json  JSON Schema (draft 2020-12), package data
scripts/tidetable.py                  Thin shim -> tidetable.cli.main
ralph/ralph-json.sh                      Thin wrapper -> tidetable CLI
skills/tidetable/SKILL.md             Hermes skill
integrations/                            opencode, pi, paseo, openchamber adapters
web/                                     SvelteKit read-only dashboard
docker-compose.yml                       VPS hub, built from this repo (git pull && compose up)
.env.example                             TK_API_TOKEN starter for the compose file
deploy/                                  compose.yml, compose.vps.yml (superseded), migrations, registry.json
tests/                                   stdlib unittest suite (test_cli, sidebar,
                                         overnight (compat shim), integrations, projector, tokens)
.github/workflows/ci.yml                 Tests + example validation, Linux/Windows
docs/                                    philosophy, integration guide, memory,
                                         module-plan, notes, tkrun,
                                         singularity.lanes
examples/                                simple-project, zoidmatter-v7
pyproject.toml                           setuptools packaging, console script
```

## Current State (2026-10-12, v0.8.0 + unreleased)

- v0.1 scaffold (Aug 27 2026) → hardened (Sep 2) → v0.2.0 rework (Sep 8),
  which fixed everything raised in the `docs/notes.md` review → v0.3.0 (Sep 8),
  which added the agent provider/model config → v0.4.0 (Sep 8), provider presets
  → v0.5.0 (Sep 8), parallel execution → v0.6.0 (Sep 21), the VPS hub →
  v0.7.0 (Sep 23), harness sidebars → v0.8.0 (Oct), `add-phase` → unreleased:
  TKRun and the integrations installer.
- Schema now ships as package data, so `pip install .` works, not just
  `pip install -e .`. Verified from site-packages in a clean venv.
- Scheduling is complete: `ready` lists every runnable task, `start` claims one,
  `reset` recovers an orphan, `next` resumes `in_progress` before handing out
  new work. Phase-level prerequisites are enforced, not just task-level.
- `validate` does semantic checks on top of the schema: duplicate IDs, dangling
  task/phase prerequisites, cycles, task ID vs. phase ID mismatch, `moved`
  without `moved_to`, and a warning for prerequisites on `cancelled`/`moved`
  tasks.
- `add-phase` (v0.8.0) inserts an empty phase, anchored with `--after`/`--before`
  or appended. Its `--prereq` phase IDs gate the new phase's tasks through the
  same `blockers_for` used everywhere else, so adding a phase changes no
  scheduling rule.
- All read commands support `--json`, and task output carries the resolved
  provider/model (v0.3.0).
- Writes are lock-guarded (`<file>.lock`) and atomic (temp + rename). The lock
  records pid + host + time, and a lock whose holder is confirmed gone (or which
  has aged past `stale_after`, default 300s) is broken automatically.
- v0.5.0 made parallel execution real rather than advertised: claims with
  leases, `--disjoint` fan-out from `touches`, an append-only event log,
  complexity-driven tier selection, and an explicit `release` command.
- v0.6.0 added the hub: `serve` is one single-writer HTTP API over the same DAG
  logic (GET reads, locked POST writes, heartbeats, an SSE stream), with
  presence, a projection, per-project machine tokens, and a read-only SvelteKit
  dashboard under `web/`.
- v0.7.0 added `sidebar` (one budgeted payload) and four thin adapters under
  `integrations/` (opencode, pi, paseo, openchamber), plus
  `integrations list|install|uninstall` to wire them in.
- v0.8.0 added `add-phase <file> --phase <id> --title <t>` (and
  `POST /api/<slug>/add-phase`): inserts an empty phase with `--goal`,
  repeatable `--prereq` phase IDs, and an `--after`/`--before` anchor (default
  append). Phase prerequisites gate the new phase through the existing
  `blockers_for` path — no scheduling change, and nothing half-writes on
  rejection.
- 0.11.0 retired the `overnight` name: `tidetable tkrun` and `tidetable/tkrun.py`
  are canonical, `overnight` stays a subparser alias plus a same-object module
  shim, and `.tidetable/overnight.json` / `.overnight/` are read as fallbacks.
  A gray-zone command is now either adjudicated (decision model) or prompted to
  the person; the static allow/deny lists are unchanged. The sidebar gained a
  live `run` section. `tkrun kill` hard-stops with a sentinel plus a process-tree
  signal; leased `lanes` route a reservation-window provider to a fallback. See
  `docs/tkrun.md` and `docs/singularity.lanes.md`.
- Tests are stdlib `unittest`, run on Linux and Windows in CI against a
  non-editable install.

### Local test note (macOS)

The suite shells out to `sys.executable -m tidetable` for the TKRun tests, so
the package must be importable by that interpreter: run `pip install -e .` first,
or export `PYTHONPATH`. On macOS the `/var` → `/private/var` symlink also makes
`Path.resolve().relative_to()` raise in a few temp-dir tests; that is an
environment artifact, not a code failure.

## Parallel Execution (v0.5.0)

- **Claims.** `start` writes `claimed_by` / `claimed_at` / `lease_expires_at`;
  any non-`in_progress` status clears them. `start` refuses a live claim held by
  someone else, `done` needs `--force` for one, and `find_next` returns an
  `in_progress` task only when `claimable_by` accepts it. Owner comes from
  `--owner`, else `TIDETABLE_OWNER`, else `host:pid`; lease from `--lease`,
  else `TIDETABLE_LEASE_MINUTES`, else 60 minutes.
- **Disjoint fan-out.** `ready --disjoint` / `next --disjoint` filter by
  `touches` overlap (`paths_conflict`: equal, or one a directory containing the
  other), counting in-progress tasks as holding their paths. Greedy in ID order
  so the result is stable. Plain `ready` reports the overlaps as a warning.
- **Event log.** `<file>.events.jsonl`, one JSON line per transition, written
  inside the lock, best-effort. `history` replays it. `TIDETABLE_EVENTS=0`
  disables.
- **Complexity → tier.** A task with `complexity` and no `agent` gets a tier:
  Low→basic, Medium→mid, High→pro, Very High→flagship, overridable by a tier's
  `complexity_range` in any config layer. `resolve_task` reports the tier it
  used and whether it was derived.
- **Releases.** `done --json` reports `release_ready`; `release [phase]` prints
  what would be tagged and `release --tag` creates the annotated tag from the
  release notes plus the collected changelog.

## Agent Model Config (v0.3.0, presets in v0.4.0)

- A task names a tier (`task.agent`); `tidetable/agents.py` resolves that
  tier to a provider and model, and `next` / `ready` / `parallel --json` carry
  the resolved values so a supervisor dispatches in one lookup.
- Layers, last wins: built-in `anthropic` preset -> user config -> repo config
  -> the todo file's `agent_config` -> a task's own `provider`/`model`.
- Each config layer contributes twice: the preset it names via `provider`, then
  its own `tiers` entries. So `agents use` moves a whole scope and `agents set`
  in that scope still wins.
- `PROVIDER_PRESETS` ships `anthropic` and `opencode-go` (glm-5.3-flash for
  basic/mid, deepseek-v4-pro for pro, qwen3.8-max for flagship). Adding a
  provider is a dict entry plus a README row.
- `agents show` reports which layer supplied each setting, and which preset is
  active. `agents providers` lists the presets.
- `TIDETABLE_CONFIG_HOME` relocates the user config; the tests rely on it.

## TKRun session notes (2026-10-14)

What changed in one long dogfooding session driving Swaygent with
`deeprelay/deepseek-v4.1-flash` through opencode, and what it taught us.

- **Per-task and per-phase review** are independent. `review.phase`
  (`{enabled, model, on_fail}`) runs once, when the last task of a phase
  closes, against the phase goal and that phase's commits. `on_fail` is
  `continue` (default) or `stop`. Flags: `--task-review-model`,
  `--no-task-review`, `--phase-review-model` (also enables it),
  `--no-phase-review`. The default task reviewer is `anthropic/claude-opus-5-5`.
- **`--jobs N`** runs tasks in parallel: one worktree and branch per task
  (`tkrun/<date>-<id>`), merged back `--no-ff` one at a time. Only tasks that
  declare disjoint `touches` run together; a task with no `touches` runs alone.
  Todo state is only written in the run worktree, so jobs never conflict on it.
  Share a cargo target dir with `parallel.job_env`. Not combinable with
  `siblings` yet. Tested against stub agents; first real run is still to do.
- **`--follow`** echoes agent output while it runs (default on a tty).
- **Secrets stay out of the agent.** The runner's `TIDETABLE_TOKEN` and friends
  are stripped from the agent environment, `agent_env_drop` hides more, the
  prompt forbids printing env/.env, and env-dumping commands are denied.
- **A custom `agent_commands` entry that is really `opencode run` bypasses the
  runner's permissions** unless it sets `opencode_permissions: true`. It also
  needs `--standalone`, or the agent session lives in opencode's background
  service and keeps editing the worktree after the runner is killed.
- **opencode 2.0.26 permissions:** an allowlist (`"*": "deny"` plus allow
  patterns) refuses the shell outright, even `git status`; and one rejected
  command removes the shell tool for the rest of the session. So the generated
  config is a denylist of only what must never happen (`git push`/`tag`,
  tidetable state, env/secret dumps), also behind the `rtk ` prefix because the
  rtk plugin rewrites `git ...` before the permission check. Local git is
  allowed; the runner folds an agent's own commits back into the working tree
  with a soft reset so the gate and diff still see them.
- **Stale `STOP`/`KILL` sentinels are ignored at start** (they ended the next run
  before its first task). An empty leftover worktree folder is reused.
- **`push: true`** pushes the run branch (never the base) to origin at the end.
- **Windows/Git-Bash traps hit while doing this:** `taskkill //PID` is Git Bash
  syntax, PowerShell wants `/PID`; `cp` is aliased interactive in Git Bash (use
  `cat src > dst` or Python); heredocs with apostrophes or backticks break the
  Bash tool, so write scripts to a file; Git Bash `/tmp` is not visible to
  Python on Windows. (Running the suite from a shell pointed at a hub used to
  need `env -u TIDETABLE_SERVER`; `tests/hermetic.py` now scrubs it.)
- **First real `--jobs 2` run (Swaygent, 2026-10-14) and what broke:**
  1.3.1 finished and merged cleanly after 51 minutes while 1.1.6 ran beside it.
  Then (a) the runner's base-branch SAFETY check aborted 1.3.2 because someone
  committed to `main` mid-run, and the crashed job's worktree was deleted with
  24 minutes of work in it (now kept, `KEEP <id>`); (b) a shared
  `CARGO_TARGET_DIR` across worktrees gave 1.1.6's agent compile errors about
  `ModelInfo` fields that exist only in another worktree (private target dirs
  compiled the same source fine, 35 tests passed); (c) a textual-clean merge can
  still be a semantic conflict: 1.3.1 added fields to `ModelInfo` while 1.1.6
  constructed it, so run the full gate on the merged tree. Rule of thumb: do
  not touch the base branch while a run is live (the SAFETY check is strict, and
  a config tweak is enough to trip it); commit config changes before launching.
- **Gate mistakes cost whole runs.** A gate that includes a crate with
  pre-existing clippy errors fails every task. Scope the gate to what an agent
  can fix, and run it by hand once before an unattended run.

## Decisions

- **One implementation of the DAG.** `ralph/ralph-json.sh` was a parallel
  jq implementation and had already drifted (different ordering, different
  `done` enforcement, a tautological `select`, a `// "none"` that never fired).
  It is now a wrapper that execs the CLI. jq is no longer a dependency.
- **Only `done` satisfies a task prerequisite**, but `cancelled` and `moved`
  count toward *phase* completion. Cancelled work is not coming back, so it
  should not stall a phase; but silently treating it as a satisfied dependency
  would hide a real planning error, so `validate` warns instead.
- **`done` refuses unmet prerequisites without `--force`.** The old jq path
  warned and wrote anyway; the Python path did not check at all. Refusing is the
  safer default now that both go through one code path.
- **Changelog collection is real.** `done --changelog TEXT` appends to the
  phase's `release.changelog_entries`, or to the last phase that has a `release`
  object. The schema used to claim this happened and nothing did it.
- **Changelog is nullable.** Task `changelog` allows `null` = "no entry".
  The zoidmatter-v7 example (task 7.1.2, the merge task) relies on this.
- **Files read/written as UTF-8** (`encoding="utf-8"`, `ensure_ascii=False`).
  Timestamps are written as `...Z` to match the examples.
- **CLI glyphs are intentional** (`✓ ✗ ► · → ═ ─ •`). The UTF-8 reconfigure at
  import makes them safe on Windows cp1252 consoles. Keep both.
- **`convert` is one-way** (JSON → markdown). Markdown → JSON is a manual
  migration; see `docs/philosophy.md`.
- **`parallel_group` schedules nothing.** It was documented as making groups run
  sequentially and no code ever did that. Ordering is `prerequisites`;
  concurrency safety is `touches`. Two gating mechanisms would mean two ways for
  a task to be mysteriously blocked.
- **Tagging is explicit.** `done` reports `release_ready` rather than creating a
  tag, because the phase's last task is an ordinary task and a git side effect
  fired from it lands unpredictably.
- **The event log is a sibling file, not a field.** Appending is one syscall,
  the todo file stays diffable, and a crash mid-append costs one line instead of
  the roadmap.
- **Pid liveness is never checked with `os.kill(pid, 0)`.** On Windows that
  terminates the process. `jsonio._pid_alive` uses the Win32 API and returns
  `None` when unsure; unknown liveness falls back to lock age.
- **Model config is per machine by default, not per roadmap.** Which model runs
  a tier is a property of whoever runs the agents, so the user layer is its
  normal home. The repo and todo layers exist for projects that genuinely need
  to pin something, not as the default place to put it.
- **TideTable never calls a provider.** It resolves strings and hands them
  over: no SDK dependency, no API keys, no network. Unknown settings
  (`--option effort=xhigh`) pass through untouched so a supervisor can use them
  without TideTable knowing what they mean.
- **The unattended runner is TKRun.** `tkrun` is the name; a new config is
  `.tidetable/tkrun.json` and a new run dir `.tkrun/`, with the pre-0.11
  `overnight` names (subparser alias, same-object module shim, legacy
  `.tidetable/overnight.json` config and `.overnight/` run dir) kept as
  fallbacks. `stop` is
  cooperative (a `STOP` sentinel checked between tasks); `kill` is a hard stop
  (writes `KILL` *and* tree-kills the runner PID so a mid-task agent dies too).
  Claims are left to expire rather than edited, because the supervisor, not the
  agent, owns tidetable state. A gray-zone command is either adjudicated (with
  `adjudicator.enabled`) or prompted to the person; it is never silently run.
- **Leased lanes never book.** `lanes` only reads `GET /reservations`; keys come
  from the environment (`infisical run`), and the generated `opencode.json` holds
  `{env:...}` references, never values. A task already running when the window
  closes is not interrupted — only the *next* task reroutes to `fallback`.
- **A Rust core may replace everything under TKRun, but `tkrun.py` stays
  Python.** The seam is already a process boundary: `Runner.tk()` shells out to
  `python -m tidetable` at eleven call sites, six distinct commands (`ready`,
  `list`, `start`, `done`, `look-defer`, `reset`), so a Rust core only changes
  that one line. Python is kept for TKRun because it is intentionally unstable
  and the edit-run cycle is worth more there than the type safety.
  `docs/rust-port.md` is the parity contract: the JSON shapes, exit codes and
  refusal wording a port must reproduce. Two details are load-bearing and easy
  to lose: `ready` and `next` exit `1` when nothing is runnable (that means
  "no work", not "broken"), and `task_summary` overwrites a task's own `agent`
  with the resolved tier while adding `agent_derived`.

## Known Gaps / Next

- The lock is still cooperative. Stale locks now break themselves, but two
  processes racing on a filesystem without atomic `O_EXCL` (some network mounts)
  are still not protected.
- `FileLock.__exit__` unlinks the lock file unguarded, so two processes racing on
  one todo file can raise `PermissionError: [WinError 32]` when the other still
  has it open. Observed once as an uncaught traceback out of `cmd_status`, not on
  retry. Catch it like the existing `FileNotFoundError` rather than copying the
  pattern into a rewrite.
- A lease is not renewed while a task runs, so a task genuinely longer than the
  lease can be stolen mid-flight. Re-running `start` as the same owner refreshes
  it; a supervisor with long tasks should either do that periodically or raise
  `TIDETABLE_LEASE_MINUTES`.
- `touches` conflict detection is textual. It does not know that two tasks
  editing different functions in one file might be fine, and it cannot see a
  file a task forgot to declare.
- The event log has no rotation or compaction.
- `test_jobs_serialise_tasks_with_overlapping_or_missing_touches` is flaky: it
  failed roughly one run in five of the full suite under load, while passing in
  isolation and in ten full-suite runs on `main`. Timing-sensitive (the stub
  agent sleeps 2.5s and the tasks must serialise on overlapping `touches`); the
  parallel scheduler or the test's timing assumption needs the real fix.
  - Investigated 2026-10-14 and **not reproduced**: 8 solo runs, 6 concurrent,
    12 x the four `--jobs` tests, 5 full suites - all clean. The scheduler logic
    (`run_parallel` + `next_compatible`, `tidetable/tkrun.py`) was traced by hand
    and could not be made to place two overlapping tasks in `inflight` together:
    an empty-`touches` task returns `None` while anything runs, and overlapping
    `touches` are skipped via `touches_overlap`. Jobs share the parent worktree's
    `todo.json` (`make_job` does a shallow `copy.copy`, so `job.todo` still points
    at the parent path), so `start`/`done` claims are visible to the parent's
    `ready` call.
  - The only failure seen under CPU saturation was a **different** test,
    `test_missing_or_wrong_token_is_a_clean_failure` (got `unauthorized` from a
    real hub). That is environment bleed (`TIDETABLE_SERVER` / `TIDETABLE_TOKEN`
    leaking into the test run) - see the Windows/Git-Bash note above - **not**
    this flake. The very first "reproduction" attempt was the same trap: the hub
    hijacked the run (`hub ... project '...todo.json' not reachable`).
  - Honest read: no evidence of a scheduler concurrency bug. The flake is either
    (a) the test's timing assumption - it infers scheduler behaviour from
    wall-clock timestamps written by sleeping subprocesses, which is inherently
    racy under load - or (b) an environmental hiccup (`s`/`e` line outside the
    measured window, or a stray extra stub invocation writing to the track file).
    Neither was confirmed.
  - Fix direction (not applied): assert the **scheduler's decisions**, not
    wall-clock. Have the stub take an OS-level exclusive lock (or an `O_EXCL`
    "running" marker file) per task so a second concurrent start fails at once,
    or expose dispatch order from `run_parallel` (e.g. `START` log lines) and
    assert the sequence, keeping the timestamp check as a smoke test only.
- The leased-lane reader has no booking and no renewal: if a provider changes its
  reservations API shape, `lane_window` silently sees no window and every task
  falls back. There is no test against a live API, only a stub.
- `cli.py` still fuses scheduling logic, rendering, and argparse. Splitting it
  into an importable core is the next structural change — see
  `docs/module-plan.md`.
- Not published to PyPI; consumers clone. Also in `docs/module-plan.md`.
- Markdown → JSON conversion is not implemented, by decision rather than
  omission.
- No GitHub Issues sync (see `docs/philosophy.md` "Future Directions").
- Skill directories were not readable by default. An `opencode` agent that opens
  a skill reference outside the worktree (a project-level `<repo>/.opencode/skills`
  or a global `~/.agents/skills`, `~/.config/opencode/skills`) was denied with
  `Permission denied: external_directory` unless the repo listed that path in
  `external_read`, then fell back to slower shell reads. Seen 2026-10-15 on
  Wheelhouse 1.1.1. Fixed in `1590f35`: `SKILL_READ_DIRS` plus the checkout's own
  `.opencode/skills` are always allowed read-only. Repos that list them in
  `external_read` (Wheelhouse's `.tidetable/tkrun.json`) can drop those entries.
- `docs/notes.md` is the external v0.1 review that drove the v0.2 rework. It is
  history now; its findings are closed.
