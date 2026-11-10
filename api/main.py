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
- GET /api/dashboard: Master admin dashboard (users, spend, activity)
- GET /api/users: List users
- POST /api/users: Create user
- GET /api/users/{user_id}: Get user details
"""

import os
import sys
import asyncio
import json
import hashlib
import re
import secrets
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form, Cookie, Response, Body, Depends, Request
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

def get_upload_token() -> str | None:
    """Get UPLOAD_TOKEN for scoped production API access (upload-only). Returns None if not configured."""
    token = os.getenv("UPLOAD_TOKEN", "")
    if not token:
        return None
    if len(token) < 32:
        raise ValueError(
            "UPLOAD_TOKEN must be at least 32 characters if set. "
            "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return token

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

# Session management (G-AUTH-1): HMAC-signed, expiring tokens, also stored in DB (revocation).
#
# Token format: "v1.<expires_unix>.<nonce>.<sig>" with sig = base64url(HMAC-SHA256(key, "v1.<exp>.<nonce>"))
# (no padding). The key is SESSION_SECRET (>= 32 chars) when set, otherwise derived from ADMIN_SECRET as
# HMAC-SHA256(ADMIN_SECRET, SESSION_KEY_CONTEXT). The Next.js middleware verifies the same token with
# Web Crypto (web/lib/session.mjs) using the same env vars, so a forged or expired cookie never reaches
# a studio page. The API additionally requires the DB row (logout revokes it).
SESSION_TTL_SECONDS = 86400
SESSION_KEY_CONTEXT = b"hfvg-studio-session-v1"
_SESSION_NONCE_RE = r"[A-Za-z0-9_-]{16,64}"

# Login rate limiting: simple in-memory tracker
# Key: email, Value: (attempt_count, first_attempt_time, lockout_until)
_LOGIN_ATTEMPTS: dict[str, tuple[int, float, float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes


def check_rate_limit(email: str) -> tuple[bool, str]:
    """
    Check if login is rate limited for an email.
    
    Returns:
        (allowed, message): (True, "") if allowed, (False, reason) if blocked
    """
    import time
    
    now = time.time()
    email_lower = email.lower()
    
    if email_lower in _LOGIN_ATTEMPTS:
        attempts, first_attempt, lockout_until = _LOGIN_ATTEMPTS[email_lower]
        
        # Check if currently locked out
        if lockout_until > now:
            remaining = int(lockout_until - now)
            return False, f"Too many failed attempts. Try again in {remaining} seconds."
        
        # Reset if outside window
        if now - first_attempt > LOGIN_WINDOW_SECONDS:
            del _LOGIN_ATTEMPTS[email_lower]
            return True, ""
        
        # Check if exceeded attempts within window
        if attempts >= MAX_LOGIN_ATTEMPTS:
            # Start lockout
            _LOGIN_ATTEMPTS[email_lower] = (attempts, first_attempt, now + LOCKOUT_DURATION_SECONDS)
            return False, f"Too many failed attempts. Account locked for {LOCKOUT_DURATION_SECONDS // 60} minutes."
        
    return True, ""


def record_failed_login(email: str):
    """Record a failed login attempt."""
    import time
    
    now = time.time()
    email_lower = email.lower()
    
    if email_lower in _LOGIN_ATTEMPTS:
        attempts, first_attempt, lockout_until = _LOGIN_ATTEMPTS[email_lower]
        _LOGIN_ATTEMPTS[email_lower] = (attempts + 1, first_attempt, lockout_until)
    else:
        _LOGIN_ATTEMPTS[email_lower] = (1, now, 0)


def clear_failed_logins(email: str):
    """Clear failed login attempts after successful login."""
    email_lower = email.lower()
    if email_lower in _LOGIN_ATTEMPTS:
        del _LOGIN_ATTEMPTS[email_lower]


def session_signing_key() -> bytes:
    """Signing key for session tokens (fail closed: raises ValueError when not configured)."""
    import hmac as _hmac

    dedicated = os.getenv("SESSION_SECRET", "")
    if dedicated:
        if len(dedicated) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        return dedicated.encode()
    return _hmac.new(get_admin_secret().encode(), SESSION_KEY_CONTEXT, hashlib.sha256).digest()


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_session_token(expires_at: int, nonce: str, key: bytes | None = None) -> str:
    """Build a signed session token (see format above)."""
    import hmac as _hmac

    payload = f"v1.{int(expires_at)}.{nonce}"
    sig = _hmac.new(key if key is not None else session_signing_key(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64url(sig)}"


def check_session_signature(token: str, now: float | None = None, key: bytes | None = None) -> bool:
    """True only for a well-formed, correctly signed (constant-time compare), unexpired token."""
    import re
    import time

    if not isinstance(token, str) or len(token) > 256:
        return False
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != "v1" or not re.fullmatch(r"[0-9]{1,12}", parts[1]):
        return False
    if not re.fullmatch(_SESSION_NONCE_RE, parts[2]):
        return False
    try:
        expected = sign_session_token(int(parts[1]), parts[2], key)
    except ValueError:
        return False  # no key configured: fail closed
    if not secrets.compare_digest(expected.encode(), token.encode()):
        return False
    return int(parts[1]) > (time.time() if now is None else now)


async def _ensure_sessions_table(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            role TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    # Add columns if they don't exist (for existing databases)
    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
    except:
        pass  # Column already exists
    try:
        await db.execute("ALTER TABLE sessions ADD COLUMN role TEXT")
    except:
        pass  # Column already exists


def get_cookie_secure_flag() -> bool:
    """Get session cookie Secure flag from environment (default: False)."""
    return os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"


async def create_session(user_id: str = "admin", role: str = "admin") -> str:
    """Create a signed session token (24 h expiry) and store it in the database."""
    import aiosqlite
    import time

    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    token = sign_session_token(expires_at, secrets.token_urlsafe(24))

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _ensure_sessions_table(db)
        await db.execute(
            """INSERT INTO sessions (token, user_id, role, created_at, expires_at)
               VALUES (?, ?, ?, datetime('now'), datetime(?, 'unixepoch'))""",
            (token, user_id, role, expires_at)
        )
        await db.commit()

    return token


async def verify_session_token(token: str) -> bool:
    """Verify a session token: HMAC signature + expiry first (no DB hit for forgeries), then the DB row
    (a logged-out / revoked session is refused even while its signature is still valid)."""
    import aiosqlite

    if not check_session_signature(token):
        return False
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await _ensure_sessions_table(db)
            async with db.execute(
                """SELECT token FROM sessions 
                   WHERE token = ? AND expires_at > datetime('now')""",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
    except Exception:
        return False


async def revoke_session(token: str) -> None:
    import aiosqlite

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await _ensure_sessions_table(db)
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await db.commit()


async def get_user_id_from_session(token: str) -> str | None:
    """Get user_id from session token. Returns None if session invalid/expired."""
    import aiosqlite
    
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await _ensure_sessions_table(db)
            async with db.execute(
                """SELECT user_id FROM sessions 
                   WHERE token = ? AND expires_at > datetime('now')""",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception:
        return None


async def get_session_role(token: str) -> str | None:
    """Get role from session token. Returns None if session invalid/expired."""
    import aiosqlite
    
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await _ensure_sessions_table(db)
            async with db.execute(
                """SELECT role FROM sessions 
                   WHERE token = ? AND expires_at > datetime('now')""",
                (token,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and Temporal client on startup."""
    global temporal_client
    
    try:
        # Initialize database tables
        await init_studio_db(DATABASE_PATH)
        
        # Initialize database tables in correct order
        from hfvg.users import UserManager
        from hfvg.cost_tracking import CostTracker
        from hfvg.migrations import run_all_migrations
        from hfvg.budget import BudgetLedger
        
        # Initialize budget ledger first (creates job_ledger table)
        budget = BudgetLedger(DATABASE_PATH)
        await budget.init_db()
        
        # Then cost tracker (migrates job_ledger if it exists)
        cost_tracker = CostTracker(DATABASE_PATH)
        await cost_tracker.init_db()
        
        # Then users and migrations
        user_mgr = UserManager(DATABASE_PATH)
        await user_mgr.init_db()
        await run_all_migrations(DATABASE_PATH)
        
        # Connect to Temporal
        temporal_client = await TemporalClient.connect(
            TEMPORAL_ADDRESS,
            namespace=TEMPORAL_NAMESPACE,
            data_converter=temporal_data_converter,
        )
        # GAP-79: persist every episode's reopen snapshot outside Temporal (history retention)
        saver = asyncio.create_task(_snapshot_saver_loop()) if SNAPSHOT_SAVE_INTERVAL_S > 0 else None
        try:
            yield
        finally:
            if saver:
                saver.cancel()
    finally:
        if temporal_client:
            await temporal_client.close()


# Routes reachable without a session cookie or Bearer secret (G-AUTH-1). Everything else is refused
# with 401 by the app-wide guard below BEFORE request validation or the handler runs (secure by
# default: a new route cannot forget its auth check). Handlers keep their own checks as well.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/"),             # service banner
    ("GET", "/api/health"),   # monitoring
    ("GET", "/gates"),        # static gates page (no data; it calls the authenticated API)
    ("POST", "/api/login"),
    ("POST", "/api/logout"),  # idempotent: revokes the presented session, clears the cookie
    ("POST", "/api/auth/set-password"),  # token-based password setting (public, no auth required)
})


async def require_admin_auth(
    request: Request,
    authorization: str | None = Header(None, include_in_schema=False),
    studio_admin_token: str | None = Cookie(None, include_in_schema=False),
):
    """
    FAIL CLOSED: Admin-only auth middleware (default for all routes).
    
    Routes can opt-in to allow members by setting route.allow_members = True.
    New routes that forget auth checks default to admin-only.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    method = "GET" if request.method == "HEAD" else request.method
    if (method, path) in PUBLIC_ROUTES:
        return
    
    # Allow UPLOAD_TOKEN through for production upload endpoints (routes handle their own checks)
    if method == "POST" and path in ("/api/episodes/upload", "/api/episodes/upload/validate"):
        if authorization and authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ")
            upload_token = get_upload_token()
            # Compare as bytes; empty or unset token must never match
            if upload_token:
                try:
                    token_bytes = token.encode("utf-8")
                    upload_bytes = upload_token.encode("utf-8")
                    if secrets.compare_digest(token_bytes, upload_bytes):
                        return
                except (UnicodeDecodeError, UnicodeEncodeError, AttributeError):
                    pass  # Invalid token, fall through to verify_auth_unified
    
    # Check if route explicitly allows members (via endpoint function attribute)
    endpoint = getattr(route, "endpoint", None) if route else None
    allow_members = getattr(endpoint, "allow_members", False) if endpoint else False
    await verify_auth_unified(authorization, studio_admin_token, require_admin=not allow_members)


logger = logging.getLogger(__name__)

app = FastAPI(
    dependencies=[Depends(require_admin_auth)],
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
        HTTPException: 401 if unauthenticated, 403 if authenticated but not admin
    """
    if not studio_admin_token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in."
        )
    
    if not await verify_session_token(studio_admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session. Please log in again."
        )
    
    # Check that the session has admin role (403 for authenticated non-admin)
    role = await get_session_role(studio_admin_token)
    if role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required."
        )


async def verify_member_or_admin_cookie(studio_admin_token: str | None = Cookie(None)) -> tuple[str, str]:
    """
    Verify member or admin auth from httpOnly cookie (fail closed).
    
    Args:
        studio_admin_token: Session token from cookie
    
    Returns:
        Tuple of (user_id, role)
        
    Raises:
        HTTPException: 401 if unauthenticated
    """
    if not studio_admin_token:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in."
        )
    
    if not await verify_session_token(studio_admin_token):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session. Please log in again."
        )
    
    user_id = await get_user_id_from_session(studio_admin_token)
    role = await get_session_role(studio_admin_token)
    
    if not user_id or not role:
        raise HTTPException(
            status_code=401,
            detail="Invalid session data."
        )
    
    return (user_id, role)


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
    
    # Reject non-ASCII tokens immediately (403, not 500)
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        raise HTTPException(status_code=403, detail="Invalid admin secret (non-ASCII)")
    
    # Compare as bytes to handle encoding properly
    try:
        token_bytes = token.encode("utf-8")
        secret_bytes = current_secret.encode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError, AttributeError):
        raise HTTPException(status_code=403, detail="Invalid admin secret")
    
    if not secrets.compare_digest(token_bytes, secret_bytes):
        raise HTTPException(
            status_code=403,
            detail="Invalid admin secret"
        )


def verify_upload_token(authorization: str | None = Header(None)):
    """
    Verify upload token from Authorization header (scoped to upload operations only).
    Accepts UPLOAD_TOKEN or ADMIN_SECRET (admin can do everything).
    Compares as UTF-8 bytes for security. Never raises 500 on missing config.
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required. Set 'Authorization: Bearer <TOKEN>'"
        )
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization must use Bearer token format"
        )
    
    token = authorization[7:]
    
    # Validate token is valid UTF-8 and ASCII-safe
    try:
        token_bytes = token.encode('utf-8')
        # Reject non-ASCII tokens (security: avoid encoding tricks)
        if not token.isascii():
            raise HTTPException(status_code=403, detail="Invalid token format")
    except (UnicodeDecodeError, UnicodeEncodeError):
        raise HTTPException(status_code=403, detail="Invalid token format")
    
    # Get configured tokens (handle missing config gracefully, never 500)
    try:
        admin_secret = get_admin_secret()
        admin_bytes = admin_secret.encode('utf-8')
    except ValueError:
        # ADMIN_SECRET not configured (shouldn't happen in prod, but be safe)
        admin_bytes = b""
    
    upload_token = get_upload_token()
    upload_bytes = upload_token.encode('utf-8') if upload_token else b""
    
    # Accept either UPLOAD_TOKEN or ADMIN_SECRET (constant-time comparison on bytes)
    is_valid = (
        (upload_bytes and secrets.compare_digest(token_bytes, upload_bytes)) or
        (admin_bytes and secrets.compare_digest(token_bytes, admin_bytes))
    )
    
    if not is_valid:
        raise HTTPException(
            status_code=403,
            detail="Invalid upload token"
        )


async def verify_auth_unified(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
    require_admin: bool = True,
):
    """
    Unified auth: accept either Bearer token OR session cookie.
    
    This allows both programmatic access (Bearer token) and browser access (cookie).
    Fail closed: at least one must be valid.
    
    Args:
        authorization: Bearer token from Authorization header
        studio_admin_token: Session token from cookie
        require_admin: If True, require admin role; if False, allow member or admin
    
    Returns 401 when no credential is provided or credential is invalid/expired.
    Returns 403 when authenticated but not authorized (e.g. member accessing admin route).
    
    Ignores malformed/non-Bearer Authorization headers (e.g. Basic auth from reverse proxy)
    when a valid session cookie is present.
    """
    # Try cookie first (browser access)
    cookie_error = None
    if studio_admin_token:
        try:
            if require_admin:
                await verify_admin_cookie(studio_admin_token)
            else:
                await verify_member_or_admin_cookie(studio_admin_token)
            return  # Cookie is valid
        except HTTPException as e:
            cookie_error = e
    
    # Try Bearer token (programmatic access), but only if it's well-formed
    # Ignore non-Bearer headers (e.g. "Basic xyz" from reverse proxy)
    bearer_error = None
    if authorization and authorization.startswith("Bearer "):
        try:
            verify_admin_secret(authorization)
            return  # Bearer token is valid (admin secret always has full access)
        except HTTPException as e:
            bearer_error = e
    
    # Decide which error to raise based on what was attempted
    if cookie_error:
        # Cookie was tried and failed
        if cookie_error.status_code == 403:
            # Authenticated but not authorized (e.g. member accessing admin route)
            # This takes precedence over any Bearer token error
            raise cookie_error
        # Cookie authentication failed (401)
        if bearer_error:
            # Both failed, prefer Bearer error if it's a 403 (invalid secret)
            # Otherwise raise generic 401
            if bearer_error.status_code == 403:
                raise bearer_error
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide either session cookie or 'Authorization: Bearer <ADMIN_SECRET>'"
        )
    
    # No cookie was tried
    if bearer_error:
        # Only Bearer token was tried and failed
        raise bearer_error
    
    # Neither credential provided (or only non-Bearer Authorization header)
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


# The one episode id format of the API, the workflow ids (ep04, children ep04-shot-A01) and the
# episode directories (data/episodes/ep04/). StartEpisodeRequest enforces it, so no episode with
# another id can be started through the API; every route (incl. the file route) uses the same check.
# Optional lowercase suffix for a cut of an episode (ep05full: the full Ep05 next to the ep05
# teaser). It must start with a letter, so ep001 stays invalid; no "-" (child ids are
# <ep>-shot-<id>), no "/" or "." (path safety), ASCII only, at most 12 characters.
EPISODE_ID_PATTERN = r"ep[0-9]{2}(?:[a-z][a-z0-9]{0,11})?"  # ASCII digits only (\d also matches other scripts)


def validate_episode_id(episode_id: str) -> str:
    """
    Validate episode ID format.
    
    Must match: ep## with an optional lowercase suffix (e.g., ep04, ep99, ep05full)
    Prevents path traversal attacks.
    """
    import re
    # fullmatch: re.match(r"^ep\d{2}$") also accepted a trailing newline ("ep04\n")
    if not isinstance(episode_id, str) or not re.fullmatch(EPISODE_ID_PATTERN, episode_id):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid episode_id format: {episode_id}. Must match ep## (e.g., ep04)"
        )
    return episode_id


class StartEpisodeRequest(BaseModel):
    episode_id: str = Field(..., pattern=rf"^{EPISODE_ID_PATTERN}$", description="Episode ID (e.g., ep04)")
    beatmap_content: str | None = Field(None, description="Beatmap markdown content (optional)")
    dry_run: bool = Field(True, description="Run in dry-run mode (default: true, live requires explicit false)")
    shots_subset: list[str] | None = Field(None, description="Subset of shot IDs to run (e.g., ['A01', 'A02'] for 2-shot canary)")
    credit_cap: int | None = Field(None, description="Credit cap for this run (overrides episode line cap)")
    attempt_overrides: dict[str, int] | None = Field(None, description="Shot ID -> attempt number overrides (e.g., {'A02': 2} starts A02 as first retry)")
    enable_story_shaping: bool = Field(False, description="Enable story-shaping stage before stills generation (default: false)")
    archetype_id: str | None = Field(None, description="Genre preset ID for story shaping (e.g., 'nostalgia-time-travel')")
    idea: str | None = Field(None, description="User's story idea for story shaping")


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
    headcount_overrides: dict[str, str] | None = Field(
        None, description="G2 only: shot_id -> reason to approve despite a headcount FAIL (audited)")


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
    # Run-control view for the studio episode page (all straight from the workflow query)
    current_gate: str | None = None
    passed_gates: list[str] = []
    shot_decisions: dict[str, str] = {}
    pending_retries: list[str] = []
    clip_retries: dict[str, int] = {}
    pending_clip_retries: list[str] = []
    final_cut_refusals: list = []
    final_cut_rejections: list = []
    failed_shots: list[dict | str] = []  # workflow state holds {shot_id, status, error} dicts (GAP-16)
    shot_outcomes: dict = {}


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
    admin_secret: Optional[str] = Field(None, description="Admin secret for authentication")
    email: Optional[str] = Field(None, description="Member email")
    password: Optional[str] = Field(None, description="Member password")
    api_token: Optional[str] = Field(None, description="Member API token")


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
    Returns server status including whether live mode is available.
    """
    return {
        "status": "ok",
        "temporal": temporal_client is not None,
        "dry_run_default": DRY_RUN_DEFAULT,
        "live_available": not DRY_RUN_DEFAULT,  # Live is available when server DRY_RUN is false
    }


