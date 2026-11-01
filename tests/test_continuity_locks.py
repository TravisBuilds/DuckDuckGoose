"""GAP-40: prompt-kit continuity locks are injected by the harness into every matching paid prompt
(still and clip) and the lint requires them; plus the duck style-split check (no realism word on the
duck). Synthetic kit text only (public repo)."""

import json

import httpx
import pytest
import respx

from hfvg.prompt_lint import (
    LintKit,
    PromptLintError,
    apply_continuity_locks,
    clip_lint_violations,
    inject_continuity_locks,
    lint_violations,
    load_lint_kit,
)
from tests.lint_helpers import SYNTHETIC_LINT_KIT, capture, lint_meta, lint_ok

DUCK_LOCK = ("SYNTH DUCK LOCK: match attached refs exactly. Plush toy duck, tiny tail nub at most; NO tail feathers. "
             "Refs: duck body/face/apparel only; ignore ref backgrounds.")
DUCK_CLIP = "Duck tail stays a tiny plush nub; vest and beanie never change; no morphing."
SCALE_LOCK = "SYNTH SCALE: real size, back below the reception counter; exactly the stated count."
LOCKS = [
    {"id": "duck_identity", "when": "duck", "kinds": ["still", "clip"], "text": DUCK_LOCK},
    {"id": "duck_clip", "when": "duck", "kinds": ["clip"], "text": DUCK_CLIP},
    {"id": "animal_scale", "when": "animals", "kinds": ["still", "clip"], "text": SCALE_LOCK},
]


def _kit(**over) -> LintKit:
    data = dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS, animal_characters=["Otter1"])
    data.update(over)
    return LintKit.from_dict(data)


def _duck_meta(count=0, **kw):
    return lint_meta(count=count, characters=["Duck"], **kw)


def _clause(prompt: str) -> str:  # the synthetic kit's duck_clause rule (unrelated to the locks)
    return f"{prompt} {SYNTHETIC_LINT_KIT['duck_clause']}."


def test_inject_adds_applicable_locks_once_and_respects_kind():
    kit = _kit()
    still, added = inject_continuity_locks(lint_ok("Scene", 0), _duck_meta(), kit, "still")
    assert added == ["duck_identity"]
    assert DUCK_LOCK in still and DUCK_CLIP not in still and SCALE_LOCK not in still
    clip, added = inject_continuity_locks(lint_ok("Scene", 0), _duck_meta(), kit, "clip")
    assert added == ["duck_identity", "duck_clip"]
    again, added2 = inject_continuity_locks(clip, _duck_meta(), kit, "clip")
    assert again == clip and added2 == []  # idempotent (no double lock on a retry/reword)


def test_animal_lock_follows_count_or_characters():
    kit = _kit()
    _, added = inject_continuity_locks("x", lint_meta(count=3), kit, "still")
    assert added == ["animal_scale"]
    _, added = inject_continuity_locks("x", lint_meta(count=0), kit, "still")
    assert added == []
    _, added = inject_continuity_locks("x", lint_meta(count=0, characters=["Otter1"]), kit, "clip")
    assert added == ["animal_scale"]
    _, added = inject_continuity_locks("x", _duck_meta(count=2), kit, "still")
    assert added == ["duck_identity", "animal_scale"]


def test_lint_requires_every_applicable_lock():
    kit = _kit()
    meta = _duck_meta(count=1)
    bare = _clause(lint_ok("Scene", 1))
    problems = lint_violations(bare, meta, kit)
    assert any("duck_identity" in p for p in problems)
    assert any("animal_scale" in p for p in problems)
    fixed, _ = inject_continuity_locks(bare, meta, kit, "still")
    assert lint_violations(fixed, meta, kit) == []
    # the still prompt lacks the clip-only line: the clip lint refuses it
    assert any("duck_clip" in p for p in clip_lint_violations(fixed, meta, kit))
    clip, _ = inject_continuity_locks(fixed, meta, kit, "clip")
    assert clip_lint_violations(clip, meta, kit) == []


def test_kit_without_locks_is_unchanged():
    kit = LintKit.from_dict(dict(SYNTHETIC_LINT_KIT))
    p = _clause(lint_ok("Scene", 1))
    assert inject_continuity_locks(p, _duck_meta(1), kit, "clip") == (p, [])
    assert lint_violations(p, _duck_meta(1), kit) == []


@pytest.mark.parametrize("bad", [
    {"id": "a", "when": "sometimes", "text": "t"},
    {"id": "a", "when": "duck", "text": ""},
    {"id": "", "when": "duck", "text": "t"},
    {"id": "a", "when": "duck", "kinds": ["audio"], "text": "t"},
])
def test_bad_lock_spec_refused(bad):
    err = capture(PromptLintError, LintKit.from_dict, dict(SYNTHETIC_LINT_KIT, continuity_locks=[bad]))
    assert err is not None


def test_duplicate_lock_id_refused():
    two = [{"id": "a", "when": "duck", "text": "t1"}, {"id": "a", "when": "duck", "text": "t2"}]
    assert capture(PromptLintError, LintKit.from_dict, dict(SYNTHETIC_LINT_KIT, continuity_locks=two))


