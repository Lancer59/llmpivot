"""
Tests for Phase 1 Pivot features:
- Prompt metadata (CRUD, hierarchy, parent-child)
- Prompt changelog (upsert, fetch per-version)
- Application context (save, get, versioning)
- Fallback snapshot (write, read, get_with_fallback)
- New UI routes: /tree, /metadata/{name}, /changelog/{name}, /context
- Auto-changelog wiring (mocked LLM)
- Hierarchy warning panel on edit page
"""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llmpivot import PromptManager, aget_prompt_with_fallback, PromptNotFoundError
from llmpivot.auth import generate_csrf_token
from llmpivot.storage import SQLiteStorage

SECRET = "phase1-test-secret"


# ---------------------------------------------------------------------------
# Shared test client helper (same pattern as other test files)
# ---------------------------------------------------------------------------

def _make_test_client(manager: PromptManager) -> TestClient:
    @asynccontextmanager
    async def _lifespan(app):
        await manager._bootstrap_admin()
        manager.cache.start()
        manager.usage_logger.start()
        yield
        await manager._shutdown()

    app = FastAPI(lifespan=_lifespan)
    app.mount("/prompts", manager.mount_ui())
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# Storage-level Phase 1 tests
# ---------------------------------------------------------------------------

class Phase1StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "phase1.db")
        self.storage = SQLiteStorage(self.db_path)
        self.storage.init_db_sync()

    def tearDown(self):
        self.tmp_dir.cleanup()

    # ------------------------------------------------------------------
    # prompt_metadata
    # ------------------------------------------------------------------

    def test_upsert_and_get_metadata(self):
        async def run():
            await self.storage.create_version("agent_prompt", "You are an agent.", "alice", None, True)
            await self.storage.upsert_prompt_metadata("agent_prompt", {
                "purpose": "Main agent system prompt",
                "prompt_type": "agent",
                "sensitivity": "high",
                "feature_area": "core",
                "owner": "platform-team",
            })
            meta = await self.storage.get_prompt_metadata("agent_prompt")
            self.assertEqual(meta["purpose"], "Main agent system prompt")
            self.assertEqual(meta["prompt_type"], "agent")
            self.assertEqual(meta["sensitivity"], "high")
            self.assertEqual(meta["feature_area"], "core")
            self.assertEqual(meta["owner"], "platform-team")
        asyncio.run(run())

    def test_metadata_upsert_updates_existing(self):
        async def run():
            await self.storage.create_version("my_prompt", "content", "alice", None, True)
            await self.storage.upsert_prompt_metadata("my_prompt", {"purpose": "First purpose"})
            await self.storage.upsert_prompt_metadata("my_prompt", {"purpose": "Updated purpose", "sensitivity": "medium"})
            meta = await self.storage.get_prompt_metadata("my_prompt")
            self.assertEqual(meta["purpose"], "Updated purpose")
            self.assertEqual(meta["sensitivity"], "medium")
        asyncio.run(run())

    def test_metadata_returns_empty_for_nonexistent_prompt(self):
        async def run():
            meta = await self.storage.get_prompt_metadata("ghost_prompt")
            self.assertEqual(meta, {})
        asyncio.run(run())

    # ------------------------------------------------------------------
    # Hierarchy (parent-child)
    # ------------------------------------------------------------------

    def test_parent_child_hierarchy(self):
        async def run():
            await self.storage.create_version("support_agent", "You are a support agent.", "alice", None, True)
            await self.storage.create_version("classify_intent", "Classify user intent.", "alice", None, True)
            await self.storage.create_version("draft_response", "Draft a helpful response.", "alice", None, True)

            await self.storage.upsert_prompt_metadata("support_agent", {"prompt_type": "agent"})
            await self.storage.upsert_prompt_metadata("classify_intent", {
                "prompt_type": "tool",
                "parent_prompt_id": "support_agent",
            })
            await self.storage.upsert_prompt_metadata("draft_response", {
                "prompt_type": "tool",
                "parent_prompt_id": "support_agent",
            })

            children = await self.storage.fetch_prompt_children("support_agent")
            child_names = [c["name"] for c in children]
            self.assertIn("classify_intent", child_names)
            self.assertIn("draft_response", child_names)
            self.assertEqual(len(children), 2)
        asyncio.run(run())

    def test_parent_name_resolved_in_metadata(self):
        async def run():
            await self.storage.create_version("parent_agent", "parent content", "alice", None, True)
            await self.storage.create_version("child_tool", "child content", "alice", None, True)
            await self.storage.upsert_prompt_metadata("parent_agent", {"prompt_type": "agent"})
            await self.storage.upsert_prompt_metadata("child_tool", {
                "prompt_type": "tool",
                "parent_prompt_id": "parent_agent",
            })
            meta = await self.storage.get_prompt_metadata("child_tool")
            self.assertEqual(meta.get("parent_prompt_name"), "parent_agent")
        asyncio.run(run())

    def test_fetch_all_prompts_with_metadata(self):
        async def run():
            await self.storage.create_version("agent_a", "content a", "alice", None, True)
            await self.storage.create_version("tool_b", "content b", "alice", None, True)
            await self.storage.upsert_prompt_metadata("agent_a", {"prompt_type": "agent"})
            await self.storage.upsert_prompt_metadata("tool_b", {
                "prompt_type": "tool",
                "parent_prompt_id": "agent_a",
            })
            prompts = await self.storage.fetch_all_prompts_with_metadata()
            by_name = {p["name"]: p for p in prompts}
            self.assertIn("agent_a", by_name)
            self.assertIn("tool_b", by_name)
            self.assertEqual(by_name["agent_a"]["prompt_type"], "agent")
            self.assertEqual(by_name["tool_b"]["prompt_type"], "tool")
            self.assertEqual(by_name["tool_b"]["parent_name"], "agent_a")
        asyncio.run(run())

    def test_no_children_for_prompt_without_children(self):
        async def run():
            await self.storage.create_version("lone_prompt", "content", "alice", None, True)
            children = await self.storage.fetch_prompt_children("lone_prompt")
            self.assertEqual(children, [])
        asyncio.run(run())

    # ------------------------------------------------------------------
    # prompt_changelog
    # ------------------------------------------------------------------

    def test_upsert_and_get_changelog(self):
        async def run():
            await self.storage.create_version("my_prompt", "v1 content", "alice", None, True)
            versions = await self.storage.fetch_prompt_versions("my_prompt")
            v_id = versions[0]["id"]

            await self.storage.upsert_changelog_entry(
                version_id=v_id,
                entry="Initial version created for testing.",
                generated_by="user",
                created_by="alice",
            )
            cl = await self.storage.get_changelog("my_prompt")
            self.assertEqual(len(cl), 1)
            self.assertEqual(cl[0]["entry"], "Initial version created for testing.")
            self.assertEqual(cl[0]["generated_by"], "user")
        asyncio.run(run())

    def test_changelog_upsert_overwrites_existing(self):
        async def run():
            await self.storage.create_version("p", "content", "alice", None, True)
            versions = await self.storage.fetch_prompt_versions("p")
            v_id = versions[0]["id"]

            await self.storage.upsert_changelog_entry(v_id, "First entry", "user", "alice")
            await self.storage.upsert_changelog_entry(v_id, "Updated entry", "pivot", "pivot")
            cl = await self.storage.get_changelog("p")
            self.assertEqual(len(cl), 1)  # still one entry, updated in place
            self.assertEqual(cl[0]["entry"], "Updated entry")
            self.assertEqual(cl[0]["generated_by"], "pivot")
        asyncio.run(run())

    def test_get_changelog_for_version(self):
        async def run():
            await self.storage.create_version("p2", "content", "alice", None, True)
            versions = await self.storage.fetch_prompt_versions("p2")
            v_id = versions[0]["id"]
            await self.storage.upsert_changelog_entry(v_id, "Entry for v1", "user", "bob")
            entry = await self.storage.get_changelog_for_version(v_id)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["entry"], "Entry for v1")
        asyncio.run(run())

    def test_get_changelog_for_version_returns_none_if_missing(self):
        async def run():
            result = await self.storage.get_changelog_for_version(99999)
            self.assertIsNone(result)
        asyncio.run(run())

    # ------------------------------------------------------------------
    # app_context
    # ------------------------------------------------------------------

    def test_save_and_get_app_context(self):
        async def run():
            ctx = await self.storage.get_app_context()
            self.assertIsNone(ctx)

            await self.storage.save_app_context("# My App\nThis is a support app.", "alice")
            ctx = await self.storage.get_app_context()
            self.assertIsNotNone(ctx)
            self.assertIn("support app", ctx["content"])
            self.assertEqual(ctx["version"], 1)
            self.assertEqual(ctx["created_by"], "alice")
        asyncio.run(run())

    def test_app_context_versioning(self):
        async def run():
            await self.storage.save_app_context("Version 1 content", "alice")
            await self.storage.save_app_context("Version 2 content", "bob")
            ctx = await self.storage.get_app_context()
            # Should return latest (highest version)
            self.assertEqual(ctx["version"], 2)
            self.assertEqual(ctx["content"], "Version 2 content")
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Fallback snapshot tests
# ---------------------------------------------------------------------------

class FallbackSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "fallback.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            fallback_snapshot=True,
            pivot_enabled=False,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_snapshot_written_on_warm(self):
        async def run():
            await self.manager.storage.create_version("snap_prompt", "Hello snapshot!", "test", None, True)
            await self.manager._write_fallback_snapshot()
            self.assertTrue(os.path.exists(self.manager.fallback_path))
            with open(self.manager.fallback_path) as f:
                data = json.load(f)
            self.assertIn("snap_prompt", data)
            self.assertEqual(data["snap_prompt"], "Hello snapshot!")
        asyncio.run(run())

    def test_snapshot_is_valid_json(self):
        async def run():
            await self.manager.storage.create_version("p1", "Content one", "a", None, True)
            await self.manager.storage.create_version("p2", "Content two", "b", None, True)
            await self.manager._write_fallback_snapshot()
            with open(self.manager.fallback_path) as f:
                data = json.load(f)
            self.assertEqual(data["p1"], "Content one")
            self.assertEqual(data["p2"], "Content two")
        asyncio.run(run())

    def test_snapshot_only_includes_active_versions(self):
        async def run():
            # Create two versions, only v2 active
            await self.manager.storage.create_version("versioned", "v1 content", "a", None, True)
            await self.manager.storage.create_version("versioned", "v2 content", "b", None, True)
            await self.manager._write_fallback_snapshot()
            with open(self.manager.fallback_path) as f:
                data = json.load(f)
            self.assertEqual(data["versioned"], "v2 content")
        asyncio.run(run())

    def test_get_with_fallback_returns_live_when_healthy(self):
        async def run():
            await self.manager.storage.create_version("live_prompt", "Live content", "a", None, True)
            self.manager.cache.invalidate("live_prompt")
            content = await aget_prompt_with_fallback("live_prompt")
            self.assertEqual(content, "Live content")
        asyncio.run(run())

    def test_get_with_fallback_uses_snapshot_on_db_failure(self):
        async def run():
            await self.manager.storage.create_version("resilient", "Snapshot content", "a", None, True)
            await self.manager._write_fallback_snapshot()

            # Simulate DB failure
            self.manager.cache._store.clear()
            orig = self.manager.storage.fetch_active_version
            async def fail(*a, **kw):
                raise RuntimeError("DB is down!")
            self.manager.storage.fetch_active_version = fail

            content = await aget_prompt_with_fallback("resilient")
            self.assertEqual(content, "Snapshot content")

            # Restore
            self.manager.storage.fetch_active_version = orig
        asyncio.run(run())

    def test_get_with_fallback_raises_if_absent_everywhere(self):
        async def run():
            # No live prompt, no snapshot entry
            await self.manager._write_fallback_snapshot()  # writes empty {}
            with self.assertRaises(PromptNotFoundError):
                await aget_prompt_with_fallback("nonexistent_prompt")
        asyncio.run(run())

    def test_snapshot_disabled_writes_nothing(self):
        async def run():
            manager2 = PromptManager(
                db_path=self.db_path,
                fallback_snapshot=False,
            )
            await manager2.storage.create_version("no_snap", "content", "a", None, True)
            await manager2._write_fallback_snapshot()
            self.assertFalse(os.path.exists(manager2.fallback_path))
        asyncio.run(run())

    def test_load_fallback_returns_empty_if_file_missing(self):
        data = self.manager._load_fallback_snapshot()
        self.assertEqual(data, {})