@app.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """
    Login and set httpOnly cookie.
    
    Supports three auth methods:
    1. Admin secret (admin_secret)
    2. Member email + password (email, password)
    3. Member API token (api_token)
    
    Args:
        request: Login request
        response: Response to set cookie on
    
    Returns:
        Success message with user info
    """
    user_info = None
    
    # Method 1: Admin secret
    if request.admin_secret:
        admin_secret = os.getenv("ADMIN_SECRET", "")
        if not admin_secret or len(admin_secret) < 32:
            raise HTTPException(status_code=500, detail="Server not configured: ADMIN_SECRET required")
        
        if not secrets.compare_digest(
            request.admin_secret.encode(), admin_secret.encode()
        ):
            raise HTTPException(status_code=401, detail="Invalid admin secret")
        
        user_info = {"user_id": "admin", "name": "Admin", "role": "admin"}
    
    # Method 2: Member email + password
    elif request.email and request.password:
        # Check rate limit
        allowed, message = check_rate_limit(request.email)
        if not allowed:
            raise HTTPException(status_code=429, detail=message)
        
        from hfvg.users import UserManager
        mgr = UserManager(DATABASE_PATH)
        await mgr.init_db()
        
        user_info = await mgr.authenticate_password(request.email, request.password)
        if not user_info:
            record_failed_login(request.email)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # Clear failed attempts on successful login
        clear_failed_logins(request.email)
    
    # Method 3: Member API token
    elif request.api_token:
        from hfvg.users import UserManager
        mgr = UserManager(DATABASE_PATH)
        await mgr.init_db()
        
        user_info = await mgr.authenticate_token(request.api_token)
        if not user_info:
            raise HTTPException(status_code=401, detail="Invalid API token")
    
    else:
        raise HTTPException(status_code=401, detail="Must provide admin_secret, (email + password), or api_token")
    
    # Create session and set httpOnly cookie with session token
    session_token = await create_session(user_info["user_id"], user_info.get("role", "admin"))
    response.set_cookie(
        key="studio_admin_token",
        value=session_token,
        httponly=True,
        secure=get_cookie_secure_flag(),
        samesite="lax",
        max_age=86400,  # 24 hours
    )
    
    return {
        "success": True,
        "message": "Logged in successfully",
        "user": {
            "user_id": user_info["user_id"],
            "name": user_info.get("name", "Admin"),
            "role": user_info.get("role", "admin")
        }
    }


@app.post("/api/logout")
async def logout(response: Response, studio_admin_token: str | None = Cookie(None)):
    """Revoke the session (DB row) and clear the cookie. Idempotent; no auth needed to log out."""
    if studio_admin_token:
        try:
            await revoke_session(studio_admin_token)
        except Exception as e:  # the cookie is cleared regardless
            print(f"Session revoke failed: {e}")
    response.delete_cookie(key="studio_admin_token", httponly=True, samesite="lax")
    return {"success": True, "message": "Logged out"}


@app.post("/api/episodes/upload/validate")
async def validate_upload(
    request: Request,
    episode_id: str = Form(...),
    beatmap_file: UploadFile = File(...),
    credit_plan_file: UploadFile = File(None),
    continuity_file: UploadFile = File(None),
):
    """
    Validate episode upload files WITHOUT saving (dry-run validation).
    
    Runs same validation as /api/episodes/upload but doesn't persist anything.
    Returns validation errors if any, or success with shot count.
    
    Auth: Bearer token (UPLOAD_TOKEN or ADMIN_SECRET) OR session cookie (member or admin)
    Handled by middleware (require_admin_auth with .allow_members = True)
    """
    # Auth handled by middleware
    
    errors = []
    shots_count = 0
    
    # Validate episode ID format (early return on invalid)
    try:
        validate_episode_id(episode_id)
    except HTTPException as e:
        return {"success": False, "valid": False, "errors": [f"Invalid episode_id: {e.detail}"], "shots_count": 0}
    
    # Validate beatmap content
    try:
        beatmap_content = await beatmap_file.read()
        if not beatmap_content:
            errors.append("Beatmap file is empty")
        else:
            # Check UTF-8 decoding
            try:
                beatmap_content.decode('utf-8')
            except UnicodeDecodeError:
                errors.append("Beatmap file is not valid UTF-8")
            
            # Try parsing beatmap without saving
            import tempfile
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.md', delete=False) as tmp:
                tmp.write(beatmap_content)
                tmp_path = tmp.name
            try:
                shots = parse_beatmap(tmp_path)
                if not shots:
                    errors.append("No shots found in beatmap")
                shots_count = len(shots)
            except Exception as e:
                errors.append(f"Beatmap parsing error: {str(e)}")
            finally:
                Path(tmp_path).unlink(missing_ok=True)
    except Exception as e:
        errors.append(f"Error reading beatmap: {str(e)}")
    
    # Validate credit plan if provided (treat empty files as None - issue #7)
    if credit_plan_file:
        try:
            credit_plan_content = await credit_plan_file.read()
            # Empty files are treated same as None
            if credit_plan_content and credit_plan_content.strip():
                # Check UTF-8
                try:
                    credit_plan_content.decode('utf-8')
                except UnicodeDecodeError:
                    errors.append("Credit plan file is not valid UTF-8")
                
                import tempfile
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.md', delete=False) as tmp:
                    tmp.write(credit_plan_content)
                    tmp_path = tmp.name
                try:
                    parse_credit_plan(Path(tmp_path))
                except Exception as e:
                    errors.append(f"Credit plan parsing error: {str(e)}")
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            else:
                credit_plan_file = None  # Treat empty as None
        except Exception as e:
            errors.append(f"Error reading credit plan: {str(e)}")
    
    # Validate continuity if provided (treat empty files as None - issue #7)
    if continuity_file:
        try:
            continuity_content = await continuity_file.read()
            # Empty files are treated same as None
            if continuity_content and continuity_content.strip():
                # Check UTF-8
                try:
                    continuity_content.decode('utf-8')
                except UnicodeDecodeError:
                    errors.append("Continuity file is not valid UTF-8")
            else:
                continuity_file = None  # Treat empty as None
        except Exception as e:
            errors.append(f"Error reading continuity: {str(e)}")
    
    if errors:
        return {
            "success": False,
            "valid": False,
            "errors": errors,
            "shots_count": shots_count
        }
    
    return {
        "success": True,
        "valid": True,
        "errors": [],
        "episode_id": episode_id,
        "shots_count": shots_count,
        "files_validated": {
            "beatmap": True,
            "credit_plan": credit_plan_file is not None,
            "continuity": continuity_file is not None,
        }
    }

validate_upload.allow_members = True


