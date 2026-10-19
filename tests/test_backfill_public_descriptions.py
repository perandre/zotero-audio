from __future__ import annotations

from pathlib import Path
import json
import uuid
from types import SimpleNamespace

from scripts.backfill_public_descriptions import (
    _assert_publication_identity_unchanged,
    _commit_local_outputs,
    _known_abstracts,
    _looks_like_legacy_generated,
    enrich_records,
    is_published_episode,
    main,
)
from zotero_audio.podcast import LocalPublisher, _show_notes_html


def published_record(**changes):
    value = {
        "guid": "urn:uuid:published-episode",
        "edition": "full",
        "pub_date": "2026-01-02T03:04:05Z",
        "audio_url": "https://podcast.example/episodes/audio.m4a",
        "bytes": 1234,
        "revision": "revision-1",
        "source_sha256": "source-1",
        "paper_guid": str(uuid.UUID(int=1)),
        "title": "Synthetic Paper — Example (2026)",
        "paper_title": "Synthetic Paper",
        "authors": ["A. Example"],
        "brief_contents": "the authors' abstract",
        "description_abstract": "A short synthetic abstract.",
        "description_abstract_source": "zotero",
        "show_notes": "A short synthetic abstract.\n\nMy hand-edited introduction and recommendation.",
    }
    value.update(changes)
    return value


def test_backfill_preserves_manual_notes_and_publication_identity(tmp_path):
    record = published_record()
    before = dict(record)
    config = SimpleNamespace(state_root=tmp_path / "podcast")

    output, counts = enrich_records(config, [record], fetch_zotero=False)

    assert output[0]["show_notes"] == before["show_notes"]
    assert output[0]["description_abstract"] == before["description_abstract"]
    assert output[0]["description_is_manual"] is True
    assert counts["manual_notes_preserved"] == 1
    rendered = _show_notes_html(
        output[0]["show_notes"], edition="full",
        description_abstract=output[0]["description_abstract"],
        description_source=output[0]["description_abstract_source"],
        generated_show_notes=output[0].get("generated_show_notes", ""),
    )
    assert 'class="episode-abstract"' not in rendered
    _assert_publication_identity_unchanged([before], output)


def test_legacy_auto_notes_require_source_match_and_preserve_pair_url(monkeypatch):
    abstract = "A source abstract that is safely synthetic."
    record = published_record(
        show_notes=(
            f"{abstract}\n\nAuthors: A. Example\n"
            "Read the paper: https://source.example/paper\n"
            "The authors and publisher do not sponsor or endorse this recording.\n"
            f"Paired edition: https://podcast.example/papers/{record_guid()}/brief/"
        ),
        paired_url=f"https://podcast.example/papers/{record_guid()}/brief/",
    )
    record.pop("description_abstract")
    record.pop("description_abstract_source")
    structure = {"source": {"sha256": "source-1"}, "document": {
        "metadata": {"abstract": abstract}, "abstract": abstract, "abstract_source": "zotero"}}
    monkeypatch.setattr("scripts.backfill_public_descriptions._structure_for", lambda config, item: structure)
    known = _known_abstracts(record, {"abstract": abstract}, structure)

    assert _looks_like_legacy_generated(record, known)
    config = SimpleNamespace(state_root=Path("/does/not/exist"))
    output, counts = enrich_records(config, [record], fetch_zotero=False)

    assert counts["legacy_notes_updated"] == 1
    assert "This Full Reading presents" in output[0]["show_notes"]
    assert "Narration: Read verbatim by an AI-generated voice." in output[0]["show_notes"]
    assert f"Paired edition: {record['paired_url']}" in output[0]["show_notes"]
    assert "Abstract source: zotero" in output[0]["show_notes"]
    assert output[0]["description_abstract"] == abstract
    _assert_publication_identity_unchanged([record], output)


def test_legacy_notes_with_unrecognized_manual_line_are_not_rewritten():
    abstract = "A source abstract that is safely synthetic."
    record = published_record(show_notes=(
        f"{abstract}\n\nAuthors: A. Example\n"
        "My personal listening note.\n"
        "The authors and publisher do not sponsor or endorse this recording."
    ))

    structure = {"document": {"metadata": {"abstract": abstract}, "abstract": abstract}}
    assert not _looks_like_legacy_generated(record, _known_abstracts(record, {"abstract": abstract}, structure))
    output, counts = enrich_records(SimpleNamespace(state_root=Path("/missing")), [record], fetch_zotero=False)
    assert output[0]["show_notes"] == record["show_notes"]
    assert counts["manual_notes_preserved"] == 1


def test_only_existing_enclosures_are_eligible_and_identity_guard_is_strict():
    record = published_record()
    assert is_published_episode(record)
    assert not is_published_episode({**record, "audio_url": None})
    changed = {**record, "bytes": record["bytes"] + 1}
    try:
        _assert_publication_identity_unchanged([record], [changed])
    except AssertionError as exc:
        assert "bytes" in str(exc)
    else:
        raise AssertionError("publication byte length changes must be rejected")


