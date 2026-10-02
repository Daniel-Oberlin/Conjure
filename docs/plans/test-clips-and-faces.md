# Plan — test clips and faces on a headset

**Status:** all phases open · **Opened:** 2026-09-17

**This file is temporary.** It exists to hold one verification sequence across areas the specs keep
apart, and is deleted once every phase has settled. Each phase names where its result goes: a
confirmation to [`specs/figures.md`](../specs/figures.md), a defect to
[`backlogs/figures.md`](../backlogs/figures.md), a debugging campaign that produced negative knowledge
to [`investigations/`](../investigations/).

**Dissolves to:** `specs/figures.md` §8b/§8c/§8d/§8e (confirmations, and any measurement that changes
what those sections claim) · `backlogs/figures.md` (whatever fails) · `investigations/facial-expression.md`
(anything that needs a campaign rather than a fix).

---

## 0. Why this plan exists

Everything below was built and verified **numerically or in Blender**, and none of it has been seen on
a headset. That is the whole gap. Specifically:

| built | how it was verified | what is missing |
|---|---|---|
| `set_expression` (§8d) | two geometric checks over the corpus; Blender renders of 8 expressions × 2 rigs | never applied to a live figure in a world |
| facial clips playing natively | name-resolution and track counts — `1_idle` drives 62 facial bones, all present on Barbie | nobody has watched a captured figure blink |
| facial retarget (§8e) | carried lid swings 26.38°, bit-identical to the source; 46 unit tests | never rendered, let alone worn |
| clip playback at all | `clip_diff.mjs` through the client's own code | **`backlogs/figures.md` still says clip playback has not been run on a headset** (2026-09-13) |

The honest framing: this is not "confirm it works". Four separate things could be wrong, and the plan
is ordered so that a failure in an early phase explains the later ones rather than being confused with
them.

**A render is not a headset.** `scripts/glb_preview.py --morph --head` renders an expression through
Blender, and `clip_diff.mjs` plays a clip through the client's own `retarget()`. Both have been green
on things the headset later disagreed with — the residual-swing chase, the 97% stall, the bride's
eyelid. Treat every green below as "not yet contradicted".

---

## 1. Prerequisite — is the catalog current?

**Re-measured 2026-09-28 on `feat/room-refinement`: 98 live model rows, every one at `frame_rev` 21,
and the face attributes are present. This phase now passes on arrival** — run it as a re-check, not as
work.

| first pass (2026-09-17) | today |
|---|---|
| 38 rigged figures at `frame_rev` 18 | 38 rigged still, all at **21** — 19 carried the face work, 21 a hand's materials |
| NONE carry `morph_names` / `expression_scheme` / `facial_morphs` | Saka and Alice carry all three; nobody else should |
| a 23 h-old server reported `0 of 98 updated`, meaning "I cannot see the new attributes" | the server matches the code on disk, so `0 of 98` means "already current" |

**RESTART THE SERVER FIRST — when the tripwire tells you to.** A refresh runs *inside* the server
process, so it can only derive what that process's code knows, and a server started before the face
work reports `0 of 98 updated`, which reads as "the build needed no refresh". `--force` does not help
either; the old process has no idea the new attributes exist.

**The two `0 of N` readings are told apart by the tripwire, not by the count.** `ctl refresh-models`
prints this and only this when the running process is behind:

```
! this server's FRAME_REV does not report one; the code on disk is at 21.
  A refresh derives what the SERVER knows, so RESTART IT and run this again
```

Silence means the server reported the same revision as the code on disk, and `0 of N` is then the
healthy answer. Do not infer it from uptime — check for the line.

Run these three:

```
.venv/bin/python -m conjure.ctl refresh-models
.venv/bin/python scripts/faces.py
.venv/bin/python scripts/faces.py --check
```

**Use the venv interpreter, not bare `python3`** — the plan first said `python3` and that fails at
`import dotenv` before it reaches any of this. `source scripts/activate` works too.

| check | expect | observed |
|---|---|---|
| `refresh-models` | `0 of 98 model(s) updated.` and **no** `! this server's FRAME_REV` line | **✓** `0 of 98 model(s) updated.`, tripwire silent |
| `faces.py` — Saka | `57 / 57 / vrm` | **✓** `57 / 57 / vrm` |
| `faces.py` — Alice | `34 / 26 / cc`, and she is the only one offering `look_*` | **✓** `34 / 26 / cc`, all four `look_*`, hers alone |
| `faces.py` — everyone else | `facial 0`, bar three with a single stray shape (Goddess, Kawaii, Moon Girl) | **✓** `facial 0`; the three strays are exactly those, 1 each |
| `faces.py --check` | `OK` for Saka and Alice, `n/a` for those three | **✓** `OK` Alice + Saka, `n/a` ×3 |

