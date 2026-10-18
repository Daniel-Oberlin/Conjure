// Conjure voice link — the headset's ears and mouth over one WebSocket (docs/specs/voice.md §11).
//
// Captures the microphone in an AudioWorklet, streams Int16 PCM to the world server's /audio relay
// (→ the voice server, one pipeline per headset), and plays the reply's PCM through a second worklet.
// Nothing here touches the render loop: both worklets run on the audio thread, and the main thread's
// share is a postMessage and a socket send per 20 ms frame.
//
// Three facts shape it:
//   • The permission prompt cannot render inside an immersive session and an AudioContext created outside
//     a user gesture starts suspended (figure-clip.js learned that one), so BOTH happen on the ENTER AR
//     tap, before requestSession — see `arm()`.
//   • Echo cancellation needs the reply to be played by THIS browser: Chromium's AEC3 uses the audio the
//     page renders as its reference. So playback is Web Audio in this page, not positional, and capture
//     asks for echoCancellation:true with noiseSuppression/autoGainControl OFF (the recognizer wants the
//     raw wideband signal — docs/backlogs/voice.md, "NR off, AEC on").
//   • Activation is the input layer's business: `talk` is an ACTION (CONJURE_BINDINGS), and
//     CONJURE_VOICE_ACTIVATION picks ptt (hold it — decision #5's shared-mode default) | open | off.
//     In ptt the worklet captures continuously (AEC stays converged) but frames are SENT only while the
//     action is held plus a short tail; the server's VAD then sees silence and closes the segment.
(function () {
  "use strict";
  var ACT = (window.CONJURE_VOICE_ACTIVATION || "ptt").toLowerCase();
  var TAIL_MS = 350;              // keep sending this long after `talk` is released, so the last word lands
  var FRAME_MS = 20;
  // Mirrors conjure-client.js's debugLog: console + POST /client_log → temp/conjure.log, gated the same way,
  // so a headset's voice link can be diagnosed from the host without remote debugging.
  var log = function (m) {
    console.log("[voice] " + m);
    if (!window.CONJURE_DEBUG_LOG) return;
    try { fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: "voice", msg: m }) }).catch(function () {}); } catch (e) {}
  };

  // Worklets are loaded from a Blob so this stays one file served with one ?v= (the Quest caches hard).
  var WORKLET_SRC = [
    "class Cap extends AudioWorkletProcessor {",
    "  constructor(){ super(); this.buf=[]; this.n=0; this.frame=" + FRAME_MS + "*sampleRate/1000|0; }",
    "  process(inputs){ var ch=inputs[0]&&inputs[0][0]; if(!ch) return true;",
    "    var out=new Int16Array(ch.length); for(var i=0;i<ch.length;i++){var v=ch[i]; v=v<-1?-1:v>1?1:v; out[i]=v<0?v*32768:v*32767;}",
    "    this.buf.push(out); this.n+=out.length;",
    "    if(this.n>=this.frame){ var all=new Int16Array(this.n), o=0; for(var b of this.buf){all.set(b,o); o+=b.length;}",
    "      this.port.postMessage(all.buffer,[all.buffer]); this.buf=[]; this.n=0; }",
    "    return true; } }",
    "registerProcessor('conjure-cap', Cap);",
    // Playback: a ring of Int16 frames at out_rate, upsampled to sampleRate by simple linear interpolation
    // (24k→48k on the Quest). ~120 ms is buffered before playing starts so network jitter does not gap it.
    "class Play extends AudioWorkletProcessor {",
    "  constructor(){ super(); this.q=[]; this.qn=0; this.rate=24000; this.pos=0; this.cur=null; this.ci=0; this.primed=false; this.under=0;",
    "    this.port.onmessage=(e)=>{ var d=e.data; if(d&&d.rate){this.rate=d.rate; return;} if(d==='flush'){this.q=[]; this.qn=0; this.cur=null; this.primed=false; return;}",
    "      var a=new Int16Array(d); this.q.push(a); this.qn+=a.length; }; }",
    "  process(_i, outputs){ var out=outputs[0][0]; if(!out) return true; var step=this.rate/sampleRate;",
    "    if(!this.primed){ if(this.qn<this.rate*0.12){ out.fill(0); return true;} this.primed=true; }",
    "    for(var i=0;i<out.length;i++){",
    "      while(this.cur&&this.ci>=this.cur.length-1){ this.cur=null; }",
    "      if(!this.cur){ if(this.q.length){ this.cur=this.q.shift(); this.qn-=this.cur.length; this.ci=0; this.pos=0; } else { out.fill(0,i); this.primed=false; this.under++; return true; } }",
    "      var a=this.cur[this.ci]/32768, b=this.cur[this.ci+1]/32768, f=this.pos-Math.floor(this.pos);",
    "      out[i]=a+(b-a)*f; this.pos+=step; this.ci=Math.floor(this.pos); }",
    "    return true; } }",
    "registerProcessor('conjure-play', Play);",
  ].join("\n");

  AFRAME.registerComponent("voice-link", {
    init: function () {
      if (ACT === "off") { log("activation off — no microphone"); return; }
      var self = this;
      this.ctx = null; this.ws = null; this.cap = null; this.play = null; this.stream = null;
      this.sending = false; this.tailUntil = 0; this.speaking = false; this.armed = false;
      this.stats = { sent: 0, recv: 0, since: performance.now() };
      // The ENTER AR tap is the one user gesture we get before the session: take the mic and resume the
      // context on it, in the capture phase so we run before scene.enterVR. Also on a plain click anywhere
      // (desktop testing).
      var arm = function () { self.arm(); };
      document.addEventListener("click", arm, true);
      this.el.sceneEl.addEventListener("exit-vr", function () { self.setSending(ACT === "open"); });
    },

    arm: function () {
      var self = this;
      if (this.armed) return;
      this.armed = true;
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { log("no Web Audio / getUserMedia here"); return; }
      this.ctx = new AC();
      var cons = { audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: false, channelCount: 1 } };
      navigator.mediaDevices.getUserMedia(cons).then(function (stream) {
        self.stream = stream;
        var t = stream.getAudioTracks()[0], st = t && t.getSettings ? t.getSettings() : {};
        log("mic granted: rate=" + self.ctx.sampleRate + " aec=" + st.echoCancellation + " ns=" + st.noiseSuppression + " agc=" + st.autoGainControl);
        var url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
        return self.ctx.audioWorklet.addModule(url).then(function () { self.wire(stream); });
      }).catch(function (e) { log("mic refused: " + e); self.armed = false; });
    },

    wire: function (stream) {
      var self = this, ctx = this.ctx;
      if (ctx.state !== "running") ctx.resume();
      var src = ctx.createMediaStreamSource(stream);
      this.cap = new AudioWorkletNode(ctx, "conjure-cap", { numberOfInputs: 1, numberOfOutputs: 0 });
      src.connect(this.cap);
      this.play = new AudioWorkletNode(ctx, "conjure-play", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      this.play.connect(ctx.destination);
      this.cap.port.onmessage = function (e) {
        var now = performance.now();
        var send = self.sending || now < self.tailUntil;
        if (send && self.ws && self.ws.readyState === 1) { self.ws.send(e.data); self.stats.sent++; }
      };
      this.setSending(ACT === "open");
      this.connect();
    },

    connect: function () {
      var self = this;
      var proto = location.protocol === "https:" ? "wss" : "ws";
      var u = new URLSearchParams(location.search).get("user") || "";
      var ws = new WebSocket(proto + "://" + location.host + "/audio" + (u ? "?user=" + encodeURIComponent(u) : ""));
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      ws.onopen = function () {
        ws.send(JSON.stringify({ type: "hello", user: u, in_rate: self.ctx.sampleRate }));
        log("audio link open (" + ACT + ")");
      };
      ws.onclose = function () { log("audio link closed — retrying in 2s"); setTimeout(function () { if (self.ws === ws) self.connect(); }, 2000); };
      ws.onmessage = function (ev) {
        if (ev.data instanceof ArrayBuffer) { self.stats.recv++; if (self.play) self.play.port.postMessage(ev.data, [ev.data]); return; }
        var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === "ready") { if (self.play) self.play.port.postMessage({ rate: msg.out_rate || 24000 }); log("ready out_rate=" + msg.out_rate); }
        else if (msg.type === "speaking") { self.speaking = !!msg.on; self.el.emit("voice-speaking", { on: self.speaking }); }
        else if (msg.type === "transcript") { log("heard: " + msg.text); self.el.emit("voice-transcript", { text: msg.text }); }
        else if (msg.type === "notice") { log(msg.text); }
      };
    },

    setSending: function (on) {
      if (on === this.sending) return;
      this.sending = on;
      if (!on) this.tailUntil = performance.now() + TAIL_MS;
    },

    // Push-to-talk reads the `talk` action through the input layer each frame — a controller's stick
    // press by default (CONJURE_BINDINGS), or whatever the user binds. Any pointer holding it talks.
    tick: function () {
      if (ACT !== "ptt" || !this.cap) return;
      var P = window.ConjurePointers, held = false;
      if (P && P.acting) {
        var ps = P.acting(this.el.sceneEl);
        for (var i = 0; i < ps.length; i++) { if (ps[i].active("talk")) { held = true; break; } }
      }
      if (held !== this.sending) {
        this.setSending(held);
        if (held && this.speaking && this.ws && this.ws.readyState === 1) {
          // Pressing to talk while the bot is speaking IS the barge-in gesture: drop what is queued here
          // and tell the server (C3b). Deliberate, not acoustic — no AEC quality required.
          this.play.port.postMessage("flush");
          this.ws.send(JSON.stringify({ type: "interrupt" }));
        }
      }
      // A line a minute in the log is the whole instrumentation: frames sent/received, so a silent
      // headset can be told apart from a deaf one without a debugger.
      var now = performance.now();
      if (now - this.stats.since > 60000) {
        log("link: sent " + this.stats.sent + " frames, received " + this.stats.recv + " in the last minute");
        this.stats = { sent: 0, recv: 0, since: now };
      }
    },

    remove: function () {
      if (this.ws) { var w = this.ws; this.ws = null; w.close(); }
      if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); });
      if (this.ctx) this.ctx.close();
    },
  });
})();
