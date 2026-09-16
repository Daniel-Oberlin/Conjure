# Captures — backlog

Unfinished work, future directions and known problems for the capture pipeline
([`specs/captures.md`](../specs/captures.md)). What is built lives there; rejected alternatives and the
reasoning behind consequential forks live in [`decisions.md`](../decisions.md).

The active sequence for the composition work is
[`specs/captures.md`](../specs/captures.md) §3, where the scene-conversion model settled.

---

## Known problems — verified against the code

### ~~A dangling asset id is invisible to every check we have~~ — BUILT 2026-09-14

`dangling(build)`, now in [`specs/captures.md`](../specs/captures.md) § 3. Reports 42 across twenty
captures, including the deck texture `194421251` that made the railing grey.

### ~~A mesh bound only by a TEMPLATE is not distinguished~~ — BUILT 2026-09-14

`dead_meshes(build)` and `Binding.source`, same section. 47 across twenty captures, which is where the
Japanese deck and Alice's white scalp both come from.

### Small, open, carried over when the figures plan dissolved

- **Whether the promo-audio skip rule generalises.** It is a name match against one origin's marketing
  lines. It will not survive a second site; a skip list keyed on content hash would. Worth doing only
  once there is a second site to test it against.
- **Template-only bindings on mapless materials** — 188 across the captures, Susan's white skull-cap
  among them. Whether a template may dress a mesh the running scene never instantiates is unresolved,
  and the test protecting Jane's hair depends on the current answer.

### A composed thing's rest pose is the SCENE's pose, not the container's bind

**Found 2026-09-16 while retargeting, and it cost nothing there — which is the surprising half.**

A thing's node tree is built from the scene entity tree, keeping each entity's rotation and scale, so a
composed figure's rest is *the scene's pose of the skeleton* rather than the container's bind pose.
Measured: Alice's composed thing and her clip's authoring rig recover the IDENTICAL bone map — same
names, `CC_Base_Hip`, `CC_Base_Waist`, … — and their rests differ on **all 22 bones, several by more
than 100°** (`rightUpperArm` 117°, `leftLowerArm` 113°). On Jane the two match to **0.075°**, which is
what the retargeting work generalised from, and the generalisation was the mistake.

`rebound` already exists to adopt the container's bone transform and did not fire for Alice
(`rebound: []`).

**Why it survives at all:** a rotation track REPLACES a node's rotation, so for any bone a clip drives,
the rest is irrelevant to playback. That is why Alice looks right playing natively. The rest matters
only for bones a clip does NOT drive, and for arithmetic built from nothing else.

**It is NOT the cause of the retargeting tilt**, which is worth knowing before opening this: that was a
whole-body alignment term, removed, and the composed-rest problem cost nothing measurable once it was
gone. The absolute-carry law never consults a rest except through `K`, which is convention and not pose.
So this is real, and it is not urgent.

**Anything proposed here has to explain why it is not the per-mesh IBM rewrite** that was tried and
rejected on device — see [`investigations/bride-eyelid.md`](../investigations/bride-eyelid.md).

### `adopt_unbound` is a patch, and the right model removes the need for it

It gives an unbound mesh the material of an identically-named mesh elsewhere
([`decisions.md`](../decisions.md) §28). It works, and it is the wrong shape: a mesh no entity renders is
DEAD, and a conversion that walked scene subtrees would never emit it, so there would be nothing to
adopt a material for. Keep it until the composition work lands, then delete it rather than extend it.

### Two builds are missing a declared scene file

`nancy` and `susan` share build `r5ibnnavi5zfoja`, which declares one scene and has none on disk. Its
six templates are VR-shell pieces, so the character content is presumably in a sibling build that does
have its scene. Not currently harmful; noted because any rule that reads scenes needs to say what it
does here.

---

## What the capture is missing, measured — and what the downloader should do differently

**Audited 2026-09-15** over the twenty captures, counting only assets the pipeline consumes
(container, texture, audio, animation, json, cubemap, template) that declare a `file.url`. Builds are
keyed by their asset-id set, so the same build under twenty capture roots counts once.

