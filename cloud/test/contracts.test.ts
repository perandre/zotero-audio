import { test } from "node:test";
import assert from "node:assert/strict";
import { jobSchema } from "../src/jobs";
import { readableDocumentFilename, searchExpression, searchableBody } from "../src/library";
import { settingsSchema, defaultSettings } from "../src/settings";
import { idFromPath, integer } from "../src/http";

test("A single-paper run cannot silently turn into a library-wide run", () => {
  assert.equal(
    jobSchema.safeParse({ action: "full", scope: "one" }).success,
    false,
  );
  assert.equal(
    jobSchema.safeParse({
      action: "full",
      scope: "one",
      article_id: "ABCD1234",
    }).success,
    true,
  );
  assert.equal(
    jobSchema.safeParse({ action: "shell", scope: "all" }).success,
    false,
  );
});
test("Search terms preserve Unicode and neutralize FTS operators", () => {
  assert.equal(
    searchExpression('AI OR "success" *'),
    '"AI"* AND "OR"* AND "success"*',
  );
  assert.equal(
    searchExpression("norsk læring 2025"),
    '"norsk"* AND "læring"* AND "2025"*',
  );
  assert.equal(searchExpression("***"), "");
  assert.equal(
    searchExpression(Array(40).fill("paper").join(" ")).split(" AND ").length,
    20,
  );
});
test("Settings keep QA warnings enabled and restrict models to implemented Kokoro", () => {
  assert.equal(settingsSchema.parse(defaultSettings).qa_enabled, true);
  assert.equal(
    settingsSchema.safeParse({
      ...defaultSettings,
      tts_model: "unconfigured-cloud-api",
    }).success,
    false,
  );
  assert.equal(
    settingsSchema.safeParse({ ...defaultSettings, speed: 0 }).success,
    false,
  );
});
test("Artifact paths reject traversal and all pagination is bounded", () => {
  assert.equal(idFromPath("zotero%3AABCD1234"), "zotero:ABCD1234");
  assert.throws(() => idFromPath("..%2Fprivate"));
  assert.throws(() => idFromPath("%zz"));
  assert.equal(integer("100000000", 50, 100), 100);
  assert.equal(integer("-4", 50, 100), 0);
  assert.equal(integer("NaN", 50, 100), 50);
});

test("Document object names remain readable while rejecting unsafe path characters", () => {
  assert.equal(
    readableDocumentFilename("How companies use AI — Author (2025)"),
    "How companies use AI - Author (2025).md",
  );
  assert.equal(
    readableDocumentFilename("A paper / with a very long title"),
    "A paper - with a very long title.md",
  );
});

test("Search indexes the article, while provenance stays in its original file", () => {
  const original =
    "---\nschema: internal-v1\nzotero_key: HIDDENKEY\n---\n# Company success\n<!-- pdf-page: 4 -->\nThe company improved training.\n\n---\nReferences remain.";
  const text = searchableBody(original);
  assert.ok(!text.includes("HIDDENKEY"));
  assert.ok(!text.includes("pdf-page"));
  assert.ok(text.includes("Company success"));
  assert.ok(text.includes("---\nReferences remain."));
});
