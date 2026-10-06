"""
Studio API: Backend service wrapping the Temporal workflow.

Environment variables:
- ADMIN_SECRET: Required. Admin secret for access control (min 32 chars).
- TEMPORAL_ADDRESS: Temporal server address (default: localhost:7233).
- TEMPORAL_NAMESPACE: Temporal namespace (default: default).
- DATABASE_PATH: SQLite database path (default: ./data/studio.db).
- OPENAI_API_KEY: OpenAI API key for vision judge (optional, escalates if missing).
- DRY_RUN: Run in dry-run mode (default: true).

Routes:
- POST /api/episodes: Start a new episode
- GET /api/episodes/{episode_id}: Get episode state
- POST /api/episodes/{episode_id}/approve: Send approval signal
- GET /api/episodes/{episode_id}/shots: List shots with status
- GET /api/episodes/{episode_id}/budget: Get budget status
- POST /api/episodes/{episode_id}/canary: Run canary (1 still + 1 clip)
- POST /api/episodes/{episode_id}/set-live: Switch to live mode
"""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from temporalio.client import Client as TemporalClient

# Check ADMIN_SECRET early
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
if not ADMIN_SECRET or len(ADMIN_SECRET) < 32:
    raise ValueError(
        "ADMIN_SECRET environment variable is required and must be at least 32 characters. "
        "Generate with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/studio.db")
DRY_RUN_DEFAULT = os.getenv("DRY_RUN", "true").lower() == "true"

