"""Searchable text for a figure, from what the catalog already knows.

**A figure's whole searchable self is her name**, and three of ours are called `Animated Woman`. The
FTS index covers `label, prompt, query, notes, tags`, and for every rigged model in this catalog the
last two are empty — so "a blonde in a cocktail dress" cannot match, and neither can "someone with
shoes".

This is the free layer of `backlogs/library.md` § *Describing and embedding models*: a pure function of
the row, deterministic, cheap enough to re-run on every refresh, and impossible to refuse or
hallucinate. The vision layer that follows is better prose and worse epistemics; this one can be
trusted, so it goes first and the other is written beside it rather than over it.

**Only what is actually known.** No adjectives, no inference from a name, and no "probably". A figure
with no clips says nothing about clips. The temptation is to pad this into something that reads like a
description, and padding is what makes a search index lie.
"""
from __future__ import annotations

from typing import Optional

#: Part categories worth naming in prose, in the order a person would say them. `body` and `face` are
#: on every figure and carry no information; `parts.py` owns the vocabulary itself.
_WEARABLE = ("clothing", "hair", "shoes", "accessory", "held")

#: How a rig convention reads out loud. The signature is for machines; this is for the search box.
_CONVENTION = {
    "vrm": "VRM",
    "convention:cc-base": "Character Creator",
    "convention:mixamo": "Mixamo",
    "convention:rigify-fk": "Rigify",
    "convention:rigify-def": "Rigify",
    "convention:dot-side": "Blender",
    "inferred": "",
}


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def structured_text(label: str, attrs: dict, *, clips: Optional[list] = None,
                    voiced: int = 0) -> str:
    """One paragraph of fact about a figure, for `notes`. `""` when there is nothing worth saying.

    `clips` and `voiced` come from RELATIONS rather than attributes, so they are passed in — a caller
    without a library still gets everything the file itself knows.
    """
    if not attrs.get("rigged"):
        return ""
    bits: list[str] = []

    height = attrs.get("height_m")
    if isinstance(height, (int, float)) and height > 0:
        bits.append(f"{height:.2f} m tall")

    parts = attrs.get("parts") or {}
    if isinstance(parts, dict) and parts:
        by_kind: dict[str, list[str]] = {}
        hidden = set(attrs.get("parts_hidden") or ())
        for mesh, kind in parts.items():
            if mesh not in hidden:
                by_kind.setdefault(kind, []).append(mesh)
        # The MESH NAMES themselves, because they are what a rigger called the thing and are often the
        # most searchable text in the file: `Rolled_sleeves_shirt`, `Canvas_shoes`, `Pigtail_braid_side`.
        worn = [n.replace("_", " ") for k in _WEARABLE for n in sorted(by_kind.get(k, []))]
        if worn:
            bits.append("wearing " + ", ".join(worn))
        spare = sum(1 for m in parts if m in hidden)
        if spare:
            bits.append(f"{_plural(spare, 'alternate part')} that can be swapped in")

    if clips:
        named = ", ".join(sorted(c for c in clips if c)[:8])
        bits.append(f"{_plural(len(clips), 'animation')}" + (f" including {named}" if named else ""))
    if voiced:
        bits.append(f"{_plural(voiced, 'clip')} with recorded speech")

    morphs = attrs.get("morph_targets")
    if isinstance(morphs, int) and morphs:
        bits.append(f"{_plural(morphs, 'morph target')}")

    rig = _CONVENTION.get(attrs.get("humanoid_source") or "", "")
    bones = len(attrs.get("humanoid") or {})
    if bones:
        bits.append(f"{rig + ' ' if rig else ''}rig, {_plural(bones, 'mapped bone')}".strip())

    return f"{label}. " + "; ".join(bits) + "." if bits else ""
