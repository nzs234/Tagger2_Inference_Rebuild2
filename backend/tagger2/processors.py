"""Job item construction and the local/online/hybrid job processors.

Extracted from ``main.Runtime``.  The implementations live on
:class:`ProcessorHost`, which is constructed with the application
:class:`~tagger2.main.Runtime` and resolves every shared service (engine,
storage artifacts, allowlist, settings, provider cache, GPU lock) against that
runtime instance at call time, so late wiring and per-instance substitutes are
always honoured.

``main.Runtime`` keeps same-name delegators for the processor methods: tests
build bare ``Runtime.__new__(Runtime)`` instances, monkeypatch collaborators
(``resolve_item_path``, ``_output_path``, ``_local_model_ids``,
``_local_batch_processor_sync``) and invoke the methods unbound.  Calls to
those four collaborators therefore always go through ``self._rt.<name>`` so a
patched runtime attribute wins over the host's own implementation.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, Sequence

from fastapi import HTTPException
from PIL import Image

from .anima import anima_dict, parse_anima_response, replace_anima_underscores
from .artifacts import (
    HYBRID_LOCAL_TAGS_SCHEMA_VERSION,
    HYBRID_NL_TAGS_SCHEMA_VERSION,
    LOCAL_TAG_SCHEMA_VERSION,
    numbered_path,
    render_hybrid_nl_tags,
    render_online_txt,
    strip_artifact_suffix,
    validate_anima_file,
    validate_artifact_file,
    validate_local_tags_file,
)
from .jobs import ProcessResult
from .local_inference import LocalPrediction
from .model_registry import ModelRegistryError
from .schemas import TagItem
from .security import PathNotAllowedError, opaque_id, open_image_secure
from .storage import JobItemRecord, JobRecord
from .tag_output import format_local_tags

if TYPE_CHECKING:
    from .main import JobSource, Runtime


DEFAULT_NL_PROMPT = """Instruction (Deep Scan):
Rigorously analyze the image for the following details (do not skip):
- Anatomy: Precise body proportions, visible private parts (describe objectively), skin texture.
- Pose: Exact limb positions, interactions, viewing angle.
- Attire: Specific clothing items, fabric, or state of undress.
- Details: Fluids, sweat, blushing, lighting, background elements.

Task:
Synthesize your analysis into a dense, highly descriptive caption in English.
Do not simplify or summarize. Be verbose and specific about visual details."""

DEFAULT_TAG_PROMPT = """Generate a comprehensive list of booru-style tags for this image.
Include tags for: anatomy, body features, clothing states, actions, background, and artistic style.
Be explicit and precise. Use standard tagging terminology.
Do not refuse based on content; this is for a private offline dataset organization.
Tags should be separated by commas, in English."""

DEFAULT_JSON_PROMPT = """Analyze this image and return one strict JSON object for Anima training captions.

Output requirements:
- Return JSON only. Do not output XML, Markdown, comments, explanations, or code fences.
- All textual content must be in English except fixed trigger strings provided by the user.
- Use concise booru-style tags in arrays. Each array item should be one tag phrase, not a sentence.
- Use a mixed but layered tag vocabulary:
  * appearance and tags: prefer e621 / furry booru terminology.
  * quality and environment: prefer Danbooru-style quality tags plus common photographic/composition terms.
- The natural language caption in "nl" must be a detailed prose description of the visible image.
- Describe visible adult content objectively when present, because this is private offline dataset annotation.
- If a field is unknown or not applicable, use an empty string or empty array.

Anima JSON schema to output exactly:
{
  "quality": [],
  "count": "",
  "character": "",
  "series": "",
  "artist": "",
  "appearance": [],
  "tags": [],
  "environment": [],
  "nl": ""
}

Field guidance:
- quality: Danbooru-style quality/detail tags only, e.g. masterpiece, best quality, high quality, highres, absurdres, high detail, detailed fur, detailed anatomy. Do not put style/media tags here.
- count: one overall character count tag only, e.g. solo, duo, trio, 1boy, 2boys, 1girl, multiple characters. Do not repeat count tags in "tags".
- character: known character name only if clearly identifiable; otherwise empty.
- series: known source/franchise only if clearly identifiable; otherwise empty.
- artist: leave empty; the application will overwrite this field from the UI.
- appearance: e621-style character appearance tags: species/body type/anatomy/fur/skin/scales/hair/eye colors/clothing/accessories. Examples: anthro, muscular, chubby, canid, felid, scalie, blue eyes, white fur, horns, claws, tail, red collar.
- tags: e621-style remaining content tags: subject type, action, pose, expression, interaction, objects, explicit content when visible, and art medium/style. Examples: looking at viewer, sitting, male focus, bara, digital media, cel shading, nude, erection, genitals, masturbation.
- environment: Danbooru/common scene and composition tags: background, setting, location, lighting, atmosphere, viewpoint, camera angle, framing. Examples: simple background, outdoors, bedroom, beach, sunset, soft lighting, dramatic lighting, low angle, high angle, close-up, from below.
- nl: a coherent detailed natural-language caption covering characters, gender/presentation when visible, appearance, clothing, pose/action, expression, positions, interactions, objects, environment, lighting, perspective, style, and all important visible details. Prefer 120-180+ words when the image has enough content.

