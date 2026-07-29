import asyncio
import os
import tempfile
import unittest

from llmpivot import PromptManager, PromptNotFoundError, get_prompt, aget_prompt, aget_prompt_with_meta


class ManagerAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_manager.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            cache_ttl=1,
            tenant_id="test_org",
            auth_mode="rbac",
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_manager_singleton(self):
        async def run():
            # Create a prompt version
            v_num = await self.manager.storage.create_version(
                name="welcome",
                content="Hello {name}!",
                created_by="admin",
                tag="prod",
                set_active=True,
                tenant_id="test_org",
            )
            self.assertEqual(v_num, 1)

            # Test aget_prompt
            content = await aget_prompt("welcome")
            self.assertEqual(content, "Hello {name}!")

            # Test aget_prompt_with_meta
            meta = await aget_prompt_with_meta("welcome")
            self.assertEqual(meta["content"], "Hello {name}!")
            self.assertIn("version_id", meta)

            # Test cache invalidation & update
            await self.manager.storage.create_version(
                name="welcome",
                content="Welcome to our platform, {name}!",
                created_by="admin",
                tag="prod",
                set_active=True,
                tenant_id="test_org",
            )
            self.manager.cache.invalidate("welcome")

            updated_content = await aget_prompt("welcome")
            self.assertEqual(updated_content, "Welcome to our platform, {name}!")

        asyncio.run(run())

    def test_prompt_not_found(self):
        async def run():
            with self.assertRaises(PromptNotFoundError):
                await aget_prompt("non_existent_prompt")

        asyncio.run(run())

    def test_tenant_isolation(self):
        async def run():
            # Create prompt under default tenant
            await self.manager.storage.create_version(
                name="shared_name",
                content="Org Content",
                created_by="admin",
                tag="prod",
                set_active=True,
                tenant_id="test_org",
            )

            # Check prompt exists for test_org
            res = await self.manager.storage.fetch_active_version("shared_name", tenant_id="test_org")
            self.assertIsNotNone(res)
            self.assertEqual(res["content"], "Org Content")

            # Check prompt is isolated from another tenant
            res_other = await self.manager.storage.fetch_active_version("shared_name", tenant_id="other_org")
            self.assertIsNone(res_other)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
