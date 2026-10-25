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
- POST /api/episodes/{episode_id}/shots/{shot_id}/reject: Reject still (G2, per shot)
- POST /api/episodes/{episode_id}/shots/{shot_id}/retry-clip: Bounded clip retry after G2
- GET /api/episodes/{episode_id}/gates: Get gate status
- GET /api/episodes/{episode_id}/budget: Get budget status
- GET /api/episodes/{episode_id}/audit: Get audit trail
- POST /api/episodes/{episode_id}/canary: Run canary (1 still + 1 clip)
- POST /api/episodes/{episode_id}/set-live: Switch to live mode (requires confirmation)
"""

import os
import sys
import asyncio
import json
import hashlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Cookie, Response, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from hfvg.studio_db import init_studio_db, create_episode, set_live_mode as db_set_live_mode, approve_g108, insert_shots_from_beatmap
from hfvg.budget import BudgetLedger
from hfvg.pricing import DRY_CLIP_USD_PER_SECOND_MICROS, DRY_STILL_USD_MICROS
from hfvg.episode_parser import parse_beatmap
from hfvg.credit_plan_parser import parse_credit_plan
from hfvg.temporal_converter import temporal_data_converter
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider

# Get ADMIN_SECRET - can be overridden in tests
def get_admin_secret() -> str:
    """Get admin secret from environment. Can be overridden in tests."""
    secret = os.getenv("ADMIN_SECRET", "")
    if not secret or len(secret) < 32:
        raise ValueError(
            "ADMIN_SECRET environment variable is required and must be at least 32 characters. "
            "NO DEFAULT SECRET. Generate with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return secret

# Check ADMIN_SECRET early - FAIL CLOSED (unless in test mode)
try:
    ADMIN_SECRET = get_admin_secret()
except ValueError as e:
    # Allow tests to set it later
    if "pytest" not in sys.modules:
        raise
    ADMIN_SECRET = ""

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
    """Initialize database and Temporal client on startup."""
    global temporal_client
    
    try:
        # Initialize database tables
        await init_studio_db(DATABASE_PATH)
        
        # Connect to Temporal
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

# CORS middleware (configurable origins)
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:3001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in CORS_ORIGINS],
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
    current_secret = get_admin_secret()
    
    if not secrets.compare_digest(token, current_secret):
        raise HTTPException(
            status_code=403,
            detail="Invalid admin secret"
        )


async def verify_auth_unified(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Unified auth: accept either Bearer token OR session cookie.
    
    This allows both programmatic access (Bearer token) and browser access (cookie).
    Fail closed: at least one must be valid.
    """
    # Try cookie first (browser access)
    if studio_admin_token:
        try:
            await verify_admin_cookie(studio_admin_token)
            return  # Cookie is valid
        except HTTPException:
            pass  # Fall through to try Bearer token
    
    # Try Bearer token (programmatic access)
    if authorization:
        try:
            verify_admin_secret(authorization)
            return  # Bearer token is valid
        except HTTPException:
            pass
    
    # Neither worked
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide either session cookie or 'Authorization: Bearer <ADMIN_SECRET>'"
    )


# Request/Response models
def episode_workflow_id(episode_id: str) -> str:
    """Temporal workflow id of the episode workflow: the bare episode id (``ep04``).

    Also accepts the ``ep04-episode`` style used by clients. Starting and looking up an episode
    workflow MUST both go through this function (child shot workflows are ``ep04-shot-<id>``).
    """
    return episode_id[: -len("-episode")] if episode_id.endswith("-episode") else episode_id


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
    dry_run: bool = Field(True, description="Run in dry-run mode (default: true, live requires explicit false)")
    shots_subset: list[str] | None = Field(None, description="Subset of shot IDs to run (e.g., ['A01', 'A02'] for 2-shot canary)")
    credit_cap: int | None = Field(None, description="Credit cap for this run (overrides episode line cap)")
    attempt_overrides: dict[str, int] | None = Field(None, description="Shot ID -> attempt number overrides (e.g., {'A02': 2} starts A02 as first retry)")


class StartEpisodeResponse(BaseModel):
    workflow_id: str
    run_id: str
    episode_id: str
    message: str


class StartCanaryRequest(BaseModel):
    episode_id: str
    shots_subset: list[str] | None = None
    credit_cap: int = 100


class ApprovalRequest(BaseModel):
    gate_id: str = Field(..., description="Gate ID to approve (e.g., g101, g103, g108, g212, g409)")
    note: str | None = Field(None, description="Optional approval note")
    override_unavailable_estimates: bool = Field(False, description="Override unavailable estimates for G1 approval in live mode")


class EpisodeStateResponse(BaseModel):
    episode_id: str
    stage: str | None
    approvals: dict[str, bool]
    shots_count: int
    estimates_available: bool | None = None  # From G1 plan - None if G1 not yet computed
    active_stall_alerts: int = 0  # Stall watchdog alerts not yet resolved (GET .../stalls)
    # Mute picture edit: {"path", "manifest", "qc": {"verdict", "blocking", "reason", ...},
    # "acceptance", "blocking", "stale", "edit_version"}; None before the clips finished
    assembly: dict | None = None
    assembly_path: str | None = None
    # Audio + delivery: {"paths": {mix_wav, master_subs, nosubs, phone, ass, srt, manifest, stems...},
    # "manifest", "qc": {"verdict", "blocking", "checks", ...}, "acceptance", "blocking", "due"}
    delivery: dict | None = None
    delivery_paths: dict | None = None


class AssemblyAcceptRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the blocking mute-edit QC verdict is accepted")


class DeliveryAcceptRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the blocking delivery QC verdict is accepted")


class ShotStatus(BaseModel):
    shot_id: str
    status: str
    prompt: str | None
    still_url: str | None
    clip_url: str | None
    qc_results: dict | None
    retries: int


class BudgetLineStatus(BaseModel):
    """One budget line. All amounts are integer usd_micros (1 USD = 1_000_000)."""
    line_name: str
    provider: str
    spent: int
    reserved: int
    total: int
    cap: int
    stop: int
    at_stop: bool
    unit: str = "usd_micros"
    revision_reserve: bool = False


class BudgetResponse(BaseModel):
    """Episode budget in integer usd_micros; *_usd fields are exact decimal strings."""
    episode_id: str
    unit: str = "usd_micros"
    lines: list[BudgetLineStatus]
    higgsfield_total: int
    elevenlabs_total: int
    episode_total: int
    episode_cap: int
    episode_stop: int
    gc01_headroom: int
    episode_total_usd: str
    episode_cap_usd: str
    episode_stop_usd: str
    gc01_headroom_usd: str
    balance: dict


class ManualBalanceRequest(BaseModel):
    balance_usd: str | float | int = Field(..., description="Current Higgsfield USD balance as decimal string (e.g. '82.40') or numeric value")
    note: str = Field("", max_length=500)


class JobVerdictRequest(BaseModel):
    verdict: str = Field(..., max_length=100)
    used: bool | None = None


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


@app.get("/gates")
async def serve_gates_page():
    """Serve the gates approval UI page."""
    gates_path = Path(__file__).parent.parent / "web" / "gates.html"
    if not gates_path.exists():
        raise HTTPException(status_code=404, detail="Gates page not found")
    return FileResponse(gates_path)


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
    # Read from env at request time to support test monkeypatching
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret or len(admin_secret) < 32:
        raise HTTPException(status_code=500, detail="Server not configured: ADMIN_SECRET required")
    
    if not request.admin_secret or request.admin_secret != admin_secret:
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
    beatmap_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id
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
    episode_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id
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


