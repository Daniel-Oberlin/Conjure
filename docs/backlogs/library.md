# The asset library — backlog

Unfinished work, future directions, and known problems for the asset catalog, its search, and its
ingest paths. The current state is [`docs/specs/library.md`](../specs/library.md); the reasoning behind
rejected alternatives is [`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems — verified against the code

### A semantic match can never be `strong` — calibrate distance → tier

*Noted 2026-06-30. Symptom: "I made an image of a key, but reuse shows it as a **weak** match."*

Embedding search already *surfaces* the right asset (cross-modal text→image), but it can never be a
confident hit. `find()` decides the tier from **match type**, not distance:

```python
strong = any(c["match"] in ("alias", "exact") for c in cands)
```

A `vector` hit, like an `fts` hit, is never in that set — so semantic similarity is structurally capped
at **weak**, however close. The `distance` is computed and returned but never thresholded.

That floor is deliberate: `strong` means "safe to auto-reuse without asking," which is a claim about
*intent* (you typed the stored description verbatim, or you pinned the phrase), and semantic "near"
doesn't reliably mean "the asset you meant" — a "key" query is also near a padlock, a door handle, a
keyboard. It bites because a generated image stores `label = prompt =` the whole generation prompt, so a
short later query only ever hits via FTS or vector ⇒ weak.

**A doc/impl wrinkle worth fixing either way:** `vector_search`'s docstring claims "the reuse-tier layer
maps distance → strong/weak/none." `find()` maps no distances. The calibrated mapping was never built,
and the old plan doc asserted the thresholds lived in `config.py` — they never did.

**Proposed:** a calibrated distance→tier rule ("a `vector` hit below X ⇒ strong", a mid band ⇒ weak).
Needs empirical tuning on real catalog data and likely **per-kind**, since distances aren't calibrated
across categories — so keep it opt-in until it's seen to behave. Workaround today is to give the asset
real intent: `update_asset(id, default_for="key")` (alias ⇒ strong) or `update_asset(id, label="key")`
(exact ⇒ strong). Adjacent nudge, behavioural and untested: when the user clearly *names* what they're
generating ("make me a key"), auto-pin `default_for` so it's instantly strong-reusable.

**Open:** one global threshold vs. per-kind; and whether to auto-pin on naming.

### Deleting an asset orphans its bytes

*Noted 2026-06-25. Observed: 2 files left behind after 2 deletions.*

`library.delete()` and `delete_asset` are **catalog-only** — the row, its FTS entry, aliases, relations
and vector go; the file in the content-addressed store stays. The docstring says so ("bytes kept"), and
it is the right default: a placed entity references `/assets/<hash>` directly from the world document,
*independently* of the catalog row, so unlinking on delete would 404 a texture the live scene is still
drawing.

The consequence is unbounded growth — nothing ever reclaims them.

**Proposed:** a separate, deliberate **prune sweep**, not coupled into delete. Remove cache files with
**no catalog row and no reference in the world doc** (scan entity material `src` and gltf-model paths).
**Dry-run by default**, `--apply` to unlink. Would also mop up what has already accumulated. Belongs
beside `reindex` / `caption` / `retag-skyboxes` as a `conjure-ctl` command.

**Open:** whether it must consider *other* scopes' worlds. Today the single live world doc is the only
reference set; with worlds now per-session and per-agent, that assumption is already shakier than when
this was written — a prune that only reads the live world would delete bytes another session references.
**Re-verify before building.**

### The SigLIP embedder pings Hugging Face on every load

*Noted 2026-08-10, from a live startup log. Shelved.*

Loading the local embedder prints `You are sending unauthenticated requests to the HF Hub…`. The weights
*are* local (~4.3 GB at `~/.cache/huggingface/hub/models--google--siglip2-so400m-patch14-384`) and the
log confirms an instant disk load — but `SigLipEmbedder._ensure` calls `AutoModel.from_pretrained` /
`AutoProcessor.from_pretrained` with **no offline flag**: no `local_files_only`, no `cache_dir`, and no
prefetch config anywhere. So every load makes a network metadata/revision call before falling back to
cache. That ping emits the warning, adds startup latency, and makes first-embed **depend on the Hub
being reachable** — it can stall offline, which contradicts the local-first posture the embedder exists
to serve.

**Proposed:** load cache-first, network only as a fallback for a genuinely empty cache. Either
(a) `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` in the environment — blunt, whole-process; or
(b) `local_files_only=True` on both `from_pretrained` calls, wrapped so a first-ever run with an empty
cache still falls back to a one-time download (catch not-cached → retry without the flag). **(b)**,
because it is offline-by-default without breaking a fresh setup.

The accompanying `bos_token_id/eos_token_id … got 49406/49407` lines are cosmetic — CLIP special-token
ids outside the text vocab, irrelevant to `pooler_output`. `TRANSFORMERS_VERBOSITY=error` mutes them.

**Open:** (a) vs. (b); and whether `doctor` should warm the cache so the fallback is never hit.

### No cross-stage ranking

The old plan described ranking as `score → favorite/rating → last_used → quality (licence cleanliness,
tris in range, resolution)`. What exists is per-stage only: `favorite DESC, last_used DESC` on the exact
stage, FTS `rank` on keyword, `distance` on vector. `rating` is stored and never ranked on; licence
cleanliness, triangle count and resolution are stored and never consulted. Candidates from different
stages are simply concatenated in stage order.

Cheap to improve and probably worth it before the tier calibration above, since a better ordering makes
a `weak` tier more useful without needing to trust a threshold.

### `library.annotate()` is dead code

A public method with **zero callers** — the remnant of the retired `annotate_asset` tool, whose job
`update_asset` absorbed. Either delete it or route `update_asset`'s curation path through it.

### `reject` is narrower than it reads

`reject(asset, query)` excludes an asset from `find()` for the **exact normalized query string** only.
It is not fuzzy or semantic (rejecting "starship enterprise" doesn't cover "the enterprise"), it is not
applied to image/vector-only queries, and **it does not gate the web fetch** — so after a reject,
`place_asset` can re-fetch the same wrong model from Poly Pizza. The full fix today is the combination:
reject + relabel + alias the right model.

---

## Never built — designed but absent

### `conjure/nas.py` — the ingestion stub

The plan listed a documented-but-unimplemented `scan(path)` entrypoint raising `NotImplementedError`,
naming the passes it would run. **The file does not exist.** What landed instead, and generically, is
`importer.py` + `POST /library/import` + `conjure-import` — a handler registry any scanner can call. So
the seam exists; the NAS-shaped stub over it never did, and arguably shouldn't until there is a scanner.

### Eviction / LRU prune

`last_used` and `use_count` are maintained. Nothing consumes them. The designed shape is an LRU prune
under a configurable size cap (`conjure-ctl library prune` / `POST /library/prune`), off by default.
Overlaps with the orphaned-bytes sweep above — they are one command, not two.

### `conjure-ctl library search`

The plan assigned a `library search` CLI to `cli.py` for testing. It was never built, and `cli.py` is
the wrong home — maintenance verbs live in `ctl.py`. `search_library` through an agent, or
`query_assets`, covers the need today.

---

## Future directions

### Visual model embedding via rendered thumbnails

3D models are kept out of the vector index and matched by FTS/exact on their title, because
text-derived vectors dominate text queries and bury the images (measured: model titles at distance
~0.69 vs images at ~1.33). To make models semantically searchable ("a large deciduous tree" → an oak)
and image-similarity-searchable, render each GLB to a thumbnail and embed **that**, so models join the
image space at a consistent scale. Needs a GLB→PNG renderer (trimesh/pyrender).

### Rigged humanoid import — see `backlogs/figures.md`

Bringing in **figures** (rigged human models from Blender, Open3DLAB and similar) puts most of its weight
on *this* subsystem rather than on the runtime, because the hard part is manufacturing a vocabulary — a
per-model map from semantic names ("left upper arm", "the jacket") to that model's own nodes. Designed in
full at [`backlogs/figures.md`](./figures.md); what touches the library specifically:

- A **conversion step in front of import** — headless Blender as a universal front door (`.blend`, `.fbx`,
  `.dae`, `.vrm` → GLB). Deliberately *separate* from import, so the world server never depends on Blender
  and a machine without it can still import GLBs. `trimesh` cannot substitute: it carries no skeletons.
- An **LLM- and vision-assisted extraction pass** whose output — bone map, outfit slots, clips, height,
  facing, and the *provenance* of each — rides the existing JSON `attributes` bag, so **no schema change**.
  Affordable because figures are never imported in bulk.
- **Licence capture at conversion time**, while the source page is still in hand; for these sources it is
  the only place attribution exists.

It also builds the **headless GLB→PNG renderer** that *Visual model embedding via rendered thumbnails*
(below) needs and does not have.

### Better model fetch — let the director pick

`AssetResolver` takes Poly Pizza's `results[0]` verbatim: no relevance gate, no LLM in the loop. That is
the root cause of the X-wing-for-Enterprise mismatch that the whole correction loop exists to clean up
after. Since the fetch already pulls `Limit: 8`, returning the candidates to the director to **pick** —
or reject all ("no real Enterprise found; generate one, or use this X-wing?") — prevents the wrong
download instead of correcting it afterwards. Reshapes the fetch path more deeply than the reuse
tooling did, which is why it was kept separate.

### ANN graduation

`sqlite-vec` is brute-force KNN: instant over hundreds or thousands, linear thereafter — tens to
hundreds of ms at 100k+, growing. Past a few hundred thousand you want an ANN index (LanceDB IVF/HNSW,
embedded and file-based, or a FAISS sidecar). Metadata, relations and FTS stay on SQLite at any scale;
**the vector index is the only part that swaps**, which is why the repository interface abstracts it
specifically.

### The shared toolkit, and other domain stores

The design point is **segregate by domain, not by media type**: the Conjure procured-asset cache, a
personal photo library (NAS), a music library — each with its own schema, backend, lifecycle and
on-disk location, conforming to a shared Asset record + repository interface so the machinery
(embedding, search, relations, curation, dedup) is written once and any app can consume any store. A 2D
photo browser should be able to import the photo store with no VR baggage.

Deliberately **not built**: extract the toolkit when a second real store lands, generalizing from two
implementations rather than one imagined one. `library.py` today *is* the cache store; the discipline
that keeps this reachable is keeping its public interface app-agnostic — no "surface" or "director"
concepts leak into it.

Shared stores would be **user-scoped and outside the cache** (e.g. `~/.local/share/medialib/`), and one
embedding space stays pinned across stores so vectors remain comparable.

### Faces and persons

`faces` and `persons` exist and stay empty. CLIP/SigLIP embed *what is in* a photo and **cannot identify
individuals** — that needs a separate pipeline: face detection → identity-trained embedding
(ArcFace/InsightFace/FaceNet) → clustering (HDBSCAN/DBSCAN) into named persons, in its own vector space.
Same store and index infrastructure, different model, a per-face sub-entity record. Face vectors are
biometric data and would stay local-first.

### Perceptual hashing for true near-duplicates

CLIP/SigLIP similarity is **semantic** ("another beach sunset"), not pixel-exact. Real near-duplicate
detection — cache dedup, NAS dedup, re-exports of the same original — wants a **pHash** alongside the
semantic vector. Different question, different index; the per-space vector-table pattern already
accommodates it.

### NAS metadata, when a scanner lands

The guiding principle if it does: **capture the curation the user already did** (XMP `Rating`,
keywords, IPTC captions, colour labels, album/folder structure, existing face-tag regions — which can
bootstrap person clusters instead of starting cold), and **enrich raw fields into queryable
dimensions** — GPS → reverse-geocoded place names, time + place → clustered events as first-class
objects, screenshot detection to separate clutter from real captures. Use `exiftool`/`pyexiftool` for
breadth; Python EXIF libraries silently miss IPTC, XMP, video and HEIC/RAW.

Imported curation lands in the *same* core fields (`notes`/`tags`/`rating`/`favorite`) that
`update_asset` writes live, so user curation is one concept whether you authored it here or it came
from a sidecar. No new top-level tables beyond what the schema already reserves.
