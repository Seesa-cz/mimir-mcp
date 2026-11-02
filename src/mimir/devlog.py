"""Project dev history format — pure parse/render functions for TASK, PROGRESS and DECISIONS notes.

Notes live in write_dir/projects/<slug>/dev/. All functions take and return the note body
(without frontmatter) and raise DevlogError on invalid input. Lines inside ``` fences are ignored.
"""

from __future__ import annotations
import re

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TASK_ID_RE = re.compile(r"^T\d+$")
DECISION_ID_RE = re.compile(r"^D\d+$")
TASK_RE = re.compile(r"^- \[([ ~!x-])\] T(\d+)(?: (.*))?$")
DATE_HEADING_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\s*$")
DECISION_RE = re.compile(r"^## D(\d+) — (\d{4}-\d{2}-\d{2}) — (.*?)  \[(.+)\]\s*$")
STATUS_RE = re.compile(r"^(accepted|rejected|superseded by D\d+)$")

TASK_STATES = (" ", "~", "!", "x", "-")
# Trailing annotations of a task line, rebuilt on every state change.
_SUFFIX_RE = re.compile(r"(?: — (?:blocked|cancelled): .*?)?(?: → (D\d+(?:, D\d+)*))?$")

KINDS = ("STATUS", "TASK", "PROGRESS", "DECISIONS")
PROGRESS_HEADER = "| Task | Summary | Implementation |\n|------|---------|----------------|"


class DevlogError(ValueError):
    pass


def check_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise DevlogError(f"Invalid project slug '{slug}' — use lowercase project directory name ({SLUG_RE.pattern})")


def skeleton(kind: str, slug: str, today: str) -> str:
    """Initial body of a new dev note."""
    if kind == "STATUS":
        return (f"# Status — {slug}\n\n_Updated: {today}_\n\n## Works\n\n## In progress\n\n"
                "## Blocked / open questions\n\n## Next step\n")
    titles = {"TASK": "Tasks", "PROGRESS": "Progress", "DECISIONS": "Decisions"}
    return f"# {titles[kind]} — {slug}\n"


def _unfenced(lines: list[str]):
    """Yield (index, line) for lines outside ``` code fences."""
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield i, line


def _append_block(text: str, block: str) -> str:
    return text.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"


def _one_line(value: str) -> str:
    return " ".join(value.split())


# ── TASK ────────────────────────────────────────────────────────────────────

def parse_tasks(text: str) -> dict[str, tuple[int, str]]:
    """{task_id: (line_index, state)}; raises on duplicate IDs."""
    out: dict[str, tuple[int, str]] = {}
    for i, line in _unfenced(text.splitlines()):
        m = TASK_RE.match(line)
        if m:
            tid = f"T{m.group(2)}"
            if tid in out:
                raise DevlogError(f"Duplicate task ID {tid} — fix the note manually")
            out[tid] = (i, m.group(1))
    return out


def next_task_id(text: str) -> str:
    ids = [int(t[1:]) for t in parse_tasks(text)]
    return f"T{max(ids, default=0) + 1}"


def add_task(text: str, description: str, ac: list[str] | None = None) -> tuple[str, str]:
    """Append a new open task; returns (new_text, task_id)."""
    description = _one_line(description)
    if not description:
        raise DevlogError("description is required for a new task")
    tid = next_task_id(text)
    lines = [f"- [ ] {tid} {description}"] + [f"  - AC: {_one_line(a)}" for a in ac or [] if a.strip()]
    body = text.rstrip("\n")
    last = body.splitlines()[-1] if body else ""
    sep = "\n" if TASK_RE.match(last) or last.startswith("  - ") else "\n\n"
    return body + sep + "\n".join(lines) + "\n", tid


def set_task_state(text: str, task_id: str, state: str,
                   reason: str | None = None, decision: str | None = None) -> str:
    """Change a task's state and rebuild its trailing reason / decision annotations."""
    if state not in TASK_STATES:
        raise DevlogError(f"Invalid state '{state}' — use one of {', '.join(repr(s) for s in TASK_STATES)}")
    if decision is not None and not DECISION_ID_RE.match(decision):
        raise DevlogError(f"Invalid decision ID '{decision}' — expected D<number>")
    if reason and state not in ("!", "-"):
        raise DevlogError("reason is only allowed for states '!' (blocked) and '-' (cancelled)")
    if state == "!" and not reason:
        raise DevlogError("state '!' (blocked) requires reason")
    if state == "-" and not (reason or decision):
        raise DevlogError("state '-' (cancelled) requires reason or decision")
    tasks = parse_tasks(text)
    if task_id not in tasks:
        raise DevlogError(f"Task {task_id} not found")
    lines = text.splitlines()
    idx, _ = tasks[task_id]
    rest = TASK_RE.match(lines[idx]).group(3) or ""
    m = _SUFFIX_RE.search(rest)
    description = rest[:m.start()]
    decisions = m.group(1).split(", ") if m.group(1) else []
    if decision and decision not in decisions:
        decisions.append(decision)
    line = f"- [{state}] {task_id} {description}".rstrip()
    if reason:
        line += f" — {'blocked' if state == '!' else 'cancelled'}: {_one_line(reason)}"
    if decisions:
        line += f" → {', '.join(decisions)}"
    lines[idx] = line
    return "\n".join(lines) + "\n"


