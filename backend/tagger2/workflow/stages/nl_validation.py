# Ported verbatim from the e621-standard-caption-workflow project
# (workers/nl/src/anima_nl_worker/validation.py). Strict NL response
# validation: refusal detection, wrapper stripping and the structured
# count/layout observation contract are security relevant, so the rules are
# kept byte-identical rather than paraphrased.
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit


MAX_NL_BYTES = 16_384
MAX_RESPONSE_BODY_BYTES = 1_048_576
REFUSAL_SNIPPETS = (
    "no image was provided", "i cannot analyze images", "i can't analyze images",
    "i cannot assist with that", "i can't assist with that", "i cannot help with that",
    "i can't help with that", "i cannot generate", "i can't generate",
    "i am unable to help", "i'm unable to help", "as an ai language model",
    "content policy", "content_policy", "request was blocked", "request has been blocked",
    "request was rejected", "moderation", "policy violation",
)
# F29: third-party proxies answer HTTP 200 with localized or custom moderation text.
REFUSAL_PATTERN = re.compile("|".join((
    r"无法(?:分析|识别|处理|查看|描述|生成|提供|回答|完成)",
    r"不能(?:分析|识别|处理|查看|描述|生成|提供|回答|协助|帮助)",
    r"(?:抱歉|对不起|很遗憾)[^\n]{0,8}?(?:无法|不能|不便|不予)",
    r"(?:内容|安全|合规|风控)?(?:审核|审查)(?:未通过|不通过|失败|拦截)",
    r"(?:命中|触发)(?:了)?(?:敏感|违规|风控|安全)",
    r"违反(?:了)?[^\n]{0,8}?(?:政策|规定|规范|条款|法律法规)",
    r"(?:已)?被(?:拦截|屏蔽|拒绝)",
    r"敏感(?:词|内容|信息)",
    r"请求(?:被)?(?:拒绝|拦截|屏蔽)",
)))
TRAILING_CONNECTORS = frozenset({"and", "or", "with", "of", "the", "a", "an", "to", "for", "in", "on", "at", "by", "from"})
OBSERVATION_COUNTS = frozenset({"solo", "duo", "trio", "group", "unknown"})
OBSERVATION_LAYOUTS = frozenset({"single_scene", "multi_view", "character_sheet", "multi_panel", "unknown"})
# F25: weak prompts return the caption wrapped in code fences, labels, quotes or emphasis.
CODE_FENCE = re.compile(r"^```[^\n`]*\n(?P<body>.*?)\n?```$", re.DOTALL)
LABEL_PREFIX = re.compile(r"^[*_#>\s]*(?:natural language caption|caption|description|output|answer|result|nl)[*_\s]*[:：][*_]*\s*", re.IGNORECASE)
WRAPPERS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("**", "**"), ("*", "*"), ("`", "`"))


class NlValidationError(ValueError):
    pass


def normalize_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2_048 or any(c.isspace() for c in value):
        raise NlValidationError("API endpoint is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise NlValidationError("API endpoint is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise NlValidationError("API endpoint must be an absolute URL without credentials, query, or fragment")
    hostname = parsed.hostname
    if hostname is None:
        raise NlValidationError("API endpoint hostname is invalid")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: list[str] = []
        for part in value:
            if not isinstance(part, dict) or set(part) - {"type", "text"} or part.get("type") != "text" or not isinstance(part.get("text"), str):
                raise NlValidationError("API content array must contain text items only")
            pieces.append(part["text"])
        return "".join(pieces)
    raise NlValidationError("API content is not text")


def _completion_parts(body: bytes) -> tuple[str, str | None, dict[str, int]]:
    if len(body) > MAX_RESPONSE_BODY_BYTES:
        raise NlValidationError("API response exceeds 1 MiB")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NlValidationError("API response is not UTF-8 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("choices"), list) or not value["choices"]:
        raise NlValidationError("API response has no choices")
    choice = value["choices"][0]
    if not isinstance(choice, dict) or choice.get("finish_reason") not in {"stop", None} or not isinstance(choice.get("message"), dict):
        raise NlValidationError("API response finish reason is invalid or truncated")
    content = _content(choice["message"].get("content"))
    request_id = value.get("id")
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 512):
        request_id = None
    usage = value.get("usage")
    summary: dict[str, int] = {}
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            item = usage.get(key)
            if type(item) is int and 0 <= item <= 1_000_000:
                summary[key] = item
    return content, request_id, summary


def validate_completion_response(body: bytes) -> tuple[str, str | None, dict[str, int]]:
    content, request_id, summary = _completion_parts(body)
    return validate_nl(content), request_id, summary


def _strict_object(text: str) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise NlValidationError("structured NL response contains a duplicate key")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise NlValidationError("structured NL response contains a non-finite number")

    try:
        value = json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, NlValidationError) as exc:
        raise NlValidationError("structured NL response is not a JSON object") from exc
    if not isinstance(value, dict):
        raise NlValidationError("structured NL response is not a JSON object")
    return value


