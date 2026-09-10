/* Read Zotero's feed inbox through the existing localhost HTTP server. */
var AudioFeedAPI = {
  schema: "zotero-audio-feeds/v1",

  create(zotero) {
    const schema = this.schema;
    const base = "/zotero-audio/feeds";
    const endpoints = new Map();

    function response(status, body) {
      return [status, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
      }, JSON.stringify({ schema, ...body })];
    }

    function failure(status, code, message) {
      return response(status, { error: { code, message } });
    }

    function feedJSON(feed) {
      return {
        libraryID: feed.libraryID,
        name: feed.name,
        url: feed.url,
        unreadCount: feed.unreadCount,
        lastCheck: feed.lastCheck || null,
        lastUpdate: feed.lastUpdate || null,
        lastCheckError: feed.lastCheckError || null
      };
    }

    function integer(value, fallback, min, max) {
      if (value === null) return fallback;
      if (!/^(0|[1-9][0-9]*)$/.test(value)) return null;
      const number = Number(value);
      return Number.isSafeInteger(number) && number >= min && number <= max ? number : null;
    }

    function endpoint(run) {
      return class {
        supportedMethods = ["GET"];
        allowRequestsFromUnsafeWebContent = false;

        async init(request) {
          if (!zotero.Prefs.get("httpServer.localAPI.enabled")) {
            return failure(403, "local_api_disabled", "Enable Zotero's local API in Settings > Advanced.");
          }
          if (request.method !== "GET") {
            return failure(405, "method_not_allowed", "This endpoint supports GET only.");
          }
          // Also reject browser origins when a caller sets Zotero's connector header.
          if (Object.keys(request.headers || {}).some(name => name.toLowerCase() === "origin")) {
            return failure(403, "browser_request_denied", "Use a local application to read feeds.");
          }
          try {
            return await run(request);
          } catch (_) {
            // Feed URLs can contain private alert tokens; do not log request data or errors.
            return failure(500, "feed_read_failed", "Could not read Zotero's feed data. Retry after Zotero finishes updating.");
          }
        }
      };
    }

    endpoints.set(base, endpoint(async ({ searchParams }) => {
      if ([...searchParams.keys()].length) {
        return failure(400, "invalid_query", "The subscriptions endpoint takes no query parameters.");
      }
      const feeds = zotero.Feeds.getAll().slice().sort((a, b) => a.libraryID - b.libraryID);
      return response(200, { feeds: feeds.map(feedJSON) });
    }));

    endpoints.set(base + "/:libraryID/items", endpoint(async ({ pathParams, searchParams }) => {
      const libraryID = integer(pathParams.libraryID, null, 1, Number.MAX_SAFE_INTEGER);
      if (libraryID === null) {
        return failure(400, "invalid_library_id", "libraryID must be a positive integer from the subscriptions endpoint.");
      }
      const allowed = new Set(["state", "start", "limit"]);
      if ([...searchParams.keys()].some(name => !allowed.has(name) || searchParams.getAll(name).length > 1)) {
        return failure(400, "invalid_query", "Use each of state, start, and limit at most once.");
      }
      const state = searchParams.get("state") ?? "all";
      const start = integer(searchParams.get("start"), 0, 0, Number.MAX_SAFE_INTEGER);
      const limit = integer(searchParams.get("limit"), 100, 1, 500);
      if (!["all", "read", "unread"].includes(state) || start === null || limit === null) {
        return failure(400, "invalid_query", "state must be all, read, or unread; start a nonnegative integer; limit an integer from 1 to 500.");
      }
      // Resolve only subscribed feeds; never accept a personal or group library ID.
      const feed = zotero.Feeds.getAll().find(candidate => candidate.libraryID === libraryID);
      if (!feed) return failure(404, "feed_not_found", "No subscribed feed has this libraryID.");
      if (!feed.getDataLoaded("item")) await feed.waitForDataLoad("item");
      const items = (await zotero.FeedItems.getAll(libraryID, true, false))
        .filter(item => item.isFeedItem && !item.deleted && item.libraryID === libraryID
          && (state === "all" || item.isRead === (state === "read")))
        .sort((a, b) => b.id - a.id);
      const page = [];
      for (const item of items.slice(start, start + limit)) {
        await item.loadAllData();
        page.push({
          id: item.id,
          key: item.key,
          guid: item.guid,
          isRead: item.isRead,
          isTranslated: item.isTranslated,
          data: item.toJSON()
        });
      }
      return response(200, {
        feed: feedJSON(feed), state, total: items.length, start, limit,
        nextStart: start + page.length < items.length ? start + page.length : null,
        items: page
      });
    }));

    return {
      start() {
        for (const [path, handler] of endpoints) {
          if (zotero.Server.Endpoints[path] && zotero.Server.Endpoints[path] !== handler) {
            throw new Error("A Zotero Audio feed endpoint is already registered");
          }
        }
        for (const [path, handler] of endpoints) zotero.Server.Endpoints[path] = handler;
      },
      stop() {
        for (const [path, handler] of endpoints) {
          if (zotero.Server.Endpoints[path] === handler) delete zotero.Server.Endpoints[path];
        }
      }
    };
  }
};

if (typeof module !== "undefined") module.exports = AudioFeedAPI;
