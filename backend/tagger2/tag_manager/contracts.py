"""Strict public request models for the tag manager."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SidecarKindLiteral = Literal[
    "none",
    "tag_txt",
    "tags_json",
    "standard_json",
    "raw_e621_json",
]

EditableSidecarKind = Literal["tag_txt", "tags_json", "standard_json"]

TagManagerProfile = Literal["e621", "danbooru"]

# Frozen nine-field order shared with the dataset workflow; the count field
# uses the same closed vocabulary as the workflow contracts.
COUNT_VALUES = ("", "solo", "duo", "trio", "group")


class CreateDatasetRequest(BaseModel):
    """Open a dataset directory for browsing and tag editing."""

    model_config = ConfigDict(extra="forbid")

    root_id: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(default="", max_length=1024)
    profile: TagManagerProfile = "e621"
    recursive: bool = True
    name: str | None = Field(default=None, max_length=128)


class ImageFilter(BaseModel):
    """Image selection criteria shared by listing and batch operations."""

    model_config = ConfigDict(extra="forbid")

    include_tags: list[str] = Field(default_factory=list, max_length=64)
    exclude_tags: list[str] = Field(default_factory=list, max_length=64)
    include_mode: Literal["all", "any"] = "all"
    kind: Literal["any", "none", "tag_txt", "tags_json", "standard_json", "raw_e621_json"] = "any"
    sidecar: Literal["any", "present", "missing"] = "any"

    @model_validator(mode="after")
    def _strip_tags(self) -> "ImageFilter":
        self.include_tags = [tag.strip() for tag in self.include_tags if tag.strip()]
        self.exclude_tags = [tag.strip() for tag in self.exclude_tags if tag.strip()]
        if not self.include_tags and self.include_mode == "any":
            # An "any" match over an empty set would select nothing; make the
            # empty filter behave like "no constraint" instead.
            self.include_mode = "all"
        return self


class BatchOperationRequest(BaseModel):
    """Apply one tag operation to many images at once."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "remove", "replace"]
    tags: list[str] = Field(default_factory=list, max_length=256)
    replacement: str | None = Field(default=None, max_length=256)
    use_regex: bool = False
    image_ids: list[Annotated[int, Field(ge=1)]] | None = None
    filter: ImageFilter | None = None
    reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _validate_operation(self) -> "BatchOperationRequest":
        self.tags = [tag.strip() for tag in self.tags if tag.strip()]
        if self.op in {"add", "remove"} and not self.tags:
            raise ValueError("add/remove operations require at least one tag")
        if self.op == "replace":
            if self.replacement is None:
                raise ValueError("replace operations require a replacement tag")
            if self.use_regex:
                # With regex mode the single tag IS the search pattern.
                if len(self.tags) != 1:
                    raise ValueError(
                        "regex replace requires exactly one tag holding the pattern"
                    )
            elif not self.tags:
                raise ValueError("replace operations require tags or use_regex")
        if (self.image_ids is None) == (self.filter is None):
            raise ValueError("exactly one of image_ids or filter is required")
        if self.image_ids is not None and not self.image_ids:
            raise ValueError("image_ids must not be empty")
        return self


class TagEdit(BaseModel):
    """One tag inside a tags_json sidecar."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=512)
    category: str | None = Field(default=None, max_length=32)
    score: float | None = Field(default=None, ge=0.0, le=1.0)


class NineFieldEdit(BaseModel):
    """Nine-field standard JSON payload; field order is frozen."""

    model_config = ConfigDict(extra="forbid")

    quality: list[str] = Field(default_factory=list, max_length=8)
    count: Literal["", "solo", "duo", "trio", "group"] = ""
    character: str = Field(default="", max_length=2048)
    series: str = Field(default="", max_length=2048)
    artist: str = Field(default="", max_length=2048)
    appearance: list[str] = Field(default_factory=list, max_length=512)
    tags: list[str] = Field(default_factory=list, max_length=2048)
    environment: list[str] = Field(default_factory=list, max_length=512)
    nl: str = Field(default="", max_length=16_384)


class TagTxtContent(BaseModel):
    """Booru flat TXT payload: plain comma-separated tags."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tag_txt"] = "tag_txt"
    tags: list[str] = Field(max_length=2048)


class TagsJsonContent(BaseModel):
    """Local tags JSON payload: an object tag list with optional metadata."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tags_json"] = "tags_json"
    tags: list[TagEdit] = Field(max_length=2048)


class StandardJsonContent(BaseModel):
    """Nine-field standard JSON payload."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["standard_json"] = "standard_json"
    fields: NineFieldEdit


SaveTagsContent = Annotated[
    Union[TagTxtContent, TagsJsonContent, StandardJsonContent],
    Field(discriminator="kind"),
]


class ImageEditRequest(BaseModel):
    """Save one image's sidecar with optimistic concurrency on the sidecar mtime."""

    model_config = ConfigDict(extra="forbid")

    content: SaveTagsContent
    expected_sidecar_mtime: float | None = Field(default=None, ge=0.0)


class TranslationLookupRequest(BaseModel):
    """Resolve Chinese names for a batch of tags in one round trip."""

    model_config = ConfigDict(extra="forbid")

    profile: TagManagerProfile = "e621"
    tags: list[str] = Field(max_length=500)

    @model_validator(mode="after")
    def _strip_tags(self) -> "TranslationLookupRequest":
        self.tags = [tag.strip() for tag in self.tags if tag.strip()]
        return self


class NlTranslateRequest(BaseModel):
    """Translate one natural-language caption with a configured online model."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=8000)
    target: Literal["zh", "en"] = "zh"
    provider_id: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _strip_text(self) -> "NlTranslateRequest":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("text must not be blank")
        return self


class TagTranslateRequest(BaseModel):
    """Translate tags missing from the offline dictionary with the online model."""

    model_config = ConfigDict(extra="forbid")

    profile: TagManagerProfile = "e621"
    tags: list[str] = Field(max_length=200)
    provider_id: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _strip_tags(self) -> "TagTranslateRequest":
        self.tags = [tag.strip() for tag in self.tags if tag.strip()]
        if not self.tags:
            raise ValueError("tags must contain at least one tag")
        if any(len(tag) > 100 for tag in self.tags):
            raise ValueError("each tag must be at most 100 characters")
        return self


__all__ = [
    "BatchOperationRequest",
    "COUNT_VALUES",
    "CreateDatasetRequest",
    "EditableSidecarKind",
    "ImageEditRequest",
    "ImageFilter",
    "NineFieldEdit",
    "NlTranslateRequest",
    "SidecarKindLiteral",
    "StandardJsonContent",
    "TagEdit",
    "TagManagerProfile",
    "TagTranslateRequest",
    "TagTxtContent",
    "TagsJsonContent",
    "TranslationLookupRequest",
]
