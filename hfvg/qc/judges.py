"""Automated QC judges: the paid vision-model call behind every judge verdict (docs/QC-JUDGES.md).

Off unless ``HFVG_QC_JUDGES_ENABLED`` is true. With the flag off nothing is registered
(``register_qc_judges`` does nothing), so the still reviewer, the counter, the clip checker and the
identity gate are absent exactly as before and every live check escalates to a person.

Every call goes through ``JudgeService.ask``:

1. ``DRY_RUN`` makes no call (the caller gets NEEDS_REVIEW, never PASS).
2. A cached answer for the same inputs (content hashes of the media and references plus every text
   input, the model id and ``JUDGE_VERSION``) is returned without spending: page loads, G2
   approvals and retried activities never pay twice. A failed call is remembered for
   ``HFVG_QC_JUDGE_RETRY_AFTER_S`` so a broken judge is not paid again on every page load.
3. Balance gate (``BalanceMonitor.check_preflight_balance``) for the model's provider before each
   paid call.
4. Reserve the estimate (integer micro-dollars, an upper bound) on the episode's ``qc_judges`` line
   (the model's provider, created at the episode's first judge call with ``HFVG_QC_LINE_CAP_USD``,
   or sized from the episode's shot plan when that is ``auto``, hfvg.qc.judge_budget): the line's
   own 80% stop, the episode cap and stop and the GC.01 headroom all apply.
5. The model call, bounded by ``HFVG_QC_JUDGE_TIMEOUT_S`` and heartbeating inside an activity.
6. Commit the real cost from the response's token usage (the estimate when usage is missing), or
   release the hold when no answer came back.

Any refusal or failure raises ``JudgeUnavailable`` (``JudgeBudgetExhausted`` when the judge budget
is used up); callers turn it into NEEDS_REVIEW. The model comes from ``HFVG_QC_JUDGE_MODEL``
(``provider/model`` or a bare OpenAI model id), is priced by ``HFVG_QC_JUDGE_USD_PER_MTOK_*`` (both
required for any model but the default) and never appears in a verdict, an issue or a ledger reason.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import uuid
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import aiosqlite

from hfvg import subproc
from hfvg.pricing import micros_to_usd_str, usd_to_micros

logger = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"

FLAG_ENV = "HFVG_QC_JUDGES_ENABLED"
MODEL_ENV = "HFVG_QC_JUDGE_MODEL"
DEFAULT_MODEL = "gpt-4.1-mini"
TIMEOUT_ENV = "HFVG_QC_JUDGE_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 60.0
RETRY_AFTER_ENV = "HFVG_QC_JUDGE_RETRY_AFTER_S"
DEFAULT_RETRY_AFTER_S = 300
LINE_CAP_ENV = "HFVG_QC_LINE_CAP_USD"
# $44 Higgsfield + $3.50 ElevenLabs default line caps + $0.50 = the default $48 episode stop
DEFAULT_LINE_CAP_USD = "0.50"
LINE_CAP_AUTO = "auto"  # size each new line from the episode's shot plan (hfvg.qc.judge_budget)
PRICE_IN_ENV = "HFVG_QC_JUDGE_USD_PER_MTOK_IN"
PRICE_OUT_ENV = "HFVG_QC_JUDGE_USD_PER_MTOK_OUT"
DEFAULT_PRICE_IN_USD = "0.40"
DEFAULT_PRICE_OUT_USD = "1.60"
IMAGE_TOKENS_ENV = "HFVG_QC_JUDGE_IMAGE_TOKENS"
DEFAULT_IMAGE_TOKENS = 1600  # reservation upper bound per image (every image sent is <= 1536 px)
MAX_OUTPUT_TOKENS_ENV = "HFVG_QC_JUDGE_MAX_OUTPUT_TOKENS"
DEFAULT_MAX_OUTPUT_TOKENS = 600
TEMPERATURE_ENV = "HFVG_QC_JUDGE_TEMPERATURE"
DEADLINE_ENV = "HFVG_QC_REVIEW_DEADLINE_S"
# All judge calls of one still or clip review; review_still / review_clip have 5 min to finish
DEFAULT_DEADLINE_S = 200.0

QC_LINE = "qc_judges"
DEFAULT_PROVIDER = "openai"  # the provider of a bare model id; the only one with a judge client
JUDGE_VERSION = "2"  # bump when a judge's instructions change: cached verdicts are not reused

MEDIA_PX = 768   # longest side of a judged still or frame
REF_PX = 512     # longest side of a reference image
TEXT_CHARS_PER_TOKEN = 3
TEXT_OVERHEAD_TOKENS = 200

_TRUE = ("1", "true", "yes", "on")


class JudgeUnavailable(Exception):
    """No verdict from the judge (dry run, no key, refused by the budget or the balance gate,
    timeout, error, malformed answer). The caller records NEEDS_REVIEW with this reason."""


class JudgeInputError(JudgeUnavailable):
    """The media or a reference could not be read or prepared (missing file, not an image)."""


class JudgeBudgetExhausted(JudgeUnavailable):
    """The budget refused the call because the episode's judge line, or the episode itself, reached
    its stop or cap. The check goes to a person and its reason says the judge budget is used up."""


# ------------------------------------------------------------------------------------------ settings


def judges_enabled() -> bool:
    return os.getenv(FLAG_ENV, "false").strip().lower() in _TRUE


def judge_model() -> str:
    return os.getenv(MODEL_ENV, "").strip() or DEFAULT_MODEL


def model_parts(setting: str | None = None) -> tuple[str, str]:
    """(provider, model id) of a model setting: ``provider/model``, or a bare model id (OpenAI)."""
    raw = (judge_model() if setting is None else setting).strip()
    provider, sep, model_id = raw.partition("/")
    if not sep:
        return DEFAULT_PROVIDER, raw
    return provider.strip().lower(), model_id.strip()


def judge_provider(setting: str | None = None) -> str:
    """The provider the judge model bills: its budget line and balance gate."""
    return model_parts(setting)[0]


def dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").strip().lower() in ("true", "1")


def db_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/studio.db")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= minimum:
        raise ValueError(f"{name} must be greater than {minimum}, got {raw!r}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


def judge_timeout_s() -> float:
    return _env_float(TIMEOUT_ENV, DEFAULT_TIMEOUT_S)


def retry_after_s() -> int:
    try:
        return _env_int(RETRY_AFTER_ENV, DEFAULT_RETRY_AFTER_S)
    except ValueError:
        return DEFAULT_RETRY_AFTER_S


def review_deadline_s() -> float:
    """Time for every judge call of one still or clip review (an invalid value: the default)."""
    try:
        return _env_float(DEADLINE_ENV, DEFAULT_DEADLINE_S)
    except ValueError:
        return DEFAULT_DEADLINE_S


def max_output_tokens() -> int:
    return _env_int(MAX_OUTPUT_TOKENS_ENV, DEFAULT_MAX_OUTPUT_TOKENS)


def qc_line_cap_setting() -> int | str:
    """HFVG_QC_LINE_CAP_USD: a cap in micro-dollars, or ``auto`` (ValueError when invalid)."""
    raw = os.getenv(LINE_CAP_ENV, "").strip()
    if raw.lower() == LINE_CAP_AUTO:
        return LINE_CAP_AUTO
    cap = usd_to_micros(raw or DEFAULT_LINE_CAP_USD)
    if cap <= 0:
        raise ValueError(f"{LINE_CAP_ENV} must be more than 0")
    return cap


def _price_micros_per_mtok(env: str, default: str) -> int:
    price = usd_to_micros(os.getenv(env, "").strip() or default)
    if price <= 0:
        raise ValueError(f"{env} must be more than 0")
    return price


def judge_prices(model: str | None = None) -> tuple[int, int]:
    """Micro-dollars per million input and output tokens of the judge model. The defaults price the
    default model only: any other model needs both price settings, or no call is made (ValueError)."""
    if model_parts(model) != (DEFAULT_PROVIDER, DEFAULT_MODEL):
        missing = [env for env in (PRICE_IN_ENV, PRICE_OUT_ENV) if not os.getenv(env, "").strip()]
        if missing:
            raise ValueError(f"{' and '.join(missing)} must be set for a judge model other than the default")
    return (_price_micros_per_mtok(PRICE_IN_ENV, DEFAULT_PRICE_IN_USD),
            _price_micros_per_mtok(PRICE_OUT_ENV, DEFAULT_PRICE_OUT_USD))


def call_estimate_usd_micros(n_images: int, text_chars: int, output_tokens: int, model: str | None = None) -> int:
    """Upper bound of one call (the reservation): text at 1 token per 3 characters plus overhead,
    HFVG_QC_JUDGE_IMAGE_TOKENS per image, the full output allowance. Raises ValueError when the
    price settings are invalid (the call is then refused, never made unpriced)."""
    image_tokens = _env_int(IMAGE_TOKENS_ENV, DEFAULT_IMAGE_TOKENS)
    tokens_in = math.ceil(text_chars / TEXT_CHARS_PER_TOKEN) + TEXT_OVERHEAD_TOKENS + n_images * image_tokens
    return max(1, _cost_micros(tokens_in, output_tokens, model))


def _cost_micros(tokens_in: int, tokens_out: int, model: str | None = None) -> int:
    price_in, price_out = judge_prices(model)
    return -(-(tokens_in * price_in + tokens_out * price_out) // 1_000_000)


def actual_usd_micros(tokens_in: int, tokens_out: int, model: str | None = None) -> int:
    """Cost of an answered call from the response's token usage (rounded up to a micro-dollar)."""
    return _cost_micros(max(0, int(tokens_in)), max(0, int(tokens_out)), model)


