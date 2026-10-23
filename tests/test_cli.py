"""Tests for the TideTable CLI.

Stdlib unittest on purpose: `python -m unittest` works in a bare checkout with
no dev dependencies. pytest runs these too.
"""

from __future__ import annotations

try:  # before anything reads the environment; see tests/hermetic.py
    from . import hermetic  # python -m unittest tests.test_x
except ImportError:
    import hermetic  # python -m unittest discover -s tests

import io
import json
import os
import socket
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tidetable import agents, cli, jsonio, serve, sessions

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


def todo(*phases: dict) -> dict:
    return {
        "schema_version": "1.0.0",
        "project": {
            "name": "Test",
            "version": {"milestone": "v1", "release_version": "0.1.0"},
            "description": "fixture",
        },
        "phases": list(phases),
    }


def phase(pid: str, *tasks: dict, prerequisites: list[str] | None = None, **extra) -> dict:
    out = {"id": pid, "title": f"Phase {pid}", "goal": "goal", "tasks": list(tasks)}
    if prerequisites:
        out["prerequisites"] = prerequisites
    out.update(extra)
    return out


def task(tid: str, status: str = "pending", prerequisites: list[str] | None = None, **extra) -> dict:
    out = {"id": tid, "title": f"Task {tid}", "status": status}
    if prerequisites:
        out["prerequisites"] = prerequisites
    out.update(extra)
    return out


class TempTodo:
    """A todo file on disk that the CLI can mutate."""

    def __init__(self, data: dict):
        self.data = data
        self._tmp = TemporaryDirectory()

    def __enter__(self) -> str:
        self.path = Path(self._tmp.name) / "todo.json"
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        return str(self.path)

    def __exit__(self, *exc):
        self._tmp.cleanup()


