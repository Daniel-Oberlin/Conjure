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

One SQLite file, schema-versioned by `PRAGMA user_version` (**v7** today). Known versions migrate in
place — ALTER, never DROP, because captions and user curation are not recoverable from the
content-addressed cache. Only a fresh or unrecognised database is rebuilt from disk.

```sql
assets(
  id TEXT PRIMARY KEY,             -- <sha16>.<ext>, also the /assets filename
  kind TEXT,                       -- image | model | animation | audio | set | skybox |
                                   --   grounded_skybox | photo | …
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

### 2a. Clips, audio, and what links them

Three kinds arrived together, because a captured character is not one file.

| kind | what it is | what the record carries |
|---|---|---|
| `animation` | a skeleton-only GLB: channels bound to bone **names**, no geometry | `clips`, `channels`, `targets`, `rig_sig`, and a descriptor — `duration_s`, `activity_deg_s`, `travel_deg` per bone, `dominant` |
| `audio` | the voice of a clip, or a room's ambience | `duration_s`, `sample_rate`, `channels`, `bitrate_kbps`, `role` |
| `set` | one capture, **asserted** at import | the grouping is not in the build — nothing links a character's registry to the room it appears in — so it is an argument, not a guess |

**`rig_sig` is what makes a clip and a figure comparable without either naming the other.** It
fingerprints a skeleton over its *mapped humanoid bones*, so the same value comes out of a character's
GLB and of a clip authored on it. Sixteen of twenty captured figures share one signature. Over the
mapped bones and not every node on purpose: two of them differ by 51 skirt and anatomy bones while
agreeing on all 37 core ones, and a fingerprint that split them would answer a question nobody asks.
`None` for a rig no map could be recovered from. `RIG_SIG_REV` stands apart from `FRAME_REV` — a
discovery fix can change a signature without changing what a signature *means*.

**The descriptor exists so a clip can be chosen before it has a name.** Slot names like `1_idle` are
per-figure labels: that one exists in 16 captures in 14 distinct versions, 10 to 43 seconds long.
Summing the angle between successive keyframe quaternions separates them objectively and costs one pass
over the accessors — `stands still, arms only` at 8 deg/s against `busy hands` at 66.

**Three axes, kept separate** ([decisions.md §27](../decisions.md)):

| question | answered by | nature |
|---|---|---|
| can it drive this skeleton? | `rig_sig` equality | mechanical, total |
| was it authored for this figure? | `shipped_with` | the original intent, unrecoverable once lost |
| does it need something in the room? | `wants_props`, parsed from the name | a **hint**, never a gate |

The third is why the first is not sufficiency: `pc_leanOnSink_headLeft` plays on any figure of the right
rig and is wrong on one standing in a field. 93 of 206 clip names call out a fixture.

Relations, in the table that was always there:

```
figure  --shipped_with-->  clip      the clips that shipped in the figure's OWN build
clip    --voiced_by----->  audio     MANY-TO-MANY: `1-4-5-7-10_idle` voices five clips
env     --ambience------>  audio     room soundtracks
asset   --part_of------->  set       provenance only, never the compatibility test
```

### Seeing them — `show` and `dir`

Relations are only worth recording if they can be READ. Two surfaces, and no SQL:

**`show <asset>`** prints the `attributes` bag one scalar per row (a nested value is summarised, never
dumped — a clip's `travel_deg` is six joints) and then the links, grouped by type and direction:

```
  attributes        17 keys
  rig_sig           85e41f9b8e
  rigged            yes
  parts             {4 keys}: clothes_maiddress, model_britney, …
  links             22 edges
    → part_of         1  set        jane
    → shipped_with   21  animation  10_action, 10_idle, 1_action, 1_idle, +17
