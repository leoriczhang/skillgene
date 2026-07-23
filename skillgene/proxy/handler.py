"""Core request handling: skill injection, forwarding, and turn recording.

``HandlerMixin`` implements the ``/v1/chat/completions`` request lifecycle:
inject skills, truncate to context budget, forward upstream, normalize the
assistant message + tool calls, attribute read/modified skills, and record
the main-turn artifact for PRM scoring and cloud upload.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException

from .attribution import (
    _extract_modified_skills_from_tool_calls,
    _extract_read_skills_from_tool_calls,
)
from .messages import (
    _NON_STANDARD_BODY_KEYS,
    _build_tool_summaries,
    _deduplicate_tool_calls,
    _extract_last_user_instruction,
    _extract_recent_tool_result_messages,
    _extract_recent_tool_results,
    _extract_tool_calls_from_text,
    _flatten_message_content,
    _merge_tool_error_info,
    _normalize_assistant_content_parts,
    _restore_missing_reasoning_content,
)
from .session import _classify_raw_turn_kind, _is_user_turn_boundary
from .tokens import _estimate_openai_body_input_tokens

logger = logging.getLogger(__name__)

_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


class HandlerMixin:
    """Request handling, context truncation, and skill injection."""

    async def _handle_request(
        self,
        body: dict[str, Any],
        session_id: str,
        turn_type: str,
        session_done: bool,
    ) -> dict[str, Any]:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages must be a non-empty list")
        self._touch_session(session_id)
        rewritten = 0
        for msg in messages:
            if (
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
                and msg.get("content", "").startswith("A new chat session just started.")
            ):
                rewritten += 1
        if rewritten:
            logger.info("[Proxy] rewrote %d /new bootstrap user prompt(s) for provider safety", rewritten)

        def _prompt_len(msgs):
            return _estimate_openai_body_input_tokens({"messages": msgs, "tools": body.get("tools")})

        restored_reasoning = _restore_missing_reasoning_content(
            messages,
            self._session_turns.get(session_id, []),
        )
        if restored_reasoning:
            logger.info(
                "[Proxy] restored reasoning_content on %d prior assistant tool-call message(s)",
                restored_reasoning,
            )

        tools = body.get("tools")

        # Inject skills into system message for main turns
        injected_skills: list[str] = []
        if self.skill_manager and turn_type == "main":
            messages, injected_skills = self._inject_skills(messages)

        # Truncate to fit within max_context_tokens (keep system + most-recent messages)
        max_prompt = self.config.max_context_tokens - int(body.get("max_tokens") or 2048)
        if max_prompt > 0:
            messages = self._truncate_messages(messages, tools, max_prompt)

        forward_body = {k: v for k, v in body.items() if k not in _NON_STANDARD_BODY_KEYS}
        forward_body["stream"] = False
        forward_body.pop("stream_options", None)
        if "model" not in forward_body:
            forward_body["model"] = self._served_model
        forward_body["messages"] = messages  # potentially skill-injected

        output = await self._forward_to_llm(forward_body)
        output["model"] = forward_body.get("model") or self._served_model

        choice = output.get("choices", [{}])[0]
        assistant_msg = choice.get("message", {})
        if not isinstance(assistant_msg, dict):
            assistant_msg = {"role": "assistant", "content": _flatten_message_content(assistant_msg)}

        raw_tool_calls = assistant_msg.get("tool_calls") or []
        tool_calls = list(raw_tool_calls) if isinstance(raw_tool_calls, list) else []

        raw_content = assistant_msg.get("content")
        if isinstance(raw_content, list):
            part_text, part_tool_calls = _normalize_assistant_content_parts(raw_content)
            content = part_text
            tool_calls.extend(part_tool_calls)
        else:
            content = _flatten_message_content(raw_content)

        # Upstream models sometimes emit tool calls as text tags instead of
        # structured `message.tool_calls`; parse and normalize both sources.
        clean_content, text_tool_calls = _extract_tool_calls_from_text(content)
        if text_tool_calls:
            content = clean_content
            tool_calls.extend(text_tool_calls)
        tool_calls = _deduplicate_tool_calls(tool_calls)

        assistant_msg["content"] = content
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        else:
            assistant_msg.pop("tool_calls", None)
        choice["message"] = assistant_msg
        if isinstance(output.get("choices"), list) and output["choices"]:
            output["choices"][0] = choice
        else:
            output["choices"] = [choice]

        reasoning = assistant_msg.get("reasoning_content") or ""

        logger.info(f"{_YELLOW}[Proxy] [{turn_type}] session={session_id} prompt_msgs={len(messages)}{_RESET}")
        logger.info(
            f"{_RED}[Proxy] [{turn_type}] session={session_id} "
            f"thinking={len(reasoning)} chars, response:\n{content}{_RESET}"
        )
        if tool_calls:
            logger.info("[Proxy] tool_calls: %s", json.dumps(tool_calls, ensure_ascii=False)[:500])

        if turn_type == "main":
            tool_results = _extract_recent_tool_results(messages)
            prev_turns = self._session_turns.get(session_id, [])
            if tool_results and prev_turns:
                raw_tool_results = _extract_recent_tool_result_messages(messages)
                _merge_tool_error_info(prev_turns[-1], tool_results, raw_tool_results)

            if session_id in self._pending_records and messages:
                self._flush_pending_record(session_id, messages[-1])

            response_msg = dict(assistant_msg)
            if response_msg.get("content") is None:
                response_msg["content"] = ""

            skill_path_map = self.skill_manager.get_skill_path_map() if self.skill_manager else {}
            read_skills = _extract_read_skills_from_tool_calls(
                tool_calls,
                skill_path_map,
            )
            modified_skills = _extract_modified_skills_from_tool_calls(
                tool_calls,
                skill_path_map,
            )
            tool_summaries = _build_tool_summaries(tool_calls)
            if read_skills:
                logger.info(
                    "[SkillManager] model read %d skill(s): %s",
                    len(read_skills),
                    ", ".join(r.get("skill_name", "?") for r in read_skills),
                )
            if modified_skills:
                logger.info(
                    "[SkillManager] model modified %d skill(s): %s",
                    len(modified_skills),
                    ", ".join(r.get("skill_name", "?") for r in modified_skills),
                )

            user_instruction = _extract_last_user_instruction(messages)
            self._turn_counts[session_id] = self._turn_counts.get(session_id, 0) + 1
            turn_num = self._turn_counts[session_id]
            prompt_text = "\n".join(
                f"{m.get('role', '?')}: {_flatten_message_content(m.get('content', ''))}" for m in messages
            )
            response_text = content or (json.dumps(tool_calls, ensure_ascii=False) if tool_calls else "")
            self._buffer_record(session_id, turn_num, messages, prompt_text, response_text, tool_calls)
            raw_turn_kind = _classify_raw_turn_kind(content, tool_calls)
            turn_record = {
                "turn_num": turn_num,
                "raw_turn_kind": raw_turn_kind,
                "prompt_text": user_instruction,
                "response_text": response_text,
                "reasoning_content": reasoning or None,
                "tool_calls": tool_calls,
                "read_skills": read_skills,
                "modified_skills": modified_skills,
                "tool_results": tool_summaries,
                "tool_results_raw": [],
                "tool_observations": [],
                "tool_errors": [],
                "injected_skills": injected_skills,
                "prm_score": None,
            }
            self._session_turns.setdefault(session_id, []).append(turn_record)
            if _is_user_turn_boundary(raw_turn_kind):
                user_turn_num = self._next_user_turn_num(session_id)
                turn_record["user_turn_num"] = user_turn_num
                self._maybe_upload_session_snapshot(session_id, user_turn_num)
            self._pending_turn_data.setdefault(session_id, {})[turn_num] = {
                "prompt_text": prompt_text,
                "response_text": response_text,
            }
            logger.info(
                "[Proxy] MAIN session=%s turn=%d user_turn=%s kind=%s prompt_est_tokens=%d response_chars=%d",
                session_id,
                turn_num,
                turn_record.get("user_turn_num", "-"),
                raw_turn_kind,
                _estimate_openai_body_input_tokens({"messages": messages, "tools": tools}),
                len(response_text),
            )
            self._maybe_finalize_ready_turns(session_id)
        else:
            logger.info("[Proxy] SIDE session=%s -> skipped (side-channel turn)", session_id)

        if session_done:
            await self._close_session(session_id)

        output["session_id"] = session_id
        return {"response": output}

    def _truncate_messages(
        self,
        messages: list[dict],
        tools,
        max_prompt_tokens: int,
    ) -> list[dict]:
        """Drop oldest non-system messages using a dependency-free token estimate."""

        def _prompt_len(msgs):
            return _estimate_openai_body_input_tokens({"messages": msgs, "tools": tools})

        if _prompt_len(messages) <= max_prompt_tokens:
            return messages

        # Split into system and non-system messages
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        non_sys = [m for m in messages if m.get("role") != "system"]

        # Drop oldest non-system messages until the prompt fits, always keeping
        # the system message(s) plus at least the most recent non-system message.
        # The loop MUST be bounded by len(non_sys): if even sys_msgs + the last
        # message exceeds the limit (e.g. an oversized skill-injected system
        # prompt), the old `while len(non_sys) > 1` form spun forever at 100% CPU
        # because len(non_sys) never changed and no candidate ever fit.
        dropped = 0
        while dropped < len(non_sys) - 1:
            candidate = sys_msgs + non_sys[dropped:]
            if _prompt_len(candidate) <= max_prompt_tokens:
                break
            dropped += 1

        result = sys_msgs + non_sys[dropped:]
        if dropped >= len(non_sys) - 1 and _prompt_len(result) > max_prompt_tokens:
            logger.warning(
                "[Proxy] context still over limit after truncation "
                "(%d est tokens > limit=%d): system prompt alone may exceed the "
                "context budget; forwarding best-effort to avoid stalling",
                _prompt_len(result),
                max_prompt_tokens,
            )
        if dropped:
            logger.info(
                "[Proxy] context truncated: dropped %d oldest messages (%d -> %d est tokens, limit=%d)",
                dropped,
                _prompt_len(messages),
                _prompt_len(result),
                max_prompt_tokens,
            )
        return result

    def _inject_skills(self, messages: list[dict]) -> tuple[list[dict], list[str]]:
        """Inject skill catalog into the system message.

        Lists ALL eligible skills as an XML ``<available_skills>`` catalog
        with ``<name>``, ``<description>``, and ``<location>`` per entry.
        The model is instructed to ``read`` at most one SKILL.md when
        relevant (lazy loading).

        Returns (modified_messages, listed_skill_names).
        """
        if not self.skill_manager:
            return messages, []

        try:
            self.skill_manager.refresh_if_changed()
        except Exception as e:
            logger.warning("[SkillManager] failed to refresh local skills: %s", e)

        skill_text = self.skill_manager.build_injection_prompt(
            max_chars=getattr(self.config, "max_skills_prompt_chars", 30_000),
            read_tool_name="skill_view",
        )
        if not skill_text:
            return messages, []

        all_skills = self.skill_manager.get_all_skills()
        skill_names = [s.get("name", "unknown_skill") for s in all_skills if isinstance(s, dict)]
        logger.info(
            "[SkillManager] listing %d skills in catalog: %s",
            len(skill_names),
            ", ".join(skill_names)[:400],
        )

        self.skill_manager.record_injection(skill_names)

        messages = list(messages)
        sys_indices = [i for i, m in enumerate(messages) if m.get("role") == "system"]
        if sys_indices:
            idx = sys_indices[0]
            existing = _flatten_message_content(messages[idx].get("content", ""))
            messages[idx] = {**messages[idx], "content": existing + "\n\n" + skill_text}
        else:
            messages.insert(0, {"role": "system", "content": skill_text})

        return messages, skill_names
