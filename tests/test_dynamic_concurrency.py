import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llmpivot import PromptManager, aget_prompt, aget_prompt_with_meta, log_prompt_usage
from llmpivot.auth import generate_csrf_token
from llmpivot.storage import MongoStorage

SECRET = "dynamic-test-secret"


class DynamicConcurrencyAndEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_dynamic.db")

        self.manager = PromptManager(
            db_path=self.db_path,
            cache_ttl=10,
            tenant_id="org_dynamic",
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
        )

        self.app = FastAPI()
        self.app.mount("/prompts", self.manager.mount_ui())
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _login(self):
        """Log in as admin and return (cookies_dict, csrf_token)."""
        asyncio.run(self.manager._bootstrap_admin())
        res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res.status_code, 303)
        session_cookie = res.cookies["llmpivot_session"]
        cookies = {"llmpivot_session": session_cookie}
        csrf = generate_csrf_token(session_cookie, SECRET)
        return cookies, csrf

    def test_concurrent_multi_user_traffic(self):
        """Simulate 50 concurrent requests fetching prompts and logging usage under heavy load."""
        async def run_stress_test():
            await self.manager.storage.create_version(
                name="rag_system_prompt",
                content="You are a helpful assistant. Context: {context}",
                created_by="system",
                tag="prod",
                set_active=True,
                tenant_id="org_dynamic",
            )
            self.manager.cache.invalidate("rag_system_prompt")

            async def user_request_worker(user_id: int):
                meta = await aget_prompt_with_meta("rag_system_prompt")
                self.assertIn("Context: {context}", meta["content"])
                log_prompt_usage(
                    "rag_system_prompt",
                    meta["version_id"],
                    input_text=f"User {user_id} query",
                    output_text=f"LLM response to user {user_id}",
                )

            async def admin_updater():
                await asyncio.sleep(0.05)
                await self.manager.storage.create_version(
                    name="rag_system_prompt",
                    content="You are an expert assistant v2. Context: {context}",
                    created_by="admin",
                    tag="prod",
                    set_active=True,
                    tenant_id="org_dynamic",
                )
                self.manager.cache.invalidate("rag_system_prompt")

            tasks = [user_request_worker(i) for i in range(50)]
            tasks.append(admin_updater())
            await asyncio.gather(*tasks)

            await asyncio.sleep(0.5)

            logs = await self.manager.storage.fetch_logs("rag_system_prompt", limit=100, tenant_id="org_dynamic")
            self.assertGreaterEqual(len(logs), 40)

        asyncio.run(run_stress_test())

    def test_live_cache_invalidation_workflow(self):
        """Test that prompt edits instantly update cache without waiting for TTL."""
        async def run_cache_test():
            await self.manager.storage.create_version(
                name="dynamic_prompt",
                content="Version 1 content",
                created_by="alice",
                tag=None,
                set_active=True,
                tenant_id="org_dynamic",
            )
            v1_content = await aget_prompt("dynamic_prompt")
            self.assertEqual(v1_content, "Version 1 content")

            await self.manager.storage.create_version(
                name="dynamic_prompt",
                content="Version 2 updated content",
                created_by="bob",
                tag="prod",
                set_active=True,
                tenant_id="org_dynamic",
            )
            self.manager.cache.invalidate("dynamic_prompt")

            v2_content = await aget_prompt("dynamic_prompt")
            self.assertEqual(v2_content, "Version 2 updated content")

        asyncio.run(run_cache_test())

    def test_full_end_to_end_user_session(self):
        """Test full user flow: Login -> Create -> Edit -> Activate -> Export -> Delete -> Import."""
        cookies, csrf = self._login()

        # Create new prompt v1
        c1 = self.client.post(
            "/prompts/edit/__new__",
            data={
                "prompt_name": "e2e_prompt",
                "content": "Original v1 text",
                "edited_by": "admin",
                "tag": "staging",
                "set_active": "1",
                "csrf_token": csrf,
            },
            cookies=cookies,
        )
        self.assertEqual(c1.status_code, 303)

        # Create prompt v2
        c2 = self.client.post(
            "/prompts/edit/e2e_prompt",
            data={
                "content": "Modified v2 text",
                "edited_by": "admin",
                "tag": "prod",
                "set_active": "1",
                "csrf_token": csrf,
            },
            cookies=cookies,
        )
        self.assertEqual(c2.status_code, 303)

        # Export JSON
        export_res = self.client.get("/prompts/export", cookies=cookies)
        self.assertEqual(export_res.status_code, 200)
        export_data = json.loads(export_res.text)
        self.assertIn("e2e_prompt", export_data)
        self.assertEqual(export_data["e2e_prompt"], "Modified v2 text")

        # Activate v1 (id=1)
        act_res = self.client.post(
            "/prompts/activate/e2e_prompt/1",
            data={"csrf_token": csrf},
            cookies=cookies,
        )
        self.assertEqual(act_res.status_code, 303)

        async def check_v1():
            return await aget_prompt("e2e_prompt")
        self.assertEqual(asyncio.run(check_v1()), "Original v1 text")

        # Soft delete
        del_res = self.client.post(
            "/prompts/delete/e2e_prompt",
            data={"csrf_token": csrf},
            cookies=cookies,
        )
        self.assertEqual(del_res.status_code, 303)

        # Import JSON back
        import_json_data = {"imported_prompt": "Imported content via JSON"}
        import_res = self.client.post(
            "/prompts/import",
            files={"file": ("prompts.json", json.dumps(import_json_data), "application/json")},
            data={"csrf_token": csrf},
            cookies=cookies,
        )
        self.assertEqual(import_res.status_code, 200)
        self.assertIn("Import Complete", import_res.text)

    def test_mongo_storage_mocked_queries(self):
        """Test MongoStorage query building using mocked Motor client."""
        with patch("llmpivot.storage.AsyncIOMotorClient") as mock_motor:
            mock_client = MagicMock()
            mock_db = MagicMock()
            mock_motor.return_value = mock_client
            mock_client.__getitem__.return_value = mock_db

            mock_db.prompts.update_one = AsyncMock()
            mock_db.prompt_versions.find_one = AsyncMock(return_value={"version_number": 1})
            mock_db.prompt_versions.update_many = AsyncMock()
            mock_db.prompt_versions.insert_one = AsyncMock()

            storage = MongoStorage(mongo_uri="mongodb://localhost:27017", db_name="test_db")

            async def run_mongo():
                next_v = await storage.create_version(
                    name="mongo_prompt",
                    content="Mongo content",
                    created_by="tester",
                    tag="prod",
                    set_active=True,
                    tenant_id="mongo_tenant",
                )
                self.assertEqual(next_v, 2)
                mock_db.prompts.update_one.assert_called()
                mock_db.prompt_versions.insert_one.assert_called_once()

            asyncio.run(run_mongo())


if __name__ == "__main__":
    unittest.main()
