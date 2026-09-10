from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from zotero_audio.app_projects import ProjectDocuments, git, sha, source_path
from zotero_audio.app_state import Store


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "phd"
    root.mkdir()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test Researcher")
    git(root, "config", "user.email", "researcher@example.test")
    (root / "NOW.md").write_text("# Current PhD work\n\nReview the synthetic interview guide.\n")
    (root / "PROJECT.md").write_text("# Synthetic research project\n\nProject facts.\n")
    (root / ".gitignore").write_text("private/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial synthetic documents")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    store = Store(tmp_path / "runtime")
    (store.root / "projects.json").write_text(json.dumps({"phd": {"enabled": True, "root": str(root), "write_enabled": True}}))
    return root, ProjectDocuments(store)


def test_inventory_reads_current_saved_text_and_reports_missing_next(project):
    root, docs = project
    (root / "NOW.md").write_text("# Current PhD work\n\nSaved but uncommitted changes.\n")
    result = docs.whats_next()
    assert result["missing"] == ["NEXT.md"]
    assert "uncommitted" in result["documents"][0]["text"]
    assert docs.search_documents("uncommitted")["results"][0]["path"] == "NOW.md"
    assert docs.list_documents(limit=1)["next_offset"] == 1


def test_hidden_ignored_untracked_and_symlink_files_are_not_read(project):
    root, docs = project
    (root / "untracked.md").write_text("Private untracked text")
    (root / "private").mkdir()
    (root / "private" / "note.md").write_text("Ignored private text")
    (root / "escape.md").symlink_to(root.parent / "outside.md")
    (root.parent / "outside.md").write_text("Outside text")
    git(root, "add", "escape.md")
    records, warnings = docs.inventory()
    assert set(records) == {"NOW.md", "PROJECT.md"}
    assert any("escape.md" in warning for warning in warnings)
    for path in ["../outside.md", "/NOW.md", ".git/config", "private/../NOW.md", "escape.md", "a\\b.md"]:
        with pytest.raises(ValueError):
            source_path(root, path)


def test_unsupported_files_have_explicit_warnings_and_pdf_has_page_markers(project):
    from pypdf import PdfWriter
    root, docs = project
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(root / "source.pdf")
    (root / "diagram.png").write_bytes(b"synthetic")
    git(root, "add", "source.pdf", "diagram.png")
    records, warnings = docs.inventory()
    assert "## Page 1" in records["source.pdf"]["text"]
    assert "No extractable text" in records["source.pdf"]["text"]
    assert "diagram.png" in " ".join(warnings)


def change(docs, **overrides):
    return {"id": "synthetic-change-001", "path": "NOW.md", "text": "# Current PhD work\n\nA documented next step.\n",
            "expected_revision": docs.read_document("NOW.md")["revision"], **overrides}


def test_save_preserves_unrelated_staged_changes_and_is_idempotent(project):
    root, docs = project
    (root / "PROJECT.md").write_text("# Unrelated local edits\n")
    git(root, "add", "PROJECT.md")
    request = change(docs)
    receipt = docs.apply_change(request)
    assert receipt["status"] == "completed", receipt
    assert receipt["result"]["pushed"] is True
    assert git(root, "diff", "--cached", "--name-only").stdout.strip() == b"PROJECT.md"
    assert git(root, "show", "--format=", "--name-only", "HEAD").stdout.strip() == b"NOW.md"
    assert docs.apply_change(request) == receipt
    assert git(root, "rev-parse", "HEAD").stdout.strip().decode() == receipt["result"]["commit"]
    assert (docs.store.root / "project-change-backups" / request["id"] / "original.md").exists()


def test_intervening_edit_returns_conflict_without_overwrite(project):
    root, docs = project
    request = change(docs)
    (root / "NOW.md").write_text("# A newer saved edit\n")
    result = docs.apply_change(request)
    assert result["status"] == "conflict"
    assert (root / "NOW.md").read_text() == "# A newer saved edit\n"


def test_new_meeting_note_is_tracked_committed_and_pushed(project):
    root, docs = project
    request = change(docs, path="admin/meetings/2026-09-10-synthetic.md", expected_revision=None)
    result = docs.apply_change(request)
    assert result["status"] == "completed", result
    assert git(root, "show", "origin/main:" + request["path"]).stdout.decode() == request["text"]


