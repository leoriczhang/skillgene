"""Recording, PRM scoring, and turn feedback finalization.

``RecordingMixin`` buffers per-turn conversation records to JSONL, fires
PRM scoring tasks, applies scores back onto turn records / skill stats, and
finalizes turn feedback. It also records the per-session client identity and
OpenViking routing context forwarded via request headers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from .messages import _extract_last_user_instruction, _flatten_message_content

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_RESET = "\033[0m"


class RecordingMixin:
    """Conversation recording, PRM scoring, and turn feedback finalization."""

    # ------------------------------------------------------------------ #
    # Record helpers                                                       #
    # ------------------------------------------------------------------ #

    def _flush_pending_record(self, session_id: str, next_state):
        """Write out the buffered record for *session_id* and fire PRM."""
        rec = self._pending_records.pop(session_id, None)
        if rec is None:
            return
        rec["next_state"] = next_state
        if next_state:
            ns_role = next_state.get("role", "?")
            ns_content = _flatten_message_content(next_state.get("content"))
            logger.info(
                f"{_GREEN}[Proxy] session={session_id} turn={rec['turn']} "
                f"next_state role={ns_role} len={len(ns_content)}: "
                f"{ns_content[:200]}{_RESET}"
            )
            self._fire_prm_scoring(
                session_id,
                rec["turn"],
                rec["response_text"],
                rec.get("instruction_text", ""),
                next_state,
            )
        if self._record_file:
            try:
                with open(self._record_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("[Proxy] failed to write record: %s", e)

    def _buffer_record(
        self, session_id: str, turn_num: int, messages: list, prompt_text: str, response_text: str, tool_calls: list
    ):
        if not self._record_file:
            return
        instruction_text = _extract_last_user_instruction(messages)
        self._pending_records[session_id] = {
            "session_id": session_id,
            "turn": turn_num,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "messages": messages,
            "instruction_text": instruction_text,
            "prompt_text": prompt_text,
            "response_text": response_text,
            "tool_calls": tool_calls or None,
        }

    def _append_prm_record(self, session_id: str, turn_num: int, score: float, votes: list):
        if not self._prm_record_file:
            return
        try:
            with open(self._prm_record_file, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "session_id": session_id,
                            "turn": turn_num,
                            "score": score,
                            "votes": votes,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError as e:
            logger.warning("[Proxy] failed to write PRM record: %s", e)

    def purge_record_files(self):
        """Clear all record JSONL files."""
        for path, label in [
            (self._record_file, "record"),
            (self._prm_record_file, "PRM record"),
        ]:
            if not path:
                continue
            try:
                open(path, "w").close()
                logger.info("[Proxy] %s file purged: %s", label, path)
            except OSError as e:
                logger.warning("[Proxy] failed to purge %s file: %s", label, e)

    # ------------------------------------------------------------------ #
    # PRM scoring                                                          #
    # ------------------------------------------------------------------ #

    def _fire_prm_scoring(
        self,
        session_id: str,
        turn_num: int,
        response_text: str,
        instruction_text: str,
        next_state,
        finalize_ready_turns: bool = True,
    ):
        if not self.prm_scorer or not next_state:
            return
        inst_text = instruction_text or ""
        task = asyncio.create_task(
            self.prm_scorer.evaluate(response_text, inst_text, session_id=session_id, turn_num=turn_num)
        )
        task.add_done_callback(self._task_done_cb)
        if finalize_ready_turns:
            task.add_done_callback(lambda _t: self._on_prm_done(session_id, turn_num, _t))
        else:
            task.add_done_callback(lambda _t: self._on_prm_done_record_only(session_id, turn_num, _t))
        self._prm_tasks.setdefault(session_id, {})[turn_num] = task
        td = self._pending_turn_data.get(session_id, {}).get(turn_num)
        if td is not None:
            td["has_next_state"] = True

    def _apply_prm_result(
        self,
        session_id: str,
        turn_num: int,
        prm_result: Optional[dict],
    ) -> None:
        score = prm_result.get("score", 0.0) if prm_result else 0.0
        turns = self._session_turns.get(session_id, [])
        # turn_num is 1-based; list index is 0-based
        idx = turn_num - 1
        if 0 <= idx < len(turns):
            turns[idx]["prm_score"] = score
            injected = turns[idx].get("injected_skills", [])
            if injected and self.skill_manager:
                self.skill_manager.record_feedback(injected, score)
            read = turns[idx].get("read_skills", [])
            if read and self.skill_manager:
                read_names = [r["skill_name"] for r in read if isinstance(r, dict) and r.get("skill_name")]
                if read_names:
                    self.skill_manager.record_feedback(read_names, score)
        pending_turn = self._pending_turn_data.get(session_id, {}).get(turn_num)
        if isinstance(pending_turn, dict):
            pending_turn["prm_result"] = prm_result

    def _on_prm_done(self, session_id: str, turn_num: int, task: asyncio.Task):
        """Callback after PRM scoring completes — write score back and update skill stats."""
        if task.cancelled():
            return
        try:
            prm_result = task.result()
        except Exception:
            return
        self._apply_prm_result(session_id, turn_num, prm_result)
        if session_id in self._closing_sessions:
            return
        self._maybe_finalize_ready_turns(session_id)

    def _on_prm_done_record_only(self, session_id: str, turn_num: int, task: asyncio.Task):
        """Callback used for close-session PRM tasks; records score only."""
        if task.cancelled():
            return
        try:
            prm_result = task.result()
        except Exception:
            return
        self._apply_prm_result(session_id, turn_num, prm_result)

    # ------------------------------------------------------------------ #
    # Session identity / routing context                                   #
    # ------------------------------------------------------------------ #

    def _record_session_user(self, session_id: str, user_alias: Optional[str]) -> None:
        """Remember the real client identity behind a session.

        hermes (and other clients) send ``X-SkillGene-User`` so the proxy can
        attribute recorded sessions to the actual team member rather than the
        proxy's own server-side ``$USER``/``sharing_user_alias``.
        """
        alias = (user_alias or "").strip()
        if session_id and alias:
            self._session_user_alias[session_id] = alias

    def _record_session_context(
        self,
        session_id: str,
        user_alias: Optional[str],
        *,
        viking_api_key: Optional[str] = None,
        viking_account: Optional[str] = None,
        viking_user: Optional[str] = None,
        viking_agent_id: Optional[str] = None,
        viking_customer_id: Optional[str] = None,
        viking_group_id: Optional[str] = None,
        viking_root_prefix: Optional[str] = None,
    ) -> None:
        """Remember per-request OpenViking routing for session upload.

        In eval/proxy deployments, the server process has one static local
        config, while each incoming agent request may belong to a distinct
        OpenViking account/user/peer/team. The client forwards that routing
        context with X-SkillGene-Viking-* / X-SkillGene-Group-Ids /
        X-SkillGene-Root-Prefix headers so recorded sessions land in the caller's
        team-shared queue (``viking://resources/{root_prefix}/sessions/``, with an
        optional ``{group_id}`` segment when the caller sets one for isolation)
        instead of falling back to the server's static local config. ``group_id``
        may arrive comma-separated; only the first group is used for the storage
        path.

        When the caller also forwards ``X-SkillGene-Viking-Api-Key``, that
        key authenticates the upload so skills land under *that* key's Resources
        space; omitting it falls back to the server's statically configured
        ``sharing_viking_api_key`` (fully backward compatible).
        """
        self._record_session_user(session_id, user_alias)
        if not session_id:
            return
        group_id = (viking_group_id or "").split(",")[0].strip().strip("/")
        ctx = {
            "api_key": (viking_api_key or "").strip(),
            "account": (viking_account or "").strip(),
            "user": (viking_user or "").strip(),
            "agent_id": (viking_agent_id or "").strip(),
            "customer_id": (viking_customer_id or user_alias or "").strip(),
            "group_id": group_id,
            "root_prefix": (viking_root_prefix or "").strip().strip("/"),
        }
        ctx = {k: v for k, v in ctx.items() if v}
        if ctx:
            self._session_viking_context[session_id] = ctx

    # ------------------------------------------------------------------ #
    # Turn feedback finalization                                           #
    # ------------------------------------------------------------------ #

    async def _finalize_turn_feedback(
        self,
        turn_num: int,
        turn_data: dict[str, Any],
        session_id: str,
        prm_result: Optional[dict],
    ):
        """Finalize a turn after optional PRM scoring.

        The proxy fronts external agents, so finalization keeps only
        feedback/record side effects that are consumed by the framework.
        """
        score = prm_result.get("score", 0.0) if prm_result else 0.0
        if prm_result:
            self._append_prm_record(session_id, turn_num, score, prm_result.get("votes", []))
            self._session_scored_turns[session_id] = self._session_scored_turns.get(session_id, 0) + 1

        logger.info(
            "[Proxy] finalized turn session=%s turn=%d score=%.1f response_chars=%d",
            session_id,
            turn_num,
            score,
            len(turn_data.get("response_text", "")),
        )
