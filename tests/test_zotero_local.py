import json
from pathlib import Path

from zotero_audio.zotero_local import ZoteroLocalAPI


class Response:
    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_local_api_maps_parent_metadata_and_collection_ancestors(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")

    def opener(request, timeout):
        assert timeout == 2.0
        path = request.full_url.split("/api/users/0/", 1)[1]
        if path == "collections?limit=10000":
            return Response([
                {"key": "CHILD", "data": {"name": "Podcast Queue", "parentCollection": "ROOT"}},
                {"key": "ROOT", "data": {"name": "Research", "parentCollection": False}},
            ])
        if path == "items/ATTACH01":
            return Response({"key": "ATTACH01", "data": {
                "itemType": "attachment", "parentItem": "PARENT01",
            }})
        if path == "items/PARENT01":
            return Response({"key": "PARENT01", "data": {
                "itemType": "journalArticle", "title": "A Paper", "date": "2026-04-05",
                "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Smith"}],
                "publicationTitle": "Journal", "DOI": "10.1/example",
                "rights": "CC BY 4.0", "abstractNote": "Abstract.",
                "tags": [{"tag": "podcast"}], "collections": ["CHILD"],
            }})
        raise AssertionError(path)

    metadata = ZoteroLocalAPI(opener=opener).metadata("ATTACH01")
    assert metadata == {
        "item_type": "journalArticle", "literature_type": "academic",
        "institution": None, "report_type": None, "report_number": None,
        "series_title": None, "evidence": {}, "parent_key": "PARENT01",
        "title": "A Paper", "publication_year": "2026", "publication_date": "2026-04-05",
        "authors": ["Ada Smith"], "journal": "Journal", "publisher": None,
        "university": None, "doi": "10.1/example", "url": None, "rights": "CC BY 4.0",
        "rights_source": "zotero-parent-item", "abstract": "Abstract.", "language": None,
        "tags": ["podcast"], "collections": ["Podcast Queue", "Research"],
        "podcast_selected": True,
    }


def test_local_api_discovers_pdf_paths_without_storage_layout(tmp_path: Path):
    pdf = tmp_path / "linked.pdf"
    pdf.write_bytes(b"pdf")

    def opener(request, timeout):
        path = request.full_url.split("/api/users/0/", 1)[1]
        if path == "items?itemType=attachment&limit=10000":
            return Response([{"key": "ATTACH01", "data": {
                "itemType": "attachment", "contentType": "application/pdf", "filename": "linked.pdf",
            }}])
        if path == "items/ATTACH01/file/view/url":
            response = Response(None)
            response.read = lambda: f"file://{pdf}".encode()
            return response
        raise AssertionError(path)

    assert ZoteroLocalAPI(opener=opener).pdf_attachments() == [("ATTACH01", pdf.resolve())]
