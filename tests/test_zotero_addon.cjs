const assert = require("node:assert/strict");
const { test } = require("node:test");
const status = require("../zotero-addon/license-status.js");

function item(extra = "My original notes.\n", tags = ["podcast", "research"]) {
  return {
    extra, tags: new Set(tags), saves: 0,
    hasTag(tag) { return this.tags.has(tag); },
    addTag(tag) { this.tags.add(tag); },
    removeTag(tag) { this.tags.delete(tag); },
    isRegularItem() { return true; },
    getField(field) { assert.equal(field, "extra"); return this.extra; },
    setField(field, value) { assert.equal(field, "extra"); this.extra = value; },
    async saveTx() { this.saves++; }
  };
}
const blocked = { status: "blocked", attachments: [
  { key: "PDF00001", allowed: false, reason: "missing-license-record" }
] };
const passed = { status: "pass", attachments: [
  { key: "PDF00001", allowed: true, license_url: "https://creativecommons.org/licenses/by/4.0/" }
] };

test("updates only managed metadata and avoids redundant writes", async () => {
  const record = item();
  const original = record.extra;
  await status.applyOutcome(record, blocked);
  assert(record.tags.has("audio:license:blocked"));
  assert(record.tags.has("podcast"));
  assert(record.tags.has("research"));
  assert.equal(status.stripStatus(record.extra), original);
  await status.applyOutcome(record, blocked);
  assert.equal(record.saves, 1);
  await status.applyOutcome(record, passed);
  assert(!record.tags.has("audio:license:blocked"));
  assert(record.tags.has("audio:license:pass"));
  assert.equal(status.stripStatus(record.extra), original);
  assert(!record.extra.includes("missing-license-record"));
  await status.applyOutcome(record, { status: null, attachments: [] });
  assert.deepEqual([...record.tags], ["podcast", "research"]);
  assert.equal(record.extra, original);
});

test("notes appended after the managed block survive refresh", () => {
  const extra = status.extraFor("Before", blocked) + "\nAfter";
  assert.equal(status.stripStatus(extra), "Before\nAfter");
  assert.equal(status.stripStatus(status.extraFor(extra, passed)), "Before\nAfter");
});

test("standalone PDFs receive tags without unsupported Extra fields", async () => {
  const record = item();
  record.isRegularItem = () => false;
  record.getField = () => { throw new Error("Attachment has no Extra"); };
  await status.applyOutcome(record, blocked);
  assert(record.tags.has("audio:license:blocked"));
});
