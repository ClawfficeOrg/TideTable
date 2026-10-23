"""tidetable.serve — single-writer core API over tidetable.cli.

Single-writer rule: all DAG reads and writes delegate to tidetable.cli
functions (ready_tasks, find_next, set_status, FileLock + write_json, ...).
This module implements no scheduling, claim, or lock logic of its own —
it only maps HTTP routes to cli entry points and enforces the bearer check.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from tidetable import cli
from tidetable import sessions as sessions_store
from tidetable import tokens as tokens_store
from tidetable.jsonio import FileLock

#: Serializes write handlers in-process; the FileLock serializes across processes.
#: Both are held for the whole reload-mutate-write cycle.
_WRITE_LOCK = threading.Lock()

#: (method, path template) -> tidetable.cli handler name.
#: GET reads return current --json shapes; POST writes go through the same
#: locked reload-mutate-write path as the CLI. Presence lives in
#: tidetable.sessions (1.1.3); per-project machine tokens in
#: tidetable.tokens (1.4.1).
#: most bytes read from an unauthenticated request body before answering 401/403.
_UNAUTH_DRAIN_LIMIT = 64 * 1024

ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects"): "cmd_list",
    ("GET", "/api/{slug}/ready"): "cmd_ready",
    ("GET", "/api/{slug}/next"): "cmd_next",
    ("GET", "/api/{slug}/list"): "cmd_list",
    ("GET", "/api/{slug}/deps"): "cmd_deps",
    ("GET", "/api/{slug}/history"): "cmd_history",
    ("GET", "/api/{slug}/look-queue"): "cmd_look_queue",
    ("GET", "/api/{slug}/validate"): "cmd_validate",
    ("GET", "/api/{slug}/parallel"): "cmd_parallel",
    ("GET", "/api/{slug}/sessions"): "sessions",
    ("GET", "/api/{slug}/sidebar"): "sidebar",
    ("GET", "/api/{slug}/base"): "base",
    ("GET", "/api/{slug}/conflicts"): "cmd_conflicts",
    ("GET", "/api/stream"): "stream",
    ("POST", "/api/{slug}/start"): "cmd_start",
    ("POST", "/api/{slug}/done"): "cmd_done",
    ("POST", "/api/{slug}/reset"): "cmd_reset",
    ("POST", "/api/{slug}/status"): "cmd_status",
    ("POST", "/api/{slug}/add"): "cmd_add",
    ("POST", "/api/{slug}/add-phase"): "cmd_add_phase",
    ("POST", "/api/{slug}/edit"): "cmd_edit",
    ("POST", "/api/{slug}/look-defer"): "cmd_look_defer",
    ("POST", "/api/{slug}/look-clear"): "cmd_look_clear",
    ("POST", "/api/{slug}/heartbeat"): "heartbeat",
}

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8471
DEFAULT_REGISTRY = "deploy/registry.json"


def _extract_presented(headers: Mapping[str, str]) -> str:
    """The Bearer secret from an Authorization header, or "" when absent."""
    auth = headers.get("Authorization", headers.get("authorization", ""))
    scheme, _, provided = auth.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        return ""
    return provided.strip()


def check_bearer(headers: Mapping[str, str],
                 tokens_db: str | None = None) -> bool:
    """True when the Bearer token verifies — machine token or TK_API_TOKEN.

    Machine tokens (machine_tokens table, 1.4.1) are tried first: the
    presented ``tk_<prefix>.<secret>`` is prefix-looked-up, hash-compared,
    and expiry/revoked-checked via tidetable.tokens. Anything else falls
    back to the single shared TK_API_TOKEN constant-time compare.
    """
    presented = _extract_presented(headers)
    if presented and tokens_db:
        try:
            if tokens_store.verify_token(tokens_db, presented) is not None:
                return True
        except (OSError, ValueError):
            pass
    expected = os.environ.get("TK_API_TOKEN", "")
    if not expected:
        return False
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def tokens_db_for_registry(registry: str = DEFAULT_REGISTRY,
                           explicit: str | None = None) -> str:
    """Where machine tokens live: explicit flag, else $TK_TOKENS_DB, else beside the registry."""
    return tokens_store.tokens_db_for_registry(registry, explicit)


def _resolve_tokens_db(registry: str,
                       tokens_db: str | None = None) -> str:
    return tokens_db_for_registry(registry, tokens_db)


def bearer_auth(headers: Mapping[str, str], slug: str | None = None,
                scope: str | None = None, registry: str = DEFAULT_REGISTRY,
                tokens_db: str | None = None) -> tuple[bool, bool]:
    """Auth with slug/scope gate. Returns (ok, forbidden).

    ``ok`` False + ``forbidden`` False means 401 (no valid credential);
    ``forbidden`` True means 403 (a valid machine token bound to other
    slugs/scopes). TK_API_TOKEN fallback has full access and never 403s.
    Scope convention: GET routes need ``"read"``, POST routes need
    ``"write"``; ``"*"`` passes either.
    """
    presented = _extract_presented(headers)
    if presented:
        db = _resolve_tokens_db(registry, tokens_db)
        verified = None
        try:
            verified = tokens_store.verify_token(db, presented)
        except (OSError, ValueError):
            verified = None
        if verified is not None:
            slugs, scopes = verified
            if slug is not None and slug not in slugs:
                return False, True
            if scope is not None and scope not in scopes and "*" not in scopes:
                return False, True
            return True, False
    if check_bearer(headers):
        # No db arg: machine lookup already failed above, so this is the
        # TK_API_TOKEN fallback only.
        return True, False
    return False, False


def allowed_slugs(headers: Mapping[str, str], registry: str = DEFAULT_REGISTRY,
                  tokens_db: str | None = None) -> list[str] | None:
    """Slugs a presented machine token may see, or None for full access.

    The shared TK_API_TOKEN and any unverified token (the caller already passed
    auth) see everything. A scoped machine token sees exactly its slugs; that is
    what keeps ``GET /api/projects`` from naming a project it cannot open.
    """
    presented = _extract_presented(headers)
    if not presented:
        return None
    try:
        verified = tokens_store.verify_token(
            _resolve_tokens_db(registry, tokens_db), presented)
    except (OSError, ValueError):
        return None
    if verified is None:
        return None
    slugs, _ = verified
    if "*" in slugs:
        return None
    return list(slugs)


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for `tidetable serve`."""
    parser = argparse.ArgumentParser(
        prog="tidetable serve",
        description="Serve the single-writer core API (scaffold delegating to cli)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--bind", default=DEFAULT_BIND,
                        help=f"Address to bind (default: {DEFAULT_BIND})")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help=f"Registry JSON path (default: {DEFAULT_REGISTRY})")
    parser.add_argument("--sessions-db", default=None,
                        help="Sessions sqlite path (default: sessions.db beside "
                             "the registry, or $TK_SESSIONS_DB)")
    parser.add_argument("--tokens-db", default=None,
                        help="Machine-tokens sqlite path (default: tokens.db beside "
                             "the registry, or $TK_TOKENS_DB)")
    return parser


