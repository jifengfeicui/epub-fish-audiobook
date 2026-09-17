from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


RenderStartMode = Literal["after_review_batch", "after_all_reviews"]
JobType = Literal["preprocess", "render", "merge"]


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
    archived: Optional[bool] = None


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


class JobCreate(BaseModel):
    type: JobType


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
    fish_workers: Optional[int] = Field(default=None, ge=1, le=16)


class VoiceInput(BaseModel):
    id: Optional[str] = None
    reference_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    pool_order: int = Field(default=0, ge=0)
    gender: str = Field(default="", max_length=100)
    traits: str = Field(default="", max_length=1000)
    enabled: bool = True


class VoiceListUpdate(BaseModel):
    voices: list[VoiceInput]


class VoiceValidationInput(BaseModel):
    reference_id: str = Field(min_length=1, max_length=100)


class ProjectVoicePoolUpdate(BaseModel):
    excluded_voice_ids: list[str] = Field(default_factory=list)
    narrator_voice_profile_id: Optional[str] = Field(default=None, max_length=36)


class CharacterInput(BaseModel):
    speaker: str = Field(min_length=1, max_length=200)
    gender: str = Field(default="", max_length=100)
    personality: str = Field(default="", max_length=1000)
    voice_profile_id: Optional[str] = Field(default=None, max_length=36)


class CharacterListUpdate(BaseModel):
    characters: list[CharacterInput]
