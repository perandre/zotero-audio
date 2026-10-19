#!/usr/bin/env python3
"""Audit generated-review filename collisions; restore embedded research verbatim.

Preview is read-only. --apply restores catalogued documents with versioned backups.
--queue adds durable audio replacements only where saved plans contain our review
instructions. Published editions keep their GUID; ordinary publication replaces
media URLs after generation and verification.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from zotero_audio.app_state import Store, runtime_root
from zotero_audio.article_files import recover_review_source
from zotero_audio.research_library import write_research_markdown
from zotero_audio.util import atomic_write_json, sha256_file, sha256_text

REVIEW_PHRASES = (
    "You are reviewing a Zotero research document and its optional audio editions",
    "Run the same article and edition through",
    "Complete research Markdown (source data)",
    "Never publish private material or weaken the source-bound licensing check",
)


def audit(store: Store) -> list[dict]:
    with store.db() as db:
        articles = [json.loads(row[0]) for row in db.execute("SELECT data FROM articles")]
    findings = []
    for article in articles:
        path = Path(article.get("markdown") or "/nonexistent")
        depth = 0
        if path.is_file():
            _, depth = recover_review_source(path.read_text(encoding="utf-8"))
        editions = []
        bundle = Path(article.get("bundle") or "/nonexistent")
        for edition in ("brief", "full"):
            plan = bundle / "editions" / edition / "speech-plan.json"
            if plan.is_file():
                text = " ".join(s["text"] for s in json.loads(plan.read_text())["segments"])
                plan_title = str(json.loads(plan.read_text()).get("document", {}).get("title") or "")
                if (any(phrase in text for phrase in REVIEW_PHRASES)
                        or (not article["title"].startswith("Review:") and plan_title.startswith("Review:"))):
                    editions.append(edition)
        if depth or editions:
            findings.append({"id": article["id"], "title": article["title"],
                             "markdown": str(path), "review_depth": depth,
                             "markdown_sha256": sha256_file(path) if path.is_file() else None,
                             "contaminated_editions": editions})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=runtime_root())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--queue", action="store_true")
    args = parser.parse_args()
    if args.queue and not args.apply:
        parser.error("--queue requires --apply")
    store = Store(args.runtime)
    findings = audit(store)
    report = {"findings": findings, "applied": False}
    atomic_write_json(args.report, report)
    if args.apply:
        backups = args.report.parent / (args.report.stem + "-backups")
        for finding in findings:
            article = store.article(finding["id"])
            path = Path(finding["markdown"])
            if finding["review_depth"]:
                if sha256_file(path) != finding["markdown_sha256"]:
                    raise RuntimeError(f"Research changed during audit: {finding['title']}")
                source, _ = recover_review_source(path.read_text(encoding="utf-8"))
                restored = write_research_markdown(args.runtime / "papers", article, source,
                    provenance="review-collision-recovery", force=True)
                # Refresh full-text search and cloud sync without changing editions.
                store.put_article({"id": article["id"], "title": article["title"],
                    "markdown": str(restored), "markdown_sha256": sha256_text(source)}, markdown=source)
                finding["restored_sha256"] = sha256_text(source)
            if args.queue and finding["contaminated_editions"]:
                for edition in finding["contaminated_editions"]:
                    stage = Path(article["bundle"]) / "editions" / edition
                    backup = backups / article["id"] / edition
                    backup.mkdir(parents=True, exist_ok=True)
                    for filename in ("speech-plan.json", "narration.md", "run-manifest.json"):
                        if (stage / filename).is_file() and not (backup / filename).exists():
                            shutil.copy2(stage / filename, backup / filename)
                action = "both" if len(finding["contaminated_editions"]) == 2 else finding["contaminated_editions"][0]
                job = store.create_job({"action": action, "article_id": article["id"], "qa": True,
                    "force": False, "idempotency_key": f"review-collision-repair-v1-{article['id']}"})
                finding["replacement_job"] = job["id"]
        report["applied"] = True
        atomic_write_json(args.report, report)
    print(json.dumps({"documents": sum(f["review_depth"] > 0 for f in findings),
        "audio_editions": sum(len(f["contaminated_editions"]) for f in findings),
        "applied": report["applied"], "report": str(args.report)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