# ------------------------------------------------------------------------------------------ media


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        raise JudgeInputError(f"cannot read {Path(path).name}: {e.strerror or e}") from None
    return h.hexdigest()


def prepare_image(source: str | os.PathLike | bytes, max_px: int = MEDIA_PX) -> bytes:
    """JPEG bytes of an image (EXIF orientation applied, RGB, longest side at most max_px)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        with Image.open(io.BytesIO(raw)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
    except (OSError, UnidentifiedImageError, ValueError) as e:
        name = "image" if isinstance(source, bytes) else Path(source).name
        raise JudgeInputError(f"cannot read {name} as an image ({type(e).__name__})") from None
    return buf.getvalue()


VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


def is_video(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in VIDEO_SUFFIXES


async def probe_duration(video: str | os.PathLike) -> float:
    """The clip's real duration (ffprobe through hfvg.subproc: never blocks the event loop)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)]
    try:
        code, out, _err = await subproc.run(cmd, "ffprobe (QC judge)", timeout=30)
        duration = float(json.loads(out or b"{}")["format"]["duration"])
    except (OSError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        raise JudgeInputError(f"cannot read the duration of {Path(video).name}") from None
    if code != 0 or not math.isfinite(duration) or duration <= 0:
        raise JudgeInputError(f"cannot read the duration of {Path(video).name}")
    return duration


def spread_times(duration: float, n: int, start: float = 0.0, end: float | None = None) -> list[float]:
    """n times spread evenly across [start, end] (the middle of n equal slices of the clip)."""
    end = duration if end is None else min(end, duration)
    start = max(0.0, min(start, end))
    span = max(end - start, 0.0)
    return [round(start + span * (i + 0.5) / n, 3) for i in range(n)]


async def extract_frames(video: str | os.PathLike, times: list[float], out_dir: str | os.PathLike) -> list[Path]:
    """One JPEG per time (ffmpeg through hfvg.subproc, like review_clip's other ffmpeg calls)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(times):
        path = out / f"frame_{i:02d}.jpg"
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
               "-q:v", "3", str(path)]
        try:
            code, _out, _err = await subproc.run(cmd, "ffmpeg frame (QC judge)", timeout=60)
        except (OSError, asyncio.TimeoutError):
            code = -1
        if code != 0 or not path.is_file() or path.stat().st_size == 0:
            raise JudgeInputError(f"cannot extract a frame at {t:.2f}s from {Path(video).name}")
        frames.append(path)
    return frames


def parse_answer(text: str) -> dict[str, Any]:
    """The JSON object the judge answered (ValueError when it is not one)."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def confidence_of(value: Any) -> float:
    """A 0-1 confidence; anything else is a malformed answer (ValueError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"confidence must be a number, got {value!r}")
    conf = float(value)
    if not math.isfinite(conf) or conf < 0 or conf > 1:
        raise ValueError(f"confidence must be between 0 and 1, got {value!r}")
    return conf


def count_of(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"count must be a whole number >= 0, got {value!r}")
    return value


def bool_of(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{what} must be true or false, got {value!r}")
    return value


def text_list(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("issues must be a list of strings")
    return [v.strip()[:300] for v in value if v.strip()][:limit]


# ------------------------------------------------------------------------------------------ clients


@dataclass
class JudgeReply:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None


class JudgeClient(ABC):
    """Sends one judge request to a vision model."""

    @abstractmethod
    async def complete(self, *, model: str, system: str, text: str, images: list[bytes],
                       max_output_tokens: int) -> JudgeReply:
        """The model's text answer and its token usage."""


class OpenAIJudgeClient(JudgeClient):
    """OpenAI chat completions with images (the OPENAI_API_KEY the app already has)."""

    def __init__(self, api_key: str, http_client: Any = None):
        self._api_key = api_key
        self._http_client = http_client  # the SDK's own HTTP client by default

    async def complete(self, *, model, system, text, images, max_output_tokens):
        from openai import AsyncOpenAI

        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(img).decode("ascii"), "detail": "high"}})
        extra: dict[str, Any] = {}
        temperature = os.getenv(TEMPERATURE_ENV, "0").strip()
        if temperature and temperature.lower() != "none":  # empty / none: models without a temperature
            extra["temperature"] = float(temperature)
        options: dict[str, Any] = {"http_client": self._http_client} if self._http_client is not None else {}
        async with AsyncOpenAI(api_key=self._api_key, max_retries=0, **options) as client:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                response_format={"type": "json_object"},
                # in the body, not as a keyword: openai>=1.0 SDKs that predate the parameter send it too
                extra_body={"max_completion_tokens": max_output_tokens},
                **extra,
            )
        usage = getattr(response, "usage", None)
        return JudgeReply(response.choices[0].message.content or "",
                          getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))


