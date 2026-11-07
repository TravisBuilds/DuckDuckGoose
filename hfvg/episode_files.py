"""Check an episode's private files together, before anything is saved (POST /api/episodes/upload/validate).

The checks the upload routes, the lint and preflight routes and the live submit would run, in one pass:
parse_shots, the beatmap and shots.json agreeing, the refs.json keys, the routing rules, the prompt lint
(continuity locks injected first, as the submit does) and preflight. Writes nothing; the only thing read
from disk is whether a ref path exists.
"""
import json
from pathlib import Path
from typing import Any

from hfvg.preflight import preflight_plans
from hfvg.prompt_lint import PromptLintError, kit_from_prompt_kit, lint_plans
from hfvg.routing import RoutingError, plan_route
from hfvg.shot_data import ShotDataError, parse_shots


def _json(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ShotDataError(f"{name} is not valid JSON: {e}") from None


def check_episode_files(beatmap_shots: list[dict[str, Any]], shots_raw: bytes, refs_raw: bytes | None = None,
                        kit_raw: bytes | None = None) -> dict[str, Any]:
    """{"errors", "warnings", "shots": {shot_id: {"errors", "warnings"}}, "shot_list"}. Errors are what an
    upload, a G1 approval or a live submit would refuse; ``shot_list`` is shots.json's list, for estimates.
    ``beatmap_shots`` is parse_beatmap output (empty when the beatmap did not parse)."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = _json(shots_raw, "shots.json")
        plans = parse_shots(data)
    except ShotDataError as e:
        return {"errors": [f"shots.json: {e}"], "warnings": [], "shots": {}, "shot_list": []}
    per_shot: dict[str, dict[str, list[str]]] = {sid: {"errors": [], "warnings": []} for sid in plans}

    def shot_problem(sid: str, text: str, severity: str = "errors") -> None:
        per_shot[sid][severity].append(text)
        (errors if severity == "errors" else warnings).append(f"{sid}: {text}")

    rows = {s["shot_id"]: s for s in beatmap_shots}
    if rows and set(rows) != set(plans):
        errors.append(f"BEATMAP.md and shots.json list different shots: only in the beatmap "
                      f"{sorted(set(rows) - set(plans))}, only in shots.json {sorted(set(plans) - set(rows))}")
    for sid, plan in plans.items():
        row, model = rows.get(sid), plan["params"]["model"]
        if row and row["model"] != ("S" if model == "seedance" else "K"):
            shot_problem(sid, f"the beatmap says model {row['model']}, shots.json says {model}")

    if refs_raw is None:
        warnings.append("No refs.json: the ref keys were not checked (a live run needs it).")
    else:
        try:
            refs = _json(refs_raw, "refs.json")
        except ShotDataError as e:
            refs = None
            errors.append(str(e))
        listed_by_shot = refs.get("per_shot") if isinstance(refs, dict) else None
        if refs is not None and not isinstance(listed_by_shot, dict):
            errors.append("refs.json must contain a 'per_shot' object")
        for sid, plan in plans.items() if isinstance(listed_by_shot, dict) else ():
            entries = listed_by_shot.get(sid)
            if not isinstance(entries, list):
                shot_problem(sid, "refs.json has no ref list for this shot")
                continue
            listed = [e.get("key") for e in entries if isinstance(e, dict)]
            if listed != plan["ref_keys"]:
                shot_problem(sid, f"refs.json lists {listed}, but shots.json ref_keys are {plan['ref_keys']}")
            for entry in (e for e in entries if isinstance(e, dict)):
                path = str(entry.get("path") or "")
                if path and not entry.get("url") and not Path(path).is_file():
                    shot_problem(sid, f"ref {entry.get('key')} is not on the server yet: {path}", "warnings")

    for sid, plan in plans.items():
        # The workflow routes on the beatmap's text tags and shots.json's together (shot_data merges them).
        tags = sorted(set(plan["tags"]) | set((rows.get(sid) or {}).get("tags") or []))
        try:
            plan_route({"model": plan["params"]["model"], "tier": "final", "duration": plan["params"]["duration"],
                        "count": plan["shot_meta"]["count"], "tags": tags, "shot_id": sid,
                        "seedance_resolution": plan["params"]["seedance_resolution"]})
        except RoutingError as e:
            shot_problem(sid, f"routing: {e}")

    if kit_raw is None:
        warnings.append("No prompt_kit.json: the prompts were not linted (a live run needs it).")
    else:
        try:
            kit_data = _json(kit_raw, "prompt_kit.json")
            missing = [k for k in ("style", "characters") if not isinstance(kit_data, dict) or k not in kit_data]
            if missing:
                errors.append(f"prompt_kit.json must contain {', '.join(missing)}")
            kit = kit_from_prompt_kit(kit_data)
        except (ShotDataError, PromptLintError) as e:
            errors.append(f"prompt_kit.json: {e}")
        else:
            for sid, problems in lint_plans(plans, kit).items():
                for problem in problems:
                    shot_problem(sid, f"lint {problem}")

    report = preflight_plans(data["shots"])
    for sid, entry in report["shots"].items():
        for text in entry["errors"]:
            shot_problem(sid, f"preflight: {text}")
        for text in entry["warnings"]:
            shot_problem(sid, f"preflight: {text}", "warnings")
    return {"errors": errors, "warnings": warnings, "shots": per_shot, "shot_list": data["shots"]}
