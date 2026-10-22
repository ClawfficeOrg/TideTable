# Rust port: the contract tkrun depends on

**Status:** plan, nothing ported yet.
**Goal:** move the TideTable core (todo model, scheduling, claims, validation)
into Rust so `tidetable-tui` and embedders like Oarlock stop needing a Python
install, without changing what TKRun sees.

`tkrun.py` stays Python. It is unstable by design, and Python's edit-run cycle
is an asset there, not a cost. This document exists so that decision stays
cheap: the port is allowed to replace everything under TKRun, and is not
allowed to change anything TKRun can observe.

---

## 1. Why the split falls where it does

`tkrun.py` is 2,087 lines and has exactly one dependency on core: a single
method that shells out.

```python
def tk(self, *args: str) -> Proc:
    env = {"TIDETABLE_OWNER": self.owner, "TASKERKEEPER_OWNER": self.owner}
    head: list[str] = []
    if self.hub:
        env["TIDETABLE_SERVER"] = self.hub["server"]
        env["TIDETABLE_TOKEN"] = self.hub.get("token", "")
    else:
        head = ["--local"]    # a $TIDETABLE_SERVER in the environment must not hijack a path
    return run_proc([sys.executable, "-m", "tidetable", *head, *args], self.root, env=env)
```

Eleven call sites, all through that method, all in the same shape
(`<command> <todo_or_slug> <args> --json?`). Nothing else in `tkrun.py`
imports core logic; it imports `jsonio._pid_alive` and three constants from
`runs.py` (`RUN_DIR_NAME`, `LEGACY_RUN_DIR_NAME`, `control_dir`), and the rest
is `subprocess`, `git`, `ctypes` and threads.

That means the port boundary is a process boundary that already exists. When a
Rust binary lands, `tk()` changes one line:

```python
return run_proc([self.tt_exe, *head, *args], self.root, env=env)
```

`self.tt_exe` is the binary path resolved once at startup, not a bare name from
`PATH`. That matters even though the binary is called `tidetable`: the current
line is `[sys.executable, "-m", "tidetable", ...]`, which runs the *Python
module* and would keep doing so no matter what `tidetable.exe` sits on `PATH`.
The switch is therefore explicit in `tk()`, and rolling back is putting the
`sys.executable` form back.

Everything in this document is the set of facts that makes that line safe.

### What is not ported

| Module | Lines | Why it stays |
| --- | --- | --- |
| `tkrun.py` | 2,087 | Unstable; Windows process-tree kills; lane windows; quick fixes matter |
| `tui.py` | 81 | Already a launcher shim that finds and execs `tidetable-tui` |
| `integrations.py` | 249 | Writes each harness's own config file; no core logic |
| `claude_hook.py` | 94 | Harness hook, spawned per tool call |
| `integrations/**/*.ts` | ~1,000 | Harness plugins must be JS/TS |
| `web/` | ~1,000 | Svelte dashboard |

---

## 2. The six commands TKRun calls

Argument order matters and is not uniform. Reproduced exactly as
`tkrun.py` builds it.

| # | Call | Args after the method's `head` | Reads |
| --- | --- | --- | --- |
| 1 | `ready <todo> --json` | only the todo argument, no owner | `.ready[]`, filters `status == "pending"` |
| 2 | `list <todo> --json` | only the todo argument | `.phases[]` for phase review gate |
| 3 | `start <todo> <id> --owner <owner> --lease <minutes>` | no `--json` | exit code and stdout text |
| 4 | `done <todo> <id> --owner <owner> --changelog <line>` | no `--json` | exit code and stdout text |
| 5 | `look-defer <todo> <id> --owner <owner> --note <text>` | no `--json` | exit code only |
| 6 | `reset <todo> <id> --owner <owner>` | no `--json` | exit code and stdout text |

`<todo>` is a filesystem path in local mode; in hub mode it is a project slug
and `--local` is absent, so `TIDETABLE_SERVER` and `TIDETABLE_TOKEN` route the
call over HTTP. A Rust core must support both addressing modes, or hub mode
stops working. Slice 3 covers this.

