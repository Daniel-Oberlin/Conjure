/* global AFRAME */
// Controller pointer beams — a laser from each hand's controller so you can SEE what you're aiming at when
// you interact (e.g. rippling a Water Picture, and future pointer-driven modules). Purely local & visual:
// no world state, no cross-client sync — every headset shows its OWN controllers.
//
// Shown on demand, not always: a beam ARMS when its trigger is pulled past a threshold and LINGERS for a
// window after the MOST RECENT pull (continuous pulls keep re-arming it), so a momentary release
// mid-interaction doesn't flicker it off. Both the linger duration and the threshold come from settings
// (window.CONJURE_BEAM_MS / CONJURE_BEAM_TRIGGER, injected by the server from config) — the duration is
// never hard-coded here; config.py is the single source of truth.
//
// Poses come straight from the XR frame (targetRaySpace in the reference space = the A-Frame world frame,
// since the rig sits at the origin) — the same source the water module reads — so beams need no controller
// entities in the scene.

(function () {
  "use strict";
  if (!window.AFRAME) return;
  if (AFRAME.components["controller-beams"]) return;

  var LENGTH = 5.0;     // beam length (m): a laser pointer extending forward from the controller
  var RADIUS = 0.004;   // beam thickness (m)
  var COLOR = 0x66ccff; // a cool cyan that reads over passthrough

  AFRAME.registerComponent("controller-beams", {
    init: function () {
      var THREE = AFRAME.THREE;
      this._beams = {};                       // handedness/key → { mesh, activeUntil }
      this._group = new THREE.Group();        // world-space container (scene root is the reference frame)
      this._group.name = "controller-beams";
      this.el.sceneEl.object3D.add(this._group);
    },

    // A beam mesh: a thin cylinder along local -Z (forward) with a small tip dot, both hidden until armed.
    _makeBeam: function () {
      var THREE = AFRAME.THREE;
      var geo = new THREE.CylinderGeometry(RADIUS, RADIUS, LENGTH, 8, 1, true);
      geo.rotateX(-Math.PI / 2);              // cylinder's Y axis → Z
      geo.translate(0, 0, -LENGTH / 2);       // span local z ∈ [-LENGTH, 0]; forward is -Z
      var mat = new THREE.MeshBasicMaterial({ color: COLOR, transparent: true, opacity: 0.5, depthWrite: false });
      var mesh = new THREE.Mesh(geo, mat);
      mesh.frustumCulled = false;
      mesh.visible = false;
      var tip = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 2.5, 8, 8), mat);
      tip.position.set(0, 0, -LENGTH);        // a dot at the far end of the ray
      mesh.add(tip);
      this._group.add(mesh);
      return mesh;
    },

    tick: function () {
      try {
        var CP = window.ConjurePointers;
        var pointers = CP ? CP.controllers(this.el.sceneEl) : [];
        if (!pointers.length) { this._hideAll(); return; }

        var now = (window.performance && performance.now) ? performance.now() : Date.now();
        var thresh = +window.CONJURE_BEAM_TRIGGER; if (!(thresh >= 0)) thresh = 0.05;
        var lingerMs = +window.CONJURE_BEAM_MS; if (!(lingerMs >= 0)) lingerMs = 0;   // from settings only

        var live = {};
        for (var i = 0; i < pointers.length; i++) {
          var p = pointers[i], key = p.key;
          live[key] = true;
          var b = this._beams[key] || (this._beams[key] = { mesh: this._makeBeam(), activeUntil: 0 });
          // Arm on a light pull of the SELECT action, or on ANY bound action being engaged — so the beam
          // also stays lit while you're grabbing or resizing, instead of vanishing mid-gesture (it used to
          // watch the trigger alone). Presentation follows intent without knowing which action it is.
          if (p.value("select") >= thresh || p.anyActive()) b.activeUntil = now + lingerMs;

          var on = now < b.activeUntil;
          if (on) {
            b.mesh.position.copy(p.origin);
            b.mesh.quaternion.copy(p.quat);
          }
          b.mesh.visible = on;
        }
        for (var k in this._beams) if (!live[k]) this._beams[k].mesh.visible = false;   // vanished controller
      } catch (e) { /* never break the render loop over a beam */ }
    },

    _hideAll: function () { for (var k in this._beams) this._beams[k].mesh.visible = false; },

    remove: function () {
      for (var k in this._beams) {
        var m = this._beams[k].mesh;
        this._group.remove(m);
        if (m.geometry) m.geometry.dispose();
        if (m.material) m.material.dispose();
      }
      if (this._group.parent) this._group.parent.remove(this._group);
      this._beams = {};
    }
  });
})();
