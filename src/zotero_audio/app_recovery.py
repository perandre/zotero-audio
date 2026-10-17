"""Recover publication intent, never infer it from imported recordings."""
from __future__ import annotations

import json
from pathlib import Path

from .podcast import resolve_license
from .util import sha256_file
from .zotero import license_record_from_metadata


def reconcile_publications(store):
    from .generation import NARRATION_POLICY
    if not store.settings()["auto_publish"]:
        return 0
    # Only durable audio requests establish intent. Importing an old recording,
    # changing its metadata or discovering an open license is insufficient.
    with store.db() as db:
        jobs = [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs ORDER BY created_at")]
        deliveries = [json.loads(row[0])["record"] for row in db.execute(
            "SELECT data FROM deliveries WHERE kind='publish' AND status IN ('pending','running')")]
    intents = {}
    for job in jobs:
        editions = {"brief", "full"} if job["action"] == "both" else {job["action"]}
        if not editions & {"brief", "full"}:
            continue
        ids = [job["article_id"]] if job.get("article_id") else [r["id"] for r in job.get("results", [])]
        for key in ids:
            for edition in editions:
                outcome = next((r for r in job.get("results", []) if r.get("id") == key), {})
                intents[key, edition] = (job if outcome.get("status") != "failed" else {"status": "failed"})
    pending = {(v.get("audio_sha256"), v.get("edition")) for v in deliveries}
    queued = 0
    for article in store.all_articles():
        if article.get("source_changed"):
            continue
        for edition, old in article.get("editions", {}).items():
            intent = intents.get((article["id"], edition), {})
            if (not old.get("selected") or intent.get("status") != "completed"
                    or (old.get("generation_job_id") and old["generation_job_id"] != intent["id"])
                    or old.get("publication_status") not in {"private", "pending", "retry_pending"}
                    or (old.get("audio_sha256"), edition) in pending):
                continue
            # A restored historical snapshot cannot stand in for a requested
            # narration refresh. Legacy records must match their saved current
            # plan and encoded revision; new records also carry the job receipt.
            stage = Path(article.get("bundle") or "") / "editions" / edition
            try:
                plan = json.loads((stage / "speech-plan.json").read_text())
                encoded = json.loads((stage / "edition.json").read_text())
            except (OSError, ValueError):
                continue
            if (plan.get("narration_policy") != NARRATION_POLICY
                    or not old.get("plan_sha256") or old["plan_sha256"] != plan.get("plan_sha256")
                    or not old.get("assembly_sha256") or old["assembly_sha256"] != encoded.get("assembly_sha256")
                    or old.get("audio_sha256") != encoded.get("audio_sha256")
                    or old.get("synthesis_config") != encoded.get("synthesis_config")):
                continue
            source_sha = old.get("source_sha256")
            if not source_sha or source_sha != article.get("source_sha256"):
                continue
            metadata = article.get("metadata", {})
            evidence = (license_record_from_metadata(metadata, source_sha) if metadata.get("rights")
                        else old.get("license_record"))
            license_result = resolve_license(evidence, source_sha256=source_sha)
            if not license_result.get("allowed"):
                continue
            # Verify actual bytes before reviving an old receipt. Missing files
            # are handled separately by automatic generation recovery.
            fields = [("audio", old.get("audio_sha256")), ("markdown", old.get("markdown_sha256"))]
            if any(not old.get(f) or not h or not Path(old[f]).is_file()
                   or sha256_file(Path(old[f])) != h for f, h in fields):
                continue
            source = Path(article.get("source_path") or "")
            if not source.is_file() or sha256_file(source) != source_sha:
                continue
            record = {**old, "license_record": evidence, "source_license": license_result,
                      "public_eligible": True, "publication_status": "pending"}
            # Queue first: restart between these writes cannot strand intent.
            store.enqueue_delivery("publish", article["id"], {"record": record})
            with store.edit_article(article["id"]) as current:
                latest = current.get("editions", {}).get(edition, {})
                if latest.get("audio_sha256") == record["audio_sha256"] and latest.get("publication_status") != "published":
                    latest.update(record)
                    current["publication_status"] = "pending"
            store.event("publication_recovered", title=article["title"], edition=edition)
            queued += 1
    return queued
