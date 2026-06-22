# Asset Library — implementation plan

**Status:** Phases 0–2 **done** (catalog + embeddings + director-facing reuse/correction tools);
Phase 3 (director prompt policy) landed alongside Phase 2. Phases 4–5 deferred.
**Scope of first pass:** Phases 0–3 (foundation + embeddings + tools + prompt). Phases 4–5 deferred.

Turn the passive content-addressed byte cache into an **explicit, director-controlled asset
library** with intelligent cross-session reuse — one that generalizes to new media kinds (audio,
music, SFX) and is *ready but not built out* for a large NAS photo/video collection.

**Bigger picture (see §5):** this is not one universal store. We segregate by **domain** — the
Conjure procured-asset cache, a personal photo library (NAS), a music library — each its own
store/schema/backend/lifecycle, conforming to a **shared contract** so the machinery (embedding,
search, relations, curation) is reused and **other (non-VR) apps can consume any store** directly.

---

## 1. Motivation

Today the cache (`.cache/assets/`) is keyed on **outputs**: image bytes (SHA-256) and a model's
download URL. Consequences:

- **No reuse on re-request.** Image generation is non-deterministic, so the same prompt yields
  different bytes → a new key → a fresh web/gen call every time.
- **The 3D search call is never saved.** `place_asset "oak tree"` always hits the Poly Pizza
  *search* API; the cached download only helps *after* the search returns a URL.
- **Metadata is volatile.** Image records live in the in-memory `IMAGES` dict (`server.py:76`);
  after a restart `_get_image` rebuilds them as `provider="?", model="?", prompt=""`
  (`server.py:127-131`). Provenance is lost.

The fix is to key on **intent** (the prompt/query), make reuse an **explicit tool** the director
chooses (never a hidden default), and persist a durable, queryable catalog.

## 2. Principles

1. **Explicit over hidden.** Reuse is always a visible tool call the director makes and narrates;
   never baked into the generator tools. The user can always see (and correct) whether they got a
   fresh asset or a recalled one.
2. **Catalog separated from bytes.** A record's `source` pointer abstracts location:
   `cache://<hash>` now; `nas://<path>`, `https://…` later. This single seam lets the procured
   cache and the NAS index share one data model.
3. **One schema, swappable *vector index*.** The metadata store (SQLite) is stable at any scale;
   only the vector index graduates (sqlite-vec for the cache → LanceDB/FAISS for the NAS). The
   repository interface abstracts the vector index specifically.
4. **Local-first / offline.** Local embeddings; no network on the reuse path.
5. **NAS-ready, NAS-stubbed.** Schema and interfaces accommodate the NAS (incl. faces/persons and
   multiple embedding spaces); no scanner/captioner/face pipeline built this round.
6. **Segregate by domain, not by media type.** Stores split on ownership/lifecycle/scale/consumer
   (cache vs. personal photo library vs. music library), *not* on file kind. Within a domain, media
   kinds stay unified (images + models + skyboxes + generated SFX share the cache) so reuse search is
   kind-agnostic ("a dragon" may be an image *or* a model). A generated SFX and a ripped album are
   both "audio" but live in different stores — different domains. (See §5.)
7. **Shared contract over separate stores.** A common Asset record + repository interface lets a 2D
   GUI AI app consume the photo or music store with zero Conjure coupling. Stores stay app-agnostic
   (no "surface"/"director" concepts) and — when shared across apps — user-scoped (live outside
   `.cache/`, e.g. `~/.local/share/medialib/`). One embedding space is pinned across stores so vectors
   stay comparable.
8. **User curation is first-class.** Your own thoughts about an asset — notes, favorite/rating, tags,
   and "default for X" aliases — are core, cross-kind, cross-domain fields (§6), the same ones NAS
   ingestion imports from EXIF/XMP. Aliases act as authoritative reuse overrides (§7).

## 3. Why SQLite (and where it stops)

Two separate jobs that scale differently:

- **Metadata / relational / FTS store** — 100k+ rows is trivial for SQLite; fine into the millions.
  This stays SQLite at NAS scale.
- **Vector search** — `sqlite-vec` is currently **brute-force KNN** (scans every vector). Fine for
  the procured cache (hundreds–thousands → instant). At a 100k+ NAS it lands ~tens-to-hundreds of
  ms and grows *linearly* → past a few hundred thousand you want an **ANN index** (LanceDB IVF/HNSW,
  embedded & file-based, or a FAISS sidecar).

**Therefore:** metadata/FTS/relations = SQLite everywhere; the vector index is the part that swaps.

## 4. Embeddings — CLIP/SigLIP shared text-image space

We are committed to the NAS, so we adopt a **CLIP-family shared space from the start** (text and
image embeddings land in the same space → query text can match image *content*, not just typed
prompts). Default: a local **SigLIP** model (a `so400m` / SigLIP 2 checkpoint), lazy-loaded, offline.
3D models embed their title text (coexists in the shared space).

**Why SigLIP over OpenCLIP (decided — quality over footprint):** SigLIP's sigmoid loss gives better
**recall per parameter** — for the same query it surfaces more of the genuinely-matching items and
ranks them higher, with the gains concentrated on *hard* queries (fine-grained, compositional, tail
concepts) and, for us, on the trustworthiness of the `strong/weak/none` reuse tiers. The cost we
accept: ~2× vector storage (so400m is **1152-dim** vs ~512–768 for an OpenCLIP ViT-B/L) and slower
one-time NAS ingest. The decision is *sticky* — every vector must share one space, so a later swap
means re-embedding the whole corpus (cheap for the cache, expensive at a 100k+ NAS), which is why we
pick the higher-quality model up front. Note: the genuinely reusable NAS building blocks (vector
index, face pipeline, pHash, exiftool) are **model-agnostic**, so this choice is about retrieval
quality, not code volume.

**Backend posture (decided):** the embedding *model* is SigLIP; the *backend that runs it* is
swappable behind the `Embedder` interface (`embed_text`, `embed_image`), and chosen by deployment:
- **Dev default: local torch + transformers.** Zero export friction, every checkpoint available
  immediately, and the matched image-processor/tokenizer ships with the model (avoids the silent
  preprocessing-mismatch footgun). Best for getting the model + pipeline *correct*.
- **Lean/Pi deploy: export to ONNX Runtime.** Same SigLIP vectors, far smaller footprint, no
  CUDA/torch stack, faster cold start — once the model choice is frozen. The export step is the cost.
- **Thin/offline-incapable deploy: hosted** (e.g. Voyage multimodal) — no local ML deps, Pi-friendly;
  needs network + a key, so not offline. Note: **Anthropic has no embeddings endpoint.**

This directly honors the cloud-first / thin-Pi posture in `docs/decisions.md` #1: torch is **one
backend, never a hard requirement** — isolated as an **optional dependency group** (`conjure[embed]`),
lazy-loaded (no torch at boot or in the default test path).
- **Query-by-image is first-class, not an add-on.** Because text and image share one space, an image
  query is the *same* KNN as a text query with `embed_image` swapped for `embed_text` — the vector
  tables and search are modality-agnostic. Two flavors: querying by an asset **already in the
  catalog** ("more like this") is a pure vector lookup on its stored embedding (no re-embedding, model
  need not even be loaded); querying by an **external** image embeds it once at query time. This also
  enables hybrid queries (image vector nudged by text: "like this, but at night").
- **CI/test seam:** embeddings are optional. Exact-key + FTS5 work without the model; tests inject a
  deterministic fake embedder so `torch` stays out of the default (fast, no-network) test path.
- **Cost in eyes-open:** torch is multi-GB (CUDA wheel 3–8 GB on Linux unless the CPU index is used),
  ~1–2 GB RAM to load so400m, and slow on a CPU/Pi. Fine for the cache (tiny embed volume); the NAS
  corpus-embed (Phase 5) genuinely wants a GPU. Mitigations above (optional group, lazy-load, ONNX/
  hosted backends, CPU wheels) keep the core install lean and the Pi path viable.