Observed 2026-09-28, branch `feat/room-refinement`, server up since 16:16 that day against a
`conjure/figures.py` last touched 09-22. Every expectation met, nothing contradicted.
**§1 is settled** — it gates the rest and produces no spec confirmation of its own.

**`0 of 98`, not `0 of 193`, and that is correct.** Every read applies `AND superseded_by IS NULL` —
a re-import tombstones the row it replaces, and `_scope_sql` hides tombstones unconditionally, so they
are reachable by id and by nothing else. 95 of the 193 model rows are superseded copies sitting at
`frame_rev` 14/15; Alice's chain runs `cac1d7a6` (14) → `a41a1f78` (15) → `add5b6c3` (21, live). They
cannot reach `search_library`, so they cannot hand the director a face-less duplicate. **A filtered
count is not a partial one** — this was misread once already, as evidence of a trap that does not
exist.

**If `facial` is 0 for Saka:** stop. Nothing downstream can work, and the fault is in extraction or in
the refresh, not in the runtime.

## 2. Faces, on the two figures that have them

`set_expression` reaches morph targets, and only Saka and Alice carry any. This phase is the cheapest
and the most likely to work, which is why it is first.

Place Saka, then:

| call | expect | observed |
|---|---|---|
| `inspect_figure` | a **Face** line listing what she can do. If it is absent, `meta.morph_names` did not travel — place her again after the refresh | present for both |
| `set_expression(id, {"blink": 1})` | both eyes shut, and STAY shut | eyes stay shut on both models |
| `set_expression(id, {"neutral": 1})` | back to rest | saka worked, Alice did not: "ok":false,"error":"'ent_asset_151253' (cc scheme) cannot do ...|
| `set_expression(id, {"joy": 1})` | mouth open, eyes curved, brows up — visibly more than `smile` | both models looked joyful |
| `set_expression(id, {"smile": 0.4})` | a slight version of the same shape, not a different one | hard to tell the difference between smile and joy for Alice |
| `set_expression(id, {"surprised": 1, "aa": 1})` | one face, not the last one to arrive — `Fcl_MTH_A` and the surprise compose | This works correctly with Saka, but Alice doesn't respond to the aa viseme at all - maybe expected. **Not expected — resolved 09-30:** her jaw is bone-driven (`CC_Base_JawRoot`) and `Jaw_Open` is a lip corrective, so the morph alone moves almost nothing. The server side is clean: it resolved and stored `Jaw_Open: 1.0` (0.5+0.7, correctly clamped). Same root cause as the `look_*` findings below |
| `set_expression(id, {"look_left": 1})` | **refused in words.** VRM aims eyes with bones; a silent no-op here is a bug | error, correct |
| `set_expression(id, clear=true)` | rest | works for both |

Then **Alice** — the only figure in the catalog with eye-direction morphs, so this is the only place
`look_*` can be tested at all. Each one drives **both** eyes (`look_left` → `Eye_L_Look_L` +
`Eye_R_Look_L`), so the pair should travel together; one eye moving alone, or the two diverging, is a
finding.

```
A=ent_asset_151253          # her entity id — re-placed since? `.venv/bin/python -m conjure.ctl world`
E=localhost:8080/figure/expression
H='Content-Type: application/json'
expr() { curl -s $E -H "$H" -d "{\"id\":\"$A\",\"expression\":$1}"; }
rest() { curl -s $E -H "$H" -d "{\"id\":\"$A\",\"clear\":true}"; }
```

| call | expect | observed |
|---|---|---|
| `expr '{"look_left":1}'` | both eyes travel together — and **record which way she actually looked.** `Eye_L_Look_L` is the author's name for the target, not a measurement, so whether `left` means hers or the viewer's is the thing being established here | **lids only.** They steer, correctly; the EYEBALLS never move. **Convention pinned 09-30: `left` is HER left**, not the viewer's — so it agrees with the target's own name (`Eye_L_Look_L`) and with the side-aware bone words (`spread`, `out`/`in`). One vocabulary, one handedness |
| `expr '{"look_right":1}'` | the mirror of the above, same magnitude | **lids only.** They steer, correctly; the EYEBALLS never move |
| `expr '{"look_up":1}'` | both eyes up, lids not dragged along with them | **inverted:** the lids are ALL that moved. Eyeballs static |
| `expr '{"look_down":1}'` | both eyes down | **lids only.** They steer, correctly; the EYEBALLS never move |
| `expr '{"look_left":1,"look_up":1}'` | **one diagonal glance**, not the last word to arrive. Four separate targets, so nothing clamps — this isolates the merge from the composition case above | **merge is fine** — both landed together, no last-write-wins. Lids only, as above |
| `rest` | eyes forward | lids return to rest |

