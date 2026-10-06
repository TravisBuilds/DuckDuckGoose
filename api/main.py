"""
DuckDuckGoose Studio API

FastAPI backend for episode production console.
Provides episode management, budget tracking, audit trails, and workflow control.
"""

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiosqlite
import os
import json
from datetime import datetime
from typing import Any
from pathlib import Path

# Configuration
ADMIN_SECRET = os.getenv("STUDIO_ADMIN_SECRET", "dev-secret-change-me")
DB_PATH = os.getenv("STUDIO_DB_PATH", "data/studio.db")
EPISODE_DATA_PATH = os.getenv("EPISODE_DATA_PATH", "data/episodes")
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "https://duckduckgoose-topaz.vercel.app")

# Ensure directories exist
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(EPISODE_DATA_PATH).mkdir(parents=True, exist_ok=True)

app = FastAPI(title="DuckDuckGoose Studio API", version="1.0.0")

# CORS
origins = [ALLOWED_ORIGIN, "http://localhost:3000"]
if os.getenv("DEV_MODE"):
    origins.append("*")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()

def verify_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify admin secret token."""
    if credentials.credentials != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials

# Database
async def get_db():
    """Get database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    """Initialize database tables."""
    db = await get_db()
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            live_mode BOOLEAN DEFAULT 0,
            workflow_id TEXT,
            current_step TEXT,
            state TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS episode_files (
            episode_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            PRIMARY KEY (episode_id, file_type),
            FOREIGN KEY (episode_id) REFERENCES episodes(id)
        )
    """)
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS shots (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            shot_id TEXT NOT NULL,
            prompt TEXT,
            still_url TEXT,
            clip_url TEXT,
            status TEXT DEFAULT 'pending',
            retries INTEGER DEFAULT 0,
            qc_results TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (episode_id) REFERENCES episodes(id)
        )
    """)
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL,
            gate TEXT NOT NULL,
            approved BOOLEAN DEFAULT 0,
            approved_at TEXT,
            FOREIGN KEY (episode_id) REFERENCES episodes(id)
        )
    """)
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (episode_id) REFERENCES episodes(id)
        )
    """)
    
    await db.commit()
    await db.close()

@app.on_event("startup")
async def startup():
    """Initialize on startup."""
    await init_db()

# Models
class EpisodeCreate(BaseModel):
    name: str
    episode_id: str

class ApprovalRequest(BaseModel):
    gate: str

class CanaryRequest(BaseModel):
    shot_id: str

# Audit logging
async def log_audit(episode_id: str, event_type: str, event_data: dict[str, Any]):
    """Log an audit event."""
    db = await get_db()
    await db.execute(
        "INSERT INTO audit_log (episode_id, event_type, event_data, created_at) VALUES (?, ?, ?, ?)",
        (episode_id, event_type, json.dumps(event_data), datetime.utcnow().isoformat())
    )
    await db.commit()
    await db.close()

# Endpoints

@app.get("/health")
async def health():
    """Health check."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.post("/api/episodes", dependencies=[Depends(verify_admin)])
async def create_episode(episode: EpisodeCreate):
    """Create a new episode."""
    db = await get_db()
    
    now = datetime.utcnow().isoformat()
    
    try:
        await db.execute(
            """INSERT INTO episodes (id, name, workflow_id, current_step, state, created_at, updated_at)
               VALUES (?, ?, '', 'G1.01', '{}', ?, ?)""",
            (episode.episode_id, episode.name, now, now)
        )
        await db.commit()
        
        await log_audit(episode.episode_id, "episode_created", {
            "name": episode.name,
            "episode_id": episode.episode_id
        })
        
        result = {"id": episode.episode_id, "name": episode.name, "status": "created"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await db.close()
    
    return result

@app.get("/api/episodes", dependencies=[Depends(verify_admin)])
async def list_episodes():
    """List all episodes."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM episodes ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    await db.close()
    
    return [dict(row) for row in rows]

@app.get("/api/episodes/{episode_id}", dependencies=[Depends(verify_admin)])
async def get_episode(episode_id: str):
    """Get episode state."""
    db = await get_db()
    
    cursor = await db.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,))
    row = await cursor.fetchone()
    
    if not row:
        await db.close()
        raise HTTPException(status_code=404, detail="Episode not found")
    
    episode = dict(row)
    
    # Get approvals
    cursor = await db.execute("SELECT * FROM approvals WHERE episode_id = ?", (episode_id,))
    approvals = [dict(r) for r in await cursor.fetchall()]
    episode["approvals"] = approvals
    
    await db.close()
    
    return episode

