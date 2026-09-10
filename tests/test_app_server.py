"""Real loopback HTTP tests; no live daemon or generation worker is started."""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from zotero_audio.app_library import artifact_path
from zotero_audio.app_server import make_handler
from zotero_audio.app_state import Store


@pytest.fixture
def local_server(tmp_path):
    store = Store(tmp_path / "runtime")
    markdown = ("# A private article with a complete title\n\n" + "Research about successful AI adoption [9].\n" * 2000
                + "\nFinal paragraph with a source link: https://example.org/research\n")
    path = tmp_path / "Private research.md"
    path.write_text(markdown)
    audio = tmp_path / "Private Brief.m4a"
    audio.write_bytes(bytes(range(256)) * 30)
    article = {"id": "ARTICLE1", "title": "A private article with a complete title", "authors": ["Anna Author"],
               "markdown": str(path), "review": str(tmp_path / "missing-review.md"), "source_path": str(tmp_path / "source.pdf"),
               "editions": {"brief": {"title": "A private article with a complete title", "audio": str(audio)}},
               "license_status": "private", "artifacts": {"markdown": True, "audio": True}}
    store.put_article(article, markdown=markdown)
    store.set_state("local_api_token", "test-local-api-credential")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(store, None, port)
    httpd.daemon_threads = True
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    def request(method, route, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, route, body=body, headers=headers or {})
            response = connection.getresponse()
            return SimpleNamespace(status=response.status, headers=dict(response.getheaders()), body=response.read())
        finally:
            connection.close()

    yield SimpleNamespace(store=store, article=article, request=request, port=port,
                          origin=f"http://127.0.0.1:{port}", markdown=markdown, markdown_path=path, audio=audio)
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def credentials(server, *, browser=False):
    return {"Content-Type": "application/json", **({"Origin": server.origin} if browser else {"Authorization": "Bearer test-local-api-credential"})}


def test_full_private_markdown_is_not_truncated_and_head_has_no_body(local_server):
    server = local_server
    response = server.request("GET", "/api/articles/ARTICLE1/markdown")
    assert response.status == 200
    assert response.body.decode() == server.markdown
    assert int(response.headers["Content-Length"]) == len(response.body)
    assert response.headers["Content-Type"].startswith("text/markdown")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "Access-Control-Allow-Origin" not in response.headers
    head = server.request("HEAD", "/api/articles/ARTICLE1/markdown")
    assert head.status == 200 and not head.body
    assert head.headers["Content-Length"] == response.headers["Content-Length"]


@pytest.mark.parametrize("range_header, start, end", [("bytes=2-9", 2, 9), ("bytes=7675-", 7675, 7679),
                                                     ("bytes=-5", 7675, 7679), ("bytes=7675-99999", 7675, 7679)])
def test_audio_byte_ranges_support_playback_and_seeking(local_server, range_header, start, end):
    server = local_server
    response = server.request("GET", "/api/articles/ARTICLE1/audio?edition=brief", headers={"Range": range_header})
    assert response.status == 206
    assert response.body == server.audio.read_bytes()[start:end + 1]
    assert response.headers["Content-Range"] == f"bytes {start}-{end}/7680"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Type"] == "audio/mp4"
    assert int(response.headers["Content-Length"]) == len(response.body)


@pytest.mark.parametrize("range_header", ["bytes=7680-", "bytes=9-2", "bytes=-0", "bytes=", "bytes=0-1,4-5", "things=2-4"])
def test_invalid_and_unsatisfiable_ranges_return_explicit_416(local_server, range_header):
    response = local_server.request("GET", "/api/articles/ARTICLE1/audio?edition=brief", headers={"Range": range_header})
    assert response.status == 416
    assert response.headers["Content-Range"] == "bytes */7680"
    assert json.loads(response.body)["error"]["code"] == "invalid_range"


def test_requested_full_never_silently_returns_brief(local_server):
    response = local_server.request("GET", "/api/articles/ARTICLE1/audio?edition=full")
    assert response.status == 404
    assert "Full audio" in json.loads(response.body)["error"]["message"]
    assert local_server.article["title"] in json.loads(response.body)["error"]["message"]
    assert local_server.request("GET", "/api/articles/ARTICLE1/audio?edition=unknown").status == 400
    with pytest.raises(FileNotFoundError, match="Full audio"):
        artifact_path(local_server.article, "audio", "full")