# ── PROGRESS ────────────────────────────────────────────────────────────────

def _cell(value: str) -> str:
    return _one_line(value).replace("|", "\\|")


def append_log(text: str, today: str, tasks: list[str], summary: str, implementation: str) -> str:
    """Append a log row under today's date heading, creating the heading when needed."""
    if not tasks:
        raise DevlogError("tasks must list at least one task ID")
    for t in tasks:
        if not TASK_ID_RE.match(t):
            raise DevlogError(f"Invalid task ID '{t}' — expected T<number>")
    if not summary.strip() or not implementation.strip():
        raise DevlogError("summary and implementation are required")
    row = f"| {', '.join(tasks)} | {_cell(summary)} | {_cell(implementation)} |"
    last_date = None
    for _, line in _unfenced(text.splitlines()):
        m = DATE_HEADING_RE.match(line)
        if m:
            last_date = m.group(1)
    if last_date == today:
        body = text.rstrip("\n")
        if not body.splitlines()[-1].startswith("|"):
            body += "\n\n" + PROGRESS_HEADER
        return body + "\n" + row + "\n"
    return _append_block(text, f"## {today}\n{PROGRESS_HEADER}\n{row}")


# ── DECISIONS ───────────────────────────────────────────────────────────────

def parse_decisions(text: str) -> dict[str, tuple[int, str]]:
    """{decision_id: (line_index, status)}; raises on duplicate IDs."""
    out: dict[str, tuple[int, str]] = {}
    for i, line in _unfenced(text.splitlines()):
        m = DECISION_RE.match(line)
        if m:
            did = f"D{m.group(1)}"
            if did in out:
                raise DevlogError(f"Duplicate decision ID {did} — fix the note manually")
            out[did] = (i, m.group(4))
    return out


def next_decision_id(text: str) -> str:
    ids = [int(d[1:]) for d in parse_decisions(text)]
    return f"D{max(ids, default=0) + 1}"


def _check_status(status: str) -> None:
    if not STATUS_RE.match(status):
        raise DevlogError(f"Invalid status '{status}' — use accepted, rejected or 'superseded by D<n>'")


def add_decision(text: str, today: str, title: str, context: str, decision: str,
                 rejected: list[str] | None = None, consequences: str | None = None,
                 tasks: list[str] | None = None, status: str = "accepted") -> tuple[str, str]:
    """Append a new decision entry; returns (new_text, decision_id)."""
    _check_status(status)
    if status.startswith("superseded"):
        raise DevlogError("a new decision cannot start as superseded")
    if not (title.strip() and context.strip() and decision.strip()):
        raise DevlogError("title, context and decision are required for a new decision")
    for t in tasks or []:
        if not TASK_ID_RE.match(t):
            raise DevlogError(f"Invalid task ID '{t}' — expected T<number>")
    did = next_decision_id(text)
    lines = [f"## {did} — {today} — {_one_line(title)}  [{status}]",
             f"Context:   {_one_line(context)}",
             f"Decision:  {_one_line(decision)}"]
    if rejected:
        lines.append("Rejected:")
        lines += [f"  - {_one_line(r)}" for r in rejected if r.strip()]
    if consequences:
        lines.append(f"Consequences: {_one_line(consequences)}")
    if tasks:
        lines.append(f"Tasks: {', '.join(tasks)}")
    return _append_block(text, "\n".join(lines)), did


def set_decision_status(text: str, decision_id: str, status: str) -> str:
    _check_status(status)
    decisions = parse_decisions(text)
    if decision_id not in decisions:
        raise DevlogError(f"Decision {decision_id} not found")
    if status == f"superseded by {decision_id}":
        raise DevlogError("a decision cannot supersede itself")
    lines = text.splitlines()
    idx, _ = decisions[decision_id]
    m = DECISION_RE.match(lines[idx])
    lines[idx] = f"## {decision_id} — {m.group(2)} — {m.group(3)}  [{status}]"
    return "\n".join(lines) + "\n"