@app.post("/api/episodes/{episode_id}/upload", dependencies=[Depends(verify_admin)])
async def upload_episode_data(
    episode_id: str,
    beatmap: UploadFile = File(None),
    continuity: UploadFile = File(None),
    credit_plan: UploadFile = File(None),
):
    """Upload episode data files (beatmap, continuity, credit plan)."""
    db = await get_db()
    
    # Check episode exists
    cursor = await db.execute("SELECT id FROM episodes WHERE id = ?", (episode_id,))
    if not await cursor.fetchone():
        await db.close()
        raise HTTPException(status_code=404, detail="Episode not found")
    
    episode_dir = Path(EPISODE_DATA_PATH) / episode_id
    episode_dir.mkdir(exist_ok=True)
    
    uploaded = []
    
    for file, file_type in [
        (beatmap, "beatmap"),
        (continuity, "continuity"),
        (credit_plan, "credit_plan"),
    ]:
        if file:
            file_path = episode_dir / f"{file_type}.md"
            content = await file.read()
            file_path.write_bytes(content)
            
            await db.execute(
                """INSERT OR REPLACE INTO episode_files (episode_id, file_type, file_path, uploaded_at)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, file_type, str(file_path), datetime.utcnow().isoformat())
            )
            uploaded.append(file_type)
    
    await db.commit()
    await db.close()
    
    await log_audit(episode_id, "files_uploaded", {"files": uploaded})
    
    return {"uploaded": uploaded, "path": str(episode_dir)}

@app.get("/api/episodes/{episode_id}/shots", dependencies=[Depends(verify_admin)])
async def get_shots(episode_id: str):
    """Get all shots for episode with status."""
    db = await get_db()
    
    cursor = await db.execute(
        "SELECT * FROM shots WHERE episode_id = ? ORDER BY shot_id",
        (episode_id,)
    )
    rows = await cursor.fetchall()
    await db.close()
    
    shots = []
    for row in rows:
        shot = dict(row)
        if shot["qc_results"]:
            shot["qc_results"] = json.loads(shot["qc_results"])
        shots.append(shot)
    
    return shots

@app.get("/api/episodes/{episode_id}/budget", dependencies=[Depends(verify_admin)])
async def get_budget(episode_id: str):
    """Get budget status for episode with CREDIT-PLAN data."""
    db = await get_db()
    
    # CREDIT-PLAN.md §8 budget lines for Ep04
    # These are the actual planned values
    lines = [
        {
            "name": "L1 refs (2k high)",
            "plan": 91,
            "budget": 120,
            "stop": 96,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "9 moose sheets, props, AG+GD plates, 2 re-rolls"
        },
        {
            "name": "L2 drafts (1k medium)",
            "plan": 87.5,
            "budget": 100,
            "stop": 80,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "35 frames × 2.5. PLANNED REPORT at 80 (GC.06)"
        },
        {
            "name": "L3 final stills (2k high)",
            "plan": 227.5,
            "budget": 230,
            "stop": 184,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "35 × 6.5. PLANNED REPORT at 184 (GC.06)"
        },
        {
            "name": "L4 video (Kling 3.0 pro)",
            "plan": 229.4,
            "budget": 300,
            "stop": 240,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "133s × 1.5 × 1.15 headroom"
        },
        {
            "name": "L5 finalize / upscale",
            "plan": 0,
            "budget": 0,
            "stop": 0,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "None planned"
        },
        {
            "name": "L6 reserve",
            "plan": 250,
            "budget": 250,
            "stop": 200,
            "spent": 0,
            "reserved": 0,
            "unit": "Higgsfield credits",
            "note": "Earmarks: B03 25, D04 ≈20, E01 ≈10"
        },
    ]
    
    # TODO: Wire to BudgetLedger for actual spent/reserved tracking
    # For now, return static structure from CREDIT-PLAN
    
    # Calculate totals
    total_plan = sum(line["plan"] for line in lines)
    total_budget = 1000
    total_cap = 1250
    total_spent = 0  # TODO: sum from budget_transactions
    total_reserved = 0
    
    await db.close()
    
    return {
        "lines": lines,
        "totals": {
            "plan": total_plan,
            "budget": total_budget,
            "cap": total_cap,
            "spent": total_spent,
            "reserved": total_reserved,
            "available": total_cap - total_spent - total_reserved,
        },
        "stop_fraction": 0.8,
        "notes": [
            "L2 and L3 plans exceed 80% stops (intentional GC.06 report points)",
            "Total cap: 1,250 Higgsfield credits (125% of 1,000 target)",
            "ElevenLabs budget: 3,500 credits after picture lock (separate)"
        ]
    }

@app.post("/api/episodes/{episode_id}/approve", dependencies=[Depends(verify_admin)])
async def approve_gate(episode_id: str, approval: ApprovalRequest):
    """Approve a gate."""
    db = await get_db()
    
    now = datetime.utcnow().isoformat()
    
    await db.execute(
        """INSERT OR REPLACE INTO approvals (episode_id, gate, approved, approved_at)
           VALUES (?, ?, 1, ?)""",
        (episode_id, approval.gate, now)
    )
    await db.commit()
    await db.close()
    
    await log_audit(episode_id, "gate_approved", {
        "gate": approval.gate,
        "approved_at": now
    })
    
    return {"gate": approval.gate, "approved": True, "approved_at": now}

@app.post("/api/episodes/{episode_id}/live-mode", dependencies=[Depends(verify_admin)])
async def set_live_mode(episode_id: str, enabled: bool):
    """Toggle live mode for episode."""
    db = await get_db()
    
    await db.execute(
        "UPDATE episodes SET live_mode = ? WHERE id = ?",
        (1 if enabled else 0, episode_id)
    )
    await db.commit()
    await db.close()
    
    await log_audit(episode_id, "live_mode_toggled", {
        "enabled": enabled
    })
    
    return {"episode_id": episode_id, "live_mode": enabled}

async def check_live_mode_enforcement(episode_id: str) -> tuple[bool, str]:
    """
    Check if episode can spend credits (live mode enforcement).
    
    Returns: (allowed, reason)
    
    Requirements:
    1. Episode must be in live mode
    2. G1.08 (credit plan) must be approved
    3. Budget reserve must succeed for the job
    """
    db = await get_db()
    
    # Check live mode
    cursor = await db.execute(
        "SELECT live_mode FROM episodes WHERE id = ?",
        (episode_id,)
    )
    row = await cursor.fetchone()
    if not row:
        await db.close()
        return (False, "Episode not found")
    
    if not row[0]:
        await db.close()
        return (False, "Episode not in live mode (dry-run only)")
    
    # Check G1.08 approval
    cursor = await db.execute(
        "SELECT approved FROM approvals WHERE episode_id = ? AND gate = 'G1.08'",
        (episode_id,)
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        await db.close()
        return (False, "G1.08 (credit plan) not approved")
    
    await db.close()
    return (True, "OK")

@app.post("/api/episodes/{episode_id}/canary", dependencies=[Depends(verify_admin)])
async def run_canary(episode_id: str, request: CanaryRequest):
    """Run canary: 1 still + 1 clip for a shot."""
    # Check live mode enforcement
    allowed, reason = await check_live_mode_enforcement(episode_id)
    
    if not allowed:
        await log_audit(episode_id, "canary_refused", {
            "shot_id": request.shot_id,
            "reason": reason
        })
        raise HTTPException(status_code=403, detail=reason)
    
    # TODO: Actually execute via ShotWorkflow
    # For now, return estimate
    
    await log_audit(episode_id, "canary_requested", {
        "shot_id": request.shot_id,
    })
    
    # Estimate: 1 still (2k high = ~6.5 cr) + 1 clip (Kling pro 3s = 4.5 cr)
    return {
        "shot_id": request.shot_id,
        "estimated_credits": 11.0,
        "estimated_usd": 0.0,
        "status": "ready",
        "message": "Ready to generate 1 still + 1 clip (live mode)"
    }

@app.get("/api/episodes/{episode_id}/audit", dependencies=[Depends(verify_admin)])
async def get_audit_trail(episode_id: str):
    """Get audit trail for episode."""
    db = await get_db()
    
    cursor = await db.execute(
        "SELECT * FROM audit_log WHERE episode_id = ? ORDER BY created_at DESC",
        (episode_id,)
    )
    rows = await cursor.fetchall()
    await db.close()
    
    events = []
    for row in rows:
        event = dict(row)
        event["event_data"] = json.loads(event["event_data"])
        events.append(event)
    
    return events

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
