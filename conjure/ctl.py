"""Conjure control plane — drive the world server directly from the terminal, no LLM in the loop.

The deterministic counterpart to `conjure.cli` (which talks to the *agent* server and puts a director
between you and the world). Everything here is a plain HTTP call to the world server's REST API — the
same endpoints the agent reaches through MCP (`mcp_server.place_asset` → `POST /place_asset`, exactly
as `ctl asset` does). Skipping the LLM is the point: no API spend, no nondeterminism, no waiting on a
model when you're debugging placement math or reindexing the library.

    python -m conjure.ctl                      # print the world
    python -m conjure.ctl asset "oak tree" --size 7
    python -m conjure.ctl image "an oil painting of a red dragon"
    python -m conjure.ctl skybox "a misty pine forest"
    python -m conjure.ctl add box --color red --pos 0 1 -3
    python -m conjure.ctl reindex

The world server must be running (`python -m conjure`). Quiet by default; `-v/--verbose` prints the
raw JSON response plus library logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid

import httpx

from .config import Settings, get_settings


# --------------------------------------------------------------------------- helpers

def _server_ok(s: Settings) -> bool:
    try:
        httpx.get(f"{s.world_url}/world", timeout=3.0)
        return True
    except Exception:
        return False


def _post(s: Settings, path: str, body: dict) -> dict:
    r = httpx.post(f"{s.world_url}{path}", json=body, timeout=240.0)
    r.raise_for_status()
    return r.json()


def _get(s: Settings, path: str) -> dict:
    r = httpx.get(f"{s.world_url}{path}", timeout=30.0)
    r.raise_for_status()
    return r.json()


def _say(obj: dict, verbose: bool, fallback: str) -> None:
    if verbose:
        print(json.dumps(obj, indent=2))
    elif obj.get("ok") is False:
        print(f"error: {obj.get('error', 'unknown error')}")
    else:
        print(fallback)


def _working(msg: str) -> None:
    """A one-line status to stderr so a slow image generation doesn't look like a freeze."""
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- world

def cmd_world(s: Settings, a) -> None:
    doc = _get(s, "/world")
    print(f"{doc.get('name', '')} (rev {doc['rev']}), {len(doc['entities'])} entities:")
    for e in doc["entities"]:
        c, m = e.get("components", {}), e.get("meta", {})
        if "gltf-model" in c:
            d = f"model {m.get('title', '?')!r}"
        elif c.get("material", {}).get("src"):
            d = f"image {(m.get('prompt') or m.get('title') or '?')!r}"
        elif "grid" in c:
            d = "grid"
        else:
            d = f"{c.get('geometry', {}).get('primitive', '?')} {c.get('material', {}).get('color', '')}".strip()
        print(f"  {e['id']}: {d} @ {e.get('transform', {}).get('position')}")


def cmd_add(s: Settings, a) -> None:
    eid = a.name or f"ent_{a.shape}_{uuid.uuid4().hex[:6]}"
    entity = {
        "id": eid,
        "transform": {"position": a.pos or [0.0, 1.0, -3.0]},
        "components": {"geometry": {"primitive": a.shape}, "material": {"color": a.color}},
    }
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "add", "entity": entity}]})
    print(f"added {eid} (rev {r['rev']})") if not a.verbose else _say(r, True, "")


def cmd_move(s: Settings, a) -> None:
    r = _post(s, "/patch", {"origin": "cli",
                            "ops": [{"op": "update", "id": a.id, "set": {"transform.position": a.pos}}]})
    print(f"moved {a.id} (rev {r['rev']})")


def cmd_remove(s: Settings, a) -> None:
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "remove", "id": a.id}]})
    print(f"removed {a.id} (rev {r['rev']})")


def cmd_env(s: Settings, a) -> None:
    sets: dict = {}
    if a.sky_color:
        sets["sky"] = {"color": a.sky_color}
    if a.fog_color or a.fog_density is not None:
        fog = {"type": "exponential"}
        if a.fog_color:
            fog["color"] = a.fog_color
        if a.fog_density is not None:
            fog["density"] = a.fog_density
        sets["fog"] = fog
    if not sets:
        print("nothing to set")
        return
    r = _post(s, "/patch", {"origin": "cli", "ops": [{"op": "env", "set": sets}]})
    print(f"environment updated (rev {r['rev']})")


# --------------------------------------------------------------------------- content

def cmd_asset(s: Settings, a) -> None:
    body = {"query": a.query, "size_m": a.size}
    if a.pos:
        body["position"] = a.pos
    _say(_post(s, "/place_asset", body), a.verbose, f"placed asset for {a.query!r}")


