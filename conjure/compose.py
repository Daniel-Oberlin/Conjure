"""Compose a THING into one self-contained GLB — and check that the file is the thing it claims to be.

A container is a FILE, one artist's export; a *thing* is a scene entity SUBTREE. The two disagree in
both directions — `office-babe` draws from three containers and `TOOLS LIBRARYblend5.glb` splits into
fifteen props — so converting containers emits meshes the scene never renders, drops meshes it does,
and loses everything that lives on the entity rather than in the file: position, parent, instancing.
`playcanvas.things` reads the subtree; this writes it out. (`docs/specs/captures.md` § 4.)

**The verifier comes first in this file because it came first in time, and that was the point.** A
composed GLB is mechanically comparable to the `Thing` that described it, so most of a regression is
caught in a second by `verify_thing` rather than by a person opening a viewer. It also fixes the
contract — what provenance a composed file carries, and what is allowed to be in one — before any
geometry is written, which is why the composer below has so little room to be creative.

**What the composer must preserve, stated by the cases that found each one:**

  · a piece's PARENT — Oktoberfest's beer hangs off the bone `DEF-hand.R`, and a merge that reparents
    it to the figure's root drops it on the floor
  · a piece's TRANSFORM, *with rotation* — `WOODout.glb` is meaningless at the origin and the scene
    puts it at `y = -0.1`; 538 of 948 pieces across the twenty captures have a rotation somewhere in
    their chain, so composing position and scale alone is wrong for the majority of them. The whole
    chain is rebuilt as nodes instead of summarised into one transform
  · a piece's INSTANCING — bride's heels are one mesh on two nodes, and flattening loses a shoe
  · the THING's own scale but not its position — `Banana` is a 0.5 wrapper around a 0.9166 mesh and a
    banana that skips the wrapper comes out twice life size, while where this scene happens to stand
    the vase is not a property of the vase

**The skeleton join is the hard part and it is verified, not assumed.** A figure's pieces come from
several containers and must end up skinned to ONE skeleton. Measured on this corpus the donors are
identical by name (181/181 office-babe, 175/175 Oktoberfest) *and* their inverse bind matrices agree
to within 1e-5, so the join is name-keyed. That is a property of these files and not of the format:
`_weldable` re-checks both on every compose, and a donor that fails keeps its own skeleton with a
loud note rather than being welded into a figure that is wrong in a way nothing reports.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .figures import split_glb, write_glb
from .playcanvas import (Build, Thing, _Textures, container_meshes, find_builds,
                         material_from, read_build, things)

#: How far a composed transform may sit from the one the scene states before it is a problem. Generous
#: next to the numbers involved (a bone offset is ~1e-2 and the tolerance is a thousandth of that), and
#: it has to be: the values round-trip through JSON as float32-ish decimals on the way out.
TOL = 1e-4


# ---------------------------------------------------------------- the provenance a composed file carries

#: Every composed GLB carries `extras.conjure` at the top and on each mesh-bearing node. It exists so
#: the verifier can compare the FILE against the `Thing` without guessing which node came from which
#: piece — the comparisons themselves are against the source containers and the scene, never against
#: this. Importers read the `hidden` list; viewers ignore extras entirely.
MARK = "conjure"


def _tag(obj: dict) -> dict:
    return ((obj.get("extras") or {}).get(MARK)) or {}


# ---------------------------------------------------------------- small matrix arithmetic


def quat_from_euler(deg: tuple) -> list[float]:
    """PlayCanvas euler angles (degrees) → `[x, y, z, w]`.

    Transcribed from `pc.Quat.setFromEulerAngles`, which is three.js's **ZYX** order — not the YXZ
    A-Frame uses (`conjure/server.py:_euler_yxz_quat`) and not XYZ. Getting this wrong tilts a prop
    plausibly rather than obviously, which is the kind of error that survives a look in a viewer.
    """
    hx, hy, hz = (math.radians(v) * 0.5 for v in deg)
    sx, cx = math.sin(hx), math.cos(hx)
    sy, cy = math.sin(hy), math.cos(hy)
    sz, cz = math.sin(hz), math.cos(hz)
    return [sx * cy * cz - cx * sy * sz,
            cx * sy * cz + sx * cy * sz,
            cx * cy * sz - sx * sy * cz,
            cx * cy * cz + sx * sy * sz]


def mat_trs(t, q, s) -> list[float]:
    """A row-major 4×4 as a flat 16-list, from translation, quaternion `[x,y,z,w]` and scale."""
    x, y, z, w = q
    r = (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
         2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
         2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y))
    return [r[0] * s[0], r[1] * s[1], r[2] * s[2], t[0],
            r[3] * s[0], r[4] * s[1], r[5] * s[2], t[1],
            r[6] * s[0], r[7] * s[1], r[8] * s[2], t[2],
            0.0, 0.0, 0.0, 1.0]


def mat_mul(a: list[float], b: list[float]) -> list[float]:
    out = [0.0] * 16
    for i in range(4):
        for j in range(4):
            out[i * 4 + j] = sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
    return out


IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def node_matrix(node: dict) -> list[float]:
    """A glTF node's local matrix, from `matrix` (COLUMN-major, per the spec) or from its TRS."""
    if node.get("matrix"):
        m = node["matrix"]
        return [m[0], m[4], m[8], m[12], m[1], m[5], m[9], m[13],
                m[2], m[6], m[10], m[14], m[3], m[7], m[11], m[15]]
    return mat_trs(node.get("translation") or (0.0, 0.0, 0.0),
                   node.get("rotation") or (0.0, 0.0, 0.0, 1.0),
                   node.get("scale") or (1.0, 1.0, 1.0))


