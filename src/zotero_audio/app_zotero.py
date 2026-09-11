"""Keep Zotero discovery independent of generation, delivery and the browser."""
from __future__ import annotations

from pathlib import Path

from .app_library import import_existing, refresh_zotero
from .app_state import Store, digest, now


class ZoteroMonitor:
    POLL_SECONDS = 60

    def __init__(self, store: Store, stop):
        self.store = store
        self.stop = stop
        self.imported = False

    def run(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                # A disappearing attachment or import failure must not kill the
                # monitor or masquerade as a healthy, current Zotero catalog.
                self.store.set_state("zotero", {**self.store.state("zotero", {}),
                    "online": False, "checked_at": now(),
                    "message": "Zotero refresh failed; retrying automatically in one minute."})
                self.store.event("zotero_refresh_failed", error=type(exc).__name__)
            self.stop.wait(self.POLL_SECONDS)

    def cycle(self):
        if not self.imported:
            import_existing(self.store)
            self.imported = True
        result = refresh_zotero(self.store)
        if not result["online"] or self.stop.is_set():
            return result
        settings = self.store.settings()
        action = settings["auto_generate"]
        if action != "off":
            for article in self.store.all_articles():
                if self.stop.is_set():
                    break
                self.queue_article(article, action, settings["qa_enabled"])
        return result

    def queue_article(self, article, action, qa):
        if not article.get("in_zotero") or article.get("source_changed"):
            return
        if not article.get("source_sha256"):
            return
        if action == "markdown":
            if article.get("markdown") and Path(article["markdown"]).is_file():
                return
        else:
            wanted = {"brief", "full"} if action == "both" else {action}
            missing = [edition for edition in sorted(wanted)
                       if not (article.get("editions", {}).get(edition, {}).get("audio")
                               and Path(article["editions"][edition]["audio"]).is_file())]
            if not missing:
                return
            # Request only missing editions: importing existing audio must never
            # select it for publication as a side effect of making another one.
            action = "both" if len(missing) == 2 else missing[0]
        job_id = "zotero-" + digest({"article": article["id"], "source": article["source_sha256"], "action": action})[:32]
        self.store.create_job({"action": action, "scope": "one", "article_id": article["id"], "qa": qa}, job_id=job_id, automatic=True)
