# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Every TUI action works in hub mode.** `s` start, `d` done, `l` defer, `c` clear and `x` reject post their hub routes with one owner, and the toast matches the CLI.
- **Hub slug resolution and project picker in `tidetable tui --server`.** The slug comes from the argument, `$TIDETABLE_PROJECT`, a `slug` key in the nearest `.tidetable/config.json`, or a picker over `GET /api/projects`. It never reads git remotes.
- **Remote claim detail in the TUI.** In-progress rows show the owner, claim age and lease time left.
- **`GET /api/projects` is token-scoped.** A machine token saw only the slugs it may open; the shared `TK_API_TOKEN` saw all.
- **`tidetable edit <file> <id>`** changed a task's plan fields without hand-editing JSON: `--title`, `--goal`, `--tests`, `--complexity`, `--agent`, `--parallel-group`, repeatable `--prereq`/`--touches`/`--success` that replace the whole list, and the matching `--clear-*` flags. Status and claims were never touched; dangling, self or cyclic prerequisites were refused and nothing was written. Every edit appended one `edit` event. Also `POST /api/<slug>/edit` and `tidetable --server`.
- Hub routes `POST /api/<slug>/look-defer` and `look-clear`, and `tidetable --server` support for both (`look-clear --reject` sends the task back to pending).

### Fixed

