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

### Re-importing a capture leaves the previous rows behind

An asset id is a content address, so reconverting a capture with a fixed converter produces DIFFERENT
bytes and therefore a different id. The importer creates the new row and knows nothing about the old
one, so a search returns both and the director may place either.

Seen live: fixing the roughness and mirror translations changed `Teacher_v1` and `bride_ready`, and the
catalog then held two of each. The stale rows were identified by hashing the current rebuilt files and
removing set members that matched none of them — which works, and is a script nobody has written.

**Unresolved:** whether the importer should retire a superseded row automatically. It would need a
notion of identity above the bytes — same label, same set, newer — and getting that wrong deletes an
asset somebody placed. Content addressing is what makes the problem, and it is also what makes a wrong
answer cheap to recover from, since the bytes are still there.

**Now a blocker, not just an annoyance.** The composition work (plan § Phase 2b) merges several
containers into one asset, which is by definition a re-import that supersedes: new bytes, new content
address, new row. Landing it without this fixed does not halve the catalog, it doubles it. The
`shipped_with` edges have the same problem — they are keyed to the old model id, so 21 clips per figure
need re-pointing rather than inheriting.

---

## Related

- [`specs/captures.md`](../specs/captures.md) — what the pipeline does today.
- [`backlogs/library.md`](./library.md) — the catalog's own backlog.
- [`backlogs/figures.md`](./figures.md) — what a figure needs once it has arrived.
