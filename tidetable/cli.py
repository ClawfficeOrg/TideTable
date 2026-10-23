#!/usr/bin/env python3
"""
tidetable — CLI for managing TideTable JSON todo files.

Usage:
    tidetable validate <todo.json>            Schema + semantic validation
    tidetable next <todo.json>                Next task to work on
    tidetable ready <todo.json>               Every currently-runnable task
    tidetable start <todo.json> <task_id>     Claim a task (-> in_progress)
    tidetable done <todo.json> <task_id>      Mark a task done
    tidetable reset <todo.json> <task_id>     in_progress -> pending
    tidetable status <todo.json> <task_id> <status>
                                                 Set any status (cancelled/moved/...)
    tidetable list <todo.json>                Status overview
    tidetable parallel <todo.json>            Parallel groups
    tidetable deps <todo.json> <task_id>      Dependency chain
    tidetable add <todo.json> --phase <id> --title <t>
                                                 Append a task to a phase
    tidetable add-phase <todo.json> --phase <id> --title <t>
                                                 Insert a new phase
    tidetable release <todo.json> [phase]     Show/cut the tag a phase ships as
    tidetable history <todo.json>             Replay the event log
    tidetable convert <todo.json>             Render JSON as markdown

Every read command accepts --json for machine-readable output.

Claiming: `start` records an owner and a lease, so `next` never hands a live
task to a second agent. `TIDETABLE_OWNER` names the agent;
`TIDETABLE_LEASE_MINUTES` sets how long a claim holds.

Requirements: pip install jsonschema
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tidetable import agents
from tidetable.jsonio import (
    env_get,
    FileLock,
    LockTimeout,
    append_conflict,
    append_event,
    conflicts_path,
    events_path,
    read_conflicts,
    read_events,
    read_json,
    write_json,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    from jsonschema import ValidationError
    from jsonschema import validate as _jsonschema_validate

    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

#: Statuses that mean the task will not be worked on again.
#: Done as far as the task graph goes, but a human still has to look at the running
#: app. Unblocks dependents like done; waits in the queue `tidetable look-queue` lists.
LOOK = "needs_human_look"

TERMINAL_STATUSES = frozenset({"done", LOOK, "cancelled", "moved"})

#: Statuses the schema allows.
VALID_STATUSES = ("pending", "in_progress", "done", LOOK, "cancelled", "moved")

STATUS_ICONS = {
    "done": "✓",
    "in_progress": "►",
    LOOK: "◐",
    "pending": "·",
    "cancelled": "✗",
    "moved": "→",
}

#: How long a claim on a task stays valid without being refreshed. A claim that
#: outlives this is treated as abandoned, which is how a crashed agent's task
#: gets handed to someone else without a human deleting fields by hand.
DEFAULT_LEASE_MINUTES = 60

#: Claim bookkeeping, cleared whenever a task stops being in_progress.
CLAIM_FIELDS = ("claimed_by", "claimed_at", "lease_expires_at")


def schema_path() -> Path:
    """Locate the bundled JSON Schema.

    Package data first, so a non-editable ``pip install`` works; then the
    source-checkout layouts (package dir, then the pre-0.2 repo root).
    """
    try:
        from importlib.resources import files

        packaged = Path(str(files("tidetable").joinpath("schema/todo-v1.schema.json")))
        if packaged.is_file():
            return packaged
    except (ImportError, TypeError, FileNotFoundError, ModuleNotFoundError):
        pass

    here = Path(__file__).resolve().parent
    for candidate in (here / "schema" / "todo-v1.schema.json",
                      here.parent / "schema" / "todo-v1.schema.json"):
        if candidate.is_file():
            return candidate
    return here / "schema" / "todo-v1.schema.json"


SCHEMA_PATH = schema_path()


def utc_now() -> str:
    """Timestamp in the same ...Z form the examples use."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# File IO — see tidetable/jsonio.py for the lock and the atomic write.
# ---------------------------------------------------------------------------


def load_todo(path: str) -> dict:
    """Load and parse a todo JSON file."""
    return read_json(path)


def save_todo(data: dict, path: str) -> None:
    """Write the todo file atomically."""
    write_json(data, path)


# ---------------------------------------------------------------------------
# Claims and leases
# ---------------------------------------------------------------------------


def default_owner() -> str:
    """Who this process is, for claim bookkeeping.

    `TIDETABLE_OWNER` when a supervisor wants stable agent names across
    restarts; host:pid otherwise, which is at least unique.
    """
    named = env_get("OWNER").strip()
    return named or f"{socket.gethostname()}:{os.getpid()}"


def default_lease_minutes() -> int:
    raw = env_get("LEASE_MINUTES").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LEASE_MINUTES
    return value if value > 0 else DEFAULT_LEASE_MINUTES


def parse_ts(value: object) -> datetime | None:
    """Parse one of our ...Z timestamps. None when it is missing or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def lease_expired(task: dict, now: datetime | None = None) -> bool:
    """True when a claim has run out — including a claim with no expiry recorded.

    A task claimed by a pre-0.5 `start` has no lease fields at all; treating that
    as expired is what lets the old single-agent behaviour keep working.
    """
    expires = parse_ts(task.get("lease_expires_at"))
    if expires is None:
        return True
    return (now or datetime.now(timezone.utc)) >= expires


def claim_task(task: dict, owner: str, lease_minutes: int) -> None:
    now = datetime.now(timezone.utc)
    task["claimed_by"] = owner
    task["claimed_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    task["lease_expires_at"] = (
        now + timedelta(minutes=lease_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_claim(task: dict) -> None:
    for field in CLAIM_FIELDS:
        task.pop(field, None)


def claim_summary(task: dict) -> dict:
    """The claim as a supervisor sees it, empty when the task is unclaimed."""
    if task.get("status") != "in_progress" or not task.get("claimed_by"):
        return {}
    return {
        "claimed_by": task.get("claimed_by"),
        "claimed_at": task.get("claimed_at"),
        "lease_expires_at": task.get("lease_expires_at"),
        "lease_expired": lease_expired(task),
    }


def claimable_by(task: dict, owner: str) -> bool:
    """True when `owner` may take an in_progress task: it is theirs, or stale."""
    holder = task.get("claimed_by")
    return not holder or holder == owner or lease_expired(task)


# ---------------------------------------------------------------------------
# Owned-path conflicts
# ---------------------------------------------------------------------------


def norm_path(path: object) -> str:
    """A comparable form of a `touches` entry: forward slashes, no ./ or trailing /."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def paths_conflict(a: str, b: str) -> bool:
    """True when two owned paths overlap.

    Equal paths conflict, and so does a directory against anything beneath it —
    a task that owns `src/` and one that owns `src/auth.rs` cannot safely run at
    the same time even though the strings differ.
    """
    a, b = norm_path(a), norm_path(b)
    if not a or not b:
        return False
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def overlapping_paths(first: dict, second: dict) -> list[str]:
    """Every path pair between two tasks that collides, rendered for a human."""
    hits = []
    for a in first.get("touches", []) or []:
        for b in second.get("touches", []) or []:
            if paths_conflict(a, b):
                hits.append(a if norm_path(a) == norm_path(b) else f"{a} ~ {b}")
    return sorted(set(hits))


def touch_conflicts(tasks: list[dict]) -> list[dict]:
    """Every pair among `tasks` that owns overlapping paths.

    `ready` answers "what could run"; this is what turns that into "what is safe
    to fan out", because two agents editing the same file is the failure this
    tool exists to avoid.
    """
    out = []
    for i, first in enumerate(tasks):
        for second in tasks[i + 1:]:
            shared = overlapping_paths(first, second)
            if shared:
                out.append({"a": first["id"], "b": second["id"], "paths": shared})
    return out


