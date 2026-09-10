from pathlib import Path

import pytest

from zotero_audio.license_status import evaluate_library
from zotero_audio.util import atomic_write_json, sha256_file


@pytest.fixture
def case(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"checksum-bound PDF fixture")
    request = {"request_id": "test", "items": [{
        "key": "PARENT01", "fields": {"rights": "CC BY 4.0", "DOI": "10.1/example"},
        "attachments": [{"key": "ATTACH01", "path": str(pdf)}],
    }]}
    paths = {"bundles": tmp_path / "bundles", "evidence": tmp_path / "evidence"}
    return request, paths, pdf


@pytest.mark.parametrize("rights,doi,status", [
    ("CC BY 4.0", "10.1/example", "pass"),
    ("CC0", "10.1/example", "pass"),
    ("Public Domain Mark 1.0", "10.1/example", "pass"),
    ("CC BY 4.0", "", "blocked"),
    ("", "10.1/example", "blocked"),
    ("Open access", "10.1/example", "blocked"),
    ("https://creativecommons.org/licenses/by-nc/4.0/", "10.1/example", "blocked"),
    ("https://creativecommons.org/licenses/by-nd/4.0/", "10.1/example", "blocked"),
])
def test_status_uses_publication_license_gate(case, rights, doi, status):
    request, paths, _ = case
    request["items"][0]["fields"] = {"rights": rights, "DOI": doi}
    assert evaluate_library(request, **paths)["items"][0]["status"] == status


def test_strict_extra_rights_and_current_rights_precedence(case):
    request, paths, _ = case
    fields = request["items"][0]["fields"]
    fields.update(rights="", extra="License: CC BY 4.0\nUnrelated text")
    assert evaluate_library(request, **paths)["items"][0]["status"] == "pass"
    fields["rights"] = "All rights reserved"
    assert evaluate_library(request, **paths)["items"][0]["status"] == "blocked"


def test_deleted_metadata_does_not_reuse_old_parent_assertion(case):
    request, paths, pdf = case
    request["items"][0]["fields"]["rights"] = ""
    atomic_write_json(paths["evidence"] / f"{sha256_file(pdf)}.json", {"record": {
        "source_sha256": sha256_file(pdf), "license_url": "CC BY 4.0",
        "evidence_sha256": "evidence", "read_url": "https://example.org/paper",
        "content_version": "zotero-local-source",
    }})
    assert evaluate_library(request, **paths)["items"][0]["status"] == "blocked"


@pytest.mark.parametrize("flag", ["embargoed", "conflict"])
def test_metadata_cannot_clear_a_recorded_restriction(case, flag):
    request, paths, pdf = case
    atomic_write_json(paths["evidence"] / f"{sha256_file(pdf)}.json", {"record": {
        "source_sha256": sha256_file(pdf), flag: True,
    }})
    assert evaluate_library(request, **paths)["items"][0]["status"] == "blocked"


def test_pdf_evidence_is_bound_to_attachment_and_current_bytes(case):
    request, paths, pdf = case
    request["items"][0]["fields"]["rights"] = ""
    atomic_write_json(paths["bundles"] / "Paper [ATTACH01]" / "structure.json", {
        "source": {"sha256": sha256_file(pdf), "zotero_key": "ATTACH01"},
        "document": {"rights": "CC BY 4.0", "rights_source": "pdf-text"},
    })
    assert evaluate_library(request, **paths)["items"][0]["status"] == "pass"
    pdf.write_bytes(b"replacement PDF without verified license evidence")
    assert evaluate_library(request, **paths)["items"][0]["status"] == "blocked"


def test_mixed_attachments_and_removed_pdf(case):
    request, paths, _ = case
    item = request["items"][0]
    item["attachments"].append({"key": "MISSING1", "path": None})
    result = evaluate_library(request, **paths)["items"][0]
    assert result["status"] == "blocked"
    assert [a["allowed"] for a in result["attachments"]] == [True, False]
    item["attachments"] = []
    assert evaluate_library(request, **paths)["items"][0]["status"] is None


def test_malformed_evidence_blocks_only_affected_record(case):
    request, paths, pdf = case
    paths["evidence"].mkdir()
    (paths["evidence"] / f"{sha256_file(pdf)}.json").write_text("invalid JSON")
    result = evaluate_library(request, **paths)
    assert result["request_id"] == "test"
    assert result["items"][0]["attachments"][0]["reason"] == "license-evaluation-error"