- The TUI stops retrying on a hub 401 or 403 and says what to change (`token rejected · set $TIDETABLE_TOKEN`, `no access to this project`) instead of backing off forever.
- **Hub writes carry a base and a stale one is refused.** `POST /api/<slug>/start`, `/done`, `/reset` and `/status` took a `base` (the hub's event count when the caller decided) and answered `409 {"conflict": true, "task", "base", "current"}` when that task changed after it, without applying the write. A duplicate `done` still returned `already_done`.
- **`POST /api/<slug>/edit`, `/look-defer` and `/look-clear` refused a stale base too.** They answered the same `409` conflict, left the task unchanged and appended the refusal to `<todo>.conflicts.jsonl`; a request without a base was still applied, as for start, done and status. `tidetable --server edit`, `look-defer` and `look-clear` took `--base N` and otherwise fetched the base just before the write. A duplicate `look-clear` kept its plain not-in-queue 409.
- **`POST /api/<slug>/reset` no longer ran blind.** It required a `base` (428 without one) and refused another owner's live claim with a 409 unless `force` was set.
- `tidetable --server` sent `--base N` when given, else fetched the hub's current base just before the write; `reset --force` reached the hub.
- The hub drains a POST body (up to 64 KiB) before answering 401/403, so a rejected write no longer surfaces as a connection reset.

### Added

- **Conflict list.** The hub appended every refused write to `<todo>.conflicts.jsonl`, read by `GET /api/<slug>/conflicts` and the read-only `tidetable conflicts <slug>`; nothing replayed them.
- **`GET /api/<slug>/base`** returned the hub's current event count for a slug.

## [0.12.0] — 2026-10-17

### Added

- TUI reuses the ratatui ecosystem instead of hand-rolled pieces: `tachyonfx` for the row-change fade, `tui-input` for the dialog text field, ratatui `Tabs` for the tab bar (same stack as the other clawffice apps).
- **`tidetable tui` and the `tidetable-tui` binary (`tui/`).** A ratatui front end:
  Now / Upcoming / Done / Review views, standard, side and bottom layouts that follow
  the pane shape, five vendored palettes (`--palette`, `$TIDETABLE_PALETTE`), a review
  checklist with local ticks, confirm-dialog actions (`s` start, `d` done, `l` defer, `c` clear,
  `x` reject; `--read-only` removes them) and a hub mode (`--server URL SLUG`,
  `$TIDETABLE_TOKEN`) that refreshes on the event stream, else polls, and keeps stale
  data dimmed and labelled when the hub is offline. `--tmux` splits a 40-column (side)
  or 10-row (bottom) pane. It never writes a todo file. Not part of the wheel; build
  with `cargo build --release --manifest-path tui/Cargo.toml`.
- **`GET /api/<slug>/look-queue`** on the hub (and `look-queue` in `tidetable --server`),
  the same JSON as `look-queue --json`.

- **`look-queue -v`.** Prints each queued task's success criteria as a `[ ]`
  checklist and drops a note that only points at them, so a human can read every
  pending check in one list. `--json` entries gain `goal` and `success`.
- **Default todo file.** `todo_file` is optional on every command and on
  `tkrun run|init|stop|kill|adjudicate`. Omitted, TideTable uses the `todo*.json`
  in the first of `.`, `docs/`, `.tidetable/` that has one, prints
  `Using <path>` to stderr, and errors if that directory holds more than one.
  With nothing found it prompts on a terminal and errors otherwise, so agents
  never block on input. Not applied with `--server`, where the argument is a slug.
- **Claude Code agents are adjudicated too.** With `adjudicator.enabled`, a
  `claude -p` dev agent now gets a `PreToolUse` hook (`tidetable/claude_hook.py`,
  passed with `--settings`) that sends every Bash command to the runner's
  loopback service. The hook answers `allow` or `deny` itself, so claude still
  never prompts, and it fails closed. Claude's requests are judged against
  claude's own (stricter) deny list. Previously the claude gray zone fell back to
  the static lists, so the decision model was never consulted for those tiers.
- **Cleanup crew: a failed gate gets repaired instead of parked.** When a task's
  gate fails, a repair agent (`repair.model`, default the task's own agent) gets
  the gate output and the working tree and fixes it in place, up to
  `repair.attempts` times (default 2), then the gate re-runs. Only a failure that
  survives the repair counts toward `max_consecutive_failures`. The prompt forbids
  deleting or weakening tests to get a pass. `repair.enabled: false` restores the
  old behaviour. The crew also takes a task whose agent reported FAILED, timed
  out, printed no result line, or said DONE with nothing changed: it inspects the
  partial work and finishes the task (or leaves it PARTIAL for a human).
- **`merge_resolve`: parallel jobs no longer conflict on generated files.** A job
  merge whose only conflicted paths are listed here (default `Cargo.lock` →
  `cargo update --workspace`) takes the run branch's copy, runs the command and
  commits the merge instead of failing the task.

### Fixed

- **An empty commit is not a crash.** `git status` can list a path as modified
  when nothing real changed (autocrlf worktrees), and `git commit -- <path>` then
  failed with "nothing to commit", marking a finished task `ERROR`. A path with
  nothing staged is now skipped.
- **`write_json` retries a Windows sharing violation.** `os.replace` onto a file
  another process held open raised `PermissionError` (WinError 5) and failed the
  claim; it is now retried a few times with a short back-off.

### Changed

- **The Paseo plugin is rebuilt for Paseo ≥ 0.8 (declares `>=0.11.0`).** The
  daemon rejected the old one at install: no `paseo-plugin.json`, a CommonJS
  root `client.js`/`server.js`, and a DOM panel that cannot run on Paseo's
  React Native clients. It now has `index.client.tsx` (screen, sidebar header
  item with the in-progress badge, workspace panel, Command Center item, and a
  settings screen) and `index.server.ts` (one typed `tidetable.sidebar` RPC over
  `tidetable sidebar --json`), with code under `client/`, `server/`, `shared/`.
  Settings moved from `package.json` to a host settings document edited under
  Settings → Plugins; the `TIDETABLE_*` env fallbacks are unchanged.
- **The Paseo panel lives in the right sidebar and follows the project.** It
  is an Explorer panel that finds the TideTable todo files in the open
  workspace's directory (JSON with the schema's `schema_version`, `project`
  and `phases`) and reads them locally, with a chip per file when there are
  several. The left-sidebar row is gone; the settings-driven view (a fixed todo
  or a hub project) stays as a screen in the Command Center.
- **`integrations install paseo` runs `paseo plugin install <checkout>/integrations/paseo`.**
  Paseo ≥ 0.8 does not scan a plugins directory, so the old symlink loaded
  nothing. `uninstall` runs `paseo plugin remove paseo-tidetable`, and `list`
  reads `paseo plugin ls --json`. `--paseo-dir` keeps the old link/copy
  behaviour for anyone who still needs it.

### Fixed

- **The adjudicator retries a transient decision-server fault.** A 5xx, 429 or
  refused/timed-out connection is retried (3 tries, 0.75 s steps) inside the
  same `timeout_seconds` budget, so the agent hooks' ~20 s limit still holds. A
  persistent fault, a 4xx or a bad reply still denies. Before this, one `507
  Insufficient Storage` denied a harmless command and sent the agent off to work
  around it.
- **The opencode adjudicator bridge now loads in any repo.** The runner pointed
  `opencode.json` at `<worktree>/integrations/opencode/tk-adjudicator.ts`, which
  only exists in TideTable's own checkout. A copy now ships in the package
  (`tidetable/bridges/`) and is used when the served repo has none.
- **The test suite ignores the developer's environment.** With
  `TIDETABLE_SERVER` exported, about a hundred tests sent temp todo paths to the
  real hub as slugs, and `tkrun kill` tests resolved their repo to the cwd and
  wrote a live KILL sentinel into the checkout's own `.overnight/`. Every test
  module now imports `tests/hermetic.py`, which strips the project's env
  namespaces and gives the run a throwaway `TIDETABLE_CONFIG_HOME`.
- **`python -m tidetable` exits 1 on a handled error.** `main()` returned 1 for
  "not found", invalid JSON, lock timeouts and remote errors without calling
  `sys.exit`, so the module form, `scripts/tidetable.py` and
  `ralph/ralph-json.sh` exited 0 beside `Error: ...`. The console script was
  unaffected.
- **The Paseo sidebar no longer sends a local todo path to a hub.** A
  `TIDETABLE_SERVER` inherited by the Paseo daemon turned a file path into an
  "unknown slug"; an env-only hub is now ignored when the todo is an existing
  file, and local reads pass `--local`. Its CLI fallback also stopped retrying
  the CLI's own `... not found` errors through `python -m`.

## [0.11.0]

### Changed

- **`overnight` is retired as a name; the runner is TKRun.** `tidetable tkrun`
  is the command and `tidetable/tkrun.py` is the module. The old name still
  works as a compatibility shim: `tidetable overnight ...` is a subparser alias,
  `import tidetable.overnight` is the same module object as `tidetable.tkrun`,
  `TKRUN_*` env vars fall back to `OVERNIGHT_*`, and a repo keeps its legacy
  `.tidetable/overnight.json` config and `.overnight/` run directory until it
  writes the new names. New on-disk names: `.tidetable/tkrun.json`, `.tkrun/`,
  branch prefix `tkrun/<date>`, worktree `<repo>-tkrun-<date>`, owner
  `tkrun-<date>`, and the agent's `TKRUN_RESULT:`/`TKRUN_SUMMARY:` markers (the
  parser still accepts the old ones).
