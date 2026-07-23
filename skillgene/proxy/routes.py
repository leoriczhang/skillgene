"""FastAPI application and route wiring for the proxy.

``RoutesMixin`` builds the ``FastAPI`` app and its endpoints (healthz,
reload-skills, flush-sessions, models, chat.completions) plus the
bearer-token auth check. Route bodies delegate to the owning
:class:`~skillgene.proxy.server.ProxyServer` instance.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
from .messages import _rewrite_new_session_bootstrap_prompt
from .session import _resolve_session_done, _resolve_turn_type
from ..config_store import ConfigStore

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


class RoutesMixin:
    """FastAPI app construction, routing, and request authentication."""

    def _build_app(self) -> FastAPI:
        owner = self

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            owner._ready_event.set()
            owner._start_session_idle_sweeper()
            owner._start_skill_reload_polling()
            try:
                yield
            finally:
                owner._ready_event.clear()
                await owner._shutdown_cleanup()

        app = FastAPI(title="SkillGene Proxy", lifespan=lifespan)
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
            prm = data.setdefault("prm", {})
            prm.setdefault("provider", provider)
            prm.setdefault("url", base_url)
            prm.setdefault("model", model)
            store.save(data)
            owner.config = store.to_config()
            owner._served_model = owner.config.served_model_name
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

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.post("/internal/reload-skills")
        async def reload_skills(
            request: Request,
            authorization: Optional[str] = Header(default=None),
        ):
            owner = request.app.state.owner
            await owner._check_auth(authorization)
            await owner._pull_skills_from_cloud()
            skill_count = len(owner.skill_manager.get_all_skills()) if owner.skill_manager else 0
            return {"ok": True, "skills": skill_count}

        @app.post("/internal/flush-sessions")
        async def flush_sessions(
            request: Request,
            authorization: Optional[str] = Header(default=None),
        ):
            """Close + upload active sessions on demand and wait for uploads.

            Clients (e.g. the eval framework) that don't send an explicit
            ``session_done`` never trigger ``_close_session``, so a just-finished
            conversation's turns linger in memory and never reach OpenViking until
            the idle sweeper fires (~180s) or the process shuts down. Post-run
            consumers that read the uploaded ``sessions/*.json`` therefore see
            nothing. This endpoint forces the drain+upload synchronously so a
            follow-up read is guaranteed to observe the session data.

            Optional body ``{"user_aliases": ["team-a-test1", ...]}`` restricts the
            flush to sessions recorded for those client identities (matched against
            ``X-SkillGene-User``); omitting it flushes every active session.
            """
            owner = request.app.state.owner
            await owner._check_auth(authorization)
            try:
                body = await request.json()
            except Exception:
                body = {}
            aliases_raw = (body or {}).get("user_aliases")
            aliases: Optional[set[str]] = None
            if isinstance(aliases_raw, (list, tuple, set)):
                aliases = {str(a).strip() for a in aliases_raw if str(a).strip()}
            flushed = await owner._flush_sessions(aliases)
            return {"ok": True, "flushed": flushed}

        @app.get("/v1/models")
        async def list_models(
            request: Request,
            authorization: Optional[str] = Header(default=None),
        ):
            owner = request.app.state.owner
            await owner._check_auth(authorization)
            model_id = owner._served_model
            return JSONResponse(
                content={
                    "object": "list",
                    "data": [
                        {
                            "id": model_id,
                            "object": "model",
                            "created": 0,
                            # wire constant: owned_by matches the OpenViking data
                            # contract namespace, do not rename
                            "owned_by": "skillgene",
                        }
                    ],
                }
            )

        @app.post("/v1/chat/completions")
        async def chat_completions(
            request: Request,
            authorization: Optional[str] = Header(default=None),
            x_session_id: Optional[str] = Header(default=None),
            x_turn_type: Optional[str] = Header(default=None),
            x_session_done: Optional[str] = Header(default=None),
            # wire constants: X-SkillGene-* request headers are produced by
            # external Hermes clients; the FastAPI param names encode the header
            # names, do not rename
            x_teamskillevolver_user: Optional[str] = Header(default=None),
            x_teamskillevolver_viking_api_key: Optional[str] = Header(default=None),
            x_teamskillevolver_viking_account: Optional[str] = Header(default=None),
            x_teamskillevolver_viking_user: Optional[str] = Header(default=None),
            x_teamskillevolver_viking_agent_id: Optional[str] = Header(default=None),
            x_teamskillevolver_viking_customer_id: Optional[str] = Header(default=None),
            x_teamskillevolver_group_ids: Optional[str] = Header(default=None),
            x_teamskillevolver_root_prefix: Optional[str] = Header(default=None),
        ):
            owner = request.app.state.owner
            # Update idle tracker so the scheduler knows the user is active
            owner._mark_request_activity()
            await owner._check_auth(authorization)

            body = await request.json()
            incoming_messages = body.get("messages", [])
            if isinstance(incoming_messages, list):
                rewritten_messages, _ = _rewrite_new_session_bootstrap_prompt(incoming_messages)
                body["messages"] = rewritten_messages
            session_id = x_session_id or body.get("session_id") or ""
            turn_type = _resolve_turn_type(x_turn_type, body.get("turn_type"), default="main")
            session_done = _resolve_session_done(x_session_done, body.get("session_done"))
            # Do not infer session_done from bootstrap text — only explicit
            # X-Session-Done or body session_done trigger session close.
            owner._record_session_context(
                session_id,
                x_teamskillevolver_user,
                viking_api_key=x_teamskillevolver_viking_api_key,
                viking_account=x_teamskillevolver_viking_account,
                viking_user=x_teamskillevolver_viking_user,
                viking_agent_id=x_teamskillevolver_viking_agent_id,
                viking_customer_id=x_teamskillevolver_viking_customer_id,
                viking_group_id=x_teamskillevolver_group_ids,
                viking_root_prefix=x_teamskillevolver_root_prefix,
            )

            stream = bool(body.get("stream", False))
            result = await owner._handle_request(
                body,
                session_id=session_id,
                turn_type=turn_type,
                session_done=session_done,
            )
            if stream:
                return StreamingResponse(owner._stream_response(result), media_type="text/event-stream")
            return JSONResponse(content=result["response"])

        return app

    async def _check_auth(self, authorization: Optional[str]):
        if not self._expected_api_key:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        if token != self._expected_api_key:
            raise HTTPException(status_code=401, detail="invalid api key")
