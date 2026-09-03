"""Batch Chinese summary job for the tag wiki (offline pre-translation).

Every page in the requested scope gets ONE model call that returns a
structured JSON summary (``meaning`` / ``usage`` / ``pairing`` / ``notes`` /
``tags``) in Simplified Chinese. Results are persisted through the wiki store,
so a later run skips pages that already carry a summary. The job mirrors the
tag manager's translation conventions: tolerant extraction of the outermost
JSON object, echo/oversized-value dropping and one failure mode surfaced to
the UI with ``retryable=True``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Mapping, Sequence

from .wiki_store import WikiStore

logger = logging.getLogger("tagger2.tag_wiki.translator")

# One call per page: the wiki text is truncated to keep prompts (and cost)
# bounded; the retrieval stack still searches the full English chunks.
MAX_PAGE_TEXT_CHARS = 4000
MAX_SUMMARY_FIELD_CHARS = 400
MAX_SUMMARY_TAGS = 12

SUMMARY_SYSTEM_PROMPT = (
    "You summarize booru tag wiki pages in Simplified Chinese for artists who "
    "cannot read English well. The user message is JSON like "
    '{"tag": "hug", "title": "hug", "text": "<wiki content>"}. Read the wiki '
    "text and return ONLY one JSON object exactly like "
    '{"meaning": "...", "usage": "...", "pairing": "...", "notes": "...", '
    '"tags": ["related_tag", ...]}. '
    "meaning: 这个 tag 描述什么内容（2-3 句）。usage: 什么时候应该使用这个 tag。"
    "pairing: 它常与哪些 tag 搭配、会隐含哪些 tag（没有就留空字符串）。"
    "notes: 常见误用、区别或注意事项（没有就留空字符串）。"
    "tags: 3-8 个相关英文 booru tag，小写、下划线分隔，必须是 wiki 文本中出现过的 tag。"
    "All four text fields must be Simplified Chinese, each at most 200 "
    "characters, plain text without markdown. Never return anything outside "
    "the JSON object."
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_summary_json(reply: str) -> dict[str, Any] | None:
    """Extract the outermost JSON object from a model reply, tolerantly."""

    text = str(reply or "").strip()
    if not text:
        return None
    candidates = [text]
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _clean_field(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_SUMMARY_FIELD_CHARS:
        text = text[: MAX_SUMMARY_FIELD_CHARS - 1].rstrip() + "…"
    return text


def _clean_tags(value: Any) -> list[str]:
    """Normalize the suggested-tag list: lowercase underscore, dedup, cap."""

    if not isinstance(value, (list, tuple, set)):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = str(item or "").strip().replace(" ", "_").casefold()
        if not tag or len(tag) > 100 or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= MAX_SUMMARY_TAGS:
            break
    return tags


def page_context_text(page: Mapping[str, Any]) -> str:
    """Render one store page into the compact text handed to the model."""

    parts: list[str] = []
    for section in page.get("sections") or ():
        heading = str(section.get("heading", "")).strip()
        text = str(section.get("text", "")).strip()
        if not text:
            continue
        parts.append(f"## {heading}\n{text}" if heading else text)
    combined = "\n\n".join(parts)
    if len(combined) > MAX_PAGE_TEXT_CHARS:
        combined = combined[:MAX_PAGE_TEXT_CHARS] + "…"
    return combined


async def summarize_page(
    provider: Any,
    *,
    tag: str,
    title: str,
    text: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Summarize one wiki page; ``None`` means the reply was unusable."""

    prompt = json.dumps({"tag": tag, "title": title, "text": text}, ensure_ascii=False)
    reply = await provider.generate(image=None, prompt=prompt, model=model, system_prompt=SUMMARY_SYSTEM_PROMPT)
    data = extract_summary_json(str(reply or ""))
    if data is None:
        return None
    meaning = _clean_field(data.get("meaning"))
    usage = _clean_field(data.get("usage"))
    pairing = _clean_field(data.get("pairing"))
    notes = _clean_field(data.get("notes"))
    tags = _clean_tags(data.get("tags"))
    if not any((meaning, usage, pairing, notes)):
        return None
    return {"meaning": meaning, "usage": usage, "pairing": pairing, "notes": notes, "tags": tags}


async def translate_pages(
    store: WikiStore,
    provider: Any,
    titles: Sequence[str],
    *,
    model: str | None = None,
    provider_id: str = "",
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Summarize ``titles`` one by one, persisting each summary as it lands.

    The loop is deliberately resumable: pages that already carry a summary are
    skipped by the caller, and pages whose model call fails are counted and
    left for a later run. Returns ``{"done", "failed"}``.
    """

    done = 0
    failed = 0
    skipped = 0
    resolved_model = model or str(getattr(provider, "model", "") or "")
    for title in titles:
        page = store.get_page(title)
        if page is None:
            continue
        text = page_context_text(page)
        if not text:
            # Nothing readable to summarize (e.g. a page whose only content
            # was filtered as chunk junk); not a translation failure.
            skipped += 1
            continue
        try:
            summary = await summarize_page(
                provider,
                tag=str(page.get("title", title)),
                title=str(page.get("display_title") or page.get("title", title)),
                text=text,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 - one failure mode for the UI
            logger.warning("tag wiki summary failed for %s: %s", title, exc)
            summary = None
        if summary is None:
            failed += 1
        else:
            store.upsert_summary(
                title,
                {**summary, "provider_id": provider_id, "model": resolved_model},
            )
            done += 1
        if on_progress is not None:
            on_progress(done, failed)
    return {"done": done, "failed": failed, "skipped": skipped}


__all__ = [
    "MAX_PAGE_TEXT_CHARS",
    "MAX_SUMMARY_FIELD_CHARS",
    "MAX_SUMMARY_TAGS",
    "SUMMARY_SYSTEM_PROMPT",
    "extract_summary_json",
    "page_context_text",
    "summarize_page",
    "translate_pages",
]
