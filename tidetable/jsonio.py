"""JSON reads, atomic writes, the file lock, and the append-only event log.

Parallel agents are the point of this tool, so every write goes through here:
a temp file in the same directory followed by an atomic rename, under a lock.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

#: A lock this old whose holder cannot be confirmed alive is presumed abandoned.
#: Every legitimate hold is one command long — sub-second — so this is generous.
DEFAULT_STALE_AFTER = 300.0


class LockTimeout(RuntimeError):
    """Another process holds the lock and did not release it in time."""


def _pid_alive(pid: int) -> bool | None:
    """True/False if we can tell whether `pid` is running, None if we cannot.

    Never use `os.kill(pid, 0)` for this on Windows: CPython implements
    `os.kill` there as `TerminateProcess(handle, sig)`, so the POSIX
    "signal 0 just probes" idiom would kill the process instead.
    """
    if pid <= 0:
        return None

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER means no such pid; anything else
                # (typically ERROR_ACCESS_DENIED) means it exists but is not ours.
                return False if ctypes.get_last_error() == 87 else None
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return None


class FileLock:
    """Cooperative lock on <target>.lock via exclusive create.

    A crashed holder used to leave the lock behind forever, so the lock file
    records who took it and this breaks it back open when that holder is gone:
    positively dead by pid, or older than `stale_after` when the pid cannot be
    checked (a different machine on a shared filesystem, a recycled pid).
    """

    def __init__(
        self,
        target: str | Path,
        timeout: float = 10.0,
        poll: float = 0.05,
        stale_after: float = DEFAULT_STALE_AFTER,
    ):
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout
        self.poll = poll
        self.stale_after = stale_after
        self._fd: int | None = None
        #: Set when this acquisition had to break an abandoned lock, so callers
        #: can report it rather than silently papering over a crashed agent.
        self.broke_stale_lock: str | None = None

    def holder(self) -> dict:
        """Whatever the lock file says about its holder. Empty when unreadable.

        Pre-0.5 locks hold a bare pid, so that shape is still understood.
        """
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
        # A bare pid is valid JSON, and is what pre-0.5 locks contain.
        return {"pid": parsed} if isinstance(parsed, int) else {}

    def _stale_reason(self) -> str | None:
        """Why the existing lock is abandoned, or None if it looks live."""
        try:
            age = max(0.0, time.time() - self.path.stat().st_mtime)
        except FileNotFoundError:
            return None

        info = self.holder()
        pid = info.get("pid")
        host = info.get("host")

        # A pid on another machine says nothing about a process on this one.
        same_host = host is None or host == socket.gethostname()
        if same_host and isinstance(pid, int):
            alive = _pid_alive(pid)
            if alive is False:
                return f"holder pid {pid} is gone"
            if alive is True:
                return None

        if age >= self.stale_after:
            return f"held for {age:.0f}s with no reachable holder"
        return None

    def _break(self, reason: str) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        self.broke_stale_lock = reason

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                os.write(self._fd, json.dumps(payload).encode("utf-8"))
                return self
            except FileExistsError:
                reason = self._stale_reason()
                if reason:
                    self._break(reason)
                    continue
                if time.monotonic() >= deadline:
                    info = self.holder()
                    held_by = ""
                    if info:
                        held_by = (f" (held by pid {info.get('pid', '?')} on "
                                   f"{info.get('host', '?')} since {info.get('at', '?')})")
                    raise LockTimeout(
                        f"could not acquire {self.path} within {self.timeout:g}s{held_by}; "
                        f"delete it if no other agent is running"
                    )
                time.sleep(self.poll)

    def __exit__(self, *exc_info) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def read_json(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _replace_with_retry(tmp: str, target: Path, attempts: int = 8, delay: float = 0.05) -> None:
    """os.replace, retried on Windows sharing violations.

    Windows refuses to replace a file another process has open (a reader, an editor,
    antivirus) with WinError 5 / 32. Those clear in milliseconds. A real permission
    problem still surfaces after the last attempt, about a second later.
    """
    for i in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2 ** min(i, 4)))


def write_json(data: dict, path: str | Path) -> None:
    """Write JSON atomically: temp file in the same dir, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        _replace_with_retry(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Append-only event log
# ---------------------------------------------------------------------------


LEGACY_ENV_PREFIX = "TASKERKEEPER_"
ENV_PREFIX = "TIDETABLE_"


def env_get(suffix: str, default: str = "") -> str:
    """`TIDETABLE_<suffix>`, falling back to the pre-rename `TASKERKEEPER_<suffix>`.

    The new name wins when both are set. The fallback keeps existing
    supervisors and shell profiles working through the rename.
    """
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        value = os.environ.get(prefix + suffix)
        if value is not None:
            return value
    return default


def events_path(todo_path: str | Path) -> Path:
    """The event log beside a todo file."""
    return Path(str(todo_path) + ".events.jsonl")


def events_enabled() -> bool:
    """`TIDETABLE_EVENTS=0` turns the log off."""
    return env_get("EVENTS", "1").strip().lower() not in ("0", "false", "no")


def append_event(todo_path: str | Path, event: dict) -> None:
    """Append one event. Never raises — losing an audit line must not fail a write.

    JSONL rather than an array inside the todo file: appending is one syscall,
    the todo file stays small and diffable, and a crash mid-append truncates one
    line instead of corrupting the roadmap.
    """
    if not events_enabled():
        return
    try:
        path = events_path(todo_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def conflicts_path(todo_path: str | Path) -> Path:
    """The hub's conflict list beside a todo file: writes it refused as stale."""
    return Path(str(todo_path) + ".conflicts.jsonl")


def append_conflict(todo_path: str | Path, entry: dict) -> None:
    """Append one refused write. Append-only: a person resolves conflicts, nothing replays them.

    Unlike `append_event` this ignores `TIDETABLE_EVENTS=0` and does not swallow
    errors: the conflict list is the only record of a write the hub threw away.
    """
    path = conflicts_path(todo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_conflicts(todo_path: str | Path) -> list[dict]:
    """Every recorded conflict, oldest first."""
    return _read_jsonl(conflicts_path(todo_path))


def read_events(todo_path: str | Path) -> list[dict]:
    """Every event, oldest first. Unparseable lines are skipped, not fatal."""
    return _read_jsonl(events_path(todo_path))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out
