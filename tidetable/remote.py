"""tidetable.remote — run CLI commands against a `tidetable serve` hub.

With `--server URL` (or `$TIDETABLE_SERVER`) the `todo_file` positional is a
project **slug** on the hub, not a path, and the command becomes one HTTP call
to the single-writer core API. This module maps argv to routes and prints what
the server returned; it holds no DAG, claim, or lock logic (see AGENTS.md —
`serve.py` owns that, over `cli.py`).

Output is the server's JSON payload, indented, whether or not `--json` was
given (only `sidebar` without `--json` renders text): remote mode is for agents. Exit codes follow the local commands.
Stdlib only (`urllib`); the token is `--token` / `$TIDETABLE_TOKEN`.
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

from tidetable.jsonio import env_get

DEFAULT_TIMEOUT = 30

#: command name -> (HTTP method, route suffix after /api/<slug>/)
ROUTES = {
    "validate": ("GET", "validate"),
    "next": ("GET", "next"),
    "ready": ("GET", "ready"),
    "list": ("GET", "list"),
    "parallel": ("GET", "parallel"),
    "deps": ("GET", "deps"),
    "history": ("GET", "history"),
    "conflicts": ("GET", "conflicts"),
    "look-queue": ("GET", "look-queue"),
    "sidebar": ("GET", "sidebar"),
    "start": ("POST", "start"),
    "done": ("POST", "done"),
    "reset": ("POST", "reset"),
    "status": ("POST", "status"),
    "add": ("POST", "add"),
    "add-phase": ("POST", "add-phase"),
    "edit": ("POST", "edit"),
    "look-defer": ("POST", "look-defer"),
    "look-clear": ("POST", "look-clear"),
}

#: writes that carry a base: the hub event count the caller decided against.
BASED = {"start", "done", "reset", "status", "edit", "look-defer", "look-clear"}

#: commands that take a todo file but have no hub route.
LOCAL_ONLY = {"convert", "release"}


class RemoteError(Exception):
    """A transport or HTTP failure, already phrased for the user."""


def server_url(args) -> str:
    return (getattr(args, "server", None) or env_get("SERVER")).strip().rstrip("/")


def token(args) -> str:
    return (getattr(args, "token", None) or env_get("TOKEN")).strip()


def _owner(args) -> str:
    # The server's default_owner() would name *its* host:pid, so always send ours.
    from tidetable.cli import default_owner

    return getattr(args, "owner", None) or default_owner()


def build_request(args) -> tuple[str, str, dict]:
    """(method, path-with-query, body) for one parsed command. Pure, for tests."""
    method, suffix = ROUTES[args.command]
    path = f"/api/{urllib.parse.quote(args.todo_file, safe='')}/{suffix}"
    cmd = args.command
    query: dict[str, str] = {}
    body: dict = {}
    extras = {"host": socket.gethostname()}

    if cmd == "next":
        query["owner"] = _owner(args)
        if args.no_resume:
            query["resume"] = "0"
        if args.disjoint:
            query["disjoint"] = "1"
    elif cmd == "ready":
        if args.disjoint:
            query["disjoint"] = "1"
    elif cmd == "sidebar":
        query["width"] = str(args.width)
        query["height"] = str(args.height)
    elif cmd == "deps":
        query["task"] = args.task_id
    elif cmd == "history":
        if args.task_id:
            query["task"] = args.task_id
        query["limit"] = str(args.limit)
    elif cmd == "start":
        body = {"task": args.task_id, "owner": _owner(args), "force": bool(args.force), **extras}
        if args.lease:
            body["lease"] = args.lease
    elif cmd == "done":
        body = {"task": args.task_id, "owner": _owner(args), "force": bool(args.force), **extras}
        if args.changelog is not None:
            body["changelog"] = args.changelog
    elif cmd == "reset":
        body = {"task": args.task_id, "owner": _owner(args), **extras}
        if getattr(args, "force", False):
            body["force"] = True
    elif cmd == "status":
        body = {"task": args.task_id, "status": args.status, "owner": _owner(args), **extras}
        if args.moved_to:
            body["moved_to"] = args.moved_to
    elif cmd == "add":
        body = {"phase": args.phase, "title": args.title, "owner": _owner(args)}
        for key, value in (("goal", args.goal), ("complexity", args.complexity),
                           ("agent", args.agent), ("parallel_group", args.parallel_group),
                           ("prerequisites", args.prereq), ("touches", args.touches),
                           ("success", args.success)):
            if value:
                body[key] = value
    elif cmd == "add-phase":
        body = {"phase": args.phase, "title": args.title, "owner": _owner(args)}
        for key, value in (("goal", args.goal), ("prerequisites", args.prereq),
                           ("after", args.after), ("before", args.before)):
            if value:
                body[key] = value
    elif cmd == "edit":
        body = {"task": args.task_id, "owner": _owner(args), **extras}
        for key in ("title", "goal", "tests", "complexity", "agent", "parallel_group"):
            value = getattr(args, key)
            if value is not None:
                body[key] = value
        for key, values in (("touches", args.touches), ("success", args.success),
                            ("prerequisites", args.prereq)):
            if values is not None:
                body[key] = values
        for key, flag in (("touches", args.clear_touches),
                          ("success", args.clear_success),
                          ("prerequisites", args.clear_prereq),
                          ("tests", args.clear_tests),
                          ("parallel_group", args.clear_parallel_group)):
            if flag:
                body[key] = None
    elif cmd == "look-defer":
        body = {"task": args.task_id, "owner": _owner(args), **extras}
        if args.note is not None:
            body["note"] = args.note
    elif cmd == "look-clear":
        body = {"task": args.task_id, "owner": _owner(args),
                "reject": bool(args.reject), **extras}

    if cmd in BASED and getattr(args, "base", None) is not None:
        body["base"] = args.base

    if query:
        path += "?" + urllib.parse.urlencode(query)
    return method, path, body


def fetch_base(base_url: str, slug: str, bearer: str) -> int:
    """The hub's current event count for `slug`, for a write decided right now."""
    path = f"/api/{urllib.parse.quote(slug, safe='')}/base"
    status, payload = request(base_url, "GET", path, {}, bearer)
    if status != 200 or not isinstance(payload.get("base"), int):
        message = payload.get("error") or f"HTTP {status}"
        raise RemoteError(f"cannot read the hub base for {slug}: {message}")
    return payload["base"]