**Faces are a different space.** CLIP/SigLIP embed *what's in the photo* and **cannot identify
individuals**. Grouping/finding people requires a separate pipeline — face **detection** →
identity-trained face **embedding** (ArcFace / InsightFace / FaceNet) → **clustering**
(HDBSCAN/DBSCAN) into named persons. Same store/ANN infrastructure, different model and a per-face
record. This belongs to the NAS subsystem (stubbed), but the schema anticipates it now (§6).

## 5. Architecture — stores, domains & layers

### Three layers (target)

```
  Apps        Conjure VR director │ 2D photo browser │ music tool │ …
                       │                  │                │
              ─────────┼──────────────────┼────────────────┼──────────  repository interface
  Domain      conjure-cache        photo-library      music-library      (separate stores:
  stores      (SQLite+vec)         (SQLite+LanceDB)   (SQLite+…)           own schema/backend/
                       │                  │                │               lifecycle/LOCATION)
              ─────────┴──────────────────┴────────────────┴──────────
  Shared      Asset record contract · Embedder (SigLIP) · vector+FTS search ·
  toolkit     relations · curation · dedup · eviction        (domain-agnostic machinery)
```

- **Segregate by domain** (principle 6). The three domains diverge hard — owner (app vs. you),
  lifecycle (regenerable vs. precious, index-in-place), scale (10³ vs. 10⁵–10⁶), backend (brute-force
  vec vs. ANN), metadata (prompt/tris vs. EXIF/faces vs. bpm/key), and consumers (Conjure vs. many
  apps). Cramming them into one store/schema buys null-sprawl and a scale/lifecycle mismatch. So:
  **separate stores**, each owning its schema + backend + on-disk location.
- **Shared contract, not shared table** (principle 7). All stores implement a common Asset record +
  repository interface, so the toolkit (embedding, search, relations, curation, dedup) is written
  once and any app consumes any store. A 2D photo app imports `photo-library` with no VR baggage;
  Conjure can *federate* (query several stores, merge) when it wants.
- **Don't build the federation framework yet** (YAGNI). `conjure/library.py` today **is the
  Conjure cache store** — keep its public interface clean and app-agnostic. **Extract the shared
  toolkit when the photo store lands (Phase 5)**, generalizing from two real implementations rather
  than one imagined one.
- **Per-agent scoping is a cross-cutting layer over this and the world store** — see
  `persistence-model.md`. Each agent works inside `private/<agent>/…`, scope is a capability injected
  by the runtime (never an LLM tool param), and **worlds live in a separate document store**, not the
  asset catalog. A `scope` field gets added to the asset schema when Phase 2 touches it; enforcement
  is deferred to the second agent.

### Conjure cache store (what Phase 0 built)

```
                ┌──────────── AssetLibrary (conjure/library.py) — the CACHE store ──────────┐
 director ──►   │  catalog (SQLite): assets | relations | aliases | faces | persons | FTS5  │
 (MCP tools)    │  vector index:     sqlite-vec (cache)   ┄┄►  LanceDB/FAISS (other stores)  │
                │  bytes:            cache://<id>          ┄┄►  nas://<path> | https://       │
                └──────────────────────────────────────────────────────────────────────────┘
                         ▲ write-through                ▲ embed
              _store_image / AssetResolver        Embedder (CLIP/SigLIP; faces: NAS-only)
```

Reuse flow (cache-aside, explicit): director calls `search_library` → tool runs staged matching
(**alias/default override** → exact → FTS5 → vector) → returns candidates + a **confidence tier** →
director decides (place / offer / generate). A miss generates via the existing web tools, which
**write through** to the catalog.

## 6. Data model (core + attributes)

SQLite file at `.cache/library.db`. **Schema pattern: shared *core* columns + a per-kind JSON
`attributes` bag.** Truly-common fields are columns; kind-specific ones (`tris`/`bbox` for models,
`transparent` for images, `bpm`/`key` for audio, EXIF for photos) live in `attributes`, so a new
kind adds *no* columns and creates *no* null sprawl. Promote a JSON path to an indexed generated
column only when you actually query on it.

