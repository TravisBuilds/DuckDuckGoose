"""Command-line interface for HFVG."""

import asyncio
import json
import os

import click
from temporalio.client import Client

from hfvg.config import config
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ReconcilePendingJobsWorkflow


@click.group()
def cli():
    """Hands-Free Video Generator CLI."""
    pass


@cli.command()
@click.argument("episode_id")
def lint(episode_id: str):
    """Lint episode configuration and beatmap."""
    import httpx
    from pathlib import Path
    
    # Check if episode directory exists
    episode_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id
    if not episode_dir.exists():
        click.echo(f"❌ Episode directory not found: {episode_dir}", err=True)
        import sys
        sys.exit(1)
    
    issues = []
    warnings = []
    
    # Check BEATMAP.md
    beatmap_path = episode_dir / "BEATMAP.md"
    if not beatmap_path.exists():
        issues.append(f"BEATMAP.md missing at {beatmap_path}")
    else:
        try:
            from hfvg.episode_parser import parse_beatmap
            shots = parse_beatmap(str(beatmap_path))
            if not shots:
                warnings.append("BEATMAP.md contains no shots")
            else:
                click.echo(f"✓ BEATMAP.md: {len(shots)} shots")
        except Exception as e:
            issues.append(f"BEATMAP.md parse error: {str(e)}")
    
    # Check shots.json
    shots_json = episode_dir / "shots.json"
    if not shots_json.exists():
        warnings.append("shots.json missing (required for live mode)")
    else:
        try:
            import json
            from hfvg.shot_data import parse_shots
            plans = parse_shots(json.loads(shots_json.read_text()))
            click.echo(f"✓ shots.json: valid")
        except Exception as e:
            issues.append(f"shots.json parse error: {str(e)}")
    
    # Check refs.json
    refs_json = episode_dir / "refs.json"
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
                warnings.append(f"{len(missing_refs)} reference files not found")
                for ref in missing_refs[:5]:
                    warnings.append(f"  - {ref}")
            else:
                click.echo(f"✓ refs.json: all references exist")
        except Exception as e:
            issues.append(f"refs.json parse error: {str(e)}")
    
    # Summary
    if issues:
        click.echo(f"\n❌ {len(issues)} issue(s) found:", err=True)
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
    
    if warnings:
        click.echo(f"\n⚠  {len(warnings)} warning(s):")
        for warning in warnings:
            click.echo(f"  - {warning}")
    
    if not issues and not warnings:
        click.echo(f"\n✓ {episode_id}: All checks passed")
        import sys
        sys.exit(0)
    
    import sys
    sys.exit(1 if issues else 0)


@cli.command()
@click.option("--episode-id", required=True, help="Unique episode ID")
@click.option("--idea", required=True, help="Video idea/concept")
@click.option("--platforms", default="instagram", help="Comma-separated platforms")
def start_episode(episode_id: str, idea: str, platforms: str):
    """Start a new episode workflow."""
    platform_list = [p.strip() for p in platforms.split(",")]

    async def _start():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        handle = await client.start_workflow(
            EpisodeWorkflow.run,
            args=[episode_id, idea, platform_list],
            id=episode_id,
            task_queue=config.TASK_QUEUE,
        )

        click.echo(f"Started episode workflow: {episode_id}")
        click.echo(f"Workflow ID: {handle.id}")
        click.echo(f"Run ID: {handle.result_run_id}")

    asyncio.run(_start())


@cli.command()
@click.argument("episode_id")
def query_state(episode_id: str):
    """Query current state of an episode."""

    async def _query():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        handle = client.get_workflow_handle(episode_id)
        state = await handle.query(EpisodeWorkflow.get_state)

        click.echo(json.dumps(state, indent=2))
        
        # Show what gate is waiting
        stage = state.get("stage", "")
        if not state.get("readback_approved"):
            click.echo("\n⏸  Waiting: approve readback")
        elif not state.get("character_locks_approved", True):  # May not be in old states
            click.echo("\n⏸  Waiting: approve character-locks")
        elif not state.get("storyboard_approved"):
            click.echo("\n⏸  Waiting: approve storyboard")
        elif stage in ["still_generation", "still_review"]:
            approved = set(state.get("scenes_approved", []))
            # Would need storyboard to know all scene IDs, so just show what's approved
            if approved:
                click.echo(f"\n⏸  Approved scenes: {sorted(approved)}")
                click.echo("   Waiting: approve remaining scene stills")
            else:
                click.echo("\n⏸  Waiting: approve scene stills (use --scene-id)")
        elif not state.get("final_approved"):
            click.echo("\n⏸  Waiting: approve final")
        elif stage == "post_approval":
            click.echo("\n⏸  Waiting: approve-posts")

    asyncio.run(_query())