def request(base: str, method: str, path: str, body: dict, bearer: str,
            timeout: int = DEFAULT_TIMEOUT) -> tuple[int, dict]:
    """One HTTP round trip. Returns (status, decoded JSON object)."""
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if method == "POST":
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise RemoteError(f"cannot reach {base}: {reason}") from e
    try:
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RemoteError(f"{base}{path}: HTTP {status} with a non-JSON body") from None
    if not isinstance(payload, dict):
        raise RemoteError(f"{base}{path}: HTTP {status} with an unexpected body")
    return status, payload


def exit_code(cmd: str, status: int, payload: dict) -> int:
    """Mirror the local commands' exit codes from the server's answer."""
    if cmd == "next":
        return 0 if status == 200 and payload.get("task") else 1
    if cmd == "ready":
        return 0 if status == 200 and (payload.get("ready") or payload.get("in_progress")) else 1
    if cmd == "validate":
        return 0 if status == 200 and payload.get("ok") else 1
    return 0 if 200 <= status < 300 else 1


def run(args) -> int:
    base = server_url(args)
    if not base:
        raise RemoteError("no server configured (--server or $TIDETABLE_SERVER)")
    if not base.startswith(("http://", "https://")):
        raise RemoteError(f"server must be an http(s) URL, got {base!r}")
    method, path, body = build_request(args)
    if args.command in BASED and "base" not in body:
        # No --base: this call is being decided now, so now is its base. A queued
        # write must not take this path; it passes the base it saw with --base.
        body["base"] = fetch_base(base, args.todo_file, token(args))
    status, payload = request(base, method, path, body, token(args))
    if status == 409 and payload.get("conflict"):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"Error: conflict: task {payload.get('task')} changed on the hub since base "
              f"{payload.get('base')} (now {payload.get('current')}); the write was not "
              f"applied and is on the conflict list (`tidetable conflicts {args.todo_file}`).",
              file=sys.stderr)
        return 1
    # Reads that legitimately answer "nothing" carry their payload on a 404/422.
    expected_shape = (args.command == "next" and "task" in payload) or \
        (args.command == "validate" and "ok" in payload)
    if status >= 400 and not expected_shape:
        message = payload.get("error") or f"HTTP {status}"
        print(f"Error: {message}", file=sys.stderr)
        if status in (401, 403) and not token(args):
            print("Hint: set --token or $TIDETABLE_TOKEN.", file=sys.stderr)
        return 1
    if args.command == "sidebar" and not args.json:
        # The harness panels ask for --json; a human gets the same text a local run prints.
        from tidetable import sidebar

        print(sidebar.render_sidebar(payload, args.width), end="")
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    return exit_code(args.command, status, payload)


def active(args) -> bool:
    """Remote mode is on when a server is configured and `--local` is absent."""
    return bool(server_url(args)) and not getattr(args, "local", False) \
        and getattr(args, "command", None) in ROUTES


def reject_if_local_only(args) -> str | None:
    """Message when a configured server meets a command that has no hub route."""
    if getattr(args, "local", False) or not server_url(args):
        return None
    if getattr(args, "command", None) in LOCAL_ONLY:
        return (f"`{args.command}` has no remote mode; run it against a local "
                f"checkout (or pass --local).")
    return None


__all__ = ["ROUTES", "RemoteError", "active", "build_request", "exit_code", "run"]
