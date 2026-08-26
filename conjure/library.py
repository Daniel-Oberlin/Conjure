"""Asset library — a durable, queryable catalog over the content-addressed asset cache.

This is the **Conjure cache store** (docs/asset-library-plan.md §5): one domain among several planned
(cache | photo library | music library). It persists one row per procured asset (image, model,
skybox) keyed by its file id, carrying the **originating intent** (prompt/query), the output-affecting
params, provider/model, licence/attribution, kind-specific `attributes`, and the user's own
**curation** (notes/tags/rating/favorite + "default for X" aliases).

Two design choices worth holding onto:
- **Catalog separated from bytes** — a row's `source` points at `cache://<id>` today and
  `nas://<path>` / `https://…` later, so the cache and a future NAS index share one model.
- **Core + attributes** — truly-common fields are columns; kind-specific ones (tris/bbox for models,
  transparent for images, bpm/key for audio) live in a JSON `attributes` bag, so a new kind adds no
  columns and no null sprawl.

Search is metadata + FTS5 (`assets_fts`) blended with vector similarity: `sqlite-vec` is loaded when
available and `assets_vec` is created lazily at the live embedder's dimension. Both are over **assets**
— there is no world-level embedding, so there is no semantic recall of worlds by description.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import DEFAULT_USER, agent_of

try:                                  # optional: vector search. Absent ⇒ catalog still works (FTS/exact)
    import sqlite_vec
    from sqlite_vec import serialize_float32
except Exception:                     # noqa: BLE001
    sqlite_vec = None
    serialize_float32 = None

# Bump when the schema changes, and add a branch to _migrate() to upgrade existing data in place
# (ALTER, not DROP — captions/embeddings/curation aren't recoverable from the cache bytes). The
# destructive rebuild is a last resort for a fresh or unrecognised DB only.
_SCHEMA_VERSION = 6

# faces/persons are reserved now (sub-image entities + named clusters) so the NAS seam is honest;
# they stay empty until the NAS ingestion subsystem (Phase 5) populates them.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  kind TEXT,                       -- image | model | skybox | grounded_skybox | audio | photo | …
  scope TEXT,                      -- capability namespace <user>/agents/<agent> (specs/spaces.md);
                                   --   a data seam now — enforcement arrives with the second agent
  public INTEGER DEFAULT 1,        -- visibility flag (NOT a path segment): 1 = world-readable, 0 = private
  source TEXT,                     -- cache://<id> | nas://<path> | https://…
  filename TEXT,                   -- physical name under .cache/assets (NULL for external sources)
  label TEXT,                      -- machine display name: prompt (images) or title/query (models)
  prompt TEXT,
  query TEXT,
  params_json TEXT,                -- output-affecting params (op, transparent, size, …) — match key
  provider TEXT,
  model TEXT,
  width INTEGER, height INTEGER,   -- common visual dims (NULL for non-visual kinds)
  transparent INTEGER,             -- image has a real alpha channel (decal/sticker); NULL = unchecked
  licence TEXT, attribution TEXT, creator TEXT,
  attributes TEXT,                 -- kind-specific JSON: {tris,bbox}·model {bpm,key}·audio
  notes TEXT, tags TEXT,           -- USER CURATION (FTS-indexed): "my favorite city skybox", keywords
  rating INTEGER, favorite INTEGER,-- ⭐ 0–5 / boolean — filter & rank
  embed_model TEXT, embed_dim INTEGER,   -- which model/space this asset's vector is in (Phase 1)
  created_at REAL, last_used REAL, use_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS assets_kind ON assets(kind);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, asset_id TEXT);   -- "dog" → id (override)
CREATE TABLE IF NOT EXISTS relations (
  from_id TEXT, to_id TEXT, type TEXT,    -- derived_from, co_occurs, depicts_person, at_event, …
  UNIQUE(from_id, to_id, type)
);
CREATE TABLE IF NOT EXISTS persons (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS faces (id TEXT PRIMARY KEY, asset_id TEXT, bbox TEXT, person_id TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(id UNINDEXED, label, prompt, query, notes, tags);
CREATE TABLE IF NOT EXISTS vec_meta (dim INTEGER);   -- dim of the live assets_vec table (Phase 1)
"""

