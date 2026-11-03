"""Tests for `tidetable register-project` and the hub's admin registration endpoint."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tidetable import register
from tidetable import serve

ADMIN = "tk_admin_register_secret"


def write_registry(path: Path, slugs: list[str]) -> None:
    projects = [{"slug": s, "name": s, "repo_url": f"https://example.com/{s}",
                 "todo_path": f"projects/{s}/todo.json", "default_branch": "main"} for s in slugs]
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def entry(slug: str = "oarlock", **overrides) -> dict:
    body = {"slug": slug, "name": "Oarlock", "repo_url": "https://github.com/ClawfficeOrg/Oarlock",
            "todo_path": None, "default_branch": "main"}
    body.update(overrides)
    return body


class ValidateProjectTest(unittest.TestCase):
    def test_accepts_a_new_slug_and_defaults_the_todo_path(self):
        clean, reason = register.validate_project(entry(), {"chiton"})
        self.assertEqual(reason, "")
        self.assertEqual(clean["todo_path"], "projects/oarlock/todo.json")
        self.assertEqual(clean["name"], "Oarlock")

    def test_rejects_a_slug_that_is_already_registered(self):
        _, reason = register.validate_project(entry("chiton"), {"chiton"})
        self.assertIn("already registered", reason)

    def test_rejects_a_bad_slug(self):
        for bad in ["Oarlock", "a b", "-x", "", "x" * 64]:
            _, reason = register.validate_project(entry(bad), set())
            self.assertIn("slug must be", reason, bad)

    def test_rejects_a_plain_http_repo(self):
        _, reason = register.validate_project(entry(repo_url="http://example.com/x"), set())
        self.assertIn("https", reason)

    def test_rejects_a_todo_path_that_escapes_the_projects_directory(self):
        _, reason = register.validate_project(entry(todo_path="../../etc/todo.json"), set())
        self.assertIn("todo_path", reason)
        _, reason = register.validate_project(entry(todo_path="/abs/todo.json"), set())
        self.assertIn("todo_path", reason)

    def test_rejects_an_empty_name(self):
        _, reason = register.validate_project(entry(name="   "), set())
        self.assertIn("name", reason)


class AddProjectTest(unittest.TestCase):
    def test_appends_the_entry_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            write_registry(registry, ["chiton"])
            stored, reason = register.add_project(str(registry), entry())
            self.assertEqual(reason, "")
            slugs = [p["slug"] for p in json.loads(registry.read_text(encoding="utf-8"))["projects"]]
            self.assertEqual(slugs, ["chiton", "oarlock"])
            self.assertEqual(stored["slug"], "oarlock")

    def test_refuses_a_duplicate_and_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            write_registry(registry, ["chiton"])
            before = registry.read_text(encoding="utf-8")
            _, reason = register.add_project(str(registry), entry("chiton"))
            self.assertIn("already registered", reason)
            self.assertEqual(registry.read_text(encoding="utf-8"), before)


class AdminEndpointTest(unittest.TestCase):
    """Drives the real hub handler over HTTP on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.registry = Path(cls._tmp.name) / "registry.json"
        write_registry(cls.registry, ["chiton"])
        cls._env = mock.patch.dict(os.environ, {"TK_API_TOKEN": ADMIN})
        cls._env.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
        cls.server.registry = str(cls.registry)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()
        cls._tmp.cleanup()

    def post(self, body: dict, token: str | None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + "/api/admin/projects",
                                     data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_no_token_is_refused_and_nothing_is_written(self):
        before = self.registry.read_text(encoding="utf-8")
        status, _ = self.post(entry(), None)
        self.assertEqual(status, 401)
        self.assertEqual(self.registry.read_text(encoding="utf-8"), before)

    def test_admin_registers_a_project(self):
        status, payload = self.post(entry("oarlock-web"), ADMIN)
        self.assertEqual(status, 201)
        self.assertEqual(payload["slug"], "oarlock-web")
        slugs = [p["slug"] for p in json.loads(self.registry.read_text(encoding="utf-8"))["projects"]]
        self.assertIn("oarlock-web", slugs)

    def test_a_duplicate_is_a_409(self):
        status, payload = self.post(entry("chiton"), ADMIN)
        self.assertEqual(status, 409)
        self.assertIn("already registered", payload["error"])


if __name__ == "__main__":
    unittest.main()