- **A gray-zone command is adjudicated or prompted, never silently run.** With
  the adjudicator enabled the decision model settles a command on neither list,
  as before. With it disabled there is no unattended decision-maker, so opencode
  asks the person (`bash: "*": "ask"`) and `claude -p` drops `--permission-prompts
  none` and uses the asking permission mode. The static allow/deny lists are
  unchanged.

### Added

- **Live run state in the sidebar.** `tidetable sidebar` now reports a `run`
  section read from the runner's control directory (`.tkrun/`, or a legacy
  `.overnight/`): status (`running`/`finished`/`stopped`/`killed`/`stale`), the
  date, pid, branch, deadline and last log line. The new `tidetable/runs.py`
  owns that read; the opencode, Pi and Paseo sidebars show a run line/badge, and
  the hub's `GET /api/<slug>/sidebar` includes it too.

- **`tkrun run --task ID`.** Run only the named task or tasks (repeatable);
  the run ends when none of them is left to run. Composes with `--jobs`.
- **Root `docker-compose.yml` for the VPS hub.** The core API is now brought
  up from a checkout: `git pull && cp .env.example .env && mkdir -p projects &&
  docker compose up -d --build`. The compose file, the Dockerfile and the
  registry all come from git; only `.env` and `./projects` are host state. The
  old `deploy/compose.vps.yml` (a `./src` sub-checkout and a hand-maintained
  registry) is superseded.
- **Command adjudicator for the gray zone.** A command on neither the allow nor
  the deny list can now go to a decision model instead of being refused outright.
  With `adjudicator.enabled`, the runner starts a loopback decision service; the
  agent's opencode hook (`integrations/opencode/tk-adjudicator.ts`, registered
  via `execute.before`) asks it and blocks the command on a non-allow answer. The
  agent only ever learns a `host:port` — the endpoint, key and model stay in the
  supervisor's environment. It fails closed (missing config, network error, low
  confidence, unexpected verdict all deny) and logs every verdict to
  `adjudicator.jsonl`. Judge one command without a run with
  `tidetable tkrun adjudicate <todo> "<command>"`.

### Documentation

- **Attended vs. unattended runs.** The runner is now described by its mode, not
  as "overnight": attended is an interactive session under your own permissions,
  unattended is TKRun. The permission model (the static allow/deny lists, and the
  implemented `clef`/Jev adjudicator for commands on neither list) is in
  `docs/permissions.md`, with the `--jobs`/`--task`/`--follow` material folded
  into `skills/tidetable/SKILL.md`. `overnight` remains a subcommand alias and
  the on-disk names (`.tidetable/overnight.json`, `.overnight/`,
  `overnight/<date>`) are unchanged.

### Fixed

- **A crashed parallel job keeps its worktree.** An exception in a `--jobs`
  task (for example the base-branch SAFETY check) used to remove the job's
  worktree and branch with the agent's uncommitted work in it. It is now kept
  and logged as `KEEP <id>`.
- **Docs no longer recommend a shared `CARGO_TARGET_DIR` for `--jobs`.** Sharing
  one across worktrees of the same crate gave an agent compile errors from
  another worktree's artifacts.

### Fixed

- **opencode agents kept their shell.** The generated `opencode.json` used an
  allowlist (`"*": "deny"` plus allow patterns), which opencode 2.x applies to
  the shell tool itself, so even `git status` was refused. It is now a denylist.
  The `rtk` prefix is matched too, since the rtk plugin rewrites `git ...` to
  `rtk git ...` before the permission check. Only publishing (`git push`, `git
  tag`), tidetable state and secret-dumping commands are denied; local git is
  allowed because one rejected command makes opencode drop the shell for the
  whole session. An agent that commits is tolerated: the runner folds its
  commits back into the working tree.

### Added

- **`push` option.** `"push": true` pushes the run branch to `origin` when a
  run ends. The agent itself still cannot push.

