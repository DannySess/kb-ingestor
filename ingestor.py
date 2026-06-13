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

_kb_files = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("kb-ingestor")

OPEN_WEBUI_URL    = os.environ["OPEN_WEBUI_URL"].rstrip("/")
OPEN_WEBUI_TOKEN  = os.environ["OPEN_WEBUI_TOKEN"]
KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
WATCH_DIR         = os.environ.get("WATCH_DIR", "/watch")
ALLOWED_EXTS      = {e.strip().lower() for e in os.environ.get("ALLOWED_EXTENSIONS", ".md").split(",")}

def api_headers():
    return {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}

def get_kb_filenames():
    try:
        r = requests.get(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{KNOWLEDGE_BASE_ID}/files",
            headers=api_headers(), timeout=10
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            items = data.get("items", data) if isinstance(data, dict) else data
            return {f["meta"]["name"] for f in items if isinstance(f, dict) and "meta" in f and f["meta"].get("name", "").endswith(".md")}
        return set()
    except Exception as e:
        log.warning(f"Could not fetch KB files: {e}")
        return set()

def file_already_uploaded(filename):
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

def upload_file(path):
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

def add_file_to_knowledge_base(file_id, filename):
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

def ingest_file(path):
    if path.suffix.lower() not in ALLOWED_EXTS:
        return False
    log.info(f"📄 Processing: {path.name}")
    if path.name in _kb_files:
        log.info(f"  ⏭  Already in KB, skipping: {path.name}")
        return False
    file_id = upload_file(path)
    if not file_id:
        return False
    result = add_file_to_knowledge_base(file_id, path.name)
    if result:
        _kb_files.add(path.name)
    return result

class MarkdownHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        path = Path(event.src_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 New file detected: {path.name}")
            time.sleep(2)
            ingest_file(path)
    def on_moved(self, event):
        if event.is_directory: return
        path = Path(event.dest_path)
        if path.suffix.lower() in ALLOWED_EXTS:
            log.info(f"🔔 File moved into watch dir: {path.name}")
            time.sleep(2)
            ingest_file(path)

def startup_scan(watch_dir):
    global _kb_files
    _kb_files = get_kb_filenames()
    log.info(f"  KB already contains {len(_kb_files)} file(s)")
    files = sorted(p for p in watch_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTS)
    if not files:
        log.info("🔍 Startup scan: no existing files found.")
        return
    log.info(f"🔍 Startup scan: found {len(files)} file(s) to process...")
    success = 0
    failed = []
    for path in files:
        if ingest_file(path):
            success += 1
        elif path.name not in _kb_files:
            failed.append(path)
    log.info(f"✅ Startup scan complete: {success}/{len(files)} file(s) ingested.")
    if failed:
        log.info(f"🔄 Retrying {len(failed)} failed file(s) in 30 seconds...")
        time.sleep(30)
        retry_success = 0
        for path in failed:
            if path.name not in _kb_files and ingest_file(path):
                retry_success += 1
        log.info(f"✅ Retry complete: {retry_success}/{len(failed)} file(s) ingested.")

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
    startup_scan(watch_dir)
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