```

**`dir --rel TYPE`** adds a COLUMN, so a listing answers "which of these has a voice" in one pass
rather than one `show` per row. `--with NAME` narrows to assets linked to NAME; `--out` / `--in` pin
which way the edge points.

```
dir --kind animation --rel voiced_by            every clip, with its audio or a dash
dir --with jane_export --rel shipped_with       her 21 clips
dir --kind set --rel part_of --in               each set, with what belongs to it
```

**A `--with` name is disambiguated by the RELATION.** Labels collide constantly here by design: every
capture names its set after its figure, so `barbie` is both `set:barbie` and her model, and a flat
refusal made `--with barbie` useless for the most ordinary question there is. When exactly one
candidate has edges of the type asked for — only the model ships clips, only the set holds parts — that
is the one meant. Genuine ambiguity is still refused, and `3_idle` is the case that stays genuinely
ambiguous: the clip and the audio that voices it both touch a `voiced_by` edge, so only `--out`/`--in`
can separate them.

**The arrow is load-bearing.** Both directions is the default, because a figure's clips and the clips
one audio voices are the same question from opposite ends — but `part_of` fans IN 107 where it fans
OUT 15, so "the set this belongs to" and "the parts of this set" are very different lists.

**Listings page at 200 rows and say so** — `… (more than 200)`. `--all` lifts the cap, `--limit N`
sets it. A **glob always searches everything** regardless of the page size, because a search that
reads the first page is not a search: on the live catalog `dir *idle*` matched 32 of 183 and reported
no truncation at all, since the cut happened in the candidate set rather than the result and the
marker is itself a row the glob skips.

**A filter runs over every asset, then the listing is capped** — not the reverse. The 200-row cap used
to be applied while building rows, so a filter narrowed an arbitrary first page: measured on the live
catalog, `--kind animation` returned 98 of 364 and `--with jane_export` found **none** of her 21 clips
because they sat past the cut. A filter that silently narrows its own input is worse than no filter.

`conjure/capture_set.py` holds the rules, and the point of it is where they **stop**. A name that does
not parse is left unlinked rather than guessed — `HotelAction0`'s number indexes a script we do not
have, and 698 of 898 audio files across twenty captures attach to nothing by name. They are still
imported and still searchable; they simply carry no assertion.

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

### 5a. More than one owner

An asset id is a **content address** (`sha256(data)[:16] + ext`), so the id *is* the bytes and two
agents cannot hold one asset under two rows. Ownership is therefore a join table,
`asset_scopes(asset_id, scope)`, and the predicate gains a second half:

```sql
scope = ? OR (public = 1 AND scope GLOB '*/agents/<agent>')      -- the CREATING scope
  OR EXISTS (SELECT 1 FROM asset_scopes s WHERE s.asset_id = id AND s.scope = ?)   -- a grant
```

`assets.scope` remains the scope that *created* an asset — provenance, and what keeps every earlier
query meaning what it meant. A grant is **exact**: it names one scope and widens nothing, which is what
stops it being a way around the agent wall. `grant` / `revoke` / `transfer` / `owners`; a transfer is a
grant then a revoke, in that order so a failure leaves two owners rather than none.

**Curation is shared.** Notes, tags and rating live on the asset, so two owners cannot disagree about
one. That is knowingly given up — a composite `(id, scope)` key would have allowed it — and is a later
migration if it is ever wanted ([decisions.md §26](../decisions.md)). The join table carries no `public`
flag of its own: it had one briefly, and the first `update` that flipped an asset private proved why —
a copy of the flag per owner drifts the moment one owner changes it.

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

**Admin (shell `dir`, `show`):** `POST /admin/tree` and `/admin/match` take optional `kind`, `related`,
`relation` and `direction` — so a capture's figure, sixty clips and thirty audio files can be asked
apart. `related` resolves an id or an exact label and refuses an ambiguous one rather than picking;
`relation` alone adds a column instead of filtering; `direction` is `out` | `in` | omitted for both;
`limit` is the page size, `0` for all.
`POST /admin/show` returns the attributes and the links (§2a).

**Ownership:** `library.grant(id, scope)` / `revoke` / `transfer(id, from, to)` / `owners(id)` (§5a).
**Relations:** `add_relation(from, to, type)`, `related(id, type, reverse=)`, `relations_of(id)`.

**Importers** (`conjure/importer.py`): one handler per asset family, claiming extensions and confirming
by content — `image`, `model`, `animation`, `audio`, plus the stereo variant. `.glb` is claimed by two
of them, so `plan_import` asks the FILE: channels and no mesh is an `animation`.

**Capture ingest:** `scripts/import_capture.py <capture> [--commit]` — dry by default, because the bytes
are content-addressed and shared, so deleting a row is not the inverse of an import that went wrong.
Its linking rules are `conjure/capture_set.py`.

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
