"""FastAPI application and route wiring for the SkillGene service.

``RoutesMixin`` builds the ``FastAPI`` app and its endpoints (console,
health, skill/user admin, model settings, and internal skill reload). Route bodies delegate to the owning
:class:`~skillgene.proxy.server.ProxyServer` instance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .users_admin import (
    _find_user,
    _load_registry,
    _public_user,
    _registry_path,
    _save_registry,
    _upsert_user,
    _verify_password,
)
from ..config_store import ConfigStore
from ..session_filter import SessionValueClassifier
from ..session_store import SessionStore
from ..skills.hub import SkillHub
from ..storage import is_not_found_error
from ..validation.store import ValidationStore

logger = logging.getLogger(__name__)
_SESSION_COOKIE = "skillgene_console_session"
_SESSION_TTL_SECONDS = 24 * 60 * 60


def _model_settings_payload(config, store_data: dict[str, Any]) -> dict[str, Any]:
    llm = store_data.get("llm") if isinstance(store_data.get("llm"), dict) else {}
    api_key = str(getattr(config, "llm_api_key", "") or llm.get("api_key") or "")
    return {
        "provider": str(llm.get("provider") or getattr(config, "llm_provider", "") or "custom"),
        "base_url": str(getattr(config, "llm_api_base", "") or llm.get("api_base") or ""),
        "model": str(getattr(config, "llm_model_id", "") or llm.get("model_id") or ""),
        "max_tokens": int(getattr(config, "llm_max_tokens", 0) or llm.get("max_tokens") or 100000),
        "temperature": float(getattr(config, "llm_temperature", 0.0) if getattr(config, "llm_temperature", None) is not None else llm.get("temperature", 0.4)),
        "api_key_present": bool(api_key),
    }


def _require_admin_user(user: dict | None) -> None:
    if not user or str(user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="only admin users can perform this operation")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_session_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="session_id is required")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip(".-/")[:160] or "session"


def _check_ingest_api_key(request: Request) -> None:
    expected = str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
    if not expected:
        return
    header = str(request.headers.get("authorization") or "").strip()
    token = header[7:].strip() if header.lower().startswith("bearer ") else header
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid ingest api key")


def _max_session_body_bytes() -> int:
    try:
        value = int(os.environ.get("SKILLGENE_MAX_SESSION_BODY_BYTES", str(8 * 1024 * 1024)) or 0)
    except ValueError:
        value = 8 * 1024 * 1024
    return max(1024, value)


async def _read_limited_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    limit = _max_session_body_bytes()
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"session body exceeds {limit} bytes")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="session body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="session body must be an object")
    return parsed


def _session_queue_snapshot(config, *, limit: int = 100) -> dict[str, Any]:
    try:
        store = SessionStore.from_config(config)
        rows = store.list_queue(limit=limit if limit > 0 else 100000)
        return {
            "reachable": True,
            "pending": len(rows),
            "sessions": rows[:limit] if limit > 0 else [],
        }
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "sessions": [], "pending": 0, "reason": str(exc)}


def _session_detail_payload(session: dict[str, Any]) -> dict[str, Any]:
    status = str(session.get("status") or "queued")
    turns = session.get("turns") if isinstance(session.get("turns"), list) else []
    metrics = session.get("metrics") if isinstance(session.get("metrics"), dict) else {}
    return {
        "meta": {
            "title": session.get("title") or "",
            "user_alias": session.get("user_alias") or "",
            "status": status,
            "num_turns": len(turns) if turns else metrics.get("interaction_turns"),
        },
        "turns_available": bool(turns),
        "turns_source": "archive",
        "system_prompt": session.get("system_prompt") or "",
        "injected_skills": session.get("injected_skills") or [],
        "used_skills": session.get("used_skills") or [],
        "metrics": metrics,
        "turns": turns,
        "value_judge": session.get("value_judge") if isinstance(session.get("value_judge"), dict) else {},
    }


def _history_from_archived_sessions(config, *, limit: int = 50, session_id: str = "") -> list[dict[str, Any]]:
    try:
        store = SessionStore.from_config(config)
        rows = store.list_conversations(limit=100000)
    except Exception:
        return []
    wanted = str(session_id or "").strip()
    if wanted:
        rows = [row for row in rows if str(row.get("session_id") or "") == wanted]
    cycles: list[dict[str, Any]] = []
    for row in rows[: max(0, int(limit))]:
        status = str(row.get("status") or "")
        judge = row.get("value_judge") if isinstance(row.get("value_judge"), dict) else {}
        cycles.append(
            {
                "timestamp": row.get("ingested_at") or row.get("timestamp"),
                "session_ids": [row.get("session_id")],
                "sessions": 1,
                "skill_groups": 0,
                "uploaded_skills": 0,
                "candidates_queued": 0,
                "judge": {
                    "overall_score": judge.get("confidence"),
                    "rationale": judge.get("reason"),
                    "decision": judge.get("decision"),
                },
                "evolutions": [],
                "status": status,
            }
        )
    return cycles


def _storage_status(config) -> dict[str, Any]:
    backend = str(getattr(config, "sharing_backend", "") or "").strip().lower()
    endpoint = str(getattr(config, "sharing_viking_endpoint", "") or getattr(config, "sharing_endpoint", "") or "")
    namespace = "resources" if backend == "viking" else backend or "none"
    api_key_present = bool(
        str(getattr(config, "sharing_viking_team_api_key", "") or "")
        or str(getattr(config, "sharing_viking_api_key", "") or "")
    )
    payload: dict[str, Any] = {
        "backend": backend or "none",
        "endpoint": endpoint,
        "namespace": namespace,
        "api_key_present": api_key_present,
        "reachable": False,
    }
    if not getattr(config, "sharing_enabled", False):
        payload["reason"] = "sharing_disabled"
        return payload
    try:
        hub = SkillHub.team_from_config(config)
        # Probe the configured store. Missing manifest is still a successful
        # connectivity check: it means the bucket/key is reachable but empty.
        try:
            hub._bucket.get_object(hub._manifest_key())
        except Exception as exc:  # noqa: BLE001
            if not is_not_found_error(exc):
                raise
        payload["reachable"] = True
        return payload
    except Exception as exc:  # noqa: BLE001
        payload["reason"] = str(exc)
        return payload


class RoutesMixin:
    """FastAPI app construction, routing, and request authentication."""

    def _build_app(self) -> FastAPI:
        owner = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            owner._ready_event.set()
            owner._start_skill_reload_polling()
            try:
                yield
            finally:
                owner._ready_event.clear()
                await owner._shutdown_cleanup()

        app = FastAPI(title="SkillGene", lifespan=lifespan)
        app.state.owner = self
        self._console_sessions = getattr(self, "_console_sessions", {})
        dist_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "dist"))
        dist_index = os.path.join(dist_dir, "index.html")
        dist_assets = os.path.join(dist_dir, "assets")
        if os.path.isdir(dist_assets):
            app.mount("/assets", StaticFiles(directory=dist_assets), name="assets")

        def _session_user(request: Request) -> dict | None:
            token = request.cookies.get(_SESSION_COOKIE, "")
            if not token:
                return None
            session = owner._console_sessions.get(token)
            if not isinstance(session, dict):
                return None
            if float(session.get("expires_at", 0) or 0) < time.time():
                owner._console_sessions.pop(token, None)
                return None
            user_id = str(session.get("user_id") or "")
            if not user_id:
                return None
            data = _load_registry(_registry_path(owner.config))
            try:
                _idx, user = _find_user(data, user_id)
            except HTTPException:
                owner._console_sessions.pop(token, None)
                return None
            session["expires_at"] = time.time() + _SESSION_TTL_SECONDS
            return _public_user(user)

        def _users_empty() -> bool:
            data = _load_registry(_registry_path(owner.config))
            return not bool(data.get("users"))

        @app.middleware("http")
        async def require_console_auth(request: Request, call_next):
            path = request.url.path
            if path.startswith("/api/") and not path.startswith("/api/auth/"):
                if _users_empty():
                    return JSONResponse(status_code=401, content={"detail": "setup required", "needs_setup": True})
                if _session_user(request) is None:
                    return JSONResponse(status_code=401, content={"detail": "login required"})
            return await call_next(request)

        # Skill and user management REST APIs used by the unified console.
        self._register_skills_admin_routes(app)
        self._register_users_admin_routes(app)

        @app.get("/")
        @app.get("/console")
        async def console():
            if os.path.isfile(dist_index):
                return FileResponse(dist_index)
            return JSONResponse(status_code=404, content={"detail": "SkillGene console is not built"})

        @app.get("/api/auth/status")
        async def auth_status(request: Request):
            user = _session_user(request)
            return {
                "authenticated": bool(user),
                "needs_setup": _users_empty(),
                "user": user,
            }

        @app.post("/api/auth/bootstrap")
        async def auth_bootstrap(request: Request):
            if not _users_empty():
                raise HTTPException(status_code=409, detail="users already exist")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="bootstrap body must be an object")
            password = str(body.get("password") or "admin")
            payload = {
                "id": body.get("username") or body.get("id") or "admin",
                "display_name": body.get("display_name") or body.get("username") or "admin",
                "email": body.get("email") or "",
                "role": "admin",
                "password": password,
            }
            path = _registry_path(owner.config)
            data = _load_registry(path)
            user = _upsert_user(data, payload)
            _save_registry(path, data)
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            resp = JSONResponse(content={"authenticated": True, "needs_setup": False, "user": _public_user(user)})
            resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=_SESSION_TTL_SECONDS, path="/")
            return resp

        @app.post("/api/auth/login")
        async def auth_login(request: Request):
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="login body must be an object")
            username = str(body.get("username") or body.get("id") or "").strip()
            password = str(body.get("password") or "")
            if not username or not password:
                raise HTTPException(status_code=400, detail="username and password are required")
            data = _load_registry(_registry_path(owner.config))
            try:
                _idx, user = _find_user(data, username)
            except HTTPException as exc:
                raise HTTPException(status_code=401, detail="invalid username or password") from exc
            if not user.get("password_hash") or not _verify_password(password, str(user.get("password_hash") or "")):
                raise HTTPException(status_code=401, detail="invalid username or password")
            token = secrets.token_urlsafe(32)
            owner._console_sessions[token] = {
                "user_id": user.get("id"),
                "created_at": time.time(),
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
            resp = JSONResponse(content={"authenticated": True, "needs_setup": False, "user": _public_user(user)})
            resp.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=_SESSION_TTL_SECONDS, path="/")
            return resp

        @app.post("/api/auth/logout")
        async def auth_logout(request: Request):
            token = request.cookies.get(_SESSION_COOKIE, "")
            if token:
                owner._console_sessions.pop(token, None)
            resp = JSONResponse(content={"authenticated": False})
            resp.delete_cookie(_SESSION_COOKIE, path="/")
            return resp

        @app.get("/api/evolve-model")
        async def api_get_evolve_model():
            store = ConfigStore()
            return JSONResponse(content=_model_settings_payload(owner.config, store.load()))

        @app.post("/api/evolve-model")
        async def api_save_evolve_model(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="model settings body must be an object")

            model = str(body.get("model") or "").strip()
            base_url = str(body.get("base_url") or "").strip()
            provider = str(body.get("provider") or "custom").strip() or "custom"
            if not model:
                raise HTTPException(status_code=400, detail="model is required")
            if not base_url:
                raise HTTPException(status_code=400, detail="base_url is required")
            try:
                max_tokens = max(1, int(body.get("max_tokens") or owner.config.llm_max_tokens or 100000))
                temperature = float(
                    body.get("temperature")
                    if body.get("temperature") is not None
                    else owner.config.llm_temperature
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid max_tokens or temperature") from exc
            temperature = max(0.0, min(2.0, temperature))

            store = ConfigStore()
            data = store.load()
            llm = data.setdefault("llm", {})
            existing_key = str(llm.get("api_key") or owner.config.llm_api_key or "")
            raw_key = body.get("api_key")
            clear_key = bool(body.get("clear_api_key", False))
            api_key = "" if clear_key else existing_key
            if raw_key is not None and str(raw_key).strip():
                api_key = str(raw_key).strip()
            llm.update(
                {
                    "provider": provider,
                    "api_base": base_url,
                    "model_id": model,
                    "api_key": api_key,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
            )
            store.save(data)
            owner.config = store.to_config()
            return JSONResponse(content=_model_settings_payload(owner.config, data))

        @app.post("/api/evolve-model/test")
        async def api_test_evolve_model(request: Request):
            _require_admin_user(_session_user(request))
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
            store = ConfigStore()
            data = store.load()
            llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
            base_url = str(body.get("base_url") or owner.config.llm_api_base or llm.get("api_base") or "").strip()
            model = str(body.get("model") or owner.config.llm_model_id or llm.get("model_id") or "").strip()
            raw_key = body.get("api_key")
            api_key = str(raw_key).strip() if raw_key is not None and str(raw_key).strip() else str(owner.config.llm_api_key or llm.get("api_key") or "")
            if not base_url or not model or not api_key:
                raise HTTPException(status_code=400, detail="base_url, model and api_key are required for test")
            try:
                from openai import OpenAI

                client = OpenAI(api_key=api_key, base_url=base_url)
                started = time.time()
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a connectivity test endpoint."},
                        {"role": "user", "content": "Reply with exactly: ok"},
                    ],
                    "max_completion_tokens": 16,
                    "temperature": 0,
                }
                try:
                    resp = client.chat.completions.create(**payload)
                except Exception as first_exc:
                    body_text = getattr(getattr(first_exc, "response", None), "text", "") or ""
                    if "'temperature' is not supported" in body_text:
                        payload.pop("temperature", None)
                        resp = client.chat.completions.create(**payload)
                    elif "max_completion_tokens" in body_text:
                        payload["max_tokens"] = payload.pop("max_completion_tokens")
                        resp = client.chat.completions.create(**payload)
                    else:
                        raise
                content = resp.choices[0].message.content or ""
                return {
                    "ok": True,
                    "model": model,
                    "base_url": base_url,
                    "latency_ms": int((time.time() - started) * 1000),
                    "response": content[:200],
                }
            except Exception as exc:  # noqa: BLE001
                detail = str(exc)
                body_text = getattr(getattr(exc, "response", None), "text", "") or ""
                if body_text:
                    detail = body_text[:1000]
                raise HTTPException(status_code=400, detail=f"model test failed: {detail}") from exc

        @app.post("/ingest_session")
        async def ingest_session(request: Request):
            _check_ingest_api_key(request)
            body = await _read_limited_json_body(request)
            session_id = _safe_session_id(body.get("session_id"))
            session = dict(body)
            session["session_id"] = session_id
            session.setdefault("user_alias", str(getattr(owner.config, "sharing_user_alias", "") or "anonymous"))

            classifier = SessionValueClassifier.from_config(owner.config)
            value_judge = await classifier.classify(session)
            session["value_judge"] = value_judge
            session["ingested_at"] = _utc_now_iso()
            try:
                session_store = SessionStore.from_config(owner.config)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=503, detail="session storage is not configured") from exc

            if value_judge.get("decision") != "valuable":
                session_store.save_skipped(session)
                logger.info(
                    "[SessionFilter] skipped session=%s decision=%s reason=%s",
                    session_id,
                    value_judge.get("decision"),
                    value_judge.get("reason"),
                )
                return {
                    "status": "skipped",
                    "session_id": session_id,
                    "queued": False,
                    "value_judge": value_judge,
                }

            key = session_store.save_queued(session)
            trigger_scheduled = owner._schedule_evolve_trigger()
            logger.info("[SessionFilter] queued valuable session=%s key=%s", session_id, key)
            return {
                "status": "queued",
                "session_id": session_id,
                "queued": True,
                "key": key,
                "trigger_scheduled": trigger_scheduled,
                "value_judge": value_judge,
            }

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        @app.get("/storage/status")
        async def storage_status():
            return JSONResponse(content=_storage_status(owner.config))

        @app.get("/status")
        async def dashboard_status():
            skills: dict[str, dict[str, Any]] = {}
            session_queue = _session_queue_snapshot(owner.config, limit=0)
            try:
                hub = SkillHub.team_from_config(owner.config)
                for item in hub.list_remote():
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    skills[name] = {
                        "skill_id": item.get("skill_id") or name,
                        "version": item.get("version") or 0,
                    }
            except Exception:
                pass
            if not skills and owner.skill_manager is not None:
                for skill in owner.skill_manager.get_all_skills():
                    name = str(skill.get("name") or "")
                    if name:
                        skills[name] = {"skill_id": name, "version": 0}
            return {
                "running": False,
                "pending_sessions": int(session_queue.get("pending") or 0),
                "registered_skills": len(skills),
                "skills": skills,
            }

        @app.get("/sessions")
        async def dashboard_sessions():
            snapshot = _session_queue_snapshot(owner.config)
            return {
                "reachable": bool(snapshot.get("reachable")),
                "sessions": snapshot.get("sessions", []),
                "pending": int(snapshot.get("pending") or 0),
                **({"reason": snapshot.get("reason")} if snapshot.get("reason") else {}),
            }

        @app.get("/conversations")
        async def dashboard_conversations(limit: int = 100):
            try:
                store = SessionStore.from_config(owner.config)
                conversations = store.list_conversations(limit=max(1, int(limit or 100)))
                return {"reachable": True, "conversations": conversations}
            except Exception as exc:  # noqa: BLE001
                return {"reachable": False, "conversations": [], "reason": str(exc)}

        @app.get("/conversations/{session_id}")
        async def dashboard_conversation_detail(session_id: str):
            try:
                store = SessionStore.from_config(owner.config)
                session = store.load_session(_safe_session_id(session_id))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
            return _session_detail_payload(session)

        @app.get("/conversations/{session_id}/process")
        async def dashboard_conversation_process(session_id: str):
            cycles = _history_from_archived_sessions(
                owner.config,
                limit=50,
                session_id=_safe_session_id(session_id),
            )
            return {"cycles": cycles}

        @app.get("/history")
        async def dashboard_history(limit: int = 50, session_id: str = ""):
            return {
                "cycles": _history_from_archived_sessions(
                    owner.config,
                    limit=max(1, int(limit or 50)),
                    session_id=_safe_session_id(session_id) if session_id else "",
                )
            }

        @app.get("/api/session-filter/audit")
        async def api_session_filter_audit(limit: int = 100, decision: str = ""):
            try:
                store = SessionStore.from_config(owner.config)
                return {
                    "stats": store.filter_stats(),
                    "items": store.list_filter_audit(
                        limit=max(1, int(limit or 100)),
                        decision=decision,
                    ),
                }
            except Exception as exc:  # noqa: BLE001
                return {"stats": {"total": 0, "decisions": {}, "statuses": {}, "modes": {}}, "items": [], "reason": str(exc)}

        @app.get("/validation/candidates")
        async def validation_candidates():
            try:
                store = ValidationStore.from_config(owner.config)
                candidates = store.list_open_jobs(user_alias=str(owner.config.sharing_user_alias or ""))
            except Exception:
                candidates = []
            return {"candidates": candidates}

        @app.post("/internal/reload-skills")
        async def reload_skills(
            request: Request,
        ):
            owner = request.app.state.owner
            await owner._pull_skills_from_cloud()
            skill_count = len(owner.skill_manager.get_all_skills()) if owner.skill_manager else 0
            return {"ok": True, "skills": skill_count}

        return app
