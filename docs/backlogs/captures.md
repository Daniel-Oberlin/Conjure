# Captures — backlog

Unfinished work, future directions and known problems for the capture pipeline
([`specs/captures.md`](../specs/captures.md)). What is built lives there; rejected alternatives and the
reasoning behind consequential forks live in [`decisions.md`](../decisions.md).

The active sequence for the composition work is
[`plans/figures-and-library.md`](../plans/figures-and-library.md) § Phase 2b while that plan is live.

---

## Known problems — verified against the code

### ~~A dangling asset id is invisible to every check we have~~ — BUILT 2026-09-14

`dangling(build)`, now in [`specs/captures.md`](../specs/captures.md) § 3. Reports 42 across twenty
captures, including the deck texture `194421251` that made the railing grey.

### ~~A mesh bound only by a TEMPLATE is not distinguished~~ — BUILT 2026-09-14

`dead_meshes(build)` and `Binding.source`, same section. 47 across twenty captures, which is where the
Japanese deck and Alice's white scalp both come from.

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

### Why a re-download will not fix the props library, and what would

The props textures are missing in **all fifteen copies, identically**: 43 textures, 2 cubemaps,
3 containers, including every face of two cubemaps (`px/nx/py/ny/pz/nz`, `sky_*`). Every one of the
fifteen props is `enabled: false` — a CATALOGUE the page never renders — so the page never requests
their textures and a downloader that mirrors network traffic can never see them. Same shape as the
audio: 276 of one shared scene's files absent because the page plays a handful of the banks it declares.

So the recommendation is a change of mechanism rather than more clicking: **enumerate `config.json` and
fetch every asset's `file.url` plus every `variants` url**, instead of recording what the page asked
for. Two things make that cheap — the registry is already the thing this pipeline reads, and a failure
per asset is then reportable, which is what separates "the server no longer has this" from "nobody
asked for it".

Suggestive but not conclusive: susan's fresh everything-selected capture is missing 14 real files from
the shared app shell that ebony's older capture HAS. Either the selection is not registry-complete or
the server stopped serving them; a per-asset 404 log would say which.

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