```sql
assets(
  id TEXT PRIMARY KEY,          -- <sha16>.<ext> (also the /assets filename)
  kind TEXT,                    -- image | model | skybox | grounded_skybox | audio | photo | …
  source TEXT,                  -- cache://<id> | nas://<path> | https://…
  filename TEXT,
  -- intent (the reuse key) + machine display name:
  label TEXT,                   -- prompt (images) or title/query (models)
  prompt TEXT, query TEXT,
  params_json TEXT,             -- output-affecting params (op, transparent, size, …) — exact-match key
  provider TEXT, model TEXT,
  width INT, height INT,        -- common visual dims (NULL for non-visual kinds)
  licence TEXT, attribution TEXT, creator TEXT,
  attributes JSON,              -- kind-specific: {tris,bbox}·model {transparent}·image {bpm,key}·audio {exif…}·photo
  -- USER CURATION (core, cross-kind, cross-domain; same fields NAS ingest imports from EXIF/XMP):
  notes TEXT,                   -- freeform: "my favorite city skybox", "important family photo" (FTS)
  tags TEXT,                    -- keywords: favorite, family, … (FTS, also filterable)
  rating INT, favorite INT,     -- ⭐ 0–5 / boolean — filter & rank
  embed_model TEXT, embed_dim INT,   -- which model/space this asset's vector is in
  created_at REAL, last_used REAL, use_count INT
)

aliases(alias TEXT PRIMARY KEY, asset_id TEXT)   -- "default dog" → id; an authoritative reuse OVERRIDE

relations(from_id, to_id, type)        -- derived_from, co_occurs, depicts_person, at_event, …
                                        -- walked with recursive CTEs (lineage, co-occurrence)

-- Reserved now, populated by the NAS subsystem later (faces are SUB-entities of an image):
faces(id, asset_id, bbox, embedding, person_id)
persons(id, name)

-- text search + vector index (per kind / per space, not one global table):
assets_fts   USING fts5(id UNINDEXED, label, prompt, query, notes, tags)
assets_vec   USING vec0(id, embedding float[N])     -- semantic (CLIP) space
-- faces_vec  USING vec0(...)                         -- face space, NAS-only (reserved)
```

Notes:
- **User curation is first-class** (principle 8). `notes`/`tags` join the FTS columns, so "my favorite
  city skybox" is searchable; `rating`/`favorite` are filter/rank signals; **`aliases` are an
  authoritative override** — "make this my default dog" pins `dog → id`, and the reuse policy honors
  it ahead of any semantic match (auto-STRONG tier). These same fields are where NAS ingestion lands
  imported ratings/keywords/captions (§13), so live annotation and imported curation unify.
- **Per-kind / per-space vector tables** rather than one global vector table — so the semantic space
  and the (future) face space never mix, and a kind can graduate to a different index independently.
- The **exact-match key** = normalized `prompt|query` + the output-affecting `params_json` (so a
  transparent cutout is never reused for an opaque request, etc.).
- **Embedding provenance is recorded** (`embed_model`, `embed_dim`). Query and corpus vectors are
  only comparable within the same model/space, so a model swap must trigger a **corpus re-embed**
  (mixed versions are detectable and migratable rather than silently corrupting distances).
- *Status:* this schema is **implemented** (`conjure/library.py`, schema v2). `width`/`height` stayed
  core (common visual dims); `tris`/`bbox` moved into `attributes`; curation fields + `aliases` added.

## 7. Phases

### Phase 0 — Catalog foundation ✅ DONE (no director-visible change)
- New `conjure/library.py` → `AssetLibrary` over `.cache/library.db` (the **Conjure cache store**).
- `_store_image` and `place_asset`'s `AssetResolver.resolve` **write through** to the catalog
  (prompt/query, params, provider, **licence/attribution**, dims). `_get_image` recovers provenance
  from the catalog — the restart-degradation (`provider="?"`) regression is fixed.
