"""GAP-37 (ep05v2 G3 round 2, V19 recompose 1, 2026-10-27): a recompose still's attempt is its
recompose VERSION, so its job id came out as -r{attempt-1} = still-ep05v2-V19-r2, the id of the
shot's committed G2 retry. The new paid request was submitted, provider_jobs kept the OLD mapping
(INSERT OR IGNORE) and the poll re-saved the old still as still_v3. Now:
  - a recompose still gets its own -rc{n} id (never an -r id);
  - no still or clip is submitted under a job id whose ledger row is already committed.
No network, no spend."""
import aiosqlite
import pytest
import respx
from temporalio.exceptions import ApplicationError

from tests.lint_helpers import lint_meta, lint_ok
from tests.test_moderation_recovery import _live_episode, _live_routes
from tests.test_recompose_round import _routes, _submit


async def _episode(tmp_path, monkeypatch):
    from tests.ledger_helpers import insert_line
    db_path, ledger, credits = await _live_episode(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "5")
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L2_drafts", 2_000_000, 1_600_000)
        await db.commit()
    ref = tmp_path / "character.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return db_path, ledger, str(ref)


async def _row(db_path, job_id, status, model="kling", line="L4_video"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO job_ledger (job_id, episode_id, shot_id, model, tier, line_name, usd_micros, status,"
            " created_at, updated_at) VALUES (?, 'ep99', 'A01', ?, 'pro', ?, 60000, ?, '2026-10-27', '2026-10-27')",
            (job_id, model, line, status))
        await db.commit()


def _paid_posts(post):
    return [c for c in post.calls if "estimate" not in str(c.request.url) and "upload" not in str(c.request.url)]


@pytest.mark.asyncio
async def test_recompose_still_gets_rc_id_not_a_g2_retry_id(tmp_path, monkeypatch):
    db_path, ledger, ref = await _episode(tmp_path, monkeypatch)
    await _row(db_path, "still-ep99-A01-r2", "committed", model="nano_banana_pro", line="L2_drafts")  # the G2 retry
    with respx.mock(assert_all_called=False) as router:
        _routes(router)
        result = await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc1"}, attempt=3)
    assert result["job_id"] == "still-ep99-A01-rc1", result


@pytest.mark.asyncio
async def test_still_refused_under_a_committed_job_id(tmp_path, monkeypatch):
    db_path, ledger, ref = await _episode(tmp_path, monkeypatch)
    await _row(db_path, "still-ep99-A01-r2", "committed", model="nano_banana_pro", line="L2_drafts")
    with respx.mock(assert_all_called=False) as router:
        post = _routes(router)
        with pytest.raises(ApplicationError) as e:
            await _submit(ref, None, attempt=3)               # a G2 re-roll -> still-ep99-A01-r2
        submitted = _paid_posts(post)
    assert e.value.type == "JobIdAlreadyCommitted" and "still-ep99-A01-r2" in str(e.value), e.value
    assert e.value.non_retryable
    assert submitted == []                                     # nothing paid twice


@pytest.mark.asyncio
async def test_clip_refused_under_a_committed_job_id(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    db_path, ledger, _ = await _live_episode(tmp_path, monkeypatch)
    await _row(db_path, "clip-ep99-A01-v1", "committed")
    with respx.mock(assert_all_called=False) as router:
        submit = _live_routes(router)
        with pytest.raises(ApplicationError) as e:
            await submit_clip_job_enforced("ep99", "A01", "https://cdn.example.com/start.png",
                                           lint_ok("Duck walks"), 5.0, 1, shot_meta=lint_meta())
        called = submit.called
    assert e.value.type == "JobIdAlreadyCommitted", e.value
    assert not called


@pytest.mark.asyncio
async def test_reserved_in_flight_id_is_not_refused(tmp_path, monkeypatch):
    """An activity retry of an in-flight (reserved) job keeps the existing resume behaviour."""
    from hfvg.activities.studio_generation import refuse_committed_job_id
    db_path, ledger, _ = await _episode(tmp_path, monkeypatch)
    await _row(db_path, "clip-ep99-A01-v1", "reserved")
    await refuse_committed_job_id(db_path, "clip-ep99-A01-v1")      # no raise
    await refuse_committed_job_id(str(tmp_path / "empty.db"), "x")  # no ledger table: no raise


def test_job_id_collision_is_a_pre_spend_refusal():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    assert EpisodeWorkflowV2.refused_before_spend(
        {"status": "failed", "error": "JobIdAlreadyCommitted: job id still-x-r2 is already committed"})