### Fixed

- **TKRun ignores stale sentinels and empty leftover folders.** A `STOP` or
  `KILL` file left over from an earlier run is removed at start instead of
  ending the next run before it begins. An empty, unregistered worktree folder
  (what `git worktree remove` can leave on Windows) is reused instead of
  refused.

### Security

- **TKRun keeps secrets out of the agent.** The runner's own tidetable
  credentials (`TIDETABLE_TOKEN`, `TIDETABLE_SERVER`, `TASKERKEEPER_TOKEN`,
  `TK_API_TOKEN`) are removed from the agent's environment, and `agent_env_drop`
  hides more. The prompt now forbids printing environment variables, `.env`
  files and credentials, and `env`, `printenv`, `set`, `git config --list`,
  `cat *.env*` and the PowerShell `env:` drive are on the shell deny list.
  An `agent_commands` entry that is really `opencode run` can set
  `opencode_permissions` so the shell allow/deny lists apply to it too; before,
  such a command ran under the user's own opencode permissions.
  `opencode_extra` merges more into that generated file, e.g. to disable MCP
  servers from the global opencode config, which the bash/edit rules do not cover.

### Added

- **`tkrun run --jobs N`.** Runs up to N tasks at once, each in its own
  worktree on its own branch, merged back onto the run branch one at a time as
  they finish. Only tasks that declare `touches` and whose `touches` do not
  overlap run together; a task without `touches` runs alone. A merge conflict
  keeps the job branch and reopens the task. `parallel.jobs` sets the default
  and `parallel.job_env` adds env (e.g. a shared `CARGO_TARGET_DIR`) to agent
  and gate processes. Not yet combinable with `siblings`.
- **`tkrun run --follow`.** Echoes the agent's output to the console as it
  runs, like the old ralph scripts. On by default when attached to a terminal;
  `--no-follow` restores the quiet log-only behaviour. The transcript file is
  written either way.
- **Per-phase review in TKRun.** `review.phase` (`{enabled, model, on_fail}`)
  adds an optional read-only review that runs once, when the last task of a
  phase closes, against the phase goal and the commits made for it. It has its
  own model, independent of the per-task review, so each can be on, off or on
  a different model. `on_fail: "stop"` ends the run on a phase FAIL; the default
  logs it and carries on. New flags: `--task-review-model`, `--no-task-review`
  (aliases of `--review-model`, `--no-review`), `--phase-review-model` and
  `--no-phase-review`. The default task reviewer is now `anthropic/claude-opus-5-5`.

- **Hub-aware sidebar.** `GET /api/<slug>/sidebar?width=&height=` on
  `tidetable serve` returns the same payload as `tidetable sidebar --json`,
  and `sidebar` now works in remote mode (JSON, or the rendered text when
  `--json` is absent). The Paseo plugin gains `server` / `token` settings
  (env `TIDETABLE_SERVER` / `TIDETABLE_TOKEN`); with a server set, `todo` is a
  project slug. The token reaches the CLI through its environment, not argv.
  The OpenCode and pi sidebars already shell out to the CLI, so exporting
  `TIDETABLE_SERVER`, `TIDETABLE_TOKEN` and `TIDETABLE_TODO=<slug>` works for
  them too.
- **TKRun against a hub.** `tidetable --server URL tkrun run <slug> --repo .`
  works a hub project from a local checkout: claims, `done` and `reset` go to
  the hub; code, gates, review and commits stay on a local throwaway branch.
  Nothing of the todo file is committed or diffed locally, and the owner is
  `overnight-<date>@<host>` so two machines never share an identity.
  `tkrun init|stop|kill` take `--repo` the same way. Without a server, the
  runner now passes `--local` to its CLI calls so a stray `$TIDETABLE_SERVER`
  cannot hijack a file path.

- **Remote client mode.** `tidetable --server https://hub next <slug>`
  (or `$TIDETABLE_SERVER` / `$TIDETABLE_TOKEN`) runs the command against a
  `tidetable serve` hub instead of a local file; `todo_file` is then the
  project slug. Covers `validate next ready list parallel deps history start
  done reset status add add-phase`. Always sends this machine's owner and
  host, prints the hub's JSON, and mirrors the local exit codes. `--local`
  opts out; `convert`, `sidebar` and `release` have no hub route and are
  refused. Stdlib `urllib` only, new module `tidetable/remote.py`.

### Changed

- **TaskerKeeper is now TideTable (0.9.0).** Package `taskerkeeper/` is
  `tidetable/`; commands `tidetable` and the short `tt`; env vars
  `TIDETABLE_*`; repo config `.tidetable/`; user config
  `~/.config/tidetable/`; skill `skills/tidetable/`; distribution name
  `tidetable`. Nothing in the todo format, the `--json` output, or the
  scheduling semantics changed.
