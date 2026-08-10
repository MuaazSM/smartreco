/*
 * SmartReco behavioral tracker (Jinja2 frontend).
 *
 * Efficient and non-blocking, mirroring frontend/lib/tracker.ts's contract in compact vanilla JS:
 *   - events are queued in memory and flushed in BATCHES (at 20 events or every 10s), never one
 *     request per action;
 *   - high-frequency inputs are throttled/debounced (search debounced 400ms);
 *   - nothing blocks the main thread and no synchronous XHR is used;
 *   - on tab hide / pagehide the queue is flushed via navigator.sendBeacon so in-flight signals
 *     (especially dwell) survive navigation.
 * The server assigns each event's weight by type (POST /api/events/batch) — the client never sends a
 * weight. Only runs when authenticated (events require the session cookie).
 */
(function () {
  if (!window.SMARTRECO_AUTHED) return;

  var ENDPOINT = "/api/events/batch";
  var FLUSH_AT = 20;
  var FLUSH_EVERY_MS = 10000;
  var SEARCH_DEBOUNCE_MS = 400;

  var sid = sessionStorage.getItem("sr_sid");
  if (!sid) { sid = uuid(); sessionStorage.setItem("sr_sid", sid); }

  var queue = [];

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function track(type, productId, payload) {
    queue.push({
      session_id: sid,
      event_type: type,
      client_ts: new Date().toISOString(),
      product_id: productId || null,
      event_id: uuid(),
      payload: payload || {}
    });
    if (queue.length >= FLUSH_AT) flush(false);
  }
  window.srTrack = track; // let pages fire custom events (e.g. "cart")

  function flush(useBeacon) {
    if (!queue.length) return;
    var batch = queue.splice(0, queue.length);
    var body = JSON.stringify({ events: batch });
    try {
      if (useBeacon && navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
      } else {
        fetch(ENDPOINT, {
          method: "POST", credentials: "include", keepalive: true,
          headers: { "Content-Type": "application/json" }, body: body
        }).catch(function () {});
      }
    } catch (_) { /* tracking must never break the page */ }
  }

  setInterval(function () { flush(false); }, FLUSH_EVERY_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush(true);
  });
  window.addEventListener("pagehide", function () { flush(true); });

  // --- page view (+ product view when a product page sets data-product-id) ---
  var pid = document.body.getAttribute("data-product-id") || null;
  track("view", pid, { path: location.pathname });

  // --- dwell: accumulated foreground time on a product page, emitted once on leave ---
  if (pid) {
    var lastVisible = Date.now(), visibleMs = 0;
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") { visibleMs += Date.now() - lastVisible; }
      else { lastVisible = Date.now(); }
    });
    window.addEventListener("pagehide", function () {
      if (document.visibilityState !== "hidden") visibleMs += Date.now() - lastVisible;
      track("dwell", pid, { dwell_ms: visibleMs });
      flush(true);
    });
  }

  // --- clicks on anything marked data-track-click (product links / cards) ---
  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-track-click]");
    if (el) track("click", el.getAttribute("data-track-click"), { source: el.getAttribute("data-source") || "link" });
  }, true);

  // --- search: debounced so a fast typist fires one event, not one per keystroke ---
  var search = document.getElementById("search");
  if (search) {
    var t;
    search.addEventListener("input", function () {
      clearTimeout(t);
      t = setTimeout(function () {
        var v = search.value.trim();
        if (v) track("search", null, { q: v });
      }, SEARCH_DEBOUNCE_MS);
    });
  }
})();
