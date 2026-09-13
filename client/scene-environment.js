/* global AFRAME, THREE */
// `scene-environment` — give the scene something for METAL to reflect.
//
// A metallic PBR material has no diffuse colour of its own. Its whole appearance IS the environment it
// reflects, so a `metallicFactor: 1` material in a scene whose `environment` is null renders BLACK — not
// dark, not flat, black — no matter how many lights are in it, because lights feed the diffuse and
// specular-highlight terms and a pure metal has neither. Ambient and directional lights do nothing here.
//
// That is what turned office-babe's glasses matte black: her `Metal` is metallicFactor 1, roughness 0.1,
// no texture. Her gold ring and the diamond setting are the same, and so is every chrome fitting in every
// model the library will ever import. It reads as a broken import and it is a missing environment.
//
// The map is GENERATED, not fetched: a two-stop vertical gradient (sky above, floor below) rendered into
// a small equirect and pre-filtered by `PMREMGenerator` into the roughness mip chain three wants. No
// asset to ship, no request to fail, and it is neutral enough not to tint anything — the alternative,
// three's `RoomEnvironment`, lives in examples/ and is not in A-Frame's bundled build.
//
// `scene.environment` feeds IBL ONLY. It is not the background — `a-sky` and the grounded skybox still
// own what you SEE, and in AR passthrough there is no background at all — so turning this on changes how
// metal and glossy surfaces are lit without putting anything new in front of the camera.
AFRAME.registerComponent('scene-environment', {
  schema: {
    enabled:   { type: 'boolean', default: true },
    intensity: { type: 'number', default: 1.0 },   // scene.environmentIntensity — dial reflections down
    sky:       { type: 'color', default: '#b4c4d8' },
    floor:     { type: 'color', default: '#3a3a3e' },
  },

  init: function () {
    this.generated = null;
    this.apply();
  },

  update: function (old) {
    // The gradient is baked, so any of the three inputs to it means a new one.
    if (old && (old.sky !== this.data.sky || old.floor !== this.data.floor
                || old.intensity !== this.data.intensity)) {
      this.dispose();
    }
    this.apply();
  },

  apply: function () {
    var scene = this.el.sceneEl.object3D;
    var renderer = this.el.sceneEl.renderer;
    if (!renderer) {                       // before the renderer exists there is nothing to pre-filter with
      this.el.sceneEl.addEventListener('renderstart', this.apply.bind(this), { once: true });
      return;
    }
    if (!this.data.enabled) {
      if (scene.environment === this.generated) { scene.environment = null; }
      this.dispose();
      return;
    }
    if (!this.generated) { this.generated = this.build(renderer); }
    scene.environment = this.generated;
    // `scene.environmentIntensity` is the obvious knob and it does not exist here: A-Frame 1.5.0 bundles
    // three r158 and it landed in r163. Setting it would be a control that silently does nothing, so
    // intensity is baked into the gradient instead (see `build`) and this only rides along if a later
    // three turns up underneath us.
    if ('environmentIntensity' in scene) { scene.environmentIntensity = 1; }
  },

  // A 16x32 equirect is plenty: PMREM blurs it into the mip chain anyway, and the point is a smooth
  // sky-to-floor falloff, not detail. Detail in a reflection would be WRONG here — we are inventing it.
  build: function (renderer) {
    var W = 32, H = 16;
    // Intensity scales the COLOURS, which is equivalent to scaling the reflection and works on every
    // three — see `apply` for why the property that would do it directly is not available.
    var k = Math.max(0, this.data.intensity);
    var sky = new THREE.Color(this.data.sky).multiplyScalar(k);
    var floor = new THREE.Color(this.data.floor).multiplyScalar(k);
    var data = new Float32Array(W * H * 4);
    var c = new THREE.Color();
    for (var y = 0; y < H; y++) {
      // Row 0 is the top of the equirect (straight up). Smoothstep so the horizon is a soft band rather
      // than a hard line, which a low-roughness metal would otherwise mirror as a visible seam.
      var t = y / (H - 1);
      var s = t * t * (3 - 2 * t);
      c.copy(sky).lerp(floor, s);
      for (var x = 0; x < W; x++) {
        var i = (y * W + x) * 4;
        data[i] = c.r; data[i + 1] = c.g; data[i + 2] = c.b; data[i + 3] = 1;
      }
    }
    var tex = new THREE.DataTexture(data, W, H, THREE.RGBAFormat, THREE.FloatType);
    tex.mapping = THREE.EquirectangularReflectionMapping;
    tex.needsUpdate = true;
    var pmrem = new THREE.PMREMGenerator(renderer);
    pmrem.compileEquirectangularShader();
    var target = pmrem.fromEquirectangular(tex);
    pmrem.dispose();
    tex.dispose();
    return target.texture;
  },

  dispose: function () {
    if (this.generated) { this.generated.dispose(); this.generated = null; }
  },

  remove: function () {
    var scene = this.el.sceneEl.object3D;
    if (scene.environment === this.generated) { scene.environment = null; }
    this.dispose();
  },
});