def disjoint_tasks(tasks: list[dict], claimed: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Split `tasks` into a conflict-free set and the rest.

    Greedy in the order given (id order from `ready_tasks`), so the answer is
    stable across runs and the lowest-numbered task always wins a contested
    path. Tasks already in progress hold their paths too — their agent is
    editing those files right now.
    """
    taken: list[dict] = list(claimed or [])
    selected: list[dict] = []
    deferred: list[dict] = []
    for task in tasks:
        blockers = [held["id"] for held in taken if overlapping_paths(task, held)]
        if blockers:
            deferred.append({"task": task, "conflicts_with": blockers})
        else:
            selected.append(task)
            taken.append(task)
    return selected, deferred


# ---------------------------------------------------------------------------
# Traversal helpers
# ---------------------------------------------------------------------------


def iter_tasks(data: dict):
    """Yield (phase, task) for every task, in document order."""
    for phase in data.get("phases", []):
        for task in phase.get("tasks", []):
            yield phase, task


def all_tasks(data: dict) -> list[dict]:
    return [t for _, t in iter_tasks(data)]


def id_key(task_id: str):
    """Sort key that orders 1.0.10 after 1.0.9."""
    parts = str(task_id).split(".")
    try:
        return (0, tuple(int(p) for p in parts), "")
    except ValueError:
        return (1, (), str(task_id))


def done_task_ids(data: dict) -> set[str]:
    return {t["id"] for t in all_tasks(data) if t.get("status") in ("done", LOOK)}


def phase_is_complete(phase: dict) -> bool:
    """True when no task in the phase will be worked on again.

    cancelled and moved count as complete — that work is not coming back.
    """
    return all(t.get("status") in TERMINAL_STATUSES for t in phase.get("tasks", []))


def complete_phase_ids(data: dict) -> set[str]:
    return {p["id"] for p in data.get("phases", []) if phase_is_complete(p)}


def blockers_for(task: dict, phase: dict, done_ids: set[str], done_phases: set[str]) -> list[str]:
    """Reasons this task cannot start yet. Empty list means runnable."""
    reasons = [f"task {p}" for p in task.get("prerequisites", []) if p not in done_ids]
    reasons += [f"phase {p}" for p in phase.get("prerequisites", []) if p not in done_phases]
    return reasons


def ready_tasks(data: dict) -> list[dict]:
    """Every pending task whose task and phase prerequisites are all satisfied."""
    done_ids = done_task_ids(data)
    done_phases = complete_phase_ids(data)
    ready = [
        task
        for phase, task in iter_tasks(data)
        if task.get("status") == "pending"
        and not blockers_for(task, phase, done_ids, done_phases)
    ]
    ready.sort(key=lambda t: id_key(t["id"]))
    return ready


def in_progress_tasks(data: dict) -> list[dict]:
    tasks = [t for t in all_tasks(data) if t.get("status") == "in_progress"]
    tasks.sort(key=lambda t: id_key(t["id"]))
    return tasks


def blocked_report(data: dict) -> list[dict]:
    """Pending tasks that are not runnable, with the reason each is stuck."""
    done_ids = done_task_ids(data)
    done_phases = complete_phase_ids(data)
    out = []
    for phase, task in iter_tasks(data):
        if task.get("status") != "pending":
            continue
        reasons = blockers_for(task, phase, done_ids, done_phases)
        if reasons:
            out.append({"id": task["id"], "title": task.get("title", ""), "blocked_by": reasons})
    out.sort(key=lambda t: id_key(t["id"]))
    return out


def find_next(data: dict, resume: bool = True, owner: str | None = None,
              disjoint: bool = False) -> dict | None:
    """The single task `owner` should pick up.

    A resumable in_progress task wins: it was claimed by a session that may have
    crashed, and nothing downstream unblocks until it is finished. Resumable
    means the caller's own claim or an expired one — handing out a task another
    agent is actively holding would put two agents on the same work, which is
    exactly what claiming exists to prevent. With `disjoint`, a pending task
    whose owned paths collide with in-progress work is skipped too.
    """
    if resume:
        who = owner or default_owner()
        for task in in_progress_tasks(data):
            if claimable_by(task, who):
                return task
    ready = ready_tasks(data)
    if disjoint:
        # Skip work whose files another agent is editing right now.
        ready = disjoint_tasks(ready, in_progress_tasks(data))[0]
    return ready[0] if ready else None


def find_task(data: dict, task_id: str) -> tuple[dict, dict] | tuple[None, None]:
    for phase, task in iter_tasks(data):
        if task.get("id") == task_id:
            return phase, task
    return None, None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def semantic_errors(data: dict) -> tuple[list[str], list[str]]:
    """Checks the JSON Schema cannot express. Returns (errors, warnings).

    Schema validation only proves the shape is right. These catch the failures
    that make a todo file silently unusable: an ID that no task has, a cycle no
    agent can ever break, a task filed under the wrong phase.
    """
    errors: list[str] = []
    warnings: list[str] = []

    phases = data.get("phases", [])
    phase_ids: set[str] = set()
    for phase in phases:
        pid = phase.get("id")
        if pid in phase_ids:
            errors.append(f"duplicate phase id: {pid}")
        phase_ids.add(pid)

    tasks_by_id: dict[str, dict] = {}
    for phase, task in iter_tasks(data):
        tid = task.get("id")
        if tid in tasks_by_id:
            errors.append(f"duplicate task id: {tid}")
        tasks_by_id[tid] = task

        # A task id is its phase id plus one sequence number. Anything else and
        # phase-level gating silently applies the wrong phase to the task.
        prefix = str(tid).rsplit(".", 1)[0]
        if prefix != phase.get("id"):
            errors.append(f"task {tid} sits in phase {phase.get('id')} but its id implies phase {prefix}")

    for phase in phases:
        for dep in phase.get("prerequisites", []):
            if dep not in phase_ids:
                errors.append(f"phase {phase.get('id')} requires unknown phase {dep}")

    for tid, task in tasks_by_id.items():
        for dep in task.get("prerequisites", []):
            if dep not in tasks_by_id:
                errors.append(f"task {tid} requires unknown task {dep}")
            elif dep == tid:
                errors.append(f"task {tid} requires itself")
            elif tasks_by_id[dep].get("status") in ("cancelled", "moved"):
                warnings.append(
                    f"task {tid} requires {dep}, which is {tasks_by_id[dep].get('status')} "
                    f"and can never be done — {tid} is blocked forever"
                )
        if task.get("status") == "moved":
            moved_to = task.get("moved_to")
            if not moved_to:
                errors.append(f"task {tid} is moved but has no moved_to")
            elif moved_to not in tasks_by_id:
                errors.append(f"task {tid} moved_to {moved_to}, which does not exist")

    errors.extend(f"prerequisite cycle: {' -> '.join(cycle)}" for cycle in find_cycles(tasks_by_id))
    return errors, warnings


def find_cycles(tasks_by_id: dict[str, dict]) -> list[list[str]]:
    """Every prerequisite cycle, each reported once, via iterative DFS."""
    cycles: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()
    color: dict[str, int] = {}  # 0 = unvisited, 1 = on stack, 2 = finished

    for root in sorted(tasks_by_id, key=id_key):
        if color.get(root, 0) != 0:
            continue
        stack: list[tuple[str, list[str]]] = [(root, [])]
        while stack:
            node, path = stack.pop()
            if node == "\0pop":
                color[path[-1]] = 2
                continue
            if color.get(node, 0) == 1:
                cycle = path[path.index(node):] + [node]
                signature = frozenset(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(cycle)
                continue
            if color.get(node, 0) == 2:
                continue
            color[node] = 1
            stack.append(("\0pop", path + [node]))
            for dep in tasks_by_id[node].get("prerequisites", []):
                if dep in tasks_by_id:
                    stack.append((dep, path + [node]))
    return cycles


def validate_todo(path: str, schema_only: bool = False) -> bool:
    """Validate a todo file. Prints findings; returns True when it is usable."""
    try:
        data = load_todo(path)
    except json.JSONDecodeError as e:
        print(f"✗ Invalid JSON: {e}")
        return False

    ok = True
    if HAS_JSONSCHEMA:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            schema = json.load(f)
        try:
            _jsonschema_validate(instance=data, schema=schema)
            print(f"✓ Schema: {path} matches {SCHEMA_PATH.name}")
        except ValidationError as e:
            print(f"✗ Invalid: {e.message}")
            print(f"  Path: {list(e.absolute_path)}")
            return False
    else:
        print("Warning: jsonschema not installed. Install with: pip install jsonschema")
        print(f"✓ Parsed as JSON: {path} (schema check skipped)")

    if schema_only:
        return ok

    errors, warnings = semantic_errors(data)
    for w in warnings:
        print(f"! Warning: {w}")
    if errors:
        for e in errors:
            print(f"✗ {e}")
        return False
    print("✓ Semantics: ids unique, prerequisites resolve, no cycles")
    return ok


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def record_event(todo_file: str, event: str, **fields) -> None:
    """Append one line to the todo file's event log.

    Status fields are overwritten in place, so the file alone cannot answer
    "how many times did this task get reset, and by whom" — the questions that
    matter when an autonomous loop misbehaves unattended.
    """
    payload = {"at": utc_now(), "event": event}
    payload.update({k: v for k, v in fields.items() if v is not None})
    append_event(todo_file, payload)


def unmet_prereqs(data: dict, task: dict, phase: dict) -> list[str]:
    return blockers_for(task, phase, done_task_ids(data), complete_phase_ids(data))


def set_status(data: dict, task_id: str, status: str, moved_to: str | None = None) -> dict | None:
    """Set a task status. Returns the task, or None when the ID is unknown."""
    _, task = find_task(data, task_id)
    if task is None:
        return None
    task["status"] = status
    if status != "in_progress":
        release_claim(task)
    if moved_to:
        task["moved_to"] = moved_to
    task["updated_at"] = utc_now()
    return task


def collect_changelog(data: dict, phase: dict, task: dict) -> str | None:
    """File a finished task changelog line under the release it ships in.

    The phase release if it has one, else the last phase in the file that does
    (intermediate phases are planning groups; the milestone ships at the end).
    """
    entry = task.get("changelog")
    if not entry:
        return None

    target = phase if isinstance(phase.get("release"), dict) else None
    if target is None:
        for candidate in reversed(data.get("phases", [])):
            if isinstance(candidate.get("release"), dict):
                target = candidate
                break
    if target is None:
        return None

    entries = target["release"].setdefault("changelog_entries", [])
    if entry not in entries:
        entries.append(entry)
    return target["id"]


def release_state(phase: dict) -> dict | None:
    """The release a phase ships, or None when the phase is a planning group."""
    release = phase.get("release")
    if not isinstance(release, dict):
        return None
    return {
        "phase": phase["id"],
        "version": release.get("version"),
        "tag_on_complete": bool(release.get("tag_on_complete")),
        "release_notes": release.get("release_notes"),
        "changelog_entries": list(release.get("changelog_entries") or []),
        "complete": phase_is_complete(phase),
    }


def releasing_phases(data: dict) -> list[dict]:
    """Phases that carry a release object, in document order."""
    return [p for p in data.get("phases", []) if isinstance(p.get("release"), dict)]


def tag_message(state: dict) -> str:
    """The annotated-tag body: release notes, then the collected changelog."""
    lines = [state.get("release_notes") or f"Release {state.get('version') or ''}".strip()]
    entries = state.get("changelog_entries") or []
    if entries:
        lines.append("")
        lines.extend(f"- {entry}" for entry in entries)
    return "\n".join(lines).strip() + "\n"


def unblocked_by(data: dict, task_id: str) -> list[dict]:
    """Tasks that became runnable because task_id just finished."""
    done_ids = done_task_ids(data)
    done_phases = complete_phase_ids(data)
    out = []
    for phase, task in iter_tasks(data):
        if task.get("status") != "pending":
            continue
        if task_id not in task.get("prerequisites", []):
            continue
        if not blockers_for(task, phase, done_ids, done_phases):
            out.append(task)
    out.sort(key=lambda t: id_key(t["id"]))
    return out


def next_task_id(phase: dict) -> str:
    """Next free sequence number in a phase, tolerating hand-edited IDs."""
    nums = []
    for task in phase.get("tasks", []):
        tail = str(task.get("id", "")).rsplit(".", 1)[-1]
        if tail.isdigit():
            nums.append(int(tail))
    return f"{phase['id']}.{max(nums, default=0) + 1}"


def add_task(data: dict, phase_id: str, title: str, **fields) -> dict | None:
    """Append a task to a phase. Returns the new task, or None if no such phase."""
    for phase in data.get("phases", []):
        if phase.get("id") != phase_id:
            continue
        now = utc_now()
        complexity = fields.get("complexity") or "Medium"
        task = {
            "id": next_task_id(phase),
            "title": title,
            "status": "pending",
            "goal": fields.get("goal") or "",
            "touches": list(fields.get("touches") or []),
            "success": list(fields.get("success") or []),
            "prerequisites": list(fields.get("prerequisites") or []),
            "complexity": complexity,
            "agent": fields.get("agent") or agents.effective_agent(
                {"complexity": complexity}, fields.get("tiers")
            ),
            "created_at": now,
            "updated_at": now,
        }
        if fields.get("parallel_group"):
            task["parallel_group"] = fields["parallel_group"]
        phase.setdefault("tasks", []).append(task)
        return task
    return None

PHASE_ID_RE = re.compile(r"^[0-9]+\.[0-9]+$")

#: Fields `edit_task` accepts. Status is deliberately absent: use `status`/`start`/`done`.
EDITABLE_TASK_FIELDS = ("title", "goal", "touches", "success", "prerequisites",
                        "complexity", "agent", "tests", "parallel_group")
LIST_TASK_FIELDS = ("touches", "success", "prerequisites")


def edit_task(data: dict, task_id: str, **fields) -> dict | None:
    """Change plan fields on one task, in place. Returns it, or None when unknown.

    Only the keys passed are touched, so an edit never resets status, claims or
    timestamps it was not asked about. A list field is replaced wholesale (an
    empty list clears it); ``None`` removes an optional scalar such as
    ``tests`` or ``parallel_group``.
    """
    _, task = find_task(data, task_id)
    if task is None:
        return None
    for key, value in fields.items():
        if key not in EDITABLE_TASK_FIELDS:
            raise ValueError(f"not an editable task field: {key}")
        if key in LIST_TASK_FIELDS:
            task[key] = [str(v) for v in (value or [])]
        elif value is None:
            task.pop(key, None)
        else:
            task[key] = value
    task["updated_at"] = utc_now()
    return task


def add_phase(data: dict, phase_id: str, title: str, **fields) -> dict | None:
    """Insert a new phase. Returns it, or None when the ID is already taken.

    Appends at the end unless `after`/`before` names the anchor phase to sit
    next to (callers check the anchor exists first, like cmd_add checks task
    prerequisites). Scheduling needs no update: blockers_for already gates a
    phase's tasks on its phase prerequisites.
    """
    phases = data.setdefault("phases", [])
    if any(p.get("id") == phase_id for p in phases):
        return None
    phase: dict = {
        "id": phase_id,
        "title": title,
        "goal": fields.get("goal") or "",
        "tasks": [],
    }
    prereqs = list(fields.get("prerequisites") or [])
    if prereqs:
        phase["prerequisites"] = prereqs
    anchor = fields.get("after") or fields.get("before")
    if anchor:
        index = next((i for i, p in enumerate(phases) if p.get("id") == anchor), None)
        if index is None:
            return None
        phases.insert(index + 1 if fields.get("after") else index, phase)
    else:
        phases.append(phase)
    return phase


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def task_summary(task: dict, tiers: dict[str, dict] | None = None) -> dict:
    """The task fields an agent actually consumes, for --json output.

    With `tiers`, the resolved provider/model are folded in so a supervisor can
    dispatch straight from this output.
    """
    keys = ("id", "title", "status", "goal", "touches", "success", "tests",
            "prerequisites", "parallel_group", "complexity", "agent")
    summary = {k: task[k] for k in keys if k in task}
    summary.update(claim_summary(task))
    if tiers is not None:
        summary.update(agents.resolve_task(task, tiers))
    return summary


def print_task(task: dict, header: str, tiers: dict[str, dict] | None = None) -> None:
    print(f"{header}: {task['id']} — {task.get('title', '')}")
    print(f"\nGoal:\n{task.get('goal', 'No goal specified')}")
    print(f"\nComplexity: {task.get('complexity', 'unset')} | Agent: {task.get('agent', 'unset')}")
    if tiers is not None:
        resolved = agents.resolve_task(task, tiers)
        if resolved.get("model"):
            print(f"Model: {resolved.get('provider', '?')} / {resolved['model']}")
    if task.get("parallel_group"):
        print(f"Parallel group: {task['parallel_group']}")
    if task.get("touches"):
        print("\nOwned paths:")
        for p in task["touches"]:
            print(f"  • {p}")
    if task.get("success"):
        print("\nSuccess criteria:")
        for s in task["success"]:
            print(f"  ✓ {s}")
    if task.get("tests"):
        print(f"\nTests: {task['tests']}")


def print_blocked(data: dict) -> None:
    blocked = blocked_report(data)
    if not blocked:
        return
    print("\nBlocked tasks:")
    for item in blocked:
        print(f"  {item['id']} — {item['title']} [waiting on: {', '.join(item['blocked_by'])}]")


def list_tasks(data: dict) -> None:
    print("Task Status Overview")
    print("═" * 50)
    for phase in data.get("phases", []):
        marker = " (complete)" if phase_is_complete(phase) else ""
        print(f"\nPhase {phase['id']}: {phase.get('title', '')}{marker}")
        print("─" * 50)
        for task in phase.get("tasks", []):
            icon = STATUS_ICONS.get(task.get("status"), "?")
            print(f"  [{icon}] {task['id']} — {task.get('title', '')}")

    tasks = all_tasks(data)
    print("\nSummary:")
    print(f"  Total: {len(tasks)}")
    for status in VALID_STATUSES:
        count = sum(1 for t in tasks if t.get("status") == status)
        print(f"  {status.replace('_', ' ').title()}: {count}")


def list_payload(data: dict) -> dict:
    return {
        "phases": [
            {
                "id": p["id"],
                "title": p.get("title", ""),
                "complete": phase_is_complete(p),
                "tasks": [
                    {"id": t["id"], "title": t.get("title", ""), "status": t.get("status")}
                    for t in p.get("tasks", [])
                ],
            }
            for p in data.get("phases", [])
        ],
        "summary": {
            status: sum(1 for t in all_tasks(data) if t.get("status") == status)
            for status in VALID_STATUSES
        },
    }


def parallel_groups(data: dict) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for task in all_tasks(data):
        group = task.get("parallel_group")
        if group:
            groups.setdefault(group, []).append(task)
    return groups


def show_parallel(data: dict) -> None:
    print("Parallel Groups")
    print("═" * 50)
    groups = parallel_groups(data)
    if not groups:
        print("No parallel groups defined.")
        return
    ready_ids = {t["id"] for t in ready_tasks(data)}
    for name, tasks in groups.items():
        print(f"\nGroup: {name}")
        print("─" * 50)
        for task in tasks:
            flag = " READY" if task["id"] in ready_ids else ""
            print(f"  {task['id']} — {task.get('title', '')} [{task.get('status')}]{flag}")
        print("  → Tasks marked READY can be dispatched in parallel right now")


def deps_payload(data: dict, task_id: str) -> dict | None:
    phase, task = find_task(data, task_id)
    if task is None:
        return None
    index = {t["id"]: t for t in all_tasks(data)}
    return {
        "id": task["id"],
        "title": task.get("title", ""),
        "status": task.get("status"),
        "phase": phase["id"],
        "prerequisites": [
            {"id": p, "status": index[p].get("status") if p in index else "unknown"}
            for p in task.get("prerequisites", [])
        ],
        "phase_prerequisites": [
            {"id": p, "complete": p in complete_phase_ids(data)}
            for p in phase.get("prerequisites", [])
        ],
        "unblocks": [
            {"id": t["id"], "title": t.get("title", ""), "status": t.get("status")}
            for t in all_tasks(data)
            if task_id in t.get("prerequisites", [])
        ],
        "blocked_by": unmet_prereqs(data, task, phase),
    }


def show_deps(data: dict, task_id: str) -> bool:
    payload = deps_payload(data, task_id)
    if payload is None:
        print(f"Error: task {task_id} not found")
        return False

    print(f"Dependency chain for {task_id}")
    print("═" * 50)
    print(f"Task: {payload['id']} — {payload['title']}")
    print(f"Status: {payload['status']} | Phase: {payload['phase']}")
    print("\nPrerequisites:")
    if not payload["prerequisites"]:
        print("  (none)")
    for p in payload["prerequisites"]:
        print(f"  • {p['id']} [{p['status']}]")
    if payload["phase_prerequisites"]:
        print("\nPhase prerequisites:")
        for p in payload["phase_prerequisites"]:
            print(f"  • phase {p['id']} [{'complete' if p['complete'] else 'incomplete'}]")
    print("\nTasks this unblocks:")
    if not payload["unblocks"]:
        print("  (none)")
    for t in payload["unblocks"]:
        print(f"  • {t['id']} — {t['title']} [{t['status']}]")
    return True


def to_markdown(data: dict) -> str:
    """Render the todo file as markdown for human review (one-way, JSON -> md)."""
    project = data.get("project", {})
    version = project.get("version", {})
    lines = [f"# {project.get('name', 'Project')} — {version.get('milestone', '?')} "
             f"({version.get('release_version', '?')})", ""]
    if project.get("description"):
        lines += [project["description"], ""]

    for phase in data.get("phases", []):
        lines.append(f"## Phase {phase['id']} — {phase.get('title', '')}")
        if phase.get("goal"):
            lines += ["", phase["goal"]]
        if phase.get("prerequisites"):
            lines += ["", f"Requires phases: {', '.join(phase['prerequisites'])}"]
        release = phase.get("release")
        if isinstance(release, dict) and release.get("version"):
            lines += ["", f"Ships as **{release['version']}**"]
        lines.append("")
        for task in phase.get("tasks", []):
            box = "x" if task.get("status") == "done" else " "
            lines.append(f"- [{box}] **{task['id']}** {task.get('title', '')} "
                         f"({task.get('status', 'pending')})")
            if task.get("goal"):
                lines.append(f"  - Goal: {task['goal']}")
            if task.get("prerequisites"):
                lines.append(f"  - Requires: {', '.join(task['prerequisites'])}")
            if task.get("touches"):
                lines.append(f"  - Touches: {', '.join(task['touches'])}")
            for criterion in task.get("success", []):
                lines.append(f"  - Success: {criterion}")
            if task.get("tests"):
                lines.append(f"  - Tests: {task['tests']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def emit_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def resolved_tiers(data: dict, todo_path: str) -> dict[str, dict]:
    return agents.resolve_tiers(data, todo_path)[0]


def report_lock(lock: FileLock) -> None:
    """Say so when a crashed agent's lock had to be broken to get in."""
    if lock.broke_stale_lock:
        print(f"! Broke a stale lock on {lock.path.name}: {lock.broke_stale_lock}")


def cmd_next(args) -> int:
    data = load_todo(args.todo_file)
    tiers = resolved_tiers(data, args.todo_file)
    owner = args.owner or default_owner()
    task = find_next(data, resume=not args.no_resume, owner=owner, disjoint=args.disjoint)
    if task is None:
        if args.json:
            emit_json({"task": None, "blocked": blocked_report(data)})
        else:
            print("No runnable tasks.")
            print_blocked(data)
        return 1
    if args.json:
        emit_json({"task": task_summary(task, tiers),
                   "resumed": task.get("status") == "in_progress"})
    else:
        header = "Resume task" if task.get("status") == "in_progress" else "Next task"
        print_task(task, header, tiers)
    return 0


def cmd_ready(args) -> int:
    data = load_todo(args.todo_file)
    tiers = resolved_tiers(data, args.todo_file)
    ready = ready_tasks(data)
    running = in_progress_tasks(data)

    # in_progress tasks hold their owned paths too: an agent is editing them now.
    selected, deferred = disjoint_tasks(ready, running)
    conflicts = touch_conflicts(ready + running)
    listed = selected if args.disjoint else ready

    if args.json:
        emit_json({
            "ready": [task_summary(t, tiers) for t in listed],
            "disjoint": bool(args.disjoint),
            "deferred": [
                {"id": d["task"]["id"], "title": d["task"].get("title", ""),
                 "conflicts_with": d["conflicts_with"]}
                for d in deferred
            ],
            "conflicts": conflicts,
            "in_progress": [task_summary(t, tiers) for t in running],
            "blocked": blocked_report(data),
        })
        return 0 if listed or running else 1

    print("Ready to start now, conflict-free" if args.disjoint else "Ready to start now")
    print("═" * 50)
    if not listed:
        print("  (none)")
    for task in listed:
        group = f" [group: {task['parallel_group']}]" if task.get("parallel_group") else ""
        resolved = agents.resolve_task(task, tiers)
        model = resolved.get("model", "unset")
        tier = resolved.get("agent", "unset")
        derived = "*" if resolved.get("agent_derived") else ""
        print(f"  {task['id']} — {task.get('title', '')} "
              f"[{tier}{derived} → {model}]{group}")

    if args.disjoint and deferred:
        print("\nDeferred — owned paths overlap something already dispatched:")
        for item in deferred:
            print(f"  {item['task']['id']} — {item['task'].get('title', '')} "
                  f"[conflicts with: {', '.join(item['conflicts_with'])}]")
    elif conflicts:
        print("\n! Owned paths overlap — do NOT dispatch these together:")
        for clash in conflicts:
            print(f"  {clash['a']} ~ {clash['b']}: {', '.join(clash['paths'])}")
        print("  Pass --disjoint for a set that is safe to fan out.")

    if running:
        print("\nAlready claimed (in_progress):")
        for task in running:
            claim = claim_summary(task)
            who = claim.get("claimed_by", "unclaimed")
            stale = " (lease expired)" if claim.get("lease_expired") else ""
            print(f"  {task['id']} — {task.get('title', '')} [{who}]{stale}")
    print_blocked(data)
    return 0 if listed or running else 1


def cmd_start(args) -> int:
    owner = args.owner or default_owner()
    lease = args.lease or default_lease_minutes()
    stolen_from = None
    refreshed = False

    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        phase, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        if task.get("status") == "done":
            print(f"Error: task {args.task_id} is already done. Use reset to reopen it.")
            return 1

        if task.get("status") == "in_progress":
            holder = task.get("claimed_by")
            if holder == owner:
                refreshed = True
            elif not claimable_by(task, owner):
                # Two agents on one task is the failure claiming exists to stop.
                print(f"Error: task {args.task_id} is held by {holder} "
                      f"until {task.get('lease_expires_at')}.")
                print("Wait for the lease to lapse, or `reset` it if that agent is gone.")
                return 1
            else:
                stolen_from = holder

        previous = task.get("status")
        blocked = unmet_prereqs(data, task, phase)
        if blocked and not args.force:
            print(f"Error: task {args.task_id} is blocked by: {', '.join(blocked)}")
            print("Finish those first, or pass --force to override.")
            return 1
        if blocked:
            print(f"Warning: starting {args.task_id} with unmet prerequisites: {', '.join(blocked)}")

        set_status(data, args.task_id, "in_progress")
        claim_task(task, owner, lease)
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "start", task=args.task_id, owner=owner,
                     **{"from": previous, "to": "in_progress"},
                     lease_expires_at=task.get("lease_expires_at"),
                     stolen_from=stolen_from,
                     forced=True if blocked else None)

    payload = {
        "task": args.task_id,
        "owner": owner,
        "lease_expires_at": task.get("lease_expires_at"),
        "refreshed": refreshed,
        "stolen_from": stolen_from,
    }
    if args.json:
        emit_json(payload)
        return 0
    if stolen_from:
        print(f"! Reclaimed {args.task_id} from {stolen_from} — their lease had expired.")
    verb = "lease refreshed" if refreshed else "started"
    print(f"► Task {args.task_id} {verb} by {owner}, lease to {task.get('lease_expires_at')}.")
    return 0


