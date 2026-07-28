// Conjure geometry worker (branch fix/pops-and-jitters). Runs the heavy, PURE room-math off the render
// thread so the ~0.5 Hz capture solve never blows an XR frame's budget (the walking jitter: register was
// ~7 ms, spiking to ~11 ms, on the same frame as the render). Step 1 hosts RoomSnap.register; matchRef /
// snap / anchor-solve can migrate behind this SAME message boundary as scenes grow — never touching the
// frame budget again.
//
// Module worker, because: three 0.163 is ESM-only (no UMD to importScripts), and room-snap.js is UMD —
// imported for side effect, its wrapper takes the `else` branch (no CommonJS `module` here) and assigns
// `self.RoomSnap`. All RoomSnap math takes THREE as its first argument, so this worker's own standalone
// three (version-independent for pure vector/quat/matrix arithmetic) composes fine with A-Frame's three on
// the main thread — they never share objects, only plain numbers cross the wire.
//
// Imports are DYNAMIC + version-tagged: the main thread spawns us as `room-worker.js?v=<mtime>` and we
// forward that `?v=` onto our own imports, so a change to room-snap.js/three busts the worker's cached copy
// the same way the server's `?v=` busts the page's <script>s (the Quest caches /static aggressively). Top-
// level await is fine in a module worker — messages posted before we finish loading queue until we do.
var _v = new URLSearchParams(self.location.search).get("v");
var _q = _v ? "?v=" + _v : "";
var THREE = await import("./three.module.min.js" + _q);   // ESM namespace (THREE.Vector3, …)
await import("./room-snap.js" + _q);                       // UMD side effect → self.RoomSnap
var RoomSnap = self.RoomSnap;

// Rehydrate the compact wire form (plain arrays/scalars) into what register() consumes. Only the fields
// register() reads are sent (pos, nyaw, sem, orient, ext [+ id on refs]) — no quaternions, polygons, or raw
// poses — so the payload is a few KB even for a large room.
function deCur(c) {
  return { pos: new THREE.Vector3(c.p[0], c.p[1], c.p[2]), nyaw: c.nyaw, sem: c.sem, orient: c.orient, ext: c.ext };
}
function deRef(r) {
  return { id: r.id, pos: new THREE.Vector3(r.p[0], r.p[1], r.p[2]), nyaw: r.nyaw, sem: r.sem, orient: r.orient, ext: r.ext };
}

self.onmessage = function (e) {
  var m = e.data;
  if (!m || m.type !== "register") return;
  var cur = (m.cur || []).map(deCur), ref = (m.ref || []).map(deRef);
  var r = RoomSnap.register(THREE, cur, ref, m.opts);
  // Matrix4 → plain 16-element array; main rebuilds it. `seq` lets main drop a stale reply.
  self.postMessage({ type: "register", seq: m.seq,
    els: r.Tmat ? Array.from(r.Tmat.elements) : null,
    stat: r.stat, cov: r.cov, residuals: r.residuals || null });
};
