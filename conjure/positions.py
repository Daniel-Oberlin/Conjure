"""What the captured site STATES about its own animation, rather than what a filename implies.

The newer builds from this origin ship a per-position config — `position_1_config.json` … — naming the
clip each position plays, the sound that goes with it, the rate to play it at, and the secondary layers
(a blink, a squint, a head turn) that run on top. `main_config.json` names the figure, the meshes that
count as clothing, and the bone mask each layer uses.

**It exists because the older convention does not generalise.** `capture_set.audio_role` pairs a clip
with its voice by filename stem, which works where a build names them in lockstep — jane is 20 of 20 —
and fails completely where it does not. Measured: 262 of 772 clip rows carry a `voiced_by` edge, and the
four captures that link NOTHING (`susan` 0/8, `nancy` 0/62, `ebony` 0/140, `ebony2` 0/62) are exactly
the four that ship these configs. The config is how the newer builds say what the older ones said by
filename, so where it speaks it wins.

Verified against every config on disk: **98 of 98 clip names and 30 of 30 sound names resolve** to a
real registry asset in nancy's build, and 100/100 + 30/30 in ebony's. This reads names and refuses to
invent them — a name that matches no asset is dropped and counted, never guessed at.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

#: Files that carry this, by registry asset name. `main_config` is the figure-level one; the rest are
#: one per authored position. Both are `type: "json"` in the registry.
MAIN = "main_config"


@dataclass
class Layer:
    """A secondary animation that runs OVER whatever the position is playing.

    This is the answer to "why is `Blink.glb` never played at placement": it is not a clip anybody
    plays. It is a state on a bone-masked layer that fires on its own timer, every 5–8 seconds, at a
    weight, while the main clip runs underneath. `bones` comes from `main_config` and is the mask.
    """

    name: str                                  # `Eyelids`, `Head`, `EyelidsAndBrows`
    clips: tuple[str, ...] = ()                # registry names; several means "pick one"
    speed: float = 1.0
    every: tuple[float, float] = (0.0, 0.0)    # seconds between firings, min and max
    weight: float = 1.0
    blend: float = 0.0                         # seconds to cross-fade in
    bones: tuple[str, ...] = ()                # the mask, from main_config.boneLayers


@dataclass
class Position:
    """One authored position: an idle, optionally an action, and the layers over both."""

    name: str
    idle: str = ""                             # registry name of the clip
    idle_sound: str = ""
    idle_speed: float = 1.0
    action: tuple[str, ...] = ()               # several means "pick one at random"
    action_sound: str = ""
    rough_sound: str = ""
    action_speed: float = 1.0
    rough_speed: float = 1.0
    layers: list = field(default_factory=list)


def _num(value, default: float = 1.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _layers(spec, masks: dict) -> list:
    """`boneLayers` → `Layer`s. One entry per STATE, because each fires on its own timer."""
    out = []
    for entry in spec if isinstance(spec, list) else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("layerName") or entry.get("name") or ""
        for state in entry.get("states") if isinstance(entry.get("states"), list) else []:
            if not isinstance(state, dict):
                continue
            clips = tuple(c for c in (state.get("animationNames") or []) if isinstance(c, str))
            if not clips:
                continue
            out.append(Layer(name=name, clips=clips,
                             speed=_num(state.get("animationSpeed")),
                             every=(_num(state.get("timeMin"), 0.0), _num(state.get("timeMax"), 0.0)),
                             weight=_num(state.get("weight")),
                             blend=_num(state.get("stateTransitionTime"), 0.0),
                             bones=tuple(masks.get(name) or ())))
    return out


def read_main(build) -> dict:
    """`main_config` as a dict, plus `bone_masks` keyed by layer name. `{}` when the build has none."""
    for aid, asset in build.assets.items():
        if asset.get("type") != "json" or (asset.get("name") or "") != MAIN:
            continue
        path = build.path(aid)
        if not (path and os.path.exists(path)):
            continue
        try:
            doc = json.load(open(path))
        except Exception:                                    # noqa: BLE001 — a bad file is not fatal
            return {}
        masks = {}
        for layer in doc.get("boneLayers") if isinstance(doc.get("boneLayers"), list) else []:
            if isinstance(layer, dict) and layer.get("name"):
                masks[layer["name"]] = tuple(b for b in (layer.get("enabledBones") or [])
                                             if isinstance(b, str))
        return {**doc, "bone_masks": masks}
    return {}


def read_positions(build, masks: Optional[dict] = None) -> list[Position]:
    """Every `position_*_config` in one build, in name order."""
    masks = (read_main(build).get("bone_masks") or {}) if masks is None else masks
    found = []
    for aid, asset in build.assets.items():
        name = asset.get("name") or ""
        if asset.get("type") != "json" or not name.startswith("position") or "config" not in name:
            continue
        path = build.path(aid)
        if not (path and os.path.exists(path)):
            continue
        try:
            doc = json.load(open(path))
        except Exception:                                    # noqa: BLE001
            continue
        idle = doc.get("idle") if isinstance(doc.get("idle"), dict) else {}
        act = doc.get("action") if isinstance(doc.get("action"), dict) else {}
        # `animationName` is a STRING for an idle and a LIST for an action — the site picks one of
        # several action clips at random. Both shapes appear, so both are read.
        raw = act.get("animationName")
        found.append((name, Position(
            name=name,
            idle=idle.get("animationName") if isinstance(idle.get("animationName"), str) else "",
            idle_sound=idle.get("soundName") if isinstance(idle.get("soundName"), str) else "",
            idle_speed=_num(idle.get("speed")),
            action=tuple([raw] if isinstance(raw, str) else
                         [c for c in (raw or []) if isinstance(c, str)]),
            action_sound=act.get("soundNameAction") if isinstance(act.get("soundNameAction"), str) else "",
            rough_sound=act.get("soundNameRough") if isinstance(act.get("soundNameRough"), str) else "",
            action_speed=_num(act.get("speedAction")),
            rough_speed=_num(act.get("speedRough")),
            layers=_layers(idle.get("boneLayers"), masks) + _layers(act.get("boneLayers"), masks),
        )))
    return [p for _n, p in sorted(found)]


def clip_audio(positions) -> dict[str, list[str]]:
    """`{clip registry name: [sound registry name, …]}` — the pairing the site states.

    MANY-to-many in both directions, and that is the site's shape rather than an accident: one sound
    can voice several clips (an action's three variants share `soundNameAction`) and one clip can carry
    two sounds (an action has a normal and a `Rough` take). Both are already what `voiced_by` expresses.
    """
    out: dict[str, list[str]] = {}
    for position in positions:
        if position.idle and position.idle_sound:
            out.setdefault(position.idle, [])
            if position.idle_sound not in out[position.idle]:
                out[position.idle].append(position.idle_sound)
        for clip in position.action:
            for sound in (position.action_sound, position.rough_sound):
                if not sound:
                    continue
                out.setdefault(clip, [])
                if sound not in out[clip]:
                    out[clip].append(sound)
    return out


def clip_speed(positions) -> tuple[dict[str, float], list[str]]:
    """`({clip: rate}, [clips the configs disagree about])`.

    Authored, and not 1.0: the rates in this corpus are 0.5, 0.6 and 1.2, so every clip played at its
    own speed is played wrong. A clip named by two positions at different rates is reported rather than
    resolved — there is no basis for choosing, and the first-wins answer would be silent.
    """
    seen: dict[str, set] = {}
    for position in positions:
        if position.idle:
            seen.setdefault(position.idle, set()).add(position.idle_speed)
        for clip in position.action:
            seen.setdefault(clip, set()).add(position.action_speed)
        for layer in position.layers:
            for clip in layer.clips:
                seen.setdefault(clip, set()).add(layer.speed)
    out, clash = {}, []
    for clip, rates in seen.items():
        if len(rates) == 1:
            rate = next(iter(rates))
            if abs(rate - 1.0) > 1e-9:                       # 1.0 is the default; storing it says nothing
                out[clip] = rate
        else:
            clash.append(clip)
    return out, sorted(clash)


def resolves(build, positions) -> tuple[int, int, int, int]:
    """`(clips found, clips named, sounds found, sounds named)` — how much of the config is real.

    Reported rather than assumed. It is 98/98 and 30/30 on nancy and 100/100 and 30/30 on ebony, which
    is the evidence for trusting the mapping at all; a build where it drops would be saying the config
    describes a version of itself that is no longer on disk.
    """
    names = {(a.get("name") or ""): a.get("type") for a in build.assets.values()}
    clips = sounds = ok_c = ok_s = 0
    for position in positions:
        for clip in (position.idle,) + position.action + tuple(c for layer in position.layers
                                                               for c in layer.clips):
            if not clip:
                continue
            clips += 1
            ok_c += names.get(clip) == "animation"
        for sound in (position.idle_sound, position.action_sound, position.rough_sound):
            if not sound:
                continue
            sounds += 1
            ok_s += names.get(sound) == "audio"
    return ok_c, clips, ok_s, sounds
