"""Tests for `tidetable tkrun`, with a stub agent and a scratch git repo.

No model is ever called: TKRUN_CLAUDE points at a python stub whose
behaviour STUB_MODE selects.
"""

from __future__ import annotations

try:  # before anything reads the environment; see tests/hermetic.py
    from . import hermetic  # python -m unittest tests.test_x
except ImportError:
    import hermetic  # python -m unittest discover -s tests

import io
import json
import socket
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tidetable import cli, tkrun

STUB = r'''
import os, sys, time
prompt = sys.stdin.read()
mode = os.environ.get("STUB_MODE", "done")
if "strict read-only reviewer" in prompt:
    phase = "Every task of phase" in prompt
    if os.environ.get("STUB_LOG"):
        open(os.environ["STUB_LOG"], "a").write(("phase" if phase else "task") + "\n")
    key = "STUB_PHASE_REVIEW" if phase else "STUB_REVIEW"
    print("REVIEW_RESULT: " + os.environ.get(key, "PASS"))
    sys.exit(0)
if "cleanup agent" in prompt:
    if os.environ.get("STUB_LOG"):
        open(os.environ["STUB_LOG"], "a").write("repair\n")
    if os.environ.get("STUB_REPAIR") == "fix":
        open("fixed.txt", "w").write("fixed")
    if os.environ.get("STUB_REPAIR") == "work":
        open("work.txt", "w").write("finished by the cleanup crew")
    print("TKRUN_SUMMARY: repair the gate")
    print("TKRUN_RESULT: FAILED cannot" if os.environ.get("STUB_REPAIR") == "fail" else "TKRUN_RESULT: DONE")
    sys.exit(0)
if mode == "limit":
    print("Claude usage limit reached|%d" % (int(time.time()) + 3600))
    sys.exit(1)
if mode == "silent":
    print("I did some things")
    sys.exit(0)
tid = prompt.split("Task: ")[1].split(" ")[0]
if os.environ.get("STUB_COMMIT"):
    import subprocess
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=agent", "commit", "-qm",
                    "agent own commit"], check=True)
if os.environ.get("STUB_SENTINEL"):
    open(os.environ["STUB_SENTINEL"], "w").write("x")
if os.environ.get("STUB_ENVDUMP"):
    open(os.environ["STUB_ENVDUMP"], "w").write(
        "|".join(k for k in ("TIDETABLE_TOKEN", "TIDETABLE_SERVER", "KEEP_ME") if k in os.environ))
if os.environ.get("STUB_TRACK"):
    open(os.environ["STUB_TRACK"], "a").write("s %s %f\n" % (tid, time.time()))
    time.sleep(float(os.environ.get("STUB_SLEEP", "1.0")))
    open(os.environ["STUB_TRACK"], "a").write("e %s %f\n" % (tid, time.time()))
if mode != "nochange":
    name = os.environ.get("STUB_FILE") or ("work-%s.txt" % tid if os.environ.get("STUB_PER_TASK") else "work.txt")
    open(name, "w").write("work for " + tid)
if os.environ.get("STUB_SIB_FILE"):
    os.makedirs(os.path.dirname(os.environ["STUB_SIB_FILE"]), exist_ok=True)
    open(os.environ["STUB_SIB_FILE"], "w").write("sib")
print("TKRUN_SUMMARY: add work file")
print({"partial": "TKRUN_RESULT: PARTIAL needs eyes",
       "failed": "TKRUN_RESULT: FAILED cannot"}.get(mode, "TKRUN_RESULT: DONE"))
'''


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True, check=True).stdout


class Scratch:
    """A git repo with a todo file, a tkrun config and a stub agent."""

    def __init__(self, gate_exit: int = 0, tasks: int = 1, review: bool = False,
                 repair: bool = False):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "T")
        task_list = [{"id": f"1.{i}", "title": f"Task {i}", "status": "pending",
                      "goal": "make work.txt", "success": ["work.txt exists"],
                      "changelog": f"added {i}"} for i in range(1, tasks + 1)]
        todo = {"schema_version": "1.0.0",
                "project": {"name": "T", "version": {"milestone": "v1", "release_version": "0.1.0"},
                            "description": "fixture"},
                "phases": [{"id": "1", "title": "P", "goal": "g", "tasks": task_list}]}
        self.todo = self.root / "todo.json"
        self.todo.write_text(json.dumps(todo), encoding="utf-8")
        self.cfg_path = self.root / ".tidetable" / "tkrun.json"
        self.cfg_path.parent.mkdir()
        self.set_config({"gate": [{"name": "gate", "cmd": [sys.executable, "-c",
                                                           f"raise SystemExit({gate_exit})"]}],
                         "review": {"enabled": review},
                         "repair": {"enabled": repair}})
        stub = Path(self._tmp.name) / "stub.py"
        stub.write_text(STUB, encoding="utf-8")
        self.env = {"TKRUN_CLAUDE": f'"{sys.executable}" "{stub}"',
                    "TKRUN_RATE_MARGIN_SEC": "0"}

    def set_config(self, cfg: dict) -> None:
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        _git(self.root, "add", "-A")
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "cfg"], check=True,
                       capture_output=True)

    def run(self, mode: str = "done", *extra: str, review: str = "PASS", **env_extra) -> tuple[int, str]:
        env = {**self.env, "STUB_MODE": mode, "STUB_REVIEW": review, **env_extra}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(buf):
            code = cli.main(["tkrun", "run", str(self.todo), "--model", "anthropic/stub",
                             *(extra or ("--hours", "1"))])
        return code, buf.getvalue()

    def status(self, tid: str = "1.1") -> str:
        """Task status as the run sees it: in the worktree once one exists."""
        todo = self.work / "todo.json" if self.work.exists() else self.todo
        data = json.loads(todo.read_text(encoding="utf-8"))
        return next(t["status"] for p in data["phases"] for t in p["tasks"] if t["id"] == tid)

    @property
    def work(self) -> Path:
        """The run's worktree, beside the repo."""
        return self.root.parent / f"{self.root.name}-tkrun-{datetime.now():%Y-%m-%d}"

    @property
    def branch(self) -> str:
        return f"tkrun/{datetime.now():%Y-%m-%d}"

    def close(self):
        self._tmp.cleanup()


