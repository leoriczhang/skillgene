"""Session lifecycle: turn classification, idle sweeping, and close/drain.

Holds the pure turn-classification helpers plus ``SessionMixin`` — the
state-machine half of :class:`~skillgene.proxy.server.ProxyServer` that
tracks per-session turn counters, sweeps idle sessions, and flushes /
closes sessions (finalizing pending PRM feedback and queuing uploads).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .attribution import _extract_modified_skill_names

logger = logging.getLogger(__name__)

_SESSION_IDLE_CLOSE_SECONDS = 180
_SESSION_SWEEP_INTERVAL_SECONDS = 15
_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 15
_VALID_TURN_TYPES = {"main", "side"}
_TRUE_STRINGS = {"1", "true", "yes", "on"}


def _resolve_turn_type(
    header_turn_type: Optional[str],
    body_turn_type: Any,
    *,
    default: str = "main",
) -> str:
    """Resolve request turn_type safely.

    Defaults to ``main`` to avoid silently dropping record/PRM paths when
    clients include a session id but forget to provide turn_type.
    """
    if default not in _VALID_TURN_TYPES:
        default = "main"
    candidate = header_turn_type if header_turn_type is not None else body_turn_type
    raw = str(candidate or "").strip().lower()
    if not raw:
        return default
    if raw in _VALID_TURN_TYPES:
        return raw
    logger.warning("[SessionDetect] invalid turn_type=%r; fallback=%s", raw, default)
    return default


def _resolve_session_done(
    header_session_done: Optional[str],
    body_session_done: Any,
) -> bool:
    """Resolve session_done from header or body."""
    candidate = header_session_done if header_session_done is not None else body_session_done
    if isinstance(candidate, bool):
        return candidate
    if candidate is None:
        return False
    return str(candidate).strip().lower() in _TRUE_STRINGS


def _classify_raw_turn_kind(content: str, tool_calls: list[dict]) -> str:
    """Classify recorded raw/main turns for user-turn cadence decisions."""
    if tool_calls:
        return "tool_use"
    return "final"


def _is_user_turn_boundary(raw_turn_kind: str) -> bool:
    """Only final assistant responses advance the user-visible turn counter."""
    return raw_turn_kind == "final"


class SessionMixin:
    """Per-session turn tracking, idle sweeping, and close/drain state machine."""

    def _touch_session(self, session_id: str) -> None:
        if session_id:
            self._session_last_active[session_id] = time.time()

    def _collect_active_session_ids(self) -> list[str]:
        session_ids = set(self._session_last_active.keys())
        session_ids.update(self._pending_records.keys())
        session_ids.update(self._session_turns.keys())
        session_ids.update(self._pending_turn_data.keys())
        session_ids.update(self._turn_counts.keys())
        session_ids.update(self._session_scored_turns.keys())
        session_ids.update(self._prm_tasks.keys())
        return sorted(s for s in session_ids if s and s not in self._closing_sessions)

    def _collect_idle_session_ids(self, now: Optional[float] = None) -> list[str]:
        if self._session_idle_close_seconds <= 0:
            return []
        if now is None:
            now = time.time()
        threshold = float(self._session_idle_close_seconds)
        return sorted(
            sid
            for sid, ts in self._session_last_active.items()
            if sid and sid not in self._closing_sessions and (now - float(ts)) >= threshold
        )

    def _start_session_idle_sweeper(self) -> None:
        if self._session_idle_close_seconds <= 0:
            logger.info("[SessionDetect] idle sweeper disabled (timeout <= 0)")
            return
        if self._session_sweeper_task is not None and not self._session_sweeper_task.done():
            return
        self._session_sweeper_task = asyncio.create_task(self._session_idle_sweeper_loop())
        self._session_sweeper_task.add_done_callback(self._task_done_cb)
        logger.info(
            "[SessionDetect] idle sweeper started (timeout=%ss interval=%ss)",
            self._session_idle_close_seconds,
            self._session_sweep_interval_seconds,
        )

    async def _session_idle_sweeper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._session_sweep_interval_seconds)
                stale_ids = self._collect_idle_session_ids()
                for sid in stale_ids:
                    await self._close_session(sid, reason="idle_timeout")
        except asyncio.CancelledError:
            logger.info("[SessionDetect] idle sweeper stopped")
            raise

    async def _drain_active_sessions(self, reason: str) -> None:
        active_ids = self._collect_active_session_ids()
        if not active_ids:
            return
        logger.info("[SessionDetect] draining %d active session(s): reason=%s", len(active_ids), reason)
        for sid in active_ids:
            await self._close_session(sid, reason=reason)

    async def _flush_sessions(self, user_aliases: Optional[set[str]] = None) -> list[str]:
        """Close matching active sessions and block until their uploads finish.

        Unlike ``_drain_active_sessions``, this waits for the fire-and-forget
        upload tasks queued inside ``_close_session`` so that an immediate read of
        the uploaded ``sessions/*.json`` reflects the just-closed sessions. When
        ``user_aliases`` is given, only sessions whose recorded client identity
        (``X-SkillGene-User``) matches are closed — this keeps a flush for
        one suite's test users from tearing down another suite's in-flight
        sessions on a shared proxy.
        """
        active_ids = self._collect_active_session_ids()
        if user_aliases:
            targets = [
                sid
                for sid in active_ids
                if (self._session_user_alias.get(sid) or "").strip() in user_aliases
            ]
        else:
            targets = list(active_ids)
        if not targets:
            return []
        logger.info(
            "[SessionDetect] flushing %d session(s) on demand (aliases=%s)",
            len(targets),
            sorted(user_aliases) if user_aliases else "*",
        )
        for sid in targets:
            await self._close_session(sid, reason="explicit_flush")
        # _close_session queues uploads via _safe_create_task; wait for them so the
        # caller can safely read the pooled sessions immediately afterwards.
        await self._await_background_tasks(self._shutdown_drain_timeout_seconds)
        return targets

    async def _close_session(self, session_id: str, reason: str = "explicit") -> None:
        """Flush a session: finalize pending turn feedback, upload session data, clean up state."""
        if not session_id:
            return
        if session_id in self._closing_sessions:
            return
        self._closing_sessions.add(session_id)
        try:
            self._flush_pending_record(session_id, None)
            pending = self._pending_turn_data.get(session_id, {})
            prm_tasks = self._prm_tasks.setdefault(session_id, {})
            if self.config.use_prm and self.prm_scorer:
                for turn_num, turn_data in list(pending.items()):
                    if turn_num in prm_tasks:
                        continue
                    prm_task = asyncio.create_task(
                        self.prm_scorer.evaluate(
                            turn_data.get("response_text", ""),
                            turn_data.get("prompt_text", ""),
                            session_id=session_id,
                            turn_num=turn_num,
                        )
                    )
                    prm_task.add_done_callback(self._task_done_cb)
                    prm_task.add_done_callback(
                        lambda _t, sid=session_id, tnum=turn_num: self._on_prm_done_record_only(sid, tnum, _t)
                    )
                    prm_tasks[turn_num] = prm_task
            active_prm_tasks = list(prm_tasks.values())
            if active_prm_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*active_prm_tasks, return_exceptions=True),
                        timeout=_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[SessionDetect] PRM drain timed out for session=%s", session_id)
            for turn_num in sorted(list(pending.keys())):
                turn_data = pending.pop(turn_num)
                prm_result = turn_data.pop("prm_result", None)
                prm_task = prm_tasks.get(turn_num)
                if prm_result is None and prm_task is not None and prm_task.done():
                    try:
                        prm_result = prm_task.result()
                    except (asyncio.CancelledError, Exception):
                        prm_result = None
                prm_tasks.pop(turn_num, None)
                await self._finalize_turn_feedback(
                    turn_num,
                    turn_data,
                    session_id,
                    prm_result,
                )
            eff = self._session_scored_turns.pop(session_id, 0)
            self._turn_counts.pop(session_id, None)
            self._user_turn_counts.pop(session_id, None)
            self._pending_turn_data.pop(session_id, None)
            prm_tasks = self._prm_tasks.pop(session_id, {})
            for task in prm_tasks.values():
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
            logger.info(
                "[SessionDetect] closed session=%s reason=%s (scored_turns=%d)",
                session_id,
                reason,
                eff,
            )
            if self.skill_manager:
                self.skill_manager.flush_stats()
            turns = self._session_turns.pop(session_id, [])
            modified_skill_names = _extract_modified_skill_names(turns)
            if self.config.sharing_enabled:
                self._safe_create_task(self._pull_skills_from_cloud(skip_names=modified_skill_names))
            self._session_last_active.pop(session_id, None)
        finally:
            self._closing_sessions.discard(session_id)

    def _next_user_turn_num(self, session_id: str) -> int:
        self._user_turn_counts[session_id] = self._user_turn_counts.get(session_id, 0) + 1
        return self._user_turn_counts[session_id]

    def _advance_user_turn_and_maybe_upload(self, session_id: str) -> int:
        return self._next_user_turn_num(session_id)

    def _maybe_finalize_ready_turns(self, session_id: str):
        """Finalize turns whose optional PRM scoring is done."""
        prm_tasks = self._prm_tasks.setdefault(session_id, {})
        pending = self._pending_turn_data.get(session_id, {})
        for turn_num in sorted(list(pending.keys())):
            prm_task = prm_tasks.get(turn_num)
            if self.config.use_prm and self.prm_scorer:
                if prm_task is None:
                    continue  # waiting for the next turn to provide scoring context
                if not prm_task.done():
                    continue

            turn_data = pending.pop(turn_num)
            prm_result = turn_data.pop("prm_result", None)
            if prm_result is None and prm_task is not None and prm_task.done():
                try:
                    prm_result = prm_task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                prm_tasks.pop(turn_num, None)

            self._safe_create_task(
                self._finalize_turn_feedback(
                    turn_num,
                    turn_data,
                    session_id,
                    prm_result,
                )
            )
