# The asset library — the spec

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/library.md`](../backlogs/library.md); rejected alternatives
and the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

This is the **catalog**: one durable, queryable record per procured asset, and the staged search that
lets an agent reuse something it made before instead of generating it again. It is the *leaf-content*
half of the two-store split in [`specs/agents.md §2.2`](./agents.md) — the other half is the world
document store.

---

## 1. What the library is for

The cache is keyed on **outputs** — image bytes by SHA-256, a model by its download URL. That gives no
reuse, because image generation is non-deterministic: the same prompt yields different bytes, a
different key, and a fresh paid call every time. The library keys on **intent** instead — the prompt or
query that produced the asset — and makes reuse an **explicit tool call** the agent makes and narrates,
never a hidden default.

Three properties follow, and they are the whole design:

- **The catalog is separate from the bytes.** A row's `source` is a pointer (`cache://<id>` today), so
  the same schema can index bytes it does not own.
- **Reuse is visible.** `search_library` returns candidates and a confidence tier; the agent decides and
  says what it did, so a wrong recall is correctable by the user in the next sentence.
- **The catalog is precious; the bytes are not.** Bytes are regenerable and content-addressed. Curation
  — your notes, ratings, aliases, rejections — is not, which is why `library.db` lives in the **data**
  tree (`~/.local/share/conjure/library.db`) beside worlds and spaces, not in the disposable cache, and
  is backed up before a destructive migration (`server.py:370`).

## 2. The record

One SQLite file, schema-versioned by `PRAGMA user_version` (**v6** today). A version bump **drops and
rebuilds** the catalog from disk, which is safe only because the *derivable* parts are derivable — hence
the backup on the curation columns.

```sql
assets(
  id TEXT PRIMARY KEY,             -- <sha16>.<ext>, also the /assets filename
  kind TEXT,                       -- image | model | skybox | grounded_skybox | audio | photo | …
  scope TEXT,                      -- <user>/agents/<agent> — the capability namespace (§5)
  public INTEGER DEFAULT 1,        -- visibility FLAG, never a path segment
  source TEXT,                     -- cache://<id> | nas://<path> | https://…
  filename TEXT,
  label TEXT, prompt TEXT, query TEXT,      -- intent: the reuse key
  params_json TEXT,                -- output-affecting params — part of the exact-match key
  provider TEXT, model TEXT,
  width INTEGER, height INTEGER,
  transparent INTEGER,             -- real alpha channel; NULL = unchecked
  licence TEXT, attribution TEXT, creator TEXT,
  attributes TEXT,                 -- kind-specific JSON: {tris,bbox}·model, {bpm,key}·audio
  notes TEXT, tags TEXT,           -- user curation, FTS-indexed
  rating INTEGER, favorite INTEGER,
  embed_model TEXT, embed_dim INTEGER,      -- which vector space this row's embedding is in
  created_at REAL, last_used REAL, use_count INTEGER DEFAULT 0
)

aliases(alias, asset_id)                    -- "dog" → id: an authoritative reuse override
relations(from_id, to_id, type)             -- derived_from, reject, … UNIQUE(from,to,type)
persons(id, name)                           -- reserved, empty
faces(id, asset_id, bbox, person_id)        -- reserved, empty

assets_fts  USING fts5(id UNINDEXED, label, prompt, query, notes, tags)
assets_vec  USING vec0(asset_id, kind, embedding float[N])   -- created LAZILY at the embedder's dim
vec_meta(dim)                               -- the dim assets_vec was built at
```

**Core columns plus a per-kind `attributes` bag.** Truly-common fields are columns; kind-specific ones
live in JSON, so a new kind adds no columns and no null sprawl. `transparent` is the one field promoted
back out of `attributes` into a column, because it is *queried* — `WHERE transparent=1` finds decals,
and guessing it from a label was wrong often enough to matter.

`faces` and `persons` are reserved and stay empty. They exist so the NAS seam is honest about being a
sub-entity model rather than a column.

