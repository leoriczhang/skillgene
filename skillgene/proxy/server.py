"""Proxy server composition and lifecycle.

``ProxyServer`` composes the FastAPI route and skill synchronization mixins
into the SkillGene service. It owns threading lifecycle (uvicorn in a
background thread) and idle/validation accessors.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

import uvicorn

from ..config import SkillGeneConfig
from ..prm import PRMScorer
from ..skills.manager import SkillManager
from .routes import RoutesMixin
from .skills_admin import SkillsAdminMixin
from .uploads import UploadsMixin
from .users_admin import UsersAdminMixin

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_RESET = "\033[0m"


class ProxyServer(
    RoutesMixin,
    UploadsMixin,
    SkillsAdminMixin,
    UsersAdminMixin,
):
    """SkillGene service: console, skill sync, user management, and validation.

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

        self._background_tasks: set[asyncio.Task] = set()
        self._skill_reload_task: Optional[asyncio.Task] = None
        self._shutdown_drain_timeout_seconds = 15
        self._skill_reload_interval_seconds = max(
            5,
            int(getattr(config, "sharing_skill_reload_interval_seconds", 30) or 30),
        )

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
        return 0

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
        banner = (
            f"\n{'=' * 70}\n"
            f"  SkillGene service ready\n"
            f"  http://{self.config.proxy_host}:{self.config.proxy_port}\n"
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
