/* global AFRAME, THREE */
// Dynamic content modules (docs/specs/dynamics.md). A module is just an A-Frame COMPONENT: the
// world server delivers an entity carrying it (conjure-client applies arbitrary components via
// setAttribute), so a module is config-in-snapshot, shared across clients, and persisted — for free,
// on the existing entity/patch/snapshot path. No bespoke loader.
//
// TIER A (autonomous-procedural): state is f(sharedClock, seed, config), so every headset computes the
// SAME thing from the same (clock, seed, config) with ZERO per-frame sync — the cheapest way to honour
// "one shared reality, absolute". Determinism is deliberate: seed-driven params (no Math.random at
// runtime) + window.ConjureClock.now() as the only time input.
//
// First module: `fireflies` — a swarm of gently wandering glow points around the entity's origin.

(function () {
  "use strict";
  if (!window.AFRAME) return;

  // Shared time in SECONDS. Falls back to local time before the clock has synced (single-client still
  // animates; cross-client agreement kicks in once ConjureClock is ready).
  function clockSeconds() {
    var c = window.ConjureClock;
    return (c && c.now ? c.now() : Date.now()) / 1000;
  }

  // Small deterministic PRNG (mulberry32) so per-firefly params derive identically on every client from
  // the same integer seed — no Math.random anywhere in a module.
  function mulberry32(a) {
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  if (!AFRAME.components.fireflies) {
    AFRAME.registerComponent("fireflies", {
      schema: {
        count: { type: "int", default: 40 },
        color: { type: "color", default: "#ffe08a" },   // warm firefly glow
        radius: { type: "number", default: 1.2 },        // swarm half-extent (m) around the entity origin
        height: { type: "number", default: 1.0 },        // vertical centre of the swarm (m)
        drift: { type: "number", default: 0.35 },        // wander amplitude (m)
        speed: { type: "number", default: 1.0 },         // time multiplier
        size: { type: "number", default: 0.05 },         // point size (m, size-attenuated)
        seed: { type: "int", default: 1 }                // → identical swarm on every client
      },

      init: function () {
        this._build();
      },

      // Rebuild on any config change (count/seed/etc. alter the buffer or params). update() with the old
      // data just tears down and rebuilds — modules are cheap and this keeps state a pure function of data.
      update: function (oldData) {
        if (oldData && Object.keys(oldData).length) { this._dispose(); this._build(); }
      },

      _build: function () {
        var T = AFRAME.THREE, d = this.data, n = Math.max(0, d.count);
        var rand = mulberry32((d.seed | 0) * 2654435761 >>> 0);
        // Per-firefly static params (base point in the swarm volume + orbit freqs/phases/amps), all from
        // the seeded PRNG → identical across clients.
        var P = this._params = new Array(n);
        for (var i = 0; i < n; i++) {
          P[i] = {
            bx: (rand() * 2 - 1) * d.radius, by: d.height + (rand() * 2 - 1) * d.radius * 0.6, bz: (rand() * 2 - 1) * d.radius,
            fx: 0.1 + rand() * 0.4, fy: 0.1 + rand() * 0.4, fz: 0.1 + rand() * 0.4,   // Hz
            px: rand() * 6.283, py: rand() * 6.283, pz: rand() * 6.283,               // phase
            ax: 0.4 + rand() * 0.6, ay: 0.4 + rand() * 0.6, az: 0.4 + rand() * 0.6    // amp scale
          };
        }
        var geo = new T.BufferGeometry();
        geo.setAttribute("position", new T.BufferAttribute(new Float32Array(n * 3), 3));
        var mat = new T.PointsMaterial({
          color: new T.Color(d.color), size: d.size, sizeAttenuation: true,
          transparent: true, opacity: 0.9, depthWrite: false, blending: T.AdditiveBlending
        });
        this._points = new T.Points(geo, mat);
        this._points.frustumCulled = false;
        this.el.setObject3D("fireflies", this._points);
        this._t0 = clockSeconds();
      },

      tick: function () {
        var p = this._points, P = this._params, d = this.data;
        if (!p || !P) return;
        var pos = p.geometry.attributes.position, arr = pos.array;
        var t = (clockSeconds()) * d.speed, TWO_PI = 6.283185307;
        for (var i = 0, j = 0; i < P.length; i++, j += 3) {
          var f = P[i];
          arr[j]     = f.bx + d.drift * f.ax * Math.sin(TWO_PI * f.fx * t + f.px);
          arr[j + 1] = f.by + d.drift * f.ay * Math.sin(TWO_PI * f.fy * t + f.py);
          arr[j + 2] = f.bz + d.drift * f.az * Math.sin(TWO_PI * f.fz * t + f.pz);
        }
        pos.needsUpdate = true;
      },

      // Full teardown — the module lifecycle contract: release geometry, material, and the object3D so
      // unloading a module leaks nothing (critical on mobile-class Quest over a long session).
      _dispose: function () {
        if (this._points) {
          this.el.removeObject3D("fireflies");
          if (this._points.geometry) this._points.geometry.dispose();
          if (this._points.material) this._points.material.dispose();
          this._points = null; this._params = null;
        }
      },

      remove: function () { this._dispose(); }
    });
  }
})();