def chain_matrix(chain: tuple, *, skip_root_position: bool = True) -> list[float]:
    """The world matrix a piece's entity chain composes to, in the THING's own frame.

    The thing's own POSITION is dropped by default: it is where this scene stands the thing, not a
    property of the thing, and carrying it would emit a vase 2 m from its own origin. Its rotation and
    scale are kept, because those are how big and which way up the thing is.
    """
    out = IDENTITY
    for i, (_name, p, r, s) in enumerate(chain):
        if i == 0 and skip_root_position:
            p = (0.0, 0.0, 0.0)
        out = mat_mul(out, mat_trs(p, quat_from_euler(r), s))
    return out


def _close(a, b, tol: float = TOL) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------------------------------------------------------------- the verifier


def verify_thing(build: Build, thing: Thing, data: bytes) -> list[str]:
    """Everything wrong with a composed GLB, judged against the `Thing` and the source containers.

    Silence is the pass. Each check exists because something can go wrong there that a viewer will not
    show you: a dead twin creeping back in looks like nothing at all, a welded skeleton with one joint
    remapped to the wrong bone looks like a shrug, and an instanced mesh flattened to one node looks
    like a woman wearing one shoe only if you happen to look down.
    """
    bad: list[str] = []
    doc, blob = split_glb(data)
    if not doc:
        return ["not a binary glTF"]
    nodes = doc.get("nodes") or []
    meshes = doc.get("meshes") or []
    materials = doc.get("materials") or []
    head = _tag(doc)

    # --- the file says what it is, and it is this thing
    if head.get("thing") != thing.name:
        bad.append(f"the file says it is {head.get('thing')!r}, not {thing.name!r}")
    scenes = doc.get("scenes") or []
    if len(scenes) != 1 or len(scenes[0].get("nodes") or []) != 1:
        bad.append(f"expected one scene with one root node, found {len(scenes)} scene(s) with "
                   f"{[len(s.get('nodes') or []) for s in scenes]} root(s)")

    # --- one node per piece, no piece missing and nothing extra rendered
    by_piece: dict[int, list[int]] = {}
    for i, node in enumerate(nodes):
        if node.get("mesh") is None:
            continue
        tag = _tag(node)
        if "piece" not in tag:
            bad.append(f"node {i} {node.get('name')!r} draws mesh {node['mesh']} and claims no piece — "
                       f"geometry the thing did not ask for")
            continue
        by_piece.setdefault(int(tag["piece"]), []).append(i)
    only_shown = bool(head.get("shown"))
    for idx, piece in enumerate(thing.pieces):
        if idx not in by_piece:
            if only_shown and (not piece.enabled or piece.shadowed):
                continue                        # a viewing copy leaves these out on purpose
            path = build.path(piece.container)
            why = ("" if path and os.path.exists(path) else
                   " — its container never downloaded, so this is a gap in the CAPTURE and not in "
                   "the compose")
            bad.append(f"piece {idx} ({piece.entity!r}, container {build.name(piece.container)} "
                       f"mesh {piece.mesh}) is not in the file{why}")
        elif len(by_piece[idx]) > 1:
            bad.append(f"piece {idx} ({piece.entity!r}) is drawn by {len(by_piece[idx])} nodes")
    for idx in sorted(set(by_piece) - set(range(len(thing.pieces)))):
        bad.append(f"the file draws a piece {idx}, which the thing does not have")

    # --- parent links, so a world matrix can be computed and a bone parent checked
    parent: dict[int, int] = {}
    for i, node in enumerate(nodes):
        for c in node.get("children") or []:
            parent[c] = i

    def world(i: int) -> list[float]:
        out, seen = IDENTITY, set()
        while i is not None and i not in seen:
            seen.add(i)
            out = mat_mul(node_matrix(nodes[i]), out)
            i = parent.get(i)
        return out

    # --- per piece: the geometry, the materials, the frame it sits in
    counts_by_container: dict[int, list] = {}
    for idx, piece in enumerate(thing.pieces):
        here = by_piece.get(idx) or []
        if not here:
            continue
        node = nodes[here[0]]
        tag = _tag(node)
        if (tag.get("container"), tag.get("mesh")) != (piece.container, piece.mesh):
            bad.append(f"piece {idx} claims container {tag.get('container')} mesh {tag.get('mesh')}, "
                       f"the thing says {piece.container}/{piece.mesh}")
            continue
        if piece.container not in counts_by_container:
            counts_by_container[piece.container] = container_meshes(build, piece.container)
        source = counts_by_container[piece.container]
        prims = (meshes[node["mesh"]].get("primitives") or []) if node["mesh"] < len(meshes) else []
        if piece.mesh < len(source) and source[piece.mesh][1]:
            want = source[piece.mesh][1]
            got = tuple(_vertices(doc, p) for p in prims)
            if got != want:
                bad.append(f"piece {idx} ({piece.entity!r}): vertex counts {got} do not match "
                           f"{build.name(piece.container)} mesh {piece.mesh}, which is {want}")
        # Materials by NAME, because the index is ours and the name is the registry's. A missing one is
        # the whole flat-white failure this pipeline exists to prevent.
        for i, prim in enumerate(prims):
            pc = piece.materials[i] if i < len(piece.materials) else None
            if pc is None:
                continue
            if "material" not in prim:
                bad.append(f"piece {idx} ({piece.entity!r}) primitive {i} has no material; the scene "
                           f"binds {build.name(pc)}")
            elif materials[prim["material"]].get("name") != build.name(pc):
                bad.append(f"piece {idx} ({piece.entity!r}) primitive {i} wears "
                           f"{materials[prim['material']].get('name')!r}, the scene binds "
                           f"{build.name(pc)!r}")
        # Where it sits, compared against the entity CHAIN with its rotations rather than against the
        # flattened position-and-scale summary, which is wrong for 538 of this corpus's 948 pieces.
        # Checked for skinned meshes too: their node transform does not pose them, but it is the frame
        # the scene put them in and a composer that loses it has lost the scene.
        rebound = set(head.get("rebound") or ())
        if rebound & set(piece.path):
            pass            # it hangs off a bone taken from the container; the scene chain is not the answer
        elif not _close(world(here[0]), chain_matrix(piece.chain)):
            bad.append(f"piece {idx} ({piece.entity!r}) is not where the scene puts it: "
                       f"{_short(world(here[0]))} vs {_short(chain_matrix(piece.chain))}")
        if tag.get("skinned") and node.get("skin") is None:
            bad.append(f"piece {idx} ({piece.entity!r}) is recorded as skinned but has no skin")
        # The bone it hangs off. Only checked when the composed file HAS that node — a piece whose
        # parent is an ordinary group has no bone to keep.
        named = {nodes[j].get("name") for j in range(len(nodes))}
        if piece.parent and piece.parent in named:
            up = parent.get(here[0])
            if up is None or nodes[up].get("name") != piece.parent:
                got = repr(nodes[up].get("name")) if up is not None else "nothing"
                bad.append(f"piece {idx} ({piece.entity!r}) hangs off {got}, "
                           f"the scene hangs it off {piece.parent!r}")

    # --- instancing: N uses of one mesh stay N nodes, and ideally one mesh
    uses: dict[tuple, list[int]] = {}
    for idx, piece in enumerate(thing.pieces):
        uses.setdefault((piece.container, piece.mesh, piece.materials), []).append(idx)
    for (cid, mesh, _m), idxs in uses.items():
        if len(idxs) < 2 or any(thing.pieces[i].shadowed or
                                (only_shown and not thing.pieces[i].enabled) for i in idxs):
            continue                            # a variant pair is not an instance — see `_shadow_variants`
        got = [n for i in idxs for n in by_piece.get(i, [])]
        if len(got) != len(idxs):
            bad.append(f"{build.name(cid)} mesh {mesh} is drawn {len(idxs)}× by the scene and "
                       f"{len(got)}× in the file — instancing was flattened")
        elif len({nodes[n]["mesh"] for n in got}) != 1:
            bad.append(f"{build.name(cid)} mesh {mesh} is instanced {len(idxs)}× but the file holds "
                       f"{len({nodes[n]['mesh'] for n in got})} copies of the geometry")

    # --- optional pieces are PRESENT and flagged, live ones are not flagged
    hidden = set(head.get("hidden") or ())
    for idx, piece in enumerate(thing.pieces):
        node = nodes[by_piece[idx][0]] if by_piece.get(idx) else None
        if node is None:
            continue
        flagged = bool(_tag(node).get("optional"))
        if flagged != (not piece.enabled):
            bad.append(f"piece {idx} ({piece.entity!r}) is "
                       f"{'optional' if not piece.enabled else 'live'} in the scene and "
                       f"{'flagged' if flagged else 'not flagged'} in the file")
        if bool(_tag(node).get("variant")) != piece.shadowed:
            bad.append(f"piece {idx} ({piece.entity!r}) is "
                       f"{'a variant' if piece.shadowed else 'the only claim'} and the file says "
                       f"otherwise")
        # A piece is hidden when the scene switched it off OR when it is the losing half of a variant.
        # Both are switchable and both are wrong to draw on load.
        want_hidden = ((not piece.enabled) or piece.shadowed) and not only_shown
        if (node.get("name") in hidden) != want_hidden:
            bad.append(f"piece {idx} ({piece.entity!r}) is "
                       f"{'absent from' if want_hidden else 'in'} the hidden list, wrongly")

    # --- the skeleton, once it has been welded: every joint resolves and names are unique
    for si, skin in enumerate(doc.get("skins") or []):
        for j in skin.get("joints") or []:
            if j >= len(nodes):
                bad.append(f"skin {si} names joint {j}, which is not a node")
        ibm = skin.get("inverseBindMatrices")
        if ibm is not None and len(skin.get("joints") or []) != (doc["accessors"][ibm].get("count")):
            bad.append(f"skin {si} has {len(skin['joints'])} joints and "
                       f"{doc['accessors'][ibm]['count']} inverse bind matrices")
    # Every bone stands where the container that supplied the geometry was bound against it. This is
    # the check that a name-keyed skeleton join actually joined, rather than pointing a donor's weights
    # at a same-named bone in a different place.
    for cid in sorted({p.container for p in thing.pieces}):
        path = build.path(cid)
        if not path or not os.path.exists(path):
            continue
        sdoc, sblob = split_glb(open(path, "rb").read())
        if not sdoc or not (sdoc.get("skins") or []):
            continue
        here: dict = {}
        for i in range(len(nodes)):
            here.setdefault(nodes[i].get("name"), node_matrix(nodes[i]))
        off = joints_agree(_Src(sdoc, sblob), here)
        if off:
            bad.append(f"{len(off)} bone(s) are not where {build.name(cid)} was bound against them "
                       f"({', '.join(off[:4])}) — its meshes are posed against the wrong rest position")

    joint_names = [nodes[j].get("name") for s in (doc.get("skins") or []) for j in s.get("joints") or []]
    dupes = {n for n in joint_names if n and joint_names.count(n) > 1 and
             len({j for s in (doc.get("skins") or []) for j in s["joints"] if nodes[j].get("name") == n}) > 1}
    if dupes:
        bad.append(f"{len(dupes)} bone name(s) resolve to more than one node "
                   f"({', '.join(sorted(dupes)[:4])}) — the skeletons did not weld, they stacked")

    # --- structural sanity, cheap and it catches a buffer written short
    for ai, acc in enumerate(doc.get("accessors") or []):
        bv = acc.get("bufferView")
        if bv is None:
            continue
        view = (doc.get("bufferViews") or [])[bv]
        if view["byteOffset"] + view["byteLength"] > len(blob):
            bad.append(f"accessor {ai} reads past the end of the binary chunk")
    return bad


