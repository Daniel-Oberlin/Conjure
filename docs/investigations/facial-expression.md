# Facial expression: what can drive a face, and what only looks like it can

**2026-09-16.** The campaign that produced `conjure/expressions.py` (§8d) and the facial retarget
(§8e). The specs say what those do. This is the part that does not fit there: the frames that were
wrong, the evidence that turned out not to be evidence, and the conclusions I reached twice and had to
withdraw.

Read this before proposing a facial mapping of any kind. Four separate ideas here looked right,
measured wrong, and would each be proposed again by anyone starting from the names.

---

## Symptom

A figure who never blinks or changes expression, with no mechanism to make her. The starting belief,
from `backlogs/figures.md`, was that morph targets were the route and that the captures held no facial
performance to carry.

Both halves of that turned out to be half-true in a way that pointed the work in the wrong direction
for most of a day.

---

## Experiments and what each proved

| # | Experiment | Result | Conclusion |
|---|---|---|---|
| 1 | Count morph targets per rigged figure | 22 of 38 carry some | most figures have "morphs" |
| 2 | Classify those targets by name | **2 figures carry a face** | the rest are `Body_Alabaster`, `Shirt_Blue`, `Vagina_Open` — wardrobe, skin tone, anatomy |
| 3 | Compare the catalog's `morph_targets` count against distinct names | Saka **399 vs 57**, Alice **172 vs 34** | the count summed per primitive; "who can smile" was querying a meaningless number |
| 4 | Measure where each target displaces vertices | brow/eye/mouth bands **do not overlap** on either rig | a morph table is geometrically checkable, unlike a retarget |
| 5 | Measure which way each target displaces them | every signed name correct on both rigs | a second, independent check — catches an inverted shape the band test cannot |
| 6 | Count morph channels in 543 captured clips | 249 drive weights, **0 drive a facial target** | those channels are `Dress` and `Body`; a clip GLB has no meshes so its weights are anonymous |
| 7 | Measure facially-named **bone** rotation in the same clips | **519 of 543** rotate one | the facial performance is on bones, and it is nearly the whole corpus |
| 8 | Measure travel per bone in `1_idle` | **9,031°** on the face, eyelids the top two movers | ordinary body clips contain blinking; `1_idle_speaking` puts 74.9% into `DEF-jaw_master` |
| 9 | Check a clip's facial track names against a figure's bones | `1_idle` drives 189 bones on Barbie, **62 facial, all present** | a native play already carries the face — nothing needed building for 16 figures |
| 10 | Check which bones the retarget writes | 51, **none facial** | retargeting was the only thing discarding the face |
| 11 | Compare same-named facial bones' **world** rests, clip vs figure | ~175° apart, everywhere | see *Tried and rejected* #1 |
| 12 | Compare them **relative to the head** | 73° apart on rigs that agree | see *Tried and rejected* #2 |
| 13 | Compare their **local** rests | Akari 11°, Barbie 14°, office-babe 14°, **Eve 143°** | the frame a local delta actually depends on |
| 14 | Carry `1_idle` outward from the capture set | reaches office-babe and nothing else | see *Tried and rejected* #4 |
| 15 | Carry office-babe's clips **inward** | full face on **all 16** capture figures | the measurement that changed the verdict |

---

## Tried and rejected

### 1. Gate the facial carry on the bones' WORLD rest agreement

**Rejected — it refuses everything, including rigs that agree.**

A clip GLB and a figure GLB routinely disagree about the whole armature: `1_idle`'s head rests
**180°** from Barbie's. Measured in world, every facial bone then reads ~175° apart and every single
one is refused, on a pair whose faces agree to within 14°.

*What would change my mind:* nothing. A world rest says as much about which way the armature was
exported as about the face.

### 2. Gate it relative to the HEAD instead

**Rejected — it depends on both humanoid maps choosing the same vertebra, and they do not.**

office-babe's `head` is `DEF-spine.007`; Barbie's and the clip's are `DEF-spine.006`. That injects
**73°** of pure bookkeeping into a comparison of two faces that agree.

*What would change my mind:* a humanoid map that is verified to pick the anatomically same bone across
rigs. `validate()` does not check this, and `chain_breaks()` finds a related but different defect.

The answer was the **local** rest: a local delta is indifferent to every ancestor above the bone, so it
is the only frame neither the armature nor the bone map's choice can corrupt.

### 3. Carry a facial bone because both rigs NAME it the same

**Rejected — a name match is not evidence, and the counter-example is severe.**

Eve Maccaro's facial bones carry the **identical** Rigify names and rest inverted: median **142.6°**
against the clip, worst 179°. Carried by name alone, a blink swings her lid the wrong way while every
count — bones written, tracks kept, channels emitted — says it worked.

*What would change my mind:* nothing about names. This is why `FACE_REST_TOLERANCE` exists and why the
carry is gated per bone rather than per rig.

### 4. "The facial retarget is marginal — it reaches one figure"