def cmd_done(args) -> int:
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        phase, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        if task.get("status") == "done":
            print(f"Task {args.task_id} is already done.")
            return 0

        owner = args.owner or default_owner()
        holder = task.get("claimed_by")
        if (task.get("status") == "in_progress" and holder and holder != owner
                and not lease_expired(task) and not args.force):
            print(f"Error: task {args.task_id} is held by {holder}, not {owner}.")
            print("Pass --force to finish someone else's claim.")
            return 1

        blocked = unmet_prereqs(data, task, phase)
        if blocked and not args.force:
            print(f"Error: task {args.task_id} has unmet prerequisites: {', '.join(blocked)}")
            print("Pass --force to mark it done anyway.")
            return 1
        if blocked:
            print(f"Warning: unmet prerequisites: {', '.join(blocked)} (forced)")
        if args.changelog:
            task["changelog"] = args.changelog

        previous = task.get("status")
        set_status(data, args.task_id, "done")
        filed_under = collect_changelog(data, phase, task)
        unblocked = unblocked_by(data, args.task_id)
        phase_done = phase_is_complete(phase)
        release = release_state(phase) if phase_done else None
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "done", task=args.task_id, owner=owner,
                     **{"from": previous, "to": "done"},
                     changelog=args.changelog,
                     filed_under=filed_under,
                     phase_complete=True if phase_done else None,
                     forced=True if blocked else None)

    if args.json:
        emit_json({
            "task": args.task_id,
            "unblocked": [{"id": t["id"], "title": t.get("title", "")} for t in unblocked],
            "changelog_filed_under": filed_under,
            "phase": phase["id"],
            "phase_complete": phase_done,
            "release_ready": bool(release and release["tag_on_complete"]),
            "release": release,
        })
        return 0

    print(f"✓ Task {args.task_id} marked as done.")
    if filed_under:
        print(f"  Changelog entry filed under phase {filed_under}.")
    if unblocked:
        print("\nUnblocked tasks:")
        for item in unblocked:
            print(f"  → {item['id']} — {item.get('title', '')}")
    if phase_done:
        print(f"\nPhase {phase['id']} is complete.")
        if release and release["tag_on_complete"]:
            print(f"  Release {release['version'] or '?'} is ready to tag:")
            print(f"    tidetable release {args.todo_file} {phase['id']} --tag")
    return 0


