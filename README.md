# TideTable

**Structured task management for autonomous agents.**

> TideTable was **TaskerKeeper** before 0.9.0. The `taskerkeeper` command, `TASKERKEEPER_*`
> env vars and `.taskerkeeper/` config dirs still work; `tt` is the short command.

JSON-based alternative to markdown todo files. Stable IDs, dependency DAG, parallel execution groups, and semver version mapping. Built for [ralph](https://github.com/ClawfficeOrg/Zoid) but works with any autonomous agent.

## Why?

Markdown todo files (`docs/todo-v*.md`) are great for humans but painful for agents:

- **Inserting tasks breaks ordering** — IDs are positional, so adding `v9.0` between `v8.x` and `v10.0` requires renumbering everything below
- **No dependency expression** — prerequisites are prose, not machine-readable
- **No parallel support** — everything is linear by design
- **Token waste** — agents read the whole file even for one task

TideTable solves all of these with structured JSON.

## Quick Start

```bash
git clone https://github.com/ClawfficeOrg/TideTable.git
cd TideTable
pip install .          # or: pip install -e .  for development

tidetable validate my-todo.json
tidetable ready my-todo.json          # everything runnable right now
tidetable start my-todo.json 7.0.1    # claim it
tidetable done my-todo.json 7.0.1     # finish it
```

Without installing, `python -m tidetable <command>` and `python scripts/tidetable.py <command>` both work from a checkout.

## Commands

The todo file is optional on every command. Leave it out and TideTable uses the
`todo*.json` in the first of `.`, `docs/`, `.tidetable/` that has one. Several
files in that directory is an error (name one); none found asks on a terminal
and errors when there is none. With `--server` the argument is the project slug
and is never defaulted.

| Command | What it does |
|---------|--------------|
| `validate <file>` | JSON Schema check **plus** semantic checks: duplicate IDs, dangling prerequisites, cycles, task/phase ID mismatches |
| `ready <file>` | Every task that can start right now. `--disjoint` narrows it to a set whose owned paths do not collide — the list that is actually safe to fan out |
| `next <file>` | The single task to pick up. Resumes an `in_progress` task the caller may claim, else the lowest-numbered ready task |
| `start <file> <id>` | Claim a task (`pending` → `in_progress`) under an owner and a lease. Refuses if prerequisites are unmet, or if another agent holds a live claim |
| `done <file> <id>` | Finish a task, report what it unblocked, file its `--changelog` line under the phase release |
| `reset <file> <id>` | Return a task to `pending` — how you recover a task orphaned by a crashed session |
| `status <file> <id> <status>` | Set any status, including `cancelled` and `moved --moved-to <id>` |
| `list <file>` | Status overview per phase, with a summary |
| `parallel <file>` | Parallel groups, marking which members are runnable now |
| `deps <file> <id>` | What a task waits on and what it unblocks |
| `add <file> --phase <id> --title <t>` | Append a task. Accepts `--goal`, `--prereq` (repeatable), `--complexity`, `--agent`, `--parallel-group`, `--touches`, `--success` |
| `edit <file> <id> [flags]` | Change a task's plan fields (`--title`, `--goal`, `--prereq`, `--complexity`, `--agent`, `--parallel-group`, `--touches`, `--success`, `--tests`) and clear lists with `--clear-touches`/`--clear-success`/`--clear-prereq`/`--clear-tests`/`--clear-parallel-group`. Never touches status or claims; a dangling or cyclic prerequisite is refused. Also `POST /api/<slug>/edit` |
| `add-phase <file> --phase <id> --title <t>` | Insert a phase (`--goal`, `--prereq` repeatable phase IDs, `--after`/`--before` anchor; also `POST /api/<slug>/add-phase`) |
| `release <file> [phase]` | The tag a completed phase ships as. Prints it by default; `--tag` actually creates it |
| `history <file>` | Replay the append-only event log. `--task <id>` filters, `--limit N` tails |
| `convert <file>` | Render the JSON as markdown for human review (one-way) |
| `agents show` | The resolved provider/model for every agent tier, and where each setting came from |
| `agents providers` | List the built-in provider presets and which one is active |
| `agents use <provider>` | Point a scope at a provider preset — switches every tier at once |
| `agents set <tier>` | Point a tier at a provider/model. `--scope user` (default), `repo`, or `todo` |
| `agents unset <tier>` | Drop a tier, or `--key` one setting, from a scope |
| `agents path` | Print the config file a scope writes to |
| `serve [--port 8471] [--bind 127.0.0.1]` | Single-writer core API over HTTP: GET reads, locked POST writes, heartbeats, and an SSE stream — all delegating to the same DAG logic, never a second implementation |
| `sidebar <file> [--width 80] [--height 40]` | Budgeted sidebar payload for harness panels: current, concurrent (disjoint), upcoming, current-phase tree, overall tree, and a live `run` section (TKRun status, branch, deadline, last log line) — sections cut to room with `more` counts instead of silent drops |
| `tkrun run <file> [--hours 8] [--dry-run] [--jobs N] [--task ID] [--follow]` | Unattended runner (**TKRun**; `overnight` is a kept alias): claims each ready task, runs its tier's agent headless in its own git worktree on `tkrun/<date>`, gates it with *your* commands, reviews, commits, closes. Commits only there: never merges, pushes, or touches the base branch. `--jobs N` runs up to N tasks at once, one worktree each, only disjoint `touches` together; `--task ID` (repeatable) narrows the run to named tasks; `--follow` echoes output live. `tkrun init` writes `.tidetable/tkrun.json` from the detected stack; `tkrun stop` ends a run after the current task; `tkrun kill` hard-stops now (sentinel plus process-tree signal). Leased lanes route a reservation-window provider to a fallback when the window closes. See [docs/tkrun.md](docs/tkrun.md) |
| `integrations list\|install\|uninstall <name\|all>` | Wire `integrations/` (opencode, pi, paseo, openchamber) into the tools that load them: runs `paseo plugin install` on the Paseo plugin (`--paseo-dir` keeps the old symlink), adds the opencode `tui.json` entry without touching your other plugins, runs `pi install`, prints the OpenChamber folder to paste. Needs a checkout; `--dry-run` previews |

Every read command takes `--json`, so agents parse structured output instead of scraping box-drawing characters:

```bash
tidetable ready docs/todo-v7.json --json
```

## Claiming Work

`start` does not just flip a status — it records **who** holds the task and for
how long:

```json
{ "status": "in_progress", "claimed_by": "worker-3",
  "claimed_at": "2026-09-08T22:10:00Z", "lease_expires_at": "2026-09-08T23:10:00Z" }
```

That is what lets several agents share one file safely:

- `start` refuses a task another agent holds under a live lease.
- `next` never hands out a live claim. It resumes only the caller's own task, or
  one whose lease has lapsed.
- `done` refuses to finish someone else's live claim without `--force`.
- A crashed agent's task becomes claimable again when its lease expires — no
  human editing fields by hand.

Name the agent with `--owner`, or `TIDETABLE_OWNER`; without either it is
`host:pid`. The lease is `--lease MINUTES`, or `TIDETABLE_LEASE_MINUTES`,
default 60.

## Fanning Out Safely

`ready` lists what *could* run. Two of those tasks editing the same file is the
failure this tool exists to prevent, so `--disjoint` answers the question a
supervisor actually has — what can I dispatch **together**:

```bash
tidetable ready docs/todo-v7.json --disjoint --json
```

Tasks conflict when their `touches` paths overlap: the same path, or one a
directory containing the other (`src/routes/` collides with
`src/routes/handlers.rs`). Tasks already `in_progress` hold their paths too.
Selection is greedy in ID order, so the answer is stable and the
lowest-numbered task wins a contested path; everything skipped comes back under
`deferred` with what it collided with. Without `--disjoint`, overlaps are
reported as a warning rather than silently ignored. `next --disjoint` applies
the same rule to a single task.

## Unattended Runs

TideTable runs in two modes that differ only in who approves a shell command.

**Attended** is you driving a session: your own opencode/claude permissions
apply, you see each command, and the base branch is yours. That is the ordinary
`start`/`done` CLI and the `serve` hub.

**Unattended** is **TKRun** (`tidetable tkrun run`), which works through a todo
file on a throwaway worktree branch with no one watching. The runner — never the
agent — owns git history and TideTable state, and it decides each command with
static lists passed to `claude -p` or written into a temporary `opencode.json`:
a read-only allow list, and a deny list of what must never happen (`git
push`/`tag`, history rewrites, TideTable state writes, env/secret dumps). A
command on neither list goes to an **adjudicator** — a decision model (clef on a
Jev endpoint) that returns allow/deny with a confidence. It fails closed: no
endpoint, a network error or a low-confidence verdict all mean deny.
`adjudicator.enabled` turns it on; `TIDETABLE_DECISION_ENDPOINT`,
`TIDETABLE_DECISION_API_KEY` and `TIDETABLE_DECISION_MODEL` carry the credentials
(env only). See [docs/permissions.md](docs/permissions.md).

`tkrun run --jobs N` runs up to N tasks at once, each in its own worktree on its
own branch, merged back one at a time; only tasks with disjoint `touches` run
together. `--task ID` narrows a run to named tasks. See
[docs/tkrun.md](docs/tkrun.md).

## Self-Hosting the Hub

`tidetable serve` is the single-writer core API remote workers and the
dashboard talk to. On the VPS it is one container, built from this repo:

```bash
git clone git@github.com:ClawfficeOrg/TideTable.git
cd TideTable
cp .env.example .env      # set TK_API_TOKEN
mkdir -p projects
docker compose up -d --build
```

The root `docker-compose.yml` builds the image from the repo `Dockerfile`,
mounts `./projects` for the todo files it writes, and mounts
`deploy/registry.json` (tracked) as the registry. Routing is Traefik labels on
the shared `proxy-public` network; the `/api` route carries no forwardAuth, so
machine agents authenticate with the bearer token at the app layer.

## History

Status fields are overwritten in place, so the todo file cannot answer "how many
times did this task get reset, and by whom". An append-only log beside it can:

```bash
tidetable history docs/todo-v7.json --task 7.0.1
```

Every `start`, `done`, `reset`, `status`, `add`, and `release` appends one JSON
line to `<file>.events.jsonl`. Set `TIDETABLE_EVENTS=0` to turn it off.

## Which Model Runs a Task

A task names an agent tier (`basic_dev_agent`, `mid_dev_agent`, `pro_dev_agent`,
`flagship`). TideTable turns that tier into a concrete provider and model, so
a supervisor reading `ready --json` can dispatch without a second lookup.

A task may name a `complexity` instead and let the tier table decide:

| `complexity` | Default tier |
|---|---|
| `Low` | `basic_dev_agent` |
| `Medium` | `mid_dev_agent` |
| `High` | `pro_dev_agent` |
| `Very High` | `flagship` |

A tier's `complexity_range` overrides that mapping —
`agents set pro_dev_agent --option complexity_range="High, Very High"` routes
both to `pro_dev_agent`. Ranges accept the shapes people write: `"High"`,
`"Low-Medium"`, `"Medium to High"`, `"Low, Very High"`. An explicit `agent` on a
task always wins, and resolved output reports `agent_derived: true` when the
tier was inferred.

```bash
tidetable agents providers                 # the presets on offer
tidetable agents use opencode-go           # switch every tier at once
tidetable agents show                      # what that resolved to, and why
tidetable agents set pro_dev_agent --model claude-opus-5 --scope repo
```

### Provider presets

A preset is a whole tier table for one provider, so moving providers is one
command rather than four:

| Tier | `anthropic` | `opencode-go` |
|------|-------------|---------------|
| `basic_dev_agent` | `claude-haiku-4-5` | `glm-5.3-flash` |
| `mid_dev_agent` | `claude-sonnet-5` | `glm-5.3-flash` |
| `pro_dev_agent` | `claude-opus-5` | `deepseek-v4-pro` |
| `flagship` | `claude-fable-5-1` | `qwen3.8-max` |

`anthropic` is the preset in force until a scope selects another one.
`agents use <provider> --scope repo` pins a project to one; `agents use --clear`
drops a scope's selection.

### Layers

Configuration is layered, last wins:

| Layer | Where | For |
|-------|-------|-----|
| built-in | the `anthropic` preset in `tidetable/agents.py` | Works unconfigured |
| user | `~/.config/tidetable/agents.json` (under `%APPDATA%` on Windows) | Your machine's normal choice |
| repo | `<repo>/.tidetable/agents.json` | A project pinning something different |
| todo | the todo file's `agent_config` | One milestone pinning something different |
| task | a task's own `provider` / `model` | The one task that needs a specific model |

Each of the user, repo, and todo layers contributes twice: the preset it names
(`provider`), then its own per-tier settings (`tiers`). So `agents use` moves
every tier in a scope while an `agents set` in that same scope still wins.

`agents show` prints the source of every resolved setting, so you can see which
layer won. Set `TIDETABLE_CONFIG_HOME` to relocate the user config.

Any other key you set (`--option effort=xhigh`) passes through untouched into
the resolved output — TideTable does not call any provider itself, it only
tells your supervisor what to call.

Resolved values appear on every task in `--json` output:

```json
{ "id": "7.0.1", "agent": "mid_dev_agent",
  "provider": "anthropic", "model": "claude-sonnet-5" }
```

## Scheduling Rules

A task is runnable when **all** of these hold:

1. its `status` is `pending`,
2. every ID in its `prerequisites` belongs to a task with `status: "done"`, and
3. every phase in its phase's `prerequisites` is complete.

A phase is complete when none of its tasks will be worked on again — every task is `done`, `cancelled`, or `moved`. Only `done` satisfies a *task* prerequisite; `validate` warns when a prerequisite is `cancelled` or `moved`, because dependents would block forever.

Nothing above involves `parallel_group`: it is a label for humans, not an input
to scheduling. Ordering is `prerequisites`; fan-out safety is `touches`.

Writes take a `<file>.lock` and land atomically, so parallel agents cannot
silently overwrite each other. The lock records the pid and host that took it,
so a lock left behind by a crashed agent is broken automatically — when its
holder is confirmed gone, or after five minutes when the holder cannot be
checked at all.

## Terminal UI

`tidetable tui` opens a ratatui front end (Now / Upcoming / Done / Review) for a todo file
or a hub project. It is a separate binary, not part of the wheel, and it never writes a todo
file: actions go through the CLI or the hub, and every action (start, done, defer, clear,
reject) works in both modes.

```bash
cargo build --release --manifest-path tui/Cargo.toml     # Rust 1.88+
tidetable tui [--mode auto|standard|side|bottom] [--tmux] [-- docs/todo.json --read-only]
tidetable-tui --server https://hub.example myproj        # token from $TIDETABLE_TOKEN
```

In hub mode the project slug comes from the positional argument, `$TIDETABLE_PROJECT`, a
`slug` key in the nearest `.tidetable/config.json`, or a picker over `GET /api/projects`.
The picker and the project list are scoped to the token.

Keys: `j`/`k` move, `Tab`/`1`-`4` views, `Space` tick a review criterion, `s` start (Upcoming), `d` done, `l` defer,
`c` clear, `x` reject, `r` refresh, `q` quit. `--read-only` removes the action keys.
Palettes: `--palette biolume|deep-tide|sea-glass|phosphor|boiled`. In tmux,
`--tmux --mode side` splits a 40-column pane (`bottom` a 10-row one). Full reference in
[docs/integration-guide.md](docs/integration-guide.md#terminal-ui-tidetable-tui).

## Features

| Feature | Markdown | TideTable JSON |
|---------|----------|-------------------|
| Task ordering | Positional (line number) | ID-based (stable) |
| Inserting a task | Resequence all below | Add anywhere, no renumber |
| Dependencies | Prose in goal text | `prerequisites: [...]` array |
| Parallel work | Impossible | `ready --disjoint`, from `touches` |
| Claiming a task | Nothing | Owner + lease, enforced by `start`/`next`/`done` |
| Audit trail | Git history of the file | Append-only `<file>.events.jsonl` |
| Version mapping | Manual/disconnected | `release.version` on phase |
| Machine parsing | Regex/line-based | Native JSON, `--json` output |

## Schema

The JSON Schema ships inside the package at `tidetable/schema/todo-v1.schema.json`, so it resolves from any install. Validate any todo file:

```bash
tidetable validate docs/todo-v7.json
```

## For Ralph

`ralph/ralph-json.sh` is a thin wrapper around the CLI — same commands, same semantics, no `jq` required. (It used to be a second implementation of the DAG logic; the two drifted, so there is now one.)

```bash
./ralph/ralph-json.sh ready docs/todo-v7.json --json
./ralph/ralph-json.sh start docs/todo-v7.json 7.0.1
./ralph/ralph-json.sh done docs/todo-v7.json 7.0.1
```

## For Hermes

TideTable includes a Hermes skill at `skills/tidetable/SKILL.md`:

```bash
cp -r skills/tidetable ~/.hermes/skills/
```

## Releases

A phase can declare the release it ships:

```json
"release": { "version": "v0.7.0", "tag_on_complete": true,
             "release_notes": "SDKs and deployment." }
```

`done` never creates a tag. The last task of a phase is an ordinary task, and a
git side effect fired from it lands at a moment nobody chose — so `done --json`
reports `release_ready` and stops there. Tagging is its own step:

```bash
tidetable release docs/todo-v7.json          # what would be tagged, and why
tidetable release docs/todo-v7.json 7.1 --tag
```

`--tag` writes an annotated tag whose message is the release notes followed by
the changelog lines collected by `done`. It refuses an incomplete phase without
`--force`, and refuses to clobber an existing tag. Pushing it stays yours.

## Version Mapping

Task IDs are planning references, not release versions. The release version lives on the final phase of a milestone:

```
Planning milestone (roadmap)     →    Release version (semver)
─────────────────────────────         ──────────────────────────
todo-v7.md (internal roadmap)    →    v0.7.0 (git tag)
todo-v8.md                       →    v0.8.0
...                               →    ...
todo-v1.0.md                     →    v1.0.0 (first stable)
```

## Project Structure

```
TideTable/
├── tidetable/
│   ├── cli.py                  # All CLI logic
│   ├── agents.py               # Tier → provider/model resolution
│   ├── jsonio.py               # Atomic writes + the file lock
│   ├── __main__.py             # python -m tidetable
│   └── schema/
│       └── todo-v1.schema.json # JSON Schema, shipped as package data
├── ralph/
│   └── ralph-json.sh           # Thin wrapper around the CLI
├── scripts/
│   └── tidetable.py         # Backward-compatible shim
├── skills/
│   └── tidetable/
│       └── SKILL.md            # Hermes skill for AI models
├── tests/
│   └── test_cli.py             # stdlib unittest suite
├── docs/
│   ├── philosophy.md           # Why this exists
│   ├── integration-guide.md    # ZoidMatter + standalone setup
│   ├── module-plan.md          # Plan: importable core, exit codes, PyPI
│   └── memory.md               # Project state for agents
├── examples/
│   ├── simple-project.json     # Minimal example
│   └── zoidmatter-v7-example.json  # Real-world example
├── AGENTS.md                   # Rules for agents working on this repo
└── pyproject.toml
```

## Tests

```bash
python -m unittest discover -s tests -v
```

No dev dependencies — stdlib `unittest`. `pytest` runs the same suite if you prefer it.

## Integration with ZoidMatter

See [docs/integration-guide.md](docs/integration-guide.md).

## License

MIT
