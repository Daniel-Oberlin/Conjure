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

  function isConstant(track) {
    var n = track.getValueSize(), v = track.values;
    for (var i = n; i < v.length; i++) {
      if (Math.abs(v[i] - v[i % n]) > 1e-5) return false;
    }
    return true;
  }

  // Decide what of a clip may touch THIS model. Three rules, each one a bug that was seen.
  //
  // 1. **A track whose node the model does not have is dropped.** three would otherwise warn once per
  //    unresolved binding — 51 lines for one clip — and the warning is all you get: nothing says
  //    whether the clip half-played or not at all.
  //
  // 2. **Nothing may write the transform of the node the mixer is rooted at.** That node carries
  //    PLACEMENT and UNIT CONVERSION, not animation. Alice's `RootNode` has `scale 0.01` baked in —
  //    centimetres to metres — and `WhiteboardIdleFIXFIXU` carries a `RootNode` scale track of
  //    `[1, 1, 1]`. Playing it multiplied her by a hundred: she filled the room, her clothes turned
  //    inside out around the camera, and because a hundred-metre figure has no parallax she appeared
  //    pinned to the viewer while the world slid past.
  //
  // 3. **A CONSTANT scale track is dropped** wherever it sits. Measured across 120 clips: 22,714 of
  //    22,718 scale tracks never change value. They animate nothing and exist only because the
  //    exporter wrote a channel per bone per property — so every one is a latent version of rule 2,
  //    waiting for a model whose rest scale is not 1.
  //
  // 4. **An animating POSITION track is re-based onto the target's rest.** The clip states where the
  //    figure stood in the scene it was captured from: `CC_Base_BoneRoot` sweeps 294→316 units where
  //    Alice rests at 0, which is three metres of displacement before she has moved at all. Motion is
  //    the clip's business and location is the entity's, so the first frame is pinned to the model's
  //    own rest and the rest of the curve rides on top — every bit of movement kept, the authored
  //    address discarded. A constant position track is just a rest offset restated, and goes.
  function retarget(clip, root) {
    var have = Object.create(null);
    root.traverse(function (o) {
      if (o.name) have[THREE.PropertyBinding.sanitizeNodeName(o.name)] = o;
    });
    var rootName = root.name ? THREE.PropertyBinding.sanitizeNodeName(root.name) : null;
    var kept = [];
    var why = { missing: 0, rootTransform: 0, flatScale: 0, restPosition: 0, rebased: 0 };

    clip.tracks.forEach(function (track) {
      var parsed = THREE.PropertyBinding.parseTrackName(track.name);
      var nodeName = parsed && parsed.nodeName;
      var prop = parsed && parsed.propertyName;
      var node = nodeName ? have[nodeName] : root;
      if (!node) { why.missing++; return; }
      if (nodeName && nodeName === rootName &&
          (prop === "scale" || prop === "position" || prop === "quaternion")) {
        why.rootTransform++;
        return;
      }
      if (prop === "scale") {
        if (isConstant(track)) { why.flatScale++; return; }
        kept.push(track);
        return;
      }
      if (prop === "position") {
        if (isConstant(track)) { why.restPosition++; return; }
        var v = Float32Array.from(track.values);
        var dx = node.position.x - v[0], dy = node.position.y - v[1], dz = node.position.z - v[2];
        for (var i = 0; i < v.length; i += 3) { v[i] += dx; v[i + 1] += dy; v[i + 2] += dz; }
        kept.push(new THREE.VectorKeyframeTrack(track.name, Array.from(track.times), Array.from(v),
                                                track.getInterpolation()));
        why.rebased++;
        return;
      }
      kept.push(track);
    });
    return { clip: new THREE.AnimationClip(clip.name, clip.duration, kept, clip.blendMode), why: why };
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
      startedAt: { type: "number", default: 0 },
      // The voice recorded against this clip, if any — 20 of Jane's 21 have one. Spatial, so it comes
      // from the figure rather than from the middle of your head.
      audio: { type: "string", default: "" }
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
      this._sound = null;
      this._media = null;
      this._audioUrl = "";
      this.apply();
    },

    update: function (old) {
      if (old && old.clip !== this.data.clip) this._teardown();
      this.apply();
      this._audio();
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
        // Teardown FIRST: it calls `figure.restore()`, so the skeleton is on its bind pose when the
        // rest positions are read out for re-basing. Retargeting against a half-animated model would
        // pin the clip to wherever the previous one left off.
        self._teardown();
        var bound = retarget(picked, live);
        self._mixer = new THREE.AnimationMixer(live);
        self._action = self._mixer.clipAction(bound.clip);
        self._action.loop = self.data.loop ? THREE.LoopRepeat : THREE.LoopOnce;
        self._action.clampWhenFinished = !self.data.loop;
        self._action.play();
        self._loaded = url + "|" + want;
        self._started = self.data.startedAt || now();
        self._audio();
        var w = bound.why;
        log("playing " + picked.name + " on " + (self.el.id || "?") + ": kept "
            + bound.clip.tracks.length + " of " + picked.tracks.length + " track(s) — dropped "
            + w.missing + " for missing nodes, " + w.rootTransform + " on the model root, "
            + w.flatScale + " flat scale, " + w.restPosition + " rest position; re-based "
            + w.rebased + " moving position track(s)");
        self.el.emit("figure-clip-started", {
          clip: url, name: picked.name, duration: picked.duration,
          tracks: bound.clip.tracks.length, dropped: w
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

    // The voice track, seeked to the same shared-clock offset as the skeleton. A media element is used
    // rather than a decoded buffer for exactly one reason: `currentTime` is writable, so a client that
    // joins mid-clip starts the voice where the body already is instead of from the top.
    //
    // Autoplay can be refused — a browser will not start audio without a gesture, and a page that has
    // not been touched yet has had none. That is a REFUSAL, not an error: the clip keeps playing, the
    // voice joins on the next gesture, and saying so is better than a silent catch. In an immersive
    // session the gesture that entered it already counts.
    _audio: function () {
      var url = this.data.playing ? this.data.audio : "";
      if (url !== this._audioUrl) this._silence();
      if (!url) return;
      var scene = this.el.sceneEl;
      var listener = scene.audioListener || new THREE.AudioListener();
      if (!scene.audioListener) {
        // A-Frame creates this lazily inside its own `sound` component, which this scene never uses —
        // so we create it and adopt its convention, or two components would each make one and the
        // second would be deaf.
        scene.audioListener = listener;
        if (scene.camera) scene.camera.add(listener);
        scene.addEventListener("camera-set-active", function (e) {
          e.detail.cameraEl.getObject3D("camera").add(listener);
        });
      }
      // A listener parented to nothing sits at the origin while the camera walks away, so a positional
      // sound is attenuated by a distance that has no relation to where you are standing.
      if (!listener.parent) log("audio listener is NOT attached to the camera — distance will be wrong");
      var media = new Audio();
      media.crossOrigin = "anonymous";
      media.loop = !!this.data.loop;
      media.src = url;
      var sound = new THREE.PositionalAudio(listener);
      sound.setMediaElementSource(media);
      this.el.object3D.add(sound);
      this._sound = sound;
      this._media = media;
      this._audioUrl = url;
      var offset = (now() - this._started) / 1000;
      var self = this;
      var ctx = listener.context;

      var seek = function () {
        if (media.duration && isFinite(media.duration)) {
          var t = (now() - self._started) / 1000;
          media.currentTime = self.data.loop ? ((t % media.duration) + media.duration) % media.duration
                                             : Math.min(Math.max(t, 0), media.duration);
        }
      };

      // **A resolved `play()` is not a sound.** An AudioContext created outside a user gesture starts
      // SUSPENDED, and `setMediaElementSource` routes the element through it — so the element plays,
      // the promise resolves, no error is thrown anywhere, and the output goes into a stopped graph.
      // That is silence that reports success, and it is what shipped: the director announced "her voice
      // is playing along with it" to a browser making no noise at all.
      var armed = false;
      var arm = function (why) {
        if (armed) return;
        armed = true;
        log("voice waiting for a gesture (" + why + ")");
        var once = function () {
          ["pointerdown", "keydown", "touchstart"].forEach(function (ev) {
            window.removeEventListener(ev, once, true);
          });
          armed = false;
          if (self._media !== media) return;          // the clip changed while we waited
          if (ctx && ctx.state === "suspended") ctx.resume().catch(function () {});
          seek();                                     // land where the BODY is, not where we left off
          media.play().catch(function () {});
        };
        // Capture phase: A-Frame's canvas and the look-controls swallow pointer events on the way down.
        ["pointerdown", "keydown", "touchstart"].forEach(function (ev) {
          window.addEventListener(ev, once, true);
        });
      };

      var begin = function () {
        seek();
        if (ctx && ctx.state === "suspended") ctx.resume().catch(function () {});
        media.play().then(function () {
          // Resolved is not enough — re-check the graph. This is the branch the original code had no
          // concept of, and the only one that actually fired.
          if (ctx && ctx.state !== "running") arm("context " + ctx.state);
          else log("voice playing (context " + (ctx ? ctx.state : "none") + ")");
        }).catch(function (err) {
          arm(String(err && err.name));
        });
      };

      if (media.readyState >= 1) begin();
      else media.addEventListener("loadedmetadata", begin, { once: true });
    },

    _silence: function () {
      if (this._sound) {
        if (this._sound.parent) this._sound.parent.remove(this._sound);
        this._sound = null;
      }
      if (this._media) { this._media.pause(); this._media.src = ""; this._media = null; }
      this._audioUrl = "";
    },

    // Unbind AND put the skeleton back. An AnimationMixer leaves every bone it touched wherever the last
    // frame left it, so simply stopping is how a figure ends up frozen mid-stride.
    _teardown: function () {
      this._silence();
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
  // Exported for `node --test` only; in the browser this file is a plain <script> and `module` is
  // undefined. The retargeting rules are arithmetic on keyframes and deserve tests that do not need
  // a headset — every one of them is a bug that was found by wearing one.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { retarget: retarget, isConstant: isConstant };
  }
})();
