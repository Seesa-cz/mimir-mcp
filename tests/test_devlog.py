"""Tests for mimir.devlog — pure parse/render functions of the dev history format."""

import pytest

from mimir import devlog
from mimir.devlog import DevlogError

TASKS = """# Tasks — demo

- [x] T1 First task → D1
- [ ] T3 Third task
  - AC: works

```
- [ ] T99 inside a code fence
```
"""


def test_check_slug():
    devlog.check_slug("mimir-mcp")
    for bad in ("Mimir", "../x", "", "-x", "a/b"):
        with pytest.raises(DevlogError):
            devlog.check_slug(bad)


def test_parse_tasks_ignores_fences_and_gaps():
    tasks = devlog.parse_tasks(TASKS)
    assert set(tasks) == {"T1", "T3"}
    assert tasks["T1"][1] == "x"
    assert devlog.next_task_id(TASKS) == "T4"


def test_parse_tasks_duplicate_id():
    with pytest.raises(DevlogError, match="Duplicate"):
        devlog.parse_tasks("- [ ] T1 a\n- [x] T1 b\n")


def test_add_task_with_ac():
    text, tid = devlog.add_task(TASKS, "New  task\nwrapped", ["crit one", " "])
    assert tid == "T4"
    assert text.endswith("- [ ] T4 New task wrapped\n  - AC: crit one\n")


def test_add_task_to_skeleton():
    text, tid = devlog.add_task(devlog.skeleton("TASK", "demo", "2026-11-02"), "x")
    assert tid == "T1"
    assert text == "# Tasks — demo\n\n- [ ] T1 x\n"


def test_add_task_requires_description():
    with pytest.raises(DevlogError):
        devlog.add_task(TASKS, "  ")


def test_set_task_state_blocked_then_done_keeps_decision():
    text = devlog.set_task_state(TASKS, "T1", "!", reason="needs access")
    assert "- [!] T1 First task — blocked: needs access → D1" in text
    text = devlog.set_task_state(text, "T1", "x", decision="D2")
    assert "- [x] T1 First task → D1, D2" in text


def test_set_task_state_cancelled():
    text = devlog.set_task_state(TASKS, "T3", "-", reason="obsolete")
    assert "- [-] T3 Third task — cancelled: obsolete" in text
    assert "  - AC: works" in text


@pytest.mark.parametrize("kwargs", [
    dict(task_id="T3", state="?"),
    dict(task_id="T3", state="!"),
    dict(task_id="T3", state="-"),
    dict(task_id="T3", state="x", reason="why"),
    dict(task_id="T3", state="x", decision="7"),
    dict(task_id="T99", state="x"),
])
def test_set_task_state_invalid(kwargs):
    with pytest.raises(DevlogError):
        devlog.set_task_state(TASKS, **kwargs)


def test_append_log_creates_and_reuses_heading():
    text = devlog.skeleton("PROGRESS", "demo", "2026-11-01")
    text = devlog.append_log(text, "2026-11-01", ["T1"], "Did a | b", "f.py: x")
    text = devlog.append_log(text, "2026-11-01", ["T2", "T3"], "Next", "g.py")
    text = devlog.append_log(text, "2026-11-02", ["T4"], "Other\nday", "h.py")
    assert text.count("## 2026-11-01") == 1
    assert text.count(devlog.PROGRESS_HEADER) == 2
    assert "| T1 | Did a \\| b | f.py: x |\n| T2, T3 | Next | g.py |" in text
    assert text.endswith("## 2026-11-02\n" + devlog.PROGRESS_HEADER + "\n| T4 | Other day | h.py |\n")


def test_append_log_invalid():
    with pytest.raises(DevlogError):
        devlog.append_log("", "2026-11-02", [], "s", "i")
    with pytest.raises(DevlogError):
        devlog.append_log("", "2026-11-02", ["42"], "s", "i")
    with pytest.raises(DevlogError):
        devlog.append_log("", "2026-11-02", ["T1"], " ", "i")


def test_add_decision_and_status():
    text = devlog.skeleton("DECISIONS", "demo", "2026-11-02")
    text, did = devlog.add_decision(text, "2026-11-02", "Use X", "ctx", "X",
                                    rejected=["Y — slow"], consequences="c", tasks=["T1"])
    assert did == "D1"
    assert "## D1 — 2026-11-02 — Use X  [accepted]\nContext:   ctx\nDecision:  X\nRejected:\n  - Y — slow\n" in text
    text, did = devlog.add_decision(text, "2026-11-03", "Tried Z", "ctx", "Z", status="rejected")
    assert did == "D2"
    assert devlog.parse_decisions(text) == {"D1": (2, "accepted"), "D2": (10, "rejected")}
    text = devlog.set_decision_status(text, "D1", "superseded by D2")
    assert "## D1 — 2026-11-02 — Use X  [superseded by D2]" in text


@pytest.mark.parametrize("args", [
    ("", "c", "d"),
    ("t", "c", ""),
])
def test_add_decision_requires_fields(args):
    with pytest.raises(DevlogError):
        devlog.add_decision("", "2026-11-02", *args)


def test_decision_status_invalid():
    text, _ = devlog.add_decision("", "2026-11-02", "t", "c", "d")
    with pytest.raises(DevlogError):
        devlog.add_decision(text, "2026-11-02", "t", "c", "d", status="superseded by D1")
    for status in ("done", "superseded by D1"):
        with pytest.raises(DevlogError):
            devlog.set_decision_status(text, "D1", status)
    with pytest.raises(DevlogError):
        devlog.set_decision_status(text, "D5", "rejected")