- **Backfill** seeds the catalog from the on-disk cache + world doc once (best-effort, never blocks
  boot). Verified: 74 rows recovered from the real cache.
- **Core+attributes + curation refactor ✅ done:** kind-specific fields (tris/bbox) moved into
  `attributes` JSON; curation fields (`notes`/`tags`/`rating`/`favorite`) + `aliases` table added;
  FTS now covers notes/tags; staged `search` honors aliases first. Schema-versioned (`PRAGMA
  user_version`) — a schema bump drops & rebuilds the regenerable catalog from disk automatically.
- **`annotate_asset` tool ✅ done:** MCP tool + `/annotate_asset` endpoint + `library.annotate()`
  ("remember this as my favorite", "make this my default dog" → an alias override).
- Tests: `tests/test_library.py` + integration in `test_server.py`/`test_mcp.py` (full suite green).

### Phase 1 — Embeddings ✅ DONE
- New `conjure/embeddings.py` → `Embedder` protocol with swappable backends: **SigLipEmbedder**
  (local torch+transformers, lazy — no torch import until first embed) and **FakeEmbedder**
  (deterministic, dep-free, for tests). `build_embedder(settings)` selects by `embed_backend`
  (`auto`/`siglip`/`fake`/`none`) and returns **None** when torch is absent → server degrades to
  FTS/exact, ML-free. ONNX/hosted remain drop-in implementations of the same protocol (future).
- **Vector storage/search in `library.py`** via sqlite-vec: `add_embedding(id, vec, model)` (lazy
  `assets_vec` vec0 table at the embedding dim; records `embed_model`/`embed_dim`) and
  `vector_search(vec, kind?, limit)` (L2 KNN on unit vectors → cosine order, kind-filtered). Gated on
  the extension — degrades cleanly if absent. Schema → v3 (auto-rebuilds the regenerable catalog).
- **Write-through** (`server.py` `_embed_asset`): images embed their pixels, models their title text;
  best-effort (never breaks the request path), skipped when no embedder.
- **Deps:** `sqlite-vec` added to **core**; `torch`+`transformers` in the optional **`conjure[embed]`**
  group (not core). Config: `embed_backend`/`embed_model` (env-overridable).
- Tests: `test_embeddings.py` + vector tests in `test_library.py` + write-through in `test_server.py`
  (full suite green).
- **SigLIP torch path verified** ✅ via `scripts/smoke_embed.py` on Apple Silicon (transformers 5.x,
  SigLIP 2 base): unit vectors @ correct dim, text↔text semantics order correctly, and **text↔image
  alignment** holds (red/blue swatch vs colour-word). Fixed one real bug in the process — transformers
  5.x returns `BaseModelOutputWithPooling`, so the embedder now reads `.pooler_output` (the old
  `feat[0]` grabbed `last_hidden_state`). Re-run after `pip install -e ".[embed]"` to verify on a host.

### Phase 2 — Library tools (explicit, director-facing) ✅ DONE
*(Phase 3 director prompt policy landed with it.)* Implemented: `library.find()` (tiered, reject-aware),
`reject()`, the `scope` seam (schema v4, written as `private/builder`); endpoints `/library/search`,
`/place_cached_asset`, `/correct_asset`; MCP tools `search_library` / `place_cached_asset` /
`correct_asset`; and the reuse-before-create policy in `agents/builder/prompt.md`. Query embedding runs
off the loop. Tests across library/server/mcp; full suite green.
- `search_library(query?, image_id?, kind?)` MCP tool → **read-only**; returns
  `{candidates: [{id, kind, label, created, last_used, licence, match}], confidence_tier}` where
  tier ∈ `strong|weak|none` is **computed server-side** from staged matching (exact → FTS5 → vector
  score). The LLM never thresholds a raw float. Server endpoint `/library/search`.
  - Accepts **either** a text `query` **or** an `image_id` (an asset already in the catalog) → the
    "more like this" path is a pure vector lookup on the stored embedding. (Querying by external
    image bytes can come later; the catalog-id path covers the director's natural case — user points
    at an in-scene asset and says "more like that.")