def load_registry(registry: str = DEFAULT_REGISTRY) -> dict:
    """Read the registry file (slug, repo_url, todo_path per project)."""
    with open(registry, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"projects": []}
    return data


def todo_path_for_slug(slug: str, registry: str = DEFAULT_REGISTRY) -> str | None:
    """Resolve a registry slug to its todo file path. None when unknown.

    todo_path entries are relative to the repo root, so probe the sensible
    bases in order: cwd, the registry file's grandparent (deploy/ -> root),
    then the registry's own directory. First existing file wins; otherwise
    the cwd-joined path so the caller surfaces a normal file-not-found.
    """
    data = load_registry(registry)
    projects = data.get("projects", [])
    entry = next((p for p in projects if p.get("slug") == slug), None)
    if entry is None:
        return None
    raw = str(entry.get("todo_path", ""))
    if not raw:
        return None
    if Path(raw).is_absolute():
        return raw
    candidates = [
        Path.cwd() / raw,
        Path(registry).parent.parent / raw,
        Path(registry).parent / raw,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


def sessions_db_for_registry(registry: str = DEFAULT_REGISTRY,
                             explicit: str | None = None) -> str:
    """Where presence lives: explicit flag, else $TK_SESSIONS_DB, else beside the registry.

    Beside-the-registry keeps /srv layouts working with no extra flags: point
    --registry at /srv/.../registry.json and sessions.db lands next to it.
    """
    if explicit:
        return explicit
    env = os.environ.get("TK_SESSIONS_DB", "").strip()
    if env:
        return env
    return str(Path(registry).parent / "sessions.db")


def _resolve_sessions_db(registry: str,
                         sessions_db: str | None = None) -> str:
    return sessions_db_for_registry(registry, sessions_db)


def _text(value: object) -> str | None:
    """A body field as text, or None when absent — None keeps the stored value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _note_heartbeat(sessions_db: str, agent_id: str, slug: str,
                    body: Mapping[str, object], task_id: str | None,
                    lease_expires_at: str | None = None) -> None:
    """Best-effort presence write. Never raises — losing a heartbeat must not
    fail the todo write it rides along with (same rule as the event log)."""
    try:
        sessions_store.upsert_heartbeat(
            sessions_db, agent_id, slug, task_id,
            repo=_text(body.get("repo")),
            branch=_text(body.get("branch")),
            worktree=_text(body.get("worktree")),
            host=_text(body.get("host")) or socket.gethostname(),
            model=_text(body.get("model")),
            detail=_text(body.get("detail")),
            lease_expires_at=lease_expires_at,
        )
    except Exception:
        pass


def _note_release(sessions_db: str, agent_id: str) -> None:
    """Best-effort task clear after done/reset. Never raises."""
    try:
        sessions_store.clear_task(sessions_db, agent_id)
    except Exception:
        pass


def match_route(method: str, path: str) -> tuple[str | None, str | None]:
    """Match (method, path) against ROUTES. Returns (template, slug)."""
    if (method, path) in ROUTES:
        return path, None
    for (route_method, template) in ROUTES:
        if route_method != method:
            continue
        if "{slug}" not in template:
            continue
        prefix, _, suffix = template.partition("{slug}")
        if not path.startswith(prefix) or not path.endswith(suffix):
            continue
        slug = path[len(prefix):len(path) - len(suffix) if suffix else None]
        if slug and "/" not in slug:
            return template, slug
    return None, None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


#: Live poll cadence for /api/stream (spec: 1-2s). Heartbeat comment keeps
#: proxies/EventSource alive; Traefik must run with buffering off for this route.
SSE_POLL_INTERVAL = 1.0
SSE_HEARTBEAT_INTERVAL = 15.0


def parse_stream_params(query: Mapping[str, str],
                        headers: Mapping[str, str] | None = None) -> tuple[list[str] | None, int]:
    """Parse ``?slugs=a,b&since=SEQ`` plus ``Last-Event-ID`` fallback.

    Returns (slugs or None for all, since). ``since`` is a 1-based global
    sequence over the merged event list; non-numeric values fall back to 0.
    The query param wins; otherwise the ``Last-Event-ID`` request header
    (reconnects from EventSource) is honored.
    """
    raw_slugs = (query.get("slugs") or query.get("slug") or "").strip()
    slugs: list[str] | None = None
    if raw_slugs:
        slugs = [s.strip() for s in raw_slugs.split(",") if s.strip()] or None
    since_raw = (query.get("since") or "").strip()
    if not since_raw and headers is not None:
        since_raw = str(headers.get("Last-Event-ID",
                                    headers.get("last-event-id", "")) or "").strip()
    try:
        since = int(since_raw) if since_raw else 0
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0
    return slugs, since


def resolve_stream_slugs(wanted: list[str] | None,
                         registry: str = DEFAULT_REGISTRY) -> list[str]:
    """Slugs to stream: the requested subset, or every registry slug."""
    data = load_registry(registry)
    known = [str(p.get("slug", "")) for p in data.get("projects", []) if p.get("slug")]
    if wanted is None:
        return known
    keep = set(wanted)
    return [s for s in known if s in keep]


def collect_stream_events(slugs: list[str] | None,
                          registry: str = DEFAULT_REGISTRY) -> list[dict]:
    """Merged replay list across slugs, oldest first, each stamped ``_seq``.

    Reads the same ``*.events.jsonl`` tail the projector ingests (1.2.1), so
    replay works with no DB. ``_seq`` is 1-based over the merged list sorted
    by ``at`` (stable: file order breaks ties), which is what ``since`` filters on.
    """
    resolved = resolve_stream_slugs(slugs, registry)
    merged: list[dict] = []
    for slug in resolved:
        todo_path = todo_path_for_slug(slug, registry)
        if todo_path is None:
            continue
        try:
            events = cli.read_events(todo_path)
        except (OSError, json.JSONDecodeError):
            continue
        for event in events:
            stamped = dict(event)
            stamped.setdefault("slug", slug)
            merged.append(stamped)
    merged.sort(key=lambda e: str(e.get("at", "")))
    for index, event in enumerate(merged, start=1):
        event["_seq"] = index
    return merged


def stream_events_since(slugs: list[str] | None, since: int,
                        registry: str = DEFAULT_REGISTRY) -> list[dict]:
    """Replay slice: events with ``_seq`` strictly greater than ``since``."""
    return [e for e in collect_stream_events(slugs, registry)
            if int(e.get("_seq", 0)) > since]


def format_sse(payload: dict, seq: int | None = None,
               event: str | None = None) -> str:
    """One SSE frame: optional ``event:``/``id:`` lines plus ``data: {json}``.

    The ``_seq`` stamp is transport-only and never serialized into ``data``.
    """
    body = {k: v for k, v in payload.items() if k != "_seq"}
    lines: list[str] = []
    if event:
        lines.append(f"event: {event}")
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"data: {json.dumps(body, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n\n"


def handle_get(route: str, slug: str | None, query: Mapping[str, str],
               registry: str = DEFAULT_REGISTRY,
               sessions_db: str | None = None,
               visible_slugs: list[str] | None = None) -> tuple[int, dict]:
    """Serve one GET read by delegating to tidetable.cli — no DAG logic here.

    Returns (status, payload) where payload matches the CLI --json shape for
    the same read. Raises FileNotFoundError for a missing registry/todo file.
    ``visible_slugs`` (a scoped machine token's slugs; None means all) filters
    the project list so it never names a project the caller cannot open.
    """
    if route == "/api/projects":
        data = load_registry(registry)
        projects = data.get("projects", [])
        if visible_slugs is not None:
            projects = [p for p in projects if p.get("slug") in visible_slugs]
        return 200, {"projects": projects}

    if slug is None:
        return 400, {"error": "missing slug"}
    todo_path = todo_path_for_slug(slug, registry)
    if todo_path is None:
        return 404, {"error": f"unknown slug: {slug}"}
    data = cli.load_todo(todo_path)

    if route == "/api/{slug}/ready":
        tiers = cli.resolved_tiers(data, todo_path)
        ready = cli.ready_tasks(data)
        running = cli.in_progress_tasks(data)
        # in_progress tasks hold their paths: same greedy ID-order split as cmd_ready.
        selected, deferred = cli.disjoint_tasks(ready, running)
        conflicts = cli.touch_conflicts(ready + running)
        listed = selected if _truthy(query.get("disjoint")) else ready
        return 200, {
            "ready": [cli.task_summary(t, tiers) for t in listed],
            "disjoint": _truthy(query.get("disjoint")),
            "deferred": [
                {"id": d["task"]["id"], "title": d["task"].get("title", ""),
                 "conflicts_with": d["conflicts_with"]}
                for d in deferred
            ],
            "conflicts": conflicts,
            "in_progress": [cli.task_summary(t, tiers) for t in running],
            "blocked": cli.blocked_report(data),
        }

    if route == "/api/{slug}/next":
        tiers = cli.resolved_tiers(data, todo_path)
        owner = query.get("owner") or cli.default_owner()
        resume_raw = query.get("resume", "1").strip().lower()
        resume = resume_raw not in ("0", "false", "no", "off")
        task = cli.find_next(
            data,
            resume=resume,
            owner=owner,
            disjoint=_truthy(query.get("disjoint")),
        )
        if task is None:
            return 404, {"task": None, "blocked": cli.blocked_report(data)}
        return 200, {
            "task": cli.task_summary(task, tiers),
            "resumed": task.get("status") == "in_progress",
        }

    if route == "/api/{slug}/list":
        return 200, cli.list_payload(data)

    if route == "/api/{slug}/deps":
        task_id = (query.get("task") or query.get("task_id") or "").strip()
        if not task_id:
            return 400, {"error": "missing ?task=<id>"}
        payload = cli.deps_payload(data, task_id)
        if payload is None:
            return 404, {"error": f"task {task_id} not found"}
        return 200, payload

    if route == "/api/{slug}/history":
        events = cli.read_events(todo_path)
        task_id = (query.get("task") or "").strip()
        if task_id:
            events = [e for e in events if e.get("task") == task_id]
        limit_raw = (query.get("limit") or "").strip()
        try:
            limit = int(limit_raw) if limit_raw else 50
        except ValueError:
            limit = 50
        if limit and limit > 0:
            events = events[-limit:]
        return 200, {"path": str(cli.events_path(todo_path)), "events": events}

    if route == "/api/{slug}/look-queue":
        return 200, {"queue": cli.look_queue(data)}

    if route == "/api/{slug}/validate":
        schema_status: str = "skipped"
        schema_error: str | None = None
        if cli.HAS_JSONSCHEMA:
            try:
                from jsonschema import ValidationError as _ValidationError
                from jsonschema import validate as _validate

                with open(cli.SCHEMA_PATH, encoding="utf-8") as f:
                    schema = json.load(f)
                _validate(instance=data, schema=schema)
                schema_status = "ok"
            except _ValidationError as e:
                schema_status = "invalid"
                schema_error = str(e.message)
        errors, warnings = cli.semantic_errors(data)
        if schema_error:
            errors = [f"schema: {schema_error}"] + errors
        ok = not errors and schema_status != "invalid"
        payload: dict = {"ok": ok, "errors": errors, "warnings": warnings,
                         "schema": schema_status}
        return (200 if ok else 422), payload

    if route == "/api/{slug}/parallel":
        tiers = cli.resolved_tiers(data, todo_path)
        ready_ids = {t["id"] for t in cli.ready_tasks(data)}
        return 200, {
            name: [dict(cli.task_summary(t, tiers), ready=t["id"] in ready_ids)
                   for t in tasks]
            for name, tasks in cli.parallel_groups(data).items()
        }

    if route == "/api/{slug}/sidebar":
        from pathlib import Path

        from tidetable import runs
        from tidetable import sidebar as sidebar_mod

        def _dim(name: str, default: int) -> int:
            try:
                value = int(query.get(name, ""))
            except ValueError:
                return default
            return value if value > 0 else default

        # The hub knows the checkout for this slug, so it can read TKRun state too.
        run = runs.find_state(Path(todo_path).resolve().parent) if todo_path else None
        return 200, sidebar_mod.sidebar_payload(data, _dim("width", 80), _dim("height", 40), run)

    if route == "/api/{slug}/sessions":
        db = _resolve_sessions_db(registry, sessions_db)
        return 200, {"slug": slug, "sessions": sessions_store.list_sessions(db, slug)}

    if route == "/api/{slug}/base":
        return 200, {"slug": slug, "base": event_base(todo_path)}

    if route == "/api/{slug}/conflicts":
        return 200, {"path": str(cli.conflicts_path(todo_path)),
                     "conflicts": cli.read_conflicts(todo_path)}

    return 404, {"error": f"unknown route: {route}"}


def event_base(todo_path: str) -> int:
    """The hub's version for a slug: how many events its log holds.

    A write carries the base it saw when it decided; the hub compares it with
    this count to tell whether the task moved underneath the writer.
    """
    return len(cli.read_events(todo_path))


#: A base that is not a non-negative integer.
_BAD_BASE = object()


def _parse_base(value: object) -> object:
    """``None`` when absent, an ``int`` when valid, else ``_BAD_BASE``."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, bool):
        return _BAD_BASE
    try:
        base = int(str(value).strip())
    except ValueError:
        return _BAD_BASE
    return base if base >= 0 else _BAD_BASE


def _stale_base(todo_path: str, route: str, task_id: str, base: int, owner: str,
                body: Mapping[str, object]) -> tuple[int, dict] | None:
    """409 when ``task_id`` changed after ``base``; the refusal goes on the conflict list.

    Called inside the write lock, so the events read here are the ones the
    write would land after. A base beyond the log (rotated, or from another
    hub) is a conflict too: the hub cannot vouch for what the writer saw.
    Nothing ever replays a recorded conflict; a person reads the list and
    decides.
    """
    events = cli.read_events(todo_path)
    current = len(events)
    since = [e for e in events[base:] if e.get("task") == task_id] if base <= current else []
    if base <= current and not since:
        return None
    cli.append_conflict(todo_path, {
        "at": cli.utc_now(),
        "route": route.rsplit("/", 1)[-1],
        "task": task_id,
        "owner": owner,
        "base": base,
        "current": current,
        "changed_by": [{k: e.get(k) for k in ("at", "event", "owner", "from", "to")
                        if e.get(k) is not None} for e in since],
        "request": {k: v for k, v in body.items() if v is not None},
    })
    return 409, {"conflict": True, "task": task_id, "base": base, "current": current}


def _body_truthy(value: object) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _body_extras(body: Mapping[str, object]) -> dict:
    """repo/branch/agent audit extras passed through into the event log."""
    extras: dict = {}
    for key in ("repo", "branch", "agent"):
        value = body.get(key)
        if value is not None and str(value).strip() != "":
            extras[key] = value
    return extras


def handle_post(route: str, slug: str | None, body: Mapping[str, object] | None,
                registry: str = DEFAULT_REGISTRY,
                sessions_db: str | None = None) -> tuple[int, dict]:
    """Serve one POST write via the same locked reload-mutate-write path as the CLI.

    Each write holds the module-level ``_WRITE_LOCK`` (in-process threads) and
    ``FileLock`` (across processes), reloads inside the lock via
    ``cli.load_todo``, mutates with the ``cli`` functions the CLI itself uses
    (``set_status``/``claim_task``/``claimable_by``/``add_task``/... — never a
    local reimplementation, so ``set_status`` clearing of ``CLAIM_FIELDS`` and
    ``claimable_by`` lease checks stay enforced), saves via ``cli.save_todo``
    (atomic ``write_json``), and appends an event with the actor plus any
    repo/branch/agent extras from the body.

    Returns (status, payload) where payload matches the CLI --json shape for
    the same mutation.
    """
    if slug is None:
        return 400, {"error": "missing slug"}
    todo_path = todo_path_for_slug(slug, registry)
    if todo_path is None:
        return 404, {"error": f"unknown slug: {slug}"}
    data_in: dict = dict(body or {})
    owner = str(data_in.get("owner") or data_in.get("actor") or cli.default_owner())
    extras = _body_extras(data_in)
    base = _parse_base(data_in.get("base"))
    if base is _BAD_BASE:
        return 400, {"error": "base must be a non-negative integer (GET /api/<slug>/base)"}
    db = _resolve_sessions_db(registry, sessions_db)

    if route == "/api/{slug}/heartbeat":
        agent_id = str(data_in.get("agent_id") or data_in.get("owner")
                       or data_in.get("agent") or cli.default_owner()).strip()
        if not agent_id:
            return 400, {"error": "missing agent_id"}
        task_id = _text(data_in.get("task") or data_in.get("task_id")
                        or data_in.get("id"))
        try:
            session = sessions_store.upsert_heartbeat(
                db, agent_id, slug, task_id,
                repo=_text(data_in.get("repo")),
                branch=_text(data_in.get("branch")),
                worktree=_text(data_in.get("worktree")),
                host=_text(data_in.get("host")) or socket.gethostname(),
                model=_text(data_in.get("model")),
                detail=_text(data_in.get("detail")),
                lease_expires_at=_text(data_in.get("lease_expires_at")),
            )
        except ValueError as e:
            return 400, {"error": str(e)}
        return 200, {"session": session}

    if route == "/api/{slug}/start":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        try:
            lease = int(data_in.get("lease") or data_in.get("lease_minutes")
                        or cli.default_lease_minutes())
        except (TypeError, ValueError):
            lease = cli.default_lease_minutes()
        if lease <= 0:
            lease = cli.default_lease_minutes()
        force = _body_truthy(data_in.get("force"))
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                phase, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                if isinstance(base, int):
                    stale = _stale_base(todo_path, route, task_id, base, owner, data_in)
                    if stale:
                        return stale
                if task.get("status") == "done":
                    return 409, {"error": f"task {task_id} is already done. Use reset to reopen it."}
                stolen_from = None
                refreshed = False
                if task.get("status") == "in_progress":
                    holder = task.get("claimed_by")
                    if holder == owner:
                        refreshed = True
                    elif not cli.claimable_by(task, owner):
                        return 409, {
                            "error": f"task {task_id} is held by {holder} "
                                     f"until {task.get('lease_expires_at')}.",
                        }
                    else:
                        stolen_from = holder
                previous = task.get("status")
                blocked = cli.unmet_prereqs(data, task, phase)
                if blocked and not force:
                    return 422, {"error": f"task {task_id} is blocked by: {', '.join(blocked)}"}
                cli.set_status(data, task_id, "in_progress")
                cli.claim_task(task, owner, lease)
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "start", task=task_id, owner=owner,
                                 **{"from": previous, "to": "in_progress"},
                                 lease_expires_at=task.get("lease_expires_at"),
                                 stolen_from=stolen_from,
                                 forced=True if blocked else None,
                                 **extras)
                _note_heartbeat(db, owner, slug, data_in, task_id,
                                task.get("lease_expires_at"))
                return 200, {
                    "task": task_id,
                    "owner": owner,
                    "lease_expires_at": task.get("lease_expires_at"),
                    "refreshed": refreshed,
                    "stolen_from": stolen_from,
                }

    if route == "/api/{slug}/done":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        force = _body_truthy(data_in.get("force"))
        changelog = data_in.get("changelog")
        changelog_text = str(changelog) if changelog is not None else None
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                phase, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                if task.get("status") == "done":
                    return 200, {
                        "task": task_id,
                        "already_done": True,
                        "unblocked": [],
                        "changelog_filed_under": None,
                        "phase": phase["id"] if phase else None,
                        "phase_complete": bool(phase and cli.phase_is_complete(phase)),
                        "release_ready": False,
                        "release": cli.release_state(phase) if phase else None,
                    }
                # After already_done, so a retried done stays idempotent.
                if isinstance(base, int):
                    stale = _stale_base(todo_path, route, task_id, base, owner, data_in)
                    if stale:
                        return stale
                holder = task.get("claimed_by")
                if (task.get("status") == "in_progress" and holder and holder != owner
                        and not cli.lease_expired(task) and not force):
                    return 409, {
                        "error": f"task {task_id} is held by {holder}, not {owner}.",
                    }
                blocked = cli.unmet_prereqs(data, task, phase)
                if blocked and not force:
                    return 422, {
                        "error": f"task {task_id} has unmet prerequisites: {', '.join(blocked)}",
                    }
                if changelog_text:
                    task["changelog"] = changelog_text
                previous = task.get("status")
                cli.set_status(data, task_id, "done")
                filed_under = cli.collect_changelog(data, phase, task)
                unblocked = cli.unblocked_by(data, task_id)
                phase_done = cli.phase_is_complete(phase)
                release = cli.release_state(phase) if phase_done else None
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "done", task=task_id, owner=owner,
                                 **{"from": previous, "to": "done"},
                                 changelog=changelog_text,
                                 filed_under=filed_under,
                                 phase_complete=True if phase_done else None,
                                 forced=True if blocked else None,
                                 **extras)
                _note_release(db, owner)
                return 200, {
                    "task": task_id,
                    "unblocked": [{"id": t["id"], "title": t.get("title", "")}
                                  for t in unblocked],
                    "changelog_filed_under": filed_under,
                    "phase": phase["id"],
                    "phase_complete": phase_done,
                    "release_ready": bool(release and release["tag_on_complete"]),
                    "release": release,
                }

    if route == "/api/{slug}/reset":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        # Reset throws away someone's claim or finished work, so it never runs blind.
        if base is None:
            return 428, {"error": "reset needs base=<event count> "
                                  "(GET /api/<slug>/base when you decide)"}
        force = _body_truthy(data_in.get("force"))
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                _, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                stale = _stale_base(todo_path, route, task_id, base, owner, data_in)
                if stale:
                    return stale
                previous = task.get("status")
                held_by = task.get("claimed_by")
                if (previous == "in_progress" and held_by and held_by != owner
                        and not cli.lease_expired(task) and not force):
                    return 409, {
                        "error": f"task {task_id} is held by {held_by} until "
                                 f"{task.get('lease_expires_at')}; reset refused.",
                    }
                cli.set_status(data, task_id, "pending")
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "reset", task=task_id, owner=owner,
                                 **{"from": previous, "to": "pending"},
                                 released_claim=held_by,
                                 **extras)
                _note_release(db, owner)
                return 200, {
                    "task": task_id,
                    "from": previous,
                    "to": "pending",
                    "released_claim": held_by,
                }

    if route == "/api/{slug}/status":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        status = str(data_in.get("status") or data_in.get("to") or "").strip()
        moved_to = data_in.get("moved_to") or data_in.get("movedTo")
        moved_to_text = str(moved_to).strip() if moved_to else None
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        if not status:
            return 400, {"error": "missing status"}
        if status not in cli.VALID_STATUSES:
            return 400, {"error": f"invalid status: {status}"}
        if status == "moved" and not moved_to_text:
            return 400, {"error": "--moved-to is required when setting status to moved"}
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                _, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                if moved_to_text and cli.find_task(data, moved_to_text)[1] is None:
                    return 404, {"error": f"moved_to target {moved_to_text} does not exist"}
                if isinstance(base, int):
                    stale = _stale_base(todo_path, route, task_id, base, owner, data_in)
                    if stale:
                        return stale
                previous = task.get("status")
                cli.set_status(data, task_id, status, moved_to_text)
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "status", task=task_id, owner=owner,
                                 **{"from": previous, "to": status},
                                 moved_to=moved_to_text,
                                 **extras)
                return 200, {
                    "task": task_id,
                    "from": previous,
                    "to": status,
                    "moved_to": moved_to_text,
                }

    if route == "/api/{slug}/add":
        phase_id = str(data_in.get("phase") or data_in.get("phase_id") or "").strip()
        title = str(data_in.get("title") or "").strip()
        if not phase_id:
            return 400, {"error": "missing phase=<id>"}
        if not title:
            return 400, {"error": "missing title"}
        prereqs = data_in.get("prerequisites") or data_in.get("prereq") or []
        if isinstance(prereqs, str):
            prereqs = [prereqs]
        prereqs = list(prereqs)
        touches = data_in.get("touches") or []
        if isinstance(touches, str):
            touches = [touches]
        success = data_in.get("success") or []
        if isinstance(success, str):
            success = [success]
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                tiers = cli.resolved_tiers(data, todo_path)
                for dep in prereqs:
                    if cli.find_task(data, dep)[1] is None:
                        return 404, {"error": f"prerequisite {dep} does not exist"}
                tier_name = (data_in.get("tier") or data_in.get("agent_tier")
                             or data_in.get("agentTier"))
                # A body "agent" is the audit identity for the event log, not a
                # tier — only reuse it as the tier when it names a known tier.
                agent_field = data_in.get("agent")
                if tier_name is None and isinstance(agent_field, str):
                    if agent_field in ("basic_dev_agent", "mid_dev_agent",
                                       "pro_dev_agent", "flagship"):
                        tier_name = agent_field
                task = cli.add_task(
                    data,
                    phase_id,
                    title,
                    goal=data_in.get("goal"),
                    prerequisites=prereqs,
                    complexity=data_in.get("complexity"),
                    agent=tier_name,
                    parallel_group=data_in.get("parallel_group") or data_in.get("parallelGroup"),
                    touches=list(touches),
                    success=list(success),
                    tiers=tiers,
                )
                if task is None:
                    return 404, {"error": f"phase {phase_id} not found"}
                cli.save_todo(data, todo_path)
                event_fields: dict = {
                    "task": task["id"],
                    "owner": owner,
                    "to": "pending",
                    "complexity": task.get("complexity"),
                    "agent": task.get("agent"),
                }
                # Body extras win so repo/branch/agent audit fields always land.
                event_fields.update(extras)
                cli.record_event(todo_path, "add", **event_fields)
                return 200, {
                    "task": cli.task_summary(task, tiers),
                    "id": task["id"],
                    "phase": phase_id,
                }

    if route == "/api/{slug}/add-phase":
        phase_id = str(data_in.get("phase") or data_in.get("phase_id")
                       or data_in.get("id") or "").strip()
        title = str(data_in.get("title") or "").strip()
        if not phase_id:
            return 400, {"error": "missing phase=<id>"}
        if not title:
            return 400, {"error": "missing title"}
        if not cli.PHASE_ID_RE.match(phase_id):
            return 400, {"error": f"invalid phase id {phase_id} (want MAJOR.MINOR)"}
        prereqs = data_in.get("prerequisites") or data_in.get("prereq") or []
        if isinstance(prereqs, str):
            prereqs = [prereqs]
        prereqs = list(prereqs)
        after = data_in.get("after")
        after = str(after).strip() if after else None
        before = data_in.get("before")
        before = str(before).strip() if before else None
        if after and before:
            return 400, {"error": "--after and --before are mutually exclusive"}
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                ids = {p.get("id") for p in data.get("phases", [])}
                if phase_id in ids:
                    return 409, {"error": f"phase {phase_id} already exists"}
                for dep in prereqs:
                    if dep not in ids:
                        return 404, {"error": f"prerequisite phase {dep} does not exist"}
                anchor = after or before
                if anchor and anchor not in ids:
                    return 404, {"error": f"anchor phase {anchor} not found"}
                phase = cli.add_phase(
                    data,
                    phase_id,
                    title,
                    goal=data_in.get("goal"),
                    prerequisites=prereqs,
                    after=after,
                    before=before,
                )
                if phase is None:
                    return 409, {"error": f"phase {phase_id} already exists"}
                cli.save_todo(data, todo_path)
                event_fields: dict = {
                    "phase": phase["id"],
                    "owner": owner,
                    "title": phase.get("title", ""),
                }
                event_fields.update(extras)
                cli.record_event(todo_path, "add-phase", **event_fields)
                return 200, {"phase": phase, "id": phase["id"]}

    if route == "/api/{slug}/look-defer":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        note = _text(data_in.get("note"))
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                _, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                previous = task.get("status")
                if previous == "done":
                    return 200, {
                        "task": task_id,
                        "already_done": True,
                        "from": previous,
                        "to": previous,
                    }
                cli.set_status(data, task_id, cli.LOOK)
                task["human_look"] = note or task.get("human_look", "")
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "look_defer", task=task_id, owner=owner,
                                 **{"from": previous, "to": cli.LOOK}, note=note,
                                 **extras)
                return 200, {
                    "task": task_id,
                    "from": previous,
                    "to": cli.LOOK,
                    "note": task["human_look"],
                }

    if route == "/api/{slug}/look-clear":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        reject = _body_truthy(data_in.get("reject"))
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                _, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                if task.get("status") != cli.LOOK:
                    return 409, {
                        "error": f"task {task_id} is {task.get('status')}, not {cli.LOOK}",
                    }
                target = "pending" if reject else "done"
                cli.set_status(data, task_id, target)
                task.pop("human_look", None)
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "look_clear", task=task_id, owner=owner,
                                 **{"from": cli.LOOK, "to": target}, **extras)
                return 200, {
                    "task": task_id,
                    "from": cli.LOOK,
                    "to": target,
                }

    if route == "/api/{slug}/edit":
        task_id = str(data_in.get("task") or data_in.get("task_id")
                      or data_in.get("id") or "").strip()
        if not task_id:
            return 400, {"error": "missing task=<id>"}
        updates: dict = {}
        for key in ("title", "goal", "tests", "complexity", "agent"):
            if key in data_in:
                updates[key] = data_in[key]
        if "parallel_group" in data_in or "parallelGroup" in data_in:
            updates["parallel_group"] = data_in.get(
                "parallel_group", data_in.get("parallelGroup"))
        for key, aliases in (("touches", ("touches",)),
                             ("success", ("success",)),
                             ("prerequisites", ("prerequisites", "prereq"))):
            for alias in aliases:
                if alias not in data_in:
                    continue
                value = data_in[alias]
                if value is None:
                    updates[key] = []
                elif isinstance(value, str):
                    updates[key] = [value]
                elif isinstance(value, list):
                    updates[key] = value
                else:
                    return 400, {"error": f"{key} must be a list of strings"}
                break
        if not updates:
            return 400, {"error": "nothing to edit; pass at least one field"}
        if "title" in updates and not _text(updates["title"]):
            return 400, {"error": "title cannot be empty"}
        if "complexity" in updates and updates["complexity"] not in (
                "Low", "Medium", "High", "Very High"):
            return 400, {"error": f"invalid complexity: {updates['complexity']}"}
        if "agent" in updates and updates["agent"] not in (
                "basic_dev_agent", "mid_dev_agent", "pro_dev_agent", "flagship"):
            return 400, {"error": f"invalid agent: {updates['agent']}"}
        with _WRITE_LOCK:
            with FileLock(todo_path):
                data = cli.load_todo(todo_path)
                _, task = cli.find_task(data, task_id)
                if task is None:
                    return 404, {"error": f"task {task_id} not found"}
                if task_id in updates.get("prerequisites", []):
                    return 400, {"error": f"task {task_id} cannot require itself"}
                for dep in updates.get("prerequisites", []):
                    if cli.find_task(data, dep)[1] is None:
                        return 404, {"error": f"prerequisite {dep} does not exist"}
                before, _ = cli.semantic_errors(data)
                cli.edit_task(data, task_id, **updates)
                after, _ = cli.semantic_errors(data)
                introduced = [e for e in after if e not in before]
                if introduced:
                    return 422, {"error": "; ".join(introduced)}
                cli.save_todo(data, todo_path)
                cli.record_event(todo_path, "edit", task=task_id, owner=owner,
                                 fields=sorted(updates), **extras)
                tiers = cli.resolved_tiers(data, todo_path)
                return 200, {
                    "task": cli.task_summary(task, tiers),
                    "changed": sorted(updates),
                }


    return 404, {"error": f"unknown route: {route}"}