- **Compatibility, so existing agents keep working.** The `taskerkeeper`
  command, `python -m taskerkeeper` and `import taskerkeeper` (same module
  objects as `tidetable`) still work. `TASKERKEEPER_*` env vars are read as a
  fallback (the new name wins). A `.taskerkeeper/` repo dir and a
  `~/.config/taskerkeeper/` user dir apply until the new one exists; new files
  are written to the new names. `scripts/taskerkeeper.py` stays as a shim.
  The `tkrun`/`overnight` subcommands and the `TK_*` serve env vars are
  unchanged.

### Added

- **`taskerkeeper add-phase <file> --phase <id> --title <t>`.** Inserts a new
  (empty) phase: `--goal`, repeatable `--prereq` of existing phase IDs,
  `--after`/`--before` anchor (mutually exclusive, default append). IDs must
  match `MAJOR.MINOR` and be unused; nothing half-writes on rejection. Also
  `POST /api/<slug>/add-phase` on the serve API. Phase prerequisites
  immediately gate the new phase's tasks through the existing `blockers_for`
  path — no scheduling change.

- **`taskerkeeper integrations list|install|uninstall`.** Installer for the
  harness sidebars: symlinks the Paseo plugin (copy fallback), merges the
  opencode `tui.json` plugin entry (refuses non-JSON, keeps a `.bak`),
  runs `pi install`, and prints the OpenChamber folder to paste. Run from a
  checkout (`integrations/` is not packaged); `--dry-run` previews.
- **`taskerkeeper overnight run|init|stop`.** A repo-agnostic unattended
  runner, ported from a per-repo PowerShell script. Per task: claim, run the
  tier's agent (`claude -p` / `opencode run`) with a generated allow/deny
  list and no skip-permissions flag, run the repo's gate commands itself, read-only
  review, commit, `done`. Sleeps through usage limits, parks failures as stashes,
  stops after two consecutive failures. Repo specifics (gate, prompt rules,
  deny-edit globs, sibling repos) live in `.taskerkeeper/overnight.json`;
  with no config the gate is inferred from Cargo.toml / package.json /
  pyproject.toml / go.mod. See `docs/overnight.md`.
- **The runner is TKRun; `overnight` is kept as an alias.** `taskerkeeper tkrun
  run|init|stop|kill` and `taskerkeeper overnight ...` are the same command.
- **`taskerkeeper tkrun kill` — hard stop.** Writes the `KILL` sentinel and
  tree-kills the runner PID, so a mid-task agent subprocess dies too. Unlike the
  cooperative `stop`, which is checked between tasks. Task claims are left to
  expire; partial work stays in the run worktree.
- **Leased lanes.** A provider that only answers inside a reserved window (for
  example SingularityAPI lanes) goes under `lanes` in
  `.taskerkeeper/overnight.json`. The runner reads reservations (never books),
  merges back-to-back hours, uses the lane while more than `margin_minutes`
  remain, and otherwise routes the task to `fallback` (also on a missing key,
  network error, or no reservation). A task already running when the lease ends
  is not interrupted. Keys come only from the environment.

### Fixed

- **TKRun resolves the run root, not just the todo path.** `Runner.__post_init__`
  resolved `todo` before `relative_to()` but left `home` as the raw root, so a
  root that was not already canonical (a symlink, or macOS `/var` under
  `/private/var`) raised `not in the subpath of`. `home` is now resolved too.

## [0.7.0] — 2026-09-23

Harness sidebars: one budgeted payload, four thin adapters.

### Added

- **`taskerkeeper sidebar <file> [--width] [--height]`.** Emits current
  (in-progress with owner, claim age, goal, touches), concurrent (the
  disjoint fan-out set), upcoming (deferred plus blocked with reasons), the
  current-phase tree anchored on live work, and the overall phase tree.
  Sections cut to room report `more` counts instead of silently dropping
  tasks; touches are basenamed. Text render matches `list` glyphs.
  Formatting only — every scheduling answer delegates to `cli.py`.
- **`integrations/opencode/`.** TUI plugin registering the `sidebar_content`
  slot (opencode-harness-panel pattern): collapsible read-only block, polls
  the sidebar command, fail-soft one-liner when the CLI is missing.
- **`integrations/pi/`.** Extension panel plus `/tk-sidebar on|off|width`
  in pi-sidebar-tui style; declares both `pi` and `omp` manifest keys so it
  loads under oh-my-pi too.
- **`integrations/openchamber/`.** Rail extension (manifest, dependency-free
  IIFE panel, full-page board): reads the serve API, attaches tasks to chat,
  zero capabilities for read-only.
- **`integrations/paseo/`.** Plugin with sidebar item (in-progress badge)
  and workspace panel tab, served through a read-only server RPC.
- **`docs/todo-sidebars.json`.** The milestone roadmap, worked through
  claim/ready/done like the hub before it.

## [0.6.0] — 2026-09-22

The VPS hub milestone: one single-writer core API so remote agents share a
todo file without NFS locking, plus a read-only live dashboard.

### Added

