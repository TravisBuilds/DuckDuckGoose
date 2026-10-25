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


class ShotDecision(str, Enum):
    """G2 decision for a shot."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETRY = "retry"


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
    current_gate: str = ""  # Tracks which gate workflow is waiting at
    credit_cap_raised: bool = False  # Signal that credit cap was raised
    new_credit_cap: int = 0  # New credit cap value
    failed_shots: list[dict] = Field(default_factory=list)  # Failed shot workflows at gate 2
    shots_on_hold: list[str] = Field(default_factory=list)  # Shots waiting on hold at gate 2 for approval
    shot_attempts: dict[str, int] = Field(default_factory=dict)  # Retry attempts per shot (actual retries done)
    shot_attempt_overrides: dict[str, int] = Field(default_factory=dict)  # Override start attempt (doesn't count against budget)
    pending_retries: dict[str, str] = Field(default_factory=dict)  # shot_id -> workflow_id for pending retries
    shot_decisions: dict[str, str] = Field(default_factory=dict)  # shot_id -> ShotDecision for G2 per-shot approve/reject/retry
    g1_plan: dict = Field(default_factory=dict)  # G1 credit plan (per-shot and total credits/USD)
    output_paths: dict[str, dict] = Field(default_factory=dict)  # Local paths: {shot_id: {still_v1: path, clip_v1: path}}
    assembly_path: str | None = None  # Path to mute assembly video
    assembly_manifest: dict = Field(default_factory=dict)  # Assembly manifest with clips[] and metadata
    assembly_qc: dict = Field(default_factory=dict)  # Latest mute-edit QC (verdict, blocking, reasons)
    assembly_acceptance: dict | None = None  # Human acceptance of a blocking QC verdict (reason)
    episode_metadata: dict = Field(default_factory=dict)  # Episode config: locked_vo_path, bgm_config, etc.
    delivery_paths: dict = Field(default_factory=dict)  # Delivery paths: master, nosubs, phone, subs
    delivery_manifest: dict = Field(default_factory=dict)  # Delivery manifest with mix metadata
    delivery_qc: dict = Field(default_factory=dict)  # Delivery QC: loudness, true peak, duration, subs


class CreditTransaction(BaseModel):
    """A credit ledger entry."""

    tx_id: str
    episode_id: str
    job_id: Optional[str] = None
    amount: float
    tx_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    balance_after: float