class FakeJudgeClient(JudgeClient):
    """Deterministic client for tests: answers in order (a dict is sent back as JSON, a str as-is,
    an exception is raised, a callable gets the request). Never used unless passed explicitly."""

    def __init__(self, *answers: Any, tokens: tuple[int, int] | None = (1000, 100), delay: float = 0.0):
        self.answers = list(answers)
        self.tokens = tokens
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def add(self, *answers: Any) -> "FakeJudgeClient":
        self.answers.extend(answers)
        return self

    async def complete(self, *, model, system, text, images, max_output_tokens):
        request = {"model": model, "system": system, "text": text, "images": list(images),
                   "max_output_tokens": max_output_tokens}
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.answers:
            raise RuntimeError("FakeJudgeClient has no answer left")
        answer = self.answers.pop(0)
        if callable(answer) and not isinstance(answer, type):
            answer = answer(request)
        if isinstance(answer, BaseException) or (isinstance(answer, type) and issubclass(answer, BaseException)):
            raise answer
        body = answer if isinstance(answer, str) else json.dumps(answer)
        tin, tout = self.tokens if self.tokens else (None, None)
        return JudgeReply(body, tin, tout)


# ------------------------------------------------------------------------------------------ service


@dataclass
class JudgeCall:
    """One judge request. ``key_parts`` is everything the answer depends on besides the model and
    JUDGE_VERSION (content hashes, prompt, conditions...): equal parts reuse the cached answer.
    ``validate`` turns the parsed answer into the stored one or raises ValueError (malformed)."""
    kind: str
    episode_id: str | None
    subject: str
    system: str
    text: str
    images: list[bytes]
    key_parts: dict[str, Any]
    validate: Callable[[dict[str, Any]], dict[str, Any]] = field(default=lambda answer: answer)
    max_output_tokens: int | None = None


