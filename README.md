# kb-ingestor

Auto-watches a NAS folder and uploads new Markdown files to an [Open WebUI](https://github.com/open-webui/open-webui) knowledge base in real time.

## How it works

1. On startup — scans the watch folder and ingests any files not already in Open WebUI
2. While running — uses inotify to detect new `.md` files the moment they appear
3. For each file — uploads it via the Open WebUI Files API, then adds it to the target knowledge base
4. All actions logged to stdout (`docker logs kb-ingestor`)

## Requirements

- Open WebUI running and accessible on your network
- A knowledge base already created in Open WebUI (e.g. "BMS Documents")
- An API key from Open WebUI (Profile → API Keys)

## Setup

### 1. Get your credentials from Open WebUI

**API Token:**
Open WebUI → Profile (top right) → API Keys → Create new key

**Knowledge Base ID:**
Open WebUI → Workspace → Knowledge → BMS Documents  
Copy the UUID from the URL bar: `.../workspace/knowledge/<UUID-IS-HERE>`

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in OPEN_WEBUI_TOKEN and KNOWLEDGE_BASE_ID
```

### 3. Build and run

```bash
# Build the image
docker build -t kb-ingestor:latest .

# Run with docker-compose
docker-compose up -d

# Watch the logs
docker logs -f kb-ingestor
```

### Unraid deployment

1. Copy the repo to your Unraid server (e.g. `/mnt/user/appdata/kb-ingestor/`)
2. Fill in `.env`
3. Run `docker-compose up -d` via Unraid terminal or add to a User Script

## Configuration

All config is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPEN_WEBUI_URL` | ✅ | — | Base URL of Open WebUI, e.g. `http://192.168.0.230:8181` |
| `OPEN_WEBUI_TOKEN` | ✅ | — | API key from Open WebUI profile |
| `KNOWLEDGE_BASE_ID` | ✅ | — | UUID of the target knowledge base |
| `WATCH_DIR` | ❌ | `/watch` | Path inside the container to watch |
| `ALLOWED_EXTENSIONS` | ❌ | `.md` | Comma-separated file extensions to ingest |

## Log output example

```
2026-06-12 10:00:01 [INFO] ============================================================
2026-06-12 10:00:01 [INFO] kb-ingestor starting
2026-06-12 10:00:01 [INFO]   Watch dir:        /watch
2026-06-12 10:00:01 [INFO]   Open WebUI:       http://192.168.0.230:8181
2026-06-12 10:00:01 [INFO]   Knowledge base:   xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
2026-06-12 10:00:01 [INFO] ============================================================
2026-06-12 10:00:01 [INFO] 🔍 Startup scan: found 3 file(s) to process...
2026-06-12 10:00:01 [INFO] 📄 Processing: SE_StruxureWare_Guide.md
2026-06-12 10:00:03 [INFO]   ✅ Uploaded file: SE_StruxureWare_Guide.md → file_id=abc123
2026-06-12 10:00:04 [INFO]   ✅ Added to knowledge base: SE_StruxureWare_Guide.md
2026-06-12 10:00:05 [INFO] ✅ Startup scan complete: 1/3 file(s) ingested.
2026-06-12 10:00:05 [INFO] 👁  Watching for new files in /watch ...
2026-06-12 10:15:32 [INFO] 🔔 New file detected: EBO_Commission_Guide.md
2026-06-12 10:15:34 [INFO]   ✅ Uploaded file: EBO_Commission_Guide.md → file_id=def456
2026-06-12 10:15:35 [INFO]   ✅ Added to knowledge base: EBO_Commission_Guide.md
```

## Notes

- Only `.md` files are ingested by default — this is intentional. Markdown retrieves dramatically better than PDF in RAG.
- Files already present in Open WebUI (matched by filename) are skipped on startup scan to avoid duplicates.
- The volume is mounted `:ro` (read-only) — the container never modifies your NAS files.