def look_queue(data: dict) -> list[dict]:
    return [{"id": t["id"], "title": t.get("title", ""), "phase": p["id"],
             "note": t.get("human_look", ""), "goal": t.get("goal", ""),
             "success": list(t.get("success") or [])}
            for p in data.get("phases", []) for t in p.get("tasks", [])
            if t.get("status") == LOOK]


def cmd_look_defer(args) -> int:
    """Park a finished-but-unverified task in the human queue (counts as done for deps)."""
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        _, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        previous = task.get("status")
        if previous == "done":
            print(f"Task {args.task_id} is already done.")
            return 0
        set_status(data, args.task_id, LOOK)
        task["human_look"] = args.note or task.get("human_look", "")
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "look_defer", task=args.task_id,
                     owner=args.owner or default_owner(),
                     **{"from": previous, "to": LOOK}, note=args.note)
    print(f"{STATUS_ICONS[LOOK]} Task {args.task_id} queued for a human look (was {previous}).")
    return 0


def cmd_look_queue(args) -> int:
    queue = look_queue(load_todo(args.todo_file))
    if args.json:
        emit_json({"queue": queue})
        return 0
    if not queue:
        print("Human look queue is empty.")
        return 0
    print(f"Needs a human look ({len(queue)})")
    print("=" * 50)
    for q in queue:
        print(f"  {q['id']} - {q['title']}")
        if args.verbose:
            # An agent's note that only points at the criteria ("see the task's
            # success criteria") is noise once the criteria are printed below it.
            pointer = len(q["note"]) < 80 and "success criteria" in q["note"].lower()
            if q["note"] and not pointer:
                print(f"      check: {q['note']}")
            for c in q["success"]:
                print(f"      [ ] {c}")
            print()
        elif q["note"]:
            print(f"      check: {q['note']}")
    print(f"\nClear one: tidetable look-clear {args.todo_file} <id>")
    return 0