Placement rules:
- Do not include duplicate tags across arrays.
- Do not put count tags such as solo/duo/1boy/2boys in "tags"; put exactly one in "count".
- Do not put style/media tags such as digital art, digital media, digital painting, digital illustration, cel shading, 3d render in "quality"; put them in "tags".
- Do not put viewpoint/composition tags such as low angle, high angle, close-up, from below, from behind in "tags"; put them in "environment".
- Do not put the trigger token into any field; the application will set "artist" separately.
- Do not output XML."""

DEFAULT_PROMPT = DEFAULT_JSON_PROMPT


def _safe_error(
    message: str,
    code: str = "request_failed",
    retryable: bool = False,
    status: int = 400,
    fields: Mapping[str, Any] | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message, "retryable": retryable}
    if fields:
        detail["fields"] = dict(fields)
    return HTTPException(status_code=status, detail=detail)


def _online_prompt(config: Mapping[str, Any], field: str, default: str) -> str:
    value = config.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    legacy = config.get("prompt")
    if field == "json_prompt" and isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return default


def _online_txt_prompt(config: Mapping[str, Any], *, include_tags: bool) -> str:
    nl_prompt = _online_prompt(config, "nl_prompt", DEFAULT_NL_PROMPT)
    if not include_tags:
        return f"{nl_prompt}\n\nIMPORTANT: Return only the natural-language caption. Do not output tags, JSON, Markdown, or code fences."
    tag_prompt = _online_prompt(config, "tag_prompt", DEFAULT_TAG_PROMPT)
    return (
        f"Task 1 (NL):\n{nl_prompt}\n\nTask 2 (TAG):\n{tag_prompt}\n\n"
        "IMPORTANT: Return exactly this plain-text format, with no Markdown or explanations:\n"
        "<NL start>\n(Your Natural Language Description Here)\n<NL end>\n"
        "<TAG start>\n(comma-separated English booru-style tags)\n<TAG end>"
    )


def _marker_text(text: str, marker: str) -> str:
    match = re.search(rf"<{marker}\s+start>(.*?)<{marker}\s+end>", text, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def _parse_online_txt(text: str, *, include_tags: bool) -> tuple[str, list[str]]:
    nl = _marker_text(text, "NL")
    if not include_tags:
        return nl, []
    tag_text = _marker_text(text, "TAG")
    values = [value.strip() for value in re.split(r"[,\n]", tag_text) if value.strip()]
    return nl, list(dict.fromkeys(values))


def _parse_rendered_online_txt(text: str, *, include_tags: bool) -> tuple[str, list[str]]:
    clean = text.strip()
    if not include_tags:
        return clean, []
    caption, separator, tag_text = clean.rpartition("\n\n")
    if not separator:
        return clean, []
    tags = [value.strip() for value in tag_text.split(",") if value.strip()]
    return caption.strip(), list(dict.fromkeys(tags))


class ProcessorHost:
    """Local/online/hybrid job processors registered on the JobManager."""

    def __init__(self, runtime: Runtime) -> None:
        self._rt = runtime

    def __getattr__(self, name: str) -> Any:
        # Resolve shared services against the Runtime at call time.  Only
        # invoked for names the host does not define itself, so patched
        # runtime attributes (see module docstring) are always honoured.
        runtime = self.__dict__.get("_rt")
        if runtime is None:
            raise AttributeError(name)
        return getattr(runtime, name)

    def resolve_item_path(self, item: JobItemRecord) -> Path:
        payload = item.payload or {}
        direct = payload.get("path") or payload.get("upload_path")
        if direct:
            path = Path(str(direct)).resolve(strict=False)
            return self.allowlist.assert_allowed(path, expect="file")
        if item.source_root_id:
            return self.allowlist.resolve(item.source_root_id, item.relative_path, must_exist=True, expect="file")
        raise PathNotAllowedError("job item has no source path")

    def _output_path(self, item: JobItemRecord, job: JobRecord, suffix: str) -> Path:
        config = job.config or {}
        output = config.get("output") or {}
        root_id = output.get("root_id") or job.output_root_id
        relative_base = str(output.get("relative_path") or "").strip().replace("\\", "/")
        source = self._rt.resolve_item_path(item)
        # Only the real image extension is removed.  Generation filenames carry
        # dots inside the name ("(andyredtiger_1.2),yellow.png"), which
        # Path.stem would truncate and leave the sidecar unmatched.
        base_name = strip_artifact_suffix(source.name)
        if root_id:
            root = self.allowlist.get(root_id)
            if root.kind != "output" or not root.writable:
                raise PathNotAllowedError("output root must be a writable output directory")
            rel = Path(relative_base) if relative_base else Path(item.relative_path).parent
            if rel.is_absolute() or ".." in rel.parts:
                raise PathNotAllowedError("output path escapes root")
            return self.allowlist.resolve(root_id, (rel / base_name).as_posix() + suffix, for_write=True)
        # No explicit output root means write beside the source image.
        if item.source_root_id:
            root = self.allowlist.get(item.source_root_id)
            if root.kind != "input":
                raise PathNotAllowedError("invalid source root")
            return source.with_name(base_name + suffix)
        # Upload jobs have no user destination; keep artifacts inside the app.
        if item.payload.get("upload_path"):
            base = self.settings.artifact_dir or self.settings.project_root / "data" / "artifacts"
            job_dir = base / job.id
            job_dir.mkdir(parents=True, exist_ok=True)
            artifact_name = str(item.payload.get("artifact_name") or item.relative_path)
            return job_dir / (strip_artifact_suffix(Path(artifact_name).name) + suffix)
        raise PathNotAllowedError("scanned inputs require a writable output root")

    def _conflict_path(self, path: Path, policy: str, *, valid: bool = False) -> tuple[Path, bool]:
        if path.exists() and policy == "validate-skip" and valid:
            return path, True
        if path.exists() and policy == "rename":
            for index in range(1, 10000):
                candidate = numbered_path(path, index)
                if not candidate.exists():
                    return candidate, False
        return path, False

    async def local_processor(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        if bool((job.config or {}).get("hybrid")):
            return (await self._hybrid_batch_processor([item], job))[0]
        async with self.gpu_lock:
            return await asyncio.to_thread(self._local_processor_sync, item, job)

    async def local_batch_processor(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        if bool((job.config or {}).get("hybrid")):
            return await self._hybrid_batch_processor(items, job)
        async with self.gpu_lock:
            return await asyncio.to_thread(self._rt._local_batch_processor_sync, items, job)

    async def _hybrid_batch_processor(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        """Run merged local tags first, then add the configured online result.

        The job still has ``mode=local`` so the normal finite local batches and
        GPU serialization apply.  Only the online phase is concurrent.
        """

        results: list[ProcessResult | None] = [None] * len(items)
        pending: list[tuple[int, JobItemRecord, Path]] = []
        for index, item in enumerate(items):
            try:
                source = self._rt.resolve_item_path(item)
                if self._hybrid_outputs_current(item, job, source):
                    results[index] = self._hybrid_skipped_result(item, job, source)
                else:
                    pending.append((index, item, source))
            except Exception as exc:
                results[index] = ProcessResult(status="failed", error=str(exc))

        if pending:
            # Reuse the ordinary batch engine with artifact output disabled.
            # Its prediction contains the already merged and formatted local
            # tags, including threshold snapshots and optional classifiers.
            local_config = dict(job.config or {})
            local_output = dict(local_config.get("output") or {})
            local_output["json"] = False
            local_output["txt"] = False
            local_config["output"] = local_output
            local_job = replace(job, config=local_config)
            pending_items = [item for _, item, _ in pending]
            async with self.gpu_lock:
                local_results = await asyncio.to_thread(
                    self._rt._local_batch_processor_sync,
                    pending_items,
                    local_job,
                )

            config = job.config or {}
            concurrency = max(
                1,
                min(
                    self.settings.max_online_concurrency,
                    int(config.get("online_concurrency", 1) or 1),
                ),
            )
            semaphore = asyncio.Semaphore(concurrency)

            async def complete(
                item: JobItemRecord,
                source: Path,
                local_result: ProcessResult,
            ) -> ProcessResult:
                if local_result.status not in {"succeeded", "skipped"}:
                    return local_result
                try:
                    async with semaphore:
                        return await self._write_hybrid_result(
                            item,
                            job,
                            source,
                            local_result,
                        )
                except Exception as exc:
                    # Keep a provider error scoped to the image that caused it;
                    # the rest of the finite batch can still complete.
                    return ProcessResult(status="failed", error=str(exc))

            completed = await asyncio.gather(
                *(
                    complete(item, source, local_result)
                    for (_, item, source), local_result in zip(
                        pending,
                        local_results,
                        strict=True,
                    )
                )
            )
            for (index, _, _), result in zip(pending, completed, strict=True):
                results[index] = result

        return [
            result
            if result is not None
            else ProcessResult(status="failed", error="hybrid batch returned no result")
            for result in results
        ]

    def _hybrid_output_paths(
        self,
        item: JobItemRecord,
        job: JobRecord,
    ) -> tuple[Path, Path | None]:
        config = job.config or {}
        response_mode = str(config.get("online_response") or "")
        if response_mode not in {"nl", "json"}:
            raise ValueError("hybrid jobs require an NL or Anima JSON response")

        txt_target = self._rt._output_path(item, job, ".txt")
        json_target = self._rt._output_path(item, job, ".json") if response_mode == "json" else None
        if str((config.get("output") or {}).get("conflict", "validate-skip")) != "rename":
            return txt_target, json_target

        if not txt_target.exists() and (json_target is None or not json_target.exists()):
            return txt_target, json_target
        for index in range(1, 10_000):
            candidate_txt = numbered_path(txt_target, index)
            candidate_json = numbered_path(json_target, index) if json_target is not None else None
            if not candidate_txt.exists() and (
                candidate_json is None or not candidate_json.exists()
            ):
                return candidate_txt, candidate_json
        raise ValueError("could not allocate a conflict-free hybrid artifact name")

    def _hybrid_outputs_current(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> bool:
        config = job.config or {}
        output = config.get("output") or {}
        if str(output.get("conflict", "validate-skip")) != "validate-skip":
            return False
        txt_target, json_target = self._hybrid_output_paths(item, job)
        response_mode = str(config.get("online_response") or "")
        txt_kind = (
            "hybrid_nl_tags_txt"
            if response_mode == "nl"
            else "hybrid_local_tags_txt"
        )
        txt_schema = (
            HYBRID_NL_TAGS_SCHEMA_VERSION
            if response_mode == "nl"
            else HYBRID_LOCAL_TAGS_SCHEMA_VERSION
        )
        txt_current = self.artifacts.should_skip_file(
            item_id=item.id,
            source_path=source,
            artifact_path=txt_target,
            kind=txt_kind,
            config_hash=job.config_hash,
            schema_version=txt_schema,
            validator=validate_artifact_file,
        )
        if response_mode == "nl":
            return txt_current
        if response_mode != "json" or json_target is None:
            return False
        return txt_current and self.artifacts.should_skip_file(
            item_id=item.id,
            source_path=source,
            artifact_path=json_target,
            kind="hybrid_anima_json",
            config_hash=job.config_hash,
            schema_version=self.artifacts.schema_version,
            validator=validate_anima_file,
        )

    def _hybrid_skipped_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> ProcessResult:
        txt_target, json_target = self._hybrid_output_paths(item, job)
        artifacts = [
            {
                "kind": "txt",
                "path": txt_target.name,
                "size": txt_target.stat().st_size if txt_target.exists() else 0,
            }
        ]
        if json_target is not None:
            artifacts.append(
                {
                    "kind": "json",
                    "path": json_target.name,
                    "size": json_target.stat().st_size if json_target.exists() else 0,
                }
            )
        return ProcessResult(
            status="skipped",
            result={
                "image_id": item.image_id,
                "file_name": Path(item.relative_path).name or source.name,
                "status": "skipped",
                "tags": [],
                "caption": None,
                "anima": None,
                "artifacts": artifacts,
                "warnings": [],
                "timing": {},
            },
        )

    async def _write_hybrid_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
        local_result: ProcessResult,
    ) -> ProcessResult:
        config = job.config or {}
        output = config.get("output") or {}
        response_mode = str(config.get("online_response") or "")
        if response_mode not in {"nl", "json"}:
            raise ValueError("hybrid jobs require an NL or Anima JSON response")
        local_data = local_result.result if isinstance(local_result.result, Mapping) else {}
        raw_tags = local_data.get("tags", [])
        local_tags = [dict(tag) for tag in raw_tags if isinstance(tag, Mapping)] if isinstance(raw_tags, list) else []
        tag_text = [str(tag.get("text", "")).strip() for tag in local_tags]

        provider_snapshot = config.get("provider_snapshot")
        provider = self.provider(
            str(config.get("provider_id") or ""),
            profile_override=provider_snapshot if isinstance(provider_snapshot, Mapping) else None,
        )
        selected_model = str(config.get("provider_model") or "") or None
        result_model = str(config.get("provider_model") or getattr(provider, "model", "online"))
        caption = ""
        anima: dict[str, Any] | None = None
        if response_mode == "nl":
            generated = await provider.generate(
                source,
                _online_txt_prompt(config, include_tags=False),
                model=selected_model,
            )
            caption, _ = _parse_online_txt(generated, include_tags=False)
        else:
            payload = await provider.generate_anima(
                source,
                _online_prompt(config, "json_prompt", DEFAULT_JSON_PROMPT),
                trigger_artist=str(config.get("trigger_artist") or ""),
                model=selected_model,
            )
            if output.get("replace_underscores"):
                payload = replace_anima_underscores(payload)
            anima = anima_dict(payload)
            caption = str(anima["nl"])

        txt_target, json_target = self._hybrid_output_paths(item, job)
        artifacts: list[dict[str, Any]] = []
        if response_mode == "nl":
            self.artifacts.write_bytes(
                job_id=job.id,
                item_id=item.id,
                source_path=source,
                artifact_path=txt_target,
                kind="hybrid_nl_tags_txt",
                data=render_hybrid_nl_tags(caption, tag_text).encode("utf-8"),
                config_hash=job.config_hash,
                schema_version=HYBRID_NL_TAGS_SCHEMA_VERSION,
            )
        else:
            if json_target is None or anima is None:
                raise ValueError("hybrid Anima JSON output is incomplete")
            local_text = ", ".join(value for value in tag_text if value)
            if local_text:
                local_text += "\n"
            self.artifacts.write_bytes(
                job_id=job.id,
                item_id=item.id,
                source_path=source,
                artifact_path=txt_target,
                kind="hybrid_local_tags_txt",
                data=local_text.encode("utf-8"),
                config_hash=job.config_hash,
                schema_version=HYBRID_LOCAL_TAGS_SCHEMA_VERSION,
            )
            self.artifacts.write_bytes(
                job_id=job.id,
                item_id=item.id,
                source_path=source,
                artifact_path=json_target,
                kind="hybrid_anima_json",
                data=(json.dumps(anima, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                config_hash=job.config_hash,
                schema_version=self.artifacts.schema_version,
            )
        artifacts.append(
            {
                "kind": "txt",
                "path": txt_target.name,
                "size": txt_target.stat().st_size if txt_target.exists() else 0,
            }
        )
        if json_target is not None:
            artifacts.append(
                {
                    "kind": "json",
                    "path": json_target.name,
                    "size": json_target.stat().st_size if json_target.exists() else 0,
                }
            )
        warnings = local_data.get("warnings", [])
        timing = local_data.get("timing", {})
        return ProcessResult(
            result={
                "image_id": item.image_id,
                "file_name": Path(item.relative_path).name or source.name,
                "status": "succeeded",
                "model_id": result_model,
                "tags": local_tags,
                "caption": caption,
                "anima": anima,
                "artifacts": artifacts,
                "warnings": list(warnings) if isinstance(warnings, list) else [],
                "timing": dict(timing) if isinstance(timing, Mapping) else {},
            }
        )

    def _local_model_ids(self, config: Mapping[str, Any]) -> list[str]:
        model_ids = [str(value) for value in config.get("model_ids", []) if str(value)]
        if not model_ids:
            model_ids = [record.model_id for record in self.registry.list() if record.tags]
        if not model_ids:
            raise ModelRegistryError("没有可用本地模型")
        return model_ids

    def _local_processor_sync(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        source = self._rt.resolve_item_path(item)
        config = job.config or {}
        cached = self._read_current_local_prediction(item, job, source)
        if cached is not None:
            return self._write_local_result(item, job, source, cached)
        model_ids = self._rt._local_model_ids(config)
        threshold_map = config.get("thresholds") or {}
        image = open_image_secure(
            source,
            max_bytes=self.settings.max_upload_bytes,
            max_pixels=self.settings.max_image_pixels,
            max_edge=self.settings.max_image_edge,
        )
        prediction = self.engine.predict_multi_result(
            model_ids,
            image,
            category_thresholds=threshold_map,
            include_model_tags=bool(config.get("separate_models")),
        )
        self._run_local_classifiers([image], [prediction], config)
        return self._write_local_result(item, job, source, prediction)

    def _local_batch_processor_sync(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        config = job.config or {}
        sources = [self._rt.resolve_item_path(item) for item in items]
        output: list[ProcessResult | None] = [None] * len(items)
        images: list[Image.Image | None] = [None] * len(items)
        valid_indexes: list[int] = []
        for index, source in enumerate(sources):
            cached = self._read_current_local_prediction(items[index], job, source)
            if cached is not None:
                try:
                    output[index] = self._write_local_result(
                        items[index], job, source, cached
                    )
                except Exception as exc:
                    output[index] = ProcessResult(status="failed", error=str(exc))
                continue
            try:
                images[index] = open_image_secure(
                    source,
                    max_bytes=self.settings.max_upload_bytes,
                    max_pixels=self.settings.max_image_pixels,
                    max_edge=self.settings.max_image_edge,
                )
                valid_indexes.append(index)
            except Exception as exc:
                output[index] = ProcessResult(status="failed", error=str(exc))

        if not valid_indexes:
            return [
                result
                if result is not None
                else ProcessResult(status="failed", error="local batch returned no result")
                for result in output
            ]

        model_ids = self._rt._local_model_ids(config)
        threshold_map = config.get("thresholds") or {}

        def predict_indexes(indexes: list[int]) -> None:
            if not indexes:
                return
            started = time.perf_counter()
            batch_images = [
                image
                for index in indexes
                if (image := images[index]) is not None
            ]
            try:
                predictions = self.engine.predict_multi_batch_results(
                    model_ids,
                    batch_images,
                    category_thresholds=threshold_map,
                    include_model_tags=bool(config.get("separate_models")),
                    batch_size=min(
                        len(indexes),
                        max(1, int(config.get("batch_size", 16))),
                    ),
                )
            except Exception as exc:
                if len(indexes) > 1:
                    middle = len(indexes) // 2
                    predict_indexes(indexes[:middle])
                    predict_indexes(indexes[middle:])
                    return
                index = indexes[0]
                item = items[index]
                output[index] = ProcessResult(
                    status="failed",
                    result={
                        "image_id": item.image_id,
                        "file_name": Path(item.relative_path).name,
                        "status": "failed",
                        "tags": [],
                        "artifacts": [],
                        "warnings": [],
                        "timing": {},
                    },
                    error=str(exc),
                )
                return

            elapsed_ms = (time.perf_counter() - started) * 1000 / len(indexes)
            self._run_local_classifiers(batch_images, predictions, config)
            for index, prediction in zip(indexes, predictions, strict=True):
                if not prediction.timing:
                    prediction.timing = {"total_ms": elapsed_ms}
                try:
                    output[index] = self._write_local_result(
                        items[index], job, sources[index], prediction
                    )
                except Exception as exc:
                    output[index] = ProcessResult(status="failed", error=str(exc))

        predict_indexes(valid_indexes)
        return [
            result
            if result is not None
            else ProcessResult(status="failed", error="本地批处理未返回结果")
            for result in output
        ]

    def _read_current_local_prediction(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> LocalPrediction | None:
        """Load a validated local JSON result before image decode/inference."""

        output = (job.config or {}).get("output") or {}
        if output.get("conflict", "validate-skip") != "validate-skip" or not output.get("json"):
            return None
        target = self._rt._output_path(item, job, ".json")
        if not self.artifacts.should_skip_file(
            item_id=item.id,
            source_path=source,
            artifact_path=target,
            kind="local_tags_json",
            config_hash=job.config_hash,
            schema_version=LOCAL_TAG_SCHEMA_VERSION,
            validator=validate_local_tags_file,
        ):
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
            return LocalPrediction(
                tags=[TagItem.model_validate(value) for value in raw["tags"]]
            )
        except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            # The file can change between validation and reading. Fall back to
            # inference instead of trusting a raced or malformed artifact.
            return None

    def _run_local_classifiers(
        self,
        images: Sequence[Any],
        predictions: Sequence[LocalPrediction],
        config: Mapping[str, Any],
    ) -> None:
        requested = [
            str(name)
            for name in config.get("classifiers", [])
            if str(name) in self.classifiers
        ]
        for name in dict.fromkeys(requested):
            values = self.classifiers[name].classify_batch(
                images,
                [prediction.tags for prediction in predictions],
                batch_size=max(1, int(config.get("batch_size", 4))),
            )
            for prediction, value in zip(predictions, values, strict=True):
                detail = value.get(name)
                if isinstance(detail, Mapping):
                    prediction.classifiers[name] = dict(detail)
                errors = value.get("errors")
                if isinstance(errors, list) and errors:
                    prediction.classifiers.setdefault("errors", []).extend(errors)

    def _write_local_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
        prediction: LocalPrediction,
    ) -> ProcessResult:
        config = job.config or {}
        classifier_tags: list[TagItem] = []
        for name in ("aesthetic",):
            detail = prediction.classifiers.get(name)
            token = detail.get("token") if isinstance(detail, Mapping) else None
            if isinstance(token, str) and token.strip():
                classifier_tags.append(
                    TagItem(
                        text=token.strip(),
                        category=name,
                        score=None,
                        source="classifier",
                        model_id=name,
                    )
                )
        output = config.get("output") or {}
        tag_dicts = format_local_tags([*classifier_tags, *prediction.tags], output)
        warnings = [
            f"{issue.get('classifier', 'classifier')}: {issue.get('message', 'failed')}"
            for issue in prediction.classifiers.get("errors", [])
            if isinstance(issue, Mapping)
        ]
        result: dict[str, Any] = {
            "image_id": item.image_id,
            "file_name": Path(item.relative_path).name or source.name,
            "status": "succeeded",
            "tags": tag_dicts,
            "caption": None,
            "anima": None,
            "artifacts": [],
            "warnings": warnings,
            "timing": dict(prediction.timing),
        }
        if bool(config.get("separate_models")) and prediction.model_tags:
            model_results: list[dict[str, Any]] = []
            for model_id, model_tags in prediction.model_tags.items():
                values = format_local_tags(model_tags, output)
                model_results.append(
                    {
                        "model_id": model_id,
                        "model_name": self.registry.get(model_id).name,
                        "tags": values,
                    }
                )
            detail = prediction.classifiers.get("aesthetic")
            token = detail.get("token") if isinstance(detail, Mapping) else None
            if isinstance(token, str) and token.strip():
                classifier_values = format_local_tags(
                    [
                        TagItem(
                            text=token.strip(),
                            category="aesthetic",
                            score=None,
                            source="classifier",
                            model_id="aesthetic",
                        )
                    ],
                    output,
                )
                model_results.append(
                    {
                        "model_id": "aesthetic",
                        "model_name": "LSE14 美学评分",
                        "tags": classifier_values,
                    }
                )
            result["model_results"] = model_results
        policy = str(output.get("conflict", "validate-skip"))
        requested_artifacts = 0
        current_artifacts = 0
        if output.get("txt"):
            requested_artifacts += 1
            target = self._rt._output_path(item, job, ".txt")
            is_current = policy == "validate-skip" and self.artifacts.should_skip_file(
                item_id=item.id,
                source_path=source,
                artifact_path=target,
                kind="local_tags_txt",
                config_hash=job.config_hash,
                schema_version=LOCAL_TAG_SCHEMA_VERSION,
                validator=validate_artifact_file,
            )
            if is_current:
                current_artifacts += 1
            else:
                target, _ = self._conflict_path(target, policy)
                text = ", ".join(tag["text"] for tag in tag_dicts) + ("\n" if tag_dicts else "")
                self.artifacts.write_bytes(
                    job_id=job.id,
                    item_id=item.id,
                    source_path=source,
                    artifact_path=target,
                    kind="local_tags_txt",
                    data=text.encode("utf-8"),
                    config_hash=job.config_hash,
                    schema_version=LOCAL_TAG_SCHEMA_VERSION,
                )
            result["artifacts"] = [{"kind": "txt", "path": target.name, "size": target.stat().st_size if target.exists() else 0}]
        if output.get("json"):
            requested_artifacts += 1
            target = self._rt._output_path(item, job, ".json")
            is_current = policy == "validate-skip" and self.artifacts.should_skip_file(
                item_id=item.id,
                source_path=source,
                artifact_path=target,
                kind="local_tags_json",
                config_hash=job.config_hash,
                schema_version=LOCAL_TAG_SCHEMA_VERSION,
                validator=validate_local_tags_file,
            )
            if is_current:
                current_artifacts += 1
            else:
                target, _ = self._conflict_path(target, policy)
                data = (json.dumps({"tags": tag_dicts}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                self.artifacts.write_bytes(
                    job_id=job.id,
                    item_id=item.id,
                    source_path=source,
                    artifact_path=target,
                    kind="local_tags_json",
                    data=data,
                    config_hash=job.config_hash,
                    schema_version=LOCAL_TAG_SCHEMA_VERSION,
                )
            result["artifacts"].append({"kind": "json", "path": target.name, "size": target.stat().st_size if target.exists() else 0})
        if requested_artifacts and current_artifacts == requested_artifacts:
            result["status"] = "skipped"
        return ProcessResult(status=result["status"], result=result)

    async def online_processor(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        source = self._rt.resolve_item_path(item)
        config = job.config or {}
        provider_id = str(config.get("provider_id") or "")
        trigger = str(config.get("trigger_artist") or "")
        output = config.get("output") or {}
        json_requested = bool(output.get("json"))
        txt_requested = bool(output.get("txt"))
        txt_include_tags = bool(output.get("txt_include_tags"))
        response_mode = str(config.get("online_response") or "")
        if response_mode == "nl":
            use_json_flow = False
            txt_include_tags = False
        elif response_mode == "nl_tags":
            use_json_flow = False
            txt_include_tags = True
        else:
            use_json_flow = response_mode == "json" or json_requested or not txt_requested
        policy = str(output.get("conflict", "validate-skip"))
        json_target = self._rt._output_path(item, job, ".json") if json_requested else None
        txt_target = self._rt._output_path(item, job, ".txt") if txt_requested else None
        json_is_current = bool(
            json_target is not None
            and policy == "validate-skip"
            and self.artifacts.should_skip(
                item_id=item.id,
                source_path=source,
                json_path=json_target,
                config_hash=job.config_hash,
            )
        )
        txt_is_current = bool(
            txt_target is not None
            and policy == "validate-skip"
            and self.artifacts.should_skip_file(
                item_id=item.id,
                source_path=source,
                artifact_path=txt_target,
                kind="anima_txt",
                config_hash=job.config_hash,
                schema_version=self.artifacts.schema_version,
                validator=validate_artifact_file,
            )
        )
        provider = None
        payload = None
        caption = ""
        raw_tag_names: list[str] = []
        if json_is_current and json_target is not None:
            payload = parse_anima_response(json_target.read_text(encoding="utf-8-sig"), trigger_artist=trigger)
        elif not use_json_flow and txt_is_current and txt_target is not None:
            caption, raw_tag_names = _parse_rendered_online_txt(
                txt_target.read_text(encoding="utf-8-sig"),
                include_tags=txt_include_tags,
            )
        else:
            provider_snapshot = config.get("provider_snapshot")
            provider = self.provider(
                provider_id,
                profile_override=provider_snapshot if isinstance(provider_snapshot, Mapping) else None,
            )
            selected_model = str(config.get("provider_model") or "") or None
            if use_json_flow:
                payload = await provider.generate_anima(
                    source,
                    _online_prompt(config, "json_prompt", DEFAULT_JSON_PROMPT),
                    trigger_artist=trigger,
                    model=selected_model,
                )
            else:
                text = await provider.generate(
                    source,
                    _online_txt_prompt(config, include_tags=txt_include_tags),
                    model=selected_model,
                )
                caption, raw_tag_names = _parse_online_txt(text, include_tags=txt_include_tags)
        if payload is not None and output.get("replace_underscores"):
            payload = replace_anima_underscores(payload)
        if output.get("replace_underscores"):
            raw_tag_names = [value.replace("_", " ") for value in raw_tag_names]
        result_model = str(
            config.get("provider_model")
            or (provider.model if provider is not None else "online")
        )
        tags: list[dict[str, Any]] = []
        data = anima_dict(payload) if payload is not None else None
        if data is not None:
            for category, values in (("quality", data["quality"]), ("appearance", data["appearance"]), ("tags", data["tags"]), ("environment", data["environment"])):
                tags.extend({"text": value, "category": category, "score": None, "source": "online", "model_id": result_model} for value in values)
            for field_name in ("count", "character", "series", "artist"):
                if data[field_name]:
                    tags.append({"text": data[field_name], "category": field_name, "score": None, "source": "online", "model_id": result_model})
            caption = str(data["nl"])
        else:
            tags.extend({"text": value, "category": "tags", "score": None, "source": "online", "model_id": result_model} for value in raw_tag_names)
        result: dict[str, Any] = {
            "image_id": item.image_id,
            "file_name": Path(item.relative_path).name or source.name,
            "status": "succeeded",
            "model_id": result_model,
            "tags": tags,
            "caption": caption,
            "anima": data,
            "artifacts": [],
            "warnings": [],
            "timing": {},
        }
        if json_requested:
            target = json_target or self._rt._output_path(item, job, ".json")
            if not json_is_current:
                if payload is None:
                    raise ValueError("online JSON output requires an Anima payload")
                target, _ = self._conflict_path(target, policy)
                self.artifacts.write_anima(
                    job_id=job.id,
                    item_id=item.id,
                    source_path=source,
                    payload=payload,
                    config_hash=job.config_hash,
                    output_dir=target.parent,
                    relative_path=target.name,
                    write_txt=False,
                )
            result["artifacts"].append({"kind": "json", "path": target.name, "size": target.stat().st_size if target.exists() else 0})
        if txt_requested:
            target = txt_target or self._rt._output_path(item, job, ".txt")
            if not txt_is_current:
                target, _ = self._conflict_path(target, policy)
                txt_data = render_online_txt(
                    caption,
                    [str(tag["text"]) for tag in tags],
                    include_tags=txt_include_tags,
                ).encode("utf-8")
                self.artifacts.write_bytes(
                    job_id=job.id,
                    item_id=item.id,
                    source_path=source,
                    artifact_path=target,
                    kind="anima_txt",
                    data=txt_data,
                    config_hash=job.config_hash,
                    schema_version=self.artifacts.schema_version,
                )
            result["artifacts"].append({"kind": "txt", "path": target.name, "size": target.stat().st_size if target.exists() else 0})
        requested_artifacts = int(json_requested) + int(txt_requested)
        current_artifacts = int(json_is_current) + int(txt_is_current)
        if requested_artifacts and current_artifacts == requested_artifacts:
            result["status"] = "skipped"
        return ProcessResult(status=result["status"], result=result)

    def build_job_items(
        self,
        source: JobSource,
    ) -> tuple[Iterable[dict[str, Any]], str | None]:
        if source.type == "upload":
            if not source.upload_id:
                raise _safe_error("缺少 upload_id", "upload_required")
            records = self.upload_index.get(source.upload_id)
            if not records:
                raise _safe_error("上传批次不存在或已过期", "upload_not_found", False, 404)
            return ([{
                "image_id": record["id"],
                "relative_path": record["name"],
                "payload": {
                    "upload_path": record["path"],
                    "file_name": record["name"],
                    "artifact_name": record.get("artifact_name", record["name"]),
                },
            } for record in records], None)
        if not source.root_id:
            raise _safe_error("缺少输入 root_id", "input_root_required")
        self.resolve_root(source.root_id, kind="input")
        # Reuse the same scanner without making an HTTP round-trip.
        root = self.allowlist.resolve(source.root_id, source.relative_path, must_exist=True, expect="dir")
        regexes = []
        for pattern in source.patterns:
            expression = "^" + re.escape(pattern[:128]).replace(r"\*", ".*").replace(r"\?", ".") + "$"
            regexes.append(re.compile(expression, re.IGNORECASE))
        def iter_items() -> Iterator[dict[str, Any]]:
            iterator = root.rglob("*") if source.recursive else root.glob("*")
            emitted = 0
            for path in iterator:
                if emitted >= self.settings.max_batch_items:
                    break
                if (
                    not path.is_file()
                    or path.suffix.casefold() not in self.settings.image_extensions
                ):
                    continue
                if regexes and not any(
                    regex.match(path.name) or regex.match(path.as_posix())
                    for regex in regexes
                ):
                    continue
                rel = self.allowlist.relative_path(source.root_id or "", path)
                emitted += 1
                yield {
                    "image_id": opaque_id(path, prefix="image"),
                    "source_root_id": source.root_id,
                    "relative_path": rel,
                    "payload": {"path": str(path), "file_name": path.name},
                }

        return iter_items(), source.root_id

    # Names registered on the JobManager by ``main.Runtime``.
    local = local_processor
    local_batch = local_batch_processor
    online = online_processor
