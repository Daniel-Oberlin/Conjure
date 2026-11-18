"""Asset pipeline — resolve a text query to a cached, locally-served glTF model.

Phase 3 (decision #4): Poly Pizza — free, low-poly **GLB**, CC-licensed, keyword search. A model
is cached content-addressed and served by the world server at ``/assets/<hash>.glb`` so the headset
loads it over the same connection. License + attribution are captured per asset (architecture.md §10
asset descriptor; spec §11 licensing).

**Where the bytes come from** (docs/specs/library.md §3a). Since November 2026 Poly Pizza's file host sits
behind Cloudflare's Bot Fight Mode, which on their free tier cannot exempt API users — the owner said so
(2026-11-17) — so automated downloads are refused, and the search endpoint fails on many queries too.
Models are therefore downloaded BY HAND into a local collection (`LocalCollection`: loose GLBs and
GLB-pack zips, never imported wholesale) and resolved from there first. Poly Pizza's search is kept as a
synonym bridge onto that collection; downloading is the last resort and is expected to fail.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

SEARCH_URL = "https://api.poly.pizza/v1.1/search/{query}"


@dataclass
class AssetRecord:
    hash: str
    title: str
    attribution: str
    licence: str
    creator: str
    tris: int
    source_url: str
    bbox_min: list[float] | None = None  # axis-aligned bounds (model units) for auto-scaling
    bbox_max: list[float] | None = None


def _bounding_box(glb_path: Path) -> tuple[list[float], list[float]] | None:
    """Axis-aligned bounding box of a GLB (node transforms applied), or None if unavailable."""
    try:
        import trimesh

        mesh = trimesh.load(glb_path, force="scene")
        bounds = getattr(mesh, "bounds", None)
        if bounds is None:
            return None
        return [float(v) for v in bounds[0]], [float(v) for v in bounds[1]]
    except Exception:
        return None


_STOP = {"a", "an", "the", "of", "some", "with", "and"}
_PACK_SUFFIX = re.compile(r"^(?P<title>.+)-(?P<id>[A-Za-z0-9]{10})$")   # "Bird-h5IzAUdltz": a duplicate title


def _tokens(text: str) -> frozenset[str]:
    """Lower-cased words, stop words dropped, a plain plural folded ("cows" → "cow", not "glass")."""
    out = set()
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.add(t)
    return frozenset(out)


@dataclass(frozen=True)
class LocalModel:
    """One model in the local collection: a loose `.glb`, or a `.glb` member of a pack zip."""
    title: str
    path: Path                      # the file on disk (the .glb, or the zip holding it)
    member: str | None = None       # the zip member, for a pack model
    creator: str = ""               # only loose files carry one ("<title> by <creator> - <id>.glb")
    pp_id: str = ""                 # Poly Pizza model id, when the file name carries it
    pack: str = ""                  # the pack's name, for a pack model
    mtime_ns: int = 0               # of `path`: a file replaced under the same name is a different model

    @property
    def key(self) -> str:
        """Identity for the cache index — the same model is extracted once, and re-extracted only if the
        file it came from is replaced."""
        return f"local:{self.path.name}@{self.mtime_ns}" + (f"#{self.member}" if self.member else "")

    @property
    def cid(self) -> str:
        """The id the director sees and passes back to place it (`polypizza:<file>[#<member>]`)."""
        return f"polypizza:{self.path.name}" + (f"#{self.member}" if self.member else "")

    @property
    def link(self) -> str:
        return f"https://poly.pizza/m/{self.pp_id}" if self.pp_id else ""

    def attribution(self) -> str:
        """What can honestly be said from the file name alone. A pack file names neither its creator
        nor its licence, so the pack is the credit and the licence stays blank rather than guessed."""
        who = f" by {self.creator}" if self.creator else ""
        where = f", {self.link}" if self.link else ""
        pack = f" from the {self.pack} pack on Poly Pizza" if self.pack else ""
        return f'"{self.title}"{who}{pack}{where}'


class LocalCollection:
    """Poly Pizza models downloaded by hand into one folder, used IN PLACE — nothing is imported until it
    is placed, and then only that one model is extracted into the asset cache.

    The folder holds loose `.glb` files named as Poly Pizza names its downloads
    (``Tree by Quaternius - 2paAm1ja4w.glb``) and pack zips (``Animal Kit.undefined-glb.zip``) whose members
    are named by title alone (``Beagle.glb``; a repeated title gets a 10-character id suffix). Anything
    else — an OBJ-only zip, a stray file — is ignored. The folder is rescanned whenever its listing
    changes, so models added later need no restart."""

    def __init__(self, root: Path, rng: random.Random | None = None):
        self.root = Path(root).expanduser()
        self._stamp: tuple | None = None
        self._models: list[LocalModel] = []
        self._rng = rng or random.Random()

    def _listing(self) -> tuple:
        try:
            return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                                for p in self.root.iterdir() if p.is_file()))
        except OSError:
            return ()

    def models(self) -> list[LocalModel]:
        stamp = self._listing()
        if stamp != self._stamp:
            self._stamp = stamp
            self._models = self._scan()
        return self._models

    def _scan(self) -> list[LocalModel]:
        out: list[LocalModel] = []
        for name, mtime_ns, _sz in self._stamp or ():
            path = self.root / name
            low = name.lower()
            if low.endswith(".glb"):
                stem = path.stem
                title, creator, pp_id = stem, "", ""
                if " - " in stem:                                    # "<title> by <creator> - <id>"
                    head, pp_id = stem.rsplit(" - ", 1)
                    title, _, creator = head.partition(" by ")
                out.append(LocalModel(title=title.strip(), path=path, creator=creator.strip(),
                                      pp_id=pp_id.strip(), mtime_ns=mtime_ns))
            elif low.endswith(".zip"):
                try:
                    with zipfile.ZipFile(path) as zf:
                        members = [m for m in zf.namelist() if m.lower().endswith(".glb") and not m.endswith("/")]
                except (OSError, zipfile.BadZipFile):
                    continue
                pack = re.sub(r"(\.undefined)?-glb$", "", path.stem)
                for m in members:
                    stem = Path(m).stem
                    mm = _PACK_SUFFIX.match(stem)
                    title, pp_id = (mm["title"], mm["id"]) if mm else (stem, "")
                    out.append(LocalModel(title=title.strip(), path=path, member=m, pp_id=pp_id, pack=pack,
                                          mtime_ns=mtime_ns))
        return out

    def search(self, query: str, limit: int | None = None) -> list[LocalModel]:
        """Name matches for `query`, best first. Exact title beats the query within a title ("couch" →
        "Couch Small") beats a title within the query ("a big brown cow" → "Cow"); among partial matches,
        the one sharing more words wins ("a red sports car" → "Sports Car", not "Car"), then the shorter
        title. Equally good matches — four packs each have a "Cow" — come in a RANDOM order, fresh each
        call, so asking twice can give two different cows."""
        q = _tokens(query)
        if not q:
            return []
        ranked: list[tuple[tuple, float, LocalModel]] = []
        for m in self.models():
            t = _tokens(m.title)
            if not t:
                continue
            score = 3 if t == q else 2 if q <= t else 1 if t <= q else 0
            if score:
                ranked.append(((score, len(t & q), -len(t)), self._rng.random(), m))
        ranked.sort(key=lambda r: (r[0], r[1]), reverse=True)
        out = [m for _rank, _r, m in ranked]
        return out[:limit] if limit else out

    def find(self, query: str) -> LocalModel | None:
        """The best name match (a random one among equals), or None."""
        hits = self.search(query, limit=1)
        return hits[0] if hits else None

    @staticmethod
    def exact(query: str, m: LocalModel) -> bool:
        return _tokens(query) == _tokens(m.title)

    def get(self, cid: str) -> LocalModel | None:
        return next((m for m in self.models() if m.cid == cid), None)

    def packs(self, query: str) -> list[dict]:
        """Packs whose NAME matches the query ("animals" → Animals, Animal Kit, …), each with its size and
        titles — what answers "what animals do I have?", which no single model's name does."""
        q = _tokens(query)
        groups: dict[str, list[str]] = {}
        for m in self.models():
            if m.pack:
                groups.setdefault(m.pack, []).append(m.title)
        return [{"pack": p, "count": len(ts), "titles": sorted(set(ts))}
                for p, ts in sorted(groups.items()) if q and q & _tokens(p)]

    def match_result(self, item: dict) -> LocalModel | None:
        """The local copy of a Poly Pizza search result, by its id, else by exactly its title."""
        pid, title = item.get("ID") or "", _tokens(item.get("Title") or "")
        for m in self.models():
            if pid and m.pp_id == pid:
                return m
        same = [m for m in self.models() if title and _tokens(m.title) == title]
        return self._rng.choice(same) if same else None

    def read(self, m: LocalModel) -> bytes:
        if m.member is None:
            return m.path.read_bytes()
        with zipfile.ZipFile(m.path) as zf:
            return zf.read(m.member)


