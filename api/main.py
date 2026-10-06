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
- POST /api/episodes/{episode_id}/clips/{clip_id}/approve: Approve clip
- POST /api/episodes/{episode_id}/clips/{clip_id}/reject: Reject clip
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
):
    """
    Get current episode workflow state.
    
    Requires: Authorization header with admin secret.
    """
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
    authorization: str | None = Header(None),
):
    """
    Get budget status for an episode.
    
    Requires: Authorization header with admin secret.
    """
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
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Send signal to shot workflow
    workflow_id = f"{episode_id}-shot-{shot_id}"
    
    try:
        # Import workflow type for exact targeting
        from hfvg.workflows.shot import ShotWorkflow
        
        handle = temporal_client.get_workflow_handle_for(
            ShotWorkflow.run,
            workflow_id=workflow_id,
        )
        await handle.signal("stills_approved")
        
        # Audit log
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO audit_log (episode_id, action, details, user)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, "approve_still", f"Shot {shot_id} still approved", "admin")
            )
            await db.commit()
        
        return {"success": True, "shot_id": shot_id, "message": "Still approved"}
    
    except Exception as e:
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


@app.post("/api/episodes/{episode_id}/clips/{shot_id}/approve")
async def approve_clip(
    episode_id: str,
    shot_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Approve clip for a shot (no signal needed - just logs approval).
    
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
            (episode_id, "approve_clip", f"Shot {shot_id} clip approved", "admin")
        )
        await db.commit()
    
    return {"success": True, "shot_id": shot_id, "message": "Clip approved"}


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
            
            first_shot_id, first_prompt = shot_row
    
    # Create canary shot using first real shot from episode
    canary_shot = {
        "shot_id": first_shot_id,
        "prompt": first_prompt,
        "refs": [],
        "params": {"duration": 5.0},
    }
    
    # Start ShotWorkflow (async)
    from hfvg.workflows.shot import ShotWorkflow
    import time
    
    workflow_id = f"{episode_id}-canary-{first_shot_id}-{int(time.time())}"
    
    try:
        handle = await temporal_client.start_workflow(
            ShotWorkflow.run,
            args=[episode_id, canary_shot],
            id=workflow_id,
            task_queue="hfvg-tasks",
        )
        
        # Charge ledger for canary (record as L6_reserve)
        from hfvg.budget import BudgetLedger
        ledger = BudgetLedger(DATABASE_PATH)
        await ledger.init_db()
        
        # Reserve from L6_reserve (canary budget)
        canary_cost = 10.0  # Estimated: 1 still + 1 clip
        reserved = await ledger.reserve(episode_id, "L6_reserve", canary_cost, "Canary test")
        
        # Return immediately with workflow_id (async)
        return {
            "success": True,
            "episode_id": episode_id,
            "shot_id": first_shot_id,
            "workflow_id": workflow_id,
            "dry_run": dry_run,
            "live_mode": bool(live_mode),
            "g108_approved": bool(g108_approved),
            "message": "Canary started (async). Poll workflow for status.",
            "budget_reserved": reserved,
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Canary failed to start: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
