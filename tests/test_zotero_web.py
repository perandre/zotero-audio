"""Zotero Web API discovery against a fake local HTTP server (synthetic data only)."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from zotero_audio import zotero_web
from zotero_audio.zotero_local import ZoteroLocalAPI, ZoteroLocalAPIUnavailable
from zotero_audio.zotero_web import ZoteroWebAPI, ZoteroWebAuthError, make_zotero_api

KEY = "K" * 24
PDF_A = b"%PDF-1.4 synthetic A"
PDF_B = b"%PDF-1.4 synthetic B"


def md5(data):
    return hashlib.md5(data).hexdigest()


class Library:
    def __init__(self, seed=True):
        self.version = 10
        self.items = {}      # key -> (version, data)
        self.collections = {}
        self.deleted_items = []
        self.files = {}
        self.log = []        # (path, query, headers)
        self.fail = []       # queued (status, headers) responses for /items
        self.corrupt = set()
        self.groups = {}     # group id -> (name, Library)
        if not seed:
            return
        self.add_parent("PARENT01", "A Synthetic Paper", collections=["CHILD001"])
        self.add_pdf("ATTACH01", "PARENT01", PDF_A)
        self.collections["ROOT0001"] = (5, {"name": "Research", "parentCollection": False})
        self.collections["CHILD001"] = (5, {"name": "Podcast Queue", "parentCollection": "ROOT0001"})

    def bump(self):
        self.version += 1
        return self.version

    def add_parent(self, key, title, collections=(), doi="10.1/x"):
        self.items[key] = (self.bump(), {"itemType": "journalArticle", "title": title, "date": "2026-04-05",
            "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Smith"}],
            "tags": [{"tag": "podcast"}], "collections": list(collections), "DOI": doi})

    def add_pdf(self, key, parent, content, **extra):
        data = {"itemType": "attachment", "parentItem": parent, "linkMode": "imported_file",
                "contentType": "application/pdf", "filename": key + ".pdf", "md5": md5(content), **extra}
        self.items[key] = (self.bump(), data)
        self.files[key] = content


@pytest.fixture
def server():
    lib = Library()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=b"", headers=None):
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send(405)
        do_PUT = do_PATCH = do_DELETE = do_POST

        def do_GET(self):
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            lib.log.append((url.path, query, dict(self.headers)))
            if url.path.startswith("/storage/"):
                assert "Zotero-API-Key" not in self.headers
                _, _, library, key, _ = url.path.split("/")
                owner = lib if library == "user" else lib.groups[library][1]
                return self.send(200, owner.files[key])
            if self.headers.get("Zotero-API-Key") != KEY:
                return self.send(403)
            if url.path == "/keys/current":
                return self.send(200, json.dumps({"userID": 42}).encode())
            if url.path == "/users/42/groups":
                rows = [{"id": int(g), "data": {"id": int(g), "name": name}} for g, (name, _) in lib.groups.items()]
                return self.send(200, json.dumps(rows).encode())
            if url.path.startswith("/groups/"):
                group = url.path.split("/")[2]
                base, current, library = f"/groups/{group}/", lib.groups[group][1], group
            else:
                base, current, library = "/users/42/", lib, "user"
            assert url.path.startswith(base)
            name = url.path[len(base):]
            if name.endswith("/file"):
                key = name.split("/")[1]
                if key in current.corrupt:
                    current.files[key] = b"corrupted"
                return self.send(302, headers={"Location": f"http://127.0.0.1:{self.server.server_port}/storage/{library}/{key}/f"})
            if name == "deleted":
                return self.send(200, json.dumps({"items": current.deleted_items, "collections": []}).encode())
            if name == "items" and current.fail:
                status, headers = current.fail.pop(0)
                return self.send(status, headers=headers)
            if name == "items/top":
                table = {k: v for k, v in current.items.items() if not v[1].get("parentItem")}
            else:
                table = {"items": current.items, "collections": current.collections}[name]
            since = int(query.get("since", 0))
            hv = {"Last-Modified-Version": str(current.version)}
            if since and self.headers.get("If-Modified-Since-Version") and since >= current.version:
                return self.send(304, headers=hv)
            rows = [{"key": k, "version": v, "data": d} for k, (v, d) in sorted(table.items()) if v > since]
            start, limit = int(query.get("start", 0)), int(query.get("limit", 100))
            self.send(200, json.dumps(rows[start:start + limit]).encode(), {**hv, "Total-Results": str(len(rows))})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    lib.url = f"http://127.0.0.1:{httpd.server_port}"
    yield lib
    httpd.shutdown()


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(zotero_web, "_NOT_BEFORE", 0.0)
    zotero_web._MEMO.clear()


def client(server, tmp_path, **kw):
    kw.setdefault("sleep", lambda s: None)
    user_id = kw.pop("user_id", 42)
    return ZoteroWebAPI(KEY, user_id, cache_dir=tmp_path / "cache", base_url=server.url, **kw)


class Resp:
    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_downloads_and_verifies_pdf_and_matches_local_metadata_shape(server, tmp_path):
    api = client(server, tmp_path)
    [(key, path)] = api.pdf_attachments()
    assert key == "ATTACH01" and path.read_bytes() == PDF_A
    assert path.parent.name == "ATTACH01" and path.name == md5(PDF_A) + ".pdf"

    def opener(request, timeout):  # the local API fed the same item payloads
        rel = request.full_url.split("/api/users/0/", 1)[1]
        if rel.startswith("collections"):
            return Resp([{"key": k, "data": d} for k, (_, d) in server.collections.items()])
        item = rel.split("/")[1]
        return Resp({"key": item, "data": {**server.items[item][1], "key": item}})

    expected = ZoteroLocalAPI(opener=opener).metadata("ATTACH01")
    assert api.metadata("ATTACH01") == expected
    assert expected["collections"] == ["Podcast Queue", "Research"] and expected["podcast_selected"]
    assert expected["title"] == "A Synthetic Paper"


def test_empty_web_fields_are_dropped_like_the_local_api(server, tmp_path):
    server.items["PARENT01"][1].update(abstractNote="", publisher="", tags=[], language="")
    metadata = client(server, tmp_path).metadata("ATTACH01")
    assert metadata["abstract"] is None and metadata["publisher"] is None
    assert metadata["tags"] == [] and metadata["language"] is None


def test_requests_are_gets_and_key_never_reaches_storage_host(server, tmp_path):
    client(server, tmp_path).pdf_attachments()
    assert any(p.startswith("/storage/") for p, _, _ in server.log)
    for path, _, headers in server.log:
        assert ("Zotero-Api-Key" in headers) == (not path.startswith("/storage/"))


def test_paging_covers_every_item(server, tmp_path):
    for n in range(7):
        server.add_parent(f"PAR{n:05d}", f"Paper {n}")
        server.add_pdf(f"ATT{n:05d}", f"PAR{n:05d}", PDF_B + bytes([n]))
    api = client(server, tmp_path, page_size=3, max_downloads=100)
    assert len(api.pdf_attachments()) == 8
    starts = [q["start"] for p, q, _ in server.log if p == "/users/42/items"]
    assert starts == ["0", "3", "6", "9", "12", "15"]


def test_incremental_sync_uses_since_and_304_and_deletions(server, tmp_path):
    client(server, tmp_path).pdf_attachments()
    version = str(server.version)
    server.log.clear()
    zotero_web._MEMO.clear()
    api = client(server, tmp_path)  # a fresh process: state comes from disk
    assert [k for k, _ in api.pdf_attachments()] == ["ATTACH01"]
    item_calls = [(q, h) for p, q, h in server.log if p == "/users/42/items"]
    assert len(item_calls) == 1 and item_calls[0][0]["since"] == version
    assert item_calls[0][1]["If-Modified-Since-Version"] == version
    assert not any(p.endswith("/file") for p, _, _ in server.log)  # cached, not re-downloaded
    server.add_parent("PARENT02", "Second")
    server.add_pdf("ATTACH02", "PARENT02", PDF_B)
    server.deleted_items = ["ATTACH01"]
    api = client(server, tmp_path)
    assert [k for k, _ in api.pdf_attachments()] == ["ATTACH02"]


def test_changed_md5_replaces_cached_file(server, tmp_path):
    client(server, tmp_path).pdf_attachments()
    server.add_pdf("ATTACH01", "PARENT01", PDF_B)
    [(_, path)] = client(server, tmp_path).pdf_attachments()
    assert path.read_bytes() == PDF_B
    assert [p.name for p in path.parent.glob("*.pdf")] == [md5(PDF_B) + ".pdf"]


def test_md5_mismatch_is_reported_not_cached(server, tmp_path):
    server.corrupt.add("ATTACH01")
    api = client(server, tmp_path)
    assert api.pdf_attachments() == []
    assert "md5 verification" in api.unavailable[0]["reason"]
    assert not list((tmp_path / "cache").rglob("*.pdf*"))


def test_linked_and_unsynced_files_are_listed_as_unavailable(server, tmp_path):
    server.add_parent("PARENT03", "Linked Paper")
    server.add_pdf("LINKED01", "PARENT03", b"x", linkMode="linked_file")
    server.add_pdf("NOMD5001", "PARENT03", b"y")
    server.items["NOMD5001"][1].pop("md5")
    api = client(server, tmp_path)
    assert [k for k, _ in api.pdf_attachments()] == ["ATTACH01"]
    reasons = {u["key"]: u for u in api.unavailable}
    assert set(reasons) == {"LINKED01", "NOMD5001"}
    assert reasons["LINKED01"]["title"] == "Linked Paper" and "linked file" in reasons["LINKED01"]["reason"]
    assert not any("LINKED01" in p for p, _, _ in server.log)


def test_trashed_attachments_are_excluded(server, tmp_path):
    server.items["ATTACH01"][1]["deleted"] = 1
    assert client(server, tmp_path).pdf_attachments() == []


def test_download_budget_defers_remaining_and_retains_them(server, tmp_path):
    server.add_parent("PARENT02", "Second")
    server.add_pdf("ATTACH02", "PARENT02", PDF_B)
    api = client(server, tmp_path, max_downloads=1)
    assert len(api.pdf_attachments()) == 1 and api.retained_keys() == {"ATTACH02"}
    api = client(server, tmp_path, max_downloads=1)
    assert len(api.pdf_attachments()) == 2 and not api.retained_keys()


def test_retry_after_and_server_errors_are_retried(server, tmp_path):
    sleeps = []
    server.fail = [(429, {"Retry-After": "7"}), (503, {})]
    api = client(server, tmp_path, sleep=sleeps.append, clock=lambda: 0.0)
    assert len(api.pdf_attachments()) == 1
    assert 7 in sleeps and len(sleeps) >= 2


def test_backoff_header_delays_next_request(tmp_path):
    sleeps = []

    class R:
        status = 200
        headers = {"Backoff": "5", "Total-Results": "0", "Last-Modified-Version": "1"}

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    api = ZoteroWebAPI(KEY, 1, cache_dir=tmp_path, opener=lambda r, timeout: R(),
                       sleep=sleeps.append, clock=lambda: 100.0)
    api.sync()
    assert sleeps and sleeps[0] == 5  # the second request waited out the Backoff


def test_persistent_rate_limit_is_offline_not_crash(server, tmp_path):
    server.fail = [(429, {"Retry-After": "1"})] * 20
    with pytest.raises(ZoteroLocalAPIUnavailable):
        client(server, tmp_path).pdf_attachments()


def test_bad_key_is_an_auth_error(server, tmp_path):
    api = ZoteroWebAPI("B" * 24, 42, cache_dir=tmp_path, base_url=server.url, sleep=lambda s: None)
    with pytest.raises(ZoteroWebAuthError):
        api.sync()


def test_user_id_is_discovered_from_key(server, tmp_path):
    api = client(server, tmp_path, user_id=None)
    assert len(api.pdf_attachments()) == 1 and api.user_id == "42"


def test_repr_hides_key(server, tmp_path):
    assert KEY not in repr(client(server, tmp_path))


def test_settings_and_factory(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    assert isinstance(make_zotero_api(control, environ={}), ZoteroLocalAPI)
    (control / "zotero.json").write_text(json.dumps({"mode": "web", "user_id": 7}))
    (control / "Zotero API key.txt").write_text(KEY + "\n")
    api = make_zotero_api(control, environ={})
    assert isinstance(api, ZoteroWebAPI) and api.user_id == "7" and api._key == KEY
    assert api.cache_dir == control / "zotero-web"
    assert type(make_zotero_api(control, environ={"ZOTERO_AUDIO_ZOTERO_MODE": "local"})) is ZoteroLocalAPI
    with pytest.raises(ValueError):
        make_zotero_api(control, environ={"ZOTERO_AUDIO_ZOTERO_MODE": "bogus"})
    (control / "Zotero API key.txt").unlink()
    with pytest.raises(ZoteroWebAuthError):
        make_zotero_api(control, environ={})
    assert make_zotero_api(control, environ={"ZOTERO_API_KEY": KEY})._key == KEY


def test_refresh_zotero_in_web_mode_surfaces_unavailable(server, tmp_path):
    from zotero_audio.app_library import refresh_zotero
    from zotero_audio.app_state import Store

    store = Store(tmp_path / "runtime")
    server.add_parent("PARENT03", "Linked Paper")
    server.add_pdf("LINKED01", "PARENT03", b"x", linkMode="linked_file")
    (store.root / "zotero.json").write_text(json.dumps({"mode": "web", "user_id": 42, "base_url": server.url}))
    (store.root / "Zotero API key.txt").write_text(KEY)
    result = refresh_zotero(store)
    assert result == {"online": True, "count": 1}
    article = store.article("ATTACH01")
    assert article["title"] == "A Synthetic Paper" and article["in_zotero"] and article["source_path"].endswith(".pdf")
    state = store.state("zotero")
    assert state["mode"] == "web" and state["unavailable"][0]["title"] == "Linked Paper"


GROUP = "6696698"


def add_group(server, name="synthetic-group", group=GROUP):
    glib = Library(seed=False)
    glib.version = 50
    glib.collections["GCOLL001"] = (51, {"name": "Application", "parentCollection": False})
    glib.add_parent("GPARENT1", "A Group Paper", collections=["GCOLL001"], doi="10.9/group")
    glib.add_pdf("GATTACH1", "GPARENT1", b"%PDF-1.4 group")
    server.groups[group] = (name, glib)
    return glib


def test_groups_are_off_by_default(server, tmp_path):
    add_group(server)
    assert [k for k, _ in client(server, tmp_path).pdf_attachments()] == ["ATTACH01"]
    assert not any(p.startswith("/groups/") or p.endswith("/groups") for p, _, _ in server.log)


def test_group_items_get_qualified_ids_and_library_metadata(server, tmp_path):
    glib = add_group(server)
    api = client(server, tmp_path, groups="all")
    found = dict(api.pdf_attachments())
    assert set(found) == {"ATTACH01", f"g{GROUP}:GATTACH1"}
    personal, grouped = found["ATTACH01"], found[f"g{GROUP}:GATTACH1"]
    assert personal == (tmp_path / "cache/files/ATTACH01" / f"{md5(PDF_A)}.pdf").resolve()  # unchanged path
    assert grouped == (tmp_path / f"cache/groups/{GROUP}/files/GATTACH1/{md5(glib.files['GATTACH1'])}.pdf").resolve()
    metadata = api.metadata(f"g{GROUP}:GATTACH1")
    assert metadata["title"] == "A Group Paper" and metadata["collections"] == ["Application"]
    assert metadata["zotero_library"] == f"groups/{GROUP}" and metadata["zotero_library_name"] == "synthetic-group"
    assert "zotero_library" not in api.metadata("ATTACH01")
    assert api.attachment_path(f"g{GROUP}:GATTACH1") == grouped
    assert api.item(f"g{GROUP}:GATTACH1")["parentItem"] == "GPARENT1"
    for path, _, headers in server.log:
        assert ("Zotero-Api-Key" in headers) == (not path.startswith("/storage/"))


def test_explicit_group_list_and_disabled_group_keys(server, tmp_path):
    add_group(server)
    add_group(server, "other", group="1234")
    api = client(server, tmp_path, groups=[int(GROUP)])
    assert {k for k, _ in api.pdf_attachments()} == {"ATTACH01", f"g{GROUP}:GATTACH1"}
    assert not any(p.startswith("/groups/1234/") for p, _, _ in server.log)
    with pytest.raises(zotero_web.ZoteroLocalAPIError):
        api.metadata("g1234:GATTACH1")


def test_duplicate_doi_prefers_personal_then_lowest_group(server, tmp_path):
    glib = add_group(server)
    glib.add_parent("GPARENT2", "Same Paper In Group", doi="https://doi.org/10.1/X")
    glib.add_pdf("GATTACH2", "GPARENT2", b"%PDF-1.4 dup")
    other = add_group(server, "other", group="99")  # lower ID than GROUP
    api = client(server, tmp_path, groups="all")
    keys = {k for k, _ in api.pdf_attachments()}
    assert keys == {"ATTACH01", "g99:GATTACH1"}
    assert {(d["key"], d["duplicate_of"]) for d in api.duplicates} == {
        (f"g{GROUP}:GATTACH2", "ATTACH01"), (f"g{GROUP}:GATTACH1", "g99:GATTACH1")}
    assert not any("GATTACH2" in p for p, _, _ in server.log)  # duplicates are never downloaded
    assert other.items  # sanity


def test_download_budget_is_shared_across_libraries(server, tmp_path):
    add_group(server)
    api = client(server, tmp_path, groups="all", max_downloads=1)
    assert [k for k, _ in api.pdf_attachments()] == ["ATTACH01"]
    assert api.retained_keys() == {f"g{GROUP}:GATTACH1"}
    api = client(server, tmp_path, groups="all", max_downloads=1)
    assert {k for k, _ in api.pdf_attachments()} == {"ATTACH01", f"g{GROUP}:GATTACH1"}


def test_group_settings_parse(tmp_path):
    control = tmp_path / "control"
    control.mkdir()
    (control / "zotero.json").write_text(json.dumps({"mode": "web", "groups": ["12", 3]}))
    assert zotero_web.zotero_settings(control, {})["groups"] == ["3", "12"]
    assert zotero_web.zotero_settings(control, {"ZOTERO_AUDIO_ZOTERO_GROUPS": "all"})["groups"] == "all"
    (control / "zotero.json").write_text(json.dumps({"mode": "web"}))
    assert zotero_web.zotero_settings(control, {})["groups"] == []
    (control / "zotero.json").write_text(json.dumps({"mode": "web", "groups": ["x"]}))
    with pytest.raises(ValueError):
        zotero_web.zotero_settings(control, {})


def test_refresh_zotero_and_research_discover_include_groups(server, tmp_path):
    from zotero_audio import app_research
    from zotero_audio.app_library import refresh_zotero
    from zotero_audio.app_state import Store

    add_group(server)
    store = Store(tmp_path / "runtime")
    (store.root / "zotero.json").write_text(json.dumps(
        {"mode": "web", "user_id": 42, "base_url": server.url, "groups": "all"}))
    (store.root / "Zotero API key.txt").write_text(KEY)
    assert refresh_zotero(store) == {"online": True, "count": 2}
    article = store.article(f"g{GROUP}:GATTACH1")
    assert article["title"] == "A Group Paper" and article["in_zotero"]
    assert article["metadata"]["collections"] == ["Application"]
    assert store.article("ATTACH01")["title"] == "A Synthetic Paper"
    records = app_research.discover(make_zotero_api(store.root))
    assert {(r["library_id"], r["key"]) for r in records} == {("users/0", "PARENT01"), (f"groups/{GROUP}", "GPARENT1")}
