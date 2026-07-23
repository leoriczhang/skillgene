"""Upstream LLM forwarding and streaming.

``ForwardingMixin`` forwards a normalized OpenAI-style body to the
configured upstream (OpenAI-compatible or OpenRouter), with retry/backoff,
temperature-drop and stream-required fallbacks, and re-serializes the
response as SSE chunks for streaming clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from typing import Any

from fastapi import HTTPException

from .messages import _assemble_streaming_chat_completion, _collect_sse_chat_events

logger = logging.getLogger(__name__)


def _llm_request_timeout_seconds() -> float:
    # wire constant: env var name is a deployment contract, do not rename
    raw = str(os.environ.get("TEAM_SKILL_EVOLVER_LLM_REQUEST_TIMEOUT_S", "600")).strip()
    try:
        timeout = float(raw)
    except ValueError:
        return 600.0
    return timeout if timeout > 0 else 600.0


class ForwardingMixin:
    """Forward requests to the upstream LLM and stream responses back."""

    async def _forward_to_llm(self, body: dict[str, Any]) -> dict[str, Any]:
        """Forward to a real LLM API.

        Supports OpenAI-compatible providers:
          - ``"openai"`` (default) — any OpenAI-compatible ``/v1/chat/completions`` endpoint.
          - ``"openrouter"`` — OpenRouter gateway (OpenAI-compatible + routing extensions).
        """
        return await self._forward_to_llm_openai(body)

    async def _forward_to_llm_openai(self, body: dict[str, Any]) -> dict[str, Any]:
        """Forward to an OpenAI-compatible API."""
        import httpx

        api_base = self.config.llm_api_base.rstrip("/")
        if not api_base:
            raise HTTPException(
                status_code=503,
                detail="llm_api_base is not configured. Set it via 'skillgene config' first.",
            )

        # Strip Tinker-specific fields not supported by standard OpenAI APIs
        send_body = {k: v for k, v in body.items() if k not in {"logprobs", "top_logprobs", "stream_options"}}
        send_body["model"] = self.config.llm_model_id or body.get("model", "")
        send_body["stream"] = False

        headers: dict[str, str] = {}
        if self.config.llm_api_key:
            headers["Authorization"] = f"Bearer {self.config.llm_api_key}"

        # OpenRouter-specific headers and body extensions
        if self.config.llm_provider == "openrouter":
            if self.config.openrouter_app_name:
                headers["X-Title"] = self.config.openrouter_app_name
            if self.config.openrouter_app_url:
                headers["HTTP-Referer"] = self.config.openrouter_app_url
            # Routing strategy
            route = self.config.openrouter_route
            if route and route != "fallback":
                send_body["provider"] = {"sort": route}
            # Fallback model list
            fallback = self.config.openrouter_fallback_models
            if fallback:
                models = [m.strip() for m in fallback.split(",") if m.strip()]
                if models:
                    send_body["models"] = [send_body.get("model", "")] + models
            # Data collection policy
            if self.config.openrouter_data_policy == "deny":
                send_body.setdefault("provider", {})
                send_body["provider"]["data_collection"] = "deny"

        max_retries = 6
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=_llm_request_timeout_seconds()) as client:
                    resp = await client.post(
                        f"{api_base}/chat/completions",
                        json=send_body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as e:
                response_text = e.response.text[:200]
                if e.response.status_code == 400 and "'temperature' is not supported" in e.response.text:
                    logger.info("[Proxy] upstream rejects temperature param, retrying without it")
                    send_body.pop("temperature", None)
                    continue
                if e.response.status_code == 400 and "Stream must be set to true" in e.response.text:
                    logger.info("[Proxy] upstream requires stream=true, retrying with SSE collection")
                    stream_body = dict(send_body)
                    stream_body["stream"] = True
                    try:
                        async with httpx.AsyncClient(timeout=_llm_request_timeout_seconds()) as client:
                            async with client.stream(
                                "POST",
                                f"{api_base}/chat/completions",
                                json=stream_body,
                                headers=headers,
                            ) as stream_resp:
                                stream_resp.raise_for_status()
                                events = await _collect_sse_chat_events(stream_resp)
                        return _assemble_streaming_chat_completion(
                            events,
                            fallback_model=send_body.get("model", ""),
                        )
                    except httpx.HTTPStatusError as stream_error:
                        logger.error(
                            "[Proxy] upstream SSE retry error: %s %s",
                            stream_error.response.status_code,
                            stream_error.response.text[:200],
                        )
                        raise HTTPException(
                            status_code=502,
                            detail=f"Upstream LLM SSE retry error: {stream_error}",
                        ) from stream_error
                    except Exception as stream_error:
                        logger.error("[Proxy] upstream SSE retry failed: %s", stream_error, exc_info=True)
                        raise HTTPException(
                            status_code=502,
                            detail=f"Upstream LLM SSE retry failed: {stream_error}",
                        ) from stream_error
                # Retryable upstream error — retry if attempts remain
                if attempt < max_retries - 1:
                    wait = min(2**attempt + random.uniform(0, 1), 30)
                    logger.warning(
                        "[Proxy] upstream LLM error (attempt %d/%d), retrying in %.1fs: %s %s",
                        attempt + 1,
                        max_retries,
                        wait,
                        e.response.status_code,
                        response_text,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error("[Proxy] upstream LLM error: %s %s", e.response.status_code, response_text)
                raise HTTPException(status_code=502, detail=f"Upstream LLM error: {e}") from e
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = min(2**attempt + random.uniform(0, 1), 30)
                    logger.warning(
                        "[Proxy] LLM forward failed (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1,
                        max_retries,
                        wait,
                        e,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error("[Proxy] LLM forward failed: %s", e, exc_info=True)
                raise HTTPException(status_code=502, detail=f"LLM forward error: {e}") from e

    async def _stream_response(self, result: dict[str, Any]):
        payload = result["response"]
        choice = payload.get("choices", [{}])[0]
        message = choice.get("message", {})
        delta = {"role": "assistant", "content": message.get("content", "") or ""}
        if message.get("tool_calls"):
            delta["tool_calls"] = message["tool_calls"]
        chunk_base = {
            "id": payload.get("id", ""),
            "object": "chat.completion.chunk",
            "created": payload.get("created", int(time.time())),
            "model": payload.get("model", ""),
            "session_id": payload.get("session_id", ""),
        }
        first = {**chunk_base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        final = {
            **chunk_base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}],
        }
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        # Forward upstream token usage so streaming clients (e.g. Hermes) can
        # accumulate it. OpenAI's include_usage convention delivers usage in a
        # trailing chunk with an empty choices array.
        usage = payload.get("usage")
        if isinstance(usage, dict) and usage:
            usage_chunk = {**chunk_base, "choices": [], "usage": usage}
            yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