@cli.command()
@click.argument("episode_id")
def query_storyboard(episode_id: str):
    """Query storyboard for an episode."""

    async def _query():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        handle = client.get_workflow_handle(episode_id)
        storyboard = await handle.query(EpisodeWorkflow.get_storyboard)

        click.echo(json.dumps(storyboard, indent=2))

    asyncio.run(_query())


@cli.command()
@click.argument("episode_id")
@click.argument("gate", type=click.Choice(["readback", "character-locks", "storyboard", "stills", "final"]))
@click.option("--scene-id", type=int, help="Scene ID (required for 'stills' gate)")
def approve(episode_id: str, gate: str, scene_id: int = None):
    """Send approval for a gate."""

    async def _approve():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        handle = client.get_workflow_handle(episode_id)

        if gate == "readback":
            result = await handle.execute_update(EpisodeWorkflow.approve_readback)
            click.echo(f"Read-back approved: {result}")

        elif gate == "character-locks":
            result = await handle.execute_update(EpisodeWorkflow.approve_character_locks)
            click.echo(f"Character locks approved: {result}")

        elif gate == "storyboard":
            result = await handle.execute_update(EpisodeWorkflow.approve_storyboard)
            click.echo(f"Storyboard approved: {result}")

        elif gate == "stills":
            if scene_id is None:
                click.echo("Error: --scene-id required for 'stills' gate", err=True)
                return
            result = await handle.execute_update(
                EpisodeWorkflow.approve_scene_stills, args=[scene_id]
            )
            click.echo(f"Scene {scene_id} stills approved: {result}")

        elif gate == "final":
            result = await handle.execute_update(EpisodeWorkflow.approve_final)
            click.echo(f"Final video approved: {result}")

    asyncio.run(_approve())


@cli.command()
@click.argument("episode_id")
@click.option("--platforms", default="instagram", help="Comma-separated platforms to approve")
def approve_posts(episode_id: str, platforms: str):
    """Approve posts for specified platforms."""
    platform_list = [p.strip() for p in platforms.split(",")]

    async def _approve():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        posting_id = f"{episode_id}-posting"
        handle = client.get_workflow_handle(posting_id)

        result = await handle.execute_update(PostingWorkflow.approve_posts, args=[platform_list])
        click.echo(f"Posts approved for {platforms}: {result}")

    asyncio.run(_approve())


@cli.command()
@click.argument("episode_id")
def wait_result(episode_id: str):
    """Wait for episode to complete and show result."""

    async def _wait():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )

        handle = client.get_workflow_handle(episode_id)
        result = await handle.result()

        click.echo(json.dumps(result, indent=2))

    asyncio.run(_wait())


