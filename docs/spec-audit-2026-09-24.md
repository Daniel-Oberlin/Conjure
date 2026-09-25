# Spec audit — 2026-09-24

**One-off.** A drift audit of all twelve living specs against the code, run on branch `spec-audit`.
Not an investigation (no symptom, nothing rejected) and not a plan — a point-in-time record of what
was wrong and what was corrected. Delete it once reviewed; the corrections themselves live in the
specs and in the branch's diff.

**Scope:** `docs/specs/*.md`, 7,545 lines. ~1,100 substantive claims checked against source;
**116 corrections applied**. Every spec had drift. None had structural drift — no spec described a
system that no longer exists, and no section had to be rewritten wholesale.

**Method:** one auditor per spec, code as the sole truth, minimum edit to make each statement true.
Constants, field names, endpoint paths, algorithm steps and control flow were read in source rather
than pattern-matched. Two specs had claims re-measured by running the real code (`captures`,
`figures`); one was checked against the live capture set on disk.

---

## 1. What this found that is not a doc problem

Four real code defects surfaced, each confirmed independently against source. **These are the most
valuable output of the audit** — in every case the spec described the intended behaviour correctly
and the code had drifted away from it, so fixing the spec would have written the bug down as truth.
All four were left as code findings; **no spec was edited to match them**.

| # | Finding | Evidence | Why it matters |
|---|---|---|---|
| 1 | **`/show_surface` is not owner-gated.** `_OWNER_ONLY_PATHS` covers `/style_surface` and `/texture_surface` but not `/show_surface` | `conjure/server.py:763` vs route at `:3935` | Any admitted guest can hide or reveal another user's surfaces. `worlds-surfaces.md §6` claims "scene mutation is owner-gated server-side". The endpoint is recent — reads as an oversight when matching moved out of the MCP layer |
| 2 | **`vector_search` bypasses the tombstone predicate.** It filters on agent / scope / `public`, never on `superseded_by`; `supersede` drops the FTS row but leaves `assets_vec` intact | `conjure/library.py:429-460`, `:632` | A superseded asset still returns from the vector stage of `find()`. `library.md §3/§5` assert a tombstone is "reachable by id and by nothing else". `tests/test_library.py:362` pins this invariant for `query`/`search`/`by_user` — not for `vector_search`. Also skips `asset_scopes`, so granted assets yield no vector hits |
| 3 | **`scripts/geo_bench.mjs` throws.** Calls `RS.floatingRoom`, which no longer exists — the export is `roomResiduals` | `scripts/geo_bench.mjs:64` vs `client/space-snap.js:1606` | `spaces-geometry.md §10` calls the cost figures "replayable"; they currently are not. A diagnostic that stopped working without anyone noticing |
| 4 | **`beam_timeout = 0` never arms the beam.** `armed()` tests `nowMs() < armedUntil`; with `linger = 0`, `armedUntil = tNow`, false on the same frame | `client/conjure-pointers.js:236`, `:312` | Both `input.md:199` and the comment at `conjure/config.py:476` say the beam shows while the trigger is held. Off-by-one; invisible in practice because the default is 10 s |

### Stale prose inside the code

Not spec problems, but the same rot in comments and docstrings:

- `retarget.py` **contradicts itself on bone count** — `:67` says "22 bones -> 52", `:534` says "51 bones". `CORE_BONES` (21) + `FINGER_BONES` (30) = 51, but a map may also carry `upperChest` → 52. `figures.md` repeats both numbers in different sections; **the code should settle it first.**
- `conjure/expressions.py` module docstring says "31 facial" with stale mouth bands, and `facial()` says "30 of 32 rigged models" where the module head says 38. Re-measurement confirmed **the spec is right and the docstrings are wrong** (Alice 34 distinct / 26 facial; Saka 57/57).
- `resolve_paths`' docstring claims 5 returned keys; it returns 8 (`conjure/config.py`).
- `_IMMERSION["authored"]`'s docstring says it "matches `ar`"; `authored` is `passthrough: False`, `ar` is `True`. The spec table is right.
- `client/index.html:53` still says injected module scripts are "scoped to the ACTIVE agent"; they are deliberately not.
- `captures/things.json` carries a `revision` field and a comment claiming an import records which revision decided — nothing reads it; `importer.py:514` records `parts_rev` only.

---

## 2. How the specs drift

The corrections fall into four patterns. Three are mechanical and preventable.

**Line references rot silently — the single largest category.** `spaces.md` alone had **28** stale
`file.py:NNN` citations, most off by 600–1,700 lines; every one of `worlds-surfaces.md`'s nine was
stale, by 300–1,000. They rot on every insertion above them, nothing checks them, and a reader who
follows one lands in unrelated code and mistrusts the whole document. Prose beside them stayed
accurate far longer. *Worth deciding:* cite symbols (`_default_surface_material`) rather than
lines, or add a check that greps each `path:NNN` and verifies the named symbol is near it.

