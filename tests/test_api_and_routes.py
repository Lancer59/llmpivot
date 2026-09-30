import os
import tempfile
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llmpivot import PromptManager
from llmpivot.auth import generate_csrf_token


def _get_csrf(client: TestClient, session_cookie: str, secret_key: str) -> str:
    """Derive the CSRF token the server expects for this session."""
    return generate_csrf_token(session_cookie, secret_key)


SECRET = "test-secret-key"


class APIAndRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_api.db")

        self.manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
            llm_url="https://mock-llm.com/v1/chat/completions",
            llm_api_key="sk-mock",
        )

        self.manager.llm.suggest = AsyncMock(return_value="Improved system prompt version.")
        self.manager.llm.run = AsyncMock(return_value="Mock LLM execution output.")

        self.app = FastAPI()
        self.app.mount("/prompts", self.manager.mount_ui())
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _login(self):
        """Log in as admin and return (cookies_dict, csrf_token)."""
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())
        res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res.status_code, 303)
        session_cookie = res.cookies["llmpivot_session"]
        cookies = {"llmpivot_session": session_cookie}
        csrf = _get_csrf(self.client, session_cookie, SECRET)
        return cookies, csrf

    def test_unauthenticated_redirect(self):
        res = self.client.get("/prompts/list")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["location"].endswith("/prompts/login"))

    def test_admin_bootstrap_requires_explicit_opt_in(self):
        import asyncio
        manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key="another-test-secret",
        )
        asyncio.run(manager._bootstrap_admin())
        user = asyncio.run(manager.storage.get_user("admin", tenant_id="default"))
        self.assertIsNone(user)

    def test_secret_key_guard_raises_for_default(self):
        """PromptManager must raise ValueError when using the default key with rbac mode."""
        with self.assertRaises(ValueError):
            PromptManager(
                db_path=self.db_path,
                auth_mode="rbac",
                # no secret_key — will use the default public one
            )

    def test_login_flow(self):
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())

        res_bad = self.client.post("/prompts/login", data={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn("Invalid username or password", res_bad.text)

        res_ok = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res_ok.status_code, 303)
        self.assertIn("llmpivot_session", res_ok.cookies)

        cookies = {"llmpivot_session": res_ok.cookies["llmpivot_session"]}
        res_list = self.client.get("/prompts/list", cookies=cookies)
        self.assertEqual(res_list.status_code, 200)
        self.assertIn("Prompts", res_list.text)
        self.assertIn("👤 admin", res_list.text)

    def test_prompt_creation_and_viewing(self):
        cookies, csrf = self._login()

        create_res = self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "summary_test",
                "content": "Summarize this document:",
                "edited_by": "admin",
                "tag": "prod",
                "set_active": "1",
                "csrf_token": csrf,
            },
            cookies=cookies,
        )
        self.assertEqual(create_res.status_code, 303)

        detail_res = self.client.get("/prompts/detail/summary_test", cookies=cookies)
        self.assertEqual(detail_res.status_code, 200)
        self.assertIn("summary_test", detail_res.text)

    def test_csrf_rejected_on_missing_token(self):
        """POST without CSRF token must be rejected with 403."""
        cookies, _ = self._login()

        res = self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "csrf_test",
                "content": "Should be blocked",
                "edited_by": "admin",
                "tag": "",
                "set_active": "1",
                # no csrf_token
            },
            cookies=cookies,
        )
        self.assertEqual(res.status_code, 403)

    def test_llm_suggest_api(self):
        cookies, _ = self._login()

        res = self.client.post(
            "/prompts/api/suggest",
            json={"content": "Draft prompt content"},
            cookies=cookies,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["suggestion"], "Improved system prompt version.")

    def test_ab_run_api(self):
        cookies, csrf = self._login()

        self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "ab_prompt",
                "content": "Translate to Spanish:",
                "edited_by": "admin",
                "tag": "prod",
                "set_active": "1",
                "csrf_token": csrf,
            },
            cookies=cookies,
        )

        res = self.client.post(
            "/prompts/api/run",
            json={"version_id": 1, "input": "Hello world"},
            cookies=cookies,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["output"], "Mock LLM execution output.")

    def test_user_management_api(self):
        cookies, csrf = self._login()

        users_view = self.client.get("/prompts/users", cookies=cookies)
        self.assertEqual(users_view.status_code, 200)
        self.assertIn("User Management", users_view.text)

        new_user_res = self.client.post(
            "/prompts/users",
            data={
                "username": "editor_bob",
                "password": "Password123",
                "role": "editor",
                "email": "bob@team.com",
                "csrf_token": csrf,
            },
            cookies=cookies,
        )
        self.assertEqual(new_user_res.status_code, 200)
        self.assertIn("editor_bob", new_user_res.text)

    def test_healthz_returns_ok(self):
        """Healthz endpoint must return 200 with status=ok after startup."""
        res = self.client.get("/prompts/healthz")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("cache_size", data)
        self.assertIn("log_queue_depth", data)


if __name__ == "__main__":
    unittest.main()
