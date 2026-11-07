#!/usr/bin/env python3
"""Demo server for dashboard screenshots (no Temporal required)."""

import os
import sys

# Set environment before importing
os.environ["DATABASE_PATH"] = os.path.join(os.path.dirname(__file__), "../data/dashboard_demo.db")
os.environ["ADMIN_SECRET"] = "admin-secret-for-dashboard-testing-12345678901234567890"
os.environ["DRY_RUN"] = "true"
os.environ["TEMPORAL_ADDRESS"] = "localhost:7233"  # Won't actually connect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio

# Import only what we need
from hfvg.studio_db import init_studio_db
from hfvg.users import UserManager
from hfvg.cost_tracking import CostTracker
from hfvg.migrations import run_all_migrations

# Import the endpoints we need (reuse from api/main.py)
# We'll create a minimal app without Temporal

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize without Temporal."""
    DATABASE_PATH = os.environ["DATABASE_PATH"]
    
    # Initialize database
    await init_studio_db(DATABASE_PATH)
    
    # Initialize user system
    user_mgr = UserManager(DATABASE_PATH)
    await user_mgr.init_db()
    cost_tracker = CostTracker(DATABASE_PATH)
    await cost_tracker.init_db()
    await run_all_migrations(DATABASE_PATH)
    
    print(f"Demo server initialized with database: {DATABASE_PATH}")
    yield

app = FastAPI(lifespan=lifespan)

# Enable CORS for Next.js dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and add dashboard routes
# We'll manually add just the dashboard endpoint
from api.main import (
    get_dashboard,
    list_users,
    create_user,
    get_user_details,
    CreateUserRequest,
    login,
    LoginRequest
)

app.get("/api/dashboard")(get_dashboard)
app.get("/api/users")(list_users)
app.post("/api/users")(create_user)
app.get("/api/users/{user_id}")(get_user_details)
app.post("/api/login")(login)

@app.get("/api/health")
async def health():
    return {"status": "ok", "temporal": False}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
