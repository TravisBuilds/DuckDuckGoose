"""Automated QC judge (a), the still reviewer (hfvg/qc/still_review.py, docs/QC-JUDGES.md), and its
place in ``review_still``: a live still against its prompt and references, then the identity of
every locked character. PASS passes, FAIL is a re-roll (escalate False: the shot fails and only
``retry_shot`` makes another still), anything the judge could not decide escalates to a person.
With no reviewer registered (the flag off) ``review_still`` is exactly as before, whatever the
rubric. FakeJudgeClient only: no network, no spend."""

from __future__ import annotations

import io

import httpx
import pytest
import respx

from hfvg.activities.qc import review_still
from hfvg.qc.identity import VisionCharacterDetector, VisionIdentityJudge, set_identity_judge
from hfvg.qc.judges import FakeJudgeClient, JudgeService
from hfvg.qc.still_review import (
    MAX_REFS,
    StillReviewer,
    VisionStillReviewer,
    judged_still_review,
    set_still_reviewer,
)
from tests.qc_judge_helpers import (
    STILL_FAIL,
    STILL_PASS,
    detect_answer,
    identity_answer,
    image,
    live_episode,
    locked_character,
    qc_line,
    reset_registries,
    routed_client,
)
from tests.test_picture_assembly_round3 import HB

EP = "ep-qcstill"
PROMPT = "A teal robot waves from the end of a wooden pier at dawn."
URL = "https://cdn.example.com/stills/A01_v1.png"
UNCHANGED_LIVE = {"passed": False, "issues": ["Real QC agent not implemented - escalate to human review"],
                  "escalate": True}


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_episode(tmp_path, monkeypatch, EP)
    yield path
    reset_registries()


def _still(tmp_path, seed=0):
    return image(tmp_path / "outputs" / "A01" / f"still_v{seed + 1}.png", seed=seed)


def _refs(tmp_path, n=2):
    return [str(image(tmp_path / "refs" / f"ref{i}.png", color=(200, 60 + i * 20, 60), seed=10 + i)) for i in range(n)]


def _rubric(tmp_path, **kw):
    rubric = {"prompt": PROMPT, "refs": _refs(tmp_path), "shot_meta": {"count": 1, "characters": ["R"]},
              "still_path": str(_still(tmp_path))}
    rubric.update(kw)
    return rubric


async def _judged(client, rubric, episode_id=EP):
    reviewer = VisionStillReviewer(JudgeService(client))
    return await judged_still_review(reviewer, URL, rubric, episode_id, "A01")


def _decoded_size(jpeg: bytes) -> tuple[int, int]:
    from PIL import Image
    with Image.open(io.BytesIO(jpeg)) as im:
        return im.size


# ------------------------------------------------------------------------------------------ verdicts


async def test_a_still_that_matches_passes(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path))
    assert (out["passed"], out["issues"], out["escalate"]) == (True, [], False)
    assert out["qc_judges"]["status"] == "PASS" and out["qc_judges"]["still"]["confidence"] == 0.93
    assert len(client.calls) == 1


async def test_a_fail_is_a_reroll_with_the_judges_reasons(db, tmp_path):
    out = await _judged(FakeJudgeClient(STILL_FAIL), _rubric(tmp_path))
    assert out["passed"] is False and out["escalate"] is False
    assert out["issues"] == ["QC judge: two robots in frame, the prompt has one"]
    assert out["qc_judges"]["status"] == "FAIL"


@pytest.mark.parametrize("answer, reason", [
    ({**STILL_FAIL, "confidence": 0.5}, "unsure (confidence 0.50 < 0.7)"),
    ({**STILL_PASS, "confidence": 0.69}, "unsure (confidence 0.69 < 0.7)"),
    ({"verdict": "unsure", "confidence": 0.95, "issues": ["the robot is too small to judge"]}, "could not decide"),
], ids=["low-confidence-fail", "low-confidence-pass", "unsure"])
async def test_low_confidence_or_unsure_needs_review(db, tmp_path, answer, reason):
    out = await _judged(FakeJudgeClient(answer), _rubric(tmp_path))
    assert out["passed"] is False and out["escalate"] is True
    assert out["issues"][0].startswith("QC judge needs review:") and reason in out["issues"][0], out
    assert out["qc_judges"]["status"] == "NEEDS_REVIEW"


