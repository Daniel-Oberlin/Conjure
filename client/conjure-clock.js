/* global window, document, fetch */
// Shared clock (docs/specs/dynamics.md §6). A tiny Cristian/NTP-
// style sync to the SERVER clock so every client agrees on one time base — the foundation for tier-A
// dynamic modules (solar system, fireflies, spirographs, …) that compute state as
// f(sharedClock, seed, config) and therefore stay identical across headsets with ZERO per-frame sync.
//
//   window.ConjureClock.now()    → shared epoch milliseconds (server time, estimated on this client).
//   window.ConjureClock.status() → { offset, rttMs, synced } for diagnostics.
//   window.ConjureClock.sync()   → force a re-sync (returns a Promise).
//
// Until the first sync completes now() returns local Date.now() (offset 0), so callers never block; it
// converges to server time within the first round-trips and re-syncs periodically to correct drift.

(function () {
  "use strict";

  var offset = 0;              // serverNow - localNow (ms); added to Date.now() to get shared time
  var rttMs = null;           // round-trip time of the sample we adopted (uncertainty proxy)
  var synced = false;
  var SAMPLES = 5;            // round-trips per sync; keep the tightest (smallest RTT)
  var RESYNC_MS = 30000;     // re-sync cadence to correct clock drift

  function log(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[clock] " + msg); } catch (e) {}
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "clock", msg: msg }) }).catch(function () {});
    } catch (e) { /* never let logging break anything */ }
  }

  // One round-trip → estimate (serverNow - localNow) via Cristian's algorithm. The server's stamp is
  // assumed to be taken ~halfway through the round-trip, so we align it to t1. Returns {offset, rtt} or
  // null on failure.
  function sample() {
    var t0 = Date.now();
    return fetch("/time", { cache: "no-store" }).then(function (r) { return r.json(); })
      .then(function (j) {
        var t1 = Date.now(), rtt = t1 - t0;
        var serverAtT1 = j.t + rtt / 2;
        return { offset: serverAtT1 - t1, rtt: rtt };
      })
      .catch(function () { return null; });
  }

  // Take SAMPLES round-trips sequentially; adopt the one with the smallest RTT (least uncertainty).
  function sync() {
    var best = null, i = 0;
    function next() {
      if (i >= SAMPLES) {
        if (best) {
          offset = best.offset; rttMs = best.rtt; synced = true;
          log("synced offset=" + Math.round(offset) + "ms rtt=" + best.rtt + "ms");
        }
        return best;
      }
      i++;
      return sample().then(function (s) {
        if (s && (!best || s.rtt < best.rtt)) best = s;
        return next();
      });
    }
    return Promise.resolve(next());
  }

  window.ConjureClock = {
    now: function () { return Date.now() + offset; },
    status: function () { return { offset: offset, rttMs: rttMs, synced: synced }; },
    sync: sync
  };

  function start() { sync(); setInterval(sync, RESYNC_MS); }
  if (document.readyState === "complete" || document.readyState === "interactive") start();
  else window.addEventListener("DOMContentLoaded", start);
})();
