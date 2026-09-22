"""Linking rules for an imported capture — what goes with what, and what is left alone.

Spec: docs/specs/captures.md.

A capture arrives as a pile of files. Three of the links between them are recoverable from the names
the build already uses, and the rest are not; this module is the line between those two, kept pure so
the line can be tested without a library, a server, or a capture on disk.

The rules are **measured against twenty captures**, and every one of them declines to guess where the
evidence runs out. That restraint is the point: a wrong link is worse than a missing one, because the
missing one is visible and the wrong one is acted on.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

#: Audio that is not content. Twenty-four marketing lines and a UI click arrive identically in every
#: capture from this origin — the site names those files after their own transcripts, which is what
#: makes them recognisable at all. ORIGIN-SPECIFIC and known to be: a second site will need a second
#: rule, and a skip list keyed on content hash would generalise where this does not (see the plan's
#: open questions). Worth doing only once there is a second site to test it against.
_PROMO = re.compile(r"in the full version|vr\s*holes\s*dot\s*com|^click\s*-\s*compressed$", re.I)

#: Sound that belongs to a ROOM rather than to any clip — `Washitsu soundtrack`, `Moon Base Ambience`,
#: `Ceiling Fan`. Narrow on purpose. `HotelAction0` is also unattached to a clip and is NOT ambience,
#: so the rule matches what it recognises and leaves everything else unlinked.
_AMBIENCE = re.compile(r"\b(soundtrack|ambience|ambient|atmos|outside|music|loop|fan)\b", re.I)

#: `1-5-8-9_idle` — one file serving four clips, which names them. `2-10_action` is two. A bare
#: `3_idle` is one. `HotelAction0` and `AulaIdle0` match nothing here and are meant not to.
_SLOTS = re.compile(r"^(\d+(?:-\d+)*)_([A-Za-z]+)$")


def stem(name: str) -> str:
    """A filename without its extension, and without the path a registry sometimes keeps."""
    return re.sub(r"\.[A-Za-z0-9]{1,5}$", "", (name or "").rsplit("/", 1)[-1]).strip()


def is_promo(name: str) -> bool:
    """Marketing and UI sound, which no capture should carry into the catalog."""
    return bool(_PROMO.search(stem(name)))


def voiced_clips(audio_name: str, clip_names: Iterable[str]) -> list[str]:
    """Which clips an audio file is the voice for, by the name the build gave it.

    `1-5-8-9_idle.mp3` against a set of clips returns the four it names, in the order it names them.
    A name that does not parse returns `[]` — `HotelAction0` is real audio for a real scene and its
    number is an index into a script we do not have, so linking it to `10_idle` because both end in a
    digit would be inventing a fact.

    Only clips that EXIST are returned: the name is a claim, and a claim about a clip this capture
    does not contain is not worth recording.
    """
    match = _SLOTS.match(stem(audio_name))
    if not match:
        return []
    numbers, kind = match.group(1).split("-"), match.group(2)
    have = {stem(c): c for c in clip_names}
    out = []
    for number in numbers:
        want = f"{number}_{kind}"
        if want in have and have[want] not in out:
            out.append(have[want])
    return out


def is_ambience(name: str) -> bool:
    """Sound belonging to a room rather than to a clip."""
    return not is_promo(name) and bool(_AMBIENCE.search(stem(name)))


def audio_role(audio_name: str, clip_names: Iterable[str]) -> tuple[str, list[str]]:
    """`(role, clips)` for one audio file — `skip`, `voice`, `ambience`, or `unlinked`.

    `unlinked` is a real answer and the common one: 698 of 898 audio files across twenty captures
    attach to no clip by name. They are still imported, still searchable, and simply carry no assertion
    about what they go with.
    """
    if is_promo(audio_name):
        return "skip", []
    clips = voiced_clips(audio_name, clip_names)
    if clips:
        return "voice", clips
    if is_ambience(audio_name):
        return "ambience", []
    return "unlinked", []


#: Fixtures a clip's name says it needs. 93 of 206 clip names in the corpus call one out, which is why
#: rig compatibility is not sufficiency: `pc_leanOnSink_headLeft` plays on any figure of the right rig
#: and is wrong on one standing in a field.
_PROPS = ("bed", "sink", "toilet", "floor", "door", "wall", "chair", "table",
          "sofa", "desk", "mirror", "stool", "pole", "whiteboard")


def wants_props(clip_name: str) -> list[str]:
    """Fixtures the clip's NAME mentions — a hint, never a gate.

    The name is evidence and not a manifest: a clip called `LayTableIdle` almost certainly wants a
    table, and one called `3_action` may want anything at all. Recording the hint lets a director say
    "six of these want a bed" instead of offering all seventeen as if they were interchangeable.
    """
    # A plain substring, not a word match: these names are run-together — `pc_leanOnSink_headLeft`,
    # `LayTableIdle`, `TableHangIdle` — and a boundary rule finds none of them. The cost is the odd
    # false positive from a word that contains a fixture, which a hint can afford and a gate could not.
    low = stem(clip_name).lower()
    return [p for p in _PROPS if p in low]


def clip_kind(clip_name: str) -> Optional[str]:
    """`idle`, `action`, `rough` or None — the only semantic the slot names carry.

    Worth recording because it is the one axis the corpus shows travels: action clips are shared
    across captures twice as often as idles, 25% against 12%. Idles are personal to a figure.
    """
    low = stem(clip_name).lower()
    for kind in ("idle", "action", "rough", "speaking"):
        if kind in low:
            return kind
    return None


def is_slot_named(clip_name: str) -> bool:
    """Does this clip carry only a SLOT NUMBER, rather than a name that says anything?

    `1_idle` means a different motion on every figure — it exists in 16 captures in 14 distinct
    versions, 10 to 43 seconds long — so it is a label within a set and nothing across one. This is
    what marks the clips a naming pass should look at: four captures carry none at all.
    """
    return bool(re.match(r"^\d+[-_]", stem(clip_name)))