def test_staged_target_or_unpublished_commit_blocks_write(project):
    root, docs = project
    (root / "NOW.md").write_text("# Staged current work\n")
    git(root, "add", "NOW.md")
    request = change(docs)
    assert docs.apply_change(request)["status"] == "conflict"
    git(root, "commit", "-m", "Unrelated unpublished commit")
    request["id"] = "synthetic-change-002"
    assert docs.apply_change(request)["status"] == "conflict"
    assert (root / "NOW.md").read_text() == "# Staged current work\n"


def test_invalid_and_ignored_write_targets_cannot_escape_project(project):
    root, docs = project
    for i, path in enumerate(["../outside.md", "private/hidden.md", ".git/config.md", "script.py"]):
        receipt = docs.apply_change(change(docs, id=f"synthetic-invalid-{i}", path=path, expected_revision=None))
        assert receipt["status"] == "failed"
    assert not (root.parent / "outside.md").exists()
    assert not (root / "private" / "hidden.md").exists()


def test_sync_uploads_only_changed_documents(project):
    root, docs = project
    calls = []
    bridge = SimpleNamespace(worker_id="test-worker", worker=SimpleNamespace(stop=threading.Event()))
    def request(method, path, data=None):
        calls.append((method, path, data))
        return {"changes": []} if method == "GET" else {"ok": True}
    bridge.request = request
    docs.sync(bridge)
    assert sum(method == "PUT" for method, _, _ in calls) == 2
    calls.clear()
    docs.sync(bridge)
    assert not any(method == "PUT" for method, _, _ in calls)
    (root / "NOW.md").write_text("# Current work\n\nChanged locally.\n")
    docs.sync(bridge)
    assert sum(method == "PUT" for method, _, _ in calls) == 1
    config_path = docs.store.root / "projects.json"
    config = json.loads(config_path.read_text())
    config["phd"]["exclude"] = ["NOW.md"]
    config_path.write_text(json.dumps(config))
    docs.sync(bridge)
    assert "NOW.md" not in docs.store.state("project_uploaded")
    config["phd"]["exclude"] = []
    config_path.write_text(json.dumps(config))
    calls.clear()
    docs.sync(bridge)
    assert [data["document"]["path"] for method, _, data in calls if method == "PUT"] == ["NOW.md"]


def test_lost_acknowledgement_uses_durable_receipt_after_restart(project):
    root, docs = project
    request = change(docs)
    first = docs.apply_change(request)
    restarted = ProjectDocuments(Store(docs.store.runtime))
    assert restarted.apply_change(request) == first
    assert git(root, "rev-list", "--count", "HEAD").stdout.strip() == b"2"
    with pytest.raises(ValueError, match="reused"):
        restarted.apply_change({**request, "text": "Different request"})


def test_push_failure_reports_saved_content_and_unpushed_commit(project, monkeypatch):
    import zotero_audio.app_projects as module
    root, docs = project
    real_git = module.git
    def blocked_push(root, *args, **kwargs):
        if args[0] == "push":
            raise RuntimeError("Synthetic network failure")
        return real_git(root, *args, **kwargs)
    monkeypatch.setattr(module, "git", blocked_push)
    request = change(docs)
    receipt = docs.apply_change(request)
    assert receipt["status"] == "failed"
    assert receipt["result"]["pushed"] is False
    assert receipt["result"]["commit"]
    assert (root / "NOW.md").read_text() == request["text"]
    assert "blocked" in receipt["result"]["message"]


def test_project_tools_work_over_real_stdio(project):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root, docs = project
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-c",
            "from zotero_audio.app_mcp import create_server; create_server().run(transport='stdio')"],
            env={**os.environ, "ZOTERO_AUDIO_RUNTIME": str(docs.store.runtime)})
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert {"whats_next", "list_documents", "read_document", "search_documents", "save_document"} <= tools
                found = await session.call_tool("search", {"query": "interview"})
                hit = found.structuredContent["results"][0]
                assert hit["id"] == "project:phd:NOW.md"
                assert "/api/project/document?path=" in hit["url"]
                result = await session.call_tool("fetch", {"id": hit["id"]})
                assert result.structuredContent["text"] == (root / "NOW.md").read_text()
                saved = await session.call_tool("save_document", {"path": "NEXT.md", "text": "# Synthetic next step\n", "expected_revision": None, "request_id": "stdio-synthetic-note"})
                assert saved.structuredContent["status"] == "completed"
                current = await session.call_tool("whats_next", {})
                assert current.structuredContent["missing"] == []
    asyncio.run(run())
