/* global AFRAME, THREE */
// `figure-clip` — play a captured animation on a rigged figure (docs/plans/figures-and-library.md § 3).
//
// A clip is a GLB containing nothing but an `AnimationClip`: 222 channels of quaternion per frame, one
// per bone, and no mesh. It binds to a figure BY NODE NAME, which is why it plays on more than the
// figure it shipped with — measured across the library, Jane's `1_idle` resolves 222 of 222 target nodes
// on three other figures sharing her rig signature and 171–187 on the rest. The ones it does not resolve
// are bones those figures do not have, and dropping those channels is the whole of what "tier 1 reuse"
// means. Rigs that do NOT share a signature need the channels rewritten, which is a different feature.
//
// **Time comes from the shared clock, not from frame deltas.** Every headset in a world must be on the
// same frame of the same clip, and accumulating `delta` per client guarantees they drift apart — they
// start at different moments, drop different frames, and nothing ever pulls them back. So the entity
// stores the shared-clock instant the clip STARTED and each client computes its own offset into it: a
// client that joins late, stalls, or backgrounds for a minute lands on the right frame the next time it
// draws, with no resynchronisation message and nothing to get out of step with.
//
// **Precedence: a playing clip wins, a pose applies when idle.** Both write the same bones, so without a
// rule they fight — the mixer would win every frame simply by running later, and the pose would look
// broken rather than overridden. On stop the figure is put back on its bind pose and the pose re-applied
// (`figure.restore()`), because the alternative is leaving the skeleton wherever the clip's last frame
// happened to be.
(function () {
  "use strict";

  function log(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try {
      fetch("/client-log", { method: "POST", headers: { "Content-Type": "application/json" },
                             body: JSON.stringify({ tag: "figure-clip", msg: msg }) }).catch(function () {});
    } catch (e) { /* logging must never break playback */ }
  }

  function now() {
    return (window.ConjureClock && window.ConjureClock.now()) ? window.ConjureClock.now() : Date.now();
  }

  // One parse per URL per page. A clip is ~1 MB of keyframes and several figures may dance to the same
  // one; re-parsing per entity would also give each its own AnimationClip object for no reason.
  var CACHE = Object.create(null);

  function loadClip(url) {
    if (CACHE[url]) return CACHE[url];
    var Loader = THREE.GLTFLoader || (AFRAME.THREE && AFRAME.THREE.GLTFLoader);
    if (!Loader) return Promise.reject(new Error("no GLTFLoader on THREE"));
    CACHE[url] = new Promise(function (resolve, reject) {
      new Loader().load(url, function (gltf) { resolve(gltf.animations || []); },
                        undefined, function (err) { delete CACHE[url]; reject(err); });
    });
    return CACHE[url];
  }

  // Keep only the tracks whose target node exists on THIS figure. three would otherwise warn once per
  // unresolved binding — 51 lines for one clip on one figure — and, worse, the warning is all you get:
  // nothing tells you whether the clip half-played or not at all.
  function bindable(clip, root) {
    var have = Object.create(null);
    root.traverse(function (o) { if (o.name) have[THREE.PropertyBinding.sanitizeNodeName(o.name)] = true; });
    var kept = [], dropped = [];
    clip.tracks.forEach(function (track) {
      var parsed = THREE.PropertyBinding.parseTrackName(track.name);
      if (parsed && parsed.nodeName && !have[parsed.nodeName]) { dropped.push(parsed.nodeName); return; }
      kept.push(track);
    });
    return { clip: new THREE.AnimationClip(clip.name, clip.duration, kept, clip.blendMode),
             dropped: dropped };
  }

  AFRAME.registerComponent("figure-clip", {
    schema: {
      clip: { type: "string", default: "" },        // URL of the clip GLB, "" = nothing to play
      name: { type: "string", default: "" },        // which AnimationClip inside it; "" = the first
      playing: { type: "boolean", default: true },
      loop: { type: "boolean", default: true },
      speed: { type: "number", default: 1 },
      // Shared-clock milliseconds. 0 means "whenever this arrived", which is right for a clip started
      // by one client and wrong for one replayed from a snapshot — the server stamps it either way.
      startedAt: { type: "number", default: 0 }
    },

    init: function () {
      var self = this;
      this._mixer = null;
      this._action = null;
      this._loaded = "";                            // the URL the current action came from
      this._started = 0;
      // `gltf-model` loads asynchronously and a patch can arrive first — same reason `figure` and
      // `figure-parts` both re-apply here. Without it, playing a clip on a figure that is still loading
      // silently does nothing.
      this._onLoad = function () { self._teardown(); self.apply(); };
      this.el.addEventListener("model-loaded", this._onLoad);
      this.apply();
    },

    update: function (old) {
      if (old && old.clip !== this.data.clip) this._teardown();
      this.apply();
    },

    apply: function () {
      var self = this;
      var root = this.el.getObject3D("mesh");
      if (!root) return;                            // model-loaded retries
      if (!this.data.clip || !this.data.playing) { this._teardown(); return; }
      if (this._loaded === this.data.clip + "|" + this.data.name) return;   // already bound
      var url = this.data.clip, want = this.data.name;
      loadClip(url).then(function (clips) {
        if (self.data.clip !== url) return;         // it changed again while we were loading
        var picked = want ? clips.filter(function (c) { return c.name === want; })[0] : clips[0];
        if (!picked) {
          log("no clip " + (want || "[first]") + " in " + url + " (has: "
              + clips.map(function (c) { return c.name; }).join(", ") + ")");
          self.el.emit("figure-clip-error", { clip: url, name: want }, false);
          return;
        }
        var live = self.el.getObject3D("mesh");
        if (!live) return;
        var bound = bindable(picked, live);
        self._teardown();
        self._mixer = new THREE.AnimationMixer(live);
        self._action = self._mixer.clipAction(bound.clip);
        self._action.loop = self.data.loop ? THREE.LoopRepeat : THREE.LoopOnce;
        self._action.clampWhenFinished = !self.data.loop;
        self._action.play();
        self._loaded = url + "|" + want;
        self._started = self.data.startedAt || now();
        log("playing " + picked.name + " on " + (self.el.id || "?") + ": kept "
            + bound.clip.tracks.length + " track(s), dropped " + bound.dropped.length);
        self.el.emit("figure-clip-started", {
          clip: url, name: picked.name, duration: picked.duration,
          tracks: bound.clip.tracks.length, dropped: bound.dropped.length
        }, false);
      }).catch(function (err) {
        log("failed to load " + url + ": " + (err && err.message));
        self.el.emit("figure-clip-error", { clip: url, error: String(err && err.message) }, false);
      });
    },

    // Seek, do not advance. `mixer.update(delta)` would accumulate this client's own frame times, which
    // is exactly the drift the shared clock exists to avoid; setting the time and evaluating with a zero
    // delta makes the frame a pure function of the shared instant.
    tick: function () {
      if (!this._mixer || !this._action) return;
      var duration = this._action.getClip().duration;
      if (!duration) return;
      var t = (now() - this._started) / 1000 * (this.data.speed || 1);
      if (t < 0) t = 0;                             // a start stamped in the future: hold frame zero
      this._action.time = this.data.loop ? ((t % duration) + duration) % duration
                                         : Math.min(t, duration);
      this._mixer.update(0);
    },

    // Unbind AND put the skeleton back. An AnimationMixer leaves every bone it touched wherever the last
    // frame left it, so simply stopping is how a figure ends up frozen mid-stride.
    _teardown: function () {
      if (this._mixer) {
        this._mixer.stopAllAction();
        this._mixer.uncacheRoot(this._mixer.getRoot());
      }
      var had = !!this._mixer;
      this._mixer = null;
      this._action = null;
      this._loaded = "";
      var figure = this.el.components && this.el.components.figure;
      if (had && figure && figure.restore) figure.restore();
      if (had) this.el.emit("figure-clip-stopped", {}, false);
    },

    remove: function () {
      this.el.removeEventListener("model-loaded", this._onLoad);
      this._teardown();
    }
  });
})();
