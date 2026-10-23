# AGENTS.md

Rules for any agent working **on** this repository. This is the authoritative
rules file; `CLAUDE.md` only points here.

## What this project is

TideTable is a JSON todo format plus a Python CLI for driving autonomous
agents through a task DAG. A todo file describes a milestone as phases, each
phase holding tasks with stable IDs, prerequisites, and parallel groups. The
CLI answers one question well: *what can be worked on right now?*

It is a tool for agents, so the output contract matters as much as the logic.
Every read command has a `--json` mode; do not break it.

## Layout

```
tidetable/cli.py                    All CLI logic. One file on purpose.
tidetable/agents.py                 Tier -> provider/model resolution
tidetable/integrations.py           `integrations list|install|uninstall` for the sidebar adapters
tidetable/tkrun.py                  Unattended runner TKRun (`tkrun run|init|stop|kill`, `overnight` alias), the one supervisor
tidetable/runs.py                   Live run state (pid / STOP / KILL / log) read by the sidebar
tidetable/remote.py                 `--server` client: argv -> hub HTTP route, no logic
tidetable/tui.py                    `tidetable tui`: finds tidetable-tui, builds the (tmux) command line, no logic
tui/                                tidetable-tui, the ratatui front end (Rust, not in the wheel; `cd tui && cargo test`)
tidetable/jsonio.py                 Atomic writes, the file lock, the event log
tidetable/__main__.py               python -m tidetable
taskerkeeper/                          Pre-0.9 name: aliases every tidetable module, no logic
tidetable/schema/todo-v1.schema.json  JSON Schema (draft 2020-12), package data
scripts/tidetable.py                Forwarding shim, no logic (scripts/taskerkeeper.py is the legacy twin)
ralph/ralph-json.sh                    Thin wrapper that execs the CLI, no logic
skills/tidetable/SKILL.md           Hermes skill describing the format
tests/test_cli.py                      stdlib unittest suite (tests/test_tui.py covers `tidetable tui`)
examples/                              simple-project, zoidmatter-v7
docs/                                  philosophy, integration-guide, memory, module-plan, notes
```

## Commands you will need

```bash
python -m unittest discover -s tests -v     # the whole suite, under a second
tidetable agents show                    # resolved tier -> provider/model
tidetable validate examples/simple-project.json
pip install .                               # non-editable install must keep working
(cd tui && cargo test && cargo clippy --all-targets -- -D warnings)   # when tui/ changed
```

Run both before claiming any change works. `pip install .` matters because the
schema is package data; a change that moves it out of `tidetable/` bricks
every non-editable install.

## Rules

**`remote.py` is a client, not a second implementation.** With `--server` the
CLI maps argv to one `serve` route and prints the JSON it gets back. It holds
no scheduling, claim, or lock logic and must stay that way: the hub's
`handle_get`/`handle_post` over `cli.py` is the only writer. It always sends
`owner` and `host` from the *calling* machine, because the server's defaults
name the server. Remote output is JSON only; a new `serve` route that should be
reachable from the CLI gets a row in `remote.ROUTES` and a test in
`TestRemoteClient`.

**TKRun on a hub keeps the todo off the branch.** With `--server`, `Runner.hub`
is set: `todo_arg` is the slug, `todo_paths` is empty, and `todo_changes()`
returns `[]` — never call `changed_paths(root, [])` for todo files, an empty
pathspec means "everything". The owner includes the hostname. Local mode passes
`--local` to every CLI call; keep it.

**The old name keeps working.** The project was TaskerKeeper before 0.9.0.
`TASKERKEEPER_*` env vars are read through `jsonio.env_get` (new name wins),
`.taskerkeeper/` and `~/.config/taskerkeeper/` apply until the new directory
exists, and `import taskerkeeper` aliases the same module objects. New code
uses the new names; do not add logic to the `taskerkeeper/` shim package, and
do not remove a fallback without a changelog line.

**One implementation of the DAG logic.** It lives in `tidetable/cli.py`.
`ralph/ralph-json.sh`, `scripts/tidetable.py` and the `taskerkeeper/` package are forwarding shims and
must stay that way. A second implementation in bash/jq existed once, drifted
from the Python within a single release, and disagreed about task ordering and
prerequisite enforcement. Do not reintroduce one.