class AssetResolver:
    """Resolves a query to a cached GLB: the local collection first, Poly Pizza's search as a bridge onto
    it, and a Poly Pizza download as the last resort (see the module docstring for why)."""

    def __init__(self, api_key: str | None, cache_dir: Path, local_dir: Path | None = None):
        self._key = api_key
        self.local = LocalCollection(local_dir) if local_dir else None
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.json"
        self._index: dict[str, str] = (
            json.loads(self._index_path.read_text()) if self._index_path.exists() else {}
        )
        # The sidecars are the record and the index only a shortcut to it: rebuild what the index lost.
        # On 2026-11-03 it held 4 of 27 downloads, so a Beagle already on disk was fetched again — and
        # Poly Pizza, now refusing automated downloads (403), made a cached model unplaceable.
        for meta in self._dir.glob("*.json"):
            if meta == self._index_path:
                continue
            try:
                rec = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("source_url") and rec.get("hash") == meta.stem \
                    and self.path_for(meta.stem).exists():
                self._index.setdefault(rec["source_url"], meta.stem)

    def path_for(self, asset_hash: str) -> Path:
        return self._dir / f"{asset_hash}.glb"

    def _meta_for(self, asset_hash: str) -> Path:
        return self._dir / f"{asset_hash}.json"

    def _cached(self, key: str) -> AssetRecord | None:
        h = self._index.get(key)
        if (h and self.path_for(h).exists() and self._meta_for(h).exists()
                and self.path_for(h).open("rb").read(4) == b"glTF"):
            return AssetRecord(**json.loads(self._meta_for(h).read_text()))
        return None

    def _store(self, blob: bytes, key: str, *, title: str, attribution: str, licence: str, creator: str,
               tris: int, source_url: str) -> AssetRecord:
        asset_hash = hashlib.sha256(blob).hexdigest()[:16]
        self.path_for(asset_hash).write_bytes(blob)
        bbox = _bounding_box(self.path_for(asset_hash))
        record = AssetRecord(hash=asset_hash, title=title, attribution=attribution, licence=licence,
                             creator=creator, tris=tris, source_url=source_url,
                             bbox_min=bbox[0] if bbox else None, bbox_max=bbox[1] if bbox else None)
        self._meta_for(asset_hash).write_text(json.dumps(asdict(record)))
        self._index[key] = asset_hash
        self._index_path.write_text(json.dumps(self._index))
        return record

    def _from_local(self, m: LocalModel, item: dict | None = None) -> AssetRecord:
        """Extract (once) and record a local model. `item` — the Poly Pizza search result it was reached
        through, when there was one — supplies the real credit and licence the file name cannot."""
        hit = self._cached(m.key)
        if hit:
            return hit
        blob = self.local.read(m)
        if blob[:4] != b"glTF":
            raise RuntimeError(f"{m.key} is not a GLB")
        if item:
            return self._store(blob, m.key, title=item.get("Title") or m.title,
                               attribution=item.get("Attribution") or m.attribution(),
                               licence=item.get("Licence") or "",
                               creator=(item.get("Creator") or {}).get("Username", "") or m.creator,
                               tris=int(item.get("Tri Count") or 0),
                               source_url=m.link or item.get("Download") or m.key)
        return self._store(blob, m.key, title=m.title, attribution=m.attribution(), licence="",
                           creator=m.creator, tris=0, source_url=m.link or m.key)

    def held(self, m: LocalModel) -> str | None:
        """The cache hash a collection model was already extracted to, if it was."""
        h = self._index.get(m.key)
        return h if h and self.path_for(h).exists() else None

    def local_record(self, cid: str) -> AssetRecord | None:
        """Extract (once) and record the collection model with this id — the director picked it from
        search_library. None if no such model (the file was removed since the search)."""
        m = self.local.get(cid) if self.local else None
        return self._from_local(m) if m else None

    def _no_model(self, query: str, why: str) -> RuntimeError:
        n = len(self.local.models()) if self.local else 0
        where = (f"nothing called {query!r} among the {n} models in your Poly Pizza collection"
                 if self.local else "no local Poly Pizza collection is set up")
        return RuntimeError(f"{where}, and {why}")

    async def resolve(self, query: str) -> AssetRecord | None:
        """Return a cached AssetRecord for the best match, or None if nothing was found.

        Order: a name match in the local collection; else Poly Pizza's search, taking the first result
        the collection holds (by id or title) — the synonym bridge, "dog" → a Pug you have; else the top
        result's cached download; else a download. Raises if the download fails or isn't a real GLB (so
        callers report the failure instead of caching/serving an error page).
        """
        if self.local:
            m = self.local.find(query)
            if m:
                return self._from_local(m)
        if not self._key:
            if self.local:
                raise self._no_model(query, "no Poly Pizza API key is set to search further")
            return None
        headers = {"User-Agent": "Conjure/0.1 (+https://github.com/; VR holodeck)"}
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            try:
                resp = await client.get(
                    SEARCH_URL.format(query=query),
                    headers={"x-auth-token": self._key},
                    params={"Limit": 8},
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                if self.local:
                    raise self._no_model(query, f"Poly Pizza's search failed ({exc})") from exc
                raise
            results = resp.json().get("results", [])
            if not results:
                if self.local:
                    raise self._no_model(query, "Poly Pizza's search found nothing either")
                return None
            if self.local:
                for it in results:
                    m = self.local.match_result(it)
                    if m:
                        return self._from_local(m, it)
            item = results[0]  # Poly Pizza returns by relevance
            download_url = item["Download"]

            # Cache hit: same source URL already downloaded (and the cached file is a real GLB —
            # otherwise it's a poisoned entry from a past failed download, so re-fetch).
            hit = self._cached(download_url)
            if hit:
                return hit

            dl = await client.get(download_url, timeout=60.0)
            blob = dl.content if dl.status_code == 200 else b""
            if blob[:4] != b"glTF":  # GLB magic; a 403/HTML error page would slip through otherwise
                why = (f"Poly Pizza refused the download of '{item.get('Title') or query}' "
                       f"(HTTP {dl.status_code}, {dl.headers.get('content-type', '?')}) — it blocks "
                       "automated downloads; download it by hand into the collection")
                if self.local:
                    raise self._no_model(query, why)
                raise RuntimeError(why)

        return self._store(blob, download_url, title=item.get("Title") or query,
                           attribution=item.get("Attribution") or "", licence=item.get("Licence") or "",
                           creator=(item.get("Creator") or {}).get("Username", ""),
                           tris=int(item.get("Tri Count") or 0), source_url=download_url)