_VERDICTS_TABLE = """CREATE TABLE IF NOT EXISTS qc_judge_verdicts (
    cache_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    episode_id TEXT,
    subject TEXT,
    answer TEXT,
    error TEXT,
    usd_micros INTEGER NOT NULL DEFAULT 0,
    cost_source TEXT,
    created_at TEXT NOT NULL
)"""

# Weak: a lock disappears once no check holds it, so the registry does not grow with every judged input.
_LOCKS: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = weakref.WeakValueDictionary()


def _lock_for(key: str) -> asyncio.Lock:
    slot = (id(asyncio.get_running_loop()), key)
    lock = _LOCKS.get(slot)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[slot] = lock
    return lock


def cache_key(kind: str, model: str, key_parts: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "version": JUDGE_VERSION, "model": model, "parts": key_parts},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# First in the message: the checks cut a refusal's text at 200 characters.
BUDGET_USED_UP = "judge budget used up, a person checks this"


def _budget_limit(e: ValueError) -> bool:
    """A reserve refused at the line cap or the episode cap (not the GC.01 headroom or balance)."""
    text = str(e)
    return text.startswith("Budget cap exceeded") or "would exceed episode cap" in text


async def _stop_detail(ledger: Any, episode_id: str, estimate: int) -> str:
    try:
        line = await ledger.get_line_status(episode_id, QC_LINE)
    except Exception:  # noqa: BLE001 - the refusal stands; this only explains it
        return "the episode stop"
    used, stop = line["total_committed"], line["stop_threshold"]
    if used + estimate > stop:
        return f"the {QC_LINE} line: ${micros_to_usd_str(used)} spent or held of its ${micros_to_usd_str(stop)} stop"
    return "the episode stop"