**Scheduling semantics are the contract.** A task is runnable only when it is
`pending`, every task in its `prerequisites` is `done`, and every phase in its
phase's `prerequisites` is complete. A phase is complete when all its tasks are
`done`, `cancelled`, or `moved`. Only `done` satisfies a task prerequisite.
Changing any of this changes what every downstream agent does — update
`ready_tasks`, `blockers_for`, the tests, and the README table together.

**`parallel_group` schedules nothing.** It is a label, read only by `parallel`
and `convert`. It used to be documented as making groups run sequentially, which
no code ever did. There is exactly one way to block a task — `prerequisites` —
and exactly one way to decide what runs together — `touches` overlap. Do not add
a second gate to `blockers_for`.

**A claim is enforced, not advisory.** `start` writes `claimed_by`,
`claimed_at`, and `lease_expires_at`; anything that leaves `in_progress` clears
all three (`set_status` does it, so do not bypass it). `find_next` may only
return an `in_progress` task that `claimable_by` accepts — the caller's own, or
one whose lease has lapsed. Handing a live claim to a second agent puts two
agents on one task, which is the failure this whole file exists to prevent. A
task with no `lease_expires_at` counts as expired, so pre-0.5 files still work.

**`touches` is scheduling input, not documentation.** `ready --disjoint` and
`next --disjoint` use `paths_conflict` — equal paths, or one a directory
containing the other — to keep two agents out of the same file, and in-progress
tasks hold their paths. Keep the greedy selection in ID order: a supervisor that
gets a different set each call cannot reason about what it dispatched.

**Never probe a pid with `os.kill(pid, 0)`.** On Windows CPython implements
`os.kill` as `TerminateProcess`, so the POSIX idiom would kill the process it
was checking. `jsonio._pid_alive` uses `OpenProcess`/`GetExitCodeProcess` there
and returns `None` when it cannot tell; a test asserts `os.kill` is never
called. Unknown liveness falls back to lock age, never to "assume dead".

**TideTable does not touch git except in `release --tag`.** `done` reports
`release_ready` and stops. The last task of a phase is an ordinary task, and a
tag created as its side effect appears at a moment nobody chose. Keep tagging
behind the explicit command and the explicit flag. The one other exception is
`tkrun.py` (below).

**The event log is append-only and best-effort.** `append_event` swallows its
own IO errors on purpose: losing an audit line must never fail the write that
produced it. Events go to `<file>.events.jsonl`, never into the todo file — the
roadmap stays small and diffable, and a torn append costs one line.

**Writes are locked and atomic.** Mutating commands go through
`with FileLock(path):`, reload inside the lock, and save via `write_json`, which
writes a temp file and renames. Both live in `tidetable/jsonio.py`. Parallel
agents are the entire point; a plain `open(path, "w")` reintroduces lost updates.

**TideTable never calls a provider.** `agents.py` resolves a tier to a
provider/model string and hands it to whoever is dispatching. Do not add an SDK
dependency, an API key lookup, or a network call — the supervisor owns that.
Model IDs in `DEFAULT_TIERS` are data, not endorsements; keep them current but
do not build logic around specific ones.

**`tkrun.py` is the only supervisor.** `tidetable tkrun run` (alias
`overnight run`) shells out to agent CLIs and to git, because an unattended loop
has to. That is
allowed in exactly that module: it never imports an SDK or reads an API key
(the CLIs own auth), `cli.py` only registers its subparser, and no scheduling
logic may depend on it. It must never touch the base branch, push, or tag; the
runner, not the agent, owns git history and tidetable state. Everything
repo-specific belongs in `<repo>/.tidetable/tkrun.json`, never in code.

**The old name keeps working.** The runner was `overnight` before 0.11.
`overnight` stays a subparser alias, `tidetable/overnight.py` is a shim that
re-exports the *same module object* as `tidetable/tkrun.py` (so monkeypatching
either name affects both), `TKRUN_*` env vars fall back to `OVERNIGHT_*`, and a
legacy `.tidetable/overnight.json` config or `.overnight/` run directory is used
when the new one is absent. Do not remove a fallback without a changelog line.
`tidetable/runs.py` is the one reader of the run directory; `tkrun.py` and the
sidebar both go through it rather than naming `.tkrun`/`.overnight` themselves.

**Config layering is last-wins, and the order is fixed:** built-in, user, repo,
todo file, then a task's own `provider`/`model`. Each config layer contributes
twice — the preset it names, then its own per-tier settings — so an
`agents set` beats an `agents use` in the same scope. `agents show` reports the
source of every resolved setting; keep that attribution working when you touch
`resolve_tiers` or `layers`.

