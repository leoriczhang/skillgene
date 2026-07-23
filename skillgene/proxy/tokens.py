"""Dependency-free token estimation for proxied request bodies.

The proxy forwards external agents and does not own the upstream model's
exact tokenization. These helpers keep the estimate local and
dependency-free so daemon readiness never hinges on model-specific
tokenization.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

_IMAGE_TOKEN_ESTIMATE = 1600


def _data_url_bytes(url: str) -> bytes | None:
    if not url.startswith("data:") or "," not in url:
        return None
    header, data = url.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        return base64.b64decode(data, validate=False)
    except Exception:
        return None


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width > 0 and height > 0 else None
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        if len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return (width, height) if width > 0 and height > 0 else None
        return None
    if data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return (width, height) if width > 0 and height > 0 else None
        if data[12:16] == b"VP8 " and len(data) >= 30:
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return (width, height) if width > 0 and height > 0 else None
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                return None
            segment_length = struct.unpack(">H", data[index : index + 2])[0]
            if segment_length < 2 or index + segment_length > len(data):
                return None
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if segment_length >= 7:
                    height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                    return (width, height) if width > 0 and height > 0 else None
                return None
            index += segment_length
    return None


def _image_token_estimate_from_url(url: str) -> int:
    data = _data_url_bytes(url)
    if data is None:
        return _IMAGE_TOKEN_ESTIMATE
    dimensions = _image_dimensions_from_bytes(data)
    if dimensions is None:
        return _IMAGE_TOKEN_ESTIMATE
    width, height = dimensions
    return max(_IMAGE_TOKEN_ESTIMATE, (width * height + 749) // 750)


def _image_token_estimate_from_part(content: dict[str, Any]) -> int:
    image_url = content.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        source = content.get("source") if isinstance(content.get("source"), dict) else {}
        if source.get("type") == "base64":
            media_type = str(source.get("media_type") or "image/png")
            data = str(source.get("data") or "")
            url = f"data:{media_type};base64,{data}" if data else ""
        else:
            url = str(content.get("url") or "")
    if not url:
        return _IMAGE_TOKEN_ESTIMATE
    return _image_token_estimate_from_url(url)


def _estimate_image_content_tokens(content: Any) -> int:
    if isinstance(content, list):
        return sum(_estimate_image_content_tokens(item) for item in content)
    if isinstance(content, dict):
        item_type = content.get("type")
        count = _image_token_estimate_from_part(content) if item_type in {"image", "image_url", "input_image"} else 0
        if "content" in content:
            count += _estimate_image_content_tokens(content.get("content"))
        return count
    return 0


def _token_estimate_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item_type in {"image", "image_url"}:
                parts.append("[image]")
            elif "content" in item:
                parts.append(_token_estimate_text(item.get("content")))
        return " ".join(part for part in parts if part)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content) if content is not None else ""


def _estimate_openai_body_input_tokens(openai_body: dict[str, Any]) -> int:
    """Return a provider-agnostic rough input token estimate.

    The proxy sits in front of external agents and does not own the upstream
    model's exact tokenization. Keep this estimate local and dependency-free
    so daemon readiness never depends on model-specific tokenization.
    """
    messages = list(openai_body.get("messages") or [])
    tools = openai_body.get("tools")
    image_tokens = sum(_estimate_image_content_tokens(msg.get("content")) for msg in messages if isinstance(msg, dict))
    text_parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text_parts.append(f"{msg.get('role', '')}: {_token_estimate_text(msg.get('content'))}")
        if msg.get("tool_calls"):
            text_parts.append(json.dumps(msg.get("tool_calls"), ensure_ascii=False, sort_keys=True))
    if tools:
        text_parts.append(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    text = "\n".join(part for part in text_parts if part)
    return max(1, (len(text) + 3) // 4 + image_tokens)
