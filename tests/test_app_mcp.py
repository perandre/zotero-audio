"""Exercise actual JSON-RPC stdio with the official MCP Python client."""
import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from zotero_audio.app_state import Store


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path)
    markdown = "# A company that successfully adopted AI\n\n" + "A private company improved customer service using AI.\n" * 1200
    markdown += "\nFinal evidence marker: productivity increased while error rates fell.\n"
    path = tmp_path / "article.md"
    path.write_text(markdown)
    review = tmp_path / "ai-review.md"
    review.write_text("# Full review\n\nCompare findings with the source PDF.\n\n" + markdown)
    (tmp_path / "qa-report.json").write_text(json.dumps({"status": "ready_with_warnings", "findings": [{"code": "layout-review"}]}))
    store.put_article({"id": "PRIVATE01", "title": "A company that successfully adopted AI",
                       "authors": ["Anna Author"], "year": "2025", "markdown": str(path),
                       "review": str(review), "qa_status": "warnings", "license_status": "private",
                       "editions": {}, "artifacts": {"markdown": True, "review": True}}, markdown=markdown)
    return store, markdown


def server_parameters(runtime):
    # Use the same entrypoint implementation with worker startup disabled by
    # dependency injection. No real extraction/audio work occurs in this test.
    return StdioServerParameters(command=sys.executable, args=["-c",
        "from zotero_audio.app_mcp import create_server; create_server().run(transport='stdio')"],
        env={**os.environ, "ZOTERO_AUDIO_RUNTIME": str(runtime)})


def test_official_stdio_handshake_discovery_full_private_retrieval_and_errors(library):
    store, markdown = library

    async def run():
        async with stdio_client(server_parameters(store.runtime)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=10)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "1 More Paper"
                assert initialized.protocolVersion
                tools = (await session.list_tools()).tools
                by_name = {tool.name: tool for tool in tools}
                assert {"search", "fetch", "create_job", "get_job", "cancel_job", "retry_job", "review_article"} <= set(by_name)
                assert by_name["search"].annotations.readOnlyHint is True
                assert by_name["create_job"].annotations.readOnlyHint is False
                assert by_name["fetch"].outputSchema is not None

                found = await session.call_tool("search", {"query": "productivity"})
                assert not found.isError
                data = found.structuredContent
                assert data["results"][0]["title"] == "A company that successfully adopted AI"
                assert data["results"][0]["url"] == "http://127.0.0.1:8765/articles/PRIVATE01"
                fetched = await session.call_tool("fetch", {"id": data["results"][0]["id"]})
                assert fetched.structuredContent["text"] == markdown
                assert json.loads(fetched.content[0].text)["text"] == markdown
                assert fetched.structuredContent["metadata"]["license_status"] == "private"

                resource = await session.read_resource("article://PRIVATE01/markdown")
                assert resource.contents[0].text == markdown
                review = await session.call_tool("review_article", {"id": "PRIVATE01"})
                assert "Final evidence marker" in review.structuredContent["text"]
                assert review.structuredContent["complete"]

                missing = await session.call_tool("fetch", {"id": "../../etc/passwd"})
                assert missing.isError
                invalid = await session.call_tool("create_job", {"action": "shell", "scope": "all"})
                assert invalid.isError
                page = await session.call_tool("list_library", {"limit": 1})
                assert page.structuredContent["items"][0]["title"] == "A company that successfully adopted AI"

    asyncio.run(run())


def test_job_idempotency_cancellation_and_retry_persist_across_stdio_connections(library):
    store, _ = library

    async def run():
        async with stdio_client(server_parameters(store.runtime)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                request = {"action": "markdown", "scope": "one", "article_id": "PRIVATE01", "idempotency_key": "one-intended-request"}
                queued = await session.call_tool("create_job", request)
                repeated = await session.call_tool("create_job", request)
                assert queued.structuredContent["id"] == repeated.structuredContent["id"]
                job_id = queued.structuredContent["id"]
                assert queued.structuredContent["title"] == "A company that successfully adopted AI"
                assert queued.structuredContent["status"] == "queued"
                cancelled = await session.call_tool("cancel_job", {"id": job_id})
                assert cancelled.structuredContent["status"] == "cancelled"

        async with stdio_client(server_parameters(store.runtime)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                persisted = await session.call_tool("get_job", {"id": job_id})
                assert persisted.structuredContent["status"] == "cancelled"
                retry = await session.call_tool("retry_job", {"id": job_id})
                again = await session.call_tool("retry_job", {"id": job_id})
                assert retry.structuredContent["id"] == again.structuredContent["id"]
                assert retry.structuredContent["id"] != job_id
                assert retry.structuredContent["force"] is False
                assert retry.structuredContent["status"] == "queued"
                assert len((await session.call_tool("list_jobs", {})).structuredContent["jobs"]) == 2

    asyncio.run(run())