# assets_vec is created lazily (its dim depends on the embedder, and it needs the sqlite-vec extension).
_TABLES = ("assets_fts", "assets_vec", "assets", "aliases", "relations", "faces", "persons", "vec_meta")

# Columns a caller may set via upsert kwargs (everything except id, attributes, and the lifecycle
# bookkeeping). `attributes` is handled separately so it can be *merged* rather than replaced.
_UPSERT_COLS = (
    "kind", "scope", "public", "source", "filename", "label", "prompt", "query", "params_json",
    "provider", "model", "width", "height", "transparent", "licence", "attribution", "creator",
    "notes", "tags", "rating", "favorite", "embed_model", "embed_dim",
)


def normalize(text: str) -> str:
    """Canonical form for exact intent / alias matching: lowercase, trimmed, whitespace-collapsed."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _fts_query(text: str) -> Optional[str]:
    """Turn free text into a safe FTS5 MATCH expression (OR of word tokens); None if empty."""
    tokens = re.findall(r"\w+", (text or "").lower())
    return " OR ".join(tokens) if tokens else None


class AssetLibrary:
    """SQLite-backed catalog. Single connection guarded by a lock — writes are tiny and local, so
    synchronous access from the async server is sub-millisecond and simpler than a pool."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._vec = False                              # vector search available? (sqlite-vec loaded)
        if sqlite_vec is not None:
            try:
                self._db.enable_load_extension(True)
                sqlite_vec.load(self._db)
                self._db.enable_load_extension(False)
                self._vec = True
            except Exception:                          # noqa: BLE001 — degrade to FTS/exact only
                self._vec = False
        ver = self._db.execute("PRAGMA user_version").fetchone()[0]
        if ver != _SCHEMA_VERSION:
            if not self._migrate(ver):                   # try in-place upgrade; preserves data
                for t in _TABLES:                        # fresh/unrecognised → (re)build from disk
                    self._db.execute(f"DROP TABLE IF EXISTS {t}")
                self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._db.commit()

    def _migrate(self, ver: int) -> bool:
        """Upgrade an existing catalog in place, preserving its data. Returns True on success; False
        when the DB is fresh or its version is unrecognised, signalling the caller to rebuild from
        scratch. We ALTER rather than DROP for known versions because captions and user curation are
        not recoverable from the content-addressed cache files — only a destructive last resort drops."""
        has_assets = self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone()
        if not has_assets or ver < 4:
            return False                                 # fresh / pre-v4 unknown → rebuild from scratch
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(assets)")}
        if ver <= 4 and "transparent" not in cols:       # v4 → v5: transparency promoted to a column
            self._db.execute("ALTER TABLE assets ADD COLUMN transparent INTEGER")
        if ver <= 5:                                     # v5 → v6: public flag + user-first scope
            if "public" not in cols:
                self._db.execute("ALTER TABLE assets ADD COLUMN public INTEGER DEFAULT 1")
            # `private/<agent>` → `<DEFAULT_USER>/agents/<agent>` (substr(.,9) drops "private/")
            self._db.execute("UPDATE assets SET scope = ? || substr(scope, 9) WHERE scope LIKE 'private/%'",
                             (f"{DEFAULT_USER}/agents/",))
        return True                                      # migrated in place (cumulative)

    # ---- writes -------------------------------------------------------------------------------
    def upsert(self, id: str, *, kind: Optional[str] = None, params: Optional[dict] = None,
               attributes: Optional[dict] = None, **fields: Any) -> None:
        """Insert a new asset or merge fields into an existing one. Only **non-None** fields are
        written on update, so a later partial write (e.g. an embedding, or a curation note) never
        clobbers good data set at creation. `params` → params_json; `attributes` is *merged* into the
        existing JSON bag (so adding `tris` doesn't drop `bbox`)."""
        if params is not None:
            fields["params_json"] = json.dumps(params, sort_keys=True)
        if kind is not None:
            fields["kind"] = kind
        cols = {k: v for k, v in fields.items() if k in _UPSERT_COLS}
        now = time.time()
        with self._lock:
            exists = self._db.execute("SELECT 1 FROM assets WHERE id=?", (id,)).fetchone() is not None
            if attributes is not None:
                cols["attributes"] = self._merge_attributes(id, attributes)
            if exists:
                sets = {k: v for k, v in cols.items() if v is not None}
                if sets:
                    clause = ", ".join(f"{k}=?" for k in sets)
                    self._db.execute(f"UPDATE assets SET {clause} WHERE id=?", (*sets.values(), id))
            else:
                keys = list(cols.keys())
                self._db.execute(
                    "INSERT INTO assets (id, created_at, last_used, use_count" +
                    "".join(f", {k}" for k in keys) + ") VALUES (?,?,?,?" + ",?" * len(keys) + ")",
                    (id, now, now, 0, *cols.values()),
                )
            self._sync_fts(id)
            self._db.commit()

    def _merge_attributes(self, id: str, new: dict) -> str:
        row = self._db.execute("SELECT attributes FROM assets WHERE id=?", (id,)).fetchone()
        merged: dict = {}
        if row and row["attributes"]:
            try:
                merged = json.loads(row["attributes"])
            except (ValueError, TypeError):
                merged = {}
        merged.update({k: v for k, v in new.items() if v is not None})
        return json.dumps(merged, sort_keys=True)

    def _sync_fts(self, id: str) -> None:
        """Keep the FTS row in lockstep with the (possibly merged) text columns. Caller holds lock."""
        row = self._db.execute(
            "SELECT label, prompt, query, notes, tags FROM assets WHERE id=?", (id,)).fetchone()
        self._db.execute("DELETE FROM assets_fts WHERE id=?", (id,))
        self._db.execute(
            "INSERT INTO assets_fts (id, label, prompt, query, notes, tags) VALUES (?,?,?,?,?,?)",
            (id, row["label"] or "", row["prompt"] or "", row["query"] or "",
             row["notes"] or "", row["tags"] or ""),
        )

    def annotate(self, id: str, *, note: Optional[str] = None, tags: Optional[str] = None,
                 rating: Optional[int] = None, favorite: Optional[bool] = None,
                 default_for: Optional[str] = None) -> bool:
        """Record the user's own thoughts about an existing asset: free-text `note`, `tags`, a
        `rating`/`favorite` signal, and/or a `default_for` alias ("default dog" → this asset). Returns
        False if the id is unknown."""
        if self.get(id) is None:
            return False
        fields: dict[str, Any] = {}
        if note is not None:
            fields["notes"] = note
        if tags is not None:
            fields["tags"] = tags
        if rating is not None:
            fields["rating"] = rating
        if favorite is not None:
            fields["favorite"] = 1 if favorite else 0
        if fields:
            self.upsert(id, **fields)
        if default_for:
            self.set_alias(default_for, id)
        return True

    def set_alias(self, alias: str, asset_id: str) -> None:
        """Pin `alias` (e.g. 'dog') to an asset — an authoritative reuse override."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO aliases (alias, asset_id) VALUES (?,?)",
                             (normalize(alias), asset_id))
            self._db.commit()

    def resolve_alias(self, text: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT asset_id FROM aliases WHERE alias=?",
                                   (normalize(text),)).fetchone()
        return row["asset_id"] if row else None

    def touch(self, id: str) -> None:
        """Record a reuse: bump last_used (powers future LRU eviction) and use_count."""
        with self._lock:
            self._db.execute("UPDATE assets SET last_used=?, use_count=use_count+1 WHERE id=?",
                             (time.time(), id))
            self._db.commit()

    def add_relation(self, from_id: str, to_id: str, type: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO relations (from_id, to_id, type) VALUES (?,?,?)",
                             (from_id, to_id, type))
            self._db.commit()

    # ---- vectors (Phase 1) --------------------------------------------------------------------
    @property
    def has_vectors(self) -> bool:
        return self._vec

    def _ensure_vec_table(self, dim: int) -> None:
        """Create assets_vec at `dim` on first use. If a different-dim table exists (the model/space
        changed — an `embed_model` swap), drop & recreate; the corpus then needs re-embedding. Caller
        holds the lock."""
        row = self._db.execute("SELECT dim FROM vec_meta LIMIT 1").fetchone()
        cur = row["dim"] if row else None
        if cur == dim:
            return
        if cur is not None:
            self._db.execute("DROP TABLE IF EXISTS assets_vec")
            self._db.execute("DELETE FROM vec_meta")
        self._db.execute(
            f"CREATE VIRTUAL TABLE assets_vec USING vec0(asset_id TEXT PRIMARY KEY, kind TEXT, "
            f"embedding float[{dim}])")
        self._db.execute("INSERT INTO vec_meta (dim) VALUES (?)", (dim,))

    def add_embedding(self, id: str, vector: list[float], model: str) -> None:
        """Store an asset's embedding (expects an already-normalized vector) and record its space
        (`embed_model`/`embed_dim`). No-op if sqlite-vec isn't available or the vector is empty."""
        if not self._vec or not vector:
            return
        with self._lock:
            self._ensure_vec_table(len(vector))
            krow = self._db.execute("SELECT kind FROM assets WHERE id=?", (id,)).fetchone()
            kind = krow["kind"] if krow else None
            self._db.execute("DELETE FROM assets_vec WHERE asset_id=?", (id,))
            self._db.execute("INSERT INTO assets_vec (asset_id, kind, embedding) VALUES (?,?,?)",
                             (id, kind, serialize_float32(vector)))
            self._db.execute("UPDATE assets SET embed_model=?, embed_dim=? WHERE id=?",
                             (model, len(vector), id))
            self._db.commit()

    def vector_search(self, query_vec: list[float], *, kind: Optional[str] = None,
                      scope: Optional[str] = None, limit: int = 20) -> list[dict]:
        """KNN over the embedding space; each result carries `distance` (L2 on unit vectors → cosine
        order) and `match="vector"`. Empty list if vectors aren't available/populated. The reuse-tier
        layer (Phase 2) maps distance → strong/weak/none. If `scope` is given, results are limited to
        the caller's own scope ∪ `public=1` (visibility lives on the row, not the vector index, so we
        over-fetch KNN and filter — specs/agents.md §2.2)."""
        if not self._vec or not query_vec:
            return []
        k = limit if scope is None else limit * 4       # over-fetch so private rows don't crowd out visible ones
        with self._lock:
            if self._db.execute("SELECT dim FROM vec_meta LIMIT 1").fetchone() is None:
                return []
            q = ("SELECT asset_id AS id, distance FROM assets_vec WHERE embedding MATCH ? "
                 + ("AND kind=? " if kind else "") + "ORDER BY distance LIMIT ?")
            args: list[Any] = [serialize_float32(query_vec)] + ([kind] if kind else []) + [k]
            hits = self._db.execute(q, args).fetchall()
            out = []
            for h in hits:
                a = self._db.execute("SELECT * FROM assets WHERE id=?", (h["id"],)).fetchone()
                if a:
                    if scope is not None and agent_of(a["scope"] or "") != agent_of(scope):
                        continue                        # different agent — hard wall (even if public)
                    if scope is not None and a["scope"] != scope and not a["public"]:
                        continue                        # same agent, another user's private — not visible
                    d = dict(a)
                    d["distance"] = h["distance"]
                    d["match"] = "vector"
                    out.append(d)
                    if len(out) >= limit:
                        break
        return out

    # ---- reads --------------------------------------------------------------------------------
    def get(self, id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    # -- admin (shell dir/delete; scoped by owning user = the first scope segment) -----------------
    def list_users(self) -> list[str]:
        """Every user owning at least one asset — the `<user>` prefix of each distinct scope."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT scope FROM assets WHERE scope IS NOT NULL").fetchall()
        return sorted({(r[0] or "").split("/", 1)[0] for r in rows} - {""})

    def by_user(self, user: str, *, limit: int = 200) -> list[dict]:
        """Assets owned by `user` (scope == user or `user/…`), most-recently-used first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM assets WHERE scope = ? OR scope LIKE ? "
                "ORDER BY last_used DESC LIMIT ?", (user, f"{user}/%", limit)).fetchall()
        return [dict(r) for r in rows]

    def count_by_user(self, user: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM assets WHERE scope = ? OR scope LIKE ?",
                                    (user, f"{user}/%")).fetchone()[0]

    def delete_by_user(self, user: str) -> int:
        """Remove every asset owned by `user` (row + FTS + aliases + relations + vector). Returns the
        count. Cache bytes are left (regenerable), matching single-asset `delete`."""
        ids = [r["id"] for r in self.by_user(user, limit=1_000_000)]
        for i in ids:
            self.delete(i)                                     # acquires the lock itself
        return len(ids)

    def assets_missing_embedding(self, kind: Optional[str] = None) -> list[dict]:
        """Assets with no vector yet (e.g. everything backfilled before embeddings existed). Used by
        the reindex pass to embed the existing catalog."""
        with self._lock:
            q = "SELECT * FROM assets WHERE embed_model IS NULL"
            args: list[Any] = []
            if kind:
                q += " AND kind=?"
                args.append(kind)
            return [dict(r) for r in self._db.execute(q, args).fetchall()]

    def assets_missing_caption(self, kinds) -> list[dict]:
        """Visual assets with no label (the bare backfilled images) — targets for the caption pass."""
        ph = ",".join("?" for _ in kinds)
        with self._lock:
            q = (f"SELECT * FROM assets WHERE (label IS NULL OR label='') AND kind IN ({ph})")
            return [dict(r) for r in self._db.execute(q, list(kinds)).fetchall()]

    def embedded_nonvisual(self, visual_kinds) -> list[dict]:
        """Embedded assets whose kind is NOT visual — i.e. vectors that don't belong in the (image)
        index. Text-derived vectors (e.g. model titles) sit at a different similarity scale and would
        dominate a text query, so they're cleared out (see plan §1)."""
        ph = ",".join("?" for _ in visual_kinds)
        with self._lock:
            q = f"SELECT id, kind FROM assets WHERE embed_model IS NOT NULL AND kind NOT IN ({ph})"
            return [dict(r) for r in self._db.execute(q, list(visual_kinds)).fetchall()]

    def retag_skyboxes(self, min_aspect: float = 1.9) -> int:
        """Backfill cleanup: an early backfill couldn't tell a skybox `.png` from a regular image, so
        all became kind='image'. Wide images (aspect ≥ min_aspect — equirectangular panoramas, e.g.
        the 21:9 skybox output) are almost certainly skyboxes; re-tag them kind='skybox' and fix the
        vector's `kind` metadata in place (the embedding is unchanged) so kind-filtered search finds
        them. Returns the count re-tagged."""
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM assets WHERE kind='image' AND width IS NOT NULL AND height > 0 "
                "AND (CAST(width AS REAL) / height) >= ?", (min_aspect,)).fetchall()]
            for id in ids:
                self._db.execute("UPDATE assets SET kind='skybox' WHERE id=?", (id,))
                if self._vec:
                    try:
                        self._db.execute("UPDATE assets_vec SET kind='skybox' WHERE asset_id=?", (id,))
                    except sqlite3.OperationalError:
                        pass                       # no vector for this asset yet — nothing to fix
            self._db.commit()
        return len(ids)

    def set_kind(self, id: str, kind: str) -> None:
        """Change an asset's kind, keeping the vector index's kind metadata in sync (so kind-filtered
        vector search stays correct — same dual-write as retag_skyboxes)."""
        with self._lock:
            self._db.execute("UPDATE assets SET kind=? WHERE id=?", (kind, id))
            if self._vec:
                try:
                    self._db.execute("UPDATE assets_vec SET kind=? WHERE asset_id=?", (kind, id))
                except sqlite3.OperationalError:
                    pass
            self._db.commit()

    def update(self, id: str, *, scope: Optional[str] = None, kind: Optional[str] = None,
               default_for: Optional[str] = None, reject_for: Optional[str] = None,
               favorite: Optional[bool] = None, public: Optional[bool] = None,
               **fields: Any) -> tuple[bool, Optional[str]]:
        """The single invariant-preserving mutator (subsumes the old correct_asset/annotate_asset). Sets
        scalar fields (label/query/tags/notes/rating via upsert → FTS synced), `kind` (→ vector kind
        synced), `public` (catalog visibility: others' reads see your public assets — co-location §8a),
        a `default_for` alias, and/or a `reject_for` exclusion. If `scope` is given, the asset must be in
        it (you can only curate your OWN assets). Returns (ok, error)."""
        rec = self.get(id)
        if rec is None:
            return False, f"no asset {id!r}"
        if scope is not None and rec.get("scope") != scope:
            return False, f"asset {id!r} is not in your scope"
        sets = {k: v for k, v in fields.items() if k in _UPSERT_COLS and v is not None}
        if favorite is not None:
            sets["favorite"] = 1 if favorite else 0
        if public is not None:
            sets["public"] = 1 if public else 0
        if sets:
            self.upsert(id, **sets)
        if kind is not None:
            self.set_kind(id, kind)
        if default_for:
            self.set_alias(default_for, id)
        if reject_for:
            self.reject(id, reject_for)
        return True, None

    def delete(self, id: str, *, scope: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """Remove an asset from the catalog: its row, FTS entry, aliases, relations, and vector. (Bytes
        in the cache are left — regenerable, and may still be referenced by a placed entity.) Scope-
        enforced. Returns (ok, error)."""
        rec = self.get(id)
        if rec is None:
            return False, f"no asset {id!r}"
        if scope is not None and rec.get("scope") != scope:
            return False, f"asset {id!r} is not in your scope"
        with self._lock:
            self._db.execute("DELETE FROM assets WHERE id=?", (id,))
            self._db.execute("DELETE FROM assets_fts WHERE id=?", (id,))
            self._db.execute("DELETE FROM aliases WHERE asset_id=?", (id,))
            self._db.execute("DELETE FROM relations WHERE from_id=? OR to_id=?", (id, id))
            if self._vec:
                try:
                    self._db.execute("DELETE FROM assets_vec WHERE asset_id=?", (id,))
                except sqlite3.OperationalError:
                    pass
            self._db.commit()
        return True, None

    def query(self, sql: str, *, scope: str, limit: int = 200) -> list[dict]:
        """Read-only SQL over the catalog, **scoped to `scope` ∪ `public=1`**: runs on a fresh read-only
        connection where `assets` is a temp view of the caller's own rows plus every world-readable row
        (a friend on the same server discovers your public assets — specs/agents.md §2.2), with
        SELECT/PRAGMA-only + single-statement validation. Raises ValueError on a disallowed query."""
        s = sql.strip().rstrip(";").strip()
        low = s.lower()
        if not (low.startswith("select") or low.startswith("pragma")):
            raise ValueError("only SELECT / PRAGMA queries are allowed")
        if ";" in s:
            raise ValueError("only a single statement is allowed")
        if re.search(r"\b(attach|detach)\b|\bmain\s*\.|\btemp\s*\.|pragma\s+\w+\s*=", low):
            raise ValueError("disallowed (no ATTACH, schema-qualified tables, or PRAGMA writes)")
        if not re.fullmatch(r"[\w/.-]+", scope or ""):     # scope is inlined into the view → sanitize
            raise ValueError("bad scope")
        ro = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        try:
            # Hard agent wall: own scope ∪ public, but public only within the SAME agent segment
            # (scope + agent are sanitized to [\w/.-]+ above → safe to inline; no GLOB metachars).
            ro.execute(f"CREATE TEMP VIEW assets AS SELECT * FROM main.assets "
                       f"WHERE scope = '{scope}' OR (public = 1 AND scope GLOB '*/agents/{agent_of(scope)}')")
            return [dict(r) for r in ro.execute(s).fetchmany(limit)]
        finally:
            ro.close()

    def clear_embedding(self, id: str) -> None:
        """Remove an asset's vector and forget its space (so reindex won't think it's embedded)."""
        with self._lock:
            if self._vec:
                try:
                    self._db.execute("DELETE FROM assets_vec WHERE asset_id=?", (id,))
                except sqlite3.OperationalError:
                    pass                       # assets_vec not created yet — nothing to delete
            self._db.execute("UPDATE assets SET embed_model=NULL, embed_dim=NULL WHERE id=?", (id,))
            self._db.commit()

    def search(self, text: Optional[str] = None, *, kind: Optional[str] = None,
               scope: Optional[str] = None, limit: int = 20) -> list[dict]:
        """Phase-0 staged lookup: a user **alias** override first, then exact intent match, then FTS5
        keyword match (label/prompt/query/notes/tags); recency-ordered. (The confidence-tier + vector
        stages layer on top in Phases 1–2.) Each result carries a `match` label of how it was found.
        If `scope` is given, results are limited to the caller's own scope ∪ `public=1` rows (a friend
        on the same server discovers your public assets, not your private ones — specs/agents.md §2.2)."""
        results: list[dict] = []
        seen: set[str] = set()
        # Hard agent wall: only assets whose scope has the SAME agent segment, and within that, own
        # scope ∪ public (cross-user). `*/agents/<agent>` GLOB keeps public from crossing agents. The
        # `a.`-prefixed variant is for the join queries. Both consume [scope, agent-glob] from scv.
        sc = " AND (scope=? OR (public=1 AND scope GLOB ?))" if scope else ""
        sca = " AND (a.scope=? OR (a.public=1 AND a.scope GLOB ?))" if scope else ""
        scv: list[Any] = [scope, "*/agents/" + agent_of(scope)] if scope else []

        def add(rows, how: str) -> None:
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                d = dict(r)
                d["match"] = how
                results.append(d)

        with self._lock:
            if not text:
                q = "SELECT * FROM assets WHERE 1=1" + (" AND kind=?" if kind else "") + sc
                args: list[Any] = ([kind] if kind else []) + scv
                q += " ORDER BY last_used DESC LIMIT ?"
                args.append(limit)
                add(self._db.execute(q, args).fetchall(), "recent")
                return results

            norm = normalize(text)
            # 1. alias override — "dog" → the pinned asset, ahead of everything else.
            aargs: list[Any] = [norm] + ([kind] if kind else []) + scv
            alias_rows = self._db.execute(
                "SELECT a.* FROM aliases al JOIN assets a ON a.id=al.asset_id WHERE al.alias=?"
                + (" AND a.kind=?" if kind else "") + sca,
                aargs,
            ).fetchall()
            add(alias_rows, "alias")

            # 2. exact intent match.
            q = ("SELECT * FROM assets WHERE (lower(trim(label))=? OR lower(trim(prompt))=? "
                 "OR lower(trim(query))=?)")
            args = [norm, norm, norm]
            if kind:
                q += " AND kind=?"
                args.append(kind)
            q += sc
            args += scv
            add(self._db.execute(q + " ORDER BY favorite DESC, last_used DESC", args).fetchall(), "exact")

            # 3. FTS keyword match (now covers notes/tags too).
            match = _fts_query(text)
            if match:
                fq = ("SELECT a.* FROM assets_fts f JOIN assets a ON a.id=f.id "
                      "WHERE assets_fts MATCH ?")
                fargs: list[Any] = [match]
                if kind:
                    fq += " AND a.kind=?"
                    fargs.append(kind)
                fq += sca
                fargs += scv
                fq += " ORDER BY rank LIMIT ?"
                fargs.append(limit)
                try:
                    add(self._db.execute(fq, fargs).fetchall(), "fts")
                except sqlite3.OperationalError:
                    pass  # malformed MATCH — earlier results still stand
        return results[:limit]

    def reject(self, asset_id: str, query: str) -> None:
        """Record that `asset_id` is the WRONG answer for `query` (e.g. the X-wing for 'starship
        enterprise') — a `reject` relation that `find()` filters out of future matches for it."""
        self.add_relation(asset_id, normalize(query), "reject")

    def find(self, text: Optional[str] = None, *, query_vec: Optional[list[float]] = None,
             kind: Optional[str] = None, scope: Optional[str] = None, limit: int = 20) -> dict:
        """The director-facing reuse query: staged match (alias → exact → FTS → vector), rejects
        filtered out, with a server-computed **confidence tier** so the LLM never thresholds a raw
        score. Returns {"candidates": [...], "confidence_tier": "strong"|"weak"|"none"}.

        - strong: an authoritative hit (a user alias, or an exact intent match) — safe to reuse.
        - weak:   only fuzzy hits (keyword/semantic) — offer, don't assume.
        - none:   nothing — generate/fetch fresh.
        """
        cands = self.search(text, kind=kind, scope=scope, limit=limit) if text else []
        seen = {c["id"] for c in cands}
        if query_vec:
            for v in self.vector_search(query_vec, kind=kind, scope=scope, limit=limit):
                if v["id"] not in seen:
                    seen.add(v["id"])
                    cands.append(v)
        if text:                                  # drop assets the user rejected for this query
            norm = normalize(text)
            with self._lock:
                rejected = {r["from_id"] for r in self._db.execute(
                    "SELECT from_id FROM relations WHERE to_id=? AND type='reject'", (norm,)).fetchall()}
            cands = [c for c in cands if c["id"] not in rejected]
        cands = cands[:limit]
        strong = any(c["match"] in ("alias", "exact") for c in cands)
        tier = "strong" if strong else ("weak" if cands else "none")
        return {"candidates": cands, "confidence_tier": tier}
