# Provider Authentication for Headless Production

Research and decisions for running Higgsfield (image/video generation) and ElevenLabs (audio) in a headless server-side worker without interactive OAuth.

**Date**: 2026-10-03  
**Status**: Headless auth via API keys SUPPORTED for both providers

---

## Summary

Both Higgsfield and ElevenLabs support headless server-side auth using API keys from environment variables. No interactive OAuth sign-in required for production workflows.

### Credentials Required

| Provider | Env Var | How to Obtain | Docs |
|----------|---------|---------------|------|
| **Higgsfield** | `HIGGSFIELD_API_KEY` | Dashboard → Account → API Keys | https://higgsfield.ai/docs/api-authentication |
| **ElevenLabs** | `ELEVENLABS_API_KEY` or `XI_API_KEY` | Profile Settings → API Key | https://docs.elevenlabs.io/api-reference/authentication |

---

## 1. Higgsfield Authentication

### MCP vs Direct API

Higgsfield provides two integration paths:

1. **MCP Server** (`@higgsfield/mcp-server`) - Browser-based OAuth flow
   - Primary user interface through Cursor/Claude Desktop
   - Requires interactive browser sign-in
   - **NOT suitable for headless server workers**

2. **Direct REST API** with API key authentication
   - Official Python SDK: `higgsfield-python` (https://github.com/higgsfield-ai/higgsfield-python)
   - API key passed via `HIGGSFIELD_API_KEY` environment variable or client initialization
   - **RECOMMENDED for production workflows**

### Implementation Decision

**Use Direct REST API** with the official Python SDK for all generation activities:

```python
from higgsfield import Higgsfield

client = Higgsfield(api_key=os.getenv("HIGGSFIELD_API_KEY"))

# Image generation (GPT Image 2.0)
job = client.images.generate(
    model="gpt_image_2",
    prompt="A cozy mountain resort lobby",
    resolution="2k",
    quality="high"
)

# Video generation (Seedance 2.5, Kling 3.0)
job = client.video.generate(
    model="seedance_2.5",
    start_image=image_url,
    prompt="Gentle breathing motion",
    resolution="480p",
    draft=True  # For draft-first tiering
)
```

### Models & Endpoints

| Model | Use Case | Endpoint | Playbook Tier |
|-------|----------|----------|---------------|
| `gpt_image_2` | Stills generation | `/images/generate` | Draft: 1k medium (1cr), Final: 2k high (6.5cr) |
| `seedance_2.5` | Dry shots, hand-offs | `/video/generate` | Draft: 480p (3cr/s), Final: 1080p finalize (12cr/s) |
| `kling_3.0` | Wet/mud/pool shots | `/video/generate` | Pro 4-5s (6cr), draft=final |
| `upscale_image` | Still upscaling | `/images/upscale` | 2k/4k = 2cr flat |
| `upscale_video` | Video upscaling | `/video/upscale` | ~0.02cr/s (ByteDance) |

### Credit Tracking

- Balance check: `client.account.get_balance()`
- Job cost estimate: `client.get_cost(model, params)` (preflight, 0 credits)
- Transaction history: `client.account.list_transactions()`

### Moderation Handling

Seedance 2.5 false-flags wet/mud content (~46% block rate in Ep02). The API returns:
- Job status: `"blocked"` or `"flagged"`
- Refund applied automatically (0 net credits)
- Error mapped to `ContentBlockError` in our adapters

Recovery ladder:
1. Rephrase once with fresh media upload
2. Switch to Kling 3.0 (never blocked on wet content)
3. Never fall back to Ken Burns or static holds

---

## 2. ElevenLabs Authentication

### API Key Authentication

ElevenLabs uses simple API key authentication for all endpoints:

```python
from elevenlabs import ElevenLabs

client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

# Text-to-speech
audio = client.text_to_speech.convert(
    voice_id="4sirbXwrtRlmPV80MJkQ",  # Sora voice
    text="きょうのお客様は、三世代のニホンザルのご家族です。",
    model_id="eleven_multilingual_v2"
)

# Sound effects
sfx = client.sound_generation.generate(
    text="Gentle hot spring water bubbling",
    duration_seconds=5.0
)

# Music generation
music = client.music_generation.generate(
    prompt="Calm Japanese ambient music with koto",
    duration_seconds=30
)
```

### Models & Endpoints

| Model | Use Case | Endpoint | Playbook Budget |
|-------|----------|----------|-----------------|
| `eleven_multilingual_v2` or `eleven_turbo_v2_5` | Voiceover (Japanese) | `/text-to-speech` | ~900cr per VO take |
| `eleven_music` | Background music | `/music-generation` | ~1,400cr per 95s |
| Sound Generation API | SFX beds | `/sound-generation` | ~100cr per 5 beds |

### Voice Selection

Series voice locks:
- **Duck narrator**: Sora voice ID `4sirbXwrtRlmPV80MJkQ`
- Settings: `[softly] [gently] [warm smile]`, atempo 0.90, ~4.2 mora/s
- Language: Japanese (です/ます form), English hard-subs

### Credit Budget

Ep03 audio budget: ≤4,000 ElevenLabs credits (~$0.80)
- Picture lock required before any audio generation
- One VO take pair + targeted retakes
- Reuse series beds and SFX where possible

---

## 3. Configuration

### Environment Variables

Production workers must set:

```bash
# Required for generation
HIGGSFIELD_API_KEY=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
ELEVENLABS_API_KEY=sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional: Override default behavior
DRY_RUN=false  # Must be explicitly false for real generation
HIGGSFIELD_BASE_URL=https://api.higgsfield.ai  # Default
ELEVENLABS_BASE_URL=https://api.elevenlabs.io   # Default
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
# Add to pyproject.toml dependencies
pip install higgsfield-python>=1.0.0
pip install elevenlabs>=1.0.0
```

Both SDKs handle:
- Automatic retries with exponential backoff
- Rate limiting (429 responses)
- Webhook polling for async jobs
- Multipart uploads for media references

---

## 5. Job Polling & Idempotency

### Higgsfield Job Flow

```python
# Submit job
job = client.video.generate(...)
job_id = job.id  # e.g., "job_abc123xyz"

# Poll until complete
while True:
    status = client.jobs.get(job_id)
    if status.state in ["completed", "failed", "blocked"]:
        break
    activity.heartbeat({"job_id": job_id, "progress": status.progress})
    await asyncio.sleep(poll_interval)

# Retrieve result
if status.state == "completed":
    asset_url = status.output.url
    actual_cost = status.cost
```

### Idempotency

Provider job IDs serve as idempotency anchors:
- Store `provider_job_id` in ledger on first submit
- On activity retry, check ledger first
- Resume polling same job ID instead of submitting new job
- Never double-charge for the same generation

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

## 7. Sources

- Higgsfield API Docs: https://higgsfield.ai/docs/api
- Higgsfield Python SDK: https://github.com/higgsfield-ai/higgsfield-python
- ElevenLabs API Reference: https://docs.elevenlabs.io/api-reference
- ElevenLabs Python SDK: https://github.com/elevenlabs/elevenlabs-python
- MCP Server Specs: https://github.com/higgsfield-ai/mcp-server (OAuth-based, not for headless)

---

## Decision

✅ **APPROVED**: Use direct API key authentication for both providers.

Travis must provide:
1. `HIGGSFIELD_API_KEY` from Higgsfield dashboard
2. `ELEVENLABS_API_KEY` from ElevenLabs profile settings

Both can be loaded from:
- Environment variables (production)
- `.env` file (local, not committed)
- Cursor Cloud Agent secrets (if running as agent)

No interactive OAuth required. Ready for headless production workflows.
