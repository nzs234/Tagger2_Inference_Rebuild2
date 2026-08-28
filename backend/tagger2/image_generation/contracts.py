"""Strict public request models for image generation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImageJobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    operation: Literal["generate", "edit"] = "generate"
    prompt: str = Field(min_length=1, max_length=20_000)
    n: int = Field(default=1, ge=1, le=10)
    aspect_ratio: str | None = Field(default=None, max_length=16)
    image_size: str | None = Field(default=None, max_length=16)
    resolution: str | None = Field(default=None, max_length=8)
    multi_image_strategy: Literal["parallel", "candidate_count"] = "parallel"
    include_text_modality: bool = False
    system_instruction: str | None = Field(default=None, max_length=20_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=1000)
    size: str | None = Field(default=None, max_length=32)
    quality: str | None = Field(default=None, max_length=32)
    background: str | None = Field(default=None, max_length=32)
    output_format: str | None = Field(default=None, max_length=16)
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str | None = Field(default=None, max_length=32)
    input_fidelity: str | None = Field(default=None, max_length=32)
    response_format: Literal["b64_json", "url"] | None = None

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt is required")
        return value


__all__ = ["ImageJobConfig"]
