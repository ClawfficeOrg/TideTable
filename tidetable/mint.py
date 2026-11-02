"""tidetable mint-token: make a machine token for one project, in one command.

    tidetable mint-token chiton --scope read --scope write --days 90 \
        --server https://tidetable.compewtertutor.com --save

Against a hub (``--server``) the token is minted by the hub's admin endpoint
(``POST /api/admin/tokens``), which needs the hub's ``TK_API_TOKEN``: pass it
with ``--admin-token`` or set it in the environment. Without ``--server`` the
token is minted straight into the local tokens database (run that on the hub
host, e.g. ``docker exec tidetable-core tidetable mint-token ...``).

The token is printed once. ``--save`` also stores ``TIDETABLE_SERVER`` and
``TIDETABLE_TOKEN`` as user environment variables (Windows: ``setx``), so the
secret never has to be copied by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from tidetable import tokens as tokens_store
from tidetable.serve import DEFAULT_REGISTRY

ALLOWED_SCOPES = ("read", "write", "*")
DEFAULT_DAYS = 90
ADMIN_PATH = "/api/admin/tokens"


def registry_slugs(registry: str) -> set[str]:
    """Slugs in the registry file, so a typo cannot mint a token for nothing."""
    data = json.loads(Path(registry).read_text(encoding="utf-8"))
    return {str(p["slug"]) for p in data.get("projects", []) if p.get("slug")}


def validate_request(body: object, known_slugs: set[str] | None) -> tuple[dict, str]:
    """Check a mint request. Returns (clean request, "") or ({}, reason).

    ``known_slugs`` None skips the registry check (used when the hub checks it).
    """
    if not isinstance(body, dict):
        return {}, "body must be a JSON object"
    slugs = body.get("slugs")
    if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) and s for s in slugs):
        return {}, "slugs must be a non-empty list of project slugs"
    scopes = body.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        return {}, "scopes must be a non-empty list: read, write or *"
    bad = [s for s in scopes if s not in ALLOWED_SCOPES]
    if bad:
        return {}, f"unknown scope {bad[0]!r}; use read, write or *"
    days = body.get("days", DEFAULT_DAYS)
    if days is not None and (not isinstance(days, (int, float)) or isinstance(days, bool) or days <= 0):
        return {}, "days must be a positive number, or null for no expiry"
    if known_slugs is not None:
        unknown = [s for s in slugs if s not in known_slugs]
        if unknown:
            return {}, f"unknown project slug {unknown[0]!r}; add it to the registry first"
    return {"slugs": list(slugs), "scopes": list(scopes), "days": days}, ""


def mint_local(tokens_db: str, request: dict) -> str:
    """Mint into the local tokens database and return the token (shown once)."""
    tokens_store.init_db(tokens_db)
    return tokens_store.mint(tokens_db, request["slugs"], request["scopes"], request["days"])


def mint_remote(server: str, admin_token: str, request: dict) -> str:
    """Ask the hub's admin endpoint to mint; return the token it shows once."""
    url = server.rstrip("/") + ADMIN_PATH
    data = json.dumps(request).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"hub refused the mint: HTTP {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {server}: {e.reason}") from None
    token = payload.get("token")
    if not token:
        raise SystemExit("hub answered without a token; see the hub log")
    return str(token)


def save_user_env(server: str | None, token: str) -> list[str]:
    """Store the server and token as user environment variables. Returns what was set."""
    pairs = [("TIDETABLE_TOKEN", token)]
    if server:
        pairs.insert(0, ("TIDETABLE_SERVER", server))
    if sys.platform == "win32":
        for name, value in pairs:
            subprocess.run(["setx", name, value], check=True, capture_output=True)
        return [name for name, _ in pairs]
    # Elsewhere: print the lines to add to the shell profile instead of guessing one.
    for name, value in pairs:
        print(f'export {name}="{value}"')
    return []


def add_parser(sub) -> None:
    """Register ``tidetable mint-token`` on the top-level CLI."""
    p = sub.add_parser(
        "mint-token",
        help="Mint a machine token for one project (against a hub or locally)",
        description="Mint a per-project machine token. The token is shown once.",
    )
    p.add_argument("slug", help="Project slug from the registry, e.g. chiton")
    p.add_argument("--scope", action="append", dest="scopes", default=None,
                   help="read, write or * (repeatable; default: read and write)")
    p.add_argument("--days", type=float, default=DEFAULT_DAYS,
                   help=f"Expiry in days, 0 for no expiry (default: {DEFAULT_DAYS})")
    p.add_argument("--server", default=None,
                   help="Hub URL, e.g. https://tidetable.compewtertutor.com "
                        "(default: $TIDETABLE_SERVER; without one, mint locally)")
    p.add_argument("--admin-token", default=None,
                   help="The hub's TK_API_TOKEN (default: $TK_API_TOKEN)")
    p.add_argument("--registry", default=DEFAULT_REGISTRY,
                   help="Local registry, used to check the slug (default: deploy/registry.json)")
    p.add_argument("--tokens-db", default=None,
                   help="Local tokens database (default: beside the registry, or $TK_TOKENS_DB)")
    p.add_argument("--save", action="store_true",
                   help="Also store TIDETABLE_SERVER and TIDETABLE_TOKEN as user env vars")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Mint the token, print it once, and optionally save it to the user environment."""
    scopes = args.scopes or ["read", "write"]
    days = None if args.days == 0 else args.days
    server = args.server or os.environ.get("TIDETABLE_SERVER", "").strip() or None
    if args.save and not server:
        raise SystemExit("--save needs a hub: pass --server or set TIDETABLE_SERVER")

    if server:
        # The hub checks the slug itself, so a local registry is not needed.
        request, reason = validate_request(
            {"slugs": [args.slug], "scopes": scopes, "days": days}, None)
        if reason:
            raise SystemExit(reason)
        admin = args.admin_token or os.environ.get("TK_API_TOKEN", "").strip()
        if not admin:
            raise SystemExit("set TK_API_TOKEN (the hub's admin token) or pass --admin-token")
        token = mint_remote(server, admin, request)
    else:
        request, reason = validate_request(
            {"slugs": [args.slug], "scopes": scopes, "days": days},
            registry_slugs(args.registry))
        if reason:
            raise SystemExit(reason)
        tokens_db = tokens_store.tokens_db_for_registry(args.registry, args.tokens_db)
        token = mint_local(tokens_db, request)

    expiry = "no expiry" if days is None else f"expires in {days:g} days"
    print(f"token for {args.slug} ({', '.join(scopes)}; {expiry}):")
    print(token)
    print("Shown once. Copy it now, or rerun with --save.")
    if args.save:
        saved = save_user_env(server, token)
        if saved:
            print(f"saved user env vars: {', '.join(saved)} (open a new terminal to see them)")
    return 0
