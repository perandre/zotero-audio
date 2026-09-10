/* Zotero writes its own records; the existing Python gate evaluates evidence. */
var AudioLicenseStatus = {
  tags: { pass: "audio:license:pass", blocked: "audio:license:blocked" },
  searchName: "Audio — License blocked",
  marker: /\n?\[Zotero Audio license gate\][\s\S]*?\[\/Zotero Audio license gate\]/g,

  stripStatus(extra) {
    return extra.replace(this.marker, "");
  },

  extraFor(extra, outcome) {
    const original = this.stripStatus(extra);
    if (!outcome.status) return original;
    const lines = outcome.attachments.map(a => {
      const license = a.license_url || a.asserted_license || "license unknown";
      return `${a.key}: ${a.allowed ? "pass" : "blocked"}; ${license}${a.reason ? "; " + a.reason : ""}`;
    });
    return [original, "[Zotero Audio license gate]", `Status: ${outcome.status}`,
      ...lines, "[/Zotero Audio license gate]"].filter(Boolean).join("\n");
  },

  start() {
    this.stopped = false;
    this.timer = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
    this.timer.initWithCallback(() => this.refresh(), 300000, Ci.nsITimer.TYPE_REPEATING_SLACK);
    this.observer = Zotero.Notifier.registerObserver({
      notify: () => {
        if (this.applying || this.stopped) return;
        if (this.debounce) this.debounce.cancel();
        this.debounce = Cc["@mozilla.org/timer;1"].createInstance(Ci.nsITimer);
        this.debounce.initWithCallback(() => this.refresh(), 2000, Ci.nsITimer.TYPE_ONE_SHOT);
      }
    }, ["item"], "audio-license-status");
    this.refresh();
  },

  stop() {
    this.stopped = true;
    this.timer?.cancel();
    this.debounce?.cancel();
    if (this.observer) Zotero.Notifier.unregisterObserver(this.observer);
  },

  paths() {
    const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
    const runtime = Zotero.Prefs.get("audioLicenseStatus.runtime")
      || PathUtils.join(home, "Sites", "zotero-audio-runtime");
    const repo = Zotero.Prefs.get("audioLicenseStatus.repo")
      || PathUtils.join(home, "Sites", "zotero-audio");
    return { runtime, repo,
      input: PathUtils.join(runtime, "zotero-license-status-input.json"),
      output: PathUtils.join(runtime, "zotero-license-status.json"),
      sync: PathUtils.join(runtime, "zotero-license-status-sync.json") };
  },

  async snapshot(item) {
    const fields = {};
    if (item.isRegularItem()) {
      for (const field of ["rights", "extra", "DOI", "url"]) {
        fields[field] = item.getField(field) || "";
      }
      fields.extra = this.stripStatus(fields.extra);
    }
    const children = item.isRegularItem()
      ? await Zotero.Items.getAsync(item.getAttachments()) : [item];
    const attachments = [];
    for (const child of children) {
      if (child.deleted || !child.isPDFAttachment()) continue;
      const path = await child.getFilePathAsync();
      let fileState = null;
      if (path) {
        try {
          const stat = await IOUtils.stat(path);
          fileState = { size: stat.size, modified: stat.lastModified };
        } catch (error) {
          if (error.name !== "NotFoundError") throw error;
        }
      }
      attachments.push({ key: child.key, path: path || null, fileState });
    }
    attachments.sort((a, b) => a.key.localeCompare(b.key));
    return { key: item.key, fields, attachments };
  },

  async ensureSearch(libraryID) {
    const searches = await Zotero.Searches.getAll(libraryID);
    const key = Zotero.Prefs.get("audioLicenseStatus.searchKey");
    const existing = searches.find(s => s.key === key)
      || searches.find(s => s.name === this.searchName);
    if (existing) {
      const conditions = Object.values(existing.getConditions());
      if (conditions.length !== 1 || conditions[0].condition !== "tag"
          || conditions[0].operator !== "is" || conditions[0].value !== this.tags.blocked) {
        throw new Error("The license saved search was edited; restore its single blocked-tag condition.");
      }
      return existing;
    }
    const search = new Zotero.Search();
    search.libraryID = libraryID;
    search.name = this.searchName;
    search.addCondition("tag", "is", this.tags.blocked);
    await search.saveTx();
    Zotero.Prefs.set("audioLicenseStatus.searchKey", search.key);
    return search;
  },

  async applyOutcome(item, outcome) {
    let changed = false;
    for (const [status, tag] of Object.entries(this.tags)) {
      if (status === outcome.status && !item.hasTag(tag)) {
        item.addTag(tag, 1);
        changed = true;
      } else if (status !== outcome.status && item.hasTag(tag)) {
        item.removeTag(tag);
        changed = true;
      }
    }
    if (item.isRegularItem()) {
      const extra = item.getField("extra") || "";
      const next = this.extraFor(extra, outcome);
      if (next !== extra) {
        item.setField("extra", next);
        changed = true;
      }
    }
    if (changed) await item.saveTx();
    return changed;
  },

  async refresh() {
    if (this.running || this.stopped) return;
    this.running = true;
    const paths = this.paths();
    try {
      const libraryID = Zotero.Libraries.userLibraryID;
      await this.ensureSearch(libraryID);
      const items = await Zotero.Items.getAll(libraryID, true, false);
      const snapshots = new Map();
      for (const item of items) {
        if (item.deleted || (!item.isRegularItem() && !item.isPDFAttachment())) continue;
        const snapshot = await this.snapshot(item);
        if (snapshot.attachments.length || Object.values(this.tags).some(t => item.hasTag(t))) {
          snapshots.set(item.key, snapshot);
        }
      }
      const requestID = Zotero.Utilities.randomString(24);
      await IOUtils.writeJSON(paths.input, { request_id: requestID, items: [...snapshots.values()] });
      await Zotero.Utilities.Internal.exec(PathUtils.join(paths.runtime, "venv", "bin", "python"), [
        PathUtils.join(paths.repo, "scripts", "sync_license_status.py"),
        "--input", paths.input, "--output", paths.output,
        "--bundles", PathUtils.join(paths.runtime, "full-library", "bundles"),
        "--evidence", PathUtils.join(paths.runtime, "podcast", "license-evidence")
      ]);
      const result = await IOUtils.readJSON(paths.output);
      if (result.schema !== "zotero-audio-license-status/v1" || result.request_id !== requestID) {
        throw new Error("License evaluator returned a stale or unsupported response");
      }
      if (this.stopped) return;
      this.applying = true;
      const summary = { status: "complete", pass: 0, blocked: 0, changed: 0, stale: 0 };
      for (const outcome of result.items) {
        const item = Zotero.Items.getByLibraryAndKey(libraryID, outcome.key);
        if (!item || item.deleted) continue;
        const current = await this.snapshot(item);
        if (JSON.stringify(current) !== JSON.stringify(snapshots.get(outcome.key))) {
          summary.stale++;
          continue;
        }
        if (await this.applyOutcome(item, outcome)) summary.changed++;
        if (outcome.status) summary[outcome.status]++;
      }
      await IOUtils.writeJSON(paths.sync, { ...summary, checked_at: new Date().toISOString() });
      this.lastResult = summary;
    } catch (error) {
      Zotero.logError(error);
      this.lastResult = { status: "failed", error: String(error) };
      await IOUtils.writeJSON(paths.sync, { ...this.lastResult, checked_at: new Date().toISOString() });
    } finally {
      this.applying = false;
      this.running = false;
    }
  }
};

if (typeof module !== "undefined") module.exports = AudioLicenseStatus;
