"""Proxy server composition and lifecycle.

``ProxyServer`` composes the mixin modules (routing, handling, session,
recording, forwarding, uploads) into a client-side proxy between agent
clients and the upstream model. It owns per-session state, threading
lifecycle (uvicorn in a background thread), and idle/validation accessors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Optional

import uvicorn

from ..config import SkillGeneConfig
from ..prm import PRMScorer
from ..skills.manager import SkillManager
from .forwarding import ForwardingMixin
from .handler import HandlerMixin
from .recording import RecordingMixin
from .routes import RoutesMixin
from .session import (
    _SESSION_IDLE_CLOSE_SECONDS,
    _SESSION_SWEEP_INTERVAL_SECONDS,
    _SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
    SessionMixin,
)
from .skills_admin import SkillsAdminMixin
from .uploads import UploadsMixin
from .users_admin import UsersAdminMixin

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_RESET = "\033[0m"


class ProxyServer(
    RoutesMixin,
    HandlerMixin,
    SessionMixin,
    RecordingMixin,
    ForwardingMixin,
    UploadsMixin,
    SkillsAdminMixin,
    UsersAdminMixin,
):
    """Proxy between client agents and the upstream model with skill hooks.

    Hermes sends ``X-Session-Id`` and ``X-Turn-Type`` headers with every
    request. The proxy injects skills, records conversation artifacts when
    enabled, and can attach PRM scoring when configured. Side tasks
    (``turn_type != "main"``) are forwarded but do not generate the main
    conversation artifact path.

    Parameters
    ----------
    config:
        SkillGeneConfig instance.
    skill_manager:
        Optional SkillManager for injecting skills into system prompts.
    prm_scorer:
        Optional PRMScorer for turn feedback.
    """

    def __init__(
        self,
        config: SkillGeneConfig,
        sampling_client=None,
        skill_manager: Optional[SkillManager] = None,
        prm_scorer: Optional[PRMScorer] = None,
        last_request_tracker=None,
    ):
        self.config = config
        self._sampling_client = sampling_client
        self.skill_manager = skill_manager
        self.prm_scorer = prm_scorer
        self._last_request_tracker = last_request_tracker
        self._last_request_at = time.time()

        self._served_model = config.served_model_name
        self._expected_api_key = config.proxy_api_key
        os.makedirs(config.record_dir, exist_ok=True)

        # State machines
        self._turn_counts: dict[str, int] = {}
        self._user_turn_counts: dict[str, int] = {}
        self._pending_turn_data: dict[str, dict[int, dict]] = {}  # session → {turn → data}
        self._prm_tasks: dict[str, dict[int, asyncio.Task]] = {}  # session → {turn → task}
        self._pending_records: dict[str, dict] = {}  # for record logging
        self._session_scored_turns: dict[str, int] = {}  # session -> finalized PRM turn count
        self._session_turns: dict[str, list] = {}
        self._session_user_alias: dict[str, str] = {}  # session -> real client user
        self._session_viking_context: dict[str, dict[str, str]] = {}
        self._session_last_active: dict[str, float] = {}  # session -> unix_ts
        self._closing_sessions: set[str] = set()  # session ids currently being closed
        self._background_tasks: set[asyncio.Task] = set()  # transient async tasks (upload, submit)
        self._session_sweeper_task: Optional[asyncio.Task] = None
        self._skill_reload_task: Optional[asyncio.Task] = None
        self._session_idle_close_seconds = max(
            0,
            int(getattr(config, "session_idle_close_seconds", _SESSION_IDLE_CLOSE_SECONDS)),
        )
        self._session_sweep_interval_seconds = max(
            1,
            int(getattr(config, "session_sweep_interval_seconds", _SESSION_SWEEP_INTERVAL_SECONDS)),
        )
        self._shutdown_drain_timeout_seconds = max(
            1,
            int(getattr(config, "shutdown_drain_timeout_seconds", _SHUTDOWN_DRAIN_TIMEOUT_SECONDS)),
        )
        self._skill_reload_interval_seconds = max(
            5,
            int(getattr(config, "sharing_skill_reload_interval_seconds", 30) or 30),
        )

        # Record files
        self._record_file = ""
        self._prm_record_file = ""
        if config.record_enabled:
            os.makedirs(config.record_dir, exist_ok=True)
            self._record_file = os.path.join(config.record_dir, "conversations.jsonl")
            self._prm_record_file = os.path.join(config.record_dir, "prm_scores.jsonl")
            with open(self._record_file, "w"):
                pass
            with open(self._prm_record_file, "w"):
                pass

        self.app = self._build_app()

        # Threading lifecycle (set by start())
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._server_stopped_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Idle / validation accessors                                          #
    # ------------------------------------------------------------------ #

    def _mark_request_activity(self) -> None:
        self._last_request_at = time.time()
        if self._last_request_tracker is not None:
            try:
                self._last_request_tracker.touch()
            except Exception:
                pass

    def last_request_age_seconds(self) -> Optional[float]:
        last = getattr(self, "_last_request_at", None)
        if last is None:
            return None
        return max(0.0, time.time() - float(last))

    def active_session_count(self) -> int:
        return len(self._collect_active_session_ids())

    def is_idle_for_validation(self, idle_after_seconds: int) -> bool:
        age = self.last_request_age_seconds()
        if age is None:
            return False
        if self.active_session_count() > 0:
            return False
        return age >= max(0, int(idle_after_seconds))

    async def _shutdown_cleanup(self) -> None:
        if self._skill_reload_task is not None:
            self._skill_reload_task.cancel()
            await asyncio.gather(self._skill_reload_task, return_exceptions=True)
            self._skill_reload_task = None
        if self._session_sweeper_task is not None:
            self._session_sweeper_task.cancel()
            await asyncio.gather(self._session_sweeper_task, return_exceptions=True)
            self._session_sweeper_task = None
        await self._drain_active_sessions(reason="server_shutdown")
        await self._await_background_tasks(self._shutdown_drain_timeout_seconds)

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready_event.clear()
        self._server_stopped_event.clear()
        cfg = uvicorn.Config(
            self.app,
            host=self.config.proxy_host,
            port=self.config.proxy_port,
            log_level="info",
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        threading.Thread(target=self._print_ready_banner, daemon=True).start()

    def _run_server(self):
        try:
            self._server.run()
        finally:
            self._server_stopped_event.set()
            self._ready_event.clear()

    def _print_ready_banner(self):
        if not self._ready_event.wait(timeout=30):
            return
        if self._server_stopped_event.is_set():
            return
        backend = f"LLM ({self.config.llm_model_id or 'upstream'})"
        banner = (
            f"\n{'=' * 70}\n"
            f"  SkillGene proxy ready\n"
            f"  proxy {self.config.proxy_host}:{self.config.proxy_port} → {backend}\n"
            f"  Agent has been configured to use this proxy automatically.\n"
            f"{'=' * 70}\n"
        )
        logger.info(f"{_GREEN}{banner}{_RESET}")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._ready_event.clear()
        self._server_stopped_event.set()

    def wait_until_ready(self, timeout_s: float = 30.0) -> bool:
        return self._ready_event.wait(timeout=timeout_s)

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def _safe_create_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            self._task_done_cb(t)

        task.add_done_callback(_on_done)
        return task

    @staticmethod
    def _task_done_cb(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("[Proxy] background task failed: %s", exc, exc_info=exc)