@pytest.mark.parametrize("sentence", [
    "A realistic duck stands on the deck",
    "The duck, photoreal feathers, waits",
    "Photorealistic duck host by the door",
    "A lifelike duck at the desk",
])
def test_realism_word_on_the_duck_refused(sentence):
    kit = _kit(continuity_locks=[])
    problems = lint_violations(_clause(lint_ok(sentence, 0)), _duck_meta(), kit)
    assert any("realism word" in p for p in problems), problems


def test_realism_elsewhere_is_fine_and_non_duck_shots_ignored():
    kit = _kit(continuity_locks=[])
    ok = _clause(lint_ok("Photoreal otters soak in the pool. The duck is a soft plush toy on the deck", 2))
    assert not any("realism word" in p for p in lint_violations(ok, _duck_meta(2), kit))
    no_duck = lint_ok("A realistic duck decoy is not in this shot", 0)
    assert not any("realism word" in p for p in lint_violations(no_duck, lint_meta(0), kit))
    # word boundaries: "unrealistically" / "surrealist" do not count
    edge = _clause(lint_ok("The duck looks surrealist and unrealistically plush", 0))
    assert not any("realism word" in p for p in lint_violations(edge, _duck_meta(), kit))


def test_full_kit_top_level_locks_are_loaded(tmp_path, monkeypatch):
    full = tmp_path / "prompt_kit.json"
    full.write_text(json.dumps({"style": "s", "lint": dict(SYNTHETIC_LINT_KIT), "continuity_locks": LOCKS}))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(full))
    kit = load_lint_kit("ep98")
    assert [dict(item)["id"] for item in kit.continuity_locks] == ["duck_identity", "duck_clip", "animal_scale"]
    out, added = apply_continuity_locks("ep98", "x", _duck_meta(), live=True, kind="clip")
    assert added == ["duck_identity", "duck_clip"] and out.endswith(DUCK_CLIP)


def test_apply_live_without_kit_raises_dry_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(tmp_path / "missing.json"))
    assert capture(PromptLintError, apply_continuity_locks, "ep98", "x", _duck_meta(), True, "still")
    assert apply_continuity_locks("ep98", "x", _duck_meta(), False, "still") == ("x", [])


def _kit_env(tmp_path, monkeypatch):
    path = tmp_path / "kit_locks.json"
    path.write_text(json.dumps(dict(SYNTHETIC_LINT_KIT, continuity_locks=LOCKS)))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(path))


@pytest.mark.asyncio
@respx.mock
async def test_live_clip_submit_sends_the_injected_locks(tmp_path, monkeypatch):
    """The provider receives the prompt WITH the locks although the caller passed it without."""
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from tests.test_seedance_routing import _body, _live_env

    await _live_env(tmp_path, monkeypatch)
    _kit_env(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "k-live-locks"}))
    prompt = _clause(lint_ok("Synthetic clip", 1))
    await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/still.jpg", prompt, 5.0, 1, "final",
        _duck_meta(count=1), "kling", [],
    )
    sent = _body(sub)["prompt"]
    assert sent.startswith(prompt.rstrip())
    for text in (DUCK_LOCK, DUCK_CLIP, SCALE_LOCK):
        assert text in sent


@pytest.mark.asyncio
@respx.mock
async def test_live_clip_realism_on_duck_refused_before_provider(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from tests.test_seedance_routing import _live_env

    await _live_env(tmp_path, monkeypatch)
    _kit_env(tmp_path, monkeypatch)
    est = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "k-x"}))
    with pytest.raises(PromptLintError) as e:
        await submit_clip_job_enforced(
            "ep99", "A01", "https://example.com/still.jpg", _clause(lint_ok("A photoreal duck waits", 1)),
            5.0, 1, "final", _duck_meta(count=1), "kling", [],
        )
    assert "realism word" in str(e.value)
    assert not sub.called and not est.called


def test_short_locks_injected_and_realism_still_refused(tmp_path, monkeypatch):
    """r7: the shorter Studio locks are still injected verbatim, and the injected lock text itself
    (which names the duck) never trips the realism check, while a realism word on the duck still refuses."""
    full = tmp_path / "prompt_kit.json"
    full.write_text(json.dumps({"style": "s", "lint": dict(SYNTHETIC_LINT_KIT), "continuity_locks": LOCKS}))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(full))
    out, added = apply_continuity_locks("ep98", lint_ok("The duck waddles on the deck"), _duck_meta(),
                                        live=True, kind="clip")
    assert added == ["duck_identity", "duck_clip"] and DUCK_LOCK in out and out.endswith(DUCK_CLIP)
    kit = load_lint_kit("ep98")
    assert not any("realism word" in p for p in clip_lint_violations(out, _duck_meta(), kit))
    for word in ("photoreal", "realistic"):
        bad, _ = apply_continuity_locks("ep98", lint_ok(f"The {word} duck waddles on the deck"), _duck_meta(),
                                        live=True, kind="still")
        assert DUCK_LOCK in bad
        assert any("realism word" in p for p in lint_violations(bad, _duck_meta(), kit))
