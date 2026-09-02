/* global AFRAME */
// `figure` — pose a rigged humanoid through its SEMANTIC bone map (docs/backlogs/figures.md).
//
// The entity already carries `gltf-model`; this rides alongside it and rotates named bones. The map
// ({leftUpperArm: "upper_arm.L", …}) is resolved server-side and handed over in the component config, so
// a caller says "leftUpperArm" and this file never needs to know that Grace's rig calls it `upper_arm.L`,
// Saka's `J_Bip_L_UpperArm`, and a Daz figure `lShldrBend`.
//
// Both fields are JSON strings rather than A-Frame schema objects: the map is arbitrary key/value data
// whose keys differ per model, which A-Frame's flat schema types cannot express.
//
// Not a dynamic module — it has no independent existence, it decorates a placed model. It is ordinary
// world state, so a pose is shared, persisted and replayed on reload for free.
(function () {
  "use strict";
  if (!window.AFRAME) return;
  if (AFRAME.components.figure) return;

  function log(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[figure] " + msg); } catch (e) {}
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "figure", msg: msg }) }).catch(function () {});
    } catch (e) {}
  }

  // three sanitizes glTF node names when it builds the scene graph (PropertyBinding.sanitizeNodeName):
  // dots, spaces and colons become underscores. So a Daz/Rigify rig's `upper_arm.L` is `upper_arm_L` by
  // the time we look it up, and a DOA rig's `arm left shoulder` becomes `arm_left_shoulder`. The humanoid
  // map holds the names as the FILE spells them, so both spellings have to be tried.
  //
  // This failed silently and looked like success: the server resolved the bone fine, the client found
  // nothing, and nobody reported it. Saka worked only because VRM names (`J_Bip_L_UpperArm`) happen to
  // contain no punctuation.
  function norm(s) { return String(s).replace(/[\s.:]/g, "_"); }

  function parse(s) {
    if (!s) return null;
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  AFRAME.registerComponent("figure", {
    schema: {
      humanoid: { type: "string", default: "" },   // {semanticBone: nodeName}
      pose: { type: "string", default: "" }        // {semanticBone: [x, y, z]} euler DEGREES
    },

    init: function () {
      var self = this;
      this._bones = null;
      // gltf-model loads asynchronously, and a pose that arrives first would find no skeleton at all.
      // Re-apply on every load so a model swap does not silently drop the pose.
      this._onLoad = function () { self._bones = null; self.apply(); };
      this.el.addEventListener("model-loaded", this._onLoad);
      this.apply();
    },

    update: function () { this.apply(); },

    // Bone objects by name, collected once per loaded model. three names bones after the glTF nodes,
    // which is exactly what the humanoid map stores — that is why the map holds NAMES not indices.
    _collect: function () {
      if (this._bones) return this._bones;
      var obj = this.el.getObject3D("mesh");
      if (!obj) return null;
      var bones = {};
      var put = function (n) {
        if (!n || !n.name) return;
        bones[n.name] = n;
        var k = norm(n.name);
        if (!bones[k]) bones[k] = n;          // raw name wins; sanitized is the fallback spelling
      };
      obj.traverse(function (n) { if (n.isBone) put(n); });
      // A skeleton can also be reached through a SkinnedMesh whose bones are not in this subtree.
      obj.traverse(function (n) {
        if (n.isSkinnedMesh && n.skeleton) {
          n.skeleton.bones.forEach(put);
        }
      });
      if (!Object.keys(bones).length) return null;
      this._bones = bones;
      return bones;
    },

    apply: function () {
      var map = parse(this.data.humanoid) || {};
      var pose = parse(this.data.pose) || {};
      var bones = this._collect();
      if (!bones) return;                              // model not loaded yet; model-loaded retries

      // Reset anything previously posed but absent now, so clearing a pose really restores the figure
      // rather than leaving the last rotation stuck.
      var prev = this._applied || {};
      Object.keys(prev).forEach(function (bone) {
        if (pose[bone]) return;
        var b = bones[map[bone]] || bones[norm(map[bone])];
        if (b) b.rotation.set(0, 0, 0);
      });

      var applied = {}, missing = [];
      Object.keys(pose).forEach(function (bone) {
        var node = map[bone], b = node && (bones[node] || bones[norm(node)]);
        if (!b) { missing.push(bone); return; }
        var e = pose[bone];
        if (!e || e.length !== 3) return;
        var x = +e[0], y = +e[1], z = +e[2];
        // A non-finite angle blanks that branch of the scene graph and STAYS blanked — the same hazard
        // the server guards, guarded again here because a stale snapshot can carry one in.
        if (!isFinite(x) || !isFinite(y) || !isFinite(z)) return;
        b.rotation.set(x * Math.PI / 180, y * Math.PI / 180, z * Math.PI / 180);
        applied[bone] = true;
      });
      this._applied = applied;
      if (missing.length) {
        log("NO BONE for " + missing.join(", ") + " on " + (this.el.id || "?")
            + " — map has " + Object.keys(map).length + " entries, model has "
            + Object.keys(bones).length + " bones");
      }
      this._once = this._once || (log("posed " + Object.keys(applied).length + " bone(s) on "
                                     + (this.el.id || "?")) || true);
    },

    remove: function () {
      this.el.removeEventListener("model-loaded", this._onLoad);
      var map = parse(this.data.humanoid) || {}, bones = this._bones || {};
      Object.keys(this._applied || {}).forEach(function (bone) {
        var b = bones[map[bone]] || bones[norm(map[bone])];
        if (b) b.rotation.set(0, 0, 0);
      });
    }
  });
})();
