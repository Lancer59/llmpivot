import asyncio
import os
import sqlite3
import tempfile
import unittest

from llmpivot import PromptManager
from llmpivot.storage import SQLiteStorage
from llmpivot.auth import hash_password, verify_password, create_token, verify_token, has_permission
from llmpivot.logger import PromptLogger
from llmpivot.ui.templates import prompt_list, login_page, users_page


class StorageAndAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_prompts.db")
        self.storage = SQLiteStorage(self.db_path)
        self.storage.init_db_sync()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_password_hashing(self):
        pwd = "SecretPassword123"
        hashed = hash_password(pwd)
        self.assertTrue(verify_password(pwd, hashed))
        self.assertFalse(verify_password("WrongPassword", hashed))

    def test_token_creation_and_verification(self):
        secret = "test-secret-key"
        payload = {"username": "admin", "role": "admin"}
        token = create_token(payload, secret, expires_in=3600)
        
        decoded = verify_token(token, secret)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["username"], "admin")
        self.assertEqual(decoded["role"], "admin")

        # Wrong secret
        self.assertIsNone(verify_token(token, "wrong-secret"))

    def test_rbac_permissions(self):
        self.assertTrue(has_permission("admin", "editor"))
        self.assertTrue(has_permission("editor", "editor"))
        self.assertFalse(has_permission("viewer", "editor"))
        self.assertFalse(has_permission("editor", "admin"))

    def test_sqlite_storage_crud(self):
        async def run_test():
            # Create version
            v1 = await self.storage.create_version(
                name="summary",
                content="Summarize the text:",
                created_by="alice",
                tag="prod",
                set_active=True,
                tenant_id="tenant_a",
            )
            self.assertEqual(v1, 1)

            # Active version
            active = await self.storage.fetch_active_version("summary", tenant_id="tenant_a")
            self.assertIsNotNone(active)
            self.assertEqual(active["content"], "Summarize the text:")

            # Create second version
            v2 = await self.storage.create_version(
                name="summary",
                content="Summarize in bullet points:",
                created_by="bob",
                tag=None,
                set_active=True,
                tenant_id="tenant_a",
            )
            self.assertEqual(v2, 2)

            # Verify active updated to v2
            active_v2 = await self.storage.fetch_active_version("summary", tenant_id="tenant_a")
            self.assertEqual(active_v2["content"], "Summarize in bullet points:")

            # Create user
            pwd_hash = hash_password("pass123")
            user = await self.storage.create_user("admin_user", pwd_hash, role="admin", tenant_id="tenant_a")
            self.assertEqual(user["username"], "admin_user")

            fetched_user = await self.storage.get_user("admin_user", tenant_id="tenant_a")
            self.assertIsNotNone(fetched_user)
            self.assertEqual(fetched_user["role"], "admin")

        asyncio.run(run_test())

    def test_tenant_scoping_is_enforced(self):
        async def run_test():
            await self.storage.create_version(
                name="shared_prompt",
                content="tenant-a content",
                created_by="alice",
                tag="prod",
                set_active=True,
                tenant_id="tenant_a",
            )
            await self.storage.create_version(
                name="shared_prompt",
                content="tenant-b content",
                created_by="bob",
                tag=None,
                set_active=True,
                tenant_id="tenant_b",
            )

            tenant_a_prompts = await self.storage.fetch_all_prompts(tenant_id="tenant_a")
            self.assertEqual(len(tenant_a_prompts), 1)
            self.assertEqual(tenant_a_prompts[0]["name"], "shared_prompt")

            tenant_b_prompts = await self.storage.fetch_all_prompts(tenant_id="tenant_b")
            self.assertEqual(len(tenant_b_prompts), 1)
            self.assertEqual(tenant_b_prompts[0]["name"], "shared_prompt")

            active_a = await self.storage.fetch_active_version("shared_prompt", tenant_id="tenant_a")
            self.assertEqual(active_a["content"], "tenant-a content")
            active_b = await self.storage.fetch_active_version("shared_prompt", tenant_id="tenant_b")
            self.assertEqual(active_b["content"], "tenant-b content")

            version_a = active_a["id"]
            self.assertFalse(await self.storage.set_active_version(version_a, tenant_id="tenant_b"))
            self.assertTrue(await self.storage.set_active_version(version_a, tenant_id="tenant_a"))

            user = await self.storage.create_user("tenant_user", hash_password("pass123"), tenant_id="tenant_a")
            fetched_user = await self.storage.get_user(user["username"], tenant_id="tenant_a")
            self.assertIsNotNone(fetched_user)
            self.assertEqual(fetched_user["tenant_id"], "tenant_a")

        asyncio.run(run_test())

    def test_migrate_missing_tenant_ids(self):
        async def run_test():
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO prompts (name, tenant_id) VALUES (?, ?)", ("legacy_prompt", None))
                prompt_id = cur.lastrowid
                cur.execute(
                    "INSERT INTO prompt_versions (prompt_id, tenant_id, content, version_number, created_by, tag, is_active) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (prompt_id, None, "legacy content", 1, "alice", None, 1),
                )
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, tenant_id) VALUES (?, ?, ?, ?)",
                    ("legacy_user", "hash", "editor", None),
                )
                conn.commit()
            finally:
                conn.close()

            result = await self.storage.migrate_missing_tenant_ids(tenant_id="default")
            self.assertEqual(result["prompts"], 1)
            self.assertEqual(result["prompt_versions"], 1)
            self.assertEqual(result["users"], 1)

            migrated_user = await self.storage.get_user("legacy_user", tenant_id="default")
            self.assertIsNotNone(migrated_user)
            self.assertEqual(migrated_user["tenant_id"], "default")

        asyncio.run(run_test())

    def test_async_logger_batching(self):
        async def run_log_test():
            await self.storage.create_version("rag_prompt", "Context: {ctx}", "admin", "prod", True)
            active = await self.storage.fetch_active_version("rag_prompt")

            logger = PromptLogger(self.storage, batch_size=10, flush_interval=0.1)
            logger.log("rag_prompt", active["id"], "input 1", "output 1")
            logger.log("rag_prompt", active["id"], "input 2", "output 2")

            # Allow batch worker time to process
            await asyncio.sleep(0.3)

            logs = await self.storage.fetch_logs("rag_prompt")
            self.assertGreaterEqual(len(logs), 2)

        asyncio.run(run_log_test())

    def test_templates_rendering(self):
        html_login = login_page(base="/prompts")
        self.assertIn("Login to Prompt Manager", html_login)

        html_users = users_page([], current_user={"username": "admin", "role": "admin"}, base="/prompts")
        self.assertIn("User Management", html_users)


if __name__ == "__main__":
    unittest.main()
