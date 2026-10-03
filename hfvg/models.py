"""Data models for workflows and activities."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class PipelineStage(str, Enum):
    """Pipeline stages from the spec."""

    INFO_GATHERING = "info_gathering"
    READBACK = "readback"
    SCRIPT = "script"
    CHARACTER_LOCKS = "character_locks"
    STORYBOARD = "storyboard"
    SHOT_PLANNING = "shot_planning"
    STILL_GENERATION = "still_generation"
    STILL_REVIEW = "still_review"
    CLIP_GENERATION = "clip_generation"
    CLIP_QC = "clip_qc"
    MUTE_EDIT = "mute_edit"
    PICTURE_LOCK = "picture_lock"
    AUDIO = "audio"
    FINAL_MIX = "final_mix"
    POST_APPROVAL = "post_approval"
    COMPLETE = "complete"


class JobStatus(str, Enum):
    """Status of a generation job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class GenerationRequest(BaseModel):
    """Request for a generation job (still or clip)."""

    shot_id: str
    version: int
    prompt: str
    refs: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: float


class Asset(BaseModel):
    """A generated asset (still or clip)."""

    asset_id: str
    job_id: str
    url: str
    status: JobStatus
    cost: float
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShotPlan(BaseModel):
    """Plan for a single shot."""

    shot_id: str
    scene_id: int
    description: str
    prompt: str
    refs: list[str] = Field(default_factory=list)
    estimated_cost: float


class ScenePlan(BaseModel):
    """Plan for a scene (collection of shots)."""

    scene_id: int
    description: str
    shots: list[ShotPlan]


class Storyboard(BaseModel):
    """Full storyboard with cost estimate."""

    scenes: list[ScenePlan]
    total_estimated_cost: float
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EpisodeState(BaseModel):
    """Current state of an episode."""

    episode_id: str
    stage: PipelineStage
    idea: str
    readback_approved: bool = False
    storyboard_approved: bool = False
    storyboard: Optional[Storyboard] = None
    scenes_approved: set[int] = Field(default_factory=set)
    final_approved: bool = False
    post_approved: bool = False


class CreditTransaction(BaseModel):
    """A credit ledger entry."""

    tx_id: str
    episode_id: str
    job_id: Optional[str] = None
    amount: float
    tx_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    balance_after: float