## 3. Ingest — one write-through

Every path that procures bytes catalogs them through the same registry (`register_asset` /
`_catalog_asset` in `server.py`), so there is no path that fills the cache without filling the catalog:

| Path | Enters via |
|---|---|
| image / skybox generation | `_store_image` |
| 3D model fetch | `AssetResolver.resolve` (Poly Pizza) |
| files from disk | `POST /library/import` ← `conjure-import` CLI, handlers in `importer.py` |

`importer.py` is an extensible handler registry — each handler claims extensions, confirms by magic
bytes (`sniff`), and extracts catalog metadata (`extract`). Images, stereo pairs and `.glb` models
today. It has **no dependency on the running server** (stdlib + Pillow, trimesh lazily), so it is
unit-testable alone and reusable by any future scanner.

**Visibility is inherited, not chosen.** A new asset takes the live session's `public` flag
(`_inherit_visibility`, `server.py:872`) and never overwrites a visibility the owner set later.

## 4. Search — four stages and a tier

`find()` is the director-facing query. It runs `search()`'s staged lookup, appends vector hits, drops
rejections, and labels the result with a **confidence tier** so the LLM never thresholds a raw float.

| Stage | How it matches | `match` label |
|---|---|---|
| 1 | **alias** — a user-pinned override (`"dog"` → id) | `alias` |
| 2 | **exact** — normalized `label` / `prompt` / `query` | `exact` |
| 3 | **FTS5** — keyword over label, prompt, query, notes, tags | `fts` |
| 4 | **vector** — L2 KNN on unit vectors (cosine order), kind-filtered | `vector` |

Then rejections are removed: a `reject` relation for the *exact normalized* query string excludes that
asset from the results.

**The tier is decided by stage, not by score:**

```python
strong = any(c["match"] in ("alias", "exact") for c in cands)
tier = "strong" if strong else ("weak" if cands else "none")
```

- **`strong`** — an authoritative hit (a user alias, or an exact intent match). Safe to reuse.
- **`weak`** — only fuzzy hits (keyword or semantic). Offer, don't assume.
- **`none`** — nothing. Generate or fetch fresh.

> **A semantic match can therefore never be `strong`**, however close the vector. That is a deliberate
> floor — an embedding distance is not evidence of *intent* the way a pinned alias is — but it means the
> vector stage's precision is currently unused for tiering. See
> [`backlogs/library.md`](../backlogs/library.md).

**Ranking within a stage** is `favorite DESC, last_used DESC` on the exact stage, FTS `rank` on the
keyword stage, and `distance` on the vector stage. There is no cross-stage quality ranking.

## 5. Scope — the hard agent wall

Scope is a capability injected by the runtime (`CONJURE_SCOPE`), never an LLM argument — the full model
is [`specs/agents.md §2`](./agents.md). In this store it is one SQL predicate, applied on every read:

```sql
scope = ? OR (public = 1 AND scope GLOB '*/agents/<agent>')
```

Two things fall out. A caller sees its **own** scope plus **public** rows — so a friend on the same
server discovers your public assets and not your private ones. And the `*/agents/<agent>` glob means
public **never crosses agents**: `builder` cannot see `outdoor`'s assets even when they are public, and
no prompt injection can widen that, because the predicate is baked into the handle the store hands out
rather than passed in.

Writes and deletes are checked per-id against the caller's scope. `query_assets` runs read-only SQL
against a **temp view on a read-only connection** with the predicate already applied, so an agent cannot
`SELECT` its way out.

## 6. Embeddings

`Embedder` is a two-method protocol (`embed_text`, `embed_image`) with the model recorded per row, so
vectors are only ever compared within one space.

| Backend | When |
|---|---|
| `SigLipEmbedder` | local torch + transformers, **lazy** — no torch import until the first embed |
| `FakeEmbedder` | deterministic, dependency-free; what the tests use |
| `None` | torch absent → the server degrades to FTS + exact, entirely ML-free |