def cmd_look_clear(args) -> int:
    """A human has looked: the task becomes plain done (or back to pending with --reject)."""
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        _, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        if task.get("status") != LOOK:
            print(f"Error: task {args.task_id} is {task.get('status')}, not {LOOK}")
            return 1
        target = "pending" if args.reject else "done"
        set_status(data, args.task_id, target)
        task.pop("human_look", None)
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "look_clear", task=args.task_id,
                     owner=args.owner or default_owner(),
                     **{"from": LOOK, "to": target})
    print(f"{STATUS_ICONS[target]} Task {args.task_id} cleared: {target}.")
    return 0


def cmd_reset(args) -> int:
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        _, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        previous = task.get("status")
        held_by = task.get("claimed_by")
        set_status(data, args.task_id, "pending")
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "reset", task=args.task_id,
                     owner=args.owner or default_owner(),
                     **{"from": previous, "to": "pending"},
                     released_claim=held_by)
    suffix = f", releasing {held_by}'s claim" if held_by else ""
    print(f"· Task {args.task_id} reset to pending (was {previous}){suffix}.")
    return 0


def cmd_status(args) -> int:
    if args.status == "moved" and not args.moved_to:
        print("Error: --moved-to is required when setting status to moved")
        return 1
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        _, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        if args.moved_to and find_task(data, args.moved_to)[1] is None:
            print(f"Error: moved_to target {args.moved_to} does not exist")
            return 1
        previous = task.get("status")
        set_status(data, args.task_id, args.status, args.moved_to)
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "status", task=args.task_id,
                     owner=args.owner or default_owner(),
                     **{"from": previous, "to": args.status},
                     moved_to=args.moved_to)
    print(f"{STATUS_ICONS.get(args.status, '?')} Task {args.task_id}: {previous} → {args.status}")
    return 0


def cmd_list(args) -> int:
    data = load_todo(args.todo_file)
    if args.json:
        emit_json(list_payload(data))
    else:
        list_tasks(data)
    return 0


def cmd_sidebar(args) -> int:
    from pathlib import Path

    from tidetable import runs, sidebar

    data = load_todo(args.todo_file)
    # A hub todo is a slug, not a path: there is no local repo to scan for run state.
    run = None
    if not str(args.todo_file).startswith(("http", "hub:")) and Path(args.todo_file).exists():
        run = runs.find_state(Path(args.todo_file).resolve().parent)
    payload = sidebar.sidebar_payload(data, args.width, args.height, run)
    if args.json:
        emit_json(payload)
    else:
        print(sidebar.render_sidebar(payload, args.width), end="")
    return 0


def cmd_parallel(args) -> int:
    data = load_todo(args.todo_file)
    if args.json:
        tiers = resolved_tiers(data, args.todo_file)
        ready_ids = {t["id"] for t in ready_tasks(data)}
        emit_json({
            name: [dict(task_summary(t, tiers), ready=t["id"] in ready_ids) for t in tasks]
            for name, tasks in parallel_groups(data).items()
        })
    else:
        show_parallel(data)
    return 0


