"""
`tidetable tui` — launch the ratatui front end (`tui/`, binary `tidetable-tui`).

This module only finds the binary and builds one command line; it never reads or writes a
todo file, and the TUI itself never writes one either (it shells out to the CLI or talks to a
hub). The Rust crate is not part of the wheel, so the binary comes from a checkout build or
from PATH.

Hub mode resolves the project slug inside the binary, in this order: the positional
argument, `$TIDETABLE_PROJECT`, a `slug` key in the nearest `.tidetable/config.json`, then
a project picker. This launcher forwards arguments untouched and never guesses a slug.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BIN = "tidetable-tui"
MODES = ("auto", "standard", "side", "bottom")
#: tmux split geometry per mode: (orientation flag, size).
SPLITS = {"side": ("-h", "40"), "bottom": ("-v", "10")}
BUILD_HINT = "cargo build --release --manifest-path tui/Cargo.toml"


class TuiError(Exception):
    """A launch problem, already phrased for the user."""


def find_binary(env=None, which=shutil.which, root: Path | None = None) -> str:
    """$TIDETABLE_TUI, then PATH, then the checkout's cargo output."""
    env = os.environ if env is None else env
    explicit = (env.get("TIDETABLE_TUI") or "").strip()
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise TuiError(f"$TIDETABLE_TUI points at {explicit!r}, which is not a file")
    found = which(BIN)
    if found:
        return found
    root = root or Path(__file__).resolve().parent.parent
    exe = BIN + (".exe" if sys.platform == "win32" else "")
    for profile in ("release", "debug"):
        cand = root / "tui" / "target" / profile / exe
        if cand.is_file():
            return str(cand)
    raise TuiError(
        f"{BIN} is not built. From a TideTable checkout run:\n  {BUILD_HINT}\n"
        f"then put tui/target/release on PATH or set $TIDETABLE_TUI to the binary."
    )


def build_command(binary: str, args, env=None) -> list[str]:
    """The argv to run. With --tmux inside tmux that is a `tmux split-window ...`."""
    env = os.environ if env is None else env
    mode = args.mode
    forwarded = [a for a in (args.tui_args or []) if a != "--"]
    tmux = bool(args.tmux) and bool(env.get("TMUX"))
    if tmux and mode == "auto":
        mode = "side"  # a pane that is split off is a side pane unless asked otherwise
    inner = [binary]
    if mode != "auto":
        inner += ["--mode", mode]
    inner += forwarded
    if tmux and mode in SPLITS:
        flag, size = SPLITS[mode]
        return ["tmux", "split-window", flag, "-l", size, *inner]
    return inner


def cmd_tui(args) -> int:
    try:
        binary = find_binary()
    except TuiError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.tmux and not os.environ.get("TMUX"):
        print("Note: --tmux needs a tmux session; running in this terminal.", file=sys.stderr)
    cmd = build_command(binary, args)
    try:
        return subprocess.call(cmd)
    except OSError as e:
        print(f"Error: cannot start {cmd[0]}: {e}", file=sys.stderr)
        return 1


def add_parser(sub) -> None:
    p = sub.add_parser("tui", help="Open the terminal UI (needs the tidetable-tui binary)")
    p.add_argument("--mode", choices=MODES, default="auto",
                   help="Layout: auto follows the pane shape (default)")
    p.add_argument("--tmux", action="store_true",
                   help="Inside tmux, split a pane (side: 40 columns, bottom: 10 rows) instead of running in place")
    p.add_argument("tui_args", nargs=argparse.REMAINDER, metavar="-- TUI_ARGS",
                   help="Forwarded to tidetable-tui, e.g. -- docs/todo.json --read-only")
    p.set_defaults(func=cmd_tui)