`build_embedder(settings)` picks by `embed_backend` (`auto` / `siglip` / `fake` / `none`) and returns
`None` rather than raising, which is what makes `torch` an optional dependency
(`pip install -e ".[embed]"`) instead of a boot requirement.

**The vector index is visual-only.** Images and skyboxes are embedded from their **pixels**; 3D models
are deliberately *not* vector-embedded and are found by FTS/exact on their title. The reason is
measured, not theoretical: SigLIP text↔text similarity sits at a much higher scale than text↔image, so
text-derived vectors (model titles) dominate every text query and bury the images — observed live at
distance ~0.69 for model titles against ~1.33 for images. `reindex` actively **clears** any non-visual
vector that crept in.

`assets_vec` is created lazily at the live embedder's dimension and gated on the `sqlite-vec` extension
loading; absent either, search silently drops to stages 1–3.

## 7. Maintenance

Three director-facing tools, all scope-enforced:

- **`query_assets`** — read-only SQL for inspection (`SELECT kind, COUNT(*) FROM assets GROUP BY kind`).
- **`update_asset`** — the one mutator: `label`/`query`/`tags`/`notes`/`kind`/`rating`/`favorite`, plus
  `default_for` (writes an alias) and `reject_for` (writes a `reject` relation). Keeps FTS, the vector's
  `kind`, and aliases consistent.
- **`delete_asset`** — removes the row.

`update_asset` is the consolidation of two earlier tools that overlapped — `annotate_asset` (which only
*added* curation) and `correct_asset` (which *fixed* or *excluded*). One mutator, because the director
could not reliably pick between them.

Four operator passes, all off the request path, via `conjure-ctl` or `POST /library/*`:

| Command | What it does |
|---|---|
| `conjure-ctl reindex` | embed cataloged assets that have no vector; clear stray non-visual vectors |
| `conjure-ctl caption` | image→text for assets with **no label** (Gemini by default; `Captioner` is swappable) — makes bare backfilled images readable and FTS-searchable |
| `conjure-ctl retag-skyboxes` | re-tag wide images (aspect ≥ 1.9 — equirectangular) as `skybox`, fixing the vector's `kind` in place, no re-embed |
| `conjure-import` | ingest files from disk through `POST /library/import` |

Captioning exists because embeddings give **visual similarity only** — no readable text. A backfilled
image with no prompt showed a blank label and matched no keyword search even though vector search found
it fine. Generated assets now carry their prompt as the label, so this is a backfill tool, not an
ongoing need.

## 8. Why SQLite, and where it stops

Two jobs that scale differently. **Metadata, relations and FTS** are trivial for SQLite at 100k+ rows
and stay there. **Vector search** is the part that would graduate: `sqlite-vec` is brute-force KNN —
instant over hundreds or thousands, linear thereafter. The repository interface abstracts the vector
index specifically, so that swap is the one anticipated.

## 9. Surface reference

| Endpoint | Purpose |
|---|---|
| `POST /library/search` | staged reuse query → `{candidates, confidence_tier}` |
| `POST /library/import` | ingest a file from disk |
| `POST /library/reindex` | embed rows with no vector |
| `POST /library/caption` | backfill labels for label-less visual assets |
| `POST /library/retag-skyboxes` | re-tag wide images as skyboxes |

**MCP tools:** `search_library`, `place_cached_asset`, `query_assets`, `update_asset`, `delete_asset`.
`search_library` and `query_assets` are in `_READONLY_TOOLS`, so a `access: "read"` agent gets them and
none of the mutators ([`specs/agents.md §4`](./agents.md)).

**Config:** `embed_backend`, `embed_model`, `caption_*`. **Deps:** `sqlite-vec` is core; `torch` +
`transformers` are the optional `[embed]` group.

## 10. Related specs

- [`specs/agents.md`](./agents.md) — scope as a capability, the two-store split, tool gating.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — how a placed asset becomes an entity.
- [`architecture.md §10`](../architecture.md) — the asset pipeline around this catalog, and which of
  its stages exist.