| build | copies | recoverable by MERGING copies | in no copy at all |
|---|---|---|---|
| `i6yg4l9h6n5u02e` — the scene akari/arabic/geeky share | 3 | 105 | 316 (276 audio) |
| `29rpyemwum5rgc3` — moon-girl's own scene | 1 | 0 | 325 (230 audio) |
| `r5ibnnavi5zfoja` — app shell (ebony, ebony2, nancy, susan) | 4 | 70 | 23 |
| `2049393.json` — the app's own scene | 16 | 66 | 17 |
| `1qrfsvzaf4wkg24` — the props library | 15 | 0 | **48** |
| `q0ocbff6aiicpki` — nancy's own | 1 | 0 | 12 (her 11 position configs) |
| `1dwq8nxgw51ms34` — bride's own | 1 | 0 | 3 (`underwear.glb` + 2 textures) |

**241 files are recoverable with no download at all**, by filling one copy of a shared build from
another. A capture is a mirror of a build, so two copies of the same build are interchangeable
file-for-file. Worth a small tool; nothing in the pipeline does it today.

**The character builds are in good shape.** Fifteen of twenty are complete; teacher is 2 textures short,
bride 3, ebony and ebony2 one container each, and nancy 12 — and susan reached **zero** after being
re-downloaded with everything selected (2026-09-15).

### Why files were missing, corrected 2026-09-15

**A first reading of this was wrong and is worth recording as such.** I concluded from "48 props files
missing in all fifteen copies" that the grabber mirrors network traffic and therefore could never see a
file the page does not request. It does not merely mirror traffic: it already reads `config.json` and
offers **every** asset the registry declares, as URL-only entries fetched at save time. That is how
susan's re-download reached seven `position_*_config.json` files the page never asked for.

So the gaps had two different causes, and only one was a bug:

- **Not ticked.** The props textures, the 43-file cubemap faces and most of the missing textures were
  *offered* and not selected — the older captures predate the everything-selected habit. A re-download
  with everything ticked reaches them, which is the opposite of what I first said.
- **Not offered.** `MANIFEST_SKIP` excluded `audio` from registry enumeration, so only the banks a page
  happened to play were ever available. That is the 276 and 289 missing audio files, and the four
  captures with no `voiced_by` edges at all. **Fixed in the grabber at 0.20.0**, along with `text`, and
  the per-tab entry ceiling raised from 2000 to 4000 because one capture holds several builds.

The registry is a complete manifest and worth knowing the shape of:

```json
"file": { "filename": "Agnes2_alpha.png", "size": 9403854,
          "hash": "df8224f2ec0f3aa0bd7cf646c86f64be",
          "url": "files/assets/218203895/1/Agnes2_alpha.png",
          "variants": { "basis": { "url": "…/Agnes2_alpha.basis", "size": 1323790, "hash": "…" } } }
```

In susan's build 109 of 221 assets declare a `file`; the other 112 are inline (materials, render
assets, the state graphs) and need no request. Two things still unused:

- **`hash` would verify a download** rather than trusting its size. Not free: it is MD5, and
  `crypto.subtle` does not implement MD5, so this needs a small hash routine in the extension.
- **`variants` is why a browsed capture holds the `.basis` and never the `.png`.** `parse()` already
  takes the uncompressed original's URL, so the `.png` is *offered*; whether it then 404s on the server
  or was simply never ticked is untested. The next re-download answers it, since a failure is now
  reported per asset with a reason.

**Some of it is genuinely gone.** Of the 23 always-missing app-shell files, 13 are marketing lines
("Bang me in any sex position in the full version", `VRHolescom.mp3`) and three are `.mp4`s — content
the importer already skips. Those are not worth a request.

## The site states what this pipeline infers — `main_config` and `position_N_config`

**Found 2026-09-15, chasing "Alice doesn't have any sounds with her animations, but you can hear her on
the web site".** Four of the twenty captures register JSON config assets, and they are not decoration:
they are the site's own declaration of most of what this pipeline currently recovers by convention.

`position_N_config.json` — one per authored position, ten or eleven per build:

```
idle:   animationName "pc_leanOnBed_idle.glb"   soundName       "HotelIdle4.mp3"    speed       0.5
action: animationName [ …action1, …action2, …action3 ]
        soundNameAction "HotelAction3.mp3"      soundNameRough  "HotelRough3.mp3"
        speedAction 0.6                          speedRough      1.2
boneLayers: Head        <- [pc_leanOnBed_headLeft, …headRight]  every 3–10 s, weight 1
            Eyelids     <- [pc_blink]                            every 5–8 s
            EyelidsAndBrows <- [pc_squint], [pc_eyes_closed]      every 5–10 s / 10–15 s
```

