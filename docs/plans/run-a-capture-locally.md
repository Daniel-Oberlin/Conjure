# Plan — run a captured build locally

**Status:** **IT RUNS.** `scripts/capture_serve.py` serves a capture as a working app — measured on
`barbie`: the engine starts, WebGL initialises, the app's own Tailwind is injected, and the server logs
**zero misses**, meaning every file the running build asked for was already on disk. Not yet looked at
in a real browser (headless SwiftShader renders it far too slowly to screenshot); the URL is the test.
**Opened:** 2026-09-16 · **First boot:** 2026-09-16

**This file is temporary.** It holds one sequence across `specs/captures.md` and the grabber's own repo
while that sequence is being executed, and is deleted when the last part has settled — finished work to
[`specs/captures.md`](../specs/captures.md), deferred work to
[`backlogs/captures.md`](../backlogs/captures.md), forks taken to [`decisions.md`](../decisions.md).

**Dissolves to:** `specs/captures.md` §2 (what the grabber must get) · `backlogs/captures.md` (whatever
is not done) · the grabber's own repo, `browser-extension-glb-download`, for the extension changes.

---

## 1. The question this answers

*"Does our downloader capture everything we need to build the apps we are capturing from, so we can run
them locally?"*

**Nearly.** Content coverage is excellent and is not the problem. The app SHELL is, and the gap is
one file.

The grabber is registry-driven: a build's `config.json` is an exact manifest, so the extension offers
every declared texture, model, audio file and config whether or not the page requested it. That is why
`capture_audit.py` can answer "is this capture complete" exactly for assets. **The shell is not in the
registry**, so nothing offers it, nothing checks it, and nothing notices when it is absent.

---

## 2. What is actually on disk, measured

Across `temp/vrh`, twenty captures of one origin:

| file | captures having it |
|---|---|
| `config.json`, the scene JSON | 16–20 |
| `playcanvas-stable.min.js` (the engine) | 14 |
| `__settings__.js` `__start__.js` `__loading__.js` `__modules__.js` `__game-scripts.js` | 14 |
| `styles.css`, `manifest.json` | 14 |
| ammo + basis `.wasm` / `.wasm.js`, under `files/assets/` | 14 |
| `cdnjs.cloudflare.com/…/axios.min.js`, `mobx.js`, `pocketbase.umd.js` | 14 |
| **`index.html`** | **0** |
| `fonts.gstatic.com` woff2 | 1 |

**Every script the shell references resolves to a captured file.** Nothing is left dangling except the
Cloudflare analytics beacon, which wants deleting anyway. The build is one HTML file short of complete.

### The `index.html` is on disk 143 times, misfiled

The site is an SPA: a 404 is answered `200` with the app shell. The grabber trusts the requested URL for
the filename, so those bodies were written as textures — `files/assets/204984325/1/CARROT 1k
diffuse.html`, 143 of them across the corpus, all byte-identical and all the real thing:

```html
<link rel="stylesheet" type="text/css" href="styles.css">
<title>vrp_master</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/axios/1.7.3/axios.min.js"></script>
<script src="files/assets/191982195/1/mobx.js"></script>
<script src="files/assets/196026717/1/pocketbase.umd.js"></script>
<script src="playcanvas-stable.min.js"></script>
<script src="__settings__.js"></script>
… __modules__.js, __start__.js, __loading__.js
```

**It is a stray, not a loss** — the real `.basis` and the decoded `.png` sit beside each one, so no asset
is missing. But the single file the whole exercise needs has been in every capture all along, under the
wrong name, in the wrong directory, and unnoticed because nothing looks.

### Six captures cannot boot at all

`akari`, `ebony`, `ebony2`, `jane`, `nancy`, `susan` have `config.json` and assets and **no shell** — no
engine, no `__*.js`. Assets-only. Whether they are re-capturable depends on whether those builds still
exist unchanged upstream, which is not known. This is the concrete cost of having no check: six of
twenty were broken in a way nobody could see.

---

## 3. What to change in the grabber

`browser-extension-glb-download`. In value order, and the third is the one that lasts.

1. **Offer the top-level document.** It is the page's own URL and it is not a registry entry, so no
   existing rule reaches it. Small, and it is the blocker.
2. **Stop skipping `css` and `font`.** The README calls them "page furniture", which is right for an
   asset capture and wrong for a runnable build. `styles.css` survived only because it was also
   observed as a network request; `fonts.gstatic.com` woff2 made it into one capture of twenty.
3. **Teach `capture_audit.py` a SHELL CHECK.** It verifies assets against `config.json` exactly and says
   nothing about whether a build can boot. A list of required shell files, reported the way `BUILD GONE`
   already is, would have caught all six broken captures and flagged the 143 strays. **This is the hour
   of work with lasting value** — it is the same rule the retargeting campaign kept re-learning: a stage
   whose failures it does not report cannot be trusted by the next one.

