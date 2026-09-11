"""Catalog existing artifacts and saved Zotero PDFs without rendering anything."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .article_files import episode_title_for, research_markdown_path, review_markdown_path
from .article_recency import reading_scope
from .app_state import Store
from .util import sha256_file


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def visible_article(article: dict, *, local: bool = True) -> dict:
    """Only explicitly public fields cross the cloud boundary; no arbitrary paths."""
    fields = ("id", "title", "authors", "year", "source_url", "license_status", "markdown_status", "audio_status",
              "qa_status", "warnings", "updated_at", "artifacts", "source_sha256", "markdown_sha256", "editions",
              "publication_status", "icloud_status", "backup_status", "source_changed", "metadata_warning")
    value = {key: article[key] for key in fields if key in article}
    value["reading_scope"] = reading_scope(article)
    value["episode_title"] = episode_title_for(article)
    value["editions"] = {
        name: {key: record[key] for key in ("edition", "title", "duration", "audio_sha256", "audio_url", "status", "publication_status", "qa_status") if key in record}
        for name, record in article.get("editions", {}).items()
    }
    if local:
        for name, record in value["editions"].items():
            source = article.get("editions", {}).get(name, {})
            if source.get("audio") and Path(source["audio"]).is_file():
                record["audio_url"] = f"/api/articles/{article['id']}/audio?edition={name}"
        value["audio_url"] = next((r.get("audio_url") for r in value["editions"].values() if r.get("audio_url")), None)
    else:
        value["audio_url"] = next((r.get("audio_url") for r in value["editions"].values() if str(r.get("audio_url", "")).startswith("https://")), None)
    return value


def import_existing(store: Store) -> int:
    root = store.runtime / "full-library"
    manifest = read_json(root / "batch-manifest.json")
    by_key = {row.get("zotero_key"): row for row in manifest.get("items", [])}
    destination = Path(manifest.get("destination", str(Path.home() / "Music/Zotero Audio")))
    publication = read_json(store.runtime / "podcast/publication-manifest.json").get("episodes", {})
    records = list(publication.values()) if isinstance(publication, dict) else publication
    count = 0
    for bundle in sorted((root / "bundles").glob("*")):
        if not bundle.is_dir():
            continue
        metadata = read_json(bundle / "metadata.json")
        structure = read_json(bundle / "structure.json")
        markdown_path = research_markdown_path(bundle, structure.get("document", {}), migrate=True)
        if not markdown_path.is_file():
            continue
        doc = structure.get("document", {})
        key = metadata.get("zotero_key") or structure.get("zotero_key")
        if not key:
            match = re.search(r"\[([A-Z0-9]+)\]$", bundle.name)
            key = match.group(1) if match else None
        if not key:
            continue
        try:
            old = store.article(key)
            if old.get("managed"):
                continue
        except KeyError:
            old = {}
        batch = by_key.get(key, {})
        # Metadata from the Zotero parent is preferable to PDF filenames/headers.
        title = metadata.get("title") or doc.get("title") or batch.get("title") or bundle.name
        authors = metadata.get("authors") or doc.get("authors") or [x.strip() for x in str(doc.get("author") or "").split(";") if x.strip()]
        rights = metadata.get("rights") or doc.get("rights")
        source_sha = batch.get("source_sha256") or structure.get("source", {}).get("sha256") or structure.get("source_sha256")
        metadata = {**doc, **metadata}
        metadata["rights"] = rights
        audio = destination / batch.get("output_file", "__missing__")
        editions = dict(old.get("editions", {}))
        if audio.is_file() and "full" not in editions:
            editions["full"] = {"edition": "full", "title": title, "audio": str(audio), "audio_sha256": batch.get("output_sha256"),
                                "duration": batch.get("duration_seconds"), "status": "ready", "origin": "existing"}
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("zotero_key") == key or f"[{key}]" in record.get("audio", ""):
                edition = record.get("edition", "full")
                existing = editions.get(edition, {})
                editions[edition] = {**existing, **{k: record[k] for k in ("audio", "audio_url", "audio_sha256", "duration", "title", "edition", "guid") if k in record},
                                     "status": "ready", "publication_status": "published", "origin": "existing"}
        review = read_json(bundle / "qa-report.json")
        qa_status = "unchecked"  # Legacy audio measurements do not establish text quality.
        warnings = list(old.get("warnings", []))
        if review.get("status") == "fail":
            qa_status = "warnings"
            warning = "Existing audio has QA findings; it remains available for listening."
            if warning not in warnings:
                warnings.append(warning)
        markdown = markdown_path.read_text(encoding="utf-8")
        review_path = review_markdown_path(bundle, {"title": title, "authors": authors, "year": metadata.get("publication_year")}, migrate=True)
        article = {**old, "id": key, "title": title, "authors": authors, "year": metadata.get("publication_year"),
                   "source_url": metadata.get("url") or (f"https://doi.org/{metadata['doi']}" if metadata.get("doi") else None),
                   "license_status": "open" if rights and ("creativecommons.org/licenses/by/4.0" in rights.lower() or "creativecommons.org/publicdomain/zero" in rights.lower()) else "private",
                   "source_path": batch.get("source_path") or structure.get("source", {}).get("path"), "source_sha256": source_sha,
                   "bundle": str(bundle), "markdown": str(markdown_path), "markdown_sha256": sha256_file(markdown_path),
                   "markdown_status": "ready", "audio_status": "ready" if editions else "not_started", "qa_status": qa_status,
                   "warnings": warnings, "metadata": metadata, "editions": editions,
                   "artifacts": {"markdown": True, "audio": bool(editions), "review": review_path.is_file()}}
        if review_path.is_file():
            article["review"] = str(review_path)
        source = Path(article["source_path"]) if article.get("source_path") else None
        if source and source.is_file():
            article["source_stat"] = [source.stat().st_size, source.stat().st_mtime_ns]
        if old:
            with store.edit_article(key, markdown=markdown) as current:
                if current.get("managed"):
                    continue
                # Keep a delivery that completed since this import began.
                article["editions"] = {**article.get("editions", {}), **current.get("editions", {})}
                current.update(article)
        else:
            store.put_article(article, markdown=markdown)
        count += 1
    store.set_state("last_import_count", count)
    return count


def migrate_catalog_markdown(store: Store) -> int:
    """Rename known research and review Markdown files to their episode names."""
    migrated = 0
    for article in store.all_articles():
        changed = False
        markdown = Path(article["markdown"]) if article.get("markdown") else None
        if markdown and markdown.is_file():
            desired = research_markdown_path(markdown.parent, article, current=markdown, migrate=True)
            if desired != markdown:
                changed = True
                article["markdown"] = str(desired)
                article["markdown_sha256"] = sha256_file(desired)
        review = Path(article["review"]) if article.get("review") else None
        if review and review.is_file():
            desired = review_markdown_path(review.parent, article, current=review, migrate=True)
            if desired != review:
                changed = True
                article["review"] = str(desired)
        if changed:
            markdown_text = Path(article["markdown"]).read_text(encoding="utf-8") if article.get("markdown") else None
            with store.edit_article(article["id"], markdown=markdown_text) as current:
                current.update({key: value for key, value in article.items() if key in {"markdown", "markdown_sha256", "review"}})
            migrated += 1
    return migrated


def refresh_zotero(store: Store) -> dict:
    """Read the supported Zotero GET API; leave the cached library usable offline."""
    # Explicit jobs and background discovery must not commit scans out of order.
    # Network I/O never holds the control database lock.
    with store.zotero_lock:
        return _refresh_zotero(store)


def _refresh_zotero(store: Store) -> dict:
    from .zotero_local import ZoteroLocalAPI, ZoteroLocalAPIError
    from .app_state import now
    api = ZoteroLocalAPI(timeout=5)
    try:
        discovered = api.pdf_attachments()
    except ZoteroLocalAPIError as exc:
        store.set_state("zotero", {**store.state("zotero", {}), "online": False, "message": str(exc), "checked_at": now()})
        return {"online": False, "count": 0, "message": str(exc)}
    count = 0
    discovered_keys = {key for key, _ in discovered}
    for key, source in discovered:
        try:
            previous = store.article(key)
        except KeyError:
            previous = {}
        try:
            metadata = api.metadata(key) or previous.get("metadata", {})
        except ZoteroLocalAPIError:
            metadata = previous.get("metadata", {})
        title = metadata.get("title") or previous.get("title")
        if not title:
            # Standalone PDFs have no Zotero parent. Use PDF metadata when present.
            try:
                from pypdf import PdfReader
                title = str(PdfReader(source).metadata.title or "").strip()
            except Exception:
                title = ""
            title = title or source.stem.replace("_", " ")
        rights = metadata.get("rights", "") or ""
        stat = [source.stat().st_size, source.stat().st_mtime_ns]
        source_sha = previous.get("source_sha256") if previous.get("source_stat") == stat else sha256_file(source)
        license_status = "open" if "creativecommons.org/licenses/by/4.0" in rights.lower() or "creativecommons.org/publicdomain/zero" in rights.lower() else "private"
        if not rights and previous.get("bundle"):
            # Empty Zotero Rights does not invalidate independently verified PDF
            # evidence. Never reuse a parent assertion or evidence for old bytes.
            from .podcast import resolve_license
            evidence = read_json(Path(previous["bundle"]) / "generation.json").get("license_record") or {}
            if evidence.get("content_version") == "local-pdf-license-v1" and resolve_license(evidence, source_sha256=source_sha).get("allowed"):
                license_status = "open"
        changed = bool(previous.get("source_sha256") and source_sha != previous["source_sha256"])
        article = {**previous, "id": key, "title": title, "authors": metadata.get("authors", previous.get("authors", [])),
                   "year": metadata.get("publication_year", previous.get("year")), "source_path": str(source),
                   "source_sha256": source_sha, "source_stat": stat, "source_changed": changed or previous.get("source_changed", False),
                   "source_url": metadata.get("url", previous.get("source_url")), "metadata": {**previous.get("metadata", {}), **metadata},
                   "license_status": license_status,
                   "markdown_status": previous.get("markdown_status", "not_started"), "audio_status": previous.get("audio_status", "not_started"),
                   "qa_status": previous.get("qa_status", "unchecked"), "warnings": previous.get("warnings", []),
                   "editions": previous.get("editions", {}), "artifacts": previous.get("artifacts", {"markdown": False, "audio": False, "review": False}),
                   "bundle": previous.get("bundle", str(store.runtime / "library" / key))}
        article["metadata_warning"] = None if metadata.get("title") else "No parent article title in Zotero; showing the PDF title."
        if previous:
            with store.edit_article(key) as current:
                for field in ("title", "authors", "year", "source_path", "source_sha256", "source_stat", "source_changed", "source_url", "metadata", "license_status", "metadata_warning"):
                    if field in article:
                        current[field] = article[field]
                current["in_zotero"] = True
        else:
            article["in_zotero"] = True
            store.put_article(article)
        count += 1
    # Keep historical documents searchable, but a whole-Zotero run should only
    # process PDFs still returned by a successful library scan.
    for article in store.all_articles():
        if article["id"] not in discovered_keys and article.get("in_zotero") is not False:
            with store.edit_article(article["id"]) as current:
                current["in_zotero"] = False
    checked_at = now()
    store.set_state("zotero", {"online": True, "count": count, "checked_at": checked_at, "last_success_at": checked_at})
    return {"online": True, "count": count}


def artifact_path(article: dict, kind: str, edition: str = "full") -> Path:
    if kind not in {"markdown", "audio", "review"}:
        raise ValueError("Choose Markdown, audio or review")
    if kind == "audio":
        if edition not in {"brief", "full"}:
            raise ValueError("Choose the Brief or Full audio edition")
        record = article.get("editions", {}).get(edition) or {}
        value = record.get("audio")
    else:
        value = article.get(kind)
    if not value or not Path(value).is_file():
        label = f"{edition.capitalize()} audio" if kind == "audio" else kind.capitalize()
        raise FileNotFoundError(f"{label} is not available for {article['title']}")
    return Path(value)