async def _new_line_cap(ledger: Any, episode_id: str, setting: int | str, model: str) -> int:
    """The cap ``ensure_line`` gives the episode's line when it creates it (an existing line keeps
    its own). ``auto``: sized from the episode's shot plan, or the default cap when it cannot be. A
    fixed cap is kept as set, with a warning when it is smaller than that size."""
    fixed = None if setting == LINE_CAP_AUTO else int(setting)
    default = usd_to_micros(DEFAULT_LINE_CAP_USD) if fixed is None else fixed
    try:
        await ledger.get_line_status(episode_id, QC_LINE)
        return default
    except Exception:  # noqa: BLE001 - no line yet (or no ledger yet): ensure_line creates it
        pass
    from hfvg.qc.judge_budget import TAKES_PER_SHOT, episode_line_cap_usd_micros

    try:
        sized = await asyncio.to_thread(episode_line_cap_usd_micros, episode_id, model)
    except Exception as e:  # noqa: BLE001 - an unsized line gets the default cap, never none
        if fixed is None:
            logger.warning(f"QC judge line of {episode_id} not sized from its shot plan ({str(e)[:200]}): "
                           f"default cap ${DEFAULT_LINE_CAP_USD}")
        return default
    if fixed is None:
        return sized
    if sized > fixed:
        logger.warning(f"QC judge line of {episode_id}: its ${micros_to_usd_str(fixed)} cap is below the "
                       f"${micros_to_usd_str(-(-sized // 10_000) * 10_000)} its shot plan needs for "
                       f"{TAKES_PER_SHOT} takes of every shot; checks past the line's stop go to a person "
                       f"({LINE_CAP_ENV}={LINE_CAP_AUTO} sizes the line from the plan)")
    return fixed