async def test_a_judge_error_needs_review(db, tmp_path):
    out = await _judged(FakeJudgeClient(RuntimeError("upstream broke")), _rubric(tmp_path))
    assert out["escalate"] is True and "judge error (RuntimeError)" in out["issues"][0]


async def test_a_judge_timeout_needs_review(db, tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_QC_JUDGE_TIMEOUT_S", "0.05")
    out = await _judged(FakeJudgeClient(STILL_PASS, delay=2.0), _rubric(tmp_path))
    assert out["escalate"] is True and "the judge timed out" in out["issues"][0]
    assert qc_line(db, EP)["reserved"] == 0


@pytest.mark.parametrize("answer", [
    "{not json",
    {"verdict": "maybe", "confidence": 0.9, "issues": []},
    {"verdict": "fail", "confidence": 0.9, "issues": []},
    {"verdict": "pass", "confidence": 1.5, "issues": []},
    {"verdict": "pass", "confidence": True, "issues": []},
    {"verdict": "pass", "issues": []},
    {"verdict": "fail", "confidence": 0.9, "issues": "a string, not a list"},
], ids=["not-json", "unknown-verdict", "fail-without-issues", "confidence-above-1", "confidence-bool",
        "no-confidence", "issues-not-a-list"])
async def test_a_malformed_answer_needs_review(db, tmp_path, answer):
    out = await _judged(FakeJudgeClient(answer), _rubric(tmp_path))
    assert out["passed"] is False and out["escalate"] is True
    assert "malformed answer" in out["issues"][0], out


async def test_a_budget_refusal_needs_review_without_a_call(db, tmp_path):
    from hfvg.budget import BudgetLedger

    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path))
    assert out["escalate"] is True and "budget refused the judge call" in out["issues"][0]
    assert client.calls == []


async def test_dry_run_reviewer_needs_review_without_a_call(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path))
    assert out["escalate"] is True and "dry run" in out["issues"][0] and client.calls == []


async def test_the_review_deadline_needs_review_and_releases_the_hold(db, tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_QC_REVIEW_DEADLINE_S", "0.2")
    out = await _judged(FakeJudgeClient(STILL_PASS, delay=3.0), _rubric(tmp_path))
    assert out["escalate"] is True and "the QC judges took longer than 0.2 s" in out["issues"][0], out
    assert qc_line(db, EP)["reserved"] == 0


async def test_a_crashing_reviewer_needs_review(db, tmp_path):
    class Broken(StillReviewer):
        async def review(self, still_path, rubric, episode_id, shot_id):
            raise KeyError("verdict")

    out = await judged_still_review(Broken(), URL, _rubric(tmp_path), EP, "A01")
    assert out["passed"] is False and out["escalate"] is True
    assert "the QC judges failed (KeyError" in out["issues"][0]


# ------------------------------------------------------------------------------------------ inputs


async def test_a_rubric_without_a_prompt_needs_review_without_a_call(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS)
    for rubric in ({"still_path": str(_still(tmp_path))}, _rubric(tmp_path, prompt="   ")):
        out = await _judged(client, rubric)
        assert out["escalate"] is True and "no still prompt" in out["issues"][0]
    assert client.calls == []


async def test_the_request_has_the_prompt_the_cast_clauses_and_the_references(db, tmp_path):
    cast = [{"character": "pip", "version": 1, "name": "Pip", "locked": False,
             "clause": "Pip is a small round robot with a teal shell."}]
    client = FakeJudgeClient(STILL_PASS)
    await _judged(client, _rubric(tmp_path, shot_meta={"count": 1, "cast": cast}))
    (request,) = client.calls
    assert PROMPT in request["text"] and "Pip is a small round robot with a teal shell." in request["text"]
    assert "Images 2-3 are the shot's reference images." in request["text"]
    assert len(request["images"]) == 3
    assert max(_decoded_size(request["images"][0])) <= 768
    assert all(max(_decoded_size(img)) <= 512 for img in request["images"][1:])


async def test_without_references_the_still_is_judged_against_the_prompt(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS)
    await _judged(client, _rubric(tmp_path, refs=[]))
    assert "no reference images: judge against the prompt" in client.calls[0]["text"]
    assert len(client.calls[0]["images"]) == 1


async def test_at_most_six_references_are_sent(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS)
    await _judged(client, _rubric(tmp_path, refs=_refs(tmp_path, 8)))
    assert len(client.calls[0]["images"]) == 1 + MAX_REFS == 7


async def test_the_same_still_and_inputs_are_not_paid_twice(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS, STILL_FAIL, STILL_FAIL)
    rubric = _rubric(tmp_path)
    assert (await _judged(client, rubric))["passed"] is True
    assert (await _judged(client, dict(rubric)))["passed"] is True  # e.g. a retried activity
    assert len(client.calls) == 1
    out = await _judged(client, dict(rubric, prompt=PROMPT + " Light rain."))
    assert out["passed"] is False and len(client.calls) == 2
    image(tmp_path / "refs" / "ref0.png", color=(10, 10, 10), seed=99)  # a reference changed on disk
    out = await _judged(client, rubric)
    assert out["passed"] is False and len(client.calls) == 3


async def test_a_still_that_is_not_an_image_needs_review(db, tmp_path):
    bad = tmp_path / "outputs" / "A01" / "still_v9.png"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path, still_path=str(bad)))
    assert out["escalate"] is True and "cannot read still_v9.png as an image" in out["issues"][0]
    assert client.calls == []


