"""Data models for workflows and activities."""

from datetime import datetime, timezone
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
    DELIVERY = "delivery"
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    delivery_paths: dict = Field(default_factory=dict)  # Deterministic delivery outputs (mix, masters, phone, subs)
    delivery_manifest: dict = Field(default_factory=dict)  # Mix/delivery manifest (input_key, loudnorm, cues)
    delivery_qc: dict = Field(default_factory=dict)  # Latest delivery QC (verdict, blocking, checks)
    delivery_acceptance: dict | None = None  # Human acceptance of a blocking delivery QC verdict (reason)


class CreditTransaction(BaseModel):
    """A credit ledger entry."""

    tx_id: str
    episode_id: str
    job_id: Optional[str] = None
    amount: float
    tx_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    balance_after: float


class BeatTiming(BaseModel):
    """Timing specification for a story beat."""

    beat_id: str
    target_duration_s: float
    description: str


class StoryBeat(BaseModel):
    """A single story beat with emotional arc and shot assignments."""

    beat_id: str
    description: str
    emotional_tone: str
    target_duration_s: float
    shot_ids: list[str] = Field(default_factory=list)


class Act(BaseModel):
    """A narrative act (typically 3 acts for a story)."""
    act_number: int = Field(..., ge=1, le=5)
    description: str
    target_duration_s: float = Field(..., gt=0)
    beat_ids: list[str] = Field(default_factory=list)


class StoryShape(BaseModel):
    """Story structure for a ~2-minute episode."""

    logline: str
    hook: str
    hook_deadline_s: float = 5.0
    acts: list[Act] = Field(..., min_length=1)
    beats: list[StoryBeat]
    midpoint_beat_id: str
    payoff: str
    payoff_references_hook: bool = False
    total_duration_s: float = 120.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1

    def validate_structure(self) -> list[str]:
        """Validate story structure and return list of issues."""
        from hfvg.config import config
        
        issues = []
        
        if len(self.hook) < 10:
            issues.append("Hook is too short (minimum 10 characters)")
        
        # Validate acts
        if not self.acts:
            issues.append("No acts defined")
        else:
            act_durations = sum(act.target_duration_s for act in self.acts)
            if abs(act_durations - self.total_duration_s) > 5.0:
                issues.append(
                    f"Act durations ({act_durations:.1f}s) don't match target "
                    f"({self.total_duration_s:.1f}s), difference: "
                    f"{abs(act_durations - self.total_duration_s):.1f}s"
                )
        
        if len(self.beats) < 3:
            issues.append("Story must have at least 3 beats for 3-act structure")
        
        beat_durations = sum(beat.target_duration_s for beat in self.beats)
        if abs(beat_durations - self.total_duration_s) > 5.0:
            issues.append(
                f"Beat durations ({beat_durations:.1f}s) don't match target "
                f"({self.total_duration_s:.1f}s), difference: "
                f"{abs(beat_durations - self.total_duration_s):.1f}s"
            )
        
        if self.midpoint_beat_id not in [b.beat_id for b in self.beats]:
            issues.append(f"Midpoint beat '{self.midpoint_beat_id}' not found in beats")
        
        # Dead beats: use configurable threshold (default 20s)
        max_beat_duration = config.STORY_SHAPING_MAX_BEAT_DURATION_S
        dead_beats = [b for b in self.beats if b.target_duration_s > max_beat_duration]
        if dead_beats:
            issues.append(
                f"Beats too long (>{max_beat_duration:.0f}s): " + ", ".join(
                    f"{b.beat_id} ({b.target_duration_s:.1f}s)" for b in dead_beats
                )
            )
        
        unassigned_beats = [b for b in self.beats if not b.shot_ids]
        if unassigned_beats:
            issues.append(
                f"Beats without shots: " + ", ".join(b.beat_id for b in unassigned_beats)
            )
        
        return issues


class StoryShapeCritique(BaseModel):
    """Critique and scoring of a story shape."""

    score: int = Field(..., ge=0, le=100)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    hook_present_early: bool = False
    payoff_references_hook: bool = False
    durations_fit: bool = False
    all_beats_have_shots: bool = False
    no_dead_beats: bool = False
    passes: bool = False
