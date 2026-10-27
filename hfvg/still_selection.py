"""GAP-26: G2 reviewer keeps an EARLIER still version of a shot (e.g. V23 v2 instead of the latest v3).

Shared by the API route (POST /api/episodes/{ep}/shots/{shot}/select-still) and the CLI
(``hfvg select-still``). Before the workflow update ``select_still_version`` is called, the version
must (1) exist on disk as outputs/<shot>/still_v<N>.<ext> (and its _end frame when the shot declares
one), (2) be held by a still child that is still waiting at G2 with a landed, non-rejected still, and
(3) pass the same preflight a still submit passes: the precheck rules, the prompt lint kit and the
moderation hard rules on that version's prompt, with every planned ref on disk. No spend: the kept
version's existing still is animated after G2; nothing is regenerated.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

STILL_EXTS = (".jpg", ".jpeg", ".png", ".webp")
PRECHECK_BANNED = ("nsfw", "explicit", "gore")  # same rules as activities.qc.precheck_still_qc


def outputs_dir(episode_id: str, shot_id: str, data_dir: str | os.PathLike | None = None) -> Path:
    root = Path(data_dir if data_dir is not None else os.getenv("DATA_DIR", "./data"))
    return root / "episodes" / episode_id / "outputs" / shot_id


def still_file(out_dir: Path, version: int, end: bool = False) -> Path | None:
    """outputs/<shot>/still_v<N>[_end].<ext> (non-empty) or None."""
    stem = f"still_v{version}{'_end' if end else ''}"
    for ext in STILL_EXTS:
        p = out_dir / f"{stem}{ext}"
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def precheck_problems(prompt: str | None) -> list[str]:
    issues = []
    if not prompt or len(prompt) < 10:
        issues.append("Prompt too short")
    if prompt and any(w in prompt.lower() for w in PRECHECK_BANNED):
        issues.append("Prompt contains banned content")
    return issues


def lint_problems(episode_id: str, shot_id: str, prompt: str, shot_meta: dict | None, refs: list,
                  live: bool, kind: str = "still") -> list[str]:
    """The SAME prompt gate a paid still submit runs (enforce_prompt_lint: lint kit, fail closed in live
    without a kit or shot_meta) plus the moderation hard rules. Dry mode only warns, like the submit."""
    from hfvg import moderation_lint
    from hfvg.prompt_lint import PromptLintError, enforce_prompt_lint
    try:
        problems = [f"lint: {p}" for p in enforce_prompt_lint(episode_id, prompt, shot_meta, live, kind=kind)]
    except PromptLintError as e:
        problems = [f"lint: {e}"]
    if shot_meta is not None:
        rec = moderation_lint.evaluate(shot_id, prompt, shot_meta, kind=kind, model=None, refs=refs, live=False)
        problems += [f"moderation: {v}" for v in rec.get("hard_violations", [])]
    return problems if live else []


def selection_problems(*, episode_id: str, shot_id: str, version: int, plan: dict | None,
                       child_state: dict | None, live: bool,
                       data_dir: str | os.PathLike | None = None) -> list[str]:
    """Every reason still v<version> of shot_id cannot be kept at G2 (empty list = OK)."""
    problems: list[str] = []
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return [f"version must be a positive integer, got {version!r}"]
    if not plan:
        return [f"no still v{version} for {shot_id} in this episode"]
    status = plan.get("status", "running")
    if status != "running":
        problems.append(f"v{version} ({plan.get('workflow_id')}) already ended ({status}); nothing waits at G2")
    params = plan.get("params") or {}
    out = outputs_dir(episode_id, shot_id, data_dir)
    if still_file(out, version) is None:
        problems.append(f"still v{version} not on disk: {out}/still_v{version}.(jpg|png|webp)")
    if params.get("end_frame") and still_file(out, version, end=True) is None:
        problems.append(f"end frame of v{version} not on disk: {out}/still_v{version}_end.(jpg|png|webp)")
    cs = child_state
    if cs is None:
        problems.append(f"child {plan.get('workflow_id')} not queryable")
    else:
        if cs.get("version") not in (None, version):
            problems.append(f"child {plan.get('workflow_id')} holds v{cs.get('version')}, not v{version}")
        if cs.get("still_status") != "done":
            problems.append(f"v{version} has no landed still (still_status={cs.get('still_status')!r})")
        if not cs.get("still_url"):
            problems.append(f"v{version} has no still URL")
        if cs.get("still_rejected"):
            problems.append(f"v{version} was rejected: {cs.get('still_rejected')}")
        if cs.get("still_superseded"):
            problems.append(f"v{version} was already superseded: {cs.get('still_superseded')}")
        if params.get("end_frame") and not cs.get("end_still_url"):
            problems.append(f"v{version} declares an end frame but its _end still did not land "
                            f"({cs.get('end_still_error') or cs.get('end_still_status')})")
    prompt = plan.get("prompt") or ""
    problems += [f"precheck: {p}" for p in precheck_problems(prompt)]
    missing = list(plan.get("missing_refs") or [])
    if missing:
        problems.append(f"planned refs not on disk: {missing}")
    refs = list(plan.get("refs") or [])
    for r in refs:
        if isinstance(r, str) and r and not r.startswith(("http://", "https://")) and not Path(r).exists():
            problems.append(f"ref not on disk: {r}")
    problems += lint_problems(episode_id, shot_id, prompt, plan.get("shot_meta"), refs, live)
    return problems


async def check_selection(client: Any, episode_id: str, workflow_id: str, shot_id: str, version: int,
                          live: bool, data_dir: str | os.PathLike | None = None) -> tuple[dict | None, list[str]]:
    """Query the episode + the version's child, return (plan, problems)."""
    ep = client.get_workflow_handle(workflow_id)
    plan = await ep.query("still_version_plan", args=[shot_id, version])
    child_state = None
    if plan and plan.get("workflow_id"):
        try:
            child_state = await client.get_workflow_handle(plan["workflow_id"]).query("get_state")
        except Exception:
            child_state = None
    return plan, selection_problems(episode_id=episode_id, shot_id=shot_id, version=version, plan=plan,
                                    child_state=child_state, live=live, data_dir=data_dir)
