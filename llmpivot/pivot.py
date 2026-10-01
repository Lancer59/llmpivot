"""
PivotAgent — Phase 2 of the Pivot ambient assistant.

Two entry points:
  observe(context)  → async generator of observation dicts (streamed via SSE)
  chat(session_id, message, context) → async generator of text chunks (chunked HTTP)

Observations are rule-based (fast, no LLM) so they appear instantly regardless
of whether an LLM is configured. LLM-powered analysis is additive and optional.

Tool-calling in chat:
  The LLM is given a set of JSON-schema-described tools. When it emits a tool_use
  block, PivotAgent executes the tool, injects the result, and continues streaming.
  Write tools (create_version) require an explicit confirm flag — they never fire
  silently. All tool execution is bounded: no tool call can run longer than 10s.
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Dict, Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .manager import LLMAssetManager

logger = logging.getLogger("llmpivot.pivot")

# ---------------------------------------------------------------------------
# Observation types
# ---------------------------------------------------------------------------
OBS_OBSERVATION = "observation"   # grey — neutral context
OBS_ALERT       = "alert"         # amber — worth knowing
OBS_WARNING     = "warning"       # red   — active risk
OBS_SUGGESTION  = "suggestion"    # blue  — actionable recommendation


def _obs(obs_type: str, content: str, action: Optional[Dict] = None) -> Dict[str, Any]:
    return {"type": obs_type, "content": content, "action": action}


# ---------------------------------------------------------------------------
# Pivot system prompt
# ---------------------------------------------------------------------------

_PIVOT_SYSTEM = """\
You are Assistant, an intelligent helper embedded inside Instruction Studio, a runtime prompt and agent skill management system.

You have access to tools that let you read prompts, versioned Agent Skills, metadata, version history,
usage statistics, the application context document, and the prompt hierarchy. In progressive skill mode,
call list_skills to inspect short descriptions, get_skill to load only the matching SKILL.md, and
get_skill_reference to read a specific reference only when the skill requests it. You can suggest edits (as diffs) but you
NEVER save changes without the user explicitly confirming.

Your role:
- Answer questions about prompts, their history, purpose, and impact
- Suggest specific, concrete improvements — show diffs, not descriptions
- Analyse cross-prompt consistency and hierarchy relationships
- Be direct and concise. No filler phrases.
- When you don't know something, say so.

