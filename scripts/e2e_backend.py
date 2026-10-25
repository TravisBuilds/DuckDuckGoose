#!/usr/bin/env python3
"""Real dry-run backend for the Playwright E2E (no mocks, no provider calls, no spend).

Starts, in ONE process:
  * a Temporal dev/test server (WorkflowEnvironment.start_local, binary downloaded by the SDK; the
    time-skipping server cannot be used: it lacks ListWorkflowExecutions, which G1 approval needs),
  * the production worker (PRODUCTION_WORKFLOWS + PRODUCTION_ACTIVITIES on config.TASK_QUEUE),
  * the real FastAPI app (api.main:app) under uvicorn, connected to that test server.

Everything runs in DRY_RUN with a throw-away work dir (database, EPISODES_DIR, dry-run outputs),
so the browser drives exactly the code paths the studio uses in production, minus provider calls.

Usage:  ADMIN_SECRET=<32+ chars> python scripts/e2e_backend.py [--port 8000] [--workdir DIR]
Readiness: GET http://127.0.0.1:<port>/api/health returns 200 once the worker and API are up.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _prepare_env(workdir: Path) -> None:
    secret = os.environ.get("ADMIN_SECRET", "")
    if len(secret) < 32:
        sys.exit("ADMIN_SECRET (32+ chars) must be set: the API refuses to authenticate without it")
    (workdir / "data" / "episodes").mkdir(parents=True, exist_ok=True)
    os.environ["DRY_RUN"] = "true"  # hard-wired: this launcher never talks to a provider
    os.environ["DATABASE_PATH"] = str(workdir / "studio.db")
    os.environ["EPISODES_DIR"] = str(workdir / "data" / "episodes")
    os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    for key in [k for k in os.environ if k.endswith("_API_KEY") or k.startswith("HIGGSFIELD_")]:
        os.environ.pop(key)  # belt and braces: no provider credentials reach the dry run
    os.chdir(workdir)  # dry-run outputs and reports are written relative to the cwd
    sys.path.insert(0, str(REPO))


async def _main(port: int) -> None:
    import uvicorn
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from hfvg.budget import BudgetLedger
    from hfvg.config import config
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.temporal_converter import temporal_data_converter
    from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS

    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()

    async with await WorkflowEnvironment.start_local(data_converter=temporal_data_converter) as env:
        target = env.client.service_client.config.target_host
        os.environ["TEMPORAL_ADDRESS"] = target
        os.environ["TEMPORAL_NAMESPACE"] = env.client.namespace
        async with Worker(env.client, task_queue=config.TASK_QUEUE,
                          workflows=PRODUCTION_WORKFLOWS, activities=PRODUCTION_ACTIVITIES):
            from api import main as api_main  # imported after the env is final (module-level config)

            api_main.TEMPORAL_ADDRESS = target
            api_main.TEMPORAL_NAMESPACE = env.client.namespace
            print(f"[e2e-backend] temporal dev server {target}; worker on {config.TASK_QUEUE}; "
                  f"api on :{port}; workdir {os.getcwd()}", flush=True)
            server = uvicorn.Server(uvicorn.Config(api_main.app, host="127.0.0.1", port=port, log_level="info"))
            await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--workdir", type=Path, default=None)
    args = parser.parse_args()
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="hfvg-e2e-"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    _prepare_env(workdir)
    asyncio.run(_main(args.port))


if __name__ == "__main__":
    main()