- **`taskerkeeper serve`.** stdlib HTTP API over the same DAG functions in
  `cli.py` — no second implementation. GET reads (`ready`, `next`, `list`,
  `deps`, `history`, `validate`, `parallel`) return the `--json` shapes;
  POST writes (`start`, `done`, `reset`, `status`, `add`) hold a server lock
  plus `FileLock`, reload inside the lock, and enforce claim leases, refusing
  a live foreign claim. Binds `127.0.0.1:8471` by default behind Traefik.
- **Agent presence.** `POST /api/<slug>/heartbeat` records repo, branch,
  worktree, and task in a sqlite sessions store
  (`taskerkeeper/sessions.py`); `start` heartbeats automatically,
  `done`/`reset` clear the task. Rows older than 180s read as stale.
- **Postgres projection.** `deploy/migrations/001_init.sql` defines
  `projects`, `tasks_snap`, `events`, `sessions`, and `machine_tokens`;
  `taskerkeeper/projector.py` tails each todo file and its `.events.jsonl`
  into those tables. Best-effort, never in the write path; Postgres decides
  nothing about scheduling.
- **SSE live stream.** `GET /api/stream?slugs=a,b&since=SEQ` replays merged
  events with `Last-Event-ID` support and a 15s heartbeat comment.
- **Dashboard scaffold (`web/`).** SvelteKit shell with a fleet view, an
  agents wall with staleness display, and a per-project view with blockers,
  disjoint `deferred` warnings, and a live event tail. Read-only; BetterAuth
  OIDC against Authentik as IdP.
- **Machine tokens (`taskerkeeper/tokens.py`).** Mintable per-project tokens
  with scopes and expiry, stored hashed, verified before the shared
  `TK_API_TOKEN` fallback. Revocation is a flag.
- **Deploy topology (`deploy/`).** `registry.json` maps slugs to todo paths;
  `compose.yml` wires core, Postgres, and dashboard with Traefik labels where
  `/api` deliberately bypasses forwardAuth (bearer only).
- **`docs/todo-vps-hub.json`.** The milestone roadmap, built and worked
  entirely through claim/ready/done.

## [0.5.0] — 2026-09-08

Parallel execution was the headline feature and was not actually enforced. This
release makes it real.

### Added

- **Claims with leases.** `start` records `claimed_by`, `claimed_at`, and
  `lease_expires_at` on a task. `start` refuses a task another agent holds under
  a live lease, `next` never hands out a live claim, and `done` needs `--force`
  to finish someone else's. A crashed agent's task becomes claimable when its
  lease lapses, so recovery no longer means editing fields by hand. Owner from
  `--owner` / `TASKERKEEPER_OWNER` / `host:pid`; lease from `--lease` /
  `TASKERKEEPER_LEASE_MINUTES` / 60 minutes.
- **`ready --disjoint` and `next --disjoint`.** `touches` is now scheduling
  input: tasks whose owned paths overlap are never emitted together, and tasks
  already in progress hold their paths. Paths overlap when equal or when one is
  a directory containing the other. Selection is greedy in ID order, so results
  are stable; skipped tasks come back under `deferred`. Plain `ready` reports
  overlaps as a warning instead of silently listing unsafe work.
- **Append-only event log.** Every `start`, `done`, `reset`, `status`, `add`,
  and `release` appends a JSON line to `<file>.events.jsonl`. `taskerkeeper
  history` replays it, with `--task` and `--limit`. `TASKERKEEPER_EVENTS=0`
  disables it. Status fields are overwritten in place, so the todo file alone
  could never answer "how many times was this reset, and by whom".
- **`taskerkeeper release [phase]`.** Prints the tag a completed phase ships as,
  with the changelog `done` collected; `--tag` creates the annotated tag,
  refusing an incomplete phase without `--force` and refusing to clobber an
  existing tag. `done --json` now reports `release_ready`. `tag_on_complete` was
  previously a field nothing acted on.
- **Complexity-driven tier selection.** A task with a `complexity` and no
  `agent` is routed by tier: Low→basic, Medium→mid, High→pro, Very High→
  flagship. A tier's `complexity_range` overrides that, accepting `"High"`,
  `"Low-Medium"`, `"Medium to High"`, `"Low, Very High"`. `complexity_range` was
  documentation nothing read; `add` hardcoded `mid_dev_agent`.
- **Stale lock recovery.** The lock file records pid, host, and time. A lock
  whose holder is confirmed gone is broken immediately; one whose holder cannot
  be checked is broken after `stale_after` (default 300s). Commands say when
  they broke one. Pid liveness on Windows goes through `OpenProcess`, never
  `os.kill(pid, 0)` — which on Windows terminates the process.
- `--json` on `start` and `done`; `--owner` on `next`, `start`, `done`, `reset`,
  and `status`.

### Changed

- **`parallel_group` no longer claims to schedule anything.** The docs said
  "different groups run sequentially within a phase"; no code ever implemented
  it. It is a label, read only by `parallel` and `convert`. Ordering is
  `prerequisites`; concurrency safety is `touches`.