# Global Temporal client
temporal_client: TemporalClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Temporal client on startup."""
    global temporal_client
    
    try:
        temporal_client = await TemporalClient.connect(
            TEMPORAL_ADDRESS,
            namespace=TEMPORAL_NAMESPACE,
        )
        yield
    finally:
        if temporal_client:
            await temporal_client.close()


app = FastAPI(
    title="DuckDuckGoose Studio API",
    description="Backend API for the DuckDuckGoose video production console",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_admin_secret(authorization: str | None = Header(None)):
    """Verify admin secret from Authorization header."""
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required. Set 'Authorization: Bearer <ADMIN_SECRET>'"
        )
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization must use Bearer token format"
        )
    
    token = authorization[7:]  # Remove 'Bearer ' prefix
    
    if token != ADMIN_SECRET:
        raise HTTPException(
            status_code=403,
            detail="Invalid admin secret"
        )


# Request/Response models
class StartEpisodeRequest(BaseModel):
    episode_id: str = Field(..., pattern=r"^ep\d{2}$", description="Episode ID (e.g., ep04)")
    beatmap_content: str | None = Field(None, description="Beatmap markdown content (optional)")
    dry_run: bool = Field(True, description="Run in dry-run mode (default: true)")


class StartEpisodeResponse(BaseModel):
    workflow_id: str
    run_id: str
    episode_id: str
    message: str


class ApprovalRequest(BaseModel):
    gate_id: str = Field(..., description="Gate ID to approve (e.g., g101, g103, g108, g212, g409)")
    note: str | None = Field(None, description="Optional approval note")


class EpisodeStateResponse(BaseModel):
    episode_id: str
    stage: str | None
    approvals: dict[str, bool]
    shots_count: int


class ShotStatus(BaseModel):
    shot_id: str
    status: str
    prompt: str | None
    still_url: str | None
    clip_url: str | None
    qc_results: dict | None
    retries: int


class BudgetLineStatus(BaseModel):
    line_name: str
    provider: str
    spent: float
    reserved: float
    total: float
    cap: float
    stop: float
    at_stop: bool
    unit: str


class BudgetResponse(BaseModel):
    episode_id: str
    lines: list[BudgetLineStatus]
    higgsfield_total: float
    elevenlabs_total: float


# Routes
@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "DuckDuckGoose Studio API",
        "version": "1.0.0",
        "status": "running",
        "temporal_connected": temporal_client is not None,
    }


@app.get("/api/health")
async def health(_: None = Header(None, alias="Authorization", include_in_schema=False)):
    """
    Health check (no auth required for monitoring).
    """
    return {"status": "ok", "temporal": temporal_client is not None}


@app.post("/api/episodes", response_model=StartEpisodeResponse)
async def start_episode(
    request: StartEpisodeRequest,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    Start a new episode workflow.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(_admin)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Save beatmap content if provided
    beatmap_path: str | None = None
    if request.beatmap_content:
        beatmap_dir = Path("./data/episodes") / request.episode_id
        beatmap_dir.mkdir(parents=True, exist_ok=True)
        beatmap_path = str(beatmap_dir / "BEATMAP.md")
        
        with open(beatmap_path, "w") as f:
            f.write(request.beatmap_content)
    
    # Start workflow
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    workflow_id = f"{request.episode_id}-{asyncio.get_event_loop().time()}"
    
    handle = await temporal_client.start_workflow(
        EpisodeWorkflowV2.run,
        args=[request.episode_id, beatmap_path, request.dry_run],
        id=workflow_id,
        task_queue="hfvg-task-queue",
    )
    
    return StartEpisodeResponse(
        workflow_id=handle.id,
        run_id=handle.result_run_id or "",
        episode_id=request.episode_id,
        message=f"Episode {request.episode_id} started (dry_run={request.dry_run})",
    )


@app.get("/api/episodes/{episode_id}", response_model=EpisodeStateResponse)
async def get_episode_state(
    episode_id: str,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    Get current episode workflow state.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(_admin)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Find the most recent workflow for this episode
    from temporalio.client import WorkflowExecutionStatus
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    try:
        # List workflows matching the episode prefix
        workflows = temporal_client.list_workflows(f'WorkflowId STARTS_WITH "{episode_id}-"')
        
        # Get the first (most recent) workflow
        async for workflow_info in workflows:
            if workflow_info.status in [WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.COMPLETED]:
                handle = temporal_client.get_workflow_handle(workflow_info.id)
                
                # Query workflow state
                state = await handle.query("get_state")
                
                return EpisodeStateResponse(
                    episode_id=state["episode_id"],
                    stage=state.get("stage"),
                    approvals=state.get("approvals", {}),
                    shots_count=state.get("shots", 0),
                )
        
        raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error querying workflow: {str(e)}")


@app.post("/api/episodes/{episode_id}/approve")
async def approve_gate(
    episode_id: str,
    request: ApprovalRequest,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    Send approval signal to episode workflow.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(_admin)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        # Find the workflow
        workflows = temporal_client.list_workflows(f'WorkflowId STARTS_WITH "{episode_id}-"')
        
        async for workflow_info in workflows:
            handle = temporal_client.get_workflow_handle(workflow_info.id)
            
            # Map gate_id to signal method
            signal_name = f"approve_{request.gate_id.lower()}"
            
            await handle.signal(signal_name)
            
            return {
                "success": True,
                "episode_id": episode_id,
                "gate_id": request.gate_id,
                "message": f"Approval sent for gate {request.gate_id}",
                "note": request.note,
            }
        
        raise HTTPException(status_code=404, detail=f"No running workflow found for episode {episode_id}")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sending approval: {str(e)}")


@app.get("/api/episodes/{episode_id}/budget", response_model=BudgetResponse)
async def get_budget_status(
    episode_id: str,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    Get budget status for an episode.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(_admin)
    
    from hfvg.budget import BudgetLedger
    
    ledger = BudgetLedger(DATABASE_PATH)
    summary = await ledger.get_episode_summary(episode_id)
    
    lines = [
        BudgetLineStatus(
            line_name=line["line_name"],
            provider=line["provider"],
            spent=line["spent"],
            reserved=line["reserved"],
            total=line["total"],
            cap=line["cap"],
            stop=line["stop"],
            at_stop=line["at_stop"],
            unit=line["unit"],
        )
        for line in summary["lines"]
    ]
    
    return BudgetResponse(
        episode_id=episode_id,
        lines=lines,
        higgsfield_total=summary["higgsfield_total"],
        elevenlabs_total=summary["elevenlabs_total"],
    )


@app.get("/api/episodes/{episode_id}/shots")
async def get_shots(
    episode_id: str,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    List all shots for an episode with their status.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(_admin)
    
    # TODO: Implement shot status tracking
    # For now, return placeholder
    return {
        "episode_id": episode_id,
        "shots": [],
        "message": "Shot tracking not yet implemented",
    }


@app.post("/api/episodes/{episode_id}/set-live")
async def set_live_mode(
    episode_id: str,
    _admin: None = Header(None, alias="Authorization"),
):
    """
    Switch episode to live (paid) mode.
    
    Requires: Authorization header with admin secret.
    Also requires G1.08 credit plan approval before any paid generation.
    """
    verify_admin_secret(_admin)
    
    # TODO: Implement live mode tracking
    return {
        "success": True,
        "episode_id": episode_id,
        "mode": "live",
        "message": "Episode switched to live mode. Paid generation enabled after G1.08 approval.",
    }


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
