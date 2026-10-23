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
  // …and then SILENCE until this long after release. The recognizers end a sentence on silence they
  // HEAR — xAI after ~400 ms, our VAD after 0.6 s — and audio that simply stops is not silence: the
  // sentence was held until the next press, then arrived glued to it, or drew "One moment — I'm still
  // working on the last request" (measured 2026-10-20/22: a 350 ms tail held it; 700 ms+ never did).
  // Digital zeros, not the mic, so nothing said after letting go is ever sent.
  // TEMPORARY — a push-to-talk workaround: delete it with PTT (docs/backlogs/voice.md).
  var RELEASE_MS = 1000;
  var FRAME_MS = 20;
  var GAP_MS = 600;               // reply audio this recent still counts as "speaking" (the gap between sentences)
  var DISCARD_MS = 2000;          // after a barge-in release, a turn with no transcript stops discarding reply audio here
  // What starts an immersive session: our own ENTER AR (conjure-client.js marks it) and A-Frame's buttons.
  var ENTER_XR = "[data-xr-enter], .a-enter-vr-button, .a-enter-ar-button";
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
      // NEVER name a field after an A-Frame lifecycle method (init/update/play/pause/tick/remove): the
      // playback node was once `this.play`, which replaced Component.play with null, so the scene's
      // start-up threw on this component and its tick — push-to-talk — never ran (2026-10-20).
      this.ctx = null; this.ws = null; this.cap = null; this.speaker = null; this.stream = null;
      this.press = { frames: 0, peak: 0, at: 0 }; this.src = null; this._reacquiring = false;
      this.sending = false; this.tailUntil = 0; this.silenceUntil = 0; this.speaking = false; this.armed = false; this.lastAudio = 0; this.discard = false; this.discardUntil = 0; this.discarded = 0;
      this.stats = { sent: 0, recv: 0, since: performance.now() };
      // The ENTER AR tap is the one user gesture we get before the session: take the mic and resume the
      // context on it, in the capture phase so we run before scene.enterVR. Also on a plain click anywhere
      // (desktop testing).
      //
      // …but a FIRST-TIME permission prompt can't ride that tap: it opens behind the session the same tap
      // starts and waits there unseen, so the first entry on a new site had no voice until you exited,
      // answered and re-entered (2026-10-20/22; a cloudflared quick tunnel is a new site every run). So
      // while the permission is still `prompt`, the ENTER tap is HELD: the prompt shows on the 2D page,
      // and the next ENTER tap — permission now granted — goes straight in. Unknown state (no Permissions
      // API) or denied → the tap passes, as before.
      this.perm = "unknown";
      if (navigator.permissions && navigator.permissions.query) {
        navigator.permissions.query({ name: "microphone" }).then(function (st) {
          self.perm = st.state;
          log("microphone permission: " + st.state);
          st.onchange = function () { self.perm = st.state; log("microphone permission → " + st.state); };
        }).catch(function () {});
      }
      var onClick = function (ev) {
        var t = ev.target, enter = t && t.closest && t.closest(ENTER_XR);
        if (enter && self.perm === "prompt" && !self.stream) {
          ev.stopImmediatePropagation(); ev.preventDefault();
          log("held the ENTER tap: asking for the microphone first");
          self.hint("Allow the microphone, then tap " + ((enter.textContent || "ENTER").trim() || "ENTER") + " again.");
        }
        self.arm();
      };
      document.addEventListener("click", onClick, true);
      this.el.sceneEl.addEventListener("exit-vr", function () { self.setSending(ACT === "open"); });
      this.el.sceneEl.addEventListener("enter-vr", function () {
        var h = document.getElementById("voice-link-hint"); if (h) h.style.display = "none";
        if (!self.stream) return;
        var t = self.stream.getAudioTracks()[0];
        log("entered AR: context=" + (self.ctx && self.ctx.state) + " track=" + (t ? t.readyState + (t.muted ? " (muted)" : "") : "none"));
        if (self.ctx && self.ctx.state !== "running") self.ctx.resume();
        if (!t || t.readyState !== "live") self.reacquire("the track was not live on entering AR");
      });
    },

    // A short line on the 2D page, beside the ENTER button — the one place a person looks at that moment.
    hint: function (text) {
      var el = document.getElementById("voice-link-hint");
      if (!el) {
        el = document.createElement("div");
        el.id = "voice-link-hint";
        el.style.cssText = "position:fixed;bottom:76px;left:20px;z-index:99999;max-width:320px;padding:10px 14px;" +
          "font:14px sans-serif;color:#fff;background:rgba(20,30,50,0.92);border-radius:8px;";
        document.body.appendChild(el);
      }
      el.textContent = text;
      el.style.display = "";
      clearTimeout(this._hintTimer);
      this._hintTimer = setTimeout(function () { el.style.display = "none"; }, 8000);
    },

    arm: function () {
      var self = this;
      if (this.armed) return;
      this.armed = true;
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { log("no Web Audio / getUserMedia here"); return; }
      this.ctx = new AC();
      var cons = this.cons = { audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: false, channelCount: 1 } };
      // A request nobody answers neither resolves nor fails, so say so: on a site the headset has not
      // granted yet, the prompt opens behind the AR session this same tap starts, and waits there unseen.
      // (A cloudflared quick tunnel is a new site every run, so it asks every time.)
      log("asking for the microphone");
      var unanswered = setTimeout(function () {
        log("no answer to the microphone request after 5 s — if you're in AR its prompt is hidden; exit AR to answer it");
      }, 5000);
      navigator.mediaDevices.getUserMedia(cons).then(function (stream) {
        clearTimeout(unanswered);
        self.stream = stream;
        var t = stream.getAudioTracks()[0], st = t && t.getSettings ? t.getSettings() : {};
        var h = document.getElementById("voice-link-hint");
        if (h && h.style.display !== "none") self.hint("Microphone on — now tap ENTER AR.");
        log("mic granted: rate=" + self.ctx.sampleRate + " aec=" + st.echoCancellation + " ns=" + st.noiseSuppression + " agc=" + st.autoGainControl);
        var url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
        return self.ctx.audioWorklet.addModule(url).then(function () { self.wire(stream); });
      }).catch(function (e) { clearTimeout(unanswered); log("mic refused: " + e); self.armed = false; });
    },

    wire: function (stream) {
      var self = this, ctx = this.ctx;
      if (ctx.state !== "running") ctx.resume();
      this.cap = new AudioWorkletNode(ctx, "conjure-cap", { numberOfInputs: 1, numberOfOutputs: 0 });
      this.attach(stream);
      // A context the browser suspends stops the worklet outright; say so, and take it back.
      ctx.onstatechange = function () {
        log("audio context → " + ctx.state);
        if (ctx.state === "suspended" || ctx.state === "interrupted") ctx.resume();
      };
      this.speaker = new AudioWorkletNode(ctx, "conjure-play", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      this.speaker.connect(ctx.destination);
      this.cap.port.onmessage = function (e) {
        var now = performance.now();
        var send = self.sending || now < self.tailUntil;
        if (!send && now < self.silenceUntil && self.ws && self.ws.readyState === 1) {
          self.ws.send(new ArrayBuffer(e.data.byteLength)); self.stats.sent++;   // zeros: silence, heard
          return;
        }
        if (send && self.ws && self.ws.readyState === 1) {
          // The level of what is actually sent. A press that carries only digital zeros means the capture
          // went dead under us — seen after leaving and re-entering AR on the same page (2026-10-22): the
          // page kept sending, the server's VAD heard nothing, and no log could tell silence from speech.
          var a = new Int16Array(e.data), pk = 0;
          for (var i = 0; i < a.length; i++) { var v = a[i] < 0 ? -a[i] : a[i]; if (v > pk) pk = v; }
          if (pk > self.press.peak) self.press.peak = pk;
          self.press.frames++;
          self.ws.send(e.data); self.stats.sent++;
        }
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
        if (ev.data instanceof ArrayBuffer) {
          self.stats.recv++;
          if (self.discard) {
            if (performance.now() < self.discardUntil) { self.discarded++; return; }
            self.endDiscard("no transcript came");
          }
          self.lastAudio = performance.now(); if (self.speaker) self.speaker.port.postMessage(ev.data, [ev.data]); return;
        }
        var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === "ready") { if (self.speaker) self.speaker.port.postMessage({ rate: msg.out_rate || 24000 }); log("ready out_rate=" + msg.out_rate); }
        else if (msg.type === "speaking") { self.speaking = !!msg.on; self.el.emit("voice-speaking", { on: self.speaking }); }
        else if (msg.type === "transcript") { if (self.discard) self.endDiscard("the new turn was heard"); log("heard: " + msg.text); self.el.emit("voice-transcript", { text: msg.text }); }
        else if (msg.type === "notice") { log(msg.text); }
      };
    },

    // (Re)connect a mic stream to the capture worklet. Used once on arm and again whenever the stream dies.
    attach: function (stream) {
      var self = this;
      if (this.src) { try { this.src.disconnect(); } catch (e) {} }
      if (this.stream && this.stream !== stream) this.stream.getTracks().forEach(function (t) { t.stop(); });
      this.stream = stream;
      this.src = this.ctx.createMediaStreamSource(stream);
      this.src.connect(this.cap);
      var t = stream.getAudioTracks()[0];
      if (t) {
        t.onmute = function () { log("mic track muted by the browser"); };
        t.onunmute = function () { log("mic track unmuted"); };
        t.onended = function () {
          // Out of AR the Quest ends the track, and ends a re-acquired one again ~15 s later — a loop that
          // held the mic while nobody was talking (2026-10-23). Push-to-talk waits for AR or the next press.
          if (ACT === "open" || self.el.sceneEl.is("vr-mode")) { log("mic track ended"); self.reacquire("the track ended"); }
          else log("mic track ended — re-acquired on entering AR or the next press");
        };
      }
    },

    // A fresh getUserMedia — the permission is already granted, so no prompt and no gesture needed.
    reacquire: function (why) {
      var self = this;
      if (this._reacquiring || !this.cap) return;
      this._reacquiring = true;
      log("re-acquiring the microphone: " + why);
      navigator.mediaDevices.getUserMedia(this.cons).then(function (stream) {
        self._reacquiring = false;
        self.attach(stream);
        log("microphone re-acquired");
      }).catch(function (e) { self._reacquiring = false; log("microphone re-acquire failed: " + e); });
    },

    setSending: function (on) {
      if (on === this.sending) return;
      if (on) {
        this.press = { frames: 0, peak: 0, at: performance.now() };
        var tr = this.stream && this.stream.getAudioTracks()[0];
        if (tr && tr.readyState !== "live") this.reacquire("the track had ended before this press");
      }
      else if (this.press && this.press.frames) {
        var pk = this.press.peak, db = pk ? (20 * Math.log10(pk / 32768)).toFixed(0) + " dBFS" : "SILENT (digital zero)";
        log("press: " + this.press.frames + " frames over " + Math.round(performance.now() - this.press.at) + " ms, peak " + db);
        if (!pk) this.reacquire("a whole press carried nothing but zeros");
      }
      this.sending = on;
      if (!on) { var t = performance.now(); this.tailUntil = t + TAIL_MS; this.silenceUntil = t + RELEASE_MS; if (this.discard) this.discardUntil = t + DISCARD_MS; }
    },

    endDiscard: function (why) {
      if (this.discarded) log("barge-in: dropped " + this.discarded + " late frames of the interrupted reply (" + why + ")");
      this.discard = false; this.discarded = 0;
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
        // "Speaking" = the server says so, OR reply audio arrived moments ago: the server reports stopped
        // between two TTS sentences (~0.7 s), and a press landing in that gap must still interrupt.
        var talking = this.speaking || performance.now() - this.lastAudio < GAP_MS;
        if (held && talking && this.ws && this.ws.readyState === 1) {
          // Pressing to talk while the bot is speaking IS the barge-in gesture: drop what is queued here
          // and tell the server (C3b). Deliberate, not acoustic — no AEC quality required.
          this.speaker.port.postMessage("flush");
          // Reply audio already in flight still lands after this flush — too little to fill the 120 ms
          // priming buffer, so it waited there and played just before the NEW reply (2026-10-23). Drop
          // all reply audio until this press's transcript arrives, which precedes the new reply's audio.
          this.discard = true; this.discardUntil = Infinity; this.discarded = 0;
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
