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

Vector search (sqlite-vec) + embeddings arrive in Phase 1; this module is metadata + FTS5 only.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Bump when the schema changes. The cache catalog is regenerable (backfill rebuilds it from the bytes
# on disk), so on a version mismatch we drop & recreate rather than migrate in place.
_SCHEMA_VERSION = 2

# faces/persons are reserved now (sub-image entities + named clusters) so the NAS seam is honest;
# they stay empty until the NAS ingestion subsystem (Phase 5) populates them.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id TEXT PRIMARY KEY,
  kind TEXT,                       -- image | model | skybox | grounded_skybox | audio | photo | …
  source TEXT,                     -- cache://<id> | nas://<path> | https://…
  filename TEXT,                   -- physical name under .cache/assets (NULL for external sources)
  label TEXT,                      -- machine display name: prompt (images) or title/query (models)
  prompt TEXT,
  query TEXT,
  params_json TEXT,                -- output-affecting params (op, transparent, size, …) — match key
  provider TEXT,
  model TEXT,
  width INTEGER, height INTEGER,   -- common visual dims (NULL for non-visual kinds)
  licence TEXT, attribution TEXT, creator TEXT,
  attributes TEXT,                 -- kind-specific JSON: {tris,bbox}·model {transparent}·image {bpm,key}·audio
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
"""

_TABLES = ("assets_fts", "assets", "aliases", "relations", "faces", "persons")

# Columns a caller may set via upsert kwargs (everything except id, attributes, and the lifecycle
# bookkeeping). `attributes` is handled separately so it can be *merged* rather than replaced.
_UPSERT_COLS = (
    "kind", "source", "filename", "label", "prompt", "query", "params_json",
    "provider", "model", "width", "height", "licence", "attribution", "creator",
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
        ver = self._db.execute("PRAGMA user_version").fetchone()[0]
        if ver != _SCHEMA_VERSION:                       # fresh OR stale schema → (re)build from disk
            for t in _TABLES:
                self._db.execute(f"DROP TABLE IF EXISTS {t}")
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._db.commit()

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

    # ---- reads --------------------------------------------------------------------------------
    def get(self, id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM assets WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    def search(self, text: Optional[str] = None, *, kind: Optional[str] = None,
               limit: int = 20) -> list[dict]:
        """Phase-0 staged lookup: a user **alias** override first, then exact intent match, then FTS5
        keyword match (label/prompt/query/notes/tags); recency-ordered. (The confidence-tier + vector
        stages layer on top in Phases 1–2.) Each result carries a `match` label of how it was found."""
        results: list[dict] = []
        seen: set[str] = set()

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
                q, args = "SELECT * FROM assets", []
                if kind:
                    q += " WHERE kind=?"
                    args.append(kind)
                q += " ORDER BY last_used DESC LIMIT ?"
                args.append(limit)
                add(self._db.execute(q, args).fetchall(), "recent")
                return results

            norm = normalize(text)
            # 1. alias override — "dog" → the pinned asset, ahead of everything else.
            alias_rows = self._db.execute(
                "SELECT a.* FROM aliases al JOIN assets a ON a.id=al.asset_id WHERE al.alias=?"
                + (" AND a.kind=?" if kind else ""),
                (norm, kind) if kind else (norm,),
            ).fetchall()
            add(alias_rows, "alias")

            # 2. exact intent match.
            q = ("SELECT * FROM assets WHERE (lower(trim(label))=? OR lower(trim(prompt))=? "
                 "OR lower(trim(query))=?)")
            args = [norm, norm, norm]
            if kind:
                q += " AND kind=?"
                args.append(kind)
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
                fq += " ORDER BY rank LIMIT ?"
                fargs.append(limit)
                try:
                    add(self._db.execute(fq, fargs).fetchall(), "fts")
                except sqlite3.OperationalError:
                    pass  # malformed MATCH — earlier results still stand
        return results[:limit]

    # ---- one-time migration -------------------------------------------------------------------
    def backfill(self, asset_cache: str | Path, world_doc: Optional[dict] = None) -> int:
        """Seed the catalog from the existing on-disk cache (+ the world doc, to recover prompts for
        already-placed images). Idempotent: skips ids already present. Returns rows added."""
        asset_cache = Path(asset_cache)
        added = 0
        if asset_cache.is_dir():
            for p in sorted(asset_cache.iterdir()):
                if not p.is_file() or self.get(p.name):
                    continue
                ext = p.suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    w, h = _image_dims(p)
                    self.upsert(p.name, kind="image", source=f"cache://{p.name}",
                                filename=p.name, width=w, height=h)
                    added += 1
                elif ext == ".glb":
                    m = _read_sidecar(p.with_suffix(".json"))
                    self.upsert(p.name, kind="model", source=f"cache://{p.name}", filename=p.name,
                                label=m.get("title"), query=m.get("title"),
                                attributes={"tris": m.get("tris"), "bbox_min": m.get("bbox_min"),
                                            "bbox_max": m.get("bbox_max")},
                                licence=m.get("licence"), attribution=m.get("attribution"),
                                creator=m.get("creator"))
                    added += 1
        for ent in (world_doc or {}).get("entities", []):
            meta = ent.get("meta") or {}
            iid, prompt = meta.get("image_id"), meta.get("prompt")
            if iid and prompt and self.get(iid):
                self.upsert(iid, label=prompt, prompt=prompt)
        return added


def _image_dims(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None


def _read_sidecar(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
