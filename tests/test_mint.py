"""Tests for `tidetable mint-token` and the hub's admin mint endpoint."""

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

from tidetable import mint
from tidetable import serve
from tidetable import tokens as tokens_store

ADMIN = "tk_admin_test_secret"


def write_registry(path: Path, slugs: list[str]) -> None:
    projects = [{"slug": s, "name": s, "repo_url": f"https://example.com/{s}",
                 "todo_path": f"projects/{s}/todo.json", "default_branch": "main"} for s in slugs]
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


class ValidateRequestTest(unittest.TestCase):
    def test_accepts_a_known_slug_with_scopes(self):
        req, reason = mint.validate_request(
            {"slugs": ["chiton"], "scopes": ["read", "write"], "days": 90}, {"chiton"})
        self.assertEqual(reason, "")
        self.assertEqual(req, {"slugs": ["chiton"], "scopes": ["read", "write"], "days": 90})

    def test_rejects_unknown_slug_when_registry_is_checked(self):
        _, reason = mint.validate_request(
            {"slugs": ["chtion"], "scopes": ["read"]}, {"chiton"})
        self.assertIn("unknown project slug 'chtion'", reason)

    def test_skips_the_registry_check_when_none_is_given(self):
        _, reason = mint.validate_request({"slugs": ["anything"], "scopes": ["read"]}, None)
        self.assertEqual(reason, "")

    def test_rejects_unknown_scope(self):
        _, reason = mint.validate_request({"slugs": ["a"], "scopes": ["admin"]}, None)
        self.assertIn("unknown scope 'admin'", reason)

    def test_rejects_empty_scopes_and_slugs(self):
        self.assertIn("scopes", mint.validate_request({"slugs": ["a"], "scopes": []}, None)[1])
        self.assertIn("slugs", mint.validate_request({"slugs": [], "scopes": ["read"]}, None)[1])

    def test_days_default_to_ninety_and_null_means_no_expiry(self):
        req, _ = mint.validate_request({"slugs": ["a"], "scopes": ["read"]}, None)
        self.assertEqual(req["days"], 90)
        req, _ = mint.validate_request({"slugs": ["a"], "scopes": ["read"], "days": None}, None)
        self.assertIsNone(req["days"])

    def test_rejects_non_positive_days(self):
        _, reason = mint.validate_request({"slugs": ["a"], "scopes": ["read"], "days": 0}, None)
        self.assertIn("positive", reason)


class LocalMintTest(unittest.TestCase):
    def test_minted_token_verifies_and_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            write_registry(registry, ["chiton"])
            request, reason = mint.validate_request(
                {"slugs": ["chiton"], "scopes": ["read", "write"], "days": 90},
                mint.registry_slugs(str(registry)))
            self.assertEqual(reason, "")
            db = str(Path(tmp) / "tokens.db")
            token = mint.mint_local(db, request)
            verified = tokens_store.verify_token(db, token)
            self.assertIsNotNone(verified)
            slugs, scopes = verified
            self.assertEqual(list(slugs), ["chiton"])
            self.assertIn("write", list(scopes))


class AdminEndpointTest(unittest.TestCase):
    """Drives the real hub handler over HTTP on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.registry = tmp / "registry.json"
        write_registry(cls.registry, ["chiton"])
        cls.tokens_db = str(tmp / "tokens.db")
        tokens_store.init_db(cls.tokens_db)
        cls._env = mock.patch.dict(os.environ, {"TK_API_TOKEN": ADMIN})
        cls._env.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
        cls.server.registry = str(cls.registry)
        cls.server.tokens_db = cls.tokens_db
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
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + "/api/admin/tokens", data=data,
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_no_token_is_refused(self):
        status, _ = self.post({"slugs": ["chiton"], "scopes": ["read"]}, None)
        self.assertEqual(status, 401)

    def test_a_machine_token_cannot_mint_more_tokens(self):
        _, machine, _ = tokens_store.mint_token(self.tokens_db, ["chiton"], ["read", "write"])
        status, _ = self.post({"slugs": ["chiton"], "scopes": ["read"]}, machine)
        self.assertEqual(status, 401)

    def test_admin_mints_a_token_that_verifies(self):
        status, payload = self.post({"slugs": ["chiton"], "scopes": ["read", "write"],
                                     "days": 30}, ADMIN)
        self.assertEqual(status, 201)
        self.assertEqual(payload["slugs"], ["chiton"])
        self.assertEqual(payload["expires_days"], 30)
        self.assertIsNotNone(tokens_store.verify_token(self.tokens_db, payload["token"]))

    def test_unknown_slug_is_a_400_and_mints_nothing(self):
        before = len(tokens_store.list_tokens(self.tokens_db))
        status, payload = self.post({"slugs": ["chtion"], "scopes": ["read"]}, ADMIN)
        self.assertEqual(status, 400)
        self.assertIn("unknown project slug", payload["error"])
        self.assertEqual(len(tokens_store.list_tokens(self.tokens_db)), before)


if __name__ == "__main__":
    unittest.main()
