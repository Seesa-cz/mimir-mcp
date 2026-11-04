"""MCP tool definitions for Mimir knowledge base."""

from __future__ import annotations
import functools
import inspect
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
import frontmatter
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .config import Config
from .store import VectorStore, KNN_MAX
from .indexer import index_file, scan_schema, flatten_schema, file_id, is_indexing
from . import devlog, version_check

logger = logging.getLogger(__name__)
usage_logger = logging.getLogger("mimir.usage")

_N_FETCH_FLOOR = 200    # min candidates per search — demotion needs alternatives to promote


def _log_usage(tool: str, **fields) -> None:
    """Write one JSONL record for a tool call — see mimir.usage logger setup in server.py."""
    record = {"ts": datetime.now().isoformat(timespec="seconds"), "tool": tool, **fields}
    usage_logger.info(json.dumps(record, ensure_ascii=False, default=str))


def _tracked(tool_name: str, *arg_names: str):
    """Decorator: logs duration, selected args, and a result/error summary for a tool call.

    For tools whose usage-relevant output isn't captured by the return string
    (e.g. search_notes' hit count/relevance), log explicitly inside the function instead.
    """
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            t0 = time.monotonic()
            error = None
            result: str = ""
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                fields = {name: bound.arguments.get(name) for name in arg_names}
                fields["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
                if error is not None:
                    fields["error"] = error
                elif isinstance(result, str):
                    fields["result_summary"] = result[:150]
                _log_usage(tool_name, **fields)
        return wrapper
    return deco


def _collect_weights(schema: dict) -> dict[str, float]:
    """Flatten a category tree into {category: search_weight}, skipping neutral 1.0."""
    out: dict[str, float] = {}
    for key, info in schema.items():
        w = info.get("search_weight", 1.0)
        if w != 1.0:
            out[key] = w
        out.update(_collect_weights(info.get("subcategories", {})))
    return out


def _resolve_within(path_str: str, index_dir: Path, boundary: Path) -> Path | None:
    """Resolve path relative to index_dir; reject if outside boundary. Does not check existence."""
    p = (index_dir / path_str).resolve()
    return p if p.is_relative_to(boundary) else None


def _resolve_read(path_str: str, index_dir: Path) -> Path | None:
    """Resolve path relative to index_dir. Rejects path traversal and non-.md files."""
    if not path_str.endswith(".md"):
        return None
    p = _resolve_within(path_str, index_dir, index_dir)
    return p if p and p.exists() else None


def _resolve_write(path_str: str, index_dir: Path, write_dir: Path) -> Path | None:
    """Resolve path relative to index_dir (same as search_notes returns); reject paths outside write_dir."""
    p = _resolve_within(path_str, index_dir, write_dir)
    return p if p and p.exists() else None


def register_tools(mcp: FastMCP, cfg: Config, store: VectorStore, touch_fn) -> None:
    """Register all MCP tools onto the FastMCP instance."""

    # Schema cache — avoids rescanning filesystem on every search_notes/add_note call.
    # TTL of 30 s is sufficient since categories change only on manual .mimir.yaml edits.
    _schema: dict = {"data": [], "weights": {}, "ts": 0.0}
    _SCHEMA_TTL = 30.0

    def _refresh_schema() -> None:
        if time.monotonic() - _schema["ts"] > _SCHEMA_TTL:
            tree = scan_schema(cfg.write_dir)
            _schema["data"] = flatten_schema(tree)
            _schema["weights"] = _collect_weights(tree)
            _schema["ts"] = time.monotonic()

    def _flat_schema() -> list[str]:
        _refresh_schema()
        return _schema["data"]

    def _category_weights() -> dict[str, float]:
        """{category: search_weight} for categories that deviate from neutral 1.0."""
        _refresh_schema()
        return _schema["weights"]

    def _build_where(category: str) -> dict | None:
        flat = _flat_schema()
        matching = [c for c in flat if c == category or c.startswith(f"{category}/")]
        if not matching:
            return None
        if len(matching) == 1:
            return {"category": {"$eq": matching[0]}}
        return {"$or": [{"category": {"$eq": c}} for c in matching]}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_notes(
        query: str = "",
        k: int = 5,
        tags: list[str] | None = None,
        category: str | None = None,
        mode: str = "hybrid",
    ) -> str:
        """Search notes by semantic query and/or filters.

        Args:
            query: Natural language description. Optional — omit to browse by tags/category.
            k: Number of results (default 5).
            tags: Filter by tags (OR logic).
            category: Hierarchical category filter, e.g. "patterns" or "patterns/gis".
                      An unknown category is an error, not a silently ignored filter.
            mode: Search mode — "hybrid" (default, dense+BM25 RRF), "dense" (semantic only),
                  "keyword" (BM25 only, good for exact names/error strings).

        Categories may set `search_weight` in .mimir.yaml to demote (<1) or boost (>1)
        their notes in the ranking. Demoted notes are still searched and still surface
        when nothing better matches — only "hybrid" mode applies these weights.
        """
        touch_fn()
        t0 = time.monotonic()
        notice = version_check.update_notice() + (
            "[indexing in progress — results may be incomplete]\n\n" if is_indexing() else ""
        )
        total = store.count()
        if total == 0:
            _log_usage("search_notes", query=query, k=k, tags=tags, category=category,
                       mode=mode, duration_ms=round((time.monotonic() - t0) * 1000, 1),
                       results_n=0, top_relevance=None, reason="empty_kb")
            return notice + "Knowledge base is empty. Use add_note() to add the first note."

        where = None
        if category:
            where = _build_where(category)
            if where is None:
                # Never fall through to an unfiltered search — that silently returns
                # the whole corpus while the caller believes the filter applied.
                known = ", ".join(_flat_schema()) or "(none)"
                _log_usage("search_notes", query=query, k=k, tags=tags, category=category,
                           mode=mode, duration_ms=round((time.monotonic() - t0) * 1000, 1),
                           results_n=0, top_relevance=None, reason="unknown_category")
                return (
                    f"Unknown category '{category}' — nothing was searched. "
                    f"Known categories: {known}"
                )

        if query.strip():
            # Fetch well beyond k so category weights have alternatives to promote.
            # Tags are post-filtered in Python, so go as deep as allowed or rare tags
            # get dropped before they are ever seen. Both are capped at the knn limit.
            if tags:
                n_fetch = min(total, KNN_MAX)
            else:
                n_fetch = max(min(k * 20, total), min(_N_FETCH_FLOOR, total))
            if mode == "keyword":
                candidates = store.bm25_query(query, n_fetch, where)
            elif mode == "dense":
                embedding = store.encode(query)
                candidates = store.query(embedding, n_fetch, where)
            else:  # "hybrid" (default)
                embedding = store.encode(query)
                candidates = store.hybrid_query(
                    embedding, query, n_fetch, where, _category_weights()
                )
        else:
            raw = store.get_filtered(where, total)
            candidates = [(doc, meta, None) for doc, meta in raw]

        if tags:
            tags_lower = [t.lower() for t in tags]
            candidates = [
                (doc, meta, dist) for doc, meta, dist in candidates
                if any(
                    tag in [t.strip().lower() for t in meta["tags"].split(",")]
                    for tag in tags_lower
                )
            ]

        # Deduplicate by file path — keep best (lowest distance) chunk per file
        seen: dict[str, tuple] = {}
        for doc, meta, dist in candidates:
            path_key = meta["path"]
            if path_key not in seen or (
                dist is not None and (seen[path_key][2] is None or dist < seen[path_key][2])
            ):
                seen[path_key] = (doc, meta, dist)
        candidates = list(seen.values())[:k]

        if not candidates:
            label = f"query='{query}'" if query.strip() else "browse"
            extra = ""
            if tags:
                extra += f", tags={tags}"
            if category:
                extra += f", category='{category}'"
            _log_usage("search_notes", query=query, k=k, tags=tags, category=category,
                       mode=mode, duration_ms=round((time.monotonic() - t0) * 1000, 1),
                       results_n=0, top_relevance=None)
            return notice + f"No notes found matching: {label}{extra}"

        top_relevance = None
        if candidates[0][2] is not None:
            top_relevance = round((1.0 - candidates[0][2]) * 100, 1)
        _log_usage("search_notes", query=query, k=k, tags=tags, category=category,
                   mode=mode, duration_ms=round((time.monotonic() - t0) * 1000, 1),
                   results_n=len(candidates), top_relevance=top_relevance)

        parts = []
        for doc, meta, dist in candidates:
            header = f"### {meta['title']} [{meta['category']}]"
            if dist is not None:
                # dist ∈ [0, 1] for all modes — 0 = perfect match
                relevance = round((1.0 - dist) * 100, 1)
                header += f" — {relevance}% relevance"
            path_display = f"`{meta['path']}`"
            chunk_heading = meta.get("chunk_heading", "")
            if chunk_heading and chunk_heading != meta["title"]:
                path_display += f" › *{chunk_heading}*"
            snippet = doc[:600] + ("…" if len(doc) > 600 else "")
            parts.append(
                f"{header}\n"
                f"**Tags:** {meta['tags']}  |  {path_display}\n\n{snippet}"
            )
        return notice + "\n\n---\n\n".join(parts)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("add_note", "path", "tags", "author")
    def add_note(path: str, tags: list[str], author: str, content: str) -> str:
        """Save a new note to the knowledge base (writes to write_dir only).

        Args:
            path: Destination path relative to index_dir, e.g. 'mimir-kb/reference/pid-api.md'.
                  Must be within write_dir. Must end with .md. Content should start with # Title.
            tags: Relevant tags, e.g. ["python", "async", "retry"]. Reuse tags visible in
                  search_notes results where possible.
            author: Model ID of the creating agent, e.g. 'claude-sonnet-4-6'.
            content: Full markdown body starting with # Title heading.
        """
        touch_fn()
        p = _resolve_within(path, cfg.index_dir, cfg.write_dir)
        if p is None:
            return f"Path '{path}' is outside write_dir — cannot write."
        if not path.endswith(".md"):
            return "Path must end with .md"
        if p.exists():
            return f"Note already exists: {path}"
        p.parent.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(
            content,
            tags=tags,
            author=author,
            created=datetime.now().isoformat()[:10],
        )
        p.write_text(frontmatter.dumps(post))
        index_file(p, cfg.index_dir, store, cfg.write_dir)
        return f"Saved: {p.relative_to(cfg.index_dir)}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @_tracked("read_note", "path", "offset", "limit")
    def read_note(path: str, offset: int = 0, limit: int | None = None) -> str:
        """Return the content of a note, including frontmatter (whole note by default).

        Args:
            path: Relative path as returned by search_notes, e.g. 'mimir-kb/solutions/foo.md'.
            offset: First line, 0-based; negative counts from the end (-30 = last 30 lines).
            limit: Max number of lines to return (default: to the end).
        """
        touch_fn()
        p = _resolve_read(path, cfg.index_dir)
        if p is None:
            return f"Note not found: {path}"
        text = p.read_text()
        if offset == 0 and limit is None:
            return text
        if limit is not None and limit <= 0:
            return "limit must be a positive number of lines"
        lines = text.splitlines()
        total = len(lines)
        start = offset if offset >= 0 else max(total + offset, 0)
        if start >= total:
            return f"[offset {offset} is beyond the end — note has {total} lines]"
        end = total if limit is None else min(start + limit, total)
        return f"[lines {start + 1}–{end} of {total}]\n" + "\n".join(lines[start:end])

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("edit_note", "path", "tags")
    def edit_note(path: str, content: str, tags: list[str] | None = None) -> str:
        """Replace the body of an existing note (write_dir only).

        Args:
            path: Relative path as returned by search_notes.
            content: New markdown body (frontmatter preserved).
            tags: If provided, replaces the existing tags list.
        """
        touch_fn()
        p = _resolve_write(path, cfg.index_dir, cfg.write_dir)
        if p is None:
            if _resolve_read(path, cfg.index_dir):
                return f"Note '{path}' is outside write_dir — cannot edit."
            return f"Note not found: {path}"
        post = frontmatter.load(str(p))
        post.content = content
        if tags is not None:
            post["tags"] = tags
        p.write_text(frontmatter.dumps(post))
        index_file(p, cfg.index_dir, store, cfg.write_dir)
        return f"Updated: {path}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("patch_note", "path", "replace_all")
    def patch_note(path: str, old: str, new: str, replace_all: bool = False) -> str:
        """Partially edit a note by replacing a substring (write_dir only).

        Args:
            path: Relative path as returned by search_notes.
            old: Exact substring to find.
            new: Replacement text (empty string to delete).
            replace_all: Replace all occurrences (default: False).
        """
        touch_fn()
        p = _resolve_write(path, cfg.index_dir, cfg.write_dir)
        if p is None:
            if _resolve_read(path, cfg.index_dir):
                return f"Note '{path}' is outside write_dir — cannot edit."
            return f"Note not found: {path}"
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return f"String not found in {path}"
        if not replace_all and count > 1:
            return f"Ambiguous: {count} occurrences — use replace_all=True or more context"
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        index_file(p, cfg.index_dir, store, cfg.write_dir)
        return f"Patched {count if replace_all else 1} occurrence(s) in {path}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("delete_note", "path")
    def delete_note(path: str) -> str:
        """Delete a note from disk and remove it from the index (write_dir only).

        Args:
            path: Relative path as returned by search_notes.
        """
        touch_fn()
        p = _resolve_write(path, cfg.index_dir, cfg.write_dir)
        if p is None:
            if _resolve_read(path, cfg.index_dir):
                return f"Note '{path}' is outside write_dir — cannot delete."
            return f"Note not found: {path}"
        store.delete(file_id(p, cfg.index_dir))
        p.unlink()
        return f"Deleted: {path}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("move_note", "path", "new_path")
    def move_note(path: str, new_path: str) -> str:
        """Move or rename a note within write_dir. Updates category: frontmatter automatically.

        Args:
            path: Current relative path as returned by search_notes.
            new_path: Target relative path (same format, e.g. 'mimir-kb/solutions/new-name.md').
        """
        touch_fn()
        src = _resolve_write(path, cfg.index_dir, cfg.write_dir)
        if src is None:
            if _resolve_read(path, cfg.index_dir):
                return f"Note '{path}' is outside write_dir — cannot move."
            return f"Note not found: {path}"

        dst = (cfg.index_dir / new_path).resolve()
        if not dst.is_relative_to(cfg.write_dir):
            return f"Target '{new_path}' is outside write_dir."
        if dst == src:
            return "Source and target are the same path."

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
        store.delete(file_id(src, cfg.index_dir))
        index_file(dst, cfg.index_dir, store, cfg.write_dir)
        src.unlink()

        return f"Moved: {path} → {new_path}"

    # ── Project dev history (projects/<slug>/dev/) ────────────────────────────
    _dev_lock = threading.Lock()

    def _dev_scaffold(project: str) -> Path:
        """Create projects/<slug>/dev/ with categories and the four notes if missing."""
        devlog.check_slug(project)
        proj_dir = (cfg.write_dir / "projects" / project).resolve()
        if not proj_dir.is_relative_to(cfg.write_dir):
            raise devlog.DevlogError(f"Project path '{project}' is outside write_dir")
        dev_dir = proj_dir / "dev"
        today = datetime.now().date().isoformat()
        for d, meta in (
            (proj_dir, {"usage": f"Project {project}: knowledge notes"}),
            (dev_dir, {"usage": f"Project {project}: dev history (STATUS, TASK, PROGRESS, DECISIONS) "
                                "— write via dev_task / dev_log / dev_decision, STATUS via edit_note",
                       "search_weight": 0.5}),
        ):
            if not (d / ".mimir.yaml").exists():
                d.mkdir(parents=True, exist_ok=True)
                (d / ".mimir.yaml").write_text(yaml.dump(meta, allow_unicode=True, sort_keys=False))
                _schema["ts"] = 0.0
        for kind in devlog.KINDS:
            p = dev_dir / f"{kind}.md"
            if not p.exists():
                post = frontmatter.Post(devlog.skeleton(kind, project, today),
                                        tags=["dev-history", project], author="mimir", created=today)
                p.write_text(frontmatter.dumps(post) + "\n")
                index_file(p, cfg.index_dir, store, cfg.write_dir)
        return dev_dir

    def _dev_update(project: str, kind: str, fn) -> tuple[str, str]:
        """Apply fn(body) -> (new_body, result) to a dev note under the lock; returns (result, display path)."""
        with _dev_lock:
            p = _dev_scaffold(project) / f"{kind}.md"
            post = frontmatter.load(str(p))
            new_body, result = fn(post.content)
            post.content = new_body
            p.write_text(frontmatter.dumps(post) + "\n")
            index_file(p, cfg.index_dir, store, cfg.write_dir)
            return result, str(p.relative_to(cfg.index_dir))

    def _dev_body(project: str, kind: str) -> str:
        with _dev_lock:
            return frontmatter.load(str(_dev_scaffold(project) / f"{kind}.md")).content

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("dev_task", "project", "id", "state")
    def dev_task(
        project: str,
        id: str | None = None,
        description: str | None = None,
        ac: list[str] | None = None,
        state: str | None = None,
        reason: str | None = None,
        decision: str | None = None,
    ) -> str:
        """Add a task or change its state in projects/<project>/dev/TASK.md.

        Without id: adds '- [ ] T<next>' from description (+ ac = acceptance criteria), returns the ID.
        With id: sets state — ' ' todo, '~' in progress, '!' blocked (needs reason),
        'x' done, '-' cancelled (needs reason or decision). decision links a D<n>.

        Args:
            project: Project slug = lowercase project directory name.
        """
        touch_fn()
        try:
            if id is None:
                if state is not None:
                    return "New task always starts as '[ ]' — add it first, then set state with id"
                result, rel = _dev_update(project, "TASK", lambda t: devlog.add_task(t, description or "", ac))
                return f"Added {result} to {rel}"
            def _set(t: str) -> tuple[str, str]:
                if state is None:
                    raise devlog.DevlogError("state is required when id is given")
                return devlog.set_task_state(t, id, state, reason, decision), id
            result, rel = _dev_update(project, "TASK", _set)
            return f"{result} set to [{state}] in {rel}"
        except devlog.DevlogError as e:
            return f"Error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("dev_log", "project", "tasks")
    def dev_log(project: str, tasks: list[str], summary: str, implementation: str) -> str:
        """Append a row to projects/<project>/dev/PROGRESS.md under today's date.

        Args:
            project: Project slug = lowercase project directory name.
            tasks: Task IDs, e.g. ["T42"]; must exist in TASK.md.
            summary: One sentence — what was done.
            implementation: Files + change, commit hash, D<n> link — one line.
        """
        touch_fn()
        try:
            known = devlog.parse_tasks(_dev_body(project, "TASK"))
            missing = [t for t in tasks if t not in known]
            if missing:
                return f"Error: unknown task(s) {', '.join(missing)} — add them with dev_task first"
            today = datetime.now().date().isoformat()
            _, rel = _dev_update(project, "PROGRESS", lambda t: (
                devlog.append_log(t, today, tasks, summary, implementation), None))
            return f"Logged {', '.join(tasks)} in {rel}"
        except devlog.DevlogError as e:
            return f"Error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("dev_decision", "project", "id", "status")
    def dev_decision(
        project: str,
        title: str | None = None,
        context: str | None = None,
        decision: str | None = None,
        rejected: list[str] | None = None,
        consequences: str | None = None,
        tasks: list[str] | None = None,
        status: str = "accepted",
        id: str | None = None,
    ) -> str:
        """Record a design decision in projects/<project>/dev/DECISIONS.md, or change its status.

        Without id: new D<next> (title, context, decision required), returns the ID.
        rejected = alternatives not chosen, each 'Option — why not'.
        status 'rejected' = approach tried and abandoned (decision = what was tried, consequences = why it failed).
        With id: only sets status, e.g. 'superseded by D9'.

        Args:
            project: Project slug = lowercase project directory name.
        """
        touch_fn()
        try:
            if id is not None:
                result, rel = _dev_update(project, "DECISIONS",
                                          lambda t: (devlog.set_decision_status(t, id, status), id))
                return f"{result} status set to [{status}] in {rel}"
            today = datetime.now().date().isoformat()
            result, rel = _dev_update(project, "DECISIONS", lambda t: devlog.add_decision(
                t, today, title or "", context or "", decision or "", rejected, consequences, tasks, status))
            return f"Added {result} to {rel}"
        except devlog.DevlogError as e:
            return f"Error: {e}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("add_category", "category")
    def add_category(category: str, usage: str) -> str:
        """Create a new category directory with .mimir.yaml in write_dir.

        Args:
            category: Category path, e.g. "solutions" or "patterns/gis".
            usage: One-line guidance on what to store here.
        """
        touch_fn()
        cat_dir = (cfg.write_dir / category).resolve()
        if not cat_dir.is_relative_to(cfg.write_dir):
            return f"Category path '{category}' is outside write_dir."
        yaml_file = cat_dir / ".mimir.yaml"
        if yaml_file.exists():
            return f"Category '{category}' already exists."
        cat_dir.mkdir(parents=True, exist_ok=True)
        yaml_file.write_text(yaml.dump(
            {"usage": usage},
            allow_unicode=True,
            sort_keys=False,
        ))
        _schema["ts"] = 0.0
        return f"Created category: {category}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    @_tracked("edit_category", "category")
    def edit_category(category: str, usage: str) -> str:
        """Update the usage guidance of an existing category's .mimir.yaml.

        Args:
            category: Existing category path, e.g. "solutions".
            usage: New one-line guidance on what to store here (replaces the old value).
        """
        touch_fn()
        cat_dir = (cfg.write_dir / category).resolve()
        if not cat_dir.is_relative_to(cfg.write_dir):
            return f"Category path '{category}' is outside write_dir."
        yaml_file = cat_dir / ".mimir.yaml"
        if not yaml_file.exists():
            return f"Category '{category}' not found."
        try:
            existing = yaml.safe_load(yaml_file.read_text()) or {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        # Preserve every other key (e.g. search_weight) — this tool only edits `usage`.
        existing["usage"] = usage
        yaml_file.write_text(yaml.dump(
            existing,
            allow_unicode=True,
            sort_keys=False,
        ))
        _schema["ts"] = 0.0
        return f"Updated category: {category}"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("move_category", "category", "new_category")
    def move_category(category: str, new_category: str) -> str:
        """Rename or move a category and update category: frontmatter in all notes inside.

        Args:
            category: Existing category path, e.g. "projects".
            new_category: Target path, e.g. "work" or "archive/projects".
        """
        touch_fn()
        src_dir = (cfg.write_dir / category).resolve()
        if not src_dir.is_relative_to(cfg.write_dir):
            return f"Category '{category}' is outside write_dir."
        if not (src_dir / ".mimir.yaml").exists():
            return f"Category '{category}' not found."
        dst_dir = (cfg.write_dir / new_category).resolve()
        if not dst_dir.is_relative_to(cfg.write_dir):
            return f"Target '{new_category}' is outside write_dir."
        if dst_dir.exists():
            return f"Target category '{new_category}' already exists."

        md_files = list(src_dir.rglob("*.md"))
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        src_dir.rename(dst_dir)

        updated = 0
        for old_path in md_files:
            rel = old_path.relative_to(src_dir)
            new_path = dst_dir / rel
            try:
                post = frontmatter.load(str(new_path))
                if "category" in post:
                    old_cat = str(post["category"])
                    if old_cat == category or old_cat.startswith(f"{category}/"):
                        post["category"] = new_category + old_cat[len(category):]
                        new_path.write_text(frontmatter.dumps(post))
                store.delete(file_id(old_path, cfg.index_dir))
                index_file(new_path, cfg.index_dir, store, cfg.write_dir)
                updated += 1
            except Exception:
                pass

        _schema["ts"] = 0.0
        return f"Moved category {category} → {new_category} ({updated} notes updated)"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    @_tracked("delete_category", "category", "force")
    def delete_category(category: str, force: bool = False) -> str:
        """Delete a category from write_dir.

        Args:
            category: Category path to delete, e.g. "snippets".
            force: If True, deletes the whole directory (rmtree) — including any
                non-.md files inside it (attachments, subfolders), not just indexed
                notes. Default False refuses if the directory contains any .md files.
        """
        touch_fn()
        cat_dir = (cfg.write_dir / category).resolve()
        if not cat_dir.is_relative_to(cfg.write_dir):
            return f"Category '{category}' is outside write_dir."
        if not (cat_dir / ".mimir.yaml").exists():
            return f"Category '{category}' not found."

        md_files = list(cat_dir.rglob("*.md"))
        if md_files and not force:
            return f"Category '{category}' has {len(md_files)} note(s). Use force=True to delete with all notes."

        for p in md_files:
            store.delete(file_id(p, cfg.index_dir))
            p.unlink()
        shutil.rmtree(cat_dir)
        _schema["ts"] = 0.0
        return f"Deleted category {category} ({len(md_files)} notes removed)"

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @_tracked("list_tags")
    def list_tags() -> str:
        """List all tags in the knowledge base with note counts, sorted by frequency."""
        touch_fn()
        metas = store.get_all_metadata()
        seen_files: set[str] = set()
        counts: dict[str, int] = {}
        for m in metas:
            file_id_ = m.get("file_id", "")
            if file_id_ in seen_files:
                continue
            seen_files.add(file_id_)
            for tag in m.get("tags", "").split(","):
                tag = tag.strip()
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
        if not counts:
            return "No tags yet."
        return "\n".join(f"  {tag}: {n}" for tag, n in sorted(counts.items(), key=lambda x: -x[1]))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    @_tracked("reindex")
    def reindex() -> str:
        """Re-index notes modified since last index.

        The file watcher handles this automatically while the server is running.
        Use this only after editing note files while the server was stopped.
        """
        touch_fn()
        from .indexer import sync
        n = sync(cfg.index_dir, store, cfg.write_dir)
        if n is None:
            return "Sync already in progress — try again when it finishes."
        return f"Updated {n} note(s). Total indexed: {store.count_files()} note(s), {store.count()} chunk(s)."

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    @_tracked("get_schema")
    def get_schema() -> str:
        """Return knowledge base schema with categories and descriptions.

        Schema is auto-discovered from filesystem hierarchy.
        Each category directory must contain .mimir.yaml file.

        Hierarchy format: "patterns", "patterns/gis", "patterns/gis/praha"
        """
        touch_fn()
        schema = scan_schema(cfg.write_dir)
        if not schema:
            return "No categories found. Create .mimir.yaml files in write_dir directories."

        def fmt(items: dict, indent: int = 0) -> str:
            out = ""
            for path, info in sorted(items.items()):
                pad = "  " * indent
                weight = info.get("search_weight", 1.0)
                marker = "" if weight == 1.0 else f", search_weight {weight:g}"
                out += f"{pad}**{path}** ({info['note_count']} notes{marker})\n"
                out += f"{pad}  *{info['name']}* — {info['usage']}\n"
                if info["subcategories"]:
                    out += fmt(info["subcategories"], indent + 1)
                out += "\n"
            return out

        return "# Knowledge Base Schema\n\n" + fmt(schema)
