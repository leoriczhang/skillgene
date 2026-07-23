"""Message and protocol normalization for the proxy pipeline.

Pure functions that reshape client/upstream messages into a chat-template
compatible form: content flattening, Kimi/Qwen tool-call extraction,
reasoning backfill, streaming-chunk assembly, tool-result summarization,
and bootstrap-prompt rewriting.
"""

from __future__ import annotations

import json
import re
from typing import Any

_NON_STANDARD_BODY_KEYS = {
    "session_id",
    "session_done",
    "turn_type",
}

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_TOOL_HANDLE_RE = re.compile(r"^call_(?:kimi|xml)_\d+$")
_KIMI_TOOL_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*([a-zA-Z0-9_.-]+)(?::\d+)?\s*"
    r"<\|tool_call_argument_begin\|>\s*(\{.*?\})\s*"
    r"<\|tool_call_end\|>",
    re.DOTALL,
)
_QWEN_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_TOOL_ARGS_MAX_CHARS = 4_000
_TOOL_RESULT_CONTENT_MAX_CHARS = 4_000


def _flatten_message_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        return " ".join(parts) if parts else ""
    return str(content) if content is not None else ""


def _normalize_assistant_content_parts(content: list[dict]) -> tuple[str, list[dict]]:
    """Extract plain text and OpenAI-style tool_calls from assistant content parts."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for i, item in enumerate(content):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif item_type == "toolCall":
            name = item.get("name")
            args = item.get("arguments", {})
            if not isinstance(args, str):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = "{}"
            tc_id = item.get("id") or f"call_{i}"
            tool_calls.append(
                {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": name or "unknown_tool",
                        "arguments": args,
                    },
                }
            )
    return (" ".join(text_parts).strip(), tool_calls)


def _normalize_tool_name(raw_name: str, args_raw: str) -> str:
    """
    Normalize tool names from model output.
    Fixes common drift where a call handle (e.g. call_kimi_0) is emitted as
    function name instead of the actual tool name.
    """
    name = (raw_name or "").strip()
    if name.startswith("functions."):
        name = name.split(".", 1)[1]
    if not _TOOL_HANDLE_RE.fullmatch(name):
        return name or "unknown_tool"

    try:
        args_obj = json.loads(args_raw or "{}")
    except Exception:
        args_obj = {}
    if isinstance(args_obj, dict):
        if isinstance(args_obj.get("command"), str) and args_obj.get("command"):
            return "exec"
        if isinstance(args_obj.get("sessionId"), str) and args_obj.get("sessionId"):
            return "process"
    return "unknown_tool"


def _normalize_tool_call_name(raw_name: str) -> str:
    """Strip transport-specific prefixes from a tool name."""
    name = str(raw_name or "").strip()
    if name.startswith("functions."):
        return name.split(".", 1)[1]
    return name


def _extract_tool_calls_from_text(text: str) -> tuple[str, list[dict]]:
    """
    Parse tool-call tags embedded in assistant text into OpenAI-style tool_calls.
    Supports Kimi markers and Qwen <tool_call> wrappers.
    """
    if not text:
        return "", []

    tool_calls: list[dict] = []

    for i, m in enumerate(_KIMI_TOOL_CALL_RE.finditer(text)):
        raw_name = (m.group(1) or "").strip()
        args_raw = (m.group(2) or "{}").strip()
        tool_name = _normalize_tool_name(raw_name, args_raw)
        try:
            args_obj = json.loads(args_raw)
            args_str = json.dumps(args_obj, ensure_ascii=False)
        except Exception:
            args_str = args_raw if args_raw else "{}"
        tool_calls.append(
            {
                "id": f"call_kimi_{i}",
                "type": "function",
                "function": {"name": tool_name or "unknown_tool", "arguments": args_str},
            }
        )

    for i, m in enumerate(_QWEN_TOOL_CALL_RE.finditer(text), start=len(tool_calls)):
        payload_raw = (m.group(1) or "").strip()
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        name = (
            payload.get("name") or payload.get("tool_name") or payload.get("function", {}).get("name") or "unknown_tool"
        )
        args = payload.get("arguments") or payload.get("function", {}).get("arguments") or {}
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except Exception:
                args = "{}"
        name = _normalize_tool_name(str(name), args)
        tool_calls.append(
            {
                "id": f"call_xml_{i}",
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        )

    clean = text
    clean = _THINK_RE.sub("", clean)
    clean = clean.replace("</think>", "")
    # Keep tool call data only in structured field; strip markup from plain text.
    clean = re.sub(r"<\|tool_call_begin\|>.*?<\|tool_call_end\|>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>", "", clean, flags=re.DOTALL)
    clean = _QWEN_TOOL_CALL_RE.sub("", clean)
    clean = clean.strip()
    return clean, tool_calls


def _assistant_message_has_tool_calls(message: dict[str, Any]) -> bool:
    raw_tool_calls = message.get("tool_calls")
    if isinstance(raw_tool_calls, list) and raw_tool_calls:
        return True

    raw_content = message.get("content")
    if isinstance(raw_content, list):
        _, part_tool_calls = _normalize_assistant_content_parts(raw_content)
        return bool(part_tool_calls)
    if isinstance(raw_content, str) and raw_content:
        _, text_tool_calls = _extract_tool_calls_from_text(raw_content)
        return bool(text_tool_calls)
    return False


def _restore_missing_reasoning_content(
    messages: list[dict[str, Any]],
    prior_turns: list[dict[str, Any]],
) -> int:
    """Backfill reasoning_content for prior assistant tool-call messages."""
    assistant_tool_indices = [
        idx
        for idx, msg in enumerate(messages)
        if isinstance(msg, dict) and msg.get("role") == "assistant" and _assistant_message_has_tool_calls(msg)
    ]
    prior_tool_turns = [turn for turn in prior_turns if isinstance(turn, dict) and turn.get("tool_calls")]
    if not assistant_tool_indices or not prior_tool_turns:
        return 0

    restored = 0
    for msg_idx, turn in zip(reversed(assistant_tool_indices), reversed(prior_tool_turns)):
        msg = messages[msg_idx]
        if msg.get("reasoning_content"):
            continue
        reasoning = str(turn.get("reasoning_content") or "").strip()
        if not reasoning:
            continue
        messages[msg_idx] = {**msg, "reasoning_content": reasoning}
        restored += 1
    return restored


def _deduplicate_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Deduplicate tool calls while preserving order.

    Priority key is tool-call id. When id is missing, fallback to
    (function.name, function.arguments).
    """
    deduped: list[dict] = []
    seen: set[str] = set()
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tc_id = str(tc.get("id") or "").strip()
        func = tc.get("function") or {}
        fn_name = str(func.get("name") or "")
        fn_args = str(func.get("arguments") or "")
        key = f"id:{tc_id}" if tc_id else f"fn:{fn_name}|args:{fn_args}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tc)
    return deduped


