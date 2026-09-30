"""
Tests for Phase 2 Pivot features:
- pivot_observations and pivot_conversations storage
- PivotAgent.observe() — rule-based observation generation
- PivotAgent.chat() — tool-calling loop, streaming output
- /pivot/observe SSE endpoint
- /pivot/chat chunked HTTP endpoint
- Floating widget injected into page HTML when pivot_enabled=True
- Widget absent when pivot_enabled=False
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

from llmpivot import PromptManager
from llmpivot.auth import generate_csrf_token
from llmpivot.storage import SQLiteStorage
from llmpivot.pivot import PivotAgent, OBS_OBSERVATION, OBS_ALERT, OBS_WARNING, OBS_SUGGESTION

SECRET = "phase2-test-secret"


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
# Storage tests for Phase 2 tables
# ---------------------------------------------------------------------------

class Phase2StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "phase2.db")
        self.storage = SQLiteStorage(self.db_path)
        self.storage.init_db_sync()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_save_and_fetch_observation(self):
        async def run():
            obs_id = await self.storage.save_observation(
                obs_type=OBS_ALERT,
                content="This prompt has no active version.",
                page_context="/prompts/list",
                tenant_id="default",
            )
            self.assertIsNotNone(obs_id)
            self.assertGreater(obs_id, 0)

            obs_list = await self.storage.fetch_observations(
                page_context="/prompts/list",
                tenant_id="default",
            )
            self.assertEqual(len(obs_list), 1)
            self.assertEqual(obs_list[0]["obs_type"], OBS_ALERT)
            self.assertIn("active version", obs_list[0]["content"])
        asyncio.run(run())

    def test_observations_filtered_by_page_context(self):
        async def run():
            await self.storage.save_observation(OBS_OBSERVATION, "List page obs", "/prompts/list")
            await self.storage.save_observation(OBS_ALERT, "Edit page alert", "/prompts/edit/x")
            list_obs = await self.storage.fetch_observations(page_context="/prompts/list")
            edit_obs = await self.storage.fetch_observations(page_context="/prompts/edit/x")
            self.assertEqual(len(list_obs), 1)
            self.assertEqual(len(edit_obs), 1)
        asyncio.run(run())

    def test_save_and_fetch_conversation_turn(self):
        async def run():
            await self.storage.save_conversation_turn(
                session_id="sess-123",
                role="user",
                content="What does this prompt do?",
                tenant_id="default",
            )
            await self.storage.save_conversation_turn(
                session_id="sess-123",
                role="assistant",
                content="This prompt classifies user intent.",
                tenant_id="default",
            )
            history = await self.storage.fetch_conversation_history("sess-123")
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0]["role"], "user")
            self.assertEqual(history[1]["role"], "assistant")
        asyncio.run(run())

    def test_conversation_history_ordered_chronologically(self):
        async def run():
            for i in range(5):
                await self.storage.save_conversation_turn(
                    session_id="sess-order",
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}",
                )
            history = await self.storage.fetch_conversation_history("sess-order", limit=10)
            self.assertEqual(len(history), 5)
            for i, turn in enumerate(history):
                self.assertIn(f"Message {i}", turn["content"])
        asyncio.run(run())

    def test_conversation_history_scoped_by_session(self):
        async def run():
            await self.storage.save_conversation_turn("sess-a", "user", "Hello from A")
            await self.storage.save_conversation_turn("sess-b", "user", "Hello from B")
            hist_a = await self.storage.fetch_conversation_history("sess-a")
            hist_b = await self.storage.fetch_conversation_history("sess-b")
            self.assertEqual(len(hist_a), 1)
            self.assertEqual(len(hist_b), 1)
            self.assertIn("A", hist_a[0]["content"])
            self.assertIn("B", hist_b[0]["content"])
        asyncio.run(run())

    def test_conversation_history_limit(self):
        async def run():
            for i in range(30):
                await self.storage.save_conversation_turn("sess-big", "user", f"msg {i}")
            history = await self.storage.fetch_conversation_history("sess-big", limit=10)
            self.assertEqual(len(history), 10)
        asyncio.run(run())


# ---------------------------------------------------------------------------
# PivotAgent observe() tests
# ---------------------------------------------------------------------------

class PivotAgentObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "pivot_obs.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            pivot_enabled=True,
            pivot_proactive=True,
        )
        self.agent = PivotAgent(self.manager)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _collect(self, context: dict) -> list:
        """Collect all observations from the generator synchronously."""
        async def run():
            obs = []
            async for o in self.agent.observe(context):
                obs.append(o)
            return obs
        return asyncio.run(run())

    def test_observe_list_page_reports_no_active(self):
        async def setup():
            await self.manager.storage.create_version("broken_prompt", "content", "a", None, False)
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/list", "prompt_name": ""})
        types = [o["type"] for o in obs]
        self.assertIn(OBS_WARNING, types)
        warning = next(o for o in obs if o["type"] == OBS_WARNING)
        self.assertIn("broken_prompt", warning["content"])

    def test_observe_list_page_reports_unclassified(self):
        async def setup():
            await self.manager.storage.create_version("mystery_prompt", "content", "a", None, True)
            # No metadata → unclassified
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/list", "prompt_name": ""})
        suggestions = [o for o in obs if o["type"] == OBS_SUGGESTION]
        self.assertTrue(len(suggestions) > 0)

    def test_observe_list_page_total_count(self):
        async def setup():
            await self.manager.storage.create_version("p1", "c1", "a", None, True)
            await self.manager.storage.create_version("p2", "c2", "a", None, True)
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/list", "prompt_name": ""})
        observations = [o for o in obs if o["type"] == OBS_OBSERVATION]
        # Should include a count observation
        count_obs = [o for o in observations if "2" in o["content"]]
        self.assertTrue(len(count_obs) > 0)

    def test_observe_detail_page_no_active_warns(self):
        async def setup():
            await self.manager.storage.create_version("draft_p", "c", "a", None, False)
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/detail/draft_p", "prompt_name": "draft_p"})
        warnings = [o for o in obs if o["type"] == OBS_WARNING]
        self.assertTrue(len(warnings) > 0)
        self.assertIn("no active version", warnings[0]["content"].lower())

    def test_observe_detail_no_changelog_suggests(self):
        async def setup():
            await self.manager.storage.create_version("no_cl", "content", "a", None, True)
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/detail/no_cl", "prompt_name": "no_cl"})
        suggestions = [o for o in obs if o["type"] == OBS_SUGGESTION]
        changelog_sug = [s for s in suggestions if "changelog" in s["content"].lower()]
        self.assertTrue(len(changelog_sug) > 0)

    def test_observe_edit_page_alerts_about_children(self):
        async def setup():
            await self.manager.storage.create_version("agent_x", "agent content", "a", None, True)
            await self.manager.storage.create_version("tool_y", "tool content", "a", None, True)
            await self.manager.storage.upsert_prompt_metadata(
                "tool_y", {"prompt_type": "tool", "parent_prompt_id": "agent_x"}
            )
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/edit/agent_x", "prompt_name": "agent_x"})
        alerts = [o for o in obs if o["type"] == OBS_ALERT]
        child_alerts = [a for a in alerts if "child" in a["content"].lower()]
        self.assertTrue(len(child_alerts) > 0)
        self.assertIn("tool_y", child_alerts[0]["content"])

    def test_observe_high_sensitivity_alerts(self):
        async def setup():
            await self.manager.storage.create_version("critical_p", "c", "a", None, True)
            await self.manager.storage.upsert_prompt_metadata(
                "critical_p", {"sensitivity": "high", "purpose": "Critical prompt"}
            )
        asyncio.run(setup())
        obs = self._collect({"page": "/prompts/detail/critical_p", "prompt_name": "critical_p"})
        alerts = [o for o in obs if o["type"] == OBS_ALERT]
        sensitivity_alerts = [a for a in alerts if "sensitivity" in a["content"].lower() or "HIGH" in a["content"]]
        self.assertTrue(len(sensitivity_alerts) > 0)

    def test_observe_returns_no_obs_for_unknown_prompt(self):
        obs = self._collect({"page": "/prompts/detail/ghost", "prompt_name": "ghost"})
        # Should return gracefully (possibly empty or warning)
        self.assertIsInstance(obs, list)

    def test_observe_empty_context_returns_gracefully(self):
        obs = self._collect({})
        self.assertIsInstance(obs, list)


# ---------------------------------------------------------------------------
# PivotAgent chat() tests
# ---------------------------------------------------------------------------

class PivotAgentChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "pivot_chat.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            pivot_enabled=True,
            llm_url="https://mock-llm.test/v1/chat/completions",
            llm_api_key="sk-mock",
        )
        # Mock LLM to return a simple reply
        self.manager.llm._call = AsyncMock(return_value="This is Pivot's reply.")
        self.agent = PivotAgent(self.manager)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _collect_chat(self, session_id, message, context=None) -> list:
        async def run():
            chunks = []
            async for chunk in self.agent.chat(session_id, message, context or {}):
                chunks.append(chunk)
            return chunks
        return asyncio.run(run())

    def test_chat_streams_text_chunks(self):
        chunks = self._collect_chat("sess-1", "What is this prompt for?")
        # Should have at least text chunks and a done event
        data_chunks = [c for c in chunks if c.startswith("data: ")]
        parsed = [json.loads(c[6:]) for c in data_chunks]
        text_chunks = [p for p in parsed if p["type"] == "text"]
        done_chunks = [p for p in parsed if p["type"] == "done"]
        self.assertTrue(len(text_chunks) > 0)
        self.assertEqual(len(done_chunks), 1)
        # Reassemble content
        full = "".join(p["content"] for p in text_chunks)
        self.assertIn("Pivot", full)

    def test_chat_persists_conversation_turns(self):
        asyncio.run(self._async_chat("sess-persist", "Hello Pivot"))
        history = asyncio.run(
            self.manager.storage.fetch_conversation_history("sess-persist")
        )
        roles = [h["role"] for h in history]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    async def _async_chat(self, session_id, message):
        async for _ in self.agent.chat(session_id, message, {}):
            pass

    def test_chat_without_llm_returns_error(self):
        manager2 = PromptManager(
            db_path=self.db_path,
            pivot_enabled=True,
            # No llm_url
        )
        agent2 = PivotAgent(manager2)

        async def run():
            chunks = []
            async for c in agent2.chat("s", "hi", {}):
                chunks.append(c)
            return chunks

        chunks = asyncio.run(run())
        parsed = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
        error_chunks = [p for p in parsed if p["type"] == "error"]
        self.assertTrue(len(error_chunks) > 0)
        self.assertIn("LLM", error_chunks[0]["content"])

    def test_chat_tool_call_executed(self):
        """When LLM emits a tool call JSON, the agent executes it and continues."""
        call_count = 0

        async def mock_call_with_tool(system, user):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: emit a tool call
                return json.dumps({"tool": "get_app_context", "arguments": {}})
            else:
                # Second call (after tool result injected): normal reply
                return "Here is the application context information."

        self.manager.llm._call = mock_call_with_tool

        async def setup():
            await self.manager.storage.save_app_context("# My App\nThis is a test app.", "test")
        asyncio.run(setup())

        chunks = self._collect_chat("sess-tool", "What does the app do?")
        data = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
        tool_calls = [d for d in data if d["type"] == "tool_call"]
        text_chunks = [d for d in data if d["type"] == "text"]

        self.assertTrue(len(tool_calls) > 0, "Expected at least one tool_call event")
        self.assertIn("get_app_context", tool_calls[0]["content"])
        full_text = "".join(d["content"] for d in text_chunks)
        self.assertIn("application context", full_text.lower())


# ---------------------------------------------------------------------------
# Phase 2 route tests
# ---------------------------------------------------------------------------

class Phase2RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "phase2_routes.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
            pivot_enabled=True,
            pivot_proactive=True,
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
        return generate_csrf_token(session_cookie, SECRET)

    def test_observe_endpoint_returns_sse_stream(self):
        self._login()
        res = self.client.get("/prompts/pivot/observe?page=/prompts/list&prompt_name=")
        self.assertEqual(res.status_code, 200)
        # Content-type should be SSE
        self.assertIn("text/event-stream", res.headers.get("content-type", ""))

    def test_observe_endpoint_requires_auth(self):
        self.client.cookies.clear()
        res = self.client.get("/prompts/pivot/observe")
        self.assertEqual(res.status_code, 401)

    def test_observe_endpoint_404_when_pivot_disabled(self):
        # Create a manager with pivot disabled
        tmp2 = tempfile.TemporaryDirectory()
        try:
            mgr2 = PromptManager(
                db_path=os.path.join(tmp2.name, "no_pivot.db"),
                auth_mode="rbac",
                secret_key=SECRET,
                bootstrap_admin=True,
                bootstrap_password="admin",
                pivot_enabled=False,
            )
            client2_ctx = _make_test_client(mgr2)
            client2 = client2_ctx.__enter__()
            try:
                res2 = client2.post("/prompts/login", data={"username": "admin", "password": "admin"})
                if res2.status_code == 303:
                    client2.cookies.set("llmpivot_session", res2.cookies["llmpivot_session"])
                res = client2.get("/prompts/pivot/observe")
                self.assertEqual(res.status_code, 404)
            finally:
                client2_ctx.__exit__(None, None, None)
        finally:
            tmp2.cleanup()

    def test_chat_endpoint_streams_response(self):
        self._login()
        # Mock LLM so it doesn't hit network
        self.manager.llm = type('M', (), {'_call': AsyncMock(return_value="Test reply from Pivot.")})()
        res = self.client.post(
            "/prompts/pivot/chat",
            json={
                "session_id": "test-session",
                "message": "Hello Pivot",
                "context": {"page": "/prompts/list", "prompt_name": ""},
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("event-stream", res.headers.get("content-type", ""))

    def test_chat_endpoint_requires_auth(self):
        self.client.cookies.clear()
        res = self.client.post(
            "/prompts/pivot/chat",
            json={"session_id": "s", "message": "hi", "context": {}},
        )
        self.assertEqual(res.status_code, 401)

    def test_chat_endpoint_404_when_pivot_disabled(self):
        tmp2 = tempfile.TemporaryDirectory()
        try:
            mgr2 = PromptManager(
                db_path=os.path.join(tmp2.name, "no_pivot2.db"),
                pivot_enabled=False,
            )
            client2_ctx = _make_test_client(mgr2)
            client2 = client2_ctx.__enter__()
            try:
                res = client2.post("/prompts/pivot/chat",
                                   json={"session_id": "s", "message": "hi", "context": {}})
                self.assertEqual(res.status_code, 404)
            finally:
                client2_ctx.__exit__(None, None, None)
        finally:
            tmp2.cleanup()

    def test_chat_endpoint_validates_empty_message(self):
        self._login()
        res = self.client.post(
            "/prompts/pivot/chat",
            json={"session_id": "s", "message": "   ", "context": {}},
        )
        self.assertEqual(res.status_code, 400)


# ---------------------------------------------------------------------------
# Widget injection tests
# ---------------------------------------------------------------------------

class PivotWidgetTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "widget.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _make_client(self, pivot_enabled: bool) -> TestClient:
        mgr = PromptManager(
            db_path=self.db_path,
            auth_mode="rbac",
            secret_key=SECRET,
            bootstrap_admin=True,
            bootstrap_password="admin",
            pivot_enabled=pivot_enabled,
        )
        return _make_test_client(mgr), mgr

    def test_widget_injected_when_pivot_enabled(self):
        client_ctx, mgr = self._make_client(pivot_enabled=True)
        client = client_ctx.__enter__()
        try:
            res = client.post("/prompts/login", data={"username": "admin", "password": "admin"})
            client.cookies.set("llmpivot_session", res.cookies["llmpivot_session"])
            res_list = client.get("/prompts/list")
            self.assertEqual(res_list.status_code, 200)
            self.assertIn("pivot-widget", res_list.text)
            self.assertIn("alpinejs", res_list.text)
            self.assertIn("pivotWidget()", res_list.text)
        finally:
            client_ctx.__exit__(None, None, None)

    def test_widget_absent_when_pivot_disabled(self):
        tmp2 = tempfile.TemporaryDirectory()
        try:
            mgr2 = PromptManager(
                db_path=os.path.join(tmp2.name, "no_w.db"),
                auth_mode="rbac",
                secret_key=SECRET,
                bootstrap_admin=True,
                bootstrap_password="admin",
                pivot_enabled=False,
            )
            client_ctx = _make_test_client(mgr2)
            client = client_ctx.__enter__()
            try:
                res = client.post("/prompts/login", data={"username": "admin", "password": "admin"})
                client.cookies.set("llmpivot_session", res.cookies["llmpivot_session"])
                res_list = client.get("/prompts/list")
                self.assertEqual(res_list.status_code, 200)
                self.assertNotIn("pivot-widget", res_list.text)
                self.assertNotIn("alpinejs", res_list.text)
            finally:
                client_ctx.__exit__(None, None, None)
        finally:
            tmp2.cleanup()

    def test_widget_on_detail_page(self):
        client_ctx, mgr = self._make_client(pivot_enabled=True)
        client = client_ctx.__enter__()
        try:
            res = client.post("/prompts/login", data={"username": "admin", "password": "admin"})
            session_cookie = res.cookies["llmpivot_session"]
            client.cookies.set("llmpivot_session", session_cookie)
            csrf = generate_csrf_token(session_cookie, SECRET)
            # Create a prompt first
            client.post("/prompts/edit/__new__",
                        data={"prompt_name": "widget_test", "content": "content",
                              "edited_by": "admin", "tag": "", "set_active": "1", "csrf_token": csrf})
            res_detail = client.get("/prompts/detail/widget_test")
            self.assertEqual(res_detail.status_code, 200)
            self.assertIn("pivot-widget", res_detail.text)
        finally:
            client_ctx.__exit__(None, None, None)

    def test_widget_on_edit_page(self):
        client_ctx, mgr = self._make_client(pivot_enabled=True)
        client = client_ctx.__enter__()
        try:
            res = client.post("/prompts/login", data={"username": "admin", "password": "admin"})
            session_cookie = res.cookies["llmpivot_session"]
            client.cookies.set("llmpivot_session", session_cookie)
            csrf = generate_csrf_token(session_cookie, SECRET)
            client.post("/prompts/edit/__new__",
                        data={"prompt_name": "edit_widget", "content": "content",
                              "edited_by": "admin", "tag": "", "set_active": "1", "csrf_token": csrf})
            res_edit = client.get("/prompts/edit/edit_widget")
            self.assertEqual(res_edit.status_code, 200)
            self.assertIn("pivot-widget", res_edit.text)
        finally:
            client_ctx.__exit__(None, None, None)

    def test_pivot_widget_html_function_returns_empty_when_disabled(self):
        from llmpivot.ui.templates import pivot_widget_html
        html = pivot_widget_html("/prompts", pivot_enabled=False, pivot_proactive=True)
        self.assertEqual(html, "")

    def test_pivot_widget_html_function_returns_widget_when_enabled(self):
        from llmpivot.ui.templates import pivot_widget_html
        html = pivot_widget_html("/prompts", pivot_enabled=True, pivot_proactive=True)
        self.assertIn("pivot-widget", html)
        self.assertIn("pivotWidget()", html)
        self.assertIn("/prompts/pivot/observe", html)
        self.assertIn("/prompts/pivot/chat", html)


# ---------------------------------------------------------------------------
# Tool executor tests
# ---------------------------------------------------------------------------

class PivotToolExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "tools.db")
        self.manager = PromptManager(
            db_path=self.db_path,
            pivot_enabled=True,
            llm_url="https://mock.test/v1/chat/completions",
            llm_api_key="sk-mock",
        )
        self.manager.llm._call = AsyncMock(return_value="Mock LLM response.")
        self.agent = PivotAgent(self.manager)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tool_get_prompt(self):
        async def run():
            await self.manager.storage.create_version("my_tool_prompt", "Tool content here.", "a", None, True)
            result = await self.agent._execute_tool("get_prompt", {"name": "my_tool_prompt"}, "default")
            data = json.loads(result)
            self.assertEqual(data["name"], "my_tool_prompt")
            self.assertEqual(data["content"], "Tool content here.")
        asyncio.run(run())

    def test_tool_get_prompt_missing_returns_message(self):
        async def run():
            result = await self.agent._execute_tool("get_prompt", {"name": "ghost"}, "default")
            self.assertIn("No active version", result)
        asyncio.run(run())

    def test_tool_get_hierarchy(self):
        async def run():
            await self.manager.storage.create_version("parent_p", "parent", "a", None, True)
            await self.manager.storage.create_version("child_p", "child", "a", None, True)
            await self.manager.storage.upsert_prompt_metadata(
                "child_p", {"prompt_type": "tool", "parent_prompt_id": "parent_p"}
            )
            result = await self.agent._execute_tool("get_hierarchy", {"name": "parent_p"}, "default")
            data = json.loads(result)
            self.assertEqual(data["name"], "parent_p")
            self.assertEqual(len(data["children"]), 1)
            self.assertEqual(data["children"][0]["name"], "child_p")
        asyncio.run(run())

    def test_tool_list_all_prompts(self):
        async def run():
            await self.manager.storage.create_version("p1", "c1", "a", None, True)
            await self.manager.storage.create_version("p2", "c2", "b", None, True)
            result = await self.agent._execute_tool("list_all_prompts", {}, "default")
            data = json.loads(result)
            names = [p["name"] for p in data]
            self.assertIn("p1", names)
            self.assertIn("p2", names)
        asyncio.run(run())

    def test_tool_get_app_context_empty(self):
        async def run():
            result = await self.agent._execute_tool("get_app_context", {}, "default")
            self.assertIn("No application context", result)
        asyncio.run(run())

    def test_tool_get_app_context_with_content(self):
        async def run():
            await self.manager.storage.save_app_context("# My App\nSupport system.", "admin")
            result = await self.agent._execute_tool("get_app_context", {}, "default")
            self.assertIn("Support system", result)
        asyncio.run(run())

    def test_tool_get_version_history(self):
        async def run():
            await self.manager.storage.create_version("hist_p", "v1", "a", None, True)
            await self.manager.storage.create_version("hist_p", "v2", "b", None, True)
            result = await self.agent._execute_tool("get_version_history", {"name": "hist_p"}, "default")
            data = json.loads(result)
            self.assertEqual(len(data), 2)
            versions = [d["version"] for d in data]
            self.assertIn(1, versions)
            self.assertIn(2, versions)
        asyncio.run(run())

    def test_tool_unknown_returns_error_string(self):
        async def run():
            result = await self.agent._execute_tool("nonexistent_tool", {}, "default")
            self.assertIn("Unknown tool", result)
        asyncio.run(run())

    def test_extract_tool_call_valid_json(self):
        text = 'I need to check something. {"tool": "get_app_context", "arguments": {}}'
        result = self.agent._extract_tool_call(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "get_app_context")

    def test_extract_tool_call_no_json(self):
        result = self.agent._extract_tool_call("Just a plain text reply with no tool call.")
        self.assertIsNone(result)

    def test_extract_tool_call_unknown_tool_ignored(self):
        result = self.agent._extract_tool_call('{"tool": "fake_tool", "arguments": {}}')
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