@cli.command()
@click.option("--model", default=None, help="Model to use (defaults to still_model_path())")
@click.option("--resolution", default="1k", help="Image resolution")
@click.option("--quality", default="medium", help="Image quality")
@click.option("--prompt", default="A cozy mountain resort lobby with timber beams", help="Generation prompt")
def canary(model: str | None, resolution: str, quality: str, prompt: str):
    """
    Generate ONE draft-tier still for real (with confirmation).
    
    This is the canary test for verifying Higgsfield credentials and generation.
    NOT run in CI. Only run manually with real credentials.
    
    Example:
        hfvg canary --model xai/grok-imagine-image-2.0 --resolution 1k --quality medium
    """

    async def _canary():
        from hfvg.providers import HiggsfieldProvider
        from hfvg.config import still_model_path
        
        if model is None:
            model_to_use = still_model_path()
        else:
            model_to_use = model
        
        click.echo(f"Using model: {model_to_use}")
        
        # Check if running in dry-run mode
        if config.DRY_RUN:
            click.echo("❌ ERROR: DRY_RUN=true")
            click.echo("To run canary test, set DRY_RUN=false in environment")
            return
        
        # Initialize provider with real API
        provider = HiggsfieldProvider(dry_run=False)
        
        try:
            # Get balance
            balance = await provider.get_balance()
            click.echo(f"Current balance: {balance:.1f} credits")
            
            # Estimate cost
            cost = await provider.estimate_cost("image", {
                "model": model_to_use,
                "resolution": resolution,
                "quality": quality
            })
            
            click.echo(f"\n📊 Generation Details:")
            click.echo(f"  Model: {model_to_use}")
            click.echo(f"  Resolution: {resolution}")
            click.echo(f"  Quality: {quality}")
            click.echo(f"  Estimated cost: {cost:.1f} credits")
            click.echo(f"  Balance after: {balance - cost:.1f} credits")
            click.echo(f"\n  Prompt: {prompt}")
            
            # Confirm
            click.echo(f"\n⚠️  This will spend REAL credits on Higgsfield")
            confirm = click.confirm("Confirm generation?", default=False)
            
            if not confirm:
                click.echo("Cancelled.")
                return
            
            # Submit job
            click.echo("\n🚀 Submitting job...")
            job_id = await provider.submit_image(
                prompt=prompt,
                model=model_to_use,
                resolution=resolution,
                quality=quality
            )
            
            click.echo(f"Job ID: {job_id}")
            click.echo("Polling for completion...")
            
            # Poll until complete
            while True:
                status = await provider.get_job_status(job_id)
                
                if status.status.value == "completed":
                    click.echo(f"\n✅ Success!")
                    click.echo(f"Output URL: {status.output_url}")
                    click.echo(f"Actual cost: {status.cost:.1f} credits")
                    break
                    
                elif status.status.value == "failed":
                    click.echo(f"\n❌ Failed: {status.error}")
                    break
                    
                elif status.status.value == "blocked":
                    click.echo(f"\n🚫 Blocked by moderation")
                    click.echo(f"Credits refunded automatically")
                    break
                
                click.echo(f"Progress: {status.progress * 100:.0f}%")
                await asyncio.sleep(2)
                
        except Exception as e:
            click.echo(f"\n❌ Error: {e}")
            click.echo("\nTroubleshooting:")
            click.echo("1. Check HIGGSFIELD_API_KEY is set")
            click.echo("2. Check API key is valid in Higgsfield dashboard")
            click.echo("3. Check account has sufficient credits")

    asyncio.run(_canary())


@cli.command()
@click.argument("episode_id")
@click.option("--live", is_flag=True, help="Run live smoke test (spends real credits)")
def smoke_test(episode_id: str, live: bool):
    """Run smoke test to validate Higgsfield API connectivity and billing."""
    if not live:
        click.echo("❌ Smoke test requires --live flag (spends ~0.09 credits)", err=True)
        click.echo("   Usage: hfvg smoke-test <episode-id> --live", err=True)
        import sys
        sys.exit(1)
    
    import os
    import aiosqlite
    from hfvg.activities.smoke_test import run_smoke_test
    from hfvg.studio_db import store_smoke_test_result
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
    # Check episode exists before spending
    async def check_episode_exists():
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT episode_id FROM episodes WHERE episode_id = ?", (episode_id,))
            return await cursor.fetchone() is not None
    
    if not asyncio.run(check_episode_exists()):
        click.echo(f"❌ Episode '{episode_id}' not found in database", err=True)
        import sys
        sys.exit(1)
    
    click.echo(f"🧪 Running live smoke test for {episode_id}...")
    click.echo("   Upload probe + SOUL V2 generation (~0.09 credits)")
    
    result = asyncio.run(run_smoke_test(episode_id, db_path))
    
    # Store result in DB
    asyncio.run(store_smoke_test_result(db_path, episode_id, result))
    
    click.echo()
    click.echo(f"Upload probe:      {'✅ PASSED' if result['upload_probe']['passed'] else '❌ FAILED'}")
    if result['upload_probe']['error']:
        click.echo(f"  Error: {result['upload_probe']['error']}")
    else:
        click.echo(f"  Public URL: {result['upload_probe']['public_url']}")
    
    click.echo()
    click.echo(f"Generation probe:  {'✅ PASSED' if result['generation_probe']['passed'] else '❌ FAILED'}")
    if result['generation_probe']['error']:
        click.echo(f"  Error: {result['generation_probe']['error']}")
    else:
        click.echo(f"  Provider ID: {result['generation_probe']['provider_job_id']}")
    
    click.echo()
    click.echo(f"Overall:           {'✅ PASSED' if result['passed'] else '❌ FAILED'}")
    click.echo(f"Timestamp:         {result['timestamp']}")
    
    import sys
    sys.exit(0 if result['passed'] else 1)