**Finding 2026-09-30 — on a Character Creator rig the `look_*` and jaw words drive CORRECTIVES, not
the motion.** Alice's skeleton carries `CC_Base_L_Eye`, `CC_Base_R_Eye` and `CC_Base_JawRoot`. Gaze and
jaw are BONE motions on this rig; `Eye_L_Look_L` and `Jaw_Open` are the lid and lip shapes that
accompany them. Driving the morph alone moves the eyelid and leaves the eyeball pointing straight
ahead — which is exactly what was observed, and why `aa` appears to do nothing.

One thing this phase DID settle, and it is a confirmation rather than a defect: **`look_left` means
the figure's own left.** That is the same handedness the bone vocabulary already uses, so a caller
never has to ask which way a side word points — it is always the figure's. Goes to `specs/figures.md`
§8d as a dated line.

The displacements agree, and would have predicted it: the look targets measure 0.0018–0.0129 against
`Eye_Blink` at 0.0143, i.e. lid-sized adjustments rather than a gaze.

So **§2's premise is wrong, not its result.** "Alice is the only figure with eye-direction morphs" is
true and misleading: she has the morphs, and they are not what aims her eyes. The plan already knew
`set_expression` must refuse `look_left` on Saka because *"VRM aims eyes with bones"* — it turns out CC
does too, so there is no figure in the corpus whose gaze `set_expression` can drive. Aiming eyes needs
the bone path, and `look_*` on a `cc` rig should either move those bones or be refused the way VRM's is.
Goes to `backlogs/figures.md`; it is a scheme-table claim, not a runtime bug.

`neutral` is **not** available on her — it is the empty entry in the `cc` table, and the endpoint
misreports that as a refusal (`cannot do .`, with the name missing). Use `rest` where Saka's table
says `neutral`; see `backlogs/figures.md`.

**And a reload.** The expression lives on the entity, so re-entering should restore it. This is the
half that a unit test cannot reach.

**What a failure means, by shape:**

- *Nothing visibly changes but the reply says it applied* → the weights reached the store and not the
  mesh. Look for `NO SUCH MORPH TARGET` or `NO MORPH TARGETS` in the client log; `figure-face` logs
  once per model and says which of those two it is.
- *The whole figure distorts* → a target is being driven that is not facial, i.e. `role_of` is
  mis-classifying. That is `investigations/facial-expression.md` territory, not a quick fix.
- *An expression looks wrong rather than absent* → the Character Creator emotion composites are the
  least-evidenced thing in the module (`expressions.PROVENANCE` says so). Record what it looks like;
  a composite is a table entry and cheap to change.

---

## 3. Clips, and whether the face comes with them

**This is the phase that has never been run at all**, per `backlogs/figures.md`.

Use a figure from the capture set — Barbie, Akari, Teacher, any of the 16 sharing rig signature
`81b51a075b`. Their clips are **native**: the same skeleton, bound by name, nothing retargeted.

```
list_clips(id)
play_clip(id, "1_idle")
```

| expect | because | observed |
|---|---|---|
| the body moves | this is §8b, and it is the thing that has never been watched | it moves nicely |
| **she blinks** | `1_idle` spends 9,031° on facial bones with `DEF-lid.T.L`/`.R` as its top two movers | she blinks |
| `play_clip(id, "1_idle_speaking")` moves her **jaw** | 74.9% of that clip's motion is in `DEF-jaw_master` — it is recorded lip sync | her jaws move, looks like she is talking, but there isn't any talking audio (other clips do have audio which workds) |
| `stop_clip(id)` returns her to her pose, not to the clip's last frame | `restore()` exists for exactly this | it does |

**The blink is the claim worth the trip.** If the body moves and the face does not, the tracks are
being dropped somewhere between the file and the mixer — and since nothing retargets here, that is a
client-side binding question with a short list of causes.

Also worth one measurement while you are there: `pc_squint` is a **no-op** by measurement (0.13° from
rest). If it visibly does something, the travel metric is lying and §8e's numbers need re-checking.

---

## 4. The facial retarget — the part most likely to be wrong

§8e carries facial bones across rigs by local delta, gated per bone. Verified numerically and **not
once rendered**.

The valuable direction is **inward**: office-babe's rig (`64e47bd573`) has 22 clips that drive a face,
and they carry onto all 16 capture figures. So place **Barbie** and play a clip belonging to
**office-babe's** rig.

**Two traps, both of which silently give you the wrong test:**

- **Never pass `--force`.** A rig mismatch with no flag is what TRIGGERS the retarget
  (`server.py`: `if mismatch and not req.force`). `--force` means "bind by name across the mismatch and
  look at the wreck" — it SKIPS the rewrite. On the Eve row below, `--force` would move her face and
  look exactly like the gate failing.
- **Use the clip's ASSET ID, not its label.** `1_idle` exists on several rigs, and a label resolves to
  the clip belonging to THIS figure — so `clip $B 1_idle` plays Barbie's own native clip and tests
  nothing.