class JudgeService:
    """The one way a QC judge spends: cache, balance gate, ledger hold, call, commit or release."""

    def __init__(self, client: JudgeClient | None = None, *, database_path: str | None = None,
                 model: str | None = None):
        self._client = client
        self._db_path = database_path
        self._model = model
        self.calls_made = 0

    @property
    def db_path(self) -> str:
        return self._db_path or db_path()

    @property
    def model(self) -> str:
        return self._model or judge_model()

    def client(self) -> JudgeClient:
        if self._client is not None:
            return self._client
        provider = judge_provider(self.model)
        if provider != DEFAULT_PROVIDER:
            raise JudgeUnavailable(f"no QC judge client for the provider {provider!r} named in {MODEL_ENV}")
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise JudgeUnavailable("QC judges are on but no judge API key is configured (OPENAI_API_KEY)")
        return OpenAIJudgeClient(key)

    async def _cached(self, key: str) -> tuple[dict | None, str | None]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_VERDICTS_TABLE)
            row = await (await db.execute(
                "SELECT answer, error, created_at FROM qc_judge_verdicts WHERE cache_key = ?", (key,))).fetchone()
        if row is None:
            return None, None
        answer, error, created_at = row
        if answer is not None:
            return json.loads(answer), None
        try:
            age = (_now() - datetime.fromisoformat(created_at)).total_seconds()
        except (TypeError, ValueError):
            return None, None
        if age < retry_after_s():
            return None, error
        return None, None

    async def _store(self, key: str, call: JudgeCall, *, answer: dict | None = None, error: str | None = None,
                     usd_micros: int = 0, cost_source: str | None = None) -> None:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(_VERDICTS_TABLE)
                await db.execute(
                    "INSERT OR REPLACE INTO qc_judge_verdicts (cache_key, kind, episode_id, subject, answer, error, "
                    "usd_micros, cost_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, call.kind, call.episode_id, call.subject[:200],
                     json.dumps(answer) if answer is not None else None, error, usd_micros, cost_source,
                     _now().isoformat(timespec="seconds")))
                await db.commit()
        except Exception as e:  # noqa: BLE001 - bookkeeping: the verdict itself is already decided
            logger.warning(f"QC judge verdict for {call.subject} not cached: {type(e).__name__}")

    async def ask(self, call: JudgeCall) -> dict[str, Any]:
        """The judge's validated answer (cached when the same inputs were judged before).
        Raises JudgeUnavailable for every refusal or failure: the caller gives NEEDS_REVIEW."""
        if dry_run():
            raise JudgeUnavailable("dry run: the QC judges make no model calls")
        model = self.model
        key = cache_key(call.kind, model, call.key_parts)
        lock = _lock_for(key)
        async with lock:
            try:
                answer, recent_error = await self._cached(key)
            except Exception as e:  # noqa: BLE001 - no cache read, no paid call
                raise JudgeUnavailable(f"QC verdict cache unavailable ({type(e).__name__})") from None
            if answer is not None:
                return answer
            if recent_error is not None:
                raise JudgeUnavailable(f"{recent_error} (not asked again for {retry_after_s()} s)")
            return await self._paid_call(call, key, model)

    async def _paid_call(self, call: JudgeCall, key: str, model: str) -> dict[str, Any]:
        from hfvg.balance_monitor import BalanceMonitor
        from hfvg.budget import BudgetLedger

        if not call.episode_id:
            raise JudgeUnavailable("a judge call needs an episode budget line (no episode id)")
        client = self.client()
        provider, model_id = model_parts(model)
        try:
            out_tokens = call.max_output_tokens or max_output_tokens()
            estimate = call_estimate_usd_micros(len(call.images), len(call.system) + len(call.text), out_tokens,
                                                model)
            cap_setting = qc_line_cap_setting()
            timeout = judge_timeout_s()
        except (TypeError, ValueError) as e:
            raise JudgeUnavailable(f"QC judge settings invalid, no cost estimate or time limit: {e}") from None
        ledger = BudgetLedger(self.db_path)
        try:
            cap = await _new_line_cap(ledger, call.episode_id, cap_setting, model)
            await ledger.ensure_line(call.episode_id, QC_LINE, cap, provider=provider)
            passed, reason = await BalanceMonitor(self.db_path).check_preflight_balance(
                provider=provider, estimate_usd_micros=estimate, auto_topup_enabled=False)
        except Exception as e:  # noqa: BLE001 - no ledger or gate, no paid call
            raise JudgeUnavailable(f"QC budget line or balance check unavailable ({type(e).__name__})") from None
        if not passed:
            raise JudgeUnavailable(f"balance check refused the judge call: {reason}")
        call_id = f"qc-{call.episode_id}-{key[:12]}-{uuid.uuid4().hex[:8]}"
        what = f"QC {call.kind} {call.subject}"
        try:
            reserved = await ledger.reserve(call.episode_id, QC_LINE, estimate, what, job_id=call_id)
        except ValueError as e:
            if not _budget_limit(e):
                raise JudgeUnavailable(f"budget refused the judge call: {e}") from None
            raise JudgeBudgetExhausted(f"{BUDGET_USED_UP}: budget refused the judge call: {e}") from None
        if not reserved:
            where = await _stop_detail(ledger, call.episode_id, estimate)
            raise JudgeBudgetExhausted(f"{BUDGET_USED_UP}: budget refused the judge call: the {QC_LINE} line or "
                                       f"the episode is at its stop ({where})")
        try:
            self.calls_made += 1
            reply = await subproc.keep_alive(
                asyncio.wait_for(client.complete(model=model_id, system=call.system, text=call.text,
                                                 images=call.images, max_output_tokens=out_tokens), timeout),
                f"QC {call.kind} judge")
        except asyncio.CancelledError:
            await ledger.release(call.episode_id, QC_LINE, estimate, f"{what}: cancelled", job_id=call_id)
            raise
        except Exception as e:  # noqa: BLE001 - every failure is NEEDS_REVIEW, never a pass
            await ledger.release(call.episode_id, QC_LINE, estimate, f"{what}: no answer", job_id=call_id)
            error = ("the judge timed out" if isinstance(e, (asyncio.TimeoutError, TimeoutError))
                     else f"judge error ({type(e).__name__})")
            await self._store(key, call, error=error)
            raise JudgeUnavailable(error) from None
        if reply.tokens_in is not None and reply.tokens_out is not None:
            actual, source = actual_usd_micros(reply.tokens_in, reply.tokens_out, model), "token_usage"
        else:
            actual, source = estimate, "estimate"
        await ledger.commit(call.episode_id, QC_LINE, estimate, actual, job_id=call_id, reason=what,
                            cost_source=source)
        try:
            answer = call.validate(parse_answer(reply.text))
        except (TypeError, ValueError, KeyError) as e:
            error = f"the judge gave a malformed answer ({str(e)[:120]})"
            await self._store(key, call, error=error, usd_micros=actual, cost_source=source)
            raise JudgeUnavailable(error) from None
        await self._store(key, call, answer=answer, usd_micros=actual, cost_source=source)
        return answer