`main_config.json`: `mainModelName` (**which entity is the figure**), `dressObjects` (**which meshes are
clothing**), `boneLayers[].enabledBones` (the bone mask per layer), `sceneEnvironment`, `toolModeBones`
with morph targets, `materialSettings.reflectivity`.

**Verified exact, not plausible.** Against ebony's ten configs: **100 of 100 clip names resolve to an
animation asset and 30 of 30 sound names resolve to an audio asset.**

**It explains the audio gap precisely.** 262 of 772 clip rows carry a `voiced_by` edge. The captures
that link well (jane, barbie, bride, teacher… 20 of 21 each) are the SLOT-NAMED ones, where the stem
match works. The captures that link nothing — `susan` 0/8, `nancy` 0/62, `ebony` 0/140, `ebony2` 0/62 —
are exactly the four that ship configs. **The config is how the newer builds express what the older ones
expressed by filename**, and `capture_set.audio_role` only knows the filename convention.

What it would give, roughly in order of value:

- **`voiced_by` for the four capture that have none**, from a stated mapping rather than a name match.
  One audio file to several clips is already the relation's shape.
- **Playback speed.** 0.5, 0.6 and 1.2 are authored, and every clip currently plays at 1.0.
- **Layered idles** — blink, squint, head turns, with their own timing ranges and weights. This is the
  long-open *"Teacher's `Blink.glb` is not played at placement"*, and the answer is that it is not a clip
  you play, it is a state on an `Eyelids` bone layer that fires every 5–8 seconds.
- **`mainModelName` replaces a `things.json` guess** for those builds, and `dressObjects` replaces a
  `parts.py` classification with the author's own answer.

**Availability, measured:** `ebony` and `ebony2` have all 11 configs on disk. `nancy` is missing 11 and
`susan` all 7, so those two need re-fetching — the same download gap as bride's `underwear.glb` and the
51 props textures. The other sixteen captures register no configs at all and need none.

Not designed yet, and it should not be bolted onto `audio_role`: this is a second, richer SOURCE of the
same facts, and the interesting question is which wins when both speak.

## Run the captured app ourselves ✅ BUILT 2026-09-16 — `../playcanvas-unpack`

It runs. `python3 -m pcunpack build <capture> --out DIR` writes a standalone directory that serves under
any static file server with nothing reaching the internet, and `pcunpack check` says whether a capture
has what it needs. **Of the twenty captures to hand, thirteen rebuild and seven cannot.**

It lives in its own repo — a sibling of this one and of `browser-extension-glb-download` — because
turning someone else's published build into a runnable app is not Conjure's job and shares no code with
it. Its [`docs/capture-format.md`](../../../playcanvas-unpack/docs/capture-format.md) is the contract
the grabber has to satisfy, defined there because the consumer is the side that can check it.

**What the estimate below got wrong**, both worth carrying because they change what a capture must hold:

- **The capture root is only the PLAYER.** The engine, the bootstrap and the root `config.json` are a
  launcher; the scene — every model, texture and sound in it — lives under `release/<token>/` behind the
  API, reached by a chain the capture also records (scene record → `main_project` →
  `main_resource_rel` → `resource_project_release`). Anything that audits the root against the root's
  `config.json` will call a contentless capture complete.
- **Two obstacles nobody predicted, and neither is an asset.** The app takes its scene from
  `?scene_id=` and reports *"No scene found."* over a splash at 0% without one — so a perfect capture
  opened at `/` looks broken. And with no `auth_token` it runs `location.replace("https://" +
  detectPortal() + ".com")` and sends you to the live website; `detectPortal()` falls through to the
  live site, so no hostname you serve from avoids it.

The backend turned out to be **an hour, not a day**: `detectPortal()` only ever affects the ORIGIN, and
an origin is one regex, so a thirty-line shim ahead of the app redirects `fetch` and `XMLHttpRequest`
to a replay of the recorded responses. No bundle patching.

**Completeness for IMPORT and completeness for RUNNING are different questions, and this pipeline only
ever asked the first.** `capture_audit.py` checks assets against `config.json`, which is exact — and
seven captures could not be rebuilt while none reported a problem downloading. Six have `config.json`
and every texture and no engine at all. `pcunpack check` is the other half of the answer; calling it
from `capture_audit.py` is an open, cheap improvement.

### The original assessment, 2026-09-15 — kept because the reasoning still holds