def cmd_agents(args) -> int:
    """Show or edit the tier → provider/model mapping."""
    todo_path = getattr(args, "todo", None)
    todo_data = load_todo(todo_path) if todo_path else None

    if args.agents_command == "path":
        print(agents.scope_path(args.scope, todo_path))
        return 0

    if args.agents_command == "providers":
        current, _ = agents.active_provider(todo_data, todo_path)
        if args.json:
            emit_json({"active": current, "presets": agents.PROVIDER_PRESETS})
            return 0
        print("Provider presets")
        print("═" * 62)
        for name, preset in agents.PROVIDER_PRESETS.items():
            marker = "  (active)" if name == current else ""
            print(f"\n{name}{marker}")
            for tier in sorted(preset):
                print(f"  {tier:<18} {preset[tier].get('model', '-')}")
        print("\nSwitch with: tidetable agents use <provider>")
        return 0

    if args.agents_command == "use":
        if not args.clear and not args.provider:
            print("Error: name a provider, or pass --clear to drop the current one")
            return 1
        if args.clear:
            path, changed = agents.clear_provider(args.scope, todo_path)
            if not changed:
                print(f"Nothing to clear: the {args.scope} scope names no provider")
                return 1
            print(f"✓ Cleared the provider preset from the {args.scope} scope")
            print(f"  {path}")
            return 0
        if args.provider not in agents.PROVIDER_PRESETS:
            known = ", ".join(agents.PROVIDER_PRESETS)
            print(f"Error: no preset named {args.provider!r}. Known presets: {known}")
            print("Set individual tiers instead: tidetable agents set <tier> "
                  "--provider ... --model ...")
            return 1
        path = agents.set_provider(args.scope, args.provider, todo_path)
        preset = agents.PROVIDER_PRESETS[args.provider]
        print(f"✓ {args.scope} scope now uses the {args.provider} preset")
        for tier in sorted(preset):
            print(f"    {tier:<18} {preset[tier].get('model', '-')}")
        print(f"  {path}")
        return 0

    if args.agents_command == "show":
        tiers, sources = agents.resolve_tiers(todo_data, todo_path)
        provider, provider_source = agents.active_provider(todo_data, todo_path)
        if args.json:
            emit_json({
                "provider": provider,
                "provider_source": provider_source,
                "tiers": {t: dict(cfg, _sources=sources.get(t, {})) for t, cfg in tiers.items()},
                "overrides": [
                    {"id": task["id"], **{k: task[k] for k in ("provider", "model") if k in task}}
                    for task in (all_tasks(todo_data) if todo_data else [])
                    if task.get("provider") or task.get("model")
                ],
            })
            return 0

        print(f"Provider preset: {provider} (from {provider_source})")
        print()
        print("Agent tiers")
        print("═" * 62)
        print(f"  {'tier':<18} {'provider':<12} {'model':<22} source")
        for tier in sorted(tiers):
            config = tiers[tier]
            origin = sources.get(tier, {})
            print(f"  {tier:<18} {config.get('provider', '-'):<12} "
                  f"{config.get('model', '-'):<22} {origin.get('model', '-')}")
            extras = {k: v for k, v in config.items() if k not in ("provider", "model")}
            for key, value in sorted(extras.items()):
                print(f"      {key} = {value} ({origin.get(key, '-')})")

        if todo_data:
            overrides = [t for t in all_tasks(todo_data) if t.get("provider") or t.get("model")]
            if overrides:
                print("\nPer-task overrides:")
                for task in overrides:
                    resolved = agents.resolve_task(task, tiers)
                    print(f"  {task['id']} → {resolved.get('provider', '?')} / "
                          f"{resolved.get('model', '?')}")
        print(f"\nConfig files (last wins):")
        print(f"  user  {agents.user_config_path()}")
        print(f"  repo  {agents.repo_config_path(todo_path)}")
        if todo_path:
            print(f"  todo  {todo_path}")
        return 0

    if args.agents_command == "set":
        settings = {}
        if args.provider:
            settings["provider"] = args.provider
        if args.model:
            settings["model"] = args.model
        for pair in args.option or []:
            key, _, value = pair.partition("=")
            if not _:
                print(f"Error: --option expects key=value, got {pair!r}")
                return 1
            settings[key] = value
        if not settings:
            print("Error: nothing to set. Pass --provider, --model, or --option key=value")
            return 1
        path = agents.set_tier(args.scope, args.tier, settings, todo_path)
        pairs = ", ".join(f"{k}={v}" for k, v in settings.items())
        print(f"✓ {args.tier}: {pairs} ({args.scope} scope)")
        print(f"  {path}")
        return 0

    if args.agents_command == "unset":
        path, changed = agents.unset_tier(args.scope, args.tier, args.key, todo_path)
        if not changed:
            print(f"Nothing to remove: {args.tier} is not set in the {args.scope} scope")
            return 1
        target = f"{args.tier} {', '.join(args.key)}" if args.key else args.tier
        print(f"✓ Removed {target} from the {args.scope} scope")
        print(f"  {path}")
        return 0

    print(f"Error: unknown agents command {args.agents_command}")
    return 1


def cmd_deps(args) -> int:
    data = load_todo(args.todo_file)
    if args.json:
        payload = deps_payload(data, args.task_id)
        if payload is None:
            emit_json({"error": f"task {args.task_id} not found"})
            return 1
        emit_json(payload)
        return 0
    return 0 if show_deps(data, args.task_id) else 1


def cmd_add(args) -> int:
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        tiers = resolved_tiers(data, args.todo_file)
        for dep in args.prereq or []:
            if find_task(data, dep)[1] is None:
                print(f"Error: prerequisite {dep} does not exist")
                return 1
        task = add_task(
            data,
            args.phase,
            args.title,
            goal=args.goal,
            prerequisites=args.prereq,
            complexity=args.complexity,
            agent=args.agent,
            parallel_group=args.parallel_group,
            touches=args.touches,
            success=args.success,
            tiers=tiers,
        )
        if task is None:
            print(f"Error: phase {args.phase} not found")
            return 1
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "add", task=task["id"],
                     owner=default_owner(), to="pending",
                     complexity=task.get("complexity"), agent=task.get("agent"))
    derived = "" if args.agent else f" (tier {task['agent']} from complexity {task['complexity']})"
    print(f"✓ Added task {task['id']} — {task['title']}{derived}")
    return 0


def cmd_edit(args) -> int:
    updates: dict = {}
    for key in ("title", "goal", "tests", "complexity", "agent", "parallel_group"):
        value = getattr(args, key)
        if value is not None:
            updates[key] = value
    for key in ("touches", "success", "prereq"):
        values = getattr(args, key)
        if values is not None:
            updates["prerequisites" if key == "prereq" else key] = values
    if args.clear_touches:
        updates["touches"] = []
    if args.clear_success:
        updates["success"] = []
    if args.clear_prereq:
        updates["prerequisites"] = []
    if args.clear_tests:
        updates["tests"] = None
    if args.clear_parallel_group:
        updates["parallel_group"] = None
    if not updates:
        print("Error: nothing to edit; pass at least one field")
        return 1

    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        _, task = find_task(data, args.task_id)
        if task is None:
            print(f"Error: task {args.task_id} not found")
            return 1
        if args.task_id in updates.get("prerequisites", []):
            print(f"Error: task {args.task_id} cannot require itself")
            return 1
        for dep in updates.get("prerequisites", []):
            if find_task(data, dep)[1] is None:
                print(f"Error: prerequisite {dep} does not exist")
                return 1
        before, _ = semantic_errors(data)
        edit_task(data, args.task_id, **updates)
        after, _ = semantic_errors(data)
        introduced = [e for e in after if e not in before]
        if introduced:
            print(f"Error: edit rejected: {'; '.join(introduced)}")
            return 1
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "edit", task=args.task_id,
                     owner=args.owner or default_owner(), fields=sorted(updates))
    print(f"✓ Edited task {args.task_id}: {', '.join(sorted(updates))}")
    return 0


def cmd_add_phase(args) -> int:
    if not PHASE_ID_RE.match(args.phase):
        print(f"Error: invalid phase id {args.phase} (want MAJOR.MINOR, e.g. 7.2)")
        return 1
    if args.after and args.before:
        print("Error: --after and --before are mutually exclusive")
        return 1
    if args.phase in (args.prereq or []):
        print(f"Error: phase {args.phase} cannot require itself")
        return 1
    lock = FileLock(args.todo_file)
    with lock:
        report_lock(lock)
        data = load_todo(args.todo_file)
        ids = {p.get("id") for p in data.get("phases", [])}
        if args.phase in ids:
            print(f"Error: phase {args.phase} already exists")
            return 1
        for dep in args.prereq or []:
            if dep not in ids:
                print(f"Error: prerequisite phase {dep} does not exist")
                return 1
        anchor = args.after or args.before
        if anchor and anchor not in ids:
            print(f"Error: anchor phase {anchor} not found")
            return 1
        phase = add_phase(
            data,
            args.phase,
            args.title,
            goal=args.goal,
            prerequisites=args.prereq,
            after=args.after,
            before=args.before,
        )
        if phase is None:
            print(f"Error: phase {args.phase} already exists")
            return 1
        save_todo(data, args.todo_file)
        record_event(args.todo_file, "add-phase", phase=phase["id"],
                     owner=default_owner(), title=phase["title"])
    where = f" after {args.after}" if args.after else f" before {args.before}" if args.before else ""
    print(f"✓ Added phase {phase['id']} — {phase['title']}{where}")
    return 0


def cmd_release(args) -> int:
    """Report — and optionally cut — the tag a completed phase ships as.

    `done` deliberately does not tag: the last task of a phase is an ordinary
    task, and a git side effect fired from it lands at a moment nobody chose.
    This is the explicit step, so the supervisor decides when the tag exists.
    """
    data = load_todo(args.todo_file)
    candidates = releasing_phases(data)
    if not candidates:
        print("Error: no phase in this file has a release object")
        return 1

    if args.phase:
        phase = next((p for p in candidates if p["id"] == args.phase), None)
        if phase is None:
            known = ", ".join(p["id"] for p in candidates)
            print(f"Error: phase {args.phase} has no release object. Phases that do: {known}")
            return 1
    else:
        phase = candidates[-1]

    state = release_state(phase)
    version = state["version"]
    repo = Path(args.todo_file).resolve().parent
    message = tag_message(state)
    command = ["git", "-C", str(repo), "tag", "-a", version or "", "-m", message]

    if args.json and not args.tag:
        emit_json(dict(state, tag_command=command[:6] + ["<message>"]))
        return 0 if state["complete"] else 1

    if not args.tag:
        print(f"Release for phase {phase['id']}: {version or '(no version set)'}")
        print(f"  tag_on_complete: {state['tag_on_complete']}")
        print(f"  phase complete:  {state['complete']}")
        if state["changelog_entries"]:
            print("  changelog:")
            for entry in state["changelog_entries"]:
                print(f"    - {entry}")
        if not state["complete"]:
            print("\nPhase is not complete — nothing to tag yet.")
            return 1
        print("\nTag it with:")
        print(f"  tidetable release {args.todo_file} {phase['id']} --tag")
        return 0

    if not version:
        print(f"Error: phase {phase['id']} has no release.version to tag")
        return 1
    if not state["complete"] and not args.force:
        print(f"Error: phase {phase['id']} is not complete. Pass --force to tag anyway.")
        return 1

    existing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/tags/{version}"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        print(f"Error: tag {version} already exists ({existing.stdout.strip()})")
        return 1

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: git tag failed: {(result.stderr or result.stdout).strip()}")
        return 1
    record_event(args.todo_file, "release", phase=phase["id"], version=version,
                 owner=default_owner(), forced=True if not state["complete"] else None)
    if args.json:
        emit_json(dict(state, tagged=True))
    else:
        print(f"✓ Tagged {version} in {repo}")
        print(f"  Push it with: git -C {repo} push origin {version}")
    return 0


