# Studio API

Backend API service for the DuckDuckGoose production console.

## Setup

### Prerequisites

1. **Temporal Server**: Running on `localhost:7233` (or set `TEMPORAL_ADDRESS`)
2. **Temporal Worker**: The Python worker must be running to execute workflows
3. **Admin Secret**: Generate a secure admin secret

### Installation

```bash
# Install dependencies
pip install -r api/requirements.txt

# Also install the hfvg package
pip install -e .
```

### Generate Admin Secret

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Save this secret - you'll need it for both the API and the console.

### Environment Variables

Required:
- `ADMIN_SECRET`: Admin secret (min 32 chars) - **REQUIRED**

Optional:
- `TEMPORAL_ADDRESS`: Temporal server address (default: `localhost:7233`)
- `TEMPORAL_NAMESPACE`: Temporal namespace (default: `default`)
- `DATABASE_PATH`: SQLite database path (default: `./data/studio.db`)
- `OPENAI_API_KEY`: OpenAI API key for vision judge (if missing, gates escalate)
- `DRY_RUN`: Default dry-run mode (default: `true`)
- `PORT`: API server port (default: `8000`)

### Running Locally

#### Option 1: With Temporal Dev Server

```bash
# Terminal 1: Start Temporal dev server
temporal server start-dev

# Terminal 2: Start Temporal worker
python -m hfvg.worker

# Terminal 3: Start API
export ADMIN_SECRET="your-secret-here"
export OPENAI_API_KEY="sk-..."  # optional
python api/main.py
```

#### Option 2: With Temporal Cloud

```bash
# Set Temporal Cloud connection
export TEMPORAL_ADDRESS="your-namespace.tmprl.cloud:7233"
export TEMPORAL_NAMESPACE="your-namespace"

# Start worker (connects to cloud)
python -m hfvg.worker

# Start API
export ADMIN_SECRET="your-secret-here"
python api/main.py
```

The API will be available at `http://localhost:8000`.

## Access Control

All endpoints except `/` and `/api/health` require the admin secret in the `Authorization` header:

```bash
curl -H "Authorization: Bearer your-secret-here" http://localhost:8000/api/episodes
```

## Endpoints

### `POST /api/episodes`

Start a new episode workflow.

**Request:**
```json
{
  "episode_id": "ep04",
  "beatmap_content": "# Episode beatmap markdown...",
  "dry_run": true
}
```

**Response:**
```json
{
  "workflow_id": "ep04-1234567890",
  "run_id": "...",
  "episode_id": "ep04",
  "message": "Episode ep04 started (dry_run=true)"
}
```

### `GET /api/episodes/{episode_id}`

Get current episode state.

**Response:**
```json
{
  "episode_id": "ep04",
  "stage": "STILL_GENERATION",
  "approvals": {
    "g101": true,
    "g103": true,
    "g108": false
  },
  "shots_count": 31
}
```

### `POST /api/episodes/{episode_id}/approve`

Send approval signal for a gate.

**Request:**
```json
{
  "gate_id": "g103",
  "note": "Beatmap approved with all defaults"
}
```

### `GET /api/episodes/{episode_id}/budget`

Get budget status for all lines.

**Response:**
```json
{
  "episode_id": "ep04",
  "lines": [
    {
      "line_name": "L1_refs",
      "provider": "higgsfield",
      "spent": 45.0,
      "reserved": 46.0,
      "total": 91.0,
      "cap": 120.0,
      "stop": 96.0,
      "at_stop": false,
      "unit": "Higgsfield app credits"
    }
  ],
  "higgsfield_total": 635.4,
  "elevenlabs_total": 0.0
}
```

### `POST /api/episodes/{episode_id}/set-live`

Switch episode from dry-run to live (paid) mode.

**Response:**
```json
{
  "success": true,
  "episode_id": "ep04",
  "mode": "live",
  "message": "Episode switched to live mode..."
}
```

## Deployment

### Container (Docker/Podman)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy requirements
COPY api/requirements.txt api/
COPY pyproject.toml .
COPY hfvg/ hfvg/

# Install dependencies
RUN pip install --no-cache-dir -r api/requirements.txt
RUN pip install --no-cache-dir -e .

# Copy API
COPY api/ api/

# Expose port
EXPOSE 8000

# Run
CMD ["python", "api/main.py"]
```

Build and run:
```bash
docker build -t duckduckgoose-api .
docker run -p 8000:8000 \
  -e ADMIN_SECRET="..." \
  -e TEMPORAL_ADDRESS="host.docker.internal:7233" \
  duckduckgoose-api
```

### Persistent Machine (Linux/Mac)

1. Clone the repo
2. Install dependencies: `pip install -r api/requirements.txt && pip install -e .`
3. Set environment variables in `.env` or systemd service
4. Run with systemd or supervisor:

```ini
# /etc/systemd/system/duckduckgoose-api.service
[Unit]
Description=DuckDuckGoose Studio API
After=network.target

[Service]
Type=simple
User=duckduckgoose
WorkingDirectory=/opt/duckduckgoose
Environment="ADMIN_SECRET=..."
Environment="TEMPORAL_ADDRESS=localhost:7233"
ExecStart=/usr/bin/python3 api/main.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Worker Deployment

The Temporal worker must run alongside the API:

```bash
# Same environment variables
export ADMIN_SECRET="..."
export TEMPORAL_ADDRESS="..."

# Run worker
python -m hfvg.worker
```

Worker can run in the same container or as a separate service.

## Security Notes

1. **Never commit ADMIN_SECRET** to the repository
2. Use a strong secret (min 32 characters, use `secrets.token_urlsafe(32)`)
3. In production, use HTTPS only
4. Rotate secrets periodically
5. The public marketing site (`/`, `/archetypes`, `/pricing`) has no authentication
6. Only the console (`/studio`) and API require the admin secret
