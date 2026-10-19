"""Private OKF reference inventory alongside the authoritative full-text library."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from .app_research import clean_text, discover
from .app_state import Store, digest
from .episode_descriptions import normalize_abstract, select_description_abstract
from .research_library import _library_lock
from .util import atomic_write_json, atomic_write_text, filename_part, sha256_file
from .zotero_web import make_zotero_api


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _identity(library: str, key: str) -> str:
    return f"g{library.split('/')[1]}:{key}" if library.startswith("groups/") else key


def _source(library: str, key: str) -> str:
    scope = library if library.startswith("groups/") else "library"
    return f"zotero://select/{scope}/items/{key}"


def _label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")


def export_library(store: Store, *, records: list[dict] | None = None) -> dict:
    """Export every reference, without changing research, jobs or publication.

    Reference files are derived views. Changed files are preserved on reruns;
    the manifest retains the last generated hash so edits stay protected.
    """
    if records is None:
        records = discover(make_zotero_api(store.root, timeout=15))
    root = store.runtime / "papers"
    directory = root / "references"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog: dict[tuple[str, str], list[dict]] = {}
    for article in store.all_articles():
        metadata = article.get("metadata") or {}
        key = metadata.get("parent_key") or metadata.get("zotero_parent_key")
        if key:
            prefix = str(article["id"]).split(":", 1)[0]
            library = f"groups/{prefix[1:]}" if ":" in str(article["id"]) and prefix.startswith("g") else "users/0"
            catalog.setdefault((library, str(key)), []).append(article)
    # Reuse source-matched public metadata already retrieved by research-fit.
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='research_abstract_cache'").fetchone()
        cache = dict(db.execute("SELECT hash,abstract FROM research_abstract_cache")) if exists else {}
    rows = []
    preserved = []
    with _library_lock(root):
        previous = _read(directory / "index.json").get("references", {})
        for record in sorted(records, key=lambda r: (str(r["data"]["title"]).casefold(), r["library_id"], r["key"])):
            data, key, library = record["data"], record["key"], record["library_id"]
            identity = _identity(library, key)
            old = previous.get(identity, {})
            # Stable paths survive Zotero title edits; full titles remain in files.
            name = Path(old["path"]).name if old.get("path") else f"{filename_part(data['title'], max_length=150, max_bytes=190)} [{filename_part(identity, max_length=40, max_bytes=45)}].md"
            path = directory / name
            abstract = normalize_abstract(data.get("abstractNote"))
            provenance = "zotero" if abstract else "unavailable"
            articles = catalog.get((library, key), [])
            full = []
            for article in articles:
                markdown = Path(article.get("markdown") or "/missing")
                if markdown.is_file() and markdown.resolve().parent == root.resolve():
                    full.append(markdown)
                if not abstract:
                    metadata = article.get("metadata") or {}
                    abstract = normalize_abstract(metadata.get("abstract"))
                    if abstract:
                        provenance = str(metadata.get("abstract_source") or "saved-article-metadata")
                    else:
                        structure = _read(Path(article.get("bundle") or "/missing") / "structure.json")
                        extracted_hash = structure.get("source", {}).get("sha256") or structure.get("source_sha256")
                        if not article.get("source_changed") and article.get("source_sha256") and extracted_hash == article["source_sha256"]:
                            selected = select_description_abstract({}, structure=structure, fetch_doi=lambda _: "")
                            abstract = selected["description_abstract"]
                            provenance = selected["description_abstract_source"]
            if not abstract:
                fingerprint = digest({"doi": str(data.get("DOI") or "").strip().casefold(), "title": clean_text(data["title"]), "policy": 1})
                cached = cache.get(fingerprint, "")
                # Older research-fit caches may be bounded excerpts, not complete abstracts.
                if cached and len(cached) <= 8000:
                    abstract, provenance = cached, "cached-crossref-metadata"
            fields = {
                "type": "Research Reference", "title": data["title"], "resource": _source(library, key),
                "sources": [{"id": "zotero", "resource": _source(library, key)}],
                "privacy": "private", "zotero_key": key, "library_id": library,
                "authors": [c.get("name") or " ".join(filter(None, [c.get("firstName"), c.get("lastName")])) for c in data.get("creators", [])],
                "date": data.get("date"), "doi": data.get("DOI"), "url": data.get("url"),
                "rights": data.get("rights"), "abstract_source": provenance,
                "abstract_status": "available" if abstract else "unavailable",
                "full_text_status": "available" if full else "unavailable",
            }
            if abstract:
                fields["sources"].append({"id": "abstract", "resource": (f"https://doi.org/{data['DOI']}" if provenance == "cached-crossref-metadata" and data.get("DOI") else _source(library, key)), "title": f"Abstract ({provenance})"})
            body = ["---", *[f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()], "---", "", f"# {data['title']}", "", "## Abstract", "", abstract or "No sourced abstract is available. This file is a bibliographic record.", "", "## Full text", ""]
            body += [f"- [Full research Markdown]({quote('../' + p.name)})" for p in sorted(set(full))] or ["Full-text Markdown is not available locally. A reference or abstract does not establish full-text coverage."]
            body += ["", "## Provenance", "", f"[Zotero reference]({_source(library, key)}). Abstract source: {provenance}. Private research use; publication permissions are separate.", ""]
            content = "\n".join(body)
            edited = path.exists() and (not old.get("sha256") or sha256_file(path) != old["sha256"])
            if edited:
                preserved.append(data["title"])
            elif not path.exists() or path.read_text(encoding="utf-8") != content:
                atomic_write_text(path, content)
            rows.append({"id": identity, "path": name, "title": data["title"], "abstract_source": provenance,
                         "abstract_available": bool(abstract), "full_text": [p.name for p in sorted(set(full))],
                         "sha256": old.get("sha256") if edited else sha256_file(path), "edited": edited})
        index = ["# Private research library", "", "All Zotero references, with sourced abstracts where available. Full research Markdown stays in the parent papers directory. Missing text is explicitly marked; license does not filter private coverage.", ""]
        index += [f"- [{_label(row['title'])}]({quote(row['path'])}) — abstract: {'available' if row['abstract_available'] else 'unavailable'}; full text: {'available' if row['full_text'] else 'unavailable'}" for row in rows]
        index_content = "\n".join(index) + "\n"
        index_path = directory / "index.md"
        prior_index_hash = _read(directory / "index.json").get("index_sha256")
        index_edited = index_path.exists() and (not prior_index_hash or sha256_file(index_path) != prior_index_hash)
        if not index_edited:
            atomic_write_text(index_path, index_content)
        # Keep historical records/files when Zotero references disappear.
        entries = {**previous, **{row["id"]: row for row in rows}}
        atomic_write_json(directory / "index.json", {"schema": "one-more-paper-references/v1", "okf_version": "0.2", "references": entries,
                                                  "active_ids": [row["id"] for row in rows],
                                                  "index_sha256": prior_index_hash if index_edited else sha256_file(index_path)})
    return {"directory": str(directory), "references": len(rows), "with_abstract": sum(row["abstract_available"] for row in rows),
            "with_full_text": sum(bool(row["full_text"]) for row in rows), "preserved_edits": preserved, "index_edited": index_edited}
