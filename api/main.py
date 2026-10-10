"""
Studio API: Backend service wrapping the Temporal workflow.

Environment variables:
- ADMIN_SECRET: Required. Admin secret for access control (min 32 chars). NO DEFAULT.
- TEMPORAL_ADDRESS: Temporal server address (default: localhost:7233).
- TEMPORAL_NAMESPACE: Temporal namespace (default: default).
- DATABASE_PATH: SQLite database path (default: ./data/studio.db).
- OPENAI_API_KEY: OpenAI API key for vision judge (optional, escalates if missing).
- DRY_RUN: Run in dry-run mode (default: true).

Routes:
- POST /api/login: Set admin cookie
- POST /api/episodes: Start a new episode
- POST /api/episodes/upload: Upload beatmap
- GET /api/episodes/{episode_id}: Get episode state
- POST /api/episodes/{episode_id}/approve: Send approval signal
- GET /api/episodes/{episode_id}/shots: List shots with status
- POST /api/episodes/{episode_id}/shots/{shot_id}/approve: Approve still
- POST /api/episodes/{episode_id}/shots/{shot_id}/reject: Reject still
- GET /api/episodes/{episode_id}/gates: Get gate status
- GET /api/episodes/{episode_id}/budget: Get budget status
- GET /api/episodes/{episode_id}/audit: Get audit trail
- POST /api/episodes/{episode_id}/canary: Run canary (1 still + 1 clip)
- POST /api/episodes/{episode_id}/set-live: Switch to live mode (requires confirmation)
"""

import os
import asyncio
import json
import hashlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Cookie, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from temporalio.client import Client as TemporalClient

from hfvg.studio_db import init_studio_db, create_episode, set_live_mode as db_set_live_mode, approve_g108, insert_shots_from_beatmap
from hfvg.budget import BudgetLedger
from hfvg.episode_parser import parse_beatmap
from hfvg.credit_plan_parser import parse_credit_plan
from hfvg.temporal_converter import temporal_data_converter
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider

