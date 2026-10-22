"""Vector store — sqlite-vec (dense) + SQLite FTS5 (keyword) + sqlite3 (metadata).

Dense embeddings: sqlite-vec `vec0` virtual table in metadata.db, cosine distance.
Sparse index:     SQLite FTS5 `entries_fts` virtual table, bm25() ranking.
                  Both are incremental — insert/delete touch only changed rows,
                  no full-index rebuild per write.
Hybrid search:    RRF (Reciprocal Rank Fusion) over dense + BM25 ranked lists.

Query scale: O(n·d) for dense, O(log n) for FTS5 — fast enough up to ~100k chunks.
"""

from __future__ import annotations
import hashlib
import math
import re
import sqlite3
import threading
import time
from array import array
from pathlib import Path

import sqlite_vec


_UPSERT_CHUNK = 1000    # max rows per executemany
_RRF_K = 60             # RRF constant — standard value, controls rank-score decay
KNN_MAX = 4096          # sqlite-vec hard limit on `k` in a MATCH query
_CACHE_VERSION = 1      # bump when vectors for the same model/text change (normalization, fastembed pooling)
_CACHE_PARAMS = 900     # max hashes per IN (...) lookup, well under SQLite's variable limit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _batches(seq: list, size: int = _UPSERT_CHUNK):
    """Split an IN (...) parameter list — SQLite caps bound variables at 32766."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _normalize(v: list[float]) -> list[float]:
    """Normalize vector to unit length. fastembed >=0.6 mean-pooling is not normalized."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return v
    return [x / norm for x in v]


_WHERE_ALLOWED_FIELDS = {
    "row_id", "str_id", "document", "title", "tags", "category", "path",
    "mtime", "file_id", "chunk_heading", "chunk_idx",
}


def _where_to_sql(where: dict) -> tuple[str, list]:
    """Convert chromadb-style where dict to a SQL WHERE fragment + params."""
    if "$or" in where:
        parts, params = [], []
        for sub in where["$or"]:
            clause, sub_params = _where_to_sql(sub)
            parts.append(f"({clause})")
            params.extend(sub_params)
        return " OR ".join(parts), params
    for field, condition in where.items():
        if isinstance(condition, dict) and "$eq" in condition:
            if field not in _WHERE_ALLOWED_FIELDS:
                raise ValueError(f"Unsupported where field: {field!r}")
            return f"{field} = ?", [condition["$eq"]]
    return "1=1", []


def _matches_where(meta: dict, where: dict) -> bool:
    """Check metadata dict against a chromadb-style where filter (Python-side)."""
    if "$or" in where:
        return any(_matches_where(meta, sub) for sub in where["$or"])
    for field, condition in where.items():
        if isinstance(condition, dict) and "$eq" in condition:
            if meta.get(field) != condition["$eq"]:
                return False
    return True


def _tokenize(text: str) -> list[str]:
    """Simple multilingual tokenizer: lowercase, split on non-word chars, drop empties."""
    return [t for t in re.split(r"[^\w]+", text.lower()) if t]


def _rrf(ranked_ids: list[int], k: int = _RRF_K) -> dict[int, float]:
    return {rid: 1.0 / (k + rank + 1) for rank, rid in enumerate(ranked_ids)}


