#!/usr/bin/env python3
"""
kb-ingestor v6: Multi-KB routing, file update detection, config-driven.
KB-index only dedup — no stale file store IDs.
Persistent hash cache survives restarts.
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kb-ingestor")

WATCH_DIR     = Path(os.environ.get("WATCH_DIR", "/watch"))
CONFIG_PATH   = Path(os.environ.get("CONFIG_PATH", "/config/kb_config.json"))
ALLOWED_EXTS  = {e.strip().lower() for e in os.environ.get("ALLOWED_EXTENSIONS", ".md").split(",")}

OPEN_WEBUI_URL    = os.environ.get("OPEN_WEBUI_URL", "").rstrip("/")
OPEN_WEBUI_TOKEN  = os.environ.get("OPEN_WEBUI_TOKEN", "")
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID", "")

_config      = {}
_kb_mappings = {}   # folder -> kb_id
_kb_index    = {}   # kb_id -> {filename -> file_id}
_file_hashes = {}   # str(path) -> md5

HASH_CACHE_PATH = Path(os.environ.get("CONFIG_PATH", "/config/kb_config.json")).parent / "kb_hashes.json"


def load_hash_cache():
    global _file_hashes
    try:
        if HASH_CACHE_PATH.exists():
            _file_hashes = json.loads(HASH_CACHE_PATH.read_text())
            log.info(f"  Hash cache loaded: {len(_file_hashes)} entries")
        else:
            log.info("  No hash cache found, starting fresh")
    except Exception as e:
        log.warning(f"  Could not load hash cache: {e}")
        _file_hashes = {}


def save_hash_cache():
    try:
        HASH_CACHE_PATH.write_text(json.dumps(_file_hashes))
    except Exception as e:
        log.warning(f"  Could not save hash cache: {e}")


def load_config():
    global _config, _kb_mappings, OPEN_WEBUI_URL, OPEN_WEBUI_TOKEN
    if CONFIG_PATH.exists():
        try:
            _config = json.loads(CONFIG_PATH.read_text())
            OPEN_WEBUI_URL   = _config.get("open_webui_url", OPEN_WEBUI_URL).rstrip("/")
            OPEN_WEBUI_TOKEN = _config.get("open_webui_token", OPEN_WEBUI_TOKEN)
            _kb_mappings     = {k: v["kb_id"] for k, v in _config.get("mappings", {}).items()}
            log.info(f"  Config loaded: {len(_kb_mappings)} KB mapping(s)")
        except Exception as e:
            log.warning(f"Could not load config: {e}")
    else:
        log.info("  No config file — using env vars")
        if KNOWLEDGE_BASE_ID:
            _kb_mappings["__default__"] = KNOWLEDGE_BASE_ID


def api_headers():
    return {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}


def get_kb_for_file(path: Path) -> str:
    try:
        rel = path.relative_to(WATCH_DIR)
        top = rel.parts[0] if len(rel.parts) > 1 else "__default__"
        if top in _kb_mappings:
            return _kb_mappings[top]
    except Exception:
        pass
    return _kb_mappings.get("__default__", KNOWLEDGE_BASE_ID)


def file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def build_kb_index(kb_id: str) -> dict:
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
                if name:
                    index[name] = f["id"]
        return index
    except Exception as e:
        log.warning(f"Could not build KB index for {kb_id}: {e}")
        return {}


def get_kb_index(kb_id: str) -> dict:
    if kb_id not in _kb_index:
        _kb_index[kb_id] = build_kb_index(kb_id)
    return _kb_index[kb_id]


def delete_file(file_id: str, filename: str):
    try:
        r = requests.delete(
            f"{OPEN_WEBUI_URL}/api/v1/files/{file_id}",
            headers=api_headers(), timeout=10
        )
        r.raise_for_status()
        log.info(f"  🗑  Deleted: {filename}")
    except Exception as e:
        log.warning(f"  Could not delete {filename}: {e}")


def upload_file(path: Path) -> str | None:
    try:
        time.sleep(0.5)
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
    delays = [3, 8, 20]
    for attempt, delay in enumerate(delays + [None], 1):
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
            if delay is None:
                log.error(f"  ❌ Failed to add {filename} to KB after {len(delays)+1} attempts: {e}")
                return False
            log.warning(f"  ⏳ KB add failed (attempt {attempt}), retrying in {delay}s...")
            time.sleep(delay)
    return False


def ingest_file(path: Path) -> bool:
    if path.suffix.lower() not in ALLOWED_EXTS:
        return False

    kb_id = get_kb_for_file(path)
    if not kb_id:
        log.warning(f"  No KB mapped for {path.name} — skipping")
        return False

    kb_idx    = get_kb_index(kb_id)
    curr_hash = file_hash(path)
    prev_hash = _file_hashes.get(str(path))

    # Already in KB and unchanged
    if path.name in kb_idx and prev_hash == curr_hash:
        log.debug(f"  ⏭  Unchanged in KB, skipping: {path.name}")
        return False

    log.info(f"📄 Processing: {path.relative_to(WATCH_DIR)}")

    # Delete old version if exists in KB
    if path.name in kb_idx:
        log.info(f"  🔄 File changed, replacing: {path.name}")
        delete_file(kb_idx[path.name], path.name)
        del kb_idx[path.name]

    # Always upload fresh
    file_id = upload_file(path)
    if not file_id:
        return False

    if add_to_kb(file_id, kb_id, path.name):
        kb_idx[path.name] = file_id
        _file_hashes[str(path)] = curr_hash
        save_hash_cache()
        return True

    # KB add failed — clean up the uploaded file
    log.warning(f"  🧹 Cleaning up failed upload: {path.name}")
    delete_file(file_id, path.name)
    return False


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


def startup_scan():
    files = sorted(p for p in WATCH_DIR.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTS)
    if not files:
        log.info("🔍 No files found.")
        return

    log.info(f"🔍 Startup scan: {len(files)} file(s) to check...")
    success = skipped = failed = 0
    failed_files = []

    for path in files:
        kb_id = get_kb_for_file(path)
        if not kb_id:
            skipped += 1
            continue
        kb_idx    = get_kb_index(kb_id)
        curr_hash = file_hash(path)
        if path.name in kb_idx and _file_hashes.get(str(path)) == curr_hash:
            skipped += 1
            continue
        if ingest_file(path):
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


def main():
    if not WATCH_DIR.exists():
        log.error(f"Watch directory does not exist: {WATCH_DIR}")
        sys.exit(1)

    load_config()
    load_hash_cache()

    log.info("=" * 60)
    log.info("kb-ingestor v6 starting")
    log.info(f"  Watch dir:   {WATCH_DIR}")
    log.info(f"  Open WebUI:  {OPEN_WEBUI_URL}")
    log.info(f"  Config:      {CONFIG_PATH}")
    log.info(f"  KB mappings: {list(_kb_mappings.keys())}")
    log.info("=" * 60)

    log.info("⏳ Waiting 30s for services to be ready...")
    time.sleep(30)
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
