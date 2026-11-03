"""tidetable register-project: add a project to the hub registry, in one command.

    tidetable register-project oarlock --name Oarlock \\
        --repo-url https://github.com/ClawfficeOrg/Oarlock \\
        --server https://tidetable.compewtertutor.com

Against a hub (``--server``) the entry is added by the hub's admin endpoint
(``POST /api/admin/projects``), which needs the hub's ``TK_API_TOKEN``: pass it
with ``--admin-token`` or set it in the environment. Without ``--server`` the
entry is written straight into the registry file (run that on the hub host).

The todo file is not created here: it lives at ``todo_path`` and must exist
before the project has tasks to run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from tidetable.jsonio import FileLock
from tidetable.serve import DEFAULT_REGISTRY

ADMIN_PATH = "/api/admin/projects"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_project(body: object, known_slugs: set[str]) -> tuple[dict, str]:
    """Check a new registry entry. Returns (clean entry, "") or ({}, reason)."""
    if not isinstance(body, dict):
        return {}, "body must be a JSON object"
    slug = body.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        return {}, "slug must be lowercase letters, digits and dashes, 1-63 characters"
    if slug in known_slugs:
        return {}, f"project slug {slug!r} is already registered"
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}, "name must be a non-empty string"
    repo_url = body.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.startswith("https://"):
        return {}, "repo_url must be an https URL"
    todo_path = body.get("todo_path") or f"projects/{slug}/todo.json"
    if not isinstance(todo_path, str) or todo_path.startswith("/") or ".." in todo_path.split("/"):
        return {}, "todo_path must be a relative path inside the projects directory"
    default_branch = body.get("default_branch") or "main"
    if not isinstance(default_branch, str) or not default_branch.strip():
        return {}, "default_branch must be a non-empty string"
    entry = {
        "slug": slug,
        "name": name.strip(),
        "repo_url": repo_url,
        "todo_path": todo_path,
        "default_branch": default_branch.strip(),
    }
    return entry, ""


def add_project(registry: str, body: object) -> tuple[dict, str]:
    """Append a validated entry to the registry file. Returns (entry, "") or ({}, reason).

    The file is written in place, not replaced: a single-file bind mount in
    Docker cannot be renamed over, so ``os.replace`` would fail on the hub.
    """
    path = Path(registry)
    with FileLock(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        projects = data.get("projects", [])
        entry, reason = validate_project(body, {str(p.get("slug")) for p in projects})
        if reason:
            return {}, reason
        projects.append(entry)
        data["projects"] = projects
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    return entry, ""


def register_remote(server: str, admin_token: str, body: dict) -> dict:
    """Ask the hub's admin endpoint to add the entry; return what it stored."""
    url = server.rstrip("/") + ADMIN_PATH
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"hub refused the project: HTTP {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {server}: {e.reason}") from None


def add_parser(sub) -> None:
    """Register ``tidetable register-project`` on the top-level CLI."""
    p = sub.add_parser(
        "register-project",
        help="Add a project to the hub registry (against a hub or the local file)",
        description="Add a project to deploy/registry.json, on the hub or locally.",
    )
    p.add_argument("slug", help="Project slug, lowercase, e.g. oarlock")
    p.add_argument("--name", required=True, help="Display name, e.g. Oarlock")
    p.add_argument("--repo-url", required=True, help="https URL of the project's repository")
    p.add_argument("--todo-path", default=None,
                   help="Todo file under the projects directory (default: projects/<slug>/todo.json)")
    p.add_argument("--default-branch", default="main", help="Branch runs cut from (default: main)")
    p.add_argument("--server", default=None,
                   help="Hub URL (default: $TIDETABLE_SERVER; without one, edit the local registry)")
    p.add_argument("--admin-token", default=None,
                   help="The hub's TK_API_TOKEN (default: $TK_API_TOKEN)")
    p.add_argument("--registry", default=DEFAULT_REGISTRY,
                   help="Local registry file (default: deploy/registry.json)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Validate and add the project, then say what was stored."""
    body = {
        "slug": args.slug,
        "name": args.name,
        "repo_url": args.repo_url,
        "todo_path": args.todo_path,
        "default_branch": args.default_branch,
    }
    server = args.server or os.environ.get("TIDETABLE_SERVER", "").strip() or None
    if server:
        admin = args.admin_token or os.environ.get("TK_API_TOKEN", "").strip()
        if not admin:
            raise SystemExit("set TK_API_TOKEN (the hub's admin token) or pass --admin-token")
        entry = register_remote(server, admin, body)
    else:
        entry, reason = add_project(args.registry, body)
        if reason:
            raise SystemExit(reason)
    print(f"registered {entry['slug']} ({entry['name']}): todo at {entry['todo_path']}")
    return 0
