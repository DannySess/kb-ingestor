#!/usr/bin/env python3
"""
kb-ingestor v2: Multi-KB routing, file update detection, config-driven.
"""

import os
import sys
import time
import json
import hashlib
import logging
import requests
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kb-ingestor")

# ── Config ────────────────────────────────────────────────────────────────────
WATCH_DIR      = Path(os.environ.get("WATCH_DIR", "/watch"))
CONFIG_PATH    = Path(os.environ.get("CONFIG_PATH", "/config/kb_config.json"))
ALLOWED_EXTS   = {e.strip().lower() for e in os.environ.get("ALLOWED_EXTENSIONS", ".md").split(",")}

# Fallback env vars (used if no config file)
OPEN_WEBUI_URL    = os.environ.get("OPEN_WEBUI_URL", "").rstrip("/")
OPEN_WEBUI_TOKEN  = os.environ.get("OPEN_WEBUI_TOKEN", "")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")

# ── State ─────────────────────────────────────────────────────────────────────
_config = {}
_kb_mappings = {}       # folder_name -> kb_id
_kb_file_index = {}     # kb_id -> {filename -> file_id}
_file_hashes = {}       # filepath -> md5 hash


def load_config():
    global _config, _kb_mappings, OPEN_WEBUI_URL, OPEN_WEBUI_TOKEN, KNOWLEDGE_BASE_ID
    if CONFIG_PATH.exists():
        try:
            _config = json.loads(CONFIG_PATH.read_text())
            OPEN_WEBUI_URL = _config.get("open_webui_url", OPEN_WEBUI_URL).rstrip("/")
            OPEN_WEBUI_TOKEN = _config.get("open_webui_token", OPEN_WEBUI_TOKEN)
            _kb_mappings = {k: v["kb_id"] for k, v in _config.get("mappings", {}).items()}
            log.info(f"  Config loaded: {len(_kb_mappings)} KB mapping(s)")
        except Exception as e:
            log.warning(f"Could not load config: {e}")
    else:
        log.info("  No config file found, using env vars for single KB mode")
        if KNOWLEDGE_BASE_ID:
            _kb_mappings["__default__"] = KNOWLEDGE_BASE_ID


def get_kb_for_file(path: Path) -> str:
    """Return KB ID for a given file based on its top-level folder."""
    try:
        rel = path.relative_to(WATCH_DIR)
        top_folder = rel.parts[0] if len(rel.parts) > 1 else "__default__"
        if top_folder in _kb_mappings:
            return _kb_mappings[top_folder]
    except Exception:
        pass
    return _kb_mappings.get("__default__", KNOWLEDGE_BASE_ID)


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def api_headers() -> dict:
    return {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}


# ── KB File Index ─────────────────────────────────────────────────────────────