Calls 3 to 6 are read for their **exit code and their stdout text**, because
`run_proc` merges stderr into the captured stream and `tkrun` logs
`f"{r.out}{r.err}"` into `.tkrun/log.md`. A port that moves refusal messages to
stderr, or changes their wording, produces log lines that no longer match the
history in every existing repo.

---

## 3. JSON contracts

Shapes below were captured from the running CLI against `docs/todo.json`. They
are the parity target: a Rust implementation must emit the same keys with the
same types, in the same nesting. Key order is not part of the contract
(`json.dumps(indent=2)` order is incidental), and no consumer indexes by
position.

### 3.1 `ready --json`

Top level, from `cmd_ready`:

```json
{
  "ready": [ ...task_summary... ],
  "disjoint": false,
  "deferred": [ { "id": "4.0.2", "title": "...", "conflicts_with": ["a.rs"] } ],
  "conflicts": [ { "a": "4.0.1", "b": "4.0.2", "paths": ["src/"] } ],
  "in_progress": [ ...task_summary... ],
  "blocked": [ { "id": "4.0.4", "title": "...", "blocked_by": ["task 4.0.3"] } ]
}
```

**Exit code is `0` when `ready` or `in_progress` is non-empty, and `1` when both
are empty.** This is not an error condition; it means "nothing runnable". Any
port that exits `1` on success, or `0` on an empty queue, changes TKRun's
termination behaviour.

`task_summary` is defined in `cli.py` and is the single most important shape in
this document. Its keys are, in this order:

```
id, title, status, goal, touches, success, tests,
prerequisites, parallel_group, complexity, agent
```

only those present on the task, then the claim fields from `claim_summary`
(`claimed_by`, `claimed_at`, `lease_expires_at`, `lease_expired`) and then the
resolved agent fields from `agents.resolve_task` (`provider`, `model`,
`agent`, `agent_derived`).

Observed on a real task (optional keys `prerequisites` and `parallel_group`
absent from the source file, so absent here):

```json
{
  "id": "4.0.1",
  "title": "Hub routes for look-defer and look-clear",
  "status": "pending",
  "goal": "Add POST /api/<slug>/look-defer ...",
  "touches": ["tidetable/serve.py", "tidetable/remote.py", "tests/"],
  "success": ["look-defer, look-clear and look-clear --reject over HTTP ..."],
  "tests": "python -m pytest tests/test_cli.py tests/test_serve.py -q",
  "complexity": "Medium",
  "provider": "anthropic",
  "model": "claude-sonnet-5",
  "agent": "mid_dev_agent",
  "agent_derived": true
}
```

Note that `agent` is **overwritten** by the resolved tier, and
`agent_derived: true` records that no tier was named on the task. A port that
emits the task's own `agent` field here, or omits `agent_derived`, silently
changes which model TKRun dispatches.

`--disjoint` additionally filters `ready` and sets `disjoint: true`; `deferred`
then lists what was held back and why.

### 3.2 `list --json`

```json
{
  "phases": [
    { "id": "1.0", "title": "Foundation", "complete": true,
      "tasks": [ { "id": "1.0.1", "title": "...", "status": "done" } ] }
  ],
  "summary": { "pending": 4, "in_progress": 0, "done": 27,
               "needs_human_look": 1, "cancelled": 0, "moved": 0 }
}
```

Two shapes worth pinning: tasks here are **trimmed to three keys** (not
`task_summary`), and `summary` always contains **all six** statuses including
zeroes, in `VALID_STATUSES` order.

`complete` is `phase_is_complete`, which treats `done`, `needs_human_look`,
`cancelled` and `moved` as terminal. A phase with no tasks is complete.

The phase-review gate in TKRun reads `phases[]` and picks the entry containing
the finished task, so `id`, `complete` and the nested `tasks[].id` are the parts
that matter; `title` is used as the review prompt's fallback goal.