def _vertices(doc: dict, prim: dict) -> int:
    pos = (prim.get("attributes") or {}).get("POSITION")
    return int((doc["accessors"][pos] or {}).get("count") or 0) if pos is not None else 0


def _short(m: list[float]) -> str:
    return "t=" + ",".join(f"{m[i * 4 + 3]:.4g}" for i in range(3))


# ---------------------------------------------------------------- the composer


@dataclass
class _Src:
    """One container GLB, read once per compose."""

    doc: dict
    blob: bytes
    # `mesh index -> skin index or None`, from whichever node draws it. A container draws each mesh
    # once; where it does not, the first node wins and the rest would have said the same thing.
    skin_of: dict = field(default_factory=dict)


class _Compose:
    """Accumulates one output document. Every `_x` method copies from a source and returns a new index."""

    def __init__(self, build: Build, thing: Thing, max_texture: int, quality: int):
        self.build, self.thing = build, thing
        self.notes: list[str] = []
        self.nodes: list[dict] = []
        self.meshes: list[dict] = []
        self.accessors: list[dict] = []
        self.views: list[dict] = []
        self.skins: list[dict] = []
        self.materials: list[dict] = []
        self.blob = bytearray()
        self.tex = _Textures(build, max_texture, quality)
        self._view: dict[tuple, int] = {}
        self._acc: dict[tuple, int] = {}
        self._mesh: dict[tuple, int] = {}
        self._skin: dict[tuple, int] = {}
        self._mat: dict[int, int] = {}
        self._chain: dict[tuple, int] = {}
        self.joints: dict[str, int] = {}          # bone name -> node, the ONE skeleton pieces weld onto

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def add_node(self, node: dict, under: Optional[int] = None) -> int:
        self.nodes.append(node)
        index = len(self.nodes) - 1
        if under is not None:
            self.nodes[under].setdefault("children", []).append(index)
        return index

    # -------------------------------------------------- binary

    def view(self, src: _Src, cid: int, i: int) -> int:
        key = (cid, i)
        if key not in self._view:
            bv = src.doc["bufferViews"][i]
            off, length = bv.get("byteOffset", 0), bv["byteLength"]
            self.blob += b"\x00" * (-len(self.blob) % 4)      # every view starts 4-aligned, as required
            out = {"buffer": 0, "byteOffset": len(self.blob), "byteLength": length}
            self.blob += src.blob[off:off + length]
            for keep in ("byteStride", "target"):
                if keep in bv:
                    out[keep] = bv[keep]
            self.views.append(out)
            self._view[key] = len(self.views) - 1
        return self._view[key]

    def accessor(self, src: _Src, cid: int, i: int) -> int:
        key = (cid, i)
        if key not in self._acc:
            a = dict(src.doc["accessors"][i])
            if "bufferView" in a:
                a["bufferView"] = self.view(src, cid, a["bufferView"])
            if "sparse" in a:
                # 27 of the 1,293 container GLBs use sparse accessors, all of them for morph targets.
                # Its two sub-views are separate and are missed by anything that only walks the main one.
                sparse = json.loads(json.dumps(a["sparse"]))
                for part in ("indices", "values"):
                    sparse[part]["bufferView"] = self.view(src, cid, sparse[part]["bufferView"])
                a["sparse"] = sparse
            self.accessors.append(a)
            self._acc[key] = len(self.accessors) - 1
        return self._acc[key]

    # -------------------------------------------------- materials

    def material(self, pc: Optional[int]) -> Optional[int]:
        if pc is None:
            return None
        if pc not in self._mat:
            # `is None` and not falsiness: a material whose settings are all defaults has an EMPTY data
            # bag, and dropping it leaves the primitive untextured for looking correct.
            data = (self.build.asset(pc) or {}).get("data")
            if data is None:
                self.note(f"material {pc} is bound but not in the registry")
                return None
            self.materials.append(material_from(data, self.build.name(pc), self.tex))
            self._mat[pc] = len(self.materials) - 1
        return self._mat[pc]

    # -------------------------------------------------- geometry

    def mesh(self, src: _Src, cid: int, index: int, mats: tuple) -> int:
        """One container mesh with the scene's materials on it. Keyed so an instance reuses the copy."""
        key = (cid, index, mats)
        if key in self._mesh:
            return self._mesh[key]
        source = src.doc["meshes"][index]
        prims = []
        for i, prim in enumerate(source.get("primitives") or []):
            out: dict = {"attributes": {k: self.accessor(src, cid, v)
                                        for k, v in (prim.get("attributes") or {}).items()}}
            if "indices" in prim:
                out["indices"] = self.accessor(src, cid, prim["indices"])
            if "mode" in prim:
                out["mode"] = prim["mode"]
            if prim.get("targets"):
                out["targets"] = [{k: self.accessor(src, cid, v) for k, v in t.items()}
                                  for t in prim["targets"]]
            slot = self.material(mats[i] if i < len(mats) else None)
            if slot is not None:
                out["material"] = slot
            prims.append(out)
        if len(mats) != len(prims):
            self.note(f"{self.build.name(cid)} mesh {index}: {len(mats)} material(s) for {len(prims)} "
                      f"primitive(s) — pairing as far as they go")
        out_mesh: dict = {"primitives": prims}
        for keep in ("name", "weights", "extras"):          # `extras.targetNames` is how a morph is named
            if keep in source:
                out_mesh[keep] = source[keep]
        self.meshes.append(out_mesh)
        self._mesh[key] = len(self.meshes) - 1
        return self._mesh[key]

    # -------------------------------------------------- the skeleton

    def copy_skeleton(self, src: _Src, cid: int, under: int) -> dict[str, int]:
        """Copy a container's own joints, with the ancestors that hold the hierarchy together.

        The FALLBACK, and it has never fired on this corpus. Normally the skeleton comes from the scene
        (see `compose_thing`), because the scene expands every bone into an entity — 181/181 for
        office-babe across three containers, 222/222 bride, 85/85 Alice. This is what happens when a
        container arrives whose bones the scene did NOT expand, and it is reported when it does,
        because a figure with two skeletons in it is not a figure.
        """
        doc = src.doc
        up: dict[int, int] = {}
        for i, node in enumerate(doc.get("nodes") or []):
            for c in node.get("children") or []:
                up[c] = i
        need: set[int] = set()
        for skin in doc.get("skins") or []:
            for j in skin.get("joints") or []:
                k: Optional[int] = j
                while k is not None and k not in need:
                    need.add(k)
                    k = up.get(k)
        mapping: dict[int, int] = {}
        for i in sorted(need):
            node = doc["nodes"][i]
            mapping[i] = self.add_node({k: v for k, v in node.items()
                                        if k in ("name", "translation", "rotation", "scale", "matrix")})
        for i in sorted(need):
            kids = [mapping[c] for c in (doc["nodes"][i].get("children") or []) if c in mapping]
            if kids:
                self.nodes[mapping[i]]["children"] = kids
        for i in sorted(need):
            if up.get(i) not in need:
                self.nodes[under].setdefault("children", []).append(mapping[i])
        by_name: dict[str, int] = {}
        for i in sorted(need):
            name = doc["nodes"][i].get("name")
            if name:
                by_name.setdefault(name, mapping[i])
        self.note(f"{self.build.name(cid)} brought its OWN skeleton ({len(by_name)} bones) because the "
                  f"scene does not expand its joints into entities — this figure now has more than one "
                  f"skeleton and one clip cannot drive both")
        return by_name

    def skin(self, src: _Src, cid: int, index: int, joints: dict[str, int]) -> int:
        """One container skin, its joints re-pointed at the scene's bones by NAME.

        The inverse bind matrices are copied unchanged and that is correct even where two containers
        disagree about them: an IBM maps a MESH's own space into bone space, so two meshes authored in
        different units legitimately carry different ones. Bride is exactly that — `bride_ready.glb`
        is in centimetres and `model_britney_bride.glb` in metres, 222 bones' worth of difference —
        and an earlier version of this refused to merge her over it. What has to agree is where the
        BONES are, which `joints_agree` checks directly.
        """
        key = (cid, index)
        if key not in self._skin:
            source = src.doc["skins"][index]
            out: dict = {"joints": [joints[src.doc["nodes"][j].get("name")]
                                    for j in source.get("joints") or []]}
            if "inverseBindMatrices" in source:
                out["inverseBindMatrices"] = self.accessor(src, cid, source["inverseBindMatrices"])
            if "skeleton" in source:
                root = src.doc["nodes"][source["skeleton"]].get("name")
                if root in joints:
                    out["skeleton"] = joints[root]
            self.skins.append(out)
            self._skin[key] = len(self.skins) - 1
        return self._skin[key]


