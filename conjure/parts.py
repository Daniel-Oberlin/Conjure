"""Which mesh on a figure is clothing, hair, a shoe — and which is the figure.

Turning a garment off needs two things: a list of the nodes that ARE the garment, and somewhere to put
it that a person can correct. This module does the first and writes it into the catalog at import, so
the second is a row you can edit rather than a heuristic that re-fires every time something renders.

**Classified once, at import.** The same argument as reading a normal map from the registry instead of
from its pixels: a guess made at import can be inspected, overruled and versioned, while one made at
render time is invisible and happens again on every load.

**The vocabulary is data** (`parts/parts.json`, on the user-first search path `config.PARTS_PATH`),
because it is never finished. A prefix rule would be the obvious approach and it does not work here:
`clothes_*` appears in only **8 of 20** captures, and the rest say `Clothes`, `Hair`, `Shoes`, `Dress`,
`Skirt`, `Shorts`, `underwear` — bare words with no prefix at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import config

VOCAB_FILE = "parts.json"

#: Fallback if no vocabulary is on the path at all — enough to not crash, not enough to rely on.
_FALLBACK = {"revision": 0, "rules": [], "removable": ["clothing", "shoes", "accessory", "hair"]}


def load_vocabulary(parts_path: Optional[list[Path]] = None) -> dict:
    """The first `parts.json` on the search path — a user file shadows the bundled one entirely.

    Shadowing rather than merging, deliberately: a merged vocabulary is one nobody can predict, because
    the answer depends on rule ORDER across two files you cannot see at once. Copy the bundled file and
    edit it.
    """
    path = parts_path if parts_path is not None else config.PARTS_PATH
    for base in path:
        candidate = Path(base) / VOCAB_FILE
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except Exception:                            # noqa: BLE001 — a broken edit must not stop an import
                print(f"[conjure] parts vocabulary at {candidate} is not readable JSON — ignoring it")
    return dict(_FALLBACK)


def categorize(name: str, vocabulary: dict) -> Optional[str]:
    """The category of one mesh-node name, or None when nothing claims it.

    First match wins, so the rules are ordered specific-first — `Clothes_Teacher_Hair` is hair, and
    `clothes_weddingdress_heels_L` is a shoe, and both would be clothing under any other ordering.
    Case-insensitive SUBSTRING, because these names run together and a word-boundary rule matches
    almost none of them.
    """
    low = (name or "").lower()
    if not low:
        return None
    for rule in vocabulary.get("rules") or []:
        for word in rule.get("match") or []:
            if word in low:
                return rule.get("category")
    return None


def classify(doc: dict, vocabulary: Optional[dict] = None) -> dict:
    """`{parts: {node: category}, unclassified: [node], revision: n}` for one glTF document.

    Only nodes that actually carry a mesh — a bone called `Skirt01` drives a garment and is not one,
    and hiding it would do nothing while implying it had.

    `unclassified` is reported rather than swallowed. That list IS the vocabulary's backlog and the only
    honest measure of its coverage; a classifier that silently calls everything it does not know "body"
    would read as complete while quietly refusing to undress anyone.
    """
    vocabulary = vocabulary if vocabulary is not None else load_vocabulary()
    parts: dict[str, str] = {}
    unknown: list[str] = []
    for node in doc.get("nodes") or []:
        if node.get("mesh") is None:
            continue
        name = node.get("name") or ""
        if not name:
            continue
        category = categorize(name, vocabulary)
        if category:
            parts[name] = category
        else:
            unknown.append(name)
    out: dict = {"parts": parts, "revision": vocabulary.get("revision", 0)}
    if unknown:
        out["unclassified"] = sorted(unknown)
    return out


def removable(parts: dict, vocabulary: Optional[dict] = None) -> dict[str, list[str]]:
    """`{category: [node names]}` for the categories a figure can be stripped of.

    `body` and `face` are never removable: taking the eyes out of a head is not undressing it. `hair`
    is removable and is NOT clothing — stripping a figure to check its integrity should not scalp it.
    """
    vocabulary = vocabulary if vocabulary is not None else load_vocabulary()
    allowed = set(vocabulary.get("removable") or [])
    out: dict[str, list[str]] = {}
    for node, category in (parts or {}).items():
        if category in allowed:
            out.setdefault(category, []).append(node)
    return {k: sorted(v) for k, v in sorted(out.items())}