def _normalize_messages_for_template(messages: list[dict]) -> list[dict]:
    """Normalize messages into chat-template-compatible format."""
    out = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")

        if role == "developer":
            m["role"] = "system"
            role = "system"

        if role == "toolResult":
            tool_msg: dict[str, Any] = {
                "role": "tool",
                "content": _flatten_message_content(m.get("content")),
            }
            tc_id = m.get("toolCallId") or m.get("tool_call_id")
            if tc_id:
                tool_msg["tool_call_id"] = tc_id
            tool_name = m.get("toolName") or m.get("name")
            if tool_name:
                tool_msg["name"] = tool_name
            out.append(tool_msg)
            continue

        # assistant content parts may contain text + toolCall blocks
        raw = m.get("content")
        if role == "assistant" and isinstance(raw, list):
            text, tool_calls = _normalize_assistant_content_parts(raw)
            m["content"] = text
            if tool_calls:
                m["tool_calls"] = tool_calls
        elif not isinstance(raw, str) and raw is not None:
            m["content"] = _flatten_message_content(raw)

        out.append(m)
    return out


def _extract_last_user_instruction(messages: list[dict]) -> str:
    """Return the most recent user message text from the current turn context."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text = _flatten_message_content(msg.get("content"))
            if text:
                return text
    return ""


_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"exited with code (?!0\b)\d+|exit code (?!0\b)\d+|exit status (?!0\b)\d+",
            re.IGNORECASE,
        ),
        "exit_code",
    ),
    (re.compile(r"Traceback \(most recent call last\)|\.py\", line \d+", re.IGNORECASE), "traceback"),
    (re.compile(r"Permission denied|EACCES|PermissionError", re.IGNORECASE), "permission"),
    (re.compile(r"No such file|FileNotFoundError|ENOENT|not found", re.IGNORECASE), "not_found"),
    (re.compile(r"command not found|not recognized as|is not recognized", re.IGNORECASE), "command_not_found"),
    (re.compile(r"timed?\s*out|TimeoutError|ETIMEDOUT", re.IGNORECASE), "timeout"),
    (re.compile(r"(?:^|\W)(?:Error|Exception):\s", re.MULTILINE), "generic_error"),
]


def _classify_tool_error(content: str) -> tuple[bool, str | None]:
    """Return (has_error, error_type) by matching content against known patterns."""
    for pattern, error_type in _ERROR_PATTERNS:
        if pattern.search(content):
            return True, error_type
    return False, None


def _extract_recent_tool_results(messages: list[dict]) -> list[dict]:
    """Extract tool results from the most recent tool-call round in messages.

    Scans backwards from the end of *messages*, collecting all consecutive
    tool / toolResult messages that appear after the last assistant message.
    Returns a list of summary dicts suitable for skill feedback tracking.
    """
    results: list[dict] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role in ("toolResult", "tool"):
            content = _flatten_message_content(msg.get("content"))
            tool_name = msg.get("toolName") or msg.get("name") or msg.get("tool_name") or "unknown"
            has_error, error_type = _classify_tool_error(content)
            results.append(
                {
                    "tool_name": tool_name,
                    "tool_call_id": (msg.get("toolCallId") or msg.get("tool_call_id") or ""),
                    "content": content[:_TOOL_RESULT_CONTENT_MAX_CHARS],
                    "has_error": has_error,
                    "error_type": error_type,
                }
            )
        elif role == "user":
            continue
        else:
            break
    results.reverse()
    return results


def _extract_recent_tool_result_messages(messages: list[dict]) -> list[dict]:
    """Extract raw tool result messages from the most recent tool round.

    This preserves the original payload shape so cloud sessions can retain a
    complete tool-execution snapshot for future analysis. No truncation or
    error classification is applied here.
    """
    results: list[dict] = []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role in ("toolResult", "tool"):
            try:
                results.append(json.loads(json.dumps(msg, ensure_ascii=False)))
            except Exception:
                results.append(dict(msg))
        elif role == "user":
            continue
        else:
            break
    results.reverse()
    return results


def _assemble_streaming_chat_completion(
    events: list[dict[str, Any]],
    *,
    fallback_model: str,
) -> dict[str, Any]:
    """Collapse OpenAI-style SSE chat chunks into a single response dict."""
    import time

    builders: dict[int, dict[str, Any]] = {}
    response_id = ""
    response_model = fallback_model
    response_created = int(time.time())
    usage: dict[str, Any] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        response_id = str(event.get("id") or response_id)
        response_model = str(event.get("model") or response_model)
        created = event.get("created")
        if isinstance(created, int):
            response_created = created
        if isinstance(event.get("usage"), dict):
            usage = dict(event["usage"])

        for choice in event.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            index = int(choice.get("index", 0))
            entry = builders.setdefault(
                index,
                {
                    "role": "assistant",
                    "content_parts": [],
                    "tool_calls": {},
                    "finish_reason": None,
                },
            )
            delta = choice.get("delta") or {}
            if isinstance(delta.get("role"), str):
                entry["role"] = delta["role"]

            content = delta.get("content")
            if isinstance(content, str):
                entry["content_parts"].append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        entry["content_parts"].append(item["text"])

            for tc in delta.get("tool_calls", []) or []:
                if not isinstance(tc, dict):
                    continue
                tc_index = int(tc.get("index", 0))
                tool_entry = entry["tool_calls"].setdefault(
                    tc_index,
                    {
                        "id": tc.get("id") or f"call_{tc_index}",
                        "type": tc.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tc.get("id"):
                    tool_entry["id"] = tc["id"]
                if tc.get("type"):
                    tool_entry["type"] = tc["type"]
                fn = tc.get("function") or {}
                if isinstance(fn.get("name"), str):
                    tool_entry["function"]["name"] += fn["name"]
                if isinstance(fn.get("arguments"), str):
                    tool_entry["function"]["arguments"] += fn["arguments"]

            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                entry["finish_reason"] = finish_reason

    choices: list[dict[str, Any]] = []
    for index in sorted(builders):
        entry = builders[index]
        message: dict[str, Any] = {
            "role": entry["role"],
            "content": "".join(entry["content_parts"]),
        }
        if entry["tool_calls"]:
            message["tool_calls"] = [entry["tool_calls"][i] for i in sorted(entry["tool_calls"])]
        choices.append(
            {
                "index": index,
                "message": message,
                "finish_reason": entry["finish_reason"] or "stop",
            }
        )

    return {
        "id": response_id or f"chatcmpl-stream-{response_created}",
        "object": "chat.completion",
        "created": response_created,
        "model": response_model,
        "choices": choices
        or [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


async def _collect_sse_chat_events(response) -> list[dict[str, Any]]:
    """Read SSE `data:` lines from a streaming chat completion response."""
    events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _build_tool_summaries(tool_calls: list[dict]) -> list[dict]:
    """Build tool summary dicts from the model's tool_calls.

    Extracts the tool name and key arguments (``command`` for shell-like
    tools, ``path`` for file-based tools) into a compact format for the
    turn record.  ``has_error`` defaults to ``False`` and is merged later
    when actual tool results arrive.
    """
    from .attribution import _SHELL_TOOL_NAMES, _extract_skill_paths_from_tool_call

    summaries: list[dict] = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = _normalize_tool_call_name(func.get("name", "unknown"))
        args_raw = func.get("arguments", "{}")
        if not isinstance(args_raw, str):
            try:
                args_raw = json.dumps(args_raw, ensure_ascii=False)
            except Exception:
                args_raw = "{}"
        try:
            args = json.loads(args_raw)
        except Exception:
            args = {}
        _, skill_paths = _extract_skill_paths_from_tool_call(tc)

        summary: dict[str, Any] = {
            "tool_name": name,
            "tool_call_id": str(tc.get("id") or ""),
            "arguments": args_raw[:_TOOL_ARGS_MAX_CHARS],
            "has_error": False,
        }

        if name.lower() in _SHELL_TOOL_NAMES:
            cmd = str(args.get("command") or args.get("cmd") or "")
            if cmd:
                summary["command"] = cmd[:_TOOL_ARGS_MAX_CHARS]

        path = str(args.get("path") or args.get("file") or args.get("file_path") or "")
        if path:
            summary["path"] = path
        elif skill_paths:
            summary["path"] = skill_paths[0]

        summaries.append(summary)
    return summaries


def _merge_tool_error_info(
    turn_record: dict,
    tool_results: list[dict],
    raw_tool_results: list[dict] | None = None,
) -> None:
    """Merge error information from tool results into the turn record.

    Matches tool results to the ``tool_results`` summaries built from tool
    calls (by position).  Updates ``has_error``, ``error_type``, and
    ``content`` on matching entries, then rebuilds ``tool_errors``.
    ``raw_tool_results`` preserves the original tool payloads for cloud upload.
    """
    from .attribution import _drop_failed_hermes_skill_writes

    summaries = turn_record.get("tool_results", [])
    observations: list[dict] = []

    if raw_tool_results is not None:
        raw_snapshot: list[dict] = []
        for item in raw_tool_results:
            if not isinstance(item, dict):
                continue
            try:
                raw_snapshot.append(json.loads(json.dumps(item, ensure_ascii=False)))
            except Exception:
                raw_snapshot.append(dict(item))
        turn_record["tool_results_raw"] = raw_snapshot
    else:
        turn_record.setdefault("tool_results_raw", [])

    for i, result in enumerate(tool_results):
        obs: dict[str, Any] = {
            "tool_name": result.get("tool_name", "unknown"),
            "tool_call_id": result.get("tool_call_id", ""),
            "has_error": bool(result.get("has_error", False)),
        }
        if result.get("error_type"):
            obs["error_type"] = result["error_type"]
        content = result.get("content", "")
        if content:
            obs["content"] = str(content)[:_TOOL_RESULT_CONTENT_MAX_CHARS]
        observations.append(obs)

        if i < len(summaries):
            summaries[i]["has_error"] = bool(result.get("has_error", False))
            summaries[i]["tool_name"] = result.get("tool_name", summaries[i].get("tool_name", "unknown"))
            if result.get("tool_call_id"):
                summaries[i]["tool_call_id"] = result["tool_call_id"]
            if result.get("error_type"):
                summaries[i]["error_type"] = result["error_type"]
            else:
                summaries[i].pop("error_type", None)
            content = result.get("content", "")
            if content:
                summaries[i]["content"] = str(content)[:_TOOL_RESULT_CONTENT_MAX_CHARS]
            else:
                summaries[i].pop("content", None)
        else:
            entry: dict[str, Any] = {
                "tool_name": result.get("tool_name", "unknown"),
                "tool_call_id": result.get("tool_call_id", ""),
                "has_error": bool(result.get("has_error", False)),
            }
            if result.get("error_type"):
                entry["error_type"] = result["error_type"]
            content = result.get("content", "")
            if content:
                entry["content"] = str(content)[:_TOOL_RESULT_CONTENT_MAX_CHARS]
            summaries.append(entry)

    turn_record["tool_observations"] = observations
    turn_record["tool_errors"] = [
        {
            "tool_name": s.get("tool_name", "unknown"),
            **({"tool_call_id": s["tool_call_id"]} if s.get("tool_call_id") else {}),
            **({"error_type": s["error_type"]} if s.get("error_type") else {}),
            **({"content": s["content"]} if s.get("content") else {}),
        }
        for s in summaries
        if s.get("has_error")
    ]
    _drop_failed_hermes_skill_writes(turn_record, summaries)


def _rewrite_new_session_bootstrap_prompt(messages: list[dict]) -> tuple[list[dict], int]:
    """Rewrite /new bootstrap user prompt to a safer variant.

    Some upstream providers over-trigger policy filters on the stock bootstrap
    text ("A new session was started via /new or /reset ..."). This keeps
    behavior while avoiding brittle phrasing.
    """
    rewritten = 0
    out: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        if msg.get("role") != "user":
            out.append(msg)
            continue
        text = _flatten_message_content(msg.get("content"))
        lowered = text.lower()
        if "a new session was started via /new or /reset" in lowered:
            out.append(
                {
                    **msg,
                    "content": (
                        "A new chat session just started. "
                        "Greet the user briefly in 1-3 sentences and ask what they want to do."
                    ),
                }
            )
            rewritten += 1
            continue
        out.append(msg)
    return out, rewritten