@app.post("/api/episodes/upload")
async def upload_beatmap(
    request: Request,
    episode_id: str = Form(...),
    beatmap_file: UploadFile = File(...),
    credit_plan_file: UploadFile = File(None),
    continuity_file: UploadFile = File(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Upload episode files for StoryBuilder Production tab (BEATMAP required, CREDIT-PLAN and CONTINUITY optional).
    
    Auth: Bearer token (UPLOAD_TOKEN or ADMIN_SECRET) OR session cookie (member or admin)
    Handled by middleware (require_admin_auth with .allow_members = True)
    Idempotent: Re-uploading the same episode/files is safe (deduped)
    
    Returns:
        Number of shots parsed, upload hash for deduplication, is_duplicate flag
    """
    # Auth handled by middleware (no additional check needed here)
    
    validate_episode_id(episode_id)
    
    # Return 409 while workflow is running (fail closed: 503 on Temporal errors)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        desc = await handle.describe()
        # Check if workflow exists and is running
        from temporalio.client import WorkflowExecutionStatus
        if desc.status in (WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.CONTINUED_AS_NEW):
            raise HTTPException(
                status_code=409,
                detail=f"Episode {episode_id} workflow is currently running. Cannot upload while workflow is active."
            )
    except HTTPException:
        raise  # Re-raise our 409 and 503
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        # NOT_FOUND is fine - workflow doesn't exist, upload can proceed
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            pass  # Workflow not found, can proceed with upload
        else:
            # Fail closed: any other Temporal error returns 503
            raise HTTPException(status_code=503, detail=f"Temporal error: {str(e)}")
    
    # Read all files and compute hashes
    import hashlib
    import tempfile
    import shutil
    
    beatmap_content = await beatmap_file.read()
    credit_plan_content = await credit_plan_file.read() if credit_plan_file else None
    continuity_content = await continuity_file.read() if continuity_file else None
    
    # Treat empty optional files as None (issue #7)
    if credit_plan_content and not credit_plan_content.strip():
        credit_plan_content = None
    if continuity_content and not continuity_content.strip():
        continuity_content = None
    
    # Compute combined hash from all provided files
    hasher = hashlib.sha256()
    hasher.update(b"beatmap:")
    hasher.update(beatmap_content)
    if credit_plan_content:
        hasher.update(b"|credit_plan:")
        hasher.update(credit_plan_content)
    if continuity_content:
        hasher.update(b"|continuity:")
        hasher.update(continuity_content)
    upload_hash = hasher.hexdigest()[:16]
    
    # Check for existing upload with same hash (idempotent retry detection)
    episodes_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes"))
    episode_dir = episodes_dir / episode_id
    hash_file = episode_dir / ".upload_hash"
    
    is_duplicate = False
    if hash_file.exists():
        try:
            existing_hash = hash_file.read_text(encoding='utf-8').strip()
            if existing_hash == upload_hash:
                # Content matches exactly - this is an idempotent retry
                is_duplicate = True
                # Return immediately without rewriting anything
                shots = parse_beatmap(str(episode_dir / "BEATMAP.md"))
                return {
                    "success": True,
                    "episode_id": episode_id,
                    "shots_count": len(shots),
                    "upload_hash": upload_hash,
                    "is_duplicate": True,
                    "files_uploaded": {
                        "beatmap": True,
                        "credit_plan": (episode_dir / "CREDIT-PLAN.md").exists(),
                        "continuity": (episode_dir / "CONTINUITY.md").exists(),
                    },
                    "beatmap_path": str(episode_dir / "BEATMAP.md"),
                }
        except Exception:
            pass  # Hash file corrupt or unreadable, proceed with upload
    
    # Stage upload to temp directory and validate before moving
    with tempfile.TemporaryDirectory(prefix=f"upload_{episode_id}_") as temp_dir:
        temp_path = Path(temp_dir)
        
        # Write files to temp location
        temp_beatmap = temp_path / "BEATMAP.md"
        temp_beatmap.write_bytes(beatmap_content)
        
        temp_credit_plan = None
        if credit_plan_content:
            temp_credit_plan = temp_path / "CREDIT-PLAN.md"
            temp_credit_plan.write_bytes(credit_plan_content)
        
        temp_continuity = None
        if continuity_content:
            temp_continuity = temp_path / "CONTINUITY.md"
            temp_continuity.write_bytes(continuity_content)
        
        # Parse and validate in temp location (fail before writing to episodes dir)
        try:
            shots = parse_beatmap(str(temp_beatmap))
            if not shots:
                raise ValueError("No shots found in beatmap")
            
            # Validate credit plan if provided
            if temp_credit_plan:
                parse_credit_plan(temp_credit_plan)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Upload validation failed: {str(e)}")
        
        # Validation passed - move to permanent location
        episode_dir.mkdir(parents=True, exist_ok=True)
        
        # Delete old .upload_hash before replacing files (issue #7)
        hash_file.unlink(missing_ok=True)
        
        # Move files atomically
        shutil.copy2(temp_beatmap, episode_dir / "BEATMAP.md")
        if temp_credit_plan:
            shutil.copy2(temp_credit_plan, episode_dir / "CREDIT-PLAN.md")
        else:
            # Remove stale CREDIT-PLAN.md if re-upload leaves it out
            (episode_dir / "CREDIT-PLAN.md").unlink(missing_ok=True)
        
        if temp_continuity:
            shutil.copy2(temp_continuity, episode_dir / "CONTINUITY.md")
        else:
            # Remove stale CONTINUITY.md if re-upload leaves it out
            (episode_dir / "CONTINUITY.md").unlink(missing_ok=True)
    
    # Initialize database
    await init_studio_db(DATABASE_PATH)
    
    # Extract user_id from session
    user_id = None
    if studio_admin_token:
        user_id = await get_user_id_from_session(studio_admin_token)
    
    # Create or update episode record
    await create_episode(DATABASE_PATH, episode_id, str(episode_dir / "BEATMAP.md"), user_id=user_id)
    
    # Replace shots (not INSERT OR IGNORE) - delete old, insert new
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM shots WHERE episode_id = ?", (episode_id,))
        await db.commit()
    
    await insert_shots_from_beatmap(DATABASE_PATH, episode_id, shots)
    
    # Initialize or clear budget based on credit plan presence
    credit_plan_path = episode_dir / "CREDIT-PLAN.md"
    ledger = BudgetLedger(DATABASE_PATH)
    await ledger.init_db()
    if credit_plan_path.exists():
        credit_plan = parse_credit_plan(credit_plan_path)
        await ledger.init_episode_budget_from_plan(episode_id, credit_plan)
    else:
        # Clear budget lines when re-upload omits the credit plan (preserve transactions history)
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute("DELETE FROM budget_lines WHERE episode_id = ?", (episode_id,))
            await db.commit()
    
    # Write .upload_hash LAST (after all other operations succeeded)
    hash_file.write_text(upload_hash, encoding='utf-8')
    
    return {
        "success": True,
        "episode_id": episode_id,
        "shots_count": len(shots),
        "upload_hash": upload_hash,
        "is_duplicate": False,
        "files_uploaded": {
            "beatmap": True,
            "credit_plan": credit_plan_path.exists(),
            "continuity": (episode_dir / "CONTINUITY.md").exists(),
        },
        "beatmap_path": str(episode_dir / "BEATMAP.md"),
    }

upload_beatmap.allow_members = True


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


upload_prompt_kit.allow_members = True


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


upload_shots.allow_members = True


@app.post("/api/episodes/{episode_id}/upload-audio-plan")
async def upload_audio_plan(
    episode_id: str,
    audio_plan_file: UploadFile = File(...),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Upload the audio plan: audio_plan.json (required).

    Requires: Cookie auth (members allowed). Stored in data/episodes/{episode_id}/audio/audio_plan.json.
    audio_plan.json specifies VO, music, and SFX using the real schema (vo:{voice_id, lines[...]}, music:{...}).
    """
    await verify_admin_cookie(studio_admin_token)
    validate_episode_id(episode_id)

    from hfvg.audio_mix import audio_plan_path, normalize_plan

    audio_plan_bytes = await audio_plan_file.read()
    try:
        audio_plan_data = json.loads(audio_plan_bytes)
        
        # Validate using the real normalize_plan function (dry_run=False to test live schema)
        try:
            normalize_plan(audio_plan_data, dry_run=False)
        except Exception as e:
            raise ValueError(f"Audio plan validation failed: {e}")
        
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid audio_plan.json: {e}")

    # Use the correct path: <episode>/audio/audio_plan.json
    plan_path = audio_plan_path(episode_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(audio_plan_bytes)
    
    return {
        "success": True,
        "episode_id": episode_id,
        "audio_plan_path": str(plan_path),
    }


upload_audio_plan.allow_members = True


def uploaded_shots_problems(episode_id: str) -> list[str]:
    """Validation + preflight errors of the episode's uploaded shots.json (empty = OK or none uploaded;
    without one a dry run uses the beatmap and a live run is refused by the workflow)."""
    from hfvg.preflight import preflight_plans
    from hfvg.shot_data import ShotDataError, parse_shots

    path = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "shots.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        parse_shots(raw)
    except (ValueError, ShotDataError) as e:
        return [f"invalid shots.json: {e}"]
    return [f"preflight: {e}" for e in preflight_plans(raw["shots"])["errors"]]


@app.get("/api/episodes")
async def list_episodes():
    """
    List all episodes (member or admin).
    
    Returns episodes from the database with their current status.
    """
    
    import aiosqlite
    from datetime import datetime, timezone
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT episode_id, live_mode, g108_approved, created_at
            FROM episodes
            ORDER BY created_at DESC
            LIMIT 100
        """) as cursor:
            episodes = []
            async for row in cursor:
                episodes.append({
                    "episode_id": row["episode_id"],
                    "live_mode": bool(row["live_mode"]),
                    "g108_approved": bool(row["g108_approved"]),
                    "created_at": row["created_at"]
                })
    
    return {"episodes": episodes}


# Mark list_episodes to allow members
list_episodes.allow_members = True

@app.post("/api/episodes", response_model=StartEpisodeResponse)
async def start_episode(
    request: StartEpisodeRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Start a new episode workflow.
    
    Requires: Authorization header with admin secret OR session cookie (member or admin).
    """
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
        
        with open(beatmap_path, "w", encoding="utf-8") as f:
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
    
    # G-UI-1: an uploaded shots.json must validate and pass preflight BEFORE anything starts (the new
    # episode page shows the same report; this is the server-side refusal, not a UI-only check).
    shots_problems = uploaded_shots_problems(request.episode_id)
    if shots_problems:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to start {request.episode_id}: {'; '.join(shots_problems[:10])}",
        )

    # Start workflow
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    # Stable workflow ID for approve route to find
    workflow_id = episode_workflow_id(request.episode_id)
    
    handle = await temporal_client.start_workflow(
        EpisodeWorkflowV2.run,
        args=[request.episode_id, beatmap_path, request.dry_run, request.shots_subset, request.credit_cap, request.attempt_overrides, None, request.enable_story_shaping, request.archetype_id, request.idea],
        id=workflow_id,
        task_queue="hfvg-tasks",
    )
    
    return StartEpisodeResponse(
        workflow_id=handle.id,
        run_id=handle.result_run_id or "",
        episode_id=request.episode_id,
        message=f"Episode {request.episode_id} started (dry_run={request.dry_run})",
    )


# Mark start_episode to allow members
start_episode.allow_members = True

@app.get("/api/episodes/{episode_id}", response_model=EpisodeStateResponse)
async def get_episode_state(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get current episode workflow state.
    
    Requires: Authorization (member or admin).
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    
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
        current_gate=state.get("current_gate"),
        passed_gates=list(state.get("passed_gates") or []),
        shot_decisions=dict(state.get("shot_decisions") or {}),
        pending_retries=list(state.get("pending_retries") or []),
        clip_retries=dict(state.get("clip_retries") or {}),
        pending_clip_retries=list(state.get("pending_clip_retries") or []),
        final_cut_refusals=list(state.get("final_cut_refusals") or []),
        final_cut_rejections=list(state.get("final_cut_rejections") or []),
        failed_shots=list(state.get("failed_shots") or []),
        shot_outcomes=dict(state.get("shot_outcomes") or {}),
    )


# Mark get_episode_state to allow members
get_episode_state.allow_members = True

@app.post("/api/episodes/{episode_id}/assembly/accept")
async def accept_assembly_qc(
    episode_id: str,
    request: AssemblyAcceptRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Human review of a blocking mute-edit QC verdict (FAIL / NEEDS_REVIEW): accept it with a reason
    so G_FINAL_CUT can be approved. The workflow ignores it when nothing is blocking."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


@app.post("/api/episodes/{episode_id}/delivery/rebuild")
async def request_delivery_rebuild(
    episode_id: str,
    request: DeliveryAcceptRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Rebuild the delivery for the current picture after an audio-plan edit (reason required).
    Generated audio is reused by content hash; any new generation still goes through the guards."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        await handle.signal("request_delivery_rebuild", request.reason.strip())
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error requesting delivery rebuild: {str(e)}")
    return {"success": True, "episode_id": episode_id, "message": "Delivery rebuild requested"}



@app.post("/api/episodes/{episode_id}/assembly/rebuild")
async def request_edit_rebuild(
    episode_id: str,
    request: AssemblyAcceptRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-29: re-assemble the mute edit for unchanged clips after an edit_plan.json change (reason
    required). Picture only, no generation; the delivery is rebuilt for the new edit."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        await handle.signal("request_edit_rebuild", request.reason.strip())
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error requesting edit rebuild: {str(e)}")
    return {"success": True, "episode_id": episode_id, "message": "Edit rebuild requested"}

def final_cut_blockers(state: dict) -> list[str]:
    """Why G_FINAL_CUT cannot be approved right now (empty = allowed). Mirrors the workflow's own
    approve_g_final_cut refusals: not parked at the gate, clip retry pending, blocking assembly QC
    or blocking delivery QC without a reviewer acceptance."""
    problems: list[str] = []
    if state.get("current_gate") != "G_FINAL_CUT":
        problems.append(f"not at G_FINAL_CUT (current: {state.get('current_gate')})")
    if state.get("pending_clip_retries"):
        problems.append(f"clip retry pending for: {', '.join(sorted(state['pending_clip_retries']))}")
    assembly = state.get("assembly") or {}
    if assembly.get("blocking"):
        qc = assembly.get("qc") or {}
        problems.append(f"assembly QC {qc.get('verdict')}: {qc.get('reason')} (accept it with a reason first)")
    delivery = state.get("delivery") or {}
    if delivery.get("due") or delivery.get("phase") not in (None, "done"):
        problems.append(f"delivery rebuild due or running (phase {delivery.get('phase')}); approve once it has passed QC")
    if delivery.get("blocking"):
        qc = delivery.get("qc") or {}
        problems.append(f"delivery QC {qc.get('verdict')}: {qc.get('reason')} (accept it with a reason first)")
    return problems


async def _audit(episode_id: str, action: str, details: str) -> None:
    import aiosqlite

    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
                (episode_id, action, details, "admin"),
            )
            await db.commit()
    except Exception as e:  # the audit trail never blocks a reviewer action
        print(f"Audit log write failed ({action}): {e}")


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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        # Find the EpisodeWorkflowV2 only (not child shot workflows)
        # Episode workflow ID is just the episode_id (e.g., "ep14")
        # Shot workflows have IDs like "ep14-shot-A01" which we must exclude
        # Fixed G-AUTH-1: parenthesise the OR to bind correctly with AND
        workflows = temporal_client.list_workflows(
            f'(WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-") AND WorkflowType = "EpisodeWorkflowV2"'
        )
        
        async for workflow_info in workflows:
            # Double-check: skip shot workflows
            if "-shot-" in workflow_info.id:
                continue
            
            handle = temporal_client.get_workflow_handle(workflow_info.id)
            
            # Map gate_id to signal method
            signal_name = f"approve_{request.gate_id.lower()}"
            
            # For story_shape approval
            if request.gate_id.lower() in ["story_shape", "story"]:
                try:
                    result = await handle.execute_update("approve_story_shape")
                    return {
                        "success": True,
                        "episode_id": episode_id,
                        "gate_id": request.gate_id,
                        "message": f"Approval sent for gate {request.gate_id}",
                        "note": request.note,
                        "result": result,
                    }
                except (WorkflowUpdateFailedError, ApplicationError) as e:
                    cause = getattr(e, "cause", None) or e
                    raise HTTPException(
                        status_code=409,
                        detail=f"Story shape approval refused: {str(cause)}"
                    )
            # For G1 approval, use update (with validator) to check estimates availability
            elif request.gate_id.lower() in ["g1_shot_plan", "g1"]:
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
                # GAP-27: every kept still's headcount vs the declared cast; a FAIL blocks G2
                overrides = {k: v.strip() for k, v in (request.headcount_overrides or {}).items()}
                if any(not v for v in overrides.values()):
                    raise HTTPException(status_code=422, detail="headcount_overrides need a reason per shot")
                report = await _g2_headcount(episode_id, handle, overrides)
                if report["blocking"]:
                    raise HTTPException(status_code=409, detail="G2 approval refused: headcount FAIL: " + "; ".join(
                        f"{s} v{report['shots'][s].get('version')}: {report['shots'][s]['reason']}"
                        for s in report["blocking"]) + " (re-roll, select another version, reject the shot, "
                        "or pass headcount_overrides with a reason)")
                for s, why in overrides.items():
                    if (report["shots"].get(s) or {}).get("status") == "FAIL":
                        await _audit(episode_id, "g2_headcount_override", f"{s}: {why}")
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
            elif request.gate_id.lower() in ("g_final_cut", "g3", "g3_final_cut"):
                # G_FINAL_CUT is a signal and the workflow ignores it (final_cut_refusals) while QC
                # blocks; refuse it HERE too so the caller gets a 409 instead of a silent no-op.
                state = await handle.query("get_state")
                problems = final_cut_blockers(state)
                if problems:
                    raise HTTPException(
                        status_code=409, detail=f"G_FINAL_CUT approval refused: {'; '.join(problems)}"
                    )
                await handle.signal("approve_g_final_cut")
                await _audit(episode_id, "approve_g_final_cut", request.note or "final cut approved")
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


# Mark approve_gate to allow members
approve_gate.allow_members = True

@app.get("/api/episodes/{episode_id}/story-shape")
async def get_story_shape(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get story shape for an episode. Member or admin.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        workflows = temporal_client.list_workflows(
            f'(WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-") AND WorkflowType = "EpisodeWorkflowV2"'
        )
        
        async for workflow_info in workflows:
            if "-shot-" in workflow_info.id:
                continue
            
            handle = temporal_client.get_workflow_handle(workflow_info.id)
            state = await handle.query("get_state")
            
            return {
                "episode_id": episode_id,
                "story_shape": state.get("story_shape"),
                "critique": state.get("story_shape_critique"),
                "revision": state.get("story_shape_revision"),
                "log": state.get("story_shape_log"),
                "approved": state.get("approved_story_shape", False),
            }
        
        raise HTTPException(status_code=404, detail=f"No running workflow found for episode {episode_id}")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching story shape: {str(e)}")


# Mark get_story_shape to allow members
get_story_shape.allow_members = True

@app.put("/api/episodes/{episode_id}/story-shape")
async def update_story_shape(
    episode_id: str,
    story_shape: dict,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Update story shape and re-critique. Member or admin.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    
    try:
        workflows = temporal_client.list_workflows(
            f'(WorkflowId = "{episode_id}" OR WorkflowId STARTS_WITH "{episode_id}-") AND WorkflowType = "EpisodeWorkflowV2"'
        )
        
        async for workflow_info in workflows:
            if "-shot-" in workflow_info.id:
                continue
            
            handle = temporal_client.get_workflow_handle(workflow_info.id)
            
            try:
                result = await handle.execute_update("update_story_shape", story_shape)
                return {
                    "success": True,
                    "episode_id": episode_id,
                    "story_shape": result["story_shape"],
                    "critique": result["critique"],
                    "needs_revision": result["needs_revision"],
                }
            except (WorkflowUpdateFailedError, ApplicationError) as e:
                cause = getattr(e, "cause", None) or e
                raise HTTPException(
                    status_code=409,
                    detail=f"Story shape update refused: {str(cause)}"
                )
        
        raise HTTPException(status_code=404, detail=f"No running workflow found for episode {episode_id}")
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating story shape: {str(e)}")


# Mark update_story_shape to allow members
update_story_shape.allow_members = True

@app.get("/api/episodes/{episode_id}/budget", response_model=BudgetResponse)
async def get_budget_status(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get budget status for an episode (read-only). Member or admin.
    
    Requires: Authorization header with admin secret OR session cookie.
    """
    # Member or admin access (read-only view of episode budget)
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    
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


# Mark get_budget_status to allow members
get_budget_status.allow_members = True

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


class SyncBudgetLinesRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500, description="Who approved the new caps and why")
    dry_run: bool = Field(False, description="Report the changes without writing them")


@app.post("/api/budget/{episode_id}/lines/sync")
async def sync_budget_lines(
    episode_id: str,
    request: SyncBudgetLinesRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Re-apply the gate policy / env line caps to an episode's existing budget lines.

    Lines are created once, so lines made by an earlier (failed) start keep stale caps. This is the
    sanctioned way to move them (BudgetLedger.sync_episode_lines): never below spent + reserved,
    never above the episode stop in total, each change logged as a cap_sync transaction and in
    the audit log. Requires admin auth.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    validate_episode_id(episode_id)
    from hfvg.budget import BudgetLedger

    try:
        result = await BudgetLedger(DATABASE_PATH).sync_episode_lines(
            episode_id, reason=request.reason, strict=True, dry_run=request.dry_run)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not request.dry_run and result["changed"]:
        import json as _json
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
                (episode_id, "budget_lines_sync",
                 _json.dumps({"reason": request.reason, "changed": result["changed"]}), "admin"))
            await db.commit()
    return {"success": True, "episode_id": episode_id, **result}


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


@app.get("/api/budget/all-balances")
async def get_all_provider_balances(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Check all provider balances (ElevenLabs live, Higgsfield derived).
    
    Returns current balance, source (live/derived/manual), age, and any warnings or errors.
    Requires admin auth.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.balance_monitor import BalanceMonitor
    
    monitor = BalanceMonitor(DATABASE_PATH)
    result = await monitor.check_all_balances()
    return result.to_dict()


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


# ===================================================================
# MASTER DASHBOARD AND USER MANAGEMENT
# ===================================================================

@app.get("/api/dashboard")
async def get_dashboard(
    user_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Master admin dashboard: comprehensive view of users, spend, and activity (admin only).
    
    Returns:
    - User stats: active users (today, 7d, 30d), total users
    - Spend aggregations: total, per user, per episode, per stage, per provider/model
    - Cost accuracy: actual vs estimated
    - Revision metrics: revision vs first-pass spend per user
    - Per-user drill-down data
    - Monthly run-rate projections (informational only)
    - Spend milestone notices
    
    Requires: Admin authorization.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    import aiosqlite
    from hfvg.users import UserManager, LEGACY_USER_ID
    from hfvg.cost_tracking import CostTracker
    from hfvg.budget import BudgetLedger
    from hfvg.pricing import micros_to_usd_str
    from datetime import datetime as dt, timedelta, timezone
    from collections import defaultdict
    
    user_mgr = UserManager(DATABASE_PATH)
    cost_tracker = CostTracker(DATABASE_PATH)
    ledger = BudgetLedger(DATABASE_PATH)
    
    now = dt.now(timezone.utc)
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        # ===== USER STATS =====
        
        # Total users
        async with db.execute("SELECT COUNT(*) FROM users WHERE disabled = 0") as cur:
            total_users = (await cur.fetchone())[0]
        
        # Active users (by last_active timestamp)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        
        async with db.execute("""
            SELECT COUNT(*) FROM users 
            WHERE disabled = 0 AND last_active >= ?
        """, (today.isoformat(),)) as cur:
            active_today = (await cur.fetchone())[0]
        
        async with db.execute("""
            SELECT COUNT(*) FROM users 
            WHERE disabled = 0 AND last_active >= ?
        """, (week_ago.isoformat(),)) as cur:
            active_7d = (await cur.fetchone())[0]
        
        async with db.execute("""
            SELECT COUNT(*) FROM users 
            WHERE disabled = 0 AND last_active >= ?
        """, (month_ago.isoformat(),)) as cur:
            active_30d = (await cur.fetchone())[0]
        
        # ===== SPEND AGGREGATIONS =====
        
        # Build query with optional filters
        query_base = """
            SELECT j.job_id, j.episode_id, j.shot_id, j.user_id, j.model, j.tier,
                   j.line_name, j.usd_micros, j.actual_usd_micros, j.status,
                   j.verdict, j.used, j.created_at
            FROM job_ledger j
            WHERE 1=1
        """
        params = []
        
        if user_id:
            query_base += " AND j.user_id = ?"
            params.append(user_id)
        
        if start_date:
            query_base += " AND date(j.created_at) >= ?"
            params.append(start_date)
        
        if end_date:
            query_base += " AND date(j.created_at) <= ?"
            params.append(end_date)
        
        query_base += " ORDER BY j.created_at"
        
        async with db.execute(query_base, tuple(params)) as cursor:
            jobs = [dict(row) async for row in cursor]
        
        # Process aggregations (actual cost only where recorded, not falling back to estimate)
        total_estimated = 0
        total_actual = 0
        user_spend = defaultdict(lambda: {"estimated": 0, "actual": 0, "episodes": set(), 
                                          "revision": 0, "first_pass": 0, "jobs": 0})
        episode_spend = defaultdict(lambda: {"estimated": 0, "actual": 0, "user_id": None})
        stage_spend = defaultdict(lambda: {"estimated": 0, "actual": 0})
        provider_spend = defaultdict(lambda: {"estimated": 0, "actual": 0})
        model_spend = defaultdict(lambda: {"estimated": 0, "actual": 0})
        failed_spend_estimated = 0
        failed_spend_actual = 0
        daily_spend = defaultdict(lambda: {"estimated": 0, "actual": 0})
        
        # Stage mapping
        stage_map = {
            "L1_refs": "references",
            "L2_drafts": "draft_stills",
            "L3_final_stills": "final_stills",
            "L4_video": "video_clips",
            "L5_audio": "audio",
            "L6_reserve": "assembly",
            "L7_revision": "revisions",
            "el_vo_takes": "voice_over",
            "el_sfx": "sound_effects",
            "el_music_one_take": "music",
        }
        
        for job in jobs:
            uid = job["user_id"] or LEGACY_USER_ID
            ep_id = job["episode_id"]
            estimated = job["usd_micros"]
            actual = job["actual_usd_micros"]  # May be None
            
            total_estimated += estimated
            if actual is not None:
                total_actual += actual
            
            # Per-user
            user_spend[uid]["estimated"] += estimated
            if actual is not None:
                user_spend[uid]["actual"] += actual
            user_spend[uid]["episodes"].add(ep_id)
            user_spend[uid]["jobs"] += 1
            
            if actual is not None:
                if job["tier"] in ("revision", "retry"):
                    user_spend[uid]["revision"] += actual
                else:
                    user_spend[uid]["first_pass"] += actual
            
            # Per-episode
            episode_spend[ep_id]["estimated"] += estimated
            if actual is not None:
                episode_spend[ep_id]["actual"] += actual
            episode_spend[ep_id]["user_id"] = uid
            
            # Per-stage
            stage = stage_map.get(job["line_name"], job["line_name"])
            stage_spend[stage]["estimated"] += estimated
            if actual is not None:
                stage_spend[stage]["actual"] += actual
            
            # Per-provider and model
            model_name = job["model"]
            if model_name:
                if "kling" in model_name.lower() or "seedance" in model_name.lower():
                    provider_spend["higgsfield"]["estimated"] += estimated
                    if actual is not None:
                        provider_spend["higgsfield"]["actual"] += actual
                    model_spend[model_name]["estimated"] += estimated
                    if actual is not None:
                        model_spend[model_name]["actual"] += actual
                elif job["line_name"] and job["line_name"].startswith("el_"):
                    provider_spend["elevenlabs"]["estimated"] += estimated
                    if actual is not None:
                        provider_spend["elevenlabs"]["actual"] += actual
                    model_spend["elevenlabs_audio"]["estimated"] += estimated
                    if actual is not None:
                        model_spend["elevenlabs_audio"]["actual"] += actual
            
            # Failed jobs
            if job["verdict"] in ("blocked", "failed", "rejected") or job["used"] == 0:
                failed_spend_estimated += estimated
                if actual is not None:
                    failed_spend_actual += actual
            
            # Daily aggregation
            try:
                created = dt.fromisoformat(job["created_at"].replace("Z", "+00:00"))
                day_key = created.date().isoformat()
                daily_spend[day_key]["estimated"] += estimated
                if actual is not None:
                    daily_spend[day_key]["actual"] += actual
            except (ValueError, AttributeError):
                pass
        
        # Format user breakdown
        users_list = []
        for uid, data in user_spend.items():
            user_info = await user_mgr.get_user(uid)
            revision_pct = (data["revision"] / data["actual"] * 100) if data["actual"] > 0 else 0
            users_list.append({
                "user_id": uid,
                "name": user_info["name"] if user_info else uid,
                "email": user_info["email"] if user_info else None,
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
                "episode_count": len(data["episodes"]),
                "job_count": data["jobs"],
                "revision_spend_usd_micros": data["revision"],
                "first_pass_spend_usd_micros": data["first_pass"],
                "revision_percent": round(revision_pct, 1),
            })
        
        # Sort by actual spend (top spenders first)
        users_list.sort(key=lambda x: x["actual_usd_micros"], reverse=True)
        
        # Format episode breakdown
        episodes_list = []
        for ep_id, data in episode_spend.items():
            episodes_list.append({
                "episode_id": ep_id,
                "user_id": data["user_id"],
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
                "variance_usd_micros": data["actual"] - data["estimated"],
                "variance_percent": round(
                    ((data["actual"] - data["estimated"]) / data["estimated"] * 100) 
                    if data["estimated"] > 0 else 0, 1
                ),
            })
        episodes_list.sort(key=lambda x: x["actual_usd_micros"], reverse=True)
        
        # Format stage breakdown (include all standard stages, even at zero)
        all_stages = ["references", "draft_stills", "final_stills", "video_clips", 
                      "voice_over", "sound_effects", "music", "assembly", "QC", "revisions"]
        stages_list = []
        for stage in all_stages:
            data = stage_spend.get(stage, {"estimated": 0, "actual": 0})
            stages_list.append({
                "stage": stage,
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
            })
        
        # Format provider breakdown
        providers_list = [
            {
                "provider": prov,
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
            }
            for prov, data in provider_spend.items()
        ]
        
        # Format model breakdown
        models_list = [
            {
                "model": model,
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
            }
            for model, data in sorted(model_spend.items(), 
                                     key=lambda x: x[1]["actual"], reverse=True)
        ]
        
        # Daily time series (last 30 days)
        daily_series = []
        for i in range(30):
            day = (now - timedelta(days=29-i)).date().isoformat()
            data = daily_spend.get(day, {"estimated": 0, "actual": 0})
            daily_series.append({
                "date": day,
                "estimated_usd_micros": data["estimated"],
                "actual_usd_micros": data["actual"],
                "estimated_usd": micros_to_usd_str(data["estimated"]),
                "actual_usd": micros_to_usd_str(data["actual"]),
            })
        
        # Get monthly run-rate projections
        org_runrate = await cost_tracker.get_monthly_runrate()
        
        # Get recent spend milestones
        milestones = await cost_tracker.get_recent_milestones(limit=10)
        
        # Balance status
        balance_status = await ledger.get_balance_status()
        
        # Cost accuracy metrics
        jobs_with_actual = sum(1 for j in jobs if j["actual_usd_micros"] is not None)
        total_jobs_count = len(jobs)
        coverage_pct = (jobs_with_actual / total_jobs_count * 100) if total_jobs_count > 0 else 0
        
        # When actual coverage is 0%, show estimated as headline
        if jobs_with_actual == 0:
            headline_spend = total_estimated
            headline_label = "estimated"
            display_actual = None
            display_variance = None
            display_variance_pct = None
        else:
            headline_spend = total_actual
            headline_label = "actual"
            display_actual = total_actual
            display_variance = total_actual - total_estimated
            display_variance_pct = (display_variance / total_estimated * 100) if total_estimated > 0 else 0
        
        return {
            "dry_run": dry_run,
            "timestamp": now.isoformat(),
            "unit": "usd_micros",
            
            "user_stats": {
                "total_users": total_users,
                "active_today": active_today,
                "active_7d": active_7d,
                "active_30d": active_30d,
            },
            
            "spend_summary": {
                "headline_spend_usd_micros": headline_spend,
                "headline_spend_usd": micros_to_usd_str(headline_spend),
                "headline_label": headline_label,
                "total_estimated_usd_micros": total_estimated,
                "total_actual_usd_micros": display_actual,
                "total_estimated_usd": micros_to_usd_str(total_estimated),
                "total_actual_usd": micros_to_usd_str(display_actual) if display_actual is not None else None,
                "variance_usd_micros": display_variance,
                "variance_usd": micros_to_usd_str(abs(display_variance)) if display_variance is not None else None,
                "variance_percent": round(display_variance_pct, 1) if display_variance_pct is not None else None,
                "failed_spend_estimated_usd_micros": failed_spend_estimated,
                "failed_spend_actual_usd_micros": failed_spend_actual if jobs_with_actual > 0 else None,
                "failed_spend_usd": micros_to_usd_str(failed_spend_actual) if jobs_with_actual > 0 else None,
                "cost_tracking_coverage_percent": round(coverage_pct, 1),
            },
            
            "users": users_list,
            "episodes": episodes_list[:20],  # Top 20 episodes
            "stages": stages_list,
            "providers": providers_list,
            "models": models_list,
            "daily_series": daily_series,
            
            "monthly_runrate": org_runrate,
            "milestones": milestones,
            
            "balance_status": {
                "manual_balance_usd_micros": balance_status.get("manual_balance_usd_micros"),
                "manual_balance_usd": micros_to_usd_str(balance_status["manual_balance_usd_micros"]) 
                    if balance_status.get("manual_balance_usd_micros") else None,
                "set_at": balance_status.get("set_at"),
                "set_by": balance_status.get("set_by"),
            },
            
            "recent_jobs": jobs[-50:],  # Last 50 jobs
        }


@app.get("/api/users")
async def list_users(
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """List all users (admin only)."""
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.users import UserManager
    user_mgr = UserManager(DATABASE_PATH)
    users = await user_mgr.list_users(include_disabled=False)
    return {"users": users}


class CreateUserRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., min_length=3, max_length=200)
    role: str = Field(default="member", pattern="^(admin|member)$")
    password: str | None = Field(None, min_length=8, description="Optional: if omitted, generates invite link")


@app.post("/api/users")
async def create_user(
    request: CreateUserRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Create a new user (admin only).
    
    If password is provided, user can login immediately.
    If password is omitted, generates a single-use invite link (72h expiry).
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.users import UserManager
    user_mgr = UserManager(DATABASE_PATH)
    
    try:
        user = await user_mgr.create_user(
            name=request.name,
            email=request.email,
            role=request.role,
            password=request.password
        )
        
        response = {"success": True, "user": user}
        
        # If no password provided, generate invite link
        if not request.password:
            token = await user_mgr.create_password_reset_token(user["user_id"])
            # Build the set-password URL (frontend will render the form)
            base_url = os.getenv("STUDIO_BASE_URL", "http://localhost:3000")
            invite_url = f"{base_url}/set-password?token={token}"
            # If no password provided, generate invite link
        if not request.password:
            token = await user_mgr.create_password_reset_token(user["user_id"])
            # Build the set-password URL (frontend will render the form)
            base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")
            invite_url = f"{base_url}/set-password?token={token}"
            response["invite_url"] = invite_url
            response["message"] = "User created. Share the invite link (valid for 72 hours)."
        
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


@app.post("/api/users/me/change-password")
async def change_own_password(
    request: ChangePasswordRequest,
    studio_admin_token: str | None = Cookie(None),
):
    """Change current user's password (member or admin)."""
    user_id, role = await verify_member_or_admin_cookie(studio_admin_token)
    
    from hfvg.users import UserManager
    user_mgr = UserManager(DATABASE_PATH)
    
    # Get user details to verify current password
    user = await user_mgr.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Verify current password
    auth = await user_mgr.authenticate_password(user["email"], request.current_password)
    if not auth:
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    
    # Change password
    success = await user_mgr.change_password(user_id, request.new_password)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to change password")
    
    return {"success": True, "message": "Password changed successfully"}
# Mark change_own_password to allow members
change_own_password.allow_members = True



@app.post("/api/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Generate a password reset link for a user (admin only).
    
    Returns a single-use, 72-hour expiring link that the user can use to set their password.
    """
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    from hfvg.users import UserManager
    user_mgr = UserManager(DATABASE_PATH)
    
    # Verify user exists
    user = await user_mgr.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Generate reset token
    token = await user_mgr.create_password_reset_token(user_id)
    
    # Build the set-password URL
    base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:3000")
    reset_url = f"{base_url}/set-password?token={token}"
    
    return {
        "success": True,
        "reset_url": reset_url,
        "expires_hours": 72,
        "message": "Password reset link generated (valid for 72 hours)"
    }


class SetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1, description="Password reset token from invite/reset link")
    new_password: str = Field(..., min_length=8, description="New password to set")


@app.post("/api/auth/set-password")
async def set_password_with_token(request: SetPasswordRequest):
    """
    Set password using a reset token (public endpoint, no auth required).
    
    Consumes a single-use token from an invite or password reset link.
    """
    from hfvg.users import UserManager
    user_mgr = UserManager(DATABASE_PATH)
    
    success = await user_mgr.set_password_with_token(request.token, request.new_password)
    
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, or already used token"
        )
    
    return {
        "success": True,
        "message": "Password set successfully. You can now log in."
    }


@app.get("/api/users/{user_id}")
async def get_user_details(
    user_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Get user details with spend summary (admin only)."""
    if studio_admin_token:
        await verify_admin_cookie(studio_admin_token)
    else:
        verify_admin_secret(authorization)
    
    import aiosqlite
    from hfvg.users import UserManager
    from hfvg.cost_tracking import CostTracker
    from hfvg.pricing import micros_to_usd_str
    from collections import defaultdict
    
    user_mgr = UserManager(DATABASE_PATH)
    cost_tracker = CostTracker(DATABASE_PATH)
    
    user = await user_mgr.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get user's spending data (actual cost only, not falling back to estimate)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute("""
            SELECT episode_id, SUM(actual_usd_micros) as spend
            FROM job_ledger
            WHERE user_id = ? AND actual_usd_micros IS NOT NULL
            GROUP BY episode_id
            ORDER BY spend DESC
        """, (user_id,)) as cursor:
            episodes = []
            async for row in cursor:
                ep_id, spend = row
                episodes.append({
                    "episode_id": ep_id,
                    "spend_usd_micros": spend,
                    "spend_usd": micros_to_usd_str(spend)
                })
        
        # Get monthly spend
        runrate = await cost_tracker.get_monthly_runrate(user_id=user_id)
    
    user["episodes"] = episodes
    user["monthly_runrate"] = runrate
    
    return user


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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    
    import aiosqlite
    
    # Get shots from database
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
                    "pre_approved": bool(((qc_results or {}).get("still_qc") or {}).get("pre_approved")),
                })
    
    # Merge beat context from workflow state if available
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        state = await handle.query("get_state")
        
        # Get beat context from shot_beats map (exposed by get_state query)
        shot_beats = state.get("shot_beats", {})
        
        # Merge into shots
        for shot in shots:
            shot_id = shot["shot_id"]
            if shot_id in shot_beats:
                beat_info = shot_beats[shot_id]
                if beat_info.get("beat_id"):
                    shot["beat_id"] = beat_info["beat_id"]
                    shot["beat_purpose"] = beat_info.get("beat_purpose")
                    shot["beat_emotional_tone"] = beat_info.get("beat_emotional_tone")
    except Exception:
        # Workflow not running or no beat context; shots already complete without beat fields
        pass
    
    return {
        "episode_id": episode_id,
        "shots": shots,
    }


# Mark get_shots to allow members
get_shots.allow_members = True

@app.post("/api/episodes/{episode_id}/shots/{shot_id}/approve")
async def approve_still(
    episode_id: str,
    shot_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Approve still for a shot (sends signal to workflow). Member or admin.
    
    Requires: Auth
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
            from temporalio.service import RPCError, RPCStatusCode
            if (workflow_type == "canary" and isinstance(desc_err, RPCError)
                    and desc_err.status == RPCStatusCode.NOT_FOUND):
                # No canary for this shot: it is an episode shot, so this is the per-shot G2 decision
                # "approve" (G-GATE-1 map) on the episode workflow; refusals (not at G2, retry pending,
                # G2 already approved) come back as 409.
                result = await _execute_episode_update(episode_id, "approve_shot", [shot_id], "Approve")
                await _audit(episode_id, "approve_shot", f"Shot {shot_id} approved at G2")
                return {"success": True, "shot_id": shot_id, "workflow_type": "episode",
                        "message": result.get("message") if isinstance(result, dict) else "Shot approved"}
            # If describe fails otherwise, try to send signal anyway (might work)
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


# Mark approve_still to allow members
approve_still.allow_members = True

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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


# Mark reject_still to allow members
reject_still.allow_members = True

class AcceptClipRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the needs_review clip is kept as is (audited)")
    take: int | None = Field(None, ge=0, description="Keep an earlier clip take instead of the latest "
                             "(0 = the clip before retry 1, n = clip retry n); see get_state clip_takes")


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/accept-clip")
async def accept_clip(
    episode_id: str,
    shot_id: str,
    request: AcceptClipRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Human review of a clip escalated as needs_review: keep it as is (no spend). The shot becomes
    completed and the mute edit is rebuilt with it. The local clip file is resolved from the shot
    child workflow (``clip_path``) and must exist on disk; refusals -> 409.

    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    clip_path = None
    if request.take is not None:
        # GAP-18: an earlier take; its local file must still be on disk (resolved from the workflow)
        try:
            state = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query("get_state")
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Could not read episode state: {e}")
        take = ((state.get("clip_takes") or {}).get(shot_id) or {}).get(str(request.take))
        if not take:
            raise HTTPException(status_code=409, detail=f"No clip take {request.take} for {shot_id}")
        if not take.get("clip_path") or not Path(take["clip_path"]).is_file():
            raise HTTPException(status_code=409,
                                detail=f"Local clip for {shot_id} take {request.take} not found on disk: "
                                       f"{take.get('clip_path')}")
    else:
        try:
            state = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query("get_state")
            child_id = (state.get("shot_workflow_ids") or {}).get(shot_id)
            if child_id:
                child = await temporal_client.get_workflow_handle(child_id).query("get_state")
                clip_path = child.get("clip_path")
        except Exception:
            clip_path = None  # the workflow update re-checks and refuses without a recorded clip
        if clip_path and not Path(clip_path).is_file():
            raise HTTPException(status_code=409, detail=f"Local clip for {shot_id} not found on disk: {clip_path}")
    args = [shot_id, request.reason.strip(), clip_path]
    if request.take is not None:
        args.append(request.take)
    result = await _execute_episode_update(episode_id, "accept_clip_review", args, "Clip accept")

    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "accept_clip", f"Shot {shot_id} needs_review clip kept"
             f"{'' if request.take is None else f' (take {request.take})'}: {request.reason.strip()}",
             "admin")
        )
        await db.commit()

    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result,
            "message": f"Clip {shot_id} accepted; the edit is rebuilt with it"}


# Mark accept_clip to allow members
accept_clip.allow_members = True

async def _g2_headcount(episode_id: str, handle, overrides: dict[str, str] | None = None) -> dict:
    """GAP-27 headcount report over the kept still of every shot (fails closed if unreadable)."""
    from hfvg.qc.g2_headcount import g2_headcount_report
    try:
        versions = await handle.query("still_versions") or {}
        state = await handle.query("get_state") or {}
        plans = {}
        for shot_id, row in versions.items():
            v = (row or {}).get("current_version")
            plans[shot_id] = await handle.query("still_version_plan", args=[shot_id, v]) if v else None
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"G2 headcount check unavailable (fails closed): {e}")
    return await g2_headcount_report(episode_id, versions if isinstance(versions, dict) else {},
                                     plans, (state or {}).get("shot_decisions") if isinstance(state, dict) else {},
                                     data_dir=os.getenv("DATA_DIR", "./data"),
                                     episodes_dir=os.getenv("EPISODES_DIR", "./data/episodes"),
                                     overrides=overrides)


@app.get("/api/episodes/{episode_id}/g2-headcount")
async def get_g2_headcount(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-27: per shot, animals counted in the kept still vs the declared cast (read-only, no spend)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    return await _g2_headcount(episode_id, temporal_client.get_workflow_handle(episode_workflow_id(episode_id)))


class HeadcountRequest(BaseModel):
    version: int = Field(..., ge=1)
    cast: int = Field(..., ge=0)
    duck: int = Field(..., ge=0)
    other: int = Field(0, ge=0, description="any other animal in frame")
    note: str = Field("", max_length=1000)


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/headcount")
async def record_still_headcount(
    episode_id: str,
    shot_id: str,
    request: HeadcountRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-27: record a reviewer's animal count for one still version (used by the G2 headcount check
    when no automatic counter is configured). The still file must be on disk. Audited."""
    from hfvg.qc.g2_headcount import record_headcount
    from hfvg.still_selection import outputs_dir, still_file
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", shot_id):
        raise HTTPException(status_code=422, detail="invalid shot id")
    if still_file(outputs_dir(episode_id, shot_id, os.getenv("DATA_DIR", "./data")), request.version) is None:
        raise HTTPException(status_code=409, detail=f"still v{request.version} of {shot_id} not on disk")
    row = record_headcount(episode_id, shot_id, request.version,
                           {"cast": request.cast, "duck": request.duck, "other": request.other},
                           by="admin", note=request.note.strip(),
                           episodes_dir=os.getenv("EPISODES_DIR", "./data/episodes"))
    await _audit(episode_id, "g2_headcount",
                 f"{shot_id} v{request.version}: cast={request.cast} duck={request.duck} "
                 f"other={request.other} {request.note.strip()}")
    return {"success": True, "shot_id": shot_id, "version": request.version, "headcount": row}


class SelectStillRequest(BaseModel):
    version: int = Field(..., ge=1, description="Still version to keep at G2 (1 = original, n = retry n-1); "
                         "see GET /api/episodes/{id}/still-versions")
    reason: str = Field(..., min_length=1, max_length=1000, description="Why this version is kept (audited)")


async def _select_still_checked(episode_id: str, shot_id: str, version: int) -> tuple[dict | None, list[str]]:
    from hfvg.still_selection import check_selection
    live = os.getenv("DRY_RUN", "true").lower() != "true"
    try:
        return await check_selection(temporal_client, episode_id, episode_workflow_id(episode_id), shot_id,
                                     version, live=live, data_dir=os.getenv("DATA_DIR", "./data"))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not read still versions: {e}")


@app.get("/api/episodes/{episode_id}/still-versions")
async def get_still_versions(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-26: every still version per shot (child workflow, status), the current one and any G2
    selection. Read-only."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        return await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query("still_versions")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not read still versions: {e}")


# Mark get_still_versions to allow members
get_still_versions.allow_members = True

@app.post("/api/episodes/{episode_id}/shots/{shot_id}/select-still")
async def select_still(
    episode_id: str,
    shot_id: str,
    request: SelectStillRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-26 G2 decision: keep an EARLIER (or any landed) still version of a shot instead of the latest
    re-roll. That version's still and _end frame are then used for the clip launch, and its child
    records the DB row, manifest outcome and ledger rows; the other versions end superseded at G2.
    Refused (409) unless the version's file(s) are on disk, its child still waits at G2 with a landed
    still, and its prompt passes preflight (precheck, lint kit, moderation hard rules, refs on disk).
    No spend. Does NOT approve G2.

    Requires: Authorization header with admin secret OR session cookie.
    """
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    reason = request.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    plan, problems = await _select_still_checked(episode_id, shot_id, request.version)
    if problems:
        raise HTTPException(status_code=409, detail=f"Cannot select {shot_id} v{request.version}: "
                                                    + "; ".join(problems))
    result = await _execute_episode_update(episode_id, "select_still_version",
                                           [shot_id, request.version, reason], "Still select")
    await _audit(episode_id, "select_still_version",
                 f"Shot {shot_id} keeps still v{request.version} ({plan.get('workflow_id')}) instead of "
                 f"v{result.get('previous_version') if isinstance(result, dict) else '?'}: {reason}")
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "version": request.version,
            "result": result,
            "message": f"{shot_id} will use still v{request.version} at G2 (G2 itself is NOT approved)"}


# Mark select_still to allow members
select_still.allow_members = True

class ResyncPlansRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000, description="Why the package is re-synced (audited)")


@app.post("/api/episodes/{episode_id}/resync-plans")
async def resync_plans(
    episode_id: str,
    request: ResyncPlansRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-28: at G2 (before approval) re-parse the episode package (BEATMAP.md + shots.json, the
    same parser and path the episode used at start) and put the corrected TEXT fields in force:
    still re-rolls use the new prompt/end_prompt, and every still child parked at G2 gets the new
    clip_prompt/qc_fail_if for its clip submit and clip QC. Structural changes (shot set, shot_meta,
    refs, model, duration, end frame) are refused with 409 (they need a new G1). No spend; does NOT
    approve G2. Check the result with GET /api/episodes/{id}/clip-plans."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    reason = request.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    result = await _execute_episode_update(episode_id, "resync_shot_plans", [reason], "Plan resync")
    changed = result.get("changed", {}) if isinstance(result, dict) else {}
    await _audit(episode_id, "resync_shot_plans",
                 f"G2 package resync ({reason}): " + ", ".join(f"{k}[{'/'.join(v)}]" for k, v in changed.items()))
    return {"success": True, "episode_id": episode_id, "result": result,
            "message": f"{len(changed)} shot plan(s) re-synced (G2 itself is NOT approved)"}


# Mark resync_plans to allow members
resync_plans.allow_members = True

@app.get("/api/episodes/{episode_id}/clip-plans")
async def get_clip_plans(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-28: per shot, the clip prompt and qc_fail_if its kept still's clip will be submitted and
    QC'd with, plus the clip-prompt lint result (same gate as the paid clip submit). Read-only."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
    try:
        plans = await handle.query("clip_plans")
        versions = await handle.query("still_versions")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not read clip plans: {e}")
    from hfvg.still_selection import lint_problems
    live = os.getenv("DRY_RUN", "true").lower() != "true"
    for sid, row in plans.items():
        meta = None
        try:
            vp = await handle.query("still_version_plan", args=[sid, versions.get(sid, {}).get("current_version") or 1])
            meta = (vp or {}).get("shot_meta")
        except Exception:
            pass
        row["lint_problems"] = lint_problems(episode_id, sid, row.get("clip_prompt") or "", meta, [], live, kind="clip")
    return plans


# Mark get_clip_plans to allow members
get_clip_plans.allow_members = True

async def _paid_gate_problem(episode_id: str) -> str | None:
    """GAP-65: the submit-time live gates (smoke test freshness, GC.01 manual balance freshness),
    checked by the API BEFORE a retry/recompose update is sent. The workflow counts an attempt as soon
    as the update is accepted, so a submit refused later by one of these gates (no spend) still used up
    a bounded per-shot retry/recompose. None = the gates would pass now (dry run: no gates)."""
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return None
    from hfvg.budget import BudgetLedger, balance_freshness
    from hfvg.studio_db import check_smoke_test_passed
    if os.getenv("SMOKE_TEST_OVERRIDE", "false").lower() != "true":
        ok, msg = await check_smoke_test_passed(DATABASE_PATH, episode_id)
        if not ok:
            return f"smoke test gate: {msg} (run POST /api/episodes/{episode_id}/smoke-test?live=true)"
    fresh = balance_freshness(await BudgetLedger(DATABASE_PATH).get_balance_status())
    if not fresh.get("ok"):
        return str(fresh.get("reason"))
    return None


async def _refuse_if_paid_gates_fail(episode_id: str, what: str) -> None:
    problem = await _paid_gate_problem(episode_id)
    if problem:
        raise HTTPException(status_code=409, detail=f"{what} refused before it was sent (GAP-65: nothing "
                                                    f"counted, no spend): {problem}")


async def _retry_clip_prompt_problem(episode_id: str, shot_id: str, prompt_override: str | None,
                                     clip_route: dict | None = None) -> str | None:
    """GAP-66: the clip prompt a retry-clip will SEND (override or plan prompt, plus the prompt kit's
    continuity locks) checked for the model's length limit and the clip lint BEFORE the update is sent.
    ep05v2 r12 V22: the plan prompt 2409 + injected duck locks = 3149 > Kling's 2500 was refused inside
    the child (no spend) after the workflow had already counted clip retry 3 of 3. None = OK. Live
    without a readable plan fails closed; dry run skips when the plan is unavailable."""
    live = os.getenv("DRY_RUN", "true").lower() != "true"
    plan = None
    if temporal_client:
        try:
            plan = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query(
                "retry_clip_plan", args=[shot_id])
        except Exception:
            plan = None
    if not plan or not plan.get("found", True):
        return "the shot's clip plan could not be read to preflight the prompt" if live else None
    prompt = (prompt_override or "").strip() or plan.get("clip_prompt") or ""
    clip_model = (clip_route or {}).get("model") or plan.get("clip_model") or "kling"  # GAP-72: routed model
    shot_meta = plan.get("shot_meta")
    from hfvg import preflight as _pf
    from hfvg.prompt_lint import PromptLintError as _PLE, inject_continuity_locks, lint_violations, load_lint_kit
    kit = None
    if shot_meta is not None:
        try:
            kit = load_lint_kit(episode_id)
        except _PLE as e:
            return f"prompt kit unreadable: {e}" if live else None
        prompt, _ = inject_continuity_locks(prompt, shot_meta, kit, "clip")
    elif live:
        return "the shot has no lint metadata (shot_meta) to preflight the clip prompt"
    bad = _pf.prompt_limit_violations(shot_id, still_prompt="", clip_prompt=prompt, clip_model_alias=clip_model)
    if bad:
        return "; ".join(v.message for v in bad)
    if kit is not None and live:
        problems = lint_violations(prompt, shot_meta, kit, "clip")
        if problems:
            return "clip prompt lint: " + "; ".join(str(x) for x in problems)
    return None


async def _refuse_retry_clip_prompt(episode_id: str, shot_id: str, prompt_override: str | None,
                                    clip_route: dict | None = None) -> None:
    problem = await _retry_clip_prompt_problem(episode_id, shot_id, prompt_override, clip_route)
    if problem:
        raise HTTPException(status_code=409, detail="Preflight refused the clip retry before it was sent "
                            f"(GAP-66: nothing counted, no spend): {problem}")


class RetryClipRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the clip is being re-run (audited)")
    prompt_override: str | None = None
    # GAP-64: clip length for this retry only (whole seconds; kling 3-15, seedance 4-15); None = plan
    duration_s: int | None = Field(None, ge=3, le=15)
    # GAP-72: owner-ordered clip route for this clip-only re-take (model: kling|seedance,
    # seedance_resolution); same keys/validation as the recompose clip_route (GAP-45)
    clip_route: dict | None = None


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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    await _refuse_if_paid_gates_fail(episode_id, "Clip retry")  # GAP-65
    if request.clip_route is not None and not isinstance(request.clip_route.get("model", "kling"), str):
        raise HTTPException(status_code=422, detail="invalid clip_route: model must be a string")
    # GAP-72: the route itself is validated against the launched params by the retry_clip update
    # validator, which rejects before the update is accepted (nothing counted, no spend)
    await _refuse_retry_clip_prompt(episode_id, shot_id, request.prompt_override, request.clip_route)  # GAP-66

    if request.clip_route is not None:  # GAP-72 (5-arg shape)
        args = [shot_id, request.reason, request.prompt_override, request.duration_s, dict(request.clip_route)]
    else:
        args = ([shot_id, request.reason, request.prompt_override]
                + ([request.duration_s] if request.duration_s is not None else []))  # GAP-64 (old 3-arg shape kept)
    result = await _execute_episode_update(episode_id, "retry_clip", args, "Clip retry")

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


_REF_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# Mark retry_clip to allow members
retry_clip.allow_members = True

class RecomposeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000, description="Who asked and why (audited)")
    prompt: str = Field(..., min_length=1, description="The NEW still prompt (replaces the shot's prompt)")
    ref_keys: list[str] = Field(..., min_length=1, description="Explicit refs (refs/<key>.png); replaces the plan's")
    clip_prompt: str | None = Field(None, description="Clip prompt for the new still (default: the still prompt)")
    shot_meta: dict | None = Field(None, description="GAP-32: change the cast of the new composition "
                                   "(only count, characters, duck); lint, headcount and clip QC follow it")
    clip_route: dict | None = Field(None, description="GAP-45: owner-ordered clip route for the new composition "
                                    "(model: kling|seedance, seedance_resolution: 480p|720p|1080p)")
    plate_manifest: str | None = Field(None, description="GAP-53: a hfvg.plate_composite manifest (inside the "
                                       "episode dir) whose PASSED plate becomes the start still (no still spend)")
    plate_approved_by: str | None = Field(None, max_length=200, description="GAP-53: who approved the plate")


def _recompose_plate(episode_id: str, shot_id: str, manifest_path: str, approved_by: str | None) -> dict:
    """GAP-53: load + verify a plate manifest for a recompose (inside the episode dir, passed, this shot,
    PNG unchanged).  Raises HTTPException before any spend."""
    from hfvg.plate_composite import PlateSpecError, load_passed_manifest, sha256_file
    ep_dir = (Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id).resolve()
    mp = Path(manifest_path)
    mp = (mp if mp.is_absolute() else ep_dir / mp).resolve()
    if ep_dir not in mp.parents:
        raise HTTPException(status_code=422, detail="plate_manifest must be inside the episode dir")
    try:
        m = load_passed_manifest(mp, shot_id)
    except PlateSpecError as e:
        raise HTTPException(status_code=409, detail=f"plate refused before any spend: {e}")
    png = Path(m["path"]).resolve()
    if ep_dir not in png.parents:
        raise HTTPException(status_code=422, detail="plate PNG must be inside the episode dir")
    return {"path": str(png), "sha256": m["sha256"], "manifest": str(mp), "manifest_sha256": sha256_file(mp),
            "approved_by": (approved_by or "").strip() or "plate_composite",
            "checks": {"passed": True, "count": m.get("count"),
                       "layers": {r["name"]: r.get("ratio_to_duck") for r in m.get("layers", [])}}}


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/recompose")
async def recompose_shot(
    episode_id: str,
    shot_id: str,
    request: RecomposeRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-21: re-shoot one shot after G2 with a new composition (new still prompt + explicit refs).
    The refs must exist under the episode's refs/ dir; the prompts and the ref count are preflighted
    against the model limits BEFORE any spend. The new still parks for /recompose/decision."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    await _refuse_if_paid_gates_fail(episode_id, "Recompose")  # GAP-65
    refs_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "refs"
    refs: list[str] = []
    for key in request.ref_keys:
        if not _REF_KEY_RE.match(key):
            raise HTTPException(status_code=422, detail=f"invalid ref key {key!r}")
        path = refs_dir / f"{key}.png"
        if not path.is_file():
            raise HTTPException(status_code=409, detail=f"ref {key!r} not found: {path}")
        refs.append(str(path.resolve()))
    if len(set(refs)) != len(refs):
        raise HTTPException(status_code=422, detail="duplicate ref keys")
    plate = (_recompose_plate(episode_id, shot_id, request.plate_manifest, request.plate_approved_by)
             if request.plate_manifest else None)
    from hfvg import preflight as _pf
    from hfvg.config import max_refs_for, still_model_path
    cap = max_refs_for(still_model_path())
    if len(refs) > cap:
        raise HTTPException(status_code=409, detail=f"{len(refs)} refs > still model cap {cap}")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as _EW
    meta_problem = _EW.recompose_meta_problem(request.shot_meta)
    if meta_problem:
        raise HTTPException(status_code=422, detail=f"invalid shot_meta: {meta_problem}")
    clip_model = "kling"
    shot_meta = None
    if temporal_client:
        try:
            plan = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query(
                "retry_prompt_plan", args=[shot_id, None])
            clip_model = plan.get("clip_model") or clip_model
            shot_meta = plan.get("shot_meta")
        except Exception:
            pass
    if request.clip_route is not None:
        # GAP-45: validate the route, then preflight/lint against the ROUTED clip model
        route_problem = _EW.clip_route_problem(request.clip_route, {"model": clip_model})
        if route_problem:
            raise HTTPException(status_code=422, detail=f"invalid clip_route: {route_problem}")
        clip_model = request.clip_route.get("model", clip_model)
    if request.shot_meta and shot_meta is not None:
        shot_meta = {**shot_meta, **request.shot_meta}  # GAP-32: lint the NEW cast
    still_prompt, sent_clip_prompt = request.prompt, request.clip_prompt
    if shot_meta is not None:
        # GAP-40: the paid submits inject the prompt kit's continuity locks; preflight (length) and lint
        # run on the prompts as they will be SENT, and the child gets the injected prompts
        from hfvg.prompt_lint import PromptLintError as _PLE, inject_continuity_locks, load_lint_kit as _llk
        try:
            _kit = _llk(episode_id)
        except _PLE as e:
            raise HTTPException(status_code=409, detail=f"Prompt lint refused the recompose before any spend: {e}")
        still_prompt, _ = inject_continuity_locks(request.prompt, shot_meta, _kit, "still")
        sent_clip_prompt, _ = inject_continuity_locks(request.clip_prompt or still_prompt, shot_meta, _kit, "clip")
    clip_prompt = sent_clip_prompt or still_prompt
    bad = _pf.prompt_limit_violations(shot_id, still_prompt=still_prompt,
                                      clip_prompt=clip_prompt,
                                      clip_model_alias=clip_model)
    if bad:
        raise HTTPException(status_code=409, detail="Preflight refused the recompose before any spend: "
                            + "; ".join(v.message for v in bad))
    # GAP-24: the same prompt + moderation lint the paid submits enforce, run here so a bad prompt is
    # refused at the route instead of failing inside the child (ep05full F06 recompose 1).
    problems = _recompose_lint_problems(episode_id, shot_id, still_prompt, clip_prompt, shot_meta,
                                        refs, clip_model)
    if problems:
        raise HTTPException(status_code=409, detail="Prompt lint refused the recompose before any spend: "
                            + "; ".join(problems))
    result = await _execute_episode_update(
        episode_id, "recompose_shot",
        [shot_id, request.reason.strip(), still_prompt, refs, list(request.ref_keys), sent_clip_prompt]
        + ([dict(request.shot_meta) if request.shot_meta else None,
            dict(request.clip_route) if request.clip_route else None, plate] if plate else
           [dict(request.shot_meta) if request.shot_meta else None, dict(request.clip_route)]
           if request.clip_route else ([dict(request.shot_meta)] if request.shot_meta else [])),
        "Recompose")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "recompose_shot",
             f"Shot {shot_id} recompose {result.get('recompose')} v{result.get('version')} "
             f"refs={request.ref_keys}"
             + (f" shot_meta={json.dumps(request.shot_meta, sort_keys=True)}" if request.shot_meta else "")
             + (f" clip_route={json.dumps(request.clip_route, sort_keys=True)}" if request.clip_route else "")
             + (f" plate={plate['sha256'][:12]} ({plate['manifest']})" if plate else "")
             + f": {request.reason.strip()}", "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result,
            "message": f"Recompose queued for {shot_id}; its still parks for /recompose/decision"}


def _recompose_lint_problems(episode_id: str, shot_id: str, still_prompt: str, clip_prompt: str,
                             shot_meta: dict | None, refs: list[str], clip_model: str) -> list[str]:
    """Live-mode lint of a recompose's still and clip prompts (prompt kit + playbook moderation hard
    rules). Fails closed when the shot's lint metadata is unavailable."""
    from hfvg import moderation_lint
    from hfvg.prompt_lint import PromptLintError, clip_lint_violations, lint_violations, load_lint_kit
    if shot_meta is None:
        return [f"no lint metadata for {shot_id} (workflow not queryable)"]
    try:
        kit = load_lint_kit(episode_id)
    except PromptLintError as e:
        return [str(e)]
    problems = [f"still: {p}" for p in lint_violations(still_prompt, shot_meta, kit)]
    problems += [f"clip: {p}" for p in clip_lint_violations(clip_prompt, shot_meta, kit)]
    for kind, prompt, model in (("still", still_prompt, None), ("clip", clip_prompt, clip_model)):
        rec = moderation_lint.evaluate(shot_id, prompt, shot_meta, kind=kind, model=model, refs=refs, live=False)
        problems += [f"{kind} moderation: {v}" for v in rec.get("hard_violations", [])]
    return problems


class ReopenRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000, description="Who asked and why (audited)")


@app.post("/api/episodes/{episode_id}/reopen")
async def reopen_episode(
    episode_id: str,
    request: ReopenRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-41: reopen a COMPLETED episode at G_FINAL_CUT. Reads the closed run's reopen_snapshot (a
    query, no spend) and starts a NEW run with the same workflow id that restores the state and parks at
    G_FINAL_CUT again (final cut un-approved). No generation is started; every later paid step goes
    through the normal recompose / retry_clip guards. Refused unless the run COMPLETED with its final
    cut approved and nothing in flight."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="a reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.service import RPCError, RPCStatusCode
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as _EW
    workflow_id = episode_workflow_id(episode_id)
    handle = temporal_client.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise
    if desc.status != WorkflowExecutionStatus.COMPLETED:
        raise HTTPException(status_code=409, detail=f"only a COMPLETED episode can be reopened (status: "
                            f"{desc.status.name if desc.status else None})")
    closed = temporal_client.get_workflow_handle(workflow_id, run_id=desc.run_id)
    try:
        snapshot = await closed.query("reopen_snapshot")
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"reopen snapshot unavailable (fails closed): {e}")
    reopen_from = {"snapshot": snapshot, "reason": request.reason.strip(), "from_run_id": desc.run_id}
    problem = _EW.reopen_problem(episode_id, reopen_from)
    if problem:
        raise HTTPException(status_code=409, detail=f"Cannot reopen {episode_id}: {problem}")
    attrs = snapshot["attrs"]
    new = await temporal_client.start_workflow(
        _EW.run,
        args=[episode_id, attrs.get("_beatmap_path"), bool(attrs.get("_dry_run", True)), None, None, None,
              reopen_from],
        id=workflow_id,
        task_queue="hfvg-tasks",
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "reopen_episode",
             f"reopened from run {desc.run_id} as {new.result_run_id}: {request.reason.strip()}", "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "workflow_id": workflow_id,
            "from_run_id": desc.run_id, "run_id": new.result_run_id,
            "message": f"{episode_id} reopened at G_FINAL_CUT (final cut un-approved)"}


# ---------------------------------------------------------------- GAP-79 reopen from the saved state
# ep05v2 r13: the dev server purged the COMPLETED v10 run after its 24 h retention, so /reopen (GAP-41)
# had no closed run to query. (1) The API now persists every episode's reopen_snapshot to
# <episode>/state/reopen_snapshot.json (periodic saver + POST .../snapshot), and (2) /reopen-from-saved
# reopens an episode whose workflow is GONE from that file, or from a reconstruction of the saved
# artifacts + DB (hfvg.saved_state). Refused while any workflow with the id exists (idempotent).
_gap79_log = __import__("logging").getLogger("hfvg.api.snapshots")
SNAPSHOT_SAVE_INTERVAL_S = float(os.getenv("SNAPSHOT_SAVE_INTERVAL_S", "300"))


# Mark reopen_episode to allow members
reopen_episode.allow_members = True

def _episodes_root() -> Path:
    return Path(os.getenv("EPISODES_DIR", "./data/episodes"))


async def _save_episode_snapshot(episode_id: str) -> dict:
    """Query the episode workflow's reopen_snapshot (running or closed run) and persist it."""
    from datetime import datetime as _dt, timezone as _tz
    from hfvg.saved_state import write_snapshot
    handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
    snap = await handle.query("reopen_snapshot")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as _EW
    problem = _EW.reopen_problem(episode_id, {"snapshot": snap, "reason": "save"})
    if problem:  # only an approved, parked state is a reopen point; never overwrite one with less
        raise ValueError(f"not a reopenable state ({problem})")
    path = write_snapshot(_episodes_root() / episode_id, snap,
                          _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S"))
    return {"episode_id": episode_id, "path": str(path)}


async def _snapshot_saver_loop() -> None:
    import aiosqlite
    while True:
        await asyncio.sleep(SNAPSHOT_SAVE_INTERVAL_S)
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                eps = [r[0] for r in await (await db.execute("SELECT episode_id FROM episodes")).fetchall()]
            for ep in eps:
                if (_episodes_root() / ep).is_dir():
                    try:
                        await _save_episode_snapshot(ep)
                    except Exception as e:  # no workflow / old run without the query: nothing to save
                        _gap79_log.debug(f"snapshot save skipped for {ep}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _gap79_log.warning(f"snapshot saver: {e}")


@app.post("/api/episodes/{episode_id}/snapshot")
async def save_episode_snapshot(episode_id: str, authorization: str | None = Header(None),
                                studio_admin_token: str | None = Cookie(None)):
    """GAP-79: persist the episode workflow's reopen snapshot now (read-only on the workflow)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        return {"success": True, **await _save_episode_snapshot(episode_id)}
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"snapshot not saved: {e}")


class ReopenFromSavedRequest(ReopenRequest):
    supersede_run_id: str | None = Field(None, description=(
        "Replace THIS run (only one that was itself started by reopen-from-saved and recorded no shot "
        "change since): it is terminated and the episode is rebuilt from the saved state again"))


@app.post("/api/episodes/{episode_id}/reopen-from-saved")
async def reopen_episode_from_saved(
    episode_id: str,
    request: ReopenFromSavedRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-79: reopen an episode at G_FINAL_CUT when its Temporal history is gone. Refused (409) while
    ANY workflow with the episode id is known to the server (use /reopen for a closed run). The
    snapshot is the persisted one, else rebuilt from the saved artifacts + DB; either must pass the
    GAP-41 reopen rules. No generation, no ledger change; the new run parks at G_FINAL_CUT."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="a reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.service import RPCError, RPCStatusCode
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as _EW
    from hfvg.saved_state import SavedStateError, saved_snapshot
    from hfvg.activities.workflow_support import parse_beatmap_activity
    workflow_id = episode_workflow_id(episode_id)
    import aiosqlite
    from hfvg.saved_state import SHOT_CHANGING_ACTIONS
    handle = temporal_client.get_workflow_handle(workflow_id)
    superseded = None
    try:
        desc = await handle.describe()
    except RPCError as e:
        if e.status != RPCStatusCode.NOT_FOUND:
            raise
        desc = None
    if desc is not None:
        status = desc.status.name if desc.status else None
        if not request.supersede_run_id or request.supersede_run_id != desc.run_id:
            raise HTTPException(status_code=409, detail=(
                f"a workflow for {episode_id} exists (status {status}, run {desc.run_id}); reopen-from-saved "
                f"is only for a purged history (use /reopen), or supersede_run_id={desc.run_id} for a run it "
                f"started itself that recorded no change"))
        async with aiosqlite.connect(DATABASE_PATH) as db:
            rows_ = await (await db.execute(
                "SELECT action, details FROM audit_log WHERE episode_id=? ORDER BY id", (episode_id,))).fetchall()
        acts = [r[0] for r in rows_]
        last = max((i for i, a in enumerate(acts) if a == "reopen_from_saved"), default=None)
        # the run to replace must be the one the LAST reopen-from-saved started (its audit row names it)
        if last is None or f" as {desc.run_id}" not in rows_[last][1]:
            raise HTTPException(status_code=409, detail=f"run {desc.run_id} was not started by reopen-from-saved")
        if any(a in SHOT_CHANGING_ACTIONS + ("approve_g_final_cut",) for a in acts[last + 1:]):
            raise HTTPException(status_code=409, detail=f"run {desc.run_id} recorded changes; it cannot be superseded")
        if status == "RUNNING":
            try:
                st = await handle.query("get_state") or {}
            except Exception as e:
                raise HTTPException(status_code=409, detail=f"cannot read run {desc.run_id}: {e}")
            if (st.get("recompose") or {}).get("pending") or st.get("pending_clip_retries"):
                raise HTTPException(status_code=409, detail=f"run {desc.run_id} has work in flight")
        superseded = desc.run_id
        if status == "RUNNING":
            await handle.terminate(reason=f"superseded by reopen-from-saved: {request.reason.strip()}")
    ep_dir = _episodes_root() / episode_id
    try:
        parsed = None
        async with aiosqlite.connect(DATABASE_PATH) as db:
            row = await (await db.execute("SELECT beatmap_path, live_mode FROM episodes WHERE episode_id=?",
                                          (episode_id,))).fetchone()
        if row and row[0]:
            parsed = await parse_beatmap_activity(row[0], episode_id, bool(row[1]))
        snapshot, source = saved_snapshot(episode_id, ep_dir, DATABASE_PATH, lambda _p: parsed,
                                          ignore_persisted=superseded is not None)
    except SavedStateError as e:
        raise HTTPException(status_code=409, detail=f"Cannot reopen {episode_id} from saved state: {e}")
    reopen_from = {"snapshot": snapshot, "reason": request.reason.strip(), "from_run_id": None,
                   "source": f"saved_state:{source}"}
    problem = _EW.reopen_problem(episode_id, reopen_from)
    if problem:
        raise HTTPException(status_code=409, detail=f"Cannot reopen {episode_id}: {problem}")
    attrs = snapshot["attrs"]
    new = await temporal_client.start_workflow(
        _EW.run,
        args=[episode_id, attrs.get("_beatmap_path"), bool(attrs.get("_dry_run", True)), None, None, None,
              reopen_from],
        id=workflow_id,
        task_queue="hfvg-tasks",
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "reopen_from_saved",
             f"reopened from saved state ({source}) as {new.result_run_id}"
             + (f" superseding {superseded}" if superseded else "") + f": {request.reason.strip()}", "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "workflow_id": workflow_id, "source": source,
            "run_id": new.result_run_id,
            "message": f"{episode_id} reopened from saved state ({source}) at G_FINAL_CUT (final cut un-approved)"}


# Mark reopen_episode_from_saved to allow members
reopen_episode_from_saved.allow_members = True

@app.get("/api/episodes/{episode_id}/recompose-headcount")
async def get_recompose_headcount(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-32: the GAP-27 headcount check on each shot's latest RECOMPOSE still (not a G2 still version,
    so /g2-headcount never saw it) against the cast that recompose declared. Read-only, no spend. Record
    a reviewer count first with POST /shots/{shot}/headcount {version: <recompose version>}."""
    from hfvg.qc.g2_headcount import (
        FAIL as _FAIL, NEEDS_REVIEW as _NR, ReviewerHeadcountCounter, _still_for, check_still_headcount,
        load_headcounts,
    )
    from hfvg.qc.animal_count import get_animal_counter
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        plans = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query(
            "recompose_still_plans") or {}
    except Exception as e:
        raise HTTPException(status_code=409, detail=f"recompose headcount unavailable (fails closed): {e}")
    reviewer = ReviewerHeadcountCounter(
        load_headcounts(episode_id, os.getenv("EPISODES_DIR", "./data/episodes")), get_animal_counter())
    out_root = Path(os.getenv("DATA_DIR", "./data")) / "episodes" / episode_id / "outputs"
    shots = {}
    for sid in sorted(plans):
        row = plans[sid] or {}
        v = row.get("version")
        path = _still_for(out_root / sid, v)
        res = await check_still_headcount(reviewer, path, row.get("shot_meta"))
        res.update({"version": v, "still": str(path) if path else None, "decision": row.get("decision"),
                    "still_ready": row.get("still_ready")})
        shots[sid] = res
    statuses = {r["status"] for r in shots.values()}
    status = _FAIL if _FAIL in statuses else (_NR if _NR in statuses or not shots else "PASS")
    return {"status": status, "failing": sorted(s for s, r in shots.items() if r["status"] == _FAIL),
            "shots": shots}


class RecomposeDecisionRequest(BaseModel):
    approve: bool
    reason: str = Field(..., min_length=1, max_length=1000)


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/recompose/decision")
async def recompose_decision(
    episode_id: str,
    shot_id: str,
    request: RecomposeDecisionRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-21: approve (clip from the new still) or reject (no clip spend) a parked recompose still."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if request.approve:  # GAP-65: approving starts the paid clip; a reject spends nothing
        await _refuse_if_paid_gates_fail(episode_id, "Recompose approval")
    result = await _execute_episode_update(
        episode_id, "recompose_decision", [shot_id, request.approve, request.reason.strip()],
        "Recompose decision")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "recompose_decision",
             f"Shot {shot_id} recompose {'approved' if request.approve else 'rejected'}: {request.reason.strip()}",
             "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result}


class RecomposeGrantRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    # GAP-43: explicit Studio override of the per-shot grant cap (audited); empty = the normal cap
    cap_override_note: str = Field("", max_length=1000)


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/recompose/grant")
async def grant_recompose(
    episode_id: str,
    shot_id: str,
    request: RecomposeGrantRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-39: reviewer grant of ONE extra recompose for a shot whose allowance is used up (no spend
    here; the recompose itself still goes through every paid guard)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    note = request.cap_override_note.strip()
    args = [shot_id, request.reason.strip()] + ([note] if note else [])
    result = await _execute_episode_update(episode_id, "grant_recompose", args, "Recompose grant")
    import aiosqlite
    details = f"Shot {shot_id} recompose grant: {request.reason.strip()}"
    if note:
        details += f" | GRANT CAP OVERRIDE: {note}"
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "grant_recompose_override" if note else "grant_recompose", details, "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result}


class AdoptReuseRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000, description="Who asked and why (audited)")
    template_shot_id: str = Field(..., min_length=1, max_length=32,
                                  description="A launched shot of the same kind whose plan is the base")
    shot: dict = Field(..., description="The slot's package shots.json entry (id must match the slot)")


def _adopt_reuse_fields(episode_id: str, shot_id: str, entry: dict) -> dict:
    """GAP-44: the adopted plan fields for an edit_plan reuse slot from its package shots.json entry.
    The slot must be a reuse slot of the episode's edit plan, the entry id must match, and the entry's
    trim must keep the reuse slot's screen length (the edit timing does not move). Pure (reads files)."""
    from hfvg.edit_plan import load_edit_plan
    from hfvg.shot_data import parse_shots
    plan = load_edit_plan(episode_id) or {}
    reuse = (plan.get("reuse") or {}).get(shot_id)
    if not reuse:
        raise HTTPException(status_code=409, detail=f"{shot_id} is not a reuse slot of the {episode_id} edit plan")
    if entry.get("id") != shot_id:
        raise HTTPException(status_code=422, detail=f"shot.id {entry.get('id')!r} does not match {shot_id}")
    clean = {k: v for k, v in entry.items() if k not in ("reuse", "pre_approved_still")}
    try:
        parsed = parse_shots({"shots": [clean]})[shot_id]
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"invalid shot entry: {e}")
    trim = parsed.get("trim") or {}
    slot_len = float(reuse["trim_out"]) - float(reuse.get("trim_in", 0.0))
    if abs((float(trim.get("out", 0.0)) - float(trim.get("in", 0.0))) - slot_len) > 1e-6:
        raise HTTPException(status_code=409, detail=f"{shot_id} trim length must equal the reuse slot's {slot_len}s")
    refs_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "refs"
    fields = {"prompt": parsed["prompt"], "shot_meta": parsed["shot_meta"], "params": parsed["params"],
              "trim": trim, "tags": parsed.get("tags") or [], "ref_keys": parsed.get("ref_keys") or [],
              "refs": [str((refs_dir / f"{k}.png").resolve()) for k in (parsed.get("ref_keys") or [])
                       if (refs_dir / f"{k}.png").is_file()]}
    for k in ("clip_prompt", "qc_fail_if"):
        if parsed.get(k):
            fields[k] = parsed[k]
    return fields


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/adopt-reuse")
async def adopt_reuse(
    episode_id: str,
    shot_id: str,
    request: AdoptReuseRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-44: adopt an edit_plan reuse slot as a workflow shot (no spend) so it can be recomposed."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    fields = _adopt_reuse_fields(episode_id, shot_id, request.shot)
    result = await _execute_episode_update(
        episode_id, "adopt_reuse_slot", [shot_id, request.reason.strip(), request.template_shot_id, fields],
        "Adopt reuse")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "adopt_reuse_slot",
             f"Shot {shot_id} adopted (template {request.template_shot_id}): {request.reason.strip()}", "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result}


class RefJobsRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    keys: list[str] = Field(..., min_length=1, max_length=8)


@app.post("/api/episodes/{episode_id}/refs/generate")
async def generate_refs_route(
    episode_id: str,
    request: RefJobsRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-42: run versioned ref-edit jobs (refs.json to_generate entries with edit_from and an explicit
    version) while the episode is parked at G_FINAL_CUT. Every paid guard applies (provider estimate,
    L1_refs reserve, balance, episode stop); one attempt per job. Audited."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    from hfvg.activities.ref_generation import ref_edit_specs_for
    from hfvg.shot_data import refs_json_path
    try:
        ref_edit_specs_for(json.loads(refs_json_path(episode_id).read_text(encoding="utf-8")), list(request.keys), True)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"refs.json refused before any spend: {e}")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "generate_refs", f"Ref jobs {request.keys}: {request.reason.strip()}", "admin"))
        await db.commit()
    result = await _execute_episode_update(
        episode_id, "generate_refs", [list(request.keys), request.reason.strip()], "Ref jobs")
    return {"success": True, "episode_id": episode_id, "result": result}


class ShotEditRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)
    trim_in: float | None = Field(None, ge=0)
    trim_out: float | None = Field(None, gt=0)
    drop: bool = False


class RestoreOutcomeRequest(BaseModel):
    reason: str = Field(..., min_length=1, description="Why the recompose is backed out (audited)")


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/restore-outcome")
async def restore_pre_recompose_outcome(
    episode_id: str,
    shot_id: str,
    request: RestoreOutcomeRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-49: back out a recompose whose clip failed review: the shot returns to the accepted clip the
    recompose replaced and the edit is rebuilt with it (no spend). Refusals -> 409."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="reason is required")
    result = await _execute_episode_update(
        episode_id, "restore_pre_recompose_outcome", [shot_id, request.reason.strip()], "Outcome restore")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "restore_outcome", f"Shot {shot_id} restored to pre-recompose clip: {request.reason.strip()}",
             "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result}


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/edit")
async def set_shot_edit(
    episode_id: str,
    shot_id: str,
    request: ShotEditRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """GAP-22: trim or drop a shot at G_FINAL_CUT (no spend). The edit is re-assembled and the
    delivery rebuilt; with a timeline_ref in the audio plan the VO/SFX/music follow the edit."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    result = await _execute_episode_update(
        episode_id, "set_shot_edit",
        [shot_id, request.reason.strip(), request.trim_in, request.trim_out, request.drop], "Shot edit")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, ?, ?, ?)",
            (episode_id, "set_shot_edit", f"Shot {shot_id} edit {result.get('override')}: {request.reason.strip()}",
             "admin"))
        await db.commit()
    return {"success": True, "episode_id": episode_id, "shot_id": shot_id, "result": result}


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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


# Mark get_gates to allow members
get_gates.allow_members = True

@app.get("/api/episodes/{episode_id}/audit")
async def get_audit_trail(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Get audit trail for episode. Member or admin.
    
    Requires: Auth
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


# Mark get_audit_trail to allow members
get_audit_trail.allow_members = True

@app.post("/api/episodes/{episode_id}/set-live")
async def set_live_mode_endpoint(
    episode_id: str,
    request: SetLiveModeRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Switch episode to live (paid) mode with confirmation. Member or admin.
    
    Requires: Auth + confirmation string "ENABLE_LIVE_MODE"
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


# Mark set_live_mode_endpoint to allow members
set_live_mode_endpoint.allow_members = True

class RetryShotRequest(BaseModel):
    prompt_append: str | None = Field(None, max_length=1000,
                                      description="Text appended to this retry's prompt only")
    reason: str | None = Field(None, max_length=500, description="Who asked for the retry and why")


@app.post("/api/episodes/{episode_id}/shots/{shot_id}/retry")
async def retry_shot(
    episode_id: str,
    shot_id: str,
    request: RetryShotRequest | None = None,
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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
            if request is not None and request.prompt_append is not None:
                # GAP-15: preflight the AMENDED prompts (still + the clip it will become) against
                # the model limits before the paid re-roll; the clip preflight would only refuse
                # it after G2, when the re-rolled still is already paid for.
                plan = await handle.query("retry_prompt_plan", args=[shot_id, request.prompt_append])
                if plan.get("found"):
                    from hfvg import preflight as _pf
                    bad = _pf.prompt_limit_violations(
                        shot_id, still_prompt=plan["still_prompt"], clip_prompt=plan["clip_prompt"],
                        clip_model_alias=plan.get("clip_model"))
                    if bad:
                        raise HTTPException(status_code=409, detail="Preflight refused the amended re-roll "
                                            "before any spend: " + "; ".join(v.message for v in bad))
                    # The amended still prompt must also pass the episode prompt lint (the child's
                    # pre-submit gate). Refusing here keeps a doomed re-roll from consuming an attempt.
                    # No kit loadable here: left to the child's fail-closed gate.
                    from hfvg.prompt_lint import PromptLintError, lint_violations, load_lint_kit
                    try:
                        _kit = load_lint_kit(episode_id)
                    except PromptLintError:
                        _kit = None
                    if _kit is not None and plan.get("shot_meta") is not None:
                        lint_bad = lint_violations(plan["still_prompt"], plan["shot_meta"], _kit)
                        if lint_bad:
                            raise HTTPException(status_code=409, detail="Prompt lint refused the amended "
                                                "re-roll before any spend: " + "; ".join(lint_bad))
                result = await handle.execute_update(
                    "retry_shot", args=[shot_id, request.prompt_append])
            else:
                result = await handle.execute_update(
                    "retry_shot",
                    shot_id
                )
        except HTTPException:
            raise
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
                (episode_id, "retry_shot",
                 f"Shot {shot_id} retry requested"
                 + (f"; prompt_append={request.prompt_append!r}" if request and request.prompt_append else "")
                 + (f"; reason={request.reason!r}" if request and request.reason else ""), "admin")
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


# Mark retry_shot to allow members
retry_shot.allow_members = True

@app.post("/api/episodes/{episode_id}/set-dry")
async def set_dry_mode_endpoint(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Switch episode to dry-run mode (disable paid generation). Member or admin.
    
    Requires: Auth
    """
    # Member or admin access
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    
    # Set live mode to false in database
    await db_set_live_mode(DATABASE_PATH, episode_id, False, user="admin")
    
    return {
        "success": True,
        "episode_id": episode_id,
        "live_mode": False,
        "message": "Episode switched to DRY-RUN mode. Paid generation disabled.",
    }


# Mark set_dry_mode_endpoint to allow members
set_dry_mode_endpoint.allow_members = True

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
            plans = parse_shots(json.loads(shots_json.read_text(encoding="utf-8")))
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
            refs_data = json.loads(refs_json.read_text(encoding="utf-8"))
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
    refs_data = json.loads(refs_json_path.read_text(encoding="utf-8"))
    
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
    refs_json_path.write_text(json.dumps(refs_data, indent=2), encoding="utf-8")
    
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
            version_sidecar.write_text(json.dumps({"version": version, "ref_key": ref_key}), encoding="utf-8")
            updated_sidecars.append(str(version_sidecar))
    
    return {
        "success": True,
        "episode_id": episode_id,
        "versions_updated": versions,
        "sidecars_written": len(updated_sidecars),
        "refs_json_path": str(refs_json_path),
    }


@app.post("/api/lint")
async def lint_prompt_endpoint(
    request: dict,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Lint a prompt for violations.
    
    Requires: Authorization header with admin secret OR admin session cookie.
    
    Args:
        request: dict with "prompt" (str), optional "shot_meta" (dict), optional "live" (bool), optional "episode_id" (str)
    
    Returns:
        dict with "problems" list
    """
    await verify_auth_unified(authorization, studio_admin_token, require_admin=True)
    
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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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


class DraftFromIdeaRequest(BaseModel):
    """Request to generate episode files from a plain-language idea."""
    
    idea: str = Field(..., min_length=10, max_length=2000, description="User's story idea in plain language")
    archetype_id: str | None = Field(None, description="Archetype/genre preset ID (optional, defaults to 'original-mini-movie')")
    target_duration: int | None = Field(None, ge=10, le=600, description="Optional duration override (uses preset range if not specified)")
    style_preset: str | None = Field(None, max_length=500, description="Optional style description")


class DraftFromIdeaResponse(BaseModel):
    """Response from draft generation."""
    
    episode_id: str
    beatmap: str
    shots_json: dict
    refs_json: dict
    prompt_kit_json: dict
    audio_plan_json: dict
    story_shape: dict | None = None
    cost_estimate: dict | None = None
    warnings: list[str] = Field(default_factory=list)
    preset_used: dict | None = None


@app.post("/api/episodes/draft-from-idea", response_model=DraftFromIdeaResponse)
async def draft_from_idea(
    request: DraftFromIdeaRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """
    Generate all episode files (BEATMAP.md, shots.json, refs.json, prompt_kit.json) from a plain-language idea.
    
    Uses OpenAI structured output to generate files that pass existing validators.
    Content policy: original characters only, no named IP or real people.
    
    Requires: Authorization header OR session cookie (member or admin).
    """
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    
    from hfvg.config import config
    import tempfile
    from hfvg.episode_parser import parse_beatmap
    from hfvg.shot_data import parse_shots
    from hfvg.genre_presets import get_preset
    
    # Check for test-only fake LLM mode (DRAFT_LLM_FAKE=1)
    use_fake_llm = os.getenv("DRAFT_LLM_FAKE", "0") == "1"
    dry_run_mode = os.getenv("DRY_RUN", "true").lower() == "true"
    
    if use_fake_llm:
        if not dry_run_mode:
            logger.error("DRAFT_LLM_FAKE=1 is set but DRY_RUN=false - REFUSING to use fake LLM in live mode!")
            raise HTTPException(
                status_code=500,
                detail="DRAFT_LLM_FAKE is a test-only flag and cannot be used when DRY_RUN=false"
            )
        logger.warning("DRAFT_LLM_FAKE=1 is set - using deterministic canned draft data (test mode only)")
    
    # Check if OpenAI API key is configured (skip check if using fake LLM)
    if not use_fake_llm and not config.OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Story writing assistant not configured. Set OPENAI_API_KEY environment variable."
        )
    
    # Generate a unique episode ID matching pattern: ep[0-9]{2}(?:[a-z][a-z0-9]{0,11})?
    # Format: ep{2 digits}{3 lowercase letters} (e.g., ep42abc)
    import random
    import string
    
    episodes_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes"))
    
    # Initialize studio DB if not exists to check existing episodes
    await init_studio_db(DATABASE_PATH)
    
    # Generate unique ID: ep + 2 digits + 3 lowercase letters (matches existing pattern)
    for attempt in range(100):
        num = random.randint(10, 99)
        suffix = ''.join(random.choices(string.ascii_lowercase, k=3))
        episode_id = f"ep{num}{suffix}"
        
        # Check filesystem
        if (episodes_dir / episode_id).exists():
            continue
        
        # Check database
        import sqlite3
        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                cursor = conn.execute("SELECT episode_id FROM episodes WHERE episode_id = ?", (episode_id,))
                if cursor.fetchone() is not None:
                    continue
        except sqlite3.Error:
            pass  # DB might not exist yet, filesystem check is sufficient
        
        # Found unique ID
        break
    else:
        raise HTTPException(status_code=500, detail="Failed to generate unique episode ID after 100 attempts")
    
    # Load genre preset (default to original-mini-movie if not specified)
    archetype_id = request.archetype_id or "original-mini-movie"
    preset = get_preset(archetype_id)
    if preset is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown archetype_id: {archetype_id}. Use one of the 24 preset IDs."
        )
    
    # Determine target duration (user override or preset default midpoint)
    if request.target_duration:
        target_duration = max(preset.length_s[0], min(request.target_duration, preset.length_s[1]))
    else:
        target_duration = (preset.length_s[0] + preset.length_s[1]) // 2
    
    # Build prompt guidance from preset
    archetype_guidance = f"{preset.name}: {preset.description}"
    if preset.shot_grammar:
        archetype_guidance += f"\nShot grammar: {', '.join(preset.shot_grammar[:3])}"
    
    style_guidance = request.style_preset or "photorealistic, cinematic lighting"
    
    content_policy_warning = """
CONTENT POLICY (CRITICAL):
- Create ONLY original characters - no named IP, franchises, real people, or brand characters
- If the idea mentions copyrighted names (e.g., "Spider-Man", "Mario", "Mickey Mouse"), 
  create an original homage archetype instead (e.g., "A red-and-blue spider-themed hero")
- Use generic descriptive names (e.g., "Hero", "Villain", "Robot", "Dragon")
- All characters, settings, and stories must be original creations
"""
    
    # Calculate shot count from preset pacing
    estimated_shots = max(2, int(target_duration / 10 * preset.pacing.cuts_per_10s))
    avg_shot_duration = target_duration / estimated_shots if estimated_shots > 0 else 5.0
    
    system_prompt = f"""You are a video production assistant that generates complete episode specifications.
Given a user's story idea, create all required files for a {target_duration}s video in the "{preset.name}" style.

PRESET: {preset.name}
Template: {preset.template.value}
Pacing: ~{preset.pacing.cuts_per_10s} cuts per 10s (~{estimated_shots} shots total)
VO mode: {preset.vo_mode.value}
{f"Shot grammar: {', '.join(preset.shot_grammar[:3])}" if preset.shot_grammar else ""}

{content_policy_warning}

Generate these files in your response as a JSON object with these keys:
1. "beatmap_md": BEATMAP.md content (markdown table with shots)
2. "shots_json": shots.json content (structured shot data)
3. "refs_json": refs.json content (per-shot reference images)
4. "prompt_kit_json": prompt_kit.json content (style, characters, sets)
5. "audio_plan_json": audio_plan.json content (VO and music configuration)
6. "ip_steering_note": If the idea mentioned copyrighted content, explain the original alternatives you created

File format requirements:

BEATMAP.md - Shot planning table:
```
# Episode {episode_id} Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 5.0 (5.0) | [location] | [time] | A | [action description] | [narrative purpose] | - | - | K |
```
- Shot IDs: S01, S02, etc.
- Screen/gen: both same value, {avg_shot_duration:.1f} seconds per shot on average
- Model: K (Kling) for most shots
- Chars: A, B, C for up to 3 main characters
- Create approximately {estimated_shots} shots total

shots.json:
{{
  "shots": [
    {{
      "id": "S01",
      "prompt": "[detailed visual description for generation]",
      "count": 1,
      "characters": ["A"],
      "wet": false,
      "model": "kling",
      "duration": 5,
      "end_frame": false,
      "ref_keys": []
    }}
  ]
}}

refs.json:
{{
  "per_shot": {{
    "S01": [],
    "S02": []
  }}
}}

prompt_kit.json:
{{
  "style": "{style_guidance}",
  "aspect_ratio": "{preset.aspect}",
  "characters": {{
    "A": {{"description": "[main character description]"}},
    "B": {{"description": "[second character if needed]"}}
  }},
  "sets": {{
    "[LocationName]": "[location description]"
  }}
}}

audio_plan.json (real schema for audio_mix.normalize_plan):
"""
    
    # Add audio plan format based on VO mode - REAL SCHEMA
    if preset.vo_mode.value == "none":
        system_prompt += """
{
  "vo": {
    "voice_id": null,
    "lines": []
  },
  "music": {
    "prompt": "MUSIC_DESCRIPTION_HERE",
    "level_db": -30.0,
    "loop": true
  }
}
"""
    elif preset.vo_mode.value == "narration":
        system_prompt += """
{
  "vo": {
    "voice_id": null,
    "lines": [
      {"id": "L01", "at": 0.0, "text": "Narration text here", "subtitle": "Narration text here"}
    ]
  },
  "music": {
    "prompt": "MUSIC_DESCRIPTION_HERE",
    "level_db": -32.0,
    "loop": true
  }
}
"""
    elif preset.vo_mode.value == "dialogue":
        system_prompt += """
{
  "vo": {
    "voice_id": null,
    "lines": [
      {"id": "L01", "at": 0.0, "text": "Character A dialogue", "subtitle": "Character A dialogue"},
      {"id": "L02", "at": 5.0, "text": "Character B dialogue", "subtitle": "Character B dialogue"}
    ]
  },
  "music": {
    "prompt": "MUSIC_DESCRIPTION_HERE",
    "level_db": -32.0,
    "loop": true
  }
}
"""
    elif preset.vo_mode.value == "caption_only":
        system_prompt += """
{
  "vo": {
    "voice_id": null,
    "lines": []
  },
  "music": {
    "prompt": "MUSIC_DESCRIPTION_HERE",
    "level_db": -30.0,
    "loop": true
  }
}
"""
    
    system_prompt += f"""

IMPORTANT for audio_plan.json:
- vo.voice_id can be null (system will use default)
- vo.lines: each line needs {{id, at (seconds), text, subtitle}}
- music needs EITHER "prompt" (text description) OR "path" (library file)
- Use "prompt" with style: {preset.music}
- level_db typically -30 to -32 for music under VO
- Do NOT use placeholder IDs - use null for voice_id

Return ONLY valid JSON with these exact keys: beatmap_md, shots_json, refs_json, prompt_kit_json, audio_plan_json."""

    user_prompt = f"""Create a {target_duration}s video for this idea:

{request.idea}

Style/genre: {archetype_guidance}

Brief fields to consider: {', '.join(preset.brief_fields)}

Generate all 5 files (beatmap_md, shots_json, refs_json, prompt_kit_json, audio_plan_json) as a single JSON response."""

    # Fake LLM mode: return deterministic canned data (test-only)
    if use_fake_llm:
        logger.info(f"FAKE LLM: Returning canned draft data for episode {episode_id}")
        
        beatmap_content = f"""# Episode {episode_id} Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 5.0 (5.0) | Workshop | Day | A | A curious robot examines dusty books on old wooden shelves | Establish setting and character | - | - | K |
| 2 | S02 | 6.0 (6.0) | Workshop | Day | A | The robot discovers a glowing book, light emanating from its pages | Discovery moment, inciting incident | - | - | K |
"""
        
        shots_data = {
            "shots": [
                {
                    "id": "S01",
                    "prompt": "A friendly metallic robot with glowing blue eyes standing in a dusty old workshop, examining ancient books on wooden shelves, warm daylight streaming through windows, photorealistic, cinematic lighting",
                    "count": 1,
                    "characters": ["A"],
                    "wet": False,
                    "model": "kling",
                    "duration": 5,
                    "end_frame": False,
                    "ref_keys": []
                },
                {
                    "id": "S02",
                    "prompt": "Close-up of robot's metallic hands opening a leather-bound book that glows with magical golden light, mystical particles floating in the air, photorealistic, cinematic lighting",
                    "count": 1,
                    "characters": ["A"],
                    "wet": False,
                    "model": "kling",
                    "duration": 6,
                    "end_frame": False,
                    "ref_keys": []
                }
            ]
        }
        
        refs_data = {
            "per_shot": {
                "S01": [],
                "S02": []
            }
        }
        
        prompt_kit_data = {
            "style": style_guidance,
            "aspect_ratio": preset.aspect,
            "characters": {
                "A": {"description": "Friendly exploration robot with metallic silver body and glowing blue optical sensors"}
            },
            "sets": {
                "Workshop": "Abandoned dusty workshop with tall wooden bookshelves, old tools, and warm natural light"
            }
        }
        
        # Generate audio plan based on preset vo_mode - REAL SCHEMA
        if preset.vo_mode.value == "none":
            audio_plan_data = {
                "vo": {
                    "voice_id": None,
                    "lines": []
                },
                "music": {
                    "prompt": f"{preset.music or 'ambient'} instrumental background music",
                    "level_db": -30.0,
                    "loop": True
                }
            }
        elif preset.vo_mode.value == "narration":
            audio_plan_data = {
                "vo": {
                    "voice_id": None,
                    "lines": [
                        {"id": "L01", "at": 0.0, "text": "In a forgotten workshop, a curious robot awakens.", 
                         "subtitle": "In a forgotten workshop, a curious robot awakens."}
                    ]
                },
                "music": {
                    "prompt": f"{preset.music or 'gentle'} instrumental background music",
                    "level_db": -32.0,
                    "loop": True
                }
            }
        elif preset.vo_mode.value == "dialogue":
            audio_plan_data = {
                "vo": {
                    "voice_id": None,
                    "lines": [
                        {"id": "L01", "at": 0.0, "text": "What is this place?", "subtitle": "What is this place?"},
                        {"id": "L02", "at": 3.0, "text": "It's an old workshop.", "subtitle": "It's an old workshop."}
                    ]
                },
                "music": {
                    "prompt": f"{preset.music or 'mysterious'} instrumental background music",
                    "level_db": -32.0,
                    "loop": True
                }
            }
        else:  # caption_only
            audio_plan_data = {
                "vo": {
                    "voice_id": None,
                    "lines": []
                },
                "music": {
                    "prompt": f"{preset.music or 'upbeat'} instrumental background music",
                    "level_db": -30.0,
                    "loop": True
                }
            }
        
        cost_estimate = offline_cost_estimate(shots_data["shots"])
        
        preset_summary = {
            "id": preset.id,
            "name": preset.name,
            "template": preset.template.value,
            "aspect": preset.aspect,
            "vo_mode": preset.vo_mode.value
        }
        
        return DraftFromIdeaResponse(
            episode_id=episode_id,
            beatmap=beatmap_content,
            shots_json=shots_data,
            refs_json=refs_data,
            prompt_kit_json=prompt_kit_data,
            audio_plan_json=audio_plan_data,
            cost_estimate=cost_estimate,
            warnings=["Test mode: Using canned draft data (DRAFT_LLM_FAKE=1)"],
            preset_used=preset_summary,
        )

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        
        # Call OpenAI with structured output
        response = await client.chat.completions.create(
            model=config.STORY_SHAPING_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        
        result = json.loads(response.choices[0].message.content)
        
        # Extract generated content
        beatmap_content = result.get("beatmap_md", "")
        shots_data = result.get("shots_json")
        refs_data = result.get("refs_json")
        prompt_kit_data = result.get("prompt_kit_json")
        audio_plan_data = result.get("audio_plan_json")
        ip_steering = result.get("ip_steering_note", "")
        
        if not all([beatmap_content, shots_data, refs_data, prompt_kit_data, audio_plan_data]):
            raise ValueError("LLM response missing required files")
        
        # Parse JSON strings if needed
        if isinstance(shots_data, str):
            shots_data = json.loads(shots_data)
        if isinstance(refs_data, str):
            refs_data = json.loads(refs_data)
        if isinstance(prompt_kit_data, str):
            prompt_kit_data = json.loads(prompt_kit_data)
        if isinstance(audio_plan_data, str):
            audio_plan_data = json.loads(audio_plan_data)
        
        # Validate beatmap
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write(beatmap_content)
            beatmap_temp = f.name
        
        try:
            parsed_shots = parse_beatmap(beatmap_temp)
            if not parsed_shots:
                raise ValueError("Beatmap parsed but contains no shots")
        except Exception as e:
            # Retry once with a repair prompt
            repair_prompt = f"""The generated beatmap failed validation: {str(e)}

Original beatmap:
{beatmap_content}

Fix the beatmap to be valid. Return JSON with just "beatmap_md" key containing the corrected markdown."""
            
            repair_response = await client.chat.completions.create(
                model=config.STORY_SHAPING_MODEL,
                messages=[
                    {"role": "system", "content": "You fix invalid beatmap tables."},
                    {"role": "user", "content": repair_prompt},
                ],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            
            repair_result = json.loads(repair_response.choices[0].message.content)
            beatmap_content = repair_result.get("beatmap_md", beatmap_content)
            
            # Re-write and validate
            with open(beatmap_temp, 'w', encoding='utf-8') as f:
                f.write(beatmap_content)
            
            parsed_shots = parse_beatmap(beatmap_temp)
            if not parsed_shots:
                raise ValueError("Repaired beatmap still invalid")
        finally:
            Path(beatmap_temp).unlink(missing_ok=True)
        
        # Validate shots.json
        try:
            parse_shots(shots_data)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Generated shots.json failed validation: {str(e)}"
            )
        
        # Validate refs.json structure
        if not isinstance(refs_data, dict) or "per_shot" not in refs_data:
            raise HTTPException(
                status_code=500,
                detail="Generated refs.json missing 'per_shot' key"
            )
        
        # Validate prompt_kit.json
        required_kit_keys = {"style", "aspect_ratio", "characters", "sets"}
        if not all(k in prompt_kit_data for k in required_kit_keys):
            raise HTTPException(
                status_code=500,
                detail=f"Generated prompt_kit.json missing required keys: {required_kit_keys}"
            )
        
        # Validate audio_plan.json
        if not isinstance(audio_plan_data, dict) or "vo_mode" not in audio_plan_data:
            raise HTTPException(
                status_code=500,
                detail="Generated audio_plan.json missing 'vo_mode' key"
            )
        
        # Generate cost estimate
        cost_estimate = offline_cost_estimate(shots_data["shots"])
        
        warnings = []
        if ip_steering:
            warnings.append(f"Content policy note: {ip_steering}")
        
        preset_summary = {
            "id": preset.id,
            "name": preset.name,
            "template": preset.template.value,
            "aspect": preset.aspect,
            "vo_mode": preset.vo_mode.value
        }
        
        return DraftFromIdeaResponse(
            episode_id=episode_id,
            beatmap=beatmap_content,
            shots_json=shots_data,
            refs_json=refs_data,
            prompt_kit_json=prompt_kit_data,
            audio_plan_json=audio_plan_data,
            cost_estimate=cost_estimate,
            warnings=warnings,
            preset_used=preset_summary,
        )
        
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(
            status_code=500,
            detail=f"Draft generation failed: {str(e)}"
        )

draft_from_idea.allow_members = True


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
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
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
            raw = json.loads(shots_path.read_text(encoding="utf-8"))
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
        "cost_estimate": offline_cost_estimate(shots),
    }


# Mark preflight_episode to allow members
preflight_episode.allow_members = True

def offline_cost_estimate(shots: list[dict], pre_approved: set[str] | None = None) -> dict:
    """Offline USD estimate for a shot list (no provider call): the per-call estimates the dry
    submits reserve (hfvg.estimates): one start still per shot (none when the start still is
    pre-approved) + one end still for an end frame at the dry still rate, and each clip from the
    configured per-second rate table (list price before discount). Shots whose model has no rate
    are listed as unpriced instead of being guessed."""
    from hfvg.estimates import dry_shot_estimate
    from hfvg.pricing import micros_to_usd_str

    pre_approved = set(pre_approved or ())
    per_shot: dict[str, dict] = {}
    unpriced: list[str] = []
    total = 0
    for shot in shots:
        sid = str(shot.get("id") or shot.get("shot_id") or "?")
        is_pre = sid in pre_approved or bool(shot.get("pre_approved_still"))
        est = dry_shot_estimate(shot, pre_approved=is_pre)
        still_micros = est["still_usd_micros"]
        clip_micros = est["clip_usd_micros"]
        per_shot[sid] = {"stills_usd_micros": still_micros, "clip_usd_micros": clip_micros,
                         "pre_approved": is_pre}
        total += still_micros + (clip_micros or 0)
        if clip_micros is None:
            unpriced.append(f"{sid}: {est['clip_error']}")
    return {
        "unit": "usd_micros",
        "total_usd_micros": total,
        "total_usd": micros_to_usd_str(total),
        "per_shot": per_shot,
        "unpriced": unpriced,
        "source": "rate_table (list price before discount; first attempt only, no retries)",
    }


# ---------------------------------------------------------------- G-UI-1: review files, final cut, cap
# Whitelisted reviewable files: file_type -> (where the path comes from, fallback location). Paths come
# from the workflow's own deterministic layout (hfvg.audio_mix.delivery_paths / media.edit_dir) under
# EPISODES_DIR/{episode_id}/; nothing from the request is ever joined into a path.
REVIEW_FILE_TYPES: dict[str, tuple[str, str]] = {
    "master_subs": ("delivery", "master_subs"),
    "master_nosubs": ("delivery", "nosubs"),
    "phone": ("delivery", "phone"),
    "subtitles_srt": ("delivery", "srt"),
    "mute_edit": ("edit", "mute_assembly.mp4"),
}
_REVIEW_MEDIA_TYPES = {".mp4": "video/mp4", ".srt": "application/x-subrip"}


def review_file_path(episode_id: str, file_type: str) -> Path:
    """Resolved path of a whitelisted review file, contained in the episode directory.

    Raises HTTPException 400 (unknown type), 403 (escapes the episode dir, e.g. via a symlink) or
    404 (not rendered). Containment is Path.is_relative_to on fully resolved paths, never a string
    prefix test ("ep01" would otherwise also admit "ep011/...")."""
    from hfvg.audio_mix import delivery_paths, episode_dir as _episode_dir

    if file_type not in REVIEW_FILE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file_type: {file_type}. Allowed: {', '.join(REVIEW_FILE_TYPES)}",
        )
    root = _episode_dir(episode_id)
    kind, key = REVIEW_FILE_TYPES[file_type]
    if kind == "delivery":
        candidate = Path(delivery_paths(episode_id)[key])
    else:
        candidate = root / "edit" / key
    try:
        root_resolved = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        raise HTTPException(status_code=404, detail=f"{file_type} not rendered for {episode_id}")
    if not resolved.is_relative_to(root_resolved):
        raise HTTPException(status_code=403, detail="File is outside the episode directory")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"{file_type} not rendered for {episode_id}")
    return resolved


@app.get("/api/episodes/{episode_id}/files")
async def list_review_files(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Which whitelisted review files exist for the episode (for previews and downloads)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    episode_id = validate_episode_id(episode_workflow_id(episode_id))
    files = {}
    for file_type in REVIEW_FILE_TYPES:
        try:
            path = review_file_path(episode_id, file_type)
            files[file_type] = {"available": True, "size_bytes": path.stat().st_size,
                                "url": f"/api/episodes/{episode_id}/files/{file_type}"}
        except HTTPException as e:
            files[file_type] = {"available": False, "reason": e.detail}
    return {"episode_id": episode_id, "files": files}


# Mark list_review_files to allow members
list_review_files.allow_members = True

@app.get("/api/episodes/{episode_id}/files/{file_type}")
async def get_review_file(
    episode_id: str,
    file_type: str,
    download: bool = False,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Authenticated preview / download of a whitelisted episode file (delivery master with or without
    subs, phone version, subtitles, mute edit). Range requests are served (video seeking)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    episode_id = validate_episode_id(episode_workflow_id(episode_id))
    path = review_file_path(episode_id, file_type)
    if download:
        await _audit(episode_id, "download_file", f"Downloaded {file_type}")
    return FileResponse(
        path=path,
        media_type=_REVIEW_MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        filename=path.name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


_STILL_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


# Mark get_review_file to allow members
get_review_file.allow_members = True

@app.get("/api/episodes/{episode_id}/shots/{shot_id}/still")
async def get_local_still(
    episode_id: str,
    shot_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Authenticated preview of a shot's start still when it is a local file (a pre-approved still is
    copied into the episode outputs and never uploaded until its clip is submitted), so G2 can show
    it. Only files inside DATA_DIR/episodes/<episode>/outputs/<shot>/ or, for dry-run placeholder
    stills (stored relative to the run dir), inside the dry-run output dir are served."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", shot_id):
        raise HTTPException(status_code=400, detail="Invalid shot_id")
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        async with db.execute(
            "SELECT still_url FROM shots WHERE episode_id = ? AND shot_id = ?", (episode_id, shot_id)
        ) as cur:
            row = await cur.fetchone()
    still = (row[0] if row else None) or ""
    if not still or still.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="No local still for this shot")
    root = (Path(os.getenv("DATA_DIR", "./data")) / "episodes" / episode_id / "outputs" / shot_id).resolve()
    from hfvg.activities.studio_generation import DRY_RUN_OUTPUT_DIR
    dry_root = DRY_RUN_OUTPUT_DIR.resolve()
    path = Path(still).resolve()
    inside = path.is_relative_to(root) or path.is_relative_to(dry_root)
    if not inside or not path.is_file() or path.suffix.lower() not in _STILL_MEDIA_TYPES:
        raise HTTPException(status_code=404, detail="Still is not an episode output file")
    return FileResponse(path=path, media_type=_STILL_MEDIA_TYPES[path.suffix.lower()], content_disposition_type="inline")


# Mark get_local_still to allow members
get_local_still.allow_members = True

class FinalCutRejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


@app.post("/api/episodes/{episode_id}/final-cut/reject")
async def reject_final_cut(
    episode_id: str,
    request: FinalCutRejectRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Reject the final cut at G_FINAL_CUT with a reason (approval goes through POST .../approve with
    gate_id g_final_cut). The gate stays held; fix with a clip retry, then approve again."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not request.reason.strip():
        raise HTTPException(status_code=422, detail="A rejection reason is required")
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
    try:
        state = await handle.query("get_state")
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error querying workflow: {e}")
    if state.get("current_gate") != "G_FINAL_CUT":
        raise HTTPException(status_code=409, detail=f"Not at G_FINAL_CUT (current: {state.get('current_gate')})")
    await handle.signal("reject_g_final_cut", request.reason.strip())
    await _audit(episode_id, "reject_g_final_cut", request.reason.strip())
    return {"success": True, "episode_id": episode_id, "message": "Final cut rejected", "reason": request.reason.strip()}


# Mark reject_final_cut to allow members
reject_final_cut.allow_members = True

class RaiseCapRequest(BaseModel):
    new_cap: int = Field(..., gt=0, description="New credit cap for the run (credits)")


@app.post("/api/episodes/{episode_id}/raise-cap")
async def raise_credit_cap(
    episode_id: str,
    request: RaiseCapRequest,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Raise the run's credit cap and resume shots paused at the cap (workflow signal raise_credit_cap).
    Only upward: a cap at or below the current one is refused. Admin-only (financial control)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=True)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    from hfvg.credit_ledger import CreditLedger

    try:
        status = await CreditLedger(DATABASE_PATH).get_episode_status(episode_id)
        current = status["cap_credits"] or None  # 0 = no credit line yet
    except Exception:
        current = None
    if current is not None and request.new_cap <= current:
        raise HTTPException(status_code=409, detail=f"New cap {request.new_cap} must exceed the current cap {current}")
    handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
    try:
        await handle.signal("raise_credit_cap", request.new_cap)
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error raising cap: {e}")
    await _audit(episode_id, "raise_credit_cap", f"cap raised to {request.new_cap} (was {current})")
    return {"success": True, "episode_id": episode_id, "new_cap": request.new_cap, "previous_cap": current}


@app.post("/api/episodes/{episode_id}/resume-balance")
async def resume_balance(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Resume episode and shots after balance top-up when paused_insufficient_balance. Admin-only (financial control)."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=True)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        handle = temporal_client.get_workflow_handle(episode_workflow_id(episode_id))
        await handle.signal("resume_balance")
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error sending resume signal: {str(e)}")
    return {"success": True, "episode_id": episode_id, "message": "Resume balance signal sent"}


@app.get("/api/episodes/{episode_id}/credits")
async def get_credit_status(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Credit line of the run (cap, spent, reserved, pause threshold): drives the cap-raise control."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    from hfvg.credit_ledger import CreditLedger

    ledger = CreditLedger(DATABASE_PATH)
    await ledger.init_db()
    return {"episode_id": episode_id, **(await ledger.get_episode_status(episode_id)),
            "higgsfield_balance": await _higgsfield_balance_view()}


async def _higgsfield_balance_view() -> dict:
    """The manually recorded Higgsfield balance for the credits panel (GC.01 / B8): amount, when it
    was recorded and whether it is recent enough for a live run (HFVG_BALANCE_MAX_AGE_HOURS)."""
    from hfvg.budget import BudgetLedger, balance_freshness
    status = await BudgetLedger(DATABASE_PATH).get_balance_status()
    try:
        fresh = balance_freshness(status)
    except ValueError as e:
        fresh = {"ok": False, "reason": str(e), "age_hours": None, "max_age_hours": None}
    return {
        "balance_usd_micros": status.get("manual_balance_usd_micros"),
        "balance_remaining_usd_micros": status.get("balance_remaining_usd_micros"),
        "set_at": status.get("set_at"),
        "set_by": status.get("set_by"),
        "fresh": bool(fresh["ok"]),
        "age_hours": fresh.get("age_hours"),
        "max_age_hours": fresh.get("max_age_hours"),
        "reason": fresh.get("reason"),
    }


@app.get("/api/episodes/{episode_id}/shot-states")
async def get_shot_states(
    episode_id: str,
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Per-shot child workflow state for review: still / clip status and the G-REC-1 moderation ladder
    (rungs taken per phase, escalation). Shots whose workflow is gone are reported as unavailable."""
    await verify_auth_unified(authorization, studio_admin_token, require_admin=False)
    validate_episode_id(episode_id)
    if not temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    try:
        state = await temporal_client.get_workflow_handle(episode_workflow_id(episode_id)).query("get_state")
    except Exception as e:
        from temporalio.service import RPCError, RPCStatusCode
        if isinstance(e, RPCError) and e.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail=f"No workflow found for episode {episode_id}")
        raise HTTPException(status_code=500, detail=f"Error querying workflow: {e}")
    out: dict[str, dict] = {}
    for shot_id, wf_id in sorted((state.get("shot_workflow_ids") or {}).items()):
        try:
            child = await temporal_client.get_workflow_handle(wf_id).query("get_state")
        except Exception as e:
            out[shot_id] = {"available": False, "workflow_id": wf_id, "reason": str(e)[:200]}
            continue
        moderation = child.get("moderation") or {}
        out[shot_id] = {
            "available": True,
            "workflow_id": wf_id,
            "still_status": child.get("still_status"),
            "clip_status": child.get("clip_status"),
            "still_error": child.get("still_error"),
            "clip_error": child.get("clip_error"),
            "moderation": {phase: {"steps": list((m or {}).get("steps") or []),
                                   "retry": (m or {}).get("retry")}
                           for phase, m in moderation.items()},
            "moderation_escalation": child.get("moderation_escalation"),
            "pre_approved": bool(child.get("pre_approved")),
            "moderation_lint": child.get("moderation_lint") or {},
        }
    return {"episode_id": episode_id, "shots": out}


if __name__ == "__main__":  # after every route definition, or `python -m api.main` serves a partial app
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
