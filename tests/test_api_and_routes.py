import os
import tempfile
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llmpivot import PromptManager


class APIAndRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_api.db")
        
        self.manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key="test-secret-key",
            bootstrap_admin=True,
            bootstrap_password="admin",
            llm_url="https://mock-llm.com/v1/chat/completions",
            llm_api_key="sk-mock",
        )

        # Mock LLM client suggest and run
        self.manager.llm.suggest = AsyncMock(return_value="Improved system prompt version.")
        self.manager.llm.run = AsyncMock(return_value="Mock LLM execution output.")

        self.app = FastAPI()
        self.app.mount("/prompts", self.manager.mount_ui())
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_unauthenticated_redirect(self):
        # Accessing /prompts/list without logging in should redirect to /prompts/login
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

        user = asyncio.run(manager.storage.get_user("admin"))
        self.assertIsNone(user)

    def test_login_flow(self):
        # Bootstrap default admin check
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())

        # Invalid password
        res_bad = self.client.post("/prompts/login", data={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res_bad.status_code, 400)
        self.assertIn("Invalid username or password", res_bad.text)

        # Successful login
        res_ok = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res_ok.status_code, 303)
        self.assertIn("llmpivot_session", res_ok.cookies)

        # Authenticated session request to /prompts/list
        cookies = {"llmpivot_session": res_ok.cookies["llmpivot_session"]}
        res_list = self.client.get("/prompts/list", cookies=cookies)
        self.assertEqual(res_list.status_code, 200)
        self.assertIn("Prompts", res_list.text)
        self.assertIn("👤 admin", res_list.text)

    def test_prompt_creation_and_viewing(self):
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())

        # Login
        login_res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        cookies = {"llmpivot_session": login_res.cookies["llmpivot_session"]}

        # Create new prompt
        create_res = self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "summary_test",
                "content": "Summarize this document:",
                "edited_by": "admin",
                "tag": "prod",
                "set_active": "1",
            },
            cookies=cookies,
        )
        self.assertEqual(create_res.status_code, 303)

        # View detail
        detail_res = self.client.get("/prompts/detail/summary_test", cookies=cookies)
        self.assertEqual(detail_res.status_code, 200)
        self.assertIn("summary_test", detail_res.text)

    def test_llm_suggest_api(self):
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())
        login_res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        cookies = {"llmpivot_session": login_res.cookies["llmpivot_session"]}

        res = self.client.post(
            "/prompts/api/suggest",
            json={"content": "Draft prompt content"},
            cookies=cookies,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["suggestion"], "Improved system prompt version.")

    def test_ab_run_api(self):
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())
        login_res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        cookies = {"llmpivot_session": login_res.cookies["llmpivot_session"]}

        # Create version
        create_res = self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "ab_prompt",
                "content": "Translate to Spanish:",
                "edited_by": "admin",
                "tag": "prod",
                "set_active": "1",
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
        import asyncio
        asyncio.run(self.manager._bootstrap_admin())
        login_res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        cookies = {"llmpivot_session": login_res.cookies["llmpivot_session"]}

        # View users page
        users_view = self.client.get("/prompts/users", cookies=cookies)
        self.assertEqual(users_view.status_code, 200)
        self.assertIn("User Management", users_view.text)

        # Create new editor user
        new_user_res = self.client.post(
            "/prompts/users",
            data={
                "username": "editor_bob",
                "password": "Password123",
                "role": "editor",
                "email": "bob@team.com",
            },
            cookies=cookies,
        )
        self.assertEqual(new_user_res.status_code, 200)
        self.assertIn("User &#27;editor_bob&#27; created successfully", new_user_res.text.replace("'", "&#27;"))


if __name__ == "__main__":
    unittest.main()