**Raised 2026-09-15.** A published PlayCanvas app is a static site, and a capture holds essentially all
of one: `playcanvas-stable.min.js` (their engine), `__start__.js`, `__settings__.js`, `__modules__.js`,
`__game-scripts.js`, `config.json`, the `files/assets/` tree and the wasm (ammo, basis). Serve the
directory over HTTP and it should boot. No `index.html` is captured, but PlayCanvas's is boilerplate and
reconstructible from `__settings__.js`.

**The blocker is the backend, and it is smaller than it looks.** The app talks to a **PocketBase** API
(`pocketbase.umd.js`, `api/collections/scene/records`, `api/collections/pricing_plan`) for scene
selection and entitlement — the same thing answering 401 to a re-fetch. But **those responses were
captured**: `temp/vrh/susan/api/collections/pricing_plan/records` and
`api/collections/scene/records/<id>` are on disk, so replaying a handful of GETs as static JSON is most
of the work. Premium gating (`buyPremiumButton.js`, `vrholesBuyPremiumButton.js`) may need
short-circuiting.

**Why it is worth considering at all, which is not "to have the app".** Nearly every hard question in
the figure work has been *"does our composed version look like the site's?"* — Alice's white hair and
her eye sockets, bride's missing eyelid, the washed-out irises, arabic's textures. Today that comparison
is a person in a headset describing what they see, one round trip at a time, and the eyelid campaign is
still open after several. A local copy of the original turns it into a side-by-side, and it would have
settled the eyelid in one look.

**What it would NOT give.** Only what the capture holds: other characters' assets in a shared build are
401-gated, so those scenes render partially. And it proves nothing about our conversion being *right* —
it gives something to compare against, which is a different and lesser thing than a test.

Rough size: hours for one build to render, not days; the API stubbing is the uncertain part. Not started.
*(Correct on the size. Wrong that the API was the uncertain part — it was the cheapest of the five.)*

## Unsettled — asked for, not yet designed

### Interactive exclusion at capture import

`scripts/import_capture.py` takes everything a capture holds. A capture also holds the shared furniture
— controllers, a desk, a magnet, a button, the tools library — and there is no way to leave it out
short of deleting rows afterwards. Asked for as an **opt-in** mode that walks the items and takes
Enter to include, `n` to exclude, with a **separate flag per asset type** so the models can be
confirmed without being asked about clips.

**Unsettled, and the measurements are why.**

*It repeats.* Nine names account for nearly all the noise and each recurs in 15–20 of the twenty
captures, while **43 of 53 distinct models appear in exactly one**. So the real cost is not 208
decisions, it is about nine real ones and 199 repetitions — an interactive pass with no memory becomes
unusable around the third capture. That argues for persisted decisions, which is a file, a format and a
"forget my answers" escape hatch: more scope than the feature looks like.

*Per-type is the whole point, not a nicety.* Confirming everything across twenty captures is

    models 208 · animations 710 · audio 1,956 · TOTAL 2,874 prompts

Audio already has an automatic skip rule for the promo lines, and no case has come up for excluding a
clip. Models may be the only type that needs this at all.

*Name or content as the key?* Content-addressing is exact, but `VR_hand_L.glb` has two distinct hashes
across captures and `underwear.glb` has eight, so a hash-keyed skip re-asks on every variant.
Name-keyed matches the intent and could catch something wanted.

**And the prior question, unanswered:** whether exclusion is the right shape at all. Content-addressing
means the desk in twenty captures is ONE row, so a wrong include costs almost no storage. If the
problem is that props clutter a listing, `--kind` or a `prop` tag solves it without a decision per
item; if the problem is that they should not be catalogued, exclusion is right. Nobody has said which.

### ~~Re-importing a capture leaves the previous rows behind~~ — BUILT 2026-09-14

`library.supersede` + `import_capture.same_thing`, specified in
[`specs/library.md`](../specs/library.md) § 3. Relations move to the new row, the old one becomes a
tombstone reachable only by id, and identity is `(kind, label)` within one capture so a re-import
cannot retire another capture's props.

The open question in this entry — *whether the importer should retire automatically, since getting it
wrong deletes an asset somebody placed* — is answered by not deleting. A tombstone keeps the bytes, the
row and the id, so a wrong inference costs a column.

---

## Related

- [`specs/captures.md`](../specs/captures.md) — what the pipeline does today.
- [`backlogs/library.md`](./library.md) — the catalog's own backlog.
- [`backlogs/figures.md`](./figures.md) — what a figure needs once it has arrived.
