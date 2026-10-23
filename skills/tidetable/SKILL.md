---
name: tidetable
description: "Use when creating, reading, or managing TideTable JSON todo files. Structured task management with dependency DAG, parallel groups, and semver mapping."
version: 2.5.0
author: KITT (ClawfficeOrg)
license: MIT
metadata:
  hermes:
    tags: [task-management, todo, json, ralph, autonomous, project-planning]
    related_skills: [ralph-todo-format, development-workflows]
---

# TideTable Skill

Task management for autonomous agents using structured JSON instead of markdown. Stable IDs, dependency DAG, parallel execution groups, and semver version mapping.

## When to Use

- Creating todo files for projects that use ralph or other autonomous agents
- Managing tasks with dependencies that need parallel execution
- Dispatching several agents at once across a runnable task set
- Reading/managing task state during development sessions

## File Format

TideTable uses JSON files (typically `docs/todo-v{N}.json`) with this structure:

```json
{
  "schema_version": "1.0.0",
  "project": {
    "name": "MyProject",
    "version": {
      "milestone": "v7",
      "release_version": "0.7.0"
    },
    "description": "Project description"
  },
  "phases": [
    {
      "id": "7.0",
      "title": "Phase Title",
      "goal": "What this phase delivers",
      "prerequisites": [],
      "tasks": [...]
    }
  ]
}
```

## Task Structure

Each task has:

```json
{
  "id": "7.0.1",
  "title": "Task title",
  "status": "pending",
  "goal": "Detailed description",
  "touches": ["file/path.rs"],
  "success": ["Criteria 1", "Criteria 2"],
  "prerequisites": ["6.0.3"],
  "parallel_group": "sdks",
  "complexity": "Medium",
  "agent": "mid_dev_agent"
}
```

### Key Fields