def cmd_history(args) -> int:
    """Replay the append-only event log for this todo file."""
    events = read_events(args.todo_file)
    if args.task_id:
        events = [e for e in events if e.get("task") == args.task_id]
    if args.limit and args.limit > 0:
        events = events[-args.limit:]

    if args.json:
        emit_json({"path": str(events_path(args.todo_file)), "events": events})
        return 0

    if not events:
        print(f"No events recorded in {events_path(args.todo_file).name}")
        return 1
    print(f"History — {events_path(args.todo_file).name}")
    print("═" * 62)
    for event in events:
        transition = ""
        if event.get("from") or event.get("to"):
            transition = f"  {event.get('from', '?')} → {event.get('to', '?')}"
        target = event.get("task") or event.get("phase") or ""
        extras = {k: v for k, v in event.items()
                  if k not in ("at", "event", "task", "phase", "from", "to", "owner")}
        tail = f"  ({', '.join(f'{k}={v}' for k, v in extras.items())})" if extras else ""
        print(f"  {event.get('at', '?')}  {event.get('event', '?'):<8} {target:<10}"
              f"{transition}  [{event.get('owner', '-')}]{tail}")
    return 0


def cmd_conflicts(args) -> int:
    """List the writes the hub refused as stale. Read-only: resolving one is a person's call."""
    conflicts = read_conflicts(args.todo_file)
    if args.json:
        emit_json({"path": str(conflicts_path(args.todo_file)), "conflicts": conflicts})
        return 0
    if not conflicts:
        print(f"No conflicts recorded in {conflicts_path(args.todo_file).name}")
        return 0
    print(f"Conflicts — {conflicts_path(args.todo_file).name}")
    print("═" * 62)
    for c in conflicts:
        print(f"  {c.get('at', '?')}  {c.get('route', '?'):<8} {c.get('task', ''):<10}"
              f"  base {c.get('base')} < {c.get('current')}  [{c.get('owner', '-')}]")
    return 0


def cmd_validate(args) -> int:
    return 0 if validate_todo(args.todo_file, schema_only=args.schema_only) else 1


def cmd_convert(args) -> int:
    data = load_todo(args.todo_file)
    markdown = to_markdown(data)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"✓ Wrote {args.output}")
    else:
        print(markdown, end="")
    return 0


def cmd_serve(args) -> int:
    """Serve the single-writer core API. Lazy import avoids a circular import."""
    from tidetable import serve

    argv = ["--port", str(args.port), "--bind", args.bind,
            "--registry", args.registry]
    if getattr(args, "sessions_db", None):
        argv += ["--sessions-db", args.sessions_db]
    return serve.main(argv)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


TODO_SEARCH_DIRS = (".", "docs", ".tidetable")
TODO_FILE_HELP = ("Path to todo JSON file (default: the todo*.json found in ., docs/ "
                  "or .tidetable/, first directory wins)")


def find_default_todos(cwd: Path | None = None) -> list[Path]:
    """todo*.json files in the first search directory that has any, sorted by name.

    First directory wins rather than merging all three: a stray docs/todo-old.json
    must not make a project root's todo.json ambiguous.
    """
    base = cwd or Path.cwd()
    for sub in TODO_SEARCH_DIRS:
        found = sorted(p for p in (base / sub).glob("todo*.json") if p.is_file())
        if found:
            return found
    return []


def resolve_default_todo(args) -> str | None:
    """Fill a missing `todo_file`; return an error message when that is impossible.

    Never prompts when stdin is not a terminal: an agent or CI job waiting on
    input forever is worse than a clear error.
    """
    if getattr(args, "todo_file", "unset") is not None:
        return None
    from tidetable import remote
    if remote.server_url(args) and not getattr(args, "local", False):
        return "with --server the todo_file argument is the project slug and cannot be defaulted"
    found = find_default_todos()
    searched = ", ".join(TODO_SEARCH_DIRS)
    if len(found) == 1:
        args.todo_file = os.path.relpath(found[0])
        print(f"Using {args.todo_file}", file=sys.stderr)
        return None
    if len(found) > 1:
        names = ", ".join(os.path.relpath(p) for p in found)
        return f"more than one todo file found ({names}); name the one to use"
    if sys.stdin.isatty():
        try:
            answer = input(f"No todo*.json found in {searched}. Path to a todo file: ").strip()
        except EOFError:
            answer = ""
        if answer:
            args.todo_file = answer
            return None
    return f"no todo file given and no todo*.json found in {searched}"


