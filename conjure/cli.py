"""Conjure CLI — drive the world from the terminal for quick, quiet, discrete testing.

The world server must be running (`python -m conjure`). Two ways to drive it:

  • Direct tool commands (deterministic):
        conjure-cli asset "oak tree" --size 7
        conjure-cli image "an oil painting of a red dragon"
        conjure-cli skybox "a misty pine forest"
        conjure-cli grounded-skybox "a meadow you can stand in"
        conjure-cli add box --color red --pos 0 1 -3
        conjure-cli world

  • The director, by text — no voice, no audio noise:
        conjure-cli say "put an oak tree in front of me and hang a sunset painting on the wall"
        conjure-cli                # no args → interactive director REPL (type instructions)

Quiet by default. Pass -v/--verbose for tool calls + library logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Optional

import httpx

from .config import DEFAULT_USER, Settings, get_settings


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


# --------------------------------------------------------------------------- direct commands

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
    # generate an image, then map it onto a room surface (floor/ceiling/wall/all)
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


def cmd_retag_skyboxes(s: Settings, a) -> None:
    body = {"min_aspect": a.min_aspect} if a.min_aspect is not None else {}
    r = _post(s, "/library/retag-skyboxes", body)
    if r.get("ok") is False:
        _say(r, a.verbose, "")
        return
    print(f"re-tagged {r.get('retagged', 0)} wide image(s) as skyboxes")


def cmd_annotate(s: Settings, a) -> None:
    sets = {"room.annotations": a.state != "off", "room.annotationDims": bool(a.dims)}
    if a.color is not None:
        sets["room.annotationColor"] = a.color
    if a.opacity is not None:
        sets["room.annotationOpacity"] = a.opacity
    r = _post(s, "/patch", {"ops": [{"op": "env", "set": sets}]})
    print(f"annotations {a.state}{' +dims' if a.dims else ''} (rev {r['rev']})")


def cmd_edges(s: Settings, a) -> None:
    sets = {"room.edgesVisible": a.state != "off"}
    if a.color is not None:
        sets["room.edgeColor"] = a.color
    if a.opacity is not None:
        sets["room.edgeOpacity"] = a.opacity
    r = _post(s, "/patch", {"ops": [{"op": "env", "set": sets}]})
    print(f"edges {a.state} (rev {r['rev']})")


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


# --------------------------------------------------------------------------- agent-server clients
#
# The CLI is a THIN client now (shared-session Step C1): the Director + shared transcript live in the
# agent server (conjure.agent_server); the CLI POSTs a turn and renders the SSE conversation stream. The
# agent is owned by the (separately launched) agent server, so --agent no longer selects it here — switch
# with the `conjure agent <name>` command instead. Start the server first: `python -m conjure.agent_server`.

# Whole-line inputs that leave the REPL (case-insensitive). Exact match only, so "exit the room" is
# still passed through as an instruction.
_QUIT_WORDS = {":q", ":quit", "q", "quit", "exit", "bye", "goodbye"}


def _agent_unreachable_msg(s: Settings, err: str) -> str:
    return (f"Agent server not reachable at {s.agent_url} ({err}).\n"
            f"Start it first:  python -m conjure.agent_server")


async def _repl_client(s: Settings, verbose: bool, user: str) -> None:
    """Interactive REPL as a thin client: a background task renders the shared conversation stream (and
    folds `context` into the prompt); the main loop reads a line and POSTs it. Eventually-consistent per
    prompt (shared-session-plan §8) — the reply prints when it arrives, the next prompt reflects context."""
    from .agent_client import (apply_context, post_turn, prompt_from_context, render_event, stream_events)

    ctx = {"agent": "agent", "llm": "", "user": user, "in_shell": False}
    stop = asyncio.Event()

    async def listen() -> None:
        while not stop.is_set():
            try:
                async for ev in stream_events(s.agent_url):
                    if ev.get("type") == "context":
                        apply_context(ctx, ev)
                        continue
                    out = render_event(ev, me=user, verbose=verbose)
                    if out is not None:
                        print(out)
            except Exception:  # noqa: BLE001 — server down/restarting: back off and reconnect
                if stop.is_set():
                    return
                await asyncio.sleep(1.0)

    listen_task = asyncio.create_task(listen())
    await asyncio.sleep(0.25)                               # let the first `context` arrive for prompt #1
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, lambda: input(prompt_from_context(ctx)))
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if line.lower() in _QUIT_WORDS:
                break
            if not line:
                continue
            res = await post_turn(s.agent_url, user, line)
            if res.get("error"):
                print(_agent_unreachable_msg(s, res["error"]))
            elif res.get("busy"):
                print("[busy — another turn is in progress; try again]")
    finally:
        stop.set()
        listen_task.cancel()


async def _say_client(s: Settings, verbose: bool, user: str, text: str) -> None:
    """One-shot: submit `text` and print only THIS turn's reply, then exit. Skips the backlog snapshot by
    waiting for our turn to begin — an utterance echoes our own `user_turn`; a command replies with a
    `notice` (never in the backlog, since notices aren't stored)."""
    import httpx

    from .agent_client import parse_sse_line, post_turn, render_event

    text = text.strip()
    started = False
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"{s.agent_url}/stream") as r:
                async for line in r.aiter_lines():
                    ev = parse_sse_line(line)
                    if ev is None:
                        continue
                    t = ev.get("type")
                    if t == "context":                     # subscribed → submit once
                        res = await post_turn(s.agent_url, user, text)
                        if res.get("error"):
                            print(_agent_unreachable_msg(s, res["error"])); return
                        if res.get("busy"):
                            print("[busy — try again in a moment]"); return
                        continue
                    if not started:                        # skip replayed backlog until our turn begins
                        if t == "user_turn" and ev.get("speaker") == user and ev.get("text") == text:
                            started = True
                        elif t in ("notice", "busy"):      # command reply → done
                            out = render_event(ev, me=user, verbose=verbose)
                            if out:
                                print(out)
                            return
                        continue
                    out = render_event(ev, me=user, verbose=verbose)
                    if out is not None:
                        print(out)
                    if t == "assistant_final":
                        return
    except Exception as exc:  # noqa: BLE001
        print(_agent_unreachable_msg(s, str(exc)))


