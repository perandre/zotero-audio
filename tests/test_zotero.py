import sqlite3
from pathlib import Path

from zotero_audio.util import atomic_write_json
from zotero_audio.zotero import (
    bundle_metadata_snapshot,
    license_record_from_metadata,
    load_bundle_metadata,
    merge_document_metadata,
    normalize_publication_date,
    zotero_metadata,
)


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, itemTypeID INTEGER);
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        INSERT INTO itemTypes VALUES (1, 'attachment'), (2, 'journalArticle'), (3, 'report');
        CREATE TABLE itemAttachments (itemID INTEGER, parentItemID INTEGER);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, parentCollectionID INTEGER, collectionName TEXT);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
        INSERT INTO items VALUES (1, 'ATTACH01', 1), (2, 'PARENT01', 2);
        INSERT INTO itemAttachments VALUES (1, 2);
        INSERT INTO fields VALUES (1, 'title'), (2, 'date'), (3, 'DOI'), (4, 'rights'), (5, 'abstractNote');
        INSERT INTO itemDataValues VALUES
            (1, 'A Zotero Paper'), (2, '2026-04-05'), (3, '10.1/example'),
            (4, 'https://creativecommons.org/licenses/by/4.0/'), (5, 'The abstract.');
        INSERT INTO itemData VALUES (2, 1, 1), (2, 2, 2), (2, 3, 3), (2, 4, 4), (2, 5, 5);
        INSERT INTO creators VALUES (1, 'Ada', 'Smith');
        INSERT INTO creatorTypes VALUES (1, 'author');
        INSERT INTO itemCreators VALUES (2, 1, 1, 0);
        INSERT INTO tags VALUES (1, 'podcast');
        INSERT INTO itemTags VALUES (2, 1);
        INSERT INTO collections VALUES (1, NULL, 'Research');
        INSERT INTO collectionItems VALUES (1, 2);
        """
    )
    connection.commit()
    connection.close()


def test_zotero_parent_metadata_is_canonical(tmp_path: Path):
    database = tmp_path / "zotero.sqlite"
    _database(database)
    metadata = zotero_metadata(database, "ATTACH01")
    assert metadata
    assert metadata["title"] == "A Zotero Paper"
    assert metadata["authors"] == ["Ada Smith"]
    assert metadata["publication_year"] == "2026"
    assert metadata["podcast_selected"] is True
    document = merge_document_metadata({"title": "PDF title"}, metadata)
    assert document["title"] == "A Zotero Paper"
    assert document["author"] == "Ada Smith"


def test_partial_publication_dates_keep_known_precision_without_fake_day():
    assert normalize_publication_date("2026-02-00") == "2026-02"
    assert normalize_publication_date("2026-00-00") == "2026"
    assert normalize_publication_date("2026-04-05") == "2026-04-05"


def test_report_metadata_preserves_type_issuer_series_and_evidence(tmp_path: Path):
    database = tmp_path / "zotero.sqlite"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE items SET itemTypeID = 3 WHERE itemID = 2")
        for number, (field, value) in enumerate([
            ("institution", "Example Institute"), ("seriesTitle", "AI Outlook"),
            ("reportType", "Industry report"), ("reportNumber", "42"),
            ("extra", "Evidence methodology: Survey\nEvidence sample: 200 executives\nPeer review: Not stated"),
        ], 6):
            connection.execute("INSERT INTO fields VALUES (?, ?)", (number, field))
            connection.execute("INSERT INTO itemDataValues VALUES (?, ?)", (number, value))
            connection.execute("INSERT INTO itemData VALUES (2, ?, ?)", (number, number))
    metadata = zotero_metadata(database, "ATTACH01")
    assert metadata["literature_type"] == metadata["item_type"] == "report"
    assert metadata["institution"] == "Example Institute"
    assert metadata["report_type"] == "Industry report"
    assert metadata["evidence"]["sample"] == "200 executives"
    snapshot = bundle_metadata_snapshot(metadata, attachment_key="ATTACH01", source_sha256="a" * 64)
    document = merge_document_metadata({}, snapshot)
    assert document["report_number"] == "42" and document["series_title"] == "AI Outlook"
    assert document["evidence"]["methodology"] == "Survey"


def test_bundle_snapshot_is_auto_discovered_and_builds_license_record(tmp_path: Path):
    bundle = tmp_path / "full-library" / "bundles" / "Paper [ATTACH01]"
    bundle.mkdir(parents=True)
    source_sha = "a" * 64
    atomic_write_json(bundle / "structure.json", {
        "source": {"zotero_key": "ATTACH01", "sha256": source_sha},
    })
    metadata = {
        "parent_key": "PARENT01",
        "title": "A Zotero Paper",
        "authors": ["Ada Smith"],
        "rights": "https://creativecommons.org/licenses/by/4.0/",
        "doi": "10.1/example",
    }
    atomic_write_json(
        bundle / "metadata.json",
        bundle_metadata_snapshot(metadata, attachment_key="ATTACH01", source_sha256=source_sha),
    )
    discovered = load_bundle_metadata(bundle)
    assert discovered["title"] == "A Zotero Paper"
    license_record = license_record_from_metadata(discovered, source_sha)
    assert license_record and license_record["read_url"] == "https://doi.org/10.1/example"
