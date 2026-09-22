"""Faces: driving one, in semantic terms, across the two expression rigs that exist.

A figure who blinks and looks at you is a different presence, and the director has nothing to work
with today. This is the smallest thing that changes that: a morph weight is one scalar per target and
three.js applies it through the mixer that already plays clips, so there is no new client path.

**Two expression rigs, and they speak different languages** (measured across 38 rigged figures):

    Saka    57 targets, all facial   VRM's own preset set — `Fcl_ALL_Joy`, `Fcl_BRW_Angry`
    Alice   34 targets, 31 facial    Character Creator / ARKit-ish — `Brow_Raise_Inner_L`
    Bianca, Blondie  22, 21          TONGUE ONLY — no brows, no eyes
    everyone else    2–17            skin and clothing colour, anatomy, `closed_eyes_correction`

So `smile` cannot be a target name. It is a semantic request — exactly as `leftUpperArm` is a
semantic bone — resolved per scheme into whatever that figure's author happened to call it.

## Why a table here is not the thing `REF_AGAINST_UP` warns about

`docs/backlogs/figures.md` warns that a morph *retarget* has "names and nothing else", because
`Fcl_ALL_Joy` is not `Brow_Raise_Inner_L` in any sense a measurement can establish. That is true, and
it is about carrying one figure's PERFORMANCE onto another.

This is a different job. Mapping `smile` → `Fcl_ALL_Fun` is reading the author's own label — discovery
layer 0, the same move as `vrm_humanoid()` — not inferring a correspondence between two rigs. And it
IS checkable, which the retarget case is not: a morph target carries position deltas, so the vertices
it moves have a location. Measured on both rigs, with no overlap between the bands:

               brow              eye               mouth
    Saka       1.4228..1.4257    1.3973..1.4104    1.3391..1.3430
    Alice      1.5989..1.6018    1.5658..1.5854    1.5186..1.5237

`brow > eye > mouth`, which is anatomy, on both. `check_regions()` is that test, and it catches a
table entry that claims a mouth target is a brow one — the failure a name-only table would hide.
"""

from __future__ import annotations

#: Bumped when a table changes, so anything derived and cached carries the revision that derived it.
EXPRESSION_REV = 1

#: What a director can ask for, independent of the rig underneath.
#:
#: Deliberately small. Every entry is either something both schemes can express or something one can
#: express and the other honestly cannot — `look_*` is eye-morph-driven on Character Creator and does
#: not exist in the VRM preset set at all, because VRM aims the eyes with BONES. A request a figure
#: cannot meet is reported as skipped, never silently approximated.
EXPRESSIONS = (
    "neutral", "smile", "joy", "sad", "angry", "surprised",
    "blink", "blink_left", "blink_right", "squint",
    "brow_raise", "brow_lower", "mouth_open", "pucker",
    "look_left", "look_right", "look_up", "look_down",
)

#: Mouth shapes for speech. Five, because that is what the VRM preset set carries (`Fcl_MTH_A` … `O`)
#: and five is enough to read as talking.
VISEMES = ("aa", "ee", "ih", "oh", "ou")

#: Where on a face a target belongs, by the author's own naming. Used to CLASSIFY a vocabulary — "does
#: this figure have a face at all" — and to give `check_regions` something to test against.
#:
#: `face` is the WHOLE-face composite, `Fcl_ALL_Joy` and its siblings: the most facial thing on the VRM
#: rig and the one role with no band of its own, because it moves brows, eyes and mouth together and
#: its centroid lands between them by construction.
ROLES = ("face", "brow", "eye", "mouth", "tongue", "other")

#: The anatomical order the geometry must agree with. Not a convention: a brow is above an eye is above
#: a mouth on every face there has ever been. `face` is deliberately absent — a composite legitimately
#: straddles all three, and requiring it into a band would fail a correct table.
ROLE_ORDER = ("brow", "eye", "mouth")

#: Roles that make a vocabulary a FACE, as opposed to wardrobe, skin tone or anatomy.
FACIAL_ROLES = ("face", "brow", "eye", "mouth")


# --------------------------------------------------------------------- schemes