# ------------------------------------------------------------------------------------------ registration


def register_qc_judges(process: str = "worker", service: JudgeService | None = None) -> bool:
    """Register the vision judges when HFVG_QC_JUDGES_ENABLED is on (worker: all of them; api: the
    counter, which the G2 and recompose headcount read). Off: registers nothing. True when registered."""
    if not judges_enabled():
        return False
    from hfvg.qc.headcount import set_cast_counter
    from hfvg.qc.vision_counter import VisionCastCounter

    service = service or JudgeService()
    set_cast_counter(VisionCastCounter(service))
    if process == "worker":
        from hfvg.qc.clip_constraints import set_constraint_checker
        from hfvg.qc.identity import (
            VisionCharacterDetector,
            VisionIdentityJudge,
            set_identity_judge,
        )
        from hfvg.qc.still_review import VisionStillReviewer, set_still_reviewer
        from hfvg.qc.vision_constraints import VisionClipConstraintChecker

        set_still_reviewer(VisionStillReviewer(service))
        set_constraint_checker(VisionClipConstraintChecker(service))
        set_identity_judge(VisionCharacterDetector(service), VisionIdentityJudge(service))
    key_note = "" if os.getenv("OPENAI_API_KEY", "").strip() else " (no judge API key: every check needs review)"
    logger.warning(f"QC judges enabled in the {process}{key_note}")
    return True
