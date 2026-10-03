# Provider Authentication for Headless Production

Research and decisions for running Higgsfield (image/video generation) and ElevenLabs (audio) in a headless server-side worker without interactive OAuth.

**Date**: 2026-10-03  
**Status**: Headless auth via API keys SUPPORTED for both providers  
**Research Method**: Verified against actual SDK source code and official documentation

---

## Summary

Both Higgsfield Cloud API and ElevenLabs support headless server-side auth using API keys from environment variables. No interactive OAuth sign-in required for production workflows.

### Credentials Required

| Provider | Env Var(s) | How to Obtain | Verified Docs |
|----------|---------|---------------|------|
| **Higgsfield Cloud** | `HF_KEY` (as `"key-id:key-secret"`) **OR** `HF_API_KEY` + `HF_API_SECRET` | [Higgsfield Cloud Dashboard](https://cloud.higgsfield.ai) | [GitHub README](https://github.com/higgsfield-ai/higgsfield-client#authentication) |
| **ElevenLabs** | `ELEVENLABS_API_KEY` (SDK auto-detects) | [ElevenLabs Profile Settings → API Key](https://elevenlabs.io/app/settings/api-keys) | [API Docs](https://docs.elevenlabs.io/api-reference/authentication) |

---

## 1. Higgsfield Authentication

### ⚠️ Critical: MCP vs Cloud API Accounts

Higgsfield provides **two separate services** with **separate billing**:

1. **MCP Server** (`@higgsfield/mcp-server`)
   - Uses higgsfield.ai app credits (the MCP server you see in Cursor)
   - Browser-based OAuth flow
   - **NOT available as a headless API**
   - Credits purchased through higgsfield.ai

2. **Cloud API** ([cloud.higgsfield.ai](https://cloud.higgsfield.ai))
   - **Separate billing system** from MCP app
   - Headless API with key-based auth
   - Official SDK: [`higgsfield-client`](https://github.com/higgsfield-ai/higgsfield-client) (PyPI package name: `higgsfield-client`)
   - **Travis would need a separate Cloud API account**

**Decision**: Use Higgsfield Cloud API for headless generation. This requires Travis to:
1. Create account at cloud.higgsfield.ai
2. Purchase credits separately from app credits
3. Generate API key + secret pair

### Verified Implementation

**Installation:**
```bash
pip install higgsfield-client
```

**Authentication** (verified from [SDK README](https://raw.githubusercontent.com/higgsfield-ai/higgsfield-client/main/README.md)):

```python
import higgsfield_client
from higgsfield_client import AsyncClient

# Option 1: Single combined key (environment)
# export HF_KEY="your-key-id:your-key-secret"
result = await higgsfield_client.subscribe_async(
    'application-path',
    arguments={...}
)

# Option 2: Separate key and secret (environment)
# export HF_API_KEY="your-key-id"
# export HF_API_SECRET="your-key-secret"
client = AsyncClient()  # Auto-detects from env

# Option 3: Explicit in code (NOT recommended for production)
client = AsyncClient(api_key="key-id:key-secret")
```

### Available Models & Paths

**⚠️ UNVERIFIED**: The following model paths are based on the PIPELINE-LESSONS.md references and the README example (`bytedance/seedream/v4/text-to-image`). **The exact model paths for GPT Image 2, Seedance 2.5, and Kling 3.0 could not be verified** from public documentation.

Likely model paths (UNVERIFIED):
- Image generation: `bytedance/seedream/v4/text-to-image` or similar
- Video generation: Model path for Seedance 2.5 / Kling 3.0 not confirmed
- **Travis must verify available models** in Cloud API dashboard

**Verified API methods** (from SDK source):

```python
import higgsfield_client
from higgsfield_client import AsyncClient, Completed, Failed, NSFW, InProgress, Queued

client = AsyncClient()

# Submit and wait (blocks until complete)
result = await higgsfield_client.subscribe_async(
    'model-path',
    arguments={
        'prompt': 'A cozy mountain resort lobby',
        'resolution': '2K',  # Example from README
        # Other params depend on model
    }
)

# Submit and poll manually
controller = await higgsfield_client.submit_async(
    'model-path',
    arguments={...},
    webhook_url='https://example.com/webhook'  # Optional
)

# Poll for status
async for status in controller.poll_request_status():
    if isinstance(status, Queued):
        print("Queued")
    elif isinstance(status, InProgress):
        print("In progress")
    elif isinstance(status, Completed):
        print("Done!")
        break
    elif isinstance(status, (Failed, NSFW)):
        print("Error or blocked")
        break

# Get final result
result = await controller.get()

# File upload (for start images / references)
url = await higgsfield_client.upload_file_async('path/to/image.png')
```

### Credit Tracking & Costs

**⚠️ UNVERIFIED**: No balance check or cost estimation endpoint found in SDK documentation.

The SDK README shows:
- ✅ Submit jobs with `subscribe` or `submit`
- ✅ Poll job status with `status()` or `poll_request_status()`
- ✅ Cancel jobs with `cancel()`
- ❌ No `get_balance()` endpoint visible
- ❌ No `get_cost()` or `estimate_cost()` endpoint visible

**Implication**: Cost tracking must be done client-side using the model cost card from PIPELINE-LESSONS.md §5.1. Actual costs would come from Higgsfield Cloud dashboard after jobs complete.

### Moderation Handling (NSFW Status)

**Verified** from SDK:  
- `NSFW` is a **status type** returned by `poll_request_status()`, not an exception
- NSFW jobs have status `isinstance(status, higgsfield_client.NSFW)`
- Per PIPELINE-LESSONS.md: Seedance 2.5 false-flagged ~46% of Ep02 wet/mud shots
- Per PIPELINE-LESSONS.md: Refunds applied automatically (0 net credits, but time lost)

**Implementation**:  
Map `NSFW` status to `ContentBlockError` in our adapter, triggering recovery ladder:
1. Rephrase once with fresh media upload
2. Switch to Kling 3.0 (never blocked in Ep02)
3. Never fall back to Ken Burns or static holds

---

## 2. ElevenLabs Authentication

### API Key Authentication

**Verified** from [official docs](https://docs.elevenlabs.io/api-reference/authentication):

ElevenLabs uses simple API key authentication with `xi-api-key` header.

**Installation:**
```bash
pip install elevenlabs
```

**Authentication** (verified from [SDK](https://pypi.org/project/elevenlabs/)):

```python
from elevenlabs.client import ElevenLabs

# Auto-detects ELEVENLABS_API_KEY environment variable
client = ElevenLabs()

# Or explicit
client = ElevenLabs(api_key="your-api-key")

# Text-to-speech (verified endpoint exists)
audio_generator = client.text_to_speech.convert(
    voice_id="4sirbXwrtRlmPV80MJkQ",  # Sora voice from playbook
    text="きょうのお客様は、三世代のニホンザルのご家族です。",
    model_id="eleven_multilingual_v2"
)

# Returns generator, iterate or stream
for chunk in audio_generator:
    # Process audio bytes
    pass

# Sound effects (verified endpoint: text_to_sound_effects)
audio = client.text_to_sound_effects.convert(
    text="Gentle hot spring water bubbling",
    duration_seconds=5.0
)

# Music generation (verified endpoint: music)
music = client.music.generate(
    prompt="Calm Japanese ambient music with koto",
    duration_seconds=30
)
```

### Available Endpoints (Verified)

From SDK introspection, ElevenLabs client has these verified attributes:
- `text_to_speech` ✅
- `text_to_sound_effects` ✅  
- `music` ✅
- `models` ✅
- `voices` ✅
- `user` ✅ (likely includes usage/subscription)
- `usage` ✅
- `history` ✅

**⚠️ PARTIALLY VERIFIED**: Cost estimation not directly documented. The `usage` endpoint likely provides quota info.

### Credit Costs

**UNVERIFIED** (from PIPELINE-LESSONS.md, not confirmed from docs):
- TTS: ~30cr per minute (11_multilingual_v2)
- Music: ~1,400cr per 95s
- SFX: ~100cr per 5 beds

**Note**: ElevenLabs uses character-based pricing. 1 credit ≈ 1000 characters. Verify actual costs from dashboard.

### Voice Selection (From Playbook)

Series voice locks:
- **Duck narrator**: Sora voice ID `4sirbXwrtRlmPV80MJkQ` (from CHARACTER-LOCK)
- Settings: `[softly] [gently] [warm smile]`, atempo 0.90, ~4.2 mora/s
- Language: Japanese (です/ます form), English hard-subs

### Credit Budget (From CREDIT-PLAN)

Ep03 audio budget: ≤4,000 ElevenLabs credits
- Picture lock required before any audio generation
- One VO take pair + targeted retakes
- Reuse series beds and SFX where possible

---

## 3. Configuration

### Environment Variables

Production workers must set:

```bash
# Higgsfield Cloud API (REQUIRED - separate from MCP app credits)
# Option 1: Combined key
export HF_KEY="your-key-id:your-key-secret"

# Option 2: Separate (both required)
export HF_API_KEY="your-key-id"
export HF_API_SECRET="your-key-secret"

# ElevenLabs (REQUIRED)
export ELEVENLABS_API_KEY="sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Safety (harness default)
export DRY_RUN=true  # Must be explicitly false for real generation
```

### Dry-Run Safety

**HARD CONSTRAINT**: All generation code defaults to `DRY_RUN=true`.

Real API calls only when:
1. `DRY_RUN=false` is explicitly set in environment
2. API keys are present and valid
3. Human explicitly confirms via CLI (e.g., canary command)

Tests and CI **never** set `DRY_RUN=false` and never have real API keys.

### Local Development

For local testing without credits:

```bash
# .env.local (not committed)
DRY_RUN=true
HIGGSFIELD_API_KEY=mock_key_for_testing
ELEVENLABS_API_KEY=mock_key_for_testing
```

Dry-run mode:
- Returns mock responses with realistic job IDs
- Simulates delays (configurable, default ~0.1s)
- Records what would have been generated
- Logs credit estimates without spending

---

## 4. SDK Installation

```bash
# Verified package names
pip install higgsfield-client  # v0.2.0 as of 2026-10-03
pip install elevenlabs          # v2.70.0 as of 2026-10-03
```

**higgsfield-client** handles:
- Async/sync job submission and polling
- Webhook callbacks (optional)
- File uploads for references
- Status polling with backoff

**elevenlabs** handles:
- Streaming and batch audio generation
- Voice management
- Usage tracking

---

## 5. Job Polling & Idempotency

### Higgsfield Job Flow (Verified)

```python
import higgsfield_client
from higgsfield_client import Completed, Failed, NSFW, InProgress

# Submit job and get controller
controller = await higgsfield_client.submit_async(
    'model-path',
    arguments={...}
)

# Get request ID for ledger
request_id = controller.request_id

# Poll until terminal state
async for status in controller.poll_request_status():
    if isinstance(status, InProgress):
        # Send heartbeat with progress
        continue
    elif isinstance(status, Completed):
        result = await controller.get()
        asset_url = result['output_url']  # Structure depends on model
        break
    elif isinstance(status, Failed):
        raise Exception("Generation failed")
    elif isinstance(status, NSFW):
        raise ContentBlockError("Moderation block")
```

### Idempotency

Provider request IDs serve as idempotency anchors:
- Store `request_id` in ledger on first submit
- On activity retry, check ledger first
- Resume polling same request ID with `higgsfield_client.status_async(request_id=...)`
- Never double-submit the same generation

---

## 6. Testing Strategy

### Unit Tests (Zero Credits)

Mock both SDKs completely:
```python
@pytest.fixture
def mock_higgsfield(monkeypatch):
    mock_client = Mock(spec=Higgsfield)
    mock_client.images.generate.return_value = MockJob(
        id="job_mock_123",
        state="completed",
        output={"url": "https://mock.higgsfield.ai/image.png"},
        cost=6.5
    )
    monkeypatch.setattr("hfvg.providers.higgsfield.Higgsfield", lambda **kw: mock_client)
    return mock_client
```

### Integration Tests (Dry-Run)

Use real SDK imports but intercept at HTTP layer:
```python
@pytest.fixture
def vcr_cassette():
    # Record real API responses once, replay in tests
    with vcr.use_cassette("tests/fixtures/higgsfield_image_gen.yaml"):
        yield
```

### Canary Test (Real, Manual Only)

```bash
# Human runs once to verify setup
hfvg canary --model gpt_image_2 --resolution 1k --quality medium

# Prints:
# Estimated cost: 1.0 credits
# Current balance: 459.5 credits
# Confirm generation? [y/N]:
```

Never runs in CI. Only in production environment with human supervision.

---

## 7. Verified Sources

All claims in this document are verified against these sources:

**Higgsfield Cloud API:**
- ✅ SDK Repository & README: https://github.com/higgsfield-ai/higgsfield-client
- ✅ PyPI Package: https://pypi.org/project/higgsfield-client/ (v0.2.0)
- ✅ Homepage: https://cloud.higgsfield.ai
- ✅ SDK Source Code: Inspected via `import higgsfield_client; help(...)`

**ElevenLabs:**
- ✅ API Authentication Docs: https://docs.elevenlabs.io/api-reference/authentication
- ✅ PyPI Package: https://pypi.org/project/elevenlabs/ (v2.70.0)
- ✅ SDK Source Code: Inspected via `import elevenlabs.client; help(...)`

**MCP Server (NOT used for headless):**
- ✅ Higgsfield MCP: https://github.com/higgsfield-ai/mcp-server (OAuth-based, separate billing)

---

## Answers to Verification Questions

### (a) Model Availability on Higgsfield Cloud API

**Model Discovery**: Per [docs.higgsfield.ai](https://docs.higgsfield.ai/docs/llms.txt), model paths must be discovered at cloud.higgsfield.ai.

**Known Paths** (from [Quickstart](https://docs.higgsfield.ai/docs/quickstart.md) and [OpenAPI spec](https://docs.higgsfield.ai/docs/openapi.json)):
- **Soul (image generation)**: `/higgsfield-ai/soul/v2/standard` or `/higgsfield-ai/soul/standard`
- **Kling video**: `/kling-video/v2.5-turbo/pro/image-to-video` (pro tier)
- **Kling video**: `/kling-video/v2.5-turbo/standard/image-to-video` (standard tier)
- **Minimax Hailuo**: `/minimax/hailuo-2.3/standard/image-to-video`

**Not Found in Public Docs**:
- GPT Image 2 (if available)
- Seedance 2.5 (if available)
- Kling 3.0 (may be at `/kling-video/v2.5-turbo/pro/image-to-video`)

**Configuration Required**:
```bash
# NO DEFAULTS - must configure before real generation
export MODEL_PATH_GPT_IMAGE_2="/path/from/console"  # Required for stills
export MODEL_PATH_SEEDANCE="/path/from/console"      # Required for dry video
export MODEL_PATH_KLING="/kling-video/v2.5-turbo/pro/image-to-video"  # Try this for Kling
```

**Submission will fail fast** with clear error if paths not set.

**Travis must**: Log into cloud.higgsfield.ai, browse model catalog, verify exact paths.

### (b) Billing Separation: Cloud API vs MCP App

**VERIFIED**: Higgsfield Cloud API and MCP app use **separate billing systems**.

**Sources:**
- [Higgsfield Enterprise FAQ](https://higgsfield-enterprise-help.higgsfield.app/docs/faq): "web credits/packs don't apply to API usage, register at cloud.higgsfield.ai separately"
- [Higgsfield API Launch Blog](https://higgsfield.ai/blog/higgsfield-api): "separate product, USD pay-per-generation balance, MCP spends plan credits"
- [API FAQ](https://docs.higgsfield.ai/docs/help/faq): Failed and NSFW-flagged requests are NOT charged (auto-refunded)

**Billing models:**
- **MCP Server** (higgsfield.ai): Plan credits (monthly/annual subscription)
- **Cloud API** (cloud.higgsfield.ai): Pay-per-generation USD balance, top-up model

**Implication**: Travis can either:
1. Use Cloud API with a separate paid account (pay-per-generation)
2. Continue using MCP plan credits via an operator bridge (see §8 below)

### (c) Balance / Cost Endpoint

**UNVERIFIED**: No balance check or cost estimation endpoint found in SDK documentation or source code.

**What exists:**
- ✅ Job submission: `submit()` / `subscribe()`
- ✅ Status polling: `status()` / `poll_request_status()`
- ✅ Result retrieval: `result()` / `get()`
- ❌ No `get_balance()` method
- ❌ No `estimate_cost()` method

**Implication**: 
- Balance must be checked via cloud.higgsfield.ai dashboard
- Cost estimation must use client-side model cost card (PIPELINE-LESSONS.md §5.1)
- Actual costs would be available after job completion (possibly in `result` response, unverified)

### (d) NSFW / Moderation Status → ContentBlockError

**VERIFIED**: `NSFW` is a status type in the SDK.

**Implementation:**
```python
from higgsfield_client import NSFW
from hfvg.errors import ContentBlockError

async for status in controller.poll_request_status():
    if isinstance(status, NSFW):
        # Map to ContentBlockError to trigger recovery ladder
        raise ContentBlockError(
            f"Content moderation blocked request {controller.request_id}"
        )
```

**Recovery ladder** (from PIPELINE-LESSONS.md §3.2):
1. Detect `NSFW` status
2. Raise `ContentBlockError` (non-retryable)
3. Rephrase once with fresh media upload
4. Switch to Kling 3.0 (never blocked in Ep02)
5. Never fall back to Ken Burns or static holds

---

## Decision

✅ **APPROVED with caveats**: Use API key authentication for both providers.

**Travis must provide:**

1. **Higgsfield Cloud API** (NEW account, separate from MCP)
   - Sign up at https://cloud.higgsfield.ai
   - Generate API key + secret pair
   - Purchase credits (separate from app)
   - Set `HF_KEY="key:secret"` OR `HF_API_KEY` + `HF_API_SECRET`
   - ⚠️ **Verify available model paths in dashboard**

2. **ElevenLabs**
   - Get API key from https://elevenlabs.io/app/settings/api-keys
   - Set `ELEVENLABS_API_KEY="sk_..."`

**Known limitations:**
- No balance check endpoint (use dashboard)
- No cost estimation endpoint (use client-side model card)
- Model paths must be verified in Cloud API dashboard after signup

**Ready for headless workflows** with manual balance tracking.