def build_parser() -> argparse.ArgumentParser:
    from tidetable import __version__

    parser = argparse.ArgumentParser(prog="tidetable", description="TideTable CLI")
    parser.add_argument("--version", action="version", version=f"tidetable {__version__}")
    parser.add_argument("--server", metavar="URL",
                        help="Run against a `tidetable serve` hub (default: $TIDETABLE_SERVER). "
                             "The todo_file argument is then a project slug")
    parser.add_argument("--token", metavar="TOKEN",
                        help="Bearer token for --server (default: $TIDETABLE_TOKEN)")
    parser.add_argument("--local", action="store_true",
                        help="Ignore $TIDETABLE_SERVER and use local files")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(name, help_text, *, task_id=False, json_out=False):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("todo_file", nargs="?", help=TODO_FILE_HELP)
        if task_id:
            p.add_argument("task_id", help="Task ID")
        if json_out:
            p.add_argument("--json", action="store_true", help="Machine-readable output")
        return p

    def add_owner(p):
        p.add_argument("--owner", metavar="NAME",
                       help="Agent identity for the claim log "
                            "(default: $TIDETABLE_OWNER, else host:pid)")
        return p

    def add_base(p):
        # Hub only. A queued write must pass the base it saw when it decided; the
        # fetch-on-send default only suits a call made the moment it was decided.
        p.add_argument("--base", type=int, metavar="N",
                       help="With --server: the hub event count this write was decided "
                            "against; the hub refuses it if the task changed since "
                            "(default: the hub's current count, fetched just before)")
        return p

    p = add_command("validate", "Schema + semantic validation")
    p.add_argument("--schema-only", action="store_true", help="Skip the semantic checks")
    p.set_defaults(func=cmd_validate)

    p = add_owner(add_command("next", "Next task to work on", json_out=True))
    p.add_argument("--no-resume", action="store_true",
                   help="Ignore in_progress tasks; return the next pending one")
    p.add_argument("--disjoint", action="store_true",
                   help="Skip tasks whose owned paths collide with work in progress")
    p.set_defaults(func=cmd_next)

    p = add_command("ready", "Every currently-runnable task", json_out=True)
    p.add_argument("--disjoint", action="store_true",
                   help="Only tasks whose owned paths do not overlap each other "
                        "or anything in progress — safe to fan out as a set")
    p.set_defaults(func=cmd_ready)

    p = add_base(add_owner(add_command("start", "Claim a task (-> in_progress)",
                                       task_id=True, json_out=True)))
    p.add_argument("--force", action="store_true", help="Start despite unmet prerequisites")
    p.add_argument("--lease", type=int, metavar="MINUTES",
                   help=f"How long the claim holds (default: "
                        f"$TIDETABLE_LEASE_MINUTES, else {DEFAULT_LEASE_MINUTES})")
    p.set_defaults(func=cmd_start)

    p = add_base(add_owner(add_command("done", "Mark a task done", task_id=True,
                                       json_out=True)))
    p.add_argument("--force", action="store_true",
                   help="Finish despite unmet prerequisites or someone else's claim")
    p.add_argument("--changelog", help="Changelog line for this task")
    p.set_defaults(func=cmd_done)

    p = add_base(add_owner(add_command("look-defer", "Queue a task for a human look (counts as done)",
                                       task_id=True)))
    p.add_argument("--note", help="What the human should check")
    p.set_defaults(func=cmd_look_defer)

    p = add_command("look-queue", "Tasks waiting for a human look", json_out=True)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="List each task's success criteria as a checklist")
    p.set_defaults(func=cmd_look_queue)

    p = add_base(add_owner(add_command("look-clear", "A human looked: task -> done", task_id=True)))
    p.add_argument("--reject", action="store_true", help="The check failed: back to pending")
    p.set_defaults(func=cmd_look_clear)

    p = add_base(add_owner(add_command("reset", "Return a task to pending", task_id=True)))
    p.add_argument("--force", action="store_true",
                   help="With --server: reset even while another owner's lease is live")
    p.set_defaults(func=cmd_reset)

    p = add_base(add_owner(add_command("status", "Set an arbitrary status", task_id=True)))
    p.add_argument("status", choices=VALID_STATUSES)
    p.add_argument("--moved-to", dest="moved_to", help="Target task ID (required for moved)")
    p.set_defaults(func=cmd_status)

    p = add_command("list", "Status overview", json_out=True)
    p.set_defaults(func=cmd_list)

    p = add_command("parallel", "Parallel groups", json_out=True)
    p.set_defaults(func=cmd_parallel)

    p = add_command("deps", "Dependency chain for a task", task_id=True, json_out=True)
    p.set_defaults(func=cmd_deps)

    p = add_command("add", "Append a task to a phase")
    p.add_argument("--phase", required=True, help="Phase ID to add to")
    p.add_argument("--title", required=True, help="Task title")
    p.add_argument("--goal", help="Task goal")
    p.add_argument("--prereq", action="append", metavar="TASK_ID",
                   help="Prerequisite task ID (repeatable)")
    p.add_argument("--complexity", choices=["Low", "Medium", "High", "Very High"])
    p.add_argument("--agent", choices=["basic_dev_agent", "mid_dev_agent", "pro_dev_agent", "flagship"])
    p.add_argument("--parallel-group", dest="parallel_group", help="Parallel group name")
    p.add_argument("--touches", action="append", metavar="PATH", help="Owned path (repeatable)")
    p.add_argument("--success", action="append", metavar="CRITERION",
                   help="Success criterion (repeatable)")
    p.set_defaults(func=cmd_add)

    p = add_base(add_owner(add_command("edit", "Change a task's plan fields", task_id=True)))
    p.add_argument("--title", help="New title")
    p.add_argument("--goal", help="New goal")
    p.add_argument("--complexity", choices=["Low", "Medium", "High", "Very High"])
    p.add_argument("--agent", choices=["basic_dev_agent", "mid_dev_agent", "pro_dev_agent", "flagship"])
    group = p.add_mutually_exclusive_group()
    group.add_argument("--touches", action="append", metavar="PATH",
                       help="Owned path (repeatable; replaces the whole list)")
    group.add_argument("--clear-touches", action="store_true", help="Clear owned paths")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--success", action="append", metavar="CRITERION",
                       help="Success criterion (repeatable; replaces the whole list)")
    group.add_argument("--clear-success", action="store_true", help="Clear success criteria")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--prereq", action="append", metavar="TASK_ID",
                       help="Prerequisite task ID (repeatable; replaces the whole list)")
    group.add_argument("--clear-prereq", action="store_true", help="Drop every prerequisite")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--tests", help="Command that verifies the task")
    group.add_argument("--clear-tests", action="store_true", help="Remove the tests field")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--parallel-group", dest="parallel_group", help="Parallel group name")
    group.add_argument("--clear-parallel-group", action="store_true",
                       help="Remove the parallel group label")
    p.set_defaults(func=cmd_edit)

    p = add_command("add-phase", "Insert a new phase")
    p.add_argument("--phase", required=True, help="New phase ID (MAJOR.MINOR, e.g. 7.2)")
    p.add_argument("--title", required=True, help="Phase title")
    p.add_argument("--goal", help="Phase goal")
    p.add_argument("--prereq", action="append", metavar="PHASE_ID",
                   help="Prerequisite phase ID (repeatable)")
    p.add_argument("--after", metavar="PHASE_ID", help="Insert after this phase")
    p.add_argument("--before", metavar="PHASE_ID", help="Insert before this phase")
    p.set_defaults(func=cmd_add_phase)

    p = add_command("release", "Show — or cut — the tag a completed phase ships as",
                    json_out=True)
    p.add_argument("phase", nargs="?",
                   help="Phase ID (default: the last phase with a release object)")
    p.add_argument("--tag", action="store_true", help="Actually create the annotated git tag")
    p.add_argument("--force", action="store_true", help="Tag even if the phase is incomplete")
    p.set_defaults(func=cmd_release)

    p = add_command("history", "Replay the append-only event log", json_out=True)
    p.add_argument("--task", dest="task_id", metavar="TASK_ID", help="Only this task's events")
    p.add_argument("--limit", type=int, default=50, help="Show the last N events (default 50)")
    p.set_defaults(func=cmd_history)

    p = add_command("conflicts", "List writes the hub refused as stale (read-only)",
                    json_out=True)
    p.set_defaults(func=cmd_conflicts)

    p = add_command("convert", "Render the todo file as markdown")
    p.add_argument("-o", "--output", help="Write to this file instead of stdout")
    p.set_defaults(func=cmd_convert)

    p = add_command("sidebar", "Budgeted sidebar payload for harness panels",
                    json_out=True)
    p.add_argument("--width", type=int, default=80, help="Line width budget (default: 80)")
    p.add_argument("--height", type=int, default=40, help="Row budget (default: 40)")
    p.set_defaults(func=cmd_sidebar)

    s = sub.add_parser("serve", help="Serve the single-writer core API over HTTP")
    s.add_argument("--port", type=int, default=8471, help="Port to bind (default: 8471)")
    s.add_argument("--bind", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    s.add_argument("--registry", default="deploy/registry.json",
                   help="Registry JSON path (default: deploy/registry.json)")
    s.add_argument("--sessions-db", default=None,
                   help="Sessions sqlite path (default: sessions.db beside "
                        "the registry, or $TK_SESSIONS_DB)")
    s.set_defaults(func=cmd_serve)

    build_agents_parser(sub)
    from tidetable import tkrun
    tkrun.add_parser(sub)
    from tidetable import integrations
    integrations.add_parser(sub)
    from tidetable import tui
    tui.add_parser(sub)
    return parser


def build_agents_parser(sub) -> None:
    """`tidetable agents ...` — the provider/model mapping per agent tier."""
    p = sub.add_parser("agents", help="Configure which provider/model runs each tier")
    p.set_defaults(func=cmd_agents)
    inner = p.add_subparsers(dest="agents_command", required=True)

    def add_todo(cmd, help_text):
        cmd.add_argument("--todo", metavar="FILE", help=help_text)

    show = inner.add_parser("show", help="Resolved tiers and where each setting came from")
    show.add_argument("--json", action="store_true", help="Machine-readable output")
    add_todo(show, "Include this todo file as the highest-priority layer")

    providers = inner.add_parser("providers", help="List the built-in provider presets")
    providers.add_argument("--json", action="store_true", help="Machine-readable output")
    add_todo(providers, "Todo file, to report which preset is active for it")

    use = inner.add_parser("use", help="Point a scope at a provider preset")
    use.add_argument("provider", nargs="?", help=f"One of: {', '.join(agents.PROVIDER_PRESETS)}")
    use.add_argument("--clear", action="store_true", help="Drop this scope's preset instead")
    use.add_argument("--scope", choices=agents.SCOPES, default=agents.LAYER_USER,
                     help="Which config layer to edit (default: user)")
    add_todo(use, "Todo file to edit, required for --scope todo")

    for name, help_text in (("set", "Set provider/model for a tier"),
                            ("unset", "Remove a tier, or keys from it")):
        cmd = inner.add_parser(name, help=help_text)
        cmd.add_argument("tier", help="Agent tier, e.g. mid_dev_agent")
        cmd.add_argument("--scope", choices=agents.SCOPES, default=agents.LAYER_USER,
                         help="Which config layer to edit (default: user)")
        add_todo(cmd, "Todo file to edit, required for --scope todo")
        if name == "set":
            cmd.add_argument("--provider", help="Provider name, e.g. anthropic")
            cmd.add_argument("--model", help="Model ID, e.g. claude-opus-5")
            cmd.add_argument("--option", action="append", metavar="KEY=VALUE",
                             help="Any other setting to pass through (repeatable)")
        else:
            cmd.add_argument("--key", action="append",
                             help="Remove only this key (repeatable). Omit to remove the tier")

    path = inner.add_parser("path", help="Print the config file for a scope")
    path.add_argument("--scope", choices=agents.SCOPES, default=agents.LAYER_USER)
    add_todo(path, "Todo file, required for --scope todo")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from tidetable import remote
        refusal = remote.reject_if_local_only(args) or resolve_default_todo(args)
        if refusal:
            print(f"Error: {refusal}")
            code = 1
        else:
            code = remote.run(args) if remote.active(args) else args.func(args)
    except remote.RemoteError as e:
        print(f"Error: {e}")
        code = 1
    except FileNotFoundError as e:
        print(f"Error: {e.filename} not found")
        code = 1
    except json.JSONDecodeError as e:
        target = getattr(args, "todo_file", None) or getattr(args, "todo", None) or "input"
        print(f"✗ Invalid JSON in {target}: {e}")
        code = 1
    except (LockTimeout, ValueError) as e:
        print(f"Error: {e}")
        code = 1
    # Errors exit through here too: `python -m tidetable` and the script shims
    # call main() bare, so a returned 1 would leave the process exiting 0.
    if argv is None:
        sys.exit(code)
    return code


if __name__ == "__main__":
    main()
