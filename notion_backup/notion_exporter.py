"""Incremental Notion → Markdown exporter.

How it works
1. Discover: list every page and database the connection can see (search +
   a query of each database, so nothing is missed). This only reads page
   metadata, so it is cheap (~1 request per 100 pages).
2. Build the tree: each page/database gets a stable path that mirrors Notion,
   e.g. ``Business Development (1a2b3c4d)/Master Open Items (9f8e7d6c)/Row (…).md``.
3. Diff against the manifest (``_manifest.json``): only pages whose
   ``last_edited_time`` changed (or that are new) have their content fetched.
   Moves/renames are recorded without re-downloading.
4. Write Markdown files and return what changed, for the Drive uploader.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notion_client import APIErrorCode, APIResponseError

from . import markdown as md
from .config import Config
from .notion_api import NotionAPI

log = logging.getLogger(__name__)

MANIFEST_NAME = "_manifest.json"
INDEX_NAME = "_backup_index.md"
MANIFEST_VERSION = 1
# Bump whenever Markdown output changes, so every page is re-rendered once.
RENDER_VERSION = 2
SAVE_EVERY = 200
WORKERS = 3

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _uuid(i: str) -> str:
    return i.replace("-", "").lower()


def _sanitize(title: str, limit: int = 80) -> str:
    name = _BAD_CHARS.sub(" ", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rstrip(" .") + "…"
    return name or "Untitled"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Node:
    id: str                       # 32-hex, no dashes
    kind: str                     # "page" | "data_source"
    title: str
    obj: dict
    parent: str | None = None     # resolved parent node id (None = top level)
    children: list[str] = field(default_factory=list)
    path: str = ""                # relative .md path (posix)

    @property
    def last_edited(self) -> str:
        return self.obj.get("last_edited_time", "")

    @property
    def is_row(self) -> bool:
        return self.obj.get("parent", {}).get("type") in ("data_source_id", "database_id")

    @property
    def folder(self) -> str:
        return self.path[:-3]  # children live in a folder named like the file


@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)          # paths created/updated
    moved: list[tuple[str, str]] = field(default_factory=list)  # (old, new)
    removed: list[str] = field(default_factory=list)          # gone from Notion (file kept)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (title, error)
    unchanged: int = 0
    remaining: int = 0          # skipped because of --limit / time budget
    total_pages: int = 0
    total_databases: int = 0
    requests: int = 0
    seconds: float = 0.0


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self.meta: dict[str, Any] = {}
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
            if data.get("version") == MANIFEST_VERSION:
                self.entries = data.get("entries", {})
                self.meta = data.get("meta", {})
        self._lock = threading.Lock()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": MANIFEST_VERSION, "meta": self.meta, "entries": self.entries},
                ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.path)

    def set(self, node_id: str, **fields: Any) -> None:
        with self._lock:
            entry = self.entries.setdefault(node_id, {})
            entry.update(fields)
            entry.pop("removed_at", None)


# ---------------------------------------------------------------------------
# Link / title resolver used by the Markdown renderer
# ---------------------------------------------------------------------------
class _Resolver:
    def __init__(self, exporter: "NotionExporter", current_path: str) -> None:
        self.ex = exporter
        self.base = posixpath.dirname(current_path)

    def _node(self, notion_id: str) -> Node | None:
        i = _uuid(notion_id)
        return self.ex.nodes.get(i) or self.ex.nodes.get(self.ex.db_to_ds.get(i, ""))

    def title(self, notion_id: str) -> str | None:
        n = self._node(notion_id)
        return n.title if n else None

    def link(self, notion_id: str) -> str | None:
        n = self._node(notion_id)
        if not n:
            return f"https://www.notion.so/{_uuid(notion_id)}"
        return "<" + posixpath.relpath(n.path, self.base or ".") + ">"

    def user(self, user_id: str) -> str | None:
        return self.ex.users.get(_uuid(user_id))


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------
class NotionExporter:
    def __init__(self, config: Config, api: NotionAPI | None = None) -> None:
        self.config = config
        self.api = api or NotionAPI(config.notion_api_key)
        self.out = Path(config.output_dir)
        self.manifest = Manifest(self.out / MANIFEST_NAME)
        self.nodes: dict[str, Node] = {}
        self.db_to_ds: dict[str, str] = {}     # database id → first data source id
        self.users: dict[str, str] = {}
        self._block_parent_cache: dict[str, str | None] = {}

    # -- 1. discovery ------------------------------------------------------
    def discover(self) -> None:
        log.info("Discovering pages and databases…")
        for obj in self.api.search_all():
            self._add(obj)
        # Query each database too: search can lag behind very recent edits.
        for ds in [n for n in self.nodes.values() if n.kind == "data_source"]:
            try:
                for row in self.api.query_data_source(ds.id):
                    self._add(row)
            except APIResponseError as e:
                log.warning("Could not query database %r: %s", ds.title, e.code)
        try:
            for u in self.api.list_users():
                if u.get("name"):
                    self.users[_uuid(u["id"])] = u["name"]
        except APIResponseError as e:
            log.info("User names unavailable (%s); people fields will show IDs.", e.code)
        log.info("Found %d pages and %d databases (%d API requests).",
                 sum(n.kind == "page" for n in self.nodes.values()),
                 sum(n.kind == "data_source" for n in self.nodes.values()),
                 self.api.request_count)

    def _add(self, obj: dict) -> None:
        if obj.get("in_trash") or obj.get("archived") or obj.get("is_archived"):
            return
        kind = obj.get("object")
        if kind not in ("page", "data_source"):
            return
        node_id = _uuid(obj["id"])
        existing = self.nodes.get(node_id)
        if existing and existing.last_edited >= obj.get("last_edited_time", ""):
            return
        self.nodes[node_id] = Node(node_id, kind, md.page_title(obj), obj)
        if kind == "data_source":
            db_id = _uuid(obj.get("parent", {}).get("database_id", ""))
            if db_id:
                self.db_to_ds.setdefault(db_id, node_id)

    # -- 2. tree + paths ----------------------------------------------------
    def build_tree(self) -> None:
        for n in self.nodes.values():
            n.parent = self._resolve_parent(n)
        for n in self.nodes.values():
            if n.parent:
                self.nodes[n.parent].children.append(n.id)
        if self.config.page_ids or self.config.database_ids:
            self._apply_filter()
        for n in self.nodes.values():
            if n.parent is None:
                self._assign_paths(n, "")

    def _resolve_parent(self, n: Node) -> str | None:
        parent = n.obj.get("database_parent") if n.kind == "data_source" else n.obj.get("parent")
        parent = parent or {}
        ptype = parent.get("type")
        if ptype == "workspace" or not ptype:
            return None
        if ptype == "data_source_id":
            pid = _uuid(parent["data_source_id"])
        elif ptype == "database_id":
            pid = self.db_to_ds.get(_uuid(parent["database_id"]), "")
        elif ptype == "page_id":
            pid = _uuid(parent["page_id"])
        elif ptype == "block_id":
            pid = self._page_above_block(_uuid(parent["block_id"])) or ""
        else:
            return None
        return pid if pid in self.nodes and pid != n.id else None

    def _page_above_block(self, block_id: str, hops: int = 8) -> str | None:
        """Walk up from a block (column, synced block…) to the page containing it."""
        if block_id in self._block_parent_cache:
            return self._block_parent_cache[block_id]
        result = None
        current = block_id
        for _ in range(hops):
            try:
                blk = self.api.retrieve_block(current)
            except APIResponseError:
                break
            p = blk.get("parent", {})
            if p.get("type") == "page_id":
                result = _uuid(p["page_id"])
                break
            if p.get("type") != "block_id":
                break
            current = _uuid(p["block_id"])
        self._block_parent_cache[block_id] = result
        return result

    def _apply_filter(self) -> None:
        wanted = set(self.config.page_ids)
        wanted |= {self.db_to_ds.get(i, i) for i in self.config.database_ids}
        keep: set[str] = set()
        stack = [i for i in wanted if i in self.nodes]
        missing = wanted - set(stack)
        if missing:
            log.warning("Filter IDs not visible to the connection: %s", ", ".join(sorted(missing)))
        while stack:
            i = stack.pop()
            if i not in keep:
                keep.add(i)
                stack.extend(self.nodes[i].children)
        self.nodes = {i: n for i, n in self.nodes.items() if i in keep}
        for n in self.nodes.values():
            if n.parent not in self.nodes:
                n.parent = None

    def _assign_paths(self, n: Node, folder: str) -> None:
        name = f"{_sanitize(n.title)} ({n.id[-8:]})"
        n.path = posixpath.join(folder, name + ".md") if folder else name + ".md"
        for c in n.children:
            self._assign_paths(self.nodes[c], n.folder)

    # -- 3/4. export ----------------------------------------------------------
    def run(self, full: bool = False, limit: int | None = None,
            time_budget_s: float | None = None) -> ExportResult:
        started = time.monotonic()
        deadline = started + time_budget_s if time_budget_s else None
        result = ExportResult()

        self.discover()
        self.build_tree()
        self.out.mkdir(parents=True, exist_ok=True)

        pages = [n for n in self.nodes.values() if n.kind == "page"]
        dbs = [n for n in self.nodes.values() if n.kind == "data_source"]
        result.total_pages, result.total_databases = len(pages), len(dbs)

        # Moves (path changed, content didn't) are handled without downloading.
        for n in self.nodes.values():
            old = self.manifest.entries.get(n.id, {}).get("path")
            if old and old != n.path:
                self._move_local(old, n.path)
                result.moved.append((old, n.path))
                self.manifest.set(n.id, path=n.path)

        todo = [n for n in pages if full or self._needs_export(n)]
        result.unchanged = len(pages) - len(todo)
        # Regular pages first, then database rows (small databases first).
        size = {d.id: len(d.children) for d in dbs}
        todo.sort(key=lambda n: (n.is_row, size.get(n.parent or "", 0), n.path))
        if limit is not None and len(todo) > limit:
            result.remaining += len(todo) - limit
            todo = todo[:limit]
        log.info("%d pages to export, %d unchanged.", len(todo), result.unchanged)

        done = 0
        stop = threading.Event()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(self._export_page_guarded, n, deadline, stop): n for n in todo}
            for fut in as_completed(futures):
                n = futures[fut]
                status, info = fut.result()
                if status == "written":
                    result.written.append(n.path)
                elif status == "same":
                    result.unchanged += 1
                elif status == "skipped":
                    result.remaining += 1
                elif status == "failed":
                    result.failed.append((n.title or n.id, info))
                done += 1
                if done % SAVE_EVERY == 0:
                    self.manifest.save()
                    log.info("  …%d/%d pages processed (%d API requests)",
                             done, len(todo), self.api.request_count)

        # Database index pages are built from metadata already in memory.
        for d in dbs:
            if self._write_if_changed(d, self._render_database(d)):
                result.written.append(d.path)

        if not (self.config.page_ids or self.config.database_ids):
            for node_id, entry in self.manifest.entries.items():
                if node_id not in self.nodes and not entry.get("removed_at"):
                    entry["removed_at"] = _now()
                    result.removed.append(entry.get("path", node_id))

        result.requests = self.api.request_count
        result.seconds = time.monotonic() - started
        self.manifest.meta.update(last_run=_now(), pages=len(pages), databases=len(dbs))
        index = self._render_index(result)
        (self.out / INDEX_NAME).write_text(index, "utf-8")
        result.written.append(INDEX_NAME)
        self.manifest.save()
        return result

    def _needs_export(self, n: Node) -> bool:
        e = self.manifest.entries.get(n.id)
        if not e or e.get("last_edited") != n.last_edited or "hash" not in e \
                or bool(e.get("removed_at")) or e.get("render") != RENDER_VERSION:
            return True
        # Exported but never uploaded, and the file is gone (e.g. a CI run was
        # killed mid-upload and its disk wiped): export it again so it uploads.
        return e.get("uploaded_hash") != e["hash"] and not (self.out / n.path).exists()

    def _export_page_guarded(self, n: Node, deadline: float | None,
                             stop: threading.Event) -> tuple[str, str]:
        if stop.is_set() or (deadline and time.monotonic() > deadline):
            stop.set()
            return "skipped", ""
        try:
            content = self._render_page(n)
        except APIResponseError as e:
            if e.code in (APIErrorCode.ObjectNotFound, APIErrorCode.RestrictedResource):
                return "failed", f"{e.code} (page removed or access revoked?)"
            return "failed", f"{e.code}: {e}"
        except Exception as e:  # keep going; report at the end
            log.exception("Failed to export %s", n.path)
            return "failed", f"{type(e).__name__}: {e}"
        return ("written", "") if self._write_if_changed(n, content) else ("same", "")

    def _write_if_changed(self, n: Node, content: str) -> bool:
        digest = _sha(content)
        entry = self.manifest.entries.get(n.id, {})
        target = self.out / n.path
        changed = entry.get("hash") != digest or entry.get("path") != n.path
        if changed or not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")
        self.manifest.set(n.id, kind=n.kind, title=n.title, path=n.path,
                          last_edited=n.last_edited, hash=digest, render=RENDER_VERSION)
        return changed

    def _move_local(self, old: str, new: str) -> None:
        src, dst = self.out / old, self.out / new
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        # Tidy now-empty folders left behind.
        d = src.parent
        while d != self.out and d.exists() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    # -- rendering --------------------------------------------------------
    def _fetch_tree(self, block_id: str, depth: int = 0) -> list[dict]:
        blocks = self.api.block_children(block_id)
        for b in blocks:
            if b["type"] in ("child_page", "child_database") or depth > 12:
                continue
            source = b["id"]
            if b["type"] == "transcription":
                # Meeting notes keep summary / notes / transcript in linked blocks.
                sections = []
                refs = (b.get("transcription") or {}).get("children") or {}
                for key, ref in refs.items():
                    if not ref:
                        continue
                    label = key.replace("_block_id", "").replace("_", " ").title()
                    try:
                        sections.append((label, self._fetch_tree(_uuid(ref), depth + 1)))
                    except APIResponseError as e:
                        log.debug("Meeting section %s unavailable: %s", label, e.code)
                b["_sections"] = sections
                continue
            if b["type"] == "synced_block":
                orig = (b.get("synced_block") or {}).get("synced_from") or {}
                source = orig.get("block_id") or source
                if source != b["id"]:
                    b["_children"] = self._fetch_tree(source, depth + 1)
                    continue
            if b.get("has_children"):
                b["_children"] = self._fetch_tree(source, depth + 1)
        return blocks

    def _render_page(self, n: Node) -> str:
        res = _Resolver(self, n.path)
        body = md.render_blocks(self._fetch_tree(n.id), res)
        obj = n.obj
        props = None
        parent_db = self.nodes.get(n.parent or "") if n.is_row else None
        if n.is_row:
            props = {}
            for name, p in obj.get("properties", {}).items():
                if p.get("type") == "title":
                    continue
                props[name] = md.property_value(p, res)
        meta = {
            "title": n.title or "Untitled",
            "notion_id": obj["id"],
            "notion_url": obj.get("url"),
            "database": parent_db.title if parent_db else None,
            "created": obj.get("created_time"),
            "last_edited": obj.get("last_edited_time"),
            "created_by": res.user(obj.get("created_by", {}).get("id", "")),
            "last_edited_by": res.user(obj.get("last_edited_by", {}).get("id", "")),
        }
        icon = md._icon(obj.get("icon"))
        heading = f"# {icon + ' ' if icon else ''}{n.title or 'Untitled'}"
        parts = [md.front_matter(meta, props), heading]
        if body:
            parts.append(body)
        return "\n\n".join(parts) + "\n"

    def _render_database(self, d: Node) -> str:
        res = _Resolver(self, d.path)
        schema = d.obj.get("properties", {})
        # Index table: title + up to 7 short, scannable columns.
        short = ("select", "status", "multi_select", "date", "number", "checkbox",
                 "people", "unique_id", "formula")
        cols = [k for k, v in schema.items() if v.get("type") in short][:7]
        rows = sorted((self.nodes[c] for c in d.children if self.nodes[c].kind == "page"),
                      key=lambda r: r.title.lower())
        meta = {
            "title": d.title or "Untitled database",
            "type": "database",
            "notion_id": d.obj["id"],
            "notion_url": d.obj.get("url"),
            "rows": len(rows),
            "last_edited": d.last_edited,
        }
        out = [md.front_matter(meta), f"# 🗂 {d.title or 'Untitled database'}"]
        desc = md.rich_text(d.obj.get("description"), res)
        if desc:
            out.append(desc)
        out.append("## Fields\n\n" + "\n".join(
            f"- **{k}** — {v.get('type')}" for k, v in schema.items()))
        sub = [self.nodes[c] for c in d.children if self.nodes[c].kind == "data_source"]
        if sub:
            out.append("## Sub-databases\n\n" + "\n".join(
                f"- [{s.title}]({res.link(s.id)})" for s in sub))
        header = "| Name | " + " | ".join(md.table_cell(c) for c in cols) + " |"
        lines = [header, "|---|" + "---|" * len(cols)]
        for r in rows:
            props = r.obj.get("properties", {})
            cells = [md.table_cell(md.property_value(props[c], res)) if c in props else ""
                     for c in cols]
            name = md.table_cell(r.title or "Untitled")
            lines.append(f"| [{name}]({res.link(r.id)}) | " + " | ".join(cells) + " |")
        out.append(f"## Entries ({len(rows)})\n\n" + "\n".join(lines))
        return "\n\n".join(out) + "\n"

    def _render_index(self, r: ExportResult) -> str:
        res = _Resolver(self, INDEX_NAME)
        roots = sorted((n for n in self.nodes.values() if n.parent is None),
                       key=lambda n: (n.kind, n.title.lower()))
        dbs = sorted((n for n in self.nodes.values() if n.kind == "data_source"),
                     key=lambda n: -len(n.children))
        lines = [
            "# Notion backup index",
            "",
            f"Last run: {self.manifest.meta.get('last_run', _now())} — "
            f"{r.total_pages} pages, {r.total_databases} databases.",
            "",
            "## Top-level pages",
            "",
            "Anything not listed here (or nested under these) is **not** shared with the "
            "backup connection. Add new top-level pages in Notion → connection → Content access.",
            "",
        ]
        lines += [f"- [{n.title or 'Untitled'}]({res.link(n.id)})" for n in roots]
        lines += ["", "## Databases", ""]
        lines += [f"- [{d.title or 'Untitled'}]({res.link(d.id)}) — {len(d.children)} entries"
                  for d in dbs]
        return "\n".join(lines) + "\n"
