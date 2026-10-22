"""Note indexer — file scanning, frontmatter parsing, ChromaDB sync."""

from __future__ import annotations
import hashlib
import logging
import os
import re
import threading
import time
import yaml
import frontmatter
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .store import VectorStore

logger = logging.getLogger(__name__)

# Tracks whether any indexing work (startup sync, periodic sync, or file-watcher
# reindex) is currently in flight, for reporting via /health. A counter (not a
# bool) so overlapping callers — e.g. periodic sync racing a watcher event —
# don't clear the flag while another is still running.
_indexing_lock = threading.Lock()
_indexing_count = 0


def is_indexing() -> bool:
    with _indexing_lock:
        return _indexing_count > 0


def _tracks_indexing(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        global _indexing_count
        with _indexing_lock:
            _indexing_count += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with _indexing_lock:
                _indexing_count -= 1
    return wrapper

_LARGE_SYNC = 100    # warn at WARNING level when syncing this many files or more
_ENCODE_CHUNK = 500  # sub-batch size for progress visibility during encoding
_PROGRESS_INTERVAL = 10.0  # seconds between INFO progress lines
_CACHE_MAX_AGE_DAYS = 30   # embedding-cache entries unused this long are pruned after a sync
_CHUNK_THRESHOLD = 150  # split docs with more words than this into per-section chunks


def file_id(path: Path, base: Path) -> str:
    return hashlib.sha256(str(path.relative_to(base)).encode()).hexdigest()


def chunk_id(fid: str, idx: int) -> str:
    return f"{fid}:{idx}"


def _parse_tags(raw) -> list[str]:
    """Normalize tags — accept list or comma string, strip leading #."""
    if not raw:
        return []
    if isinstance(raw, str):
        items = [t.strip() for t in raw.split(",")]
    else:
        items = [str(t).strip() for t in raw]
    return [t.lstrip("#") for t in items if t]


def _split_content(title: str, content: str) -> list[tuple[str, str, str]]:
    """Split content into (heading, embed_text, display_text) chunks.

    Short docs (≤ _CHUNK_THRESHOLD words) → single chunk.
    Long docs → one chunk per ## / ### section; heading prefixed in embed_text
    for better embedding context.
    """
    if len(content.split()) <= _CHUNK_THRESHOLD:
        return [(title, f"{title}\n{content}", content)]

    sections = re.split(r"\n(?=#{1,3} )", content)
    chunks: list[tuple[str, str, str]] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        first, _, rest = section.partition("\n")
        if first.startswith("#"):
            heading = first.lstrip("#").strip()
            body = rest.strip()
        else:
            heading = title
            body = section
        if body:
            embed_text = f"{title} — {heading}\n{body}"
            chunks.append((heading, embed_text, body))

    return chunks if len(chunks) > 1 else [(title, f"{title}\n{content}", content)]


def _parse_chunks(path: Path, index_dir: Path, write_dir: Path | None = None) -> list[tuple[str, str, str, dict]] | None:
    """Parse a .md file into chunks. Returns list of (id, embed_text, document, metadata) or None."""
    try:
        post = frontmatter.load(str(path))
    except Exception:
        try:
            post = frontmatter.Post(path.read_text(errors="replace"))
        except Exception:
            return None

    title = post.get("problem") or post.get("title")
    if not title:
        for line in post.content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break
    title = str(title) if title else path.stem

    tags = _parse_tags(post.get("tags", []))
    rel = path.relative_to(index_dir)

    if write_dir is not None and path.is_relative_to(write_dir):
        rel_to_write = path.relative_to(write_dir)
        parts = rel_to_write.parts[:-1]
        category = "/".join(parts) if parts else "root"
    else:
        category = str(rel.parts[0]) if len(rel.parts) > 1 else "root"
        category = str(post.get("category", category))
    fid = file_id(path, index_dir)
    mtime = str(path.stat().st_mtime)

    results = []
    for idx, (heading, embed_text, display_text) in enumerate(_split_content(title, post.content)):
        meta = {
            "file_id": fid,
            "title": title,
            "chunk_heading": heading,
            "chunk_idx": idx,
            "tags": ",".join(tags),
            "category": category,
            "path": str(rel),
            "mtime": mtime,
        }
        results.append((chunk_id(fid, idx), embed_text, display_text, meta))
    return results


@_tracks_indexing
def index_file(path: Path, index_dir: Path, store: VectorStore, write_dir: Path | None = None) -> None:
    """Index a single .md file into the vector store (used by file watcher)."""
    chunks = _parse_chunks(path, index_dir, write_dir)
    if chunks is None:
        return
    fid = file_id(path, index_dir)
    # Delete old-style single entry (pre-chunking) and any existing new-style chunks
    store.delete(fid)
    for old_id in store.get_by_file_id(fid):
        store.delete(old_id)
    embeddings = store.encode_cached([embed_text for _, embed_text, _, _ in chunks])
    for (cid, _, document, meta), embedding in zip(chunks, embeddings):
        store.upsert(cid, embedding, document, meta)
    logger.debug("Indexed %d chunk(s): %s", len(chunks), path.relative_to(index_dir))


def _should_skip(path: Path) -> bool:
    """Return True if any component of path is a hidden directory."""
    return any(part.startswith(".") for part in path.parts)


# Serializes sync() — rows are written only at the end of a sync, so a second sync
# started meanwhile (periodic worker during a long startup reindex) would see every
# file as dirty and re-encode the whole vault in parallel.
_sync_lock = threading.Lock()


def sync(index_dir: Path, store: VectorStore, write_dir: Path | None = None) -> int | None:
    """Sync all .md files under index_dir. Returns count of updated files,
    or None when skipped because another sync is already running.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.info("Sync already running — skipped")
        return None
    try:
        return _sync(index_dir, store, write_dir)
    finally:
        _sync_lock.release()


@_tracks_indexing
def _sync(index_dir: Path, store: VectorStore, write_dir: Path | None) -> int:
    """Supports chunked documents (multiple ChromaDB entries per file). Removes
    entries for deleted files and re-indexes changed files with all their chunks.
    """
    all_ids, all_metas = store.get_all()

    # path → (set of all chunk IDs, mtime) — multiple IDs per file when chunked
    stored: dict[str, tuple[set[str], str]] = {}
    for id_, meta in zip(all_ids, all_metas):
        path_key = meta["path"]
        mtime = meta.get("mtime", "")
        if path_key not in stored:
            stored[path_key] = (set(), mtime)
        stored[path_key][0].add(id_)

    dirty: list[tuple[Path, set[str]]] = []  # (path, stale_ids_to_delete)
    live_ids: set[str] = set()

    for path in index_dir.rglob("*.md"):
        if _should_skip(path.relative_to(index_dir)):
            continue
        rel = str(path.relative_to(index_dir))
        existing_ids, existing_mtime = stored.get(rel, (set(), ""))

        if existing_mtime != str(path.stat().st_mtime):
            dirty.append((path, existing_ids))
        else:
            live_ids.update(existing_ids)

    if dirty:
        total = len(dirty)
        t0 = time.monotonic()
        logger.warning("Indexing %d files...", total)

        workers = min(32, os.cpu_count() * 4, total)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            parse_results = list(ex.map(lambda p: _parse_chunks(p, index_dir, write_dir),
                                        [p for p, _ in dirty]))

        ids, texts, docs, metas = [], [], [], []
        for chunks_result, (_, stale_ids) in zip(parse_results, dirty):
            for stale_id in stale_ids:
                store.delete(stale_id)
            if chunks_result is None:
                continue
            for cid, embed_text, doc, meta in chunks_result:
                ids.append(cid)
                texts.append(embed_text)
                docs.append(doc)
                metas.append(meta)

        # Only chunk texts missing from the embedding cache are encoded
        cached = store.cache_get(texts)
        missing = list(dict.fromkeys(t for t, v in zip(texts, cached) if v is None))
        logger.warning("Embedding cache: %d/%d chunks cached, %d to encode",
                       len(texts) - sum(v is None for v in cached), len(texts), len(missing))

        # Encode in sub-batches so time-based progress can fire between chunks;
        # each sub-batch is cached immediately, so an interrupted sync keeps its work
        fresh: dict[str, list[float]] = {}
        last_log = time.monotonic()
        for i in range(0, len(missing), _ENCODE_CHUNK):
            part = missing[i:i + _ENCODE_CHUNK]
            vectors = store.encode_batch(part)
            store.cache_put(part, vectors)
            fresh.update(zip(part, vectors))
            now = time.monotonic()
            if now - last_log >= _PROGRESS_INTERVAL:
                logger.warning("Indexing: %d/%d encoded (%.0fs)", i + len(part), len(missing), now - t0)
                last_log = now
        all_embeddings = [v if v is not None else fresh[t] for t, v in zip(texts, cached)]

        store.upsert_batch(ids, all_embeddings, docs, metas)
        store.cache_prune(_CACHE_MAX_AGE_DAYS)
        live_ids.update(ids)

        logger.warning("Indexed %d files in %.0fs", total, time.monotonic() - t0)

    for orphan_id in set(all_ids) - live_ids:
        store.delete(orphan_id)

    return len(dirty)


def scan_schema(write_dir: Path) -> dict:
    """Scan write_dir for .mimir.yaml files, return nested category schema.

    `search_weight` (float, default 1.0) demotes or boosts a category in search
    ranking; it is inherited by subcategories unless they override it.
    """
    def _weight(meta: dict, inherited: float) -> float:
        raw = meta.get("search_weight")
        if raw is None:
            return inherited
        try:
            w = float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid search_weight %r — using %s", raw, inherited)
            return inherited
        if w < 0:
            logger.warning("Negative search_weight %s — using %s", w, inherited)
            return inherited
        return w

    def _scan(base: Path, prefix: str, inherited: float) -> dict:
        schema = {}
        try:
            for item in sorted(base.iterdir()):
                if not item.is_dir() or item.name.startswith("."):
                    continue
                info_file = item / ".mimir.yaml"
                if not info_file.exists():
                    continue
                try:
                    meta = yaml.safe_load(info_file.read_text()) or {}
                except Exception:
                    continue
                full = f"{prefix}{item.name}" if prefix else item.name
                weight = _weight(meta, inherited)
                schema[full] = {
                    "name": item.name,
                    "usage": meta.get("usage", ""),
                    "search_weight": weight,
                    "note_count": len(list(item.glob("*.md"))),
                    "path": str(item.relative_to(write_dir)),
                    "subcategories": _scan(item, f"{full}/", weight),
                }
        except Exception as e:
            logger.warning("Error scanning schema: %s", e)
        return schema
    return _scan(write_dir, "", 1.0)


def flatten_schema(schema: dict) -> list[str]:
    result = []
    for key, info in schema.items():
        result.append(key)
        if info.get("subcategories"):
            result.extend(flatten_schema(info["subcategories"]))
    return result


class NotesWatcher(FileSystemEventHandler):
    """Watch index_dir for .md changes and auto-reindex."""

    def __init__(self, index_dir: Path, store: VectorStore, write_dir: Path | None = None) -> None:
        self._index_dir = index_dir
        self._store = store
        self._write_dir = write_dir

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            p = Path(event.src_path)
            if not _should_skip(p.relative_to(self._index_dir)):
                index_file(p, self._index_dir, self._store, self._write_dir)

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            p = Path(event.src_path)
            if not _should_skip(p.relative_to(self._index_dir)):
                index_file(p, self._index_dir, self._store, self._write_dir)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            fid = file_id(Path(event.src_path), self._index_dir)
            self._store.delete(fid)  # old-style single entry
            for old_id in self._store.get_by_file_id(fid):
                self._store.delete(old_id)


def start_watcher(index_dir: Path, store: VectorStore, write_dir: Path | None = None) -> Observer:
    observer = Observer()
    observer.schedule(NotesWatcher(index_dir, store, write_dir), path=str(index_dir), recursive=True)
    observer.start()
    return observer