### 3.3 `start`, with `--json`

```json
{
  "task": "4.0.1",
  "owner": "t@h",
  "lease_expires_at": "2026-10-22T06:04:35Z",
  "refreshed": false,
  "stolen_from": null
}
```

TKRun does not pass `--json` to `start`, but the shape is documented here
because it is the cleanest statement of the claim semantics and a future
embedder will want it.

### 3.4 `done`, with `--json`

```json
{
  "task": "4.0.1",
  "unblocked": [ { "id": "4.0.2", "title": "TUI defer, clear and reject over the hub" } ],
  "changelog_filed_under": null,
  "phase": "4.0",
  "phase_complete": false,
  "release_ready": false,
  "release": null
}
```

`unblocked` entries carry only `id` and `title`. `changelog_filed_under` is the
phase id whose `release.changelog_entries` received the changelog line, or
`null` when no phase in the file has a `release` object.

`release` is `release_state(phase)` when the phase just completed, else `null`.

### 3.5 `look-queue --json`

```json
{
  "queue": [
    { "id": "4.0.3", "title": "Project slug resolution", "phase": "4.0",
      "note": "n", "goal": "...", "success": ["..."] }
  ]
}
```

Note `goal` and `success` are included but `touches` is not, unlike
`task_summary`.

### 3.6 Other `--json` commands, for completeness

Not called by TKRun, needed by the TUI and the sidebar integrations.

`next --json` — exit `1` and `{"task": null, "blocked": [...]}` when nothing is
runnable, with `blocked` listing every pending task and its reasons (observed
`[]` for an all-done file, and one entry for a task whose prerequisite is
`cancelled`). Otherwise exit `0` and `{"task": {...}, "resumed": bool}`, where
`task` is a `task_summary` plus resolved provider/model/tier and `resumed` is
`true` when an existing claim is being handed back. `blocked` is absent on the
success path.

`deps --json`:

```json
{ "id": "4.0.1", "title": "...", "status": "pending", "phase": "4.0",
  "prerequisites": [], "phase_prerequisites": [],
  "unblocks": [ { "id": "4.0.2", "title": "...", "status": "pending" } ],
  "blocked_by": [] }
```

`parallel --json` — `{ "<group>": [ task_summary + "ready": bool ] }`, and `{}`
when no task names a `parallel_group`.

`sidebar --json` — `{"current", "concurrent", "upcoming", "phase", "overall",
"run"}`; the budget algorithm lives in `sidebar.py` and is already pure.

`history --json` — `{"path": "todo.json.events.jsonl", "events": [...]}`.

`agents show --json` / `agents providers --json` — resolved tiers with
`_sources` provenance, and the provider preset table. These take `--todo`, not
a positional path.

`release --json` — `release_state` of the releasing phase, or exit `1` with
`Error: no phase in this file has a release object`.

---

## 4. Behavioural contract

These are the rules a port must reproduce even though no JSON shape carries
them.

### Exit codes

| Situation | Code |
| --- | --- |
| `ready --json` with nothing ready and nothing in progress | `1` |
| `ready --json` with anything listed | `0` |
| `next --json` with nothing runnable | `1`, `task: null` plus `blocked` |
| `next --json` with a task | `0` |
| `done` on a task already `done` | `0` (idempotent, prints "already done") |
| `done` on a task with unmet prerequisites, no `--force` | `1` |
| `done` on a live claim held by someone else, no `--force` | `1` |
| `start` on a task already `done` | `1` |
| `start` with a live lease held by another owner | `1` |
| `start` re-run by the same owner while in progress | `0`, `refreshed: true` |
| `start` on a task whose prerequisites are unmet, no `--force` | `1`, `blocked by:` wording |
| `look-defer` on an `in_progress` task | `0`, releases the claim || `start` with unmet prerequisites, no `--force` | `1` |
| `look-clear` on a task not in `needs_human_look` | `1` |
| unknown task id | `1` |

