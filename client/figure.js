/* global AFRAME, THREE */
// `figure` — pose a rigged humanoid in ANATOMICAL terms (docs/backlogs/figures.md).
//
// The entity already carries `gltf-model`; this rides alongside it and rotates named bones. Three JSON
// strings arrive from the server, and each answers a different question:
//
//   humanoid  WHICH node is this bone      {leftUpperArm: "upper_arm.fk.L"}
//   axes      WHICH WAY does it rotate     {leftUpperArm: {bend: [x,y,z], spread: […], turn: […]}}
//   pose      HOW FAR, in degrees          {leftUpperArm: {bend: 45}}
//
// So a caller says "bend her left elbow 45" and neither it nor this file needs to know that Grace's rig
// calls that bone `forearm.fk.L`, Saka's `J_Bip_L_LowerArm`, and that the two disagree by 137 degrees
// about which way its local X points. The axes are measured from the bind pose at import
// (conjure/figures.py) and are model-space-free: each is already expressed in the frame the bone's own
// local rotation lives in, so applying one is a single multiplication.
//
// **A pose composes ONTO the rest rotation, never replaces it.** Writing `bone.rotation.set(...)`
// discards whatever the rigger authored — measured at 177 degrees on Grace's `thigh.fk.L` — so the leg
// went upside-down before the requested angle was even added, and "clear" left it there. That is the
// other half of why posing looked so wrong on device.
//
// The fields are JSON strings rather than A-Frame schema objects: they are arbitrary key/value data
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
  // three's PropertyBinding.sanitizeNodeName: whitespace becomes "_", and the reserved characters
  // [ ] . : / are REMOVED — not replaced. Getting that backwards is why this looked fixed and was not:
  // `upper_arm.bend.L` becomes `upper_armbendL`, and a search for `upper_arm_bend_L` finds nothing.
  // Every candidate spelling is tried, since the exact rule has shifted between three releases.
  function variants(s) {
    var raw = String(s);
    return [raw,
            raw.replace(/\s/g, "_").replace(/[\[\]./:]/g, ""),   // three's actual rule
            raw.replace(/[\s.:]/g, "_"),                          // underscore-substitution variant
            raw.replace(/[\[\]./:\s]/g, "")];                    // strip everything reserved
  }

  function lookup(bones, name) {
    var v = variants(name);
    for (var i = 0; i < v.length; i++) if (bones[v[i]]) return bones[v[i]];
    return null;
  }

  function parse(s) {
    if (!s) return null;
    try { return JSON.parse(s); } catch (e) { return null; }
  }

  // Composition order is turn, then the swing — twist innermost, swings outermost, which is the
  // swing-twist decomposition every animation system uses and what keeps "turn 20" meaning the same
  // motion whatever swing accompanies it. Mirrored exactly by figures.resolve_pose on the Python side,
  // which is what scripts/pose_test.py renders, so a headset and a Blender render agree.
  var AXIS_ORDER = ["turn", "bend", "spread"];

  // Where each named direction points, as (out, up, forward) components of this bone's body frame.
  // `out` is side-aware — already resolved to the body's left or right when the frame was measured —
  // so the same word is symmetric on both sides and there is no sign for a caller to get wrong.
  var AIM = { up: [0, 1, 0], down: [0, -1, 0], forward: [0, 0, 1], back: [0, 0, -1],
              out: [1, 0, 0], "in": [-1, 0, 0] };

  function vec(a) { return a && a.length === 3 ? new THREE.Vector3(a[0], a[1], a[2]) : null; }

  // The absolute half: swing the bone from where it RESTS onto a direction in the body's own frame, so
  // the same request means the same thing on a T-posed rig and an A-posed one whose arms differ by 48
  // degrees. Returns null when the frame predates aiming (the server refuses those before we see them).
  function aimQuat(frame, aim) {
    var comps = typeof aim === "string" ? AIM[aim] : aim;
    var out = vec(frame.out), up = vec(frame.up), fwd = vec(frame.forward), rest = vec(frame.rest);
    if (!comps || comps.length !== 3 || !out || !up || !fwd || !rest) return null;
    var target = out.multiplyScalar(+comps[0])
      .addScaledVector(up, +comps[1]).addScaledVector(fwd, +comps[2]);
    if (!target.lengthSq() || !isFinite(target.lengthSq())) return null;
    target.normalize();
    rest.normalize();
    var d = Math.max(-1, Math.min(1, rest.dot(target)));
    if (d > 1 - 1e-9) return new THREE.Quaternion();
    if (d < -1 + 1e-9) {
      // A half-turn has no unique axis, and this case is not exotic: a hanging arm aimed `up` IS one.
      // three's setFromUnitVectors would pick an arbitrary perpendicular, which for an arm means
      // swinging it through the torso as often as not. Rotate in the FRONTAL plane instead — about the
      // body's forward — so the arm goes up through the side, the way a person raises one.
      var axis = fwd.clone().addScaledVector(rest, -fwd.dot(rest));
      if (axis.lengthSq() < 1e-12) axis.copy(up).addScaledVector(rest, -up.dot(rest));
      axis.normalize();
      return new THREE.Quaternion(axis.x, axis.y, axis.z, 0);
    }
    var c = new THREE.Vector3().crossVectors(rest, target);
    return new THREE.Quaternion(c.x, c.y, c.z, 1 + d).normalize();
  }

  function rotation(frame, request) {
    if (!frame || !request) return null;
    var q = null, tmp = new THREE.Quaternion(), axis = new THREE.Vector3();
    var aiming = request.aim !== undefined && request.aim !== null;
    AXIS_ORDER.forEach(function (name) {
      if (aiming && name !== "turn") return;          // an aim sets the swing; bend/spread do not
      var deg = +request[name], a = frame[name];
      if (!deg || !isFinite(deg) || !a || a.length !== 3) return;
      // A non-finite angle blanks that branch of the scene graph and STAYS blanked — the same hazard
      // the server guards, guarded again here because a stale snapshot can carry one in.
      axis.set(a[0], a[1], a[2]);
      if (!axis.lengthSq()) return;
      tmp.setFromAxisAngle(axis.normalize(), deg * Math.PI / 180);
      q = q ? q.premultiply(tmp) : tmp.clone();
    });
    if (aiming) {
      var swing = aimQuat(frame, request.aim);
      if (swing) q = q ? q.premultiply(swing) : swing;
    }
    return q;
  }

  AFRAME.registerComponent("figure", {
    schema: {
      humanoid: { type: "string", default: "" },   // {semanticBone: nodeName}
      axes: { type: "string", default: "" },       // {semanticBone: {bend|spread|turn: [x, y, z]}}
      pose: { type: "string", default: "" }        // {semanticBone: {bend|spread|turn: DEGREES}}
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
    //
    // Each bone's REST quaternion is captured here, on the first pass over a freshly loaded model,
    // because it is the only moment it is guaranteed untouched. Every pose is a delta onto it. Kept in
    // a Map of our own rather than on the bone: three deep-copies `userData` through JSON when it
    // clones an object, which would quietly turn a Quaternion into a plain bag of `_x`/`_y` fields.
    _collect: function () {
      if (this._bones) return this._bones;
      var obj = this.el.getObject3D("mesh");
      if (!obj) return null;
      var bones = {}, rest = this._rest = new Map();
      var put = function (n) {
        if (!n || !n.name) return;
        if (!rest.has(n)) rest.set(n, n.quaternion.clone());
        bones[n.name] = n;
        variants(n.name).forEach(function (k) {   // raw name wins; the rest are fallback spellings
          if (k && !bones[k]) bones[k] = n;
        });
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
      var axes = parse(this.data.axes) || {};
      var pose = parse(this.data.pose) || {};
      var bones = this._collect();
      if (!bones) return;                              // model not loaded yet; model-loaded retries

      // Reset anything previously posed but absent now, so clearing a pose really restores the figure
      // rather than leaving the last rotation stuck. Restoring means the REST quaternion, not identity.
      var prev = this._applied || {}, rest = this._rest;
      Object.keys(prev).forEach(function (bone) {
        if (pose[bone]) return;
        var b = lookup(bones, map[bone]);
        if (b && rest.has(b)) b.quaternion.copy(rest.get(b));
      });

      var applied = {}, missing = [];
      Object.keys(pose).forEach(function (bone) {
        var node = map[bone], b = node && lookup(bones, node), frame = axes[bone];
        if (!b || !frame || !rest.has(b)) { missing.push(bone); return; }
        // Rest first, then the delta — so a request that works out to no rotation at all lands the bone
        // back on its rest pose rather than leaving the last one stuck there.
        //
        // delta * rest: the axes are in the bone's PARENT frame, which is the frame its own local
        // quaternion lives in, so the delta pre-multiplies. Doing it the other way round would apply
        // the rotation in the bone's own twisted space and put us back where we started.
        var q = rotation(frame, pose[bone]);
        b.quaternion.copy(rest.get(b));
        if (!q) return;
        b.quaternion.premultiply(q);
        applied[bone] = true;
      });
      this._applied = applied;
      if (missing.length) {
        log("NO BONE OR AXES for " + missing.join(", ") + " on " + (this.el.id || "?")
            + " — map has " + Object.keys(map).length + " entries, axes " + Object.keys(axes).length
            + ", model has " + Object.keys(bones).length + " bones");
      }
      this._once = this._once || (log("posed " + Object.keys(applied).length + " bone(s) on "
                                     + (this.el.id || "?")) || true);
    },

    remove: function () {
      this.el.removeEventListener("model-loaded", this._onLoad);
      var map = parse(this.data.humanoid) || {}, bones = this._bones || {};
      var rest = this._rest || new Map();
      Object.keys(this._applied || {}).forEach(function (bone) {
        var b = lookup(bones, map[bone]);
        if (b && rest.has(b)) b.quaternion.copy(rest.get(b));
      });
    }
  });
})();