@cli.command()
@click.argument("episode_id")
@click.argument("shot_id")
def retry_shot(episode_id: str, shot_id: str):
    """Retry a failed shot (only valid while parked at G1/G2)."""
    
    async def _retry():
        from temporalio.client import WorkflowUpdateFailedError
        from temporalio.exceptions import ApplicationError
        
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )
        
        handle = client.get_workflow_handle(episode_id)
        
        try:
            result = await handle.execute_update("retry_shot", shot_id)
            click.echo(f"✓ Retry queued for shot {shot_id}: {result}")
        except WorkflowUpdateFailedError as e:
            if e.cause and isinstance(e.cause, ApplicationError):
                click.echo(f"❌ Retry refused: {e.cause}", err=True)
            else:
                click.echo(f"❌ Error: {e}", err=True)
            import sys
            sys.exit(1)
        except Exception as e:
            click.echo(f"❌ Error: {e}", err=True)
            import sys
            sys.exit(1)
    
    asyncio.run(_retry())


if __name__ == "__main__":
    cli()


@cli.command("set-anchor")
@click.argument("episode_id")
@click.argument("anchor_shot_id")
@click.option("--for", "target_shots", required=True, help="Comma-separated list of target shot IDs (e.g., A02,A03,A04)")
@click.option("--file", "file_path", help="External file path (requires --approved-by)")
@click.option("--approved-by", help="Approval note (required for external files)")
def set_anchor(episode_id: str, anchor_shot_id: str, target_shots: str, file_path: str | None, approved_by: str | None):
    """
    Set an anchor still as a reference for target shots.
    
    Examples:
        # Use G2-approved still as anchor
        hfvg set-anchor ep05 A01 --for A02,A03,A04
        
        # Use external file as anchor
        hfvg set-anchor ep05 A01 --for A02,A03,A04 --file /path/to/still.png --approved-by "Travis approved via Studio"
    """
    import httpx
    import os
    
    target_shot_ids = [s.strip() for s in target_shots.split(",") if s.strip()]
    if not target_shot_ids:
        click.echo("❌ Error: --for must specify at least one target shot", err=True)
        import sys
        sys.exit(1)
    
    # Reject self-anchoring
    if anchor_shot_id in target_shot_ids:
        click.echo(f"❌ Error: Cannot anchor shot {anchor_shot_id} to itself", err=True)
        import sys
        sys.exit(1)
    
    # External file requires approved_by
    if file_path and not approved_by:
        click.echo("❌ Error: External file requires --approved-by note", err=True)
        import sys
        sys.exit(1)
    
    # Call API endpoint
    api_base = os.getenv("HFVG_API_BASE", "http://localhost:8000")
    admin_secret = os.getenv("ADMIN_SECRET")
    if not admin_secret:
        click.echo("❌ Error: ADMIN_SECRET environment variable not set", err=True)
        import sys
        sys.exit(1)
    
    headers = {"Authorization": f"Bearer {admin_secret}"}
    payload = {
        "anchor_shot_id": anchor_shot_id,
        "target_shot_ids": target_shot_ids,
    }
    
    if file_path:
        payload["file_path"] = file_path
    if approved_by:
        payload["approved_by"] = approved_by
    
    try:
        response = httpx.post(
            f"{api_base}/api/episodes/{episode_id}/anchor",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
        
        click.echo(f"✓ Anchor still set successfully")
        click.echo(f"  Episode: {result['episode_id']}")
        click.echo(f"  Anchor: {result['anchor_shot_id']}")
        click.echo(f"  Target shots: {', '.join(result['target_shot_ids'])}")
        click.echo(f"  Local path: {result['local_path']}")
        click.echo(f"  Version: {result['version']}")
        click.echo(f"  SHA256: {result['sha256']}")
        if result.get('approved_by'):
            click.echo(f"  Approved by: {result['approved_by']}")
        
    except httpx.HTTPStatusError as e:
        click.echo(f"❌ HTTP {e.response.status_code}: {e.response.text}", err=True)
        import sys
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Error: {str(e)}", err=True)
        import sys
        sys.exit(1)


@cli.command("start-v2")
@click.argument("episode_id")
@click.option("--beatmap", help="Path to BEATMAP.md file")
@click.option("--dry-run/--live", default=True, help="Run in dry-run mode (default: true)")
@click.option("--shots", help="Comma-separated shot IDs subset (e.g., A01,A02)")
@click.option("--credit-cap", type=int, help="Credit cap for this run")
@click.option("--attempt-overrides", help="JSON shot_id->attempt mapping (e.g., '{\"A02\": 2}')")
def start_v2(episode_id: str, beatmap: str | None, dry_run: bool, shots: str | None, credit_cap: int | None, attempt_overrides: str | None):
    """Start episode using EpisodeWorkflowV2 via API.
    
    Examples:
        # Start with default settings
        hfvg start-v2 ep05
        
        # Start specific shots as retries
        hfvg start-v2 ep05 --shots A01,A02 --attempt-overrides '{"A02": 2}'
    """
    import httpx
    import os
    
    api_base = os.getenv("HFVG_API_BASE", "http://localhost:8000")
    admin_secret = os.getenv("ADMIN_SECRET")
    if not admin_secret:
        click.echo("❌ Error: ADMIN_SECRET environment variable not set", err=True)
        import sys
        sys.exit(1)
    
    headers = {"Authorization": f"Bearer {admin_secret}"}
    payload = {
        "episode_id": episode_id,
        "dry_run": dry_run,
    }
    
    if beatmap:
        from pathlib import Path
        beatmap_path = Path(beatmap)
        if not beatmap_path.exists():
            click.echo(f"❌ Error: Beatmap file not found: {beatmap}", err=True)
            import sys
            sys.exit(1)
        payload["beatmap_content"] = beatmap_path.read_text()
    
    if shots:
        payload["shots_subset"] = [s.strip() for s in shots.split(",")]
    
    if credit_cap:
        payload["credit_cap"] = credit_cap
    
    if attempt_overrides:
        try:
            payload["attempt_overrides"] = json.loads(attempt_overrides)
        except json.JSONDecodeError as e:
            click.echo(f"❌ Error: Invalid JSON in --attempt-overrides: {e}", err=True)
            import sys
            sys.exit(1)
    
    try:
        response = httpx.post(
            f"{api_base}/api/episodes",
            json=payload,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
        
        click.echo(f"✓ Episode {episode_id} started")
        click.echo(f"  Workflow ID: {result['workflow_id']}")
        click.echo(f"  Run ID: {result['run_id']}")
        click.echo(f"  Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
        if payload.get("shots_subset"):
            click.echo(f"  Shots: {', '.join(payload['shots_subset'])}")
        if payload.get("attempt_overrides"):
            click.echo(f"  Attempt overrides: {payload['attempt_overrides']}")
        
    except httpx.HTTPStatusError as e:
        click.echo(f"❌ HTTP {e.response.status_code}: {e.response.text}", err=True)
        import sys
        sys.exit(1)
    except Exception as e:
        click.echo(f"❌ Error: {str(e)}", err=True)
        import sys
        sys.exit(1)


@cli.command()
@click.argument("episode_id")
@click.option("--shots", "shots_path", default=None, help="shots.json path (default: data/episodes/<id>/shots.json)")
def preflight(episode_id: str, shots_path: str | None):
    """
    Check every shot of an episode against the model limits (no provider calls).

    Exit code 0 if no errors, 1 if errors exist.

    Example:
        hfvg preflight ep05
    """
    import sys
    from pathlib import Path
    from hfvg.preflight import preflight_plans
    from hfvg.shot_data import ShotDataError, parse_shots

    path = Path(shots_path) if shots_path else Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "shots.json"
    if not path.exists():
        click.echo(f"❌ {path} not found", err=True)
        sys.exit(1)
    try:
        raw = json.loads(path.read_text())
        parse_shots(raw)
    except (ValueError, ShotDataError) as e:
        click.echo(f"❌ Invalid shots.json: {e}", err=True)
        sys.exit(1)

    report = preflight_plans(raw["shots"])
    click.echo(f"Preflight report: {episode_id} ({path})")
    for sid, res in report["shots"].items():
        click.echo(f"  {'✓' if res['ok'] else '✗'} {sid}")
        for text in res["errors"]:
            click.echo(f"      error: {text}", err=True)
        for text in res["warnings"]:
            click.echo(f"      warning: {text}")
    sys.exit(1 if report["errors"] else 0)


@cli.command()
@click.option("--max-jobs", default=100, help="Maximum number of jobs to reconcile")
def reconcile(max_jobs: int):
    """
    Reconcile pending_reconcile jobs: re-poll provider status, land late assets, commit/release budget.
    
    This runs the reconciler workflow once. For continuous reconciliation, schedule this via cron
    or use Temporal's schedule feature.
    
    Example:
        hfvg reconcile
        hfvg reconcile --max-jobs 50
    """
    async def run_reconcile():
        client = await Client.connect(
            config.TEMPORAL_HOST,
            namespace=config.TEMPORAL_NAMESPACE,
        )
        
        workflow_id = f"reconcile-{asyncio.get_running_loop().time():.0f}"
        
        click.echo(f"Starting reconciliation workflow (max_jobs={max_jobs})...")
        
        handle = await client.start_workflow(
            ReconcilePendingJobsWorkflow.run,
            max_jobs,
            id=workflow_id,
            task_queue=config.TASK_QUEUE,
        )
        
        click.echo(f"Workflow started: {workflow_id}")
        click.echo("Waiting for completion...")
        
        result = await handle.result()
        
        click.echo("\n✓ Reconciliation complete:")
        click.echo(f"  Total jobs: {result['total']}")
        click.echo(f"  Completed: {result['completed']}")
        click.echo(f"  Failed: {result['failed']}")
        click.echo(f"  Still pending: {result['still_pending']}")
        
        if result['errors']:
            click.echo(f"\n⚠  {len(result['errors'])} errors:")
            for err in result['errors'][:5]:
                click.echo(f"    Job {err['job_id']}: {err['error']}")
            if len(result['errors']) > 5:
                click.echo(f"    ... and {len(result['errors']) - 5} more")
    
    asyncio.run(run_reconcile())


@cli.command()
@click.option("--threshold", default=45, help="Minutes without progress to consider stalled")
def stalled(threshold: int):
    """
    Detect and report stalled jobs (jobs with no progress beyond threshold).
    
    This queries the database for jobs that have been in 'reserved' status for longer
    than the threshold without completion.
    
    Example:
        hfvg stalled
        hfvg stalled --threshold 60
    """
    async def check_stalled():
        from hfvg.activities.reconcile import detect_stalled_jobs
        from hfvg.budget import BudgetLedger
        
        # Initialize ledger
        db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        click.echo(f"Checking for stalled jobs (threshold={threshold} minutes)...")
        
        # Import and call the activity directly (not through Temporal)
        from temporalio import activity
        from unittest.mock import MagicMock
        
        # Mock activity context for direct call
        activity.info = lambda: MagicMock()
        activity.logger = MagicMock()
        
        stalled_jobs = await detect_stalled_jobs(threshold)
        
        if not stalled_jobs:
            click.echo("\n✓ No stalled jobs found")
            return
        
        click.echo(f"\n⚠  Found {len(stalled_jobs)} stalled job(s):")
        for job in stalled_jobs:
            click.echo(
                f"  {job['job_type']} {job['shot_id']} (job {job['job_id']}): "
                f"stalled for {job['minutes_stalled']} minutes"
            )
        
        click.echo(
            f"\nRun 'hfvg reconcile' to attempt recovery of these jobs."
        )
    
    asyncio.run(check_stalled())