#: VRM 0.x / VRoid preset expressions. The author states the emotion outright — `Fcl_ALL_Joy` IS joy —
#: so most of this table is one entry, and the composites are the author's, not ours.
_VRM = {
    "neutral":     {"Fcl_ALL_Neutral": 1.0},
    "smile":       {"Fcl_ALL_Fun": 1.0},          # `Fun` is VRM's smile; `Joy` is the broader one
    "joy":         {"Fcl_ALL_Joy": 1.0},
    "sad":         {"Fcl_ALL_Sorrow": 1.0},
    "angry":       {"Fcl_ALL_Angry": 1.0},
    "surprised":   {"Fcl_ALL_Surprised": 1.0},
    "blink":       {"Fcl_EYE_Close": 1.0},
    "blink_left":  {"Fcl_EYE_Close_L": 1.0},
    "blink_right": {"Fcl_EYE_Close_R": 1.0},
    "squint":      {"Fcl_EYE_Joy": 1.0},          # the narrowed-eye shape, whatever it is named after
    "brow_raise":  {"Fcl_BRW_Surprised": 1.0},
    "brow_lower":  {"Fcl_BRW_Angry": 1.0},
    "mouth_open":  {"Fcl_MTH_Large": 1.0},
    "pucker":      {"Fcl_MTH_U": 1.0},
    # No look_*: VRM aims the eyes with bones, and this rig has no eye-direction morphs to drive.
}

_VRM_VISEMES = {v: {f"Fcl_MTH_{k}": 1.0} for v, k in
                (("aa", "A"), ("ee", "E"), ("ih", "I"), ("oh", "O"), ("ou", "U"))}

#: Character Creator / ARKit-ish. The author names MUSCLES, not emotions, so an emotion here is a
#: composite WE author — and that is the part of this file with the least evidence behind it. Each one
#: is a standard FACS-ish reading (a smile is zygomatic; a real one crinkles the eyes; anger drops the
#: brows) rather than anything measured, and it is marked as such in `PROVENANCE` below.
_CC = {
    "neutral":     {},                            # every target to zero
    "smile":       {"Mouth_Smile_L": 1.0, "Mouth_Smile_R": 1.0},
    "joy":         {"Mouth_Smile_L": 1.0, "Mouth_Smile_R": 1.0,
                    "Eye_Squint_L": 0.4, "Eye_Squint_R": 0.4},
    "sad":         {"Brow_Raise_Inner_L": 0.7, "Brow_Raise_Inner_R": 0.7,
                    "Mouth_Shrug_Upper": 0.3},
    "angry":       {"Brow_Drop_L": 1.0, "Brow_Drop_R": 1.0,
                    "Eye_Squint_L": 0.3, "Eye_Squint_R": 0.3},
    "surprised":   {"Brow_Raise_Inner_L": 1.0, "Brow_Raise_Inner_R": 1.0,
                    "Brow_Raise_Outer_L": 1.0, "Brow_Raise_Outer_R": 1.0,
                    "Jaw_Open": 0.5},
    "blink":       {"Eye_Blink_L": 1.0, "Eye_Blink_R": 1.0},
    "blink_left":  {"Eye_Blink_L": 1.0},
    "blink_right": {"Eye_Blink_R": 1.0},
    "squint":      {"Eye_Squint_L": 1.0, "Eye_Squint_R": 1.0},
    "brow_raise":  {"Brow_Raise_Inner_L": 1.0, "Brow_Raise_Inner_R": 1.0,
                    "Brow_Raise_Outer_L": 1.0, "Brow_Raise_Outer_R": 1.0},
    "brow_lower":  {"Brow_Drop_L": 1.0, "Brow_Drop_R": 1.0},
    "mouth_open":  {"Jaw_Open": 1.0},
    "pucker":      {"Mouth_Pucker": 1.0},
    # BOTH eyes, always. A single eye turning is a lazy eye, not a glance.
    "look_left":   {"Eye_L_Look_L": 1.0, "Eye_R_Look_L": 1.0},
    "look_right":  {"Eye_L_Look_R": 1.0, "Eye_R_Look_R": 1.0},
    "look_up":     {"Eye_L_Look_Up": 1.0, "Eye_R_Look_Up": 1.0},
    "look_down":   {"Eye_L_Look_Down": 1.0, "Eye_R_Look_Down": 1.0},
}

#: Character Creator has no A/I/U/E/O. These are approximations from the shapes it does have, and they
#: are the weakest thing in this module: `ee` is a spread mouth, `ou` a pursed one, `aa` an open jaw.
#: Good enough to read as speech at conversational distance, and not claimed to be more.
_CC_VISEMES = {
    "aa": {"Jaw_Open": 0.7},
    "ee": {"Mouth_Smile_L": 0.5, "Mouth_Smile_R": 0.5, "Jaw_Open": 0.2},
    "ih": {"Jaw_Open": 0.3, "Mouth_Smile_L": 0.2, "Mouth_Smile_R": 0.2},
    "oh": {"Mouth_Funnel": 0.8, "Jaw_Open": 0.4},
    "ou": {"Mouth_Pucker": 0.9},
}

SCHEMES = {
    "vrm": {**_VRM, **_VRM_VISEMES},
    "cc":  {**_CC, **_CC_VISEMES},
}

