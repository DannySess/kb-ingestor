#!/usr/bin/env python3
"""
kb-ingestor: Watches a folder and auto-uploads Markdown files to Open WebUI knowledge base.
"""

import os
import sys
import time
import logging
import requests
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kb-ingestor")

# ── Config from environment ───────────────────────────────────────────────────
OPEN_WEBUI_URL   = os.environ["OPEN_WEBUI_URL"].rstrip("/")   # e.g. http://192.168.0.230:8181
OPEN_WEBUI_TOKEN = os.environ["OPEN_WEBUI_TOKEN"]             # API key from Open WebUI profile
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]           # UUID of BMS Documents KB
WATCH_DIR        = os.environ.get("WATCH_DIR", "/watch")
ALLOWED_EXTS     = {e.strip().lower() for e in os.environ.get("ALLOWED_EXTENSIONS", ".md").split(",")}


# ── Open WebUI API helpers ────────────────────────────────────────────────────

def api_headers() -> dict:
    return {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}


def file_already_uploaded(filename: str) -> bool:
    """Check if a file with this name already exists in Open WebUI files."""
    try:
        r = requests.get(f"{OPEN_WEBUI_URL}/api/v1/files/", headers=api_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            existing = [f["filename"] for f in data if isinstance(f, dict)]
        else:
            existing = []
        return filename in existing
    except Exception as e:
        log.warning(f"Could not check existing files: {e}")
        return False


def upload_file(path: Path) -> str | None:
    """Upload a file to Open WebUI. Returns file_id on success, None on failure."""
    try:
        with open(path, "rb") as fh:
            r = requests.post(
                f"{OPEN_WEBUI_URL}/api/v1/files/",
                headers=api_headers(),
                files={"file": (path.name, fh, "text/markdown")},
                timeout=60,
            )
        r.raise_for_status()
        file_id = r.json()["id"]
        log.info(f"  ✅ Uploaded file: {path.name} → file_id={file_id}")
        return file_id
    except Exception as e:
        log.error(f"  ❌ Upload failed for {path.name}: {e}")
        return None


def add_file_to_knowledge_base(file_id: str, filename: str) -> bool:
    """Add an already-uploaded file to the BMS Documents knowledge base."""
    try:
        r = requests.post(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{KNOWLEDGE_BASE_ID}/file/add",
            headers={**api_headers(), "Content-Type": "application/json"},
            json={"file_id": file_id},
            timeout=30,
        )
        r.raise_for_status()
        log.info(f"  ✅ Added to knowledge base: {filename}")
        return True
    except Exception as e:
        log.error(f"  ❌ Failed to add {filename} to knowledge base: {e}")
        return False


def ingest_file(path: Path) -> bool:
    """Full pipeline: upload file then add to knowledge base."""
    if path.suffix.lower() not in ALLOWED_EXTS:
        log.debug(f"  ⏭  Skipping (not allowed extension): {path.name}")
        return False

    log.info(f"📄 Processing: {path.name}")

    if file_already_uploaded(path.name):
        log.info(f"  ⏭  Already in Open WebUI, skipping: {path.name}")
        return False

    file_id = upload_file(path)
    if not file_id:
        return False

    return add_file_to_knowledge_base(file_id, path.name)


# ── Watchdog handler ──────────────────────────────────────────────────────────

class MarkdownHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 New file detected: {path.name}")
            # Brief delay to ensure file is fully written before reading
            time.sleep(2)
            ingest_file(path)

    def on_moved(self, event):
        """Handle files moved/renamed into the watch directory."""
        if event.is_directory:
            return
        path = Path(event.dest_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 File moved into watch dir: {path.name}")
            time.sleep(2)
            ingest_file(path)


# ── Startup scan ─────────────────────────────────────────────────────────────

def startup_scan(watch_dir: Path):
    """Process all existing allowed files in the watch directory on startup."""
    files = sorted(p for p in watch_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTS)
    if not files:
        log.info("🔍 Startup scan: no existing files found.")
        return

    log.info(f"🔍 Startup scan: found {len(files)} file(s) to process...")
    success = 0
    for path in files:
        if ingest_file(path):
            success += 1
    log.info(f"✅ Startup scan complete: {success}/{len(files)} file(s) ingested.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    watch_dir = Path(WATCH_DIR)
    if not watch_dir.exists():
        log.error(f"Watch directory does not exist: {watch_dir}")
        sys.exit(1)

    log.info("=" * 60)
    log.info("kb-ingestor starting")
    log.info(f"  Watch dir:        {watch_dir}")
    log.info(f"  Open WebUI:       {OPEN_WEBUI_URL}")
    log.info(f"  Knowledge base:   {KNOWLEDGE_BASE_ID}")
    log.info(f"  Allowed exts:     {ALLOWED_EXTS}")
    log.info("=" * 60)

    # Process existing files first
    startup_scan(watch_dir)

    # Start real-time watcher
    handler = MarkdownHandler()
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=True)
    observer.start()
    log.info(f"👁  Watching for new files in {watch_dir} ...")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