- `place_cached_asset(id, size_m?, pos?)` → places a **model** by id (gives 3D the procure→place
  symmetry that images/skyboxes already have). Images reuse `place_image`; skyboxes reuse
  `set_skybox` / `set_grounded_skybox` (they already take ids).
- `annotate_asset(id, note?, tags?, favorite?, rating?, default_for?)` → captures **user curation**:
  "remember this as my favorite city skybox" (note/favorite), "make this my default dog"
  (`default_for` → an `aliases` row), "important family photo" (note + rating). A visible, narrated
  action, consistent with the explicit-tool philosophy — it makes the library feel like memory.
- `correct_asset(id, label?, query?, tags?, reject_for?)` → the **correction loop** for mismatches
  (the real case: "starship enterprise" fetched an X-wing — Poly Pizza had no Enterprise and we took
  `results[0]` verbatim). Lets the director **rewrite the wrong machine description** (`label`/
  `query`/`tags`) so it stops masquerading, and/or **`reject_for="starship enterprise"`** to record a
  negative association (a `reject` relation) that **excludes** that asset from future matches on the
  query. Where `annotate_asset` only *adds* curation, this *fixes* or *excludes*.
- **Staged matching honors aliases first, and skips rejects:** an `aliases` hit ("dog" → pinned id)
  is an authoritative **STRONG** override, ahead of exact/FTS/vector; assets with a `reject` relation
  for the query are filtered out. Then ranking within matches: score → `favorite`/`rating` →
  `last_used` (recency) → quality (licence cleanliness, tris in range, resolution). Tier thresholds
  in `config.py`.

### Phase 3 — Director policy (prompt engineering)
Update `agents/builder/prompt.md`:
- **Intent signals:** *generate* (creation/novelty verbs → never search), *recall* (definite
  reference / memory language → search, expect a hit), *ambiguous* (indefinite → search, decide by
  tier).