```
B=$(curl -s localhost:8080/place_cached_asset -H 'Content-Type: application/json' \
     -d '{"id":"610c58c65e8f34ad.glb"}' | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E=$(curl -s localhost:8080/place_cached_asset -H 'Content-Type: application/json' \
     -d '{"id":"065f32e2ed799295.glb"}' | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')
clip() { .venv/bin/python -m conjure.ctl clip "$@"; }
```

office-babe's idle clips by id: `1_idle` **b44aa253d720320b.glb** (10.9 °/s), `1_idle_speaking`
**c99d7b70017ef6ef.glb**, `3_idle` **4c86d4511970e604.glb** (9.0), `7_idle` **baf44961a0c991c7.glb**
(9.4). A capture-set clip for the Eve row: Barbie's `1_idle` is **9159668bc9878553.glb**.

The first play of any (clip, rig) pair takes **~9 s** — that is the rewrite. It is content-addressed,
so every later play is a lookup, and the notes are stored with the result and replayed rather than
going quiet.

| expect | if not | observed |
|---|---|---|
| the body performs | if the body is wrong, stop — this is §8c, not the face | the body works the same with barbie as it does with office babe |
| eyelids and jaw move | 8–16 lids and 6 jaw bones are carried; the server's notes say how many | looks the same. **Measured 10-02: carried 78 facial bone(s), 2 REFUSED** — identical across `1_idle`, `1_idle_speaking` and `3_idle` (retarget_rev 8). The two refused are `DEF-lid.T.L/R.003`, resting **34° apart — only 4° past tolerance**, and tertiary lid bones whose primary counterparts carried. Far more than the 8–16 this row predicted |
| it does not look *wrong* — a lid closing sideways, a jaw sliding | this is the failure `FACE_REST_TOLERANCE` is supposed to prevent, and 30° may simply be too generous | it does not look wrong|
| **`clip $E 9159668bc9878553.glb` — the case that MUST fail.** Her body should retarget (she has a 21-bone inferred map); her face must be **refused in the notes**, ~63 of 69, median 143° | **If Eve's face MOVES, the gate is broken** — and that is worse than the face not moving, because every count will say it succeeded. Her facial bones carry the IDENTICAL Rigify names and rest inverted (worst 179°), which is the entire reason the gate exists: a name match is not evidence | her body moves but her face does not so good|

Read the server's notes either way; they are the point of the feature being honest:

```
carried 61 facial bone(s) — the face travels with the clip
63 facial bone(s) REFUSED: … rest them 143° apart …
```

**Decision to make here, and it is yours:** if the carried faces look poor at the top of the tolerance
range, the options are to lower `FACE_REST_TOLERANCE` (safer, carries less) or to drop §8e entirely.
It earns its keep on one measured direction; I said as much when it landed.

**Settled 10-02 — the tolerance stays at 30 and §8e keeps its keep.** Measured on the office-babe →
Barbie direction: **78 facial bones carried, 2 refused**, and the carried face was reported as looking
no different from the source performance. The gate behaved exactly as designed rather than merely
permissively: the only two it dropped sit at 34°, four degrees past the threshold, and they are the
TERTIARY lid bones (`DEF-lid.T.L.003`/`.R.003`) whose primary counterparts carried — so nothing a lid
needs was lost. Lowering the tolerance would buy nothing here and would start costing bones at 14°,
which is the median that carries.

Also recorded, since it is the same number on every clip: **129 channels drive bones the humanoid does
not name** — skirt, breast and secondary chains — and no rig has anything to receive them. That is
unrelated to the face and is the cost of the capture rig being richer than the humanoid map.

---

## 5. What is deliberately NOT in scope

Recorded so the plan is not read as a claim that these were tested:

- **No auto-blink.** A face holds what it was last set to, so `blink: 1` leaves her eyes shut. Nothing
  blinks on a timer; §8d says so.
- **No visemes from speech.** The five mouth shapes exist and the voice path exists; nothing connects
  them.
- **No cross-family facial retarget.** Grace, Trish, Yuffie, Alice, Blondie and Saka receive nothing
  from a Rigify clip — different naming families. `investigations/facial-expression.md` §7 says why a
  name map is not the obvious next step.
- **Morph-driven facial performance does not exist in the corpus.** 249 clips drive weights and none
  of them drive a facial target.

---

## 6. Where each result goes

| result | destination |
|---|---|
| a phase passes | a dated confirmation line in the matching `specs/figures.md` section |
| a phase fails cleanly (wrong output, clear cause) | `backlogs/figures.md`, with the measurement |
| a phase fails and resists diagnosis | a section in `investigations/facial-expression.md` — it already holds seven rejected approaches and is the right home for an eighth |
| a number here turns out wrong | fix it **here and in the spec**, and say which measurement replaced it |

Delete this file when phases 1–4 have all settled.