#: How much evidence is behind each half of a table, stated rather than implied.
PROVENANCE = {
    "vrm": "the author's own preset names — `Fcl_ALL_Joy` states the emotion (discovery layer 0)",
    "cc":  "muscle names read directly; the EMOTIONS are composites we author, FACS-ish and unmeasured",
}


def scheme_of(names) -> str:
    """Which expression rig a vocabulary belongs to, or `""`.

    By vocabulary rather than by an exported flag, because most of these figures arrive through a
    capture and carry no statement of origin at all. Both prefixes are distinctive enough that a
    threshold is not needed: `Fcl_` is VRM's and nothing else's.
    """
    names = set(names or ())
    if any(n.startswith("Fcl_") for n in names):
        return "vrm"
    # Character Creator names a muscle and a side. Requiring TWO independent families, not one, so a
    # figure carrying a lone `Eye_Blink_L` correction and no other facial shape is not called a face.
    families = sum(1 for probe in ("Brow_", "Eye_Blink", "Mouth_", "Jaw_")
                   if any(n.startswith(probe) for n in names))
    return "cc" if families >= 2 else ""


def role_of(name: str) -> str:
    """Where on the face a target acts, from its name alone. See `check_regions` for the geometry."""
    n = (name or "").lower()
    if n.startswith("fcl_"):
        for key, role in (("_all_", "face"), ("_brw_", "brow"), ("_eye_", "eye"),
                          ("_mth_", "mouth"), ("_ha_", "mouth")):
            if key in n:
                return role
        return "other"
    if "tongue" in n:
        return "tongue"
    if n.startswith("brow"):
        return "brow"
    if n.startswith("eye") or "eyelid" in n or "closed_eyes" in n:
        return "eye"
    if n.startswith(("mouth", "jaw", "lip", "cheek")):
        return "mouth"
    return "other"


def facial(names) -> list[str]:
    """The targets that are part of a FACE, out of everything a figure carries.

    Worth a function because the ratio is the whole story: 30 of 32 rigged models carry morph targets
    and almost none of them are a face — `Body_Alabaster`, `Shirt_Blue`, `Pussy2`, `Vagina_Open` are
    wardrobe, skin tone and anatomy. Counting targets tells you nothing; counting these tells you
    whether a figure can smile.
    """
    return [n for n in (names or ()) if role_of(n) in FACIAL_ROLES]


def resolve(available, request: dict) -> tuple[dict, list[str]]:
    """Turn a semantic request into `{target: weight}` for one figure's actual vocabulary.

    `request` maps a name from `EXPRESSIONS` or `VISEMES` — or a RAW target name, so a caller who has
    inspected a figure can drive anything it carries — to a weight in 0..1, which scales the whole
    entry. Returns what to apply and what this figure could not do.

    Composites ADD and then clamp. Two entries touching one target (a smile and a viseme both using
    `Jaw_Open`) must not silently let the last one win: a face is additive, and 1.0 is as open as a
    jaw goes.
    """
    have = set(available or ())
    scheme = scheme_of(have)
    table = SCHEMES.get(scheme, {})
    out: dict[str, float] = {}
    skipped: list[str] = []

    for key, amount in (request or {}).items():
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            skipped.append(key)
            continue
        amount = max(0.0, min(1.0, amount))

        if key in have:                     # a raw target name always wins: the caller looked it up
            out[key] = min(1.0, out.get(key, 0.0) + amount)
            continue
        entry = table.get(key)
        if entry is None:
            skipped.append(key)
            continue
        # `neutral` is the empty entry: it means every target to zero, which the caller gets by this
        # returning nothing rather than by naming targets it may not have.
        landed = False
        for target, weight in entry.items():
            if target in have:
                out[target] = min(1.0, out.get(target, 0.0) + weight * amount)
                landed = True
        if not landed and entry:
            # The scheme knows this expression and THIS FIGURE has none of the targets it needs — a
            # tongue-only rig asked to smile. Different from an unknown name, and the caller is told.
            skipped.append(key)
    return out, skipped