`start` re-run by the same owner must be `0`, because a supervisor restarting
mid-task calls it again. `done` on an already-done task must be `0` for the same
reason. Both are load-bearing.

### `ready` exits 1 when nothing is runnable

Verified: an all-done file gives `{"ready": [], ...}` and exit `1`. `ready`
returns `0 if listed or running else 1`, so exit 1 means "no work", not
"broken". Any supervisor that treats non-zero as fatal will stop working; TKRun
does not, because it reads the JSON.

### Refusals go to stdout

Every refusal in `cli.py` is a `print()`. `run_proc` merges the streams, but a
port that writes refusals to stderr and keeps stdout clean for machines will
change the text TKRun logs. Keep the wording:

```
Error: task 4.0.3 is held by a@b until 2026-10-22T05:42:14Z.
Wait for the lease to lapse, or `reset` it if that agent is gone.
```

```
Error: task 4.0.1 is blocked by: task 4.0.2
Finish those first, or pass --force to override.
```

```
Error: task 4.0.4 has unmet prerequisites: task 4.0.3
Pass --force to mark it done anyway.
```

Note `start` says `blocked by:` while `done` says `has unmet prerequisites:`.
Both texts are in the log history, so the asymmetry has to be preserved.

```
Task 4.0.1 is already done.
```

The success line for `look-defer` is `◐ Task 4.0.3 queued for a human look
(was pending).` — TKRun does not parse it, but `.tkrun/log.md` history in every
existing repo contains it. The parenthetical reports the status the task had
immediately before, so it reads `(was in_progress)` when the task was claimed.

### `--local` beats `$TIDETABLE_SERVER`; `--server` beats both

Mode is decided by `remote.active()`: hub when a server URL is known, `--local`
is absent, and the command has a route in `remote.ROUTES`.

Precedence, highest first:

1. `--server URL` — forces hub mode with no environment involved.
2. `--local` — forces local mode and ignores `$TIDETABLE_SERVER`.
3. `$TIDETABLE_SERVER` — hub when set.
4. nothing — local file.

So `--server` and `--local` are a complete pair; no third "force hub" flag is
needed, because `--server` already supplies the URL explicitly. What the port
must not break is the two consequences TKRun relies on:

- `--local` wins over an exported `$TIDETABLE_SERVER`, which is why `tk()`
  passes it for a path argument.
- Commands absent from `ROUTES` are local-only. `LOCAL_ONLY` is
  `{"convert", "release"}`, and hitting one with a server configured must
  refuse with `` `convert` has no remote mode; run it against a local checkout
  (or pass --local). `` rather than silently falling back.

### Environment

Both prefixes are read, new one first:

```
TIDETABLE_OWNER      / TASKERKEEPER_OWNER
TIDETABLE_LEASE_MINUTES / TASKERKEEPER_LEASE_MINUTES
TIDETABLE_SERVER     / TASKERKEEPER_SERVER
TIDETABLE_TOKEN      / TASKERKEEPER_TOKEN
```

Default owner is `<hostname>:<pid>`. Default lease is 60 minutes.

### Todo file discovery

When the path is omitted, search `.`, then `docs`, then `.tidetable` for
`todo*.json`; the first directory that has any wins, and its matches are sorted
by name. Directories are not merged.

Note the argument quirk on the mutation commands: usage is
`[todo_file] task_id`, so `tidetable start 4.0.1` means the task, while
`tidetable start docs/todo.json 4.0.1` means both. Ambiguity is resolved by
whether the first argument matches an existing file.

---

## 5. Storage semantics

### The file lock

`jsonio.FileLock` is the part most likely to be got wrong in a rewrite, because
its behaviour is a set of Windows-specific decisions.

- Lock path is `<todo>.lock`, created with `O_CREAT|O_EXCL|O_WRONLY`.
- Contents are JSON: `{"pid", "host", "at"}`. Bare-pid contents from pre-0.5
  locks are still understood.
