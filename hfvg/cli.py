"""Command-line interface for HFVG."""

import asyncio
import json

import click
from temporalio.client import Client

from hfvg.config import config
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow


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
    episode_dir = Path("./data/episodes") / episode_id
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
@click.option("--model", default="gpt_image_2", help="Model to use")
@click.option("--resolution", default="1k", help="Image resolution")
@click.option("--quality", default="medium", help="Image quality")
@click.option("--prompt", default="A cozy mountain resort lobby with timber beams", help="Generation prompt")
def canary(model: str, resolution: str, quality: str, prompt: str):
    """
    Generate ONE draft-tier still for real (with confirmation).
    
    This is the canary test for verifying Higgsfield credentials and generation.
    NOT run in CI. Only run manually with real credentials.
    
    Example:
        hfvg canary --model gpt_image_2 --resolution 1k --quality medium
    """

    async def _canary():
        from hfvg.providers import HiggsfieldProvider
        
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
                "model": model,
                "resolution": resolution,
                "quality": quality
            })
            
            click.echo(f"\n📊 Generation Details:")
            click.echo(f"  Model: {model}")
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
                model=model,
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
    from hfvg.activities.smoke_test import run_smoke_test
    from hfvg.studio_db import store_smoke_test_result
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
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
        click.echo(f"  Cost: {result['generation_probe']['cost']} credits")
    
    click.echo()
    click.echo(f"Overall:           {'✅ PASSED' if result['passed'] else '❌ FAILED'}")
    click.echo(f"Timestamp:         {result['timestamp']}")
    
    import sys
    sys.exit(0 if result['passed'] else 1)


if __name__ == "__main__":
    cli()