def _fts_match(text: str) -> str | None:
    """Build a safe FTS5 MATCH query: OR of quoted tokens (partial-match ranking)."""
    tokens = _tokenize(text)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class VectorStore:
    """
    sqlite-vec `vec0` virtual table (cosine distance) for dense ANN search,
    plus a SQLite FTS5 `entries_fts` virtual table (bm25() ranking) for
    keyword/hybrid search.

    row_id is the SQLite AUTOINCREMENT PK — links all three tables together
    (vec_entries.rowid == entries_fts.rowid == entries.row_id).
    dist values returned are normalized to [0, 1] across all search modes:
      0.0 = perfect match, 1.0 = worst match in the result set.
    """

    def __init__(self, data_dir: Path, model_name: str) -> None:
        self._data_dir = data_dir
        self._model_name = model_name
        self._model = None
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        # Embedding cache lives in its own file so --reindex (which deletes
        # metadata.db) keeps it; own lock so encoding never blocks searches.
        self._cache_db: sqlite3.Connection | None = None
        self._cache_lock = threading.Lock()
        self._cache_model = f"{model_name}|v{_CACHE_VERSION}"

        # Dense vector store — vec0 virtual table, created lazily on first write
        # (dimension is only known once the first embedding arrives).
        self._vec_ready = False

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    @property
    def model(self):
        if self._model is None:
            import warnings
            from fastembed import TextEmbedding
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self._model = TextEmbedding(self._model_name)
        return self._model

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            conn = sqlite3.connect(
                str(self._data_dir / "metadata.db"),
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS entries (
                    row_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    str_id        TEXT UNIQUE NOT NULL,
                    document      TEXT NOT NULL DEFAULT '',
                    title         TEXT NOT NULL DEFAULT '',
                    tags          TEXT NOT NULL DEFAULT '',
                    category      TEXT NOT NULL DEFAULT '',
                    path          TEXT NOT NULL DEFAULT '',
                    mtime         TEXT NOT NULL DEFAULT '',
                    file_id       TEXT NOT NULL DEFAULT '',
                    chunk_heading TEXT NOT NULL DEFAULT '',
                    chunk_idx     INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_path     ON entries(path);
                CREATE INDEX IF NOT EXISTS idx_file_id  ON entries(file_id);
                CREATE INDEX IF NOT EXISTS idx_category ON entries(category);
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(document, title);
            """)
            conn.commit()
            self._db = conn
        return self._db

    def _vec_table_exists(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_entries'"
        ).fetchone() is not None

    def _ensure_vec_table(self, dim: int) -> None:
        """Create the vec0 virtual table on first write. Must be called under _lock."""
        if self._vec_ready:
            return
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_entries "
            f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        self._vec_ready = True

    def _vec_insert(self, row_ids: list[int], embeddings: list[list[float]]) -> None:
        """Insert new rows into the vec0 table. Must be called under _lock."""
        if not row_ids:
            return
        self._ensure_vec_table(len(embeddings[0]))
        for i in range(0, len(row_ids), _UPSERT_CHUNK):
            sl = slice(i, i + _UPSERT_CHUNK)
            self.db.executemany(
                "INSERT INTO vec_entries(rowid, embedding) VALUES (?, ?)",
                [(rid, sqlite_vec.serialize_float32(emb))
                 for rid, emb in zip(row_ids[sl], embeddings[sl])],
            )
        self.db.commit()

    def _vec_delete(self, row_ids: list[int]) -> None:
        """Delete rows from the vec0 table by rowid. Must be called under _lock."""
        if not row_ids or not self._vec_table_exists():
            return
        for part in _batches(row_ids):
            ph = ",".join("?" * len(part))
            self.db.execute(f"DELETE FROM vec_entries WHERE rowid IN ({ph})", part)
        self.db.commit()

    # ------------------------------------------------------------------
    # FTS5 keyword index management
    # ------------------------------------------------------------------

    def _fts_insert(self, row_ids: list[int], documents: list[str], metadatas: list[dict]) -> None:
        """Insert new rows into the FTS5 table. Must be called under _lock."""
        if not row_ids:
            return
        for i in range(0, len(row_ids), _UPSERT_CHUNK):
            sl = slice(i, i + _UPSERT_CHUNK)
            self.db.executemany(
                "INSERT INTO entries_fts(rowid, document, title) VALUES (?, ?, ?)",
                [(rid, doc, meta.get("title", ""))
                 for rid, doc, meta in zip(row_ids[sl], documents[sl], metadatas[sl])],
            )
        self.db.commit()

    def _fts_delete(self, row_ids: list[int]) -> None:
        """Delete rows from the FTS5 table by rowid. Must be called under _lock."""
        if not row_ids:
            return
        for part in _batches(row_ids):
            ph = ",".join("?" * len(part))
            self.db.execute(f"DELETE FROM entries_fts WHERE rowid IN ({ph})", part)
        self.db.commit()

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    def encode(self, text: str) -> list[float]:
        return _normalize(next(iter(self.model.embed([text]))).tolist())

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [_normalize(v.tolist()) for v in self.model.embed(texts)]

    # ------------------------------------------------------------------
    # Embedding cache — (model|version, sha256(text)) → normalized vector
    # ------------------------------------------------------------------

    @property
    def cache_db(self) -> sqlite3.Connection:
        if self._cache_db is None:
            conn = sqlite3.connect(
                str(self._data_dir / "embed_cache.db"),
                check_same_thread=False,
            )
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                -- rowid table + unique index: ~2x smaller on disk than a
                -- WITHOUT ROWID table keyed by random hashes with 1.5 kB rows
                CREATE TABLE IF NOT EXISTS embed_cache (
                    id        INTEGER PRIMARY KEY,
                    model     TEXT    NOT NULL,
                    text_hash BLOB    NOT NULL,
                    embedding BLOB    NOT NULL,
                    last_used INTEGER NOT NULL,
                    UNIQUE (model, text_hash)
                );
            """)
            conn.commit()
            self._cache_db = conn
        return self._cache_db

    @staticmethod
    def _text_hash(text: str) -> bytes:
        return hashlib.sha256(text.encode("utf-8")).digest()

    def cache_get(self, texts: list[str]) -> list[list[float] | None]:
        """Cached vector per text (None on miss); refreshes last_used on hits."""
        hashes = [self._text_hash(t) for t in texts]
        found: dict[bytes, list[float]] = {}
        unique = list(dict.fromkeys(hashes))
        now = int(time.time())
        with self._cache_lock:
            db = self.cache_db
            for i in range(0, len(unique), _CACHE_PARAMS):
                part = unique[i:i + _CACHE_PARAMS]
                ph = ",".join("?" * len(part))
                for h, blob in db.execute(
                    f"SELECT text_hash, embedding FROM embed_cache "
                    f"WHERE model = ? AND text_hash IN ({ph})",
                    [self._cache_model, *part],
                ):
                    found[h] = array("f", blob).tolist()
            hits = list(found)
            for i in range(0, len(hits), _CACHE_PARAMS):
                part = hits[i:i + _CACHE_PARAMS]
                ph = ",".join("?" * len(part))
                db.execute(
                    f"UPDATE embed_cache SET last_used = ? "
                    f"WHERE model = ? AND text_hash IN ({ph})",
                    [now, self._cache_model, *part],
                )
            db.commit()
        return [found.get(h) for h in hashes]

    def cache_put(self, texts: list[str], embeddings: list[list[float]]) -> None:
        if not texts:
            return
        now = int(time.time())
        rows = [
            (self._cache_model, self._text_hash(t), sqlite_vec.serialize_float32(e), now)
            for t, e in zip(texts, embeddings)
        ]
        with self._cache_lock:
            self.cache_db.executemany(
                "INSERT OR REPLACE INTO embed_cache(model, text_hash, embedding, last_used) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            self.cache_db.commit()

    def cache_prune(self, max_age_days: int) -> int:
        """Delete entries (any model) not used for max_age_days. Returns rows removed."""
        cutoff = int(time.time()) - max_age_days * 86400
        with self._cache_lock:
            n = self.cache_db.execute(
                "DELETE FROM embed_cache WHERE last_used < ?", (cutoff,)
            ).rowcount
            self.cache_db.commit()
        return n

    def encode_cached(self, texts: list[str]) -> list[list[float]]:
        """Like encode_batch, but only texts missing from the cache are encoded."""
        vectors = self.cache_get(texts)
        missing = list(dict.fromkeys(t for t, v in zip(texts, vectors) if v is None))
        if missing:
            fresh = dict(zip(missing, self.encode_batch(missing)))
            self.cache_put(missing, list(fresh.values()))
            vectors = [v if v is not None else fresh[t] for t, v in zip(texts, vectors)]
        return vectors

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _row_to_meta(self, row: sqlite3.Row) -> dict:
        return {
            "title":         row["title"],
            "tags":          row["tags"],
            "category":      row["category"],
            "path":          row["path"],
            "mtime":         row["mtime"],
            "file_id":       row["file_id"],
            "chunk_heading": row["chunk_heading"],
            "chunk_idx":     row["chunk_idx"],
        }

    def _delete_str_ids(self, ids: list[str]) -> list[int]:
        """Delete entries by str_id; return their row_ids."""
        if not ids:
            return []
        row_ids: list[int] = []
        for part in _batches(ids):
            ph = ",".join("?" * len(part))
            rows = self.db.execute(
                f"SELECT str_id, row_id FROM entries WHERE str_id IN ({ph})", part
            ).fetchall()
            row_ids.extend(r["row_id"] for r in rows)
            self.db.execute(f"DELETE FROM entries WHERE str_id IN ({ph})", part)
        self.db.commit()
        return row_ids

    def _insert_entries(self, ids, documents, metadatas) -> list[int]:
        """Insert entries into SQLite; return their new row_ids in order."""
        for i in range(0, len(ids), _UPSERT_CHUNK):
            sl = slice(i, i + _UPSERT_CHUNK)
            self.db.executemany(
                """INSERT INTO entries
                   (str_id, document, title, tags, category, path,
                    mtime, file_id, chunk_heading, chunk_idx)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (id_, doc,
                     meta.get("title", ""),         meta.get("tags", ""),
                     meta.get("category", ""),       meta.get("path", ""),
                     meta.get("mtime", ""),          meta.get("file_id", ""),
                     meta.get("chunk_heading", ""),  meta.get("chunk_idx", 0))
                    for id_, doc, meta in zip(ids[sl], documents[sl], metadatas[sl])
                ],
            )
        self.db.commit()
        mapping: dict[str, int] = {}
        for part in _batches(ids):
            ph = ",".join("?" * len(part))
            rows = self.db.execute(
                f"SELECT str_id, row_id FROM entries WHERE str_id IN ({ph})", part
            ).fetchall()
            mapping.update((r["str_id"], r["row_id"]) for r in rows)
        return [mapping[id_] for id_ in ids]

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def upsert(self, id: str, embedding: list[float], document: str, metadata: dict) -> None:
        with self._lock:
            old_row_ids = self._delete_str_ids([id])
            self._vec_delete(old_row_ids)
            self._fts_delete(old_row_ids)
            new_row_ids = self._insert_entries([id], [document], [metadata])
            self._vec_insert(new_row_ids, [embedding])
            self._fts_insert(new_row_ids, [document], [metadata])

    def upsert_batch(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        if not ids:
            return
        with self._lock:
            old_row_ids = self._delete_str_ids(ids)
            self._vec_delete(old_row_ids)
            self._fts_delete(old_row_ids)
            new_row_ids = self._insert_entries(ids, documents, metadatas)
            self._vec_insert(new_row_ids, embeddings)
            self._fts_insert(new_row_ids, documents, metadatas)

    def delete(self, id: str) -> None:
        with self._lock:
            old_row_ids = self._delete_str_ids([id])
            if old_row_ids:
                self._vec_delete(old_row_ids)
                self._fts_delete(old_row_ids)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Number of indexed chunks (long notes are split into several)."""
        return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def count_files(self) -> int:
        """Number of distinct source files (notes), regardless of chunking."""
        return self.db.execute("SELECT COUNT(DISTINCT file_id) FROM entries").fetchone()[0]

    def get_all(self) -> tuple[list[str], list[dict]]:
        rows = self.db.execute("SELECT * FROM entries").fetchall()
        return [r["str_id"] for r in rows], [self._row_to_meta(r) for r in rows]

    def get_all_metadata(self) -> list[dict]:
        return [self._row_to_meta(r) for r in self.db.execute("SELECT * FROM entries").fetchall()]

    def get_by_file_id(self, file_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT str_id FROM entries WHERE file_id=?", (file_id,)
        ).fetchall()
        return [r["str_id"] for r in rows]

    def query(self, embedding: list[float], n: int, where: dict | None) -> list[tuple]:
        """Dense search. Returns (document, metadata, dist) where dist ∈ [0, 1]."""
        with self._lock:
            return self._query_locked(embedding, n, where)

    def _query_locked(self, embedding: list[float], n: int, where: dict | None) -> list[tuple]:
        if not self._vec_table_exists():
            return []

        q = sqlite_vec.serialize_float32(embedding)
        matches = self.db.execute(
            "SELECT rowid, distance FROM vec_entries WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (q, min(n, KNN_MAX)),
        ).fetchall()

        results = []
        for row_id, distance in matches:
            row = self.db.execute(
                "SELECT * FROM entries WHERE row_id=?", (row_id,)
            ).fetchone()
            if row is None:
                continue
            meta = self._row_to_meta(row)
            if where is not None and not _matches_where(meta, where):
                continue
            dist = float(distance) / 2.0   # cosine distance ∈ [0, 2] → normalize to [0, 1]
            results.append((row["document"], meta, dist))
        return results

    def bm25_query(self, text: str, n: int, where: dict | None) -> list[tuple]:
        """Keyword search via SQLite FTS5 bm25(). Returns (document, metadata, dist) where dist ∈ [0, 1]."""
        with self._lock:
            match = _fts_match(text)
            if match is None:
                return []

            # bm25() is more negative = better match; fetch all matches, rank ascending.
            rows = self.db.execute(
                "SELECT entries.*, bm25(entries_fts) AS score FROM entries_fts "
                "JOIN entries ON entries.row_id = entries_fts.rowid "
                "WHERE entries_fts MATCH ? ORDER BY bm25(entries_fts)",
                (match,),
            ).fetchall()
            if not rows:
                return []

            best = -rows[0]["score"]   # most negative row = best match
            if best <= 0.0:
                return []

            results = []
            for row in rows:
                if len(results) >= n:
                    break
                meta = self._row_to_meta(row)
                if where is not None and not _matches_where(meta, where):
                    continue
                dist = 1.0 - (-row["score"]) / best   # normalize to [0, 1]
                results.append((row["document"], meta, dist))
            return results

    def hybrid_query(
        self, embedding: list[float], text: str, n: int, where: dict | None,
        weights: dict[str, float] | None = None,
    ) -> list[tuple]:
        """Hybrid search: RRF fusion of dense + BM25 rankings.

        weights: optional {category: factor} demotion/boost applied to the fused
        score (1.0 = neutral, <1 pushes a category down, >1 pulls it up).
        Returns (document, metadata, dist) where dist ∈ [0, 1], 0 = best match.
        """
        with self._lock:
            return self._hybrid_locked(embedding, text, n, where, weights)

    def _hybrid_locked(
        self, embedding: list[float], text: str, n: int, where: dict | None,
        weights: dict[str, float] | None = None,
    ) -> list[tuple]:
        total = self.count()
        if not total:
            return []

        n_fetch = min(n * 4, total, KNN_MAX)

        # --- Dense ranking ---
        dense_ranked: list[int] = []
        if self._vec_table_exists():
            q = sqlite_vec.serialize_float32(embedding)
            matches = self.db.execute(
                "SELECT rowid FROM vec_entries WHERE embedding MATCH ? AND k = ? "
                "ORDER BY distance",
                (q, n_fetch),
            ).fetchall()
            dense_ranked = [r[0] for r in matches]

        # --- BM25 ranking (SQLite FTS5) ---
        bm25_ranked: list[int] = []
        match = _fts_match(text)
        if match is not None:
            rows = self.db.execute(
                "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ? "
                "ORDER BY bm25(entries_fts) LIMIT ?",
                (match, n_fetch),
            ).fetchall()
            bm25_ranked = [r[0] for r in rows]

        # --- RRF fusion ---
        scores: dict[int, float] = {}
        for rank, rid in enumerate(dense_ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, rid in enumerate(bm25_ranked):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (_RRF_K + rank + 1)

        if not scores:
            return []

        # --- Batch-fetch metadata (before ranking: weights are keyed by category) ---
        rids = list(scores)
        ph = ",".join("?" * len(rids))
        rows = self.db.execute(
            f"SELECT * FROM entries WHERE row_id IN ({ph})", rids
        ).fetchall()
        row_map = {r["row_id"]: r for r in rows}

        # --- Category weights: demote/boost the fused score before ranking ---
        if weights:
            for rid, row in row_map.items():
                w = weights.get(row["category"])
                if w is not None and w != 1.0:
                    scores[rid] *= w

        sorted_rids = sorted(scores, key=scores.__getitem__, reverse=True)
        max_score = scores[sorted_rids[0]] or 1.0   # guard against an all-zero weight

        results = []
        for rid in sorted_rids:
            if len(results) >= n:
                break
            row = row_map.get(rid)
            if row is None:
                continue
            meta = self._row_to_meta(row)
            if where is not None and not _matches_where(meta, where):
                continue
            dist = 1.0 - scores[rid] / max_score   # normalize top result to dist=0
            results.append((row["document"], meta, dist))
        return results

    def get_filtered(self, where: dict | None, limit: int) -> list[tuple]:
        """Return list of (document, metadata) without semantic scoring."""
        if where is None:
            rows = self.db.execute("SELECT * FROM entries LIMIT ?", (limit,)).fetchall()
        else:
            clause, params = _where_to_sql(where)
            rows = self.db.execute(
                f"SELECT * FROM entries WHERE {clause} LIMIT ?", params + [limit]
            ).fetchall()
        return [(r["document"], self._row_to_meta(r)) for r in rows]