- **`id`**: Stable identifier. Format: `PHASE.SEQUENCE`. Never changes.
- **`status`**: `pending`, `in_progress`, `done`, `cancelled`, `moved`
- **`prerequisites`**: Task IDs that must be `done` before this can start. THIS IS THE KEY FEATURE.
- **`touches`**: File paths this task owns. `ready --disjoint` uses them to emit a set that is safe to dispatch together — two tasks whose paths overlap are never handed out at once.
- **`parallel_group`**: A label for related work. Purely descriptive: it does not schedule, gate, or order anything. Use `prerequisites` for real ordering.
- **`complexity`**: `Low` / `Medium` / `High` / `Very High`. With no `agent`, this picks the tier (Low→basic, Medium→mid, High→pro, Very High→flagship, unless a tier's `complexity_range` says otherwise).
- **`agent`**: Which agent tier handles this: `basic_dev_agent`, `mid_dev_agent`, `pro_dev_agent`, `flagship`. Optional — derived from `complexity` when absent. The tier resolves to a concrete provider and model — run `tidetable agents show` to see the mapping, and read `provider`/`model` off `ready --json` to dispatch.
- **`claimed_by`** / **`claimed_at`** / **`lease_expires_at`**: Written by `start`, cleared on any non-`in_progress` status. Do not hand-edit; use `start` and `reset`.
- **`provider`** / **`model`**: Optional per-task override, for the one task that needs a specific model. Configure the tier instead when a whole class of work should move.

## Reading Tasks

### Find Next Task

Prefer the CLI over reimplementing this: `tidetable ready <file> --json` for
the whole runnable set, `tidetable next <file> --json` for a single task.

The rule the CLI applies, if you must reason about it directly:

1. Load the JSON file
2. Collect all task IDs with `status: "done"`, and all phases whose tasks are
   all `done` / `cancelled` / `moved`
3. A task is runnable when `status: "pending"`, every entry in its
   `prerequisites` is in the done set, AND every entry in its phase's
   `prerequisites` is a complete phase
4. An `in_progress` task means an agent claimed it. Resume it before starting
   anything new **only if the claim is yours or its `lease_expires_at` has
   passed** — otherwise another agent is working it right now
5. If nothing is runnable, report which prerequisite each pending task waits on

### Check Dependencies

For a task like `7.1.1` with `prerequisites: ["7.0.1", "7.0.2", "7.0.3", "7.0.4"]`:
- All four prerequisite tasks must have `status: "done"`
- Only then can `7.1.1` start

### Parallel Execution

Ask for the conflict-free set, do not eyeball it:

```bash
tidetable ready docs/todo-v7.json --disjoint --json
```

- `ready` is everything runnable; `--disjoint` is everything runnable that does
  not collide on `touches` paths, including against tasks already in progress
- Paths collide when equal, or when one is a directory containing the other
- Skipped tasks come back under `deferred` with what they collided with
- Each worker still calls `start --owner <name>` before touching code
- `parallel_group` is a reading aid, not an input to any of this

## Writing Tasks

### Adding a New Task

1. Find the phase it belongs to
2. Determine the next sequence number (max existing + 1)
3. Set the `id` as `PHASE.SEQUENCE`
4. Set `status: "pending"`
5. List any `prerequisites` (other task IDs that must complete first)
6. Assign to a `parallel_group` if it can run concurrently with others
7. Set `complexity` and `agent` tier

### Adding a New Phase

New phases start empty — create the phase, then `add` tasks into it:

```bash
tidetable add-phase docs/todo-v7.json --phase 7.3 --title "Hardening" \
  --goal "Polish, docs, release prep." --prereq 7.2 --after 7.2
```

- New phase IDs must be `MAJOR.MINOR` and unused; self-require, duplicate,
  dangling `--prereq`, and unknown `--after`/`--before` anchors are rejected
  with nothing half-written
- `--prereq` takes phase IDs (repeatable, all must exist); `--after` /
  `--before` pins the position (mutually exclusive, default: append at end)
- The phase prerequisite gates immediately — tasks added into the new phase
  wait on it through the normal `blockers_for` path, no special handling
- Over HTTP: `POST /api/<slug>/add-phase` with `phase`, `title`, `goal`,
  `prerequisites`, `after` / `before`

### Claiming and Marking Tasks Done

1. `start` the task first (`pending` → `in_progress`) so no other agent claims it
2. On completion set `status: "done"` and update `updated_at` (`...Z` format)
3. Check if this unblocks any other tasks (their prereqs are now all met)
4. Report unblocked tasks

Use `tidetable start` / `tidetable done` rather than editing the file
directly: they take a lock, write atomically, refuse unmet prerequisites without
`--force`, and file the task's `--changelog` line under the phase release.

`start` also records a claim — `claimed_by`, `claimed_at`, `lease_expires_at` —
and that claim is enforced:

```bash
TIDETABLE_OWNER=worker-3 tidetable start docs/todo-v7.json 7.0.1
```

- Another agent's `start` on the same task fails while the lease holds
- `next` skips tasks another agent holds, and resumes only yours or a lapsed one
- `done` on someone else's live claim needs `--force`
- A crashed agent's task frees itself when the lease expires; `reset` frees it
  immediately

Every transition is appended to `<file>.events.jsonl`; read it with
`tidetable history`.

### Moving Tasks

If a task needs to move to a different phase or version:
1. Set `status: "moved"` on the original task
2. Set `moved_to: "new.task.id"` pointing to the new location
3. Create the new task in the target location
4. Preserve history — the old ID still exists

## Version Mapping

Task IDs are planning references, not release versions:

```
Planning milestone (roadmap)     →    Release version (semver)
─────────────────────────────         ──────────────────────────
todo-v7.md (internal roadmap)    →    v0.7.0 (git tag)
todo-v8.md                       →    v0.8.0
```

The `release` object on the final phase of a milestone describes the tag:

```json
{
  "id": "7.1",
  "release": {
    "version": "0.7.0",
    "tag_on_complete": true
  }
}
```

Nothing tags automatically. Finishing the phase's last task makes `done --json`
report `"release_ready": true`; creating the tag is an explicit step:

```bash
tidetable release docs/todo-v7.json 7.1        # what would be tagged
tidetable release docs/todo-v7.json 7.1 --tag  # create it
```

## Pitfalls

- **Task IDs are stable** — never renumber. Insert new tasks with new IDs.
- **Prerequisites are IDs, not phases** — `prerequisites: ["6.0.3"]` not `prerequisites: ["6.0"]`
- **Circular dependencies are invalid** — validate before committing
- **Parallel groups mean nothing to the scheduler** — they neither order work nor make it safe to run together. `prerequisites` orders; `touches` + `ready --disjoint` decides what runs together
- **Never resume an `in_progress` task whose lease is still live** — someone else is on it
- **`status: "moved"` preserves history** — don't delete tasks, move them
- **Phase prerequisites gate too** — a task with no prereqs of its own is still blocked if its phase requires an incomplete phase
- **Only `done` satisfies a prerequisite** — a `cancelled` or `moved` prerequisite blocks its dependents forever; `validate` warns about it
- **Don't hand-edit while agents are running** — the CLI locks the file; a raw write loses updates

## CLI Usage

The todo file argument is optional: omitted, the CLI uses the single `todo*.json`
found in `.`, then `docs/`, then `.tidetable/` (first directory with any wins).
Two or more in that directory is an error; none found prompts on a terminal and
errors otherwise. Not defaulted with `--server`.

```bash
# Validate: JSON Schema plus semantic checks (dangling prereqs, cycles, dupes)
tidetable validate docs/todo-v7.json

# Everything runnable right now — fan these out across parallel agents
tidetable ready docs/todo-v7.json --json

# Only what is safe to dispatch together (no overlapping owned paths)
tidetable ready docs/todo-v7.json --disjoint --json

# One task to pick up (resumes your own in_progress task, never someone else's)
tidetable next docs/todo-v7.json --owner worker-3 --json

# Claim, finish, or recover a task
tidetable start docs/todo-v7.json 7.0.1 --owner worker-3
tidetable done docs/todo-v7.json 7.0.1 --changelog "Added the Go SDK"
tidetable reset docs/todo-v7.json 7.0.1

# Release for a finished phase: report, then tag
tidetable release docs/todo-v7.json 7.1
tidetable release docs/todo-v7.json 7.1 --tag

# Everything that ever happened to a task
tidetable history docs/todo-v7.json --task 7.0.1

# Retire a task without deleting it
tidetable status docs/todo-v7.json 7.0.5 cancelled
tidetable status docs/todo-v7.json 7.0.6 moved --moved-to 8.0.1

# List all / parallel groups / one dependency chain
tidetable list docs/todo-v7.json
tidetable parallel docs/todo-v7.json
tidetable deps docs/todo-v7.json 7.0.1

# Add a task (new ID is the phase's max sequence + 1)
tidetable add docs/todo-v7.json --phase 7.2 --title "New feature"   --goal "..." --prereq 7.0.1 --complexity High --agent pro_dev_agent

# Edit a task's plan fields (status is separate: start/done/status). Lists are replaced whole;
# --clear-* empties one; dangling or cyclic prerequisites are refused.
tidetable edit docs/todo-v7.json 7.2.1 --title "Clearer title" --touches tui/src/   --success "it works" --tests "cargo test"
tidetable edit docs/todo-v7.json 7.2.1 --clear-parallel-group

# Add a phase (new ID must be MAJOR.MINOR and unused; starts empty)
tidetable add-phase docs/todo-v7.json --phase 7.3 --title "Hardening"   --goal "..." --prereq 7.2 --after 7.2

# Render for human review (one-way)
tidetable convert docs/todo-v7.json -o docs/todo-v7.md

# Single-writer core API (VPS hub): same DAG logic over HTTP, never a second implementation
tidetable serve --port 8471 --registry deploy/registry.json
# GET /api/<slug>/ready|next|list|deps|history|validate|parallel, POST .../start|done|reset|status|add|add-phase|edit|look-defer|look-clear|heartbeat, GET /api/stream (SSE)

# Harness sidebars: budgeted current/concurrent/upcoming/phase-tree/overall payload
tidetable sidebar docs/todo-v7.json --width 80 --height 40 --json
# Adapters under integrations/ shell out to it: opencode (TUI sidebar_content),
# pi (extension panel, also loads under oh-my-pi omp), openchamber (rail panel
# over the serve API), paseo (sidebar item + workspace panel via server RPC)

# TKRun: the unattended runner, on a throwaway worktree branch (`overnight` is a kept subcommand alias)
tidetable tkrun init docs/todo-v7.json              # writes .tidetable/tkrun.json
tidetable tkrun run  docs/todo-v7.json --dry-run    # preview: routing, gate, command, prompt
tidetable tkrun run  docs/todo-v7.json --hours 8    # run until done or out of time
tidetable tkrun run  docs/todo-v7.json --jobs 3     # up to 3 tasks at once, own worktree each
tidetable tkrun run  docs/todo-v7.json --task 7.0.1 # only the named task(s), repeatable
tidetable tkrun run  docs/todo-v7.json --follow     # echo agent output live (default on a tty)
tidetable tkrun run  docs/todo-v7.json --adjudicate # gray-zone commands go to the decision model
tidetable tkrun stop docs/todo-v7.json              # cooperative, after the current task
tidetable tkrun kill docs/todo-v7.json              # hard stop: sentinel + process-tree kill
tidetable tkrun adjudicate docs/todo-v7.json "rm -rf /"  # judge one command, no run

# Which provider/model runs each tier, and where each setting came from
tidetable agents show --todo docs/todo-v7.json
tidetable agents providers                  # presets: anthropic, opencode-go
tidetable agents use opencode-go            # switch every tier at once
tidetable agents set pro_dev_agent --model claude-opus-5 --scope repo
```

## Terminal UI

`tidetable tui [--mode auto|standard|side|bottom] [--tmux] [-- TUI_ARGS]` launches the
`tidetable-tui` binary (build: `cargo build --release --manifest-path tui/Cargo.toml`). It
shows Now / Upcoming / Done / Review for a todo file or hub project (`--server URL SLUG`,
token from `$TIDETABLE_TOKEN`). Humans use it; agents keep using the CLI. It never writes a
todo file: `s` start, `d` done, `l` defer, `c` clear and `x` reject all run the CLI (`--read-only`
disables them). Details: `docs/integration-guide.md`.

## Attended and unattended runs

TideTable has two run modes, and they differ only in who approves a shell
command. The mode is the **permission model**; the commands are the same CLI.

- **Attended**: you drive the session. Your own opencode/claude permissions
  apply, a person sees and approves commands, and the base branch is yours to
  touch. This is the normal `tidetable start|done|...` use and the `serve` hub.
- **Unattended**: **TKRun** (`tidetable tkrun run`) works through a todo file on
  a throwaway worktree branch. No human is watching, so the permission model is
  stricter and the runner — not the agent — owns git history and TideTable state.

A run is unattended by construction: `tkrun run` builds the agent's environment
and permission lists itself. There is no flag that turns an attended session
unattended; `tkrun` is the unattended entry point.

### How an unattended command is approved

Today TKRun decides every command with static lists, passed to `claude -p` or
written into a temporary `opencode.json`:

- `BASE_SHELL_ALLOW` — read-only inspection (`git status|diff|log|show`, `ls`),
  the safe default for any repo; a config adds to it.
- `BASE_SHELL_DENY` — what must never happen unattended: `git push`/`tag`, local
  git history rewrites, TideTable state writes (`start|done|reset|status`), and
  commands that dump the environment or `.env` files.
- `OPENCODE_SHELL_DENY` — the smaller list opencode 2.x can live with (one
  rejected command removes its shell tool), so only the must-never commands.

An **adjudicator** handles the gray zone — a command on neither list. With
`adjudicator.enabled`, the runner starts a small decision service on loopback and
the agent's hook asks it before running a bash/shell command. The verdict comes
from `clef` on a Jev endpoint (`POST /v1/systemone`, a `choice` question returning
`allow`/`deny` plus a confidence). It **fails closed**: a missing endpoint, key or
model, a network error, a low confidence, or an unexpected verdict all mean deny.

- The agent never sees the credentials: it gets `TIDETABLE_ADJUDICATOR_HOST` /
  `_PORT` (or the `.tkrun/adjudicator.json` discovery file) and asks the
  service. Credentials are env-only (`TIDETABLE_DECISION_ENDPOINT`,
  `TIDETABLE_DECISION_API_KEY`, `TIDETABLE_DECISION_MODEL`), never in the config.
- Only opencode is wired for the runtime hook today: the generated `opencode.json`
  registers `integrations/opencode/tk-adjudicator.ts`, which throws on a non-allow
  answer in the `execute.before` hook (aborting the tool). claude still falls back
  to its static `--allowedTools`/`--disallowedTools`.
- Every verdict is appended to `<run dir>/adjudicator.jsonl`. Try one without a
  run: `tidetable tkrun adjudicate docs/todo.json "rm -rf /"`.

Until it is on, an unlisted command is simply not allowed: the static lists are
the whole model.

The prompt also forbids printing env vars, `.env` files and credentials, and
`SECRET_DENY` blocks `env`, `printenv`, `set`, `git config --list`, `cat *.env*`
and the PowerShell `env:` drive. The runner's own `TIDETABLE_TOKEN`,
`TIDETABLE_SERVER`, `TASKERKEEPER_TOKEN` and `TK_API_TOKEN` are stripped from the
agent environment, and `agent_env_drop` hides more.

## Parallel jobs

`tkrun run --jobs N` (or `parallel.jobs` in the config) runs up to N tasks at
once. Each job gets its own worktree and branch off the run branch and is
merged back `--no-ff` as it finishes, one at a time.

- A task runs beside others only if it declares `touches` and none of its paths
  overlaps a running task's. A task with no `touches` is exclusive: it waits for
  the running jobs to drain and nothing starts behind it.
- `--task ID` (repeatable) narrows the run to named tasks; it composes with
  `--jobs`. The run ends when none of the named tasks is left.
- A merge conflict leaves the job's branch and worktree in place, reopens the
  task. An exception keeps the worktree too (`KEEP <id>`); a failed *gate* does
  not.
- Do **not** share one `CARGO_TARGET_DIR` between worktrees of a crate. The
  default, a `target/` per worktree, is correct; a shared one gives an agent
  another worktree's artifacts and compile errors. `parallel.job_env` is for
  variables genuinely safe to share.
- Not combinable with `siblings` yet. Todo state is written only in the run
  worktree, so jobs never conflict on it.

## Choosing the Model for a Task

A task names an agent tier; the tier resolves to a provider and model through
layered config — the built-in `anthropic` preset, then the user config
(`~/.config/tidetable/agents.json`), then the repo config
(`<repo>/.tidetable/agents.json`), then the todo file's `agent_config`, and
finally a task's own `provider`/`model`. Last wins.

Each of those config layers can name a provider preset (a whole tier table —
`anthropic` or `opencode-go`) as well as individual tiers. The preset applies
first, so a hand-set tier in the same scope still wins.

`ready --json` and `next --json` carry the resolved values on every task, so a
supervisor dispatches in one lookup:

```json
{ "id": "7.0.1", "agent": "mid_dev_agent",
  "provider": "anthropic", "model": "claude-sonnet-5" }
```

Prefer moving a whole tier over pinning individual tasks — a per-task `model`
is for the genuine exception. TideTable only resolves the strings; it never
calls a provider itself.

Every read command accepts `--json`. Parse that, not the box-drawing output.

## Ralph Integration

`ralph/ralph-json.sh` forwards every argument to the CLI, so ralph configs that
call a shell script keep working. No `jq` required.

```bash
./ralph/ralph-json.sh ready docs/todo-v7.json --json
./ralph/ralph-json.sh start docs/todo-v7.json 7.0.1
./ralph/ralph-json.sh done docs/todo-v7.json 7.0.1
```

## See Also

- `ralph-todo-format` skill — markdown todo format (legacy)
- `development-workflows` skill — broader dev workflow context
- Schema: `tidetable/schema/todo-v1.schema.json` (ships with the package)