def joint_locals(src: _Src) -> dict[str, list[float]]:
    """Each joint's OWN matrix in its container — the rest pose, one bone at a time."""
    out: dict[str, list[float]] = {}
    for skin in src.doc.get("skins") or []:
        for j in skin.get("joints") or []:
            node = src.doc["nodes"][j]
            if node.get("name"):
                out.setdefault(node["name"], node_matrix(node))
    return out


def joints_agree(src: _Src, rest: dict[str, list[float]]) -> list[str]:
    """Bones whose rest pose in the container is not the one the scene states, by name.

    **This is the weldability test**, and it took two wrong shapes to get here. Comparing two
    containers' inverse bind MATRICES was the wrong question: an IBM maps a mesh's own space into bone
    space, so `bride_ready.glb` (centimetres) and `model_britney_bride.glb` (metres) legitimately
    differ on all 222 and can still share a skeleton. Comparing WORLD matrices was the wrong frame: it
    fails whenever the scene scales the thing, and Oktoberfest is 1.25, so all 175 of her bones read as
    broken while nothing was wrong.

    What has to agree is each bone's own transform — where it sits relative to its parent bone. That is
    invariant to how big the scene makes the figure and to how deep the container's own root was, and it
    is exactly the condition under which one skeleton can pose another container's weights.
    """
    return sorted(name for name, m in joint_locals(src).items()
                  if name in rest and not _close(m, rest[name], 1e-4))