**Every provider preset covers every tier.** `PROVIDER_PRESETS` is keyed by
provider, and each preset must define all four tiers in the schema's `agent`
enum, with its own name as the `provider` on each. A test enforces both. Adding
a provider is a new entry there and a row in the README table — no code.

**Validation has two layers.** JSON Schema checks shape; `semantic_errors`
checks meaning — duplicate IDs, dangling prerequisites, cycles, task IDs that
disagree with their phase, `moved` without `moved_to`. New structural
invariants belong in `semantic_errors` with a test, not in prose.

**Timestamps are `...Z`.** `utc_now()` produces them. Examples use the same
form; do not write `+00:00`.

**Keep the glyphs** (`✓ ✗ ► · → ═ ─ •`). stdout/stderr are reconfigured to
UTF-8 with `errors="replace"` at import, which is what makes them safe on
Windows cp1252 consoles. Do not strip to ASCII, and do not remove the
reconfigure block.

**Docs must match the code.** This repo previously advertised commands that did
not exist. If you add, rename, or remove a command, update `README.md`,
`skills/tidetable/SKILL.md`, `docs/integration-guide.md`, and `CHANGELOG.md`
in the same change.

**The TUI never writes a todo file.** `tui/` reads through the `tidetable` CLI (local) or the
hub (`--server`) and acts through the same two doors; the only file it writes is the
review-tick sidecar. Every action has a hub route: start, done, look-defer, look-clear
(handled by `handle_post`, never by a second DAG implementation). Hub mode resolves the
project slug from the argument, `$TIDETABLE_PROJECT`, `.tidetable/config.json`, then
`GET /api/projects` (token-scoped); it never reads git remotes. 401/403 stop the refresh
loop instead of backing off. A test greps the data layer for write APIs. A new hub read goes in
`remote.ROUTES` and `serve.ROUTES` with a `TestRemoteClient` test.

**Every behavior change gets a test.** `tests/test_cli.py` is stdlib
`unittest`, so it runs with no dev dependencies. Keep it that way.

## Conventions

- Python ≥ 3.10, `from __future__ import annotations`, 4-space indent, type
  hints on function signatures.
- Tests that touch agent config must set `TIDETABLE_CONFIG_HOME` to a temp
  directory. A test that reads the developer's real `~/.config` is a bug.
- Every test module imports `tests/hermetic.py` first (the `try: from . import
  hermetic` block at the top). It strips `TIDETABLE_*`, `TASKERKEEPER_*`,
  `TKRUN_*`, `OVERNIGHT_*` and `TK_*` from the environment and points
  `TIDETABLE_CONFIG_HOME` at a throwaway directory, so a shell exported at a
  hub cannot route tests to it. A new test module copies that block; a test
  that needs one of those variables sets it under `mock.patch.dict`.
- The only runtime dependency is `jsonschema`. Do not add more without a reason
  that survives "could the stdlib do this".
- Comments explain *why*, not *what*. The existing ones flag the bug a piece of
  code prevents; match that.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Bump `version` in `pyproject.toml` and `__version__` in
  `tidetable/__init__.py` together, and add a `CHANGELOG.md` entry.

## Working on a TideTable todo file

One project, one todo file (`docs/todo.json` here). Add new work as a phase in
that file; never split by size or topic, because separate files overlap and make
the bare `tidetable ready` refuse ("more than one todo file found"). Size costs
nothing: query with `tidetable ready|list` or `jq`, don't read the whole file.

When driving a project's roadmap (this repo's own, or another's):

```bash
tidetable ready docs/todo-v7.json --json   # fan these out in parallel
tidetable start docs/todo-v7.json 7.0.1    # claim before working
tidetable done docs/todo-v7.json 7.0.1 --changelog "Added the Go SDK"
```

Claim with `start` before doing the work, so a second agent does not pick up the
same task. If a session dies mid-task, the task stays `in_progress` — `next`
surfaces it first for resumption, and `reset` returns it to `pending`.

Never renumber task IDs. Insert new tasks with new IDs; retire old ones with
`status moved --moved-to <id>` or `status cancelled`.

## Known gaps

Tracked in `docs/memory.md` under "Known Gaps". `docs/notes.md` is an external
code review of v0.1; the issues it raises were fixed in v0.2, so read it as
history rather than a task list.