class TKRunRunTests(unittest.TestCase):
    def scratch(self, **kw) -> Scratch:
        s = Scratch(**kw)
        self.addCleanup(s.close)
        return s

    def test_done_commits_on_branch_and_closes_task(self):
        s = self.scratch()
        base = _git(s.root, "rev-parse", "main").strip()
        code, out = s.run("done")
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status(), "done")
        self.assertEqual(_git(s.root, "rev-parse", "main").strip(), base)
        # the checkout you started from is untouched; the work lives on the branch in the worktree
        self.assertEqual(_git(s.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertFalse((s.root / "work.txt").exists())
        self.assertEqual(json.loads(s.todo.read_text(encoding="utf-8"))["phases"][0]["tasks"][0]["status"],
                         "pending")
        self.assertEqual(_git(s.work, "rev-parse", "--abbrev-ref", "HEAD").strip(), s.branch)
        self.assertIn("1.1: add work file", _git(s.root, "log", s.branch, "--format=%s"))
        self.assertTrue((s.work / "work.txt").exists())
        self.assertEqual(_git(s.work, "status", "--porcelain").strip(), "")
        self.assertIn(str(s.work.name), out)

    def test_gate_failure_stashes_work_and_resets_task(self):
        s = self.scratch(gate_exit=1)
        _, out = s.run("done")
        self.assertEqual(s.status(), "pending")
        self.assertFalse((s.work / "work.txt").exists())
        self.assertIn("tkrun", _git(s.work, "stash", "list"))
        self.assertIn("gate", out)

    def test_agent_failed_and_silent_agent_reset_task(self):
        for mode in ("failed", "silent"):
            with self.subTest(mode=mode):
                s = self.scratch()
                s.run(mode)
                self.assertEqual(s.status(), "pending")

    def test_partial_commits_wip_and_queues_a_human_look(self):
        s = self.scratch()
        s.run("partial")
        self.assertEqual(s.status(), "needs_human_look")
        self.assertIn("wip(1.1)", _git(s.root, "log", s.branch, "--format=%s"))

    def test_done_with_no_changes_is_a_failure(self):
        s = self.scratch()
        s.run("nochange")
        self.assertEqual(s.status(), "pending")

    def test_review_verdict_decides_whether_task_closes(self):
        s = self.scratch(review=True)
        s.run("done", review="FAIL some reason")
        self.assertEqual(s.status(), "in_progress")
        s2 = self.scratch(review=True)
        s2.run("done", review="PASS")
        self.assertEqual(s2.status(), "done")

    def two_phases(self, s) -> None:
        """Move task 1.2 into its own phase 2 so a phase closes mid-run."""
        data = json.loads(s.todo.read_text(encoding="utf-8"))
        t2 = data["phases"][0]["tasks"].pop(1)
        data["phases"].append({"id": "2", "title": "P2", "goal": "g2", "tasks": [t2]})
        s.todo.write_text(json.dumps(data), encoding="utf-8")
        _git(s.root, "add", "-A")
        subprocess.run(["git", "-C", str(s.root), "commit", "-q", "-m", "phases"], check=True,
                       capture_output=True)

    def test_phase_review_runs_once_when_the_phase_completes(self):
        s = self.scratch(tasks=2)
        log = s.root.parent / "reviews.log"
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False, "phase": {"enabled": True}}})
        code, out = s.run("done", STUB_LOG=str(log))
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "done")
        self.assertEqual(log.read_text().split(), ["phase"])
        self.assertIn("PHASE REVIEW PASS 1", out)

    def test_task_and_phase_review_are_independent(self):
        s = self.scratch(tasks=2, review=True)
        log = s.root.parent / "reviews.log"
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": True, "phase": {"enabled": True}}})
        s.run("done", STUB_LOG=str(log))
        self.assertEqual(log.read_text().split(), ["task", "task", "phase"])

    def test_phase_review_off_by_default(self):
        s = self.scratch(tasks=2, review=True)
        log = s.root.parent / "reviews.log"
        s.run("done", STUB_LOG=str(log))
        self.assertEqual(log.read_text().split(), ["task", "task"])

    def test_phase_review_fail_continues_by_default(self):
        s = self.scratch(tasks=2)
        self.two_phases(s)
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False, "phase": {"enabled": True}}})
        code, out = s.run("done", STUB_PHASE_REVIEW="FAIL parts do not fit")
        self.assertIn("PHASE REVIEW FAIL 1: parts do not fit", out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "done")

    def test_phase_review_fail_can_stop_the_run(self):
        s = self.scratch(tasks=2)
        self.two_phases(s)
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False,
                                 "phase": {"enabled": True, "on_fail": "stop"}}})
        code, out = s.run("done", STUB_PHASE_REVIEW="FAIL parts do not fit")
        self.assertEqual(code, 1)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "pending")

    def test_no_phase_review_flag_wins_over_config(self):
        s = self.scratch(tasks=2)
        log = s.root.parent / "reviews.log"
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False, "phase": {"enabled": True}}})
        s.run("done", "--hours", "1", "--no-phase-review", STUB_LOG=str(log))
        self.assertFalse(log.exists())

    def test_phase_review_model_flag_enables_it(self):
        s = self.scratch(tasks=2)
        log = s.root.parent / "reviews.log"
        s.run("done", "--hours", "1", "--phase-review-model", "anthropic/stub", STUB_LOG=str(log))
        self.assertEqual(log.read_text().split(), ["phase"])

    def test_follow_echoes_agent_output_and_still_closes(self):
        s = self.scratch()
        code, out = s.run("done", "--hours", "1", "--follow")
        self.assertEqual(code, 0, out)
        self.assertIn("TKRUN_SUMMARY: add work file", out)
        self.assertEqual(s.status(), "done")

    def test_no_follow_keeps_agent_output_off_the_console(self):
        s = self.scratch()
        code, out = s.run("done", "--hours", "1", "--no-follow")
        self.assertNotIn("TKRUN_SUMMARY", out)
        self.assertEqual(s.status(), "done")

    def parallel_scratch(self, touches: list[list[str]]) -> Scratch:
        s = self.scratch(tasks=len(touches))
        data = json.loads(s.todo.read_text(encoding="utf-8"))
        for t, tt in zip(data["phases"][0]["tasks"], touches):
            if tt:
                t["touches"] = tt
        s.todo.write_text(json.dumps(data), encoding="utf-8")
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False}})
        return s

    @staticmethod
    def max_concurrency(track: Path) -> int:
        events = sorted((float(ts), kind) for kind, _tid, ts in
                        (ln.split() for ln in track.read_text().splitlines()))
        cur = best = 0
        for _ts, kind in events:
            cur += 1 if kind == "s" else -1
            best = max(best, cur)
        return best

    def test_jobs_run_disjoint_tasks_at_once_and_merge_them(self):
        s = self.parallel_scratch([["a/"], ["b/"], ["c/"]])
        track = s.root.parent / "track.log"
        code, out = s.run("done", "--hours", "1", "--jobs", "3", STUB_TRACK=str(track), STUB_SLEEP="2.5", STUB_PER_TASK="1")
        self.assertEqual(code, 0, out)
        for tid in ("1.1", "1.2", "1.3"):
            self.assertEqual(s.status(tid), "done")
            self.assertTrue((s.work / f"work-{tid}.txt").exists())
        self.assertGreaterEqual(self.max_concurrency(track), 2)
        subjects = _git(s.root, "log", s.branch, "--format=%s")
        for tid in ("1.1", "1.2", "1.3"):
            self.assertIn(f"{tid}: add work file", subjects)
        # job worktrees and branches are removed once merged
        self.assertEqual(len(_git(s.root, "worktree", "list").strip().splitlines()), 2)
        self.assertNotIn("-1.1", _git(s.root, "branch", "--list"))

    def test_jobs_serialise_tasks_with_overlapping_or_missing_touches(self):
        s = self.parallel_scratch([["lib/"], ["lib/sub/"], []])
        track = s.root.parent / "track.log"
        code, out = s.run("done", "--hours", "1", "--jobs", "3", STUB_TRACK=str(track), STUB_SLEEP="2.5", STUB_PER_TASK="1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.max_concurrency(track), 1)
        for tid in ("1.1", "1.2", "1.3"):
            self.assertEqual(s.status(tid), "done")

    def test_job_merge_conflict_keeps_branch_and_reopens_task(self):
        s = self.parallel_scratch([["a/"], ["b/"]])
        track = s.root.parent / "track.log"
        # both tasks write work.txt with different content: the second merge conflicts
        code, out = s.run("done", "--hours", "1", "--jobs", "2", STUB_TRACK=str(track), STUB_SLEEP="2.5")
        self.assertIn("MERGE CONFLICT", out)
        statuses = sorted(s.status(t) for t in ("1.1", "1.2"))
        self.assertEqual(statuses, ["done", "pending"])
        self.assertIn(s.branch + "-", _git(s.root, "branch", "--list"))

    def repair_scratch(self, **cfg) -> Scratch:
        s = self.scratch(tasks=1)
        gate = "import os, sys; sys.exit(0 if os.path.exists('fixed.txt') else 1)"
        s.set_config({"gate": [{"name": "gate", "cmd": [sys.executable, "-c", gate]}],
                      "review": {"enabled": False}, **cfg})
        return s

    def test_cleanup_crew_fixes_a_failed_gate_and_the_task_closes(self):
        s = self.repair_scratch()
        log = s.root.parent / "agents.log"
        code, out = s.run("done", "--hours", "1", STUB_REPAIR="fix", STUB_LOG=str(log))
        self.assertIn("REPAIRED 1.1 on attempt 1", out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(log.read_text().split(), ["repair"])

    def test_cleanup_crew_gives_up_after_its_attempts_and_parks_the_task(self):
        s = self.repair_scratch(repair={"attempts": 2})
        log = s.root.parent / "agents.log"
        code, out = s.run("done", "--hours", "1", STUB_REPAIR="none", STUB_LOG=str(log))
        self.assertEqual(log.read_text().split(), ["repair", "repair"])
        self.assertIn("FAILED 1.1 - gate:", out)
        self.assertEqual(s.status("1.1"), "pending")

    def test_cleanup_crew_finishes_a_task_whose_agent_did_not(self):
        for mode in ("failed", "silent", "nochange"):
            with self.subTest(mode=mode):
                s = self.scratch(tasks=1, repair=True)
                log = s.root.parent / "agents.log"
                code, out = s.run(mode, "--hours", "1", STUB_REPAIR="work", STUB_LOG=str(log))
                self.assertIn("REPAIRED 1.1 on attempt 1", out)
                self.assertEqual(s.status("1.1"), "done")
                self.assertEqual(log.read_text().split(), ["repair"])
                self.assertEqual((s.work / "work.txt").read_text(), "finished by the cleanup crew")

    def test_cleanup_crew_failing_to_finish_still_parks_the_task(self):
        s = self.scratch(tasks=1, repair=True)
        code, out = s.run("failed", "--hours", "1", STUB_REPAIR="fail")
        self.assertIn("FAILED 1.1 - agent: cannot", out)
        self.assertEqual(s.status("1.1"), "pending")

    def test_cleanup_crew_can_be_turned_off(self):
        s = self.repair_scratch(repair={"enabled": False})
        log = s.root.parent / "agents.log"
        code, out = s.run("done", "--hours", "1", STUB_REPAIR="fix", STUB_LOG=str(log))
        self.assertFalse(log.exists())
        self.assertIn("FAILED 1.1 - gate:", out)

    def test_merge_resolve_regenerates_a_conflicted_generated_file(self):
        s = self.parallel_scratch([["a/"], ["b/"]])
        regen = "open('work.txt','w').write('regenerated\\n')"
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False},
                      "merge_resolve": {"work.txt": [sys.executable, "-c", regen]}})
        track = s.root.parent / "track.log"
        code, out = s.run("done", "--hours", "1", "--jobs", "2", STUB_TRACK=str(track), STUB_SLEEP="2.5")
        self.assertNotIn("MERGE CONFLICT", out)
        self.assertIn("regenerated work.txt", out)
        self.assertEqual(sorted(s.status(t) for t in ("1.1", "1.2")), ["done", "done"])
        self.assertEqual((s.work / "work.txt").read_text().strip(), "regenerated")

    def test_write_json_retries_a_windows_sharing_violation(self):
        from tidetable import jsonio
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            real(src, dst)

        with TemporaryDirectory() as d, mock.patch.object(jsonio.os, "replace", flaky), \
                mock.patch.object(jsonio.time, "sleep"):
            target = Path(d) / "todo.json"
            jsonio.write_json({"a": 1}, target)
            self.assertEqual(json.loads(target.read_text()), {"a": 1})
            self.assertEqual(len(calls), 3)
            self.assertEqual([p.name for p in Path(d).iterdir()], ["todo.json"])

    def test_commit_paths_tolerates_a_path_with_nothing_to_commit(self):
        import types
        with TemporaryDirectory() as d:
            repo = Path(d)
            _git(repo, "init", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "T")
            (repo / "a.txt").write_text("x\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "init")
            r = types.SimpleNamespace(run_dir=repo / ".run")
            r.run_dir.mkdir()
            self.assertFalse(tkrun.Runner.commit_paths(r, repo, ["a.txt"], "no-op"))
            self.assertEqual(_git(repo, "rev-list", "--count", "HEAD").strip(), "1")

    def test_jobs_flag_defaults_to_config(self):
        s = self.parallel_scratch([["a/"], ["b/"]])
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False}, "parallel": {"jobs": 2}})
        track = s.root.parent / "track.log"
        s.run("done", "--hours", "1", STUB_TRACK=str(track), STUB_SLEEP="2.5", STUB_PER_TASK="1")
        self.assertEqual(self.max_concurrency(track), 2)

    def test_ctrl_c_is_logged_as_interrupted_not_no_more_tasks(self):
        s = self.scratch()
        with mock.patch.object(tkrun.Runner, "run_task", side_effect=KeyboardInterrupt):
            code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 1)
        log = (s.root / ".tkrun" / "log.md").read_text(encoding="utf-8")
        self.assertIn("END (interrupted (Ctrl-C))", log)
        self.assertNotIn("END (no more ready tasks)", log)

    def test_runner_credentials_are_not_in_the_agent_environment(self):
        s = self.scratch()
        dump = s.root.parent / "envdump.txt"
        s.run("done", "--hours", "1", STUB_ENVDUMP=str(dump), TIDETABLE_TOKEN="tk-secret",
              KEEP_ME="1")
        self.assertEqual(dump.read_text(), "KEEP_ME")

    def test_agent_env_drop_hides_extra_names(self):
        s = self.scratch()
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}], "review": {"enabled": False},
                      "agent_env_drop": ["KEEP_*"]})
        dump = s.root.parent / "envdump.txt"
        s.run("done", "--hours", "1", STUB_ENVDUMP=str(dump), KEEP_ME="1")
        self.assertEqual(dump.read_text(), "")

    def test_gate_steps_do_not_see_runner_credentials(self):
        s = self.scratch()
        dump = s.root.parent / "gate-envdump.txt"
        names = ("TIDETABLE_TOKEN", "TIDETABLE_SERVER", "TASKERKEEPER_TOKEN", "TASKERKEEPER_SERVER", "TK_API_TOKEN")
        probe = ("import os,pathlib; pathlib.Path(%r).write_text(','.join(sorted(k for k in os.environ "
                 "if k in %r)))" % (str(dump), names))
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", probe]}], "review": {"enabled": False}})
        dump.unlink(missing_ok=True)
        s.run("done", "--hours", "1", TIDETABLE_TOKEN="tk-secret", TK_API_TOKEN="tk-api")
        self.assertTrue(dump.exists(), "gate step did not run")
        self.assertEqual(dump.read_text(), "")

    def test_prompt_forbids_printing_secrets_and_env_dumps_are_denied(self):
        s = self.scratch()
        code, out = s.run("done", "--hours", "1", "--dry-run")
        self.assertIn("Never print, echo, cat or otherwise output environment variables", out)
        self.assertIn("printenv*", "".join(tkrun.BASE_SHELL_DENY))
        self.assertIn("env", tkrun.BASE_SHELL_DENY)

    def test_custom_command_can_opt_into_the_runners_shell_permissions(self):
        s = self.scratch()
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}], "review": {"enabled": False},
                      "agent_commands": {"viaoc": {"cmd": ["echo", "{prompt}"],
                                                   "opencode_permissions": True}}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["tkrun", "run", str(s.todo), "--model", "viaoc/m", "--dry-run"])
        self.assertIn('"permission"', buf.getvalue())
        self.assertIn('"env": "deny"', buf.getvalue())

    def test_opencode_extra_is_merged_into_the_generated_config(self):
        s = self.scratch()
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}], "review": {"enabled": False},
                      "agent_commands": {"viaoc": {"cmd": ["echo", "{prompt}"],
                                                   "opencode_permissions": True}},
                      "opencode_extra": {"mcp": {"blender": {"enabled": False}},
                                         "permission": {"webfetch": "deny"}}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["tkrun", "run", str(s.todo), "--model", "viaoc/m", "--dry-run"])
        out = buf.getvalue()
        self.assertIn('"blender"', out)
        self.assertIn('"webfetch": "deny"', out)
        self.assertIn('"env": "deny"', out)

    def test_a_stale_stop_file_does_not_end_the_next_run(self):
        s = self.scratch(tasks=2)
        stop = s.root / ".tkrun" / "STOP"
        stop.parent.mkdir(exist_ok=True)
        stop.write_text("stop", encoding="utf-8")
        code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("ignored a stale STOP file", out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "done")

    def test_an_empty_leftover_worktree_folder_is_reused(self):
        s = self.scratch()
        s.work.mkdir()
        code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status(), "done")

    def test_opencode_config_denies_publishing_and_secrets_but_not_local_git(self):
        s = self.scratch()
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}], "review": {"enabled": False},
                      "agent_commands": {"viaoc": {"cmd": ["echo", "{prompt}"],
                                                   "opencode_permissions": True}}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["tkrun", "run", str(s.todo), "--model", "viaoc/m", "--dry-run"])
        out = buf.getvalue()
        i = out.index("----- opencode.json")
        cfg = json.loads(out[i:out.index("----- prompt")].split("\n", 1)[1])
        bash = cfg["permission"]["bash"]
        self.assertEqual(bash["*"], "ask")   # no adjudicator, so a gray-zone command prompts
        for denied in ("git push*", "rtk git push*", "printenv*", "tidetable done*", "env"):
            self.assertEqual(bash[denied], "deny", denied)
        for local in ("git commit*", "git checkout*", "git stash*"):
            self.assertNotIn(local, bash)
        ext = cfg["permission"]["external_directory"]
        self.assertEqual(ext["*"], "deny")
        for skills in ("agents", "claude"):
            tail = os.sep.join([f".{skills}", "skills", "**"])
            self.assertTrue(any(k.endswith(os.sep + tail) and v == "allow" for k, v in ext.items()), skills)

    def test_agent_commits_are_folded_back_and_the_runner_commits_the_task(self):
        s = self.scratch()
        code, out = s.run("done", "--hours", "1", STUB_COMMIT="1")
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status(), "done")
        subjects = _git(s.root, "log", s.branch, "--format=%s")
        self.assertIn("1.1: add work file", subjects)
        self.assertNotIn("agent own commit", subjects)

    def test_push_option_publishes_the_run_branch_only(self):
        s = self.scratch()
        remote = s.root.parent / "remote.git"
        _git(s.root.parent, "init", "--bare", "-q", str(remote))
        _git(s.root, "remote", "add", "origin", str(remote))
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}], "review": {"enabled": False},
                      "push": True})
        code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 0, out)
        heads = _git(remote, "branch", "--list")
        self.assertIn(s.branch, heads)
        self.assertNotIn("main", heads)

    def test_a_crashed_parallel_job_keeps_its_worktree_and_branch(self):
        s = self.parallel_scratch([["a/"], ["b/"]])
        real = tkrun.Runner.run_task

        def crash_second(runner, task):
            if task["id"] == "1.2":
                (runner.root / "partial.txt").write_text("unfinished work", encoding="utf-8")
                raise RuntimeError("boom")
            return real(runner, task)
        with mock.patch.object(tkrun.Runner, "run_task", crash_second):
            code, out = s.run("done", "--hours", "1", "--jobs", "2", STUB_PER_TASK="1")
        self.assertIn("KEEP 1.2", out)
        kept = s.root.parent / f"{s.root.name}-tkrun-{datetime.now():%Y-%m-%d}-1.2"
        self.assertTrue((kept / "partial.txt").exists(), "the crashed job's work is still there")
        self.assertEqual(s.status("1.1"), "done")

    def test_task_flag_runs_only_the_named_task(self):
        s = self.scratch(tasks=3)
        code, out = s.run("done", "--hours", "1", "--task", "1.2")
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status("1.2"), "done")
        self.assertEqual(s.status("1.1"), "pending")
        self.assertEqual(s.status("1.3"), "pending")

    def test_task_flag_is_repeatable_and_works_with_jobs(self):
        s = self.parallel_scratch([["a/"], ["b/"], ["c/"]])
        code, out = s.run("done", "--hours", "1", "--jobs", "2", "--task", "1.1", "--task", "1.3",
                          STUB_PER_TASK="1")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            [s.status(t) for t in ("1.1", "1.2", "1.3")], ["done", "pending", "done"])

    def test_task_flag_naming_nothing_runnable_ends_cleanly(self):
        s = self.scratch()
        code, out = s.run("done", "--hours", "1", "--task", "9.9")
        self.assertEqual(code, 0, out)
        self.assertIn("no more ready tasks", out)
        self.assertEqual(s.status(), "pending")

    def test_required_changed_downgrades_done(self):
        s = self.scratch()
        s.set_config({"gate": [{"cmd": [sys.executable, "-c", "pass"]}],
                      "required_changed": ["CHANGELOG.md"], "review": {"enabled": False}})
        s.run("done")
        self.assertEqual(s.status(), "in_progress")

    def test_two_consecutive_failures_end_the_run(self):
        s = self.scratch(tasks=4)
        code, out = s.run("failed")
        self.assertEqual(code, 1)
        self.assertIn("2 consecutive failures", out)
        self.assertEqual(s.status("1.3"), "pending")

    def test_usage_limit_past_deadline_returns_task_to_pending(self):
        s = self.scratch()
        # the stub's reset is an hour away; the budget is ~0
        _, out = s.run("limit", "--hours", "0", "--minutes", "1")
        self.assertIn("RATE LIMIT", out)
        self.assertEqual(s.status(), "pending")

    def test_dirty_or_off_base_checkout_is_fine_because_the_run_uses_a_worktree(self):
        s = self.scratch()
        (s.root / "dirty.txt").write_text("x")
        _git(s.root, "switch", "-c", "feature")
        code, out = s.run("done")
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status(), "done")
        self.assertEqual(_git(s.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "feature")
        self.assertTrue((s.root / "dirty.txt").exists())
        self.assertFalse((s.root / "work.txt").exists())

    def test_refuses_uncommitted_todo_changes(self):
        s = self.scratch()
        s.todo.write_text(s.todo.read_text(encoding="utf-8") + " ", encoding="utf-8")
        code, out = s.run("done")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)

    def test_sibling_repo_is_a_worktree_too_and_real_checkout_is_untouched(self):
        s = self.scratch()
        sib = s.root.parent / "sib"
        sib.mkdir()
        _git(sib, "init", "-b", "work")
        _git(sib, "config", "user.email", "t@example.com")
        _git(sib, "config", "user.name", "T")
        (sib / "lib").mkdir()
        (sib / "lib" / "a.txt").write_text("a")
        _git(sib, "add", "-A")
        _git(sib, "commit", "-q", "-m", "init")
        s.set_config({"gate": [{"name": "g", "cmd": [sys.executable, "-c", "pass"]},
                               {"name": "sib-gate", "repo": "../sib", "when_repo_changed": "../sib",
                                "cmd": [sys.executable, "-c", "pass"]}],
                      "review": {"enabled": False}, "siblings": [{"path": "../sib", "scope": ["lib/"]}]})
        sib_wt = sib.parent / f"sib-tkrun-{datetime.now():%Y-%m-%d}"
        code, out = s.run("done", STUB_SIB_FILE=str(sib_wt / "lib" / "b.txt"))
        self.assertEqual(code, 0, out)
        self.assertEqual(s.status(), "done")
        self.assertEqual(_git(sib, "rev-parse", "--abbrev-ref", "HEAD").strip(), "work")
        self.assertFalse((sib / "lib" / "b.txt").exists())
        self.assertTrue((sib_wt / "lib" / "b.txt").exists())
        self.assertIn("1.1", _git(sib, "log", s.branch, "--format=%s"))

    def test_second_run_resumes_the_same_worktree(self):
        s = self.scratch(tasks=2)
        s.run("done", "--hours", "1")
        self.assertEqual(s.status("1.2"), "done")
        code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("no more ready tasks", out)

    def test_stop_file_written_during_a_task_ends_the_run_after_it(self):
        s = self.scratch(tasks=2)
        stop = s.root / ".tkrun" / "STOP"
        stop.parent.mkdir(exist_ok=True)
        code, out = s.run("done", "--hours", "1", STUB_SENTINEL=str(stop))
        self.assertEqual(code, 0)
        self.assertIn("STOP file", out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "pending")

    def test_kill_file_written_during_a_task_ends_the_run_after_it(self):
        s = self.scratch(tasks=2)
        kill = s.root / ".tkrun" / "KILL"
        kill.parent.mkdir(exist_ok=True)
        code, out = s.run("done", "--hours", "1", STUB_SENTINEL=str(kill))
        self.assertEqual(code, 0)
        self.assertIn("KILL file", out)
        self.assertEqual(s.status("1.1"), "done")
        self.assertEqual(s.status("1.2"), "pending")

    def test_a_stale_kill_file_is_ignored_too(self):
        s = self.scratch()
        kill = s.root / ".tkrun" / "KILL"
        kill.parent.mkdir(exist_ok=True)
        kill.write_text("kill", encoding="utf-8")
        code, out = s.run("done", "--hours", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("ignored a stale KILL file", out)
        self.assertEqual(s.status(), "done")

    def test_kill_without_pid_file_writes_sentinel(self):
        import types

        s = self.scratch()
        code = tkrun.cmd_kill(types.SimpleNamespace(todo_file=str(s.todo), local=True))
        self.assertEqual(code, 0)
        self.assertTrue((s.root / ".tkrun" / "KILL").exists())

    def test_kill_with_stale_pid_cleans_up(self):
        import types

        s = self.scratch()
        (s.root / ".tkrun").mkdir()
        (s.root / ".tkrun" / "pid").write_text("2147483647", encoding="ascii")
        code = tkrun.cmd_kill(types.SimpleNamespace(todo_file=str(s.todo), local=True))
        self.assertEqual(code, 0)
        self.assertTrue((s.root / ".tkrun" / "KILL").exists())
        self.assertFalse((s.root / ".tkrun" / "pid").exists())

    def test_dry_run_changes_nothing(self):
        s = self.scratch()
        code, out = s.run("done", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("TKRUN_RESULT", out)
        self.assertEqual(_git(s.root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertEqual(s.status(), "pending")

    def test_no_gate_and_no_review_refuses(self):
        s = self.scratch()
        s.set_config({"gate": [], "review": {"enabled": False}})
        code, out = s.run("done")
        self.assertEqual(code, 1)
        self.assertIn("nothing would verify", out)

    def test_symlinked_root_still_resolves_the_todo_path(self):
        # A root that is not already canonical (a symlink, or macOS /var under
        # /private/var) must not break todo_rel: __post_init__ resolves the todo
        # path, so it must resolve home the same way or relative_to() raises.
        s = self.scratch()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        link = Path(tmp.name) / "link"
        link.symlink_to(s.root)
        c = json.loads(json.dumps(tkrun.DEFAULTS))
        r = tkrun.Runner(todo=link / s.todo.name, root=link, cfg=c,
                             date="2026-01-01", deadline=datetime.now())
        self.assertEqual(r.home, s.root.resolve())
        self.assertEqual(r.todo_rel, s.todo.name)


class TKRunUnitTests(unittest.TestCase):
    def runner(self, **cfg):
        c = json.loads(json.dumps(tkrun.DEFAULTS))
        c.update(cfg)
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "t.json").write_text("{}")
            return tkrun.Runner(todo=root / "t.json", root=root, cfg=c, date="2026-01-01",
                                    deadline=datetime.now())

    def test_rate_limit_parsing(self):
        r = self.runner()
        r.rate_margin = 0
        now = datetime(2026, 1, 1, 10, 0, 0)
        self.assertIsNone(r.rate_limit_reset("all good", now))
        self.assertEqual(r.rate_limit_reset("HTTP 429", now), datetime(2026, 1, 1, 10, 15, 0))
        self.assertEqual(r.rate_limit_reset("limit reached, resets 3am", now),
                         datetime(2026, 1, 2, 3, 0, 0))
        self.assertEqual(r.rate_limit_reset("limit reached, resets at 14:30", now),
                         datetime(2026, 1, 1, 14, 30, 0))
        self.assertEqual(int(r.rate_limit_reset("usage limit reached|1900000000", now).timestamp()),
                         1900000000)

    def test_deadline_never_shortens_a_started_agent_run(self):
        r = self.runner()
        r.deadline = datetime.now()          # already past
        r.task_sec = 7200
        with TemporaryDirectory() as d:
            r.root = r.home = Path(d)
            r.run_dir = Path(d)
            seen = {}

            def fake(argv, cwd, stdin=None, timeout=0, env=None, follow=False, prefix="", drop=None):
                seen["timeout"] = timeout
                return tkrun.Proc(0, "TKRUN_RESULT: DONE", "")
            with mock.patch.object(tkrun, "run_proc", fake),                     mock.patch.object(r, "assert_safe"):
                r.run_agent("anthropic", "m", lambda _: "p", "t")
        self.assertEqual(seen["timeout"], 7200)

    def test_matches_globs_and_dirs(self):
        self.assertTrue(tkrun.matches("src/a/b.rs", ["src/"]))
        self.assertTrue(tkrun.matches("src/a/b.rs", ["*.rs"]))
        self.assertFalse(tkrun.matches("docs/a.md", ["src/", "*.rs"]))

    # ── adjudicator (decision model is stubbed; no live call) ────────────────
    @staticmethod
    def _decision_stub(choice: str, confidence: float, probabilities: dict | None = None):
        """A local HTTP server answering like the Jev /v1/systemone endpoint."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                seen.append(json.loads(self.rfile.read(n).decode("utf-8")))
                body = json.dumps({"answers": {"q": {"choice": choice, "confidence": confidence,
                                                     "probabilities": probabilities
                                                     or {"allow": confidence, "deny": 1 - confidence}}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # keep the test output clean
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, seen

    def test_adjudicator_allows_and_denies(self):
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": True}
        for choice, want in (("allow", "allow"), ("deny", "deny")):
            srv, seen = self._decision_stub(choice, 0.97)
            try:
                with mock.patch.dict(os.environ, {"TIDETABLE_DECISION_ENDPOINT": f"http://127.0.0.1:{srv.server_port}/v1/systemone",
                                                  "TIDETABLE_DECISION_API_KEY": "k",
                                                  "TIDETABLE_DECISION_MODEL": "clef-test"}):
                    v = tkrun.decide_command("cargo test", adj)
            finally:
                srv.shutdown()
                srv.server_close()
            self.assertEqual(v["decision"], want)
            self.assertEqual(v["confidence"], 0.97)
            self.assertEqual(seen[0]["model"], "clef-test")
            self.assertEqual(seen[0]["state"], "cargo test")

    def test_adjudicator_fails_closed(self):
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": True}
        with mock.patch.dict(os.environ, {"TIDETABLE_DECISION_ENDPOINT": "", "TIDETABLE_DECISION_API_KEY": "",
                                          "TIDETABLE_DECISION_MODEL": ""}, clear=False):
            v = tkrun.decide_command("rm -rf /", adj)
        self.assertEqual(v["decision"], "deny")
        self.assertIn("missing env", v["reason"])
        # disabled is a deny too (never a silent allow)
        self.assertEqual(tkrun.decide_command("ls", {**adj, "enabled": False})["decision"], "deny")

    def test_adjudicator_denies_low_confidence_and_ambiguous_allow(self):
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": True, "min_confidence": 0.8}
        srv, _ = self._decision_stub("allow", 0.5, {"allow": 0.5, "deny": 0.5})
        try:
            with mock.patch.dict(os.environ, {"TIDETABLE_DECISION_ENDPOINT": f"http://127.0.0.1:{srv.server_port}/x",
                                              "TIDETABLE_DECISION_API_KEY": "k", "TIDETABLE_DECISION_MODEL": "m"}):
                v = tkrun.decide_command("maybe", adj)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(v["decision"], "deny")

    def test_adjudicator_lists_short_circuit_the_model(self):
        # A listed command must not reach the model: point the endpoint at a dead port and
        # assert the list still decides. Deny wins when a command matches both.
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": True}
        with mock.patch.dict(os.environ, {"TIDETABLE_DECISION_ENDPOINT": "http://127.0.0.1:9/x",
                                          "TIDETABLE_DECISION_API_KEY": "k", "TIDETABLE_DECISION_MODEL": "m"}):
            allow = tkrun.decide_command("git status", adj, allow=["git status*"], deny=["git push*"])
            deny = tkrun.decide_command("git push origin", adj, allow=["git push*"], deny=["git push*"])
            gray = tkrun.decide_command("frobnicate", adj, allow=["git status*"], deny=["git push*"])
        self.assertEqual(allow["decision"], "allow")
        self.assertIn("allow list", allow["reason"])
        self.assertEqual(deny["decision"], "deny")   # a pattern on both lists denies
        self.assertEqual(gray["decision"], "deny")   # gray zone reaches the dead model -> closed

    @staticmethod
    def _flaky_decision_stub(statuses: list[int]):
        """Answers each POST with the next status; a 200 carries a clean allow."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hits: list[int] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                status = statuses[min(len(hits), len(statuses) - 1)]
                hits.append(status)
                body = json.dumps({"answers": {"q": {"choice": "allow", "confidence": 0.97,
                                                     "probabilities": {"allow": 0.97, "deny": 0.03}}}}
                                  if status == 200 else {"error": "busy"}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, hits

    def _decide_against(self, statuses: list[int]) -> tuple[dict, list[int]]:
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": True}
        srv, hits = self._flaky_decision_stub(statuses)
        try:
            with mock.patch.dict(os.environ, {
                    "TIDETABLE_DECISION_ENDPOINT": f"http://127.0.0.1:{srv.server_port}/v1/systemone",
                    "TIDETABLE_DECISION_API_KEY": "k", "TIDETABLE_DECISION_MODEL": "m"}), \
                    mock.patch.object(tkrun, "ADJUDICATOR_BACKOFF_SEC", 0.01):
                return tkrun.decide_command("frobnicate", adj), hits
        finally:
            srv.shutdown()
            srv.server_close()

    def test_adjudicator_retries_a_transient_server_fault(self):
        v, hits = self._decide_against([507, 500, 200])
        self.assertEqual(v["decision"], "allow")
        self.assertEqual(hits, [507, 500, 200])

    def test_adjudicator_fails_closed_when_the_fault_persists(self):
        v, hits = self._decide_against([507])
        self.assertEqual(v["decision"], "deny")
        self.assertIn("507", v["reason"])
        self.assertEqual(len(hits), tkrun.ADJUDICATOR_ATTEMPTS)

    def test_adjudicator_does_not_retry_a_client_error(self):
        v, hits = self._decide_against([404, 200])
        self.assertEqual(v["decision"], "deny")
        self.assertEqual(hits, [404])

    def test_adjudicator_enabled_by_flag_without_config(self):
        self.assertFalse(self.runner().adj_enabled)          # off by default
        r2 = self.runner(adjudicator={"enabled": True})
        self.assertTrue(r2.adj_enabled)
        self.assertEqual(r2.adjudicator["model_env"], "TIDETABLE_DECISION_MODEL")

    def test_opencode_config_adds_the_bridge_only_when_enabled(self):
        off = json.loads(self.runner().opencode_config())
        self.assertNotIn("plugin", off)
        r = self.runner(adjudicator={"enabled": True})
        on = json.loads(r.opencode_config())
        # this repo carries no integrations/ tree, so the bundled bridge is what loads
        self.assertEqual(on["plugin"], [str(tkrun.BUNDLED_OPENCODE_PLUGIN)])
        self.assertTrue(Path(on["plugin"][0]).is_dir())
        self.assertEqual(on["permission"]["bash"]["*"], "allow")

    def test_bundled_bridge_is_a_plugin_directory_opencode_can_load(self):
        # opencode 2.x rejects a bare file ("configured plugin path must be a directory")
        # and loads <dir>/server.ts, so the config must name the directory.
        d = tkrun.BUNDLED_OPENCODE_PLUGIN
        self.assertTrue(d.is_dir())
        self.assertTrue((d / "server.ts").is_file())

    def test_bundled_bridge_matches_the_integration_copy(self):
        """The shipped plugin is a duplicate of integrations/opencode/tk-adjudicator.ts; no drift."""
        src = Path(tkrun.__file__).resolve().parent.parent / "integrations" / "opencode" / "tk-adjudicator.ts"
        if not src.is_file():
            self.skipTest("not a TideTable checkout")
        self.assertEqual(src.read_bytes(), (tkrun.BUNDLED_OPENCODE_PLUGIN / "server.ts").read_bytes())

    def test_gray_zone_is_decided_by_the_adjudicator_or_prompted(self):
        """Adjudicator on -> the hook decides (bash allow); off -> ask, so the person does."""
        off = json.loads(self.runner().opencode_config())          # enabled: False by default
        self.assertEqual(off["permission"]["bash"]["*"], "ask")
        self.assertNotIn("plugin", off)
        on = json.loads(self.runner(adjudicator={"enabled": True}).opencode_config())
        self.assertEqual(on["permission"]["bash"]["*"], "allow")
        self.assertIn("plugin", on)

    def test_claude_prompts_for_the_gray_zone_only_without_the_adjudicator(self):
        off = self.runner().agent_call("anthropic", "m", "prompt")["argv"]
        self.assertNotIn("--permission-prompts", off)
        self.assertIn("default", off)                    # the asking permission mode
        on = self.runner(adjudicator={"enabled": True}).agent_call("anthropic", "m", "prompt")["argv"]
        self.assertIn("none", on)                        # decisions are automatic; no prompts
        self.assertIn("acceptEdits", on)

    def test_claude_routes_bash_through_the_adjudicator_hook(self):
        off = self.runner().agent_call("anthropic", "m", "prompt")["argv"]
        self.assertNotIn("--settings", off)
        r = self.runner(adjudicator={"enabled": True})
        on = r.agent_call("anthropic", "m", "prompt")["argv"]
        self.assertIn("--permission-prompts", on)        # still never asks
        hook = json.loads(on[on.index("--settings") + 1])["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Bash")
        cmd = hook["hooks"][0]["command"]
        self.assertIn(tkrun.CLAUDE_HOOK.as_posix(), cmd)
        self.assertNotIn("\\", cmd)                      # POSIX shell: forward slashes only
        self.assertTrue(tkrun.CLAUDE_HOOK.is_file())
        # a read-only review never runs commands the adjudicator would need to judge
        rv = r.agent_call("anthropic", "m", "prompt", kind="review")["argv"]
        self.assertNotIn("--settings", rv)

    def _serve_adjudicator(self, **lists):
        """Run the real loopback service; returns (host, port, stop)."""
        import threading
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        stop = threading.Event()
        adj = {**tkrun.ADJUDICATOR_DEFAULTS, "enabled": False}   # lists only: no model call
        server = {"srv": srv, "stop": stop, "record": None, **lists}
        t = threading.Thread(target=tkrun._adj_serve, args=(server, adj, ""), daemon=True)
        t.start()

        def shut():
            stop.set()
            t.join(timeout=3)
            srv.close()
        return "127.0.0.1", srv.getsockname()[1], shut

    def _run_claude_hook(self, event: dict, host: str = "", port: int = 0) -> dict | None:
        """The hook as Claude Code runs it: event on stdin, verdict JSON on stdout."""
        import io
        from contextlib import redirect_stdout
        from tidetable import claude_hook
        env = {"TIDETABLE_ADJUDICATOR_HOST": host, "TIDETABLE_ADJUDICATOR_PORT": str(port)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(event))), redirect_stdout(out):
            self.assertEqual(claude_hook.main(), 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def test_claude_hook_allows_and_denies_through_the_service(self):
        host, port, shut = self._serve_adjudicator(allow=["cargo test*"], deny=["git push*"])
        try:
            ok = self._run_claude_hook({"tool_name": "Bash", "tool_input": {"command": "cargo test -p x"}},
                                       host, port)
            bad = self._run_claude_hook({"tool_name": "Bash", "tool_input": {"command": "git push origin"}},
                                        host, port)
            gray = self._run_claude_hook({"tool_name": "Bash", "tool_input": {"command": "frobnicate"}},
                                         host, port)
        finally:
            shut()
        self.assertEqual(ok["permissionDecision"], "allow")
        self.assertEqual(bad["permissionDecision"], "deny")
        self.assertEqual(gray["permissionDecision"], "deny")   # model disabled/unreachable -> closed
        self.assertEqual(ok["hookEventName"], "PreToolUse")
        for v in (ok, bad, gray):
            self.assertNotEqual(v["permissionDecision"], "ask")   # never leaves a prompt

    def test_claude_hook_fails_closed_and_ignores_other_tools(self):
        bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        with TemporaryDirectory() as d:
            # no env address and no discovery file anywhere above the cwd -> deny
            v = self._run_claude_hook({**bash, "cwd": d})
        self.assertEqual(v["permissionDecision"], "deny")
        # a dead service is a deny, not an error
        v = self._run_claude_hook(bash, "127.0.0.1", 9)
        self.assertEqual(v["permissionDecision"], "deny")
        # only Bash is judged
        self.assertIsNone(self._run_claude_hook({"tool_name": "Edit", "tool_input": {"file_path": "x"}}))

    def test_claude_hook_is_judged_against_claudes_stricter_deny_list(self):
        """opencode may commit locally; claude may not, so `git commit` is denied for claude only."""
        host, port, shut = self._serve_adjudicator(allow=["git commit*"], deny=[],
                                                   deny_claude=["git commit*"])
        try:
            event = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
            via_claude = self._run_claude_hook(event, host, port)
            with socket.create_connection((host, port), timeout=5) as s:
                s.sendall(b'{"command": "git commit -m x"}\n')
                via_opencode = json.loads(s.makefile().readline())
        finally:
            shut()
        self.assertEqual(via_claude["permissionDecision"], "deny")
        self.assertEqual(via_opencode["decision"], "allow")

    def test_legacy_overnight_names_still_resolve(self):
        """A repo that predates the rename keeps its config and its control directory."""
        # config: tkrun.json wins, else the old overnight.json is read
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / ".tidetable").mkdir()
            (root / ".tidetable" / "overnight.json").write_text('{"project": "old"}')
            cfg, path = tkrun.load_config(root)
            self.assertEqual(cfg["project"], "old")
            self.assertEqual(path.name, "overnight.json")
            (root / ".tidetable" / "tkrun.json").write_text('{"project": "new"}')
            self.assertEqual(tkrun.load_config(root)[0]["project"], "new")
        # the compat module is the same object as tkrun, so both names patch one thing
        from tidetable import overnight as compat
        self.assertIs(compat, tkrun)

    def test_stop_and_kill_control_a_legacy_overnight_run_dir(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / ".overnight").mkdir()                # only the pre-0.11 dir exists
            self.assertEqual(tkrun.control_dir(root), root / ".overnight")
            (root / ".tkrun").mkdir()
            self.assertEqual(tkrun.control_dir(root), root / ".tkrun")

    def test_unknown_config_key_is_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / ".tidetable").mkdir()
            (root / ".tidetable" / "tkrun.json").write_text('{"gates": []}')
            with self.assertRaises(ValueError):
                tkrun.load_config(root)

    def test_stack_detection_gives_zero_config_gate(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "Cargo.toml").write_text("")
            cfg, path = tkrun.load_config(root)
            self.assertIsNone(path)
            self.assertEqual([s["name"] for s in cfg["gate"]], ["clippy", "test"])
            self.assertIn("cargo test*", cfg["shell_allow"])

    def test_prompt_keeps_state_changes_with_the_runner(self):
        r = self.runner(rules=["custom rule"])
        p = r.build_prompt({"id": "9.9", "title": "T", "success": ["window looks right"]}, "a/b", False)
        self.assertIn("custom rule", p)
        self.assertIn("tidetable\nstart/done/reset/status".replace("\n", " "), p)
        self.assertIn("window looks right", p.split("likely needing a human")[1])
        self.assertIn("git push*", r.shell_deny)


class LaneTests(unittest.TestCase):
    NOW = datetime(2026, 10, 7, 12, 30, tzinfo=__import__("datetime").timezone.utc)

    @staticmethod
    def rsv(start, end, **kw):
        return {"starts_at": f"2026-10-07T{start}:00Z", "ends_at": f"2026-10-07T{end}:00Z", **kw}

    def test_window_merges_consecutive_hours_and_ignores_cancelled(self):
        rs = [self.rsv("12:00", "13:00"), self.rsv("13:00", "14:00"),
              self.rsv("14:00", "15:00", cancelled_at="2026-10-06T00:00:00Z"), self.rsv("16:00", "17:00")]
        s, e = tkrun.lane_window(rs, self.NOW)
        self.assertEqual((s.hour, e.hour), (12, 14))

    def test_window_none_when_gap_or_empty(self):
        self.assertIsNone(tkrun.lane_window([], self.NOW))
        self.assertIsNone(tkrun.lane_window([self.rsv("13:00", "14:00")], self.NOW))
        self.assertIsNone(tkrun.lane_window([self.rsv("11:00", "12:30")], self.NOW))

    def runner(self, reservations, margin=20, fallback="opencode-go/muse-spark-1.3-contributor"):
        c = json.loads(json.dumps(tkrun.DEFAULTS))
        c["lanes"] = {"singularity": {"margin_minutes": margin, "fallback": fallback}}
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "t.json").write_text("{}")
            r = tkrun.Runner(todo=root / "t.json", root=root, cfg=c, date="2026-01-01",
                                 deadline=datetime.now())
        r.lane_reservations = lambda lane: reservations
        return r

    def test_route_uses_lane_until_margin_then_falls_back(self):
        model = "deepseek-ai/DeepSeek-V4.1-Flash"
        with mock.patch.object(tkrun, "datetime", wraps=datetime) as dt:
            dt.now.return_value = self.NOW
            dt.fromisoformat = datetime.fromisoformat
            r = self.runner([self.rsv("12:00", "13:00")], margin=20)       # 30 min left
            self.assertEqual(r.lane_route("singularity", model), ("singularity", model))
            r = self.runner([self.rsv("12:00", "13:00")], margin=40)       # inside the margin
            self.assertEqual(r.lane_route("singularity", model),
                             ("opencode-go", "muse-spark-1.3-contributor"))

    def test_route_falls_back_when_reservations_unreadable(self):
        r = self.runner([])
        def boom(lane):
            raise RuntimeError("no key")
        r.lane_reservations = boom
        self.assertEqual(r.lane_route("singularity", "m")[0], "opencode-go")
        self.assertEqual(r.lane_route("anthropic", "x"), ("anthropic", "x"))

    def test_fallback_is_required(self):
        with self.assertRaises(ValueError):
            self.runner([], fallback="")

    def test_agent_call_keeps_secrets_out_of_config(self):
        r = self.runner([])
        call = r.agent_call("singularity", "deepseek-ai/DeepSeek-V4.1-Flash", "do it")
        self.assertIn("singularity/deepseek-ai/DeepSeek-V4.1-Flash", call["argv"])
        cfg = json.loads(call["opencode"])
        opts = cfg["provider"]["singularity"]["options"]
        self.assertEqual(opts["apiKey"], "{env:SINGULARITY_LANE_APIKEY_SECRET}")
        self.assertEqual(opts["baseURL"], "{env:SINGULARITY_API_LANE_ENDPOINT}")
        self.assertEqual(cfg["provider"]["singularity"]["models"]["deepseek-ai/DeepSeek-V4.1-Flash"]
                         ["options"], {"reasoning_effort": "none"})


if __name__ == "__main__":
    unittest.main()


class HubModeTests(unittest.TestCase):
    """`--server`: the todo lives on a hub, the repo and worktree stay local."""

    TOKEN = "hub-test-token"

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from tidetable import serve

        self.s = Scratch()
        self.addCleanup(self.s.close)
        hub_dir = Path(self.s._tmp.name) / "hub"
        hub_dir.mkdir()
        self.hub_todo = hub_dir / "todo.json"
        self.hub_todo.write_text(self.s.todo.read_text(encoding="utf-8"), encoding="utf-8")
        registry = hub_dir / "registry.json"
        registry.write_text(json.dumps({"projects": [
            {"slug": "hub", "name": "Hub", "repo_url": "", "todo_path": str(self.hub_todo),
             "default_branch": "main"}]}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
        server.daemon_threads = True
        server.registry = str(registry)
        server.sessions_db = str(hub_dir / "sessions.db")
        server.tokens_db = str(hub_dir / "tokens.db")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"

    def run_hub(self, mode: str = "done", *extra: str, token: str | None = None) -> tuple[int, str]:
        env = {**self.s.env, "STUB_MODE": mode, "STUB_REVIEW": "PASS", "TK_API_TOKEN": self.TOKEN}
        argv = ["--server", self.url, "--token", self.TOKEN if token is None else token,
                "tkrun", "run", "hub", "--repo", str(self.s.root),
                "--model", "anthropic/stub", *(extra or ("--hours", "1"))]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def hub_status(self, tid: str = "1.1") -> str:
        data = json.loads(self.hub_todo.read_text(encoding="utf-8"))
        return next(t["status"] for p in data["phases"] for t in p["tasks"] if t["id"] == tid)

    def test_done_closes_the_task_on_the_hub_and_commits_code_locally(self):
        code, out = self.run_hub("done")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub_status(), "done")
        # the repo's own todo.json is not the todo; nothing of it was committed or changed
        self.assertEqual(json.loads(self.s.todo.read_text(encoding="utf-8"))
                         ["phases"][0]["tasks"][0]["status"], "pending")
        self.assertIn("1.1: add work file", _git(self.s.root, "log", self.s.branch, "--format=%s"))
        self.assertTrue((self.s.work / "work.txt").exists())
        self.assertEqual(_git(self.s.work, "status", "--porcelain").strip(), "")
        self.assertNotIn("todo.json", _git(self.s.root, "log", f"main..{self.s.branch}",
                                           "--name-only", "--format="))

    def test_owner_names_the_machine_so_two_runners_never_share_an_identity(self):
        import socket

        self.run_hub("done")
        events = (Path(str(self.hub_todo) + ".events.jsonl")).read_text(encoding="utf-8")
        owners = {json.loads(l).get("owner") for l in events.splitlines() if l.strip()}
        self.assertIn(f"tkrun-{datetime.now():%Y-%m-%d}@{socket.gethostname()}", owners)

    def test_gate_failure_returns_the_hub_task_to_pending(self):
        s = Scratch(gate_exit=1)
        self.addCleanup(s.close)
        self.s = s
        self.setUp_hub_for(s)
        code, out = self.run_hub("done")
        self.assertEqual(self.hub_status(), "pending")
        self.assertIn("gate", out)
        # a failed attempt on a hub must not commit "everything" as todo changes
        self.assertNotIn("chore(tkrun)", _git(s.root, "log", s.branch, "--format=%s"))

    def setUp_hub_for(self, scratch: "Scratch") -> None:
        self.hub_todo.write_text(scratch.todo.read_text(encoding="utf-8"), encoding="utf-8")

    def test_unreachable_hub_or_bad_token_aborts_before_any_work(self):
        code, out = self.run_hub("done", token="wrong")
        self.assertEqual(code, 1)
        self.assertIn("not reachable", out)
        self.assertFalse((self.s.work / "work.txt").exists())
        self.assertEqual(self.hub_status(), "pending")

    def test_local_flag_keeps_the_runner_off_a_server_in_the_environment(self):
        """--local wins over $TIDETABLE_SERVER, and the runner's CLI calls inherit that."""
        env = {**self.s.env, "STUB_MODE": "done", "STUB_REVIEW": "PASS",
               "TIDETABLE_SERVER": "http://127.0.0.1:1", "TIDETABLE_TOKEN": "x"}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(buf):
            code = cli.main(["--local", "tkrun", "run", str(self.s.todo),
                             "--model", "anthropic/stub", "--hours", "1"])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertEqual(self.s.status(), "done")
