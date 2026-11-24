"""Mirror the exported Markdown tree into a Google Drive (Shared Drive) folder.

The uploader reconciles against the manifest instead of trusting one run's
output, so an interrupted upload simply resumes next time:

* ``hash`` (set by the exporter) vs ``uploaded_hash``  → upload/update the file
* ``path`` vs ``uploaded_path``                        → move/rename in Drive
* entries marked ``removed_at``                        → left in Drive (it's a backup)

The manifest itself is stored in the Drive folder too, so GitHub Actions (which
starts with an empty disk) can pull it, export only what changed, and push back.
"""
from __future__ import annotations

import io
import json
import logging
import posixpath
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .config import Config
from .notion_exporter import INDEX_NAME, MANIFEST_NAME, Manifest

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
RETRIES = 6
SAVE_EVERY = 250
UPLOAD_WORKERS = 4


def _credentials(config: Config):
    if config.gdrive_service_account_json:
        info = json.loads(config.gdrive_service_account_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return service_account.Credentials.from_service_account_file(
        config.gdrive_service_account_file, scopes=SCOPES)


class DriveUploader:
    def __init__(self, config: Config) -> None:
        config.require_drive()
        self.config = config
        self.root_id = config.gdrive_folder_id
        self.out = Path(config.output_dir)
        self._creds = _credentials(config)
        self._local = threading.local()
        self._lock = threading.RLock()
        self.calls = 0
        root = self._exec(self.service.files().get(
            fileId=self.root_id, supportsAllDrives=True, fields="id,driveId"))
        self.drive_id = root.get("driveId")
        # (parent_id, name) -> file id, and folder path -> folder id
        self._children: dict[tuple[str, str], str] = {}
        self._folders: dict[str, str] = {"": self.root_id}
        self._indexed = False

    @property
    def service(self):
        """One API client per thread (the underlying HTTP object isn't thread-safe)."""
        svc = getattr(self._local, "service", None)
        if svc is None:
            svc = build("drive", "v3", credentials=self._creds, cache_discovery=False)
            self._local.service = svc
        return svc

    # -- low level ---------------------------------------------------------
    def _exec(self, request):
        """Execute with retries for rate limits and transient errors."""
        for attempt in range(1, RETRIES + 1):
            try:
                with self._lock:
                    self.calls += 1
                return request.execute(num_retries=3)
            except HttpError as e:
                reason = ""
                try:
                    reason = json.loads(e.content)["error"]["errors"][0]["reason"]
                except Exception:
                    pass
                retryable = e.resp.status in (429, 500, 502, 503, 504) or reason in (
                    "rateLimitExceeded", "userRateLimitExceeded", "backendError")
                if not retryable or attempt == RETRIES:
                    raise
                wait = min(64, 2 ** attempt)
                log.warning("Drive %s %s; retrying in %ss", e.resp.status, reason, wait)
                time.sleep(wait)

    def _list_kwargs(self) -> dict:
        kw = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
        if self.drive_id:
            kw.update(corpora="drive", driveId=self.drive_id)
        return kw

    def _index(self) -> None:
        """Load every item in the backup folder once (~1 call per 1000 items)."""
        if self._indexed:
            return
        log.info("Indexing existing files in Drive…")
        items: list[dict] = []
        token = None
        while True:
            resp = self._exec(self.service.files().list(
                q="trashed = false", pageSize=1000, pageToken=token,
                fields="nextPageToken, files(id, name, mimeType, parents)",
                **self._list_kwargs()))
            items += resp.get("files", [])
            token = resp.get("nextPageToken")
            if not token:
                break
        by_id = {f["id"]: f for f in items}
        for f in items:
            for parent in f.get("parents", []):
                self._children[(parent, f["name"])] = f["id"]
        # Rebuild folder paths under the root.
        def path_of(fid: str, depth: int = 0) -> str | None:
            if fid == self.root_id:
                return ""
            f = by_id.get(fid)
            if not f or depth > 50 or not f.get("parents"):
                return None
            parent_path = path_of(f["parents"][0], depth + 1)
            return None if parent_path is None else posixpath.join(parent_path, f["name"])
        for f in items:
            if f["mimeType"] == FOLDER_MIME:
                p = path_of(f["id"])
                if p is not None:
                    self._folders[p] = f["id"]
        self._indexed = True
        log.info("Drive has %d items (%d folders) under the backup folder.",
                 len(items), len(self._folders) - 1)

    def _folder_id(self, folder: str) -> str:
        with self._lock:  # serialize so two threads never create the same folder
            return self._folder_id_locked(folder)

    def _folder_id_locked(self, folder: str) -> str:
        if folder in self._folders:
            return self._folders[folder]
        parent = self._folder_id_locked(posixpath.dirname(folder))
        name = posixpath.basename(folder)
        existing = self._children.get((parent, name))
        if existing:
            fid = existing
        else:
            fid = self._exec(self.service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
                fields="id", supportsAllDrives=True))["id"]
            self._children[(parent, name)] = fid
        self._folders[folder] = fid
        return fid

    def _file_id(self, rel_path: str) -> str | None:
        folder = posixpath.dirname(rel_path)
        if folder not in self._folders:
            return None
        return self._children.get((self._folders[folder], posixpath.basename(rel_path)))

    # -- public operations -------------------------------------------------
    def upload(self, rel_path: str) -> str:
        """Create or overwrite one file; returns 'created' or 'updated'."""
        self._index()
        local = self.out / rel_path
        mime = "application/json" if rel_path.endswith(".json") else "text/markdown"
        media = MediaFileUpload(str(local), mimetype=mime, resumable=False)
        with self._lock:
            existing = self._file_id(rel_path)
        if existing:
            self._exec(self.service.files().update(
                fileId=existing, media_body=media, supportsAllDrives=True, fields="id"))
            return "updated"
        parent = self._folder_id(posixpath.dirname(rel_path))
        name = posixpath.basename(rel_path)
        fid = self._exec(self.service.files().create(
            body={"name": name, "parents": [parent]}, media_body=media,
            fields="id", supportsAllDrives=True))["id"]
        with self._lock:
            self._children[(parent, name)] = fid
        return "created"

    def move(self, old: str, new: str) -> bool:
        """Move/rename a file (and its sibling child folder, if any)."""
        self._index()
        moved = False
        for src, dst in ((old, new), (old[:-3], new[:-3])):   # file, then folder
            fid = self._file_id(src) if src.endswith(".md") else self._folders.get(src)
            if not fid or self._file_id(dst) or dst in self._folders:
                continue
            old_parent = self._folder_id(posixpath.dirname(src))
            new_parent = self._folder_id(posixpath.dirname(dst))
            self._exec(self.service.files().update(
                fileId=fid, body={"name": posixpath.basename(dst)},
                addParents=new_parent if new_parent != old_parent else None,
                removeParents=old_parent if new_parent != old_parent else None,
                supportsAllDrives=True, fields="id"))
            self._children.pop((old_parent, posixpath.basename(src)), None)
            self._children[(new_parent, posixpath.basename(dst))] = fid
            if not src.endswith(".md"):
                # Re-key every cached folder under the moved one.
                for p in [p for p in self._folders if p == src or p.startswith(src + "/")]:
                    self._folders[dst + p[len(src):]] = self._folders.pop(p)
            moved = True
        return moved

    def pull_manifest(self) -> bool:
        """Download _manifest.json from Drive if there is no local copy."""
        local = self.out / MANIFEST_NAME
        if local.exists():
            return False
        self._index()
        fid = self._file_id(MANIFEST_NAME)
        if not fid:
            log.info("No manifest in Drive yet: this will be a full first backup.")
            return False
        self.out.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, self.service.files().get_media(
            fileId=fid, supportsAllDrives=True))
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=RETRIES)
        local.write_bytes(buf.getvalue())
        log.info("Downloaded manifest from Drive.")
        return True

    def sync(self, manifest: Manifest, limit: int | None = None) -> dict:
        """Reconcile Drive with the manifest. Returns counts."""
        self._index()
        stats = {"created": 0, "updated": 0, "moved": 0, "missing_local": 0, "failed": 0}
        # 1) Moves, shallowest first, so a moved folder carries its children along.
        entries = sorted(((k, e) for k, e in manifest.entries.items()
                          if not e.get("removed_at") and "path" in e),
                         key=lambda kv: kv[1]["path"].count("/"))
        for _, e in entries:
            up_path = e.get("uploaded_path")
            if up_path and up_path != e["path"]:
                try:
                    if self.move(up_path, e["path"]):
                        stats["moved"] += 1
                    e["uploaded_path"] = e["path"]
                except HttpError as err:
                    stats["failed"] += 1
                    log.error("Drive move failed for %s: %s", e["path"], err)
        # 2) Uploads, several at a time.
        pending = []
        for _, e in entries:
            if e.get("uploaded_hash") == e.get("hash") and e.get("uploaded_path"):
                continue
            if not (self.out / e["path"]).exists():
                stats["missing_local"] += 1
                continue
            pending.append(e)
        if limit is not None:
            pending = pending[:limit]
        if pending:
            log.info("Uploading %d files to Drive…", len(pending))
        done = 0
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            futures = {pool.submit(self.upload, e["path"]): e for e in pending}
            for fut in as_completed(futures):
                e = futures[fut]
                try:
                    stats[fut.result()] += 1
                    e["uploaded_hash"], e["uploaded_path"] = e.get("hash"), e["path"]
                except HttpError as err:
                    stats["failed"] += 1
                    log.error("Drive upload failed for %s: %s", e["path"], err)
                done += 1
                if done % SAVE_EVERY == 0:
                    manifest.save()
                    self.upload(MANIFEST_NAME)
                    log.info("  …%d/%d files uploaded (%d Drive calls)",
                             done, len(pending), self.calls)
        if (self.out / INDEX_NAME).exists():
            self.upload(INDEX_NAME)
        manifest.save()
        self.upload(MANIFEST_NAME)
        return stats
