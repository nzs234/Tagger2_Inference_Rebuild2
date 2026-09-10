"""Online translation orchestration for the tag manager.

Two on-demand paths borrow the app's configured online providers: NL caption
translation (one paragraph in, one paragraph out) and tag translation for the
tags the offline dictionary misses (chunked model calls, results persisted into
the profile's user dictionary).  Provider errors are sanitized — details go to
the log, the client receives a fixed message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..security import sanitize_provider_error
from .contracts import NlTranslateRequest, TagTranslateRequest
from .errors import TagManagerError
from .protocols import ProviderFactory, ProviderIds, TranslationProvider
from .translations import TagTranslations, normalize_lookup_key

logger = logging.getLogger("tagger2.tag_manager")

NL_TRANSLATION_SYSTEM_PROMPT = {
    "zh": (
        "You translate image dataset captions from English into Simplified Chinese. "
        "Return only the translation as a single paragraph. Do not add notes, "
        "explanations, quotes or markdown. Keep proper nouns, character names and "
        "series titles recognizable, and preserve the original level of detail."
    ),
    "en": (
        "You translate image dataset captions into natural English. "
        "Return only the translation as a single paragraph. Do not add notes, "
        "explanations, quotes or markdown. Keep proper nouns, character names and "
        "series titles recognizable, and preserve the original level of detail."
    ),
}

# On-demand tag translation: how many tags go into one model call and how long
# a saved translation may be (mirrors the build script's sanity cap).
TAG_TRANSLATE_CHUNK_SIZE = 40
TAG_TRANSLATION_MAX_LENGTH = 64
TAG_TRANSLATION_SYSTEM_PROMPT = (
    "You translate image-dataset booru tags into Simplified Chinese. "
    "The user message is a JSON object like {\"tags\": [\"tag1\", \"tag2\"]}. "
    "Return ONLY a JSON object mapping every input tag to its standard Chinese "
    "name, like {\"tag1\": \"中文\"}. For well-known booru tags use the most "
    "common Chinese community translation; otherwise give a concise literal "
    "translation of at most 20 Chinese characters. Species and actions are "
    "translated, artist names may stay recognizable. Do not add notes or "
    "markdown, and never return anything outside the JSON object."
)


def first_provider_id(provider_ids: ProviderIds | None) -> str:
    if provider_ids is None:
        return ""
    try:
        candidates = list(provider_ids())
    except Exception:  # noqa: BLE001 - a broken registry must not 500 here
        return ""
    return str(candidates[0]) if candidates else ""


def resolve_provider(
    provider_factory: ProviderFactory | None,
    provider_ids: ProviderIds | None,
    explicit_provider_id: str | None,
    unavailable_code: str,
) -> tuple[str, TranslationProvider]:
    """Resolve the online provider to use, or raise a 409 setup state."""

    provider_id = (explicit_provider_id or "").strip() or first_provider_id(provider_ids)
    if not provider_id or provider_factory is None:
        raise TagManagerError(
            "没有可用的在线模型：请先在「Provider 配置」中添加并启用一个在线模型",
            code=unavailable_code,
            status_code=409,
        )
    try:
        provider = provider_factory(provider_id)
    except Exception as exc:  # noqa: BLE001 - provider errors are sanitized below
        logger.warning(
            "tag manager provider %s unavailable: %s",
            provider_id,
            sanitize_provider_error(exc),
        )
        raise TagManagerError(
            "在线模型不可用：请检查「Provider 配置」中的地址与密钥，或稍后重试",
            code=unavailable_code,
            status_code=409,
        ) from exc
    return provider_id, provider


async def translate_nl(
    provider_factory: ProviderFactory | None,
    provider_ids: ProviderIds | None,
    request: NlTranslateRequest,
) -> dict[str, Any]:
    """Translate one NL caption with a configured online provider."""

    provider_id, provider = resolve_provider(
        provider_factory, provider_ids, request.provider_id, "nl_translate_unavailable"
    )
    try:
        text = await provider.generate(
            image=None,
            prompt=request.text,
            model=(request.model or "").strip() or None,
            system_prompt=NL_TRANSLATION_SYSTEM_PROMPT[request.target],
        )
    except Exception as exc:  # noqa: BLE001 - one failure mode for the UI
        # The raw provider error may carry URLs, keys or account details;
        # details go to the log (sanitized), the client gets a fixed text.
        logger.warning(
            "tag manager NL translation failed via %s: %s",
            provider_id,
            sanitize_provider_error(exc),
        )
        raise TagManagerError(
            "翻译失败：在线模型暂时不可用，请稍后重试",
            code="nl_translate_failed",
            status_code=502,
            retryable=True,
        ) from exc
    translated = str(text or "").strip()
    if not translated:
        raise TagManagerError(
            "翻译失败：在线模型返回了空结果",
            code="nl_translate_failed",
            status_code=502,
            retryable=True,
        )
    return {
        "text": translated,
        "target": request.target,
        "provider_id": provider_id,
        "model": (request.model or "").strip() or str(getattr(provider, "model", "")),
    }


async def translate_tags(
    translations: TagTranslations,
    provider_factory: ProviderFactory | None,
    provider_ids: ProviderIds | None,
    request: TagTranslateRequest,
) -> dict[str, Any]:
    """Translate tags the offline dictionary misses with the online model.

    Dictionary hits are answered without a model call. Model results are
    persisted into the profile's user dictionary, so the next lookup —
    after a restart or fully offline — resolves them locally.
    """

    profile = str(request.profile)
    unique: dict[str, str] = {}
    for tag in request.tags:
        unique.setdefault(normalize_lookup_key(tag), tag)
    found = translations.translate_many(profile, list(unique.values()))
    missing = [verbatim for verbatim in unique.values() if verbatim not in found]

    translated_now: dict[str, str] = {}
    provider_id = ""
    provider_model = ""
    if missing:
        provider_id, provider = resolve_provider(
            provider_factory, provider_ids, request.provider_id, "tag_translate_unavailable"
        )
        requested_model = (request.model or "").strip()
        try:
            for start in range(0, len(missing), TAG_TRANSLATE_CHUNK_SIZE):
                chunk = missing[start:start + TAG_TRANSLATE_CHUNK_SIZE]
                reply = await provider.generate(
                    image=None,
                    prompt=json.dumps({"tags": chunk}, ensure_ascii=False),
                    model=requested_model or None,
                    system_prompt=TAG_TRANSLATION_SYSTEM_PROMPT,
                )
                for tag, zh in _parse_tag_translation_reply(reply).items():
                    key = normalize_lookup_key(tag)
                    value = str(zh).strip()
                    if not key or not value or len(value) > TAG_TRANSLATION_MAX_LENGTH:
                        continue
                    if key == normalize_lookup_key(value):
                        continue  # the model echoed the English tag back
                    translated_now.setdefault(key, value)
        except TagManagerError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failure mode for the UI
            # Details stay in the log; the client only gets the fixed text.
            logger.warning(
                "tag manager tag translation failed via %s: %s",
                provider_id,
                sanitize_provider_error(exc),
            )
            raise TagManagerError(
                "翻译失败：在线模型暂时不可用，请稍后重试",
                code="tag_translate_failed",
                status_code=502,
                retryable=True,
            ) from exc
        if not translated_now:
            raise TagManagerError(
                "翻译失败：在线模型没有返回可用的翻译结果",
                code="tag_translate_failed",
                status_code=502,
                retryable=True,
            )
        translations.ingest(profile, translated_now)
        provider_model = requested_model or str(getattr(provider, "model", ""))

    merged = dict(found)
    for key, verbatim in unique.items():
        saved = translated_now.get(key)
        if saved is not None and verbatim not in merged:
            merged[verbatim] = saved
    return {
        "profile": profile,
        "translations": merged,
        "translated_now": len(translated_now),
        "from_dictionary": len(found),
        "provider_id": provider_id,
        "model": provider_model,
    }


def _parse_tag_translation_reply(text: str) -> dict[str, str]:
    """Parse the model's JSON tag->translation reply.

    Tolerates markdown fences by extracting the outermost JSON object. Raises
    ``ValueError`` when no JSON object can be recovered so the caller maps the
    failure to a retryable 502.
    """

    raw = str(text or "").strip()
    candidates: list[str] = []
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    candidates.append(raw)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return {
                str(key): value
                for key, value in parsed.items()
                if isinstance(value, str) and value.strip()
            }
    raise ValueError("model reply was not a JSON object")


__all__ = [
    "first_provider_id",
    "resolve_provider",
    "translate_nl",
    "translate_tags",
]