def build_kb_index(kb_id: str) -> dict:
    """Returns {filename -> file_id} for all files in a KB."""
    try:
        r = requests.get(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{kb_id}/files",
            headers=api_headers(), timeout=15
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        index = {}
        for f in items:
            if isinstance(f, dict) and "meta" in f:
                name = f["meta"].get("name", "")
                if name.endswith(".md"):
                    index[name] = f["id"]
        return index
    except Exception as e:
        log.warning(f"Could not build KB index for {kb_id}: {e}")
        return {}


def get_kb_index(kb_id: str) -> dict:
    if kb_id not in _kb_file_index:
        _kb_file_index[kb_id] = build_kb_index(kb_id)
    return _kb_file_index[kb_id]


# ── Open WebUI API ────────────────────────────────────────────────────────────

def delete_file(file_id: str, filename: str) -> bool:
    try:
        r = requests.delete(
            f"{OPEN_WEBUI_URL}/api/v1/files/{file_id}",
            headers=api_headers(), timeout=10
        )
        r.raise_for_status()
        log.info(f"  🗑  Deleted old version: {filename}")
        return True
    except Exception as e:
        log.warning(f"  Could not delete {filename}: {e}")
        return False


def upload_file(path: Path) -> str | None:
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
        log.info(f"  ✅ Uploaded: {path.name} → {file_id}")
        return file_id
    except Exception as e:
        log.error(f"  ❌ Upload failed for {path.name}: {e}")
        return None


def add_to_kb(file_id: str, kb_id: str, filename: str) -> bool:
    try:
        r = requests.post(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
            headers={**api_headers(), "Content-Type": "application/json"},
            json={"file_id": file_id},
            timeout=30,
        )
        r.raise_for_status()
        log.info(f"  ✅ Added to KB: {filename}")
        return True
    except Exception as e:
        log.error(f"  ❌ Failed to add {filename} to KB: {e}")
        return False


# ── Core Ingest Logic ─────────────────────────────────────────────────────────

def ingest_file(path: Path) -> bool:
    if path.suffix.lower() not in ALLOWED_EXTS:
        return False

    kb_id = get_kb_for_file(path)
    if not kb_id:
        log.warning(f"  No KB mapped for {path.name} — skipping")
        return False

    kb_index = get_kb_index(kb_id)
    current_hash = file_hash(path)
    cached_hash = _file_hashes.get(str(path))

    # Already in KB and unchanged
    if path.name in kb_index and cached_hash == current_hash:
        log.debug(f"  ⏭  Unchanged, skipping: {path.name}")
        return False

    log.info(f"📄 Processing: {path.relative_to(WATCH_DIR)}")

    # Delete old version if it exists
    if path.name in kb_index:
        log.info(f"  🔄 File changed, replacing: {path.name}")
        delete_file(kb_index[path.name], path.name)
        del kb_index[path.name]

    # Upload new version
    time.sleep(0.5)
    file_id = upload_file(path)
    if not file_id:
        return False

    # Add to KB
    if not add_to_kb(file_id, kb_id, path.name):
        return False

    # Update index and hash cache
    kb_index[path.name] = file_id
    _file_hashes[str(path)] = current_hash
    return True


# ── Watchdog ──────────────────────────────────────────────────────────────────

class FileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        path = Path(event.src_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 New file: {path.name}")
            time.sleep(2)
            ingest_file(path)

    def on_modified(self, event):
        if event.is_directory: return
        path = Path(event.src_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 Modified: {path.name}")
            ingest_file(path)

    def on_moved(self, event):
        if event.is_directory: return
        path = Path(event.dest_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 Moved: {path.name}")
            time.sleep(2)
            ingest_file(path)


# ── Startup Scan ──────────────────────────────────────────────────────────────

def startup_scan():
    files = sorted(p for p in WATCH_DIR.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTS)
    if not files:
        log.info("🔍 Startup scan: no files found.")
        return

    log.info(f"🔍 Startup scan: {len(files)} file(s) to check...")
    success = skipped = failed = 0
    failed_files = []

    for path in files:
        kb_id = get_kb_for_file(path)
        if not kb_id:
            skipped += 1
            continue
        kb_index = get_kb_index(kb_id)
        current_hash = file_hash(path)
        if path.name in kb_index and _file_hashes.get(str(path)) == current_hash:
            skipped += 1
            continue
        result = ingest_file(path)
        if result:
            success += 1
        else:
            if path.name not in get_kb_index(kb_id):
                failed_files.append(path)
                failed += 1

    log.info(f"✅ Scan complete: {success} ingested, {skipped} skipped, {failed} failed.")

    if failed_files:
        log.info(f"🔄 Retrying {len(failed_files)} failed file(s) in 30s...")
        time.sleep(30)
        retry_ok = sum(1 for p in failed_files if ingest_file(p))
        log.info(f"✅ Retry complete: {retry_ok}/{len(failed_files)} ingested.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not WATCH_DIR.exists():
        log.error(f"Watch directory does not exist: {WATCH_DIR}")
        sys.exit(1)

    load_config()

    log.info("=" * 60)
    log.info("kb-ingestor v2 starting")
    log.info(f"  Watch dir:   {WATCH_DIR}")
    log.info(f"  Open WebUI:  {OPEN_WEBUI_URL}")
    log.info(f"  Config:      {CONFIG_PATH}")
    log.info(f"  KB mappings: {list(_kb_mappings.keys())}")
    log.info("=" * 60)

    startup_scan()

    handler = FileHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=True)
    observer.start()
    log.info(f"👁  Watching {WATCH_DIR} ...")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
