const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const api = require("../zotero-addon/feed-api.js");

function fixture() {
  const feeds = [7, 3].map(libraryID => ({
    libraryID, name: `Feed ${libraryID}`, url: `https://example.org/${libraryID}.xml`,
    unreadCount: libraryID === 3 ? 2 : 0, lastCheck: "2026-09-10 10:00:00",
    getDataLoaded() { return true; },
    async waitForDataLoad() { throw new Error("Already loaded"); }
  }));
  const items = [
    { id: 11, isRead: true },
    { id: 13, isRead: false },
    { id: 12, isRead: false },
    { id: 14, isRead: false, deleted: true },
    { id: 15, isRead: false, isFeedItem: false },
    { id: 16, isRead: false, libraryID: 1 }
  ].map(values => ({
    libraryID: 3, isFeedItem: true, deleted: false, isTranslated: false,
    key: `KEY${values.id}`, guid: `urn:entry:${values.id}`,
    ...values,
    async loadAllData() { this.loaded = true; },
    toJSON() {
      assert(this.loaded);
      return { title: `Paper ${this.id}`, abstractNote: "An abstract.",
        creators: [{ creatorType: "author", name: "A Research Group" }],
        DOI: "10.1234/example", itemType: "journalArticle" };
    },
    saveTx() { throw new Error("Feed reads must never save items"); }
  }));
  const zotero = {
    Prefs: { get(name) { assert.equal(name, "httpServer.localAPI.enabled"); return true; } },
    Feeds: { getAll() { return feeds; } },
    FeedItems: { async getAll(id, top, deleted) {
      assert.equal(top, true);
      assert.equal(deleted, false);
      return items.filter(item => item.libraryID === id || item.id === 16);
    } },
    Server: { Endpoints: {} }
  };
  const instance = api.create(zotero);
  instance.start();
  async function request(query = "", libraryID = "3", headers = {}, method = "GET") {
    const path = libraryID === null ? "/zotero-audio/feeds" : "/zotero-audio/feeds/:libraryID/items";
    const handler = new zotero.Server.Endpoints[path]();
    const result = await handler.init({ method, headers, searchParams: new URLSearchParams(query),
      pathParams: { libraryID } });
    return { status: result[0], headers: result[1], body: JSON.parse(result[2]) };
  }
  return { zotero, instance, request, items, feeds };
}

test("lists subscriptions and handles an empty inbox", async () => {
  const f = fixture();
  const result = await f.request("", null);
  assert.equal(result.status, 200);
  assert.equal(result.body.schema, "zotero-audio-feeds/v1");
  assert.deepEqual(result.body.feeds.map(feed => feed.libraryID), [3, 7]);
  assert.equal(result.body.feeds[0].unreadCount, 2);
  assert.equal(result.body.feeds[0].lastUpdate, null);
  assert.equal(result.headers["Cache-Control"], "no-store");
  assert.equal(result.headers["X-Content-Type-Options"], "nosniff");
  f.feeds.length = 0;
  assert.deepEqual((await f.request("", null)).body.feeds, []);
});

test("returns metadata and pages unread entries without changing read state", async () => {
  const f = fixture();
  const before = f.items.map(item => [item.id, item.isRead, item.isTranslated]);
  const first = await f.request("state=unread&limit=1");
  assert.equal(first.status, 200);
  assert.equal(first.body.total, 2);
  assert.equal(first.body.nextStart, 1);
  assert.equal(first.body.items[0].id, 13);
  assert.equal(first.body.items[0].data.abstractNote, "An abstract.");
  assert.equal(first.body.items[0].guid, "urn:entry:13");
  assert.equal(first.body.items[0].isRead, false);
  const second = await f.request("state=unread&limit=1&start=1");
  assert.equal(second.body.items[0].id, 12);
  assert.equal(second.body.nextStart, null);
  assert.deepEqual(f.items.map(item => [item.id, item.isRead, item.isTranslated]), before);
});

test("supports all/read filters, empty feeds and out-of-range pages", async () => {
  const f = fixture();
  assert.deepEqual((await f.request()).body.items.map(item => item.id), [13, 12, 11]);
  assert.deepEqual((await f.request("state=read")).body.items.map(item => item.id), [11]);
  const empty = await f.request("", "7");
  assert.equal(empty.status, 200);
  assert.equal(empty.body.total, 0);
  assert.deepEqual(empty.body.items, []);
  const beyond = await f.request("start=500");
  assert.deepEqual(beyond.body.items, []);
  assert.equal(beyond.body.nextStart, null);
});