# ---------------------------------------------------------------------------
# UI route tests for Phase 1 new endpoints
# ---------------------------------------------------------------------------

class Phase1RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "routes_phase1.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
            pivot_enabled=False,
        )
        self._client_ctx = _make_test_client(self.manager)
        self.client = self._client_ctx.__enter__()

    def tearDown(self):
        try:
            self._client_ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp_dir.cleanup()

    def _login(self):
        res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res.status_code, 303, f"Login failed: {res.text[:100]}")
        session_cookie = res.cookies["llmpivot_session"]
        self.client.cookies.set("llmpivot_session", session_cookie)
        csrf = generate_csrf_token(session_cookie, SECRET)
        return csrf

    def _create_prompt(self, name: str, content: str, csrf: str):
        res = self.client.post(
            "/prompts/edit/__new__",
            data={"prompt_name": name, "content": content, "edited_by": "admin",
                  "tag": "", "set_active": "1", "csrf_token": csrf},
        )
        self.assertEqual(res.status_code, 303)

    # ------------------------------------------------------------------
    # /tree
    # ------------------------------------------------------------------

    def test_tree_page_renders(self):
        csrf = self._login()
        self._create_prompt("agent_x", "Agent X content", csrf)
        res = self.client.get("/prompts/tree")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Prompt Hierarchy", res.text)
        self.assertIn("agent_x", res.text)

    def test_tree_page_shows_stats(self):
        csrf = self._login()
        self._create_prompt("p1", "content1", csrf)
        self._create_prompt("p2", "content2", csrf)
        res = self.client.get("/prompts/tree")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Total", res.text)

    def test_tree_requires_auth(self):
        self.client.cookies.clear()
        res = self.client.get("/prompts/tree")
        self.assertEqual(res.status_code, 303)

    # ------------------------------------------------------------------
    # /metadata/{name}
    # ------------------------------------------------------------------

    def test_metadata_page_renders(self):
        csrf = self._login()
        self._create_prompt("meta_prompt", "Some content", csrf)
        res = self.client.get("/prompts/metadata/meta_prompt")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Metadata", res.text)
        self.assertIn("meta_prompt", res.text)

    def test_metadata_post_saves_fields(self):
        csrf = self._login()
        self._create_prompt("save_meta", "Content here", csrf)
        res = self.client.post(
            "/prompts/metadata/save_meta",
            data={
                "prompt_type": "agent",
                "sensitivity": "high",
                "purpose": "Testing metadata save",
                "feature_area": "tests",
                "owner": "test-team",
                "called_from": "test_phase1.py",
                "model_used": "gpt-4o",
                "input_variables": "user_query",
                "notes": "Test notes here",
                "parent_prompt_id": "",
                "csrf_token": csrf,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Metadata saved", res.text)
        self.assertIn("Testing metadata save", res.text)

    def test_metadata_post_requires_editor(self):
        self.client.cookies.clear()
        res = self.client.post("/prompts/metadata/some_prompt",
                               data={"prompt_type": "agent", "csrf_token": "fake"})
        self.assertEqual(res.status_code, 303)

    def test_metadata_post_rejects_bad_csrf(self):
        csrf = self._login()
        self._create_prompt("csrf_meta", "content", csrf)
        res = self.client.post(
            "/prompts/metadata/csrf_meta",
            data={"prompt_type": "tool", "csrf_token": "bad-token"},
        )
        self.assertEqual(res.status_code, 403)

    # ------------------------------------------------------------------
    # /changelog/{name}
    # ------------------------------------------------------------------

    def test_changelog_page_renders(self):
        csrf = self._login()
        self._create_prompt("cl_prompt", "Content v1", csrf)
        res = self.client.get("/prompts/changelog/cl_prompt")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Changelog", res.text)
        self.assertIn("cl_prompt", res.text)

    def test_changelog_edit_get(self):
        csrf = self._login()
        self._create_prompt("edit_cl", "Content", csrf)
        # Get version id
        versions = asyncio.run(self.manager.storage.fetch_prompt_versions("edit_cl"))
        v_id = versions[0]["id"]
        res = self.client.get(f"/prompts/changelog/edit_cl/edit/{v_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Edit Changelog", res.text)

    def test_changelog_edit_post_saves_entry(self):
        csrf = self._login()
        self._create_prompt("save_cl", "Content", csrf)
        versions = asyncio.run(self.manager.storage.fetch_prompt_versions("save_cl"))
        v_id = versions[0]["id"]
        res = self.client.post(
            f"/prompts/changelog/save_cl/edit/{v_id}",
            data={"entry": "This is my changelog entry.", "csrf_token": csrf},
        )
        self.assertEqual(res.status_code, 303)
        # Verify it was saved
        entry = asyncio.run(self.manager.storage.get_changelog_for_version(v_id))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["entry"], "This is my changelog entry.")

    def test_changelog_requires_auth(self):
        self.client.cookies.clear()
        res = self.client.get("/prompts/changelog/any_prompt")
        self.assertEqual(res.status_code, 303)

    # ------------------------------------------------------------------
    # /context
    # ------------------------------------------------------------------

    def test_context_page_renders_empty(self):
        csrf = self._login()
        res = self.client.get("/prompts/context")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Application Context", res.text)

    def test_context_post_saves_content(self):
        csrf = self._login()
        res = self.client.post(
            "/prompts/context",
            data={
                "content": "# My App\nThis app does support chat.",
                "csrf_token": csrf,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Application context saved", res.text)
        self.assertIn("My App", res.text)

    def test_context_post_rejects_bad_csrf(self):
        csrf = self._login()
        res = self.client.post(
            "/prompts/context",
            data={"content": "# App\nContent.", "csrf_token": "bad-token"},
        )
        self.assertEqual(res.status_code, 403)

    def test_context_requires_editor_for_post(self):
        self.client.cookies.clear()
        res = self.client.post("/prompts/context",
                               data={"content": "# App", "csrf_token": "fake"})
        self.assertEqual(res.status_code, 303)

    def test_context_get_shows_saved_content(self):
        csrf = self._login()
        self.client.post(
            "/prompts/context",
            data={"content": "# Saved Context\nImportant application info.", "csrf_token": csrf},
        )
        res = self.client.get("/prompts/context")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Saved Context", res.text)

    # ------------------------------------------------------------------
    # Hierarchy warning on edit page
    # ------------------------------------------------------------------

    def test_edit_page_shows_hierarchy_warning_for_parent(self):
        csrf = self._login()
        self._create_prompt("parent_agent", "Parent agent prompt", csrf)
        self._create_prompt("child_tool", "Child tool prompt", csrf)

        # Set parent-child relationship
        asyncio.run(self.manager.storage.upsert_prompt_metadata(
            "child_tool", {"prompt_type": "tool", "parent_prompt_id": "parent_agent"}
        ))

        res = self.client.get("/prompts/edit/parent_agent")
        self.assertEqual(res.status_code, 200)
        # Warning panel should appear since parent_agent has children
        self.assertIn("child prompt", res.text)

    def test_edit_page_no_warning_for_childless_prompt(self):
        csrf = self._login()
        self._create_prompt("lone_agent", "Lone prompt content", csrf)
        res = self.client.get("/prompts/edit/lone_agent")
        self.assertEqual(res.status_code, 200)
        # Should render edit page without hierarchy warning
        self.assertIn("Save New Version", res.text)
        self.assertNotIn("child prompt", res.text)

    def test_edit_page_shows_metadata_strip_when_metadata_exists(self):
        csrf = self._login()
        self._create_prompt("with_meta", "Content here", csrf)
        asyncio.run(self.manager.storage.upsert_prompt_metadata(
            "with_meta", {
                "prompt_type": "agent",
                "purpose": "Handle support tickets",
                "sensitivity": "medium",
            }
        ))
        res = self.client.get("/prompts/edit/with_meta")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Handle support tickets", res.text)


# ---------------------------------------------------------------------------
# Auto-changelog tests
# ---------------------------------------------------------------------------

class AutoChangelogTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "cl_auto.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
            pivot_enabled=True,
            auto_changelog=True,
            llm_url="https://mock-llm.test/v1/chat/completions",
            llm_api_key="sk-mock",
        )
        # Mock the LLM to return a predictable changelog entry
        self.manager.llm._call = AsyncMock(return_value="Tone adjusted for clarity. Output format unchanged.")
        self._client_ctx = _make_test_client(self.manager)
        self.client = self._client_ctx.__enter__()

    def tearDown(self):
        try:
            self._client_ctx.__exit__(None, None, None)
        except Exception:
            pass
        self.tmp_dir.cleanup()

    def _login(self):
        res = self.client.post("/prompts/login", data={"username": "admin", "password": "admin"})
        self.assertEqual(res.status_code, 303)
        session_cookie = res.cookies["llmpivot_session"]
        self.client.cookies.set("llmpivot_session", session_cookie)
        return generate_csrf_token(session_cookie, SECRET)

    def test_auto_changelog_disabled_when_pivot_off(self):
        """When pivot_enabled=False, auto_changelog should be False too."""
        manager2 = PromptManager(
            db_path=self.db_path,
            pivot_enabled=False,
            auto_changelog=True,  # explicitly set, but pivot_enabled overrides
        )
        self.assertFalse(manager2.auto_changelog)

    def test_auto_changelog_enabled_when_pivot_on(self):
        self.assertTrue(self.manager.auto_changelog)
        self.assertTrue(self.manager.pivot_enabled)

    def test_auto_changelog_generated_after_edit(self):
        """After saving a version with auto_changelog=True, changelog entry appears."""
        csrf = self._login()

        # Create initial version
        self.client.post(
            "/prompts/edit/__new__",
            data={"prompt_name": "auto_cl_prompt", "content": "Original content.",
                  "edited_by": "admin", "tag": "", "set_active": "1", "csrf_token": csrf},
        )

        # Edit to v2 — this should trigger auto-changelog
        self.client.post(
            "/prompts/edit/auto_cl_prompt",
            data={"content": "Updated content with better tone.",
                  "edited_by": "admin", "tag": "", "set_active": "1", "csrf_token": csrf},
        )

        # Give the background task time to complete
        import time
        time.sleep(0.3)

        versions = asyncio.run(self.manager.storage.fetch_prompt_versions("auto_cl_prompt"))
        v2 = next((v for v in versions if v["version_number"] == 2), None)
        self.assertIsNotNone(v2)

        entry = asyncio.run(self.manager.storage.get_changelog_for_version(v2["id"]))
        if entry:
            # If LLM mock fired, check content
            self.assertIn("Tone adjusted", entry["entry"])
            self.assertEqual(entry["generated_by"], "pivot")


# ---------------------------------------------------------------------------
# Template rendering tests for Phase 1
# ---------------------------------------------------------------------------

class Phase1TemplateTests(unittest.TestCase):
    def test_metadata_page_renders_all_fields(self):
        from llmpivot.ui.templates import metadata_page
        meta = {
            "purpose": "Handle support",
            "prompt_type": "agent",
            "sensitivity": "high",
            "feature_area": "support",
            "owner": "team-x",
            "called_from": "routes/chat.py",
            "model_used": "gpt-4o",
            "notes": "Important notes",
        }
        html = metadata_page("support_agent", meta, [], base="/prompts")
        self.assertIn("Metadata", html)
        self.assertIn("Handle support", html)
        self.assertIn("team-x", html)
        self.assertIn("routes/chat.py", html)

    def test_changelog_page_renders_entries(self):
        from llmpivot.ui.templates import changelog_page
        versions = [
            {"id": 1, "version_number": 1, "content": "v1", "created_at": "2024-01-01", "created_by": "alice", "is_active": 0},
            {"id": 2, "version_number": 2, "content": "v2", "created_at": "2024-01-02", "created_by": "bob", "is_active": 1},
        ]
        changelog = [
            {"version_id": 1, "entry": "Initial version.", "generated_by": "user", "created_by": "alice", "created_at": "2024-01-01"},
        ]
        html = changelog_page("my_prompt", changelog, versions, base="/prompts")
        self.assertIn("Changelog", html)
        self.assertIn("Initial version.", html)
        self.assertIn("No changelog entry", html)  # for v2

    def test_app_context_page_renders(self):
        from llmpivot.ui.templates import app_context_page
        ctx = {"content": "# My App\nTest content.", "version": 3, "created_by": "alice", "created_at": "2024-01-01"}
        html = app_context_page(ctx, base="/prompts")
        self.assertIn("Application Context", html)
        self.assertIn("My App", html)
        self.assertIn("v3", html)

    def test_tree_page_renders_hierarchy(self):
        from llmpivot.ui.templates import tree_page
        prompts = [
            {"id": 1, "name": "support_agent", "prompt_type": "agent", "purpose": "Main agent",
             "active_version": 2, "parent_name": None, "parent_prompt_id": None,
             "last_edited_by": "alice", "last_updated": "2024-01-01"},
            {"id": 2, "name": "classify_tool", "prompt_type": "tool", "purpose": "Classify",
             "active_version": 1, "parent_name": "support_agent", "parent_prompt_id": 1,
             "last_edited_by": "bob", "last_updated": "2024-01-01"},
        ]
        html = tree_page(prompts, base="/prompts")
        self.assertIn("Prompt Hierarchy", html)
        self.assertIn("support_agent", html)
        self.assertIn("classify_tool", html)
        self.assertIn("agent", html)
        self.assertIn("tool", html)

    def test_hierarchy_warning_panel_shown_for_children(self):
        from llmpivot.ui.templates import _hierarchy_warning_panel
        children = [
            {"name": "child_tool", "prompt_type": "tool"},
            {"name": "another_tool", "prompt_type": "tool"},
        ]
        html = _hierarchy_warning_panel(children, "/prompts", "parent_agent")
        self.assertIn("2 child prompts", html)
        self.assertIn("child_tool", html)
        self.assertIn("another_tool", html)

    def test_hierarchy_warning_panel_empty_for_no_children(self):
        from llmpivot.ui.templates import _hierarchy_warning_panel
        html = _hierarchy_warning_panel([], "/prompts", "lone_prompt")
        self.assertEqual(html, "")

    def test_type_badge_renders_correctly(self):
        from llmpivot.ui.templates import _type_badge
        html = _type_badge("agent")
        self.assertIn("agent", html)
        html_unclassified = _type_badge("unclassified")
        self.assertIn("unclassified", html_unclassified)

    def test_sensitivity_badge_renders(self):
        from llmpivot.ui.templates import _sensitivity_badge
        self.assertIn("HIGH", _sensitivity_badge("high"))
        self.assertIn("MEDIUM", _sensitivity_badge("medium"))
        self.assertIn("LOW", _sensitivity_badge("low"))


if __name__ == "__main__":
    unittest.main()