- `resolve_task` now reports the tier it resolved (`agent`) and whether that
  tier was derived (`agent_derived`), so an unknown tier returns
  `{"agent": ..., "agent_derived": false}` rather than `{}`.
- `task_summary` carries the claim fields, so `ready --json` and `next --json`
  show who holds what.

### Fixed

- `next` could hand a live `in_progress` task to a second agent, putting two
  agents on the same work — the exact failure claiming is meant to prevent. It
  now resumes only the caller's own claim or an expired one.
- A crashed process left `<file>.lock` behind forever. The lock recorded a pid
  and nothing ever read it.

### Schema

Additive only. New task fields `claimed_by`, `claimed_at`, `lease_expires_at`;
`agent` documented as optional and derivable; descriptions corrected for
`parallel_group`, `touches`, and `complexity_range`. Existing files validate
unchanged.

## [0.4.0] — 2026-09-08

### Added

- Provider presets — a whole tier table per provider, so switching providers is
  one command instead of four.

  | Tier | `anthropic` | `opencode-go` |
  |------|-------------|---------------|
  | `basic_dev_agent` | `claude-haiku-4-5` | `glm-5.3-flash` |
  | `mid_dev_agent` | `claude-sonnet-5` | `glm-5.3-flash` |
  | `pro_dev_agent` | `claude-opus-5` | `deepseek-v4-pro` |
  | `flagship` | `claude-fable-5-1` | `qwen3.8-max` |

- `agents use <provider>` points a scope at a preset; `agents use --clear` drops
  a scope's selection. `agents providers` lists the presets and marks the active
  one. Both honour `--scope user` (default) / `repo` / `todo`.
- `agents show` reports the active preset and the layer that selected it, and
  labels preset-supplied settings as `<scope> preset` in the source column.
  `--json` gains `provider` and `provider_source`.
- Schema: `agent_config.provider` selects a preset for one todo file.

### Changed

- Each config layer now contributes twice — the preset it names, then its own
  `tiers` entries — so `agents set` still wins over `agents use` in the same
  scope, while a higher scope's preset wins over a lower scope's tier.
- `agents.read_config()` returns the whole config file; the old tiers-only
  behavior is `agents.read_tiers()`. `DEFAULT_TIERS` is now
  `PROVIDER_PRESETS["anthropic"]` and keeps working.

## [0.3.0] — 2026-09-08

### Added

- `agents` command group — configure which provider and model handles each
  agent tier, so `task.agent` resolves to something a supervisor can dispatch.
  - `agents show` prints the resolved mapping and the layer each setting came
    from; `--todo <file>` includes the todo file's layer and lists per-task
    overrides. `--json` for machine-readable output.
  - `agents set <tier>` writes `--provider` / `--model`, plus any other setting
    via `--option key=value`, into a `--scope` of `user` (default), `repo`, or
    `todo`.
  - `agents unset <tier>` removes a tier, or `--key` one setting, from a scope.
  - `agents path --scope <scope>` prints the file that scope writes to.
- Layered configuration, last wins: built-in defaults →
  `~/.config/taskerkeeper/agents.json` (under `%APPDATA%` on Windows) →
  `<repo>/.taskerkeeper/agents.json` → the todo file's `agent_config.tiers` →
  a task's own `provider` / `model`. `TASKERKEEPER_CONFIG_HOME` relocates the
  user config.
- `next`, `ready`, and `parallel` now carry the resolved `provider` and `model`
  on every task in `--json`, and `next` prints the model in its human output —
  dispatch needs one call, not two.
- Per-task `provider` / `model` in the schema, for the task that genuinely needs
  a specific model. Configure the tier when a whole class of work should move.
- `taskerkeeper/agents.py` and `taskerkeeper/jsonio.py`; the lock and the atomic
  write moved into the latter so config writes get the same guarantees as todo
  writes. `cli.FileLock`, `cli.load_todo`, and `cli.save_todo` still work.

### Changed

- Schema: `agent_config.tiers` entries accept `provider`, `model`, and any
  additional keys; `agent_config` documents where it sits in the layering.
  `agent_config.tiers` is no longer schema-only — the CLI reads it.

## [0.2.0] — 2026-09-08

Closes the findings of the v0.1 code review in `docs/notes.md`.

### Added

- `ready` — lists every currently-runnable task, with `--json`. This is what
  makes parallel dispatch possible; `parallel_group` was display-only before.
- `start` — claims a task (`pending` → `in_progress`) so two agents cannot pick
  up the same work. `in_progress` was a dead state that nothing could set.
- `reset` — returns a task to `pending`, recovering one orphaned by a crashed
  session.
- `status` — sets any status, including `cancelled` and
  `moved --moved-to <id>`. Both were in the schema with no CLI path.
