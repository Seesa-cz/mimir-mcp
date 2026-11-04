"""Tool-level tests on a temporary vault — never point these at the production vault."""

from pathlib import Path

import frontmatter
import pytest
import yaml

from mimir import tools
from mimir.config import Config


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **_):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "index_file", lambda *a, **k: None)
    index_dir = tmp_path / "vault"
    write_dir = index_dir / "kb"
    write_dir.mkdir(parents=True)
    cfg = Config(index_dir=index_dir, write_dir=write_dir, port=0, chroma_dir=tmp_path / "c", model="",
                 idle_timeout=0, sync_interval=0, log_level="INFO", log_file=tmp_path / "l",
                 max_log_mb=1, max_log_files=1, usage_log_file=tmp_path / "u", auth_token=None,
                 bind_host="127.0.0.1", upgrade_check=False, upgrade_check_interval=0)
    mcp = FakeMCP()
    tools.register_tools(mcp, cfg, store=None, touch_fn=lambda: None)
    mcp.tools["write_dir"] = write_dir
    return mcp.tools


def test_read_note_pagination(t):
    note = t["write_dir"] / "n.md"
    note.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
    assert t["read_note"]("kb/n.md") == note.read_text()
    assert t["read_note"]("kb/n.md", limit=2) == "[lines 1–2 of 10]\nline1\nline2"
    assert t["read_note"]("kb/n.md", offset=8) == "[lines 9–10 of 10]\nline9\nline10"
    assert t["read_note"]("kb/n.md", offset=-3, limit=2) == "[lines 8–9 of 10]\nline8\nline9"
    assert t["read_note"]("kb/n.md", offset=-50, limit=1) == "[lines 1–1 of 10]\nline1"
    assert "beyond the end" in t["read_note"]("kb/n.md", offset=10)
    assert "positive" in t["read_note"]("kb/n.md", limit=0)


def test_dev_scaffold_and_flow(t):
    assert t["dev_task"]("demo", description="First", ac=["works"]) == \
        "Added T1 to kb/projects/demo/dev/TASK.md"
    dev = t["write_dir"] / "projects" / "demo" / "dev"
    assert sorted(p.name for p in dev.glob("*.md")) == ["DECISIONS.md", "PROGRESS.md", "STATUS.md", "TASK.md"]
    meta = yaml.safe_load((dev / ".mimir.yaml").read_text())
    assert meta["search_weight"] == 0.5
    assert (dev.parent / ".mimir.yaml").exists()
    post = frontmatter.load(str(dev / "TASK.md"))
    assert post["tags"] == ["dev-history", "demo"]

    assert t["dev_decision"]("demo", title="Use X", context="c", decision="X",
                             rejected=["Y — slow"], tasks=["T1"]).startswith("Added D1")
    assert t["dev_task"]("demo", id="T1", state="x", decision="D1").startswith("T1 set to [x]")
    assert t["dev_log"]("demo", ["T1"], "Done", "f.py").startswith("Logged T1")
    assert "- [x] T1 First → D1" in (dev / "TASK.md").read_text()
    assert "| T1 | Done | f.py |" in (dev / "PROGRESS.md").read_text()
    assert t["dev_decision"]("demo", id="D1", status="rejected").startswith("D1 status set")


def test_dev_errors(t):
    assert t["dev_task"]("Bad/Slug", description="x").startswith("Error")
    assert t["dev_task"]("..", description="x").startswith("Error")
    assert t["dev_task"]("demo", description="x", state="x").startswith("New task")
    assert t["dev_task"]("demo", id="T9", state="x").startswith("Error")
    assert t["dev_task"]("demo", id="T1").startswith("Error")
    assert "unknown task" in t["dev_log"]("demo", ["T7"], "s", "i")
    assert t["dev_decision"]("demo", title="t").startswith("Error")
    assert not any(Path(t["write_dir"]).glob("projects/Bad*"))
