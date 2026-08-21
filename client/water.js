/* global AFRAME, THREE */
// Water Picture — a tier-B interactive dynamic module (docs/dynamic-content-plan.md). An image seen
// THROUGH a rippling clear-water surface: a GPU wave-equation sim (ping-pong height field, reflecting
// boundaries) refracts the picture and adds specular glints; touch/drag it (fingertip OR controller) to
// make waves. Per docs, tier-B broadcasts the CAUSE (touch events) and each headset evolves its OWN sim
// — deliberately NOT synchronized (waves diverge across headsets; that's fine and cheaper).
//
// Sim: two half-float RG targets store (height, velocity). Each step, in a fragment shader:
//     accel = C² · laplacian(height);  vel = (vel + accel)·damping;  height += vel
//   Neighbor sampling is CLAMPed → Neumann (reflecting) walls, so ripples bounce off the frame. C is the
//   Courant number (c·dt/dx), clamped ≤ ~0.7 for 2-D CFL stability. Touches are stamped as gaussian
//   dimples in a second pass; releasing lets the restoring term launch outgoing rings.
// Display: sample the height field, ∇h → surface normal + UV refraction of the image, plus a specular
//   highlight. GPGPU passes run offscreen during tick() with xr.enabled toggled off (the standard trick
//   so three renders to our targets, not the XR framebuffer), before A-Frame's scene render.