def run(*argv: str) -> tuple[int, str]:
    """Invoke the CLI, capturing exit code and stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, buf.getvalue()


def reload(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


class SchemaPackagingTest(unittest.TestCase):
    def test_schema_is_package_data(self):
        # The bug this guards: SCHEMA_PATH pointed outside the package, so any
        # non-editable install could not validate anything.
        self.assertTrue(cli.SCHEMA_PATH.is_file(), cli.SCHEMA_PATH)
        self.assertEqual(cli.SCHEMA_PATH.parent.parent.name, "tidetable")

    def test_examples_validate(self):
        for example in sorted(EXAMPLES.glob("*.json")):
            with self.subTest(example=example.name):
                code, _ = run("validate", str(example))
                self.assertEqual(code, 0)


class EligibilityTest(unittest.TestCase):
    def test_task_prerequisites_gate(self):
        data = todo(phase("1.0", task("1.0.1", "done"), task("1.0.2", prerequisites=["1.0.1"]),
                          task("1.0.3", prerequisites=["1.0.2"])))
        self.assertEqual([t["id"] for t in cli.ready_tasks(data)], ["1.0.2"])

    def test_phase_prerequisites_gate(self):
        # 1.1.1 has no task prereqs, but its phase requires 1.0, which is not done.
        data = todo(
            phase("1.0", task("1.0.1", "pending")),
            phase("1.1", task("1.1.1"), prerequisites=["1.0"]),
        )
        self.assertEqual([t["id"] for t in cli.ready_tasks(data)], ["1.0.1"])

    def test_phase_completes_when_all_tasks_terminal(self):
        data = todo(
            phase("1.0", task("1.0.1", "done"), task("1.0.2", "cancelled")),
            phase("1.1", task("1.1.1"), prerequisites=["1.0"]),
        )
        self.assertEqual([t["id"] for t in cli.ready_tasks(data)], ["1.1.1"])

    def test_ready_orders_numerically(self):
        data = todo(phase("1.0", task("1.0.10"), task("1.0.9"), task("1.0.2")))
        self.assertEqual([t["id"] for t in cli.ready_tasks(data)], ["1.0.2", "1.0.9", "1.0.10"])

    def test_next_resumes_in_progress(self):
        data = todo(phase("1.0", task("1.0.1", "in_progress"), task("1.0.2")))
        self.assertEqual(cli.find_next(data)["id"], "1.0.1")
        self.assertEqual(cli.find_next(data, resume=False)["id"], "1.0.2")

    def test_blocked_report_names_the_blocker(self):
        data = todo(
            phase("1.0", task("1.0.1")),
            phase("1.1", task("1.1.1", prerequisites=["1.0.1"]), prerequisites=["1.0"]),
        )
        blocked = {b["id"]: b["blocked_by"] for b in cli.blocked_report(data)}
        self.assertEqual(blocked["1.1.1"], ["task 1.0.1", "phase 1.0"])


class ValidationTest(unittest.TestCase):
    def errors(self, data: dict) -> list[str]:
        return cli.semantic_errors(data)[0]

    def warnings(self, data: dict) -> list[str]:
        return cli.semantic_errors(data)[1]

    def test_dangling_prerequisite(self):
        data = todo(phase("1.0", task("1.0.1", prerequisites=["9.9.9"])))
        self.assertIn("task 1.0.1 requires unknown task 9.9.9", self.errors(data))

    def test_dangling_phase_prerequisite(self):
        data = todo(phase("1.0", task("1.0.1"), prerequisites=["0.9"]))
        self.assertIn("phase 1.0 requires unknown phase 0.9", self.errors(data))

    def test_duplicate_task_id(self):
        data = todo(phase("1.0", task("1.0.1"), task("1.0.1")))
        self.assertIn("duplicate task id: 1.0.1", self.errors(data))

    def test_task_id_must_match_its_phase(self):
        data = todo(phase("1.0", task("1.0.1")), phase("1.1", task("1.0.2")))
        self.assertTrue(any("implies phase 1.0" in e for e in self.errors(data)))

    def test_cycle_detected(self):
        data = todo(phase("1.0",
                          task("1.0.1", prerequisites=["1.0.3"]),
                          task("1.0.2", prerequisites=["1.0.1"]),
                          task("1.0.3", prerequisites=["1.0.2"])))
        self.assertTrue(any(e.startswith("prerequisite cycle") for e in self.errors(data)))

    def test_self_prerequisite(self):
        data = todo(phase("1.0", task("1.0.1", prerequisites=["1.0.1"])))
        self.assertIn("task 1.0.1 requires itself", self.errors(data))

    def test_moved_requires_target(self):
        data = todo(phase("1.0", task("1.0.1", "moved")))
        self.assertIn("task 1.0.1 is moved but has no moved_to", self.errors(data))

    def test_prerequisite_on_cancelled_task_warns(self):
        data = todo(phase("1.0", task("1.0.1", "cancelled"), task("1.0.2", prerequisites=["1.0.1"])))
        self.assertEqual(self.errors(data), [])
        self.assertTrue(any("blocked forever" in w for w in self.warnings(data)))

    def test_valid_file_is_clean(self):
        data = todo(phase("1.0", task("1.0.1", "done"), task("1.0.2", prerequisites=["1.0.1"])))
        self.assertEqual(cli.semantic_errors(data), ([], []))


class MutationTest(unittest.TestCase):
    def test_start_claims_task(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, _ = run("start", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["status"], "in_progress")

    def test_start_refuses_blocked_task(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            code, out = run("start", path, "1.0.2")
            self.assertEqual(code, 1)
            self.assertIn("blocked by", out)
            self.assertEqual(reload(path)["phases"][0]["tasks"][1]["status"], "pending")

    def test_start_force_overrides(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            code, _ = run("start", path, "1.0.2", "--force")
            self.assertEqual(code, 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][1]["status"], "in_progress")

    def test_reset_unsticks_orphaned_in_progress(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "in_progress")))) as path:
            code, _ = run("reset", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["status"], "pending")

    def test_look_defer_unblocks_dependents_and_queues(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "in_progress"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            self.assertEqual(run("look-defer", path, "1.0.1", "--note", "eyeball the divider")[0], 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["status"], "needs_human_look")
            self.assertEqual(run("validate", path)[0], 0)
            self.assertIn("1.0.2", run("ready", path)[1])
            code, out = run("look-queue", path)
            self.assertIn("1.0.1", out)
            self.assertIn("eyeball the divider", out)
            self.assertEqual(run("look-clear", path, "1.0.1")[0], 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["status"], "done")
            self.assertIn("empty", run("look-queue", path)[1])

    def test_look_queue_verbose_lists_success_criteria(self):
        t = task("1.0.1", "needs_human_look", human_look="See the task's success criteria",
                 success=["divider is 1px", "drag resizes"])
        with TempTodo(todo(phase("1.0", t))) as path:
            _, plain = run("look-queue", path)
            self.assertIn("success criteria", plain)
            self.assertNotIn("divider is 1px", plain)
            _, out = run("look-queue", path, "-v")
            self.assertIn("[ ] divider is 1px", out)
            self.assertIn("[ ] drag resizes", out)
            self.assertNotIn("success criteria", out.lower())
            queue = json.loads(run("look-queue", path, "--json")[1])["queue"]
            self.assertEqual(queue[0]["success"], ["divider is 1px", "drag resizes"])

    def test_look_clear_reject_returns_to_pending(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "needs_human_look")))) as path:
            self.assertEqual(run("look-clear", path, "1.0.1", "--reject")[0], 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["status"], "pending")

    def test_done_refuses_unmet_prerequisites(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            code, out = run("done", path, "1.0.2")
            self.assertEqual(code, 1)
            self.assertIn("unmet prerequisites", out)
            self.assertEqual(reload(path)["phases"][0]["tasks"][1]["status"], "pending")

    def test_done_reports_unblocked(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            code, out = run("done", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertIn("1.0.2", out)

    def test_done_collects_changelog_entry(self):
        data = todo(phase("1.0", task("1.0.1"),
                          release={"version": "0.1.0", "tag_on_complete": True}))
        with TempTodo(data) as path:
            run("done", path, "1.0.1", "--changelog", "Added the thing")
            entries = reload(path)["phases"][0]["release"]["changelog_entries"]
            self.assertEqual(entries, ["Added the thing"])

    def test_changelog_files_under_last_releasing_phase(self):
        data = todo(
            phase("1.0", task("1.0.1")),
            phase("1.1", task("1.1.1"), release={"version": "0.1.0"}),
        )
        with TempTodo(data) as path:
            run("done", path, "1.0.1", "--changelog", "Groundwork")
            entries = reload(path)["phases"][1]["release"]["changelog_entries"]
            self.assertEqual(entries, ["Groundwork"])

    def test_status_moved_requires_existing_target(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            self.assertEqual(run("status", path, "1.0.1", "moved", "--moved-to", "9.9.9")[0], 1)
            self.assertEqual(run("status", path, "1.0.1", "moved", "--moved-to", "1.0.2")[0], 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["moved_to"], "1.0.2")

    def test_timestamps_use_z_suffix(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("done", path, "1.0.1")
            self.assertTrue(reload(path)["phases"][0]["tasks"][0]["updated_at"].endswith("Z"))


class AddTest(unittest.TestCase):
    def test_add_accepts_full_task_shape(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, _ = run("add", path, "--phase", "1.0", "--title", "New",
                          "--goal", "do it", "--prereq", "1.0.1",
                          "--complexity", "High", "--agent", "pro_dev_agent",
                          "--parallel-group", "grp", "--touches", "src/a.py",
                          "--success", "it works")
            self.assertEqual(code, 0)
            new = reload(path)["phases"][0]["tasks"][-1]
            self.assertEqual(new["id"], "1.0.2")
            self.assertEqual(new["prerequisites"], ["1.0.1"])
            self.assertEqual(new["complexity"], "High")
            self.assertEqual(new["parallel_group"], "grp")
            self.assertEqual(new["touches"], ["src/a.py"])

    def test_add_rejects_unknown_prerequisite(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, out = run("add", path, "--phase", "1.0", "--title", "New", "--prereq", "9.9.9")
            self.assertEqual(code, 1)
            self.assertIn("does not exist", out)

    def test_add_survives_non_numeric_ids(self):
        # int(id.split(".")[-1]) used to crash the whole command here.
        with TempTodo(todo(phase("1.0", task("1.0.a")))) as path:
            code, _ = run("add", path, "--phase", "1.0", "--title", "New")
            self.assertEqual(code, 0)
            self.assertEqual(reload(path)["phases"][0]["tasks"][-1]["id"], "1.0.1")


class EditTest(unittest.TestCase):
    def test_edit_changes_only_named_fields_and_keeps_claims(self):
        with TempTodo(todo(phase(
            "1.0",
            task("1.0.1", status="in_progress", goal="old", touches=["old/"],
                 claimed_by="a", claimed_at="2026-10-17T00:00:00Z",
                 lease_expires_at="2026-10-17T01:00:00Z"),
            task("1.0.2"),
        ))) as path:
            code, out = run("edit", path, "1.0.1",
                            "--title", "Better", "--goal", "new goal",
                            "--touches", "a/", "--touches", "b/",
                            "--success", "it works", "--tests", "make test",
                            "--complexity", "High", "--agent", "flagship",
                            "--parallel-group", "grp", "--prereq", "1.0.2")
            self.assertEqual(code, 0, out)
            edited = reload(path)["phases"][0]["tasks"][0]
            self.assertEqual(edited["title"], "Better")
            self.assertEqual(edited["goal"], "new goal")
            self.assertEqual(edited["touches"], ["a/", "b/"])
            self.assertEqual(edited["success"], ["it works"])
            self.assertEqual(edited["tests"], "make test")
            self.assertEqual(edited["complexity"], "High")
            self.assertEqual(edited["agent"], "flagship")
            self.assertEqual(edited["parallel_group"], "grp")
            self.assertEqual(edited["prerequisites"], ["1.0.2"])
            self.assertEqual(edited["status"], "in_progress")
            self.assertEqual(edited["claimed_by"], "a")
            self.assertEqual(edited["lease_expires_at"], "2026-10-17T01:00:00Z")
            self.assertTrue(edited["updated_at"].endswith("Z"))

    def test_clear_flags_empty_lists_and_remove_optional_scalars(self):
        with TempTodo(todo(phase("1.0", task(
                "1.0.1", prerequisites=["1.0.2"], touches=["x/"],
                success=["ok"], tests="make", parallel_group="grp"),
                task("1.0.2")))) as path:
            code, out = run("edit", path, "1.0.1", "--clear-touches",
                            "--clear-success", "--clear-prereq",
                            "--clear-tests", "--clear-parallel-group")
            self.assertEqual(code, 0, out)
            task_out = reload(path)["phases"][0]["tasks"][0]
            self.assertEqual(task_out["touches"], [])
            self.assertEqual(task_out["success"], [])
            self.assertEqual(task_out["prerequisites"], [])
            self.assertNotIn("tests", task_out)
            self.assertNotIn("parallel_group", task_out)

    def test_nothing_to_edit_is_an_error(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, out = run("edit", path, "1.0.1")
            self.assertEqual(code, 1)
            self.assertIn("nothing to edit", out)

    def test_dangling_or_self_prerequisite_is_refused_without_writing(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            code, out = run("edit", path, "1.0.1", "--title", "Nope",
                            "--prereq", "9.9.9")
            self.assertEqual(code, 1)
            self.assertIn("does not exist", out)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["title"], "Task 1.0.1")
            code, out = run("edit", path, "1.0.1", "--prereq", "1.0.1")
            self.assertEqual(code, 1)
            self.assertIn("cannot require itself", out)

    def test_a_prerequisite_cycle_is_refused(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", prerequisites=["1.0.2"]),
                                 task("1.0.2")))) as path:
            code, out = run("edit", path, "1.0.2", "--prereq", "1.0.1")
            self.assertEqual(code, 1)
            self.assertIn("cycle", out.lower())
            self.assertNotIn("prerequisites", reload(path)["phases"][0]["tasks"][1])

    def test_edit_appends_an_event(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            self.assertEqual(run("edit", path, "1.0.1", "--goal", "g2")[0], 0)
            events = json.loads(run("history", path, "--task", "1.0.1", "--json")[1])["events"]
            edit = [e for e in events if e.get("event") == "edit"][-1]
            self.assertEqual(edit["task"], "1.0.1")
            self.assertEqual(edit["fields"], ["goal"])


class AddPhaseTest(unittest.TestCase):
    def test_add_phase_appends_by_default(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, out = run("add-phase", path, "--phase", "1.1",
                            "--title", "Next", "--goal", "more work",
                            "--prereq", "1.0")
            self.assertEqual(code, 0)
            self.assertIn("1.1", out)
            phases = reload(path)["phases"]
            self.assertEqual([p["id"] for p in phases], ["1.0", "1.1"])
            added = phases[-1]
            self.assertEqual(added["title"], "Next")
            self.assertEqual(added["goal"], "more work")
            self.assertEqual(added["prerequisites"], ["1.0"])
            self.assertEqual(added["tasks"], [])
            # A phase prerequisite gates: 1.1's first task waits on 1.0.
            run("add", path, "--phase", "1.1", "--title", "T")
            data = reload(path)
            self.assertIn("phase 1.0", cli.blocked_report(data)[0]["blocked_by"])

    def test_add_phase_after_and_before(self):
        data = todo(phase("1.0", task("1.0.1")), phase("1.2", task("1.2.1")))
        with TempTodo(data) as path:
            code, _ = run("add-phase", path, "--phase", "1.1",
                          "--title", "Mid", "--after", "1.0")
            self.assertEqual(code, 0)
            self.assertEqual([p["id"] for p in reload(path)["phases"]],
                             ["1.0", "1.1", "1.2"])
        with TempTodo(data) as path:
            code, _ = run("add-phase", path, "--phase", "1.1",
                          "--title", "Mid", "--before", "1.2")
            self.assertEqual(code, 0)
            self.assertEqual([p["id"] for p in reload(path)["phases"]],
                             ["1.0", "1.1", "1.2"])

    def test_add_phase_rejects_bad_id_dupe_and_dangling(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            self.assertEqual(run("add-phase", path, "--phase", "nope",
                                 "--title", "X")[0], 1)
            self.assertEqual(run("add-phase", path, "--phase", "1.0",
                                 "--title", "X")[0], 1)
            self.assertEqual(run("add-phase", path, "--phase", "1.1",
                                 "--title", "X", "--prereq", "9.9")[0], 1)
            self.assertEqual(run("add-phase", path, "--phase", "1.1",
                                 "--title", "X", "--after", "9.9")[0], 1)
            self.assertEqual(run("add-phase", path, "--phase", "1.1",
                                 "--title", "X", "--after", "1.0",
                                 "--before", "1.0")[0], 1)
            # Nothing half-written on any rejection.
            self.assertEqual([p["id"] for p in reload(path)["phases"]], ["1.0"])

    def test_add_phase_logs_event_and_validates(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, _ = run("add-phase", path, "--phase", "1.1", "--title", "Next")
            self.assertEqual(code, 0)
            events = jsonio.read_events(path)
            self.assertEqual(events[-1]["event"], "add-phase")
            self.assertEqual(events[-1]["phase"], "1.1")
            self.assertEqual(run("validate", path)[0], 0)

    def test_serve_add_phase(self):
        with TemporaryDirectory() as tmp:
            todo_path = str(Path(tmp) / "todo.json")
            Path(todo_path).write_text(
                json.dumps(todo(phase("1.0", task("1.0.1")))), encoding="utf-8")
            registry = str(Path(tmp) / "registry.json")
            Path(registry).write_text(json.dumps({"projects": [
                {"slug": "demo", "name": "Demo", "repo_url": "",
                 "todo_path": todo_path, "default_branch": "main"},
            ]}), encoding="utf-8")
            db = str(Path(tmp) / "sessions.db")
            status, payload = serve.handle_post(
                "/api/{slug}/add-phase", "demo",
                {"phase": "1.1", "title": "Next", "prerequisites": ["1.0"]},
                registry, db)
            self.assertEqual(status, 200)
            self.assertEqual(payload["id"], "1.1")
            phases = json.loads(Path(todo_path).read_text(encoding="utf-8"))["phases"]
            self.assertEqual([p["id"] for p in phases], ["1.0", "1.1"])
            dup, _ = serve.handle_post(
                "/api/{slug}/add-phase", "demo",
                {"phase": "1.1", "title": "Dup"}, registry, db)
            self.assertEqual(dup, 409)


class LookHubTest(unittest.TestCase):
    """serve.py's POST look-defer/look-clear routes, unit-level."""

    def _setup(self, tmp, data):
        todo_path = str(Path(tmp) / "todo.json")
        Path(todo_path).write_text(json.dumps(data), encoding="utf-8")
        registry = str(Path(tmp) / "registry.json")
        Path(registry).write_text(json.dumps({"projects": [
            {"slug": "demo", "name": "Demo", "repo_url": "",
             "todo_path": todo_path, "default_branch": "main"},
        ]}), encoding="utf-8")
        db = str(Path(tmp) / "sessions.db")
        return todo_path, registry, db

    def test_look_defer_queues_task_and_keeps_note(self):
        with TemporaryDirectory() as tmp:
            todo_path, registry, db = self._setup(
                tmp, todo(phase("1.0", task("1.0.1", "in_progress"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-defer", "demo",
                {"task": "1.0.1", "note": "check it", "owner": "agent"},
                registry, db)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["to"], "needs_human_look")
            data = json.loads(Path(todo_path).read_text(encoding="utf-8"))
            self.assertEqual(data["phases"][0]["tasks"][0]["status"], "needs_human_look")
            self.assertEqual(data["phases"][0]["tasks"][0]["human_look"], "check it")

    def test_look_defer_already_done_is_a_no_op(self):
        with TemporaryDirectory() as tmp:
            todo_path, registry, db = self._setup(
                tmp, todo(phase("1.0", task("1.0.1", "done"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-defer", "demo", {"task": "1.0.1"}, registry, db)
            self.assertEqual(status, 200)
            self.assertTrue(payload["already_done"])
            data = json.loads(Path(todo_path).read_text(encoding="utf-8"))
            self.assertEqual(data["phases"][0]["tasks"][0]["status"], "done")

    def test_look_defer_missing_task_is_404(self):
        with TemporaryDirectory() as tmp:
            _, registry, db = self._setup(tmp, todo(phase("1.0", task("1.0.1"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-defer", "demo", {"task": "9.9.9"}, registry, db)
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"], "task 9.9.9 not found")

    def test_look_clear_marks_done_and_drops_note(self):
        with TemporaryDirectory() as tmp:
            todo_path, registry, db = self._setup(
                tmp, todo(phase("1.0", task("1.0.1", "needs_human_look",
                                           human_look="check it"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-clear", "demo", {"task": "1.0.1"}, registry, db)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["to"], "done")
            data = json.loads(Path(todo_path).read_text(encoding="utf-8"))
            self.assertEqual(data["phases"][0]["tasks"][0]["status"], "done")
            self.assertNotIn("human_look", data["phases"][0]["tasks"][0])

    def test_look_clear_reject_returns_to_pending(self):
        with TemporaryDirectory() as tmp:
            todo_path, registry, db = self._setup(
                tmp, todo(phase("1.0", task("1.0.1", "needs_human_look"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-clear", "demo", {"task": "1.0.1", "reject": True},
                registry, db)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["to"], "pending")
            data = json.loads(Path(todo_path).read_text(encoding="utf-8"))
            self.assertEqual(data["phases"][0]["tasks"][0]["status"], "pending")

    def test_look_clear_refuses_task_not_in_queue(self):
        with TemporaryDirectory() as tmp:
            _, registry, db = self._setup(tmp, todo(phase("1.0", task("1.0.1"))))
            status, payload = serve.handle_post(
                "/api/{slug}/look-clear", "demo", {"task": "1.0.1"}, registry, db)
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"], "task 1.0.1 is pending, not needs_human_look")


class OutputTest(unittest.TestCase):
    def test_next_json_is_parseable(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, out = run("next", path, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["task"]["id"], "1.0.1")

    def test_ready_json_lists_every_runnable_task(self):
        data = todo(phase("1.0", task("1.0.1"), task("1.0.2"), task("1.0.3", prerequisites=["1.0.1"])))
        with TempTodo(data) as path:
            _, out = run("ready", path, "--json")
            self.assertEqual([t["id"] for t in json.loads(out)["ready"]], ["1.0.1", "1.0.2"])

    def test_ready_exits_nonzero_when_nothing_to_do(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "done")))) as path:
            self.assertEqual(run("ready", path, "--json")[0], 1)

    def test_list_and_deps_json(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "done"), task("1.0.2", prerequisites=["1.0.1"])))) as path:
            _, out = run("list", path, "--json")
            self.assertEqual(json.loads(out)["summary"]["done"], 1)
            _, out = run("deps", path, "1.0.2", "--json")
            self.assertEqual(json.loads(out)["prerequisites"][0]["status"], "done")

    def test_convert_renders_markdown(self):
        with TempTodo(todo(phase("1.0", task("1.0.1", "done")))) as path:
            code, out = run("convert", path)
            self.assertEqual(code, 0)
            self.assertIn("## Phase 1.0", out)
            self.assertIn("- [x] **1.0.1**", out)


class AgentConfigTest(unittest.TestCase):
    """Tier -> provider/model resolution across the config layers."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()  # resolve(): agents.py resolves start dirs
        self.user_dir = self.root / "userconf"
        self.repo_dir = self.root / "repo"
        (self.repo_dir / ".git").mkdir(parents=True)
        # Keep the real home directory and any real repo config out of the test.
        patch = mock.patch.dict(os.environ, {"TIDETABLE_CONFIG_HOME": str(self.user_dir)})
        patch.start()
        self.addCleanup(patch.stop)

    def write_todo(self, data: dict) -> str:
        path = self.repo_dir / "todo.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return str(path)

    def test_defaults_cover_every_tier_the_schema_allows(self):
        schema = json.loads(cli.SCHEMA_PATH.read_text(encoding="utf-8"))
        allowed = set(schema["$defs"]["task"]["properties"]["agent"]["enum"])
        self.assertEqual(set(agents.DEFAULT_TIERS), allowed)
        for tier, config in agents.DEFAULT_TIERS.items():
            with self.subTest(tier=tier):
                self.assertTrue(config["provider"] and config["model"])

    def test_user_config_overrides_builtin(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "set", "mid_dev_agent", "--model", "claude-opus-5")
        tiers, sources = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["mid_dev_agent"]["model"], "claude-opus-5")
        self.assertEqual(sources["mid_dev_agent"]["model"], agents.LAYER_USER)
        # provider was not set at the user layer, so it still comes from defaults
        self.assertEqual(sources["mid_dev_agent"]["provider"], agents.LAYER_BUILTIN)

    def test_repo_config_overrides_user(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "set", "mid_dev_agent", "--model", "user-model")
        run("agents", "set", "mid_dev_agent", "--model", "repo-model",
            "--scope", "repo", "--todo", path)
        tiers, sources = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["mid_dev_agent"]["model"], "repo-model")
        self.assertEqual(sources["mid_dev_agent"]["model"], agents.LAYER_REPO)

    def test_todo_config_overrides_repo(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "set", "mid_dev_agent", "--model", "repo-model",
            "--scope", "repo", "--todo", path)
        run("agents", "set", "mid_dev_agent", "--model", "todo-model",
            "--scope", "todo", "--todo", path)
        tiers, sources = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["mid_dev_agent"]["model"], "todo-model")
        self.assertEqual(sources["mid_dev_agent"]["model"], agents.LAYER_TODO)

    def test_task_override_beats_every_layer(self):
        tiers = {"mid_dev_agent": {"provider": "anthropic", "model": "claude-sonnet-5"}}
        pinned = task("1.0.1", agent="mid_dev_agent", model="claude-opus-5")
        self.assertEqual(agents.resolve_task(pinned, tiers)["model"], "claude-opus-5")
        # and the provider it did not override still comes from the tier
        self.assertEqual(agents.resolve_task(pinned, tiers)["provider"], "anthropic")

    def test_unknown_tier_resolves_to_no_model(self):
        # The tier is still reported back — a supervisor needs to know which
        # tier it failed to resolve — but nothing is invented for it.
        resolved = agents.resolve_task(task("1.0.1", agent="nope"), {})
        self.assertEqual(resolved, {"agent": "nope", "agent_derived": False})

    def test_arbitrary_settings_pass_through(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="pro_dev_agent"))))
        run("agents", "set", "pro_dev_agent", "--option", "effort=xhigh")
        tiers, _ = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["pro_dev_agent"]["effort"], "xhigh")

    def test_set_rejects_malformed_option(self):
        code, out = run("agents", "set", "pro_dev_agent", "--option", "effort")
        self.assertEqual(code, 1)
        self.assertIn("key=value", out)

    def test_set_with_no_settings_is_an_error(self):
        self.assertEqual(run("agents", "set", "pro_dev_agent")[0], 1)

    def test_unset_removes_a_key_then_the_tier(self):
        run("agents", "set", "mid_dev_agent", "--model", "m", "--provider", "p")
        run("agents", "unset", "mid_dev_agent", "--key", "model")
        stored = agents.read_tiers(agents.user_config_path())
        self.assertEqual(stored["mid_dev_agent"], {"provider": "p"})
        run("agents", "unset", "mid_dev_agent")
        self.assertNotIn("mid_dev_agent", agents.read_tiers(agents.user_config_path()))

    def test_unset_reports_when_there_is_nothing_to_remove(self):
        self.assertEqual(run("agents", "unset", "mid_dev_agent")[0], 1)

    def test_todo_scope_needs_a_todo_file(self):
        code, out = run("agents", "set", "mid_dev_agent", "--model", "m", "--scope", "todo")
        self.assertEqual(code, 1)
        self.assertIn("needs a todo file", out)

    def test_missing_config_file_is_not_an_error(self):
        self.assertEqual(agents.read_tiers(self.root / "nope" / "agents.json"), {})

    def test_ready_json_carries_the_resolved_model(self):
        path = self.write_todo(todo(phase("1.0",
                                          task("1.0.1", agent="mid_dev_agent"),
                                          task("1.0.2", agent="pro_dev_agent",
                                               model="pinned-model"))))
        run("agents", "set", "mid_dev_agent", "--model", "tier-model")
        _, out = run("ready", path, "--json")
        resolved = {t["id"]: t["model"] for t in json.loads(out)["ready"]}
        self.assertEqual(resolved["1.0.1"], "tier-model")
        self.assertEqual(resolved["1.0.2"], "pinned-model")

    def test_next_json_carries_the_resolved_model(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="basic_dev_agent"))))
        _, out = run("next", path, "--json")
        self.assertEqual(json.loads(out)["task"]["provider"], "anthropic")
        self.assertEqual(json.loads(out)["task"]["model"],
                         agents.DEFAULT_TIERS["basic_dev_agent"]["model"])

    def test_show_json_reports_sources_and_overrides(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent",
                                                      model="pinned"))))
        _, out = run("agents", "show", "--todo", path, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["tiers"]["mid_dev_agent"]["_sources"]["model"],
                         agents.LAYER_BUILTIN)
        self.assertEqual(payload["overrides"], [{"id": "1.0.1", "model": "pinned"}])

    def test_path_prints_the_scope_file(self):
        _, out = run("agents", "path", "--scope", "user")
        self.assertEqual(out.strip(), str(agents.user_config_path()))

    def test_repo_path_lands_at_the_git_root(self):
        nested = self.repo_dir / "docs" / "sub"
        nested.mkdir(parents=True)
        found = agents.repo_config_path(nested / "todo.json")
        self.assertEqual(found, self.repo_dir / ".tidetable" / "agents.json")

    def test_every_preset_covers_every_tier(self):
        schema = json.loads(cli.SCHEMA_PATH.read_text(encoding="utf-8"))
        allowed = set(schema["$defs"]["task"]["properties"]["agent"]["enum"])
        for name, preset in agents.PROVIDER_PRESETS.items():
            with self.subTest(provider=name):
                self.assertEqual(set(preset), allowed)
                for tier, config in preset.items():
                    self.assertEqual(config["provider"], name, tier)
                    self.assertTrue(config["model"], tier)

    def test_use_switches_every_tier_at_once(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="pro_dev_agent"))))
        run("agents", "use", "opencode-go")
        tiers, sources = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["pro_dev_agent"], {"provider": "opencode-go",
                                                  "model": "deepseek-v4-pro"})
        self.assertEqual(tiers["flagship"]["model"], "qwen3.8-max")
        self.assertEqual(tiers["basic_dev_agent"]["model"], "glm-5.3-flash")
        self.assertEqual(tiers["mid_dev_agent"]["model"], "glm-5.3-flash")
        self.assertEqual(sources["pro_dev_agent"]["model"], f"{agents.LAYER_USER} preset")

    def test_hand_set_tier_beats_the_preset_in_the_same_scope(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "use", "opencode-go")
        run("agents", "set", "mid_dev_agent", "--model", "glm-5.3-pro")
        tiers, sources = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["mid_dev_agent"]["model"], "glm-5.3-pro")
        self.assertEqual(sources["mid_dev_agent"]["model"], agents.LAYER_USER)
        # the tier it did not touch still comes from the preset
        self.assertEqual(tiers["flagship"]["model"], "qwen3.8-max")

    def test_repo_preset_beats_a_user_tier(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="flagship"))))
        run("agents", "set", "flagship", "--model", "user-model")
        run("agents", "use", "opencode-go", "--scope", "repo", "--todo", path)
        tiers, _ = agents.resolve_tiers(json.loads(Path(path).read_text()), path)
        self.assertEqual(tiers["flagship"]["model"], "qwen3.8-max")

    def test_todo_preset_beats_the_repo_preset(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="flagship"))))
        run("agents", "use", "opencode-go", "--scope", "repo", "--todo", path)
        run("agents", "use", "anthropic", "--scope", "todo", "--todo", path)
        data = json.loads(Path(path).read_text())
        self.assertEqual(agents.active_provider(data, path), ("anthropic", agents.LAYER_TODO))
        tiers, _ = agents.resolve_tiers(data, path)
        self.assertEqual(tiers["flagship"]["model"],
                         agents.PROVIDER_PRESETS["anthropic"]["flagship"]["model"])

    def test_default_provider_when_nothing_selects_one(self):
        self.assertEqual(agents.active_provider(), (agents.DEFAULT_PROVIDER,
                                                    agents.LAYER_BUILTIN))

    def test_use_rejects_an_unknown_preset(self):
        code, out = run("agents", "use", "not-a-provider")
        self.assertEqual(code, 1)
        self.assertIn("opencode-go", out)
        self.assertEqual(agents.read_config(agents.user_config_path()), {})

    def test_use_needs_a_provider_or_clear(self):
        self.assertEqual(run("agents", "use")[0], 1)

    def test_clear_restores_the_default_preset(self):
        run("agents", "use", "opencode-go")
        code, _ = run("agents", "use", "--clear")
        self.assertEqual(code, 0)
        self.assertEqual(agents.active_provider()[0], agents.DEFAULT_PROVIDER)
        # clearing twice reports that there was nothing to clear
        self.assertEqual(run("agents", "use", "--clear")[0], 1)

    def test_providers_json_lists_presets_and_the_active_one(self):
        run("agents", "use", "opencode-go")
        _, out = run("agents", "providers", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["active"], "opencode-go")
        self.assertEqual(set(payload["presets"]), set(agents.PROVIDER_PRESETS))

    def test_ready_json_reflects_the_active_preset(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="flagship"))))
        run("agents", "use", "opencode-go")
        _, out = run("ready", path, "--json")
        entry = json.loads(out)["ready"][0]
        self.assertEqual((entry["provider"], entry["model"]), ("opencode-go", "qwen3.8-max"))

    def test_show_json_reports_the_active_preset(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "use", "opencode-go", "--scope", "repo", "--todo", path)
        _, out = run("agents", "show", "--todo", path, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["provider"], "opencode-go")
        self.assertEqual(payload["provider_source"], agents.LAYER_REPO)

    def test_configured_todo_still_validates(self):
        path = self.write_todo(todo(phase("1.0", task("1.0.1", agent="mid_dev_agent"))))
        run("agents", "set", "mid_dev_agent", "--model", "claude-opus-5",
            "--scope", "todo", "--todo", path)
        run("agents", "use", "opencode-go", "--scope", "todo", "--todo", path)
        self.assertEqual(run("validate", path)[0], 0)


class ConcurrencyTest(unittest.TestCase):
    def test_write_is_atomic_and_leaves_no_temp_files(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("done", path, "1.0.1")
            # The event log is a deliberate sibling; a *.tmp is a failed write.
            expected = {"todo.json", "todo.json.events.jsonl"}
            leftovers = [p.name for p in Path(path).parent.iterdir() if p.name not in expected]
            self.assertEqual(leftovers, [])

    def test_lock_blocks_a_second_writer(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            with cli.FileLock(path):
                with self.assertRaises(cli.LockTimeout):
                    with cli.FileLock(path, timeout=0.1):
                        pass
            # Released on exit, so the next writer succeeds.
            self.assertEqual(run("start", path, "1.0.1")[0], 0)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Claims and leases
# ---------------------------------------------------------------------------


class ClaimTest(unittest.TestCase):
    """`start` records who holds a task, so two agents cannot work one task."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"TIDETABLE_OWNER": "agent-a"})
        self._env.start()
        self.addCleanup(self._env.stop)

    @staticmethod
    def as_owner(name: str):
        return mock.patch.dict(os.environ, {"TIDETABLE_OWNER": name})

    def test_start_records_owner_and_lease(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, _ = run("start", path, "1.0.1")
            self.assertEqual(code, 0)
            claimed = reload(path)["phases"][0]["tasks"][0]
            self.assertEqual(claimed["claimed_by"], "agent-a")
            self.assertIn("claimed_at", claimed)
            self.assertTrue(claimed["lease_expires_at"].endswith("Z"))

    def test_second_agent_cannot_take_a_live_claim(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("start", path, "1.0.1")
            with self.as_owner("agent-b"):
                code, out = run("start", path, "1.0.1")
            self.assertEqual(code, 1)
            self.assertIn("held by agent-a", out)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["claimed_by"], "agent-a")

    def test_owner_restarting_refreshes_its_own_lease(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("start", path, "1.0.1")
            code, out = run("start", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertIn("lease refreshed", out)

    def test_expired_lease_can_be_reclaimed(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("start", path, "1.0.1")
            expire(path, "1.0.1")
            with self.as_owner("agent-b"):
                code, out = run("start", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertIn("Reclaimed", out)
            self.assertEqual(reload(path)["phases"][0]["tasks"][0]["claimed_by"], "agent-b")

    def test_next_does_not_hand_out_another_agents_live_task(self):
        # The whole point of claiming: a second agent must get different work.
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            run("start", path, "1.0.1")
            with self.as_owner("agent-b"):
                code, out = run("next", path, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["task"]["id"], "1.0.2")

    def test_next_resumes_the_callers_own_claim(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            run("start", path, "1.0.1")
            code, out = run("next", path, "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["task"]["id"], "1.0.1")
            self.assertTrue(payload["resumed"])

    def test_next_resumes_an_expired_claim_from_anyone(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            run("start", path, "1.0.1")
            expire(path, "1.0.1")
            with self.as_owner("agent-b"):
                code, out = run("next", path, "--json")
            self.assertEqual(json.loads(out)["task"]["id"], "1.0.1")

    def test_done_refuses_someone_elses_live_claim_without_force(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("start", path, "1.0.1")
            with self.as_owner("agent-b"):
                code, out = run("done", path, "1.0.1")
                self.assertEqual(code, 1)
                self.assertIn("held by agent-a", out)
                self.assertEqual(run("done", path, "1.0.1", "--force")[0], 0)

    def test_terminal_status_clears_the_claim(self):
        for command in (("done",), ("reset",), ("status", "cancelled")):
            with self.subTest(command=command[0]):
                with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
                    run("start", path, "1.0.1")
                    run(command[0], path, "1.0.1", *command[1:])
                    finished = reload(path)["phases"][0]["tasks"][0]
                    for field in cli.CLAIM_FIELDS:
                        self.assertNotIn(field, finished)

    def test_a_claim_with_no_lease_field_is_treated_as_expired(self):
        # Files written by a pre-lease `start` must not deadlock forever.
        legacy = task("1.0.1", status="in_progress", claimed_by="ghost")
        with TempTodo(todo(phase("1.0", legacy))) as path:
            with self.as_owner("agent-b"):
                self.assertEqual(run("start", path, "1.0.1")[0], 0)


def expire(path: str, task_id: str) -> None:
    """Push a task's lease into the past, standing in for a crashed agent."""
    data = reload(path)
    for phase_data in data["phases"]:
        for item in phase_data["tasks"]:
            if item["id"] == task_id:
                item["lease_expires_at"] = "2020-01-01T00:00:00Z"
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Owned-path conflicts
# ---------------------------------------------------------------------------


class DisjointTest(unittest.TestCase):
    def test_paths_conflict_covers_directories(self):
        self.assertTrue(cli.paths_conflict("src/auth.rs", "src/auth.rs"))
        self.assertTrue(cli.paths_conflict("./src/auth.rs", "src/auth.rs"))
        self.assertTrue(cli.paths_conflict("src\\auth.rs", "src/auth.rs"))
        self.assertTrue(cli.paths_conflict("src/", "src/auth.rs"))
        self.assertTrue(cli.paths_conflict("src/routes/handlers.rs", "src/routes"))
        self.assertFalse(cli.paths_conflict("src/auth.rs", "src/authz.rs"))
        self.assertFalse(cli.paths_conflict("src/auth.rs", "docs/auth.rs"))
        self.assertFalse(cli.paths_conflict("", "src/auth.rs"))

    def test_ready_warns_when_owned_paths_overlap(self):
        with TempTodo(todo(phase(
            "1.0",
            task("1.0.1", touches=["src/auth.rs"]),
            task("1.0.2", touches=["src/auth.rs"]),
        ))) as path:
            code, out = run("ready", path)
            self.assertEqual(code, 0)
            self.assertIn("Owned paths overlap", out)
            self.assertIn("1.0.1 ~ 1.0.2", out)

    def test_disjoint_keeps_the_lowest_id_and_defers_the_rest(self):
        with TempTodo(todo(phase(
            "1.0",
            task("1.0.1", touches=["src/routes/"]),
            task("1.0.2", touches=["src/routes/handlers.rs"]),
            task("1.0.3", touches=["docs/readme.md"]),
        ))) as path:
            code, out = run("ready", path, "--disjoint", "--json")
            payload = json.loads(out)
            self.assertEqual(code, 0)
            self.assertEqual([t["id"] for t in payload["ready"]], ["1.0.1", "1.0.3"])
            self.assertEqual(payload["deferred"][0]["id"], "1.0.2")
            self.assertEqual(payload["deferred"][0]["conflicts_with"], ["1.0.1"])

    def test_in_progress_tasks_hold_their_paths(self):
        with TempTodo(todo(phase(
            "1.0",
            task("1.0.1", status="in_progress", touches=["src/auth.rs"]),
            task("1.0.2", touches=["src/auth.rs"]),
            task("1.0.3", touches=["docs/readme.md"]),
        ))) as path:
            payload = json.loads(run("ready", path, "--disjoint", "--json")[1])
            self.assertEqual([t["id"] for t in payload["ready"]], ["1.0.3"])

    def test_next_disjoint_skips_work_colliding_with_in_progress(self):
        with TempTodo(todo(phase(
            "1.0",
            task("1.0.1", status="in_progress", touches=["src/auth.rs"]),
            task("1.0.2", touches=["src/auth.rs"]),
            task("1.0.3", touches=["docs/readme.md"]),
        ))) as path:
            with mock.patch.dict(os.environ, {"TIDETABLE_OWNER": "agent-b"}):
                payload = json.loads(
                    run("next", path, "--disjoint", "--no-resume", "--json")[1])
            self.assertEqual(payload["task"]["id"], "1.0.3")

    def test_tasks_without_touches_never_conflict(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            payload = json.loads(run("ready", path, "--disjoint", "--json")[1])
            self.assertEqual([t["id"] for t in payload["ready"]], ["1.0.1", "1.0.2"])
            self.assertEqual(payload["conflicts"], [])


# ---------------------------------------------------------------------------
# Complexity -> tier
# ---------------------------------------------------------------------------


class ComplexityTierTest(unittest.TestCase):
    def test_range_parsing(self):
        self.assertEqual(agents.parse_complexity_range("Low-High"),
                         ["low", "medium", "high"])
        self.assertEqual(agents.parse_complexity_range("Very High"), ["very high"])
        self.assertEqual(agents.parse_complexity_range("Medium to High"),
                         ["medium", "high"])
        self.assertEqual(agents.parse_complexity_range("low, very high"),
                         ["low", "very high"])
        self.assertEqual(agents.parse_complexity_range(None), [])

    def test_complexity_picks_a_tier_when_no_agent_is_named(self):
        self.assertEqual(agents.effective_agent({"complexity": "Low"}), "basic_dev_agent")
        self.assertEqual(agents.effective_agent({"complexity": "Very High"}), "flagship")

    def test_an_explicit_agent_always_wins(self):
        self.assertEqual(
            agents.effective_agent({"complexity": "Low", "agent": "flagship"}), "flagship")

    def test_no_agent_and_no_complexity_falls_back(self):
        self.assertEqual(agents.effective_agent({}), agents.FALLBACK_TIER)

    def test_configured_complexity_range_overrides_the_default_map(self):
        tiers = {"flagship": {"complexity_range": "Low-High"}}
        self.assertEqual(agents.effective_agent({"complexity": "Low"}, tiers), "flagship")

    def test_resolved_output_reports_a_derived_tier(self):
        resolved = agents.resolve_task({"id": "1.0.1", "complexity": "Low"},
                                       agents.DEFAULT_TIERS)
        self.assertEqual(resolved["agent"], "basic_dev_agent")
        self.assertTrue(resolved["agent_derived"])
        self.assertEqual(resolved["model"], "claude-haiku-4-5")

    def test_add_derives_the_tier_from_complexity(self):
        with TempTodo(todo(phase("1.0"))) as path:
            run("add", path, "--phase", "1.0", "--title", "Hard thing",
                "--complexity", "Very High")
            added = reload(path)["phases"][0]["tasks"][0]
            self.assertEqual(added["agent"], "flagship")


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"TIDETABLE_OWNER": "agent-a"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_transitions_are_appended_not_overwritten(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            run("start", path, "1.0.1")
            run("reset", path, "1.0.1")
            run("start", path, "1.0.1")
            run("done", path, "1.0.1", "--changelog", "shipped it")
            events = jsonio.read_events(path)
            self.assertEqual([e["event"] for e in events],
                             ["start", "reset", "start", "done"])
            self.assertEqual(events[-1]["changelog"], "shipped it")
            self.assertTrue(all(e["owner"] == "agent-a" for e in events))

    def test_history_can_filter_to_one_task(self):
        with TempTodo(todo(phase("1.0", task("1.0.1"), task("1.0.2")))) as path:
            run("start", path, "1.0.1")
            run("start", path, "1.0.2")
            payload = json.loads(run("history", path, "--task", "1.0.2", "--json")[1])
            self.assertEqual([e["task"] for e in payload["events"]], ["1.0.2"])

    def test_history_is_empty_before_anything_happens(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            code, out = run("history", path)
            self.assertEqual(code, 1)
            self.assertIn("No events", out)

    def test_the_log_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"TIDETABLE_EVENTS": "0"}):
            with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
                run("start", path, "1.0.1")
                self.assertFalse(jsonio.events_path(path).exists())


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def releasing(*tasks: dict) -> dict:
    return phase("1.0", *tasks,
                 release={"version": "v0.1.0", "tag_on_complete": True,
                          "release_notes": "First cut."})


class ReleaseTest(unittest.TestCase):
    def test_done_reports_release_ready_only_when_the_phase_completes(self):
        with TempTodo(todo(releasing(task("1.0.1"), task("1.0.2")))) as path:
            first = json.loads(run("done", path, "1.0.1", "--json")[1])
            self.assertFalse(first["phase_complete"])
            self.assertFalse(first["release_ready"])
            last = json.loads(run("done", path, "1.0.2", "--json")[1])
            self.assertTrue(last["phase_complete"])
            self.assertTrue(last["release_ready"])
            self.assertEqual(last["release"]["version"], "v0.1.0")

    def test_done_never_creates_a_tag_by_itself(self):
        with TempTodo(todo(releasing(task("1.0.1")))) as path:
            with mock.patch("tidetable.cli.subprocess.run") as spawned:
                run("done", path, "1.0.1")
            spawned.assert_not_called()

    def test_release_refuses_an_incomplete_phase(self):
        with TempTodo(todo(releasing(task("1.0.1"), task("1.0.2")))) as path:
            run("done", path, "1.0.1")
            code, out = run("release", path)
            self.assertEqual(code, 1)
            self.assertIn("not complete", out)

    def test_release_reports_the_collected_changelog(self):
        with TempTodo(todo(releasing(task("1.0.1")))) as path:
            run("done", path, "1.0.1", "--changelog", "shipped it")
            payload = json.loads(run("release", path, "--json")[1])
            self.assertEqual(payload["version"], "v0.1.0")
            self.assertTrue(payload["complete"])
            self.assertEqual(payload["changelog_entries"], ["shipped it"])

    def test_tag_message_carries_notes_and_changelog(self):
        state = {"version": "v0.1.0", "release_notes": "First cut.",
                 "changelog_entries": ["a", "b"]}
        self.assertEqual(cli.tag_message(state), "First cut.\n\n- a\n- b\n")

    def test_tag_shells_out_only_with_the_flag(self):
        with TempTodo(todo(releasing(task("1.0.1")))) as path:
            run("done", path, "1.0.1", "--changelog", "shipped it")
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                # First call is the "does this tag exist" probe.
                code = 1 if "rev-parse" in argv else 0
                return subprocess.CompletedProcess(argv, code, "", "")

            with mock.patch("tidetable.cli.subprocess.run", fake_run):
                code, out = run("release", path, "--tag")
            self.assertEqual(code, 0)
            self.assertIn("Tagged v0.1.0", out)
            self.assertIn("tag", calls[-1])
            self.assertIn("v0.1.0", calls[-1])

    def test_tag_refuses_to_clobber_an_existing_tag(self):
        with TempTodo(todo(releasing(task("1.0.1")))) as path:
            run("done", path, "1.0.1")
            with mock.patch("tidetable.cli.subprocess.run",
                            return_value=subprocess.CompletedProcess([], 0, "abc123", "")):
                code, out = run("release", path, "--tag")
            self.assertEqual(code, 1)
            self.assertIn("already exists", out)


# ---------------------------------------------------------------------------
# Stale locks
# ---------------------------------------------------------------------------


class StaleLockTest(unittest.TestCase):
    def test_a_lock_left_by_a_dead_process_is_broken(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            # A pid that is not running, recorded as ours so the host matches.
            dead = json.dumps({"pid": 999999, "host": socket.gethostname(),
                               "at": "2020-01-01T00:00:00Z"})
            Path(path + ".lock").write_text(dead, encoding="utf-8")
            code, out = run("start", path, "1.0.1")
            self.assertEqual(code, 0)
            self.assertIn("stale lock", out)
            self.assertFalse(Path(path + ".lock").exists())

    def test_a_live_holder_is_respected(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            mine = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                               "at": "2020-01-01T00:00:00Z"})
            Path(path + ".lock").write_text(mine, encoding="utf-8")
            lock = jsonio.FileLock(path, timeout=0.1)
            with self.assertRaises(jsonio.LockTimeout):
                lock.__enter__()

    def test_an_unreachable_holder_ages_out(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            # No host match, so the pid says nothing: only age can decide.
            other = json.dumps({"pid": os.getpid(), "host": "some-other-machine",
                                "at": "2020-01-01T00:00:00Z"})
            Path(path + ".lock").write_text(other, encoding="utf-8")
            with jsonio.FileLock(path, timeout=0.1, stale_after=0.0) as lock:
                self.assertIsNotNone(lock.broke_stale_lock)

    def test_a_pre_lease_bare_pid_lock_is_still_understood(self):
        with TempTodo(todo(phase("1.0", task("1.0.1")))) as path:
            Path(path + ".lock").write_text("999999", encoding="utf-8")
            self.assertEqual(jsonio.FileLock(path).holder(), {"pid": 999999})

    def test_pid_probing_never_signals_the_process(self):
        # os.kill(pid, 0) terminates the process on Windows; this must not use it.
        with mock.patch("tidetable.jsonio.os.kill") as killed:
            jsonio._pid_alive(os.getpid())
        if os.name == "nt":
            killed.assert_not_called()


class TestSessions(unittest.TestCase):
    """Heartbeat store behind POST /api/<slug>/heartbeat (1.1.3)."""

    def test_upsert_then_list_shows_agent_not_stale(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "sessions.db")
            row = sessions.upsert_heartbeat(db, "worker-1", "demo", "1.0.1",
                                            repo="r", branch="main",
                                            host="h", model="m")
            self.assertEqual(row["agent_id"], "worker-1")
            self.assertEqual(row["task_id"], "1.0.1")
            self.assertFalse(row["stale"])
            listed = sessions.list_sessions(db, "demo")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["agent_id"], "worker-1")
            self.assertFalse(listed[0]["stale"])

    def test_aged_row_shows_stale(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "sessions.db")
            sessions.upsert_heartbeat(db, "worker-1", "demo", "1.0.1")
            import sqlite3

            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE sessions SET last_seen = ? WHERE agent_id = ?",
                             ("2020-01-01T00:00:00Z", "worker-1"))
                conn.commit()
            finally:
                conn.close()
            listed = sessions.list_sessions(db, "demo")
            self.assertTrue(listed[0]["stale"])

    def test_clear_task_keeps_row_drops_task(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "sessions.db")
            sessions.upsert_heartbeat(db, "worker-1", "demo", "1.0.1")
            cleared = sessions.clear_task(db, "worker-1")
            assert cleared is not None
            self.assertIsNone(cleared["task_id"])
            self.assertFalse(cleared["stale"])
            self.assertIsNone(sessions.clear_task(db, "unknown-agent"))

    def test_upsert_requires_agent_id(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                sessions.upsert_heartbeat(str(Path(tmp) / "s.db"), "  ")

    def test_list_scopes_by_slug(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "sessions.db")
            sessions.upsert_heartbeat(db, "a", "one")
            sessions.upsert_heartbeat(db, "b", "two")
            self.assertEqual([r["agent_id"] for r in sessions.list_sessions(db, "one")],
                             ["a"])
            self.assertEqual(len(sessions.list_sessions(db)), 2)
            self.assertEqual(sessions.list_sessions(db, "missing"), [])


class TestServeHeartbeat(unittest.TestCase):
    """Serve wiring: heartbeat upserts, start heartbeats, done/reset clears."""

    def _registry(self, tmp: str) -> tuple[str, str]:
        todo_path = str(Path(tmp) / "todo.json")
        Path(todo_path).write_text(
            json.dumps(todo(phase("1.0", task("1.0.1")))), encoding="utf-8")
        registry = str(Path(tmp) / "registry.json")
        Path(registry).write_text(json.dumps({"projects": [
            {"slug": "demo", "name": "Demo", "repo_url": "",
             "todo_path": todo_path, "default_branch": "main"},
        ]}), encoding="utf-8")
        return registry, str(Path(tmp) / "sessions.db")

    def test_heartbeat_post_upserts_session(self):
        with TemporaryDirectory() as tmp:
            registry, db = self._registry(tmp)
            status, payload = serve.handle_post(
                "/api/{slug}/heartbeat", "demo",
                {"agent_id": "worker-1", "task_id": "1.0.1", "branch": "main"},
                registry, db)
            self.assertEqual(status, 200)
            self.assertEqual(payload["session"]["agent_id"], "worker-1")
            self.assertEqual(payload["session"]["task_id"], "1.0.1")
            listed = sessions.list_sessions(db, "demo")
            self.assertEqual(len(listed), 1)

    def test_start_auto_heartbeats_done_clears(self):
        with TemporaryDirectory() as tmp:
            registry, db = self._registry(tmp)
            status, _ = serve.handle_post(
                "/api/{slug}/start", "demo", {"task": "1.0.1", "owner": "worker-1"},
                registry, db)
            self.assertEqual(status, 200)
            row = sessions.get_session(db, "worker-1")
            assert row is not None
            self.assertEqual(row["task_id"], "1.0.1")
            status, _ = serve.handle_post(
                "/api/{slug}/done", "demo", {"task": "1.0.1", "owner": "worker-1"},
                registry, db)
            self.assertEqual(status, 200)
            row = sessions.get_session(db, "worker-1")
            assert row is not None
            self.assertIsNone(row["task_id"])

    def test_reset_clears_task(self):
        with TemporaryDirectory() as tmp:
            registry, db = self._registry(tmp)
            serve.handle_post("/api/{slug}/start", "demo",
                              {"task": "1.0.1", "owner": "worker-1"}, registry, db)
            base = serve.handle_get("/api/{slug}/base", "demo", {}, registry, db)[1]["base"]
            status, _ = serve.handle_post(
                "/api/{slug}/reset", "demo",
                {"task": "1.0.1", "owner": "worker-1", "base": base}, registry, db)
            self.assertEqual(status, 200)
            row = sessions.get_session(db, "worker-1")
            assert row is not None
            self.assertIsNone(row["task_id"])

    def test_sessions_db_defaults_beside_registry(self):
        self.assertTrue(
            serve.sessions_db_for_registry("deploy/registry.json").endswith(
                "sessions.db"))
        with mock.patch.dict(os.environ, {"TK_SESSIONS_DB": "/srv/x.db"}):
            self.assertEqual(serve.sessions_db_for_registry("deploy/registry.json"),
                             "/srv/x.db")

    def test_sessions_get_lists_slug(self):
        with TemporaryDirectory() as tmp:
            registry, db = self._registry(tmp)
            sessions.upsert_heartbeat(db, "worker-1", "demo")
            status, payload = serve.handle_get(
                "/api/{slug}/sessions", "demo", {}, registry, db)
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["sessions"]), 1)


class TestHubBase(unittest.TestCase):
    """Hub writes carry a base; a stale one is refused with 409 onto the conflict list."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.todo_path = str(root / "todo.json")
        Path(self.todo_path).write_text(json.dumps(todo(
            phase("1.0", task("1.0.1"), task("1.0.2")))), encoding="utf-8")
        self.registry = str(root / "registry.json")
        Path(self.registry).write_text(json.dumps({"projects": [
            {"slug": "demo", "name": "Demo", "repo_url": "",
             "todo_path": self.todo_path, "default_branch": "main"}]}), encoding="utf-8")
        self.db = str(root / "sessions.db")

    def post(self, route: str, body: dict) -> tuple[int, dict]:
        return serve.handle_post(f"/api/{{slug}}/{route}", "demo", body, self.registry, self.db)

    def get(self, route: str) -> tuple[int, dict]:
        return serve.handle_get(f"/api/{{slug}}/{route}", "demo", {}, self.registry, self.db)

    def base(self) -> int:
        status, payload = self.get("base")
        self.assertEqual(status, 200)
        return payload["base"]

    def statuses(self) -> dict:
        return {t["id"]: t["status"] for t in reload(self.todo_path)["phases"][0]["tasks"]}

    def test_base_counts_events(self):
        self.assertEqual(self.base(), 0)
        self.assertEqual(self.post("start", {"task": "1.0.1", "owner": "a"})[0], 200)
        self.assertEqual(self.base(), 1)

    def test_stale_base_done_is_rejected_and_recorded(self):
        seen = self.base()
        self.assertEqual(self.post("start", {"task": "1.0.1", "owner": "a"})[0], 200)
        status, payload = self.post("done", {"task": "1.0.1", "owner": "b",
                                             "base": seen, "force": True})
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"conflict": True, "task": "1.0.1", "base": 0, "current": 1})
        self.assertEqual(self.statuses()["1.0.1"], "in_progress")
        status, listed = self.get("conflicts")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["conflicts"]), 1)
        entry = listed["conflicts"][0]
        self.assertEqual((entry["route"], entry["task"], entry["owner"], entry["base"],
                          entry["current"]), ("done", "1.0.1", "b", 0, 1))
        self.assertEqual(entry["changed_by"][0]["event"], "start")
        self.assertTrue(listed["path"].endswith("todo.json.conflicts.jsonl"))

    def test_change_to_another_task_is_not_a_conflict(self):
        seen = self.base()
        self.post("start", {"task": "1.0.2", "owner": "a"})
        status, _ = self.post("done", {"task": "1.0.1", "owner": "b", "base": seen})
        self.assertEqual(status, 200)
        self.assertEqual(self.get("conflicts")[1]["conflicts"], [])

    def test_base_beyond_the_log_is_a_conflict(self):
        status, payload = self.post("done", {"task": "1.0.1", "owner": "a", "base": 99})
        self.assertEqual((status, payload["current"]), (409, 0))

    def test_bad_base_is_a_400(self):
        for bad in ("x", -1, True):
            status, _ = self.post("done", {"task": "1.0.1", "owner": "a", "base": bad})
            self.assertEqual(status, 400, bad)

    def test_duplicate_done_still_returns_already_done(self):
        seen = self.base()
        self.assertEqual(self.post("done", {"task": "1.0.1", "owner": "a", "base": seen})[0], 200)
        status, payload = self.post("done", {"task": "1.0.1", "owner": "a", "base": seen})
        self.assertEqual(status, 200)
        self.assertTrue(payload["already_done"])
        self.assertEqual(self.get("conflicts")[1]["conflicts"], [])

    def test_reset_needs_a_base(self):
        self.post("start", {"task": "1.0.1", "owner": "a"})
        status, _ = self.post("reset", {"task": "1.0.1", "owner": "a"})
        self.assertEqual(status, 428)
        self.assertEqual(self.statuses()["1.0.1"], "in_progress")

    def test_stale_base_reset_is_rejected_while_claimed(self):
        seen = self.base()
        self.post("start", {"task": "1.0.1", "owner": "a"})
        status, payload = self.post("reset", {"task": "1.0.1", "owner": "b", "base": seen})
        self.assertEqual(status, 409)
        self.assertTrue(payload["conflict"])
        self.assertEqual(self.statuses()["1.0.1"], "in_progress")
        self.assertEqual(self.get("conflicts")[1]["conflicts"][0]["route"], "reset")

    def test_fresh_base_reset_still_refuses_another_owners_live_claim(self):
        self.post("start", {"task": "1.0.1", "owner": "a"})
        status, payload = self.post("reset", {"task": "1.0.1", "owner": "b",
                                              "base": self.base()})
        self.assertEqual(status, 409)
        self.assertIn("held by a", payload["error"])
        self.assertNotIn("conflict", payload)
        self.assertEqual(self.statuses()["1.0.1"], "in_progress")
        status, _ = self.post("reset", {"task": "1.0.1", "owner": "b",
                                        "base": self.base(), "force": True})
        self.assertEqual(status, 200)

    def test_holder_resets_its_own_claim_with_a_fresh_base(self):
        self.post("start", {"task": "1.0.1", "owner": "a"})
        status, _ = self.post("reset", {"task": "1.0.1", "owner": "a", "base": self.base()})
        self.assertEqual(status, 200)
        self.assertEqual(self.statuses()["1.0.1"], "pending")

    def test_stale_base_status_and_start_are_rejected(self):
        seen = self.base()
        self.post("start", {"task": "1.0.1", "owner": "a"})
        status, _ = self.post("status", {"task": "1.0.1", "status": "cancelled",
                                         "owner": "b", "base": seen})
        self.assertEqual(status, 409)
        status, _ = self.post("start", {"task": "1.0.1", "owner": "a", "base": seen})
        self.assertEqual(status, 409)
        self.assertEqual(len(self.get("conflicts")[1]["conflicts"]), 2)

    def test_conflicts_are_never_replayed(self):
        seen = self.base()
        self.post("start", {"task": "1.0.1", "owner": "a"})
        self.post("done", {"task": "1.0.1", "owner": "b", "base": seen, "force": True})
        self.post("done", {"task": "1.0.2", "owner": "a", "base": self.base()})
        self.assertEqual(self.statuses()["1.0.1"], "in_progress")
        self.assertEqual(len(self.get("conflicts")[1]["conflicts"]), 1)

    def test_conflicts_cli_reads_the_list_locally(self):
        seen = self.base()
        self.post("start", {"task": "1.0.1", "owner": "a"})
        self.post("done", {"task": "1.0.1", "owner": "b", "base": seen})
        code, out = run("--local", "conflicts", self.todo_path, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["conflicts"][0]["task"], "1.0.1")
        code, out = run("--local", "conflicts", self.todo_path)
        self.assertEqual(code, 0)
        self.assertIn("1.0.1", out)


class TestServeStream(unittest.TestCase):
    """SSE live stream (1.2.2): replay, since, Last-Event-ID, frame format."""

    def _registry(self, tmp: str) -> tuple[str, str, str]:
        todo_path = str(Path(tmp) / "todo.json")
        Path(todo_path).write_text(
            json.dumps(todo(phase("1.0", task("1.0.1")))), encoding="utf-8")
        registry = str(Path(tmp) / "registry.json")
        Path(registry).write_text(json.dumps({"projects": [
            {"slug": "demo", "name": "Demo", "repo_url": "",
             "todo_path": todo_path, "default_branch": "main"},
        ]}), encoding="utf-8")
        return registry, todo_path, str(Path(tmp) / "sessions.db")

    def test_route_table_has_stream(self):
        self.assertIn(("GET", "/api/stream"), serve.ROUTES)

    def test_format_sse_frame(self):
        frame = serve.format_sse({"event": "done", "task": "1.0.1"}, seq=7)
        self.assertTrue(frame.startswith("id: 7\n"))
        self.assertIn('data: {"event": "done", "task": "1.0.1"}', frame)
        self.assertTrue(frame.endswith("\n\n"))
        # Transport-only _seq never leaks into data.
        framed = serve.format_sse({"event": "done", "_seq": 7}, seq=7)
        self.assertNotIn("_seq", framed.split("data:", 1)[1])

    def test_replay_and_since_filter(self):
        with TemporaryDirectory() as tmp:
            registry, todo_path, db = self._registry(tmp)
            cli.record_event(todo_path, "start", task="1.0.1", owner="w")
            cli.record_event(todo_path, "done", task="1.0.1", owner="w")
            all_events = serve.collect_stream_events(["demo"], registry)
            self.assertEqual(len(all_events), 2)
            self.assertEqual([e["_seq"] for e in all_events], [1, 2])
            replay = serve.stream_events_since(["demo"], 1, registry)
            self.assertEqual(len(replay), 1)
            self.assertEqual(replay[0]["event"], "done")
            frame = serve.format_sse(replay[0], replay[0]["_seq"])
            parsed = json.loads(frame.split("data:", 1)[1].strip())
            self.assertEqual(parsed["task"], "1.0.1")

    def test_parse_params_supports_last_event_id(self):
        slugs, since = serve.parse_stream_params(
            {"slugs": "a, b"}, {"Last-Event-ID": "4"})
        self.assertEqual(slugs, ["a", "b"])
        self.assertEqual(since, 4)
        _, since_q = serve.parse_stream_params(
            {"since": "2"}, {"Last-Event-ID": "9"})
        self.assertEqual(since_q, 2)


class TestHermeticEnv(unittest.TestCase):
    """tests/hermetic.py: the developer's shell must not reach the suite."""

    def test_scrub_drops_every_project_namespace(self):
        env = {"TIDETABLE_SERVER": "https://hub", "TASKERKEEPER_TOKEN": "t", "TKRUN_CLAUDE": "x",
               "OVERNIGHT_OPENCODE": "y", "TK_API_TOKEN": "z", "PATH": "/bin", "KEEP_ME": "1"}
        gone = hermetic.scrub(env)
        self.assertEqual(sorted(gone), ["OVERNIGHT_OPENCODE", "TASKERKEEPER_TOKEN", "TIDETABLE_SERVER",
                                        "TKRUN_CLAUDE", "TK_API_TOKEN"])
        self.assertEqual(env, {"PATH": "/bin", "KEEP_ME": "1"})

    def test_suite_runs_with_no_hub_and_a_throwaway_config_home(self):
        leaked = [k for k in os.environ if k.upper().startswith(hermetic.PREFIXES)
                  and k.upper() != "TIDETABLE_CONFIG_HOME"]
        self.assertEqual(leaked, [])
        self.assertEqual(os.environ["TIDETABLE_CONFIG_HOME"], hermetic.CONFIG_HOME)
        self.assertNotEqual(Path(hermetic.CONFIG_HOME).resolve(), (Path.home() / ".config").resolve())


class TestTaskerKeeperCompat(unittest.TestCase):
    """The pre-0.9 names keep working: TASKERKEEPER_*, .taskerkeeper/, import taskerkeeper."""

    def test_env_get_prefers_new_name_then_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(jsonio.env_get("OWNER"), "")
            os.environ["TASKERKEEPER_OWNER"] = "old"
            self.assertEqual(jsonio.env_get("OWNER"), "old")
            os.environ["TIDETABLE_OWNER"] = "new"
            self.assertEqual(jsonio.env_get("OWNER"), "new")

    def test_legacy_owner_env_names_the_agent(self):
        with mock.patch.dict(os.environ, {"TASKERKEEPER_OWNER": "legacy-agent"}, clear=True):
            self.assertEqual(cli.default_owner(), "legacy-agent")

    def test_legacy_config_home_override_is_honoured(self):
        with TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TASKERKEEPER_CONFIG_HOME": tmp}, clear=True):
            self.assertEqual(agents.user_config_path(), Path(tmp) / "agents.json")

    def test_legacy_user_config_dir_applies_until_new_one_exists(self):
        with TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "APPDATA": tmp}
            legacy = Path(tmp) / "taskerkeeper" / "agents.json"
            current = Path(tmp) / "tidetable" / "agents.json"
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(agents.user_config_path(), current)
                legacy.parent.mkdir()
                legacy.write_text("{}", encoding="utf-8")
                self.assertEqual(agents.user_config_path(), legacy)
                current.parent.mkdir()
                current.write_text("{}", encoding="utf-8")
                self.assertEqual(agents.user_config_path(), current)

    def test_legacy_repo_config_dir_is_found_and_new_one_wins(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            legacy = root / ".taskerkeeper" / "agents.json"
            legacy.parent.mkdir()
            legacy.write_text("{}", encoding="utf-8")
            self.assertEqual(agents.find_repo_config(root), legacy)
            current = root / ".tidetable" / "agents.json"
            current.parent.mkdir()
            current.write_text("{}", encoding="utf-8")
            self.assertEqual(agents.find_repo_config(root), current)

    def test_import_taskerkeeper_aliases_the_same_modules(self):
        import taskerkeeper
        import taskerkeeper.cli
        import taskerkeeper.jsonio
        self.assertIs(taskerkeeper.cli, cli)
        self.assertIs(taskerkeeper.jsonio, jsonio)
        self.assertEqual(taskerkeeper.__version__, __import__("tidetable").__version__)

    def test_legacy_module_entry_point_runs(self):
        r = subprocess.run(
            [os.sys.executable, "-m", "taskerkeeper", "validate",
             str(EXAMPLES / "simple-project.json")],
            capture_output=True, text=True, cwd=REPO_ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_module_entry_point_exits_nonzero_on_error(self):
        # main() returned 1 on handled errors without exiting, so `python -m`
        # (and the Paseo sidebar's fallback) saw exit 0 beside "Error: ...".
        for module in ("tidetable", "taskerkeeper"):
            r = subprocess.run(
                [os.sys.executable, "-m", module, "--local", "list", "no-such-todo.json"],
                capture_output=True, text=True, cwd=REPO_ROOT)
            self.assertEqual(r.returncode, 1, module)
            self.assertIn("not found", r.stdout)


class DefaultTodoTest(unittest.TestCase):
    """`todo_file` is optional: ., docs/, .tidetable/ are searched in that order."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)

    def put(self, rel: str) -> None:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(todo(phase("1.0", task("1.0.1")))), encoding="utf-8")

    def run_noninteractive(self, *argv: str) -> tuple[int, str]:
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            return run(*argv)

    def test_single_file_in_cwd_is_used(self):
        self.put("todo.json")
        code, out = self.run_noninteractive("ready", "--json")
        self.assertEqual(code, 0, out)
        self.assertIn("1.0.1", out)

    def test_search_order_is_cwd_then_docs_then_tidetable(self):
        self.put(".tidetable/todo-c.json")
        self.assertEqual([p.name for p in cli.find_default_todos(self.dir)], ["todo-c.json"])
        self.put("docs/todo-b.json")
        self.assertEqual([p.name for p in cli.find_default_todos(self.dir)], ["todo-b.json"])
        self.put("todo-a.json")
        self.assertEqual([p.name for p in cli.find_default_todos(self.dir)], ["todo-a.json"])

    def test_task_id_alone_is_not_mistaken_for_the_todo_file(self):
        self.put("docs/todo-v1.json")
        code, out = self.run_noninteractive("start", "1.0.1", "--owner", "me")
        self.assertEqual(code, 0, out)
        data = reload(str(self.dir / "docs" / "todo-v1.json"))
        self.assertEqual(data["phases"][0]["tasks"][0]["status"], "in_progress")

    def test_explicit_path_still_wins(self):
        self.put("todo.json")
        self.put("other/mine.json")
        code, out = self.run_noninteractive("ready", "other/mine.json", "--json")
        self.assertEqual(code, 0, out)

    def test_several_files_in_one_directory_is_an_error(self):
        self.put("todo-v1.json")
        self.put("todo-v2.json")
        code, out = self.run_noninteractive("ready")
        self.assertEqual(code, 1)
        self.assertIn("more than one todo file", out)
        self.assertIn("todo-v1.json", out)

    def test_nothing_found_without_a_terminal_errors_instead_of_prompting(self):
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            code, out = self.run_noninteractive("ready")
        self.assertEqual(code, 1)
        self.assertIn("no todo*.json found", out)

    def test_nothing_found_on_a_terminal_asks_for_a_path(self):
        self.put("elsewhere/plan.json")
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", return_value="elsewhere/plan.json"):
            code, out = run("ready", "--json")
        self.assertEqual(code, 0, out)

    def test_hub_mode_does_not_default_the_slug(self):
        self.put("todo.json")
        code, out = self.run_noninteractive("--server", "http://127.0.0.1:1", "ready")
        self.assertEqual(code, 1)
        self.assertIn("project slug", out)

    def test_tkrun_subcommands_accept_no_todo_file(self):
        args = cli.build_parser().parse_args(["tkrun", "run", "--dry-run"])
        self.assertIsNone(args.todo_file)
        args = cli.build_parser().parse_args(["tkrun", "adjudicate", "rm -rf x"])
        self.assertIsNone(args.todo_file)
        self.assertEqual(args.command, "rm -rf x")


class TestRemoteClient(unittest.TestCase):
    """`--server` runs a command against a `tidetable serve` hub."""

    TOKEN = "test-shared-token"

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.todo_path = str(root / "todo.json")
        Path(self.todo_path).write_text(json.dumps(todo(
            phase("1.0", task("1.0.1"), task("1.0.2", prerequisites=["1.0.1"])))),
            encoding="utf-8")
        registry = str(root / "registry.json")
        Path(registry).write_text(json.dumps({"projects": [
            {"slug": "demo", "name": "Demo", "repo_url": "",
             "todo_path": self.todo_path, "default_branch": "main"}]}), encoding="utf-8")

        server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
        server.daemon_threads = True
        server.registry = registry
        server.sessions_db = str(root / "sessions.db")
        server.tokens_db = str(root / "tokens.db")
        self.tokens_db = server.tokens_db
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

        patcher = mock.patch.dict(os.environ, {"TK_API_TOKEN": self.TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv: str, token: str | None = None, server: str | None = None):
        head = ["--server", server or self.url]
        if token is None:
            token = self.TOKEN
        if token:
            head += ["--token", token]
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(head + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_ready_matches_local_json(self):
        code, out, _ = self.run_cli("ready", "demo", "--json")
        self.assertEqual(code, 0)
        local = io.StringIO()
        with redirect_stdout(local):
            cli.main(["ready", self.todo_path, "--json"])
        self.assertEqual(json.loads(out), json.loads(local.getvalue()))

    def test_look_queue_matches_local_json(self):
        data = json.loads(Path(self.todo_path).read_text(encoding="utf-8"))
        data["phases"][0]["tasks"][0]["status"] = "needs_human_look"
        data["phases"][0]["tasks"][0]["human_look"] = "check it"
        Path(self.todo_path).write_text(json.dumps(data), encoding="utf-8")
        code, out, _ = self.run_cli("look-queue", "demo", "--json")
        self.assertEqual(code, 0, out)
        local = io.StringIO()
        with redirect_stdout(local):
            cli.main(["look-queue", self.todo_path, "--json"])
        self.assertEqual(json.loads(out), json.loads(local.getvalue()))
        self.assertEqual([q["id"] for q in json.loads(out)["queue"]], ["1.0.1"])

    def test_look_defer_then_clear_matches_local_effect(self):
        local_copy = str(Path(self.tmp.name) / "local.json")
        Path(local_copy).write_text(
            Path(self.todo_path).read_text(encoding="utf-8"), encoding="utf-8")

        code, out, _ = self.run_cli("look-defer", "demo", "1.0.1", "--note", "check it")
        self.assertEqual(code, 0, out)
        run("look-defer", local_copy, "1.0.1", "--note", "check it")

        hub = reload(self.todo_path)["phases"][0]["tasks"][0]
        local = reload(local_copy)["phases"][0]["tasks"][0]
        self.assertEqual(hub["status"], local["status"])
        self.assertEqual(hub["status"], "needs_human_look")
        self.assertEqual(hub["human_look"], local["human_look"])

        code, out, _ = self.run_cli("look-clear", "demo", "1.0.1")
        self.assertEqual(code, 0, out)
        run("look-clear", local_copy, "1.0.1")

        hub = reload(self.todo_path)["phases"][0]["tasks"][0]
        local = reload(local_copy)["phases"][0]["tasks"][0]
        self.assertEqual(hub["status"], "done")
        self.assertEqual(local["status"], "done")
        self.assertNotIn("human_look", hub)
        self.assertNotIn("human_look", local)

    def test_look_clear_reject_matches_local_effect(self):
        data = reload(self.todo_path)
        data["phases"][0]["tasks"][0]["status"] = "needs_human_look"
        data["phases"][0]["tasks"][0]["human_look"] = "check it"
        Path(self.todo_path).write_text(json.dumps(data), encoding="utf-8")
        local_copy = str(Path(self.tmp.name) / "local-reject.json")
        Path(local_copy).write_text(json.dumps(data), encoding="utf-8")

        code, out, _ = self.run_cli("look-clear", "demo", "1.0.1", "--reject")
        self.assertEqual(code, 0, out)
        run("look-clear", local_copy, "1.0.1", "--reject")

        hub = reload(self.todo_path)["phases"][0]["tasks"][0]
        local = reload(local_copy)["phases"][0]["tasks"][0]
        self.assertEqual(hub["status"], "pending")
        self.assertEqual(local["status"], "pending")

    def test_look_clear_refusal_matches_local_wording(self):
        # 1.0.1 is plain "pending", not queued for a human look.
        code, out, err = self.run_cli("look-clear", "demo", "1.0.1")
        self.assertEqual(code, 1)
        local_code, local_out = run("look-clear", self.todo_path, "1.0.1")
        self.assertEqual(local_code, 1)
        self.assertIn(local_out.strip(), err)

    def test_look_defer_not_found_matches_local_wording(self):
        code, out, err = self.run_cli("look-defer", "demo", "9.9.9")
        self.assertEqual(code, 1)
        local_code, local_out = run("look-defer", self.todo_path, "9.9.9")
        self.assertEqual(local_code, 1)
        self.assertIn(local_out.strip(), err)

    def test_look_defer_auth_401_and_403(self):
        from tidetable import tokens as tokens_store

        code, out, err = self.run_cli("look-defer", "demo", "1.0.1", token="")
        self.assertEqual(code, 1)
        self.assertIn("unauthorized", err)
        self.assertEqual(reload(self.todo_path)["phases"][0]["tasks"][0]["status"], "pending")

        _, presented, _ = tokens_store.mint_token(self.tokens_db, ["other"], ["read", "write"])
        code, out, err = self.run_cli("look-defer", "demo", "1.0.1", token=presented)
        self.assertEqual(code, 1)
        self.assertIn("forbidden", err)
        self.assertEqual(reload(self.todo_path)["phases"][0]["tasks"][0]["status"], "pending")

    def test_sidebar_matches_local_json_and_renders_text_without_it(self):
        code, out, _ = self.run_cli("sidebar", "demo", "--json", "--width", "60", "--height", "20")
        self.assertEqual(code, 0)
        local = io.StringIO()
        with redirect_stdout(local):
            cli.main(["sidebar", self.todo_path, "--json", "--width", "60", "--height", "20"])
        self.assertEqual(json.loads(out), json.loads(local.getvalue()))
        code, text, _ = self.run_cli("sidebar", "demo")
        self.assertEqual(code, 0)
        self.assertFalse(text.lstrip().startswith("{"))
        self.assertIn("1.0.1", text)

    def test_start_then_done_writes_the_hub_file(self):
        code, out, _ = self.run_cli("start", "demo", "1.0.1", "--owner", "laptop")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["owner"], "laptop")
        code, out, _ = self.run_cli("done", "demo", "1.0.1", "--owner", "laptop")
        self.assertEqual(code, 0, out)
        status = {t["id"]: t["status"] for t in
                  json.loads(Path(self.todo_path).read_text(encoding="utf-8"))["phases"][0]["tasks"]}
        self.assertEqual(status["1.0.1"], "done")

    def test_second_machine_cannot_take_a_live_claim(self):
        self.assertEqual(self.run_cli("start", "demo", "1.0.1", "--owner", "a")[0], 0)
        code, out, err = self.run_cli("start", "demo", "1.0.1", "--owner", "b")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("held by a", err)

    def test_next_with_nothing_runnable_exits_1_with_payload(self):
        self.run_cli("start", "demo", "1.0.1", "--owner", "a")
        code, out, _ = self.run_cli("next", "demo", "--owner", "b")
        self.assertEqual(code, 1)
        self.assertIsNone(json.loads(out)["task"])

    def test_owner_defaults_to_this_machine_not_the_server(self):
        with mock.patch.dict(os.environ, {"TIDETABLE_OWNER": "my-agent"}):
            code, out, _ = self.run_cli("start", "demo", "1.0.1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["owner"], "my-agent")

    def test_missing_or_wrong_token_is_a_clean_failure(self):
        code, out, err = self.run_cli("list", "demo", token="")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Error:", err)
        self.assertIn("TIDETABLE_TOKEN", err)
        self.assertEqual(self.run_cli("list", "demo", token="nope")[0], 1)

    def test_unknown_slug_and_unreachable_server(self):
        code, _, err = self.run_cli("list", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("unknown slug", err)
        out = io.StringIO()
        with redirect_stdout(out):
            code, _, _ = self.run_cli("list", "demo", server="http://127.0.0.1:1")
        self.assertEqual(code, 1)

    def test_edit_over_the_wire_changes_only_named_fields(self):
        self.assertEqual(self.run_cli("start", "demo", "1.0.1", "--owner", "w")[0], 0)
        code, out, _ = self.run_cli("edit", "demo", "1.0.1", "--title", "New",
                                    "--touches", "src/", "--clear-success")
        self.assertEqual(code, 0, out)
        payload = json.loads(out)
        self.assertEqual(payload["changed"], ["success", "title", "touches"])
        self.assertEqual(payload["task"]["status"], "in_progress")
        tasks = {t["id"]: t for t in
                 json.loads(Path(self.todo_path).read_text(encoding="utf-8"))["phases"][0]["tasks"]}
        self.assertEqual(tasks["1.0.1"]["title"], "New")
        self.assertEqual(tasks["1.0.1"]["touches"], ["src/"])
        self.assertEqual(tasks["1.0.1"]["success"], [])
        self.assertEqual(tasks["1.0.1"]["claimed_by"], "w")

    def test_edit_refusal_keeps_the_local_wording(self):
        code, out, err = self.run_cli("edit", "demo", "1.0.1", "--prereq", "9.9.9")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("prerequisite 9.9.9 does not exist", err)
        local = io.StringIO()
        with redirect_stdout(local):
            self.assertEqual(
                cli.main(["edit", self.todo_path, "1.0.1", "--prereq", "9.9.9"]), 1)
        self.assertIn("prerequisite 9.9.9 does not exist", local.getvalue())

    def test_add_phase_over_the_wire(self):
        code, out, _ = self.run_cli("add-phase", "demo", "--phase", "2.0",
                                    "--title", "Next", "--prereq", "1.0", "--after", "1.0")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["id"], "2.0")

    def test_env_server_is_used_and_local_flag_opts_out(self):
        env = {"TIDETABLE_SERVER": self.url, "TIDETABLE_TOKEN": self.TOKEN}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["list", "demo", "--json"]), 0)
            self.assertIn("phases", out.getvalue())
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["--local", "list", self.todo_path, "--json"])
            self.assertEqual(code, 0)

    def test_stale_base_over_the_wire_is_a_conflict_and_listed(self):
        self.assertEqual(self.run_cli("start", "demo", "1.0.1", "--owner", "a")[0], 0)
        code, out, err = self.run_cli("done", "demo", "1.0.1", "--owner", "b",
                                      "--force", "--base", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"conflict": True, "task": "1.0.1",
                                           "base": 0, "current": 1})
        self.assertIn("conflict", err)
        code, out, _ = self.run_cli("conflicts", "demo")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["conflicts"][0]["owner"], "b")

    def test_reset_without_base_fetches_one_and_refuses_a_live_claim(self):
        self.assertEqual(self.run_cli("start", "demo", "1.0.1", "--owner", "a")[0], 0)
        code, _, err = self.run_cli("reset", "demo", "1.0.1", "--owner", "b")
        self.assertEqual(code, 1)
        self.assertIn("held by a", err)
        code, out, _ = self.run_cli("reset", "demo", "1.0.1", "--owner", "a")
        self.assertEqual(code, 0, out)

    def test_401_on_a_write_is_an_error_not_a_fallback(self):
        before = Path(self.todo_path).read_text(encoding="utf-8")
        # Both the base fetch and the write itself refuse a bad token loudly.
        for extra in ((), ("--base", "0")):
            code, out, err = self.run_cli("done", "demo", "1.0.1", *extra, token="nope")
            self.assertEqual(code, 1, extra)
            self.assertIn("unauthorized", out + err)
            self.assertEqual(Path(self.todo_path).read_text(encoding="utf-8"), before)

    def test_commands_without_a_hub_route_are_refused(self):
        code, out, _ = self.run_cli("convert", "demo")
        self.assertEqual(code, 1)
        self.assertIn("no remote mode", out)

    def test_build_request_maps_flags_to_query_and_body(self):
        from tidetable import remote

        args = cli.build_parser().parse_args(
            ["next", "demo", "--owner", "w", "--no-resume", "--disjoint"])
        method, path, body = remote.build_request(args)
        self.assertEqual(method, "GET")
        self.assertTrue(path.startswith("/api/demo/next?"))
        for part in ("owner=w", "resume=0", "disjoint=1"):
            self.assertIn(part, path)
        args = cli.build_parser().parse_args(
            ["add", "demo", "--phase", "1.0", "--title", "T", "--prereq", "a", "--prereq", "b"])
        _, _, body = remote.build_request(args)
        self.assertEqual(body["prerequisites"], ["a", "b"])
        args = cli.build_parser().parse_args(
            ["edit", "demo", "1.0.1", "--title", "T", "--touches", "a/",
             "--clear-success", "--clear-tests"])
        method, path, body = remote.build_request(args)
        self.assertEqual((method, path), ("POST", "/api/demo/edit"))
        self.assertEqual(body["task"], "1.0.1")
        self.assertEqual(body["title"], "T")
        self.assertEqual(body["touches"], ["a/"])
        self.assertIsNone(body["success"])
        self.assertIsNone(body["tests"])
        args = cli.build_parser().parse_args(
            ["look-defer", "demo", "1.0.1", "--note", "check it"])
        method, path, body = remote.build_request(args)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/demo/look-defer")
        self.assertEqual(body["task"], "1.0.1")
        self.assertEqual(body["note"], "check it")
        args = cli.build_parser().parse_args(["look-clear", "demo", "1.0.1", "--reject"])
        method, path, body = remote.build_request(args)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/demo/look-clear")
        self.assertIs(body["reject"], True)
