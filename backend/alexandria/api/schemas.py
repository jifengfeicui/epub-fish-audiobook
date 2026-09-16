from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


RenderStartMode = Literal["after_review_batch", "after_all_reviews"]


class ProjectUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    render_start_mode: Optional[RenderStartMode] = None
    release_batch_size: Optional[int] = Field(default=None, ge=1, le=20)
    from_chapter: Optional[int] = Field(default=None, ge=1)
    to_chapter: Optional[int] = Field(default=None, ge=1)
    first_person_speaker: Optional[str] = Field(default=None, max_length=200)
    context_window: Optional[int] = Field(default=None, ge=0, le=12)
    single_speaker: Optional[bool] = None
    speaker_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    instruct: Optional[str] = Field(default=None, max_length=1000)
    workers: Optional[int] = Field(default=None, ge=1, le=16)


class ScriptEntryInput(BaseModel):
    speaker: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    instruct: str = Field(default="", max_length=2000)

    @field_validator("speaker")
    @classmethod
    def strip_speaker(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be empty")
        return value

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be empty")
        return value


class ScriptUpdate(BaseModel):
    expected_revision: int
    entries: list[ScriptEntryInput] = Field(min_length=1)


class SettingsUpdate(BaseModel):
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    fish_base_url: Optional[str] = None
    fish_api_key: Optional[str] = None
    fish_model: Optional[str] = None
    generation: Optional[dict[str, Any]] = None
    prompts: Optional[dict[str, str]] = None
    fish_tts: Optional[dict[str, Any]] = None


class VoiceInput(BaseModel):
    id: Optional[str] = None
    reference_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    bound_speaker: Optional[str] = Field(default=None, max_length=200)
    pool_order: int = Field(default=0, ge=0)


class VoiceListUpdate(BaseModel):
    voices: list[VoiceInput]