def test_only_registered_artifacts_can_be_read_or_opened(local_server, monkeypatch):
    import zotero_audio.app_server as app_server
    server = local_server
    secret = server.store.runtime / "not-registered.txt"
    secret.write_text("Unregistered private content")
    response = server.request("GET", "/api/articles/ARTICLE1/markdown?path=" + quote(str(secret)))
    assert response.body.decode() == server.markdown
    assert server.request("GET", "/api/files?path=" + quote(str(secret))).status == 404
    assert server.request("GET", "/..%2f..%2f..%2fetc/passwd").status == 403
    assert server.request("GET", "/api/articles/%2e%2e%2f%2e%2e/markdown").status in {403, 404}
    with pytest.raises(ValueError):
        artifact_path(server.article, "source_path")
    opened = []
    monkeypatch.setattr(app_server.subprocess, "run", lambda command, **kwargs: opened.append(command))
    response = server.request("POST", "/api/articles/ARTICLE1/open", headers=credentials(server),
                              body=json.dumps({"artifact": "markdown", "reveal": True, "path": str(secret)}))
    assert response.status == 200
    assert opened == [["/usr/bin/open", "-R", str(server.markdown_path)]]
    response = server.request("POST", "/api/articles/ARTICLE1/open", headers=credentials(server),
                              body=json.dumps({"artifact": "source_path"}))
    assert response.status == 400 and len(opened) == 1


def test_host_and_origin_protect_private_reads(local_server):
    server = local_server
    assert server.request("GET", "/api/library", headers={"Host": "evil.example"}).status == 403
    assert server.request("GET", "/api/articles/ARTICLE1/markdown", headers={"Origin": "https://evil.example"}).status == 403
    assert server.request("GET", "/api/articles/ARTICLE1/markdown", headers={"Origin": "null"}).status == 403
    assert server.request("GET", "/api/library", headers={"Origin": server.origin}).status == 200


def test_mutations_require_json_and_same_origin_or_nonempty_local_credential(local_server):
    server = local_server
    body = json.dumps({"qa_enabled": False})
    assert server.request("PATCH", "/api/settings", body=body, headers={"Content-Type": "application/json"}).status == 403
    assert server.request("PATCH", "/api/settings", body=body, headers={"Content-Type": "application/json", "Authorization": "Bearer wrong"}).status == 403
    assert server.request("PATCH", "/api/settings", body=body, headers={**credentials(server), "Origin": "https://evil.example"}).status == 403
    for content_type in ("text/plain", "application/jsonp", "application/x-www-form-urlencoded"):
        assert server.request("PATCH", "/api/settings", body=body,
                              headers={**credentials(server, browser=True), "Content-Type": content_type}).status == 400
    assert server.store.settings()["qa_enabled"] is True
    assert server.request("PATCH", "/api/settings", body=body, headers=credentials(server, browser=True)).status == 200
    assert server.store.settings()["qa_enabled"] is False
    assert server.request("PATCH", "/api/settings", body='{"qa_enabled":true}', headers=credentials(server)).status == 200
    server.store.set_state("local_api_token", "")
    assert server.request("PATCH", "/api/settings", body=body,
                          headers={"Content-Type": "application/json", "Authorization": "Bearer "}).status == 403


def test_invalid_json_or_oversized_bodies_cannot_change_settings(local_server):
    server = local_server
    for body in ("[]", "null", "not json"):
        assert server.request("PATCH", "/api/settings", body=body, headers=credentials(server)).status == 400
    response = server.request("PATCH", "/api/settings", body="{}", headers={**credentials(server), "Content-Length": "1048577"})
    assert response.status == 400
    assert server.store.settings()["qa_enabled"] is True


def test_http_retry_is_idempotent_and_never_repeats_force(local_server):
    server = local_server
    old = server.store.create_job({"action": "full", "scope": "one", "article_id": "ARTICLE1", "force": True})
    server.store.update_job(old["id"], status="failed")
    route = f"/api/jobs/{old['id']}/retry"
    first = server.request("POST", route, body="{}", headers=credentials(server))
    second = server.request("POST", route, body="{}", headers=credentials(server))
    assert first.status == second.status == 201
    first_job, second_job = json.loads(first.body)["job"], json.loads(second.body)["job"]
    assert first_job["id"] == second_job["id"] != old["id"]
    assert first_job["force"] is False
    assert first_job["title"] == server.article["title"]
    assert len(server.store.jobs()["jobs"]) == 2


def test_serving_an_open_revision_survives_atomic_markdown_replacement(local_server, monkeypatch):
    import zotero_audio.app_server as app_server
    server = local_server
    original_stat = server.markdown_path.stat()
    original_fstat = app_server.os.fstat
    replaced = []

    def fstat_with_replacement(fd):
        info = original_fstat(fd)
        if info.st_ino == original_stat.st_ino and not replaced:
            replacement = server.markdown_path.with_suffix(".new")
            replacement.write_text("A later edited Markdown revision")
            replacement.replace(server.markdown_path)
            replaced.append(True)
        return info

    monkeypatch.setattr(app_server.os, "fstat", fstat_with_replacement)
    response = server.request("GET", "/api/articles/ARTICLE1/markdown")
    assert replaced
    assert response.status == 200
    assert response.body.decode() == server.markdown
    assert int(response.headers["Content-Length"]) == len(response.body)
    assert server.markdown_path.read_text() == "A later edited Markdown revision"