test("waits for feed data to load before reading", async () => {
  const f = fixture();
  let waited = false;
  f.feeds[1].getDataLoaded = type => { assert.equal(type, "item"); return false; };
  f.feeds[1].waitForDataLoad = async type => { assert.equal(type, "item"); waited = true; };
  const getAll = f.zotero.FeedItems.getAll;
  f.zotero.FeedItems.getAll = async (...args) => { assert(waited); return getAll(...args); };
  assert.equal((await f.request()).status, 200);
});

test("rejects invalid queries and IDs instead of silently returning the wrong scope", async () => {
  const f = fixture();
  for (const query of ["state=other", "state=", "limit=0", "limit=501", "limit=1.5",
    "limit=2x", "limit=", "limit=01", "start=-1", "start=9007199254740992",
    "state=read&state=unread", "since=123", "libraryID=1"]) {
    assert.equal((await f.request(query)).status, 400, query);
  }
  for (const id of ["-1", "0", "abc", "3x", "1.2", "9007199254740992"]) {
    assert.equal((await f.request("", id)).status, 400, id);
  }
  for (const id of ["1", "99"]) {
    const result = await f.request("", id);
    assert.equal(result.status, 404);
    assert.equal(result.body.error.code, "feed_not_found");
  }
  assert.equal((await f.request("limit=1", null)).status, 400);
});

test("honors disabled API access and rejects browser origins and writes", async () => {
  const f = fixture();
  for (const handler of Object.values(f.zotero.Server.Endpoints)) {
    assert.deepEqual(new handler().supportedMethods, ["GET"]);
    assert.equal(new handler().allowRequestsFromUnsafeWebContent, false);
  }
  assert.equal((await f.request("", "3", { Origin: "https://example.org" })).status, 403);
  assert.equal((await f.request("", "3", { origin: "null" })).status, 403);
  assert.equal((await f.request("", "3", {}, "POST")).status, 405);
  f.zotero.Prefs.get = () => false;
  f.zotero.Feeds.getAll = () => { throw new Error("Must not read private data"); };
  assert.equal((await f.request()).body.error.code, "local_api_disabled");
  assert.equal((await f.request("", null)).status, 403);
});

test("does not expose private feed URLs through error messages", async () => {
  const f = fixture();
  f.zotero.FeedItems.getAll = () => { throw new Error("https://example.org/private?token=secret"); };
  const result = await f.request();
  assert.equal(result.status, 500);
  assert.equal(result.body.error.code, "feed_read_failed");
  assert(!JSON.stringify(result).includes("secret"));
});

test("registers and removes only its own endpoints, with no partial registration on conflict", () => {
  const f = fixture();
  f.instance.start();
  const replacement = function () {};
  f.zotero.Server.Endpoints["/zotero-audio/feeds"] = replacement;
  f.instance.stop();
  assert.equal(f.zotero.Server.Endpoints["/zotero-audio/feeds"], replacement);
  assert(!f.zotero.Server.Endpoints["/zotero-audio/feeds/:libraryID/items"]);
  assert.throws(() => f.instance.start(), /already registered/);
  assert(!f.zotero.Server.Endpoints["/zotero-audio/feeds/:libraryID/items"]);
  f.instance.stop();
});

test("bootstrap starts both features and removes endpoints on shutdown", async () => {
  const f = fixture();
  f.instance.stop();
  let starts = 0, stops = 0;
  const scope = vm.createContext({
    Zotero: f.zotero, IOUtils: {}, PathUtils: {},
    Components: { classes: {}, interfaces: {} },
    Services: { scriptloader: { loadSubScript(path, target) {
      if (path.endsWith("license-status.js")) {
        target.AudioLicenseStatus = { start() { starts++; }, stop() { stops++; } };
      } else {
        assert(path.endsWith("feed-api.js"));
        target.AudioFeedAPI = api;
      }
    } } }
  });
  vm.runInContext(fs.readFileSync(require.resolve("../zotero-addon/bootstrap.js"), "utf8"), scope);
  await scope.startup({ rootURI: "file:///addon/" });
  assert.equal(starts, 1);
  assert.equal(Object.keys(f.zotero.Server.Endpoints).length, 2);
  scope.shutdown();
  scope.shutdown();
  assert.equal(stops, 1);
  assert.equal(Object.keys(f.zotero.Server.Endpoints).length, 0);
  assert(!f.zotero.AudioLicenseStatus);
});