class _Handler(BaseHTTPRequestHandler):
    """Bearer gate + route table lookup; GET reads delegate to cli via handle_get."""

    server_version = "TideTableServe/0.1"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self, query: dict[str, str], registry: str) -> None:
        """Emit ``GET /api/stream`` as Server-Sent Events.

        Replay first (events.jsonl tail filtered by ``since``/``Last-Event-ID``),
        then poll the same tail every ``SSE_POLL_INTERVAL`` seconds and emit
        ``data: {json}`` frames, with a ``: heartbeat`` comment every
        ``SSE_HEARTBEAT_INTERVAL`` seconds. Stdlib only; ends on disconnect.
        """
        slugs, since = parse_stream_params(query, self.headers)  # type: ignore[arg-type]
        if slugs is not None and not resolve_stream_slugs(slugs, registry):
            self._send_json(404, {"error": f"unknown slugs: {','.join(slugs)}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = since
        try:
            for event in stream_events_since(slugs, last, registry):
                seq = int(event.get("_seq", last + 1))
                self.wfile.write(format_sse(event, seq).encode("utf-8"))
                last = max(last, seq)
            self.wfile.flush()
            next_beat = time.monotonic() + SSE_HEARTBEAT_INTERVAL
            while True:
                time.sleep(SSE_POLL_INTERVAL)
                for event in stream_events_since(slugs, last, registry):
                    seq = int(event.get("_seq", last + 1))
                    self.wfile.write(format_sse(event, seq).encode("utf-8"))
                    last = max(last, seq)
                if time.monotonic() >= next_beat:
                    self.wfile.write(b": heartbeat\n\n")
                    next_beat = time.monotonic() + SSE_HEARTBEAT_INTERVAL
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass

    def _handle(self) -> None:
        raw_path = urlsplit(self.path).path
        route, slug = match_route(self.command, raw_path)
        if route is None:
            # Auth first so scanners cannot probe the route table unauthenticated.
            if not check_bearer(self.headers,  # type: ignore[arg-type]
                                _resolve_tokens_db(
                                    getattr(self.server, "registry", DEFAULT_REGISTRY),
                                    getattr(self.server, "tokens_db", None))):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "unauthorized"}')
                return
            self._send_json(404, {"error": "not found"})
            return
        required_scope = "read" if self.command == "GET" else "write"
        ok, forbidden = bearer_auth(
            self.headers,  # type: ignore[arg-type]
            slug=slug, scope=required_scope if slug is not None else None,
            registry=getattr(self.server, "registry", DEFAULT_REGISTRY),
            tokens_db=getattr(self.server, "tokens_db", None),
        )
        if not ok:
            # Drain the request body first: closing with it unread makes the kernel reset the
            # connection, and a POST client then sees ECONNRESET instead of the 401/403.
            # The read is capped because the caller is unauthenticated; a larger body is
            # left unread and the connection closed, which is the old behaviour.
            try:
                length = max(0, int(self.headers.get("Content-Length", "0") or "0"))
                if length <= _UNAUTH_DRAIN_LIMIT:
                    self.rfile.read(length)
                else:
                    self.close_connection = True
            except (ValueError, OSError):
                pass
            self.send_response(403 if forbidden else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error": "forbidden for this project/scope"}' if forbidden
                else b'{"error": "unauthorized"}')
            return
        if self.command == "GET" and route == "/api/stream":
            query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            registry = getattr(self.server, "registry", DEFAULT_REGISTRY)
            self._serve_stream(query, registry)
            return
        if self.command == "GET" and route.startswith("/api/"):
            query = dict(parse_qsl(urlsplit(self.path).query, keep_blank_values=True))
            registry = getattr(self.server, "registry", DEFAULT_REGISTRY)
            sessions_db = getattr(self.server, "sessions_db", None)
            try:
                visible = (allowed_slugs(
                    self.headers,  # type: ignore[arg-type]
                    registry,
                    getattr(self.server, "tokens_db", None))
                    if route == "/api/projects" else None)
                status, payload = handle_get(
                    route, slug, query, registry, sessions_db, visible)
            except FileNotFoundError as e:
                self._send_json(404, {"error": f"not found: {e.filename or e}"})
                return
            except json.JSONDecodeError as e:
                self._send_json(500, {"error": f"invalid JSON: {e}"})
                return
            self._send_json(status, payload)
            return
        if self.command == "POST" and route.startswith("/api/"):
            registry = getattr(self.server, "registry", DEFAULT_REGISTRY)
            sessions_db = getattr(self.server, "sessions_db", None)
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                self._send_json(400, {"error": f"invalid JSON body: {e}"})
                return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return
            try:
                status, payload = handle_post(route, slug, body, registry, sessions_db)
            except FileNotFoundError as e:
                self._send_json(404, {"error": f"not found: {e.filename or e}"})
                return
            except json.JSONDecodeError as e:
                self._send_json(500, {"error": f"invalid JSON: {e}"})
                return
            self._send_json(status, payload)
            return
        self.send_response(501)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error": "not implemented yet"}')

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, *args: object) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    """Bind the core API and serve until interrupted."""
    args = build_parser().parse_args(argv)
    # Threading: each SSE stream holds its connection open in the poll loop,
    # so a single-threaded server would wedge all other reads/writes behind it.
    # _WRITE_LOCK + FileLock still serialize the write path across threads.
    server = ThreadingHTTPServer((args.bind, args.port), _Handler)
    server.daemon_threads = True
    server.registry = args.registry  # type: ignore[attr-defined]
    server.sessions_db = sessions_db_for_registry(  # type: ignore[attr-defined]
        args.registry, args.sessions_db)
    server.tokens_db = tokens_db_for_registry(  # type: ignore[attr-defined]
        args.registry, args.tokens_db)
    print(f"✓ Serving TideTable core API on {args.bind}:{args.port}")
    print(f"  registry: {args.registry}")
    print(f"  sessions: {server.sessions_db}")
    print(f"  tokens: {server.tokens_db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