def compose_thing(build: Build, thing: Thing, *, capture: str = "", shown: bool = False,
                  max_texture: int = 1024, quality: int = 90) -> tuple[Optional[bytes], list[str]]:
    """`(glb, notes)` for one thing — or `(None, notes)` if nothing of it could be read.

    `shown=True` leaves OUT everything the scene does not draw: the wardrobe it has switched off, and
    the losing half of a variant. That is not the asset — the asset keeps them, because a part the
    runtime can show again has to be in the file to be shown — it is a copy for LOOKING at. A glb
    viewer draws every node it is given and knows nothing about `extras`, so in the real asset Alice's
    hidden pale hair and bride's underwear are both plainly visible and read as bugs that were
    already fixed.
    """
    work = _Compose(build, thing, max_texture, quality)
    srcs: dict[int, _Src] = {}
    for cid in sorted(thing.containers):
        path = build.path(cid)
        if not path or not os.path.exists(path):
            work.note(f"{build.name(cid)} is not in this capture — "
                      f"{thing.containers[cid]} piece(s) of {thing.name} are missing from the file")
            continue
        doc, blob = split_glb(open(path, "rb").read())
        if not doc:
            work.note(f"{build.name(cid)} is not a binary glTF")
            continue
        src = _Src(doc, blob)
        for node in doc.get("nodes") or []:
            if node.get("mesh") is not None:
                src.skin_of.setdefault(int(node["mesh"]), node.get("skin"))
        srcs[cid] = src

    usable = [p for p in thing.pieces
              if p.container in srcs and p.mesh < len(srcs[p.container].doc.get("meshes") or [])]
    for piece in thing.pieces:
        if piece in usable:
            continue
        if piece.container in srcs:
            work.note(f"{piece.entity!r} draws mesh {piece.mesh} of {build.name(piece.container)}, "
                      f"which the file does not have")
    if not usable:
        return None, work.notes or ["nothing of this thing is on disk"]

    # ---- the node tree IS the scene's entity subtree, one for one.
    #
    # Not a merge of the containers' hierarchies, which is where the first version of this went wrong.
    # A container's own root transform is REPRODUCED by the entity that instantiates it — bride's
    # `RootNode` is scale 0.01 and so is the entity `bride_ready` — so copying both applied it twice and
    # the composed bride came out 100× small. The scene is the one description that is already
    # consistent with itself, and it expands every bone into an entity, so building from it removes the
    # skeleton merge entirely: there is one skeleton because there is one scene.
    node_of: list[int] = [-1] * len(thing.tree)
    for i, entity in enumerate(thing.tree):
        node = {"name": entity.name,
                # The thing's own POSITION is dropped and only there: it is where this scene stands the
                # thing, not a property of the thing. Its rotation and scale stay — a `Banana` is a 0.5
                # wrapper and a banana that skips it is twice life size.
                "translation": [0.0, 0.0, 0.0] if i == 0 else list(entity.position),
                "rotation": quat_from_euler(entity.rotation),
                "scale": list(entity.scale)}
        node_of[i] = work.add_node(node, under=None if i == 0 else node_of[entity.parent])
    root = node_of[0]
    for i, entity in enumerate(thing.tree):
        if entity.name in work.joints:
            work.note(f"{entity.name!r} names {1 + sum(1 for n in thing.tree if n.name == entity.name)} "
                      f"entities — a skin that asks for that bone by name gets the first one")
        else:
            work.joints[entity.name] = node_of[i]

    # ---- the bones the scene states, checked against the rest pose each container was bound in
    #
    # A scene entity's transform is usually the bind pose and is sometimes a SAVED POSE, and only the
    # containers can tell the two apart. Where every container that binds a bone agrees with the others
    # and disagrees with the scene, the scene is holding a pose and the bind pose is what a composed
    # asset must carry: Alice's two eye bones are 90° out in the scene and nowhere else, which is very
    # likely her long-standing eye trouble. Where the containers disagree with EACH OTHER the scene
    # keeps its value and the odd one out is reported — bride's `model_britney_bride.glb` is a
    # different rig wearing the same bone names, and letting it rewrite the skeleton the other eleven
    # pieces are bound to would break all of them to fix one.
    rest: dict[str, list[float]] = {}
    for i, entity in enumerate(thing.tree):
        rest.setdefault(entity.name, node_matrix(work.nodes[node_of[i]]))
    claims: dict[str, list] = {}
    for cid in sorted(srcs):
        for name, m in joint_locals(srcs[cid]).items():
            claims.setdefault(name, []).append(m)
    rebound: list[str] = []
    for name, seen in claims.items():
        if name not in rest or all(_close(m, rest[name]) for m in seen):
            continue
        if not all(_close(m, seen[0]) for m in seen[1:]):
            continue                                        # the containers disagree — reported below
        rebound.append(name)
        rest[name] = seen[0]
        for i, entity in enumerate(thing.tree):
            if entity.name == name:
                node = work.nodes[node_of[i]]
                node.pop("translation", None), node.pop("rotation", None), node.pop("scale", None)
                node["matrix"] = [seen[0][r * 4 + c] for c in range(4) for r in range(4)]  # column-major
    if rebound:
        work.note(f"{len(rebound)} bone(s) are placed as the container binds them rather than as the "
                  f"scene saves them ({', '.join(sorted(rebound)[:4])}) — the scene is holding a POSE "
                  f"there, and a composed asset carries the rest pose")

    joints_for: dict[int, dict[str, int]] = {}
    for cid in sorted(srcs):
        if not (srcs[cid].doc.get("skins") or []):
            continue
        absent = [n for n in joint_locals(srcs[cid]) if n not in work.joints]
        off = joints_agree(srcs[cid], rest)
        if absent:
            work.note(f"{build.name(cid)} names {len(absent)} bone(s) that are not entities in this "
                      f"scene ({', '.join(absent[:4])})")
        elif off:
            work.note(f"{build.name(cid)} was bound against a DIFFERENT rest pose for {len(off)} "
                      f"bone(s) ({', '.join(off[:4])}) — its meshes will be posed wrongly, and making "
                      f"it share this skeleton is retargeting (plan phase 5)")
        joints_for[cid] = (work.joints if not absent
                           else work.copy_skeleton(srcs[cid], cid, root))

    hidden: list[str] = []
    for index, piece in enumerate(thing.pieces):
        if piece not in usable:
            continue
        if shown and (not piece.enabled or piece.shadowed):
            continue
        src = srcs[piece.container]
        skin = src.skin_of.get(piece.mesh)
        node = node_of[piece.node]
        work.nodes[node]["mesh"] = work.mesh(src, piece.container, piece.mesh, piece.materials)
        if skin is not None:
            work.nodes[node]["skin"] = work.skin(src, piece.container, skin, joints_for[piece.container])
        work.nodes[node]["extras"] = {MARK: {"piece": index, "container": piece.container,
                                             "mesh": piece.mesh, "path": list(piece.path),
                                             "optional": not piece.enabled,
                                             # The losing half of a colour switch the site flips with a
                                             # script the capture does not have. Kept, because it is a
                                             # wardrobe option the moment anything can switch it — and
                                             # hidden, because drawn together the two z-fight.
                                             "variant": piece.shadowed,
                                             "skinned": skin is not None}}
        if not piece.enabled or piece.shadowed:
            hidden.append(piece.entity)
    redressed = [p for p in thing.pieces if p.redressed]
    if redressed:
        work.note(f"{len(redressed)} piece(s) wear the CONTAINER's materials rather than the scene "
                  f"entity's ({', '.join(p.entity for p in redressed[:4])}) — the scene's list repeated "
                  f"one material where the container names several, which is a degenerate copy rather "
                  f"than a choice")
    if thing.shadowed:
        work.note(f"{len(thing.shadowed)} piece(s) draw a mesh another piece already draws in the same "
                  f"place ({', '.join(p.entity for p in thing.shadowed[:4])}) — a VARIANT rather than an "
                  f"instance; the better-dressed claim is shown and this one is emitted hidden")

    doc: dict = {
        "asset": {"version": "2.0", "generator": "conjure compose"},
        "scene": 0,
        "scenes": [{"nodes": [root]}],
        "nodes": work.nodes,
        "meshes": work.meshes,
        "accessors": work.accessors,
        "bufferViews": work.views,
        "materials": work.materials,
        "extras": {MARK: {"thing": thing.name, "scene": thing.scene, "capture": capture,
                          "pieces": len(thing.pieces),
                          # A viewing copy, missing everything the scene does not draw. Recorded so the
                          # verifier judges it by what it claims to be rather than failing every
                          # wardrobe piece, and so a file like this is never mistaken for the asset.
                          "shown": shown,
                          # What the importer reads to make these parts hideable, and what the runtime
                          # `figure-parts` component already consumes: glTF node names, not mesh names.
                          "hidden": hidden,
                          # Bones taken from the container instead of the scene. The verifier needs to
                          # know: a piece hanging off one is no longer where the scene chain says.
                          "rebound": sorted(rebound),
                          "containers": {str(c): n for c, n in sorted(thing.containers.items())}}},
    }
    if work.skins:
        doc["skins"] = work.skins
    for raw, image in zip(work.tex.blobs, work.tex.images):
        work.blob += b"\x00" * (-len(work.blob) % 4)
        work.views.append({"buffer": 0, "byteOffset": len(work.blob), "byteLength": len(raw)})
        work.blob += raw
        image["bufferView"] = len(work.views) - 1
        if image.get("name") is None:
            image.pop("name")
    if work.tex.images:
        doc["images"] = work.tex.images
        doc["samplers"] = [{"wrapS": 10497, "wrapT": 10497}]      # REPEAT/REPEAT, as PlayCanvas defaults
        doc["textures"] = [{**t, "sampler": 0} for t in work.tex.textures]
    doc["buffers"] = [{"byteLength": len(work.blob)}]
    work.notes.extend(work.tex.warnings)
    return write_glb(doc, bytes(work.blob)), work.notes