def cmd_image(s: Settings, a) -> None:
    # Procurement is decoupled from placement; the CLI runs both steps for convenience.
    gen_body = {"prompt": a.prompt}
    if a.transparent:
        gen_body["transparent"] = True
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating image…")
    procured = _post(s, "/images/generate", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    body = {"image_id": procured["image_id"]}
    if a.pos:
        body["position"] = a.pos
    if a.size is not None:
        body["size_m"] = a.size
    _say(_post(s, "/place_image", body), a.verbose, f"placed image ({procured.get('provider', '?')})")


def cmd_skybox(s: Settings, a) -> None:
    gen_body = {"prompt": a.prompt}
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating skybox (high-res — this can take a minute)…")
    procured = _post(s, "/images/skybox", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    _say(_post(s, "/set_skybox", {"image_id": procured["image_id"]}), a.verbose, "set skybox")


def cmd_grounded_skybox(s: Settings, a) -> None:
    gen_body = {"prompt": a.prompt}
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating grounded skybox (high-res — this can take a minute)…")
    procured = _post(s, "/images/grounded_skybox", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    set_body = {"image_id": procured["image_id"]}
    if a.height is not None:
        set_body["height"] = a.height
    if a.radius is not None:
        set_body["radius"] = a.radius
    _say(_post(s, "/set_grounded_skybox", set_body), a.verbose, "set grounded skybox")


def cmd_texture(s: Settings, a) -> None:
    # generate an image, then map it onto a real surface (floor/ceiling/wall/all)
    gen_body = {"prompt": a.prompt}
    if a.generator:
        gen_body["generator"] = a.generator
    _working("generating image…")
    procured = _post(s, "/images/generate", gen_body)
    if procured.get("ok") is False:
        _say(procured, a.verbose, "")
        return
    body = {"target": a.target, "image_id": procured["image_id"]}
    if a.repeat is not None:
        body["repeat"] = a.repeat
    _say(_post(s, "/texture_surface", body), a.verbose, f"textured {a.target}")


def cmd_style(s: Settings, a) -> None:
    body = {"target": a.target}
    if a.color:
        body["color"] = a.color
    if a.opacity is not None:
        body["opacity"] = a.opacity
    _say(_post(s, "/style_surface", body), a.verbose, f"styled {a.target}")


def cmd_edit(s: Settings, a) -> None:
    _working("editing image…")
    _say(_post(s, "/edit_image", {"id": a.id, "prompt": a.prompt}), a.verbose, f"edited {a.id}")


def cmd_outpaint(s: Settings, a) -> None:
    body = {"id": a.id}
    if a.aspect:
        body["aspect"] = a.aspect
    _working("outpainting image…")
    _say(_post(s, "/outpaint_image", body), a.verbose, f"outpainted {a.id}")


def cmd_skybox_from(s: Settings, a) -> None:
    _working("building skybox from image (high-res — this can take a minute)…")
    _say(_post(s, "/skybox_from_image", {"id": a.id}), a.verbose, f"skybox from {a.id}")


def cmd_generators(s: Settings, a) -> None:
    out = _get(s, "/images/generators")
    if a.verbose:
        print(json.dumps(out, indent=2))
        return
    for g in out.get("generators", []):
        c = g["capabilities"]
        vendor = f" ({g['vendor']})" if g.get("vendor") else ""
        print(f"{g['name']}{vendor}: ops={','.join(c['operations'])}, edit={c['edit_mode']}, "
              f"max={c['max_resolution']}px, aspect={c['aspect']}, transparency={c['transparency']}")
    print(f"defaults: {out.get('defaults', {})}")


# --------------------------------------------------------------------------- room chrome

def cmd_annotate(s: Settings, a) -> None:
    sets = {"spacePresentation.annotations": a.state != "off", "spacePresentation.annotationDims": bool(a.dims)}
    if a.color is not None:
        sets["spacePresentation.annotationColor"] = a.color
    if a.opacity is not None:
        sets["spacePresentation.annotationOpacity"] = a.opacity
    r = _post(s, "/patch", {"ops": [{"op": "env", "set": sets}]})
    print(f"annotations {a.state}{' +dims' if a.dims else ''} (rev {r['rev']})")


def cmd_edges(s: Settings, a) -> None:
    sets = {"spacePresentation.edgesVisible": a.state != "off"}
    if a.color is not None:
        sets["spacePresentation.edgeColor"] = a.color
    if a.opacity is not None:
        sets["spacePresentation.edgeOpacity"] = a.opacity
    r = _post(s, "/patch", {"ops": [{"op": "env", "set": sets}]})
    print(f"edges {a.state} (rev {r['rev']})")


def cmd_grabmode(s: Settings, a) -> None:
    """Switch `grab`'s mode. A singleton module reuses and reconfigures its one live instance, so this is a
    plain re-conjure rather than a second entity — the running component sees a config update."""
    r = _post(s, "/module", {"module": "grab", "config": {"mode": a.mode}})
    if not r.get("ok"):
        print(f"grab mode: {r.get('error', 'failed')}")
        return
    print(f"grab mode → {a.mode}")


def cmd_worldframe(s: Settings, a) -> None:
    """Adjust or reset the user's skybox / void-world frame deltas (docs/specs/dynamics.md §8b)."""
    if a.reset:
        r = _post(s, "/world_frame", {"reset": a.reset})
        print(f"world frame reset: {a.reset}" if r.get("ok") else f"reset: {r.get('error', 'failed')}")
        return
    body: dict = {}
    sky = {k: v for k, v in (("yaw", a.sky_yaw), ("scale", a.sky_scale)) if v is not None}
    if sky:
        body["sky"] = sky
    frame = {k: v for k, v in (("yaw", a.void_yaw),) if v is not None}
    if a.void_offset is not None:
        frame["offset"] = list(a.void_offset)
    if frame:
        body["frame"] = frame
    if not body:
        print("nothing to change — pass --sky-yaw/--sky-scale/--void-yaw/--void-offset or --reset")
        return
    r = _post(s, "/world_frame", body)
    print(f"world frame {r['set']}" if r.get("ok") else f"world frame: {r.get('error', 'failed')}")


# --------------------------------------------------------------------------- library maintenance

def cmd_reindex(s: Settings, a) -> None:
    body = {"kind": a.kind} if a.kind else {}
    r = _post(s, "/library/reindex", body)
    if r.get("ok") is False:
        _say(r, a.verbose, "")
        return
    cleared = f", cleared {r['cleared']} stale" if r.get("cleared") else ""
    print(f"reindex: queued {r.get('queued', 0)} asset(s) for embedding{cleared} "
          "(runs in the background on the server)")


def cmd_caption(s: Settings, a) -> None:
    r = _post(s, "/library/caption", {})
    if r.get("ok") is False:
        _say(r, a.verbose, "")
        return
    print(f"caption: queued {r.get('queued', 0)} asset(s) for description "
          "(runs in the background on the server)")


def parse_bones(specs) -> tuple[dict, str]:
    """`--bone NAME:AXIS=VALUE` into the `pose` dict the endpoint takes. `(pose, error)`.

    The one thing that needs care: `aim` takes a DIRECTION — a word (`up`) or a three-vector
    (`0,1,1`) — while `bend`/`spread`/`turn` take degrees. Sending a string where the server wants a
    number surfaces as a validation error three layers down that names neither the bone nor the flag,
    so the split happens here where the input is still in front of you.
    """
    pose: dict = {}
    for spec in specs:
        bone, _, rest = spec.partition(":")
        axis, _, value = rest.partition("=")
        if not bone or not axis or value == "":
            return {}, f"--bone wants NAME:AXIS=VALUE, got {spec!r}"
        if axis == "aim":
            parts = value.replace(",", " ").split()
            if len(parts) == 3:
                try:
                    pose.setdefault(bone, {})[axis] = [float(x) for x in parts]
                except ValueError:
                    return {}, f"{value!r} is not a direction or a three-vector (in {spec!r})"
            else:
                pose.setdefault(bone, {})[axis] = value
        else:
            try:
                pose.setdefault(bone, {})[axis] = float(value)
            except ValueError:
                return {}, f"{value!r} is not a number of degrees (in {spec!r})"
    return pose, ""


def cmd_pose(s: Settings, a) -> None:
    """Put a placed figure into a named pose, adjust one bone, or clear it.

    `--bone` is `NAME:AXIS=DEGREES` (`leftUpperArm:bend=45`) or `NAME:aim=DIRECTION`
    (`rightUpperArm:aim=up`). Repeatable, and it composes onto a named pose — the endpoint merges per
    BONE onto whatever is already there, so adjusting an arm does not reset the legs.
    """
    if a.clear:
        _say(_post(s, "/figure", {"id": a.id, "clear": True}), a.verbose,
             f"cleared the pose on {a.id}")
        return
    pose, bad = parse_bones(a.bone or [])
    if bad:
        print(f"error: {bad}")
        return
    if not pose and not a.named:
        print("error: name a pose, or give at least one --bone (or --clear)")
        return
    body: dict = {"id": a.id}
    if a.named:
        body["named"] = a.named
    if pose:
        body["pose"] = pose
    out = _post(s, "/figure", body)
    if out.get("ok") is False:
        _say(out, a.verbose, "")
        return
    # Both halves when there are two. `posed` comes back holding the NAMED pose's whole expansion —
    # eleven bones for `kneel` — so printing it swallows the adjustment that was the point of the call,
    # and printing only the name hides it completely. The bones asked for by hand are known here.
    what = out.get("named") or ", ".join(out.get("posed") or []) or "nothing"
    if out.get("named") and pose:
        what += ", adjusted: " + ", ".join(sorted(pose))
    lines = [f"{a.id}: {what} ({out.get('bones', 0)} bone(s) held)"]
    # The refusals are the point of reporting at all. A joint limit silently clamping looked like the
    # tool ignoring the request — the director asked for 90 degrees of hip extension twice in one
    # session with nothing telling it otherwise.
    if out.get("limited"):
        lines.append("clamped by joint limits: " + ", ".join(str(x) for x in out["limited"]))
    if out.get("skipped"):
        lines.append("bones this figure does not have: " + ", ".join(out["skipped"]))
    if out.get("needs"):
        lines.append(f"needs something to rest on: {out['needs']} — nothing is put there for you")
    _say(out, a.verbose, "\n".join(lines))


def cmd_wear(s: Settings, a) -> None:
    """Wear a placed hand model, take it off, dial its fingertips, or say what there is to wear.

    `--hand auto` is the default and almost always right: which hand a model IS was measured from its
    geometry at import, not read from the `_L` in its name. Wearing does not consume the model — it
    occupies it, so `--hand off` puts it back exactly where it was placed.

    A hand has to be PLACED before it can be worn, and nothing in this CLI placed a library model — so
    `--place` does it, and `--pair` does both sides at once, which is what anyone actually wants.
    """
    a.tip_out_given = a.tip_out is not None
    if a.tip_out is None:
        a.tip_out = "0"
    if a.pair:
        return _wear_pair(s, a)
    if not a.id:
        # `--tip-out` with no id adjusts EVERY worn hand. It takes effect on the next frame — the
        # component reads `tipOut` per frame and A-Frame replaces `this.data` on the patch, and there
        # is no `update` handler to re-run `init`, so the skeleton, the bind pose and the captured rest
        # all survive. Dialling a number in should not cost a reload, and it does not.
        if a.tip_out_given:
            return _wear_tip(s, a)
        return _wear_list(s)
    eid = a.id
    if a.place:
        put = _post(s, "/place_cached_asset", {"id": a.id, "name": a.name or None})
        if not put.get("ok"):
            print(f"wear: could not place {a.id}: {put.get('error', 'failed')}")
            return
        eid = put["id"]
        print(f"placed {a.id} as {eid}")
    out = _post(s, "/figure/hand", {"id": eid, "hand": a.hand, "tip_out": a.tip_out})
    if not out.get("ok"):
        print(f"wear: {out.get('error', 'failed')}")
        return
    if not out.get("worn"):
        _say(out, a.verbose, "taken off — back where it was placed")
        return
    tip = f", fingertips out by {a.tip_out}" if a.tip_out not in ("0", 0) else ""
    _say(out, a.verbose, f"{eid}: worn on the {out.get('hand')} hand, "
                         f"{out.get('joints')} joints driven{tip}")


def _wear_list(s: Settings) -> None:
    out = _get(s, "/figure/hands")
    placed, lib = out.get("placed") or [], out.get("library") or []
    if placed:
        print("placed, and wearable right now:")
        for h in placed:
            state = f"WORN on the {h['worn']}" if h["worn"] else "not worn"
            tip = h.get("tip_out") or "0"
            if h["worn"] and tip not in ("0", ""):
                state += f", fingertips out by {tip}"
            print(f"  {h['id']:<22} {h['side'] or '?':<6} {h['label'][:22]:<22} {state}")
        print("\n  wear --tip-out 8 | radius | off       dial the fingertips, live")
    if lib:
        print("\nin the library — place one with `wear <asset-id> --place`:")
        for h in lib:
            dress = ", ".join(h.get("materials") or []) or "NO MATERIALS — it will render grey"
            print(f"  {h['id']:<24} {h['side'] or '?':<6} {h['label'][:20]:<20} {dress}")
    if not placed and not lib:
        # Two absences with one appearance, so say which. A library catalogued before hands existed
        # has no `hand_wearable` on any row and needs a refresh; a library with no hand models in it
        # needs an import, and a refresh would waste the time.
        print("no wearable hands. If you have hand models, they were catalogued before hands were\n"
              "measured — RESTART THE SERVER and run `refresh-models`, which re-derives what the\n"
              "server process knows rather than what is on disk.")
    if not placed and lib:
        print("\n  conjure-ctl wear --pair          places both sides and wears them")


def _wear_tip(s: Settings, a) -> None:
    """Push the fingertips of every WORN hand in or out, live."""
    worn = [h for h in (_get(s, "/figure/hands").get("placed") or []) if h["worn"]]
    if not worn:
        print("nothing is worn — `wear --pair` first, then `wear --tip-out MM` to dial it")
        return
    done = []
    for h in worn:
        out = _post(s, "/figure/hand", {"id": h["id"], "hand": h["worn"], "tip_out": a.tip_out})
        if out.get("ok"):
            done.append(h["id"])
        else:
            print(f"wear: {h['id']}: {out.get('error', 'failed')}")
    _say({"ok": bool(done), "adjusted": done, "tip_out": a.tip_out}, a.verbose,
         f"fingertips out by {a.tip_out} on {', '.join(done)} — live, no reload")


def _wear_pair(s: Settings, a) -> None:
    """Place both hands and wear them — the whole test, in one command.

    **Matched on MATERIAL NAMES, not taken in order.** The catalog's hand files are not one pair: two
    are the textured VR set, one is an orphaned AR left, and two rights carry no materials at all.
    Taking the first of each side put a grey untextured right next to a textured left on the first
    wearing, which reads as a broken import rather than as two files that were never a pair.
    """
    lib = (_get(s, "/figure/hands").get("library") or [])
    lefts = [h for h in lib if h["side"] == "left"]
    rights = [h for h in lib if h["side"] == "right"]
    chosen = {}
    for left in lefts:                       # already best-dressed first
        mate = next((r for r in rights if r["materials"] == left["materials"]), None)
        if mate:
            chosen = {"left": left, "right": mate}
            break
    if not chosen:
        # No two agree, so say so rather than assembling a mismatched pair in silence.
        if lefts and rights:
            print("wear: no matching pair — no left and right share a material set. Taking the "
                  "best-dressed of each; expect them to look different.")
        chosen = {"left": lefts[0] if lefts else None, "right": rights[0] if rights else None}

    worn = []
    for side in ("left", "right"):
        have = chosen.get(side)
        if not have:
            print(f"wear: no {side} hand in the library")
            continue
        put = _post(s, "/place_cached_asset", {"id": have["id"], "name": f"hand_{side}"})
        if not put.get("ok"):
            print(f"wear: could not place the {side} hand: {put.get('error', 'failed')}")
            continue
        out = _post(s, "/figure/hand", {"id": put["id"], "hand": side,
                                        "tip_out": a.tip_out})
        if out.get("ok"):
            worn.append(f"{put['id']} ({side})")
        else:
            print(f"wear: placed {put['id']} but could not wear it: {out.get('error')}")
    _say({"ok": bool(worn), "worn": worn}, a.verbose,
         ("wearing " + ", ".join(worn)) if worn else "nothing worn")


def cmd_dress(s: Settings, a) -> None:
    """Turn parts of a figure off and on — clothing, hair, shoes, accessories.

    `--hide`/`--show` take a CATEGORY or a single mesh name; `--only-body` strips everything removable,
    hair included, which is almost never what "take her dress off" means."""
    body = {"id": a.id, "hide": a.hide or [], "show": a.show or [], "only_body": bool(a.only_body)}
    out = _post(s, "/figure/parts", body)
    if out.get("ok") is False:
        _say(out, a.verbose, "")
        return
    by_cat = out.get("hidden_by_category") or {}
    # By CATEGORY as well as by mesh: a list of mesh names is not something anyone reads back.
    summary = ", ".join(f"{c} ({len(n)})" for c, n in sorted(by_cat.items())) if by_cat else "nothing"
    lines = [f"{a.id}: hidden — {summary}"]
    if out.get("hidden"):
        lines.append("  meshes: " + ", ".join(out["hidden"]))
    # What is STILL ON, not what the figure owns. `removable` is the total per category and reads as a
    # remainder next to a list of what just came off — it said `clothing (2)` with both already hidden.
    groups = out.get("removable") or {}
    left = {k: n - len(by_cat.get(k) or []) for k, n in groups.items()}
    left = {k: n for k, n in left.items() if n > 0}
    lines.append("  still on: "
                 + (", ".join(f"{k} ({n})" for k, n in sorted(left.items())) or "nothing removable"))
    if out.get("unknown"):
        lines.append("  neither a category nor a mesh here: " + ", ".join(out["unknown"]))
    _say(out, a.verbose, "\n".join(lines))


def cmd_clips(s: Settings, a) -> None:
    """What a figure can dance to. Two lists, never merged: what SHIPPED with her, and what merely fits
    her rig — `--all` for the second, which is much the larger and much the less trustworthy (many clips
    are authored around furniture that is not in the room)."""
    q = (f"/figure/clips?id={a.id}" + ("&all=true" if a.all else "")
         + (f"&kind={a.kind}" if a.kind else "") + ("&voiced=true" if a.voiced else ""))
    out = _get(s, q)
    if out.get("ok") is False:
        _say(out, a.verbose, "")
        return

    def show(rows):
        for r in rows:
            secs = f"{r['duration_s']:.0f}s" if isinstance(r.get("duration_s"), (int, float)) else "?"
            print(f"  {(r['label'] or ''):16} {(r.get('kind') or 'clip'):7} {secs:>6}"
                  f"{'  voiced' if r.get('voiced') else '        '}  {r['id']}")

    shipped = out.get("shipped") or []
    print(f"{a.id} — rig {out.get('rig_sig') or 'unknown'}")
    print(f"shipped with her ({len(shipped)}):")
    show(shipped) if shipped else print("  none")
    if a.all:
        other = out.get("compatible") or []
        print(f"also fits this rig ({len(other)}) — authored for other figures:")
        show(other) if other else print("  none")
    elif out.get("compatible_count"):
        print(f"{out['compatible_count']} more fit this rig — pass --all to list them.")


def cmd_clip(s: Settings, a) -> None:
    """Play a clip on a figure, or `--stop`. The clip is a label or an asset id; a label resolves to the
    one that shipped with THIS figure, since `10_action` is eleven different clips library-wide."""
    if a.stop or not a.clip:
        # No clip named is the same request as `--stop`, and it has to be routed here: the endpoint
        # answers an empty clip with the STOP shape, which has no `clip` key to print.
        _say(_post(s, "/figure/clip", {"id": a.id, "stop": True}), a.verbose,
             f"stopped the animation on {a.id}")
        return
    body = {"id": a.id, "clip": a.clip, "loop": not a.once, "force": a.force}
    if a.speed is not None:                    # absent means "use the rate the capture authored"
        body["speed"] = a.speed
    out = _post(s, "/figure/clip", body)
    if out.get("ok") is False:
        _say(out, a.verbose, "")
        for c in out.get("candidates") or []:
            print(f"  {c['id']}  {c.get('tags') or 'untagged'}")
        return
    secs = out.get("duration_s")
    length = f", {secs:.0f}s" if isinstance(secs, (int, float)) else ""
    if out.get("voiced"):
        voice = ", with her voice"
    elif out.get("voiced_alternatives"):
        voice = f" — silent; {out['voiced_alternatives']} of her clips are voiced (--voiced lists them)"
    else:
        voice = " — silent"
    _say(out, a.verbose,
         f"playing {out.get('label') or out['clip']} on {a.id}{length}"
         f"{' (looping)' if out.get('loop') else ''}{voice}"
         + (f"\nwarning: {out['warning']}" if out.get("warning") else ""))


def cmd_refresh_models(s: Settings, a) -> None:
    """Re-derive every model's catalog attributes from its bytes — bone map, frame, joint limits, and
    whether it is a figure at all. Placement does this one model at a time; this is the batch form, for
    after a build that changes what extraction knows."""
    _working("re-extracting models…")
    out = _post(s, "/library/refresh-models", {"force": bool(a.force)})
    # A refresh runs INSIDE the server, so it can only derive what that process's code knows. A server
    # started before a build reports "0 of 98 updated" and reads as "the build needed no refresh" —
    # measured: a server 23 hours old against a FRAME_REV that had moved 18 -> 19, and `--force` would
    # not have helped either, because the old process has no idea the new attributes exist.
    from .figures import FRAME_REV as LOCAL_FRAME_REV
    running = out.get("frame_rev")
    if running != LOCAL_FRAME_REV:
        # ABSENT counts as stale, and has to: the field was added by the same build that needed this
        # warning, so a server old enough to be the problem is also too old to report its revision.
        # Leaving `None` unhandled meant the one case the check existed for was the one it skipped.
        seen = "does not report one" if running is None else f"is at {running}"
        print(f"  ! this server's FRAME_REV {seen}; the code on disk is at {LOCAL_FRAME_REV}.\n"
              f"    A refresh derives what the SERVER knows, so RESTART IT and run this again — "
              f"--force will not help, the running process cannot see the new attributes.")
    updated = out.get("updated") or []
    for row in updated:
        print(f"  {row['label'] or row['id']}: rigged={row['rigged']} bones={row['bones']}")
    tail = ""
    if out.get("clips_respelled"):
        tail = (f" {out['clips_respelled']} of {out.get('clips_checked', 0)} clip signature(s) "
                f"re-derived.")
    _say(out, a.verbose, f"{len(updated)} of {out.get('checked', 0)} model(s) updated.{tail}")


def cmd_retag_skyboxes(s: Settings, a) -> None:
    body = {"min_aspect": a.min_aspect} if a.min_aspect is not None else {}
    r = _post(s, "/library/retag-skyboxes", body)
    if r.get("ok") is False:
        _say(r, a.verbose, "")
        return
    print(f"re-tagged {r.get('retagged', 0)} wide image(s) as skyboxes")


# --------------------------------------------------------------------------- argparse

def _pos(p):
    p.add_argument("--pos", nargs=3, type=float, metavar=("X", "Y", "Z"), help="position in meters")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="conjure-ctl",
                                description="Drive the Conjure world server directly — no LLM in the loop.")
    p.add_argument("-v", "--verbose", action="store_true", help="show raw JSON responses and library logs")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("world", help="print the current world").set_defaults(fn=cmd_world)

    a = sub.add_parser("add", help="add a primitive shape"); a.set_defaults(fn=cmd_add)
    a.add_argument("shape"); a.add_argument("--color", default="white"); a.add_argument("--name"); _pos(a)

    a = sub.add_parser("move", help="move an entity"); a.set_defaults(fn=cmd_move)
    a.add_argument("id"); a.add_argument("pos", nargs=3, type=float, metavar=("X", "Y", "Z"))

    a = sub.add_parser("remove", help="remove an entity"); a.set_defaults(fn=cmd_remove)
    a.add_argument("id")

    a = sub.add_parser("env", help="set sky/fog"); a.set_defaults(fn=cmd_env)
    a.add_argument("--sky-color"); a.add_argument("--fog-color"); a.add_argument("--fog-density", type=float)

    a = sub.add_parser("grab-mode", help="set what GRIP on empty space adjusts")
    a.set_defaults(fn=cmd_grabmode)
    a.add_argument("mode", choices=("object", "skybox", "void"))

    a = sub.add_parser("world-frame", help="adjust/reset the skybox or void-world frame")
    a.set_defaults(fn=cmd_worldframe)
    a.add_argument("--sky-yaw", type=float, help="degrees, relative to the derived frame")
    a.add_argument("--sky-scale", type=float, help="uniform scale factor (>0)")
    a.add_argument("--void-yaw", type=float, help="degrees, void worlds only")
    a.add_argument("--void-offset", nargs=2, type=float, metavar=("X", "Z"), help="metres, horizontal only")
    a.add_argument("--reset", choices=("sky", "frame", "all"), help="back to the derived frame")

    a = sub.add_parser("asset", help="place a real 3D model (Poly Pizza)"); a.set_defaults(fn=cmd_asset)
    a.add_argument("query"); a.add_argument("--size", type=float, default=1.0, help="real-world size, meters"); _pos(a)

    a = sub.add_parser("image", help="generate + hang an image"); a.set_defaults(fn=cmd_image)
    a.add_argument("prompt"); a.add_argument("--size", type=float)
    a.add_argument("--transparent", action="store_true", help="cut-out with a transparent background")
    a.add_argument("--generator", help="force an image generator (else best default)"); _pos(a)

    a = sub.add_parser("skybox", help="generate a 360 skybox"); a.set_defaults(fn=cmd_skybox)
    a.add_argument("prompt"); a.add_argument("--generator", help="force an image generator")

    a = sub.add_parser("grounded-skybox", help="generate a 360 skybox projected onto the floor")
    a.set_defaults(fn=cmd_grounded_skybox)
    a.add_argument("prompt"); a.add_argument("--generator", help="force an image generator")
    a.add_argument("--height", type=float, help="metres above the ground (default 1.6)")
    a.add_argument("--radius", type=float, help="ground reach before the horizon (default 30)")

    a = sub.add_parser("texture", help="map a generated image onto a real surface"); a.set_defaults(fn=cmd_texture)
    a.add_argument("target", help="floor | ceiling | wall | all | <surface id>")
    a.add_argument("prompt"); a.add_argument("--repeat", type=float, help="tile NxN (use a seamless image)")
    a.add_argument("--generator", help="force an image generator")

    a = sub.add_parser("style", help="color / set transparency of a real surface"); a.set_defaults(fn=cmd_style)
    a.add_argument("target", help="floor | ceiling | wall | all | <surface id>")
    a.add_argument("--color", help="CSS name or #hex"); a.add_argument("--opacity", type=float, help="0..1")

    a = sub.add_parser("edit", help="edit an in-world image"); a.set_defaults(fn=cmd_edit)
    a.add_argument("id"); a.add_argument("prompt")

    a = sub.add_parser("outpaint", help="extend an in-world image wider"); a.set_defaults(fn=cmd_outpaint)
    a.add_argument("id"); a.add_argument("--aspect")

    a = sub.add_parser("skybox-from", help="turn an in-world image into the sky"); a.set_defaults(fn=cmd_skybox_from)
    a.add_argument("id")

    sub.add_parser("generators", help="list image generators + capabilities").set_defaults(fn=cmd_generators)

    a = sub.add_parser("annotate", help="toggle / restyle surface metadata labels"); a.set_defaults(fn=cmd_annotate)
    a.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    a.add_argument("--dims", action="store_true", help="also show surface dimensions")
    a.add_argument("--color", help="label text color (CSS name or #hex)")
    a.add_argument("--opacity", type=float, help="label opacity 0..1")

    a = sub.add_parser("edges", help="show/hide / restyle surface outline wireframe"); a.set_defaults(fn=cmd_edges)
    a.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    a.add_argument("--color", help="edge color (CSS name or #hex)")
    a.add_argument("--opacity", type=float, help="edge opacity 0..1")

    a = sub.add_parser("reindex", help="embed cataloged assets that have no vector yet")
    a.set_defaults(fn=cmd_reindex)
    a.add_argument("--kind", help="restrict to image | model | skybox | …")

    sub.add_parser("caption", help="backfill labels for assets with none (image→text via Gemini)") \
        .set_defaults(fn=cmd_caption)

    a = sub.add_parser("refresh-models", help="re-derive model attributes (figures, bone maps, limits)")
    a.set_defaults(fn=cmd_refresh_models)
    a.add_argument("--force", action="store_true", help="re-extract even rows already up to date")

    a = sub.add_parser("pose", help="pose a placed figure (named pose, or bone by bone)")
    a.set_defaults(fn=cmd_pose)
    a.add_argument("id", help="the ENTITY id of a placed figure")
    a.add_argument("named", nargs="?", default="", help="a pose from the library: kneel, sit, wave, …")
    a.add_argument("--bone", action="append", metavar="NAME:AXIS=VAL",
                   help="leftUpperArm:bend=45 · rightUpperArm:aim=up — repeatable, composes onto `named`")
    a.add_argument("--clear", action="store_true", help="drop the pose, back to the bind pose")

    a = sub.add_parser("dress", help="hide/show a figure's clothing, hair, shoes, accessories")
    a.set_defaults(fn=cmd_dress)
    a.add_argument("id", help="the ENTITY id of a placed figure")
    a.add_argument("--hide", action="append", metavar="CATEGORY|MESH",
                   help="clothing · shoes · hair · accessory — or one mesh name. Repeatable")
    a.add_argument("--show", action="append", metavar="CATEGORY|MESH", help="…and to bring back")
    a.add_argument("--only-body", dest="only_body", action="store_true",
                   help="strip everything removable, HAIR INCLUDED")

    a = sub.add_parser("wear", help="drive a placed HAND model from your tracked hand")
    a.set_defaults(fn=cmd_wear)
    a.add_argument("id", nargs="?", help="the ENTITY id of a placed hand — or an ASSET id with "
                                         "--place. With neither, lists what there is to wear")
    a.add_argument("--hand", default="auto", choices=["auto", "left", "right", "off"],
                   help="auto pairs on the MEASURED side (default); off puts it back down")
    a.add_argument("--place", action="store_true",
                   help="the id is a LIBRARY asset: place it first, then wear it")
    a.add_argument("--name", help="entity id to place it as (with --place)")
    a.add_argument("--pair", action="store_true",
                   help="place BOTH hands from the library and wear them — the whole test in one")
    # `default=None` and a separate flag, because 0 is a REAL value here: "put them back exactly
    # where the runtime says" is the thing you dial back to, and it must be distinguishable from
    # not having asked.
    a.add_argument("--tip-out", dest="tip_out", default=None, metavar="MM|radius",
                   help="push each fingertip out along its own bone: millimetres (`8`), `radius` for "
                        "one tip radius as the RUNTIME reports it (`radius:0.8` scales it), or `off` "
                        "to put them back exactly where the runtime says")

    a = sub.add_parser("clips", help="list the animations a placed figure can play")
    a.set_defaults(fn=cmd_clips)
    a.add_argument("id", help="the ENTITY id of a placed figure (see `conjure-ctl` with no args)")
    a.add_argument("--all", action="store_true", help="also list clips that merely fit her rig")
    a.add_argument("--kind", help="idle | action")
    a.add_argument("--voiced", action="store_true",
                   help="only clips carrying a voice track — it plays automatically with the animation")

    a = sub.add_parser("clip", help="play an animation on a placed figure"); a.set_defaults(fn=cmd_clip)
    a.add_argument("id", help="the ENTITY id of a placed figure")
    a.add_argument("clip", nargs="?", default="", help="clip label or asset id")
    a.add_argument("--stop", action="store_true", help="stop whatever is playing")
    a.add_argument("--once", action="store_true", help="play through once instead of looping")
    a.add_argument("--speed", type=float, default=None,
                   help="override the rate the capture authored for this clip")
    a.add_argument("--force", action="store_true",
                   help="play a clip from a DIFFERENT rig — it will look wrong; that is the point")

    a = sub.add_parser("retag-skyboxes", help="re-tag wide backfilled images as skyboxes")
    a.set_defaults(fn=cmd_retag_skyboxes)
    a.add_argument("--min-aspect", dest="min_aspect", type=float, help="width/height threshold (default 1.9)")

    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    for name in ("httpx", "mcp", "google_genai"):
        logging.getLogger(name).setLevel(logging.INFO if args.verbose else logging.WARNING)

    settings = get_settings()
    if not _server_ok(settings):
        print(f"World server not reachable at {settings.world_url}. Start it: python -m conjure")
        return 1

    fn = getattr(args, "fn", None) or cmd_world      # no subcommand → show the world (a harmless default)
    try:
        fn(settings, args)
    except KeyboardInterrupt:
        return 130
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