def validate_completion_response_v2(
    body: bytes,
) -> tuple[str, dict[str, object], str | None, dict[str, int]]:
    content, request_id, summary = _completion_parts(body)
    value = _strict_object(content)
    # NL is independently valuable and is validated before the observation fields.
    nl = validate_nl(value.get("nl"))
    raw_count = value.get("count")
    raw_layout = value.get("layout")
    raw_repeated = value.get("sameCharacterRepeated")
    count_value = raw_count if isinstance(raw_count, str) and raw_count in OBSERVATION_COUNTS else None
    layout_value = raw_layout if isinstance(raw_layout, str) and raw_layout in OBSERVATION_LAYOUTS else None
    repeated = raw_repeated if type(raw_repeated) is bool else None
    valid = (
        set(value) == {"nl", "count", "layout", "sameCharacterRepeated"}
        and count_value is not None
        and layout_value is not None
        and repeated is not None
    )
    if valid:
        status = "observed"
        warning_codes = ["count_observation_unknown"] if count_value == "unknown" else []
    else:
        status = "invalid"
        warning_codes = ["count_observation_invalid"]
    observation: dict[str, object] = {
        "schemaVersion": 1,
        "status": status,
        "countValue": count_value,
        "layoutValue": layout_value,
        "sameCharacterRepeated": repeated,
        "warningCodes": warning_codes,
        "notRequestedReason": None,
    }
    return nl, observation, request_id, summary


def _unwrap(text: str) -> str:
    for left, right in WRAPPERS:
        if len(text) > len(left) + len(right) and text.startswith(left) and text.endswith(right):
            inner = text[len(left):-len(right)]
            if left not in inner and right not in inner:
                return inner.strip()
    return text


def strip_wrapper(value: str) -> str:
    """Remove code fences, leading labels and wrapping quotes/emphasis added by weak prompts."""
    text = value.strip()
    for _ in range(4):
        fenced = CODE_FENCE.match(text)
        if fenced is not None:
            text = fenced.group("body").strip()
            continue
        without_label = LABEL_PREFIX.sub("", text, count=1).strip()
        if without_label and without_label != text:
            text = without_label
            continue
        unwrapped = _unwrap(text)
        if unwrapped and unwrapped != text:
            text = unwrapped
            continue
        break
    return text


def validate_nl(value: object) -> str:
    if not isinstance(value, str):
        raise NlValidationError("NL must be text")
    nl = strip_wrapper(value)
    if not nl or len(nl.encode("utf-8")) > MAX_NL_BYTES or "\x00" in nl:
        raise NlValidationError("NL is empty or exceeds its limit")
    lowered = nl.casefold()
    if any(snippet in lowered for snippet in REFUSAL_SNIPPETS) or REFUSAL_PATTERN.search(nl) is not None:
        raise NlValidationError("NL contains a refusal or moderation response")
    if nl[-1] in ",:;-/":
        raise NlValidationError("NL appears truncated")
    if nl.count('"') % 2 or any(nl.count(left) > nl.count(right) for left, right in (("(", ")"), ("[", "]"), ("{", "}"))):
        raise NlValidationError("NL has unclosed punctuation")
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", nl)
    if len(words) >= 8 and words[-1].casefold() in TRAILING_CONNECTORS:
        raise NlValidationError("NL appears truncated")
    return nl