# ---------------------------------------------------------------- the driver


def _safe(name: str) -> str:
    """A thing's name as a file name. Thing names come from an artist and contain anything."""
    out = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name).strip(" .")
    return out or "thing"


def compose_build(root: str, out_dir: str, *, only: str = "", shown: bool = False,
                  max_texture: int = 1024, quality: int = 90, verify: bool = True,
                  report: Optional[Callable[[str], None]] = None) -> tuple[list[str], int]:
    """Compose every thing in every build under `root`. Returns `(files written, problems found)`.

    Alongside the per-container output rather than instead of it: `temp/rebuilt/` stays exactly as it
    is and the importer keeps pointing at it until these files have been looked at. See
    `docs/plans/figures-and-library.md` § 2b plan A.
    """
    say = report or (lambda _s: None)
    capture = os.path.basename(root.rstrip("/"))
    written: list[str] = []
    problems = 0
    for build_root in find_builds(root):
        build = read_build(build_root)
        found = [t for t in things(build, capture=capture)
                 if t.pieces and (not only or only.lower() in t.name.lower())]
        if not found:
            continue
        say(f"\n{os.path.relpath(build_root, root) or '.'} — {len(found)} thing(s)")
        for thing in found:
            data, notes = compose_thing(build, thing, capture=capture, shown=shown,
                                        max_texture=max_texture, quality=quality)
            if data is None:
                say(f"    {thing.name[:30]:32} SKIPPED")
                for note in notes:
                    say(f"        ! {note}")
                continue
            os.makedirs(out_dir, exist_ok=True)
            dest = os.path.join(out_dir, f"{_safe(thing.name)}.glb")
            # A thing name repeats across scenes (13 toilet variants are 13 names, but `RootNode` is
            # not). Never silently overwrite: the second file loses to the first and nobody is told.
            n = 1
            while os.path.exists(dest) and dest not in written:
                n += 1
                dest = os.path.join(out_dir, f"{_safe(thing.name)}-{n}.glb")
            open(dest, "wb").write(data)
            written.append(dest)
            say(f"    {thing.name[:30]:32} {len(thing.live):3} live {len(thing.optional):2} optional"
                f"  {len(data) / 1e6:5.1f} MB  -> {os.path.relpath(dest)}")
            for note in notes:
                say(f"        ! {note}")
            if verify:
                for problem in verify_thing(build, thing, data):
                    problems += 1
                    say(f"        WRONG: {problem}")
    return written, problems