# Check ADMIN_SECRET early - FAIL CLOSED
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")
if not ADMIN_SECRET or len(ADMIN_SECRET) < 32:
    raise ValueError(
        "ADMIN_SECRET environment variable is required and must be at least 32 characters. "
        "NO DEFAULT SECRET. Generate with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "default")
DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/studio.db")
DRY_RUN_DEFAULT = os.getenv("DRY_RUN", "true").lower() == "true"

# Global Temporal client
temporal_client: TemporalClient | None = None

# Session management - stored in DB to survive restart
async def create_session() -> str:
    """Create a new session and store in database."""
    import aiosqlite
    
    token = secrets.token_urlsafe(32)
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Create sessions table if not exists
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        
        # Insert session (24 hour expiry)
        await db.execute(
            """INSERT INTO sessions (token, created_at, expires_at)
               VALUES (?, datetime('now'), datetime('now', '+1 day'))""",
            (token,)
        )
        await db.commit()
    
    return token


async def verify_session_token(token: str) -> bool:
    """Verify a session token from database."""
    import aiosqlite
    
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            
            async with db.execute(
                """SELECT token FROM sessions 
                   WHERE token = ? AND expires_at > datetime('now')""",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Temporal client on startup."""
    global temporal_client
    
    try:
        temporal_client = await TemporalClient.connect(
            TEMPORAL_ADDRESS,
            namespace=TEMPORAL_NAMESPACE,
            data_converter=temporal_data_converter,
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


async def verify_admin_cookie(studio_admin_token: str | None = Cookie(None)):
    """
    Verify admin auth from httpOnly cookie (fail closed).
    
    Args:
        studio_admin_token: Session token from cookie
    
    Raises:
        HTTPException: If cookie missing or invalid
    """
    if not studio_admin_token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in."
        )
    
    if not await verify_session_token(studio_admin_token):
        raise HTTPException(
            status_code=403,
            detail="Invalid or expired session. Please log in again."
        )


def verify_admin_secret(authorization: str | None = Header(None)):
    """Verify admin secret from Authorization header (for programmatic access)."""
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required. Set 'Authorization: Bearer <ADMIN_SECRET>'"
        )
    
    if authorization and not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization must use Bearer token format"
        )
    
    token = authorization[7:] if authorization else ""
    
    if token != ADMIN_SECRET:
        raise HTTPException(
            status_code=403,
            detail="Invalid admin secret"
        )


# Request/Response models
def validate_episode_id(episode_id: str) -> str:
    """
    Validate episode ID format.
    
    Must match: ep## (e.g., ep04, ep99)
    Prevents path traversal attacks.
    """
    import re
    if not re.match(r"^ep\d{2}$", episode_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid episode_id format: {episode_id}. Must match ep## (e.g., ep04)"
        )
    return episode_id


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


class LoginRequest(BaseModel):
    admin_secret: str = Field(..., description="Admin secret for authentication")


class SetLiveModeRequest(BaseModel):
    confirmation: str = Field(..., description="Must be 'ENABLE_LIVE_MODE' to confirm")


class AuditLogEntry(BaseModel):
    id: int
    episode_id: str
    action: str
    details: str | None
    user: str | None
    timestamp: str


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
async def health(_auth: str | None = Header(None, alias="Authorization", include_in_schema=False)):
    """
    Health check (no auth required for monitoring).
    """
    return {"status": "ok", "temporal": temporal_client is not None}


@app.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """
    Login and set httpOnly cookie.
    
    Args:
        request: Login request with admin_secret
        response: Response to set cookie on
    
    Returns:
        Success message
    """
    # Validate secret server-side (fail closed)
    if not request.admin_secret or request.admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")
    
    # Create session and set httpOnly cookie with session token (NOT the secret)
    session_token = await create_session()
    response.set_cookie(
        key="studio_admin_token",
        value=session_token,
        httponly=True,
        secure=False,  # Set True in production with HTTPS
        samesite="lax",
        max_age=86400,  # 24 hours
    )
    
    return {"success": True, "message": "Logged in successfully"}


@app.post("/api/episodes/upload")
async def upload_beatmap(
    episode_id: str = Form(...),
    beatmap_file: UploadFile = File(...),
    credit_plan_file: UploadFile = File(None),
    continuity_file: UploadFile = File(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Upload episode files (BEATMAP required, CREDIT-PLAN and CONTINUITY optional).
    
    Requires: Cookie auth
    
    Returns:
        Number of shots parsed
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Create episode directory
    beatmap_dir = Path("./data/episodes") / episode_id
    beatmap_dir.mkdir(parents=True, exist_ok=True)
    
    # Save beatmap file
    beatmap_path = beatmap_dir / "BEATMAP.md"
    content = await beatmap_file.read()
    beatmap_path.write_bytes(content)
    
    # Save credit plan if provided
    credit_plan_path = None
    if credit_plan_file:
        credit_plan_path = beatmap_dir / "CREDIT-PLAN.md"
        content = await credit_plan_file.read()
        credit_plan_path.write_bytes(content)
    
    # Save continuity file if provided
    continuity_path = None
    if continuity_file:
        continuity_path = beatmap_dir / "CONTINUITY.md"
        content = await continuity_file.read()
        continuity_path.write_bytes(content)
    
    # Parse beatmap
    shots = parse_beatmap(str(beatmap_path))
    
    # Initialize database
    await init_studio_db(DATABASE_PATH)
    
    # Create episode record
    await create_episode(DATABASE_PATH, episode_id, str(beatmap_path))
    
    # Insert shots
    await insert_shots_from_beatmap(DATABASE_PATH, episode_id, shots)
    
    # Parse and initialize budget if credit plan was provided
    if credit_plan_path and credit_plan_path.exists():
        credit_plan = parse_credit_plan(credit_plan_path)
        
        # Initialize budget ledger with credit plan
        ledger = BudgetLedger(DATABASE_PATH)
        await ledger.init_db()
        await ledger.init_episode_budget_from_plan(episode_id, credit_plan)
    
    return {
        "success": True,
        "episode_id": episode_id,
        "shots_count": len(shots),
        "files_uploaded": {
            "beatmap": True,
            "credit_plan": credit_plan_file is not None,
            "continuity": continuity_file is not None,
        },
        "beatmap_path": str(beatmap_path),
    }


@app.post("/api/episodes/{episode_id}/upload-prompt-kit")
async def upload_prompt_kit(
    episode_id: str,
    prompt_kit_file: UploadFile = File(...),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Upload prompt_kit.json for an episode.
    
    Requires: Cookie auth
    
    The prompt kit contains:
    - style: Series visual style line
    - aspect_ratio: e.g. "9:16"
    - characters: Map of codes to {description, ref}
    - sets: Map of set IDs to descriptions
    
    Stored in data/episodes/{episode_id}/prompt_kit.json (not committed to repo).
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Create episode directory
    episode_dir = Path("./data/episodes") / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    
    # Save prompt_kit.json
    prompt_kit_path = episode_dir / "prompt_kit.json"
    content = await prompt_kit_file.read()
    
    # Validate JSON format
    try:
        prompt_kit_data = json.loads(content)
        
        # Validate required fields
        if "style" not in prompt_kit_data:
            raise HTTPException(status_code=400, detail="prompt_kit.json must contain 'style' field")
        if "characters" not in prompt_kit_data:
            raise HTTPException(status_code=400, detail="prompt_kit.json must contain 'characters' field")
        
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    
    prompt_kit_path.write_bytes(content)
    
    return {
        "success": True,
        "episode_id": episode_id,
        "prompt_kit_path": str(prompt_kit_path),
        "message": "prompt_kit.json uploaded successfully"
    }


@app.post("/api/episodes", response_model=StartEpisodeResponse)
async def start_episode(
    request: StartEpisodeRequest,
    authorization: str | None = Header(None),
):
    """
    Start a new episode workflow.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(authorization)
    validate_episode_id(request.episode_id)
    
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
        task_queue="hfvg-tasks",
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
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get current episode workflow state.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    # Accept either Bearer token or session cookie
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    validate_episode_id(episode_id)
    
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
    authorization: str | None = Header(None),
):
    """
    Send approval signal to episode workflow.
    
    Requires: Authorization header with admin secret.
    """
    verify_admin_secret(authorization)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        # Find the EpisodeWorkflowV2 only (not child shot workflows)
        # Episode workflow ID is just the episode_id (e.g., "ep14")
        # Shot workflows have IDs like "ep14-shot-A01" which we must exclude
        workflows = temporal_client.list_workflows(
            f'WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-" AND WorkflowType = "EpisodeWorkflowV2"'
        )
        
        async for workflow_info in workflows:
            # Double-check: skip shot workflows
            if "-shot-" in workflow_info.id:
                continue
            
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
        
        raise HTTPException(status_code=404, detail=f"No running EpisodeWorkflowV2 found for episode {episode_id}")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error sending approval: {str(e)}")


@app.get("/api/episodes/{episode_id}/budget", response_model=BudgetResponse)
async def get_budget_status(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get budget status for an episode.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    # Accept either Bearer token or session cookie
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    validate_episode_id(episode_id)
    
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
    studio_admin_token: str | None = Cookie(None),
):
    """
    List all shots for an episode with their status.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    import aiosqlite
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            """SELECT shot_id, status, still_url, clip_url, qc_results, retries, prompt
               FROM shots WHERE episode_id = ? ORDER BY shot_id""",
            (episode_id,)
        ) as cursor:
            shots = []
            async for row in cursor:
                qc_results = json.loads(row[4]) if row[4] else {}
                shots.append({
                    "shot_id": row[0],
                    "status": row[1],
                    "still_url": row[2],
                    "clip_url": row[3],
                    "qc_results": qc_results,
                    "retries": row[5],
                    "prompt": row[6],
                })
    
    return {
        "episode_id": episode_id,
        "shots": shots,
    }


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/approve")
async def approve_still(
    episode_id: str,
    shot_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Approve still for a shot (sends signal to workflow).
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Validate shot_id format
    if not shot_id or not isinstance(shot_id, str):
        raise HTTPException(status_code=400, detail=f"Invalid shot_id: {shot_id}")
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Try to find the workflow:
    # 1. First try canary workflow: {episode_id}-canary-{shot_id}
    # 2. Then try episode shot workflow: {episode_id}-shot-{shot_id}
    from hfvg.workflows.shot import ShotWorkflow
    
    # Try canary workflow first (most common for manual approval)
    canary_workflow_id = f"{episode_id}-canary-{shot_id}"
    episode_shot_workflow_id = f"{episode_id}-shot-{shot_id}"
    
    workflow_found = False
    workflow_type = None
    handle = None
    
    # Try canary workflow
    try:
        handle = temporal_client.get_workflow_handle_for(
            ShotWorkflow.run,
            workflow_id=canary_workflow_id,
        )
        workflow_found = True
        workflow_type = "canary"
    except Exception:
        pass
    
    # Try episode shot workflow if canary not found
    if not workflow_found:
        try:
            handle = temporal_client.get_workflow_handle_for(
                ShotWorkflow.run,
                workflow_id=episode_shot_workflow_id,
            )
            workflow_found = True
            workflow_type = "episode_shot"
        except Exception:
            pass
    
    if not workflow_found:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow found for {episode_id}/{shot_id}. "
                   f"Canary or episode shot workflow must be running to approve stills."
        )
    
    try:
        # Check if the workflow is still running before sending signal
        from temporalio.client import WorkflowExecutionStatus
        try:
            desc = await handle.describe()
            status = desc.status
            
            if status != WorkflowExecutionStatus.RUNNING:
                # Workflow already completed or in terminal state
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot approve: {workflow_type} workflow for {episode_id}/{shot_id} "
                           f"already in terminal state ({status.name}). "
                           "The workflow must be running to accept approval signals."
                )
        except HTTPException:
            raise  # Re-raise our own 409
        except Exception as desc_err:
            # If describe fails, try to send signal anyway (might work)
            pass
        
        # Send signal to ShotWorkflow (both canary and episode shots use ShotWorkflow)
        await handle.signal("stills_approved")
        
        # Audit log
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO audit_log (episode_id, action, details, user)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, "approve_still", 
                 f"Shot {shot_id} still approved ({workflow_type} workflow)", "admin")
            )
            await db.commit()
        
        return {
            "success": True, 
            "shot_id": shot_id, 
            "workflow_type": workflow_type,
            "message": "Still approved"
        }
    
    except HTTPException:
        raise  # Re-raise our HTTPExceptions (404, 409)
    except Exception as e:
        error_str = str(e).lower()
        if "not found" in error_str or "does not exist" in error_str:
            raise HTTPException(
                status_code=404,
                detail=f"Canary workflow not found for {episode_id}/{shot_id}. "
                       "Has the canary been started?"
            )
        raise HTTPException(status_code=500, detail=f"Error approving still: {str(e)}")


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/reject")
async def reject_still(
    episode_id: str,
    shot_id: str,
    reason: str = Form(...),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Reject still for a shot.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Audit log
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "reject_still", f"Shot {shot_id} still rejected: {reason}", "admin")
        )
        await db.commit()
    
    return {"success": True, "shot_id": shot_id, "message": "Still rejected"}


"""
Clip approve route removed - ShotWorkflow doesn't wait for clip_approved signal.
The clip_approved handler (shot.py:46-48) is a no-op pass statement.
To re-enable this route:
1. Add workflow.wait_condition in ShotWorkflow after clip generation
2. Add self.clip_approved_flag similar to stills_approved
3. Uncomment the route below and update tests
See verification doc § "Clip-approve semantics" for details.
"""
# @app.post("/api/episodes/{episode_id}/clips/{shot_id}/approve")
# async def approve_clip(...): ...
# (83 lines removed - see git history to restore)



@app.get("/api/episodes/{episode_id}/gates")
async def get_gates(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get gate status for episode.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    import aiosqlite
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Episode not found")
            
            live_mode, g108_approved = row
    
    # Return flat structure (not nested) for UI compatibility
    return {
        "episode_id": episode_id,
        "live_mode": bool(live_mode),
        "g108_approved": bool(g108_approved),
    }


@app.get("/api/episodes/{episode_id}/audit")
async def get_audit_trail(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get audit trail for episode.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    import aiosqlite
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            """SELECT id, episode_id, action, details, user, timestamp
               FROM audit_log WHERE episode_id = ? ORDER BY timestamp DESC LIMIT 100""",
            (episode_id,)
        ) as cursor:
            entries = []
            async for row in cursor:
                entries.append({
                    "id": row[0],
                    "episode_id": row[1],
                    "action": row[2],
                    "details": row[3],
                    "user": row[4],
                    "timestamp": row[5],
                })
    
    # Return as .entries for UI compatibility (not .audit_log)
    return {
        "episode_id": episode_id,
        "entries": entries,
    }


@app.post("/api/episodes/{episode_id}/set-live")
async def set_live_mode_endpoint(
    episode_id: str,
    request: SetLiveModeRequest,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Switch episode to live (paid) mode with confirmation.
    
    Requires: Cookie auth + confirmation string "ENABLE_LIVE_MODE"
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    if request.confirmation != "ENABLE_LIVE_MODE":
        raise HTTPException(
            status_code=400,
            detail="Must provide confirmation='ENABLE_LIVE_MODE' to enable live mode"
        )
    
    # Set live mode in database
    await db_set_live_mode(DATABASE_PATH, episode_id, True, user="admin")
    
    return {
        "success": True,
        "episode_id": episode_id,
        "live_mode": True,
        "message": "Episode switched to LIVE mode. Paid generation enabled.",
    }


@app.post("/api/episodes/{episode_id}/set-dry")
async def set_dry_mode_endpoint(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Switch episode to dry-run mode (disable paid generation).
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Set live mode to false in database
    await db_set_live_mode(DATABASE_PATH, episode_id, False, user="admin")
    
    return {
        "success": True,
        "episode_id": episode_id,
        "live_mode": False,
        "message": "Episode switched to DRY-RUN mode. Paid generation disabled.",
    }


@app.post("/api/episodes/{episode_id}/approve-g108")
async def approve_g108_endpoint(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Approve G1.08 credit plan for episode.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Approve in database
    await approve_g108(DATABASE_PATH, episode_id, user="admin")
    
    # Also send signal to workflow if running
    if temporal_client:
        try:
            workflows = temporal_client.list_workflows(f'WorkflowId STARTS_WITH "{episode_id}-"')
            async for workflow_info in workflows:
                handle = temporal_client.get_workflow_handle(workflow_info.id)
                await handle.signal("approve_g108")
                break
        except Exception:
            pass  # Workflow might not be running yet
    
    return {
        "success": True,
        "episode_id": episode_id,
        "message": "G1.08 credit plan approved",
    }


@app.get("/api/canary/{workflow_id}")
async def get_canary_status(
    workflow_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get canary workflow status.
    
    Requires: Cookie auth
    
    Returns:
        - status: running, completed, failed, canceled
        - result: workflow result if completed
    """
    await verify_admin_cookie(studio_admin_token)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    from hfvg.workflows.shot import ShotWorkflow
    from temporalio.client import WorkflowFailureError
    
    try:
        handle = temporal_client.get_workflow_handle_for(
            ShotWorkflow.run,
            workflow_id=workflow_id,
        )
        
        # Check if workflow is still running
        try:
            result = await asyncio.wait_for(handle.result(), timeout=0.1)
            
            # Workflow completed: release L6_reserve (it was already spent via L2/L4)
            await _reconcile_canary_l6(workflow_id, "completed")
            
            return {
                "workflow_id": workflow_id,
                "status": "completed",
                "result": result,
            }
        except asyncio.TimeoutError:
            return {
                "workflow_id": workflow_id,
                "status": "running",
            }
        except WorkflowFailureError as wf_err:
            # Workflow failed: release L6_reserve (no spend occurred)
            await _reconcile_canary_l6(workflow_id, "failed")
            
            return {
                "workflow_id": workflow_id,
                "status": "failed",
                "error": str(wf_err),
            }
    except Exception as e:
        error_str = str(e).lower()
        if "not found" in error_str or "does not exist" in error_str:
            raise HTTPException(status_code=404, detail=f"Canary workflow {workflow_id} not found")
        raise HTTPException(status_code=500, detail=f"Error getting canary status: {str(e)}")


async def _reconcile_canary_l6(workflow_id: str, status: str):
    """
    Reconcile L6_reserve after canary completes or fails.
    Atomic and idempotent: uses transaction to ensure exactly-once reconciliation.
    
    Looks up the workflow in audit log, releases the reservation exactly once.
    """
    import aiosqlite
    from hfvg.budget import BudgetLedger
    import re
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # BEGIN IMMEDIATE for atomic check-and-insert
        await db.execute("BEGIN IMMEDIATE")
        
        try:
            # Find the canary start entry to get episode_id and reserved amount
            async with db.execute(
                """SELECT episode_id, details FROM audit_log 
                   WHERE action = 'start_canary' AND details LIKE ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (f"%{workflow_id}%",)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await db.rollback()
                    return  # No reservation found, nothing to reconcile
                
                episode_id, details = row
            
            # Check if already reconciled (within same transaction)
            async with db.execute(
                """SELECT COUNT(*) FROM audit_log 
                   WHERE episode_id = ? AND action = 'reconcile_canary_l6' 
                   AND details LIKE ?""",
                (episode_id, f"%{workflow_id}%")
            ) as check_cursor:
                already_reconciled = (await check_cursor.fetchone())[0] > 0
            
            if already_reconciled:
                await db.rollback()
                return  # Already reconciled in another concurrent call
            
            # Extract reserved amount from details (e.g., "reserved L6=10.0")
            match = re.search(r"reserved L6=([\d.]+)", details)
            if not match:
                await db.rollback()
                return
            
            reserved_amount = float(match.group(1))
            
            # Record reconciliation in audit log FIRST (within transaction)
            # This acts as a lock - only one transaction can succeed
            await db.execute(
                """INSERT INTO audit_log (episode_id, action, details, user)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, "reconcile_canary_l6", 
                 f"Released L6={reserved_amount} for {workflow_id} ({status})", "system")
            )
            
            # Commit the audit log entry before releasing
            # This ensures the reconciliation is recorded even if release fails
            await db.commit()
            
        except Exception as e:
            await db.rollback()
            raise
    
    # Release the L6 reservation outside the audit log transaction
    # The ledger has its own atomic transaction
    try:
        ledger = BudgetLedger(DATABASE_PATH)
        await ledger.init_db()
        await ledger.release(
            episode_id=episode_id,
            line_name="L6_reserve",
            amount=reserved_amount,
            reason=f"Canary {workflow_id} {status}"
        )
    except Exception as release_err:
        # Log error but don't raise - reconciliation is already marked as done
        print(f"Warning: Reconciliation logged but release failed: {release_err}")


