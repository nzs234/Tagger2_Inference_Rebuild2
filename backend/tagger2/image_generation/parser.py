"""Bounded response parsing shared by all image providers."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_DATA_URL_RE = re.compile(
    r"data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]{16,})"
)
_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\((?P<url>https?://[^\s)]+)\)")
_BARE_IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>)\]]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s\"'<>)\]]*)?",
    re.IGNORECASE,
)

_MIME_BY_SIGNATURE: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


@dataclass(slots=True)
class ParsedImage:
    data: bytes | None = None
    mime_type: str = "image/png"
    url: str | None = None
    source: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])


@dataclass(slots=True)
class ParseResult:
    images: list[ParsedImage] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    route: str | None = None
    finish_reason: str | None = None
    hint: str | None = None


_FINISH_HINTS = {
    "STOP": "正常结束但未返回图像",
    "MAX_TOKENS": "输出达到 token 上限",
    "SAFETY": "触发安全过滤",
    "IMAGE_SAFETY": "图像触发安全策略",
    "PROHIBITED_CONTENT": "内容被上游禁止",
    "BLOCKLIST": "命中上游屏蔽词",
}


def parse_response(payload: Any, *, max_decoded_bytes: int | None = None) -> ParseResult:
    result = ParseResult()
    if not isinstance(payload, Mapping):
        return result
    result.finish_reason = _extract_finish_reason(payload)
    result.hint = _FINISH_HINTS.get((result.finish_reason or "").upper())
    result.texts = _extract_texts(payload)
    for route, extractor in (
        ("data[].b64_json", _from_data_b64),
        ("data[].url", _from_data_url),
        ("choices[].message.images[]", _from_message_images),
        ("content:data-url", _from_inline_data_url),
        ("content:markdown", _from_markdown_url),
        ("candidates[].parts[].inlineData", _from_native_parts),
        ("content:bare-url", _from_bare_url),
    ):
        images = extractor(payload, max_decoded_bytes)
        if images:
            result.images = images
            result.route = route
            break
    return result


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _decode_b64(blob: Any, max_decoded_bytes: int | None = None) -> bytes | None:
    if not isinstance(blob, str):
        return None
    if max_decoded_bytes is not None and max_decoded_bytes > 0:
        # Reject oversized encoded values before whitespace removal and base64
        # allocation.  The small allowance covers padding and a data URL
        # prefix without changing the decoded byte limit.
        max_encoded = ((int(max_decoded_bytes) + 2) // 3) * 4 + 64
        if len(blob) > max_encoded:
            return None
    value = blob.strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    value = re.sub(r"\s+", "", value)
    if len(value) < 16:
        return None
    try:
        decoded = base64.b64decode(value + "=" * ((-len(value)) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None
    if max_decoded_bytes is not None and len(decoded) > int(max_decoded_bytes):
        return None
    return decoded if decoded else None


def _sniff_mime(data: bytes, declared: Any = None) -> str:
    for signature, mime in _MIME_BY_SIGNATURE:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return str(declared) if isinstance(declared, str) and declared.startswith("image/") else "image/png"


def _from_data_b64(payload: Mapping[str, Any], max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    result: list[ParsedImage] = []
    for item in _as_list(payload.get("data")):
        if not isinstance(item, Mapping):
            continue
        blob = item.get("b64_json") or item.get("b64Json")
        data = _decode_b64(blob, max_decoded_bytes)
        if data:
            result.append(ParsedImage(data=data, mime_type=_sniff_mime(data, item.get("mime_type") or item.get("mimeType")), source="data[].b64_json"))
    return result


def _from_data_url(payload: Mapping[str, Any], max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    result: list[ParsedImage] = []
    for item in _as_list(payload.get("data")):
        if not isinstance(item, Mapping) or not isinstance(item.get("url"), str):
            continue
        value = str(item["url"])
        inline = _from_data_url_string(value, "data[].url", max_decoded_bytes)
        result.append(inline or ParsedImage(url=value, source="data[].url"))
    return result


def _from_message_images(payload: Mapping[str, Any], max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    result: list[ParsedImage] = []
    for choice in _as_list(payload.get("choices")):
        if not isinstance(choice, Mapping) or not isinstance(choice.get("message"), Mapping):
            continue
        for entry in _as_list(choice["message"].get("images")):
            value: Any = entry
            if isinstance(entry, Mapping):
                holder = entry.get("image_url") or entry.get("imageUrl")
                value = holder.get("url") if isinstance(holder, Mapping) else holder
                value = value or entry.get("url") or entry.get("b64_json")
            if not isinstance(value, str) or not value:
                continue
            inline = _from_data_url_string(value, "choices[].message.images[]", max_decoded_bytes)
            if inline:
                result.append(inline)
            elif value.startswith("http"):
                result.append(ParsedImage(url=value, source="choices[].message.images[]"))
            else:
                data = _decode_b64(value, max_decoded_bytes)
                if data:
                    result.append(ParsedImage(data=data, mime_type=_sniff_mime(data), source="choices[].message.images[]"))
    return result


def _iter_content_strings(payload: Mapping[str, Any]) -> Iterable[str]:
    for choice in _as_list(payload.get("choices")):
        if not isinstance(choice, Mapping):
            continue
        for holder_key in ("message", "delta"):
            holder = choice.get(holder_key)
            if not isinstance(holder, Mapping):
                continue
            content = holder.get("content")
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        yield part
                    elif isinstance(part, Mapping):
                        for key in ("text", "url"):
                            if isinstance(part.get(key), str):
                                yield str(part[key])
                        holder_url = part.get("image_url") or part.get("imageUrl")
                        if isinstance(holder_url, Mapping) and isinstance(holder_url.get("url"), str):
                            yield str(holder_url["url"])
        if isinstance(choice.get("text"), str):
            yield str(choice["text"])


def _from_inline_data_url(payload: Mapping[str, Any], max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    result: list[ParsedImage] = []
    for text in _iter_content_strings(payload):
        for match in _DATA_URL_RE.finditer(text):
            data = _decode_b64(match.group("data"), max_decoded_bytes)
            if data:
                result.append(ParsedImage(data=data, mime_type=_sniff_mime(data, match.group("mime")), source="content:data-url"))
    return result


def _from_markdown_url(payload: Mapping[str, Any], _max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    return [ParsedImage(url=match.group("url"), source="content:markdown") for text in _iter_content_strings(payload) for match in _MARKDOWN_IMG_RE.finditer(text)]


def _from_bare_url(payload: Mapping[str, Any], _max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    return [ParsedImage(url=match.group(0), source="content:bare-url") for text in _iter_content_strings(payload) for match in _BARE_IMAGE_URL_RE.finditer(text)]


def _from_native_parts(payload: Mapping[str, Any], max_decoded_bytes: int | None = None) -> list[ParsedImage]:
    result: list[ParsedImage] = []
    for candidate in _as_list(payload.get("candidates")):
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("content"), Mapping):
            continue
        for part in _as_list(candidate["content"].get("parts")):
            if not isinstance(part, Mapping):
                continue
            blob = part.get("inlineData") or part.get("inline_data")
            if not isinstance(blob, Mapping):
                continue
            data = _decode_b64(blob.get("data"), max_decoded_bytes)
            if data:
                result.append(ParsedImage(data=data, mime_type=_sniff_mime(data, blob.get("mimeType") or blob.get("mime_type")), source="candidates[].parts[].inlineData"))
    return result


def _from_data_url_string(value: str, source: str, max_decoded_bytes: int | None = None) -> ParsedImage | None:
    match = _DATA_URL_RE.search(value)
    if not match:
        return None
    data = _decode_b64(match.group("data"), max_decoded_bytes)
    return ParsedImage(data=data, mime_type=_sniff_mime(data, match.group("mime")), source=source) if data else None


def _extract_finish_reason(payload: Mapping[str, Any]) -> str | None:
    for choice in _as_list(payload.get("choices")):
        if isinstance(choice, Mapping) and isinstance(choice.get("finish_reason") or choice.get("finishReason"), str):
            return str(choice.get("finish_reason") or choice.get("finishReason"))
    for candidate in _as_list(payload.get("candidates")):
        if isinstance(candidate, Mapping) and isinstance(candidate.get("finishReason") or candidate.get("finish_reason"), str):
            return str(candidate.get("finishReason") or candidate.get("finish_reason"))
    feedback = payload.get("promptFeedback") or payload.get("prompt_feedback")
    if isinstance(feedback, Mapping) and isinstance(feedback.get("blockReason") or feedback.get("block_reason"), str):
        return str(feedback.get("blockReason") or feedback.get("block_reason"))
    return None


def _extract_texts(payload: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for text in _iter_content_strings(payload):
        cleaned = _DATA_URL_RE.sub("[inline image]", text).strip()
        if cleaned:
            values.append(cleaned)
    for candidate in _as_list(payload.get("candidates")):
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("content"), Mapping):
            continue
        for part in _as_list(candidate["content"].get("parts")):
            if isinstance(part, Mapping) and isinstance(part.get("text"), str) and part["text"].strip():
                values.append(part["text"].strip())
    return list(dict.fromkeys(values))


def truncate_debug(value: Any, limit: int = 1200) -> Any:
    text = str(value).replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


__all__ = ["ParsedImage", "ParseResult", "parse_response", "truncate_debug"]