Not worth doing: saving response headers / MIME types. A static server infers type from extension, and
the only extensionless files are API responses, which need a stub anyway.

---

## 4a. What actually happened, 2026-09-16

`scripts/capture_serve.py <capture-dir>`, and the estimate below was roughly right except that the
backend turned out to be cheap. Four things it does, each of which was a real obstacle:

1. **Recovers the shell** from the strays, by CONTENT and not by name — a `.html` under `files/assets`
   is the app only if it loads `__start__.js` and the engine. Works on **13 of 20** captures.
2. **Localises the absolute URLs.** The shell pulls axios from cdnjs; the capture has that exact path on
   disk, so it is a rewrite rather than a fetch. This is what the word standalone is for: a build that
   still pulls axios off a CDN runs on someone else's uptime.
3. **Redirects the API instead of patching the bundle.** 30 lines injected ahead of the app, wrapping
   `fetch` and `XMLHttpRequest`, pointing `https://api.<portal>/…` at `/__api/…` on this server — which
   replays the responses the capture already holds. Far less invasive than editing a 4 MB minified file,
   and it is visible in one place. **This was the part the estimate said would take a day. It took an
   hour**, because `detectPortal()` only ever affects the ORIGIN, and an origin is one regex.
4. **Strips the analytics.** The Cloudflare beacon and Plausible, which are not part of the app and
   throw when blocked.

**The capture is never written to.** Everything generated lives in memory.

**What booting it taught us that no amount of reading would have:**

- `cdn.tailwindcss.com` appears in the console and is **not** an external fetch — the app carries
  Tailwind as a registry asset (`files/assets/233605928/1/tailwind.js.txt`) and injects it inline, so
  the warning is Tailwind's own banner. Read statically, it looks like a missing dependency.
- **Zero server misses.** The strongest evidence there is that a capture is complete: the running app
  asked for nothing the capture did not have.
- Headless Chrome needs `--use-angle=swiftshader --enable-unsafe-swiftshader`, and even then a frame
  takes minutes. **Headless is good for "does it boot", useless for "does it look right".**

## 4b. Running one — the estimate, as written before any of it was tried

**Half a day to first pixels. One to two days to something genuinely useful.** Untried, so treat the
second number as the one that can move.

1. **`index.html`** — ~10 min. Copy one of the 143 strays to the capture root, delete the beacon line.
2. **Static serve** — ~10 min. `ASSET_PREFIX` and `SCRIPT_PREFIX` are both `""` and `SCENE_PATH` is
   relative, so the tree serves as it sits.
3. **The backend — where the day goes.** The app is a PocketBase client:
   `new PocketBase(https://api.${detectPortal()})`, with ten `authStore` touches. `detectPortal()`
   matches `location.hostname` against a portal list and **falls through to `VRHOLES`**, so from
   localhost the build will cheerfully call the live `api.vrholes.com`. Two paths:
   - *Let it phone home.* Fastest, may simply work, and is not local in any meaningful sense — it
     authenticates a copied build against someone's live service. Fine for a one-off look, not for
     anything repeatable.
   - *Stub PocketBase.* The captures already hold the responses — `auth-refresh`, `get_account`,
     `collections/*/records`. A replay server plus a one-line hostname override. This is the version
     worth building if the answer matters twice.
4. **Unknown unknowns.** `__game-scripts.js` is forty scripts of someone else's application: paywall
   gates, feature flags keyed on the account record, analytics that throw when blocked. Not sizable
   until it boots, and the reason step 3's estimate has a range.

### The tunnel is only needed for the headset, and probably not then

- **Desktop:** no tunnel. `http://localhost` is already a secure context, so WebGL, wasm and WebXR work.
- **Quest 3:** a secure context is required, and `adb reverse tcp:8080 tcp:8080` supplies one more
  cheaply than Cloudflare does — it makes the server `localhost` ON the headset. Reach for
  `scripts/tunnel.sh` only to be reachable without a cable.
- **Either way it changes nothing about the backend.** A tunnel hostname matches no portal in
  `detectPortal()` either, so it falls through to `VRHOLES` exactly as localhost does.

---

## 5. Is it worth doing

Split the question, because the two halves have different answers.

**The shell check (§3.3) is worth doing on its own merits, now.** An hour, it makes a whole class of
silent capture failure visible, and its value does not depend on ever running a build.

**The local build is worth it only for a reason.** Two good ones and one bad one:

- *Good:* comparing our playback against theirs. Phase 5 has spent this entire campaign inferring what
  the source engine does from `assignAnimation(name, resource, "Base", animSpeed, true)` and from
  device reports. Being able to A/B a clip against the original would have shortened several of those
  loops, and the residual body swing is exactly the kind of question it would settle.
- *Good:* recovering the six shell-less captures, if their builds are gone upstream — but that needs
  checking before it counts as a reason.
- *Bad:* insurance against the site disappearing. The assets are already safe and imported; the app is
  not the valuable part.