- A lock is **stale** when the recorded pid is positively dead on the same host,
  or when the file is older than `DEFAULT_STALE_AFTER` (300s) and the pid could
  not be checked. A pid on a different host proves nothing.
- Acquiring breaks a stale lock and records the reason in
  `broke_stale_lock`, which callers print as
  `! Broke a stale lock on todo.json.lock: holder pid 57584 is gone`.
- Default timeout is 10s, polling every 50ms, then `LockTimeout`.

**Do not implement the liveness probe with `os.kill(pid, 0)` on Windows.**
CPython maps that to `TerminateProcess`, so the POSIX "signal 0 just probes"
idiom kills the process. The existing code uses `OpenProcess` with
`PROCESS_QUERY_LIMITED_INFORMATION` and `GetExitCodeProcess`, returning
`True`/`False`/`None` for alive/dead/unknown. A Rust port needs the equivalent
via the `windows-sys` crate, and must preserve the three-valued result: "could
not tell" falls through to the age-based rule, it does not mean "dead".

### Atomic writes

`write_json` writes a temp file in the same directory and replaces the target,
retrying up to 8 times with 50ms delay, because Windows refuses a rename while
another process holds the target open. A port that does a bare
`fs::rename` will produce intermittent failures under TKRun's parallel jobs.

### Event log

Every mutation appends one line to `<todo>.events.jsonl` via `append_event`:
`{"at", "event", ...fields}`, with `None` values dropped. The log is
append-only and never rewritten. `history` replays it.

---

## 6. Schema

`tidetable/schema/todo-v1.schema.json` is the format definition and must be
reused verbatim, not re-specified in Rust. Top level: `schema_version`,
`project`, `phases`, `agent_config`, `meta`. `$defs` are `phase`, `task`,
`agent_config`.

Task requires `id`, `title`, `status`. `id` matches `^[0-9]+\.[0-9]+\.[0-9]+$`.
Statuses are `pending`, `in_progress`, `done`, `needs_human_look`, `cancelled`,
`moved`. Phase ids match `^[0-9]+\.[0-9]+$` and require `id`, `title`, `goal`,
`tasks`.

Schema validation currently needs `jsonschema` and degrades with a warning when
it is missing. In Rust the crate dependency removes that failure mode, which is
an improvement worth taking, but `validate` must keep printing the same two
lines on success:

```
✓ Schema: todo.json matches todo-v1.schema.json
✓ Semantics: ids unique, prerequisites resolve, no cycles
```

---

## 7. Slices

**Slice 1 — core and the read path.** New crate `tidetable-core`, no I/O in the
model: load, schema validate, `iter_tasks`, `id_key`, `blockers_for`,
`ready_tasks`, `in_progress_tasks`, `find_next`, `blocked_report`,
`phase_is_complete`, `complete_phase_ids`, `paths_conflict`,
`overlapping_paths`, `disjoint_tasks`, `semantic_errors`, `find_cycles`,
`release_state`, `collect_changelog`. Commands: `validate`, `next`, `ready`,
`list`, `status` (read form), `deps`, `parallel`, `history`, `convert`,
`sidebar`. Then `tui/src/source.rs` calls the library directly and the TUI stops
needing Python.

**Slice 2 — mutations.** `start`, `done`, `look-defer`, `look-queue`,
`look-clear`, `reset`, `status`, `add`, `add-phase`, `release`. This is the
slice that makes `tk()` work: all six calls TKRun makes are covered. Port the
lock and atomic-write semantics here, with the Windows probe done properly.

**Slice 3 — `serve`.** axum on the same core. Route table and token scoping from
`serve.py`/`sessions.py`/`projector.py`. Only after Slice 2, so hub mode has
parity to test against.

**Slice 4 — nothing.** `tkrun.py` stays. Revisit only if the CLI surface stops
changing for a long stretch, which the history in this repo suggests it will
not.

---

## 8. Parity testing

The port is only safe if a test can prove it. The method:

1. **Golden fixtures.** For each command, capture `(argv, todo file, env)` and
   the Python CLI's `(stdout, exit code)`. Store both in `tests/parity/`.
2. **Run both.** A harness executes the Python CLI and the Rust binary against
   a temp copy of the same fixture and compares output according to the tiers in
   §10: byte-identical for JSON, refusal text and exit codes; structural for
   `sidebar` and `convert` text.
3. **Seed the todo file deliberately.** One fixture per interesting state:
   empty, all done, one ready, blocked by task, blocked by phase, in progress
   held by another owner, lease expired, needs_human_look queued,
   cancelled prerequisite, cycle, duplicate id, missing required field.
4. **Cover the refusals.** The exit-1 paths carry the wording TKRun logs, so
   they need fixtures as much as the happy paths.
5. **Concurrency fixture.** Two `start` calls racing the same task; exactly one
   wins, the other exits 1. This is the lock test and it must be run on Windows.

Until step 5 passes on Windows, `tk()` must not be switched.

---

## 9. Issues found while measuring

Recorded here so the port does not inherit them silently.

**Lock teardown race.** Two processes acquiring the same lock can hit
`PermissionError: [WinError 32]` in `FileLock.__exit__`, where `self.path.unlink()`
fails because the other process still has the lock file open for reading. It
surfaced as an uncaught traceback out of `cmd_status`, not a clean refusal. It
reproduced once under deliberate racing and not on retry. Either wrap that
unlink, or treat "already gone" and "still open" the same way; the Rust
implementation should not copy the unguarded unlink.

**`agents show` / `agents providers` take `--todo`, not a positional path.**
Every other command takes a positional. Easy to trip over in a harness; worth
keeping identical rather than "fixing" during the port, because
`AGENTS.md` documents the current form.

---

## 10. Decisions and what parity applies to

### The binary is named `tidetable`

Same name as the Python console script, so a Rust build can replace it without
every script, hook and `AGENTS.md` in the suite changing. The rollback is
`pip install --force-reinstall` of the wheel, which is cheap because the Python
package stays intact and published during the port — this is a reason to keep
the two implementations side by side until Slice 2 is green, not to merge them.

Because the name is shared, a wrong `tidetable` on `PATH` is indistinguishable
from a right one. The Rust binary should therefore answer `--version` with
something that identifies it (for example `tidetable 0.13.0 (rust)`), so a
support report or a `--dry-run` log says which implementation ran.

### Byte parity: only where a machine or the log reads it

Three tiers, and the port should not spend effort above where it is needed.

| Output | Requirement |
| --- | --- |
| `--json` from every command in §3 | exact keys, types, nesting; key order free |
| Refusal and success text read by TKRun (`start`, `done`, `reset`, `look-defer`) | byte-identical, because it lands in `.tkrun/log.md` |
| `ready`/`next` exit codes | exact |
| `sidebar --json` | exact payload; this is the only thing harnesses consume |
| `sidebar` human text | structural only |
| `convert` markdown | structural only |

The reason `sidebar` text needs no byte parity: every harness plugin calls
`sidebar <todo> --json --width <w> --height <h>` and renders the text itself
(`integrations/opencode/tk-sidebar.ts` has its own `renderText`). The Python
`render_sidebar` is used only when a human runs the command in a terminal, so a
Rust version that produces a different arrangement of the same sections is not
a compatibility break. It should still keep the section set and the width
budget, since those are what the tests in `tests/test_sidebar.py` assert.

`convert` is the same story from the other direction: it writes a markdown file
for human review, referenced in `docs/integration-guide.md` and
`docs/philosophy.md`, and no code reads its output. Section headings and the
per-phase/per-task structure should match so generated docs do not churn
needlessly in diffs, but nothing should assert byte equality.

### Already settled

- **No `--hub` / `--remote` flag.** `--server URL` forces hub mode explicitly;
  `--local` forces local. The pair in §4 is complete.