**Rejected — I sampled the matrix along one axis and reported it as the matrix.**

I measured the capture set's clips carried *outward*, where they reach office-babe and nothing else,
and concluded the feature was barely worth keeping. The valuable direction is inward: office-babe's rig
has 22 clips of which 21 drive a face, and they carry onto **all sixteen** capture figures — jaw 6,
lips 6–8, eyelids 8–16, brows 16–18. One of them is `1_idle_speaking`, a recorded talking performance.

A compounding error: the first sweep picked its representative clip per rig by taking whichever came
first, which for the largest rig drives no face at all — so even the direction I did test read low.

*What would change my mind:* it already did. **A retarget is directional; measure both ways.**

### 5. Assert that a whole-face composite moves the mesh the way its name says

**Rejected — twice, in two different checks, for one reason.**

`Fcl_ALL_Joy` is the most facial thing on the VRM rig and has no band of its own, because it moves
brows, eyes and mouth together. The direction check then asserted it must *rise*, since joy is a smile.
Measured, it **falls** (-0.00376): `Fcl_EYE_Joy` is -0.00584 because happy eyes close and an eyelid
closes downward, and `Fcl_MTH_Joy` is -0.00246 because the mouth opens.

A shape that straddles every feature cannot be held to a claim about one. Both checks now exclude
composites, and that is the same fix in two places.

### 6. Build an idle-blink timer

**Rejected — blinking already works, and the timer would have hidden where it does not.**

519 of 543 clips rotate a facial bone and a native play keeps every track whose name resolves, so 16
figures have been blinking all along. The real gap was one place only: a *retargeted* clip arrived with
a dead face.

*What would change my mind:* a figure with a face and no clip that drives it. Saka and Alice are
exactly that — morph faces with no morph performance in the corpus — so a timer is still the answer
**for them**, and only them.

### 7. Map facial bones across naming families (Daz ↔ Rigify ↔ Character Creator)

**Not attempted, and deliberately.**

Daz spells an eyelid `eyelidUpper.L`, Character Creator `lEyelidUpper`, Rigify `DEF-lid.T.L`. Grace,
Trish, Yuffie, Alice, Blondie and Saka therefore receive nothing from a Rigify clip.

The reason to leave it alone is specific: **the check that makes §8e safe would refuse the mapping.**
Across two different rig builds the local rests disagree by construction, so a name map would have to
be justified by something else entirely.

*What would change my mind:* a geometric oracle that does not depend on rest agreement — most likely
the geometry of the mesh each bone moves, which is the measurement `expressions.check_regions` already
makes for morphs. Until that exists, a cross-family table is a table with nothing behind it.

---

## Remaining theories

| Theory | Likelihood | How to test |
|---|---|---|
| A retargeted face looks right on a headset | **likely** — the carried lid swings 26.38°, bit-identical to the source | put one on and watch; nothing here has been seen on device |
| The 30° tolerance is too generous at the top of its range | plausible | render a carried blink at 25–30° of rest disagreement and compare against the source |
| Mesh-displacement geometry could justify a cross-family bone map | **unknown, and the interesting one** | for a candidate pair, compare which vertices each bone moves, in head-relative coordinates — the bone-level analogue of `check_regions` |
| Saka and Alice could be driven from speech | plausible, unbuilt | the five visemes exist and the voice path exists; nothing connects them |

---

## Fixes shipped

| Symptom | Cause | Fix | Commit |
|---|---|---|---|
| No way to change a face | nothing drove morph weights | `conjure/expressions.py`, `figure-face`, `POST /figure/expression`, `set_expression` | `5f6b03c` |
| "Who can smile" unanswerable | `morph_targets` summed per primitive (Saka 399 for 57) | distinct names + `expression_scheme` + `facial_morphs`; `FRAME_REV` 18→19 | `5f6b03c` |
| Alice's face measured as empty; region check passed on no data | `_read_vec3` returned early on a sparse-only accessor — how morph targets are normally stored | read sparse; `check_regions` separates *not applicable* from *not measured* | `5f6b03c` |
| Expression invisible to the director | `inspect_figure` described bones and clothing, not the face | `figure_description` reports it | `4ad18f0` |
| No way to ask the library about faces | `dir` lists assets; `inspect_figure` needs a placed figure | `scripts/faces.py` | `4ad18f0` |
| A retargeted figure moves with a dead face | the humanoid map has 51 bones, none facial | carry facial bones by local delta, gated per bone | `d591966` |
| `glb_preview.py` printed nothing into a pipe | `_os._exit(0)` skips buffer flush; Python block-buffers a non-tty | flush first; added `--morph` and `--head` | `f4a809e` |

---

## Related

- [`specs/figures.md`](../specs/figures.md) §8d (driving a face), §8e (carrying one through a retarget)
- [`backlogs/figures.md`](../backlogs/figures.md) — what is still open
- `scripts/faces.py --check` — runs both geometric checks over the whole corpus