- `convert` — renders a todo file as markdown for human review (one-way).
- `--json` on every read command (`next`, `ready`, `list`, `parallel`, `deps`),
  so agents stop scraping box-drawing characters.
- Semantic validation on top of the JSON Schema: duplicate task/phase IDs,
  dangling task/phase prerequisites, prerequisite cycles, task IDs that
  disagree with their phase, `moved` without a valid `moved_to`, and a warning
  when a prerequisite is `cancelled` or `moved` (blocked forever).
- Flags on `add`: `--prereq` (repeatable, validated), `--complexity`,
  `--agent`, `--parallel-group`, `--touches`, `--success`.
- `--force` on `start` and `done`; `--changelog` on `done`.
- `tests/test_cli.py` — 37 stdlib `unittest` tests, no dev dependencies.
- `.github/workflows/ci.yml` — tests and example validation on Linux and
  Windows, Python 3.10 and 3.13, against a non-editable install.
- `python -m taskerkeeper` entry point.
- `AGENTS.md` (rules for agents working on this repo) and `CLAUDE.md`
  (a pointer to it).

### Fixed

- **`pip install .` was broken.** `SCHEMA_PATH` resolved to
  `<package parent>/schema/`, which does not exist in `site-packages`, so only
  an editable install could validate anything. The schema moved to
  `taskerkeeper/schema/todo-v1.schema.json` and ships as package data, resolved
  via `importlib.resources`.
- **Phase-level prerequisites were ignored.** `find_next` only checked task
  prerequisites, so a task in phase 1.1 with no prerequisites of its own was
  handed out while phase 1.0 was half-done.
- **`in_progress` blocked work forever.** A crashed session left a task claimed;
  nothing resumed it and nothing downstream unblocked. `next` now returns an
  `in_progress` task first, and `reset` clears one.
- **Two implementations of the DAG logic had drifted.** `ralph/ralph-json.sh`
  is now a thin wrapper that execs the CLI. It had a tautological `select`
  (line 57), a `join(", ") // "none"` that never produced `none` (line 70), and
  ordering and `done`-enforcement rules that disagreed with the Python. `jq` is
  no longer a dependency.
- **`done` did not check prerequisites at all** (the jq path warned and wrote
  anyway). It now refuses without `--force`.
- **Concurrent writes lost updates.** Mutating commands take a `<file>.lock`,
  reload inside it, and write atomically via temp file + rename.
- **`add` crashed on hand-edited IDs** — `int(t["id"].split(".")[-1])` raised
  `ValueError` on any non-numeric sequence. Non-numeric IDs are now skipped when
  computing the next number.
- **`release.changelog_entries` claimed to be auto-collected and was not.**
  `done` now files a task's `changelog` line under its phase release, or under
  the last phase that has one.
- Timestamps are written as `...Z`, matching the examples, instead of `+00:00`.
- Misleading comment in `find_next`: it sorted by total prerequisite count, not
  unsatisfied ones (every candidate has zero unsatisfied). Ordering is now
  numeric by ID, which also matches what `ready` lists first.

### Changed

- `next` returns an `in_progress` task before a pending one. Pass `--no-resume`
  for the old behavior.
- `done` on a task with unmet prerequisites now exits 1 instead of writing.
- Schema: `agent_config.tiers` gained `flagship`, which the `agent` enum already
  allowed; `changelog` and `changelog_entries` descriptions now match what the
  code does; `prerequisites` documents that only `done` satisfies a dependency.
- Examples dropped the redundant `_phase_id` field and use `...Z` timestamps.
- Docs across `README.md`, `docs/philosophy.md`, `docs/integration-guide.md`,
  `docs/memory.md`, and the Hermes skill now describe the actual CLI. The
  stale "Project Structure" block, the "Python 3 standard library only" claim
  (it needs `jsonschema`), and the promised markdown→JSON `convert` are gone —
  markdown→JSON is now documented as a deliberate manual migration.

## [0.1.0] — 2026-09-02

### Added

- `pyproject.toml` — installable via `pip install -e .`; pulls `jsonschema`
  and registers a `taskerkeeper` console command.
- `taskerkeeper/` package — CLI logic lives at `taskerkeeper/cli.py`;
  `scripts/taskerkeeper.py` remains as a shim.
- `docs/memory.md` — project state and decisions for agents.
- `CHANGELOG.md` — this file.

### Fixed

- CLI crashed with `UnicodeEncodeError` on Windows (cp1252 console) printing
  checkmark glyphs. stdout/stderr now reconfigured to UTF-8 with
  `errors="replace"`; files are read/written as UTF-8.
- Schema rejected task `changelog: null`. The field now allows
  `["string", "null"]`, so the zoidmatter-v7 example validates.
- Docs aligned with the actual CLI — removed references to unimplemented
  `status`, `convert-md`, `convert-json`, and `add-task` commands and the
  positional-phase `add` form.

### Changed

- First verified state: both example files pass schema validation, and
  `validate` / `next` / `done` / `add` are exercised end-to-end against both.
