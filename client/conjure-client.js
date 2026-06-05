// Conjure WebXR client (Phase 0).
// Connects to the world server's state channel, renders the snapshot, and applies
// patches live by mapping the declarative world model onto A-Frame entities/components.
// See docs/architecture.md §3 (channels), §4 (world model), §5 (patch protocol).
(function () {
  "use strict";

  var root = function () { return document.getElementById("world-root"); };

  function v3(a) { return Array.isArray(a) ? a.join(" ") : a; }

  function ensureEl(id) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("a-entity");
      el.setAttribute("id", id);
      root().appendChild(el);
    }
    return el;
  }

  // Inflate (or update) one entity: transform + components map straight onto A-Frame.
  function applyEntity(ent) {
    var el = ensureEl(ent.id);
    var t = ent.transform || {};
    if (t.position) el.setAttribute("position", v3(t.position));
    if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
    if (t.scale) el.setAttribute("scale", v3(t.scale));
    var comps = ent.components || {};
    Object.keys(comps).forEach(function (name) {
      el.setAttribute(name, comps[name]);
    });
  }

  function applyEnv(env) {
    env = env || {};
    var sky = document.getElementById("sky");
    if (sky) {
      if (env.sky && env.sky.src) {
        // 360 equirectangular image: set the full material so the texture isn't tinted and
        // renders on the inside of the sky sphere. (Setting the mapped <a-sky src> attribute
        // doesn't reliably update at runtime.)
        sky.setAttribute("material", { shader: "flat", side: "back", color: "#FFFFFF", src: env.sky.src });
      } else {
        var color = (env.sky && env.sky.color) || env.background;
        if (color) sky.setAttribute("material", { shader: "flat", side: "back", color: color, src: "" });
      }
    }
    if (env.fog) document.querySelector("a-scene").setAttribute("fog", env.fog);
  }

  function applySnapshot(world) {
    root().innerHTML = "";
    applyEnv(world.environment);
    (world.entities || []).forEach(applyEntity);
    console.log("[conjure] snapshot rev", world.rev, "(" + (world.entities || []).length + " entities)");
  }

  // Apply a single dotted-path set from an `update` op.
  //   "transform.position" -> position attribute
  //   "components.<comp>"            -> whole component value
  //   "components.<comp>.<property>" -> single property of that component
  function setPath(el, path, value) {
    var parts = path.split(".");
    if (parts[0] === "transform") {
      el.setAttribute(parts[1], v3(value));
    } else if (parts[0] === "components") {
      var comp = parts[1];
      if (parts.length === 2) el.setAttribute(comp, value);
      else el.setAttribute(comp, parts.slice(2).join("."), value);
    }
  }

  // {"fog.density": 0.1} -> {fog: {density: 0.1}} so applyEnv can consume it.
  function nest(flat) {
    var out = {};
    Object.keys(flat).forEach(function (k) {
      var ks = k.split("."), cur = out;
      for (var i = 0; i < ks.length - 1; i++) cur = cur[ks[i]] = cur[ks[i]] || {};
      cur[ks[ks.length - 1]] = flat[k];
    });
    return out;
  }

  function applyPatch(patch) {
    (patch.ops || []).forEach(function (op) {
      if (op.op === "add") {
        applyEntity(op.entity);
      } else if (op.op === "remove") {
        var el = document.getElementById(op.id);
        if (el && el.parentNode) el.parentNode.removeChild(el);
      } else if (op.op === "update") {
        var t = document.getElementById(op.id);
        if (t) Object.keys(op.set).forEach(function (p) { setPath(t, p, op.set[p]); });
      } else if (op.op === "env") {
        applyEnv(nest(op.set));
      }
    });
    console.log("[conjure] patch rev", patch.rev, "from", patch.origin);
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var ws = new WebSocket(proto + "://" + location.host + "/ws");
    ws.onopen = function () { console.log("[conjure] connected"); };
    ws.onclose = function () { console.log("[conjure] disconnected — retrying in 2s"); setTimeout(connect, 2000); };
    ws.onmessage = function (ev) {
      var msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") applySnapshot(msg.world);
      else if (msg.type === "patch") applyPatch(msg.patch);
    };
  }

  window.addEventListener("load", connect);
})();