def test_contaminated_legacy_opener_is_replaced_with_clean_source_text(monkeypatch):
    contaminated = "This unrelated page furniture must not be published."
    clean = "The authors report a measured result in this synthetic study."
    structure = {"source": {"sha256": "source-1"}, "document": {
        "metadata": {"abstract": clean}, "abstract": contaminated,
        "abstract_source": "publisher-html"}}
    record = published_record(show_notes=(
        f"{contaminated}\n\nAuthors: A. Example\n"
        "Published in: Synthetic Journal\n"
        "The authors and publisher do not sponsor or endorse this recording."
    ))
    record.pop("description_abstract")
    record.pop("description_abstract_source")
    monkeypatch.setattr("scripts.backfill_public_descriptions._structure_for", lambda config, item: structure)

    output, counts = enrich_records(SimpleNamespace(state_root=Path("/missing")), [record], fetch_zotero=False)

    assert counts["legacy_notes_updated"] == 1
    assert output[0]["show_notes"].startswith(clean)
    assert contaminated not in output[0]["show_notes"]
    assert "Published in: Synthetic Journal" in output[0]["show_notes"]
    assert output[0]["journal"] == "Synthetic Journal"


def test_missing_abstract_uses_factual_edition_line_and_safe_brief_copy():
    full = published_record(show_notes=(
        "A full audio edition of this paper.\n\nAuthors: A. Example\n"
        "The authors and publisher do not sponsor or endorse this recording."
    ))
    full.pop("description_abstract")
    full.pop("description_abstract_source")
    brief = published_record(edition="brief", brief_contents=None, show_notes=(
        "A brief audio edition of this paper.\n\nAuthors: A. Example\n"
        "The authors and publisher do not sponsor or endorse this recording."
    ))
    brief.pop("description_abstract")
    brief.pop("description_abstract_source")

    output, _ = enrich_records(SimpleNamespace(state_root=Path("/missing")), [full, brief], fetch_zotero=False)

    assert output[0]["show_notes"].startswith("This Full Reading presents")
    assert "A full audio edition of this paper." not in output[0]["show_notes"]
    assert output[1]["show_notes"].startswith("This is a Brief audio edition.")
    assert "the authors' abstract" not in output[1]["show_notes"]


def test_generated_notes_are_idempotent_with_and_without_abstract(monkeypatch):
    abstract = "A source-grounded synthetic abstract."
    structure = {"source": {"sha256": "source-1"}, "document": {
        "metadata": {"abstract": abstract}, "abstract": abstract}}
    monkeypatch.setattr("scripts.backfill_public_descriptions._structure_for", lambda config, item: structure)
    legacy = published_record(show_notes=(
        f"{abstract}\n\nAuthors: A. Example\n"
        "The authors and publisher do not sponsor or endorse this recording."
    ))
    legacy.pop("description_abstract")
    legacy.pop("description_abstract_source")
    no_abstract = published_record(
        brief_contents=None,
        show_notes=("A full audio edition of this paper.\n\nAuthors: A. Example\n"
                    "The authors and publisher do not sponsor or endorse this recording."),
    )
    no_abstract.pop("description_abstract")
    no_abstract.pop("description_abstract_source")
    empty_structure = {"source": {"sha256": "source-1"}, "document": {"metadata": {}}}
    output_notes = []
    for record, source_structure in ((legacy, structure), (no_abstract, empty_structure)):
        monkeypatch.setattr("scripts.backfill_public_descriptions._structure_for", lambda config, item, value=source_structure: value)
        first, _ = enrich_records(SimpleNamespace(state_root=Path("/missing")), [record], fetch_zotero=False)
        second, _ = enrich_records(SimpleNamespace(state_root=Path("/missing")), first, fetch_zotero=False)
        assert second[0]["show_notes"] == first[0]["show_notes"]
        output_notes.append(first[0]["show_notes"])
    assert output_notes[0].count("This Full Reading presents") == 1
    assert output_notes[0].count("Abstract source: zotero") == 1
    assert output_notes[1].startswith("This Full Reading presents")
    assert "A full audio edition of this paper." not in output_notes[1]
    assert output_notes[1].count("This Full Reading presents") == 1