(function () {
  "use strict";
  if (!window.AFRAME) return;
  var T = AFRAME.THREE;
  var MAX_SPLATS = 24;

  // Mirror status to console + server log (temp/conjure.log) like the other modules — headset console is
  // invisible, so a crash/freeze has to reach the terminal.
  function wlog(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[water] " + msg); } catch (e) {}
    try { fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: "water", msg: msg }) }).catch(function () {}); } catch (e) {}
  }

  var VERT = "varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }";

  // Wave step: (height, velocity) in RG. Clamped neighbor fetch = reflecting boundary.
  var SIM_FRAG = [
    "precision highp float; varying vec2 vUv;",
    "uniform sampler2D state; uniform vec2 texel; uniform float c2; uniform float damping;",
    "float H(vec2 uv){ return texture2D(state, clamp(uv, vec2(0.0), vec2(1.0))).r; }",
    "void main(){",
    "  vec2 s = texture2D(state, vUv).rg;",
    "  float lap = H(vUv+vec2(texel.x,0.0)) + H(vUv-vec2(texel.x,0.0))",
    "            + H(vUv+vec2(0.0,texel.y)) + H(vUv-vec2(0.0,texel.y)) - 4.0*s.r;",
    "  float v = (s.g + c2*lap) * damping;",
    "  float h = s.r + v;",
    "  gl_FragColor = vec4(h, v, 0.0, 1.0);",
    "}"
  ].join("\n");

  // Add gaussian dimples at up to MAX_SPLATS points this frame (u, v, signed strength).
  var SPLAT_FRAG = [
    "precision highp float; varying vec2 vUv;",
    "uniform sampler2D state; uniform vec3 splats[" + MAX_SPLATS + "]; uniform int count;",
    "uniform float radius; uniform float aspect;",   // aspect = width/height, to keep the brush round
    "void main(){",
    "  vec2 s = texture2D(state, vUv).rg;",
    "  float add = 0.0;",
    "  for (int i = 0; i < " + MAX_SPLATS + "; i++){",
    "    if (i >= count) break;",
    "    vec2 d = (vUv - splats[i].xy) * vec2(aspect, 1.0);",
    "    add += splats[i].z * exp(-dot(d,d) / (radius*radius));",
    "  }",
    "  gl_FragColor = vec4(s.r + add, s.g, 0.0, 1.0);",
    "}"
  ].join("\n");

  // Show the image refracted through the surface + a specular glint.
  var DISPLAY_FRAG = [
    "precision highp float; varying vec2 vUv;",
    "uniform sampler2D state; uniform sampler2D image; uniform vec2 texel;",
    "uniform float refraction; uniform float glint; uniform vec3 tint; uniform vec3 lightDir; uniform float opacity;",
    "void main(){",
    "  float hx = texture2D(state, vUv+vec2(texel.x,0.0)).r - texture2D(state, vUv-vec2(texel.x,0.0)).r;",
    "  float hy = texture2D(state, vUv+vec2(0.0,texel.y)).r - texture2D(state, vUv-vec2(0.0,texel.y)).r;",
    "  vec2 grad = 0.5 * vec2(hx, hy);",
    "  vec3 n = normalize(vec3(-grad * 40.0, 1.0));",
    "  vec3 col = texture2D(image, clamp(vUv + grad * refraction, 0.0, 1.0)).rgb * tint;",
    "  float spec = pow(max(dot(n, normalize(lightDir)), 0.0), 60.0) * glint;",
    "  gl_FragColor = vec4(col + spec, opacity);",
    "}"
  ].join("\n");

  AFRAME.registerComponent("water", {
    schema: {
      src: { type: "string", default: "" },          // image URL (a "water picture")
      width: { default: 1.2 }, height: { default: 0.9 },   // plane size (m)
      resolution: { type: "int", default: 256 },     // sim grid
      waveSpeed: { default: 0.5 },                    // Courant number (clamped ≤ 0.7)
      damping: { default: 0.996 },                    // energy loss/step (→ 1.0 = long-lived ripples)
      brushRadius: { default: 0.05 },                 // finger contact size (uv)
      brushStrength: { default: 0.06 },               // dimple depth per contact
      touchDepth: { default: 0.05 },                  // how near the fingertip must be to count (m)
      refraction: { default: 0.03 },                  // gradient→UV distortion
      glint: { default: 0.5 }, opacity: { default: 1.0 },
      tint: { type: "color", default: "#eaf4ff" }
    },

    init: function () {
      this._renderer = this.el.sceneEl.renderer;
      this._lastUV = {};                              // per input source, for drag rasterization
      this._remoteLast = {};                          // …and per remote source
      this._pending = [];                             // splats queued for the next frame
      this._buildSim();
      this._buildPlane();
      // Subscribe to peers' touches (shared-scope bus events) — each headset stamps them into its OWN sim.
      var self = this;
      this._onRemote = function (msg) {
        var p = msg && msg.payload; if (!p || p.id !== self.id) return;
        var key = "r:" + (p.src || "?");
        self._segment(self._remoteLast[key], { x: p.u, y: p.v }, p.strength);
        self._remoteLast[key] = (p.up ? null : { x: p.u, y: p.v });
      };
      if (window.ConjureBus) window.ConjureBus.on("water.touch", this._onRemote);
      wlog("init id=" + this.id + " res=" + this.data.resolution + " src=" + (this.data.src ? "yes" : "none"));
    },

    _buildSim: function () {
      var d = this.data, n = Math.max(32, d.resolution);
      var opts = { type: T.HalfFloatType, format: T.RGBAFormat, minFilter: T.NearestFilter,
                   magFilter: T.NearestFilter, depthBuffer: false, stencilBuffer: false };
      this._rt = [new T.WebGLRenderTarget(n, n, opts), new T.WebGLRenderTarget(n, n, opts)];
      this._cur = 0;
      this._texel = new T.Vector2(1 / n, 1 / n);
      this._simScene = new T.Scene();
      this._simCam = new T.OrthographicCamera(-1, 1, 1, -1, 0, 1);
      var c = Math.min(0.7, Math.max(0.0, d.waveSpeed));
      this._simMat = new T.ShaderMaterial({ vertexShader: VERT, fragmentShader: SIM_FRAG, uniforms: {
        state: { value: null }, texel: { value: this._texel }, c2: { value: c * c }, damping: { value: d.damping } } });
      this._splatMat = new T.ShaderMaterial({ vertexShader: VERT, fragmentShader: SPLAT_FRAG, uniforms: {
        state: { value: null }, splats: { value: [] }, count: { value: 0 },
        radius: { value: d.brushRadius }, aspect: { value: d.width / d.height } } });
      this._quad = new T.Mesh(new T.PlaneGeometry(2, 2), this._simMat);
      this._simScene.add(this._quad);
    },

    _buildPlane: function () {
      var d = this.data;
      var tex = d.src ? new T.TextureLoader().load(d.src) : null;
      if (tex) { tex.colorSpace = T.SRGBColorSpace; tex.wrapS = tex.wrapT = T.ClampToEdgeWrapping; }
      var col = new T.Color(d.tint);
      this._dispMat = new T.ShaderMaterial({ vertexShader: VERT, fragmentShader: DISPLAY_FRAG,
        transparent: d.opacity < 1.0, side: T.DoubleSide, uniforms: {
          state: { value: this._rt[this._cur].texture }, image: { value: tex }, texel: { value: this._texel },
          refraction: { value: d.refraction }, glint: { value: d.glint }, opacity: { value: d.opacity },
          tint: { value: new T.Vector3(col.r, col.g, col.b) }, lightDir: { value: new T.Vector3(0.3, 0.6, 1.0) } } });
      this._mesh = new T.Mesh(new T.PlaneGeometry(d.width, d.height), this._dispMat);
      this.el.setObject3D("water", this._mesh);
    },

    // Run one offscreen GPGPU pass (mat reads _rt[_cur], writes the other), then flip. xr.enabled is
    // toggled off so three renders to our target instead of the XR framebuffer.
    _pass: function (mat) {
      var r = this._renderer, xr = r.xr, was = xr.enabled, prev = r.getRenderTarget();
      mat.uniforms.state.value = this._rt[this._cur].texture;
      this._quad.material = mat;
      xr.enabled = false;
      r.setRenderTarget(this._rt[1 - this._cur]);
      r.render(this._simScene, this._simCam);
      r.setRenderTarget(prev);
      xr.enabled = was;
      this._cur = 1 - this._cur;
    },

    // Add splats along the segment last→cur (rasterized so fast drags leave no gaps).
    _segment: function (last, cur, strength) {
      var st = (strength == null ? -this.data.brushStrength : strength);
      if (!last || !isFinite(cur.x) || !isFinite(cur.y)) { if (isFinite(cur.x)) this._pending.push([cur.x, cur.y, st]); return; }
      var dx = cur.x - last.x, dy = cur.y - last.y, span = this.data.brushRadius * 0.5;
      var steps = Math.min(64, Math.max(1, Math.ceil(Math.hypot(dx, dy) / (span > 1e-4 ? span : 1e-4))));  // clamp: never runaway
      for (var i = 1; i <= steps; i++) this._pending.push([last.x + dx * i / steps, last.y + dy * i / steps, st]);
    },

    // World point → plane UV. If `proximity`, require |local z| < touchDepth (fingertip in the water).
    _toUV: function (p, proximity) {
      var lp = this._mesh.worldToLocal(new T.Vector3(p.x, p.y, p.z));
      if (proximity && Math.abs(lp.z) > this.data.touchDepth) return null;
      var u = lp.x / this.data.width + 0.5, v = lp.y / this.data.height + 0.5;
      return (u >= 0 && u <= 1 && v >= 0 && v <= 1) ? { x: u, y: v } : null;
    },

    // Controller ray (origin+orientation) → plane UV where it crosses z=0 in local space.
    _rayUV: function (tf) {
      var o = this._mesh.worldToLocal(new T.Vector3(tf.position.x, tf.position.y, tf.position.z));
      var q = new T.Quaternion(tf.orientation.x, tf.orientation.y, tf.orientation.z, tf.orientation.w);
      var dirW = new T.Vector3(0, 0, -1).applyQuaternion(q).add(new T.Vector3(tf.position.x, tf.position.y, tf.position.z));
      var d = this._mesh.worldToLocal(dirW).sub(o);
      if (Math.abs(d.z) < 1e-6) return null;
      var t = -o.z / d.z; if (t < 0) return null;
      var u = (o.x + d.x * t) / this.data.width + 0.5, vv = (o.y + d.y * t) / this.data.height + 0.5;
      return (u >= 0 && u <= 1 && vv >= 0 && vv <= 1) ? { x: u, y: vv } : null;
    },

    // Both input paths: fingertip proximity (hand tracking) OR controller ray + trigger.
    _scanInputs: function (frame, refSpace, session) {
      var sources = session.inputSources || [];
      for (var i = 0; i < sources.length; i++) {
        var src = sources[i], key = (src.handedness || i) + (src.hand ? ":hand" : ":ctrl"), uv = null;
        if (src.hand) {
          var tip = src.hand.get && src.hand.get("index-finger-tip");
          var jp = tip && frame.getJointPose && frame.getJointPose(tip, refSpace);
          if (jp) uv = this._toUV(jp.transform.position, true);
        } else if (src.targetRaySpace && src.gamepad && src.gamepad.buttons[0] && src.gamepad.buttons[0].pressed) {
          if (!this._trigLogged) { this._trigLogged = true; wlog("controller trigger seen (" + key + ")"); }
          var pp = frame.getPose(src.targetRaySpace, refSpace);
          if (pp) uv = this._rayUV(pp.transform);
        }
        if (uv) {
          if (!this._touchLogged) { this._touchLogged = true; wlog("touch " + key + " uv=" + uv.x.toFixed(2) + "," + uv.y.toFixed(2)); }
          this._segment(this._lastUV[key], uv);                 // local: stamp immediately
          if (window.ConjureBus)                                // shared: peers stamp into their own sims
            window.ConjureBus.emitShared("water.touch", { id: this.id, src: key, u: uv.x, v: uv.y, strength: -this.data.brushStrength });
          this._lastUV[key] = uv;
        } else if (this._lastUV[key]) {
          if (window.ConjureBus) window.ConjureBus.emitShared("water.touch", { id: this.id, src: key, u: this._lastUV[key].x, v: this._lastUV[key].y, up: true });
          this._lastUV[key] = null;                             // lifted → end the drag stroke
        }
      }
    },

    tick: function () {
      if (this._dead) return;                                   // a prior crash disabled us — don't spam
      try {
        var sc = this.el.sceneEl, xr = sc.renderer && sc.renderer.xr;
        var frame = sc.frame, refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
        var session = xr && xr.getSession && xr.getSession();
        if (frame && refSpace && session) this._scanInputs(frame, refSpace, session);

        this._pass(this._simMat);                               // one wave step
        if (!this._simOk) { this._simOk = true; wlog("first sim step ok"); }
        if (this._pending.length) {                             // stamp this frame's touches
          var pts = this._pending.slice(0, MAX_SPLATS).map(function (p) { return new T.Vector3(p[0], p[1], p[2]); });
          this._splatMat.uniforms.splats.value = pts;
          this._splatMat.uniforms.count.value = pts.length;
          this._pass(this._splatMat);
          if (!this._splatOk) { this._splatOk = true; wlog("first splat pass ok (" + pts.length + " pts)"); }
          this._pending.length = 0;
        }
        this._dispMat.uniforms.state.value = this._rt[this._cur].texture;
      } catch (e) {
        this._dead = true;                                      // stop after one failure so we don't hang the loop
        wlog("ERROR (disabled): " + (e && (e.message || e)) + " @sim=" + !!this._simOk + " splat=" + !!this._splatOk);
      }
    },

    remove: function () {
      if (window.ConjureBus && this._onRemote) window.ConjureBus.off("water.touch", this._onRemote);
      this.el.removeObject3D("water");
      [this._simMat, this._splatMat, this._dispMat].forEach(function (m) { if (m) m.dispose(); });
      if (this._mesh) this._mesh.geometry.dispose();
      if (this._quad) this._quad.geometry.dispose();
      if (this._rt) this._rt.forEach(function (rt) { rt.dispose(); });
      var img = this._dispMat && this._dispMat.uniforms.image.value; if (img) img.dispose();
    }
  });
})();
