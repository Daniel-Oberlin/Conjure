// Grounded skybox — a 360° panorama whose lower hemisphere is projected onto a flat ground plane so
// you stand ON the scene's ground instead of floating above a distant-at-infinity floor (the classic
// plain-<a-sky> problem). This is the OPT-IN companion to the regular skybox (env.sky.src); the plain
// sphere is left untouched. See docs / the env schema: env.sky.grounded selects this path.
//
// The GroundedSkybox class below is vendored verbatim (geometry + warp math) from three.js r161
// (examples/jsm/objects/GroundedSkybox.js — matches A-Frame 1.5.0's bundled three), adapted only to
// use A-Frame's THREE instead of an ES module import. Upstream is MIT-licensed.
(function () {
  var THREE = AFRAME.THREE;

  // height: how far the capturing camera was above the ground — larger magnifies the downward part of
  // the image. radius: the dome size; keep it large enough that the user's camera stays inside.
  function GroundedSkybox(map, height, radius, resolution) {
    resolution = resolution || 128;
    if (height <= 0 || radius <= 0 || resolution <= 0) {
      throw new Error("GroundedSkybox height, radius, and resolution must be positive.");
    }
    var geometry = new THREE.SphereGeometry(radius, 2 * resolution, resolution);
    geometry.scale(1, 1, -1);

    var pos = geometry.getAttribute("position");
    var tmp = new THREE.Vector3();
    for (var i = 0; i < pos.count; ++i) {
      tmp.fromBufferAttribute(pos, i);
      if (tmp.y < 0) {
        // Smooth out the transition from flat floor to sphere:
        var y1 = (-height * 3) / 2;
        var f = tmp.y < y1 ? -height / tmp.y : 1 - (tmp.y * tmp.y) / (3 * y1 * y1);
        tmp.multiplyScalar(f);
        tmp.toArray(pos.array, 3 * i);
      }
    }
    pos.needsUpdate = true;

    // The dome WRITES DEPTH, so content outside it is properly hidden behind it.
    //
    // This was `depthWrite: false` — a reasonable "it's a backdrop, it can never occlude" while the radius
    // was fixed at 30 m and everything was inside it anyway. Once `grab`'s skybox mode made radius a live
    // control you can shrink the dome down around you, and objects left outside kept drawing straight
    // through it (2026-09-01). A backdrop at infinity is a special case of correct depth, not a substitute
    // for it.
    //
    // polygonOffset rather than moving the ground: the projected ground lands at world y = 0, exactly where
    // floor-standing content rests, and coplanar surfaces z-fight. This biases the dome's fragments back in
    // depth only — no geometry change, and it covers any other near-coincident surface too (a real floor
    // under passthrough, say).
    var mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
      map: map, depthWrite: true, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    mesh.renderOrder = -1;     // still drawn FIRST — fills depth cheaply before content tests against it
    return mesh;
  }

  // <a-entity grounded-sky="src: ...; height: 1.6; radius: 30"> — builds the projected mesh and hangs
  // it as an object3D. Empty src tears it down (back to the plain <a-sky>). Texture loads are async,
  // so a token guards against an out-of-order load winning after src changed again.
  AFRAME.registerComponent("grounded-sky", {
    schema: {
      src: { type: "string", default: "" },
      height: { type: "number", default: 1.6 },     // ≈ eye height; the ground lands at y=0 (the floor)
      radius: { type: "number", default: 30 },       // metres; comfortably larger than a room
      resolution: { type: "number", default: 128 },
    },
    init: function () {
      this.mesh = null;
      this._token = 0;
      this.loader = new THREE.TextureLoader();
      this.loader.setCrossOrigin("anonymous");
    },
    update: function () {
      var d = this.data;
      if (!d.src) { this._teardown(); return; }
      var token = ++this._token;
      var self = this;
      this.loader.load(d.src, function (tex) {
        if (token !== self._token) { tex.dispose(); return; }   // superseded by a newer src
        tex.colorSpace = THREE.SRGBColorSpace;
        self._build(tex);
      }, undefined, function () {
        console.warn("[conjure] grounded-sky: failed to load", d.src);
      });
    },
    _build: function (tex) {
      this._teardown();
      var d = this.data;
      var sky = GroundedSkybox(tex, d.height, d.radius, d.resolution);
      sky.position.y = d.height;        // lift so the projected ground sits at the origin (floor)
      this.mesh = sky;
      this.el.setObject3D("grounded-sky", sky);
    },
    _teardown: function () {
      if (!this.mesh) return;
      this.el.removeObject3D("grounded-sky");
      if (this.mesh.material.map) this.mesh.material.map.dispose();
      this.mesh.material.dispose();
      this.mesh.geometry.dispose();
      this.mesh = null;
    },
    remove: function () { this._token++; this._teardown(); },
  });
})();