def check_regions(centroids: dict) -> list[str]:
    """Verify a vocabulary's own geometry against anatomy: brow above eye above mouth.

    `centroids` maps a target name to the Y of the vertices it actually moves, weighted by how far it
    moves them — `figures.morph_centroids()` computes it. This is the check the retarget case cannot
    have, and the reason a table here is more than an assertion: a morph target carries position
    deltas, so a claim about what it does is testable against where it acts.

    Returns the violations, empty when the geometry agrees. Roles a vocabulary does not carry are
    skipped rather than failed — a tongue-only rig has no brows to be wrong about.
    """
    bands: dict[str, list[float]] = {}
    for name, y in (centroids or {}).items():
        role = role_of(name)
        if role in ROLE_ORDER and isinstance(y, (int, float)):
            bands.setdefault(role, []).append(float(y))

    present = [r for r in ROLE_ORDER if bands.get(r)]
    problems = []
    if len(present) < 2:
        # Two different situations, and conflating them is how this check stops being read.
        #
        # A figure with ONE stray facial shape — `closed_eyes_correction` among thirteen wardrobe
        # morphs — makes no ordering claim, so there is nothing here to be right or wrong about. Not
        # applicable, and reporting it as a failure buries the real ones: three of five figures in the
        # corpus are in that state.
        #
        # A figure whose vocabulary IS a recognised expression rig and yet yields no bands is a
        # measurement that failed. That is precisely what happened to Alice — a sparse-only accessor
        # read as zero vertices, every target measured as moving nothing — and an empty `[]` there read
        # to every caller as "geometry agrees". A green result that cannot go red is worse than no
        # check, because it is trusted.
        if not scheme_of(centroids or {}):
            return []
        return [f"{len(centroids or {})} target(s) measured on a recognised expression rig and only "
                f"{present or 'no'} band(s) came back — the geometry was not read, not verified"]
    for upper, lower in zip(present, present[1:]):
        # The LOWEST of the upper band against the HIGHEST of the lower one: means overlapping at all
        # is a finding, not just means being out of order. Measured, the bands do not overlap on
        # either rig, so anything less strict would pass a table that has genuinely gone wrong.
        lo_upper, hi_lower = min(bands[upper]), max(bands[lower])
        if lo_upper <= hi_lower:
            problems.append(
                f"{upper} and {lower} overlap: lowest {upper} is {lo_upper:.4f}, "
                f"highest {lower} is {hi_lower:.4f} — a target is classified onto the wrong feature")
    return problems


#: Which way a named shape must move the mesh. Only targets whose NAME states a direction, because this
#: checks a name against geometry and a name claiming nothing cannot be wrong.
#:
#: **Whole-face composites are deliberately absent, and the first version of this got that wrong.** It
#: asserted `Fcl_ALL_Joy` rises, on the reasoning that joy is a smile. Measured, it FALLS (-0.00376),
#: and correctly: it is built from `Fcl_EYE_Joy` (-0.00584 — happy eyes close, and an eyelid closes
#: downward) and `Fcl_MTH_Joy` (-0.00246 — the mouth opens) against `Fcl_BRW_Joy` (+0.00073). A
#: composite's net vertical motion is the sum of parts pulling opposite ways and asserts nothing about
#: whether it is right.
#:
#: That is the same mistake as putting `face` in `ROLE_ORDER`, made twice: a shape that straddles every
#: feature cannot be held to a claim about one. Both checks now exclude composites for one reason.
_EXPECTED_RISE = {
    "mouth_smile": +1, "brow_raise": +1, "brow_drop": -1, "brow_down": -1,
    "jaw_open": -1, "look_up": +1, "look_down": -1,
    "fcl_mth_up": +1, "fcl_mth_down": -1,
    "fcl_brw_surprised": +1, "fcl_brw_angry": -1, "fcl_brw_sorrow": -1,
}


def check_directions(displacements: dict, floor: float = 1e-4) -> list[str]:
    """Verify that a target moves the mesh the way its NAME says it does.

    Independent of `check_regions` and catching a different fault. That one says a brow target acts on
    the brow; this one says a target called `Brow_Drop` actually drops it. A rig where the two shapes
    were modelled the wrong way round would pass the region check completely — both are brows, both in
    the brow band — and put an angry face on every request for a surprised one.

    Only targets whose name states a direction are checked, and only past a floor: a shape can be
    mostly horizontal (`Eye_L_Look_L` moves sideways) and a near-zero mean says nothing either way.
    """
    problems = []
    for name, rise in (displacements or {}).items():
        if not isinstance(rise, (int, float)) or abs(rise) < floor:
            continue
        if role_of(name) == "face":
            continue                  # a composite straddles every feature — see `_EXPECTED_RISE`
        low = (name or "").lower()
        for probe, want in _EXPECTED_RISE.items():
            if probe in low:
                if (rise > 0) != (want > 0):
                    problems.append(
                        f"{name} moves the mesh {'up' if rise > 0 else 'down'} ({rise:+.5f}) and its "
                        f"name says {'up' if want > 0 else 'down'} — the shape is inverted, or the "
                        f"table is reaching for the wrong one")
                break
    return problems


def describe(names) -> str:
    """One line for the catalog: what this figure's face can do."""
    face = facial(names)
    if not face:
        return ""
    scheme = scheme_of(names)
    can = [e for e in EXPRESSIONS if resolve(names, {e: 1.0})[0] or e == "neutral"]
    return (f"{len(face)} facial morph targets ({scheme or 'unrecognised'} scheme); "
            f"can {', '.join(can[:8])}")