def cmd_say(s: Settings, a) -> None:
    asyncio.run(_say_client(s, a.verbose, a.user, " ".join(a.text)))


def cmd_repl(s: Settings, a) -> None:
    print("Conjure REPL — thin client of the agent server (start it: 'python -m conjure.agent_server').\n"
          "Type an instruction ('exit'/'quit' to leave). 'conjure open shell' for deterministic commands "
          "(switch LLM/agent, status, …).")
    asyncio.run(_repl_client(s, a.verbose, a.user))


# --------------------------------------------------------------------------- argparse

def _pos(p):
    p.add_argument("--pos", nargs=3, type=float, metavar=("X", "Y", "Z"), help="position in meters")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="conjure-cli", description="Drive the Conjure world from the terminal.")
    p.add_argument("-v", "--verbose", action="store_true", help="show tool calls and library logs")
    p.add_argument("--user", default=DEFAULT_USER, help="logged-in user (owns spaces/worlds/assets)")
    p.add_argument("--agent", default=None,
                   help="agent to load from agents/<name>/ (default: resume your last-used, else builder)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("world", help="print the current world").set_defaults(fn=cmd_world)

    a = sub.add_parser("reindex", help="embed cataloged assets that have no vector yet")
    a.set_defaults(fn=cmd_reindex)
    a.add_argument("--kind", help="restrict to image | model | skybox | …")

    a = sub.add_parser("retag-skyboxes", help="re-tag wide backfilled images as skyboxes")
    a.set_defaults(fn=cmd_retag_skyboxes)
    a.add_argument("--min-aspect", dest="min_aspect", type=float, help="width/height threshold (default 1.9)")

    sub.add_parser("caption", help="backfill labels for assets with none (image→text via Gemini)") \
        .set_defaults(fn=cmd_caption)

    a = sub.add_parser("add", help="add a primitive shape"); a.set_defaults(fn=cmd_add)
    a.add_argument("shape"); a.add_argument("--color", default="white"); a.add_argument("--name"); _pos(a)

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

    a = sub.add_parser("texture", help="map a generated image onto a room surface"); a.set_defaults(fn=cmd_texture)
    a.add_argument("target", help="floor | ceiling | wall | all | <surface id>")
    a.add_argument("prompt"); a.add_argument("--repeat", type=float, help="tile NxN (use a seamless image)")
    a.add_argument("--generator", help="force an image generator")

    a = sub.add_parser("style", help="color / set transparency of a room surface"); a.set_defaults(fn=cmd_style)
    a.add_argument("target", help="floor | ceiling | wall | all | <surface id>")
    a.add_argument("--color", help="CSS name or #hex"); a.add_argument("--opacity", type=float, help="0..1")

    a = sub.add_parser("annotate", help="toggle / restyle surface metadata labels"); a.set_defaults(fn=cmd_annotate)
    a.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    a.add_argument("--dims", action="store_true", help="also show surface dimensions")
    a.add_argument("--color", help="label text color (CSS name or #hex)")
    a.add_argument("--opacity", type=float, help="label opacity 0..1")

    a = sub.add_parser("edges", help="show/hide / restyle surface outline wireframe"); a.set_defaults(fn=cmd_edges)
    a.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    a.add_argument("--color", help="edge color (CSS name or #hex)")
    a.add_argument("--opacity", type=float, help="edge opacity 0..1")

    sub.add_parser("generators", help="list image generators + capabilities").set_defaults(fn=cmd_generators)

    a = sub.add_parser("edit", help="edit an in-world image"); a.set_defaults(fn=cmd_edit)
    a.add_argument("id"); a.add_argument("prompt")

    a = sub.add_parser("outpaint", help="extend an in-world image wider"); a.set_defaults(fn=cmd_outpaint)
    a.add_argument("id"); a.add_argument("--aspect")

    a = sub.add_parser("skybox-from", help="turn an in-world image into the sky"); a.set_defaults(fn=cmd_skybox_from)
    a.add_argument("id")

    a = sub.add_parser("move", help="move an entity"); a.set_defaults(fn=cmd_move)
    a.add_argument("id"); a.add_argument("pos", nargs=3, type=float, metavar=("X", "Y", "Z"))

    a = sub.add_parser("remove", help="remove an entity"); a.set_defaults(fn=cmd_remove)
    a.add_argument("id")

    a = sub.add_parser("env", help="set sky/fog"); a.set_defaults(fn=cmd_env)
    a.add_argument("--sky-color"); a.add_argument("--fog-color"); a.add_argument("--fog-density", type=float)

    a = sub.add_parser("say", help="run a text instruction through the director"); a.set_defaults(fn=cmd_say)
    a.add_argument("text", nargs="+")

    sub.add_parser("repl", help="interactive director REPL").set_defaults(fn=cmd_repl)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    for name in ("httpx", "anthropic", "mcp", "google_genai"):
        logging.getLogger(name).setLevel(logging.INFO if args.verbose else logging.WARNING)

    settings = get_settings()
    if not _server_ok(settings):
        print(f"World server not reachable at {settings.world_url}. Start it: python -m conjure")
        return 1

    fn = getattr(args, "fn", None) or cmd_repl  # no subcommand → director REPL
    try:
        fn(settings, args)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:  # e.g. no LLM keys for the director (say/repl)
        print(exc)
        return 1
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
