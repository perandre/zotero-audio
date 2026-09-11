import { test } from "node:test";
import assert from "node:assert/strict";
import { jobSchema } from "../src/jobs";
import { readableDocumentFilename, searchExpression, searchableBody } from "../src/library";
import { settingsSchema, defaultSettings } from "../src/settings";
import { idFromPath, integer } from "../src/http";
import {
  articleYear,
  readableArticle,
  readableArticleYear,
  unreadableArticleMessage,
} from "../src/article-recency";

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

test("Only scoped VIKING research articles use the 2025 reading cutoff", () => {
  assert.equal(articleYear("2026-04"), 2026);
  assert.equal(readableArticleYear("2025"), true);
  assert.equal(readableArticleYear("2024"), false);
  assert.equal(readableArticleYear(null), false);
  assert.equal(readableArticle({ year: 2024, reading_scope: "general" }), true);
  assert.equal(readableArticle({ year: 2025, reading_scope: "viking_research" }), true);
  assert.equal(readableArticle({ year: 2024, reading_scope: "viking_research" }), false);
  assert.match(unreadableArticleMessage("Old paper", 2024), /2025 onward/);
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

test("Automatic generation preserves older settings and validates output choices", () => {
  const { auto_generate: _automatic, ...legacy } = defaultSettings;
  assert.equal(settingsSchema.parse({ ...defaultSettings, ...legacy }).auto_generate, "markdown");
  assert.equal(Object.hasOwn(settingsSchema.partial().parse(legacy), "auto_generate"), false);
  for (const action of ["off", "markdown", "brief", "full", "both"]) {
    assert.equal(settingsSchema.parse({ ...legacy, auto_generate: action }).auto_generate, action);
  }
  assert.equal(settingsSchema.safeParse({ ...legacy, auto_generate: "delete" }).success, false);
  assert.equal(settingsSchema.safeParse({ ...legacy, auto_generate: true }).success, false);
});