@app.post("/api/episodes/{episode_id}/upload-shots")
async def upload_shots(
    episode_id: str,
    shots_file: UploadFile = File(...),
    refs_file: UploadFile = File(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Upload the private per-shot data: shots.json (required) and refs.json (optional).

    Requires: Cookie auth. Stored in data/episodes/{episode_id}/ (gitignored, never committed).
    shots.json is validated (count, characters, wet, model, duration, end_frame, prompt, ref_keys);
    the workflow loads it (or SHOTS_JSON_PATH / REFS_JSON_PATH) and refuses live runs without it.
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    from hfvg.shot_data import ShotDataError, parse_shots

    shots_bytes = await shots_file.read()
    try:
        plans = parse_shots(json.loads(shots_bytes))
    except (ValueError, ShotDataError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid shots.json: {e}")
    refs_bytes = None
    if refs_file:
        refs_bytes = await refs_file.read()
        try:
            refs_data = json.loads(refs_bytes)
            if not isinstance(refs_data.get("per_shot"), dict):
                raise ValueError("missing 'per_shot' object")
            
            # Validate that all shots in shots.json have entries in refs.json per_shot
            per_shot = refs_data["per_shot"]
            # parse_shots returns either a list or dict depending on format
            if isinstance(plans, dict):
                shot_ids = set(plans.keys())
            else:
                shot_ids = {plan["shot_id"] for plan in plans}
            missing_shots = shot_ids - per_shot.keys()
            if missing_shots:
                raise ValueError(
                    f"refs.json missing per_shot entries for shots present in shots.json: {sorted(missing_shots)}"
                )
        except (ValueError, AttributeError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid refs.json: {e}")

    episode_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "shots.json").write_bytes(shots_bytes)
    if refs_bytes is not None:
        (episode_dir / "refs.json").write_bytes(refs_bytes)
    
    # Count shots (plans can be dict or list)
    shots_count = len(plans) if isinstance(plans, (list, dict)) else 0
    
    return {
        "success": True,
        "episode_id": episode_id,
        "shots_count": shots_count,
        "refs_uploaded": refs_bytes is not None,
    }


@app.post("/api/episodes", response_model=StartEpisodeResponse)
async def start_episode(
    request: StartEpisodeRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Start a new episode workflow.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(request.episode_id)
    
    # Initialize studio DB if not exists
    await init_studio_db(DATABASE_PATH)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Save beatmap content if provided
    beatmap_path: str | None = None
    episodes_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes"))
    if request.beatmap_content:
        beatmap_dir = episodes_dir / request.episode_id
        beatmap_dir.mkdir(parents=True, exist_ok=True)
        beatmap_path = str(beatmap_dir / "BEATMAP.md")
        
        with open(beatmap_path, "w") as f:
            f.write(request.beatmap_content)
    else:
        # No beatmap_content provided - check if BEATMAP.md exists in the episode directory
        beatmap_dir = episodes_dir / request.episode_id
        potential_beatmap = beatmap_dir / "BEATMAP.md"
        if potential_beatmap.exists():
            beatmap_path = str(potential_beatmap)
        else:
            # No beatmap content and no uploaded BEATMAP.md - error clearly
            # Never fall back to test fixtures in production code
            raise HTTPException(
                status_code=400,
                detail=f"No beatmap found for episode {request.episode_id}. "
                       f"Either provide beatmap_content in the request or upload BEATMAP.md via "
                       f"POST /api/episodes/upload before starting the episode."
            )
    
    # Start workflow
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    # Stable workflow ID for approve route to find
    workflow_id = episode_workflow_id(request.episode_id)
    
    handle = await temporal_client.start_workflow(
        EpisodeWorkflowV2.run,
        args=[request.episode_id, beatmap_path, request.dry_run, request.shots_subset, request.credit_cap, request.attempt_overrides],
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
    
    episode_id = episode_workflow_id(episode_id)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # The episode workflow id is the episode id itself (same as start_episode); do NOT prefix-match,
    # that would only ever find child shot workflows ("ep04-shot-A01").
    workflow_id = episode_workflow_id(episode_id)
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query("get_state")
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error querying workflow: {str(e)}")
    
    from hfvg.activities.reconcile import list_stall_alerts
    try:
        active_stalls = [a for a in await list_stall_alerts(DATABASE_PATH, state["episode_id"])
                         if a["status"] != "resolved"]
    except Exception as e:  # the workflow state stays readable even if the DB is unavailable
        print(f"Stall alerts unavailable for {state['episode_id']}: {e}")
        active_stalls = []
    return EpisodeStateResponse(
        episode_id=state["episode_id"],
        stage=state.get("stage"),
        approvals=state.get("approvals", {}),
        shots_count=state.get("shots", 0),
        estimates_available=state.get("g1_plan", {}).get("estimates_available"),
        active_stall_alerts=len(active_stalls),
        assembly=state.get("assembly"),
        assembly_path=(state.get("assembly") or {}).get("path"),
        delivery=state.get("delivery"),
        delivery_paths=(state.get("delivery") or {}).get("paths") or None,
    )


@app.post("/api/episodes/{episode_id}/assembly/accept")
async def accept_assembly_qc(
    episode_id: str,
    request: AssemblyAcceptRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Human review of a blocking mute-edit QC verdict (FAIL / NEEDS_REVIEW): accept it with a reason
    so G_FINAL_CUT can be approved. The workflow ignores it when nothing is blocking."""
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        await handle.signal("accept_assembly_qc", request.reason.strip())
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error sending acceptance: {str(e)}")
    return {"success": True, "episode_id": episode_id, "message": "Assembly QC acceptance sent"}


@app.post("/api/episodes/{episode_id}/delivery/accept")
async def accept_delivery_qc(
    episode_id: str,
    request: DeliveryAcceptRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Human review of a blocking delivery QC verdict (FAIL / NEEDS_REVIEW): accept it with a reason
    so G_FINAL_CUT can be approved. The workflow ignores it when nothing is blocking."""
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        await handle.signal("accept_delivery_qc", request.reason.strip())
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error sending acceptance: {str(e)}")
    return {"success": True, "episode_id": episode_id, "message": "Delivery QC acceptance sent"}


@app.post("/api/episodes/{episode_id}/approve")
async def approve_gate(
    episode_id: str,
    request: ApprovalRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Send approval signal to episode workflow.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
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
            
            # For G1 approval, use update (with validator) to check estimates availability
            if request.gate_id.lower() in ["g1_shot_plan", "g1"]:
                try:
                    result = await handle.execute_update(
                        signal_name,
                        request.override_unavailable_estimates if request.override_unavailable_estimates else False
                    )
                    return {
                        "success": True,
                        "episode_id": episode_id,
                        "gate_id": request.gate_id,
                        "message": f"Approval sent for gate {request.gate_id}",
                        "note": request.note,
                    }
                except (WorkflowUpdateFailedError, ApplicationError) as e:
                    # Validator refused - return 409 Conflict
                    cause = getattr(e, "cause", None) or e
                    raise HTTPException(
                        status_code=409,
                        detail=f"G1 approval refused: {str(cause)}"
                    )
            elif request.gate_id.lower() == "g2_scene_stills":
                # G2 is a workflow update (validator refuses while a retry is pending).
                # A signal to an update handler is silently dropped, so it MUST be an update.
                try:
                    await handle.execute_update(signal_name)
                except (WorkflowUpdateFailedError, ApplicationError) as e:
                    cause = getattr(e, "cause", None) or e
                    raise HTTPException(
                        status_code=409,
                        detail=f"G2 approval refused: {str(cause)}"
                    )
                return {
                    "success": True,
                    "episode_id": episode_id,
                    "gate_id": request.gate_id,
                    "message": f"Approval sent for gate {request.gate_id}",
                    "note": request.note,
                }
            else:
                # Other gates use signal
                await handle.signal(signal_name)
                return {
                    "success": True,
                    "episode_id": episode_id,
                    "gate_id": request.gate_id,
                    "message": f"Approval sent for gate {request.gate_id}",
                    "note": request.note,
                }
        
        raise HTTPException(status_code=404, detail=f"No running EpisodeWorkflowV2 found for episode {episode_id}")
        
    except HTTPException:
        raise  # keep 404/409 - do not turn them into 500
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
            revision_reserve=line["revision_reserve"],
        )
        for line in summary["lines"]
    ]
    
    from hfvg.pricing import micros_to_usd_str
    
    return BudgetResponse(
        episode_id=episode_id,
        lines=lines,
        higgsfield_total=summary["higgsfield_total"],
        elevenlabs_total=summary["elevenlabs_total"],
        episode_total=summary["episode_total"],
        episode_cap=summary["episode_cap"],
        episode_stop=summary["episode_stop"],
        gc01_headroom=summary["gc01_headroom"],
        episode_total_usd=micros_to_usd_str(summary["episode_total"]),
        episode_cap_usd=micros_to_usd_str(summary["episode_cap"]),
        episode_stop_usd=micros_to_usd_str(summary["episode_stop"]),
        gc01_headroom_usd=micros_to_usd_str(summary["gc01_headroom"]),
        balance=summary["balance"],
    )


@app.get("/api/episodes/{episode_id}/jobs")
async def get_job_ledger(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Per-job ledger for an episode: job id, shot id, model, tier (draft/final/retry/revision),
    usd_micros, status, verdict, used/unused.

    Requires: Authorization header with admin secret OR session cookie.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    validate_episode_id(episode_id)
    
    from hfvg.budget import BudgetLedger
    
    ledger = BudgetLedger(DATABASE_PATH)
    jobs = await ledger.get_jobs(episode_id)
    return {"episode_id": episode_id, "unit": "usd_micros", "jobs": jobs}


async def _stall_alerts_response(episode_id: str | None, status: str | None) -> dict:
    from hfvg.activities.reconcile import list_stall_alerts, stall_threshold_minutes

    if status is not None and status not in ("open", "routed", "resolved"):
        raise HTTPException(status_code=422, detail="status must be open, routed or resolved")
    alerts = await list_stall_alerts(DATABASE_PATH, episode_id, status)
    active = [a for a in alerts if a["status"] != "resolved"]
    return {
        "episode_id": episode_id,
        "threshold_minutes": stall_threshold_minutes(),
        "active_count": len(active),
        "alerts": alerts,
    }


@app.get("/api/stalls")
async def get_stall_alerts(
    status: str | None = None,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Stall watchdog alerts across episodes (jobs with no progress for STALL_THRESHOLD_MINUTES;
    routed to the reconciler). Requires: Authorization header with admin secret OR session cookie."""
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    return await _stall_alerts_response(None, status)


@app.get("/api/episodes/{episode_id}/stalls")
async def get_episode_stall_alerts(
    episode_id: str,
    status: str | None = None,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Stall watchdog alerts for one episode. Requires: admin secret OR session cookie."""
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    validate_episode_id(episode_id)
    return await _stall_alerts_response(episode_id, status)


@app.post("/api/episodes/{episode_id}/jobs/{job_id}/verdict")
async def set_job_verdict(
    episode_id: str,
    job_id: str,
    request: JobVerdictRequest,
    studio_admin_token: str | None = Cookie(None),
):
    """Record the verdict and used/unused flag on a job ledger row. Requires: Cookie auth."""
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    from hfvg.budget import BudgetLedger
    
    ledger = BudgetLedger(DATABASE_PATH)
    jobs = {j["job_id"] for j in await ledger.get_jobs(episode_id)}
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found for {episode_id}")
    await ledger.set_job_verdict(job_id, request.verdict, request.used)
    return {"success": True, "job_id": job_id, "verdict": request.verdict, "used": request.used}


@app.get("/api/budget/balance")
async def get_manual_balance(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GC.01 status: latest manual balance, holds, headroom. Requires admin auth."""
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.budget import BudgetLedger
    
    return await BudgetLedger(DATABASE_PATH).get_balance_status()


@app.post("/api/budget/balance")
async def set_manual_balance(
    request: ManualBalanceRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Record the manually entered Higgsfield USD balance (no balance endpoint exists).
    GC.01 refuses new paid jobs when balance - spend since entry - reserved < headroom.
    Requires: Cookie auth (admin) or Bearer token.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.budget import BudgetLedger
    from hfvg.pricing import usd_to_micros
    
    try:
        # Convert to string if numeric for consistent handling
        balance_value = str(request.balance_usd) if isinstance(request.balance_usd, (int, float)) else request.balance_usd
        micros = usd_to_micros(balance_value)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid balance_usd: {e}")
    
    ledger = BudgetLedger(DATABASE_PATH)
    entry = await ledger.set_manual_balance(micros, set_by="admin", note=request.note)
    await _audit_balance(entry)
    return {"success": True, **entry, "status": await ledger.get_balance_status()}


async def _audit_balance(entry: dict):
    """Audit-log a manual balance entry (balance changes gate every paid job)."""
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            ("_global", "set_manual_balance",
             f"balance_usd_micros={entry['balance_usd_micros']} at {entry['set_at']}", "admin"),
        )
        await db.commit()


@app.get("/api/episodes/{episode_id}/shots")
async def get_shots(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    List all shots for an episode with their status.
    
    Requires: Authorization header OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
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


async def _execute_episode_update(episode_id: str, update: str, args: list, action: str):
    """Run a workflow update on the episode workflow; refusals map to 409, missing workflow to 404."""
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    workflow_id = episode_workflow_id(episode_id)
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        return await handle.execute_update(update, args=args)
    except WorkflowUpdateFailedError as e:
        if e.cause and isinstance(e.cause, ApplicationError):
            raise HTTPException(status_code=409, detail=str(e.cause))
        raise HTTPException(status_code=500, detail=f"{action} failed: {e}")
    except ApplicationError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"{action} failed: {e}")


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/reject")
async def reject_still(
    episode_id: str,
    shot_id: str,
    reason: str = Form(...),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Reject the still of one shot at G2 (G-GATE-1).

    The workflow decides first (reject_shot update; refused -> 409, e.g. not parked at G2 or G2
    already approved). Only an accepted rejection is persisted in studio.db, so the DB can never
    say "rejected" for a shot whose still the workflow will still animate.

    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)

    result = await _execute_episode_update(episode_id, "reject_shot", [shot_id, reason], "Reject")

    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """UPDATE shots 
               SET status = 'rejected', rejection_reason = ?, updated_at = datetime('now')
               WHERE episode_id = ? AND shot_id = ?""",
            (reason, episode_id, shot_id)
        )
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "reject_still", f"Shot {shot_id} still rejected: {reason}", "admin")
        )
        await db.commit()

    return {"success": True, "shot_id": shot_id, "message": "Still rejected", "reason": reason,
            "result": result}


class RetryClipRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the clip is being re-run (audited)")
    prompt_override: str | None = None


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/retry-clip")
async def retry_clip(
    episode_id: str,
    shot_id: str,
    request: RetryClipRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    G-RETRY-1: re-run ONLY the clip of a shot after G2, reusing its approved still.

    Bounded by MAX_PAID_RETRIES_PER_SHOT_PER_ROUND (default 1) and the per-round USD cap, both
    checked before any paid submit; refusals -> 409. The retry has its own deterministic job id.

    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)

    result = await _execute_episode_update(
        episode_id, "retry_clip", [shot_id, request.reason, request.prompt_override], "Clip retry"
    )

    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "retry_clip",
             f"Shot {shot_id} clip retry {result.get('retry')} ({result.get('job_id')}): {request.reason}",
             "admin")
        )
        await db.commit()

    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result,
            "message": f"Clip retry queued for shot {shot_id}"}


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
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get gate status for episode.
    
    Requires: Authorization header OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
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


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/retry")
async def retry_shot(
    episode_id: str,
    shot_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Retry a failed shot (only valid while parked at G1/G2 for a shot in failed_shots).
    
    Re-runs only that shot's still child, respects credit/USD cap and smoke gate,
    enforces MAX_SHOT_RETRIES from config, uses NEW idempotency key per attempt,
    moves shot from failed_shots back to running then to done or failed.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    # Find the episode workflow
    workflow_id = episode_workflow_id(episode_id)
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        
        # Send retry update (returns result or raises ApplicationError on refusal)
        from temporalio.client import WorkflowUpdateFailedError
        from temporalio.exceptions import ApplicationError
        try:
            result = await handle.execute_update(
                "retry_shot",
                shot_id
            )
        except WorkflowUpdateFailedError as e:
            # Update was refused - check if it's an ApplicationError inside
            if e.cause and isinstance(e.cause, ApplicationError):
                # Map workflow refusal to 409 Conflict
                raise HTTPException(status_code=409, detail=str(e.cause))
            raise
        except ApplicationError as e:
            # Direct ApplicationError (shouldn't happen with execute_update, but handle it)
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Retry failed: {str(e)}")
        
        # Audit log
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO audit_log (episode_id, action, details, user)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, "retry_shot", f"Shot {shot_id} retry requested", "admin")
            )
            await db.commit()
        
        return {
            "success": True,
            "episode_id": episode_id,
            "shot_id": shot_id,
            "result": result,
            "message": f"Retry signal sent for shot {shot_id}",
        }
        
    except HTTPException:
        raise  # Re-raise HTTPExceptions without wrapping
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error sending retry signal: {str(e)}")


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
            
            # Extract reserved amount from details (e.g., "reserved L6=408000 usd_micros")
            match = re.search(r"reserved L6=(\d+) usd_micros", details)
            if not match:
                await db.rollback()
                return
            
            reserved_amount = int(match.group(1))
            
            # Record reconciliation in audit log FIRST (within transaction)
            # This acts as a lock - only one transaction can succeed
            await db.execute(
                """INSERT INTO audit_log (episode_id, action, details, user)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, "reconcile_canary_l6", 
                 f"Released L6={reserved_amount} usd_micros for {workflow_id} ({status})", "system")
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
            amount_usd_micros=reserved_amount,
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
    
    canary_shot_meta: dict | None = None  # prompt-lint metadata (set when the beatmap shot is found)
    # Load beatmap to get full shot data for prompt building
    episodes_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes"))
    beatmap_path = episodes_dir / episode_id / "BEATMAP.md"
    episode_path = episodes_dir / episode_id
    
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
            
            # Lint metadata derived from the beatmap shot (count defaults to the character count)
            _chars = list(first_shot_data.get("characters", []))
            canary_shot_meta = {
                "count": first_shot_data.get("count", len(_chars)),
                "exterior": bool(first_shot_data.get("exterior", False)),
                "water": bool(first_shot_data.get("water", False))
                or "wet" in first_shot_data.get("tags", []),
                "characters": _chars,
            }
            
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
    
    # Prompt lint gate (live: refuse before any estimate/reserve/provider call; dry: warn)
    from hfvg.prompt_lint import PromptLintError, enforce_prompt_lint
    
    lint_meta = canary_shot_meta
    try:
        enforce_prompt_lint(episode_id, prompt, lint_meta, live=not dry_run)
    except PromptLintError as e:
        raise HTTPException(status_code=422, detail=f"Canary prompt lint refused: {e}")
    
    # Create canary shot with full prompt, refs, and aspect_ratio
    canary_shot = {
        "shot_id": first_shot_id,
        "prompt": prompt,
        "shot_meta": lint_meta,
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
        # Dry-run mode: fixed estimate (0.06 still + 5 s Kling clip) with 20% margin, usd_micros
        canary_cost = (DRY_STILL_USD_MICROS + 5 * DRY_CLIP_USD_PER_SECOND_MICROS) * 12 // 10
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
            still_estimate = await still_provider.estimate_usd_micros(
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
            clip_estimate = await clip_provider.estimate_usd_micros(
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
        
        # Size L6 hold with 20% margin for safety (integer usd_micros, rounded down by 1 micro max)
        canary_cost = (still_estimate + clip_estimate) * 12 // 10
    
    try:
        reserved = await ledger.reserve(
            episode_id, "L6_reserve", canary_cost, "Canary test", require_balance=not dry_run
        )
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
            (episode_id, "start_canary", f"Workflow {workflow_id}, reserved L6={canary_cost} usd_micros", "system")
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


@app.post("/api/episodes/{episode_id}/smoke-test")
async def run_smoke_test_api(
    episode_id: str,
    live: bool = False,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Run smoke test: upload probe + SOUL V2 generation probe (~0.09 credits).
    
    Must pass live=true query param to actually run (safety).
    Gated stages check for recent passing smoke test before spending.
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    if not live:
        raise HTTPException(
            status_code=400,
            detail="Smoke test requires live=true query param (spends ~0.09 credits)"
        )
    
    from hfvg.activities.smoke_test import run_smoke_test
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
    try:
        result = await run_smoke_test(episode_id, db_path)
        
        # Store result in episode metadata
        from hfvg.studio_db import store_smoke_test_result
        await store_smoke_test_result(db_path, episode_id, result)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Smoke test failed: {e}")


@app.post("/api/episodes/{episode_id}/lint")
async def lint_episode(
    episode_id: str,
    studio_admin_token: str | None = Cookie(None),
):
    """
    Lint episode configuration and beatmap.
    
    Checks:
    - BEATMAP.md exists and is valid
    - shots.json exists (if required)
    - Budget configuration is valid
    - Reference images are accessible
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    issues = []
    warnings = []
    
    # Check BEATMAP.md exists
    beatmap_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id
    beatmap_path = beatmap_dir / "BEATMAP.md"
    if not beatmap_path.exists():
        issues.append(f"BEATMAP.md missing at {beatmap_path}")
    else:
        # Try to parse beatmap
        try:
            from hfvg.episode_parser import parse_beatmap
            shots = parse_beatmap(str(beatmap_path))
            if not shots:
                warnings.append("BEATMAP.md parsed but contains no shots")
        except Exception as e:
            issues.append(f"BEATMAP.md parse error: {str(e)}")
    
    # Check shots.json for live mode
    shots_json = beatmap_dir / "shots.json"
    if not shots_json.exists():
        warnings.append("shots.json missing (required for live mode)")
    else:
        try:
            import json
            from hfvg.shot_data import parse_shots
            plans = parse_shots(json.loads(shots_json.read_text()))
        except Exception as e:
            issues.append(f"shots.json parse error: {str(e)}")
    
    # Check budget configuration
    # Only warn if episode has reached G1.08 approval but has no budget lines
    import aiosqlite
    g108_approved = False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                g108_approved = bool(row[0])
    
    if g108_approved:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM budget_lines WHERE episode_id = ?",
                (episode_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row[0] == 0:
                    warnings.append("No budget lines configured (G1.08 approved but budget not initialized)")
    
    # Check refs.json and reference files
    refs_json = beatmap_dir / "refs.json"
    if refs_json.exists():
        try:
            import json
            refs_data = json.loads(refs_json.read_text())
            per_shot = refs_data.get("per_shot", {})
            missing_refs = []
            
            for shot_id, shot_refs in per_shot.items():
                for ref_entry in shot_refs:
                    if isinstance(ref_entry, dict):
                        ref_path = ref_entry.get("path")
                        if ref_path and not Path(ref_path).exists():
                            missing_refs.append(f"{shot_id}: {ref_path}")
            
            if missing_refs:
                warnings.append(f"{len(missing_refs)} reference files not found: {missing_refs[:3]}")
        except Exception as e:
            issues.append(f"refs.json parse error: {str(e)}")
    
    return {
        "episode_id": episode_id,
        "status": "pass" if not issues else "fail",
        "issues": issues,
        "warnings": warnings,
    }


@app.post("/api/episodes/{episode_id}/set-ref-versions")
async def set_ref_versions(
    episode_id: str,
    versions: dict[str, int],
    studio_admin_token: str | None = Cookie(None),
):
    """
    Set or bump reference image versions in refs.json.
    
    Args:
        versions: Map of ref keys to version numbers (e.g., {"r_duck_sheet": 2})
    
    Requires: Cookie auth
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)
    
    # Load refs.json
    refs_json_path = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "refs.json"
    if not refs_json_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"refs.json not found for episode {episode_id}"
        )
    
    import json
    refs_data = json.loads(refs_json_path.read_text())
    
    # Update versions map
    if "versions" not in refs_data:
        refs_data["versions"] = {}
    
    for ref_key, version in versions.items():
        if not isinstance(version, int) or version < 1:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid version for {ref_key}: {version}. Must be positive integer."
            )
        refs_data["versions"][ref_key] = version
    
    # Write updated refs.json
    refs_json_path.write_text(json.dumps(refs_data, indent=2))
    
    # Write version sidecars for any existing ref files
    per_shot = refs_data.get("per_shot", {})
    updated_sidecars = []
    
    for ref_key, version in versions.items():
        # Find the ref path in per_shot
        ref_path = None
        for shot_refs in per_shot.values():
            for ref_entry in shot_refs:
                if isinstance(ref_entry, dict) and ref_entry.get("key") == ref_key:
                    ref_path = ref_entry.get("path")
                    break
            if ref_path:
                break
        
        if ref_path and Path(ref_path).exists():
            # Write version sidecar
            version_sidecar_dir = Path(ref_path).parent / ".versions"
            version_sidecar_dir.mkdir(parents=True, exist_ok=True)
            version_sidecar = version_sidecar_dir / f"{Path(ref_path).stem}.json"
            version_sidecar.write_text(json.dumps({"version": version, "ref_key": ref_key}))
            updated_sidecars.append(str(version_sidecar))
    
    return {
        "success": True,
        "episode_id": episode_id,
        "versions_updated": versions,
        "sidecars_written": len(updated_sidecars),
        "refs_json_path": str(refs_json_path),
    }


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


@app.post("/api/lint")
async def lint_prompt_endpoint(request: dict):
    """
    Lint a prompt for violations.
    
    Args:
        request: dict with "prompt" (str), optional "shot_meta" (dict), optional "live" (bool), optional "episode_id" (str)
    
    Returns:
        dict with "problems" list
    """
    from hfvg.prompt_lint import lint_violations, load_lint_kit
    
    prompt = request.get("prompt", "")
    shot_meta = request.get("shot_meta", {})
    episode_id = request.get("episode_id", "default")  # Default episode if not specified
    
    kit = load_lint_kit(episode_id)
    problems = lint_violations(prompt, shot_meta, kit)
    
    return {"problems": problems}


@app.post("/api/episodes/{episode_id}/anchor")
async def set_anchor_still(
    episode_id: str,
    anchor_shot_id: str = Body(...),
    target_shot_ids: list[str] = Body(...),
    file_path: str | None = Body(None),
    approved_by: str | None = Body(None),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Set an anchor still as a reference for target shots.
    
    Accepts either:
    1. A G2-approved still (anchor_shot_id with approved status)
    2. An externally supplied file (file_path with explicit approved_by note)
    
    Args:
        episode_id: Episode ID
        anchor_shot_id: Shot ID of the anchor still
        target_shot_ids: List of shot IDs that will use this anchor as ref
        file_path: Optional external file path (requires approved_by)
        approved_by: Required if using external file, optional for G2-approved still
        
    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)
    
    if not target_shot_ids:
        raise HTTPException(status_code=400, detail="target_shot_ids cannot be empty")
    
    # Reject self-anchoring: anchor_shot_id must not be in target_shot_ids
    if anchor_shot_id in target_shot_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot anchor shot {anchor_shot_id} to itself"
        )
    
    import hashlib
    import aiosqlite
    from pathlib import Path
    
    # Determine the anchor still path and validate approval
    if file_path:
        # External file mode - requires explicit approved_by note
        if not approved_by:
            raise HTTPException(
                status_code=400, 
                detail="External file requires explicit --approved-by note"
            )
        
        local_path = Path(file_path)
        if not local_path.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
        
        # Compute SHA256
        sha256_hash = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        sha256 = sha256_hash.hexdigest()
        
        # Use version 0 for external files
        version = 0
        
    else:
        # G2-approved still mode
        # Check that the anchor shot is G2-approved
        async with aiosqlite.connect(DATABASE_PATH) as db:
            async with db.execute(
                "SELECT status, still_url FROM shots WHERE episode_id = ? AND shot_id = ?",
                (episode_id, anchor_shot_id)
            ) as cursor:
                row = await cursor.fetchone()
        
        if not row:
            raise HTTPException(
                status_code=404, 
                detail=f"Anchor shot {anchor_shot_id} not found"
            )
        
        status, still_url = row
        if status != "approved":
            raise HTTPException(
                status_code=400,
                detail=f"Anchor shot {anchor_shot_id} must be G2-approved (current status: {status})"
            )
        
        if not still_url:
            raise HTTPException(
                status_code=400,
                detail=f"Anchor shot {anchor_shot_id} has no still_url"
            )
        
        # Find the local file for this still
        # Pattern: {data_dir}/episodes/{episode_id}/outputs/{shot_id}/still_v{version}.{ext}
        import os
        data_dir = Path(os.getenv("DATA_DIR", "./data"))
        output_dir = data_dir / "episodes" / episode_id / "outputs" / anchor_shot_id
        if not output_dir.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Output directory not found for {anchor_shot_id}"
            )
        
        # Find the latest still version (support .png, .jpg, .jpeg, .webp)
        still_files = (
            list(output_dir.glob("still_v*.png")) +
            list(output_dir.glob("still_v*.jpg")) +
            list(output_dir.glob("still_v*.jpeg")) +
            list(output_dir.glob("still_v*.webp"))
        )
        if not still_files:
            raise HTTPException(
                status_code=404,
                detail=f"No still files found for {anchor_shot_id}"
            )
        
        # Sort by version number and take the latest
        def extract_version(p):
            import re
            m = re.search(r'still_v(\d+)', p.name)
            return int(m.group(1)) if m else 0
        
        still_files.sort(key=extract_version, reverse=True)
        local_path = still_files[0]
        version = extract_version(local_path)
        
        # Compute SHA256
        sha256_hash = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        sha256 = sha256_hash.hexdigest()
    
    # Store anchor still metadata
    from hfvg.studio_db import set_anchor_still
    await set_anchor_still(
        DATABASE_PATH,
        episode_id,
        anchor_shot_id,
        target_shot_ids,
        str(local_path),
        version,
        sha256,
        approved_by,
    )
    
    # Audit log
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (
                episode_id,
                "set_anchor",
                f"Anchor {anchor_shot_id} set for {', '.join(target_shot_ids)} (approved_by: {approved_by or 'G2'})",
                "admin"
            )
        )
        await db.commit()
    
    return {
        "success": True,
        "episode_id": episode_id,
        "anchor_shot_id": anchor_shot_id,
        "target_shot_ids": target_shot_ids,
        "local_path": str(local_path),
        "version": version,
        "sha256": sha256,
        "approved_by": approved_by,
        "message": f"Anchor still set for {len(target_shot_ids)} shot(s)",
    }


ACTIVE_MODEL_STATUSES = {
    "active_default_still", "active_default_clip", "active_default_tts",
    "active_routed", "active", "active_ref_generation",
}


@app.get("/api/models")
async def get_models(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Documented limits of every model the harness offers (creator view: unknowns are 'not published',
    each value carries its source URL and check date). Requires: Authorization header OR session cookie."""
    await verify_auth_unified(authorization, studio_admin_token)
    from hfvg import model_caps

    registry = model_caps.get_registry()
    models_data = [
        model_caps.creator_view(model_id)
        for model_id, caps in registry.models.items()
        if caps.status in ACTIVE_MODEL_STATUSES
    ]
    return {"models": models_data, "stale": registry.stale(), "checked": registry.checked}


class PreflightRequest(BaseModel):
    """Preflight check request: optional shots (shots.json entries or G1 shot plans) to check instead
    of the episode's uploaded shots.json."""

    shots: list[dict] | None = None


@app.post("/api/episodes/{episode_id}/preflight")
async def preflight_episode(
    episode_id: str,
    request: PreflightRequest | None = None,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Per-shot pass/fail against the model limits, same rules as G1 and submit (no provider calls).

    Shots come from the request or the episode's uploaded shots.json (data/episodes/{id}/shots.json).
    Requires: Authorization header OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token)
    validate_episode_id(episode_id)
    from hfvg.preflight import preflight_plans
    from hfvg.shot_data import ShotDataError, parse_shots

    if request and request.shots:
        shots = request.shots
    else:
        shots_path = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "shots.json"
        if not shots_path.exists():
            raise HTTPException(status_code=404, detail=f"No shots.json uploaded for {episode_id}")
        try:
            raw = json.loads(shots_path.read_text())
            parse_shots(raw)  # same validation as upload
        except (ValueError, ShotDataError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid shots.json: {e}")
        shots = raw["shots"]

    report = preflight_plans(shots)
    errors = [v for v in report["violations"] if v["severity"] == "error"]
    warnings = [v for v in report["violations"] if v["severity"] == "warning"]
    return {
        "episode_id": episode_id,
        "shots": report["shots"],
        "violations": report["violations"],
        "errors": errors,
        "warnings": warnings,
        "has_errors": bool(errors),
    }