Current page context is provided in the first user message.
"""

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_prompt",
            "description": "Fetch the active content and metadata for a named prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The prompt name"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hierarchy",
            "description": "Get the parent prompt and all direct children of a named prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The prompt name"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_version_history",
            "description": "Get the version history and changelog entries for a prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The prompt name"}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_usage_stats",
            "description": "Get recent usage log count for a prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The prompt name"},
                    "limit": {"type": "integer", "description": "Number of recent log entries to fetch", "default": 50},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_app_context",
            "description": "Read the Application Context document that describes the overall application.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List active Agent Skills by name and short description, without loading their instructions.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "Load the active SKILL.md instructions for one named skill.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill slug"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_reference",
            "description": "Load one Markdown reference under references/ from a named skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill slug"},
                    "path": {"type": "string", "description": "Relative path under references/"},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_all_prompts",
            "description": "List all prompts with their type, purpose, and hierarchy.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_edit",
            "description": (
                "Generate a proposed new version of a prompt based on an instruction. "
                "Returns a diff preview. Does NOT save automatically — user must confirm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The prompt name"},
                    "instruction": {"type": "string", "description": "What to change and why"},
                },
                "required": ["name", "instruction"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# PivotAgent
# ---------------------------------------------------------------------------

class PivotAgent:
    """
    Stateless per-request agent. Conversation history is loaded from storage
    at the start of each request and persisted at the end.
    """

    def __init__(self, manager: "LLMAssetManager"):
        self.manager = manager

    # ------------------------------------------------------------------
    # Public: proactive observations (SSE)
    # ------------------------------------------------------------------

    async def observe(self, context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Yield observation dicts for the given page context.
        Rule-based observations are yielded immediately (no LLM).
        LLM-powered suggestions are yielded afterward if configured.
        """
        page = context.get("page", "")
        prompt_name = context.get("prompt_name", "")
        tenant_id = context.get("tenant_id", self.manager.tenant_id)

        try:
            async for obs in self._rule_based_observations(page, prompt_name, tenant_id):
                yield obs
        except Exception as exc:
            logger.warning("Pivot observe error (rule-based): %s", exc)

    async def _rule_based_observations(
        self, page: str, prompt_name: str, tenant_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Fast, synchronous rule checks — no LLM required."""
        storage = self.manager.storage

        # ---- Prompt list page ----
        if "/list" in page or page.endswith("/"):
            try:
                prompts = await storage.fetch_all_prompts_with_metadata(tenant_id=tenant_id)
                no_active = [p["name"] for p in prompts if not p.get("active_version")]
                no_meta = [p["name"] for p in prompts
                           if not p.get("purpose") and p.get("prompt_type", "unclassified") == "unclassified"]
                if no_active:
                    yield _obs(OBS_WARNING,
                               f"{len(no_active)} prompt(s) have no active version: "
                               + ", ".join(no_active[:3]) + ("…" if len(no_active) > 3 else ""))
                if no_meta:
                    yield _obs(OBS_SUGGESTION,
                               f"{len(no_meta)} prompt(s) have no metadata. "
                               "Add purpose and type to make them searchable.",
                               action={"label": "Open first", "url": f"../metadata/{no_meta[0]}"})
                total = len(prompts)
                if total > 0:
                    yield _obs(OBS_OBSERVATION,
                               f"{total} prompt(s) in this workspace. "
                               f"{sum(1 for p in prompts if p.get('prompt_type') == 'agent')} agent(s), "
                               f"{sum(1 for p in prompts if p.get('prompt_type') == 'tool')} tool(s).")
            except Exception as exc:
                logger.debug("List page observations failed: %s", exc)
            return

        if not prompt_name:
            return

        # ---- Prompt-specific pages ----
        try:
            versions = await storage.fetch_prompt_versions(prompt_name, tenant_id=tenant_id)
            meta = await storage.get_prompt_metadata(prompt_name, tenant_id=tenant_id)
            children = await storage.fetch_prompt_children(prompt_name, tenant_id=tenant_id)
            active_ver = next((v for v in versions if v.get("is_active")), None)
            changelog = await storage.get_changelog(prompt_name, tenant_id=tenant_id)
            cl_by_vid = {c["version_id"]: c for c in changelog}
        except Exception as exc:
            logger.debug("Prompt observations failed: %s", exc)
            return

        # No active version
        if not active_ver:
            yield _obs(OBS_WARNING,
                       f"'{prompt_name}' has no active version. "
                       "Calls to this prompt will raise PromptNotFoundError.")
            return

        # Active version has no changelog
        if active_ver and active_ver["id"] not in cl_by_vid:
            yield _obs(OBS_SUGGESTION,
                       f"The active version (v{active_ver['version_number']}) has no changelog entry.",
                       action={"label": "Add entry",
                               "url": f"../changelog/{prompt_name}/edit/{active_ver['id']}"})

        # Children not reviewed after parent changed
        if children and "/edit/" in page:
            yield _obs(OBS_ALERT,
                       f"This prompt has {len(children)} child prompt(s): "
                       + ", ".join(c["name"] for c in children[:4])
                       + ("…" if len(children) > 4 else "")
                       + ". Review them before activating changes.")

        # No metadata filled in
        if not meta or not meta.get("purpose"):
            yield _obs(OBS_SUGGESTION,
                       f"'{prompt_name}' has no purpose or type set.",
                       action={"label": "Add metadata", "url": f"../metadata/{prompt_name}"})
        else:
            # Show context strip as an observation
            ptype = meta.get("prompt_type", "unclassified")
            purpose = meta.get("purpose", "")
            owner = meta.get("owner", "")
            parts = [f"Type: {ptype}"]
            if purpose:
                parts.append(f"Purpose: {purpose}")
            if owner:
                parts.append(f"Owner: {owner}")
            yield _obs(OBS_OBSERVATION, " · ".join(parts))

        # High sensitivity prompt
        if meta and meta.get("sensitivity") == "high":
            yield _obs(OBS_ALERT,
                       f"'{prompt_name}' is marked HIGH sensitivity. "
                       "Changes should be reviewed carefully before activation.")

        # Version count info
        if versions:
            yield _obs(OBS_OBSERVATION,
                       f"{len(versions)} version(s). Active: v{active_ver['version_number']} "
                       f"(saved by {active_ver.get('created_by') or 'unknown'} "
                       f"on {(active_ver.get('created_at') or '')[:10]}).")

    # ------------------------------------------------------------------
    # Public: chat (chunked HTTP)
    # ------------------------------------------------------------------

    async def chat(
        self,
        session_id: str,
        message: str,
        context: Dict[str, Any],
        tenant_id: str = "default",
    ) -> AsyncGenerator[str, None]:
        """
        Yield text chunks for the chat reply.
        Handles tool-calling loop internally — tool results are injected and
        streaming continues until the LLM produces a final text response.
        """
        if not self.manager.llm:
            yield "data: " + json.dumps({
                "type": "error",
                "content": "LLM is not configured. Set llm_url in LLMAssetManager to enable chat."
            }) + "\n\n"
            return

        # Load history
        history = await self.manager.storage.fetch_conversation_history(
            session_id, limit=20, tenant_id=tenant_id
        )

        # Build page context prefix
        prompt_name = context.get("prompt_name", "")
        page = context.get("page", "")
        ctx_prefix = f"[Current page: {page}"
        if prompt_name:
            ctx_prefix += f" | Prompt: {prompt_name}"
        ctx_prefix += "]"

        full_message = f"{ctx_prefix}\n\n{message}"

        if self.manager.skill_loading_mode == "eager":
            skill_blocks = []
            for skill in await self.manager.list_skills():
                loaded = await self.manager.get_skill(skill["name"], include_references=True)
                references = loaded.pop("reference_content", {})
                block = f"## Skill: {loaded['name']}\n{loaded['content']}"
                for path, content in references.items():
                    block += f"\n\n### {path}\n{content}"
                skill_blocks.append(block)
            if skill_blocks:
                full_message = "Available skill instructions (eager mode):\n\n" + "\n\n".join(skill_blocks) + "\n\n" + full_message

        # Persist user turn
        await self.manager.storage.save_conversation_turn(
            session_id=session_id, role="user", content=message, tenant_id=tenant_id
        )

        # Build messages list
        messages = [{"role": "system", "content": _PIVOT_SYSTEM}]
        for turn in history:
            role = turn["role"]
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": turn["content"]})
        messages.append({"role": "user", "content": full_message})

        # Tool-calling loop (max 4 rounds to prevent runaway)
        full_reply = ""
        for _round in range(4):
            try:
                reply = await asyncio.wait_for(
                    self.manager.llm._call(_PIVOT_SYSTEM, self._messages_to_text(messages)),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                yield "data: " + json.dumps({"type": "error", "content": "LLM timed out."}) + "\n\n"
                return
            except Exception as exc:
                yield "data: " + json.dumps({"type": "error", "content": f"LLM error: {exc}"}) + "\n\n"
                return

            # Check if the reply contains a tool call marker
            tool_call = self._extract_tool_call(reply)

            if tool_call:
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("arguments", {})

                # Stream tool execution notice
                yield "data: " + json.dumps({
                    "type": "tool_call",
                    "content": f"Using tool: {tool_name}…"
                }) + "\n\n"

                # Execute tool
                try:
                    tool_result = await asyncio.wait_for(
                        self._execute_tool(tool_name, tool_args, tenant_id),
                        timeout=10.0,
                    )
                except Exception as exc:
                    tool_result = f"Tool error: {exc}"

                # Inject result and loop
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": f"Tool result for {tool_name}:\n{tool_result}"})
                continue

            # Final text reply — stream word by word (simulate streaming since
            # current LLMClient buffers the full response)
            full_reply = reply
            words = reply.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield "data: " + json.dumps({"type": "text", "content": chunk}) + "\n\n"
                # Small yield point so the event loop can flush
                if i % 10 == 0:
                    await asyncio.sleep(0)
            break

        # Persist assistant turn
        if full_reply:
            await self.manager.storage.save_conversation_turn(
                session_id=session_id, role="assistant", content=full_reply, tenant_id=tenant_id
            )

        yield "data: " + json.dumps({"type": "done"}) + "\n\n"

    # ------------------------------------------------------------------
    # Tool executor
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, args: Dict[str, Any], tenant_id: str
    ) -> str:
        storage = self.manager.storage
        mgr = self.manager

        if name == "get_prompt":
            prompt_name = args.get("name", "")
            active = await storage.fetch_active_version(prompt_name, tenant_id=tenant_id)
            if not active:
                return f"No active version found for '{prompt_name}'."
            meta = await storage.get_prompt_metadata(prompt_name, tenant_id=tenant_id)
            return json.dumps({
                "name": prompt_name,
                "content": active["content"],
                "version": active["version_number"],
                "metadata": meta,
            }, indent=2)

        elif name in ("list_skills", "get_skill", "get_skill_reference"):
            return await mgr.call_skill_tool(name, args)

        elif name == "get_hierarchy":
            prompt_name = args.get("name", "")
            meta = await storage.get_prompt_metadata(prompt_name, tenant_id=tenant_id)
            children = await storage.fetch_prompt_children(prompt_name, tenant_id=tenant_id)
            return json.dumps({
                "name": prompt_name,
                "parent": meta.get("parent_prompt_name") if meta else None,
                "prompt_type": meta.get("prompt_type") if meta else "unclassified",
                "children": [{"name": c["name"], "type": c.get("prompt_type")} for c in children],
            }, indent=2)

        elif name == "get_version_history":
            prompt_name = args.get("name", "")
            versions = await storage.fetch_prompt_versions(prompt_name, tenant_id=tenant_id)
            changelog = await storage.get_changelog(prompt_name, tenant_id=tenant_id)
            cl_by_vid = {c["version_id"]: c["entry"] for c in changelog}
            result = []
            for v in versions[:10]:  # cap at 10
                result.append({
                    "version": v["version_number"],
                    "is_active": bool(v.get("is_active")),
                    "created_by": v.get("created_by"),
                    "created_at": v.get("created_at"),
                    "tag": v.get("tag"),
                    "changelog": cl_by_vid.get(v["id"], "(none)"),
                })
            return json.dumps(result, indent=2)

        elif name == "get_usage_stats":
            prompt_name = args.get("name", "")
            limit = int(args.get("limit", 50))
            logs = await storage.fetch_logs(prompt_name=prompt_name, limit=limit, tenant_id=tenant_id)
            return json.dumps({
                "prompt": prompt_name,
                "recent_log_count": len(logs),
                "latest_timestamp": logs[0]["timestamp"] if logs else None,
            }, indent=2)

        elif name == "get_app_context":
            ctx = await storage.get_app_context(tenant_id=tenant_id)
            if not ctx:
                return "No application context document has been saved yet."
            return ctx["content"]

        elif name == "list_all_prompts":
            prompts = await storage.fetch_all_prompts_with_metadata(tenant_id=tenant_id)
            result = [
                {
                    "name": p["name"],
                    "type": p.get("prompt_type", "unclassified"),
                    "purpose": p.get("purpose", ""),
                    "parent": p.get("parent_name"),
                    "active_version": p.get("active_version"),
                }
                for p in prompts
            ]
            return json.dumps(result, indent=2)

        elif name == "suggest_edit":
            prompt_name = args.get("name", "")
            instruction = args.get("instruction", "")
            active = await storage.fetch_active_version(prompt_name, tenant_id=tenant_id)
            if not active:
                return f"No active version found for '{prompt_name}'."
            if not mgr.llm:
                return "LLM not configured — cannot generate suggestion."

            suggest_prompt = (
                f"You are editing a production LLM prompt. "
                f"Apply this change: {instruction}\n\n"
                f"Current prompt:\n{active['content']}\n\n"
                f"Return ONLY the updated prompt text, nothing else."
            )
            try:
                suggested = await mgr.llm._call(suggest_prompt, instruction)
            except Exception as exc:
                return f"Suggestion failed: {exc}"

            import difflib
            diff = "\n".join(difflib.unified_diff(
                active["content"].splitlines(),
                suggested.splitlines(),
                fromfile=f"{prompt_name} (current)",
                tofile=f"{prompt_name} (suggested)",
                lineterm="",
            ))
            return json.dumps({
                "prompt": prompt_name,
                "instruction": instruction,
                "suggested_content": suggested,
                "diff": diff or "(no changes)",
                "note": "This is a preview only. Use the edit page to save.",
            }, indent=2)

        else:
            return f"Unknown tool: {name}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _messages_to_text(self, messages: List[Dict[str, Any]]) -> str:
        """
        Flatten message list to a single string for the current non-streaming
        LLMClient._call interface. The tool schemas are embedded so the LLM
        knows what tools are available.
        """
        tool_desc = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}"
            for t in _TOOLS
        )
        lines = [
            f"Available tools (call using JSON: {{\"tool\": \"name\", \"arguments\": {{...}}}}):\n{tool_desc}\n"
        ]
        for m in messages:
            role = m["role"]
            content = m["content"]
            if role == "system":
                continue  # system already embedded in _PIVOT_SYSTEM
            lines.append(f"{role.upper()}: {content}")
        return "\n\n".join(lines)

    def _extract_tool_call(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Look for a JSON tool call block in the LLM response.
        Expected format: {"tool": "name", "arguments": {...}}
        Returns the parsed dict or None if no valid tool call found.
        """
        text = text.strip()
        # Look for JSON block anywhere in the response
        start = text.find("{")
        if start == -1:
            return None
        # Find matching closing brace
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if "tool" in parsed and parsed["tool"] in {t["function"]["name"] for t in _TOOLS}:
                            return {
                                "name": parsed["tool"],
                                "arguments": parsed.get("arguments", {}),
                            }
                    except (json.JSONDecodeError, KeyError):
                        pass
                    break
        return None