- **Tier → behavior:** `strong` → reuse + light announce; `weak` → offer ("one from before, or
  new?"); `none` → generate (auto-stored).
- **Many matches:** auto-pick best + announce; **"a different one" pages the candidate list** before
  going to web (disambiguation = novelty escape hatch, unified).
- **Transparency:** announce reuse lightly (teaches memory, enables self-correction).

### Phase 4 — Eviction (deferred; unlocked by the manifest)
`last_used` / `use_count` bumped on reuse; LRU prune under a configurable size cap via
`conjure-cli library prune` and/or `/library/prune`. Off by default.

### Phase 5 — NAS seams + shared toolkit (deferred; stub only)
- **Extract the shared toolkit** from the cache store (Asset contract, Embedder, vector+FTS search,
  relations, curation, dedup, eviction) — generalizing from two real stores, not one imagined. The
  photo library becomes a **separate, user-scoped, app-agnostic store** (its own schema + LanceDB ANN
  + location, e.g. `~/.local/share/medialib/`, *not* under `.cache/`), so other apps can consume it.
- `source` supports `nas://`; the repository interface abstracts the **vector index** so the NAS can
  use LanceDB/FAISS while the cache stays on sqlite-vec.
- A documented but **unimplemented** ingestion entrypoint (`scan(path)` raising `NotImplementedError`)
  that names the model passes it will run: **semantic** embedding (CLIP), **face** detect→embed→
  cluster, and **metadata/curation** extraction (§13). No scanner/captioner/thumbnailer built.
- Privacy note: face vectors are biometric data → kept local-first (aligns with the existing posture).

## 8. Integration points (exact)

| File | Change |
|---|---|
| `conjure/library.py` *(done)* | cache store: schema (→ core+attributes, curation, aliases), write-through, staged matching, tiers |
| `conjure/embeddings.py` *(new)* | `Embedder` interface + local SigLIP default (toolkit-bound) |
| `conjure/server.py` | `_store_image`/`_get_image` → catalog *(done)*; new `/library/*` routes |
| `conjure/assets.py` | `AssetResolver.resolve` write-through *(done)*; catalog reuse before Poly Pizza |
| `conjure/mcp_server.py` | `search_library`, `place_cached_asset`, `annotate_asset` tools |
| `conjure/config.py` | embedder, tier thresholds, cache-cap settings |
| `conjure/cli.py` | `library search` / `library prune` for testing |
| `agents/builder/prompt.md` | intent → tool policy |
| `conjure/nas.py` *(new, stub)* | NAS ingestion entrypoint (NotImplementedError) |

## 9. Testing

- Catalog CRUD; backfill/migration; **restart survives metadata** (kills the `"?"` regression).
- Staged matching → tier classification (deterministic fake embedder).
- Tool payloads: `search_library` shape, `place_cached_asset` (mirrors existing MCP payload tests).
- Route-contract + op additions (as done for grounded skybox).
- Director policy: a few `test_director`-style intent→tool-call assertions (generate doesn't search;
  recall does).

## 10. Dependencies

- `sqlite-vec` (vector search in SQLite) — core.
- **Optional group `conjure[embed]`** (NOT core): **SigLIP** via HF `transformers` + `torch` for the
  local embedder. Use the CPU wheel index on non-GPU hosts to avoid the multi-GB CUDA download.
- *(deploy backends, same interface)* `onnxruntime` (lean/Pi) once the model is frozen; or a hosted
  multimodal embedder (no local ML deps).
- *(NAS, later)* LanceDB or FAISS; a face stack (InsightFace/ArcFace) + clustering (HDBSCAN).

## 11. Open decisions

1. ✅ **Embedder default** — **SigLIP** (`so400m` / SigLIP 2), local & offline. Chosen for retrieval
   quality over storage/ingest footprint; the re-embed stickiness rewards picking the better model up
   front. (See §4 for the rationale.)
2. ✅ **DB location** — cache store at `.cache/library.db`. Shared stores (photo/music) are
   user-scoped, outside `.cache/` (e.g. `~/.local/share/medialib/`).
3. ✅ **First-pass scope** — Phases **0–3** in this pass (end-to-end usable feature), 4–5 as follow-ups.
4. ✅ **Store segregation** — by **domain** (cache | photo | music), not media type; unified within a
   domain. (Principle 6, §5.)
5. ✅ **Shared contract over separate stores** — common Asset/repository interface; toolkit extracted
   at Phase 5 from two real stores; stores app-agnostic; one pinned embedding space. (Principle 7, §5.)
6. ✅ **User curation first-class** — `notes`/`tags`/`rating`/`favorite` + `aliases` override, via an
   `annotate_asset` tool; unifies with NAS-imported curation. (Principle 8, §6–7.)
7. ✅ **Embedder backend posture** — model = SigLIP; backend swappable behind the `Embedder`
   interface. **torch+transformers = dev default**, isolated as optional group `conjure[embed]`;
   **ONNX = lean/Pi deploy** (export once frozen); **hosted = thin/offline-incapable**. torch is one
   backend, never core. (§4.)

## 12. Future / out of scope this round

NAS ingestion pipeline (scan-in-place, captioning, thumbnails); face detection/recognition/
clustering and person naming; ANN graduation (LanceDB/FAISS); knowledge-graph queries
(people/places/events) and any move to a dedicated graph DB; shared/remote cache tier across devices.

**`reject` semantics (polish):** `reject(asset, query)` is a **per-query exclusion from reuse search
only** — it drops that asset from `find()` results for the *exact normalized* query string. It is
intentionally narrow today: not fuzzy/semantic (rejecting "starship enterprise" doesn't cover "the
enterprise"), not applied to image/vector-only queries, and **it does not gate the web fetch** — so
after a reject, `place_asset` can still re-fetch the same wrong model from Poly Pizza. Full fix is the
combination (reject + relabel + alias the right model); deeper options if needed later: fuzzy/semantic
reject, and gating the fetch path (ties into the candidate-selection follow-up below).

**Better model fetch (follow-up):** today `AssetResolver` takes Poly Pizza's `results[0]` verbatim
(no relevance gate, no LLM in the loop) — the root cause of the X-wing-for-Enterprise mismatch. Since
we already pull `Limit: 8`, a follow-up would return the candidates to the director to **pick** (or
reject all → "no real Enterprise found; generate one, or use this X-wing?"), preventing the wrong
download rather than only correcting it after. Kept separate from Phase 2 — it reshapes the fetch
path more deeply than the reuse/correction tooling.

Note on similarity *kinds*: CLIP/SigLIP query-by-image is **semantic** ("another beach sunset"), not
pixel-exact. True near-duplicate detection (cache dedup, NAS dedup) wants a **perceptual hash
(pHash)** alongside the semantic vector; "same person" is the **face** space, not CLIP. Both are
out of scope this round but the schema (per-space vector tables, `faces`/`persons`) anticipates them.

## 13. NAS metadata to capture (Phase 5 reference)

*Recorded now so the schema seam is honest; not built this round.* The guiding principle:
**capture the curation the user already did**, and **enrich raw fields into queryable dimensions** —
raw EXIF is only half the value.

**Capture existing curation first (highest ROI — it's your own judgment, already in the files):**
- Star **ratings** (XMP `Rating`), **keywords/tags**, **captions/titles** (IPTC `Caption-Abstract`,
  XMP `dc:description`), color **labels / flags / picks-rejects**.
- **Album & folder structure** — folder names usually encode events/trips; parse the path as metadata.
- **Existing face tags** (XMP region data from Apple Photos / Picasa / digiKam) — can *bootstrap* the
  person clusters instead of starting cold.

**The query dimensions (raw → enriched):**
- **When** — `DateTimeOriginal` (not file mtime, which copies reset), normalized with timezone
  (`OffsetTimeOriginal` when present).
- **Where** — GPS lat/long/alt, **reverse-geocoded to place names** (the words are what you search).
  Best-effort: GPS is often missing or privacy-stripped.
- **Who** — faces → persons (semantic/face pipeline; see §4).
- **What** — scene/object tags + captions (derived: CLIP + captioning).

**Cheap-to-grab, useful:**
- Camera **make/model**; **media flags** (Live Photo, burst → group, Portrait/depth, Night, HDR,
  **screenshot detection** to separate clutter from real captures); **orientation** (functional —
  must-apply); **video** duration/fps/resolution/codec (+ QuickTime capture-time & GPS).

**Dedup:**
- **Content hash** (byte-identical copies) + Apple **content identifier** / original filename
  (near-duplicate copies & re-exports).

**Photographer-niche (store if free, surface on demand):** aperture, shutter, ISO, focal length,
lens, flash.

**Three enrichments where the real payoff is:**
1. **GPS → place names** (reverse geocode).
2. **Time + place → events** (cluster by time/location gaps → nameable "Trips/Memories" as
   first-class objects; fits the `relations` table / knowledge-graph direction).
3. **Screenshot / non-photo filtering** (separate real captures from clutter).

**Tooling:** use **`exiftool`** (or `pyexiftool`) for breadth — it reads EXIF + IPTC + XMP + video +
HEIC/RAW that Python EXIF libraries (Pillow/exifread) silently miss.

**Schema implication:** these land as columns/`attributes` JSON on the photo store's rows (with
`source = nas://…`), events become rows linked via `relations` (`at_event`), and place names/keywords
feed FTS. Imported **curation** (ratings, keywords, captions) lands in the *same* core curation fields
(`notes`/`tags`/`rating`/`favorite`, §6) that the `annotate_asset` tool writes live — so user
curation is one concept whether you authored it in Conjure or it came from EXIF/XMP. No new top-level
tables beyond what §6 reserves. (This all lives in the **separate photo store**, §5 — not the cache.)