async def test_a_missing_reference_needs_review(db, tmp_path):
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path, refs=[str(tmp_path / "refs" / "gone.png")]))
    assert out["escalate"] is True and "reference not readable: gone.png" in out["issues"][0]
    assert client.calls == []


@respx.mock
async def test_url_references_and_a_still_only_at_its_url_are_downloaded(db, tmp_path):
    png = _still(tmp_path, seed=5).read_bytes()
    respx.get(URL).mock(return_value=httpx.Response(200, content=png))
    respx.get("https://cdn.example.com/refs/pier.png").mock(return_value=httpx.Response(200, content=png))
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path, still_path=None, refs=["https://cdn.example.com/refs/pier.png"]))
    assert out["passed"] is True and len(client.calls[0]["images"]) == 2


@respx.mock
async def test_a_failed_download_needs_review(db, tmp_path):
    respx.get(URL).mock(return_value=httpx.Response(404))
    respx.get("https://cdn.example.com/refs/pier.png").mock(return_value=httpx.Response(500))
    client = FakeJudgeClient(STILL_PASS)
    out = await _judged(client, _rubric(tmp_path, still_path=str(tmp_path / "missing.png")))
    assert out["escalate"] is True and "download failed (HTTPStatusError)" in out["issues"][0]
    out = await _judged(client, _rubric(tmp_path, refs=["https://cdn.example.com/refs/pier.png"]))
    assert out["escalate"] is True and "reference pier.png download failed" in out["issues"][0]
    assert client.calls == []


# ------------------------------------------------------------------------------------------ review_still


async def test_flag_off_live_review_is_unchanged_whatever_the_rubric(db, tmp_path):
    assert await HB().run(review_still, URL, {}, EP, "A01") == UNCHANGED_LIVE
    assert await HB().run(review_still, URL, _rubric(tmp_path), EP, "A01") == UNCHANGED_LIVE