**Counts go stale whenever the thing counted grows.** "Four things it detects" over a five-row
table; "exactly four members" over a list of seven; `Settings` 61 fields → 63; `FRAME_REV` 18 → 21;
`RETARGET_REV` 7 → 8; every test count in `figures.md §10` (55→74, 27→35, 46→61). Schema-version and
`*_REV` stamps are the sharpest instance: they are *designed* to change and are quoted as literals.

**A fixed bug outlives its description.** `agents.md §7.5` still documented a session minted without
`first_world` — fixed since, and the paragraph directly contradicted the one above it. Deleted.
Similarly `spaces-geometry.md §10` still said the test suite appends to the live geometry log, fixed
in `fbd26f6`.

**Measurements are unreproducible without a recorded method.** `captures.md`'s "47 dead meshes / 42
dangling" re-runs today as 87/52 (likeliest cause: `Build.locate`'s rename fallback in `8a58a5d`
recovered 199 previously-missing files, binding more containers). `figures.md §8c`'s limb spread
disagrees with `retarget.py`'s own docstring. Neither was changed — the original harness is
unrecorded, so there is no way to tell what moved. *Worth deciding:* a measured claim either names
the command that reproduces it or is marked with the date it was true.

---

## 3. Per-spec results

Ordered as audited — highest complexity first.

| Spec | Lines | Corrections | Verdict |
|---|---:|---:|---|
| `spaces-geometry.md` | 1,073 | 14 | Every threshold, gate name and algorithm step in §4/§5/§9/§10 held. Drift where the last ~20 commits moved: inset recovery is now corner-relative, the `sealWalls` room rule, the §11 knob table |
| `agents.md` | 1,394 | 9 | Drift confined to what moved after the last spec touch: `dir`'s filter flags, the resolver's move to `namespace.py`, two routes added to the owner gate, plus one section describing a since-fixed bug |
| `figures.md` | 1,368 | 14 | Algorithms, constants and named failure stories all held. Drift in version stamps, counts that grew, and surfaces added after the prose |
| `captures.md` | 682 | 13 | Pipeline, flags, key names and thresholds held. Drift in counts that moved under later commits, and two behaviours the code tightened afterwards |
| `worlds-surfaces.md` | 293 | 16 | Structurally sound; the densest drift per line. All nine line refs stale, plus three wrong behavioural claims |
| `library.md` | 417 | 13 | Structurally sound. Includes a claimed `library.db` backup pass that **has never existed** — it was a docstring intention, harvested into the spec as fact |
| `dynamics.md` | 730 | 10 | Module system, tiers, manifest and the whole of `grab` held. Three real behavioural drifts: `anchor` is inert, module scripts load *before* half the globals, `_pick` is no longer unconditionally exact |
| `spaces.md` | 558 | 10 | One real behavioural error (below) and 28 rotted line refs |
| `input.md` | 403 | 7 | Mechanism accurate line for line; drift concentrated in the shipped binding defaults and the consumer inventory |
| `config.md` | 328 | 7 | Best-matched spec in the set — ~120 checkable claims, 7 wrong, all small. The `settings.json` template is character-for-character the code's `DEFAULT_SETTINGS` |
| `occlusion.md` | 70 | 2 | Pre-pass, modes, mesh and material exactly as described; two stale references |
| `voice.md` | 229 | 1 | Most accurate in the set. Every provider, model, rate, threshold and flag checked out, including against pipecat 1.3.0 itself |

### The corrections that change meaning

Most of the 116 are mechanical. These are the ones that would have misled someone:

- **`spaces.md §7` had geometry authority backwards.** It said `/space/capture` is gated on the
  **space** owner. It is gated on the **active world's** owner — the same middleware as scene edits
  (`server.py:763-781`), and the client agrees (`WM.isCaptureAuthority(me, worldOwner)`). The
  consequence is the opposite of what the spec implied: build your own world in someone else's space
  and *you* become the capturer, writing back into their space record. The glossary carried the same
  error and was corrected too.
- **`library.md §1` documented a safety net that does not exist** — "backed up before a destructive
  migration", with a line reference. No backup code exists anywhere in the tree; the cited line was
  a docstring reading *"Back up library.db to protect curation"*. An intention harvested as fact.
- **`dynamics.md §4`: `anchor` drives nothing.** Validated at load, then read by no one — `/module`
  places from `default_pos`, `face_user` and the caller's `position`/`on_surface`. Documentation,
  like `tier`.
- **`dynamics.md §6`: the load-order claim was false.** Module scripts are injected *after*
  `conjure-clock.js` and `conjure-pointers.js` but *before* `world-model.js`, `conjure-contact.js`
  and `conjure-client.js` — and `ConjureBus`, `ConjureFrames` and `ConjureWorldFrame` all live in the
  last of those. A module must read globals at runtime, never at registration.
- **`worlds-surfaces.md §1.1`: friendly ids are derived, not assigned.** Not a sequential counter at
  ingest — taken from the number already in the id (`real_wall_3` → 3), stable by construction.