@app.post("/api/episodes/{episode_id}/canary")
async def run_canary(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Run canary test: 1 still + 1 clip through real ShotWorkflow (async).
    
    In dry-run mode: uses fake providers (no spend, fake URLs).
    In live mode: requires live_mode=true AND g108_approved=true.
    
    Returns immediately with workflow_id. Poll /api/canary/{workflow_id} for status.
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    import aiosqlite
    
    # Check live mode and G1.08
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Episode not found")
            
            live_mode, g108_approved = row
    
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Always enforce G1.08 check (even in dry run)
    if not g108_approved:
        return {
            "success": False,
            "message": "G1.08 credit plan not approved. Canary refused.",
            "dry_run": dry_run,
            "live_mode": bool(live_mode),
            "g108_approved": bool(g108_approved),
        }
    
    # In live mode (not dry run), also enforce live_mode gate
    if not dry_run and not live_mode:
        return {
            "success": False,
            "message": "Live mode not enabled. Canary refused.",
            "dry_run": dry_run,
            "live_mode": bool(live_mode),
            "g108_approved": bool(g108_approved),
        }
    
    # Load first shot from parsed beatmap for canary
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT shot_id, prompt FROM shots WHERE episode_id = ? ORDER BY shot_id LIMIT 1",
            (episode_id,)
        ) as cursor:
            shot_row = await cursor.fetchone()
            if not shot_row:
                raise HTTPException(
                    status_code=400,
                    detail="No shots found. Upload beatmap first."
                )
            
            first_shot_id, stored_prompt = shot_row
    
    # Load beatmap to get full shot data for prompt building
    beatmap_path = Path("./data/episodes") / episode_id / "BEATMAP.md"
    episode_path = Path("./data/episodes") / episode_id
    
    if beatmap_path.exists():
        # Load prompt_kit.json (required for live mode, optional for dry mode)
        from hfvg.continuity_parser import load_prompt_kit, build_prompt_with_continuity, get_character_refs
        
        prompt_kit = load_prompt_kit(episode_path)
        
        # In live mode, prompt_kit is REQUIRED
        if not dry_run and not prompt_kit:
            raise HTTPException(
                status_code=400,
                detail=f"Live mode requires prompt_kit.json for {episode_id}. "
                       "Create data/episodes/{episode_id}/prompt_kit.json with style, characters, aspect_ratio, and sets. "
                       "See docs for format."
            )
        
        # Load continuity notes as fallback (for dry mode only)
        continuity_path = episode_path / "CONTINUITY.md"
        continuity = {"characters": {}, "sets": {}, "lighting": {}, "style": ""}
        
        if continuity_path.exists():
            from hfvg.continuity_parser import parse_continuity
            continuity = parse_continuity(continuity_path)
        
        from hfvg.episode_parser import parse_beatmap
        
        shots = parse_beatmap(str(beatmap_path))
        first_shot_data = next((s for s in shots if s["shot_id"] == first_shot_id), None)
        
        if first_shot_data:
            # Build prompt from prompt_kit (preferred) or continuity (fallback)
            # In live mode, this will raise ValueError if character codes are unresolved
            try:
                prompt = build_prompt_with_continuity(
                    first_shot_data, 
                    continuity, 
                    prompt_kit=prompt_kit
                )
            except ValueError as e:
                # Fail closed: unresolved character codes in live mode
                if not dry_run:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Prompt building failed (fail closed): {str(e)}"
                    )
                # In dry mode, allow it but warn
                prompt = build_prompt_with_continuity(first_shot_data, continuity, prompt_kit=None)
            
            # Get aspect ratio from prompt_kit (default to 9:16)
            aspect_ratio = "9:16"
            if prompt_kit:
                aspect_ratio = prompt_kit.get("aspect_ratio", "9:16")
            
            # Load character reference images
            refs = []
            if prompt_kit:
                # Get refs from prompt_kit
                refs = get_character_refs(first_shot_data, prompt_kit, episode_path)
                
                # In live mode, refs must be uploaded to Higgsfield (not local paths)
                # For now, we'll document this requirement and refuse local paths in live mode
                if not dry_run and refs:
                    raise HTTPException(
                        status_code=501,
                        detail="Live mode with character refs requires Higgsfield media upload (not yet implemented). "
                               "Upload refs via Higgsfield's documented media upload mechanism and update prompt_kit.json "
                               "to reference the returned URLs. See docs/PROVIDERS.md for upload endpoint."
                    )
            elif continuity:
                # Legacy: try to find refs by character code (dry mode only)
                refs_dir = episode_path / "refs"
                if refs_dir.exists():
                    characters = first_shot_data.get("characters", [])
                    for char_code in characters[:3]:  # Limit to 3 refs
                        ref_files = list(refs_dir.glob(f"{char_code}.*"))
                        if ref_files:
                            refs.append(str(ref_files[0]))
        else:
            # Beatmap exists but shot not found - use stored prompt
            prompt = stored_prompt or f"Shot {first_shot_id}"
            refs = []
            aspect_ratio = "9:16"
    else:
        # No beatmap file - use stored prompt from database
        prompt = stored_prompt or f"Shot {first_shot_id}"
        refs = []
        aspect_ratio = "9:16"
    
    # Create canary shot with full prompt, refs, and aspect_ratio
    canary_shot = {
        "shot_id": first_shot_id,
        "prompt": prompt,
        "refs": refs,
        "params": {
            "duration": 5.0,
            "aspect_ratio": aspect_ratio,
        },
    }
    
    # Reserve from L6_reserve BEFORE starting workflow (canary budget)
    # Size the hold from provider estimates (still + clip) with a margin
    from hfvg.budget import BudgetLedger
    
    ledger = BudgetLedger(DATABASE_PATH)
    await ledger.init_db()
    
    # In dry-run mode, use default estimate
    # In live mode, get real estimates from providers (fail closed if unavailable)
    higgsfield_key = os.getenv("HIGGSFIELD_API_KEY", "")
    
    if dry_run:
        # Dry-run mode: use default estimate
        canary_cost = 10.0
    elif not higgsfield_key:
        # Live mode but no API key: fail closed (refuse canary)
        raise HTTPException(
            status_code=503,
            detail="Live canary requires HIGGSFIELD_API_KEY to estimate costs. "
                   "Cannot proceed without cost estimation. Set DRY_RUN=true for testing without keys."
        )
    else:
        # Live mode: get real estimates from providers
        from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider
        
        # Estimate still cost (with aspect_ratio but no refs for now - refs may not be uploaded yet)
        still_provider = HiggsfieldStillProvider()
        try:
            still_estimate = await still_provider.estimate_cost(
                prompt=prompt,
                resolution="1k",
                quality="medium",
                aspect_ratio="9:16",
            )
        except Exception as e:
            # Fail closed if we can't get estimate in live mode
            raise HTTPException(
                status_code=503,
                detail=f"Failed to estimate still cost in live mode (fail closed): {str(e)}"
            )
        finally:
            await still_provider.close()
        
        # Estimate clip cost (use placeholder image URL since still doesn't exist yet)
        clip_provider = KlingVideoProvider()
        try:
            clip_estimate = await clip_provider.estimate_cost(
                image_url="https://example.com/placeholder.jpg",
                prompt=prompt,
                duration=5,
            )
        except Exception as e:
            # Fail closed if we can't get estimate in live mode
            raise HTTPException(
                status_code=503,
                detail=f"Failed to estimate clip cost in live mode (fail closed): {str(e)}"
            )
        finally:
            await clip_provider.close()
        
        # Size L6 hold with 20% margin for safety
        canary_cost = (still_estimate + clip_estimate) * 1.2
    
    try:
        reserved = await ledger.reserve(episode_id, "L6_reserve", canary_cost, "Canary test")
        if not reserved:
            return {
                "success": False,
                "error": "L6_reserve budget insufficient for canary",
                "episode_id": episode_id,
                "estimated_cost": canary_cost,
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to reserve canary budget: {str(e)}",
            "episode_id": episode_id,
        }
    
    # Start ShotWorkflow (async) with reservation info
    from hfvg.workflows.shot import ShotWorkflow
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.client import WorkflowExecutionStatus
    import uuid
    
    # Check if there's already a running OR completed canary for this episode
    # Use deterministic workflow ID based on episode to enforce one-at-a-time
    workflow_id = f"{episode_id}-canary-{first_shot_id}"
    
    # Check if workflow exists (running or completed)
    try:
        existing_handle = temporal_client.get_workflow_handle(workflow_id)
        # Try to get the workflow description to see if it exists and its status
        desc = await existing_handle.describe()
        status = desc.status
        
        if status == WorkflowExecutionStatus.RUNNING:
            # Workflow is currently running - return 409 and release reservation
            try:
                await ledger.release(episode_id, "L6_reserve", canary_cost, "Canary already running (409)")
            except Exception as release_err:
                print(f"Failed to release L6 on 409: {release_err}")
            
            raise HTTPException(
                status_code=409,
                detail=f"Canary workflow already running for {episode_id}. "
                       f"Wait for the current canary to complete before starting a new one."
            )
        elif status in (WorkflowExecutionStatus.COMPLETED, 
                       WorkflowExecutionStatus.FAILED, 
                       WorkflowExecutionStatus.CANCELED,
                       WorkflowExecutionStatus.TERMINATED,
                       WorkflowExecutionStatus.TIMED_OUT):
            # Workflow already completed - refuse re-run with 409
            # (Policy: one canary per episode, no re-runs after completion)
            try:
                await ledger.release(episode_id, "L6_reserve", canary_cost, "Canary already completed (409)")
            except Exception as release_err:
                print(f"Failed to release L6 on 409: {release_err}")
            
            raise HTTPException(
                status_code=409,
                detail=f"Canary workflow already completed for {episode_id} (status: {status.name}). "
                       f"Only one canary per episode is allowed. "
                       f"To run another canary, use a different shot or reset the episode."
            )
    except HTTPException:
        # Re-raise our 409 HTTPExceptions - don't swallow them
        raise
    except Exception:
        # Workflow doesn't exist or error getting handle - proceed to create
        pass
    
    try:
        handle = await temporal_client.start_workflow(
            ShotWorkflow.run,
            args=[episode_id, canary_shot],
            id=workflow_id,
            task_queue="hfvg-tasks",
        )
    except WorkflowAlreadyStartedError:
        # Workflow already running - return 409 and release the new reservation
        try:
            await ledger.release(episode_id, "L6_reserve", canary_cost, "Canary already running (409)")
        except Exception as release_err:
            print(f"Failed to release L6 on 409: {release_err}")
        
        raise HTTPException(
            status_code=409,
            detail=f"Canary workflow already running for {episode_id}. "
                   f"Wait for the current canary to complete before starting a new one."
        )
    except Exception as e:
        # Start failed: release L6 reservation
        try:
            await ledger.release(episode_id, "L6_reserve", canary_cost, f"Canary start failed: {str(e)[:100]}")
        except Exception as release_err:
            # Log but don't hide the original error
            print(f"Failed to release L6 on start failure: {release_err}")
        
        raise HTTPException(status_code=500, detail=f"Canary failed to start: {str(e)}")
    
    # Store canary workflow_id and reservation in audit log
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "start_canary", f"Workflow {workflow_id}, reserved L6={canary_cost}", "system")
        )
        await db.commit()
    
    # Return immediately with workflow_id (async)
    # Include composed prompt, aspect_ratio, and refs BEFORE any spend
    return {
        "success": True,
        "episode_id": episode_id,
        "shot_id": first_shot_id,
        "workflow_id": workflow_id,
        "reserved_amount": canary_cost,
        "composed_prompt": prompt,  # Full prompt with resolved character descriptions
        "aspect_ratio": aspect_ratio,  # From prompt_kit or default 9:16
        "refs": refs,  # Character reference paths/URLs
        "dry_run": dry_run,
        "live_mode": bool(live_mode),
        "g108_approved": bool(g108_approved),
        "message": "Canary started (async). Poll workflow for status.",
        "budget_reserved": reserved,
    }


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