def test_partial_feed_commit_failure_restores_manifest_pages_and_headers(tmp_path, monkeypatch):
    public = tmp_path / "public"
    state = tmp_path / "state"
    config = SimpleNamespace(public_root=public, state_root=state, base_url="https://podcast.example")
    manifest_path = state / "publication-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{"episodes":[{"guid":"original"}]}', encoding="utf-8")
    keys = {"index.html", "papers/paper/index.html", "brief/feed.xml", "full/feed.xml"}
    before = {}
    for key in keys:
        target = public / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"old:{key}", encoding="utf-8")
        sidecar = target.with_suffix(target.suffix + ".headers.json")
        sidecar.write_text(f"old-header:{key}", encoding="utf-8")
        before[key] = (target.read_bytes(), sidecar.read_bytes())
    stages = tmp_path / "stages"
    rendered = {}
    for key in ("index.html", "papers/paper/index.html"):
        stage = stages / key
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_text(f"new:{key}", encoding="utf-8")
        rendered[key] = stage
    staged_feeds = {}
    for key in ("brief/feed.xml", "full/feed.xml"):
        stage = stages / key
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_text("<rss />", encoding="utf-8")
        staged_feeds[key] = stage

    original_commit = LocalPublisher.commit_feed
    commits = 0

    def fail_on_second_feed(self, staged, canonical_name, snapshot_dir):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("synthetic second-feed failure")
        return original_commit(self, staged, canonical_name, snapshot_dir)

    monkeypatch.setattr(LocalPublisher, "commit_feed", fail_on_second_feed)
    try:
        _commit_local_outputs(config, manifest_path, {"episodes": [{"guid": "updated"}]},
                              rendered, staged_feeds, keys)
    except RuntimeError as exc:
        assert "synthetic" in str(exc)
    else:
        raise AssertionError("the synthetic feed failure should propagate")

    assert manifest_path.read_text(encoding="utf-8") == '{"episodes":[{"guid":"original"}]}'
    for key in keys:
        target = public / key
        sidecar = target.with_suffix(target.suffix + ".headers.json")
        assert (target.read_bytes(), sidecar.read_bytes()) == before[key]


def test_main_apply_with_frozen_loaded_config_stages_writes_and_is_idempotent(tmp_path, capsys):
    private = tmp_path / "private"
    state = tmp_path / "state"
    public = tmp_path / "public"
    for path in (private, state, public):
        path.mkdir(parents=True)
    config_path = tmp_path / "podcast.toml"
    config_path.write_text(
        f'[paths]\nprivate_root = "{private}"\nstate_root = "{state}"\npublic_root = "{public}"\n'
        '\n[podcast]\nbase_url = "https://podcast.example"\n', encoding="utf-8")
    paper_guid = str(uuid.UUID(int=42))
    records = []
    for edition in ("brief", "full"):
        notes = "A synthetic cached abstract.\n\nThis is the existing generated note."
        records.append(published_record(
            paper_guid=paper_guid, edition=edition, guid=f"urn:uuid:{edition}",
            title=f"Synthetic Paper — {edition} (2026)",
            page_url=f"https://podcast.example/papers/{paper_guid}/{edition}/",
            duration=42,
            paired_url=f"https://podcast.example/papers/{paper_guid}/{'full' if edition == 'brief' else 'brief'}/",
            description_abstract="A synthetic cached abstract.",
            description_abstract_source="zotero",
            generated_description_abstract="A synthetic cached abstract.",
            show_notes=notes, generated_show_notes=notes,
        ))
    manifest_path = state / "publication-manifest.json"
    manifest_path.write_text(json.dumps({"episodes": records}), encoding="utf-8")
    for edition in ("brief", "full"):
        feed = public / edition / "feed.xml"
        feed.parent.mkdir(parents=True, exist_ok=True)
        feed.write_text(
            '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            '<channel><title>Synthetic</title><itunes:image href="https://podcast.example/cover.png"/>'
            '</channel></rss>', encoding="utf-8")

    args = ["--config", str(config_path), "--apply", "--no-network"]
    assert main(args) == 0
    capsys.readouterr()
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = [(row["guid"], row["audio_url"], row["bytes"], row["pub_date"])
                for row in first_manifest["episodes"]]
    assert all(row["description_abstract"] == "A synthetic cached abstract."
               for row in first_manifest["episodes"])
    assert (public / "index.html").is_file()
    for row in records:
        key = f"papers/{paper_guid}/{row['edition']}/index.html"
        assert (public / key).is_file()
        assert (public / f"{row['edition']}/feed.xml").is_file()

    assert main(args) == 0
    capsys.readouterr()
    second_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [(row["guid"], row["audio_url"], row["bytes"], row["pub_date"])
            for row in second_manifest["episodes"]] == identity
    assert [row["show_notes"] for row in second_manifest["episodes"]] == [
        row["show_notes"] for row in first_manifest["episodes"]]


def record_guid():
    return str(uuid.UUID(int=1))


def test_backfill_clears_false_manual_flag_on_verified_generated_notes():
    abstract = 'An actual synthetic source abstract.'
    record = published_record(edition='brief', description_abstract=abstract,
        generated_description_abstract=abstract, description_is_manual=True,
        generated_show_notes='New generated notes.',
        show_notes='A brief audio edition of this paper.\n\nAuthors: A. Example\nThe authors and publisher do not sponsor or endorse this recording.')
    output, counts = enrich_records(SimpleNamespace(state_root=Path('/missing')), [record], fetch_zotero=False, allow_external=False)
    assert counts['legacy_notes_updated'] == 1
    assert output[0]['show_notes'] == output[0]['generated_show_notes']
    assert output[0]['description_is_manual'] is False
    html = _show_notes_html(output[0]['show_notes'], description_abstract=output[0]['description_abstract'],
        description_source=output[0]['description_abstract_source'], generated_show_notes=output[0]['generated_show_notes'])
    assert 'episode-abstract' in html