- **`figures.md §3`: layer 0 moved.** A stated bone map is now read inside `best_humanoid`, not by
  the importer ahead of it — which is what `FRAME_REV 18` / `RIG_SIG_REV 3` exist for.
- **`captures.md §3`: the Basis normal-map rule needs both halves.** The spec said the choice comes
  from the registry, "not from the pixels". Code requires registry **and** pixels
  (`isNormal && looksPackedNormal(rgba)`) — a normal map may be stored plainly, and unpacking that
  yields z = 0 everywhere.
- **`input.md §3`: the shipped binding defaults were the pre-hands scheme**, contradicting the "as
  shipped" table three paragraphs below it.

---

## 4. Open questions — your call

Each was left **unchanged** in the spec because it could not be resolved with confidence, or because
resolving it means changing code rather than prose.

| Where | Question |
|---|---|
| `spaces.md §10` | **`/space/rename` is entirely ungated** (`server.py:2633`) — the body carries `owner`, defaulting to the active space's, and nothing checks the caller. Any shell user can retitle any user's space. Deliberate admin surface, or a missed gate? Written as "ungated" rather than asserting intent |
| `worlds-surfaces.md §4` | `update_entity` treats a target as a real surface only on an **exact id match**, while `/patch` fans a semantic target out to real surfaces. So `update_entity("wall 4", color=…)` recolours every wall but skips the `visible`/`src` treatment — exactly the "reports success, nothing appears" failure §4 exists to prevent. Narrow fix: test `meta.real` on the *resolved* ids |
| `figures.md §8c/§8e` | 51 vs 52 humanoid bones — the code contradicts itself (above). Settle in code, then the spec |
| `library.md §6` | `scripts/describe_figures.py --embed` deliberately writes `kind='model'` vectors into the image space; §6 says models are deliberately *not* embedded and `reindex` **clears** exactly those rows. One of the two is stale — could not tell which |
| `spaces-geometry.md §8` | The spec's reasoning for the prune guard ("a room's shell alone is six surfaces") mirrors the code comment, but `_PRUNE_GUARD_MIN` is **8**. The spec asserts no number, so nothing was changed — but one of the two should move |
| `config.md §6` | "Two independent mechanisms, deliberately belt-and-braces" for keeping tests off a real home — but **nothing in the repo sets `CONJURE_HOME`**; the suite is entirely in-process. Demote to one mechanism plus an escape hatch? |
| `input.md §5` | Code finding 4 above — fix `armed()`, or document that `beam_timeout = 0` disables the beam entirely |
| `spaces.md §1.1` | `meta.leads_to` for an exterior door is written as `null`, not absent as the spec says. Functionally identical for every reader found |

---

## 5. Coverage

What was read, not just grepped: `conjure/config.py` line by line; `client/space-snap.js` (1,613
lines) and `conjure/library.py` (840) in full; `conjure/figures.py`, `poses.py`, `retarget.py`,
`expressions.py`, `hands.py`, `shell.py`, `agents.py`, `director.py`, `agent_server.py`,
`playcanvas.py`, `compose.py`, `rooms.py`, `plane-anchor.js`, `world-model.js`,
`conjure-pointers.js`, `occlusion.js`; the geometry, session, space, surface, library and dynamics
paths of `conjure/server.py`; all three `dynamics/*/module.json` + `.js`; every agent definition.

Verification that went beyond reading:

- The builder agent's tool allow-list was **machine-diffed** against every `@mcp.tool` in
  `mcp_server.py` — it is exactly the full set minus `set_caller` (56 vs 57).
- `captures.md`'s counts were **re-measured** against the twenty captures in `temp/vrh` and the
  composed output in `temp/things` (263 → 258 things, 516 → 533 MB), read-only; no capture or
  rebuild script was run.
- `figures.md`'s morph counts and expression bands were **re-measured** by running the real code
  against `temp/stage/alice.glb` and `temp/3d-model-examples/Saka.vrm`.
- `voice.md` was checked against the installed `pipecat-ai 1.3.0` sources, which is how the
  `allow_interruptions` finding surfaced: the setting no longer exists in pipecat at all, and the
  behaviour now comes solely from `AlwaysUserMuteStrategy`.
- Test counts in `figures.md §10` came from **collected** pytest/node runs, not greps.

Cross-document checks: every relative link in every spec resolves; all twelve specs are indexed in
`docs/README.md`; the `Settings` field count is consistent across the two specs that cite it. The
top-level `README.md` carries none of the corrected claims and needed no change.

### Not covered

- **Prose that asserts intent or rationale** was left alone unless the code contradicted it.
- **Backlogs** (`docs/backlogs/*.md`) were out of scope — they describe what is *not* built, so they
  drift in the opposite direction (an item silently completed). Worth a separate pass.
- `architecture.md`, `vision.md`, `providers.md`, `running.md` — not living specs, not audited.
- Roughly a dozen measurements in `captures.md` could not be reproduced from this disk (the 58- and
  78-capture figures, `temp/rebuilt`'s 416 per-container files) and are flagged in place.