async def test_dry_run_review_is_unchanged_with_a_reviewer_registered(db, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    client = FakeJudgeClient(STILL_PASS)
    set_still_reviewer(VisionStillReviewer(JudgeService(client)))
    assert await HB().run(review_still, URL, _rubric(tmp_path), EP, "A01") == {"passed": True, "issues": []}
    assert client.calls == []


async def test_the_episode_checks_come_before_any_judge_call(db, tmp_path):
    import aiosqlite

    client = FakeJudgeClient(STILL_PASS)
    set_still_reviewer(VisionStillReviewer(JudgeService(client)))
    out = await HB().run(review_still, URL, _rubric(tmp_path), None, "A01")
    assert out["escalate"] is True and "episode_id not provided" in out["issues"][0]
    async with aiosqlite.connect(db) as conn:
        await conn.execute("UPDATE episodes SET live_mode = 0 WHERE episode_id = ?", (EP,))
        await conn.commit()
    out = await HB().run(review_still, URL, _rubric(tmp_path), EP, "A01")
    assert out["escalate"] is True and "not in live mode" in out["issues"][0]
    assert client.calls == []


@pytest.mark.parametrize("answer, passed, escalate", [
    (STILL_PASS, True, False), (STILL_FAIL, False, False), ({**STILL_PASS, "confidence": 0.2}, False, True),
], ids=["pass", "fail", "unsure"])
async def test_review_still_returns_the_judges_verdict(db, tmp_path, answer, passed, escalate):
    set_still_reviewer(VisionStillReviewer(JudgeService(FakeJudgeClient(answer))))
    out = await HB().run(review_still, URL, _rubric(tmp_path), EP, "A01")
    assert (out["passed"], out["escalate"]) == (passed, escalate), out


# ------------------------------------------------------------------------------------------ identity


def _register(client):
    service = JudgeService(client)
    set_still_reviewer(VisionStillReviewer(service))
    set_identity_judge(VisionCharacterDetector(service), VisionIdentityJudge(service))


def _cast_rubric(tmp_path, entry):
    return _rubric(tmp_path, shot_meta={"count": 1, "cast": [entry]})


async def test_a_passed_still_is_checked_against_each_locked_character(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(still=[STILL_PASS], detect=[detect_answer(pip["character"])], identity=[identity_answer()])
    _register(client)
    out = await HB().run(review_still, URL, _cast_rubric(tmp_path, pip), EP, "A01")
    assert out["passed"] is True, out
    assert client.asked() == ["still", "detect", "identity"]
    identity = out["qc_judges"]["identity"]
    assert identity["status"] == "PASS" and identity["characters"][0]["name"] == "Pip"
    assert "Pip" in client.calls[2]["text"] and "version 1" in client.calls[2]["text"]


async def test_identity_drift_fails_the_still(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(still=[STILL_PASS], detect=[detect_answer(pip["character"])],
                           identity=[identity_answer({"colours": 2})])
    _register(client)
    out = await HB().run(review_still, URL, _cast_rubric(tmp_path, pip), EP, "A01")
    assert out["passed"] is False and out["escalate"] is False
    assert out["issues"][0].startswith("identity: Pip: Drift detected: colours (score 2)"), out


async def test_an_unsure_identity_check_needs_review(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(still=[STILL_PASS], detect=[detect_answer(pip["character"], confidence=0.4)])
    _register(client)
    out = await HB().run(review_still, URL, _cast_rubric(tmp_path, pip), EP, "A01")
    assert out["passed"] is False and out["escalate"] is True
    assert out["issues"][0] == "identity needs review: Pip: detector unsure (confidence 0.40 < 0.6)", out


async def test_a_failed_still_is_not_checked_for_identity(db, tmp_path):
    pip = locked_character(EP)
    client = routed_client(still=[STILL_FAIL])
    _register(client)
    out = await HB().run(review_still, URL, _cast_rubric(tmp_path, pip), EP, "A01")
    assert out["escalate"] is False and client.asked() == ["still"]


async def test_a_lock_that_changed_on_disk_needs_review(db, tmp_path):
    from hfvg import cast_store

    pip = locked_character(EP)
    character = cast_store.find_character(("episode", EP), pip["character"])
    master = next(i for i in character["versions"][0]["images"] if i["role"] == cast_store.MASTER_ROLE)
    cast_store.image_file(master["sha256"], master["ext"]).write_bytes(b"swapped")
    client = routed_client(still=[STILL_PASS])
    _register(client)
    out = await HB().run(review_still, URL, _cast_rubric(tmp_path, pip), EP, "A01")
    assert out["passed"] is False and out["escalate"] is True
    assert "identity needs review" in out["issues"][0] and client.asked() == ["still"]
