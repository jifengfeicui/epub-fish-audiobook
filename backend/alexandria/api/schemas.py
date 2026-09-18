from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


RenderStartMode = Literal["after_review_batch", "after_all_reviews"]
JobType = Literal["preprocess", "render", "merge", "character_analysis"]
UploadLine = Literal["cnbldsa", "bda2", "txa", "alia"]


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


class ChapterListUpdate(BaseModel):
    included_chapter_ids: list[int]


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
    payload: dict[str, Any] = Field(default_factory=dict)


class BilibiliPrepareJob(BaseModel):
    action: Literal["prepare"]


class BilibiliPublishJob(BaseModel):
    action: Literal["publish"]
    parts: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=80)
    author: str = Field(default="", max_length=200)
    publisher: str = Field(default="", max_length=200)
    tid: int = Field(default=201, ge=1)
    tags: str = Field(default="有声书,读书,知识分享,AI配音", min_length=1, max_length=200)
    desc: str = Field(default="", max_length=2000)
    visibility: Literal["only_self", "public"] = "only_self"
    confirm_public: bool = False
    line: UploadLine = "cnbldsa"


class BilibiliAppendJob(BaseModel):
    action: Literal["append"]
    parts: int = Field(ge=1)
    line: UploadLine = "cnbldsa"


class BilibiliReplaceJob(BaseModel):
    action: Literal["replace"]
    chapter_id: int = Field(ge=1)
    line: UploadLine = "cnbldsa"


BilibiliJobCreate = Annotated[
    Union[BilibiliPrepareJob, BilibiliPublishJob, BilibiliAppendJob, BilibiliReplaceJob],
    Field(discriminator="action"),
]


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
